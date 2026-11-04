"""Continuity bible: decisions that survive versions of an audiovisual production.

One bible per production (or per project) lives in ``data/continuity/<bible_id>/bible.json``:

* ``entries``: the visual identity (palette, style, style references), recurring objects and characters (descriptor
  and reference images), voices (voice id and settings) and timing (fps, size, sample rate). Every entry has a
  revision number and a history. Changing an entry creates a new revision.
* ``shots``: id, time range, description, camera, the entries and references they use and the accepted clip. A shot
  remembers the revision of every entry its clip was made with, so a changed entry marks as stale exactly the shots
  that used it. Shots that did not use it keep their accepted clips.
* ``corrections``: notes tied to a time range and an optional region of a shot, linked to the repair that applied them.
* ``provenance``: which entries (with their revisions) and which reference assets every generated or edited clip used.

``check_consistency`` compares a colour signature of each shot with the signature of the entry's reference. It is a
colour and wording heuristic that reports warnings; it does not establish that a character or an object is the same one.
Pure logic plus ffmpeg for pixels: no FastAPI.
"""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import procutil
from .backend import ffmpeg_path
from .engine import EngineError
from .ids import new_id
from .store import NotFound, Store
from .util import now_iso, write_text_atomic

FORMAT = "prospero.continuity/1"
KINDS = ("identity", "timing", "object", "character", "voice", "style")
ENTRY_FIELDS = ("name", "descriptor", "reference_asset_ids", "palette", "style", "voice_id", "settings",
                "fps", "width", "height", "sample_rate", "channels")
SHOT_FIELDS = ("start_s", "end_s", "description", "camera", "prompt", "references", "uses", "regions")
PROVENANCE_LIMIT = 500
_LOCK = threading.RLock()
DISCLAIMER = ("Heuristic colour-signature and wording check against the bible's references. A warning is a prompt to look "
              "at the shot; no warning does not mean that the character or object is the same.")


def bibles_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "continuity"


def bible_dir(data_dir: Path, bible_id: str) -> Path:
    if not bible_id.startswith("cb_") or not bible_id.replace("_", "").isalnum():
        raise EngineError("bad_bible", f"'{bible_id}' is not a continuity bible id")
    return bibles_dir(data_dir) / bible_id


def _slug(text: str, where: str) -> str:
    out = re.sub(r"[^a-z0-9_]+", "_", str(text or "").strip().lower()).strip("_")[:40]
    if not out:
        raise EngineError("bad_continuity", f"{where} needs an id made of letters, digits or underscores")
    return out


def _save(data_dir: Path, bible: dict[str, Any]) -> None:
    bible["updated_at"] = now_iso()
    write_text_atomic(bible_dir(data_dir, bible["id"]) / "bible.json", json.dumps(bible, ensure_ascii=False, indent=1))


def load(data_dir: Path, bible_id: str) -> dict[str, Any]:
    path = bible_dir(data_dir, bible_id) / "bible.json"
    if not path.is_file():
        raise EngineError("not_found", f"continuity bible {bible_id} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ------------------------------------------------------------------ bible and entries

def create(store: Store, project_id: str, name: str, production: Optional[str] = None,
           identity: Optional[dict[str, Any]] = None, timing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    store.get_project(project_id)
    bible_id = new_id("cb")
    now = now_iso()
    bible = {"format": FORMAT, "id": bible_id, "project_id": project_id, "production": production or None,
             "name": (name or "Continuity bible").strip()[:120], "created_at": now, "revision": 0,
             "entries": {}, "shots": {}, "corrections": [], "provenance": []}
    bible_dir(store.data_dir, bible_id).mkdir(parents=True, exist_ok=True)
    with _LOCK:
        _set_entry(store, bible, "identity", {"kind": "identity", "name": "Visual identity", **(identity or {})}, "created")
        base = {"fps": 25, "width": 1280, "height": 720, "sample_rate": 48000, "channels": 2}
        _set_entry(store, bible, "timing", {"kind": "timing", "name": "Timing", **base, **(timing or {})}, "created")
        _save(store.data_dir, bible)
    return bible


def list_bibles(data_dir: Path, project_id: Optional[str] = None, limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for path in sorted(bibles_dir(data_dir).glob("cb_*/bible.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            bible = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if project_id and bible.get("project_id") != project_id:
            continue
        out.append(view(bible, brief=True))
        if len(out) >= limit:
            break
    return out


def delete_bible(data_dir: Path, bible_id: str) -> dict[str, Any]:
    """Remove a continuity bible (its folder under ``data/continuity``). The clips and reference assets it points at are
    assets of the project and stay; only the decisions, shots, corrections and provenance recorded here go."""
    import shutil
    with _LOCK:
        bible = load(data_dir, bible_id)
        folder = bible_dir(data_dir, bible_id)
        shutil.rmtree(folder)
    return {"deleted": bible_id, "name": bible.get("name"), "project_id": bible.get("project_id"),
            "removed": {"entries": len(bible.get("entries") or {}), "shots": len(bible.get("shots") or {}),
                        "corrections": len(bible.get("corrections") or []), "provenance": len(bible.get("provenance") or [])},
            "kept": {"assets": "the clips and reference images stay in the project"}}


def _clean_fields(store: Store, fields: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(fields) - set(ENTRY_FIELDS) - {"kind"})
    if unknown:
        raise EngineError("bad_continuity", f"unknown entry field(s): {', '.join(unknown)}; allowed: {', '.join(ENTRY_FIELDS)}")
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "kind":
            continue
        if key in ("name", "descriptor", "style", "voice_id"):
            out[key] = str(value or "").strip()[:1000]
        elif key == "reference_asset_ids":
            ids = [str(v) for v in (value or [])][:12]
            for aid in ids:
                store.get_asset(aid)  # NotFound if it does not exist
            out[key] = ids
        elif key == "palette":
            colours = [str(v).strip().lower() for v in (value or [])][:16]
            if not all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in colours):
                raise EngineError("bad_continuity", "palette is a list of #rrggbb colours")
            out[key] = colours
        elif key == "settings":
            if not isinstance(value, dict):
                raise EngineError("bad_continuity", "settings must be an object")
            out[key] = value
        else:  # numeric timing fields
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise EngineError("bad_continuity", f"{key} must be a number") from None
            if not math.isfinite(number) or number <= 0:
                raise EngineError("bad_continuity", f"{key} must be positive")
            out[key] = int(number) if key != "fps" or number == int(number) else number
    return out


def _stale_ids(bible: dict[str, Any]) -> list[str]:
    return [sid for sid, shot in bible["shots"].items() if stale_reasons(bible, shot)]


def _set_entry(store: Store, bible: dict[str, Any], entry_id: str, fields: dict[str, Any], note: str = "") -> dict[str, Any]:
    kind = fields.get("kind") or (bible["entries"].get(entry_id) or {}).get("kind") or "object"
    if kind not in KINDS:
        raise EngineError("bad_continuity", f"kind must be one of {', '.join(KINDS)}")
    clean = _clean_fields(store, fields)
    old = bible["entries"].get(entry_id)
    before = _stale_ids(bible)
    if old is None:
        entry = {"id": entry_id, "kind": kind, "revision": 1, "fields": clean,
                 "history": [{"revision": 1, "at": now_iso(), "note": note or "created", "changes": sorted(clean)}]}
        bible["entries"][entry_id] = entry
        bible["revision"] += 1
        return {"entry_id": entry_id, "revision": 1, "changed": sorted(clean), "created": True, "newly_stale": []}
    merged = {**old["fields"], **clean}
    changes = sorted(k for k in merged if _canon(merged.get(k)) != _canon(old["fields"].get(k)))
    if kind != old["kind"]:
        raise EngineError("bad_continuity", f"entry {entry_id} is a {old['kind']}; its kind cannot change")
    if not changes:
        return {"entry_id": entry_id, "revision": old["revision"], "changed": [], "created": False, "newly_stale": []}
    old["revision"] += 1
    old["fields"] = merged
    old["history"].append({"revision": old["revision"], "at": now_iso(), "note": note or "changed", "changes": changes, "snapshot": merged})
    bible["revision"] += 1
    after = _stale_ids(bible)
    return {"entry_id": entry_id, "revision": old["revision"], "changed": changes, "created": False,
            "newly_stale": [s for s in after if s not in before]}


def upsert_entry(store: Store, bible_id: str, entry_id: str, fields: dict[str, Any], note: str = "") -> dict[str, Any]:
    """Create an entry or change it. A real change makes a new revision and lists the shots that now need regenerating."""
    entry_id = _slug(entry_id, "entry")
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        result = _set_entry(store, bible, entry_id, fields, note)
        _save(store.data_dir, bible)
    result["must_regenerate"] = must_regenerate(bible, only=result["newly_stale"])
    result["kept_accepted"] = accepted_shots(bible)
    return result


def remove_entry(store: Store, bible_id: str, entry_id: str) -> dict[str, Any]:
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        if entry_id in ("identity", "timing"):
            raise EngineError("bad_continuity", f"{entry_id} cannot be removed")
        if entry_id not in bible["entries"]:
            raise EngineError("not_found", f"entry {entry_id} not found")
        before = _stale_ids(bible)
        del bible["entries"][entry_id]
        bible["revision"] += 1
        _save(store.data_dir, bible)
    stale = [s for s in _stale_ids(bible) if s not in before]
    return {"entry_id": entry_id, "removed": True, "newly_stale": stale, "must_regenerate": must_regenerate(bible, only=stale)}


def entry_history(bible: dict[str, Any], entry_id: str) -> list[dict[str, Any]]:
    if entry_id not in bible["entries"]:
        raise EngineError("not_found", f"entry {entry_id} not found")
    return bible["entries"][entry_id]["history"]


# ------------------------------------------------------------------ shots

def _entry_names(bible: dict[str, Any], ids: list[str]) -> list[str]:
    return [bible["entries"][i]["fields"].get("name") or i for i in ids if i in bible["entries"]]


def clip_pieces(shot: dict[str, Any]) -> list[dict[str, Any]]:
    """The shot's time range split by what made each part of its accepted clip.

    A segment repair replaces only a range of the clip: that range was made with the revisions stamped by the repair,
    the rest of the shot keeps the revisions of the original clip. Later repairs cover earlier ones. Without a time
    range or without repairs the whole shot is one piece made with the clip's own revisions."""
    clip = shot.get("clip")
    if not clip:
        return []
    base = {"start_s": shot.get("start_s"), "end_s": shot.get("end_s"), "asset_id": clip.get("asset_id"),
            "operation": clip.get("operation"), "entries": clip.get("entries") or {}, "shot_revision": clip.get("shot_revision"),
            "repaired": False}
    s0, s1 = shot.get("start_s"), shot.get("end_s")
    if s0 is None or s1 is None or not clip.get("segments"):
        return [base]
    pieces = [base]
    for seg in clip["segments"]:
        a, b = max(s0, seg["start_s"]), min(s1, seg["end_s"])
        if b - a <= 1e-6:
            continue
        cut = []
        for p in pieces:
            if p["end_s"] <= a or p["start_s"] >= b:
                cut.append(p)
                continue
            if p["start_s"] < a:
                cut.append({**p, "end_s": a})
            if p["end_s"] > b:
                cut.append({**p, "start_s": b})
        cut.append({"start_s": a, "end_s": b, "asset_id": seg.get("asset_id"), "operation": seg.get("operation") or "segment_repair",
                    "entries": seg.get("entries") or {}, "shot_revision": seg.get("shot_revision"), "repaired": True})
        pieces = sorted(cut, key=lambda p: p["start_s"])
    return pieces


def _piece_reasons(bible: dict[str, Any], shot: dict[str, Any], piece: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = []
    for entry_id, used in sorted(piece["entries"].items()):
        entry = bible["entries"].get(entry_id)
        if entry is None:
            reasons.append({"entry_id": entry_id, "used_revision": used, "current_revision": None, "why": "entry removed"})
        elif entry["revision"] != used:
            reasons.append({"entry_id": entry_id, "name": entry["fields"].get("name") or entry_id, "used_revision": used,
                            "current_revision": entry["revision"], "why": "entry changed"})
    for entry_id in shot.get("uses") or []:  # an entry added to the shot after the clip was made
        if entry_id in bible["entries"] and entry_id not in piece["entries"]:
            reasons.append({"entry_id": entry_id, "used_revision": None, "current_revision": bible["entries"][entry_id]["revision"],
                            "why": "entry added to the shot after the clip"})
    if piece.get("shot_revision") != shot["revision"]:
        reasons.append({"entry_id": None, "why": "shot description, camera, prompt, range or references changed",
                        "used_revision": piece.get("shot_revision"), "current_revision": shot["revision"]})
    return reasons


def stale_reasons(bible: dict[str, Any], shot: dict[str, Any]) -> list[dict[str, Any]]:
    """Why a shot's accepted clip no longer matches the bible (empty when it still does or has no clip).

    Checked per piece of the clip (see ``clip_pieces``): a repair stamped with the current revisions clears the range
    it replaced. When the clip has repairs every reason carries ``ranges_s``, the parts of the shot still made with the
    old revision, and ``repaired_ranges_s``, the parts a repair already brought up to date."""
    pieces = clip_pieces(shot)
    if not pieces:
        return []
    repaired = any(p["repaired"] for p in pieces)
    merged: dict[tuple, dict[str, Any]] = {}
    fresh = [p for p in pieces if p["repaired"] and not _piece_reasons(bible, shot, p)]
    for piece in pieces:
        for reason in _piece_reasons(bible, shot, piece):
            key = (reason["entry_id"], reason["why"], reason["used_revision"])
            got = merged.setdefault(key, {**reason, "ranges_s": []} if repaired else reason)
            if repaired:
                got["ranges_s"].append([piece["start_s"], piece["end_s"]])
    out = list(merged.values())
    if repaired:
        for reason in out:
            reason["ranges_s"] = _join_ranges(reason["ranges_s"])
            reason["repaired_ranges_s"] = _join_ranges([[p["start_s"], p["end_s"]] for p in fresh])
    return out


def _join_ranges(ranges: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([round(a, 4), round(b, 4)])
    return [[round(a, 4), round(b, 4)] for a, b in out]


def must_regenerate(bible: dict[str, Any], only: Optional[list[str]] = None) -> list[dict[str, Any]]:
    out = []
    for sid, shot in bible["shots"].items():
        if only is not None and sid not in only:
            continue
        reasons = stale_reasons(bible, shot)
        if reasons:
            item = {"shot_id": sid, "start_s": shot.get("start_s"), "end_s": shot.get("end_s"),
                    "clip_asset_id": shot["clip"]["asset_id"], "reasons": reasons,
                    "uses": shot["uses"], "references": shot.get("references") or []}
            if any("ranges_s" in r for r in reasons):  # a repaired clip: only these parts still need redoing
                item["stale_ranges_s"] = _join_ranges([rg for r in reasons for rg in r.get("ranges_s", [])])
                item["repaired_ranges_s"] = reasons[0].get("repaired_ranges_s", [])
            out.append(item)
    return out


def accepted_shots(bible: dict[str, Any]) -> list[str]:
    return [sid for sid, shot in bible["shots"].items() if shot.get("clip") and not stale_reasons(bible, shot)]


def upsert_shot(store: Store, bible_id: str, shot_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Create or change a shot. Changing what the shot is (description, camera, prompt, range, references, entries it
    uses) makes only this shot stale when it already has a clip."""
    shot_id = _slug(shot_id, "shot")
    unknown = sorted(set(fields) - set(SHOT_FIELDS))
    if unknown:
        raise EngineError("bad_continuity", f"unknown shot field(s): {', '.join(unknown)}; allowed: {', '.join(SHOT_FIELDS)}")
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        shot = bible["shots"].get(shot_id)
        new = shot is None
        before = _stale_ids(bible)
        if new:
            shot = {"id": shot_id, "revision": 1, "uses": ["identity"], "references": [], "regions": {}, "clip": None,
                    "repairs": [], "created_at": now_iso()}
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if key in ("start_s", "end_s"):
                try:
                    clean[key] = round(float(value), 4)
                except (TypeError, ValueError):
                    raise EngineError("bad_continuity", f"{key} must be a number of seconds") from None
            elif key in ("description", "camera", "prompt"):
                clean[key] = str(value or "").strip()[:2000]
            elif key == "references":
                ids = [str(v) for v in (value or [])][:24]
                for aid in ids:
                    store.get_asset(aid)
                clean[key] = ids
            elif key == "uses":
                ids = [_slug(v, "entry") for v in (value or [])]
                missing = [i for i in ids if i not in bible["entries"]]
                if missing:
                    raise EngineError("bad_continuity", f"shot uses unknown entries: {', '.join(missing)}")
                clean[key] = list(dict.fromkeys(["identity", *ids]))
            elif key == "regions":
                regions = {}
                for eid, box in (value or {}).items():
                    if not (isinstance(box, (list, tuple)) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)
                            and 0 <= box[0] < 1 and 0 <= box[1] < 1 and 0 < box[2] <= 1 and 0 < box[3] <= 1):
                        raise EngineError("bad_continuity", f"regions.{eid} is [x, y, w, h] as fractions of the frame")
                    regions[str(eid)] = [float(v) for v in box]
                clean[key] = regions
        start, end = clean.get("start_s", shot.get("start_s")), clean.get("end_s", shot.get("end_s"))
        if start is not None and end is not None and not 0 <= start < end:
            raise EngineError("bad_continuity", "a shot needs 0 <= start_s < end_s")
        changed = [k for k, v in clean.items() if _canon(v) != _canon(shot.get(k))]
        # regions only steer the colour check; they never invalidate a clip
        material = [k for k in changed if k != "regions"]
        shot.update(clean)
        if material and not new:
            shot["revision"] += 1
        bible["shots"][shot_id] = shot
        if changed:
            bible["revision"] += 1
        _save(store.data_dir, bible)
    stale = [s for s in _stale_ids(bible) if s not in before]
    return {"shot_id": shot_id, "created": new, "changed": changed, "revision": shot["revision"], "newly_stale": stale,
            "must_regenerate": must_regenerate(bible, only=stale)}


def remove_shot(store: Store, bible_id: str, shot_id: str) -> dict[str, Any]:
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        if shot_id not in bible["shots"]:
            raise EngineError("not_found", f"shot {shot_id} not found")
        del bible["shots"][shot_id]
        bible["revision"] += 1
        _save(store.data_dir, bible)
    return {"shot_id": shot_id, "removed": True}


def _record(bible: dict[str, Any], record: dict[str, Any]) -> None:
    bible["provenance"].append({"at": now_iso(), **record})
    del bible["provenance"][:-PROVENANCE_LIMIT]


def _stamp(bible: dict[str, Any], shot: dict[str, Any], extra_entries: Optional[list[str]] = None) -> dict[str, int]:
    ids = list(dict.fromkeys([*shot["uses"], *(extra_entries or [])]))
    return {i: bible["entries"][i]["revision"] for i in ids if i in bible["entries"]}


def accept_clip(store: Store, bible_id: str, shot_id: str, asset_id: str, operation: str = "generate",
                references: Optional[list[str]] = None, entries: Optional[list[str]] = None,
                note: str = "") -> dict[str, Any]:
    """Record a clip as the shot's accepted clip together with the revision of every bible entry and the reference
    assets it was made with (decision provenance). The shot stops being stale."""
    store.get_asset(asset_id)
    refs = [str(r) for r in (references if references is not None else [])][:24]
    for r in refs:
        store.get_asset(r)
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        shot = bible["shots"].get(shot_id)
        if shot is None:
            raise EngineError("not_found", f"shot {shot_id} not found")
        for e in entries or []:
            if e not in bible["entries"]:
                raise EngineError("bad_continuity", f"unknown entry {e}")
        stamp = _stamp(bible, shot, entries)
        refs = list(dict.fromkeys([*refs, *(shot.get("references") or [])]))
        for e in stamp:  # the entry's own reference images were used too
            refs += [r for r in bible["entries"][e]["fields"].get("reference_asset_ids", []) if r not in refs]
        shot["clip"] = {"asset_id": asset_id, "operation": operation, "at": now_iso(), "entries": stamp, "references": refs,
                        "shot_revision": shot["revision"], "bible_revision": bible["revision"], "note": note[:500]}
        _record(bible, {"asset_id": asset_id, "shot_id": shot_id, "operation": operation, "entries": stamp, "references": refs})
        _save(store.data_dir, bible)
    return {"shot_id": shot_id, "asset_id": asset_id, "entries": stamp, "references": refs, "stale": False}


def record_repair(store: Store, bible_id: str, new_asset_id: str, source_asset_id: str, start_s: float, end_s: float,
                  shot_id: Optional[str], references: list[str], entries: Optional[list[str]],
                  correction_id: Optional[str], operation: str = "segment_repair") -> dict[str, Any]:
    """Provenance of a repair: the shots whose range overlaps the repaired range now point at the repaired video.

    When the shot's accepted clip is the repaired source, the repaired range is recorded as a segment of the clip
    stamped with the current revisions: that range stops being stale, while the rest of the shot keeps the revisions
    of the original clip (and stays stale if one of them changed). Repairing every stale range, or accepting the
    repaired video as the whole shot (accept_clip), brings the shot up to date."""
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        touched = []
        resealed: list[str] = []
        for sid, shot in bible["shots"].items():
            if shot.get("start_s") is None or shot.get("end_s") is None:
                continue
            if shot_id and sid != shot_id:
                continue
            if not shot_id and not (shot["start_s"] < end_s and start_s < shot["end_s"]):
                continue
            touched.append(sid)
        stamps: dict[str, int] = {}
        for sid in touched:
            shot = bible["shots"][sid]
            stamps = {**stamps, **_stamp(bible, shot, entries)}
            shot.setdefault("repairs", []).append({"asset_id": new_asset_id, "source_asset_id": source_asset_id,
                                                   "start_s": start_s, "end_s": end_s, "at": now_iso(),
                                                   "entries": _stamp(bible, shot, entries), "references": references})
            if shot.get("clip") and shot["clip"]["asset_id"] == source_asset_id:  # the repaired video is the clip now
                was_stale = bool(stale_reasons(bible, shot))
                segments = [*(shot["clip"].get("segments") or []),
                            {"start_s": round(float(start_s), 4), "end_s": round(float(end_s), 4), "asset_id": new_asset_id,
                             "source_asset_id": source_asset_id, "operation": operation, "at": now_iso(),
                             "entries": _stamp(bible, shot, entries), "shot_revision": shot["revision"]}]
                shot["clip"] = {**shot["clip"], "asset_id": new_asset_id, "operation": "segment_repair", "at": now_iso(),
                                "segments": segments}
                if was_stale and not stale_reasons(bible, shot):
                    resealed.append(sid)
        if correction_id:
            for corr in bible["corrections"]:
                if corr["id"] == correction_id:
                    corr.update({"status": "applied", "applied_asset_id": new_asset_id, "applied_at": now_iso()})
        _record(bible, {"asset_id": new_asset_id, "source_asset_id": source_asset_id, "shots": touched,
                        "operation": operation, "range_s": [start_s, end_s], "entries": stamps, "references": references,
                        "correction_id": correction_id})
        _save(store.data_dir, bible)
    still = must_regenerate(bible, only=touched)
    return {"bible_id": bible_id, "revision": bible["revision"], "shots": touched, "entries": stamps,
            "up_to_date": resealed, "still_stale": [{"shot_id": m["shot_id"], "stale_ranges_s": m.get("stale_ranges_s"),
                                                     "repaired_ranges_s": m.get("repaired_ranges_s")} for m in still]}


def record_regeneration(store: Store, bible_id: str, shot_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Provenance of a regenerated shot: the new take, the prompt, references and revisions it was made with, the
    generator and its jobs, and the repairs that spliced its stale ranges in (those repairs already stamped the clip)."""
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        shot = bible["shots"].get(shot_id)
        if shot is None:
            raise EngineError("not_found", f"shot {shot_id} not found")
        entry = {"at": now_iso(), **{k: v for k, v in record.items() if k != "prompt"}, "prompt": str(record.get("prompt") or "")[:2000]}
        shot.setdefault("regenerations", []).append(entry)
        del shot["regenerations"][:-20]
        _record(bible, {"asset_id": record.get("result_asset_id"), "shot_id": shot_id, "operation": "regenerate",
                        "take_asset_id": record.get("take_asset_id"), "ranges_s": record.get("ranges_s"),
                        "entries": record.get("entries"), "references": record.get("references"),
                        "generator": record.get("generator")})
        _save(store.data_dir, bible)
    still = must_regenerate(bible, only=[shot_id])
    return {"shot_id": shot_id, "up_to_date": not still,
            "still_stale": [{"shot_id": m["shot_id"], "stale_ranges_s": m.get("stale_ranges_s")} for m in still]}


def add_correction(store: Store, bible_id: str, text: str, shot_id: Optional[str] = None, start_s: Optional[float] = None,
                   end_s: Optional[float] = None, region: Optional[list[float]] = None) -> dict[str, Any]:
    with _LOCK:
        bible = load(store.data_dir, bible_id)
        if shot_id and shot_id not in bible["shots"]:
            raise EngineError("not_found", f"shot {shot_id} not found")
        if (start_s is None) != (end_s is None) or (start_s is not None and not 0 <= start_s < end_s):
            raise EngineError("bad_continuity", "a correction segment needs 0 <= start_s < end_s")
        if region is not None and not (len(region) == 4 and all(0 <= float(v) <= 1 for v in region)):
            raise EngineError("bad_continuity", "region is [x, y, w, h] as fractions of the frame")
        corr = {"id": f"k{len(bible['corrections']) + 1}", "text": str(text or "").strip()[:1000], "shot_id": shot_id,
                "start_s": start_s, "end_s": end_s, "region": region, "status": "open", "created_at": now_iso()}
        if not corr["text"]:
            raise EngineError("bad_continuity", "a correction needs text")
        bible["corrections"].append(corr)
        bible["revision"] += 1
        _save(store.data_dir, bible)
    return corr


# ------------------------------------------------------------------ views

def view(bible: dict[str, Any], brief: bool = False) -> dict[str, Any]:
    stale = must_regenerate(bible)
    out = {"bible_id": bible["id"], "name": bible["name"], "project_id": bible["project_id"], "production": bible.get("production"),
           "revision": bible["revision"], "entries": len(bible["entries"]), "shots": len(bible["shots"]),
           "stale_shots": [s["shot_id"] for s in stale], "accepted_shots": accepted_shots(bible),
           "open_corrections": sum(1 for c in bible["corrections"] if c["status"] == "open"),
           "updated_at": bible.get("updated_at")}
    if brief:
        return out
    out["entries"] = {eid: {"kind": e["kind"], "revision": e["revision"], **e["fields"]} for eid, e in bible["entries"].items()}
    out["shots"] = {sid: {k: v for k, v in s.items() if k != "created_at"} | {"stale": bool(stale_reasons(bible, s)),
                    "stale_reasons": stale_reasons(bible, s)} for sid, s in bible["shots"].items()}
    out["must_regenerate"] = stale
    out["corrections"] = bible["corrections"]
    out["provenance"] = bible["provenance"][-40:]
    return out


# ------------------------------------------------------------------ colour signatures and consistency

SIG = 32  # pixels per side the frames are reduced to


def _lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def _signature(frames: np.ndarray) -> dict[str, Any]:
    """frames: (n, h, w, 3) uint8. Mean CIELAB colour plus a coarse RGB histogram (4 bins per channel)."""
    pix = frames.reshape(-1, 3).astype(np.float64)
    lab = _lab(pix).mean(axis=0)
    idx = (pix // 64).astype(int).clip(0, 3)
    hist = np.bincount(idx[:, 0] * 16 + idx[:, 1] * 4 + idx[:, 2], minlength=64).astype(np.float64)
    return {"lab": [round(float(v), 2) for v in lab], "hist": (hist / hist.sum()).round(5).tolist()}


def sample_frames(path: Path, count: int = 5, duration_s: Optional[float] = None, region: Optional[list[float]] = None,
                  size: int = SIG) -> np.ndarray:
    """`count` frames spread over a video (or the single frame of an image), optionally cropped to a region, reduced to size x size."""
    exe = ffmpeg_path()
    if not exe:
        raise EngineError("no_ffmpeg", "ffmpeg is needed to compute colour signatures")
    crop = f"crop=iw*{region[2]:.4f}:ih*{region[3]:.4f}:iw*{region[0]:.4f}:ih*{region[1]:.4f}," if region else ""
    fps = f"fps={count / duration_s:.5f}," if duration_s and duration_s > 0 else ""
    done = procutil.run([exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path), "-an", "-vf",
                         f"{fps}{crop}scale={size}:{size}:flags=area,format=rgb24", "-frames:v", str(count),
                         "-f", "rawvideo", "-"], timeout=300)
    data = np.frombuffer(done.stdout or b"", dtype=np.uint8)
    n = data.size // (size * size * 3)
    if done.returncode != 0 or n == 0:
        raise EngineError("bad_media", f"could not read frames from {path.name}")
    return data[: n * size * size * 3].reshape(n, size, size, 3)


def palette_signature(colours: list[str]) -> dict[str, Any]:
    arr = np.array([[[int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in colours]], dtype=np.uint8)
    return _signature(arr)


def drift(ref: dict[str, Any], other: dict[str, Any]) -> dict[str, float]:
    """CIE76 distance between mean colours and half the L1 distance between the histograms (0 equal .. 1 disjoint)."""
    de = float(np.linalg.norm(np.array(ref["lab"]) - np.array(other["lab"])))
    hd = float(0.5 * np.abs(np.array(ref["hist"]) - np.array(other["hist"])).sum())
    return {"delta_e": round(de, 1), "hist_distance": round(hd, 3)}


COLOUR_WORDS = {"red": "red", "rojo": "red", "roja": "red", "blue": "blue", "azul": "blue", "green": "green", "verde": "green",
                "yellow": "yellow", "amarillo": "yellow", "amarilla": "yellow", "black": "black", "negro": "black",
                "negra": "black", "white": "white", "blanco": "white", "blanca": "white", "orange": "orange",
                "naranja": "orange", "purple": "purple", "morado": "purple", "violeta": "purple", "pink": "pink",
                "rosa": "pink", "brown": "brown", "marron": "brown", "marrón": "brown", "grey": "grey", "gray": "grey",
                "gris": "grey", "gold": "gold", "dorado": "gold", "silver": "silver", "plateado": "silver"}


def _colours_in(text: str) -> set[str]:
    return {COLOUR_WORDS[w] for w in re.findall(r"[a-záéíóúñ]+", (text or "").lower()) if w in COLOUR_WORDS}


def entry_signatures(store: Store, entry: dict[str, Any], size: int = SIG) -> list[dict[str, Any]]:
    sigs = []
    for aid in entry["fields"].get("reference_asset_ids", []):
        try:
            asset = store.get_asset(aid)
            sigs.append({"source": aid, **_signature(sample_frames(store.data_dir / asset["file_path"], 1, size=size))})
        except (NotFound, EngineError, KeyError):
            continue
    if entry["fields"].get("palette"):
        sigs.append({"source": "palette", **palette_signature(entry["fields"]["palette"])})
    return sigs


def check_consistency(store: Store, bible_id: str, shot_ids: Optional[list[str]] = None,
                      delta_e_warn: float = 18.0, hist_warn: float = 0.45) -> dict[str, Any]:
    """Warnings for recurring objects and characters: the colour signature of the shot's clip (inside the region the shot
    declares for the entry, else the whole frame) against the reference images or palette of the entry, plus whether the
    shot's prompt names the entry and repeats its descriptor's colour words. Never claims the same character."""
    bible = load(store.data_dir, bible_id)
    warnings: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    recurring = {eid: e for eid, e in bible["entries"].items() if e["kind"] in ("object", "character")}
    sig_cache = {eid: entry_signatures(store, e) for eid, e in recurring.items()}
    for sid, shot in bible["shots"].items():
        if shot_ids and sid not in shot_ids:
            continue
        for eid in shot["uses"]:
            entry = recurring.get(eid)
            if not entry:
                continue
            fields = entry["fields"]
            text = " ".join([shot.get("prompt") or "", shot.get("description") or ""]).lower()
            name = (fields.get("name") or eid).lower()
            if text.strip() and name not in text and eid not in text:
                warnings.append({"shot_id": sid, "entry_id": eid, "check": "entry_not_named",
                                 "message": f"the prompt of shot {sid} does not name '{fields.get('name') or eid}'"})
            lost = _colours_in(fields.get("descriptor", "")) - _colours_in(text) if text.strip() else set()
            if lost:
                warnings.append({"shot_id": sid, "entry_id": eid, "check": "descriptor_colour_missing",
                                 "message": f"the descriptor of '{fields.get('name') or eid}' says {', '.join(sorted(lost))}; "
                                            f"the prompt of shot {sid} does not repeat it"})
            clip = shot.get("clip")
            if not clip:
                continue
            refs = sig_cache.get(eid) or []
            if not refs:
                warnings.append({"shot_id": sid, "entry_id": eid, "check": "no_reference",
                                 "message": f"'{fields.get('name') or eid}' has no reference image or palette to compare with"})
                continue
            try:
                asset = store.get_asset(clip["asset_id"])
                region = (shot.get("regions") or {}).get(eid)
                frames = sample_frames(store.data_dir / asset["file_path"], 5, asset.get("duration_s") or None, region)
            except (NotFound, EngineError, KeyError) as exc:
                warnings.append({"shot_id": sid, "entry_id": eid, "check": "unreadable_clip", "message": str(exc)})
                continue
            best = min((drift(r, _signature(frames)) | {"reference": r["source"]} for r in refs),
                       key=lambda d: d["delta_e"])
            row = {"shot_id": sid, "entry_id": eid, **best, "region": region or "whole frame", "frames": int(frames.shape[0])}
            rows.append(row)
            if best["delta_e"] >= delta_e_warn or best["hist_distance"] >= hist_warn:
                warnings.append({"shot_id": sid, "entry_id": eid, "check": "colour_drift", "level": "warning",
                                 "delta_e": best["delta_e"], "hist_distance": best["hist_distance"],
                                 "reference": best["reference"],
                                 "message": f"colour of shot {sid} differs from the reference of '{fields.get('name') or eid}' "
                                            f"(delta E {best['delta_e']}, histogram {best['hist_distance']}); look at it"
                                            + ("" if region else "; no region declared, so the background counts too")})
    for w in warnings:
        w.setdefault("level", "warning")
    return {"bible_id": bible_id, "revision": bible["revision"], "level": "warnings_only", "warnings": warnings,
            "measured": rows, "thresholds": {"delta_e": delta_e_warn, "hist_distance": hist_warn}, "note": DISCLAIMER}
