"""Voice library: turn a clean audio sample into a reusable voice.

Processing pipeline for a new sample: ffmpeg loudness normalisation
(`loudnorm`) + silence trim at both ends, a quality check (duration, a
signal-to-noise estimate, clipping), and - when an STT engine is installed -
a reference transcript. None of this trains anything: the "cloning" is
whatever the chosen TTS engine does at synthesis time from the stored
sample (see `voice_engines.py`); this module only prepares and stores it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import audio as audio_mod
from . import procutil
from .backend import ffmpeg_path
from .store import Store

MIN_SAMPLE_S = 1.0
RECOMMENDED_MAX_S = 5 * 60.0
MIN_GOOD_SNR_DB = 15.0
MAX_GOOD_CLIP_PCT = 0.1  # percent of samples at/near full scale


class VoiceLabError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise VoiceLabError("ffmpeg_missing", "ffmpeg not found: install ffmpeg or the imageio-ffmpeg wheel")
    return exe


def process_sample(src: Path, dest: Path) -> None:
    """Trim leading/trailing silence and loudness-normalise (EBU R128),
    writing a clean 44.1 kHz mono WAV to `dest`.

    Two ffmpeg passes on purpose: `silenceremove` + `areverse` (twice, to
    trim both ends) chained directly into `loudnorm` in one filtergraph can
    make ffmpeg's async resampler loop indefinitely on some inputs (seen
    with a pure, silence-free tone - exactly what the test suite's
    synthetic fixtures look like); two simple passes never hit it.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    trim = (
        "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1:detection=peak,areverse,"
        "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.1:detection=peak,areverse"
    )
    tmp = dest.with_name(dest.stem + ".trim.wav")
    try:
        cmd1 = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "44100",
                "-af", trim, str(tmp)]
        proc = procutil.run(cmd1, timeout=120)
        if proc.returncode != 0 or not tmp.is_file():
            raise VoiceLabError("process_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500] or "ffmpeg failed (trim)")
        trimmed_duration = audio_mod.probe_duration_s(tmp)
        if not trimmed_duration or trimmed_duration < 0.05:
            raise VoiceLabError("silent_sample", f"{src.name} is silent (or too quiet) from end to end: "
                                                 "nothing was left after trimming silence")
        cmd2 = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(tmp), "-af", "loudnorm=I=-19:TP=-2:LRA=7",
                str(dest)]
        proc = procutil.run(cmd2, timeout=120)
        if proc.returncode != 0 or not dest.is_file():
            raise VoiceLabError("process_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500] or "ffmpeg failed (loudnorm)")
    finally:
        tmp.unlink(missing_ok=True)


def quality_check(path: Path) -> dict[str, Any]:
    """Duration, an SNR estimate (RMS of the loudest decile of frames over
    the quietest decile, in dB) and a clipping fraction, plus warnings.
    A cheap, dependency-free approximation - not a lab-grade SNR meter -
    good enough to warn about a noisy room, a whisper-quiet clip or a
    hot-recorded, clipped sample before it is used to clone a voice."""
    duration_s = audio_mod.probe_duration_s(path)
    if duration_s is None:
        raise VoiceLabError("unreadable", f"{path.name} is not a readable audio file")
    samples = audio_mod.decode_to_mono(path, max_duration_s=min(duration_s + 1, 600.0))
    if samples.size == 0:
        raise VoiceLabError("unreadable", f"{path.name} decoded to no audio samples")
    frame = max(1, min(2048, samples.size))  # a very short clip still gets at least one (smaller) frame
    n_frames = max(1, samples.size // frame)
    frames = samples[: n_frames * frame].reshape(n_frames, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12)
    order = np.sort(rms)
    decile = max(1, len(order) // 10)
    noise_floor = float(np.mean(order[:decile]))
    signal = float(np.mean(order[-decile:]))
    snr_db = round(20.0 * np.log10(max(signal, 1e-9) / max(noise_floor, 1e-9)), 1)
    clip_pct = round(float(np.mean(np.abs(samples) >= 0.99)) * 100.0, 3)

    warnings: list[str] = []
    if duration_s < MIN_SAMPLE_S:
        warnings.append(f"very short sample ({duration_s:.1f}s) - at least 3-10s of clean speech is recommended")
    if duration_s > RECOMMENDED_MAX_S:
        warnings.append(f"long sample ({duration_s / 60:.1f} min) - most cloning engines only need 6-30s")
    if snr_db < MIN_GOOD_SNR_DB:
        warnings.append(f"low signal-to-noise ratio (~{snr_db} dB) - background noise or room echo will bleed into the clone")
    if clip_pct > MAX_GOOD_CLIP_PCT:
        warnings.append(f"clipping detected ({clip_pct}% of samples near full scale) - re-record a bit quieter")
    return {
        "duration_s": round(duration_s, 2), "snr_db": snr_db, "clipping_pct": clip_pct,
        "warnings": warnings, "ok": not warnings,
    }


def create_voice(
    store: Store, name: str, sample_src: Path, engine_id: str, language: Optional[str] = None,
    project_id: Optional[str] = None, transcript: Optional[str] = None, stt_engine: Optional[Any] = None,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Process `sample_src`, run the quality check, optionally transcribe it
    (when `stt_engine` is given and installed) and store the voice. Returns
    the store row (see `Store.get_studio_voice`)."""
    if not isinstance(name, str) or not name.strip():
        raise VoiceLabError("name_required", "give the voice a name")
    voices_dir = store.data_dir / "voice_studio" / "voices"
    voice_id_dir = voices_dir / _safe_stub(name)
    dest = voice_id_dir / "sample.wav"
    n = 1
    while dest.exists():
        n += 1
        dest = voice_id_dir.with_name(f"{voice_id_dir.name}-{n}") / "sample.wav"
    process_sample(sample_src, dest)
    quality = quality_check(dest)

    reference_transcript = transcript
    if reference_transcript is None and stt_engine is not None and stt_engine.is_installed():
        try:
            result = stt_engine.transcribe(dest, language=language, word_timestamps=False)
            reference_transcript = result.get("text") or None
        except Exception:  # noqa: BLE001 - a bad transcript must not block voice creation
            reference_transcript = None

    row = store.create_studio_voice(
        name=name, engine_id=engine_id, voice_ref=None, sample_path=_rel(store, dest), language=language,
        cloned=True, reference_transcript=reference_transcript, quality=quality, tags=tags or [],
        project_id=project_id,
    )
    return row


def _safe_stub(name: str) -> str:
    import re

    stub = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")[:60]
    return stub or "voice"


def _rel(store: Store, path: Path) -> str:
    return path.relative_to(store.data_dir).as_posix()


def sample_path(store: Store, voice: dict[str, Any]) -> Optional[Path]:
    if not voice.get("sample_path"):
        return None
    path = (store.data_dir / voice["sample_path"]).resolve()
    root = store.data_dir.resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


class VoiceSpecError(VoiceLabError):
    pass


def resolve_voice_spec(store: Store, tts_engines: list[Any], spec: dict[str, Any]) -> dict[str, Any]:
    """A `voice` request dict (from /api/voice/speak, the audiobook and dub
    pipelines) -> {"engine", "voice_ref", "sample_path", "speed", "pitch",
    "style", "language"}, merging in a library voice's own settings and any
    named preset. `spec`: {engine_id?, voice_id?, voice_ref?, preset?, speed?,
    pitch?, style?, language?}. At least one of engine_id/voice_id is needed."""
    from . import voice_engines as ve

    spec = dict(spec or {})
    library_voice = None
    if spec.get("voice_id"):
        library_voice = store.get_studio_voice(spec["voice_id"])

    engine_id = spec.get("engine_id") or (library_voice["engine_id"] if library_voice else None)
    if not engine_id:
        raise VoiceSpecError("engine_required", "voice needs engine_id or voice_id (a saved library voice)")
    try:
        engine = ve.get_engine(tts_engines, engine_id)
    except KeyError as exc:
        raise VoiceSpecError("unknown_engine", str(exc)) from None

    merged: dict[str, Any] = {}
    if library_voice:
        preset_name = spec.get("preset")
        if preset_name:
            preset = next((p for p in library_voice.get("presets") or [] if p.get("name") == preset_name), None)
            if preset is None:
                raise VoiceSpecError("unknown_preset", f"voice '{library_voice['name']}' has no preset '{preset_name}'")
            merged.update({k: v for k, v in preset.items() if k != "name"})
        merged.setdefault("language", library_voice.get("language"))

    for key in ("speed", "pitch", "style", "language"):
        if spec.get(key) is not None:
            merged[key] = spec[key]

    voice_ref = spec.get("voice_ref") or (library_voice.get("voice_ref") if library_voice else None)
    sample = sample_path(store, library_voice) if library_voice else None
    if engine.capabilities.cloning and not sample and not voice_ref:
        raise VoiceSpecError("cloning_needs_sample", f"engine '{engine.id}' needs a cloning sample (voice_id with a "
                                                      "processed sample, or voice_ref for a pre-made speaker)")
    return {"engine": engine, "voice_ref": voice_ref, "sample_path": sample, "speed": merged.get("speed"),
            "pitch": merged.get("pitch"), "style": merged.get("style"), "language": merged.get("language")}


def synthesize_with_spec(store: Store, tts_engines: list[Any], spec: dict[str, Any], text: str) -> tuple[bytes, str]:
    """Returns (wav_bytes, engine_id actually used)."""
    from .voice_engines import EngineNotInstalled

    resolved = resolve_voice_spec(store, tts_engines, spec)
    engine = resolved.pop("engine")
    if not engine.is_installed():
        raise VoiceSpecError("engine_not_installed", f"'{engine.id}' is not installed: {engine.install_hint()}")
    try:
        wav = engine.synthesize(text, **resolved)
    except EngineNotInstalled as exc:
        raise VoiceSpecError("engine_not_installed", str(exc)) from exc
    return wav, engine.id


def voice_view(voice: dict[str, Any]) -> dict[str, Any]:
    """Compact view for agent/MCP responses: id first, no file paths."""
    return {
        "id": voice["id"], "name": voice["name"], "engine_id": voice["engine_id"], "language": voice.get("language"),
        "cloned": voice["cloned"], "has_sample": bool(voice.get("sample_path")), "quality": voice.get("quality") or {},
        "presets": [p.get("name") for p in voice.get("presets") or []], "tags": voice.get("tags") or [],
        "project_id": voice.get("project_id"), "created_at": voice.get("created_at"),
    }
