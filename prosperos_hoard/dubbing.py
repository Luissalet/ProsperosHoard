"""Video dubbing: extract audio, transcribe it with timestamps, translate
each segment with the local LLM (through Hoard Link, never edited - see
`hoard_link/VENDORED.txt`), synthesize each in the target voice, time-fit it
back onto the original segment, mix it under (or over) the original track,
and mux a new video with target-language subtitles.

Every stage's files are kept under the job's work directory so one segment
can be inspected or re-synthesized without re-running the rest (see
`resynthesize_segment`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import wave
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from . import audio as audio_mod
from . import procutil
from . import voice_engines as ve
from . import voice_lab
from .backend import Backend, ffmpeg_path
from .ids import new_id
from .store import Store
from .util import now_iso

ChatFn = Callable[[list[dict[str, Any]]], str]

MIN_ATEMPO, MAX_ATEMPO = 0.5, 2.0
MAX_OVERALL_FACTOR = 4.0  # two chained atempo stages cover up to 4x
# A line that is shorter than its slot is never stretched to fill it (a
# half-second "Sí." slowed to 0.25x is unintelligible): it is slowed by at
# most this much and the rest of the slot is silence.
MIN_FIT_FACTOR = 0.9
DUCK_DB = -18.0
DUCK_RAMP_S = 0.08  # fade the original in/out around each dubbed line instead of a hard step
MIX_SAMPLE_RATE = 44100  # every mix input is decoded to this rate
MIX_CHUNK_S = 10.0  # the mix is streamed in chunks of this length, never the whole track in memory
SILENCE_THRESHOLD = 10 ** (-45 / 20)  # -45 dBFS: quieter than this at the ends of a TTS clip is trimmed
MAX_SEGMENT_TEXT_TOKENS = 2048


class DubbingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise DubbingError("ffmpeg_missing", "ffmpeg not found: install ffmpeg or the imageio-ffmpeg wheel")
    return exe


# ------------------------------------------------------------ time-fit math

def atempo_chain(factor: float) -> list[str]:
    """`factor` (source_duration / target_duration - >1 means "speed up")
    as a chain of ffmpeg `atempo` filters, each within the 0.5-2.0 range
    every ffmpeg version accepts. Pure and unit-tested without ffmpeg."""
    factor = max(1.0 / MAX_OVERALL_FACTOR, min(MAX_OVERALL_FACTOR, factor))
    if abs(factor - 1.0) < 1e-6:
        return []
    return [f"atempo={s:.6f}" for s in voice_lab._atempo_stages(factor)]


def compute_time_fit_factor(source_duration_s: float, target_duration_s: float) -> float:
    if source_duration_s <= 0 or target_duration_s <= 0:
        return 1.0
    return source_duration_s / target_duration_s


def applied_fit_factor(requested: float) -> float:
    """The tempo factor actually applied for a requested one: speed up as
    much as needed (up to MAX_OVERALL_FACTOR), slow down by at most
    MIN_FIT_FACTOR - a short line keeps its natural pace and is padded."""
    return max(MIN_FIT_FACTOR, min(MAX_OVERALL_FACTOR, requested))


def trim_silence(samples: np.ndarray, sr: int, threshold: float = SILENCE_THRESHOLD,
                 keep_s: float = 0.03) -> np.ndarray:
    """Drop leading/trailing near-silence (TTS engines pad their output),
    keeping `keep_s` of margin so consonants are not clipped."""
    if samples.size == 0:
        return samples
    loud = np.flatnonzero(np.abs(samples) > threshold)
    if loud.size == 0:
        return samples[:0]
    keep = int(keep_s * sr)
    return samples[max(0, loud[0] - keep): min(samples.size, loud[-1] + 1 + keep)]


def _decode(path: Path, sr: int = MIX_SAMPLE_RATE) -> np.ndarray:
    """Any audio file -> mono float32 at `sr`: a plain 16-bit WAV already at
    `sr` is read directly, anything else goes through ffmpeg."""
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() == 2 and wf.getframerate() == sr:
                ch = wf.getnchannels()
                data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
                return data.reshape(-1, ch).mean(axis=1) if ch > 1 else data
    except (wave.Error, EOFError):
        pass
    cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"]
    proc = procutil.run(cmd, timeout=600)
    if proc.returncode != 0:
        raise DubbingError("decode_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
    return np.frombuffer(proc.stdout, dtype="<f4").copy()


def _write_wav16(path: Path, samples: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part" + path.suffix)
    tmp.write_bytes(ve.wav_bytes_mono16(samples.astype(np.float32), sr))
    os.replace(tmp, path)


def fit_audio_to_duration(src_wav: Path, target_duration_s: float, dest_wav: Path,
                          sample_rate: int = MIX_SAMPLE_RATE) -> dict[str, Any]:
    """Fit a synthesized line onto its original segment's duration: trim
    the engine's leading/trailing silence, speed it up with `atempo` when
    it is too long (slow it down by at most 10% when it is short), then pad
    with silence or trim to land exactly on `target_duration_s`. The result
    is a 16-bit mono WAV at `sample_rate`, ready for `mix_dub_audio`."""
    samples = trim_silence(_decode(src_wav, sample_rate), sample_rate)
    source_duration_s = samples.size / sample_rate
    factor = compute_time_fit_factor(source_duration_s, target_duration_s)
    applied = applied_fit_factor(factor) if source_duration_s > 0 else 1.0
    chain = atempo_chain(applied)
    if chain:
        dest_wav.parent.mkdir(parents=True, exist_ok=True)
        trimmed = dest_wav.with_name(dest_wav.stem + ".trim.wav")
        try:
            _write_wav16(trimmed, samples, sample_rate)
            cmd = [_ffmpeg(), "-nostdin", "-loglevel", "error", "-i", str(trimmed), "-af", ",".join(chain),
                   "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]
            proc = procutil.run(cmd, timeout=120)
            if proc.returncode != 0:
                raise DubbingError("time_fit_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
            samples = np.frombuffer(proc.stdout, dtype="<f4")
        finally:
            trimmed.unlink(missing_ok=True)
    target_n = max(1, int(round(target_duration_s * sample_rate)))
    fitted = np.zeros(target_n, dtype=np.float32)
    n = min(target_n, samples.size)
    fitted[:n] = samples[:n]
    _write_wav16(dest_wav, fitted, sample_rate)
    return {"source_duration_s": round(source_duration_s, 3), "target_duration_s": round(target_duration_s, 3),
            "requested_factor": round(factor, 4), "applied_factor": round(applied, 4),
            "clamped": abs(applied - factor) > 1e-9 and source_duration_s > 0,
            "cut": samples.size > target_n}


# --------------------------------------------------------------- audio I/O

def extract_audio(video_path: Path, out_wav: Path, sr: int = MIX_SAMPLE_RATE) -> None:
    """The video's audio as mono WAV at `out_wav` - written to a temporary
    name and renamed into place, so an existing `out_wav` is always complete
    (the dub job reuses it on a retry)."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    part = out_wav.with_name(out_wav.stem + ".part" + out_wav.suffix)
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(video_path), "-vn", "-ac", "1", "-ar",
           str(sr), str(part)]
    try:
        proc = procutil.run(cmd, timeout=600)
        if proc.returncode != 0 or not part.is_file():
            raise DubbingError("extract_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
        os.replace(part, out_wav)
    finally:
        part.unlink(missing_ok=True)


def demucs_installed() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("demucs") is not None
    except (ImportError, ValueError):
        return False


def _existing_stems(audio_path: Path, out_dir: Path) -> Optional[dict[str, Path]]:
    for candidate in sorted(out_dir.glob(f"*/{audio_path.stem}/no_vocals.wav")):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return {"vocals": candidate.parent / "vocals.wav", "no_vocals": candidate}
    return None


def separate_background(audio_path: Path, out_dir: Path) -> Optional[dict[str, Path]]:
    """Background (no-vocals) stem via `demucs` (2-stem), reusing one a
    previous run already produced in `out_dir`. Returns None when demucs is
    not installed or fails, so the caller ducks the original track instead
    of replacing it. Runs `python -m demucs` with this app's own
    interpreter (a bare "demucs" is often not on PATH, e.g. on Windows)."""
    if out_dir.is_dir():
        existing = _existing_stems(audio_path, out_dir)
        if existing:
            return existing
    if not demucs_installed():
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "demucs", "--two-stems", "vocals", "-o", str(out_dir), str(audio_path)]
    try:
        proc = procutil.run(cmd, timeout=1800)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _existing_stems(audio_path, out_dir)


def duck_gain(start: int, length: int, spans: list[tuple[int, int]], duck_gain_linear: float, ramp: int) -> np.ndarray:
    """The background's gain for samples [start, start+length): 1.0, dipping
    to `duck_gain_linear` inside each (start, end) span with a linear ramp
    of `ramp` samples on either side. Pure numpy, unit-tested."""
    gain = np.ones(length, dtype=np.float32)
    end = start + length
    idx = None
    for s0, s1 in spans:
        lo, hi = s0 - ramp, s1 + ramp
        if hi <= start or lo >= end:
            continue
        if idx is None:
            idx = np.arange(start, end, dtype=np.float64)
        a, b = max(lo, start) - start, min(hi, end) - start
        pos = idx[a:b]
        w = np.ones(b - a, dtype=np.float64)
        if ramp > 0:
            w = np.minimum(w, np.clip((pos - lo) / ramp, 0.0, 1.0))
            w = np.minimum(w, np.clip((hi - pos) / ramp, 0.0, 1.0))
        seg_gain = (1.0 - (1.0 - duck_gain_linear) * w).astype(np.float32)
        gain[a:b] = np.minimum(gain[a:b], seg_gain)
    return gain


def _stream_decode(path: Path, sr: int, chunk: int):
    """Yield `path` as mono float32 chunks of `chunk` samples at `sr`."""
    cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"]
    proc = procutil.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            data = proc.stdout.read(chunk * 4)
            if not data:
                break
            usable = len(data) - len(data) % 4
            yield np.frombuffer(data[:usable], dtype="<f4")
        if proc.wait(timeout=60) != 0:
            raise DubbingError("mix_failed", f"could not decode {path.name}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def mix_dub_audio(original_audio: Path, dub_clip_paths: list[Path], segments: list[dict[str, Any]],
                  out_path: Path, duck_db: float = DUCK_DB, sample_rate: int = MIX_SAMPLE_RATE) -> None:
    """Mix each dubbed clip in at its segment's start over the original (or
    separated background) track, ducking the track under each segment, then
    loudness-normalise the result with one ffmpeg call.

    The mix itself is numpy, streamed in `MIX_CHUNK_S` chunks: one ffmpeg
    decoder for the background, each clip read (16-bit WAV at
    `sample_rate`, as `fit_audio_to_duration` writes them) only while it
    overlaps the current chunk - so hundreds of segments never become
    hundreds of ffmpeg inputs (command-line and file-descriptor limits),
    and nothing depends on filter options newer ffmpeg builds added (the
    bundled 4.2 has no `amix normalize=`). `segments`: [{"start_s", "end_s"}]
    in the same order as `dub_clip_paths`."""
    if len(dub_clip_paths) != len(segments):
        raise DubbingError("mix_failed", "one dub clip per segment is required")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    chunk = int(MIX_CHUNK_S * sample_rate)
    ramp = int(DUCK_RAMP_S * sample_rate)
    g = 10 ** (duck_db / 20)
    order = sorted(range(len(segments)), key=lambda i: segments[i]["start_s"])
    clips = [(max(0, int(round(segments[i]["start_s"] * sample_rate))), dub_clip_paths[i]) for i in order]
    spans = [(max(0, int(round(s["start_s"] * sample_rate))), max(0, int(round(s["end_s"] * sample_rate))))
             for s in segments]
    raw = out_path.with_name(out_path.stem + ".mix.f32")
    loaded: dict[int, np.ndarray] = {}
    next_clip = 0
    pos = 0

    def add_clips(buf: np.ndarray, start: int) -> None:
        nonlocal next_clip
        end = start + buf.size
        while next_clip < len(clips) and clips[next_clip][0] < end:
            loaded[next_clip] = _decode(clips[next_clip][1], sample_rate)
            next_clip += 1
        for k in list(loaded):
            c0 = clips[k][0]
            data = loaded[k]
            c1 = c0 + data.size
            if c1 <= start:
                del loaded[k]
                continue
            a, b = max(c0, start), min(c1, end)
            if b > a:
                buf[a - start:b - start] += data[a - c0:b - c0]

    try:
        with raw.open("wb") as fh:
            for bg in _stream_decode(original_audio, sample_rate, chunk):
                buf = bg * duck_gain(pos, bg.size, spans, g, ramp)
                add_clips(buf, pos)
                fh.write(buf.astype("<f4").tobytes())
                pos += bg.size
            # a line that runs past the end of the original track still plays out
            while next_clip < len(clips) or loaded:
                last_end = max([clips[k][0] + loaded[k].size for k in loaded] +
                               [clips[k][0] + 1 for k in range(next_clip, len(clips))])
                if last_end <= pos:
                    break
                buf = np.zeros(min(chunk, last_end - pos), dtype=np.float32)
                add_clips(buf, pos)
                fh.write(buf.astype("<f4").tobytes())
                pos += buf.size
        if pos == 0:
            raise DubbingError("mix_failed", f"{original_audio.name} has no audio")
        # loudnorm's single-pass true-peak limiting emits at 192kHz
        # regardless of its input; force the output back to `sample_rate`
        cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-f", "f32le", "-ar", str(sample_rate), "-ac", "1",
               "-i", raw.name, "-af", "loudnorm=I=-18:TP=-1.5:LRA=9", "-ar", str(sample_rate), "-ac", "1",
               out_path.name]
        proc = procutil.run(cmd, timeout=1800, cwd=str(out_path.parent))
        if proc.returncode != 0 or not out_path.is_file():
            raise DubbingError("mix_failed", (proc.stderr or b"").decode("utf-8", "replace")[:800])
    finally:
        raw.unlink(missing_ok=True)


def mux_video(video_path: Path, dub_audio_path: Path, out_path: Path, srt_path: Optional[Path] = None) -> None:
    """The original video stream (copied) with the dubbed audio and, when
    given, the target-language subtitles as a `mov_text` track. No
    `-shortest`: with a subtitle stream that would end the file at the last
    cue; the dub mix already has the original track's length."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(video_path), "-i", str(dub_audio_path)]
    maps = ["-map", "0:v:0", "-map", "1:a:0"]
    if srt_path and srt_path.is_file() and srt_path.stat().st_size > 0:
        cmd += ["-i", str(srt_path)]
        maps += ["-map", "2:s:0", "-c:s", "mov_text"]
    cmd += maps + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-f", "mp4", str(part)]
    try:
        proc = procutil.run(cmd, timeout=1800)
        if proc.returncode != 0 or not part.is_file():
            raise DubbingError("mux_failed", (proc.stderr or b"").decode("utf-8", "replace")[:800])
        os.replace(part, out_path)
    finally:
        part.unlink(missing_ok=True)


# --------------------------------------------------------------- translation

def build_translation_messages(text: str, target_lang: str, source_lang: Optional[str],
                               glossary: Optional[dict[str, str]]) -> list[dict[str, Any]]:
    glossary_line = ""
    if glossary:
        pairs = "; ".join(f"{k} -> {v}" for k, v in list(glossary.items())[:50])
        glossary_line = f" Use this glossary for names/terms: {pairs}."
    source_line = f" The source language is {source_lang}." if source_lang else ""
    system = (
        f"You translate video dialogue for dubbing. Translate into {target_lang}.{source_line}{glossary_line} "
        "Keep the translation about the same length (in spoken duration) as the source line so it can be "
        "timed back onto the original clip - prefer a shorter, natural phrasing over a longer literal one. "
        "Reply with only the translated line, no quotes, no notes, no explanation."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


def translation_max_tokens(text: str) -> int:
    """A reply budget that scales with the line (a token is roughly 3-4
    characters of Latin-script text; CJK can be ~1 per character), so a
    long segment is never cut off mid-sentence."""
    return max(200, min(MAX_SEGMENT_TEXT_TOKENS, 2 * len(text or "") + 64))


_WRAPPING_QUOTE_PAIRS = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("«", "»")]


def _clean_translated_line(text: str) -> str:
    """Defend against a garbled reply despite the prompt's "no quotes, no
    notes" instruction: collapse embedded newlines/repeated whitespace to
    single spaces (an embedded blank line would otherwise break the SRT
    block format), then strip one matching pair of quotes wrapping the
    *whole* line - not a quote that is only part of the dialogue itself."""
    text = " ".join(text.split())
    if len(text) >= 2:
        for open_q, close_q in _WRAPPING_QUOTE_PAIRS:
            if text[0] == open_q and text[-1] == close_q:
                text = text[1:-1].strip()
                break
    return text


def translate_segments(chat_fn: ChatFn, segments: list[dict[str, Any]], target_lang: str,
                       source_lang: Optional[str] = None, glossary: Optional[dict[str, str]] = None,
                       on_segment: Optional[Callable[[int, int], None]] = None) -> list[str]:
    """One LLM call per segment (through Hoard Link's `chat`, injected as
    `chat_fn` so this stays testable with a fake). `on_segment(i, total)`
    runs before each call (progress, and a cancel check that raises).
    Raises whatever `chat_fn` raises - typically
    `hoard_link.errors.Unavailable` when no local LLM is reachable, which
    the job handler surfaces as a clear failure."""
    out = []
    for i, seg in enumerate(segments):
        if on_segment:
            on_segment(i, len(segments))
        messages = build_translation_messages(seg["text"], target_lang, source_lang, glossary)
        translated = chat_fn(messages)
        out.append(_clean_translated_line(translated or "") or seg["text"])
    return out


# ------------------------------------------------------------------- job --

def _work_dir(store: Store, key: str) -> Path:
    return store.data_dir / "voice_studio" / "dub" / key


def _content_key(job: dict[str, Any]) -> str:
    import hashlib

    params = job["params"]
    h = hashlib.sha256()
    h.update(repr(sorted(params.items())).encode("utf-8", "replace"))
    return h.hexdigest()[:24]


def _rel(store: Store, path: Path) -> str:
    return path.relative_to(store.data_dir).as_posix()


def target_language_code(target_language: Any) -> str:
    """The dub's target language as a canonical code, or DubbingError."""
    try:
        code = ve.normalize_language(target_language)
    except ve.UnsupportedLanguage as exc:
        raise DubbingError("bad_language", str(exc)) from None
    if not code:
        raise DubbingError("bad_language", "give target_language (e.g. 'es' or 'Spanish')")
    return code


def voice_spec_for_language(voice_spec: Optional[dict[str, Any]], language_code: Optional[str]) -> dict[str, Any]:
    """The dub's voice speaks the target language: the spec's own
    `language` wins, otherwise the target language (never the library
    voice's own language, which is the language its sample was recorded in)."""
    spec = dict(voice_spec or {})
    if not spec.get("language") and language_code:
        spec["language"] = language_code
    return spec


def dub_job(store: Store, backend: Backend, job: dict[str, Any], progress: Any) -> dict[str, Any]:
    params = job["params"]
    video_path = Path(params["video_path"])
    target_code = target_language_code(params["target_language"])
    target_name = ve.language_name(target_code) or target_code
    try:
        source_lang = ve.normalize_language(params.get("source_language"), allow_auto=True)
    except ve.UnsupportedLanguage as exc:
        raise DubbingError("bad_language", str(exc)) from None
    glossary = params.get("glossary") or {}
    voice_spec = voice_spec_for_language(params["voice"], target_code)
    project_id = params.get("project_id")
    title = params.get("title") or video_path.stem
    check_cancel = getattr(progress, "check_cancel", None)

    if not video_path.is_file():
        raise DubbingError("video_not_found", f"video not found: {video_path.name}")

    work_dir = _work_dir(store, _content_key(job))
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "manifest.json"

    progress(0.02, "extracting audio")
    original_audio = work_dir / "original.wav"
    if not original_audio.is_file():
        extract_audio(video_path, original_audio)

    progress(0.08, "transcribing")
    stt_engines = ve.default_stt_engines()
    stt_engine = ve.best_installed_stt(stt_engines, prefer=params.get("stt_engine_id"))
    if stt_engine is None:
        raise DubbingError("stt_not_installed", "no speech-to-text engine is installed; "
                                                "install faster-whisper (pip install faster-whisper)")
    transcript = stt_engine.transcribe(original_audio, language=source_lang)
    segments = [{"index": i, "start_s": s["start_s"], "end_s": s["end_s"], "source_text": s["text"]}
                for i, s in enumerate(transcript["segments"])]
    if not segments:
        raise DubbingError("no_speech", "no speech was detected in this video's audio track")
    detected_source_lang = source_lang or transcript.get("language")

    progress(0.2, f"translating {len(segments)} segment(s) to {target_name}")

    def chat_fn(messages: list[dict[str, Any]]) -> str:
        result = backend.link.sync.chat(messages=messages, max_tokens=translation_max_tokens(messages[-1]["content"]),
                                        temperature=0.3)
        return result.text

    def on_translate(i: int, total: int) -> None:
        progress(0.2 + 0.1 * (i / max(1, total)), f"translating segment {i + 1}/{total}")

    translations = translate_segments(chat_fn, [{"text": s["source_text"]} for s in segments], target_name,
                                      ve.language_name(detected_source_lang) if detected_source_lang else None,
                                      glossary, on_segment=on_translate)
    for seg, tr in zip(segments, translations):
        seg["translated_text"] = tr

    tts_engines = ve.default_tts_engines(voices_dir=store.data_dir / "voices")
    seg_dir = work_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    for i, seg in enumerate(segments):
        progress(0.3 + 0.5 * (i / len(segments)), f"voicing segment {i + 1}/{len(segments)}")
        _synthesize_and_fit_segment(store, tts_engines, voice_spec, seg, seg_dir)

    _write_manifest(manifest_path, {
        "title": title, "target_language": target_code, "source_language": detected_source_lang,
        "glossary": glossary, "voice": voice_spec, "video_path": str(video_path), "segments": segments,
        "project_id": project_id, "job_id": job.get("id"), "created_at": now_iso(),
    })

    if check_cancel:
        check_cancel()
    progress(0.82, "mixing audio")
    outputs = _assemble_dub(store, backend, work_dir, video_path, segments, project_id, title, progress)
    outputs["target_language"] = target_code
    outputs["work_dir"] = _rel(store, work_dir)
    outputs["manifest"] = _rel(store, manifest_path)
    outputs["segments"] = [_segment_row(s) for s in segments]
    return outputs


def _segment_row(seg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in seg.items() if k != "fitted_path"}


def _synthesize_and_fit_segment(store: Store, tts_engines: list[Any], voice_spec: dict[str, Any],
                                seg: dict[str, Any], seg_dir: Path) -> None:
    raw_wav = seg_dir / f"seg{seg['index']:04d}.raw.wav"
    fitted_wav = seg_dir / f"seg{seg['index']:04d}.fit.wav"
    wav_bytes, engine_id = voice_lab.synthesize_with_spec(store, tts_engines, voice_spec, seg["translated_text"])
    raw_wav.write_bytes(wav_bytes)
    target_duration = max(0.05, seg["end_s"] - seg["start_s"])
    fit_info = fit_audio_to_duration(raw_wav, target_duration, fitted_wav)
    seg["engine_id"] = engine_id
    seg["fitted_path"] = fitted_wav
    seg["fit"] = fit_info


def _assemble_dub(store: Store, backend: Backend, work_dir: Path, video_path: Path, segments: list[dict[str, Any]],
                  project_id: Optional[str], title: str, progress: Any) -> dict[str, Any]:
    original_audio = work_dir / "original.wav"
    background_dir = work_dir / "background"
    separated = separate_background(original_audio, background_dir)
    bg_audio = separated["no_vocals"] if separated else original_audio

    mixed_audio = work_dir / "dub_mix.wav"
    fitted_paths = [Path(s["fitted_path"]) for s in segments]
    mix_dub_audio(bg_audio, fitted_paths, segments, mixed_audio)

    srt_path = work_dir / "subtitles.srt"
    srt_path.write_text(
        ve.segments_to_srt([{"start_s": s["start_s"], "end_s": s["end_s"], "text": s["translated_text"]} for s in segments]),
        encoding="utf-8",
    )

    progress(0.95, "muxing final video")
    final_video = work_dir / "dubbed.mp4"
    mux_video(video_path, mixed_audio, final_video, srt_path)

    outputs: dict[str, Any] = {
        "title": title, "final_video": _rel(store, final_video), "subtitles": _rel(store, srt_path),
        "background_separated": separated is not None,
    }
    if project_id:
        from . import engine as engine_mod

        asset_id = new_id("a")
        dest = store.path_for_asset_file(asset_id, ".mp4")
        shutil.copyfile(final_video, dest)
        duration_s = audio_mod.probe_duration_s(dest)
        asset = store.create_asset(
            project_id=project_id, kind="video", file_path=engine_mod._rel(store, dest), mime="video/mp4",
            duration_s=duration_s, source="generated",
            recipe={"operation": "dub", "title": title, "segments": len(segments), "created_at": now_iso()},
            asset_id=asset_id, name=title[:80], tags=["dub"],
        )
        outputs["asset_ids"] = [asset["id"]]
    return outputs


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    """Atomically (temporary file + rename): a reader never sees half a
    manifest, and a crash mid-write leaves the previous one intact."""
    clean = dict(data)
    clean["segments"] = [_segment_row(s) for s in data["segments"]]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_manifest(work_dir: Path) -> dict[str, Any]:
    path = work_dir / "manifest.json"
    if not path.is_file():
        raise DubbingError("no_manifest", "this dub has no manifest (the job did not finish)")
    return json.loads(path.read_text(encoding="utf-8"))


_WORK_DIR_LOCKS: dict[str, threading.Lock] = {}
_WORK_DIR_LOCKS_GUARD = threading.Lock()


def _work_dir_lock(work_dir: Path) -> threading.Lock:
    key = str(work_dir.resolve())
    with _WORK_DIR_LOCKS_GUARD:
        return _WORK_DIR_LOCKS.setdefault(key, threading.Lock())


def resynthesize_segment(
    store: Store, backend: Backend, work_dir: Path, segment_index: int, new_text: Optional[str] = None,
    voice_spec: Optional[dict[str, Any]] = None, remix: bool = True, job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Re-translate-free re-synthesis of one segment (optionally with edited
    text and/or a different voice, remembered for that segment only), then
    optionally rebuild the mixed audio (reusing the separated background)
    and re-mux the video - without re-running transcription or translation
    for the other segments. One re-synthesis per work directory at a time.

    With `remix`, the rebuilt video is saved as a new video asset in the
    dub's project (when it had one); with `job_id`, that dub job's outputs
    are updated too (the segment row, the new asset id). Returns
    {"segment", "asset_id"?, "final_video"?}."""
    with _work_dir_lock(work_dir):
        manifest = load_manifest(work_dir)
        segments = manifest["segments"]
        if not 0 <= segment_index < len(segments):
            raise DubbingError("bad_segment", f"segment index must be between 0 and {len(segments) - 1}")
        seg_dir = work_dir / "segments"
        # fitted_path is not persisted in manifest.json (a Path is only valid
        # on this machine); every segment's file lives at this deterministic
        # name from the original run, so it is rebuilt here before mixing.
        for s in segments:
            s["fitted_path"] = seg_dir / f"seg{s['index']:04d}.fit.wav"

        seg = segments[segment_index]
        if new_text is not None:
            if not new_text.strip():
                raise DubbingError("empty_text", "the segment's new text is empty")
            seg["translated_text"] = " ".join(new_text.split())
        if voice_spec is not None:
            seg["voice"] = voice_spec  # this segment only; manifest["voice"] stays the dub's default
        spec = voice_spec_for_language(seg.get("voice") or manifest["voice"], manifest.get("target_language"))

        tts_engines = ve.default_tts_engines(voices_dir=store.data_dir / "voices")
        _synthesize_and_fit_segment(store, tts_engines, spec, seg, seg_dir)
        _write_manifest(work_dir / "manifest.json", manifest)

        result: dict[str, Any] = {"segment": _segment_row(seg)}
        outputs: Optional[dict[str, Any]] = None
        if remix:
            video_path = Path(manifest["video_path"])
            outputs = _assemble_dub(store, backend, work_dir, video_path, segments, manifest.get("project_id"),
                                    manifest["title"], lambda *a, **k: None)
            result["final_video"] = outputs["final_video"]
            if outputs.get("asset_ids"):
                result["asset_id"] = outputs["asset_ids"][0]
        if job_id:
            _update_job_outputs(store, job_id, seg, result.get("asset_id"))
        return result


def _update_job_outputs(store: Store, job_id: str, seg: dict[str, Any], asset_id: Optional[str]) -> None:
    job = store.get_job(job_id)
    outputs = dict(job.get("outputs") or {})
    rows = list(outputs.get("segments") or [])
    row = _segment_row(seg)
    for i, existing in enumerate(rows):
        if existing.get("index") == seg["index"]:
            rows[i] = row
            break
    outputs["segments"] = rows
    if asset_id:
        outputs["asset_ids"] = [asset_id, *[a for a in outputs.get("asset_ids") or [] if a != asset_id]][:8]
    store.update_job(job_id, outputs=outputs)


# --------------------------------------------------------------- agent views

def _clip(text: Any, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def dub_segments_view(outputs: dict[str, Any], offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """One page of a finished dub's segment table for agents: index, timing,
    source and translated text (clipped); no file paths."""
    rows = outputs.get("segments") or []
    offset = max(0, int(offset))
    limit = max(1, min(200, int(limit)))
    page = rows[offset: offset + limit]
    items = [{"index": r.get("index"), "start_s": r.get("start_s"), "end_s": r.get("end_s"),
              "source_text": _clip(r.get("source_text"), 300), "translated_text": _clip(r.get("translated_text"), 300)}
             for r in page]
    has_more = offset + limit < len(rows)
    return {"total": len(rows), "offset": offset, "items": items, "next_offset": offset + limit if has_more else None}


def dub_outputs_view(outputs: dict[str, Any], preview: int = 20) -> dict[str, Any]:
    """Compact, path-free summary of a finished dub job's outputs."""
    view = {
        "title": outputs.get("title"), "target_language": outputs.get("target_language"),
        "background_separated": outputs.get("background_separated"), "asset_ids": outputs.get("asset_ids") or [],
        "segments": dub_segments_view(outputs, 0, preview),
    }
    if view["segments"]["next_offset"] is not None:
        view["hint"] = "the rest: voice_dub_segments(job_id, offset=next_offset)"
    return view
