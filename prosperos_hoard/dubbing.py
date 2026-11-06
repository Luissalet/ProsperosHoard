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
from . import dub_plan
from . import procutil
from . import voice_engines as ve
from . import video_retime
from . import voice_lab
from .backend import Backend, ffmpeg_path
from .hoard_link.media import ffmpeg as ffmpeg_shared
from .hoard_link.media.stt import is_hallucination
from .ids import new_id
from .store import Store
from .util import now_iso

ChatFn = Callable[[list[dict[str, Any]]], str]

MIN_ATEMPO, MAX_ATEMPO = 0.5, 2.0
MAX_OVERALL_FACTOR = 4.0  # two chained atempo stages cover 0.25x-4x
DUCK_DB = -18.0
MIX_SAMPLE_RATE = 44100  # matches extract_audio's default; every mix input is normalised to this
DUCK_RAMP_S = 0.08  # the original fades in/out around each dubbed line instead of a hard step
MIX_CHUNK_S = 10.0  # the mix is streamed in chunks of this length, never the whole track in memory
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

atempo_chain = ffmpeg_shared.atempo_chain   # source / target duration -> a chain of atempo= filters (ffmpeg takes 0.5-2.0 each)


def compute_time_fit_factor(source_duration_s: float, target_duration_s: float) -> float:
    if source_duration_s <= 0 or target_duration_s <= 0:
        return 1.0
    return source_duration_s / target_duration_s


# A clip shorter than its slot is padded with silence, not stretched: slowing speech down past this point
# drawls it (a dub at 0.55x was misheard by the quality check as different words). Slight easing only.
MIN_SLOWDOWN = 0.92


def fit_audio_to_duration(src_wav: Path, target_duration_s: float, dest_wav: Path,
                          max_factor: float = MAX_OVERALL_FACTOR) -> dict[str, Any]:
    """Speed the clip up to approximately `target_duration_s` with `atempo`
    (by `max_factor` at most) when it is too long; when it is short, ease it at
    most to `MIN_SLOWDOWN` and pad the rest with silence. Then pad or trim to hit
    the slot exactly (a translated line rarely lands on the original's duration
    to the millisecond; what a capped speed-up leaves over is cut)."""
    source_duration_s = audio_mod.probe_duration_s(src_wav) or 0.0
    factor = compute_time_fit_factor(source_duration_s, target_duration_s)
    clamped = max(MIN_SLOWDOWN, min(max_factor, MAX_OVERALL_FACTOR, factor))
    chain = atempo_chain(clamped)
    filters = ",".join(chain) if chain else "anull"
    dest_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(src_wav), "-af",
           f"{filters},apad,atrim=0:{target_duration_s:.6f}", str(dest_wav)]
    proc = procutil.run(cmd, timeout=120)
    if proc.returncode != 0 or not dest_wav.is_file():
        raise DubbingError("time_fit_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
    return {"source_duration_s": round(source_duration_s, 3), "target_duration_s": round(target_duration_s, 3),
            "requested_factor": round(factor, 4), "applied_factor": round(clamped, 4), "clamped": clamped != factor}


# --------------------------------------------------------------- audio I/O

def extract_audio(video_path: Path, out_wav: Path, sr: int = 44100) -> None:
    """The video's audio as mono WAV at `out_wav`, written to a temporary
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
    not installed or fails to start, so the caller ducks the original track
    instead of replacing it. Runs `python -m demucs` with this app's own
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


def build_dub_mix_filter(segments: list[dict[str, Any]], duck_db: float = DUCK_DB,
                         sample_rate: int = MIX_SAMPLE_RATE) -> str:
    """The `filter_complex` string that ducks the original track under each
    dubbed segment and mixes in the delayed dub clips. `segments`:
    [{"start_s", "end_s"}] in the original timeline; input `0:a` is the
    original (or separated background) track, inputs `1:a`..`N:a` are each
    segment's time-fitted dub clip, in the same order - each TTS engine
    (and the video's own audio track) can emit a different sample rate, so
    every branch is normalised to `sample_rate`/mono before it reaches
    `amix`: left to ffmpeg's own format negotiation, mismatched inputs do
    not fail outright but can silently resample the whole mix up to an
    unrelated, much higher rate (observed: two branches at 44100/24000
    negotiating to 192000). Pure string building - unit-tested without
    running ffmpeg."""
    fmt = f"aformat=sample_rates={sample_rate}:channel_layouts=mono"
    if not segments:
        return f"[0:a]{fmt}[mixed]"
    enables = "+".join(f"between(t\\,{s['start_s']:.3f}\\,{s['end_s']:.3f})" for s in segments)
    parts = [f"[0:a]{fmt},volume=enable='{enables}':volume={10 ** (duck_db / 20):.6f}[bg]"]
    delayed_labels = []
    for i, seg in enumerate(segments):
        delay_ms = max(0, int(round(seg["start_s"] * 1000)))
        parts.append(f"[{i + 1}:a]{fmt},adelay={delay_ms}|{delay_ms}[d{i}]")
        delayed_labels.append(f"[d{i}]")
    mix_inputs = "[bg]" + "".join(delayed_labels)
    parts.append(f"{mix_inputs}amix=inputs={len(segments) + 1}:normalize=0[mixed]")
    return ";".join(parts)


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


def duck_gain(start: int, length: int, spans: list[tuple[int, int]], duck_gain_linear: float, ramp: int) -> np.ndarray:
    """The background's gain for samples [start, start+length): 1.0, dipping
    to `duck_gain_linear` inside each (start, end) span with a linear ramp
    of `ramp` samples on either side. Pure numpy."""
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
        pending = b""
        while True:
            data = proc.stdout.read(chunk * 4)
            if not data:
                break
            pending += data
            usable = len(pending) - len(pending) % 4
            if usable >= chunk * 4 or usable and len(data) < chunk * 4:
                yield np.frombuffer(pending[:usable], dtype="<f4")
                pending = pending[usable:]
        if len(pending) >= 4:
            yield np.frombuffer(pending[: len(pending) - len(pending) % 4], dtype="<f4")
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
    decoder for the background, each clip read only while it overlaps the
    current chunk - so hundreds of segments never become hundreds of ffmpeg
    inputs (command-line and file-descriptor limits), and nothing depends on
    filter options newer ffmpeg builds added (the bundled 4.2 has no
    `amix normalize=`). `segments`: [{"start_s", "end_s"}] in the same order
    as `dub_clip_paths`."""
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
        # loudnorm's single-pass true-peak limiting emits at 192kHz regardless
        # of its input; force the output back to `sample_rate`
        cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-f", "f32le", "-ar", str(sample_rate), "-ac", "1",
               "-i", raw.name, "-af", "loudnorm=I=-18:TP=-1.5:LRA=9", "-ar", str(sample_rate), "-ac", "1",
               out_path.name]
        proc = procutil.run(cmd, timeout=1800, cwd=str(out_path.parent))
        if proc.returncode != 0 or not out_path.is_file():
            raise DubbingError("mix_failed", (proc.stderr or b"").decode("utf-8", "replace")[:800])
    finally:
        raw.unlink(missing_ok=True)


def mux_video(video_path: Path, dub_audio_path: Path, out_path: Path, srt_path: Optional[Path] = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(video_path), "-i", str(dub_audio_path)]
    maps = ["-map", "0:v:0", "-map", "1:a:0"]
    if srt_path and srt_path.is_file():
        cmd += ["-i", str(srt_path)]
        maps += ["-map", "2:s:0", "-c:s", "mov_text"]
    # the result is as long as the video: `-shortest` would also stop at the last subtitle cue
    length = audio_mod.probe_duration_s(video_path)
    limit = ["-t", f"{length:.3f}"] if length else ["-shortest"]
    part = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    cmd += maps + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"] + limit + ["-f", "mp4", str(part)]
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


# A garbled reply despite the prompt's "no quotes, no notes": one plain line, no wrapping quotes.
_clean_translated_line = dub_plan.clean_line


def _same_line(a: str, b: str) -> bool:
    norm = lambda t: " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in (t or "")).split())
    return norm(a) == norm(b)


def _lang_root(code: Optional[str]) -> str:
    return (dub_plan.normalize_lang(code) if code else "") or ""


TRANSLATION_MODES = ("direct", "adapt")
MAX_ISSUES = 6
MAX_ISSUE_CHARS = 240


def resolve_translation_mode(requested: Optional[str], chat_fn: Optional[ChatFn]) -> str:
    """`adapt` (the default) needs a model to critique and rewrite with; without
    one, or for an unknown value, the single-call `direct` mode is used."""
    mode = (requested or "adapt").strip().lower()
    if mode not in TRANSLATION_MODES:
        mode = "adapt"
    return mode if chat_fn is not None else "direct"


def build_literal_messages(text: str, target_lang: str, source_lang: Optional[str],
                           glossary: Optional[dict[str, str]]) -> list[dict[str, Any]]:
    """Step 1 of the three-step translation: a faithful rendering, length ignored."""
    glossary_line = ""
    if glossary:
        pairs = "; ".join(f"{k} -> {v}" for k, v in list(glossary.items())[:50])
        glossary_line = f" Use this glossary for names/terms: {pairs}."
    source_line = f" The source language is {source_lang}." if source_lang else ""
    system = (
        f"You translate video dialogue. Translate the line into {target_lang}.{source_line}{glossary_line} "
        "Be faithful: keep the meaning, the tone and the register of the original, and do not add or drop content. "
        "Reply with only the translated line, no quotes, no notes, no explanation."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


def build_reflect_messages(source: str, literal: str, target_lang: str, source_lang: Optional[str],
                           glossary: Optional[dict[str, str]], char_budget: Optional[int]) -> list[dict[str, Any]]:
    """Step 2: the model critiques the literal line as a dubbing line."""
    glossary_line = ""
    if glossary:
        pairs = "; ".join(f"{k} -> {v}" for k, v in list(glossary.items())[:50])
        glossary_line = f" Glossary that must be respected: {pairs}."
    budget_line = (f" The dubbed line has room for about {char_budget} characters, spaces included."
                   if char_budget else "")
    system = (
        f"You review a {target_lang} translation of a video line that will be dubbed over the original.{glossary_line} "
        f"Check: tone and register against the source; whether it can be spoken in the time the source line takes"
        f"{budget_line}; glossary terms used as given; whether it sounds natural when spoken aloud. "
        'Reply with compact JSON only, like {"issues": ["short problem", ...]}. '
        'Use {"issues": []} when the line is already good. At most 4 issues, each under 20 words.'
    )
    src_line = f" ({source_lang})" if source_lang else ""
    user = f"Source{src_line}: {source}\nTranslation: {literal}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_adapt_messages(source: str, literal: str, issues: list[str], target_lang: str, source_lang: Optional[str],
                         glossary: Optional[dict[str, str]], char_budget: Optional[int]) -> list[dict[str, Any]]:
    """Step 3: the final dubbing line, with the issues fixed and inside the character budget."""
    glossary_line = ""
    if glossary:
        pairs = "; ".join(f"{k} -> {v}" for k, v in list(glossary.items())[:50])
        glossary_line = f" Glossary for names/terms: {pairs}."
    budget_line = (f" Use at most {char_budget} characters, spaces included." if char_budget else
                   " Keep it about as long, when spoken, as the source line.")
    fixes = "\n".join(f"- {i}" for i in issues) if issues else "- none: only polish it for speech"
    system = (
        f"You write the final {target_lang} line for dubbing a video.{glossary_line} Rewrite the translation so it "
        f"sounds natural spoken aloud, keeps the source's meaning, tone and register, and fixes the issues listed.{budget_line} "
        "Return only the line: no quotes, no notes, no explanation."
    )
    src_line = f" ({source_lang})" if source_lang else ""
    user = f"Source{src_line}: {source}\nTranslation: {literal}\nIssues:\n{fixes}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_issues(reply: Optional[str]) -> list[str]:
    """The `issues` of a REFLECT reply. Tolerant: JSON inside a code fence or
    surrounded by prose is found, a bare list is accepted, a single string
    becomes one issue; anything else (garbage, an empty reply) is no issues."""
    text = (reply or "").strip()
    if not text:
        return []
    candidates = [text]
    start_obj, end_obj = text.find("{"), text.rfind("}")
    if 0 <= start_obj < end_obj:
        candidates.append(text[start_obj:end_obj + 1])
    start_arr, end_arr = text.find("["), text.rfind("]")
    if 0 <= start_arr < end_arr:
        candidates.append(text[start_arr:end_arr + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        raw = data.get("issues") if isinstance(data, dict) else data
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, list):
            issues = [" ".join(str(x).split())[:MAX_ISSUE_CHARS] for x in raw if isinstance(x, (str, int, float)) and str(x).strip()]
            return issues[:MAX_ISSUES]
    return []


def _literal_line(chat_fn: ChatFn, seg: dict[str, Any], target_lang: str, source_lang: Optional[str],
                  glossary: Optional[dict[str, str]], differs: bool, literal_mode: bool) -> tuple[str, str]:
    """One translated line and its status (`ok` / `retried` / `untranslated`; an
    untranslated line comes back empty). Up to two model calls."""
    build = build_literal_messages if literal_mode else build_translation_messages
    messages = build(seg["text"], target_lang, source_lang, glossary)
    translated = _clean_translated_line(chat_fn(messages) or "")
    status = "ok"
    if not translated or (differs and _same_line(translated, seg["text"])):
        retry = messages + [{"role": "user", "content": (
            f"That was not a translation. Write this line in {target_lang} now. "
            f"Reply with the {target_lang} line only:\n{seg['text']}")}]
        translated = _clean_translated_line(chat_fn(retry) or "")
        status = "retried"
        if not translated or (differs and _same_line(translated, seg["text"])):
            return "", "untranslated"
    return translated, status


def _adapt_line(chat_fn: ChatFn, seg: dict[str, Any], literal: str, target_lang: str, source_lang: Optional[str],
                glossary: Optional[dict[str, str]], differs: bool) -> dict[str, Any]:
    """REFLECT then ADAPT for one literal line: {literal, issues, adapted} (plus
    `fallback` naming the step that failed, when one did). At most two model
    calls; a step that raises, or answers with nothing usable, leaves the literal line."""
    budget = seg.get("char_budget") or None
    steps: dict[str, Any] = {"literal": literal, "issues": [], "adapted": literal}
    try:
        steps["issues"] = parse_issues(chat_fn(build_reflect_messages(seg["text"], literal, target_lang, source_lang,
                                                                      glossary, budget)))
    except Exception:  # noqa: BLE001 - a critique that cannot be had is no critique; the literal line stands
        steps["fallback"] = "reflect"
        return steps
    too_long = bool(budget) and dub_plan.count_chars(literal) > int(budget)
    if not steps["issues"] and not too_long:
        return steps  # nothing to fix: the literal line is the adapted line
    try:
        adapted = _clean_translated_line(chat_fn(build_adapt_messages(seg["text"], literal, steps["issues"], target_lang,
                                                                      source_lang, glossary, budget)) or "")
    except Exception:  # noqa: BLE001
        steps["fallback"] = "adapt"
        return steps
    if not adapted or (differs and _same_line(adapted, seg["text"])):
        steps["fallback"] = "adapt"
        return steps
    if budget and dub_plan.count_chars(adapted) > int(budget) and dub_plan.count_chars(adapted) >= dub_plan.count_chars(literal):
        steps["fallback"] = "adapt"  # the rewrite is no shorter and still over budget: keep the faithful line
        return steps
    steps["adapted"] = adapted
    return steps


def translate_segments(chat_fn: ChatFn, segments: list[dict[str, Any]], target_lang: str,
                       source_lang: Optional[str] = None, glossary: Optional[dict[str, str]] = None,
                       report: Optional[list[str]] = None, mode: str = "direct",
                       steps: Optional[list[Optional[dict[str, Any]]]] = None) -> list[str]:
    """Translate each segment through Hoard Link's `chat` (injected as
    `chat_fn` so this stays testable with a fake). Raises whatever `chat_fn`
    raises while translating - typically `hoard_link.errors.Unavailable` when
    no local LLM is reachable, which the job handler surfaces as a clear failure.

    `mode="direct"`: one call per segment. An empty reply or one that just
    repeats the source line (a reasoning model that spent its budget thinking,
    or one that ignored the target language) is retried once with a stricter
    instruction; if it is still not a translation the source line is kept and
    ``report`` gets ``"untranslated"`` for that segment (``"ok"`` /
    ``"retried"`` otherwise), so the job can say so instead of voicing the wrong
    language in silence.

    `mode="adapt"`: LITERAL (a faithful line, with the retry above), REFLECT (the
    model lists problems with it as a dubbing line - tone, length for the segment's
    optional ``char_budget``, glossary, naturalness - as JSON) and ADAPT (the final
    line, fixing them inside the budget). At most three calls per segment plus the
    literal retry; ADAPT is skipped when REFLECT finds nothing and the literal line
    fits the budget. A failing REFLECT or ADAPT leaves the literal line. ``steps``
    gets one ``{literal, issues, adapted}`` per segment (None in direct mode)."""
    out = []
    differs = _lang_root(target_lang) != _lang_root(source_lang) if source_lang else True
    adapt = mode == "adapt"
    for seg in segments:
        translated, status = _literal_line(chat_fn, seg, target_lang, source_lang, glossary, differs, adapt)
        record: Optional[dict[str, Any]] = None
        if adapt:
            if translated:
                record = _adapt_line(chat_fn, seg, translated, target_lang, source_lang, glossary, differs)
                translated = record["adapted"]
            else:
                record = {"literal": "", "issues": [], "adapted": ""}
        if report is not None:
            report.append(status)
        if steps is not None:
            steps.append(record)
        out.append(translated or seg["text"])
    return out


def translation_max_tokens(text: str) -> int:
    """A reply budget that scales with the line (a token is roughly 3-4
    characters of Latin-script text; CJK can be ~1 per character), so a
    long segment is never cut off mid-sentence."""
    return max(200, min(MAX_SEGMENT_TEXT_TOKENS, 2 * len(text or "") + 64))


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


def dub_job(store: Store, backend: Backend, job: dict[str, Any], progress: Any) -> dict[str, Any]:
    params = job["params"]
    video_path = Path(params["video_path"])
    target_code = target_language_code(params["target_language"])
    target_lang = ve.language_name(target_code) or target_code  # the name the prompts use
    try:
        source_lang = ve.normalize_language(params.get("source_language"), allow_auto=True)
    except ve.UnsupportedLanguage as exc:
        raise DubbingError("bad_language", str(exc)) from None
    glossary = params.get("glossary") or {}
    voice_spec = voice_spec_for_language(params["voice"], target_code)
    project_id = params.get("project_id")
    title = params.get("title") or video_path.stem

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
    source_name = ve.language_name(detected_source_lang) if detected_source_lang else None

    progress(0.2, f"translating {len(segments)} segment(s) to {target_lang}")

    calls = {"n": 0}

    def chat_fn(messages: list[dict[str, Any]]) -> str:
        # one call per step per line: progress moves, and a cancelled job stops here instead of after every line
        calls["n"] += 1
        progress(0.2 + 0.06 * min(1.0, calls["n"] / (3.0 * len(segments))), f"translating ({calls['n']})")
        # room for models that reason before answering: 200 tokens could be spent thinking and leave no line
        # dubbing lines are short: no reasoning (a reasoning model left on its default spent the whole budget thinking
        # and returned an empty line, ~100 s per call, measured live), the same "off" the rest of the studio uses
        result = backend.link.sync.chat(messages=messages, max_tokens=max(800, translation_max_tokens(messages[-1]["content"])),
                                        temperature=0.3, effort="off")
        return result.text

    translation_mode = resolve_translation_mode(params.get("translation_mode"), chat_fn)
    fit_mode = "smart" if params.get("fit_mode") == "smart" else "audio"
    items: list[dict[str, Any]] = [{"text": s["source_text"]} for s in segments]
    if translation_mode == "adapt":
        # the room each line has, so ADAPT can write to length (the slot is known before anything is translated)
        for item, planned in zip(items, dub_plan.plan_segments(segments, target_lang, text_key="source_text")):
            item["char_budget"] = planned["char_budget"]
    statuses: list[str] = []
    steps: list[Optional[dict[str, Any]]] = []
    translations = translate_segments(chat_fn, items, target_lang, source_name, glossary, report=statuses,
                                      mode=translation_mode, steps=steps)
    for i, (seg, tr, status) in enumerate(zip(segments, translations, statuses or ["ok"] * len(segments))):
        seg["translated_text"] = tr
        seg["translation"] = status  # ok | retried | untranslated (the source line was kept)
        if i < len(steps) and steps[i]:
            seg["translation_steps"] = steps[i]  # {literal, issues, adapted[, fallback]}

    options = {"plan": params.get("plan", True) is not False, "qc": params.get("qc", True) is not False,
               "qc_threshold": params.get("qc_threshold"), "stt_engine_id": stt_engine.id,
               "translation_mode": translation_mode, "fit_mode": fit_mode}
    if fit_mode == "smart":
        # the silence after each line is free to use for the fit whether or not the plan is shown
        for seg, planned in zip(segments, dub_plan.plan_segments(segments, target_lang)):
            seg["free_gap_s"] = planned["gap_s"]
    if options["plan"]:
        progress(0.26, "planning line timing")
        apply_plan(segments, target_lang, chat_fn)

    tts_engines = ve.default_tts_engines(voices_dir=store.data_dir / "voices")
    seg_dir = work_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    qc_engine = stt_engine if options["qc"] else None
    for i, seg in enumerate(segments):
        progress(0.3 + 0.5 * (i / len(segments)), f"voicing segment {i + 1}/{len(segments)}")
        _synthesize_and_fit_segment(store, tts_engines, voice_spec, seg, seg_dir, borrow_gap=options["plan"],
                                    fit_mode=fit_mode)
        if options["qc"]:
            run_qc(qc_engine, seg, target_lang, options["qc_threshold"])
    chunks = _timeline(video_path, segments, fit_mode)
    summary = _job_summary(segments, chunks, fit_mode)

    _write_manifest(manifest_path, {
        "title": title, "target_language": target_code, "source_language": detected_source_lang,
        "glossary": glossary, "voice": voice_spec, "video_path": str(video_path), "segments": segments,
        "options": options, "summary": summary, "project_id": project_id, "job_id": job.get("id"),
        "created_at": now_iso(),
    })

    progress(0.82, "mixing audio")
    outputs = _assemble_dub(store, backend, work_dir, video_path, segments, project_id, title, progress, chunks=chunks)
    outputs["target_language"] = target_code
    outputs["work_dir"] = _rel(store, work_dir)
    outputs["manifest"] = _rel(store, manifest_path)
    outputs["segments"] = [{k: v for k, v in s.items() if k != "fitted_path"} for s in segments]
    outputs["summary"] = summary
    return outputs


# ------------------------------------------------------- plan and quality

def apply_plan(segments: list[dict[str, Any]], target_lang: str, chat_fn: Optional[ChatFn]) -> None:
    """Estimate each translated line's spoken length against its slot, ask the
    model to shorten the ones that cannot possibly fit (no model: they stay and
    are marked `shorten: "no_llm"`), and record the result in `seg["plan"]`.
    A shortened line replaces `translated_text`; the original is kept in
    `plan.shortened_from`."""
    plan = dub_plan.shorten_lines(chat_fn, dub_plan.plan_segments(segments, target_lang), target_lang)
    for seg, item in zip(segments, plan):
        seg["translated_text"] = item["text"]
        seg["plan"] = _plan_record(item)


def _plan_record(item: dict[str, Any]) -> dict[str, Any]:
    rec = {k: item[k] for k in ("est_s", "slot_s", "own_s", "gap_s", "factor", "class", "beyond_hard", "cps",
                                "char_budget")}
    if item.get("shortened_from"):
        rec["shortened_from"] = item["shortened_from"]
    if item.get("shorten"):
        rec["shorten"] = item["shorten"]["status"]
        rec["shorten_attempts"] = len(item["shorten"]["attempts"])
    return rec


def _calibration(segments: list[dict[str, Any]], skip: Optional[int] = None) -> Optional[float]:
    """Characters per second the voice really produced in this job (before any
    time-fit), from the segments synthesized so far."""
    return dub_plan.calibrate((s["translated_text"], s["raw_duration_s"]) for s in segments
                              if s.get("raw_duration_s") and s["index"] != skip)


def _trim_silence(src: Path, dest: Path) -> Path:
    """The clip without its leading and trailing silence, for the quality check: a fitted clip is padded with silence
    (MIN_SLOWDOWN) and Whisper invents words on a silent tail ("see you in the next video"). Falls back to ``src``."""
    edge = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(src), "-af",
           f"{edge},areverse,{edge},areverse,apad=pad_dur=0.25", str(dest)]
    try:
        proc = procutil.run(cmd, timeout=60)
    except Exception:  # noqa: BLE001 - trimming is a nicety; the untrimmed clip still works
        return src
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1024:
        return src
    return dest


def run_qc(stt_engine: Optional[Any], seg: dict[str, Any], target_lang: str, threshold: Optional[float]) -> None:
    """Transcribe the segment's finished clip and compare it with the text that
    was meant to be said. No transcriber, or one that fails, marks the segment
    `skipped` - quality control never fails a dub.

    The clip is transcribed with its silent edges trimmed. A line that is itself
    one of the phrases the transcriber drops as a silence artifact ("Thanks for
    watching") cannot be heard back by design: when nothing is heard it is
    `skipped` with reason `filtered_phrase` instead of being flagged."""
    code = dub_plan.normalize_lang(target_lang)
    if stt_engine is None:
        seg["qc"] = {"status": "skipped", "reason": "no_transcriber", "flagged": False}
        return
    fitted = Path(seg["fitted_path"])
    try:
        clip = _trim_silence(fitted, fitted.with_name(fitted.stem + ".qc.wav"))
        heard = stt_engine.transcribe(clip, language=code, word_timestamps=False)
        hyp = (heard.get("text") or " ".join(str(x.get("text", "")) for x in heard.get("segments") or [])).strip()
    except Exception as exc:  # noqa: BLE001 - a transcriber that cannot run must not sink the job
        seg["qc"] = {"status": "skipped", "reason": f"transcribe_failed: {str(exc)[:160]}", "flagged": False}
        return
    if not hyp and is_hallucination(seg.get("translated_text") or "", deny_weak=False):
        seg["qc"] = {"status": "skipped", "reason": "filtered_phrase", "flagged": False}
        return
    seg["qc"] = dub_plan.qc_line(seg["translated_text"], hyp, code or target_lang, threshold)


def agent_result(outputs: dict[str, Any]) -> dict[str, Any]:
    """A finished dub job's answer for an agent: the per-segment table with the
    plan and quality-check fields, compact (no file paths, no model attempt logs)."""
    rows = []
    for s in outputs.get("segments") or []:
        row: dict[str, Any] = {k: s[k] for k in ("index", "start_s", "end_s", "source_text", "translated_text", "translation") if k in s}
        plan = s.get("plan")
        if plan:
            row["plan"] = {k: plan[k] for k in ("est_s", "slot_s", "class", "shortened_from", "shorten") if k in plan}
        qc = s.get("qc")
        if qc:
            row["qc"] = {k: qc[k] for k in ("status", "reason", "wer", "cer", "hyp", "flagged") if k in qc}
        issues = (s.get("translation_steps") or {}).get("issues")
        if issues:
            row["issues"] = issues
        if s.get("fit"):
            row["speedup"] = s["fit"].get("applied_factor")
            strategy = s["fit"].get("strategy")
            if strategy:
                row["fit"] = {k: strategy[k] for k in ("kind", "audio_factor", "video_factor", "trim_s") if k in strategy}
        rows.append(row)
    return {"title": outputs.get("title"), "summary": outputs.get("summary"), "segments": rows}


def _clip(text: Any, n: int) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def dub_segments_view(outputs: dict[str, Any], offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """One page of a finished dub's line table for agents (the rows of
    `agent_result`, with the texts clipped); no file paths."""
    rows = agent_result(outputs)["segments"]
    offset = max(0, int(offset))
    limit = max(1, min(200, int(limit)))
    items = [{**r, "source_text": _clip(r.get("source_text"), 300), "translated_text": _clip(r.get("translated_text"), 300)}
             for r in rows[offset: offset + limit]]
    has_more = offset + limit < len(rows)
    return {"total": len(rows), "offset": offset, "items": items, "next_offset": offset + limit if has_more else None}


def dub_outputs_view(outputs: dict[str, Any], preview: int = 20) -> dict[str, Any]:
    """Compact, path-free summary of a finished dub job's outputs: the first
    lines with their plan and quality check, and where to page for the rest."""
    view = {
        "title": outputs.get("title"), "target_language": outputs.get("target_language"),
        "background_separated": outputs.get("background_separated"), "asset_ids": outputs.get("asset_ids") or [],
        "summary": outputs.get("summary"), "segments": dub_segments_view(outputs, 0, preview),
    }
    if view["segments"]["next_offset"] is not None:
        view["hint"] = "the rest: voice_dub_segments(job_id, offset=next_offset)"
    return {k: v for k, v in view.items() if v is not None}


def _job_summary(segments: list[dict[str, Any]], chunks: Optional[list[dict[str, Any]]], fit_mode: str) -> dict[str, Any]:
    summary: dict[str, Any] = dub_plan.summarize(segments)
    summary["calibrated_cps"] = _calibration(segments)
    if fit_mode == "smart":
        summary.update(dub_plan.summarize_retime(segments))
        if chunks:
            summary["added_s"] = video_retime.added_seconds(chunks)
    return summary


def _timeline(video_path: Path, segments: list[dict[str, Any]], fit_mode: str) -> Optional[list[dict[str, Any]]]:
    """The video's chunk list when the smart fit slowed any stretch of it down
    (and each segment's place in the retimed video, `timeline`); None when the
    picture is left alone."""
    for seg in segments:
        seg.pop("timeline", None)
    if fit_mode != "smart":
        return None
    retimes = [s["retime"] for s in segments if s.get("retime")]
    if not retimes:
        return None
    total = audio_mod.probe_duration_s(video_path) or max(float(s["end_s"]) for s in segments)
    chunks = video_retime.build_chunks(retimes, total)
    if not video_retime.needs_retime(chunks):
        return None
    for seg in segments:
        seg["timeline"] = {"start_s": video_retime.map_time(chunks, seg["start_s"]),
                           "end_s": video_retime.map_time(chunks, seg["end_s"])}
    return chunks


def _mix_windows(segments: list[dict[str, Any]], chunks: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """Where each dubbed clip plays: from the line's start for as long as it was
    fitted (it may run on into the silence before the next line). With a retimed
    video the start is where the line now begins."""
    out = []
    for s in segments:
        fitted = float((s.get("fit") or {}).get("target_duration_s") or 0.0)
        if chunks:
            start = video_retime.map_time(chunks, s["start_s"])
            end = max(video_retime.map_time(chunks, s["end_s"]), start + fitted)
        else:
            start, end = s["start_s"], max(s["end_s"], s["start_s"] + fitted)
        out.append({"start_s": start, "end_s": end})
    return out


def _synthesize_and_fit_segment(store: Store, tts_engines: list[Any], voice_spec: dict[str, Any],
                                seg: dict[str, Any], seg_dir: Path, borrow_gap: bool = False,
                                fit_mode: str = "audio") -> None:
    raw_wav = seg_dir / f"seg{seg['index']:04d}.raw.wav"
    fitted_wav = seg_dir / f"seg{seg['index']:04d}.fit.wav"
    wav_bytes, engine_id = voice_lab.synthesize_with_spec(store, tts_engines, voice_spec, seg["translated_text"])
    raw_wav.write_bytes(wav_bytes)
    own_duration = max(0.05, seg["end_s"] - seg["start_s"])
    raw_duration = audio_mod.probe_duration_s(raw_wav) or 0.0
    seg.pop("retime", None)
    if fit_mode == "smart":
        gap = seg.get("free_gap_s")
        if gap is None:
            gap = (seg.get("plan") or {}).get("gap_s") or 0.0
        strategy = dub_plan.fit_strategy(raw_duration, own_duration, gap)
        # the picture may play slower over the window, which gives the clip that much more room
        target_duration = strategy["window_s"] * strategy["video_factor"]
        fit_info = fit_audio_to_duration(raw_wav, target_duration, fitted_wav,
                                         max_factor=max(1.0, strategy["audio_factor"]) * 1.001)
        fit_info["borrowed_gap_s"] = strategy["borrow_gap_s"]
        fit_info["strategy"] = strategy
        if strategy["video_factor"] > 1.0:
            seg["retime"] = {"start_s": seg["start_s"], "end_s": round(seg["start_s"] + strategy["window_s"], 3),
                             "factor": strategy["video_factor"]}
    else:
        target_duration = own_duration
        if borrow_gap and raw_duration > own_duration:
            # a line that runs long takes the silence before the next one (as the plan
            # allowed) before anything is sped up
            target_duration = min(raw_duration, own_duration + float((seg.get("plan") or {}).get("gap_s") or 0.0))
        fit_info = fit_audio_to_duration(raw_wav, target_duration, fitted_wav)
        fit_info["borrowed_gap_s"] = round(target_duration - own_duration, 3)
    seg["raw_duration_s"] = round(raw_duration, 3)
    seg["engine_id"] = engine_id
    seg["fitted_path"] = fitted_wav
    seg["fit"] = fit_info


def _retimed_media(work_dir: Path, video_path: Path, bg_audio: Path, chunks: list[dict[str, Any]],
                   progress: Any) -> tuple[Path, Path]:
    """The video and the background track slowed over the same chunks. Kept in
    the work directory and reused while the chunks (and the background) stay the same."""
    video_out, audio_out, stamp = work_dir / "retimed.mp4", work_dir / "retimed_background.wav", work_dir / "retimed.key"
    key = f"{video_retime.chunks_key(chunks)}:{bg_audio.name}:{video_path.stat().st_size}"
    if stamp.is_file() and stamp.read_text(encoding="utf-8") == key and video_out.is_file() and audio_out.is_file():
        return video_out, audio_out
    exe = _ffmpeg()
    progress(0.84, "slowing the picture where lines need room")
    try:
        video_retime.retime_video(exe, video_path, chunks, video_out, fps=_video_fps(video_path))
        video_retime.retime_audio(exe, bg_audio, chunks, audio_out)
    except video_retime.RetimeError as exc:
        raise DubbingError(exc.code, str(exc)) from exc
    stamp.write_text(key, encoding="utf-8")
    return video_out, audio_out


def _video_fps(video_path: Path) -> Optional[float]:
    from .backend import ffprobe_path

    probe = ffprobe_path()
    if not probe:
        return None
    try:
        out = procutil.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                            "-of", "csv=p=0", str(video_path)], text=True, timeout=30)
        num, _, den = out.stdout.strip().partition("/")
        fps = float(num) / float(den or 1)
        return fps if 1.0 <= fps <= 240.0 else None
    except Exception:  # noqa: BLE001 - without the rate the stretched pieces simply keep their own timestamps
        return None


def _assemble_dub(store: Store, backend: Backend, work_dir: Path, video_path: Path, segments: list[dict[str, Any]],
                  project_id: Optional[str], title: str, progress: Any,
                  chunks: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    original_audio = work_dir / "original.wav"
    background_dir = work_dir / "background"
    separated = separate_background(original_audio, background_dir)
    bg_audio = separated["no_vocals"] if separated else original_audio
    mux_source = video_path
    if chunks:
        mux_source, bg_audio = _retimed_media(work_dir, video_path, bg_audio, chunks, progress)

    mixed_audio = work_dir / "dub_mix.wav"
    fitted_paths = [Path(s["fitted_path"]) for s in segments]
    mix_dub_audio(bg_audio, fitted_paths, _mix_windows(segments, chunks), mixed_audio)

    srt_path = work_dir / "subtitles.srt"
    cues = [{"start_s": (s.get("timeline") or s)["start_s"], "end_s": (s.get("timeline") or s)["end_s"],
             "text": s["translated_text"]} for s in segments]
    srt_path.write_text(ve.segments_to_srt(cues), encoding="utf-8")

    progress(0.95, "muxing final video")
    final_video = work_dir / "dubbed.mp4"
    mux_video(mux_source, mixed_audio, final_video, srt_path)

    outputs: dict[str, Any] = {
        "title": title, "final_video": _rel(store, final_video), "subtitles": _rel(store, srt_path),
        "background_separated": separated is not None,
    }
    if chunks:
        outputs["retimed"] = True
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
    clean = dict(data)
    clean["segments"] = [{k: v for k, v in s.items() if k != "fitted_path"} for s in data["segments"]]
    tmp = path.with_name(path.name + ".tmp")  # atomic: a reader never sees half a manifest
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


def _update_job_outputs(store: Store, job_id: str, seg: dict[str, Any], asset_id: Optional[str],
                        summary: Optional[dict[str, Any]] = None) -> None:
    job = store.get_job(job_id)
    outputs = dict(job.get("outputs") or {})
    rows = list(outputs.get("segments") or [])
    row = {k: v for k, v in seg.items() if k != "fitted_path"}
    for i, existing in enumerate(rows):
        if existing.get("index") == seg["index"]:
            rows[i] = row
            break
    outputs["segments"] = rows
    if summary is not None:
        outputs["summary"] = summary
    if asset_id:
        outputs["asset_ids"] = [asset_id, *[a for a in outputs.get("asset_ids") or [] if a != asset_id]][:8]
    store.update_job(job_id, outputs=outputs)


def resynthesize_segment(
    store: Store, backend: Backend, work_dir: Path, segment_index: int, new_text: Optional[str] = None,
    voice_spec: Optional[dict[str, Any]] = None, remix: bool = True, job_id: Optional[str] = None,
) -> dict[str, Any]:
    """One re-synthesis per work directory at a time (see `_resynthesize_segment`)."""
    with _work_dir_lock(work_dir):
        return _resynthesize_segment(store, backend, work_dir, segment_index, new_text, voice_spec, remix, job_id)


def _resynthesize_segment(
    store: Store, backend: Backend, work_dir: Path, segment_index: int, new_text: Optional[str],
    voice_spec: Optional[dict[str, Any]], remix: bool, job_id: Optional[str],
) -> dict[str, Any]:
    """Re-translate-free re-synthesis of one segment (optionally with edited
    text and/or a different voice), then optionally rebuild the mixed audio
    and re-mux the video - without re-running transcription or translation
    for the other segments."""
    manifest = load_manifest(work_dir)
    segments = manifest["segments"]
    if not 0 <= segment_index < len(segments):
        raise DubbingError("bad_segment", f"segment index must be between 0 and {len(segments) - 1}")
    seg_dir = work_dir / "segments"
    # fitted_path is not persisted in manifest.json (a Path is only valid on
    # this machine); every segment's file lives at this deterministic name
    # from the original run, so it is rebuilt here before mixing.
    for s in segments:
        s["fitted_path"] = seg_dir / f"seg{s['index']:04d}.fit.wav"

    seg = segments[segment_index]
    options = manifest.get("options") or {}
    target_lang = manifest.get("target_language") or ""
    plan_on = bool(options.get("plan", "plan" in seg))
    fit_mode = "smart" if options.get("fit_mode") == "smart" else "audio"
    if new_text is not None:
        if not new_text.strip():
            raise DubbingError("empty_text", "the segment's new text is empty")
        seg["translated_text"] = " ".join(new_text.split())
        seg.pop("translation_steps", None)  # the steps described the old text
        seg.pop("raw_duration_s", None)  # measured on the old text: no use for calibration
    if voice_spec is not None:
        seg["voice"] = voice_spec  # this segment only; manifest["voice"] stays the dub's default
    spec = voice_spec_for_language(seg.get("voice") or manifest["voice"], manifest.get("target_language"))

    tts_engines = ve.default_tts_engines(voices_dir=store.data_dir / "voices")
    _synthesize_and_fit_segment(store, tts_engines, spec, seg, seg_dir, borrow_gap=plan_on, fit_mode=fit_mode)
    if plan_on:
        # re-estimated against the pace the voice really showed on the other lines
        item = dub_plan.plan_segments(segments, target_lang, _calibration(segments, skip=segment_index))[segment_index]
        previous = seg.get("plan") or {}
        seg["plan"] = _plan_record(item)
        if new_text is None:  # the text is still the shortened one: keep how it got there
            seg["plan"].update({k: previous[k] for k in ("shortened_from", "shorten", "shorten_attempts") if k in previous})
    if options.get("qc", True) is not False:
        stt_engine = ve.best_installed_stt(ve.default_stt_engines(), prefer=options.get("stt_engine_id"))
        run_qc(stt_engine, seg, target_lang, options.get("qc_threshold"))
    else:
        seg.pop("qc", None)
    video_path = Path(manifest["video_path"])
    chunks = _timeline(video_path, segments, fit_mode)
    manifest["summary"] = _job_summary(segments, chunks, fit_mode)
    _write_manifest(work_dir / "manifest.json", manifest)

    result: dict[str, Any] = {"segment": {k: v for k, v in seg.items() if k != "fitted_path"},
                              "summary": manifest["summary"]}
    if remix:
        outputs = _assemble_dub(store, backend, work_dir, video_path, segments, manifest.get("project_id"),
                                manifest["title"], lambda *a, **k: None, chunks=chunks)
        result["final_video"] = outputs["final_video"]
        if outputs.get("asset_ids"):
            result["asset_id"] = outputs["asset_ids"][0]
    if job_id:
        _update_job_outputs(store, job_id, seg, result.get("asset_id"), manifest["summary"])
    return result
