"""Pluggable TTS and STT engine registry for the Voice studio.

Every engine is an optional import: the class itself has no hard
dependency, `is_installed()` only checks whether the package is on disk
(cheap, no heavy import), and the real `import` happens inside
`synthesize`/`transcribe`, the first time the engine is actually used.
Nothing here downloads a model file or installs a package on its own -
`install_job` (used by the `/api/voice/engines/{id}/install` route) is the
one explicit, user-triggered action that runs `pip install`.

`voices.py` (Piper for character narration) is untouched and still used
as-is; `PiperEngine` below wraps it so Piper also shows up in the studio's
unified engine list next to the newer cloning-capable engines.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import procutil
from . import voices as voices_mod


class EngineNotInstalled(RuntimeError):
    def __init__(self, engine_id: str, install_hint: str):
        super().__init__(f"'{engine_id}' is not installed: {install_hint}")
        self.engine_id = engine_id
        self.install_hint = install_hint


@dataclass(frozen=True)
class EngineCapabilities:
    languages: list[str] = field(default_factory=list)
    cloning: bool = False
    streaming: bool = False
    needs_gpu: bool = False
    multi_speaker: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"languages": self.languages, "cloning": self.cloning, "streaming": self.streaming,
                "needs_gpu": self.needs_gpu, "multi_speaker": self.multi_speaker}


def _spec_installed(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def wav_bytes_mono16(samples, sample_rate: int) -> bytes:
    """A numpy float32 [-1, 1] array -> a 16-bit PCM mono WAV file's bytes."""
    import numpy as np

    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(pcm.tobytes())
    return buf.getvalue()


# --------------------------------------------------------------- TTS base --

class TTSEngine:
    """Interface every text-to-speech adapter implements."""

    id = "base-tts"
    label = "Base TTS"
    pip_packages: list[str] = []
    capabilities = EngineCapabilities()

    def is_installed(self) -> bool:
        raise NotImplementedError

    def install_hint(self) -> str:
        return f"pip install {' '.join(self.pip_packages)}" if self.pip_packages else "no installer known"

    def status(self) -> dict[str, Any]:
        installed = self.is_installed()
        return {
            "id": self.id, "label": self.label, "kind": "tts", "installed": installed,
            "capabilities": self.capabilities.to_dict(),
            "install_hint": None if installed else self.install_hint(),
            "reason": "installed and ready" if installed else f"not installed - {self.install_hint()}",
        }

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        """Returns a WAV file's bytes. `sample_path` is a clean reference
        sample for engines that clone a voice (`capabilities.cloning`)."""
        raise NotImplementedError


class PiperEngine(TTSEngine):
    """Wraps the existing `voices.py` Piper backend (curated voices,
    downloaded on first use). No cloning: Piper's voices are generic
    synthetic voices trained on public datasets."""

    id = "piper"
    label = "Piper (curated voices)"
    pip_packages = ["piper-tts"]
    capabilities = EngineCapabilities(
        languages=sorted({v["lang"] for v in voices_mod.CURATED_VOICES}), cloning=False, streaming=False,
        needs_gpu=False, multi_speaker=False,
    )

    def __init__(self, voices_dir: Optional[Path] = None):
        self.voices_dir = voices_dir

    def is_installed(self) -> bool:
        return voices_mod.piper_installed()

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        voices_dir = self.voices_dir or Path("data/voices")
        voice_id = voice_ref or voices_mod.CURATED_VOICES[0]["id"]
        try:
            return voices_mod.synthesize_piper(voices_dir, voice_id, text, speed)
        except voices_mod.VoiceError as exc:
            raise EngineNotInstalled(self.id, str(exc)) from exc


class XTTSEngine(TTSEngine):
    """Coqui XTTS-v2 (the `TTS` package): zero-shot voice cloning from a
    single 6-30s clean sample, 17 languages. Heavier install; a GPU speeds
    it up a lot but CPU inference works for short lines."""

    id = "xtts"
    label = "Coqui XTTS-v2"
    pip_packages = ["TTS"]
    capabilities = EngineCapabilities(
        languages=["en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko", "hi"],
        cloning=True, streaming=False, needs_gpu=False, multi_speaker=False,
    )
    _model_name = "tts_models/multilingual/multi-dataset/xtts_v2"
    _cache: dict[str, Any] = {}

    def is_installed(self) -> bool:
        return _spec_installed("TTS")

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        if not sample_path:
            raise EngineNotInstalled(self.id, "XTTS needs a reference sample (voice_ref/sample_path) to clone from")
        from TTS.api import TTS  # noqa: N811 - upstream package name

        model = XTTSEngine._cache.get(self._model_name)
        if model is None:
            model = TTS(self._model_name, progress_bar=False)
            XTTSEngine._cache[self._model_name] = model
        import numpy as np

        wav = model.tts(text=text, speaker_wav=str(sample_path), language=(language or "en")[:2],
                        speed=speed or 1.0)
        return wav_bytes_mono16(np.asarray(wav, dtype="float32"), 24000)


class F5TTSEngine(TTSEngine):
    """F5-TTS: fast flow-matching zero-shot cloning, strong on English and
    Chinese, needs a short reference clip plus its transcript."""

    id = "f5-tts"
    label = "F5-TTS"
    pip_packages = ["f5-tts"]
    capabilities = EngineCapabilities(languages=["en", "zh"], cloning=True, streaming=False, needs_gpu=True,
                                      multi_speaker=False)

    def is_installed(self) -> bool:
        return _spec_installed("f5_tts")

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        if not sample_path:
            raise EngineNotInstalled(self.id, "F5-TTS needs a reference sample (and its transcript) to clone from")
        from f5_tts.api import F5TTS  # type: ignore[import-not-found]

        model = F5TTS()
        wav, sr, _ = model.infer(ref_file=str(sample_path), ref_text=style or "", gen_text=text, speed=speed or 1.0)
        return wav_bytes_mono16(wav, sr)


class KokoroEngine(TTSEngine):
    """Kokoro: a small, fast multilingual CPU-friendly model with a fixed
    set of named voice packs (no cloning from a sample)."""

    id = "kokoro"
    label = "Kokoro"
    pip_packages = ["kokoro>=0.9.4", "soundfile"]
    capabilities = EngineCapabilities(languages=["en", "es", "fr", "it", "pt", "ja", "zh", "hi"], cloning=False,
                                      streaming=True, needs_gpu=False, multi_speaker=True)

    def is_installed(self) -> bool:
        return _spec_installed("kokoro")

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        from kokoro import KPipeline  # type: ignore[import-not-found]
        import numpy as np

        pipeline = KPipeline(lang_code=(language or "a"))
        chunks = [audio for _, _, audio in pipeline(text, voice=voice_ref or "af_heart", speed=speed or 1.0)]
        wav = np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")
        return wav_bytes_mono16(wav, 24000)


class ChatterboxEngine(TTSEngine):
    """Resemble AI's Chatterbox: English-focused zero-shot cloning with an
    "exaggeration" style control."""

    id = "chatterbox"
    label = "Chatterbox"
    pip_packages = ["chatterbox-tts"]
    capabilities = EngineCapabilities(languages=["en"], cloning=True, streaming=False, needs_gpu=True,
                                      multi_speaker=False)

    def is_installed(self) -> bool:
        return _spec_installed("chatterbox")

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        from chatterbox.tts import ChatterboxTTS  # type: ignore[import-not-found]

        model = ChatterboxTTS.from_pretrained(device="cuda" if _cuda_available() else "cpu")
        exaggeration = 0.5
        try:
            exaggeration = float(style) if style else 0.5
        except ValueError:
            pass
        wav = model.generate(text, audio_prompt_path=str(sample_path) if sample_path else None,
                             exaggeration=exaggeration)
        return wav_bytes_mono16(wav.squeeze().cpu().numpy(), model.sr)


def _cuda_available() -> bool:
    try:
        import torch  # type: ignore[import-not-found]

        return bool(torch.cuda.is_available())
    except Exception:
        return False


class ComfyTTSEngine(TTSEngine):
    """Status-only adapter: reports whether ComfyUI's `/object_info`
    exposes a known TTS node pack (e.g. ComfyUI-VibeVoice, F5-TTS or
    Kokoro custom nodes). Prospero has no built-in TTS workflow template
    yet, so `synthesize` is not implemented; this only feeds `voice_engines`
    so the studio can say "seen in ComfyUI" instead of hiding it."""

    id = "comfy-tts"
    label = "ComfyUI TTS node pack"
    capabilities = EngineCapabilities(cloning=True, needs_gpu=True)

    _NODE_HINTS = ("VibeVoice", "F5TTS", "ChatterboxTTS", "KokoroTTS", "IndexTTS")

    def __init__(self, object_info: Optional[dict[str, Any]] = None):
        self._object_info = object_info or {}
        self._found = [n for n in self._object_info if any(h.lower() in n.lower() for h in self._NODE_HINTS)]

    def is_installed(self) -> bool:
        return bool(self._found)

    def install_hint(self) -> str:
        return ("install a TTS custom node pack in ComfyUI (e.g. ComfyUI-VibeVoice, F5-TTS or Kokoro nodes) "
                "and restart it")

    def status(self) -> dict[str, Any]:
        base = super().status()
        if self._found:
            base["reason"] = f"ComfyUI has TTS node(s): {', '.join(self._found)} (no built-in workflow template yet)"
        return base

    def synthesize(self, *args: Any, **kwargs: Any) -> bytes:
        raise NotImplementedError("ComfyTTSEngine has no synthesis workflow yet; use it for status only")


# --------------------------------------------------------------- STT base --

@dataclass(frozen=True)
class TranscriptSegment:
    start_s: float
    end_s: float
    text: str
    words: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"start_s": round(self.start_s, 3), "end_s": round(self.end_s, 3), "text": self.text,
                "words": self.words}


class STTEngine:
    """Interface every speech-to-text adapter implements."""

    id = "base-stt"
    label = "Base STT"
    pip_packages: list[str] = []
    capabilities = EngineCapabilities()

    def is_installed(self) -> bool:
        raise NotImplementedError

    def install_hint(self) -> str:
        return f"pip install {' '.join(self.pip_packages)}" if self.pip_packages else "no installer known"

    def status(self) -> dict[str, Any]:
        installed = self.is_installed()
        return {
            "id": self.id, "label": self.label, "kind": "stt", "installed": installed,
            "capabilities": self.capabilities.to_dict(),
            "install_hint": None if installed else self.install_hint(),
            "reason": "installed and ready" if installed else f"not installed - {self.install_hint()}",
        }

    def transcribe(self, path: Path, language: Optional[str] = None,
                   word_timestamps: bool = True) -> dict[str, Any]:
        """Returns {"language", "text", "segments": [TranscriptSegment.to_dict(), ...]}."""
        raise NotImplementedError


class FasterWhisperEngine(STTEngine):
    """faster-whisper (CTranslate2 Whisper): the primary STT engine - word
    timestamps, runs on CPU, much faster than the reference implementation."""

    id = "faster-whisper"
    label = "faster-whisper"
    pip_packages = ["faster-whisper"]
    capabilities = EngineCapabilities(
        languages=["auto", "en", "es", "fr", "de", "it", "pt", "ja", "zh", "ru", "ko", "ar", "hi"],
        streaming=False, needs_gpu=False,
    )
    _cache: dict[str, Any] = {}

    def __init__(self, model_size: str = "small"):
        self.model_size = model_size

    def is_installed(self) -> bool:
        return _spec_installed("faster_whisper")

    def transcribe(self, path: Path, language: Optional[str] = None, word_timestamps: bool = True) -> dict[str, Any]:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        model = FasterWhisperEngine._cache.get(self.model_size)
        if model is None:
            model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            FasterWhisperEngine._cache[self.model_size] = model
        segments, info = model.transcribe(str(path), language=language, word_timestamps=word_timestamps)
        out = []
        text_parts = []
        for seg in segments:
            words = [{"start_s": round(w.start, 3), "end_s": round(w.end, 3), "word": w.word}
                     for w in (seg.words or [])] if word_timestamps and seg.words else []
            out.append(TranscriptSegment(seg.start, seg.end, seg.text.strip(), words).to_dict())
            text_parts.append(seg.text.strip())
        return {"language": info.language, "text": " ".join(text_parts).strip(), "segments": out}


class OpenAIWhisperEngine(STTEngine):
    """The original `openai-whisper` package: heavier and slower than
    faster-whisper, kept as a fallback for setups that already have it."""

    id = "whisper"
    label = "OpenAI Whisper"
    pip_packages = ["openai-whisper"]
    capabilities = EngineCapabilities(
        languages=["auto", "en", "es", "fr", "de", "it", "pt", "ja", "zh", "ru", "ko", "ar", "hi"],
        streaming=False, needs_gpu=False,
    )
    _cache: dict[str, Any] = {}

    def __init__(self, model_size: str = "small"):
        self.model_size = model_size

    def is_installed(self) -> bool:
        return _spec_installed("whisper")

    def transcribe(self, path: Path, language: Optional[str] = None, word_timestamps: bool = True) -> dict[str, Any]:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        import whisper  # type: ignore[import-not-found]

        model = OpenAIWhisperEngine._cache.get(self.model_size)
        if model is None:
            model = whisper.load_model(self.model_size)
            OpenAIWhisperEngine._cache[self.model_size] = model
        result = model.transcribe(str(path), language=language, word_timestamps=word_timestamps)
        out = []
        for seg in result.get("segments", []):
            words = [{"start_s": round(w["start"], 3), "end_s": round(w["end"], 3), "word": w["word"]}
                     for w in seg.get("words", [])] if word_timestamps else []
            out.append(TranscriptSegment(seg["start"], seg["end"], seg["text"].strip(), words).to_dict())
        return {"language": result.get("language"), "text": result.get("text", "").strip(), "segments": out}


# ------------------------------------------------------------- registries --

def default_tts_engines(voices_dir: Optional[Path] = None, object_info: Optional[dict[str, Any]] = None) -> list[TTSEngine]:
    return [PiperEngine(voices_dir), XTTSEngine(), F5TTSEngine(), KokoroEngine(), ChatterboxEngine(),
            ComfyTTSEngine(object_info)]


def default_stt_engines() -> list[STTEngine]:
    return [FasterWhisperEngine(), OpenAIWhisperEngine()]


def list_engine_status(tts_engines: list[TTSEngine], stt_engines: list[STTEngine]) -> dict[str, Any]:
    return {"tts": [e.status() for e in tts_engines], "stt": [e.status() for e in stt_engines]}


def get_engine(engines: list[Any], engine_id: str) -> Any:
    for e in engines:
        if e.id == engine_id:
            return e
    known = ", ".join(e.id for e in engines)
    raise KeyError(f"unknown engine '{engine_id}'; known engines: {known}")


def best_installed_tts(engines: list[TTSEngine], prefer: Optional[str] = None, needs_cloning: bool = False) -> Optional[TTSEngine]:
    candidates = [e for e in engines if e.is_installed() and (not needs_cloning or e.capabilities.cloning)]
    if prefer:
        preferred = next((e for e in candidates if e.id == prefer), None)
        if preferred:
            return preferred
    return candidates[0] if candidates else None


def best_installed_stt(engines: list[STTEngine], prefer: Optional[str] = None) -> Optional[STTEngine]:
    candidates = [e for e in engines if e.is_installed()]
    if prefer:
        preferred = next((e for e in candidates if e.id == prefer), None)
        if preferred:
            return preferred
    return candidates[0] if candidates else None


# ---------------------------------------------------------- subtitle export

def _split_ms(t: float) -> tuple[int, int, int, int]:
    """`t` seconds -> (hours, minutes, seconds, milliseconds), rounded to the
    nearest millisecond *before* splitting into fields so a value like
    59.9996s carries into the next second (and, at a minute/hour boundary,
    into the next minute/hour) instead of producing an out-of-range field
    such as "59,1000" or "01:60.000" (both invalid in SRT/VTT)."""
    total_ms = int(round(max(0.0, t) * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return h, m, s, ms


def _srt_time(t: float) -> str:
    h, m, s, ms = _split_ms(t)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_time(t: float) -> str:
    h, m, s, ms = _split_ms(t)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def segments_to_srt(segments: list[dict[str, Any]]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_srt_time(seg['start_s'])} --> {_srt_time(seg['end_s'])}")
        lines.append(seg.get("text", "").strip())
        lines.append("")
    return "\n".join(lines)


def segments_to_vtt(segments: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{_vtt_time(seg['start_s'])} --> {_vtt_time(seg['end_s'])}")
        lines.append(seg.get("text", "").strip())
        lines.append("")
    return "\n".join(lines)


def segments_to_txt(segments: list[dict[str, Any]]) -> str:
    return "\n".join(seg.get("text", "").strip() for seg in segments if seg.get("text", "").strip())


def install_engine(engine: Any, python: Optional[str] = None) -> dict[str, Any]:
    """The one explicit, user-triggered install action: `pip install` the
    engine's packages into this app's own venv. Never called on its own."""
    if not engine.pip_packages:
        raise EngineNotInstalled(engine.id, "no installer known for this engine")
    exe = python or sys.executable
    cmd = [exe, "-m", "pip", "install", *engine.pip_packages]
    proc = procutil.run(cmd, text=True, timeout=1800)
    return {
        "ok": proc.returncode == 0, "returncode": proc.returncode, "command": cmd,
        "stdout_tail": (proc.stdout or "")[-2000:], "stderr_tail": (proc.stderr or "")[-2000:],
        "installed": engine.is_installed(),
    }
