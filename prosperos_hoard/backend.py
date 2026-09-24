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
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import comfy_driver, procutil
from .hoard_link import Link, LinkConfig, Unavailable

# SDXL/SD1.5/SVD/Flux/Kontext/Wan/ACE-Step/Qwen-Image 2.1 figures from the
# model notes; editable at runtime via data/backend.json -> "vram_estimates_mb".
# Flux and Kontext fp8: ~13 GB; Wan 5B at 1280x704 (lower at 960x544): ~12 GB;
# ACE-Step 1.5 turbo: ~8 GB; Qwen-Image 2.1 int8: ~7.3 GB diffusion + 9.4 GB
# text encoder loaded one after the other, peak ~10-12 GB at 1 MP (more at 2K).
DEFAULT_VRAM_ESTIMATES_MB = {
    "sdxl": 7000,
    "sd15": 3500,
    "svd": 10000,
    "flux": 13000,
    "kontext": 13000,
    "wan": 12000,
    "ace": 8000,
    "qwen21": 12000,
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


OBJECT_INFO_TTL_S = 60.0


class ComfyRunner:
    """One job's view of ComfyUI: the Link (so one event loop) it was
    created on, and the ComfyUI client made on that loop. See
    `Backend.runner()`."""

    def __init__(self, backend: "Backend", link: Any, pool_url: Optional[str]):
        self._backend = backend
        self.link = link
        self.pool_url = pool_url
        self._client: Any = None
        self._resolved = False
        self._closed = False

    def run(self, call: Any) -> Any:
        """Run `lambda link: <coroutine>` or a coroutine on this runner's loop."""
        if callable(call):
            return self.link.sync._run(call)
        coro = call
        return self.link.sync._run(lambda _link: coro)

    def comfy(self):
        """The ComfyClient for this runner's server (None when the main
        ComfyUI cannot be resolved). Resolved once, then reused."""
        if not self._resolved:
            if self.pool_url:
                self._client = self._backend._pool_client(self.pool_url, self.link)
            else:
                self._client = self.run(lambda link: link.comfy())
            self._resolved = True
        return self._client

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._backend._release(self.link)

    def __enter__(self) -> "ComfyRunner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# The vendored ComfyClient only knows `/interrupt` without a body (which
# stops whatever ComfyUI is running, maybe someone else's prompt) and has no
# `/queue` call; these wrap its HTTP client instead of editing hoard_link.

async def comfy_queue_ids(comfy: Any) -> tuple[set[str], set[str]]:
    """(running, pending) prompt ids from ComfyUI's `GET /queue`."""
    resp = await comfy._client.get(f"{comfy.url}/queue", timeout=5.0)
    resp.raise_for_status()
    data = resp.json() or {}

    def ids(key: str) -> set[str]:
        out = set()
        for item in data.get(key) or []:
            if isinstance(item, (list, tuple)) and len(item) > 1:
                out.add(str(item[1]))
        return out

    return ids("queue_running"), ids("queue_pending")


async def cancel_prompt(comfy: Any, prompt_id: str) -> dict[str, bool]:
    """Take one prompt off ComfyUI without touching anyone else's: delete it
    from the pending queue, and interrupt only when it is the prompt
    running right now (the body names it, so a ComfyUI that understands
    `prompt_id` also refuses to stop a different one). Best effort: every
    step swallows its own errors. Returns what was done."""
    done = {"deleted": False, "interrupted": False}
    running: set[str] = set()
    try:
        running, _pending = await comfy_queue_ids(comfy)
    except Exception:
        pass
    try:
        resp = await comfy._client.post(f"{comfy.url}/queue", json={"delete": [prompt_id]}, timeout=5.0)
        done["deleted"] = resp.status_code < 400
    except Exception:
        pass
    if prompt_id in running:
        try:
            resp = await comfy._client.post(f"{comfy.url}/interrupt", json={"prompt_id": prompt_id}, timeout=5.0)
            done["interrupted"] = resp.status_code < 400
        except Exception:
            pass
    return done


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
        # render pool: which ComfyUI this worker thread talks to (None = the
        # main one Hoard Link resolves), one cached client per extra server
        # and per Link (a client's HTTP pool belongs to that Link's loop)
        self._bound = threading.local()
        self._pool_clients: dict[Any, dict[str, Any]] = {}
        self._pool_health: dict[str, tuple[float, bool]] = {}
        self._pool_lock = threading.Lock()
        # how many runners (jobs, status calls) still use each Link; a Link
        # replaced by reload() is closed once nothing holds it any more
        self._link_refs: dict[Any, int] = {}
        # object_info per ComfyUI URL: (fetched_at, info)
        self._object_info_cache: dict[str, tuple[float, dict[str, Any]]] = {}

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
        """Swap in a Link built from the current config. A job that is
        mid-call keeps the Link (and event loop) its runner pinned; the old
        Link is closed - its loop thread stopped, its clients released -
        as soon as the last runner holding it is done."""
        new_link = self._build_link()
        with self._pool_lock:
            old = self.link
            self.link = new_link
            idle = self._link_refs.get(old, 0) <= 0
        self._pool_health.clear()
        if idle:
            self._close_link(old)

    def runner(self, url: Optional[str] = None) -> "ComfyRunner":
        """A handle that pins the current Link (and so one event loop) for a
        whole job: every ComfyUI call a job makes goes through it, so a
        Settings save (`reload()`) mid-render never moves a later poll onto
        a different loop than the client it was made with. Use it as a
        context manager (or call `close()`). `url` names a render-pool
        server; by default the worker thread's bound server (or the main
        one)."""
        with self._pool_lock:
            link = self.link
            self._link_refs[link] = self._link_refs.get(link, 0) + 1
        return ComfyRunner(self, link, url.rstrip("/") if url else getattr(self._bound, "url", None))

    def _release(self, link: Any) -> None:
        with self._pool_lock:
            left = self._link_refs.get(link, 0) - 1
            if left > 0:
                self._link_refs[link] = left
                return
            self._link_refs.pop(link, None)
            retired = link is not self.link
        if retired:
            self._close_link(link)

    def _close_link(self, link: Any) -> None:
        with self._pool_lock:
            clients = list((self._pool_clients.pop(link, None) or {}).values())
        try:
            for client in clients:
                try:
                    link.sync._run(lambda _l, c=client: c.aclose())
                except Exception:  # pragma: no cover - best effort
                    pass
            link.sync.close()
        except Exception:  # pragma: no cover - best effort
            pass

    def run_async(self, call: Any) -> Any:
        """Run async Hoard Link work from a plain sync worker thread on Hoard
        Link's own background event loop. `call` is either
        `lambda link: <coroutine>` (preferred: it gets the loop-bound link)
        or a coroutine that only touches objects created on that loop
        (e.g. methods of the ComfyClient returned by `comfy()`). Work that
        spans several calls (a whole job) should use `runner()` instead."""
        with self.runner() as r:
            return r.run(call)

    def comfy(self):
        """The ComfyUI client bound to Hoard Link's loop, or None. On a
        render-pool worker thread this is that worker's own server."""
        with self.runner() as r:
            return r.comfy()

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
        """Pin the calling worker thread to one ComfyUI (None = the main one)."""
        self._bound.url = url.rstrip("/") if url else None

    def _pool_client(self, url: str, link: Any = None):
        """The cached ComfyClient for one extra server, created on `link`'s
        loop (default: the current Link)."""
        link = link if link is not None else self.link
        with self._pool_lock:
            client = (self._pool_clients.get(link) or {}).get(url)
        if client is None:
            from .hoard_link._comfy import ComfyClient

            async def make(_link):
                return ComfyClient(url)  # created on that Link's loop, reused by every job on it

            client = link.sync._run(make)
            with self._pool_lock:
                client = self._pool_clients.setdefault(link, {}).setdefault(url, client)
        return client

    def pool_server_ready(self, url: str, ttl_s: float = 10.0) -> bool:
        """Cheap, cached reachability check a pool worker makes before it
        takes a job, so a server that is off never swallows the queue."""
        now = time.monotonic()
        cached = self._pool_health.get(url)
        if cached and now - cached[0] < ttl_s:
            return cached[1]
        try:
            with self.runner(url) as r:
                r.run(r.comfy().system_stats())
            ok = True
        except Exception:
            ok = False
        self._pool_health[url] = (now, ok)
        return ok

    def object_info(self, runner: "ComfyRunner", ttl_s: Optional[float] = None) -> dict[str, Any]:
        """ComfyUI's `/object_info` for the runner's server, cached ~60 s per
        URL (it is a large answer and every job needs it). A fetch that
        fails falls back to the last copy of that server's answer, however
        old; with none, the error propagates."""
        comfy = runner.comfy()
        if comfy is None:
            raise Unavailable("image", ["ComfyUI is not reachable"])
        key = comfy.url
        ttl_s = OBJECT_INFO_TTL_S if ttl_s is None else ttl_s
        now = time.monotonic()
        with self._pool_lock:
            cached = self._object_info_cache.get(key)
        if cached and now - cached[0] < ttl_s:
            return cached[1]
        try:
            info = runner.run(comfy.object_info())
        except Exception:
            if cached:
                return cached[1]
            raise
        if info:
            with self._pool_lock:
                self._object_info_cache[key] = (now, info)
        return info

    def set_overrides(
        self,
        faustus_url: str | None = None,
        faustus_token: str | None = None,
        comfy_url: str | None = None,
        vram_estimates_mb: dict[str, int] | None = None,
        import_roots: list[str] | None = None,
        render_pool: list[str] | None = None,
    ) -> None:
        raw = self._raw_config()
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
        self.config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        self.reload()

    def vram_estimates_mb(self) -> dict[str, int]:
        raw = self._raw_config()
        return {**DEFAULT_VRAM_ESTIMATES_MB, **(raw.get("vram_estimates_mb") or {})}

    def import_roots(self, lexical: bool = False) -> list[Path]:
        """Folders `studio_import` may read from: the user's home folder,
        the app's own `data/inbox/`, and any extra folders configured in
        Settings (`backend.json -> import_roots`). `lexical=True` gives them
        made absolute without touching the filesystem (no symlink
        resolution), for a check that must run before any stat."""
        roots = [Path.home(), self.data_dir / "inbox"]
        for extra in self._raw_config().get("import_roots") or []:
            roots.append(Path(extra))
        out: list[Path] = []
        for r in roots:
            try:
                resolved = Path(os.path.abspath(r.expanduser())) if lexical else r.expanduser().resolve()
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
        `vram_free` already counts the models it keeps cached as evictable.
        Memory its allocator has reserved is added back too - ComfyUI frees
        both itself when the next job needs room, so treating them as used
        would make a job wait for memory its own renderer is keeping warm.
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
            with self.runner() as r:
                comfy = r.comfy()
                if comfy is None:
                    return None
                stats = r.run(comfy.system_stats())
        except Exception:
            return None
        frees: list[int] = []
        for dev in stats.get("devices") or []:
            if dev.get("vram_free") is None or str(dev.get("type") or "").lower() == "cpu":
                continue
            reserved_in_use = int(dev.get("torch_vram_total") or 0) - int(dev.get("torch_vram_free") or 0)
            frees.append((int(dev["vram_free"]) + max(0, reserved_in_use)) // (1024 * 1024))
        return max(frees) if frees else None

    def free_comfy_memory(self) -> dict[str, Any]:
        with self.runner() as r:
            comfy = r.comfy()
            if comfy is None:
                raise Unavailable("image", ["ComfyUI is not reachable"])
            r.run(comfy.free(unload_models=True, free_memory=True))
        return {"ok": True, "message": "asked ComfyUI to unload its models and free memory"}

    # -- status ---------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self.runner() as r:  # holds the Link so a concurrent reload cannot close it mid-call
            link_status = r.link.sync.status()
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
                with self.runner() as r:
                    client = r.comfy()
                    if client is not None:
                        object_info = self.object_info(r)
                        stats = r.run(client.system_stats())
                    comfy_info["devices"] = [
                        {"name": d.get("name"), "vram_total_mb": int(d.get("vram_total", 0)) // (1024 * 1024),
                         "vram_free_mb": int(d.get("vram_free", 0)) // (1024 * 1024)}
                        for d in stats.get("devices", [])
                    ]
                    comfy_info["version"] = (stats.get("system") or {}).get("comfyui_version")
                    # which built-in templates would run here, or what each lacks
                    comfy_info["templates"] = comfy_driver.template_readiness(object_info or {})
        except Exception as exc:  # pragma: no cover - defensive
            comfy_info = {"reachable": False, "reason": str(exc)[:300]}

        music = self.music_backends(object_info)
        raw = self._raw_config()
        faustus = raw.get("faustus") or {}

        return {
            "demo": self.demo,
            "hoard_link": link_status,
            "comfy": comfy_info,
            "render_pool": [{"url": u, "reachable": self.pool_server_ready(u, ttl_s=0.0)} for u in self.render_pool()],
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
        }


def _piper_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("piper") is not None
