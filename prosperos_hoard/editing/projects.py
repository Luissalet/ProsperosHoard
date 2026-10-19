"""Projects: the timeline document, its revision counter and the undo / redo history."""

from __future__ import annotations

import json
import hashlib
from contextlib import nullcontext
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import media as media_store
from .errors import Conflict, LumiereError, NotFound
from .ops import PRESETS, apply_ops, nest_plan
from .timeline import Clip, Project, clone, load, new_project, validate
from .util import clip, dumps, ms_to_tc, new_id

if TYPE_CHECKING:
    from .services import Services

HISTORY_LIMIT = 300


def _row(svc: "Services", project_id: str):
    row = svc.db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise NotFound(f"No project {project_id}.")
    return row


def doc(svc: "Services", project_id: str) -> Project:
    return load(json.loads(_row(svc, project_id)["doc"]))


def media_lookup(svc: "Services"):
    """Media and nested projects by id: ``prj_...`` ids answer as media of kind "sequence" (their timeline is the source)."""
    cache: dict[str, Optional[dict]] = {}

    def look(mid: str) -> Optional[dict[str, Any]]:
        if mid not in cache:
            cache[mid] = sequence_info(svc, mid) if str(mid).startswith("prj_") else media_store.lookup(svc, mid)
        return cache[mid]

    return look


def doc_lookup(svc: "Services"):
    def read(project_id: str) -> Optional[Project]:
        try:
            return doc(svc, project_id)
        except NotFound:
            return None

    return read


def nested_ids(svc: "Services", project_id: str) -> set[str]:
    """Every project reachable through sequence clips from ``project_id`` (itself included only when there is a loop)."""
    seen: set[str] = set()
    todo = [project_id]
    while todo:
        row = svc.db.one("SELECT doc FROM projects WHERE id = ?", (todo.pop(),))
        if row is None:
            continue
        for sid in load(json.loads(row["doc"])).sequence_ids():
            if sid not in seen:
                seen.add(sid)
                todo.append(sid)
    return seen


def sequence_info(svc: "Services", project_id: str) -> Optional[dict[str, Any]]:
    """A project seen as a source for a sequence clip (the same fields as a media, kind "sequence")."""
    row = svc.db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        return None
    p = load(json.loads(row["doc"]))
    contains = nested_ids(svc, project_id)
    from .render import sequences  # lazy: render imports this module

    cached = sequences.cached(svc, project_id)
    return {"id": project_id, "name": row["name"], "kind": "sequence", "duration_ms": p.duration, "width": p.canvas.width, "height": p.canvas.height,
            "fps": p.canvas.fps, "has_audio": True, "has_video": True, "missing": False, "proxy": "ready" if cached else None, "rev": row["rev"],
            "contains": sorted(contains), "cycle": project_id in contains, "path": "", "analysis": [],
            "urls": {"play": f"/api/projects/{project_id}/sequence.mp4" if cached and not cached.alpha else None, "file": None, "poster": None, "sprite": None,
                     "waveform": None}}


def summary(svc: "Services", row) -> dict[str, Any]:
    p = load(json.loads(row["doc"]))
    clips = sum(len(t.clips) for t in p.tracks)
    return {"id": row["id"], "name": row["name"], "rev": row["rev"], "duration_ms": p.duration, "duration": ms_to_tc(p.duration),
            "canvas": f"{p.canvas.width}x{p.canvas.height}@{p.canvas.fps:g}", "clips": clips, "tracks": len(p.tracks),
            "is_template": bool(row["is_template"]), "updated_ts": row["updated_ts"], "created_ts": row["created_ts"],
            "thumb": _thumb(svc, p)}


def _thumb(svc: "Services", p: Project) -> Optional[str]:
    main = p.main_track()
    for c in (main.clips if main else []):
        if c.media and c.type == "media":
            return f"/api/media/{c.media}/poster"
    return None


def list_projects(svc: "Services", templates: Optional[bool] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM projects"
    args: tuple = ()
    if templates is not None:
        sql += " WHERE is_template = ?"
        args = (int(templates),)
    return [summary(svc, r) for r in svc.db.query(sql + " ORDER BY updated_ts DESC", args)]


def view(svc: "Services", project_id: str) -> dict[str, Any]:
    row = _row(svc, project_id)
    p = load(json.loads(row["doc"]))
    look = media_lookup(svc)
    used = {}
    for mid in sorted(p.media_ids() | p.sequence_ids()):
        if not mid.startswith("prj_"):
            media_store.ensure_color_space(svc, mid)
        info = look(mid)
        if info:
            used[mid] = {k: info.get(k) for k in ("id", "name", "kind", "duration_ms", "width", "height", "fps", "has_audio", "has_video", "missing", "proxy", "urls",
                                                  "color_space")}
            if info["kind"] == "sequence":
                used[mid].update(rev=info["rev"], contains=info["contains"])
    can_undo = row["head"] > 1
    can_redo = svc.db.one("SELECT 1 FROM history WHERE project_id = ? AND seq > ?", (project_id, row["head"])) is not None
    return {**summary(svc, row), "doc": p.dump(), "media": used, "issues": validate(p, look), "can_undo": can_undo, "can_redo": can_redo,
            "undo_label": _label(svc, project_id, row["head"]) if can_undo else None,
            "redo_label": _label(svc, project_id, row["head"] + 1) if can_redo else None}


def _label(svc: "Services", project_id: str, seq: int) -> Optional[str]:
    row = svc.db.one("SELECT label FROM history WHERE project_id = ? AND seq = ?", (project_id, seq))
    return row["label"] if row else None


def _insert(svc: "Services", name: str, p: Project, *, template: bool = False, label: str = "Crear") -> str:
    pid = new_id("prj")
    now = time.time()
    with svc.db.transaction() as conn:
        conn.execute("INSERT INTO projects(id, name, doc, rev, head, is_template, created_ts, updated_ts) VALUES (?, ?, ?, 1, 1, ?, ?, ?)",
                     (pid, clip(name or "Proyecto", 120), dumps(p.dump()), int(template), now, now))
        conn.execute("INSERT INTO history(project_id, seq, label, actor, doc, ts) VALUES (?, 1, ?, 'ui', ?, ?)", (pid, label, dumps(p.dump()), now))
    return pid


def create(svc: "Services", name: str, *, preset: Optional[str] = None, width: Optional[int] = None, height: Optional[int] = None,
           fps: Optional[float] = None, media: Optional[list[str]] = None, template: bool = False, from_project: Optional[str] = None) -> dict[str, Any]:
    if from_project:
        p = doc(svc, from_project)
    else:
        spec = PRESETS.get(preset or "youtube")
        if preset and spec is None:
            raise LumiereError(f"Unknown preset {preset!r}. Known: {', '.join(PRESETS)}.")
        p = new_project(width or spec["width"], height or spec["height"], fps or spec["fps"])
    pid = _insert(svc, name, p, template=template)
    if media:
        edit(svc, pid, [{"op": "add_media", "media": m} for m in media], label="Añadir medios")
    svc.emit("lumiere.project.created", {"id": pid, "name": clip(name, 80)})
    return view(svc, pid)


# ---------------------------------------------------------------- templates with media slots

def slot_rule(slot: str) -> str:
    """full: the slot's clip takes its media's whole length and what follows moves (slots named main*); keep: the slot's own length."""
    return "full" if slot.lower().startswith("main") else "keep"


def template_slots(svc: "Services", p: Project) -> list[dict[str, Any]]:
    look = media_lookup(svc)
    rows = []
    for t, c in p.all_clips():
        if not c.slot:
            continue
        info = look(c.media) if c.media else None
        rows.append({"slot": c.slot, "clip": c.id, "track": t.id, "track_role": t.role, "track_kind": t.kind, "type": c.type, "start_ms": c.start,
                     "length_ms": c.duration, "media": c.media, "name": (info or {}).get("name") or (c.text[:40] if c.type == "text" else ""),
                     "rule": slot_rule(c.slot)})
    return sorted(rows, key=lambda r: r["start_ms"])


def template_info(svc: "Services", template: str) -> dict[str, Any]:
    row = _template_row(svc, template)
    p = load(json.loads(row["doc"]))
    return {**summary(svc, row), "slots": template_slots(svc, p)}


def list_templates(svc: "Services") -> list[dict[str, Any]]:
    return [template_info(svc, r["id"]) for r in svc.db.query("SELECT id FROM projects WHERE is_template = 1 ORDER BY updated_ts DESC")]


def _template_row(svc: "Services", ref: str):
    """A template by id or by name (a project that has slots also works, so a project can be reused without saving a template)."""
    row = svc.db.one("SELECT * FROM projects WHERE id = ?", (ref,))
    if row is None:
        rows = [r for r in svc.db.query("SELECT * FROM projects WHERE is_template = 1") if r["name"].strip().lower() == ref.strip().lower()]
        if len(rows) > 1:
            raise LumiereError(f"{len(rows)} templates are called {ref!r}: use the id ({', '.join(r['id'] for r in rows)}).")
        row = rows[0] if rows else None
    if row is None:
        names = [f"{r['name']} ({r['id']})" for r in svc.db.query("SELECT id, name FROM projects WHERE is_template = 1 LIMIT 20")]
        raise NotFound(f"No template {ref!r}." + (f" Templates: {', '.join(names)}." if names else " Save one first (template_save)."))
    return row


def save_template(svc: "Services", project_id: str, name: str, slots: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Copy a project as a template. ``slots`` maps clip ids to slot names (intro, main, outro...) on top of the slots the clips
    already carry; the copy keeps everything else (titles, captions style, music, effects, the sample media in each slot, so the
    template also renders as is). The original project is not touched."""
    current = doc(svc, project_id)
    ops = [{"op": "set", "clip": cid, "props": {"slot": (slot or None)}, "ripple": False} for cid, slot in (slots or {}).items()]
    if ops:
        current, _ = apply_ops(current, ops, media_lookup(svc))
    if not template_slots(svc, current):
        raise LumiereError("A template needs at least one slot: say which clips are replaceable, e.g. slots {'<clip id>': 'main', '<clip id>': 'intro'}.")
    pid = _insert(svc, name, current, template=True, label="Plantilla")
    svc.emit("lumiere.project.created", {"id": pid, "name": clip(name, 80)})
    return template_info(svc, pid)


def resolve_media(svc: "Services", ref: str) -> str:
    """A library media from its id or its name (exact, then the file name, then a name that contains the text when only one does)."""
    ref = (ref or "").strip()
    if not ref:
        raise LumiereError("Name the media for each slot (its id or its name in the library).")
    if media_store.lookup(svc, ref):
        return ref
    items = media_store.list_media(svc, limit=500)
    low = ref.lower()
    stem = lambda m: Path(m["path"]).stem.lower()  # noqa: E731
    for test in (lambda m: m["name"].lower() == low or Path(m["path"]).name.lower() == low, lambda m: stem(m) == low,
                 lambda m: low in m["name"].lower() or low in Path(m["path"]).name.lower()):
        hits = [m for m in items if test(m)]
        if len(hits) == 1:
            return hits[0]["id"]
        if len(hits) > 1:
            raise LumiereError(f"{len(hits)} media match {ref!r}: " + ", ".join(f"{m['name']} ({m['id']})" for m in hits[:8]) + ". Use the id.")
    raise NotFound(f"No media called {ref!r} in the library (media_list shows them; media_import adds a file).")


def create_from_template(svc: "Services", name: str, template: str, slots: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A new project from a template with its slots filled: ``slots`` maps a slot name to a media (id or name), or to
    {media, src_in?, src_out?, length?}. Everything outside the slots stays; slots left out keep the template's sample media and
    are listed in ``unfilled``. A slot named main* takes its media's whole length and the rest of the edit moves; the others keep
    their length (shorter when the media is)."""
    row = _template_row(svc, template)
    p = load(json.loads(row["doc"]))
    known = template_slots(svc, p)
    names = [r["slot"] for r in known]
    if not names:
        raise LumiereError(f"{row['name']!r} has no slots; mark clips with set {{props: {{slot: 'name'}}}} or save it again as a template with slots.")
    wanted = dict(slots or {})
    unknown = sorted(set(wanted) - set(names))
    if unknown:
        raise LumiereError(f"Unknown slot {', '.join(repr(u) for u in unknown)}. This template has: {', '.join(names)}.")
    ops = []
    for r in known:  # in timeline order, so errors read naturally
        if r["slot"] not in wanted:
            continue
        spec = wanted[r["slot"]]
        spec = {"media": spec} if isinstance(spec, str) else dict(spec)
        spec["media"] = resolve_media(svc, str(spec.get("media", "")))
        ops.append({"op": "fill_slot", "slot": r["slot"], **{k: v for k, v in spec.items() if k in ("media", "src_in", "src_out", "length", "rule")}})
    new, results = apply_ops(p, ops, media_lookup(svc)) if ops else (p, [])
    pid = _insert(svc, name or f"{row['name']} (nuevo)", new, label=f"Desde plantilla «{clip(row['name'], 60)}»")
    svc.emit("lumiere.project.created", {"id": pid, "name": clip(name, 80)})
    return {**view(svc, pid), "template": row["id"], "filled": results, "unfilled": [n for n in names if n not in wanted]}


def rename(svc: "Services", project_id: str, name: str) -> dict[str, Any]:
    _row(svc, project_id)
    svc.db.execute("UPDATE projects SET name = ?, updated_ts = ? WHERE id = ?", (clip(name, 120), time.time(), project_id))
    return summary(svc, _row(svc, project_id))


def set_template(svc: "Services", project_id: str, value: bool) -> dict[str, Any]:
    _row(svc, project_id)
    svc.db.execute("UPDATE projects SET is_template = ? WHERE id = ?", (int(value), project_id))
    return summary(svc, _row(svc, project_id))


def delete(svc: "Services", project_id: str) -> dict[str, Any]:
    row = _row(svc, project_id)
    svc.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    return {"deleted": project_id, "name": row["name"]}


def save(svc: "Services", project_id: str, p: Project, label: str, actor: str = "ui", base_rev: Optional[int] = None,
         *, _receipt: Optional[tuple[str, str, dict[str, Any]]] = None, _conn: Any = None) -> int:
    """Store a new version: drops any redo branch, appends to the history, bumps the revision."""
    now = time.time()
    with (nullcontext(_conn) if _conn is not None else svc.db.transaction()) as conn:
        row = conn.execute("SELECT rev, head FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFound(f"No project {project_id}.")
        if base_rev is not None and base_rev != row["rev"]:
            raise Conflict(f"The project changed (revision {row['rev']}, you sent {base_rev}). Read it again and repeat the edit.")
        head = row["head"] + 1
        conn.execute("DELETE FROM history WHERE project_id = ? AND seq >= ?", (project_id, head))
        conn.execute("INSERT INTO history(project_id, seq, label, actor, doc, ts) VALUES (?, ?, ?, ?, ?, ?)",
                     (project_id, head, clip(label, 200), actor, dumps(p.dump()), now))
        conn.execute("DELETE FROM history WHERE project_id = ? AND seq <= ?", (project_id, head - HISTORY_LIMIT))
        conn.execute("UPDATE projects SET doc = ?, rev = rev + 1, head = ?, updated_ts = ? WHERE id = ?", (dumps(p.dump()), head, now, project_id))
        if _receipt is not None:
            request_id, digest, result = _receipt
            conn.execute("INSERT INTO edit_receipts VALUES (?, ?, ?, ?, ?)",
                         (project_id, request_id, digest, dumps({**result, 'rev': row['rev'] + 1}), now))
        return row["rev"] + 1


def edit(svc: "Services", project_id: str, ops: list[dict[str, Any]], *, label: str = "", actor: str = "ui",
         base_rev: Optional[int] = None, request_id: str = "") -> dict[str, Any]:
    if not ops:
        raise LumiereError("No operations.")
    if not isinstance(request_id, str) or len(request_id) > 100:
        raise LumiereError('request_id must be a string of at most 100 characters.')
    # Keep read/apply/save together. A keyed retry returns the stored effect;
    # an edit without a key retains its ordinary apply-again semantics.
    with svc.db.transaction() as conn:
        row = _row(svc, project_id)
        digest = ''
        if request_id:
            try:
                encoded = json.dumps({'ops': ops, 'label': label, 'base_rev': base_rev},
                                     sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            except (ValueError, TypeError) as error:
                raise LumiereError('Edit arguments must contain finite JSON values.') from error
            digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            receipt = svc.db.one('SELECT digest, result FROM edit_receipts WHERE project_id=? AND request_id=?',
                                 (project_id, request_id))
            if receipt is not None:
                if receipt['digest'] != digest:
                    raise Conflict('This request_id already identifies a different edit; use a new key for a new edit.')
                return {**json.loads(receipt['result']), 'replayed': True, 'current_rev': row['rev']}
        current = load(json.loads(row['doc']))
        look = media_lookup(svc)
        new, results = apply_ops(current, ops, look, project_id=project_id, docs=doc_lookup(svc))
        result = {'project': project_id, 'results': results, 'duration_ms': new.duration,
                  'duration': ms_to_tc(new.duration), 'issues': validate(new, look)}
        receipt = (request_id, digest, result) if request_id else None
        rev = save(svc, project_id, new, label or describe(ops), actor, base_rev, _receipt=receipt, _conn=conn)
        return {**result, 'rev': rev, **({'replayed': False, 'current_rev': rev} if request_id else {})}


def describe(ops: list[dict[str, Any]]) -> str:
    names = {"add_media": "Añadir clip", "add_text": "Añadir texto", "split": "Dividir", "trim": "Recortar", "move": "Mover", "delete": "Borrar",
             "delete_range": "Borrar tramo", "cut_source": "Cortar", "keep_source": "Quedarse con tramos", "set": "Cambiar clip",
             "speed": "Velocidad", "transition": "Transición", "filter_add": "Efecto", "filter_remove": "Quitar efecto", "track_add": "Nueva pista",
             "track_set": "Pista", "track_delete": "Borrar pista", "canvas": "Formato", "marker_add": "Marcador", "marker_delete": "Quitar marcadores",
             "captions": "Subtítulos", "duplicate": "Duplicar", "detach_audio": "Separar audio", "close_gaps": "Cerrar huecos",
             "replace_media": "Sustituir medio", "sequence": "Secuencia", "keyframes": "Animación", "slip": "Deslizar contenido", "roll": "Mover corte",
             "insert_clips": "Pegar", "notes": "Notas", "speed_ramp": "Curva de velocidad", "add_sequence": "Añadir secuencia",
             "unnest": "Desanidar", "mask": "Máscara", "add_overlay": "Superponer plano", "fill_slot": "Rellenar hueco"}
    first = names.get(str(ops[0].get("op")), str(ops[0].get("op")))
    return first if len(ops) == 1 else f"{first} (+{len(ops) - 1})"


def undo(svc: "Services", project_id: str) -> dict[str, Any]:
    return _move(svc, project_id, -1)


def redo(svc: "Services", project_id: str) -> dict[str, Any]:
    return _move(svc, project_id, +1)


def _move(svc: "Services", project_id: str, step: int) -> dict[str, Any]:
    with svc.db.transaction() as conn:
        row = conn.execute("SELECT head FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFound(f"No project {project_id}.")
        target = row["head"] + step
        h = conn.execute("SELECT doc, label FROM history WHERE project_id = ? AND seq = ?", (project_id, target)).fetchone()
        if h is None or target < 1:
            raise LumiereError("Nothing to redo." if step > 0 else "Nothing to undo.", code="no_history")
        conn.execute("UPDATE projects SET doc = ?, head = ?, rev = rev + 1, updated_ts = ? WHERE id = ?", (h["doc"], target, time.time(), project_id))
        label = h["label"] if step > 0 else _label(svc, project_id, row["head"])
    return {"project": project_id, "undone" if step < 0 else "redone": label, **{k: v for k, v in view(svc, project_id).items() if k in ("rev", "duration_ms", "can_undo", "can_redo", "undo_label", "redo_label")}}


def history(svc: "Services", project_id: str, limit: int = 60) -> dict[str, Any]:
    row = _row(svc, project_id)
    items = [{"seq": r["seq"], "label": r["label"], "actor": r["actor"], "ts": r["ts"], "current": r["seq"] == row["head"]}
             for r in svc.db.query("SELECT seq, label, actor, ts FROM history WHERE project_id = ? ORDER BY seq DESC LIMIT ?", (project_id, limit))]
    return {"project": project_id, "head": row["head"], "items": items}


def restore(svc: "Services", project_id: str, seq: int) -> dict[str, Any]:
    """Go back to any point in the history as a new step (nothing is lost)."""
    h = svc.db.one("SELECT doc, label FROM history WHERE project_id = ? AND seq = ?", (project_id, seq))
    if h is None:
        raise NotFound(f"No history step {seq}.")
    p = load(json.loads(h["doc"]))
    rev = save(svc, project_id, p, f"Volver a «{h['label']}»")
    return {"project": project_id, "rev": rev}


def outline(svc: "Services", project_id: str) -> dict[str, Any]:
    """A compact text-like description of the timeline for an assistant: tracks, clips with times and sources."""
    p = doc(svc, project_id)
    look = media_lookup(svc)
    tracks = []
    for t in p.tracks:
        clips = []
        for c in t.clips:
            item: dict[str, Any] = {"id": c.id, "start": ms_to_tc(c.start), "end": ms_to_tc(c.end), "start_ms": c.start, "end_ms": c.end}
            if c.type == "text":
                item["text"] = clip(c.text, 80)
            else:
                info = look(c.media) or {}
                item.update({"media": c.media, "name": clip(info.get("name", ""), 40), "src": f"{ms_to_tc(c.src_in)}–{ms_to_tc(c.src_out)}"})
                if c.type == "sequence":
                    item["sequence"] = True
                if c.has_ramp:
                    item["speed_curve"] = [{"t": k.t - c.src_in, "v": k.v, "ease": k.ease} for k in c.speed_keys]
                elif c.speed != 1:
                    item["speed"] = c.speed
                if c.mask:
                    item["mask"] = c.mask.shape + (" inverted" if c.mask.invert else "") + (" animated" if any(k.startswith("mask_") for k in c.keyframes) else "")
                if c.mute:
                    item["mute"] = True
                if c.transform.fit != "contain" or c.transform.scale != 1:
                    item["fit"] = c.transform.fit
                if c.reframe:
                    item["reframed"] = True
            if c.filters:
                item["effects"] = [f.type for f in c.filters]
            if c.keyframes:
                item["keyframes"] = {prop: [{"t": k.t, "v": k.v, "ease": k.ease} for k in keys] for prop, keys in c.keyframes.items()}
            if c.transition_in:
                item["transition"] = f"{c.transition_in.type} {c.transition_in.dur}ms"
            if c.multicam:
                item["multicam"] = c.multicam
            clips.append(item)
        tracks.append({"id": t.id, "kind": t.kind, "role": t.role, "name": t.name, "muted": t.muted, "hidden": t.hidden, "locked": t.locked,
                       "clips": clips})
    return {"project": project_id, "canvas": p.canvas.model_dump(), "duration": ms_to_tc(p.duration), "duration_ms": p.duration,
            "length_mode": p.length_mode, "tracks": tracks, "markers": [m.model_dump() for m in p.markers[:200]],
            "captions": p.captions.model_dump(), "issues": validate(p, look),
            **({"multicams": [{"id": g.id, "name": g.name, "master": g.master + 1,
                               "angles": [{"n": i + 1, "media": a.media, "label": a.label, "start_ms": a.start, "audio_only": a.audio_only}
                                          for i, a in enumerate(g.angles)]} for g in p.multicams]} if p.multicams else {})}


def nest(svc: "Services", project_id: str, clip_ids: list[str], name: str = "", *, actor: str = "ui") -> dict[str, Any]:
    """Move the selected clips into a new project and put one sequence clip in their place (one undo step here; the new
    project starts its own history)."""
    p = doc(svc, project_id)
    nested, where = nest_plan(p, clip_ids)
    base = _row(svc, project_id)["name"]
    count = 1 + sum(1 for _, c in p.all_clips() if c.type == "sequence")
    title = clip(name or f"{base} · secuencia {count}", 120)
    new_pid = _insert(svc, title, nested, label="Anidar desde " + clip(base, 60))
    work = clone(p)
    for cid in dict.fromkeys(clip_ids):
        t, c = work.find(cid)
        t.clips.remove(c)
    seq = Clip(type="sequence", media=new_pid, start=where["start"], src_in=0, src_out=where["length"], label=title,
               transition_in=where["transition_in"])
    work.track(where["track"]).clips.append(seq)
    work.sort()
    work = Project.model_validate(work.dump())
    look = media_lookup(svc)
    errors = [i for i in validate(work, look) if i["level"] == "error"]
    if errors:
        delete(svc, new_pid)
        raise LumiereError("Nesting would break the timeline: " + "; ".join(i["message"] for i in errors[:3]))
    rev = save(svc, project_id, work, "Anidar selección", actor)
    svc.emit("lumiere.project.created", {"id": new_pid, "name": clip(title, 80)})
    return {"project": project_id, "rev": rev, "sequence": new_pid, "name": title, "clip": seq.id, "track": where["track"],
            "start": where["start"], "end": where["end"], "moved": len(set(clip_ids))}


def used_by(svc: "Services", project_id: str) -> list[dict[str, Any]]:
    """Projects that nest this one (so the editor can say where a sequence is used)."""
    out = []
    for row in svc.db.query("SELECT id, name, doc FROM projects WHERE id != ?", (project_id,)):
        if project_id in load(json.loads(row["doc"])).sequence_ids():
            out.append({"id": row["id"], "name": row["name"]})
    return out
