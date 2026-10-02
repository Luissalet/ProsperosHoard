"""Prospero's own thin wrapper around the vendored Hoard Link.

This module never edits `hoard_link/*`; it only composes it and adds
Prospero-specific pieces the shared library does not know about: ComfyUI
VRAM estimates per workflow family, a settings-friendly `status()` that
folds in ffmpeg/fonts, and the two `MusicBackend` adapters (neither
installed by default).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import procutil
from .hoard_link.atomic import write_text_atomic
from .hoard_link.media import bins
from .hoard_link import Link, LinkConfig, Unavailable
from .hoard_link.launch import Launcher, _port_open, comfy_port_from_url, list_gpus

# SDXL/SD1.5/SVD/Flux/Kontext/Wan/ACE-Step/Qwen-Image 2.1 figures from the
# model notes; editable at runtime via data/backend.json -> "vram_estimates_mb".
# Flux and Kontext fp8: ~13 GB; Wan 5B at 1280x704 (lower at 960x544): ~12 GB;
# ACE-Step 1.5 turbo: ~8 GB; Qwen-Image 2.1 int8: ~7.3 GB diffusion + 9.4 GB
# text encoder loaded one after the other, peak ~10-12 GB at 1 MP (more at 2K).
# Real-ESRGAN x4 (tiled) ~2.5 GB; BiRefNet matting ~3.5 GB.
DEFAULT_VRAM_ESTIMATES_MB = {
    "sdxl": 7000,
    "sd15": 3500,
    "svd": 10000,
    "flux": 13000,
    "kontext": 13000,
    "wan": 12000,
    "ace": 8000,
    "qwen21": 12000,
    "esrgan": 2500,
    "birefnet": 3500,
    # Wan 2.2 14B i2v: two 14 GB fp8 experts loaded one at a time (ComfyUI
    # offloads the rest to RAM); Wan Animate 2 int8: 16.7 GB, partly offloaded
    "wan14b": 10000,
    "wan_animate": 10000,
    # Wan 2.2 S2V 14B fp8 (16.4 GB) + wav2vec2: streams partly from RAM on 16 GB
    "wan_s2v": 12000,
    "control": 3000,
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
    bytes (wav/mp3). Nothing in this repository implements it; configure
    `HOARD_MUSIC_URL` (or data/backend.json -> capabilities.music.url)
    once something does.
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


def _port_open(url: str, timeout_s: float = 0.4) -> bool:
    """Whether something listens at a server URL's host and port (a quick
    TCP connect, no HTTP)."""
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        host, port = u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False

def ffmpeg_path() -> str | None:
    """The working ffmpeg (Hoard Link's lookup: ``HOARD_FFMPEG``, the user's PATH and the usual Windows folders, then the
    imageio-ffmpeg wheel). Each candidate is run once to prove it works, and the answer is cached for a few seconds."""
    return bins.find("ffmpeg").path


def ffprobe_path() -> str | None:
    """The ffprobe that sits next to the ffmpeg in use (None for the imageio-ffmpeg binary, which ships ffmpeg only)."""
    return bins.find("ffprobe").path


def ffmpeg_version(exe: str | None) -> str | None:
    """The first line of ``ffmpeg -version`` (``None`` when it does not run)."""
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
        # starts ComfyUI/Ollama/configured servers itself when Faustus is not
        # there (shared with the family: ~/.hoard/backends.json)
        self.launcher = Launcher(app="prospero")
        self._autostart_lock = threading.Lock()
        # render pool: which ComfyUI this worker thread talks to (None = the
        # main one Hoard Link resolves), one cached client per extra server
        self._bound = threading.local()
        self._pool_clients: dict[str, Any] = {}
        self._pool_health: dict[str, tuple[float, bool]] = {}
        self._pool_lock = threading.Lock()
        self._min_card_cache: dict[str, int] = {}
        self._card_totals: Optional[tuple[float, dict[Optional[str], Optional[int]]]] = None

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
        # The previous Link is not closed: a GPU job may be mid-call with a
        # ComfyUI client it got from it. The new Link runs on the same
        # background event loop, so that client (bound to the loop's
        # connections) keeps working for the rest of its job instead of
        # failing with "bound to a different event loop".
        old = getattr(self, "link", None)
        new = self._build_link()
        if old is not None:
            with old.sync._start_lock:
                if old.sync._loop is not None:
                    new.sync._loop, new.sync._thread = old.sync._loop, old.sync._thread
        self.link = new

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
        """The ComfyUI client bound to Hoard Link's loop, or None. On a
        render-pool worker thread this is that worker's own server."""
        url = getattr(self._bound, "url", None)
        if url:
            return self._pool_client(url)
        return self.run_async(lambda link: link.comfy())

    # -- render pool -------------------------------------------------
    # One ComfyUI per GPU, all sharing the GPU job queue: `render_pool` in
    # data/backend.json lists the extra servers (the main one is the usual
    # `comfy.url` or whatever Hoard Link finds). Each gets its own GPU
    # worker thread, so on a machine with several cards a batch of clips
    # renders in parallel. The servers are expected to be the same ComfyUI
    # install started with a different `--cuda-device` and `--port` (same
    # models, same node list); a restart applies a changed list.
    def render_pool(self) -> list[str]:
        urls = self._raw_config().get("render_pool") or []
        out: list[str] = []
        for u in urls:
            if isinstance(u, str) and u.strip().startswith(("http://", "https://")):
                clean = u.strip().rstrip("/")
                if clean not in out:
                    out.append(clean)
        return out[:7]

    def bind_comfy(self, url: Optional[str]) -> None:
        """Pin the calling worker thread to one ComfyUI (None = the main one).
        Only GPU workers call this, so it also marks the thread as one that
        may start ComfyUI when a job needs it (see `ensure_comfy`)."""
        self._bound.url = url.rstrip("/") if url else None
        self._bound.worker = True

    def _pool_client(self, url: str):
        # A client's connections belong to the event loop it was created on.
        # Saving the backend settings rebuilds the Link (and its loop), so a
        # client made for the previous Link must not be reused - it fails
        # with "Event is bound to a different event loop".
        link = self.link
        with self._pool_lock:
            entry = self._pool_clients.get(url)
        client = entry[1] if entry and entry[0] is link else None
        if client is None:
            from .hoard_link._comfy import ComfyClient

            async def make(_link):
                return ComfyClient(url)  # created on Hoard Link's loop, reused by every job

            client = self.run_async(make)
            with self._pool_lock:
                current = self._pool_clients.get(url)
                if current and current[0] is link:
                    client = current[1]
                else:
                    self._pool_clients[url] = (link, client)
        return client

    # -- which server takes which job --------------------------------------
    # A template may say how big a card it needs (`min_card_mb`, e.g. Wan
    # Animate 2: on a 12 GB card it streams its weights at minutes per step,
    # on 16 GB it runs). A GPU worker leaves such a job to a server whose
    # card holds it - unless none does, then anyone takes it.
    def _min_card_mb(self, job: dict[str, Any]) -> int:
        template = (job.get("params") or {}).get("template") if job.get("type") == "generate_image" else None
        if not template:
            return 0
        cached = self._min_card_cache.get(template)
        if cached is None:
            try:
                from .comfy_driver import load_template

                cached = int(load_template(template, self.data_dir)[1].get("min_card_mb") or 0)
            except Exception:
                cached = 0
            self._min_card_cache[template] = cached
        return cached

    def card_totals(self, ttl_s: float = 60.0) -> dict[Optional[str], Optional[int]]:
        """Card size (MB) per GPU worker target: None = the main ComfyUI,
        then each render-pool server; None when it does not answer."""
        now = time.monotonic()
        if self._card_totals and now - self._card_totals[0] < ttl_s:
            return self._card_totals[1]
        out: dict[Optional[str], Optional[int]] = {}
        for target in [None, *self.render_pool()]:
            try:
                client = self._pool_client(target) if target else self.run_async(lambda link: link.comfy())
                dev = self._primary_comfy_device(self.run_async(client.system_stats())) if client else None
                total = dev.get("vram_total") if dev else None
                out[target] = total // (1024 * 1024) if self._memory_bytes(total) and total else None
            except Exception:
                out[target] = None
        self._card_totals = (now, out)
        return out

    def accepts_job(self, job: dict[str, Any]) -> bool:
        need = self._min_card_mb(job)
        if not need:
            return True
        totals = self.card_totals()
        mine = totals.get(getattr(self._bound, "url", None))
        if mine is None or mine >= need:
            return True
        return not any(t and t >= need for t in totals.values())

    def pool_server_ready(self, url: str, ttl_s: float = 10.0) -> bool:
        """Cheap, cached reachability check a pool worker makes before it
        takes a job, so a server that is off never swallows the queue."""
        now = time.monotonic()
        cached = self._pool_health.get(url)
        if cached and now - cached[0] < ttl_s:
            return cached[1]
        ok = _port_open(url)  # a server that is off answers nothing: no client retries to wait through
        if ok:
            try:
                self.run_async(self._pool_client(url).system_stats())
            except Exception:
                ok = False
        self._pool_health[url] = (now, ok)
        return ok

    def set_overrides(
        self,
        faustus_url: str | None = None,
        faustus_token: str | None = None,
        comfy_url: str | None = None,
        vram_estimates_mb: dict[str, int] | None = None,
        import_roots: list[str] | None = None,
        render_pool: list[str] | None = None,
        comfy_dedicated: bool | None = None,
    ) -> None:
        raw = self._raw_config()
        if comfy_dedicated is not None:
            if comfy_dedicated:
                raw["comfy_dedicated"] = True
            else:
                raw.pop("comfy_dedicated", None)
        if render_pool is not None:
            clean_pool = []
            for u in render_pool:
                u = str(u).strip().rstrip("/")
                if not u:
                    continue
                if not u.startswith(("http://", "https://")):
                    raise ValueError(f"render pool entries must be http(s) URLs of ComfyUI servers, got {u!r}")
                clean_pool.append(u)
            if len(clean_pool) > 7:
                raise ValueError("the render pool takes at most 7 extra ComfyUI servers")
            raw["render_pool"] = clean_pool
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
        write_text_atomic(self.config_path, json.dumps(raw, indent=2))
        self.reload()

    def training(self) -> dict[str, Any]:
        """`backend.json -> training`: LoRA folder, trainers, base models
        (see trainers.py / docs/CHARACTERS.md)."""
        raw = self._raw_config().get("training") or {}
        return raw if isinstance(raw, dict) else {}

    def set_training(self, training: dict[str, Any]) -> dict[str, Any]:
        """Validate and store the training section (replaces it)."""
        if not isinstance(training, dict):
            raise ValueError("training must be an object")
        allowed = {"lora_dir", "trainers", "base_models", "gpu"}
        unknown = set(training) - allowed
        if unknown:
            raise ValueError(f"unknown training key(s): {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}")
        clean: dict[str, Any] = {}
        if training.get("lora_dir"):
            clean["lora_dir"] = str(Path(str(training["lora_dir"])).expanduser())
        gpu = training.get("gpu")
        if gpu not in (None, "", "auto"):
            if not isinstance(gpu, int) or not 0 <= gpu <= 15:
                raise ValueError("training.gpu is 'auto' or a GPU index (0-15)")
            clean["gpu"] = gpu
        trainers = training.get("trainers") or []
        if not isinstance(trainers, list) or len(trainers) > 8:
            raise ValueError("training.trainers is a list of at most 8 trainers")
        out = []
        for t in trainers:
            if not isinstance(t, dict) or t.get("kind") not in ("ai_toolkit", "musubi", "custom", "fake"):
                raise ValueError("each trainer needs kind: ai_toolkit | musubi | custom | fake")
            entry = {k: t[k] for k in ("kind", "name", "dir", "python", "command", "delay") if t.get(k) not in (None, "")}
            entry.setdefault("name", entry["kind"])
            if "command" in entry and (not isinstance(entry["command"], list) or not all(isinstance(c, str) for c in entry["command"])):
                raise ValueError("a custom trainer's command is a list of strings")
            out.append(entry)
        if out:
            clean["trainers"] = out
        bases = training.get("base_models") or {}
        if not isinstance(bases, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in bases.items()):
            raise ValueError("training.base_models maps an architecture to a path or hub id")
        if bases:
            clean["base_models"] = {k: v.strip() for k, v in bases.items() if v.strip()}
        raw = self._raw_config()
        raw["training"] = clean
        self.data_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.config_path, json.dumps(raw, indent=2))
        return clean

    def stock_keys(self) -> dict[str, str]:
        """Pexels/Pixabay API keys (`backend.json -> stock`, else the
        PEXELS_API_KEY / PIXABAY_API_KEY environment variables)."""
        from . import stock

        return stock.configured_keys(self._raw_config().get("stock"))

    def set_stock_keys(self, keys: dict[str, Any]) -> None:
        """Store or clear (empty string) a provider's key."""
        from . import stock

        if not isinstance(keys, dict):
            raise ValueError("stock keys must be an object {pexels?, pixabay?}")
        unknown = set(keys) - set(stock.PROVIDERS)
        if unknown:
            raise ValueError(f"unknown stock provider(s): {', '.join(sorted(unknown))}; use {', '.join(stock.PROVIDERS)}")
        raw = self._raw_config()
        block = raw.get("stock") if isinstance(raw.get("stock"), dict) else {}
        for provider, key in keys.items():
            key = str(key or "").strip()
            if key:
                if len(key) > 200 or any(ch.isspace() for ch in key):
                    raise ValueError(f"the {provider} key does not look like an API key")
                block[provider] = key
            else:
                block.pop(provider, None)
        raw["stock"] = block
        self.data_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.config_path, json.dumps(raw, indent=2))

    def vram_estimates_mb(self) -> dict[str, int]:
        raw = self._raw_config()
        return {**DEFAULT_VRAM_ESTIMATES_MB, **(raw.get("vram_estimates_mb") or {})}

    def bg_removal_model(self) -> str:
        """The background-removal model file (`backend.json -> bg_removal_model`),
        `birefnet.safetensors` unless the config names another."""
        name = self._raw_config().get("bg_removal_model")
        return name.strip() if isinstance(name, str) and name.strip() else "birefnet.safetensors"

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
        """VRAM a ComfyUI job can use, in MB; None when nothing answers.

        ComfyUI's own `/system_stats` comes first: it describes the card
        ComfyUI really runs on (a `--cuda-device` pick on a multi-GPU
        machine, not whichever card happens to be freest), and its
        `vram_free` includes unused PyTorch reservation, but not all warm
        DynamicVRAM allocations. Active PyTorch reservation is added back;
        the result remains a conservative estimate for DynamicVRAM.
        nvidia-smi through Hoard Link (the freest card) is the fallback when
        ComfyUI does not report devices.

        In demo mode the jobs run on the procedural fake ComfyUI, so only its
        `/system_stats` counts: the machine's real GPUs may be busy with other
        models, and that must not stop the demo from seeding."""
        from .hoard_link.gpu import gpu_free_mb

        comfy_free = self._comfy_available_mb()
        if comfy_free is not None or self.demo:
            return comfy_free
        gpus = gpu_free_mb()
        if gpus:
            return max(g.free_mb for g in gpus)
        return None

    def _comfy_available_mb(self) -> Optional[int]:
        try:
            comfy = self.comfy()
            if comfy is None:
                return None
            stats = self.run_async(comfy.system_stats())
        except Exception:
            return None
        dev = self._primary_comfy_device(stats)
        if dev is None:
            return None
        free = dev.get("vram_free")
        if not self._memory_bytes(free):
            return None
        total, unused = dev.get("torch_vram_total", 0), dev.get("torch_vram_free", 0)
        reserved_in_use = max(0, total - unused) if self._memory_bytes(total) and self._memory_bytes(unused) else 0
        return (free + reserved_in_use) // (1024 * 1024)

    def vram_total_mb(self) -> Optional[int]:
        """The size of the card the bound ComfyUI runs on, in MB (None when
        it does not say)."""
        try:
            comfy = self.comfy()
            if comfy is None:
                return None
            dev = self._primary_comfy_device(self.run_async(comfy.system_stats()))
        except Exception:
            return None
        total = dev.get("vram_total") if dev else None
        return total // (1024 * 1024) if self._memory_bytes(total) and total else None

    @staticmethod
    def _memory_bytes(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1

    @staticmethod
    def _primary_comfy_device(stats: Any) -> Optional[dict]:
        devices = stats.get("devices") if isinstance(stats, dict) else None
        if not isinstance(devices, list):
            return None
        # ComfyUI reports its primary first. Never pick a later GPU merely
        # because it has more free memory (nor skip a malformed primary).
        for dev in devices:
            if not isinstance(dev, dict):
                return None
            kind = str(dev.get("type") or "").lower()
            if kind == "cpu":
                continue
            return dev if kind in ("cuda", "xpu", "mps", "npu", "mlu", "privateuseone") else None
        return None

    def comfy_dedicated(self) -> bool:
        """`comfy_dedicated: true` in backend.json: the ComfyUI servers are
        Prospero's alone (see comfy_manages_memory and templates' free_after)."""
        return self._raw_config().get("comfy_dedicated") is True and not self.demo

    def comfy_manages_memory(self, needed_mb: int) -> bool:
        """Opt-in: an idle ComfyUI that belongs to Prospero may make room
        itself (drop the last job's models) instead of the job waiting for
        free VRAM. Either the explicitly pinned endpoint (`comfy.manage_memory`)
        or, with `comfy_dedicated`, every server Prospero renders on.

        No freeing/unloading request is made. ComfyUI handles its own cache
        as part of the already requested render; this is not a reservation.
        """
        if self.demo:
            return False

        def allowed_for(url: str) -> bool:
            # `comfy_dedicated: true` - every ComfyUI Prospero renders on (the
            # main one and the render pool) is its own: an idle one may drop
            # the previous job's models to load the next. Otherwise only the
            # explicitly pinned `comfy.url` with `manage_memory: true`.
            cfg = self._raw_config()
            if cfg.get("comfy_dedicated") is True:
                return True
            raw = cfg.get("comfy")
            if not isinstance(raw, dict) or raw.get("manage_memory") is not True:
                return False
            pinned = raw.get("url")
            return isinstance(pinned, str) and bool(pinned.strip()) and pinned.strip().rstrip("/") == url

        try:
            comfy = self.comfy()
            if comfy is None or not allowed_for(comfy.url.rstrip("/")):
                return False
            pinned = comfy.url.rstrip("/")
            dev = self._primary_comfy_device(self.run_async(comfy.system_stats()))
            total = dev.get("vram_total") if dev else None
            if (not self._memory_bytes(total) or not self._memory_bytes(dev.get("vram_free")) or
                    dev["vram_free"] > total or total // (1024 * 1024) < needed_mb):
                return False

            async def queue_snapshot():
                # Reuse this exact client's transport/endpoint; vendor stays intact.
                response = await comfy._client.get(comfy.url + "/queue", timeout=5.0)
                response.raise_for_status()
                return response.json()

            queue = self.run_async(queue_snapshot())
            return (isinstance(queue, dict) and queue.get("queue_running") == [] and
                    queue.get("queue_pending") == [] and allowed_for(pinned))
        except Exception:
            return False

    def free_comfy_memory(self) -> dict[str, Any]:
        comfy = self.comfy()
        if comfy is None:
            raise Unavailable("image", ["ComfyUI is not reachable"])
        self.run_async(comfy.free(unload_models=True, free_memory=True))
        return {"ok": True, "message": "asked ComfyUI to unload its models and free memory"}


    # -- local services (standalone: no Faustus needed) -----------------
    # ComfyUI (the main one and the render pool), Ollama and any server
    # configured in ~/.hoard/backends.json can be started from here. The
    # launcher is shared with the rest of the Hoard family, so a ComfyUI
    # the Hub started shows up (and can be stopped) here, and the other way
    # round. Servers started elsewhere are shown but never stopped.
    def launch_settings(self) -> dict[str, Any]:
        raw = self._raw_config().get("launch") or {}
        return raw if isinstance(raw, dict) else {}

    def autostart_comfy(self) -> bool:
        return not self.demo and bool(self.launch_settings().get("autostart_comfy", True))

    def main_comfy_port(self) -> Optional[int]:
        """Port of the main ComfyUI when it is a local one (None = remote)."""
        return comfy_port_from_url((self._raw_config().get("comfy") or {}).get("url"))

    def comfy_ports(self) -> list[int]:
        ports: list[int] = []
        main = self.main_comfy_port()
        if main is not None:
            ports.append(main)
        for url in self.render_pool():
            p = comfy_port_from_url(url)
            if p is not None and p not in ports:
                ports.append(p)
        return ports

    def _service_id(self, service_id: str) -> str:
        sid = str(service_id or "").strip()
        if sid in ("comfyui", "comfy"):
            port = self.main_comfy_port()
            if port is None:
                raise ValueError("the main ComfyUI is a remote server (Backends > ComfyUI URL): start it where it runs")
            return f"comfyui@{port}"
        return sid

    def services(self) -> dict[str, Any]:
        ports = self.comfy_ports()
        main = self.main_comfy_port()
        pool = [p for p in ports if p != main]
        items = self.launcher.statuses(ports)
        for item in items:
            if item["kind"] == "comfyui":
                port = int(item["id"].split("@", 1)[1])
                item["role"] = "main" if port == main else ("render_pool" if port in pool else "other")
        folder, python, problem = self.launcher.comfy_install()
        cfg = self.launcher.config()
        return {
            "items": items,
            "gpus": list_gpus(),
            "demo": self.demo,
            "autostart_comfy": self.autostart_comfy(),
            "main_comfy": f"comfyui@{main}" if main is not None else None,
            "comfyui": {"dir": str(folder) if folder else None, "python": str(python) if python else None,
                        "problem": problem, "gpu": (cfg.get("comfyui") or {}).get("gpu", "auto"),
                        "args": (cfg.get("comfyui") or {}).get("args") or []},
            "ollama": {"exe": self.launcher.ollama_exe()},
            "config_path": str(self.launcher.config_path),
            "logs_dir": str(self.launcher.logs_dir),
        }

    def memory(self) -> dict[str, Any]:
        """What each GPU holds and which server holds it (the main ComfyUI,
        the render pool, Ollama, llama.cpp...), with the VRAM each image
        engine needs, so the studio can say why a render is slow and what to
        stop or free."""
        from .hoard_link import launch as launch_mod

        mem = launch_mod.memory(self.launcher, self.comfy_ports())
        mem["vram_estimates_mb"] = self.vram_estimates_mb()
        mem["main_comfy"] = f"comfyui@{self.main_comfy_port()}" if self.main_comfy_port() is not None else None
        return mem

    def _after_service_change(self) -> None:
        self._pool_health.clear()
        self.reload()

    def start_service(self, service_id: str, gpu: Any = None, wait_s: float = 0.0) -> dict[str, Any]:
        if self.demo:
            raise ValueError("demo mode has no real backends to start")
        sid = self._service_id(service_id)
        if sid == "render_pool":
            return self.start_render_pool(gpu_auto=True, wait_s=wait_s)
        res = self.launcher.start(sid, gpu=gpu, wait_s=wait_s)
        if res.get("ready") or res.get("already"):
            self._after_service_change()
        return res

    def start_render_pool(self, gpu_auto: bool = True, wait_s: float = 0.0) -> dict[str, Any]:
        """Start the main ComfyUI and every render-pool server, one GPU each
        (the launcher skips GPUs another ComfyUI already uses)."""
        results = []
        for port in self.comfy_ports():
            results.append(self.launcher.start(f"comfyui@{port}", gpu="auto" if gpu_auto else None, wait_s=0))
        if wait_s > 0:
            deadline = time.monotonic() + wait_s
            for r in results:
                if r.get("ok") and not r.get("already"):
                    r["ready"] = self.launcher.wait_ready(r["service"], max(1.0, deadline - time.monotonic()))
        self._after_service_change()
        return {"ok": all(r.get("ok") for r in results), "results": results}

    def stop_service(self, service_id: str) -> dict[str, Any]:
        sid = self._service_id(service_id)
        res = self.launcher.stop(sid)
        self._after_service_change()
        return res

    def set_launch(self, comfyui_dir: Optional[str] = None, comfyui_python: Optional[str] = None,
                   comfyui_gpu: Any = None, comfyui_args: Optional[list[str]] = None,
                   ollama_exe: Optional[str] = None, autostart_comfy: Optional[bool] = None) -> dict[str, Any]:
        """Where ComfyUI/Ollama live (written to the family-wide
        ~/.hoard/backends.json) and Prospero's own autostart switch.
        "" clears a path. Only a real ComfyUI folder, a Python interpreter
        and an ollama executable are accepted."""
        comfy: dict[str, Any] = {}
        if comfyui_dir is not None:
            if comfyui_dir.strip():
                d = Path(comfyui_dir.strip()).expanduser()
                if not (d / "main.py").is_file():
                    raise ValueError(f"{d} is not a ComfyUI folder (no main.py)")
                comfy["dir"] = str(d)
            else:
                comfy["dir"] = None
        if comfyui_python is not None:
            if comfyui_python.strip():
                py = Path(comfyui_python.strip()).expanduser()
                if not py.is_file() or not py.name.lower().startswith("python"):
                    raise ValueError(f"{py} is not a Python interpreter")
                comfy["python"] = str(py)
            else:
                comfy["python"] = None
        if comfyui_gpu is not None:
            comfy["gpu"] = comfyui_gpu if comfyui_gpu != "" else None
        if comfyui_args is not None:
            comfy["args"] = [str(a) for a in comfyui_args if str(a).strip()] or None
        patch: dict[str, Any] = {}
        if comfy:
            patch["comfyui"] = comfy
        if ollama_exe is not None:
            if ollama_exe.strip():
                exe = Path(ollama_exe.strip()).expanduser()
                if not exe.is_file() or not exe.name.lower().startswith("ollama"):
                    raise ValueError(f"{exe} is not an ollama executable")
                patch["ollama"] = {"exe": str(exe)}
            else:
                patch["ollama"] = {"exe": None}
        if patch:
            self.launcher.set_config(patch)
        if autostart_comfy is not None:
            raw = self._raw_config()
            raw.setdefault("launch", {})["autostart_comfy"] = bool(autostart_comfy)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self.config_path, json.dumps(raw, indent=2))
        return self.services()

    def wants_comfy_autostart(self, explicit: Optional[bool] = None) -> bool:
        """Should the caller start the main ComfyUI before using it? Only when
        autostart is on, the caller may (`explicit`, else: a GPU worker
        thread), it is a local server and its port is closed right now."""
        if not self.autostart_comfy():
            return False
        allowed = explicit if explicit is not None else bool(getattr(self._bound, "worker", False))
        if not allowed or getattr(self._bound, "url", None):
            return False
        port = self.main_comfy_port()
        return port is not None and not _port_open(f"http://127.0.0.1:{port}")

    def ensure_comfy(self, timeout_s: float = 300.0) -> bool:
        """Start the main ComfyUI (or this worker's pool server) and wait
        until it answers. One worker at a time; the others find it running."""
        target = getattr(self._bound, "url", None)
        port = comfy_port_from_url(target) if target else self.main_comfy_port()
        if port is None:
            return False
        with self._autostart_lock:
            res = self.launcher.start(f"comfyui@{port}", wait_s=timeout_s)
        ok = bool(res.get("ok") and (res.get("ready") or res.get("state") == "running"))
        if ok:
            self._after_service_change()
        else:
            import logging

            logging.getLogger(__name__).warning("could not start ComfyUI on port %s: %s", port,
                                                res.get("error") or res.get("detail"))
        return ok

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
            "render_pool": [{"url": u, "reachable": self.pool_server_ready(u, ttl_s=3.0)} for u in self.render_pool()],
            "comfy_dedicated": self._raw_config().get("comfy_dedicated") is True,
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
                "render_pool": self.render_pool(),
                "import_roots": [str(p) for p in self.import_roots()],
            },
            "token_set": self.token_set(),
            "services": self.services(),
        }


def _piper_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("piper") is not None
