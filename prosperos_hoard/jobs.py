"""Background job queue: one GPU worker thread, one CPU worker thread.

Jobs persist in SQLite (`Store`) so a restart survives: any job left
`running`/`waiting_gpu` is requeued on boot (`Store.requeue_running_jobs`).
Handlers are plain callables registered by job `type`; they receive the
job dict and a `progress(fraction, message=None)` callback and return an
`outputs` dict. A handler may raise:

- `WaitingForResources(reason)`: not enough free VRAM right now. The job
  shows `waiting_gpu` with the reason and is retried every
  `GPU_WAIT_POLL_S` seconds up to `GPU_WAIT_TIMEOUT_S`. Nothing is unloaded
  to make room; the lane stays on this job so the queue order is kept.
- `JobCancelled`: raised for it by `progress()` (and by `check_cancel()`)
  once the user or the agent asked to cancel.

Both worker loops are plain `threading.Thread`s (no multiprocessing), which
behaves the same on Windows (spawn) and Linux.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable, Optional

from .store import NotFound, Store
from .util import now_iso

logger = logging.getLogger("prosperos_hoard.jobs")

Handler = Callable[[dict[str, Any], "ProgressFn"], dict[str, Any]]
ProgressFn = Callable[..., None]

GPU_WAIT_POLL_S = 15.0
GPU_WAIT_TIMEOUT_S = 30 * 60.0


class WaitingForResources(Exception):
    """Raised by a GPU handler when it should be retried later, not failed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class JobCancelled(Exception):
    pass


class Progress:
    """The `progress` callback handed to handlers. Calling it records the
    fraction/message and raises `JobCancelled` if a cancel was requested;
    `progress.cancelled()` lets long loops (ffmpeg, ComfyUI polling) check
    without writing."""

    def __init__(self, store: Store, job_id: str):
        self.store = store
        self.job_id = job_id

    def __call__(self, fraction: float, message: Optional[str] = None) -> None:
        if self.store.is_cancel_requested(self.job_id):
            raise JobCancelled("cancelled")
        self.store.update_job(self.job_id, progress=max(0.0, min(1.0, float(fraction))), message=message)

    def cancelled(self) -> bool:
        return self.store.is_cancel_requested(self.job_id)

    def check_cancel(self) -> None:
        if self.cancelled():
            raise JobCancelled("cancelled")


class JobQueue:
    def __init__(self, store: Store):
        self.store = store
        self._handlers: dict[str, Handler] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake = {"gpu": threading.Event(), "cpu": threading.Event()}

    def register(self, type_: str, handler: Handler) -> None:
        self._handlers[type_] = handler

    def start(self) -> None:
        self.store.requeue_running_jobs()
        for lane in ("gpu", "cpu"):
            t = threading.Thread(target=self._worker_loop, args=(lane,), daemon=True, name=f"job-worker-{lane}")
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for ev in self._wake.values():
            ev.set()
        for t in self._threads:
            t.join(timeout=5.0)

    def enqueue(self, type_: str, lane: str, params: dict[str, Any], inputs: Optional[dict[str, Any]] = None,
                project_id: Optional[str] = None) -> dict[str, Any]:
        if type_ not in self._handlers:
            raise ValueError(f"no handler for job type '{type_}'")
        job = self.store.create_job(type_, lane, params, inputs, project_id)
        self._wake[lane].set()
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.store.request_cancel(job_id)

    def wait_for(self, job_id: str, timeout_s: float, poll_s: float = 0.25) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(float(timeout_s), 600.0))
        job = self.store.get_job(job_id)
        while job["state"] in ("queued", "waiting_gpu", "running") and time.monotonic() < deadline:
            time.sleep(poll_s)
            job = self.store.get_job(job_id)
        return job

    # ------------------------------------------------------------------
    def _worker_loop(self, lane: str) -> None:
        while not self._stop.is_set():
            try:
                job = self.store.next_queued_job(lane)
            except Exception:  # pragma: no cover - a locked db must not kill the worker
                logger.exception("could not read the job queue")
                self._stop.wait(1.0)
                continue
            if job is None:
                self._wake[lane].wait(timeout=1.0)
                self._wake[lane].clear()
                continue
            self._run_job(job)

    def _run_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        handler = self._handlers.get(job["type"])
        if handler is None:
            self.store.update_job(job_id, state="failed", message=f"no handler registered for job type '{job['type']}'",
                                   finished_at=now_iso())
            return
        progress = Progress(self.store, job_id)
        self.store.update_job(job_id, state="running", started_at=now_iso(), message="starting")
        first_wait: Optional[float] = None
        while True:
            try:
                outputs = handler(self.store.get_job(job_id), progress)
            except WaitingForResources as wait_exc:
                now = time.monotonic()
                first_wait = first_wait if first_wait is not None else now
                if now - first_wait > GPU_WAIT_TIMEOUT_S:
                    self.store.update_job(job_id, state="failed", message=f"timed out waiting: {wait_exc.reason}",
                                           finished_at=now_iso())
                    return
                self.store.update_job(job_id, state="waiting_gpu", message=wait_exc.reason)
                if self._sleep_unless_cancelled(job_id, GPU_WAIT_POLL_S):
                    self.store.update_job(job_id, state="cancelled", message="cancelled while waiting for the GPU",
                                           finished_at=now_iso())
                    return
                if self._stop.is_set():
                    return  # stays waiting_gpu; requeued on next boot
                self.store.update_job(job_id, state="running", message="retrying")
                continue
            except JobCancelled:
                self.store.update_job(job_id, state="cancelled", message="cancelled", finished_at=now_iso())
                return
            except NotFound as exc:
                self.store.update_job(job_id, state="failed", message=str(exc), finished_at=now_iso())
                return
            except Exception as exc:  # noqa: BLE001 - job handlers are arbitrary
                if progress.cancelled():
                    self.store.update_job(job_id, state="cancelled", message="cancelled", finished_at=now_iso())
                    return
                logger.exception("job %s failed", job_id)
                self.store.update_job(
                    job_id, state="failed", message=(str(exc) or type(exc).__name__)[:1000],
                    log_excerpt=traceback.format_exc()[-2000:], finished_at=now_iso(),
                )
                return
            if progress.cancelled():
                # finished anyway; keep the outputs but record the request
                self.store.update_job(job_id, state="done", progress=1.0, outputs=outputs, finished_at=now_iso(),
                                       message="finished before the cancel took effect")
            else:
                self.store.update_job(job_id, state="done", progress=1.0, outputs=outputs, finished_at=now_iso(),
                                       message="done")
            return

    def _sleep_unless_cancelled(self, job_id: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._stop.wait(min(0.5, max(0.01, deadline - time.monotonic()))):
                return False
            if self.store.is_cancel_requested(job_id):
                return True
        return self.store.is_cancel_requested(job_id)
