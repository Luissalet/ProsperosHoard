"""Prospero's own thin wrapper around the vendored Hoard Link.

This module is the "small adapter of your
own" kept while hoard-link was still being finished. It
never edits `hoard_link/*`; it only composes it and adds Prospero-specific
pieces the shared library does not know about: ComfyUI VRAM estimates per
workflow family, a settings-friendly `status()` that folds in ffmpeg/fonts,
and the two `MusicBackend` adapters (neither installed by default).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from .hoard_link import Link, LinkConfig, Unavailable

# SDXL/SD1.5/SVD figures from the model notes; editable at runtime via
# data/backend.json -> "vram_estimates_mb".
DEFAULT_VRAM_ESTIMATES_MB = {
    "sdxl": 7000,
    "sd15": 3500,
    "svd": 10000,
}


class MusicBackend:
    """Interface every music generator adapter implements."""

    name = "music-backend"

    def available(self) -> bool:
        raise NotImplementedError

    def reason(self) -> str:
        raise NotImplementedError

    def generate(self, prompt: str, lyrics: str | None, duration_s: float, seed: int | None) -> bytes:
        raise NotImplementedError(self.reason())


class ComfyMusic(MusicBackend):
    """Enabled only when ComfyUI exposes an audio-generation node family."""

    name = "comfy_music"

    def __init__(self, object_info: dict[str, Any] | None):
        self._object_info = object_info or {}
        # Known node class names shipped by the common audio-gen custom node
        # packs (ACE-Step, StableAudio, MusicGen wrappers). None of these
        # are installed on a stock ComfyUI (no custom nodes) - this
        # flag simply stays False until one is.
        self._audio_nodes = [
            n for n in self._object_info
            if any(tag in n for tag in ("AceStep", "StableAudio", "MusicGen", "AudioGen"))
        ]

    def available(self) -> bool:
        return bool(self._audio_nodes)

    def reason(self) -> str:
        if self.available():
            return f"ComfyUI has audio-generation nodes: {', '.join(self._audio_nodes)}"
        return (
            "music: not installed - ComfyUI's /object_info shows no audio-generation "
            "node family (no ACE-Step / StableAudio / MusicGen custom nodes). "
            "Install one of those custom node packs to enable this."
        )

    def generate(self, prompt: str, lyrics: str | None, duration_s: float, seed: int | None) -> bytes:
        raise Unavailable("music", [self.reason()])


class HttpMusic(MusicBackend):
    """A documented minimal HTTP contract for a local music server.

    POST {url}/generate {"prompt", "lyrics", "duration_s", "seed"} -> audio
    bytes (wav/mp3). Nothing in this repository implements this contract
    today; configure `HOARD_MUSIC_URL` (or data/backend.json ->
    capabilities.music.url) once something does.
    """

    name = "http_music"

    def __init__(self, url: Optional[str]):
        self._url = url

    def available(self) -> bool:
        return bool(self._url)

    def reason(self) -> str:
        if self._url:
            return f"music: HttpMusic configured at {self._url} (contract untested until first call)"
        return (
            "music: not installed - no HttpMusic server configured. Set "
            "capabilities.music.url in data/backend.json (or HOARD_MUSIC_URL) "
            "to a server implementing POST /generate "
            "{prompt, lyrics, duration_s, seed} -> audio bytes."
        )

    def generate(self, prompt: str, lyrics: str | None, duration_s: float, seed: int | None) -> bytes:
        if not self._url:
            raise Unavailable("music", [self.reason()])
        import httpx

        resp = httpx.post(
            f"{self._url.rstrip('/')}/generate",
            json={"prompt": prompt, "lyrics": lyrics, "duration_s": duration_s, "seed": seed},
            timeout=180.0,
        )
        resp.raise_for_status()
        return resp.content


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_version(exe: str | None) -> str | None:
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=5)
        return out.stdout.splitlines()[0] if out.stdout else None
    except Exception:
        return None


class Backend:
    """Everything `/api/backend` and Settings needs, in one place."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "backend.json"
        self.link = self._build_link()

    # -- config -----------------------------------------------------
    def _raw_config(self) -> dict[str, Any]:
        if self.config_path.is_file():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _build_link(self) -> Link:
        import os

        config = LinkConfig.load(self.config_path, env=os.environ, app="prosperos")
        return Link(config)

    def reload(self) -> None:
        self.link = self._build_link()

    def run_async(self, coro):
        """Run a coroutine that needs `self.link`'s underlying async
        objects (e.g. a `ComfyClient` from `link.comfy()`) from a plain
        sync worker thread, reusing Hoard Link's own background event
        loop (the same one `link.sync.*` already runs on)."""
        return self.link.sync._run(coro)

    def set_overrides(
        self,
        faustus_url: str | None = None,
        faustus_token: str | None = None,
        comfy_url: str | None = None,
        vram_estimates_mb: dict[str, int] | None = None,
    ) -> None:
        raw = self._raw_config()
        if faustus_url is not None:
            raw.setdefault("faustus", {})["url"] = faustus_url
        if faustus_token is not None:
            raw.setdefault("faustus", {})["token"] = faustus_token
        if comfy_url is not None:
            raw.setdefault("comfy", {})["url"] = comfy_url
        if vram_estimates_mb:
            raw["vram_estimates_mb"] = {**DEFAULT_VRAM_ESTIMATES_MB, **raw.get("vram_estimates_mb", {}), **vram_estimates_mb}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        self.reload()

    def vram_estimates_mb(self) -> dict[str, int]:
        raw = self._raw_config()
        return {**DEFAULT_VRAM_ESTIMATES_MB, **raw.get("vram_estimates_mb", {})}

    def token_set(self) -> bool:
        raw = self._raw_config()
        return bool((raw.get("faustus") or {}).get("token"))

    # -- music --------------------------------------------------------
    def music_backends(self, object_info: dict[str, Any] | None) -> list[MusicBackend]:
        import os

        http_url = os.environ.get("HOARD_MUSIC_URL") or (self._raw_config().get("capabilities", {}).get("music", {}) or {}).get("url")
        return [ComfyMusic(object_info), HttpMusic(http_url)]

    # -- status ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        link_status = self.link.sync.status()
        exe = ffmpeg_path()
        fonts_dir = Path(__file__).parent / "fonts"
        bundled_fonts = sorted(p.name for p in fonts_dir.iterdir()) if fonts_dir.is_dir() else []

        comfy_info: dict[str, Any] = {"reachable": False}
        try:
            comfy = self.link.sync.resolve("image")
            comfy_info = {
                "reachable": comfy.resolved and comfy.provider == "comfyui",
                "url": comfy.url,
                "reason": comfy.reason,
                "checkpoints": comfy.details.get("checkpoints", []),
                "vram_free_mb": comfy.details.get("vram_free_mb"),
            }
        except Exception as exc:  # pragma: no cover - defensive
            comfy_info = {"reachable": False, "reason": str(exc)}

        music = self.music_backends(None)

        return {
            "hoard_link": link_status,
            "comfy": comfy_info,
            "ffmpeg": {"found": bool(exe), "path": exe, "version": ffmpeg_version(exe)},
            "fonts_bundled": bundled_fonts,
            "vram_estimates_mb": self.vram_estimates_mb(),
            "music": [
                {"name": m.name, "available": m.available(), "reason": m.reason()} for m in music
            ],
            "token_set": self.token_set(),
        }
