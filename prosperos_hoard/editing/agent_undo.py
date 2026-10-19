"""Accountable agents: what each write tool captures before it runs, what it reports after it, and how to take it back.

Three hooks per tool (see ``hoard_link.agentkit.Tool``):

* ``capture(svc, args)`` runs just before the write and returns what is needed to go back. For a project that is a pointer, not a copy:
  the history step the project was at (``seq``) and a fingerprint of that timeline. The history already keeps the timeline of every step,
  so the journal stays small however big the timeline is.
* ``track(args, result, ctx=)`` runs just after and says which objects the write touched and a fingerprint (``etag``) of what they look
  like now. It reads the database, not the result: the result an agent gets is cut to ~20 KB.
* ``undo(svc, record, dry_run=False)`` puts ``before`` back. It first compares the objects with ``etag``; when they differ (the person
  edited them in the web interface, which the journal never sees, or another session did) it raises a ``conflict`` and nothing changes.

A project is taken back by saving the earlier timeline as a **new history step** ("Deshacer cambios del agente"): nothing is lost and
the person can still step back and forth with the editor's own undo and redo. The fingerprint of a timeline is a hash of its normalised
content, never the revision counter, so undoing the newest write of a session leaves the state that the previous write left and the
older write's fingerprint matches again.

Objects are slash-separated paths, and two writes conflict when the paths are equal or one contains the other::

    project:P                  the timeline of a project (and the project itself when it is created)
    project:P/translations/L   the translated subtitles of one language
    media:M                    a library entry (when it is imported)
    media:M/tags               its labels
    media:M/transcript         the words heard (and who said them)
    plan:L                     an edit plan
    settings:KEY               one setting

Undoable: project_create, project_from_timeline, short_from_range, template_save, timeline_edit, timeline_history, edit_command, text_cut,
multicam_create, multicam_switch, multicam_auto, music_pick, broll_suggest, clip_freeze, timeline_nest, plan_create, plan_apply,
media_import, media_shared, media_receive, media_tag, transcript_fix, speakers_edit, subtitles_translate (fix and delete), settings.

Not undoable (no handler, reported as such): media_delete, project_delete, media_analyze, clip_stabilize, render_start, frame_snapshot,
project_contact_sheet, subtitles_export, project_export_otio, job_cancel and the creative_* tools. A few undoable tools have a part that
cannot be taken back (a translation or a voice separation produced by a background job, a refresh of a shared original): that single call
is reported as not undoable with the reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import media as media_store
from . import plan as plan_mod
from . import projects as project_store
from . import subtitles as subtitles_mod
from .errors import NotFound
from .hoard_link.agentkit import AppError
from .timeline import load
from .util import clip, dumps

UNDO_LABEL = "Deshacer cambios del agente"
UNDO_ACTOR = "agent-undo"


# ------------------------------------------------------------------------------------------------ small helpers

def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _conflict(message: str) -> AppError:
    return AppError("conflict", message, hint="Look at the project as it is now before undoing anything else.")


def _id_of(record: dict[str, Any], key: str) -> str:
    for item in record.get("ids") or []:
        name, _, value = str(item).partition("=")
        if name == key:
            return value
    return ""


def _before(record: dict[str, Any]) -> dict[str, Any]:
    before = record.get("before")
    return before if isinstance(before, dict) else {}


def _doc_digest(text: str) -> str:
    """Fingerprint of a stored timeline: its normalised content, so a restored step matches the step it was copied from."""
    return _digest(load(json.loads(text)).dump())


def _states(svc: Any, project_ids: list[str]) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    for pid in sorted(set(project_ids)):
        row = svc.db.one("SELECT doc FROM projects WHERE id = ?", (pid,))
        out[pid] = _doc_digest(row["doc"]) if row else None
    return out


def _snapshot(svc: Any, project_id: str) -> dict[str, Any]:
    """Where the project is in its history, verified: the step at ``head`` must hold the timeline the project shows."""
    row = svc.db.one("SELECT doc, head FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise NotFound(f"No project {project_id}.")
    digest = _doc_digest(row["doc"])
    step = svc.db.one("SELECT doc FROM history WHERE project_id = ? AND seq = ?", (project_id, row["head"]))
    if step is None or _doc_digest(step["doc"]) != digest:
        raise AppError("failed", "The history of this project does not match its timeline, so this change cannot be taken back.")
    return {"project": project_id, "seq": row["head"], "digest": digest}


def _restore_project(svc: Any, snap: dict[str, Any]) -> int:
    step = svc.db.one("SELECT doc FROM history WHERE project_id = ? AND seq = ?", (snap["project"], snap["seq"]))
    if step is None or _doc_digest(step["doc"]) != snap["digest"]:
        raise AppError("history_trimmed", "The earlier version of the project is no longer in its history (it keeps the last "
                       f"{project_store.HISTORY_LIMIT} steps), so it cannot be restored.")
    return project_store.save(svc, snap["project"], load(json.loads(step["doc"])), UNDO_LABEL, UNDO_ACTOR)


def _name_of(svc: Any, project_id: str) -> str:
    row = svc.db.one("SELECT name FROM projects WHERE id = ?", (project_id,))
    return row["name"] if row else ""


# ------------------------------------------------------------------------------------------------ a timeline edited in place

def _capture_project(writes: Callable[[Any], bool]) -> Callable[[Any, Any], dict]:
    def capture(svc: Any, args: Any) -> dict:
        if not writes(args):
            return {"noop": True}
        return _snapshot(svc, args.project)
    return capture


def _track_project(writes: Callable[[Any], bool]) -> Callable[..., dict]:
    def track(args: Any, result: dict, ctx: Any = None) -> dict:
        if not writes(args):
            return {"objects": [], "etag": ""}
        return {"objects": [f"project:{args.project}"], "ids": [f"project={args.project}"], "etag": _digest(_states(ctx, [args.project]))}
    return track


def undo_project(svc: Any, record: dict, dry_run: bool = False) -> dict:
    before = _before(record)
    if before.get("noop") or not record.get("etag"):
        return {"unchanged": True}
    pid = before["project"]
    if _digest({pid: before["digest"]}) == record["etag"]:
        return {"unchanged": True}                        # the call left the timeline as it was (a preview, a replayed request, a missing analysis)
    now = _states(svc, [pid])[pid]
    if now is None:
        raise _conflict("The project no longer exists.")
    if _digest({pid: now}) != record["etag"]:
        raise _conflict("The timeline changed after this write (in the editor or by another session).")
    if dry_run:
        return {"would_restore_project": pid, "name": _name_of(svc, pid)}
    return {"restored_project": pid, "rev": _restore_project(svc, before)}


_ALWAYS: Callable[[Any], bool] = lambda a: True  # noqa: E731
_WRITES = {
    "timeline_edit": _ALWAYS,
    "edit_command": lambda a: not a.preview,
    "text_cut": _ALWAYS,
    "multicam_create": _ALWAYS,
    "multicam_switch": _ALWAYS,
    "multicam_auto": _ALWAYS,
    "music_pick": lambda a: bool(a.add),
    "broll_suggest": lambda a: a.place != "none",
    "clip_freeze": _ALWAYS,
    "timeline_history": lambda a: a.action in ("undo", "redo", "restore"),
}


# ------------------------------------------------------------------------------------------------ projects that are created

def _created_project_id(result: dict) -> str:
    return str(result.get("id") or result.get("project_id") or result.get("project") or "")


def track_project_created(args: Any, result: dict, ctx: Any = None) -> dict:
    pid = _created_project_id(result)
    if not pid:
        return {"objects": [], "etag": ""}
    return {"objects": [f"project:{pid}"], "ids": [f"project={pid}"], "etag": _digest(_states(ctx, [pid]))}


def _check_deletable_project(svc: Any, pid: str, expect: Optional[str] = None) -> None:
    """A project this session created may go while nobody has taken it up: unchanged, not nested elsewhere, never exported."""
    if expect is not None:
        now = _states(svc, [pid])[pid]
        if now is None or _digest({pid: now}) != expect:
            raise _conflict("The project was edited after it was created.")
    users = [u["id"] for u in project_store.used_by(svc, pid)]
    if users:
        raise _conflict(f"The project is used as a sequence in {', '.join(users)}, so it is not deleted.")
    if svc.db.one("SELECT 1 FROM renders WHERE project_id = ?", (pid,)) is not None:
        raise _conflict("The project has been exported, so it is not deleted.")


def undo_project_created(svc: Any, record: dict, dry_run: bool = False) -> dict:
    pid = _id_of(record, "project")
    if not pid:
        return {"unchanged": True}
    if svc.db.one("SELECT 1 FROM projects WHERE id = ?", (pid,)) is None:
        return {"already_gone": True}
    _check_deletable_project(svc, pid, record.get("etag"))
    name = _name_of(svc, pid)
    if dry_run:
        return {"would_delete_project": pid, "name": name}
    for job in svc.jobs.list(state="active", project_id=pid):
        svc.jobs.cancel(job["id"])
    project_store.delete(svc, pid)
    return {"deleted_project": pid, "name": name}


# ------------------------------------------------------------------------------------------------ nesting

def capture_nest(svc: Any, args: Any) -> dict:
    return _snapshot(svc, args.project) if args.action in ("nest", "unnest", "add") else {"noop": True}


def track_nest(args: Any, result: dict, ctx: Any = None) -> dict:
    if args.action not in ("nest", "unnest", "add"):
        return {"objects": [], "etag": ""}
    created = str(result.get("sequence") or "") if args.action == "nest" else ""
    ids = [args.project, *([created] if created else [])]
    return {"objects": [f"project:{i}" for i in ids], "ids": [f"project={args.project}", *([f"sequence={created}"] if created else [])],
            "etag": _digest(_states(ctx, ids))}


def undo_nest(svc: Any, record: dict, dry_run: bool = False) -> dict:
    before = _before(record)
    if before.get("noop") or not record.get("etag"):
        return {"unchanged": True}
    pid, created = before["project"], _id_of(record, "sequence")
    now = _states(svc, [pid, *([created] if created else [])])
    if now[pid] is None:
        raise _conflict("The project no longer exists.")
    if _digest(now) != record["etag"]:
        raise _conflict("The timeline (or the sequence made from it) changed after this write.")
    if created:
        users = [u["id"] for u in project_store.used_by(svc, created) if u["id"] != pid]
        if users:
            raise _conflict(f"The sequence is used in {', '.join(users)}, so it is not deleted.")
    if dry_run:
        return {"would_restore_project": pid, **({"would_delete_sequence": created} if created else {})}
    out: dict[str, Any] = {"restored_project": pid, "rev": _restore_project(svc, before)}
    if created:
        project_store.delete(svc, created)
        out["deleted_sequence"] = created
    return out


# ------------------------------------------------------------------------------------------------ edit plans

def track_plan_create(args: Any, result: dict, ctx: Any = None) -> dict:
    return {"objects": [f"plan:{result['id']}"], "ids": [f"plan={result['id']}"], "etag": "draft"}


def undo_plan_create(svc: Any, record: dict, dry_run: bool = False) -> dict:
    plan_id = _id_of(record, "plan")
    row = svc.db.one("SELECT state FROM plans WHERE id = ?", (plan_id,))
    if row is None:
        return {"already_gone": True}
    if row["state"] == "applied":
        raise _conflict("The plan was applied and its effects have not been taken back.")
    if dry_run:
        return {"would_delete_plan": plan_id}
    svc.db.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    return {"deleted_plan": plan_id}


def capture_plan_apply(svc: Any, args: Any) -> dict:
    plan = plan_mod.get(svc, args.plan)
    return {"plan": args.plan, "project": plan["project"], "steps": plan["steps"], "state": plan["state"]}


def track_plan_apply(args: Any, result: dict, ctx: Any = None) -> dict:
    plan = plan_mod.get(ctx, args.plan)
    ids = [f"plan={args.plan}", f"project={plan['project']}"] + ([f"job={result['job']}"] if result.get("job") else [])
    return {"objects": [f"plan:{args.plan}", f"project:{plan['project']}"], "ids": ids, "etag": "job"}


def _plan_step(svc: Any, plan: dict) -> Optional[dict]:
    """The history step the plan's job saved (a plan with only exports saves none)."""
    label = f"Plan: {clip(plan['instruction'], 60)}"
    return svc.db.one("SELECT seq, doc FROM history WHERE project_id = ? AND actor = 'plan' AND label = ? AND ts <= ? ORDER BY seq DESC LIMIT 1",
                      (plan["project"], label, (plan["applied_ts"] or 0) + 1.0))


def undo_plan_apply(svc: Any, record: dict, dry_run: bool = False) -> dict:
    before = _before(record)
    plan_id = before["plan"]
    plan = plan_mod.get(svc, plan_id)
    job_id = _id_of(record, "job")
    if plan["state"] != "applied":
        job = svc.jobs.get(job_id) if job_id else None
        if job and job["state"] in ("queued", "running"):
            raise _conflict("The plan is still being applied by a background job; wait for it (job_status) or cancel it first.")
        return {"unchanged": True, "note": "the plan was never applied"}
    pid = plan["project"]
    step = _plan_step(svc, plan)
    earlier = None
    if step is not None:
        now = _states(svc, [pid])[pid]
        if now is None:
            raise _conflict("The project no longer exists.")
        if now != _doc_digest(step["doc"]):
            raise _conflict("The timeline changed after the plan was applied.")
        earlier = svc.db.one("SELECT doc FROM history WHERE project_id = ? AND seq = ?", (pid, step["seq"] - 1))
        if earlier is None:
            raise AppError("history_trimmed", "The version of the project before the plan is no longer in its history.")
    if dry_run:
        return {**({"would_restore_project": pid} if step is not None else {}), "would_reopen_plan": plan_id}
    out: dict[str, Any] = {"note": "exports the plan queued are not cancelled"}
    if earlier is not None:
        out["restored_project"] = pid
        out["rev"] = project_store.save(svc, pid, load(json.loads(earlier["doc"])), UNDO_LABEL, UNDO_ACTOR)
    svc.db.execute("UPDATE plans SET state = 'draft', applied_ts = NULL, steps = ? WHERE id = ?", (dumps(before.get("steps") or plan["steps"]), plan_id))
    return {**out, "reopened_plan": plan_id}


# ------------------------------------------------------------------------------------------------ the media library

_MEDIA_FIELDS = ("name", "path", "fingerprint", "tags")


def _media_state(svc: Any, media_id: str) -> Optional[str]:
    row = svc.db.one("SELECT name, path, fingerprint, tags FROM media WHERE id = ?", (media_id,))
    return _digest([row[k] for k in _MEDIA_FIELDS]) if row else None


def _folder_of(raw: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))).resolve())


def _created_media(svc: Any, args: Any, result: dict) -> list[str]:
    items = [result] if result.get("id") else list(result.get("imported") or [])
    created = [str(m["id"]) for m in items if m.get("id") and not m.get("existing")]
    if result.get("truncated") and getattr(args, "folder", ""):
        # the answer an agent gets is cut to ~20 KB: the rest of a big folder is found by where the files are and when they came in
        listed = [i for i in created]
        if listed:
            first = svc.db.one("SELECT MIN(created_ts) AS t FROM media WHERE id IN (%s)" % ",".join("?" * len(listed)), tuple(listed))["t"]
            folder = _folder_of(args.folder)
            for row in svc.db.query("SELECT id, path FROM media WHERE origin = 'import' AND created_ts >= ?", (first,)):
                if row["id"] not in created and (row["path"] == folder or row["path"].startswith(folder.rstrip("/\\") + os.sep)):
                    created.append(row["id"])
    return created


def _track_media_created(args: Any, result: dict, ctx: Any = None) -> dict:
    if result.get("refreshed"):
        return {"objects": [f"media:{result['id']}"], "ids": [f"media_id={result['id']}"], "etag": "refreshed"}
    created = _created_media(ctx, args, result)
    if not created:
        return {"objects": [], "etag": ""}
    return {"objects": [f"media:{i}" for i in created], "ids": [f"media_id={i}" for i in created],
            "etag": dumps({i: _media_state(ctx, i) for i in created})}


def _media_removal_plan(svc: Any, states: dict[str, Optional[str]], ignore_projects: set[str]) -> list[str]:
    """The media of ``states`` that exist, after checking that none was touched or is used by a timeline; raises a conflict otherwise."""
    present: list[str] = []
    for media_id, digest in states.items():
        now = _media_state(svc, media_id)
        if now is None:
            continue
        if now != digest:
            raise _conflict(f"The library entry {media_id} was renamed, labelled or relinked after the import.")
        present.append(media_id)
    for row in svc.db.query("SELECT id, doc FROM projects"):
        if row["id"] in ignore_projects:
            continue
        used = [m for m in present if f'"{m}"' in row["doc"]]
        if used:
            raise _conflict(f"{', '.join(used)} is used in project {row['id']}, so it is not removed.")
    return present


def _remove_media(svc: Any, media_ids: list[str]) -> None:
    for media_id in media_ids:
        for job in svc.jobs.list(state="active", media_id=media_id):
            svc.jobs.cancel(job["id"])
        media_store.delete(svc, media_id)


def undo_media_created(svc: Any, record: dict, dry_run: bool = False) -> dict:
    etag = str(record.get("etag") or "")
    if not etag:
        return {"unchanged": True}
    if etag == "refreshed":
        raise AppError("not_undoable", "A refresh of a shared original rebuilt the caches and dropped the old analyses; that is not taken back.")
    present = _media_removal_plan(svc, json.loads(etag), set())
    if not present:
        return {"already_gone": True}
    if dry_run:
        return {"would_remove_from_library": len(present)}
    _remove_media(svc, present)
    return {"removed_from_library": len(present), "note": "the original files were not touched"}


def capture_media_receive(svc: Any, args: Any) -> dict:
    return _snapshot(svc, args.project) if args.project else {"noop": True}


def track_media_receive(args: Any, result: dict, ctx: Any = None) -> dict:
    created_media = [] if result.get("existing") else [str(result["id"])]
    project = result.get("project") or {}
    project_ids = [str(project["id"])] if project.get("id") else []
    if not created_media and not project_ids:
        return {"objects": [], "etag": ""}
    action = project.get("action") or ""
    ids = [*[f"media_id={i}" for i in created_media], *[f"project={i}" for i in project_ids], *([f"action={action}"] if action else [])]
    return {"objects": [*[f"media:{i}" for i in created_media], *[f"project:{i}" for i in project_ids]], "ids": ids,
            "etag": dumps({"media": {i: _media_state(ctx, i) for i in created_media}, "projects": _states(ctx, project_ids)})}


def undo_media_receive(svc: Any, record: dict, dry_run: bool = False) -> dict:
    etag = str(record.get("etag") or "")
    if not etag:
        return {"unchanged": True}
    state = json.loads(etag)
    before = _before(record)
    pid, action = _id_of(record, "project"), _id_of(record, "action")
    restore = delete = ""
    if pid:
        now = _states(svc, [pid])
        if now[pid] is None:
            raise _conflict("The project no longer exists.")
        if state["projects"] != now:
            raise _conflict("The project changed after the file was received.")
        if action == "created":
            _check_deletable_project(svc, pid)
            delete = pid
        elif before.get("digest") and before["digest"] != now[pid]:
            restore = pid
    present = _media_removal_plan(svc, state["media"], {pid} if pid else set())
    if dry_run:
        return {**({"would_restore_project": restore} if restore else {}), **({"would_delete_project": delete} if delete else {}),
                "would_remove_from_library": len(present)}
    out: dict[str, Any] = {}
    if delete:
        for job in svc.jobs.list(state="active", project_id=delete):
            svc.jobs.cancel(job["id"])
        project_store.delete(svc, delete)
        out["deleted_project"] = delete
    elif restore:
        out["restored_project"], out["rev"] = restore, _restore_project(svc, before)
    _remove_media(svc, present)
    return {**out, "removed_from_library": len(present)}


def capture_media_tag(svc: Any, args: Any) -> dict:
    return {"media": args.media, "tags": media_store.get(svc, args.media)["tags"]}


def track_media_tag(args: Any, result: dict, ctx: Any = None) -> dict:
    return {"objects": [f"media:{args.media}/tags"], "ids": [f"media_id={args.media}"], "etag": _digest(media_store.get(ctx, args.media)["tags"])}


def undo_media_tag(svc: Any, record: dict, dry_run: bool = False) -> dict:
    before = _before(record)
    try:
        now = media_store.get(svc, before["media"])["tags"]
    except NotFound:
        raise _conflict("The library entry no longer exists.") from None
    if _digest(now) != record.get("etag"):
        raise _conflict("The labels changed after this write.")
    if now == before["tags"]:
        return {"unchanged": True}
    if dry_run:
        return {"would_restore_tags": before["tags"]}
    media_store.set_tags(svc, before["media"], before["tags"])
    return {"restored_tags": before["tags"]}


# ------------------------------------------------------------------------------------------------ transcript and speakers

_ANALYSES = ("transcript", "speakers")


def _analysis_rows(svc: Any, media_id: str) -> dict[str, Optional[dict[str, str]]]:
    out: dict[str, Optional[dict[str, str]]] = {}
    for kind in _ANALYSES:
        row = svc.db.one("SELECT params, result FROM analysis WHERE media_id = ? AND kind = ?", (media_id, kind))
        out[kind] = {"params": row["params"], "result": row["result"]} if row else None
    return out


def _analysis_digest(rows: dict[str, Optional[dict[str, str]]]) -> str:
    return _digest({k: (v["result"] if v else None) for k, v in rows.items()})


def _is_voice_job(args: Any) -> bool:
    return getattr(args, "action", "") == "diarize"


def capture_analysis(svc: Any, args: Any) -> dict:
    if _is_voice_job(args):
        return {"noop": "job"}
    return {"media": args.media, "rows": _analysis_rows(svc, args.media)}


def track_analysis(args: Any, result: dict, ctx: Any = None) -> dict:
    if _is_voice_job(args):
        return {"objects": [f"media:{args.media}/transcript"], "ids": [f"media_id={args.media}"], "etag": "job"}
    return {"objects": [f"media:{args.media}/transcript"], "ids": [f"media_id={args.media}"], "etag": _analysis_digest(_analysis_rows(ctx, args.media))}


def undo_analysis(svc: Any, record: dict, dry_run: bool = False) -> dict:
    before = _before(record)
    if before.get("noop") == "job":
        raise AppError("not_undoable", "Voices are separated by a background job and that result is not taken back; "
                       "speakers_edit with action=clear drops the separation.")
    media_id = before["media"]
    if svc.db.one("SELECT 1 FROM media WHERE id = ?", (media_id,)) is None:
        raise _conflict("The library entry no longer exists.")
    if _analysis_digest(_analysis_rows(svc, media_id)) != record.get("etag"):
        raise _conflict("The transcript or the speakers changed after this write (a new analysis, a correction or another session).")
    if _analysis_digest(before["rows"]) == record.get("etag"):
        return {"unchanged": True}
    if dry_run:
        return {"would_restore": "transcript and speakers of " + media_id}
    with svc.db.transaction() as conn:
        for kind, row in before["rows"].items():
            if row is None:
                conn.execute("DELETE FROM analysis WHERE media_id = ? AND kind = ?", (media_id, kind))
            else:
                conn.execute("INSERT INTO analysis(media_id, kind, params, result, updated_ts) VALUES (?, ?, ?, ?, ?) "
                             "ON CONFLICT(media_id, kind) DO UPDATE SET params = excluded.params, result = excluded.result, updated_ts = excluded.updated_ts",
                             (media_id, kind, row["params"], row["result"], time.time()))
    return {"restored": "transcript and speakers of " + media_id}


# ------------------------------------------------------------------------------------------------ translated subtitles

def _translation_row(svc: Any, project_id: str, code: str) -> Optional[dict[str, Any]]:
    row = svc.db.one("SELECT signature, data, updated_ts FROM translations WHERE project_id = ? AND language = ?", (project_id, code))
    return {"signature": row["signature"], "data": row["data"], "updated_ts": row["updated_ts"]} if row else None


def _translation_digest(row: Optional[dict[str, Any]]) -> str:
    return _digest([row["signature"], row["data"]] if row else None)


def _subtitle_code(args: Any) -> str:
    return subtitles_mod.resolve_language(args.language)[0] if args.language else ""


def capture_subtitles(svc: Any, args: Any) -> dict:
    if args.action in ("fix", "delete") and args.language:
        code = _subtitle_code(args)
        return {"project": args.project, "language": code, "row": _translation_row(svc, args.project, code)}
    return {"noop": args.action}


def track_subtitles(args: Any, result: dict, ctx: Any = None) -> dict:
    if args.action in ("status", "show") or not args.language:
        return {"objects": [], "etag": ""}
    code = _subtitle_code(args)
    path = f"project:{args.project}/translations/{code}"
    if args.action == "translate":
        return {"objects": [path], "ids": [f"project={args.project}"], "etag": "job"}
    return {"objects": [path], "ids": [f"project={args.project}"], "etag": _translation_digest(_translation_row(ctx, args.project, code))}


def undo_subtitles(svc: Any, record: dict, dry_run: bool = False) -> dict:
    before = _before(record)
    if before.get("noop") == "translate":
        raise AppError("not_undoable", "A translation is produced by a background job and is not taken back; "
                       "subtitles_translate with action=delete removes a language.")
    if before.get("noop") or not record.get("etag"):
        return {"unchanged": True}
    pid, code = before["project"], before["language"]
    if svc.db.one("SELECT 1 FROM projects WHERE id = ?", (pid,)) is None:
        raise _conflict("The project no longer exists.")
    now = _translation_row(svc, pid, code)
    if _translation_digest(now) != record["etag"]:
        raise _conflict("The translation changed after this write.")
    if _translation_digest(before["row"]) == record["etag"]:
        return {"unchanged": True}
    if dry_run:
        return {"would_restore_translation": code}
    row = before["row"]
    if row is None:
        svc.db.execute("DELETE FROM translations WHERE project_id = ? AND language = ?", (pid, code))
    else:
        svc.db.execute("INSERT INTO translations(project_id, language, signature, data, updated_ts) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT(project_id, language) DO UPDATE SET signature = excluded.signature, data = excluded.data, "
                       "updated_ts = excluded.updated_ts", (pid, code, row["signature"], row["data"], row["updated_ts"]))
    return {"restored_translation": code}


# ------------------------------------------------------------------------------------------------ settings

def capture_settings(svc: Any, args: Any) -> dict:
    if not args.patch:
        return {"noop": True}
    current = svc.get_settings()
    return {"values": {k: current.get(k) for k in args.patch}}


def track_settings(args: Any, result: dict, ctx: Any = None) -> dict:
    if not args.patch:
        return {"objects": [], "etag": ""}
    current = ctx.get_settings()
    return {"objects": [f"settings:{k}" for k in args.patch], "ids": [], "etag": _digest({k: current.get(k) for k in args.patch})}


def undo_settings(svc: Any, record: dict, dry_run: bool = False) -> dict:
    before = _before(record)
    if before.get("noop") or not record.get("etag"):
        return {"unchanged": True}
    values = before["values"]
    current = svc.get_settings()
    if _digest({k: current.get(k) for k in values}) != record["etag"]:
        raise _conflict("A setting changed after this write.")
    if _digest(values) == record["etag"]:
        return {"unchanged": True}
    if dry_run:
        return {"would_restore_settings": sorted(values)}
    svc.update_settings({k: v for k, v in values.items()})
    return {"restored_settings": sorted(values)}


# ------------------------------------------------------------------------------------------------ the table

HOOKS: dict[str, dict[str, Callable[..., Any]]] = {
    **{name: {"capture": _capture_project(writes), "track": _track_project(writes), "undo": undo_project} for name, writes in _WRITES.items()},
    "timeline_nest": {"capture": capture_nest, "track": track_nest, "undo": undo_nest},
    "project_create": {"track": track_project_created, "undo": undo_project_created},
    "project_from_timeline": {"track": track_project_created, "undo": undo_project_created},
    "short_from_range": {"track": track_project_created, "undo": undo_project_created},
    "template_save": {"track": track_project_created, "undo": undo_project_created},
    "plan_create": {"track": track_plan_create, "undo": undo_plan_create},
    "plan_apply": {"capture": capture_plan_apply, "track": track_plan_apply, "undo": undo_plan_apply},
    "media_import": {"track": _track_media_created, "undo": undo_media_created},
    "media_shared": {"track": _track_media_created, "undo": undo_media_created},
    "media_receive": {"capture": capture_media_receive, "track": track_media_receive, "undo": undo_media_receive},
    "media_tag": {"capture": capture_media_tag, "track": track_media_tag, "undo": undo_media_tag},
    "transcript_fix": {"capture": capture_analysis, "track": track_analysis, "undo": undo_analysis},
    "speakers_edit": {"capture": capture_analysis, "track": track_analysis, "undo": undo_analysis},
    "subtitles_translate": {"capture": capture_subtitles, "track": track_subtitles, "undo": undo_subtitles},
    "settings": {"capture": capture_settings, "track": track_settings, "undo": undo_settings},
}

#: writes that create or edit drafts and never delete, export or publish: what a token with the ``drafts`` profile may call
DRAFT_SAFE = {
    "media_import", "media_shared", "media_receive", "media_tag", "media_analyze", "transcript_fix", "speakers_edit",
    "project_create", "project_from_timeline", "short_from_range", "template_save",
    "timeline_edit", "timeline_history", "timeline_nest", "edit_command", "text_cut", "plan_create",
    "multicam_create", "multicam_switch", "multicam_auto", "music_pick", "broll_suggest", "clip_freeze", "clip_stabilize",
    "frame_snapshot", "project_contact_sheet",
}
