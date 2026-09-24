"""Voice library: turn a clean audio sample into a reusable voice.

Processing pipeline for a new sample: ffmpeg loudness normalisation
(`loudnorm`) + silence trim at both ends, a quality check (duration, a
signal-to-noise estimate, clipping), and - when an STT engine is installed -
a reference transcript. None of this trains anything: the "cloning" is
whatever the chosen TTS engine does at synthesis time from the stored
sample (see `voice_engines.py`); this module only prepares and stores it.
"""

from __future__ import annotations

import inspect
import io
import re
import shutil
import wave
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
MAX_PITCH_SEMITONES = 12.0
LEXICON_PRESET = "_lexicon"  # reserved preset entry holding a voice's pronunciation lexicon
MAX_LEXICON_ENTRIES = 500


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
        # ffmpeg's loudnorm filter has a well-known quirk of emitting at
        # 192kHz (for its internal true-peak oversampling) regardless of
        # the input rate; force the output back to 44.1kHz explicitly so
        # `dest` is actually the "clean 44.1 kHz mono WAV" this promises,
        # not a 192kHz file some readers (voice-cloning engines included)
        # do not expect.
        cmd2 = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(tmp), "-af", "loudnorm=I=-19:TP=-2:LRA=7",
                "-ar", "44100", str(dest)]
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


def delete_voice_files(store: Store, voice: dict[str, Any]) -> bool:
    """Remove a library voice's own folder (its processed sample - a
    recording of a person's voice - and anything next to it) when the voice
    is deleted. Only ever a folder directly under
    `data/voice_studio/voices/`; returns whether one was removed."""
    rel = voice.get("sample_path")
    if not rel:
        return False
    root = (store.data_dir / "voice_studio" / "voices").resolve()
    folder = (store.data_dir / rel).resolve().parent
    if folder.parent != root or not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return not folder.exists()


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


# ------------------------------------------------------ pronunciation lexicon

def normalize_lexicon(lexicon: Any) -> dict[str, str]:
    """Validate a pronunciation lexicon: {"written word": "how to say it"}
    (e.g. {"Prospero": "PROS-per-oh"}); raises VoiceSpecError otherwise."""
    if lexicon is None:
        return {}
    if not isinstance(lexicon, dict):
        raise VoiceSpecError("bad_lexicon", 'lexicon must be an object such as {"Prospero": "PROS-per-oh"}')
    if len(lexicon) > MAX_LEXICON_ENTRIES:
        raise VoiceSpecError("bad_lexicon", f"a lexicon holds at most {MAX_LEXICON_ENTRIES} entries")
    out: dict[str, str] = {}
    for term, say in lexicon.items():
        if not isinstance(term, str) or not isinstance(say, str) or not term.strip() or len(term) > 100 or len(say) > 200:
            raise VoiceSpecError("bad_lexicon", "each lexicon entry maps a word (<=100 chars) to its spoken form "
                                                "(<=200 chars)")
        out[term.strip()] = say.strip()
    return out


def apply_lexicon(text: str, lexicon: Optional[dict[str, str]]) -> str:
    """Replace each lexicon term with its spoken form, whole words only and
    case-insensitively ("prospero's" -> "PROS-per-oh's", "Prosperous" is
    left alone); longer terms win over shorter ones they contain."""
    if not lexicon or not text:
        return text
    terms = sorted((t for t in lexicon if t), key=len, reverse=True)
    lookup = {t.lower(): lexicon[t] for t in terms}
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.IGNORECASE)
    return pattern.sub(lambda m: lookup.get(m.group(0).lower(), m.group(0)), text)


def voice_lexicon(voice: Optional[dict[str, Any]]) -> dict[str, str]:
    """A library voice's own lexicon (kept as a reserved entry in its
    presets list, so it needs no schema change)."""
    for p in (voice or {}).get("presets") or []:
        if p.get("name") == LEXICON_PRESET:
            return dict(p.get("lexicon") or {})
    return {}


def set_voice_lexicon(store: Store, voice_id: str, lexicon: Any) -> dict[str, Any]:
    """Replace a library voice's lexicon ({} clears it); returns the row."""
    clean = normalize_lexicon(lexicon)
    voice = store.get_studio_voice(voice_id)
    presets = [p for p in voice.get("presets") or [] if p.get("name") != LEXICON_PRESET]
    if clean:
        presets.append({"name": LEXICON_PRESET, "lexicon": clean})
    return store.update_studio_voice(voice_id, presets=presets)


def user_presets(voice: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in voice.get("presets") or [] if not str(p.get("name") or "").startswith("_")]


# ---------------------------------------------------------------- pitch

def _atempo_stages(factor: float) -> list[float]:
    """`factor` as `atempo` stages each within 0.5-2.0 (the range every
    ffmpeg version accepts - newer ones allow more, 4.2 does not)."""
    stages: list[float] = []
    remaining = factor
    while remaining > 2.0:
        stages.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        stages.append(0.5)
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-6 or not stages:
        stages.append(remaining)
    return stages


def apply_pitch(wav: bytes, semitones: Optional[float]) -> bytes:
    """Shift a WAV's pitch by `semitones` (-12..12) keeping its duration:
    `asetrate` (pitch and tempo up by 2^(n/12)) + `aresample` back to the
    original rate + `atempo` to undo the tempo change. None/0 is a no-op."""
    if not semitones:
        return wav
    try:
        semitones = float(semitones)
    except (TypeError, ValueError):
        raise VoiceSpecError("bad_pitch", "pitch is a number of semitones between -12 and 12") from None
    if abs(semitones) > MAX_PITCH_SEMITONES:
        raise VoiceSpecError("bad_pitch", "pitch is a number of semitones between -12 and 12")
    with wave.open(io.BytesIO(wav), "rb") as wf:
        sr = wf.getframerate()
    ratio = 2.0 ** (semitones / 12.0)
    tempo = ",".join(f"atempo={s:.6f}" for s in _atempo_stages(1.0 / ratio))
    filters = f"asetrate={int(round(sr * ratio))},aresample={sr},{tempo}"
    cmd = [_ffmpeg(), "-nostdin", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-af", filters,
           "-ac", "1", "-ar", str(sr), "-f", "f32le", "pipe:1"]
    proc = procutil.run(cmd, input=wav, timeout=120)
    if proc.returncode != 0:
        raise VoiceLabError("pitch_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500] or "ffmpeg failed")
    from .voice_engines import wav_bytes_mono16

    return wav_bytes_mono16(np.frombuffer(proc.stdout, dtype="<f4"), sr)


def resolve_voice_spec(store: Store, tts_engines: list[Any], spec: dict[str, Any]) -> dict[str, Any]:
    """A `voice` request dict (from /api/voice/speak, the audiobook and dub
    pipelines) -> {"engine", "voice_ref", "sample_path", "speed", "pitch",
    "style", "language", "ref_text", "lexicon"}, merging in a library voice's
    own settings, lexicon and reference transcript and any named preset.
    `spec`: {engine_id?, voice_id?, voice_ref?, preset?, speed?, pitch?,
    style?, language?, lexicon?}. At least one of engine_id/voice_id is
    needed. Lexicons merge voice < preset < request."""
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
    lexicon: dict[str, str] = {}
    if library_voice:
        lexicon.update(voice_lexicon(library_voice))
        preset_name = spec.get("preset")
        if preset_name:
            preset = next((p for p in user_presets(library_voice) if p.get("name") == preset_name), None)
            if preset is None:
                raise VoiceSpecError("unknown_preset", f"voice '{library_voice['name']}' has no preset '{preset_name}'")
            merged.update({k: v for k, v in preset.items() if k not in ("name", "lexicon")})
            lexicon.update(normalize_lexicon(preset.get("lexicon")))
        merged.setdefault("language", library_voice.get("language"))
    lexicon.update(normalize_lexicon(spec.get("lexicon")))

    for key in ("speed", "pitch", "style", "language"):
        if spec.get(key) is not None:
            merged[key] = spec[key]

    voice_ref = spec.get("voice_ref") or (library_voice.get("voice_ref") if library_voice else None)
    sample = sample_path(store, library_voice) if library_voice else None
    if engine.capabilities.cloning and not sample and not voice_ref:
        raise VoiceSpecError("cloning_needs_sample", f"engine '{engine.id}' needs a cloning sample (voice_id with a "
                                                      "processed sample, or voice_ref for a pre-made speaker)")
    # a sample-cloning engine uses the library voice's own transcript of
    # its sample as the reference text (F5-TTS needs it; never the style)
    ref_text = (library_voice.get("reference_transcript") or None) if library_voice and sample else None
    return {"engine": engine, "voice_ref": voice_ref, "sample_path": sample, "speed": merged.get("speed"),
            "pitch": merged.get("pitch"), "style": merged.get("style"), "language": merged.get("language"),
            "ref_text": ref_text, "lexicon": lexicon}


def validate_voice_spec(store: Store, tts_engines: list[Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Everything `synthesize_with_spec` would reject, checked up front (a
    route calls this before queueing an audiobook or dub job so a bad voice
    fails the request, not the job an hour later). Returns the resolved spec."""
    from .voice_engines import UnsupportedLanguage

    resolved = resolve_voice_spec(store, tts_engines, spec)
    engine = resolved["engine"]
    if not engine.is_installed():
        raise VoiceSpecError("engine_not_installed", f"'{engine.id}' is not installed: {engine.install_hint()}")
    if resolved.get("pitch"):
        pitch = resolved["pitch"]
        if not isinstance(pitch, (int, float)) or abs(pitch) > MAX_PITCH_SEMITONES:
            raise VoiceSpecError("bad_pitch", "pitch is a number of semitones between -12 and 12")
    try:
        engine.check_language(resolved.get("language"))
    except UnsupportedLanguage as exc:
        raise VoiceSpecError("unsupported_language", str(exc)) from None
    return resolved


def _accepts(fn: Any, name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def synthesize_with_spec(store: Store, tts_engines: list[Any], spec: dict[str, Any], text: str) -> tuple[bytes, str]:
    """Returns (wav_bytes, engine_id actually used). Applies the merged
    pronunciation lexicon to `text` first and the requested pitch shift to
    the result (no engine here shifts pitch natively)."""
    from .voice_engines import EngineNotInstalled, UnsupportedLanguage

    resolved = resolve_voice_spec(store, tts_engines, spec)
    engine = resolved.pop("engine")
    lexicon = resolved.pop("lexicon", None)
    ref_text = resolved.pop("ref_text", None)
    pitch = resolved.get("pitch")
    resolved["pitch"] = None
    if not engine.is_installed():
        raise VoiceSpecError("engine_not_installed", f"'{engine.id}' is not installed: {engine.install_hint()}")
    kwargs = dict(resolved)
    if ref_text and _accepts(engine.synthesize, "ref_text"):
        kwargs["ref_text"] = ref_text
    try:
        wav = engine.synthesize(apply_lexicon(text, lexicon), **kwargs)
    except EngineNotInstalled as exc:
        raise VoiceSpecError("engine_not_installed", str(exc)) from exc
    except UnsupportedLanguage as exc:
        raise VoiceSpecError("unsupported_language", str(exc)) from exc
    return apply_pitch(wav, pitch), engine.id


def voice_view(voice: dict[str, Any]) -> dict[str, Any]:
    """Compact view for agent/MCP responses: id first, no file paths."""
    return {
        "id": voice["id"], "name": voice["name"], "engine_id": voice["engine_id"], "language": voice.get("language"),
        "cloned": voice["cloned"], "has_sample": bool(voice.get("sample_path")), "quality": voice.get("quality") or {},
        "presets": [p.get("name") for p in user_presets(voice)], "lexicon": voice_lexicon(voice),
        "tags": voice.get("tags") or [], "project_id": voice.get("project_id"), "created_at": voice.get("created_at"),
    }
