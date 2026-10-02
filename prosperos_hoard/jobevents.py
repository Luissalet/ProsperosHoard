"""What the family hub hears about Prospero's work, and the notification when a production needs the person.

Renders, songs, clips and whole productions send the hub's canonical job events: `prospero.job.queued|started|progress|done|failed|
cancelled`, each with `{job_id, title, kind (render|song|clip|production), progress (0..1), gpu, eta_s, url, error}`. A production
that stops for the person finishes its job as `done` with `status: "awaiting_review"` and `awaiting` (`take`, `animatic`, `script` or
`review`); a finished render or clip also lists its `asset_ids`. Stills, voices and the like are not announced one by one: they are
part of a production's own progress. Progress events are throttled so a long job never floods the bus.

Notifications go through the family hub only (Prospero has no channel of its own) and only for productions, the one thing that waits
on a person: it paused for a take or the animatic, finished, or failed (failed = high priority). `notify.via` (`family_settings`):
`auto` (the hub when it answers), `hub` (always ask), `off` (nobody is told). The notice links to the production's page.

Events and notices are hints: a failing hub never reaches a job. Pure logic: no FastAPI here.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from . import productions
from .hoard_link import fam_notify
from .family_settings import FamilySettings
from .store import NotFound, Store

log = logging.getLogger("prosperos_hoard.jobevents")

APP = "prospero"
KINDS = {"render_timeline": "render", "compose_song": "song", "animate": "clip", "production": "production"}
TEXT = {
    "es": {
        "take": ("{title}: elige una toma", "La canción tiene varias tomas. Escúchalas y elige una para seguir."),
        "animatic": ("{title}: revisa el animático", "El animático está listo. Revísalo y aprueba para renderizar los clips."),
        "script": ("{title}: revisa el guion", "El guion está listo. Revísalo antes de seguir."),
        "review": ("{title}: espera tu revisión", "La producción se ha parado para que la revises."),
        "done": ("{title}: render terminado", "La producción ha terminado."),
        "failed": ("{title}: ha fallado", "{error}"),
    },
    "en": {
        "take": ("{title}: pick a take", "The song has several takes. Listen and pick one to go on."),
        "animatic": ("{title}: review the animatic", "The animatic is ready. Review it and approve to render the clips."),
        "script": ("{title}: review the script", "The script is ready. Review it before going on."),
        "review": ("{title}: waiting for your review", "The production stopped for you to review it."),
        "done": ("{title}: render finished", "The production is finished."),
        "failed": ("{title}: failed", "{error}"),
    },
}


def kind_of(job_type: str) -> Optional[str]:
    return KINDS.get(job_type)


class JobEvents:
    def __init__(self, store: Store, emit: Callable[[str, dict[str, Any]], Any], *, base_url: Callable[[], str] = lambda: "",
                 notifier: Optional["Notifier"] = None, min_interval_s: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self.store, self._emit, self._base_url, self.notifier = store, emit, base_url, notifier
        self.min_interval_s, self._clock = float(min_interval_s), clock
        self._lock = threading.Lock()
        self._started: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._gpu: dict[str, int] = {}

    # ------------------------------------------------------------------ data
    def url(self, job: dict[str, Any]) -> str:
        base = (self._base_url() or "").rstrip("/")
        if not base:
            return ""
        params, pid = job.get("params") or {}, job.get("project_id")
        if job["type"] == "production" and params.get("slug"):
            return f"{base}/#/productions/{params['slug']}"
        if pid:
            section = {"render_timeline": "timeline", "compose_song": "audio", "animate": "library"}.get(job["type"], "overview")
            return f"{base}/#/p/{pid}/{section}"
        return base

    def title(self, job: dict[str, Any]) -> str:
        params = job.get("params") or {}
        try:
            if job["type"] == "production":
                return str(params.get("name") or params.get("slug") or "Production")[:120]
            if job["type"] == "render_timeline":
                tl = self.store.get_timeline(params["timeline_id"])
                return f"{tl.get('name') or 'Timeline'} · {params.get('quality') or 'preview'}"[:120]
            project = self.store.get_project(job["project_id"])["name"] if job.get("project_id") else ""
            what = "Song" if job["type"] == "compose_song" else "Clip"
            return f"{what} · {project}"[:120] if project else what
        except (NotFound, KeyError, TypeError):
            return {"render_timeline": "Render", "compose_song": "Song", "animate": "Clip"}.get(job["type"], str(job["type"]))

    def data(self, job: dict[str, Any], **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"job_id": job["id"], "title": self.title(job), "kind": kind_of(job["type"]), "url": self.url(job)}
        if job.get("project_id"):
            out["project_id"] = job["project_id"]
        if job["type"] == "production" and (job.get("params") or {}).get("slug"):
            out["ref"] = f"hoard://prospero/production/{job['params']['slug']}"
        gpu = self._gpu.get(job["id"])
        if gpu is not None:
            out["gpu"] = gpu
        out.update({k: v for k, v in extra.items() if v is not None})
        return out

    def _send(self, name: str, data: dict[str, Any]) -> None:
        try:
            self._emit(f"{APP}.job.{name}", data)
        except Exception:  # noqa: BLE001 - events are hints
            log.debug("event %s failed", name, exc_info=True)

    # ------------------------------------------------------------------ lifecycle
    def queued(self, job: dict[str, Any]) -> None:
        if kind_of(job["type"]):
            self._send("queued", self.data(job, progress=0.0))

    def started(self, job: dict[str, Any]) -> None:
        if not kind_of(job["type"]):
            return
        now = self._clock()
        with self._lock:
            self._started[job["id"]] = self._last[job["id"]] = now
        self._send("started", self.data(job, progress=0.0))

    def set_gpu(self, job_id: str, gpu: Optional[int]) -> None:
        if gpu is not None:
            with self._lock:
                self._gpu[job_id] = int(gpu)

    def progress(self, job_id: str, fraction: float, message: Optional[str] = None) -> None:
        """Throttled to one event every ``min_interval_s`` per job; only the kinds above are announced."""
        now = self._clock()
        with self._lock:
            if job_id not in self._started or now - self._last.get(job_id, 0.0) < self.min_interval_s:
                return
            self._last[job_id] = now
            began = self._started[job_id]
        try:
            job = self.store.get_job(job_id)
        except NotFound:
            return
        eta = int((now - began) * (1 - fraction) / fraction) if 0.02 < fraction < 1.0 and now > began else None
        self._send("progress", self.data(job, progress=round(float(fraction), 3), eta_s=eta, message=(message or None) and str(message)[:120]))

    def finished(self, job: dict[str, Any]) -> None:
        """Called once a job reached ``done``, ``failed`` or ``cancelled``."""
        gpu_data = self.data(job)
        with self._lock:
            for table in (self._started, self._last, self._gpu):
                table.pop(job["id"], None)
        if not kind_of(job["type"]):
            return
        state = job["state"]
        if state == "failed":
            self._send("failed", {**gpu_data, "error": str(job.get("message") or "failed")[:300]})
        elif state == "cancelled":
            self._send("cancelled", gpu_data)
        elif state == "done":
            extra: dict[str, Any] = {"progress": 1.0}
            outputs = job.get("outputs") or {}
            if job["type"] == "production":
                status = outputs.get("status")
                if status == "awaiting_review":
                    extra.update(status=status, awaiting=awaiting(self.store, (job.get("params") or {}).get("slug", "")))
                elif status:
                    extra["status"] = status
            else:
                ids = outputs.get("asset_ids") or ([outputs["asset_id"]] if outputs.get("asset_id") else [])
                if ids:
                    extra["asset_ids"] = list(ids)[:8]
            self._send("done", {**gpu_data, **extra})
        if self.notifier is not None and job["type"] == "production":
            self.notifier.production(job, gpu_data["title"], gpu_data["url"])


def awaiting(store: Store, slug: str) -> str:
    """Why a paused production waits: ``take`` (pick a song take), ``animatic``, ``script`` or ``review``."""
    try:
        state = productions.load_state(store.data_dir, slug)
    except Exception:  # noqa: BLE001
        return "review"
    if ((state.get("partial") or {}).get("song") or {}).get("awaiting_take"):
        return "take"
    stage = state.get("stage")
    return stage if stage in ("animatic", "script") else "review"


class Notifier:
    """Asks the family hub to tell the person that a production needs them. The `notify.via` switch is the shared `fam_notify.Router`
    (`auto`: the hub when it answers; `hub`: always ask it); `off` is checked here first. Prospero has no channel of its own."""

    def __init__(self, store: Store, settings: FamilySettings, hub: Any = None, *, background: bool = True):
        self.store, self.settings, self.background = store, settings, background
        self.router = fam_notify.Router(lambda: settings.get("notify.via"), None, "Prospero's Hoard", hub=hub)
        self.sent: list[dict[str, Any]] = []        # what was asked of the hub, newest last (status and tests read it)

    def production(self, job: dict[str, Any], title: str, url: str) -> None:
        via = self.settings.get("notify.via")
        if via == "off" or job["state"] not in ("done", "failed"):
            return
        slug = (job.get("params") or {}).get("slug", "")
        if job["state"] == "failed":
            kind, priority = "failed", "high"
        else:
            status = (job.get("outputs") or {}).get("status")
            kind = awaiting(self.store, slug) if status == "awaiting_review" else ("done" if status in (None, "done") else "")
            priority = "normal"
        if not kind:
            return
        texts = TEXT.get(self.settings.get("notify.language"), TEXT["es"])[kind]
        error = str(job.get("message") or "")[:200]
        notice = (texts[0].format(title=title), texts[1].format(error=error))
        # a failure uses the key of the hub's own failure notice (<app>:<kind>:failed), so the person is not told twice
        dedupe = "prospero:production:failed" if kind == "failed" else f"prospero:production:{slug}:{kind}:{job['id']}"
        args = (*notice, priority, url, "production", dedupe)
        if self.background:
            threading.Thread(target=self._deliver, args=args, daemon=True, name="prospero-notify").start()
        else:
            self._deliver(*args)

    def _deliver(self, title: str, body: str, priority: str, url: str, group: str, dedupe: str) -> None:
        try:
            res = self.router.send(title, body, priority=priority, url=url, group=group, dedupe_key=dedupe)
            self.sent.append({"title": title, "priority": priority, "ok": bool(res.get("ok")), "error": "" if res.get("ok") else res.get("why", "")})
            del self.sent[:-50]
        except Exception:  # noqa: BLE001
            log.debug("notification through the hub failed", exc_info=True)
