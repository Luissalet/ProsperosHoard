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
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from . import procutil
from .hoard_link import Link, LinkConfig, Unavailable

# SDXL/SD1.5/SVD/Flux/Kontext/Wan/ACE-Step figures from the model notes; editable
# at runtime via data/backend.json -> "vram_estimates_mb". Flux and Kontext
# fp8: ~13 GB; Wan 5B at 1280x704 (lower at 960x544): ~12 GB; ACE-Step 1.5
# turbo: ~8 GB.
DEFAULT_VRAM_ESTIMATES_MB = {
    "sdxl": 7000,
    "sd15": 3500,
    "svd": 10000,
    "flux": 13000,
    "kontext": 13000,
    "wan": 12000,
    "ace": 8000,
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
    """Enabled when ComfyUI exposes ACE-Step's text-encode node *and* an
    `ace_step*` checkpoint is actually on disk - either alone would still
    fail the first real call (the node with no weights, or weights with no
    node from an older ComfyUI). Real generation goes through the
    `ace15_song` built-in template (see `engine.compose_song`), the same
    way image/video templates work; this class only answers "can I?" for
    `studio_status` and `studio_compose`. Also recognises the broader
    audio-gen family (StableAudio, MusicGen, AudioGen custom nodes) for the
    status reason text, though only ACE-Step has a built-in template today.
    """

    name = "comfy_music"
    ACE_STEP_NODE = "TextEncodeAceStepAudio1.5"

    def __init__(self, object_info: dict[str, Any] | None):
        self._object_info = object_info or {}
        self._audio_nodes = [
            n for n in self._object_info
            if any(tag in n for tag in ("AceStep", "StableAudio", "MusicGen", "AudioGen"))
        ]
        self._ace_checkpoints = [
            name for name in _checkpoint_choices(self._object_info) if "ace_step" in name.lower()
        ]

    def available(self) -> bool:
        return self.ACE_STEP_NODE in self._object_info and bool(self._ace_checkpoints)

    def reason(self) -> str:
        if self.available():
            return f"ComfyUI has {self.ACE_STEP_NODE} and checkpoint(s): {', '.join(self._ace_checkpoints)}"
        if self.ACE_STEP_NODE not in self._object_info:
            extra = f" (other audio nodes present: {', '.join(self._audio_nodes)})" if self._audio_nodes else ""
            return f"music: not installed - ComfyUI's /object_info has no {self.ACE_STEP_NODE} node{extra}."
        return (
            "music: not installed - ComfyUI has the ACE-Step node but no 'ace_step*' checkpoint on disk; "
            "download one (e.g. ace_step_1.5_turbo_aio.safetensors) into models/checkpoints."
        )

    def generate(self, prompt: str, lyrics: str | None, duration_s: float, seed: int | None) -> bytes:
        # Real generation goes through engine.compose_song (the ace15_song
        # template, like every other ComfyUI job) so it gets the same
        # lineage, VRAM-wait and job-queue handling as images and video;
        # this narrower interface only exists for the generic HttpMusic
        # contract's shape and is not used for ComfyMusic.
        raise NotImplementedError("ComfyMusic generates through engine.compose_song, not this interface")


def _checkpoint_choices(object_info: dict[str, Any]) -> list[str]:
    try:
        first = object_info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        return list(first) if isinstance(first, list) else []
    except (KeyError, IndexError, TypeError):
        return []


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


_FFMPEG_CACHE: dict[str, Optional[str]] = {}


def ffmpeg_path() -> str | None:
    """ffmpeg on PATH first (the user's install), then the imageio-ffmpeg
    bundled binary. Cached: `shutil.which` is not free on Windows."""
    if "ffmpeg" in _FFMPEG_CACHE:
        return _FFMPEG_CACHE["ffmpeg"]
    found = shutil.which("ffmpeg")
    if not found:
        try:
            import imageio_ffmpeg

            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            found = None
    _FFMPEG_CACHE["ffmpeg"] = found
    return found


def ffprobe_path() -> str | None:
    """The ffprobe that sits next to the ffmpeg in use (None for the
    imageio-ffmpeg binary, which ships ffmpeg only)."""
    if "ffprobe" in _FFMPEG_CACHE:
        return _FFMPEG_CACHE["ffprobe"]
    exe = ffmpeg_path()
    found = None
    if exe:
        p = Path(exe)
        candidate = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
        if candidate != p and candidate.is_file():
            found = str(candidate)
        else:
            found = shutil.which("ffprobe")
    _FFMPEG_CACHE["ffprobe"] = found
    return found


def ffmpeg_version(exe: str | None) -> str | None:
    if not exe:
        return None
    try:
        out = procutil.run([exe, "-version"], text=True, timeout=5)
        return out.stdout.splitlines()[0] if out.stdout else None
    except Exception:
        return None


class Backend:
    """Everything `/api/backend` and Settings needs, in one place."""

    def __init__(self, data_dir: Path, demo: bool = False):
        self.data_dir = Path(data_dir)
        self.config_path = self.data_dir / "backend.json"
        self.demo = demo
        self.link = self._build_link()

    # -- config -----------------------------------------------------
    def _raw_config(self) -> dict[str, Any]:
        if self.config_path.is_file():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                return raw if isinstance(raw, dict) else {}
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                return {}
        return {}

    def _build_link(self) -> Link:
        config = LinkConfig.load(self.config_path, env=os.environ, app="prosperos")
        return Link(config)

    def reload(self) -> None:
        # The previous Link is not closed here: a GPU job may be mid-call on
        # its event loop. Its daemon loop thread simply goes idle.
        self.link = self._build_link()

    def run_async(self, call: Any) -> Any:
        """Run async Hoard Link work from a plain sync worker thread on Hoard
        Link's own background event loop. `call` is either
        `lambda link: <coroutine>` (preferred: it gets the loop-bound link)
        or a coroutine that only touches objects created on that loop
        (e.g. methods of the ComfyClient returned by `comfy()`)."""
        if callable(call):
            return self.link.sync._run(call)
        coro = call
        return self.link.sync._run(lambda _link: coro)

    def comfy(self):
        """The ComfyUI client bound to Hoard Link's loop, or None."""
        return self.run_async(lambda link: link.comfy())

    def set_overrides(
        self,
        faustus_url: str | None = None,
        faustus_token: str | None = None,
        comfy_url: str | None = None,
        vram_estimates_mb: dict[str, int] | None = None,
        import_roots: list[str] | None = None,
    ) -> None:
        raw = self._raw_config()
        if faustus_url is not None:
            if faustus_url.strip():
                raw.setdefault("faustus", {})["url"] = faustus_url.strip()
            else:
                (raw.get("faustus") or {}).pop("url", None)
        if faustus_token is not None:
            if faustus_token:
                raw.setdefault("faustus", {})["token"] = faustus_token
            else:
                (raw.get("faustus") or {}).pop("token", None)
        if comfy_url is not None:
            if comfy_url.strip():
                raw.setdefault("comfy", {})["url"] = comfy_url.strip()
            else:
                raw.pop("comfy", None)
        if vram_estimates_mb:
            clean = {}
            for key, value in vram_estimates_mb.items():
                if key not in DEFAULT_VRAM_ESTIMATES_MB:
                    raise ValueError(f"unknown VRAM class '{key}'; expected one of {sorted(DEFAULT_VRAM_ESTIMATES_MB)}")
                if not isinstance(value, int) or not 256 <= value <= 200_000:
                    raise ValueError(f"VRAM estimate for '{key}' must be an integer between 256 and 200000 MB")
                clean[key] = value
            raw["vram_estimates_mb"] = {**DEFAULT_VRAM_ESTIMATES_MB, **raw.get("vram_estimates_mb", {}), **clean}
        if import_roots is not None:
            raw["import_roots"] = [str(Path(r).expanduser()) for r in import_roots if str(r).strip()]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        self.reload()

    def vram_estimates_mb(self) -> dict[str, int]:
        raw = self._raw_config()
        return {**DEFAULT_VRAM_ESTIMATES_MB, **(raw.get("vram_estimates_mb") or {})}

    def import_roots(self) -> list[Path]:
        """Folders `studio_import` may read from: the user's home folder,
        the app's own `data/inbox/`, and any extra folders configured in
        Settings (`backend.json -> import_roots`)."""
        roots = [Path.home(), self.data_dir / "inbox"]
        for extra in self._raw_config().get("import_roots") or []:
            roots.append(Path(extra))
        out: list[Path] = []
        for r in roots:
            try:
                resolved = r.expanduser().resolve()
            except OSError:
                continue
            if resolved not in out:
                out.append(resolved)
        return out

    def token_set(self) -> bool:
        raw = self._raw_config()
        return bool((raw.get("faustus") or {}).get("token"))

    # -- music --------------------------------------------------------
    def music_backends(self, object_info: dict[str, Any] | None) -> list[MusicBackend]:
        http_url = os.environ.get("HOARD_MUSIC_URL") or (self._raw_config().get("capabilities", {}).get("music", {}) or {}).get("url")
        return [ComfyMusic(object_info), HttpMusic(http_url)]

    # -- GPU ------------------------------------------------------------
    def vram_free_mb(self) -> Optional[int]:
        """Free VRAM on the best GPU: nvidia-smi through Hoard Link first,
        then ComfyUI's own `/system_stats` (covers non-NVIDIA or a ComfyUI
        on another card). None when neither answers."""
        from .hoard_link.gpu import gpu_free_mb

        gpus = gpu_free_mb()
        if gpus:
            return max(g.free_mb for g in gpus)
        try:
            comfy = self.comfy()
            if comfy is None:
                return None
            stats = self.run_async(comfy.system_stats())
            frees = [int(d.get("vram_free", 0)) // (1024 * 1024) for d in stats.get("devices", []) if d.get("vram_free") is not None]
            return max(frees) if frees else None
        except Exception:
            return None

    def free_comfy_memory(self) -> dict[str, Any]:
        comfy = self.comfy()
        if comfy is None:
            raise Unavailable("image", ["ComfyUI is not reachable"])
        self.run_async(comfy.free(unload_models=True, free_memory=True))
        return {"ok": True, "message": "asked ComfyUI to unload its models and free memory"}

    # -- status ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        link_status = self.link.sync.status()
        exe = ffmpeg_path()
        fonts_dir = Path(__file__).parent / "fonts"
        bundled_fonts = sorted(p.name for p in fonts_dir.iterdir() if p.is_dir()) if fonts_dir.is_dir() else []

        comfy_info: dict[str, Any] = {"reachable": False}
        object_info: dict[str, Any] | None = None
        try:
            comfy = link_status.get("image") or {}
            reachable = comfy.get("state") == "resolved" and comfy.get("provider") == "comfyui"
            details = comfy.get("details") or {}
            comfy_info = {
                "reachable": reachable,
                "url": comfy.get("url"),
                "reason": comfy.get("reason"),
                "checkpoints": details.get("checkpoints", []),
                "vram_free_mb": details.get("vram_free_mb"),
                "gpus": details.get("gpus", []),
            }
            if reachable:
                client = self.comfy()
                if client is not None:
                    object_info = self.run_async(client.object_info())
                    stats = self.run_async(client.system_stats())
                    comfy_info["devices"] = [
                        {"name": d.get("name"), "vram_total_mb": int(d.get("vram_total", 0)) // (1024 * 1024),
                         "vram_free_mb": int(d.get("vram_free", 0)) // (1024 * 1024)}
                        for d in stats.get("devices", [])
                    ]
                    comfy_info["version"] = (stats.get("system") or {}).get("comfyui_version")
        except Exception as exc:  # pragma: no cover - defensive
            comfy_info = {"reachable": False, "reason": str(exc)[:300]}

        music = self.music_backends(object_info)
        raw = self._raw_config()
        faustus = raw.get("faustus") or {}

        return {
            "demo": self.demo,
            "hoard_link": link_status,
            "comfy": comfy_info,
            "ffmpeg": {"found": bool(exe), "path": exe, "version": ffmpeg_version(exe)},
            "piper": {"installed": _piper_installed()},
            "fonts_bundled": bundled_fonts,
            "vram_estimates_mb": self.vram_estimates_mb(),
            "music": [
                {"name": m.name, "available": m.available(), "reason": m.reason()} for m in music
            ],
            "overrides": {
                "faustus_url": faustus.get("url"),
                "comfy_url": (raw.get("comfy") or {}).get("url"),
                "import_roots": [str(p) for p in self.import_roots()],
            },
            "token_set": self.token_set(),
        }


def _piper_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("piper") is not None
