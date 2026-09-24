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

import gc
import importlib.util
import io
import sys
import threading
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


class UnsupportedLanguage(ValueError):
    """A language an engine cannot speak, or free text that is not a
    language at all (see `normalize_language`)."""

    def __init__(self, language: Any, detail: str = ""):
        super().__init__(f"unsupported language '{language}'" + (f": {detail}" if detail else ""))
        self.language = language


# ------------------------------------------------------------ languages --
# Canonical codes are ISO 639-1 (plus "zh" for Mandarin); requests may say
# "es", "es-ES", "es_ES", "Spanish", "español" or "Castellano" and all mean
# "es". Each engine maps the canonical code to its own convention below.

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "pl": "Polish", "tr": "Turkish", "ru": "Russian", "nl": "Dutch", "cs": "Czech", "ar": "Arabic",
    "zh": "Chinese", "ja": "Japanese", "hu": "Hungarian", "ko": "Korean", "hi": "Hindi", "ca": "Catalan",
    "gl": "Galician", "eu": "Basque", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "el": "Greek", "he": "Hebrew", "uk": "Ukrainian", "ro": "Romanian", "id": "Indonesian",
    "vi": "Vietnamese", "th": "Thai",
}

_LANGUAGE_ALIASES: dict[str, str] = {
    "english": "en", "ingles": "en", "inglés": "en", "anglais": "en", "englisch": "en",
    "spanish": "es", "espanol": "es", "español": "es", "castellano": "es", "castilian": "es", "espagnol": "es",
    "spanisch": "es",
    "french": "fr", "frances": "fr", "francés": "fr", "francais": "fr", "français": "fr",
    "german": "de", "aleman": "de", "alemán": "de", "deutsch": "de", "allemand": "de",
    "italian": "it", "italiano": "it", "italien": "it",
    "portuguese": "pt", "portugues": "pt", "português": "pt", "portugués": "pt",
    "polish": "pl", "polaco": "pl", "polski": "pl",
    "turkish": "tr", "turco": "tr", "türkçe": "tr",
    "russian": "ru", "ruso": "ru", "русский": "ru",
    "dutch": "nl", "neerlandes": "nl", "neerlandés": "nl", "holandes": "nl", "holandés": "nl", "nederlands": "nl",
    "czech": "cs", "checo": "cs", "čeština": "cs",
    "arabic": "ar", "arabe": "ar", "árabe": "ar", "العربية": "ar",
    "chinese": "zh", "mandarin": "zh", "chino": "zh", "mandarín": "zh", "中文": "zh", "zh-cn": "zh", "zh-hans": "zh",
    "zh-tw": "zh", "zh-hant": "zh", "cmn": "zh",
    "japanese": "ja", "japones": "ja", "japonés": "ja", "日本語": "ja", "jp": "ja",
    "hungarian": "hu", "hungaro": "hu", "húngaro": "hu", "magyar": "hu",
    "korean": "ko", "coreano": "ko", "한국어": "ko", "kr": "ko",
    "hindi": "hi", "हिन्दी": "hi",
    "catalan": "ca", "catalán": "ca", "català": "ca",
    "galician": "gl", "gallego": "gl", "galego": "gl",
    "basque": "eu", "euskera": "eu", "vasco": "eu", "euskara": "eu",
    "swedish": "sv", "sueco": "sv", "svenska": "sv",
    "danish": "da", "danes": "da", "danés": "da", "dansk": "da",
    "norwegian": "no", "noruego": "no", "norsk": "no", "nb": "no", "nn": "no",
    "finnish": "fi", "finlandes": "fi", "finlandés": "fi", "suomi": "fi",
    "greek": "el", "griego": "el", "ελληνικά": "el",
    "hebrew": "he", "hebreo": "he", "עברית": "he", "iw": "he",
    "ukrainian": "uk", "ucraniano": "uk", "українська": "uk",
    "romanian": "ro", "rumano": "ro", "română": "ro",
    "indonesian": "id", "indonesio": "id", "bahasa indonesia": "id",
    "vietnamese": "vi", "vietnamita": "vi", "tiếng việt": "vi",
    "thai": "th", "tailandes": "th", "tailandés": "th", "ไทย": "th",
}


def normalize_language(value: Any, allow_auto: bool = False) -> Optional[str]:
    """Free text or a code -> a canonical ISO 639-1 code ("Spanish",
    "español", "es-ES", "es_ES" -> "es"). None/"" (and "auto", when
    `allow_auto`) -> None, meaning "let the engine decide/detect". Raises
    `UnsupportedLanguage` for anything it does not recognise."""
    if value is None:
        return None
    text = " ".join(str(value).strip().lower().replace("_", "-").split())
    if not text or (allow_auto and text in ("auto", "detect", "automatic", "automatico", "automático")):
        return None
    if text in LANGUAGE_NAMES:
        return text
    if text in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[text]
    base = text.split("-")[0]
    if len(base) == 2 and base in LANGUAGE_NAMES:
        return base
    raise UnsupportedLanguage(value, "use an ISO code such as 'es' or a name such as 'Spanish'")


def language_name(code: Optional[str]) -> Optional[str]:
    """A canonical code's English name, for an LLM prompt ("es" -> "Spanish")."""
    return LANGUAGE_NAMES.get(code or "", code)


# ---------------------------------------------------------- model cache --
# Loading a TTS/STT model takes seconds to minutes and hundreds of MB to
# GBs of (V)RAM, and synthesis runs once per sentence (audiobooks) or per
# segment (dubs); so every engine keeps its loaded model here, keyed by
# what it was loaded with, and serializes load + inference behind one lock
# per engine (these models are not thread-safe, and two CPU-lane jobs plus
# an interactive "speak" could otherwise load the same model twice).

_MODELS: dict[tuple[Any, ...], Any] = {}
_MODELS_LOCK = threading.Lock()
_ENGINE_LOCKS: dict[str, threading.RLock] = {}


def engine_lock(engine_id: str) -> threading.RLock:
    with _MODELS_LOCK:
        lock = _ENGINE_LOCKS.get(engine_id)
        if lock is None:
            lock = _ENGINE_LOCKS[engine_id] = threading.RLock()
        return lock


def cached_model(engine_id: str, key: tuple[Any, ...], loader: Any) -> Any:
    """The model for `(engine_id, *key)`, loaded with `loader()` the first
    time. Call it with `engine_lock(engine_id)` held."""
    full_key = (engine_id, *key)
    with _MODELS_LOCK:
        model = _MODELS.get(full_key)
    if model is None:
        model = loader()
        with _MODELS_LOCK:
            _MODELS[full_key] = model
    return model


def loaded_models() -> list[str]:
    with _MODELS_LOCK:
        return [":".join(str(k) for k in key) for key in _MODELS]


def unload_models() -> dict[str, Any]:
    """Drop every cached voice model (waiting for an in-flight synthesis on
    that engine to finish first) and hand the memory back - the voice
    studio's half of the app's "free memory" action."""
    with _MODELS_LOCK:
        engine_ids = sorted({key[0] for key in _MODELS})
    count = 0
    for engine_id in engine_ids:
        with engine_lock(engine_id):
            with _MODELS_LOCK:
                keys = [k for k in _MODELS if k[0] == engine_id]
                for k in keys:
                    _MODELS.pop(k, None)
            count += len(keys)
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - freeing memory is best effort
            pass
    return {"unloaded": count, "engines": engine_ids}


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

    def check_language(self, language: Optional[str]) -> None:
        """Raise `UnsupportedLanguage` up front (before a job is queued)
        when this engine cannot speak `language`; engines without a fixed
        language convention accept anything."""
        return None

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        """Returns a WAV file's bytes. `sample_path` is a clean reference
        sample for engines that clone a voice (`capabilities.cloning`).
        `pitch` is applied by the caller (`voice_lab.synthesize_with_spec`
        post-processes it with ffmpeg), so engines receive None."""
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
        voice_id = voices_mod.validate_voice_id(voice_ref or voices_mod.CURATED_VOICES[0]["id"])
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

    def is_installed(self) -> bool:
        return _spec_installed("TTS")

    @staticmethod
    def engine_language(language: Optional[str]) -> str:
        """XTTS's own codes: two letters, except Mandarin's "zh-cn"."""
        code = normalize_language(language) or "en"
        xtts = "zh-cn" if code == "zh" else code
        if xtts not in XTTSEngine.capabilities.languages:
            raise UnsupportedLanguage(language, f"XTTS speaks {', '.join(XTTSEngine.capabilities.languages)}")
        return xtts

    def check_language(self, language: Optional[str]) -> None:
        self.engine_language(language)

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        if not sample_path:
            raise EngineNotInstalled(self.id, "XTTS needs a reference sample (voice_ref/sample_path) to clone from")
        xtts_language = self.engine_language(language)
        from TTS.api import TTS  # noqa: N811 - upstream package name
        import numpy as np

        device = "cuda" if _cuda_available() else "cpu"

        def load() -> Any:
            model = TTS(self._model_name, progress_bar=False)
            return model.to(device) if hasattr(model, "to") else model

        with engine_lock(self.id):
            model = cached_model(self.id, (self._model_name, device), load)
            wav = model.tts(text=text, speaker_wav=str(sample_path), language=xtts_language, speed=speed or 1.0)
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
                   sample_path: Optional[Path] = None, language: Optional[str] = None,
                   ref_text: Optional[str] = None) -> bytes:
        """`ref_text` is the reference sample's transcript (a library
        voice's `reference_transcript`); left empty, F5-TTS transcribes the
        sample itself, which is slower and less accurate."""
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        if not sample_path:
            raise EngineNotInstalled(self.id, "F5-TTS needs a reference sample (and its transcript) to clone from")
        from f5_tts.api import F5TTS  # type: ignore[import-not-found]

        with engine_lock(self.id):
            model = cached_model(self.id, ("default",), F5TTS)
            wav, sr, _ = model.infer(ref_file=str(sample_path), ref_text=ref_text or "", gen_text=text,
                                     speed=speed or 1.0)
        return wav_bytes_mono16(wav, sr)


class KokoroEngine(TTSEngine):
    """Kokoro: a small, fast multilingual CPU-friendly model with a fixed
    set of named voice packs (no cloning from a sample)."""

    id = "kokoro"
    label = "Kokoro"
    pip_packages = ["kokoro>=0.9.4", "soundfile"]
    capabilities = EngineCapabilities(languages=["en", "es", "fr", "it", "pt", "ja", "zh", "hi"], cloning=False,
                                      streaming=True, needs_gpu=False, multi_speaker=True)

    # Kokoro's one-letter lang_code per language, and a default voice pack
    # that actually speaks it (an English pack reading Spanish text sounds
    # like an English speaker sounding it out).
    LANG_CODES = {"en": "a", "en-us": "a", "en-gb": "b", "es": "e", "fr": "f", "hi": "h", "it": "i", "ja": "j",
                  "pt": "p", "pt-br": "p", "zh": "z"}
    DEFAULT_VOICES = {"a": "af_heart", "b": "bf_emma", "e": "ef_dora", "f": "ff_siwis", "h": "hf_alpha",
                      "i": "if_sara", "j": "jf_alpha", "p": "pf_dora", "z": "zf_xiaobei"}

    def is_installed(self) -> bool:
        return _spec_installed("kokoro")

    @classmethod
    def lang_code(cls, language: Optional[str]) -> str:
        raw = " ".join(str(language or "").strip().lower().replace("_", "-").split())
        if not raw:
            return "a"
        if raw in cls.DEFAULT_VOICES:  # already one of Kokoro's own one-letter codes
            return raw
        if raw in cls.LANG_CODES:
            return cls.LANG_CODES[raw]
        code = normalize_language(language)
        if code not in cls.LANG_CODES:
            raise UnsupportedLanguage(language, f"Kokoro speaks {', '.join(cls.capabilities.languages)}")
        return cls.LANG_CODES[code]

    def check_language(self, language: Optional[str]) -> None:
        self.lang_code(language)

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        lang_code = self.lang_code(language)
        from kokoro import KPipeline  # type: ignore[import-not-found]
        import numpy as np

        voice = voice_ref or self.DEFAULT_VOICES[lang_code]
        with engine_lock(self.id):
            pipeline = cached_model(self.id, (lang_code,), lambda: KPipeline(lang_code=lang_code))
            chunks = [np.asarray(audio, dtype="float32")
                      for _, _, audio in pipeline(text, voice=voice, speed=speed or 1.0)]
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

        device = "cuda" if _cuda_available() else "cpu"
        exaggeration = 0.5
        try:
            exaggeration = float(style) if style else 0.5
        except ValueError:
            pass
        with engine_lock(self.id):
            model = cached_model(self.id, (device,), lambda: ChatterboxTTS.from_pretrained(device=device))
            wav = model.generate(text, audio_prompt_path=str(sample_path) if sample_path else None,
                                 exaggeration=exaggeration)
            sr = model.sr
        return wav_bytes_mono16(wav.squeeze().cpu().numpy(), sr)


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
    def __init__(self, model_size: str = "small"):
        self.model_size = model_size

    def is_installed(self) -> bool:
        return _spec_installed("faster_whisper")

    def transcribe(self, path: Path, language: Optional[str] = None, word_timestamps: bool = True) -> dict[str, Any]:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        language = normalize_language(language, allow_auto=True)
        with engine_lock(self.id):
            model = cached_model(self.id, (self.model_size, "cpu", "int8"),
                                 lambda: WhisperModel(self.model_size, device="cpu", compute_type="int8"))
            segments, info = model.transcribe(str(path), language=language, word_timestamps=word_timestamps)
            out = []
            text_parts = []
            # `segments` is a lazy generator: the decoding happens while it
            # is iterated, so the loop stays inside the lock
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
    def __init__(self, model_size: str = "small"):
        self.model_size = model_size

    def is_installed(self) -> bool:
        return _spec_installed("whisper")

    def transcribe(self, path: Path, language: Optional[str] = None, word_timestamps: bool = True) -> dict[str, Any]:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        import whisper  # type: ignore[import-not-found]

        language = normalize_language(language, allow_auto=True)
        with engine_lock(self.id):
            model = cached_model(self.id, (self.model_size,), lambda: whisper.load_model(self.model_size))
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
