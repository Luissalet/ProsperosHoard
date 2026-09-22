"""Background job queue: one GPU worker thread, one CPU worker thread.

Jobs persist in SQLite (`Store`) so a restart survives: any job left
`running`/`waiting_gpu` is requeued on boot (`Store.requeue_running_jobs`).
Handlers are plain callables registered by job `type`; they receive the
job dict and a `progress(fraction, message=None)` callback and return an
`outputs` dict, or raise `WaitingForResources` to defer a GPU job.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable, Optional

from .store import Store
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


class JobQueue:
    def __init__(self, store: Store):
        self.store = store
        self._handlers: dict[str, Handler] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake = {"gpu": threading.Event(), "cpu": threading.Event()}
        self._waiting_since: dict[str, float] = {}

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
        job = self.store.create_job(type_, lane, params, inputs, project_id)
        self._wake[lane].set()
        return job

    def wait_for(self, job_id: str, timeout_s: float, poll_s: float = 0.3) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        job = self.store.get_job(job_id)
        while job["state"] in ("queued", "waiting_gpu", "running") and time.monotonic() < deadline:
            time.sleep(poll_s)
            job = self.store.get_job(job_id)
        return job

    # ------------------------------------------------------------------
    def _worker_loop(self, lane: str) -> None:
        while not self._stop.is_set():
            job = self.store.next_queued_job(lane)
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

        def progress(fraction: float, message: Optional[str] = None) -> None:
            self.store.update_job(job_id, progress=max(0.0, min(1.0, fraction)), message=message)

        self.store.update_job(job_id, state="running", started_at=now_iso(), message=None)
        try:
            outputs = handler(self.store.get_job(job_id), progress)
            self.store.update_job(job_id, state="done", progress=1.0, outputs=outputs, finished_at=now_iso())
            self._waiting_since.pop(job_id, None)
        except WaitingForResources as wait_exc:
            first_seen = self._waiting_since.setdefault(job_id, time.monotonic())
            if time.monotonic() - first_seen > GPU_WAIT_TIMEOUT_S:
                self.store.update_job(job_id, state="failed", message=f"timed out waiting: {wait_exc.reason}",
                                       finished_at=now_iso())
                self._waiting_since.pop(job_id, None)
                return
            self.store.update_job(job_id, state="waiting_gpu", message=wait_exc.reason)
            time.sleep(GPU_WAIT_POLL_S)
            # leave state as waiting_gpu but make it eligible to be picked up
            # again by re-marking it queued so next_queued_job() finds it.
            self.store.update_job(job_id, state="queued")
        except Exception as exc:  # noqa: BLE001 - job handlers are arbitrary
            logger.exception("job %s failed", job_id)
            self.store.update_job(
                job_id, state="failed",
                message=str(exc),
                log_excerpt=traceback.format_exc()[-2000:],
                finished_at=now_iso(),
            )
