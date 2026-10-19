"""Background jobs: proxies, analysis, transcription, renders. Persisted in the jobs table so the UI and an agent can
follow them (progress 0..1, detail line) and cancel them. Two lanes: ``work`` (several at once) and ``render`` (one at
a time; a render already encodes its chunks in parallel)."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .errors import LumiereError, NotFound
from .ffmpeg import Cancelled, RunHandle
from .util import dumps, new_id

log = logging.getLogger("lumiere.jobs")

RENDER_KINDS = {"video_compress", "render", "preview_render", "html_motion"}


@dataclass
class JobCtx:
    id: str
    kind: str
    params: dict[str, Any]
    queue: "JobQueue"
    handles: list[RunHandle] = field(default_factory=list)
    cancelled: bool = False
    _last: float = 0.0

    def progress(self, value: float, detail: Optional[str] = None, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 0.4 and value < 1:
            return
        self._last = now
        sets, args = ["progress = ?"], [max(0.0, min(1.0, float(value)))]
        if detail is not None:
            sets.append("detail = ?")
            args.append(detail[:300])
        self.queue.db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", (*args, self.id))
        self.queue._event("progress", self.id, self.kind, self.params, args[0])

    def handle(self) -> RunHandle:
        h = RunHandle()
        if self.cancelled:
            h.cancelled = True
        self.handles.append(h)
        return h

    def check(self) -> None:
        if self.cancelled:
            raise Cancelled("Canceled.")


JobFn = Callable[[JobCtx], dict[str, Any]]


class JobQueue:
    def __init__(self, db, workers: int = 2, *, on_done: Optional[Callable[[dict], None]] = None, run_inline: bool = False, events: Any = None):
        self.db = db
        self.events = events  # jobevents.JobEvents: tells the family hub how renders go (hints, never an error)
        self.workers = workers
        self.fns: dict[str, JobFn] = {}
        self.on_done = on_done
        self.run_inline = run_inline  # tests: run jobs synchronously in submit()
        self._q: "queue.Queue[str]" = queue.Queue()
        self._rq: "queue.Queue[str]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._running: dict[str, JobCtx] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def register(self, kind: str, fn: JobFn) -> None:
        self.fns[kind] = fn

    def _event(self, name: str, *args: Any) -> None:
        if self.events is None:
            return
        try:
            getattr(self.events, name)(*args)
        except Exception:  # noqa: BLE001
            log.debug("job event %s failed", name, exc_info=True)

    # ------------------------------------------------------------ lifecycle
    def start(self, *, resume_pending: bool = True) -> None:
        # anything that was running when the app stopped did not finish
        if resume_pending:
            self._recover_pending()
        else:
            self._start_threads([])

    def _recover_pending(self) -> None:
        self.db.execute("UPDATE jobs SET state = 'failed', error = 'Interrupted: the app stopped while it ran.', finished_ts = ? "
                        "WHERE state IN ('running', 'queued') AND kind IN ('render', 'preview_render')", (time.time(),))
        pending = [r["id"] for r in self.db.query("SELECT id FROM jobs WHERE state IN ('queued', 'running') ORDER BY created_ts")]
        for jid in pending:
            self.db.execute("UPDATE jobs SET state = 'queued', progress = 0 WHERE id = ?", (jid,))
        self._start_threads(pending)

    def _start_threads(self, pending) -> None:
        if self.run_inline:
            return
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, args=(self._q,), name=f"lumiere-work-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._loop, args=(self._rq,), name="lumiere-render", daemon=True)
        t.start()
        self._threads.append(t)
        for jid in pending:
            self._enqueue(jid)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for ctx in self._running.values():
                ctx.cancelled = True
                for h in ctx.handles:
                    h.cancel()
        for _ in self._threads:
            self._q.put("")
            self._rq.put("")
        for thread in self._threads:
            thread.join()
        self._threads.clear()

    def _enqueue(self, job_id: str) -> None:
        row = self.db.one("SELECT kind FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            return
        (self._rq if row["kind"] in RENDER_KINDS else self._q).put(job_id)

    # ------------------------------------------------------------ submit / cancel
    def submit(self, kind: str, params: dict[str, Any], *, label: str = "", media_id: Optional[str] = None, project_id: Optional[str] = None,
               dedupe: bool = True) -> dict[str, Any]:
        if kind not in self.fns:
            raise LumiereError(f"Unknown job kind {kind}.")
        if dedupe:
            row = self.db.one("SELECT id FROM jobs WHERE kind = ? AND state IN ('queued', 'running') AND IFNULL(media_id, '') = ? "
                              "AND IFNULL(project_id, '') = ? AND params = ?", (kind, media_id or "", project_id or "", dumps(params)))
            if row:
                return self.get(row["id"])
        job_id = new_id("job")
        self.db.execute("INSERT INTO jobs(id, kind, label, state, media_id, project_id, params, created_ts) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
                        (job_id, kind, label[:200], media_id, project_id, dumps(params), time.time()))
        self._event("queued", self.get(job_id))
        if self.run_inline:
            self._run(job_id)
        else:
            self._enqueue(job_id)
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["state"] == "queued":
            self.db.execute("UPDATE jobs SET state = 'canceled', finished_ts = ? WHERE id = ? AND state = 'queued'", (time.time(), job_id))
            self._event("cancelled", self.get(job_id))
        with self._lock:
            ctx = self._running.get(job_id)
            if ctx:
                ctx.cancelled = True
                for h in ctx.handles:
                    h.cancel()
        return self.get(job_id)

    # ------------------------------------------------------------ read
    def get(self, job_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFound(f"No job {job_id}.")
        return view(row)

    def list(self, *, state: Optional[str] = None, media_id: Optional[str] = None, project_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        where, args = [], []
        if state == "active":
            where.append("state IN ('queued', 'running')")
        elif state:
            where.append("state = ?")
            args.append(state)
        if media_id:
            where.append("media_id = ?")
            args.append(media_id)
        if project_id:
            where.append("project_id = ?")
            args.append(project_id)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_ts DESC LIMIT ?"
        return [view(r) for r in self.db.query(sql, (*args, limit))]

    def wait(self, job_id: str, timeout: float = 600) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(job_id)
            if job["state"] in ("done", "failed", "canceled"):
                return job
            time.sleep(0.1)
        return self.get(job_id)

    # ------------------------------------------------------------ worker
    def _loop(self, q: "queue.Queue[str]") -> None:
        while not self._stop.is_set():
            job_id = q.get()
            if not job_id or self._stop.is_set():
                continue
            try:
                self._run(job_id)
            except Exception:  # noqa: BLE001
                log.exception("job loop")

    def _run(self, job_id: str) -> None:
        from ..global_model import scope
        with scope():
            return self._run_pinned(job_id)

    def _run_pinned(self, job_id: str) -> None:
        row = self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None or row["state"] != "queued":
            return
        ctx = JobCtx(job_id, row["kind"], json.loads(row["params"] or "{}"), self)
        with self._lock:
            self._running[job_id] = ctx
        self.db.execute("UPDATE jobs SET state = 'running', started_ts = ?, progress = 0 WHERE id = ?", (time.time(), job_id))
        self._event("started", self.get(job_id))
        state, result, error = "done", {}, ""
        try:
            result = self.fns[row["kind"]](ctx) or {}
        except Cancelled:
            state, error = "canceled", "Canceled."
        except (LumiereError, NotFound) as exc:
            state, error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001
            state, error = "failed", f"{type(exc).__name__}: {exc}"
            log.error("job %s (%s) failed:\n%s", job_id, row["kind"], traceback.format_exc())
        finally:
            with self._lock:
                self._running.pop(job_id, None)
        self.db.execute("UPDATE jobs SET state = ?, result = ?, error = ?, finished_ts = ?, progress = CASE WHEN ? = 'done' THEN 1 ELSE progress END "
                        "WHERE id = ?", (state, dumps(result), error[:2000], time.time(), state, job_id))
        self._event("forget", job_id)
        if self.on_done:
            try:
                self.on_done(self.get(job_id))
            except Exception:  # noqa: BLE001
                log.exception("job on_done")


def view(row) -> dict[str, Any]:
    started, finished = row["started_ts"], row["finished_ts"]
    return {
        "id": row["id"], "kind": row["kind"], "label": row["label"], "state": row["state"], "progress": round(row["progress"], 4),
        "detail": row["detail"], "media_id": row["media_id"], "project_id": row["project_id"], "params": json.loads(row["params"] or "{}"),
        "result": json.loads(row["result"] or "{}"), "error": row["error"], "created_ts": row["created_ts"],
        "elapsed_s": round((finished or time.time()) - started, 1) if started else None,
    }
