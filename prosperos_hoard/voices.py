"""Character voices: Piper (local, on-demand voice download) or Hoard
Link TTS (Faustus). No voice cloning of any kind is offered in this app:
Piper's curated voices are generic synthetic TTS voices trained on public
datasets, and nothing here trains or adapts a voice to a person.
"""

from __future__ import annotations

import io
import threading
import wave
from pathlib import Path
from typing import Any, Optional

import httpx

from .hoard_link.errors import BackendError, Unavailable

HF_VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# A curated subset (id -> path in the rhasspy/piper-voices repository).
CURATED_VOICES = [
    {"id": "es_ES-davefx-medium", "lang": "es_ES", "label": "Español (España) - Davefx", "path": "es/es_ES/davefx/medium/es_ES-davefx-medium", "size_mb": 63},
    {"id": "es_ES-sharvard-medium", "lang": "es_ES", "label": "Español (España) - Sharvard", "path": "es/es_ES/sharvard/medium/es_ES-sharvard-medium", "size_mb": 77},
    {"id": "es_ES-mls_10246-low", "lang": "es_ES", "label": "Español (España) - MLS 10246", "path": "es/es_ES/mls_10246/low/es_ES-mls_10246-low", "size_mb": 63},
    {"id": "en_US-amy-medium", "lang": "en_US", "label": "English (US) - Amy", "path": "en/en_US/amy/medium/en_US-amy-medium", "size_mb": 63},
    {"id": "en_US-lessac-medium", "lang": "en_US", "label": "English (US) - Lessac", "path": "en/en_US/lessac/medium/en_US-lessac-medium", "size_mb": 63},
    {"id": "en_GB-alba-medium", "lang": "en_GB", "label": "English (UK) - Alba", "path": "en/en_GB/alba/medium/en_GB-alba-medium", "size_mb": 63},
]

_LOCK = threading.Lock()
_LOADED: dict[str, Any] = {}


class VoiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def list_curated_voices(voices_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    out = []
    for v in CURATED_VOICES:
        item = dict(v)
        item["downloaded"] = bool(voices_dir and voice_files_present(voices_dir, v["id"]))
        out.append(item)
    return out


def _voice_by_id(voice_id: str) -> Optional[dict[str, Any]]:
    return next((v for v in CURATED_VOICES if v["id"] == voice_id), None)


def voice_files_present(voices_dir: Path, voice_id: str) -> bool:
    return (voices_dir / f"{voice_id}.onnx").is_file() and (voices_dir / f"{voice_id}.onnx.json").is_file()


def download_voice(voices_dir: Path, voice_id: str, timeout_s: float = 300.0) -> Path:
    """Fetch a curated voice from Hugging Face into `data/voices/` (the only
    network access in the app, and only when the user asks for a voice that
    is not on disk yet). Written to `.part` first so an interrupted download
    never leaves a truncated model behind."""
    voice = _voice_by_id(voice_id)
    if voice is None:
        known = ", ".join(v["id"] for v in CURATED_VOICES)
        raise VoiceError("unknown_voice", f"unknown voice '{voice_id}'; curated voices: {known}")
    voices_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = voices_dir / f"{voice_id}.onnx"
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            for suffix in (".onnx.json", ".onnx"):
                dest = voices_dir / f"{voice_id}{suffix}"
                if dest.is_file():
                    continue
                part = dest.with_name(dest.name + ".part")
                with client.stream("GET", f"{HF_VOICES_BASE}/{voice['path']}{suffix}") as resp:
                    resp.raise_for_status()
                    with part.open("wb") as fh:
                        for chunk in resp.iter_bytes(1 << 20):
                            fh.write(chunk)
                part.replace(dest)
    except httpx.HTTPError as exc:
        raise VoiceError("voice_download_failed", f"could not download voice '{voice_id}' from Hugging Face: {exc}") from exc
    return onnx_path


def piper_installed() -> bool:
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return True


def synthesize_piper(voices_dir: Path, voice_id: str, text: str, speed: Optional[float] = None) -> bytes:
    try:
        from piper import PiperVoice, SynthesisConfig
    except ImportError as exc:
        raise VoiceError("piper_not_installed", "Piper is not installed in this environment: run "
                                                "'pip install -r requirements-lock.txt' again") from exc
    if _voice_by_id(voice_id) is None and not voice_files_present(voices_dir, voice_id):
        known = ", ".join(v["id"] for v in CURATED_VOICES)
        raise VoiceError("unknown_voice", f"unknown voice '{voice_id}'; curated voices: {known}")
    onnx_path = voices_dir / f"{voice_id}.onnx"
    if not voice_files_present(voices_dir, voice_id):
        onnx_path = download_voice(voices_dir, voice_id)
    with _LOCK:
        voice = _LOADED.get(voice_id)
        if voice is None:
            voice = PiperVoice.load(str(onnx_path))
            _LOADED.clear()  # keep one voice resident (they are ~60 MB each)
            _LOADED[voice_id] = voice
        buf = io.BytesIO()
        config = SynthesisConfig(length_scale=(1.0 / speed) if speed else None)
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file, syn_config=config)
    return buf.getvalue()


def synthesize(backend, voices_dir: Path, text: str, voice: Optional[dict[str, Any]] = None) -> tuple[bytes, str]:
    """`voice` is a character's `voice` dict: {"backend": "piper"|"faustus",
    "voice_id": str, "speed": float}. Returns (wav_bytes, provider_used).
    Faustus TTS falls back to Piper when Faustus has no TTS right now."""
    voice = voice or {}
    backend_name = voice.get("backend", "piper")
    voice_id = voice.get("voice_id") or CURATED_VOICES[0]["id"]
    speed = voice.get("speed")

    if backend_name == "piper":
        return synthesize_piper(voices_dir, voice_id, text, speed), "piper"

    if backend_name == "faustus":
        try:
            result = backend.link.sync.tts(text, voice=voice_id if not _voice_by_id(voice_id) else None)
            data = result if isinstance(result, (bytes, bytearray)) else getattr(result, "audio", None)
            if not data:
                raise Unavailable("tts", ["the TTS backend returned no audio"])
            return bytes(data), "faustus"
        except (Unavailable, BackendError):
            fallback = voice_id if _voice_by_id(voice_id) else CURATED_VOICES[0]["id"]
            return synthesize_piper(voices_dir, fallback, text, speed), "piper_fallback"

    raise VoiceError("unknown_voice_backend", f"unknown voice backend '{backend_name}'; use 'piper' or 'faustus'")
