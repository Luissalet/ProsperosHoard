"""Lumiere's side of the Hoard family: the events it sends, the one it accepts, and receiving media from sibling apps.

Sending is ``Services.emit`` (fire-and-forget to the hub's bus; a missing hub never breaks anything). Receiving is a plain
call: a sibling app (or an assistant) calls the tool ``media_receive`` through the hub, or posts the event
``lumiere.media.import`` to ``POST /api/family/events`` with this app's token. Both land in :func:`receive_media`.
The lists below are the contract: the agent instructions, the status line and the tests are written from them.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from . import analyze
from . import ffmpeg as ff
from . import media as media_store
from . import projects as project_store
from .errors import LumiereError, Refused
from .ops import PRESETS
from .util import clip, ms_to_tc

if TYPE_CHECKING:
    from .services import Services

LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")

#: events Lumiere sends: type -> (when, payload fields)
EMITS: dict[str, tuple[str, str]] = {
    "lumiere.render.done": ("an export finished (one event per output file; a multi-format export sends one per canvas; the hub reads it as lumiere.job.done, kind render)",
                            "id, project, preset, path, duration_ms, width, height, bytes, seconds, ok (quality check), problems[], lufs?, variant? (16x9...), job, "
                            "job_id, title (the project), kind (render), progress, url, ref (hoard://lumiere/render/<id>)"),
    "lumiere.render.failed": ("an export did not finish (the hub reads it as lumiere.job.failed, kind render)",
                              "job, project, preset, error, job_id, title, kind (render), url"),
    "lumiere.job.queued": ("an export was queued", "job_id, title (the project), kind (render), progress, url"),
    "lumiere.job.started": ("an export began", "job_id, title, kind (render), progress, url"),
    "lumiere.job.progress": ("an export is running (at most one event every 5 s)", "job_id, title, kind (render), progress (0..1), eta_s?, url"),
    "lumiere.job.cancelled": ("an export was canceled", "job_id, title, kind (render), url"),
    "lumiere.media.transcribed": ("a transcription finished", "id (media), name, words, language, duration_ms, model, job"),
    "lumiere.media.speakers": ("the voices of a transcript were separated", "id (media), speakers, method"),
    "lumiere.media.transcription_failed": ("a transcription failed", "job, id (media), error"),
    "lumiere.subtitles.translated": ("subtitles were translated into a language", "project, language, cues, fallback (cues left untranslated)"),
    "lumiere.media.received": ("media arrived from another app and was imported", "id, name, kind, source, project?, existing"),
    "lumiere.media.imported": ("a file was added to the library", "id, name, kind"),
    "lumiere.media.ready": ("proxy, filmstrip and waveform are ready", "id, name"),
    "lumiere.media.analyzed": ("a scene, beat, loudness, motion or focus analysis finished", "id, kind"),
    "lumiere.project.created": ("a project was created", "id, name"),
    "lumiere.job.failed": ("a background job that is not an export failed (an export's failure is lumiere.render.failed)", "id, kind, error"),
}
#: events Lumiere accepts at POST /api/family/events (Bearer: this app's mcp-token) and as the tool media_receive
ACCEPTS: dict[str, tuple[str, str]] = {
    "lumiere.media.import": ("import a media file from a sibling app and optionally put it on a project",
                             "path | url (file:// or http://127.0.0.1:port/...), name?, project? (add to it), create_project? (name), preset?, "
                             "transcribe?, source? (the sending app)"),
}


def contract_text() -> str:
    """The family events as the instructions an assistant reads."""
    sends = "; ".join(f"{t} ({when})" for t, (when, _) in EMITS.items() if t in ("lumiere.render.done", "lumiere.render.failed",
                                                                                 "lumiere.media.transcribed", "lumiere.subtitles.translated",
                                                                                 "lumiere.media.received"))
    return (f"Family (Hoard apps): Lumiere sends events to the hub when {sends}; the payloads carry the file path, duration and quality check, "
            "so other apps can react. To hand it a file, a sibling app calls media_receive (path, or a file:// or localhost URL; only inside the "
            "folders this app may read) or posts the event lumiere.media.import to /api/family/events; it can add the media to a project or "
            "start a new one.")


def contract() -> dict[str, Any]:
    return {"app": "lumiere", "emits": [{"type": t, "when": w, "data": d} for t, (w, d) in EMITS.items()],
            "accepts": [{"type": t, "when": w, "data": d, "endpoint": "POST /api/family/events", "tool": "media_receive"} for t, (w, d) in ACCEPTS.items()]}


# ---------------------------------------------------------------- receiving media

def _download(svc: "Services", url: str) -> dict[str, Any]:
    """A file served by an app on this machine (loopback only), saved in the uploads folder and imported."""
    parts = urlsplit(url)
    if (parts.hostname or "") not in LOOPBACK:
        raise Refused("Media can only be fetched from an app on this machine (127.0.0.1 / localhost).", code="not_local")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        resp = opener.open(urllib.request.Request(url, headers={"User-Agent": "lumiere-hoard"}), timeout=30)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise LumiereError(f"Could not fetch {clip(url, 120)}: {error}", code="fetch_failed") from error
    with resp:
        disposition = resp.headers.get("Content-Disposition") or ""
        filename = ""
        if "filename=" in disposition:
            filename = disposition.split("filename=", 1)[1].strip().strip('";')
        filename = filename or Path(unquote(parts.path)).name or "media"
        if Path(filename).suffix.lower() not in ff.MEDIA_EXTENSIONS:
            ctype = (resp.headers.get_content_type() or "").lower()
            guess = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/wav": ".wav",
                     "audio/x-wav": ".wav", "audio/mp4": ".m4a", "image/png": ".png", "image/jpeg": ".jpg"}.get(ctype, "")
            filename += guess
        return media_store.save_upload(svc, filename, resp, svc.config.max_upload_bytes)


def _file_url_to_path(url: str) -> str:
    parts = urlsplit(url)
    if parts.netloc not in ("", "localhost"):
        raise Refused("file:// URLs must point at this machine.", code="not_local")
    path = unquote(parts.path)
    if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def receive_media(svc: "Services", *, path: str = "", url: str = "", name: str = "", project: str = "", create_project: bool | str = False,
                  preset: str = "", transcribe: bool = False, source: str = "") -> dict[str, Any]:
    """Import a file a sibling app hands over and optionally add it to a project (``project``) or start one (``create_project``,
    true or a name). The file is read where it is (nothing is copied) unless it comes as a local URL, which is downloaded."""
    if bool(path) == bool(url):
        raise LumiereError("Give exactly one of path or url.", code="bad_request")
    if project and create_project:
        raise LumiereError("Choose one: add to an existing project or create a new one.", code="bad_request")
    if project:
        project_store.doc(svc, project)  # a missing project fails before anything is imported
    scheme = urlsplit(url).scheme.lower() if url else ""
    if url and scheme == "file":
        path, url = _file_url_to_path(url), ""
    elif url and scheme not in ("http", "https"):
        raise LumiereError("url must be file://, http:// or https:// (local).", code="bad_request")
    if url:
        info = _download(svc, url)
        if name:
            info = {**media_store.rename(svc, info["id"], name), "existing": info.get("existing", False)}
    else:
        info = media_store.import_path(svc, path, name=name)
    out: dict[str, Any] = {k: info[k] for k in ("id", "name", "kind", "duration_ms", "width", "height", "has_audio", "path", "existing")}
    out["duration"] = ms_to_tc(info["duration_ms"])
    label = clip(source or "otra app", 60)
    if project:
        res = project_store.edit(svc, project, [{"op": "add_media", "media": info["id"]}], label=f"Medio de {label}", actor="family")
        out["project"] = {"id": project, "rev": res["rev"], "duration_ms": res["duration_ms"], "action": "extended"}
    elif create_project:
        title = create_project if isinstance(create_project, str) and create_project.strip() not in ("", "true", "True") else info["name"]
        chosen = preset or ("reels" if info["height"] > info["width"] > 0 else "youtube")
        if chosen not in PRESETS:
            raise LumiereError(f"Unknown preset {chosen!r}. Known: {', '.join(PRESETS)}.", code="bad_request")
        created = project_store.create(svc, clip(title, 120), preset=chosen, media=[info["id"]])
        out["project"] = {"id": created["id"], "name": created["name"], "canvas": created["canvas"], "action": "created"}
    if transcribe and info["has_audio"] and info["kind"] != "image":
        try:
            out["transcribe"] = analyze.schedule(svc, info["id"], ["transcript"])
        except LumiereError as error:
            out["transcribe_error"] = str(error)
    svc.emit("lumiere.media.received", {"id": info["id"], "name": clip(info["name"], 80), "kind": info["kind"], "source": source or None,
                                        "project": (out.get("project") or {}).get("id"), "existing": bool(info.get("existing"))})
    return out


def handle_event(svc: "Services", type_: str, source: str, data: dict[str, Any]) -> dict[str, Any]:
    """An event posted by a sibling app. Unknown types are acknowledged and ignored (events are hints, never errors)."""
    if type_ not in ACCEPTS:
        return {"handled": False, "type": type_, "accepts": sorted(ACCEPTS)}
    if type_ == "lumiere.media.import":
        res = receive_media(svc, path=str(data.get("path") or ""), url=str(data.get("url") or ""), name=str(data.get("name") or ""),
                            project=str(data.get("project") or ""), create_project=data.get("create_project") or False,
                            preset=str(data.get("preset") or ""), transcribe=bool(data.get("transcribe")), source=source or str(data.get("source") or ""))
        return {"handled": True, "type": type_, "result": res}
    return {"handled": False, "type": type_}  # pragma: no cover
