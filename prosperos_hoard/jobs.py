"""Background job queue: one GPU worker thread, one CPU worker thread.

Jobs persist in SQLite (`Store`) so a restart survives: any job left
`running`/`waiting_gpu` is requeued on boot except possibly submitted image
jobs, which are held as outcome_unknown (`Store.requeue_running_jobs`).
Handlers are plain callables registered by job `type`; they receive the
job dict and a `progress(fraction, message=None)` callback and return an
`outputs` dict. A handler may raise:

- `WaitingForResources(reason)`: not enough free VRAM right now. The job
  shows `waiting_gpu` with the reason and is retried every
  `GPU_WAIT_POLL_S` seconds up to `GPU_WAIT_TIMEOUT_S`. Nothing is unloaded
  to make room; the lane stays on this job so the queue order is kept.
- `JobCancelled`: raised for it by `progress()` (and by `check_cancel()`)
  once the user or the agent asked to cancel.
- `JobInterrupted`: raised by `progress()` (and `check_cancel()`) once the
  queue is stopping, so a long handler (a production waiting for its
  sub-jobs, a render polling ComfyUI) lets go of its worker thread instead of
  outliving the queue. It is a `BaseException`, so a handler's own
  `except Exception` cannot swallow it; the job keeps the state it had and
  is requeued on the next boot, like after a crash.

Orchestrator jobs (`ORCHESTRATOR_TYPES`: a whole production, a QA pass)
are stored on the cpu lane but run on a third, dedicated worker: they only
enqueue GPU/CPU sub-jobs and wait for them, so running them on the cpu
lane itself would deadlock behind the renders they wait for.

Every worker loop is a plain `threading.Thread` (no multiprocessing), which
behaves the same on Windows (spawn) and Linux.
"""

from __future__ import annotations

import contextlib
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
ORCHESTRATOR_TYPES = ("production", "production_qa", "space_run")
GPU_WAIT_TIMEOUT_S = 30 * 60.0


class WaitingForResources(Exception):
    """Raised by a GPU handler when it should be retried later, not failed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class JobCancelled(Exception):
    pass


class JobInterrupted(BaseException):
    """The queue is shutting down; the job is left as it is and restarts on the next boot."""


class Progress:
    """The `progress` callback handed to handlers. Calling it records the
    fraction/message and raises `JobCancelled` if a cancel was requested;
    `progress.cancelled()` lets long loops (ffmpeg, ComfyUI polling) check
    without writing."""

    def __init__(self, store: Store, job_id: str, on_progress: Optional[Callable[[str, float, Optional[str]], None]] = None,
                 stopping: Optional[threading.Event] = None):
        self.store = store
        self.job_id = job_id
        self.stopping = stopping  # set when the queue stops: the next progress()/check_cancel() raises JobInterrupted
        self.on_progress = on_progress  # the family job events (throttled there; a failing hook never reaches the job)

    def __call__(self, fraction: float, message: Optional[str] = None) -> None:
        if self.store.is_cancel_requested(self.job_id):
            raise JobCancelled("cancelled")
        self.check_stopping()
        fraction = max(0.0, min(1.0, float(fraction)))
        self.store.update_job(self.job_id, progress=fraction, message=message)
        if self.on_progress is not None:
            try:
                self.on_progress(self.job_id, fraction, message)
            except Exception:  # noqa: BLE001 - events are hints
                logger.debug("progress event failed", exc_info=True)

    def cancelled(self) -> bool:
        return self.store.is_cancel_requested(self.job_id)

    def check_cancel(self) -> None:
        if self.cancelled():
            raise JobCancelled("cancelled")
        self.check_stopping()

    def check_stopping(self) -> None:
        if self.stopping is not None and self.stopping.is_set():
            raise JobInterrupted("the job queue is stopping")

    def record_comfy_submission(self, receipt: dict[str, Any]) -> None:
        if self.store.get_job(self.job_id)["type"] in ("generate_image", "edit_image"):
            self.store.record_comfy_submission(self.job_id, receipt)


class JobQueue:
    def __init__(self, store: Store, gpu_targets: Optional[list[Optional[str]]] = None,
                 bind: Optional[Callable[[Optional[str]], None]] = None,
                 target_ready: Optional[Callable[[str], bool]] = None,
                 accepts: Optional[Callable[[dict[str, Any]], bool]] = None,
                 events: Any = None, gpu_guard: Optional[Callable[[dict[str, Any]], Any]] = None):
        """`events` (jobevents.JobEvents) hears queued / started / progress and the end of every job and tells the family hub;
        `gpu_guard(job)` returns a context manager held while a GPU-lane job runs (the hub's GPU lease). Both are optional hints:
        a failing one never reaches the job. `gpu_targets`: one GPU worker per entry (None = the main ComfyUI,
        a URL = a render-pool server); they all take jobs from the one GPU
        queue. `bind(target)` pins a worker thread to its server,
        `target_ready(url)` lets a pool worker skip taking jobs while its
        server is down."""
        self.store = store
        self._handlers: dict[str, Handler] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._wake = {"gpu": threading.Event(), "cpu": threading.Event(), "orchestrator": threading.Event()}
        self._gpu_targets: list[Optional[str]] = list(gpu_targets or [None])
        self._bind = bind
        self._target_ready = target_ready
        # `accepts(job)`, asked on the GPU worker's own thread (bound to its
        # server): False leaves the job for another worker - a model too big
        # for this card goes to one that holds it
        self._accepts = accepts
        self._claim_lock = threading.Lock()
        self.events = events
        self._gpu_guard = gpu_guard

    def _event(self, name: str, *args: Any) -> None:
        if self.events is None:
            return
        try:
            getattr(self.events, name)(*args)
        except Exception:  # noqa: BLE001 - events are hints
            logger.debug("job event %s failed", name, exc_info=True)

    def register(self, type_: str, handler: Handler) -> None:
        self._handlers[type_] = handler

    def start(self) -> None:
        self.store.requeue_running_jobs()
        workers: list[tuple[str, Optional[str]]] = ([("gpu", target) for target in self._gpu_targets]
                                                    + [("cpu", None), ("orchestrator", None)])
        for i, (lane, target) in enumerate(workers):
            name = f"job-worker-{lane}" + (f"-{i}" if lane == "gpu" and i else "")
            t = threading.Thread(target=self._worker_loop, args=(lane, target), daemon=True, name=name)
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
        self._event("queued", job)
        self._wake["orchestrator" if type_ in ORCHESTRATOR_TYPES else lane].set()
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
    def _worker_loop(self, lane: str, target: Optional[str] = None) -> None:
        if self._bind is not None and lane == "gpu":
            self._bind(target)
        while not self._stop.is_set():
            if target and self._target_ready is not None and not self._target_ready(target):
                self._stop.wait(5.0)  # this worker's server is off: leave the queue to the others
                continue
            try:
                job = self._claim(lane, gpu_worker=lane == "gpu")
            except Exception:  # pragma: no cover - a locked db must not kill the worker
                logger.exception("could not read the job queue")
                self._stop.wait(1.0)
                continue
            if job is None:
                self._wake[lane].wait(timeout=1.0)
                self._wake[lane].clear()  # a GPU worker that misses a wake-up finds the job on its next 1 s poll
                continue
            self._run_job(job)

    def _claim(self, lane: str, gpu_worker: bool = False) -> Optional[dict[str, Any]]:
        """Take the oldest queued job of a lane (that this GPU worker accepts);
        with several GPU workers the read and the state change happen under
        one lock, so no two workers start the same job."""
        skipped: set[str] = set()
        with self._claim_lock:
            while True:
                if lane == "orchestrator":
                    job = self.store.next_queued_job("cpu", types=ORCHESTRATOR_TYPES)
                elif lane == "cpu":
                    job = self.store.next_queued_job("cpu", exclude_types=ORCHESTRATOR_TYPES)
                else:
                    job = self.store.next_queued_job(lane, skip_ids=skipped or None)
                if job is None or not gpu_worker or self._accepts is None or len(skipped) >= 50:
                    break
                try:
                    if self._accepts(job):
                        break
                except Exception:  # noqa: BLE001 - a failed check never blocks a job
                    break
                skipped.add(job["id"])
            if job is not None:
                self.store.update_job(job["id"], state="running", started_at=now_iso(), message="starting")
            return job

    def _run_job(self, job: dict[str, Any]) -> None:
        try:
            self._run_job_inner(job)
        finally:
            try:
                final = self.store.get_job(job["id"])
            except NotFound:
                return
            if final["state"] in ("done", "failed", "cancelled"):
                self._event("finished", final)

    def _guard(self, job: dict[str, Any]) -> Any:
        """The context a handler runs in: the hub's GPU lease on the GPU lane, nothing elsewhere."""
        if self._gpu_guard is None or job.get("lane") != "gpu":
            return contextlib.nullcontext()
        return self._gpu_guard(job)

    def _run_job_inner(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        handler = self._handlers.get(job["type"])
        if handler is None:
            self.store.update_job(job_id, state="failed", message=f"no handler registered for job type '{job['type']}'",
                                   finished_at=now_iso())
            return
        progress = Progress(self.store, job_id, (lambda *a: self._event("progress", *a)) if self.events is not None else None,
                            stopping=self._stop)
        self.store.update_job(job_id, state="running", started_at=now_iso(), message="starting")
        self._event("started", self.store.get_job(job_id))
        first_wait: Optional[float] = None
        while True:
            try:
                with self._guard(job):
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
            except JobInterrupted:
                self.store.update_job(job_id, message="interrupted by shutdown; it restarts on the next start")
                return  # state unchanged: requeue_running_jobs() decides on the next boot what is safe to restart
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
