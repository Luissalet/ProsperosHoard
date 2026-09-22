"""Character voices: Piper (local, on-demand voice download) or Hoard
Link TTS (Faustus). No voice cloning of real people is offered anywhere
in this app - Piper's curated voices are synthetic TTS voices, not clones.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any, Optional

import httpx

from .hoard_link.errors import Unavailable

HF_VOICES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# A curated subset (family/quality); the UI lists these, the user can also
# type any other rhasspy/piper-voices path.
CURATED_VOICES = [
    {"id": "es_ES-davefx-medium", "lang": "es_ES", "label": "Spanish (Spain) - Davefx", "path": "es/es_ES/davefx/medium/es_ES-davefx-medium"},
    {"id": "es_ES-mls_10246-low", "lang": "es_ES", "label": "Spanish (Spain) - MLS 10246", "path": "es/es_ES/mls_10246/low/es_ES-mls_10246-low"},
    {"id": "en_US-amy-medium", "lang": "en_US", "label": "English (US) - Amy", "path": "en/en_US/amy/medium/en_US-amy-medium"},
    {"id": "en_US-lessac-medium", "lang": "en_US", "label": "English (US) - Lessac", "path": "en/en_US/lessac/medium/en_US-lessac-medium"},
]


def list_curated_voices() -> list[dict[str, Any]]:
    return CURATED_VOICES


def _voice_by_id(voice_id: str) -> Optional[dict[str, Any]]:
    return next((v for v in CURATED_VOICES if v["id"] == voice_id), None)


def voice_files_present(voices_dir: Path, voice_id: str) -> bool:
    onnx = voices_dir / f"{voice_id}.onnx"
    return onnx.is_file() and (voices_dir / f"{voice_id}.onnx.json").is_file()


def download_voice(voices_dir: Path, voice_id: str, timeout_s: float = 120.0) -> Path:
    voice = _voice_by_id(voice_id)
    if voice is None:
        raise ValueError(f"unknown curated voice '{voice_id}'")
    voices_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = voices_dir / f"{voice_id}.onnx"
    json_path = voices_dir / f"{voice_id}.onnx.json"
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        for suffix, dest in ((".onnx", onnx_path), (".onnx.json", json_path)):
            if dest.is_file():
                continue
            url = f"{HF_VOICES_BASE}/{voice['path']}{suffix}"
            resp = client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
    return onnx_path


def synthesize_piper(voices_dir: Path, voice_id: str, text: str) -> bytes:
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise Unavailable("tts", ["piper-tts is not installed (pip install .[piper])"]) from exc

    onnx_path = voices_dir / f"{voice_id}.onnx"
    if not onnx_path.is_file():
        onnx_path = download_voice(voices_dir, voice_id)

    voice = PiperVoice.load(str(onnx_path))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    return buf.getvalue()


def synthesize(
    backend, voices_dir: Path, text: str, voice: Optional[dict[str, Any]] = None
) -> tuple[bytes, str]:
    """`voice` is a character's `voice` dict: {"backend": "piper"|"faustus", "voice_id": str, "speed": float}.
    Returns (wav_bytes, provider_used)."""
    voice = voice or {}
    backend_name = voice.get("backend", "piper")
    voice_id = voice.get("voice_id", CURATED_VOICES[0]["id"])

    if backend_name == "piper":
        return synthesize_piper(voices_dir, voice_id, text), "piper"

    if backend_name == "faustus":
        try:
            wav_bytes = backend.link.sync.tts(text, voice=voice_id)
            return wav_bytes, "faustus"
        except Unavailable:
            # Fall back to Piper rather than fail the whole operation.
            return synthesize_piper(voices_dir, voice_id, text), "piper_fallback"

    raise ValueError(f"unknown voice backend '{backend_name}'")
