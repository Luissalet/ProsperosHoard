"""The timeline as text, and edits made as exact text replacements.

``render`` writes a project as plain text: a header with its revision, then each track with one line per clip and one per
marker, fields as ``name=value`` separated by `` | ``. Times are whole milliseconds. The text is stable (the same project gives
the same text) so an assistant can edit it the way it edits a source file: with ``{old, new}`` replacements that must each match
exactly once, applied all or nothing, against the revision it read.

``apply`` parses the changed text back, turns the differences into ordinary timeline operations and then proves it: the
operations are run on a copy and the copy's text must be exactly the text that was asked for, or nothing is saved.

What can be changed in text: per clip its start, its source range (``src=in-out``), speed, volume, mute, fades, label and, on titles,
the text and length; moving a line under another track moves the clip; deleting a clip line deletes the clip; per track its name,
role, mute, hidden, locked and volume; deleting a marker line deletes the marker. Everything else (adding clips, effects, keyframes,
the media a clip plays) is done with timeline_edit.
"""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Optional

from . import projects as project_store
from .errors import Conflict, LumiereError, NotFound
from .ops import apply_ops
from .timeline import Clip, Project, validate
from .util import ms_to_tc

FORMAT = "lumiere-timeline-text/v1"
MAX_CHARS = 120_000
_HEADER = ("timeline", "revision", "canvas", "duration", "notes")
_ID = re.compile(r"^[A-Za-z0-9_\-]+$")
_YES = {"yes": True, "no": False}


# ------------------------------------------------------------------------------------------------ writing
def _q(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _num(value: float) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def clip_line(c: Clip, media_name: str = "") -> str:
    if c.type == "text":
        head = f"{c.id} text"
        fields = [f"start={c.start}", f"end={c.end}", f"length={c.length}", f"text={_q(c.text)}"]
    else:
        head = f"{c.id} {c.type} {c.media} {_q(media_name)}"
        speed = "ramp" if c.speed_keys else _num(c.speed)
        fields = [f"start={c.start}", f"end={c.end}", f"src={c.src_in}-{c.src_out}", f"speed={speed}"]
        if c.reverse:
            fields.append("reverse=yes")
    fields += [f"volume={_num(c.volume_db)}", f"mute={_yn(c.mute)}", f"fade={c.fade_in}/{c.fade_out}",
               f"afade={c.audio_fade_in}/{c.audio_fade_out}", f"label={_q(c.label)}"]
    return " | ".join([head] + fields)


def track_line(t) -> str:
    return " | ".join([f"track {t.id}", f"kind={t.kind}", f"role={t.role}", f"name={_q(t.name)}", f"muted={_yn(t.muted)}",
                       f"hidden={_yn(t.hidden)}", f"locked={_yn(t.locked)}", f"volume={_num(t.volume_db)}"])


def header_lines(p: Project, project_id: str, name: str, rev: int) -> list[str]:
    return [f"timeline: {project_id} {_q(name)}", f"revision: {rev}",
            f"canvas: {p.canvas.width}x{p.canvas.height} {_num(p.canvas.fps)}fps background={p.canvas.background} length_mode={p.length_mode}",
            f"duration: {p.duration}", f"notes: {_q(p.notes)}"]


def render(svc, project_id: str, *, track: Optional[str] = None, max_chars: int = MAX_CHARS) -> dict[str, Any]:
    row = project_store._row(svc, project_id)
    p = project_store.doc(svc, project_id)
    look = project_store.media_lookup(svc)
    lines = [f"# {FORMAT}. Times are whole milliseconds. Edit with timeline_apply_text: exact {{old,new}} replacements against `revision`.",
             "# end= is derived (start + length): change src, speed, length or start instead."] + header_lines(p, project_id, row["name"], row["rev"])
    for t in p.tracks:
        if track and track not in (t.id, t.kind, t.name):
            continue
        lines.append("")
        lines.append(track_line(t))
        for c in sorted(t.clips, key=lambda c: (c.start, c.id)):
            name = ""
            if c.type != "text" and c.media:
                name = (look(c.media) or {}).get("name") or ""
            lines.append("  " + clip_line(c, name) + f"   # {ms_to_tc(c.start)} to {ms_to_tc(c.end)}")
    if p.markers and not track:
        lines.append("")
        for m in sorted(p.markers, key=lambda m: (m.t, m.id)):
            lines.append(" | ".join([f"marker {m.id}", f"t={m.t}", f"kind={m.kind}", f"label={_q(m.label)}"]))
    text = "\n".join(lines) + "\n"
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n"
    return {"project": project_id, "name": row["name"], "revision": row["rev"], "format": FORMAT, "duration_ms": p.duration, "tracks": len(p.tracks),
            "clips": sum(len(t.clips) for t in p.tracks), "markers": len(p.markers), "text": text, "truncated": truncated,
            **({"hint": "Narrow with track=, or use timeline_get with start/end."} if truncated else {})}


# ------------------------------------------------------------------------------------------------ reading
def _fields(line: str) -> list[str]:
    """Split on `` | `` outside JSON strings."""
    parts, buf, quoted, escaped = [], [], False, False
    i = 0
    while i < len(line):
        ch = line[i]
        if quoted:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
            buf.append(ch)
        elif ch == "|":
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return parts


def _strip_comment(line: str) -> str:
    quoted, escaped = False, False
    for i, ch in enumerate(line):
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


class TextError(LumiereError):
    code = "timeline_text"


def _value(key: str, raw: str, number: int) -> Any:
    try:
        if raw.startswith('"'):
            return json.loads(raw)
        if key == "src":
            a, b = raw.split("-")
            return (int(a), int(b))
        if key in ("fade", "afade"):
            a, b = raw.split("/")
            return (int(a), int(b))
        if key in ("mute", "reverse", "muted", "hidden", "locked"):
            return _YES[raw]
        if key == "speed" and raw == "ramp":
            return "ramp"
        if key in ("kind", "role") and re.fullmatch(r"[a-z_]+", raw):
            return raw
        if key in ("volume", "speed"):
            return float(raw)
        if key in ("start", "end", "length", "t"):
            return int(raw)
    except (ValueError, KeyError, json.JSONDecodeError):
        pass
    raise TextError(f"Line {number}: {key}={raw} is not a valid value. Times are whole ms (start=5000); src=in-out; fade=in/out; "
                    f"mute, hidden, locked: yes or no; text and label are JSON strings.")


_CLIP_KEYS = {"start", "end", "src", "speed", "reverse", "volume", "mute", "fade", "afade", "label", "length", "text"}
_TRACK_KEYS = {"kind", "role", "name", "muted", "hidden", "locked", "volume"}
_MARKER_KEYS = {"t", "kind", "label"}


class Parsed:
    def __init__(self) -> None:
        self.header: dict[str, str] = {}
        self.tracks: dict[str, dict[str, Any]] = {}
        self.clips: dict[str, dict[str, Any]] = {}
        self.markers: dict[str, dict[str, Any]] = {}


def parse(text: str) -> Parsed:
    out = Parsed()
    track: Optional[str] = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        key = line.split(":", 1)[0]
        if key in _HEADER and line.startswith(key + ":"):
            out.header[key] = line
            continue
        parts = _fields(line)
        head = parts[0].split(None, 2)
        if head[0] in ("track", "marker"):
            if len(head) < 2 or not _ID.match(head[1]):
                raise TextError(f"Line {number}: {head[0]} needs an id.")
            fields = {}
            for field in parts[1:]:
                name, sep, raw_value = field.partition("=")
                name = name.strip()
                allowed = _TRACK_KEYS if head[0] == "track" else _MARKER_KEYS
                if not sep or name not in allowed:
                    raise TextError(f"Line {number}: unknown field {name!r} on a {head[0]}. Fields: {', '.join(sorted(allowed))}.")
                fields[name] = _value(name, raw_value.strip(), number)
            target = out.tracks if head[0] == "track" else out.markers
            if head[1] in target:
                raise TextError(f"Line {number}: {head[0]} {head[1]} appears twice.")
            target[head[1]] = {"fields": fields, "line": number}
            if head[0] == "track":
                track = head[1]
            continue
        # a clip line: "<id> text" or "<id> media <media id> "name"" first
        pieces = parts[0].split(None, 3)
        if len(pieces) < 2 or not _ID.match(pieces[0]) or pieces[1] not in ("text", "media", "sequence"):
            raise TextError(f"Line {number}: not a header, track, marker or clip line: {raw.strip()[:80]!r}.")
        if track is None:
            raise TextError(f"Line {number}: a clip line must come under a track line.")
        if pieces[0] in out.clips:
            raise TextError(f"Line {number}: clip {pieces[0]} appears twice.")
        fields = {}
        for field in parts[1:]:
            name, sep, raw_value = field.partition("=")
            name = name.strip()
            if not sep or name not in _CLIP_KEYS:
                raise TextError(f"Line {number}: unknown field {name!r} on a clip. Fields: {', '.join(sorted(_CLIP_KEYS))}.")
            fields[name] = _value(name, raw_value.strip(), number)
        out.clips[pieces[0]] = {"kind": pieces[1], "ref": pieces[2] if len(pieces) > 2 else "", "fields": fields, "track": track, "line": number}
    return out


# ------------------------------------------------------------------------------------------------ replacements
def replace_exact(text: str, replacements: list[dict[str, str]]) -> str:
    """Apply ``{old, new}`` in order, each ``old`` matching exactly once in the text as it stands; all or nothing."""
    if not replacements:
        raise LumiereError("No replacements.", code="bad_args")
    for index, rep in enumerate(replacements, 1):
        old, new = rep.get("old", ""), rep.get("new", "")
        where = f"Replacement {index}"
        if not old:
            raise TextError(f"{where}: old is empty.")
        if old == new:
            raise TextError(f"{where}: old and new are the same.")
        count = text.count(old)
        if count == 0:
            near = difflib.get_close_matches(old.strip().splitlines()[0], text.splitlines(), n=1, cutoff=0.5)
            raise TextError(f"{where}: old was not found in the text." + (f" Closest line: {near[0].strip()!r}." if near else "")
                            + " Nothing was changed. Read the text again (timeline_text): it may have changed.")
        if count > 1:
            raise TextError(f"{where}: old appears {count} times; add more of the line (its clip id) so it matches once. Nothing was changed.")
        text = text.replace(old, new, 1)
    return text


# ------------------------------------------------------------------------------------------------ text -> operations
def plan(current: Parsed, wanted: Parsed, *, ripple: bool = False) -> list[dict[str, Any]]:
    """The operations that take the project of ``current`` to ``wanted``; refuses what text cannot express."""
    for key in _HEADER:
        if current.header.get(key) != wanted.header.get(key):
            raise TextError(f"The header line {key!r} is read-only (it describes the project, not the edit); put it back as it was." if key in wanted.header
                            else f"The header line {key!r} is missing: the header must stay as it was.")
    for tid in set(wanted.tracks) - set(current.tracks):
        raise TextError(f"Track {tid} does not exist: tracks are added with timeline_edit track_add.")
    for tid in set(current.tracks) - set(wanted.tracks):
        raise TextError(f"Track {tid} was removed from the text: tracks are deleted with timeline_edit track_delete.")
    deletes, moves, sets, tracks, markers = [], [], [], [], []
    for tid, cur in current.tracks.items():
        new = wanted.tracks[tid]
        changes = {}
        for key, value in new["fields"].items():
            if value == cur["fields"].get(key):
                continue
            if key == "kind":
                raise TextError(f"Track {tid}: kind cannot change.")
            changes["volume_db" if key == "volume" else key] = value
        if changes:
            tracks.append({"op": "track_set", "track": tid, "props": changes})
    for mid in set(wanted.markers) - set(current.markers):
        raise TextError(f"Marker {mid} does not exist: markers are added with timeline_edit marker_add.")
    for mid, cur in current.markers.items():
        if mid not in wanted.markers:
            markers.append({"op": "marker_delete", "id": mid})
        elif wanted.markers[mid]["fields"] != cur["fields"]:
            raise TextError(f"Marker {mid}: only deleting a marker line is possible in text; change a marker with timeline_edit "
                            "(marker_delete then marker_add).")
    for cid in set(wanted.clips) - set(current.clips):
        raise TextError(f"Clip {cid} does not exist: clips are added with timeline_edit (add_media, add_text, add_overlay).")
    for cid, cur in current.clips.items():
        new = wanted.clips.get(cid)
        if new is None:
            deletes.append(cid)
            continue
        if (new["kind"], new["ref"].split(None, 1)[0] if new["ref"] else "") != (cur["kind"], cur["ref"].split(None, 1)[0] if cur["ref"] else ""):
            raise TextError(f"Clip {cid}: the kind and the media it plays are read-only here (replace_media in timeline_edit).")
        nf, cf = new["fields"], cur["fields"]
        if nf.get("end", cf.get("end")) != cf.get("end"):
            raise TextError(f"Clip {cid}: end is derived (start + length). Change start, src, speed or length.")
        props: dict[str, Any] = {}
        for key, value in nf.items():
            if key == "end" or value == cf.get(key):
                continue
            if key == "src":
                props["src_in"], props["src_out"] = value
            elif key == "fade":
                props["fade_in"], props["fade_out"] = value
            elif key == "afade":
                props["audio_fade_in"], props["audio_fade_out"] = value
            elif key == "volume":
                props["volume_db"] = value
            elif key == "speed":
                if value == "ramp":
                    raise TextError(f"Clip {cid}: speed=ramp is read-only; give a number to replace the curve with a constant speed.")
                props["speed"], props["speed_keys"] = value, []
            elif key == "reverse":
                raise TextError(f"Clip {cid}: reverse cannot be changed in text; use timeline_edit set.")
            elif key in ("start", "length", "text", "label", "mute"):
                props[key] = value
        if new["track"] != cur["track"]:
            moves.append({"op": "move", "clip": cid, "track": new["track"], **({"start": props.pop("start")} if "start" in props else {})})
        if props:
            sets.append({"op": "set", "clip": cid, "props": props, "ripple": ripple})
    ops: list[dict[str, Any]] = []
    if deletes:
        ops.append({"op": "delete", "clips": deletes, "ripple": ripple})
    return ops + moves + sets + tracks + markers


def _signature(p: Project) -> dict[str, Any]:
    out: dict[str, Any] = {"tracks": {}, "clips": {}, "markers": {}}
    for t in p.tracks:
        out["tracks"][t.id] = (t.kind, t.role, t.name, t.muted, t.hidden, t.locked, round(t.volume_db, 6))
        for c in t.clips:
            out["clips"][c.id] = (t.id, c.type, c.start, c.end, c.length, c.text, c.src_in, c.src_out, round(c.speed, 6), bool(c.speed_keys), c.reverse,
                                  round(c.volume_db, 6), c.mute, c.fade_in, c.fade_out, c.audio_fade_in, c.audio_fade_out, c.label)
    for m in p.markers:
        out["markers"][m.id] = (m.t, m.kind, m.label)
    return out


def changes(before: Project, after: Project) -> dict[str, Any]:
    """Which clips, tracks and markers differ: ids only, plus the fields of a clip that changed."""
    a, b = _signature(before), _signature(after)
    names = ("track", "type", "start", "end", "length", "text", "src_in", "src_out", "speed", "ramp", "reverse", "volume_db", "mute", "fade_in", "fade_out",
             "audio_fade_in", "audio_fade_out", "label")
    changed = {}
    for cid in a["clips"].keys() & b["clips"].keys():
        if a["clips"][cid] != b["clips"][cid]:
            changed[cid] = {n: [x, y] for n, x, y in zip(names, a["clips"][cid], b["clips"][cid]) if x != y}
    return {"clips_removed": sorted(a["clips"].keys() - b["clips"].keys()), "clips_added": sorted(b["clips"].keys() - a["clips"].keys()),
            "clips_changed": dict(sorted(changed.items())), "tracks_changed": sorted(t for t in a["tracks"].keys() & b["tracks"].keys() if a["tracks"][t] != b["tracks"][t]),
            "markers_removed": sorted(a["markers"].keys() - b["markers"].keys()), "duration_ms": [before.duration, after.duration]}


# ------------------------------------------------------------------------------------------------ apply / check
def _revision(svc, project_id: str, expected: Optional[int]) -> int:
    row = project_store._row(svc, project_id)
    if expected is not None and expected != row["rev"]:
        raise Conflict(f"The project changed (revision {row['rev']}, you read {expected}). Read it again with timeline_text and repeat the edit.")
    return row["rev"]


def prepare(svc, project_id: str, *, replacements: Optional[list[dict[str, str]]] = None, text: Optional[str] = None,
            expected_revision: Optional[int] = None, ripple: bool = False) -> dict[str, Any]:
    """Everything an apply would do, without saving: the operations, the resulting project and what differs."""
    if (replacements is None) == (text is None):
        raise LumiereError("Give either replacements or text.", code="bad_args")
    rev = _revision(svc, project_id, expected_revision)
    before = project_store.doc(svc, project_id)
    rendered = render(svc, project_id, max_chars=10 ** 9)
    if text is None:
        text = replace_exact(rendered["text"], replacements)
    current, wanted = parse(rendered["text"]), parse(text)
    ops = plan(current, wanted, ripple=ripple)
    if not ops:
        return {"project": project_id, "revision": rev, "ops": [], "changes": changes(before, before), "no_change": True, "text": text}
    look = project_store.media_lookup(svc)
    after, results = apply_ops(before, ops, look, project_id=project_id, docs=project_store.doc_lookup(svc))
    # What was written must be what is there: the operations may have moved other things (overlaps, snapping).
    reread = parse(_render_project(after, project_id, rendered, svc))
    for cid, want in wanted.clips.items():
        got = reread.clips.get(cid)
        for key, value in want["fields"].items():
            if key == "end":
                continue
            if got is None or got["fields"].get(key) != value or got["track"] != want["track"]:
                have = None if got is None else got["fields"].get(key)
                raise TextError(f"Clip {cid}: {key}={value} could not be applied exactly (it would be {have}). Nothing was saved. "
                                f"{'Another clip is in the way, or the media is shorter.' if key in ('start', 'src') else ''}".strip())
    issues = validate(after, look)
    known = {(i["code"], i.get("clip")) for i in validate(before, look)}
    worse = [i for i in issues if (i["code"], i.get("clip")) not in known and (i["level"] == "error" or i["code"] == "past_end")]
    if worse:
        raise TextError("The edit would leave the timeline with problems, so nothing was saved: " + " ".join(i["message"] for i in worse[:4]))
    return {"project": project_id, "revision": rev, "ops": ops, "results": results, "changes": changes(before, after), "duration_ms": after.duration,
            "duration": ms_to_tc(after.duration), "issues": issues, "text": text}


def _render_project(p: Project, project_id: str, rendered: dict[str, Any], svc) -> str:
    row = project_store._row(svc, project_id)
    look = project_store.media_lookup(svc)
    lines = header_lines(p, project_id, row["name"], row["rev"])
    for t in p.tracks:
        lines.append(track_line(t))
        for c in sorted(t.clips, key=lambda c: (c.start, c.id)):
            lines.append("  " + clip_line(c, (look(c.media) or {}).get("name", "") if c.type != "text" and c.media else ""))
    return "\n".join(lines) + "\n"


def apply(svc, project_id: str, *, replacements: Optional[list[dict[str, str]]] = None, text: Optional[str] = None, expected_revision: int,
          dry_run: bool = False, ripple: bool = False, label: str = "") -> dict[str, Any]:
    done = prepare(svc, project_id, replacements=replacements, text=text, expected_revision=expected_revision, ripple=ripple)
    done.pop("text", None)
    if done.get("no_change"):
        return {**done, "applied": False}
    if dry_run:
        return {**done, "applied": False, "dry_run": True, "would_be_revision": done["revision"] + 1}
    saved = project_store.edit(svc, project_id, done["ops"], label=label or "Montaje como texto", actor="agent", base_rev=expected_revision)
    return {**done, "applied": True, "revision": saved["rev"], "results": saved["results"], "issues": saved["issues"], "duration_ms": saved["duration_ms"]}


def check(svc, project_id: str, *, replacements: Optional[list[dict[str, str]]] = None, ops: Optional[list[dict[str, Any]]] = None,
          expected_revision: Optional[int] = None, ripple: bool = False) -> dict[str, Any]:
    """A dry run that reports instead of raising: would this edit apply to the revision, and what would it do."""
    try:
        if (replacements is None) == (ops is None):
            raise LumiereError("Give either replacements (text edit) or ops (timeline_edit).", code="bad_args")
        if replacements is not None:
            done = prepare(svc, project_id, replacements=replacements, expected_revision=expected_revision, ripple=ripple)
            done.pop("text", None)
            return {"ok": True, **done, "applied": False}
        rev = _revision(svc, project_id, expected_revision)
        done = project_store.edit(svc, project_id, ops, base_rev=expected_revision, dry_run=True)
        return {"ok": True, **done, "revision": rev}
    except (LumiereError, NotFound, Conflict) as error:
        return {"ok": False, "error": str(error), "code": getattr(error, "code", error.__class__.__name__.lower()), "applied": False}
