"""What the family hub hears about renders, and the notification when one needs the person.

Renders (and lossless cuts) are the jobs other apps care about. Their lifecycle is sent as the hub's canonical job events,
``lumiere.job.queued|started|progress`` here and, at the end, ``lumiere.render.done|failed`` (the hub maps those two names onto
``lumiere.job.done|failed`` with ``kind: render`` and keeps the old name, so rules written against either match once, and nothing is
sent twice). Every event carries ``{job_id, title (the project's name), kind: "render", progress, url}``; a finished render also carries
``ref`` = ``hoard://lumiere/render/<id>``, which is what the hub's rule hands to the publishing app to start a draft post.

Notifications go through the hub only (Lumiere has no channel of its own): a finished or failed export asks the hub to tell the person.
Setting ``notify.via``: ``auto`` (the hub when it answers), ``hub`` (always try it) or ``off`` (nothing).

Events and notifications are hints: a failing hub never reaches the job.
"""

from __future__ import annotations

import logging
import threading
from ..lifetime import Threads
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from .util import clip
from .hoard_link.fam_notify import Router

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger("lumiere.jobevents")

KIND = "render"
JOB_KINDS = {"render", "copy_cut", "preview_render"}   # the job kinds that are renders
VIA = ("auto", "hub", "off")


def ref(render_id: str) -> str:
    return f"hoard://lumiere/render/{render_id}"


class JobEvents:
    def __init__(self, svc: "Services", *, min_interval_s: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.svc, self.min_interval_s, self._clock = svc, float(min_interval_s), clock
        self._lock = threading.Lock()
        self._started: dict[str, float] = {}
        self._last: dict[str, float] = {}

    # ------------------------------------------------------------------ data
    def title(self, project_id: Optional[str], fallback: str = "") -> str:
        row = self.svc.db.one("SELECT name FROM projects WHERE id = ?", (project_id,)) if project_id else None
        return clip((row["name"] if row else "") or fallback or "Render", 120)

    def url(self, project_id: Optional[str]) -> str:
        base = self.svc.base_url().rstrip("/")
        return f"{base}/#/p/{project_id}" if base and project_id else ""

    def fields(self, job_id: str, project_id: Optional[str], *, label: str = "", progress: Optional[float] = None, render_id: str = "") -> dict[str, Any]:
        data: dict[str, Any] = {"job_id": job_id, "title": self.title(project_id, label), "kind": KIND, "url": self.url(project_id)}
        if progress is not None:
            data["progress"] = round(progress, 3)
        if render_id:
            data["ref"] = ref(render_id)
        return data

    @staticmethod
    def is_render(kind: str) -> bool:
        return kind in JOB_KINDS

    def _send(self, name: str, data: dict[str, Any]) -> None:
        try:
            self.svc.emit(name, data)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ lifecycle
    def queued(self, job: dict[str, Any]) -> None:
        if self.is_render(job["kind"]):
            self._send("lumiere.job.queued", self.fields(job["id"], job.get("project_id"), label=job.get("label", ""), progress=0.0))

    def started(self, job: dict[str, Any]) -> None:
        if not self.is_render(job["kind"]):
            return
        now = self._clock()
        with self._lock:
            self._started[job["id"]] = self._last[job["id"]] = now
        self._send("lumiere.job.started", self.fields(job["id"], job.get("project_id"), label=job.get("label", ""), progress=0.0))

    def progress(self, job_id: str, kind: str, params: dict[str, Any], value: float) -> None:
        """Throttled: a long render sends one event every ``min_interval_s``, never one per chunk."""
        if not self.is_render(kind):
            return
        now = self._clock()
        with self._lock:
            if now - self._last.get(job_id, 0.0) < self.min_interval_s:
                return
            self._last[job_id] = now
            began = self._started.get(job_id, now)
        data = self.fields(job_id, params.get("project"), progress=max(0.0, min(1.0, float(value))))
        if 0.02 < value < 1 and now > began:
            data["eta_s"] = int((now - began) * (1 - value) / value)
        self._send("lumiere.job.progress", data)

    def forget(self, job_id: str) -> None:
        with self._lock:
            self._started.pop(job_id, None)
            self._last.pop(job_id, None)

    def cancelled(self, job: dict[str, Any]) -> None:
        if self.is_render(job["kind"]):
            self._send("lumiere.job.cancelled", self.fields(job["id"], job.get("project_id"), label=job.get("label", "")))

    # ------------------------------------------------------------------ the end
    def done_data(self, info: dict[str, Any], base: dict[str, Any], job_id: str, project_id: Optional[str]) -> dict[str, Any]:
        """The ``lumiere.render.done`` payload of one output: the render facts plus the canonical job fields and its ``ref``."""
        return {**base, **self.fields(job_id, project_id, progress=1.0, render_id=str(info.get("id") or ""))}

    def failed_data(self, job: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        return {**base, **self.fields(job["id"], job.get("project_id"), label=job.get("label", "")), "error": clip(job.get("error", ""), 300)}


class Notifier:
    """Tells the person, through the hub, that an export finished or failed."""

    def __init__(self, svc: "Services", hub: Any = None, *, background: bool = True):
        self.svc, self._hub, self.background = svc, hub, background
        self._threads = Threads()
        self.sent: list[dict[str, Any]] = []        # what was asked of the hub (newest last, at most 50): the status line and tests read it

    def hub(self) -> Any:
        if self._hub is not None:
            return self._hub
        from .hoard_link import fam_notify

        return fam_notify

    def via(self) -> str:
        value = (self.svc.db.get_setting("notify.via", "auto") or "auto").strip().lower()
        return value if value in VIA else "auto"

    def render_done(self, job: dict[str, Any]) -> None:
        result = job.get("result") or {}
        outputs = result.get("outputs") or [result]
        title = self.svc.job_events.title(job.get("project_id"), job.get("label", ""))
        path = str(result.get("path") or "")
        name = path.replace("\\", "/").rsplit("/", 1)[-1] if path else ""
        body = (f"{len(outputs)} archivos" if len(outputs) > 1 else name) or "Exportación terminada"
        if result.get("ok") is False:
            body += " · revisar: " + clip("; ".join(map(str, (result.get("qc") or {}).get("problems", [])[:2])), 120)
        self._send(job, f"Exportación lista: {title}", body, "normal", dedupe=f"lumiere:render:{job['id']}", group="render")

    def render_failed(self, job: dict[str, Any]) -> None:
        title = self.svc.job_events.title(job.get("project_id"), job.get("label", ""))
        # the same key the hub's own failure notice uses, so the person is not told twice
        self._send(job, f"Exportación fallida: {title}", clip(job.get("error", ""), 200), "high", dedupe="lumiere:render:failed", group="job")

    def _send(self, job: dict[str, Any], title: str, body: str, priority: str, *, dedupe: str, group: str) -> None:
        if self.via() == "off":
            return
        url = self.svc.job_events.url(job.get("project_id"))
        if self.background:
            self._threads.start(self._deliver, (title, body, priority, url, group, dedupe), "editing-notify")
        else:
            self._deliver(title, body, priority, url, group, dedupe)

    def close(self) -> None:
        self._threads.close()

    def _deliver(self, title: str, body: str, priority: str, url: str, group: str, dedupe: str) -> None:
        try:
            hub = self.hub()
            if self.via() == "auto" and not hub.hub_available():
                return
            router = Router(via_getter=lambda: "hub", app_name="lumiere", hub=hub)
            res = router.send(title, body, priority=priority, url=url, group=group, dedupe_key=dedupe)
            self.sent.append({"title": title, "priority": priority, "ok": bool(res.get("ok")), "error": res.get("why", "")})
            del self.sent[:-50]
        except Exception:  # noqa: BLE001
            log.debug("notification through the hub failed", exc_info=True)
