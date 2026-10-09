"""Accountable agents: what each write tool captures before it runs, what it reports after it, and how to take it back.

Three hooks per tool (see ``hoard_link.agentkit.Tool``; Prospero's tools take their arguments flat, query parameters and body
fields in one dict, so the hooks read a plain ``dict``):

* ``capture(ctx, args)`` runs just before the write and returns the state that would be lost. It is kept in the agent journal
  as ``before`` (a production is too big for that, so its snapshot goes to ``data/agent_undo/productions/`` and ``before`` only
  names the file). Returning ``None`` says "this call cannot be taken back" (emptying the trash).
* ``track(args, result, ctx=)`` runs just after and says which objects the write touched and a fingerprint (``etag``) of what the
  object looks like now. It reads the database, not the result: the result an agent gets is cut to ~20 KB.
* ``undo(ctx, record, dry_run=False)`` puts ``before`` back. It first compares the object with ``etag``; when it differs (somebody
  edited it in the web interface, which the journal never sees, or a run changed it) it raises a ``conflict`` and nothing changes.

``ctx`` is ``family_api.Context`` (``ctx.store`` is the ``Store``). Objects are slash-separated paths, and two writes conflict
when the paths are equal or one contains the other::

    project:P                  the project itself (create, delete, restore)
    project:P/asset:A          one asset (delete, restore, import)
    project:P/cast:C           a character, place or object of the cast
    project:P/group:G          a group of the cast
    style:S                    a style card
    production:SLUG            a production's whole state (spec, settings, progress)

What is reversible is whatever Prospero already keeps: assets and projects have a trash, cast entries are soft-deleted, rows
are snapshotted (style cards, cast entries) and a production's state file is copied before an edit. Nothing here deletes data
for good: undoing a creation moves it to the trash (a project, an asset) or to ``data/trash/agent_undo/`` (a production).

Not undoable (no handler, reported as ``no_handler``): everything that queues a render or a job (images, clips, songs, voices,
dubbing, training, QA, reframing, retakes, downloads, stock search, services), because the result is a file a GPU made;
``studio_cancel_job``; the timeline, space, design, photocard, character-kit and voice-library tools; the family tools that
talk to other apps (``production_export_lumiere``, ``cast_import_character``, ``production_from_storyboard``, ``voice_tts``).
Emptying the trash (``studio_trash`` with ``action="empty"``) is recorded as not undoable on purpose.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from . import graphic_shots
from . import productions as prod
from .hoard_link.agentkit import AppError
from .ids import new_id
from .store import AssetInUse, NotFound, ProjectBusy
from .util import write_text_atomic

KEEP_SNAPSHOTS = 20          # production snapshots kept per production (older ones are deleted when a new one is taken)


# ------------------------------------------------------------------------------------------------ helpers

def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _conflict(message: str, hint: str = "Look at the object as it is now before undoing anything else.") -> AppError:
    return AppError("conflict", message, hint=hint)


def _id(record: dict[str, Any], key: str) -> str:
    for item in record.get("ids") or []:
        name, _, value = str(item).partition("=")
        if name == key:
            return value
    return ""


def _before(record: dict[str, Any]) -> dict[str, Any]:
    before = record.get("before")
    return before if isinstance(before, dict) else {}


def _s(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else ""


def _row(store: Any, table: str, key: str) -> Optional[dict[str, Any]]:
    """The raw database row (every column, JSON columns still text), or None. ``table`` is always a literal of this module."""
    row = store.conn.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()   # noqa: S608
    return dict(row) if row is not None else None


def _restore_row(store: Any, table: str, row: dict[str, Any]) -> None:
    """Put a snapshotted row back: UPDATE when the row exists, INSERT when it is gone. Never REPLACE: a project row replaced
    would cascade into its assets."""
    conn = store.conn
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (row["id"],)).fetchone()          # noqa: S608
    cols = [c for c in row if c != "id"]
    if exists:
        conn.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",        # noqa: S608
                     [row[c] for c in cols] + [row["id"]])
    else:
        names = ["id", *cols]
        conn.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",   # noqa: S608
                     [row["id"]] + [row[c] for c in cols])
    conn.commit()


def _row_etag(row: Optional[dict[str, Any]]) -> str:
    """A fingerprint of what a row holds, not of when it was touched: ``updated_at`` moves on a delete/restore pair that leaves the
    content as it was, and undoing the newest write must leave the state the older write's etag describes."""
    return _digest({k: v for k, v in row.items() if k != "updated_at"}) if row is not None else ""


def _asset_project(store: Any, asset_id: str) -> str:
    """The project of an asset that is live or in the trash ('' when it is gone)."""
    row = store.conn.execute("SELECT project_id FROM assets WHERE id=?", (asset_id,)).fetchone() \
        or store.conn.execute("SELECT project_id FROM asset_trash WHERE id=?", (asset_id,)).fetchone()
    return str(row["project_id"]) if row else ""


def _asset_exists(store: Any, asset_id: str) -> bool:
    return store.conn.execute("SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone() is not None


def _asset_path(project_id: str, asset_id: str) -> str:
    return f"project:{project_id}/asset:{asset_id}" if project_id else f"asset:{asset_id}"


def _noop(before: Any) -> bool:
    return isinstance(before, dict) and bool(before.get("noop"))


# ------------------------------------------------------------------------------------------------ projects

def _project_etag(store: Any, project_id: str) -> str:
    row = _row(store, "projects", project_id)
    if row is None:
        return ""
    return _digest({k: row.get(k) for k in ("name", "brief", "image_engine", "cover_asset_id", "deleted_at")})


def track_project_create(args: dict, result: dict, ctx: Any = None) -> dict:
    pid = str(result.get("id") or "")
    return {"objects": [f"project:{pid}"], "ids": [f"project_id={pid}"], "etag": _project_etag(ctx.store, pid)}


def undo_project_create(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    pid = _id(record, "project_id")
    row = _row(store, "projects", pid)
    if row is None:
        return {"already_gone": True}
    if row.get("deleted_at"):
        return {"already_in_trash": pid}
    if _project_etag(store, pid) != record.get("etag"):
        raise _conflict("The project's name, brief, cover or engine changed after it was created.")
    live = store.project_live_jobs(pid)
    if live:
        raise _conflict(f"The project has {len(live)} job(s) queued or running; cancel them first (studio_cancel_job).")
    if dry_run:
        return {"would_trash_project": pid, "name": row.get("name")}
    try:
        store.trash_project(pid)
    except ProjectBusy as exc:
        raise _conflict(str(exc)) from None
    moved = prod.stash_productions(store.data_dir, pid)
    return {"trashed_project": pid, "productions_moved": moved, "restore": "studio_trash action=restore projects=[id]"}


def capture_project_delete(ctx: Any, args: dict) -> dict:
    row = _row(ctx.store, "projects", _s(args, "project"))
    if row is None:
        raise NotFound("project", _s(args, "project"))
    return {"project_id": row["id"], "was_trashed": bool(row.get("deleted_at"))}


def track_project_delete(args: dict, result: dict, ctx: Any = None) -> dict:
    pid = _s(args, "project")
    return {"objects": [f"project:{pid}"], "ids": [f"project_id={pid}"], "etag": _project_etag(ctx.store, pid)}


def undo_project_delete(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    before = _before(record)
    if before.get("was_trashed"):
        return {"unchanged": True}
    pid = before.get("project_id") or _id(record, "project_id")
    row = _row(store, "projects", pid)
    if row is None:
        raise _conflict("The project was emptied from the trash after this write, so it cannot come back.")
    if not row.get("deleted_at"):
        return {"already_restored": pid}
    if _project_etag(store, pid) != record.get("etag"):
        raise _conflict("The project changed after it was deleted.")
    if dry_run:
        return {"would_restore_project": pid, "name": row.get("name")}
    store.restore_project(pid)
    return {"restored_project": pid, "productions": prod.unstash_productions(store.data_dir, pid)}


# ------------------------------------------------------------------------------------------------ assets and the trash

def capture_asset_delete(ctx: Any, args: dict) -> dict:
    found = []
    for aid in dict.fromkeys(str(i) for i in (args.get("ids") or [])):
        if _asset_exists(ctx.store, aid):
            found.append({"id": aid, "project_id": _asset_project(ctx.store, aid)})
    return {"assets": found}


def track_asset_delete(args: dict, result: dict, ctx: Any = None) -> dict:
    deleted = sorted(str(i) for i in (result.get("deleted") or []))
    objects = [_asset_path(_asset_project(ctx.store, a), a) for a in deleted]
    return {"objects": objects, "ids": [f"asset_id={a}" for a in deleted[:20]], "etag": ""}


def undo_asset_delete(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    in_trash = {r["id"] for r in store.list_trash(limit=1000)}
    restore, present, missing = [], [], []
    for item in _before(record).get("assets") or []:
        aid = item["id"]
        if aid in in_trash:
            if _row(store, "projects", item["project_id"]) is None:
                raise _conflict(f"The project of asset {aid} no longer exists.")
            restore.append(aid)
        elif _asset_exists(store, aid):
            present.append(aid)
        else:
            missing.append(aid)
    if dry_run:
        return {"would_restore": restore, "already_present": present, "emptied_from_trash": missing}
    for aid in restore:
        store.restore_asset(aid)
    return {"restored": restore, "already_present": present, "emptied_from_trash": missing}


def track_asset_create(args: dict, result: dict, ctx: Any = None) -> dict:
    aid = str(result.get("id") or "")
    return {"objects": [_asset_path(_asset_project(ctx.store, aid), aid)], "ids": [f"asset_id={aid}"], "etag": ""}


def undo_asset_create(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    aid = _id(record, "asset_id")
    if not _asset_exists(store, aid):
        return {"already_gone": True}
    refs = store.asset_references(aid)
    if refs:
        raise _conflict(f"The asset is used by {', '.join(sorted({r['kind'] for r in refs}))} now, so it is not removed.")
    if dry_run:
        return {"would_trash_asset": aid}
    try:
        store.trash_asset(aid)
    except AssetInUse as exc:
        raise _conflict(str(exc)) from None
    return {"trashed_asset": aid, "restore": "studio_trash action=restore ids=[id]"}


def capture_trash(ctx: Any, args: dict) -> Optional[dict]:
    action = _s(args, "action") or "list"
    if action == "list":
        return {"noop": True}
    if action != "restore":
        return None                                  # "empty" deletes files for good: not undoable
    store = ctx.store
    in_trash = {r["id"]: r["project_id"] for r in store.list_trash(limit=1000)}
    assets = [{"id": str(a), "project_id": in_trash[str(a)]} for a in (args.get("ids") or []) if str(a) in in_trash]
    projects = [str(p) for p in (args.get("projects") or []) if (_row(store, "projects", str(p)) or {}).get("deleted_at")]
    return {"action": "restore", "assets": assets, "projects": projects}


def track_trash(args: dict, result: dict, ctx: Any = None) -> dict:
    if (_s(args, "action") or "list") != "restore":
        return {"objects": [], "etag": ""}
    objects = [f"project:{p}" for p in (args.get("projects") or [])]
    objects += [_asset_path(_asset_project(ctx.store, str(a)), str(a)) for a in (args.get("ids") or [])]
    return {"objects": objects, "etag": ""}


def undo_trash(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    before = _before(record)
    if _noop(before) or not (before.get("assets") or before.get("projects")):
        return {"unchanged": True}
    assets = [a["id"] for a in before.get("assets") or [] if _asset_exists(store, a["id"])]
    projects = []
    for pid in before.get("projects") or []:
        row = _row(store, "projects", pid)
        if row is not None and not row.get("deleted_at"):
            projects.append(pid)
    if dry_run:
        return {"would_trash_assets": assets, "would_trash_projects": projects}
    for pid in projects:
        live = store.project_live_jobs(pid)
        if live:
            raise _conflict(f"Project {pid} has {len(live)} job(s) queued or running now; cancel them first.")
    for aid in assets:
        try:
            store.trash_asset(aid, force=True)
        except AssetInUse as exc:
            raise _conflict(str(exc)) from None
    for pid in projects:
        store.trash_project(pid)
        prod.stash_productions(store.data_dir, pid)
    return {"trashed_assets": assets, "trashed_projects": projects}


# ------------------------------------------------------------------------------------------------ cast

def _cast_table(args: dict) -> str:
    return "groups" if (_s(args, "kind") or "character") == "group" else "characters"


def _cast_path(args: dict, key: str) -> str:
    return f"project:{_s(args, 'project')}/{'group' if _cast_table(args) == 'groups' else 'cast'}:{key}"


def capture_cast(ctx: Any, args: dict) -> dict:
    action = _s(args, "action") or "list"
    if action in ("list", "deleted"):
        return {"noop": True}
    table = _cast_table(args)
    if action == "create":
        return {"action": "create", "table": table}
    row = _row(ctx.store, table, _s(args, "id"))
    if row is None:
        raise NotFound("group" if table == "groups" else "character", _s(args, "id"))
    return {"action": action, "table": table, "row": row}


def track_cast(args: dict, result: dict, ctx: Any = None) -> dict:
    action = _s(args, "action") or "list"
    if action in ("list", "deleted"):
        return {"objects": [], "etag": ""}
    key = str(result.get("id") or "") if action == "create" else _s(args, "id")
    return {"objects": [_cast_path(args, key)], "ids": [f"cast_id={key}"], "etag": _row_etag(_row(ctx.store, _cast_table(args), key))}


def undo_cast(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    before = _before(record)
    if _noop(before):
        return {"unchanged": True}
    action, table = before.get("action"), before.get("table") or "characters"
    snapshot = before.get("row") or {}
    key = snapshot.get("id") or _id(record, "cast_id")
    current = _row(store, table, key)
    if action == "create":
        if current is None or (table == "characters" and current.get("deleted_at")):
            return {"already_gone": True}
        if _row_etag(current) != record.get("etag"):
            raise _conflict("The cast entry was edited after it was created.")
        if dry_run:
            return {"would_delete": key}
        if table == "groups":
            store.delete_group(key)
            return {"deleted": key}
        store.delete_character(key)
        return {"deleted": key, "restore": "studio_cast action=restore"}
    if action == "update":
        if current is None:
            raise _conflict("The cast entry no longer exists.")
        if _row_etag(current) != record.get("etag"):
            raise _conflict("The cast entry was edited again after this write.")
        if dry_run:
            return {"would_restore_fields": sorted(k for k in snapshot if snapshot[k] != current.get(k) and k != "updated_at")}
        _restore_row(store, table, snapshot)
        return {"restored": key, "note": "an image the update cropped for the canonical stays in the assets"}
    if action == "delete" and table == "groups":
        if current is not None:
            return {"already_present": key}
        if _row(store, "projects", snapshot["project_id"]) is None:
            raise _conflict("The project of the group no longer exists.")
        if dry_run:
            return {"would_recreate_group": key}
        _restore_row(store, table, snapshot)
        return {"recreated_group": key}
    if action == "delete":
        if current is None:
            raise _conflict("The cast entry no longer exists.")
        if not current.get("deleted_at"):
            return {"already_restored": key}
        if _row_etag(current) != record.get("etag"):
            raise _conflict("The cast entry changed after it was deleted.")
        if dry_run:
            return {"would_restore": key}
        try:
            store.restore_character(key)
        except ValueError as exc:
            raise _conflict(str(exc)) from None
        return {"restored": key}
    if action == "restore":
        if current is None or current.get("deleted_at"):
            return {"already_deleted": key}
        if _row_etag(current) != record.get("etag"):
            raise _conflict("The cast entry changed after it was restored.")
        if dry_run:
            return {"would_delete": key}
        store.delete_character(key)
        return {"deleted": key}
    return {"unchanged": True}


# ------------------------------------------------------------------------------------------------ style cards

def capture_style_card(ctx: Any, args: dict) -> dict:
    action = _s(args, "action") or "list"
    if action in ("list", "get"):
        return {"noop": True}
    if action == "create":
        return {"action": "create"}
    card = graphic_shots.style_card(ctx.store, _s(args, "project") or None, _s(args, "id"))
    row = _row(ctx.store, "style_presets", card["id"]) if card else None
    if row is None:
        raise NotFound("style_card", _s(args, "id"))
    return {"action": action, "row": row}


def track_style_card(args: dict, result: dict, ctx: Any = None) -> dict:
    if (_s(args, "action") or "list") in ("list", "get"):
        return {"objects": [], "etag": ""}
    card_id = str(result.get("id") or result.get("deleted") or "")
    return {"objects": [f"style:{card_id}"], "ids": [f"style_id={card_id}"], "etag": _row_etag(_row(ctx.store, "style_presets", card_id))}


def undo_style_card(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    before = _before(record)
    if _noop(before):
        return {"unchanged": True}
    action, snapshot = before.get("action"), before.get("row") or {}
    key = snapshot.get("id") or _id(record, "style_id")
    current = _row(store, "style_presets", key)
    if action == "create":
        if current is None:
            return {"already_gone": True}
        if _row_etag(current) != record.get("etag"):
            raise _conflict("The style card was edited after it was created.")
        if dry_run:
            return {"would_delete": key}
        store.delete_style_preset(key)
        return {"deleted": key}
    if action == "update":
        if current is None:
            raise _conflict("The style card no longer exists.")
        if _row_etag(current) != record.get("etag"):
            raise _conflict("The style card was edited again after this write.")
        if dry_run:
            return {"would_restore": key}
        _restore_row(store, "style_presets", snapshot)
        return {"restored": key}
    if action == "delete":
        if current is not None:
            return {"already_present": key}
        if dry_run:
            return {"would_recreate": key, "name": snapshot.get("name")}
        _restore_row(store, "style_presets", snapshot)
        return {"recreated": key}
    return {"unchanged": True}


# ------------------------------------------------------------------------------------------------ productions

#: what a running production changes all the time and an edit does not: left out of the fingerprint
_VOLATILE = ("updated_at", "lineage", "timings")


def _state_etag(state: dict[str, Any]) -> str:
    return _digest({k: v for k, v in state.items() if k not in _VOLATILE})


def _snapshot_dir(store: Any, slug: str) -> Path:
    return store.data_dir / "agent_undo" / "productions" / prod.production_dir(store.data_dir, slug).name


def _prune(folder: Path) -> None:
    files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_SNAPSHOTS:]:
        try:
            old.unlink()
        except OSError:
            pass


def _queued_job(store: Any, state: dict[str, Any], keep: Optional[str] = None) -> Optional[str]:
    """The job id a state waits on when it has not started (so undo may cancel it); a conflict when it is running."""
    job_id = state.get("job_id")
    if not job_id or job_id == keep:
        return None
    try:
        job = store.get_job(job_id)
    except NotFound:
        return None
    if job["state"] in ("queued", "waiting_gpu"):
        return job["id"]
    if job["state"] == "running":
        raise _conflict("The run this write queued has started; cancel it first (studio_cancel_job).")
    return None


def capture_production(ctx: Any, args: dict) -> dict:
    """Copy the production's state file before an edit (a state can be hundreds of KB, too big for the journal)."""
    slug = _s(args, "production")
    state = prod.load_state(ctx.store.data_dir, slug)
    if prod.is_legacy(state):
        raise prod.ProductionError("legacy_production", "a scripted production cannot be edited here")
    folder = _snapshot_dir(ctx.store, slug)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{new_id('snap')}.json"
    write_text_atomic(folder / name, json.dumps(state, indent=1, ensure_ascii=False))
    _prune(folder)
    return {"slug": slug, "snapshot": name}


def track_production(args: dict, result: dict, ctx: Any = None) -> dict:
    slug = _s(args, "production")
    state = prod.load_state(ctx.store.data_dir, slug)
    return {"objects": [f"production:{slug}"], "ids": [f"production={slug}"], "etag": _state_etag(state)}


def undo_production(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    before = _before(record)
    slug = before.get("slug") or ""
    path = _snapshot_dir(store, slug) / str(before.get("snapshot") or "-")
    if not path.is_file():
        raise AppError("not_found", "The copy of the production taken before this write is gone.",
                       hint=f"Only the newest {KEEP_SNAPSHOTS} snapshots of each production are kept.")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    with prod.lock_for(slug):
        try:
            current = prod.load_state(store.data_dir, slug)
        except NotFound:
            raise _conflict("The production no longer exists.") from None
        if prod.is_running(current, store.data_dir):
            raise _conflict("The production is running now; pause or cancel it before undoing an edit.")
        if _state_etag(current) != record.get("etag"):
            raise _conflict("The production changed after this write (another edit, or a run that started).")
        queued = _queued_job(store, current, keep=snapshot.get("job_id"))
        if dry_run:
            return {"would_restore_production": slug, "would_cancel_queued_job": queued}
        snapshot["slug"] = slug
        prod.log(snapshot, "agent", "undo", tool=record.get("tool"), session=record.get("session") or None)
        prod.save_state(store.data_dir, snapshot)
        if queued:
            store.request_cancel(queued)
    return {"restored_production": slug, "cancelled_queued_job": queued}


def track_production_create(args: dict, result: dict, ctx: Any = None) -> dict:
    slug = str((result.get("production") or {}).get("slug") or "")
    state = prod.load_state(ctx.store.data_dir, slug)
    return {"objects": [f"production:{slug}"], "ids": [f"production={slug}"], "etag": _state_etag(state)}


def undo_production_create(ctx: Any, record: dict, dry_run: bool = False) -> dict:
    store = ctx.store
    slug = _id(record, "production")
    folder = prod.production_dir(store.data_dir, slug)
    if not folder.is_dir():
        return {"already_gone": True}
    with prod.lock_for(slug):
        current = prod.load_state(store.data_dir, slug)
        if prod.is_running(current, store.data_dir):
            raise _conflict("The production is running now; cancel it first (studio_cancel_job).")
        if _state_etag(current) != record.get("etag"):
            raise _conflict("The production changed after it was created (an edit, or the run started).")
        queued = _queued_job(store, current)
        if dry_run:
            return {"would_move_to_trash": slug, "would_cancel_queued_job": queued}
        if queued:
            store.request_cancel(queued)
        target = store.data_dir / "trash" / "agent_undo" / "productions" / f"{slug}-{new_id('u')}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(target))
    return {"moved_to_trash": slug, "folder": target.relative_to(store.data_dir).as_posix(), "cancelled_queued_job": queued,
            "note": "a cast member the production copied into its project stays there"}


# ------------------------------------------------------------------------------------------------ the table

_PRODUCTION = {"capture": capture_production, "track": track_production, "undo": undo_production}

#: tool name -> the hooks to attach (``dataclasses.replace(tool, **HOOKS[name])``)
HOOKS: dict[str, dict[str, Callable[..., Any]]] = {
    "studio_create_project": {"track": track_project_create, "undo": undo_project_create},
    "studio_delete_project": {"capture": capture_project_delete, "track": track_project_delete, "undo": undo_project_delete},
    "studio_delete_assets": {"capture": capture_asset_delete, "track": track_asset_delete, "undo": undo_asset_delete},
    "studio_import": {"track": track_asset_create, "undo": undo_asset_create},
    "studio_trash": {"capture": capture_trash, "track": track_trash, "undo": undo_trash},
    "studio_cast": {"capture": capture_cast, "track": track_cast, "undo": undo_cast},
    "studio_style_cards": {"capture": capture_style_card, "track": track_style_card, "undo": undo_style_card},
    "studio_production_shots": _PRODUCTION,
    "studio_graphic_shot": _PRODUCTION,
    "studio_production_segments": _PRODUCTION,
    "studio_production_settings": _PRODUCTION,
    "studio_production_finishing": _PRODUCTION,
    "studio_production_script": _PRODUCTION,
    "studio_production_create": {"track": track_production_create, "undo": undo_production_create},
}

#: writes that create new objects or edit drafts and never delete or publish: what a token with the ``drafts`` profile may call.
#: Left out: tools with a delete action (studio_cast, studio_style_cards, studio_trash, studio_timeline, studio_spaces, the
#: character kit tools, studio_production_shots with its ``delete`` change), the ones that stop work or touch the machine
#: (studio_cancel_job, the services), the ones that send to other apps (production_export_lumiere, cast_import_character) and
#: the ones that redo work already made (regenerate, promote, continue, QA).
DRAFT_SAFE = frozenset({
    "studio_create_project", "studio_import", "studio_generate_image", "studio_edit_image", "studio_outpaint", "studio_animate",
    "studio_compose", "studio_voice", "studio_time_lyrics", "studio_design", "studio_photocard_set", "studio_render",
    "studio_canvas", "studio_interpolate", "studio_stems", "studio_reframe", "studio_retake", "studio_clip_edit",
    "studio_video_frames", "studio_video_plan", "studio_video_from_plan", "studio_short_create", "studio_download_media",
    "studio_character_sheet", "studio_production_create", "studio_graphic_shot", "studio_graphic_render",
    "studio_production_segments", "studio_production_settings", "studio_production_finishing", "studio_production_script",
    "studio_recipe_export", "studio_recipe_run", "studio_animatic",
    "voice_create", "voice_speak", "voice_tts", "voice_audiobook", "voice_dub", "production_from_storyboard",
})
