"""Long-form TTS: split plain text into chapters and sentences, synthesize
each sentence as a background job with progress and resume, then assemble
the chapters into one narrated file with an aligned transcript.

Kept free of FastAPI (see AGENTS.md): the job handler takes the same
`(store, backend, job, progress)` shape as every other handler in
`engine.py` and is registered the same way in `api.py`.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import time
import wave
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import unquote
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
# a chapter is one 16-bit mono WAV, and a RIFF file's data size is a 32-bit
# field: past this (~24.8 h at COMMON_SR) the chapter fails clearly instead
# of writing a corrupt file. The book itself has no such limit - the final
# encode reads the chapter files through ffmpeg's concat demuxer.
MAX_CHAPTER_SAMPLES = (2 ** 32 - 1 - 4096) // 2
PROGRESS_INTERVAL_S = 1.0


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
_WS_RE = re.compile(r"[ \t ]+")
_BLOCK_TAG_RE = re.compile(
    r"(?i)</?(?:p|div|li|ul|ol|blockquote|section|article|aside|header|footer|figure|figcaption|table|tr|"
    r"dd|dt|dl|pre|nav|main|body)\b[^>]*>|<hr\b[^>]*>")
_HTML_HEADING_RE = re.compile(r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1\s*>")


def _html_to_text(html: str) -> str:
    """XHTML -> plain text with paragraph breaks: every block element ends
    a paragraph (a blank line), and an `<h1>`-`<h6>` becomes its own
    markdown heading line ("# Title") so `split_into_chapters` sees it -
    otherwise a chapter's heading and first paragraph run together into
    one line and the whole paragraph is taken for the title."""
    import html as _html_mod

    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?is)<(script|style|head)\b[^>]*>.*?</\1\s*>", " ", html)

    def heading(m: re.Match[str]) -> str:
        level = min(3, int(m.group(1)))
        title = " ".join(_html_mod.unescape(_TAG_RE.sub(" ", m.group(2))).split())
        return f"\n\n{'#' * level} {title}\n\n" if title else "\n\n"

    html = _HTML_HEADING_RE.sub(heading, html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = _BLOCK_TAG_RE.sub("\n\n", html)
    text = _TAG_RE.sub(" ", html)
    text = _html_mod.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


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
            names = set(zf.namelist())
            parts = []
            for idref in spine_ids:
                href = manifest.get(idref)
                if not href:
                    continue
                doc_path = epub_member_path(opf_path, href)
                if doc_path not in names:
                    continue
                raw = zf.read(doc_path).decode("utf-8", "replace")
                parts.append(_html_to_text(raw))
            return "\n\n".join(p for p in parts if p)
    except (KeyError, ET.ParseError, zipfile.BadZipFile, AttributeError) as exc:
        raise AudiobookError("bad_epub", f"could not read the EPUB structure: {exc}") from exc


def epub_member_path(opf_path: str, href: str) -> str:
    """A manifest `href` (relative to the OPF, URL-encoded, maybe with a
    `#fragment` or `./`/`../` segments) -> the zip member name."""
    base = posixpath.dirname(opf_path)
    target = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base, target)).lstrip("/")


# ------------------------------------------------------------ chunking -----

MAX_HEADING_CHARS = 200  # a longer "# ..." line is prose that happens to start with "#", not a title
MAX_CHAPTER_LINE_CHARS = 80
MAX_CHAPTER_TITLE_WORDS = 10
_HEADING_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*#*\s*$")
_NUMBER_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    "seventeen|eighteen|nineteen|twenty|first|second|third|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|"
    "nueve|diez|once|doce|trece|catorce|quince|diecis[eé]is|diecisiete|dieciocho|diecinueve|veinte|"
    "primer[oa]|segund[oa]|tercer[oa]"
)
# The chapter word is case-insensitive; a roman numeral must be upper case
# ("Part I", never "part i" / "part I think ...") and the whole line must
# be the heading: the number, then nothing, a separator and a short title,
# or a short capitalised title.
_CHAPTER_WORD_RE = re.compile(
    r"^(?i:chapter|cap[ií]tulo|part|parte|book|libro)\s+"
    r"(?:\d{1,4}|[IVXLCDM]{1,8}|(?i:" + _NUMBER_WORDS + r"))"
    r"(?:\s*[.:\-–—]\s*(?P<sep>.*)|\s+(?P<bare>.*))?$"
)


def chapter_heading(line: str) -> Optional[str]:
    """The chapter title when `line` is, by itself, a heading - a markdown
    `#`/`##`/`###` line, or a short "Chapter 3" / "Capítulo IV: El mar" /
    "Part II" line - else None."""
    heading = _HEADING_RE.match(line)
    if heading:
        title = heading.group(1).strip()
        return title if title and len(title) <= MAX_HEADING_CHARS else None
    s = line.strip()
    if not s or len(s) > MAX_CHAPTER_LINE_CHARS:
        return None
    m = _CHAPTER_WORD_RE.match(s)
    if not m:
        return None
    title = (m.group("sep") if m.group("sep") is not None else m.group("bare") or "").strip()
    if title:
        if len(title.split()) > MAX_CHAPTER_TITLE_WORDS or title[-1] in ".,;:!?…":
            return None  # reads like a sentence, not a title
        if m.group("bare") is not None and not (title[0].isupper() or title[0].isdigit() or title[0] in "\"'“«"):
            return None  # "Part I think he lied" is narration
    return s


def split_into_chapters(text: str) -> list[dict[str, Any]]:
    """Markdown `#`/`##`/`###` headings, or a standalone short line like
    "Chapter 3" / "Capítulo IV" / "Part II: The Sea", start a new chapter;
    otherwise the whole text is one. A heading with no text under it (e.g.
    "Part One" right before "Chapter 1") stays a chapter of its own that
    narrates just its title - nothing in the source is ever dropped."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    chapters: list[dict[str, Any]] = []
    current_title: Optional[str] = None
    current_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if body or current_title:
            chapters.append({"title": current_title or f"Chapter {len(chapters) + 1}", "text": body})

    for line in lines:
        title = chapter_heading(line)
        if title is not None:
            flush()
            current_title = title
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    if not chapters:
        chapters = [{"title": "Chapter 1", "text": text.strip()}]
    return chapters


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


def chapter_sentences(chapter: dict[str, Any]) -> list[str]:
    """What gets narrated for a chapter: its sentences, or its title alone
    when it is a heading with no text of its own."""
    return split_into_sentences(chapter["text"]) or [chapter["text"] or chapter["title"]]


# --------------------------------------------------------------- audio -----

def _wav_bytes_to_float(data: bytes) -> tuple[np.ndarray, int]:
    import io

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


def _to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _iter_sentence_audio(store: Store, tts_engines: list[Any], voice_spec: dict[str, Any], sentences: list[str],
                         target_sr: int, check_cancel: Optional[Callable[[], None]] = None,
                         ) -> Iterator[tuple[int, str, np.ndarray]]:
    for i, sentence in enumerate(sentences):
        if check_cancel:
            check_cancel()
        wav_bytes, _ = voice_lab.synthesize_with_spec(store, tts_engines, voice_spec, sentence)
        samples, sr = _wav_bytes_to_float(wav_bytes)
        yield i, sentence, _resample(samples, sr, target_sr)


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
    for i, sentence, samples in _iter_sentence_audio(store, tts_engines, voice_spec, sentences, target_sr):
        start_s = t
        chunks.append(samples)
        t += len(samples) / target_sr
        if on_sentence:
            on_sentence(i, sentence, start_s, t)
        if pause_s > 0 and i < len(sentences) - 1:
            chunks.append(_silence(pause_s, target_sr))
            t += pause_s
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def render_chapter(
    store: Store, tts_engines: list[Any], voice_spec: dict[str, Any], sentences: list[str], dest: Path,
    pause_s: float = SENTENCE_PAUSE_S, target_sr: int = COMMON_SR,
    on_sentence: Optional[Callable[[int, str, float, float], None]] = None,
    check_cancel: Optional[Callable[[], None]] = None,
) -> float:
    """Synthesize `sentences` straight into a 16-bit mono WAV at `dest`
    (streamed sentence by sentence, never the whole chapter in memory),
    checking for a cancel before each sentence. Written to `dest`.part and
    renamed into place only once complete, so an existing `dest` always
    means a finished chapter. Returns the duration in seconds."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    pause = _to_pcm16(_silence(pause_s, target_sr)) if pause_s > 0 else b""
    pause_n = len(pause) // 2
    total = 0
    try:
        with wave.open(str(part), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(target_sr)
            for i, sentence, samples in _iter_sentence_audio(store, tts_engines, voice_spec, sentences, target_sr,
                                                             check_cancel):
                extra = pause_n if (pause_n and i < len(sentences) - 1) else 0
                if total + len(samples) + extra > MAX_CHAPTER_SAMPLES:
                    raise AudiobookError("chapter_too_long", "a single chapter over ~24 hours of audio exceeds the WAV "
                                                             "size limit; split the text into chapters")
                start_s = total / target_sr
                wf.writeframes(_to_pcm16(samples))
                total += len(samples)
                if on_sentence:
                    on_sentence(i, sentence, start_s, total / target_sr)
                if extra:
                    wf.writeframes(pause)
                    total += pause_n
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return total / target_sr


def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def _write_wav(path: Path, samples: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ve.wav_bytes_mono16(samples, sr))


def _ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise AudiobookError("ffmpeg_missing", "ffmpeg not found: install ffmpeg or the imageio-ffmpeg wheel")
    return exe


def _concat_wavs(paths: list[Path], out_path: Path) -> None:
    # Entries are bare file names (every wav lives next to the list file,
    # in the same work directory) via video.concat_list_text, which escapes
    # any embedded single quote the concat-demuxer way - never the resolved
    # absolute path, which on the real Windows install lives under a folder
    # named "Prospero's Hoard" and would otherwise break the demuxer's own
    # quoting.
    from .video import concat_list_text

    list_file = out_path.with_suffix(".concat.txt")
    list_file.write_text(concat_list_text(paths), encoding="utf-8")
    try:
        cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file.name,
               "-c", "copy", out_path.name]
        proc = procutil.run(cmd, timeout=600, cwd=str(out_path.parent))
        if proc.returncode != 0 or not out_path.is_file():
            raise AudiobookError("concat_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
    finally:
        list_file.unlink(missing_ok=True)


def _ffmetadata_chapters(chapters: list[dict[str, Any]]) -> str:
    lines = [";FFMETADATA1"]
    for ch in chapters:
        start_ms = int(round(ch["start_s"] * 1000))
        end_ms = int(round(ch["end_s"] * 1000))
        title = re.sub(r"([=;#\\\n])", r"\\\1", str(ch["title"]))
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={title}")
    return "\n".join(lines) + "\n"


FINAL_SAMPLE_RATE = 44100  # a safe, universally-supported rate for the mp3/m4b encoders below


def encode_final(work_wav: Path, out_path: Path, chapters: Optional[list[dict[str, Any]]] = None) -> None:
    """`out_path`'s suffix picks the container: `.mp3` (plain, loudness
    normalised) or `.m4b` (AAC audiobook container with chapter markers,
    when `chapters` is given). One input file; see `encode_parts` for the
    chapter-by-chapter path the audiobook job uses."""
    encode_parts([work_wav], out_path, chapters)


def encode_parts(parts: list[Path], out_path: Path, chapters: Optional[list[dict[str, Any]]] = None) -> None:
    """Encode the WAVs in `parts` (all in `out_path`'s folder), in order,
    as one file, read through ffmpeg's concat demuxer so no single joined
    WAV (and its 4 GB RIFF limit) is ever written.

    Both formats explicitly resample to `FINAL_SAMPLE_RATE` after `loudnorm`:
    that filter's single-pass true-peak limiting emits at 192kHz regardless
    of the input rate, and left alone each encoder then picks its own
    nearest supported rate from *that* (observed: 48kHz for libmp3lame,
    96kHz for aac) rather than a small, predictable, universally-compatible
    rate for narrated speech.
    """
    from .video import concat_list_text

    work_dir = out_path.parent
    if any(p.parent.resolve() != work_dir.resolve() for p in parts):
        raise AudiobookError("encode_failed", "every part must live next to the output file")
    list_file = work_dir / f"{out_path.stem}.parts.txt"
    list_file.write_text(concat_list_text(parts), encoding="utf-8")
    meta_path = work_dir / f"{out_path.stem}.ffmetadata.txt"
    loudnorm = ["-af", "loudnorm=I=-18:TP=-1.5:LRA=9", "-ar", str(FINAL_SAMPLE_RATE)]
    base = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file.name]
    try:
        if out_path.suffix.lower() == ".m4b" and chapters:
            meta_path.write_text(_ffmetadata_chapters(chapters), encoding="utf-8")
            cmd = base + ["-i", meta_path.name, "-map", "0:a", "-map_metadata", "1", *loudnorm,
                          "-c:a", "aac", "-b:a", "128k", "-f", "mp4", out_path.name]
        elif out_path.suffix.lower() == ".m4b":
            cmd = base + [*loudnorm, "-c:a", "aac", "-b:a", "128k", "-f", "mp4", out_path.name]
        else:
            cmd = base + [*loudnorm, "-c:a", "libmp3lame", "-b:a", "192k", out_path.name]
        proc = procutil.run(cmd, timeout=6 * 3600, cwd=str(work_dir))
        if proc.returncode != 0 or not out_path.is_file():
            raise AudiobookError("encode_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
    finally:
        list_file.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)


# ------------------------------------------------------------------- job --

class _Throttled:
    """Progress reporting at most every `PROGRESS_INTERVAL_S` (a book can
    have tens of thousands of sentences; one DB write each is wasteful)."""

    def __init__(self, progress: Any):
        self.progress = progress
        self.last = 0.0

    def __call__(self, fraction: float, message: str, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self.last >= PROGRESS_INTERVAL_S:
            self.last = now
            self.progress(fraction, message)


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
    check_cancel = getattr(progress, "check_cancel", None)
    report = _Throttled(progress)

    # resumable: keyed by a hash of the request so retrying the exact same
    # book (after a crash or a cancel) picks up chapters already rendered
    work_dir = store.data_dir / "voice_studio" / "audiobooks" / _content_key(job, store)
    work_dir.mkdir(parents=True, exist_ok=True)

    chapter_sentences_list = [chapter_sentences(c) for c in chapters_text]
    total_sentences = sum(len(s) for s in chapter_sentences_list)
    done_sentences = 0
    chapter_files: list[Path] = []
    chapter_meta: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    t_cursor = 0.0

    for idx, chapter in enumerate(chapters_text):
        if check_cancel:
            check_cancel()
        ch_path = work_dir / f"ch{idx:03d}.wav"
        lines_path = work_dir / f"ch{idx:03d}.lines.json"
        sentences = chapter_sentences_list[idx]
        if ch_path.is_file() and lines_path.is_file():
            # resumed from a previous run of the same book: reuse the file,
            # still advance the transcript cursor and progress counters, and
            # restore this chapter's transcript lines from the sidecar saved
            # before it (the wav is only renamed into place after the
            # sidecar is written, so the pair is always complete)
            duration = _wav_duration_s(ch_path)
            done_sentences += len(sentences)
            for line in json.loads(lines_path.read_text(encoding="utf-8")):
                lines.append({"chapter": idx, "index": line["index"], "text": line["text"],
                             "start_s": round(t_cursor + line["start_s"], 3),
                             "end_s": round(t_cursor + line["end_s"], 3)})
        else:
            chapter_lines: list[dict[str, Any]] = []
            base_done = done_sentences

            def on_sentence(i: int, sentence: str, start_s: float, end_s: float, _idx=idx, _title=chapter["title"],
                            _base=base_done, _lines=chapter_lines) -> None:
                _lines.append({"index": i, "text": sentence, "start_s": round(start_s, 3), "end_s": round(end_s, 3)})
                report(min(0.95, (_base + i + 1) / max(1, total_sentences)),
                       f"chapter {_idx + 1}/{len(chapters_text)}: {_title} - sentence {i + 1}")

            # the sidecar goes first under a temporary name; the chapter wav
            # (the resume marker) is renamed into place by render_chapter
            # only after it is complete, then the sidecar is finalised
            lines_part = lines_path.with_name(lines_path.name + ".part")
            ch_path.unlink(missing_ok=True)
            duration = render_chapter(store, tts_engines, voice_spec, sentences, work_dir / f"ch{idx:03d}.tmp.wav",
                                      on_sentence=on_sentence, check_cancel=check_cancel)
            lines_part.write_text(json.dumps(chapter_lines, ensure_ascii=False), encoding="utf-8")
            os.replace(lines_part, lines_path)
            os.replace(work_dir / f"ch{idx:03d}.tmp.wav", ch_path)
            for line in chapter_lines:
                lines.append({"chapter": idx, "index": line["index"], "text": line["text"],
                             "start_s": round(t_cursor + line["start_s"], 3),
                             "end_s": round(t_cursor + line["end_s"], 3)})
            done_sentences += len(sentences)
            report(min(0.95, done_sentences / max(1, total_sentences)),
                   f"chapter {idx + 1}/{len(chapters_text)}: {chapter['title']}", force=True)
        chapter_meta.append({"index": idx, "title": chapter["title"], "start_s": round(t_cursor, 3),
                             "end_s": round(t_cursor + duration, 3), "duration_s": round(duration, 2)})
        chapter_files.append(ch_path)
        t_cursor += duration + CHAPTER_PAUSE_S

    progress(0.96, "assembling")
    # the inter-chapter pause is a tiny silence file between the chapters,
    # and the encoder reads them all through the concat demuxer: no single
    # joined WAV (with its 4 GB RIFF limit) is ever written
    parts: list[Path] = []
    pad_path = work_dir / "_pause.wav"
    if len(chapter_files) > 1:
        _write_wav(pad_path, _silence(CHAPTER_PAUSE_S, COMMON_SR), COMMON_SR)
    for i, p in enumerate(chapter_files):
        parts.append(p)
        if i < len(chapter_files) - 1:
            parts.append(pad_path)

    ext = ".m4b" if fmt == "m4b" else ".mp3"
    final_out = work_dir / f"final{ext}"
    encode_parts(parts, final_out, chapters=chapter_meta if fmt == "m4b" else None)

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
        shutil.copyfile(final_out, dest)
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


def audiobook_outputs_view(outputs: dict[str, Any], max_chapters: int = 50) -> dict[str, Any]:
    """Compact, path-free summary of a finished audiobook job's outputs for
    agents: chapters (index, title, start, duration), counts and asset ids."""
    chapters = outputs.get("chapters") or []
    view = {
        "title": outputs.get("title"), "format": outputs.get("format"), "duration_s": outputs.get("duration_s"),
        "sentence_count": outputs.get("sentence_count"), "chapter_count": len(chapters),
        "chapters": [{"index": c.get("index"), "title": str(c.get("title") or "")[:120], "start_s": c.get("start_s"),
                      "duration_s": c.get("duration_s")} for c in chapters[:max_chapters]],
        "asset_ids": outputs.get("asset_ids") or [],
    }
    if len(chapters) > max_chapters:
        view["chapters_truncated"] = True
    return view


def _content_key(job: dict[str, Any], store: Optional[Store] = None) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(job["params"].get("text", "").encode("utf-8", "replace"))
    voice = job["params"].get("voice")
    h.update(repr(voice).encode("utf-8"))
    # a library voice's own lexicon changes what is said: fold it in (only
    # when there is one, so existing work directories keep their keys)
    if store is not None and isinstance(voice, dict) and voice.get("voice_id"):
        try:
            lexicon = voice_lab.voice_lexicon(store.get_studio_voice(voice["voice_id"]))
        except Exception:  # noqa: BLE001 - an unknown voice fails later, at synthesis, with a clear error
            lexicon = {}
        if lexicon:
            h.update(repr(sorted(lexicon.items())).encode("utf-8"))
    return h.hexdigest()[:24]


def _rel(store: Store, path: Path) -> str:
    return path.relative_to(store.data_dir).as_posix()
