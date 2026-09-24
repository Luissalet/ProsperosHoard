"""Long-form TTS: split plain text into chapters and sentences, synthesize
each sentence as a background job with progress and resume, then assemble
the chapters into one narrated file with an aligned transcript.

Kept free of FastAPI (see AGENTS.md): the job handler takes the same
`(store, backend, job, progress)` shape as every other handler in
`engine.py` and is registered the same way in `api.py`.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

import numpy as np

from . import procutil
from . import voice_engines as ve
from . import voice_lab
from .backend import Backend, ffmpeg_path
from .ids import new_id
from .store import Store
from .util import now_iso

SENTENCE_PAUSE_S = 0.35
CHAPTER_PAUSE_S = 0.9
COMMON_SR = 24000
MAX_TEXT_CHARS = 2_000_000


class AudiobookError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------- text input --

def extract_text_from_file(path: Path) -> str:
    """.txt/.md are read as-is; a .epub has its spine documents' text
    concatenated in reading order (a light HTML strip - good enough for
    narration, not a full EPUB renderer)."""
    ext = path.suffix.lower()
    if ext in (".txt", ".md", ".markdown"):
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".epub":
        return _extract_epub_text(path)
    raise AudiobookError("unsupported_file", f"unsupported source file type '{ext}'; use .txt, .md or .epub")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n\n", html)
    text = _TAG_RE.sub(" ", html)
    import html as _html_mod

    text = _html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines())


def _extract_epub_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            container = zf.read("META-INF/container.xml")
            root = ET.fromstring(container)
            ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            opf_path = root.find(".//c:rootfile", ns).attrib["full-path"]
            opf = ET.fromstring(zf.read(opf_path))
            opf_ns = {"o": "http://www.idpf.org/2007/opf"}
            manifest = {item.attrib["id"]: item.attrib["href"] for item in opf.findall(".//o:manifest/o:item", opf_ns)}
            spine_ids = [ref.attrib["idref"] for ref in opf.findall(".//o:spine/o:itemref", opf_ns)]
            base = Path(opf_path).parent
            parts = []
            for idref in spine_ids:
                href = manifest.get(idref)
                if not href:
                    continue
                doc_path = (base / href).as_posix() if str(base) != "." else href
                try:
                    raw = zf.read(doc_path).decode("utf-8", "replace")
                except KeyError:
                    continue
                parts.append(_html_to_text(raw))
            return "\n\n".join(parts)
    except (KeyError, ET.ParseError, zipfile.BadZipFile, AttributeError) as exc:
        raise AudiobookError("bad_epub", f"could not read the EPUB structure: {exc}") from exc


# ------------------------------------------------------------ chunking -----

_HEADING_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*$")
_CHAPTER_WORD_RE = re.compile(r"^\s{0,3}(chapter|cap[ií]tulo|part|parte)\s+[\divxlcIVXLC]+\b.*$", re.IGNORECASE)


def split_into_chapters(text: str) -> list[dict[str, Any]]:
    """Markdown `#`/`##`/`###` headings, or a line starting "Chapter N" /
    "Capítulo N", start a new chapter; otherwise the whole text is one."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    chapters: list[dict[str, Any]] = []
    current_title: Optional[str] = None
    current_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if body or current_title:
            chapters.append({"title": current_title or f"Chapter {len(chapters) + 1}", "text": body})

    for line in lines:
        heading = _HEADING_RE.match(line)
        is_chapter_word = bool(_CHAPTER_WORD_RE.match(line.strip()))
        if heading or is_chapter_word:
            if current_lines or current_title:
                flush()
            current_title = heading.group(1).strip() if heading else line.strip()
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    if not chapters:
        chapters = [{"title": "Chapter 1", "text": text.strip()}]
    return [c for c in chapters if c["text"]] or chapters[:1]


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])[\"'”’]?\s+(?=[A-Z0-9À-Ü“¿¡])")
_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")


def split_into_sentences(text: str) -> list[str]:
    """A regex sentence splitter (paragraph breaks, then `.`/`!`/`?`/`…`
    followed by whitespace and a capital/opening quote/digit) - not
    linguistically perfect, but good enough to pace narration and to give
    an aligned transcript a sentence per timed line."""
    sentences: list[str] = []
    for para in _PARA_SPLIT_RE.split(text.strip()):
        para = " ".join(para.split())
        if not para:
            continue
        for piece in _SENTENCE_SPLIT_RE.split(para):
            piece = piece.strip()
            if piece:
                sentences.append(piece)
    return sentences


# --------------------------------------------------------------- audio -----

def _wav_bytes_to_float(data: bytes) -> tuple[np.ndarray, int]:
    import io
    import wave

    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        # engines here only ever emit 16-bit PCM; guard rather than misdecode
        raise AudiobookError("bad_audio", f"expected 16-bit PCM audio, got {sampwidth * 8}-bit")
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sr


def _resample(samples: np.ndarray, sr: int, target_sr: int) -> np.ndarray:
    if sr == target_sr or samples.size == 0:
        return samples
    from scipy.signal import resample_poly

    from math import gcd

    g = gcd(sr, target_sr)
    return resample_poly(samples, target_sr // g, sr // g).astype(np.float32)


def _silence(seconds: float, sr: int) -> np.ndarray:
    return np.zeros(max(0, int(round(seconds * sr))), dtype=np.float32)


def synthesize_sentences(
    store: Store, tts_engines: list[Any], voice_spec: dict[str, Any], sentences: list[str],
    pause_s: float = SENTENCE_PAUSE_S, target_sr: int = COMMON_SR,
    on_sentence: Optional[Any] = None,
) -> np.ndarray:
    """One TTS call per sentence, resampled to `target_sr` and joined with a
    short pause. `on_sentence(index, sentence, start_s, end_s)` fires after
    each sentence for callers that build an aligned transcript."""
    chunks: list[np.ndarray] = []
    t = 0.0
    for i, sentence in enumerate(sentences):
        wav_bytes, _ = voice_lab.synthesize_with_spec(store, tts_engines, voice_spec, sentence)
        samples, sr = _wav_bytes_to_float(wav_bytes)
        samples = _resample(samples, sr, target_sr)
        start_s = t
        chunks.append(samples)
        t += len(samples) / target_sr
        if on_sentence:
            on_sentence(i, sentence, start_s, t)
        if pause_s > 0 and i < len(sentences) - 1:
            pad = _silence(pause_s, target_sr)
            chunks.append(pad)
            t += pause_s
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ve.wav_bytes_mono16(samples, sr))


def _ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise AudiobookError("ffmpeg_missing", "ffmpeg not found: install ffmpeg or the imageio-ffmpeg wheel")
    return exe


def _concat_wavs(paths: list[Path], out_path: Path) -> None:
    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in paths), encoding="utf-8")
    try:
        cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
               "-c", "copy", str(out_path)]
        proc = procutil.run(cmd, timeout=600)
        if proc.returncode != 0 or not out_path.is_file():
            raise AudiobookError("concat_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
    finally:
        list_file.unlink(missing_ok=True)


def _ffmetadata_chapters(chapters: list[dict[str, Any]]) -> str:
    lines = [";FFMETADATA1"]
    for ch in chapters:
        start_ms = int(round(ch["start_s"] * 1000))
        end_ms = int(round(ch["end_s"] * 1000))
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={ch['title']}")
    return "\n".join(lines) + "\n"


def encode_final(work_wav: Path, out_path: Path, chapters: Optional[list[dict[str, Any]]] = None) -> None:
    """`out_path`'s suffix picks the container: `.mp3` (plain, loudness
    normalised) or `.m4b` (AAC audiobook container with chapter markers,
    when `chapters` is given)."""
    if out_path.suffix.lower() == ".m4b" and chapters:
        meta_path = out_path.with_suffix(".ffmetadata.txt")
        meta_path.write_text(_ffmetadata_chapters(chapters), encoding="utf-8")
        try:
            cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(work_wav), "-i", str(meta_path),
                   "-map_metadata", "1", "-af", "loudnorm=I=-18:TP=-1.5:LRA=9", "-c:a", "aac", "-b:a", "128k",
                   "-f", "mp4", str(out_path)]
            proc = procutil.run(cmd, timeout=1200)
            if proc.returncode != 0 or not out_path.is_file():
                raise AudiobookError("encode_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
        finally:
            meta_path.unlink(missing_ok=True)
        return
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(work_wav), "-af",
           "loudnorm=I=-18:TP=-1.5:LRA=9", "-c:a", "libmp3lame", "-b:a", "192k", str(out_path)]
    proc = procutil.run(cmd, timeout=1200)
    if proc.returncode != 0 or not out_path.is_file():
        raise AudiobookError("encode_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])


# ------------------------------------------------------------------- job --

def audiobook_job(store: Store, backend: Backend, job: dict[str, Any], progress: Any) -> dict[str, Any]:
    params = job["params"]
    text: str = params["text"]
    if len(text) > MAX_TEXT_CHARS:
        raise AudiobookError("too_long", f"text exceeds {MAX_TEXT_CHARS} characters")
    title = params.get("title") or "Audiobook"
    voice_spec = params["voice"]
    fmt = params.get("format", "mp3")
    project_id = params.get("project_id")

    tts_engines = ve.default_tts_engines(voices_dir=store.data_dir / "voices")
    chapters_text = split_into_chapters(text)

    # resumable: keyed by a hash of the request so retrying the exact same
    # book (after a crash or a cancel) picks up chapters already rendered
    work_dir = store.data_dir / "voice_studio" / "audiobooks" / _content_key(job)
    work_dir.mkdir(parents=True, exist_ok=True)

    total_sentences = sum(max(1, len(split_into_sentences(c["text"]))) for c in chapters_text)
    done_sentences = 0
    chapter_files: list[Path] = []
    chapter_meta: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    t_cursor = 0.0

    for idx, chapter in enumerate(chapters_text):
        progress.check_cancel()
        ch_path = work_dir / f"ch{idx:03d}.wav"
        lines_path = work_dir / f"ch{idx:03d}.lines.json"
        sentences = split_into_sentences(chapter["text"]) or [chapter["text"] or chapter["title"]]
        if ch_path.is_file():
            # resumed from a previous run of the same book: reuse the file,
            # still advance the transcript cursor and progress counters, and
            # restore this chapter's transcript lines from the sidecar saved
            # alongside it the first time - otherwise a resumed chapter's
            # sentences would be missing from the SRT/LRC and sentence_count
            samples, sr = _wav_bytes_to_float(ch_path.read_bytes())
            duration = len(samples) / sr
            done_sentences += len(sentences)
            if lines_path.is_file():
                for line in json.loads(lines_path.read_text(encoding="utf-8")):
                    lines.append({"chapter": idx, "index": line["index"], "text": line["text"],
                                 "start_s": round(t_cursor + line["start_s"], 3),
                                 "end_s": round(t_cursor + line["end_s"], 3)})
        else:
            chapter_lines: list[dict[str, Any]] = []

            def on_sentence(i: int, sentence: str, start_s: float, end_s: float, _ch=idx) -> None:
                entry = {"index": i, "text": sentence, "start_s": round(start_s, 3), "end_s": round(end_s, 3)}
                chapter_lines.append(entry)
                lines.append({"chapter": _ch, "index": i, "text": sentence,
                             "start_s": round(t_cursor + start_s, 3), "end_s": round(t_cursor + end_s, 3)})

            samples = synthesize_sentences(store, tts_engines, voice_spec, sentences, on_sentence=on_sentence)
            _write_wav(ch_path, samples, COMMON_SR)
            lines_path.write_text(json.dumps(chapter_lines, ensure_ascii=False), encoding="utf-8")
            duration = len(samples) / COMMON_SR
            done_sentences += len(sentences)
            progress(min(0.95, done_sentences / max(1, total_sentences)), f"chapter {idx + 1}/{len(chapters_text)}: {chapter['title']}")
        chapter_meta.append({"index": idx, "title": chapter["title"], "start_s": round(t_cursor, 3),
                             "end_s": round(t_cursor + duration, 3), "duration_s": round(duration, 2)})
        chapter_files.append(ch_path)
        t_cursor += duration + CHAPTER_PAUSE_S

    progress(0.96, "assembling")
    final_wav = work_dir / "final.wav"
    if len(chapter_files) == 1:
        final_wav.write_bytes(chapter_files[0].read_bytes())
    else:
        # insert the inter-chapter pause as a tiny silence file so `concat`
        # (stream copy) can join everything without a re-encode
        padded: list[Path] = []
        pad_path = work_dir / "_pause.wav"
        _write_wav(pad_path, _silence(CHAPTER_PAUSE_S, COMMON_SR), COMMON_SR)
        for i, p in enumerate(chapter_files):
            padded.append(p)
            if i < len(chapter_files) - 1:
                padded.append(pad_path)
        _concat_wavs(padded, final_wav)

    ext = ".m4b" if fmt == "m4b" else ".mp3"
    final_out = work_dir / f"final{ext}"
    encode_final(final_wav, final_out, chapters=chapter_meta if fmt == "m4b" else None)

    srt_text = ve.segments_to_srt([{"start_s": l["start_s"], "end_s": l["end_s"], "text": l["text"]} for l in lines])
    (work_dir / "transcript.srt").write_text(srt_text, encoding="utf-8")
    from . import audio as audio_mod

    lrc_text = audio_mod.to_lrc([{"time_s": l["start_s"], "text": l["text"]} for l in lines])
    (work_dir / "transcript.lrc").write_text(lrc_text, encoding="utf-8")

    outputs: dict[str, Any] = {
        "title": title, "chapters": chapter_meta, "sentence_count": len(lines), "format": fmt,
        "duration_s": round(t_cursor - CHAPTER_PAUSE_S, 2) if chapter_files else 0.0,
        "work_dir": _rel(store, work_dir),
        "final_file": _rel(store, final_out), "srt_file": _rel(store, work_dir / "transcript.srt"),
        "lrc_file": _rel(store, work_dir / "transcript.lrc"),
    }

    if project_id:
        from . import engine as engine_mod

        asset_id = new_id("a")
        dest = store.path_for_asset_file(asset_id, ext)
        dest.write_bytes(final_out.read_bytes())
        duration_s = len(chapter_files) and outputs["duration_s"]
        asset = store.create_asset(
            project_id=project_id, kind="audio", file_path=engine_mod._rel(store, dest),
            mime="audio/mp4" if ext == ".m4b" else "audio/mpeg", duration_s=duration_s, source="generated",
            recipe={"operation": "audiobook", "title": title, "chapters": len(chapter_meta), "voice": voice_spec,
                    "created_at": now_iso()},
            asset_id=asset_id, name=title[:80], tags=["audiobook"],
        )
        outputs["asset_ids"] = [asset["id"]]
    return outputs


def _content_key(job: dict[str, Any]) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(job["params"].get("text", "").encode("utf-8", "replace"))
    h.update(repr(job["params"].get("voice")).encode("utf-8"))
    return h.hexdigest()[:24]


def _rel(store: Store, path: Path) -> str:
    return path.relative_to(store.data_dir).as_posix()
