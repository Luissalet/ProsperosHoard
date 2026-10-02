"""The family hub's GPU lease for Prospero's GPU jobs, on top of its own VRAM gate (`engine.check_vram_or_wait`).

While a GPU-lane job runs, the hub knows it holds the card, so the language models, the editor's transcription and the image engine
queue behind one another instead of loading at the same moment. The lease is a hint with a fallback: with no hub the library checks the
card locally and lets the job go; any other failure only logs. `PROSPERO_GPU_LEASE=0` (also `off`, `false`, `no`) turns it off
completely and the job runs exactly as before. When the hub keeps the lease queued past `WAIT_S`, the job goes to `waiting_gpu`
(its own retry loop, up to 30 minutes) instead of blocking the worker.

Pure logic: no FastAPI here. `hold(job, vram_estimates)` is a context manager; `lease_fn` is injectable so tests never need a hub.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Callable, Iterator, Optional

from .hoard_link import LeaseTimeout, lease as default_lease
from .jobs import WaitingForResources

log = logging.getLogger("prosperos_hoard.gpu_lease")

ENV = "PROSPERO_GPU_LEASE"
OWNER = "prospero"
WAIT_S = 120.0
DEFAULT_MB = 7000
#: job type -> the VRAM class it needs (an image job's class comes from its engine)
TYPE_CLASS = {"animate": "wan", "compose_song": "ace", "character_sheet": "qwen21", "edit_image": "qwen21"}


def enabled() -> bool:
    return os.environ.get(ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def vram_class(job: dict[str, Any], resolve_image_engine: Optional[Callable[[], str]] = None) -> str:
    params = job.get("params") or {}
    if job["type"] == "generate_image":
        picked = str(params.get("engine") or "auto")
        if picked != "auto":
            return picked
        try:
            return resolve_image_engine() if resolve_image_engine else "sdxl"
        except Exception:  # noqa: BLE001 - no answer from ComfyUI: the default class
            return "sdxl"
    return TYPE_CLASS.get(job["type"], "sdxl")


def estimate_mb(job: dict[str, Any], vram_estimates: dict[str, int], resolve_image_engine: Optional[Callable[[], str]] = None) -> int:
    return int(vram_estimates.get(vram_class(job, resolve_image_engine), DEFAULT_MB))


def _purpose(job: dict[str, Any]) -> str:
    return f"{job['type']} {job['id']}"[:80]


@contextlib.contextmanager
def hold(job: dict[str, Any], vram_estimates: Callable[[], dict[str, int]], *, lease_fn: Optional[Callable[..., Any]] = None,
         resolve_image_engine: Optional[Callable[[], str]] = None, on_gpu: Optional[Callable[[str, Optional[int]], None]] = None) -> Iterator[Any]:
    """Hold the hub's lease while the body runs (nothing when it is off or cannot be had). ``on_gpu(job_id, gpu)`` hears which card."""
    if not enabled():
        yield None
        return
    lease_cm: Any = None
    try:
        mb = estimate_mb(job, vram_estimates(), resolve_image_engine)
        lease_cm = (lease_fn or default_lease)(vram_mb=mb, purpose=_purpose(job), owner=OWNER, timeout_s=WAIT_S)
        lease_cm.__enter__()
    except WaitingForResources:
        raise
    except Exception as error:  # noqa: BLE001
        lease_cm = None
        if isinstance(error, LeaseTimeout):
            raise WaitingForResources(f"waiting for the family hub's GPU lease ({error}); retrying every 15 s for up to 30 min.") from error
        log.info("no GPU lease for job %s (%s); it runs under its own VRAM gate", job.get("id"), error)
    if lease_cm is not None and on_gpu is not None:
        try:
            on_gpu(job["id"], getattr(lease_cm, "gpu", None))
        except Exception:  # noqa: BLE001
            pass
    try:
        yield lease_cm
    finally:
        if lease_cm is not None:
            try:
                lease_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.debug("releasing the GPU lease failed", exc_info=True)
