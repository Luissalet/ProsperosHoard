"""Filler words and pauses as proposals: a separate document, with a confidence and a reason for each, accepted in bulk.

A proposal names words by their transcript ids and says why it would cut them; it never carries a time that someone invented.
The document records a digest of the transcripts it was made from: if a transcript changes (re-transcribed, a word corrected)
the document expires and must be made again. Accepting applies every chosen proposal as ONE edit (one undo step)."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import commands, media as media_store, projects as project_store
from .analysis import speech
from .errors import LumiereError, NotFound
from .util import ms_to_tc

_SENTENCE_END = re.compile(r"[.!?…]\W*$")
KINDS = ("fillers", "pauses")


def _folder(svc, project_id: str) -> Path:
    return Path(svc.config.data_dir) / "cut_proposals" / project_id


def _path(svc, project_id: str, proposal: str) -> Path:
    if not re.fullmatch(r"cp_[0-9a-f]{8}", proposal):
        raise NotFound(f"No proposal {proposal!r}.")
    return _folder(svc, project_id) / f"{proposal}.json"


def _digest(svc, media: list[str]) -> str:
    h = hashlib.sha256()
    for mid in sorted(media):
        t = media_store.get_analysis(svc, mid, "transcript") or {"words": []}
        h.update(mid.encode())
        for w in t["words"]:
            h.update(f"{w['id']}|{w['text']}|{w['t0']}|{w['t1']}\n".encode())
    return h.hexdigest()[:20]


def _write(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(path)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def filler_confidence(hit: dict[str, Any], words_by_id: dict[str, dict[str, Any]]) -> float:
    base = {"filler": 0.9, "repeat": 0.72}.get(hit["reason"], 0.6)
    probs = [words_by_id[i].get("p") for i in hit["ids"] if words_by_id.get(i) and isinstance(words_by_id[i].get("p"), (int, float))]
    if probs:
        base *= 0.6 + 0.4 * (sum(probs) / len(probs))
    return round(_clamp(base, 0.05, 0.99), 2)


def pause_confidence(gap_ms: int, previous_text: str, min_pause_ms: int) -> tuple[float, str]:
    conf = 0.55 + 0.4 * _clamp((gap_ms - min_pause_ms) / 1800.0, 0.0, 1.0)
    natural = bool(_SENTENCE_END.search(previous_text or ""))
    if natural:
        conf -= 0.15
    return round(_clamp(conf, 0.05, 0.99), 2), ("a pause after the end of a sentence, which is often natural" if natural else "a pause inside a sentence")


def create(svc, project_id: str, *, kinds: tuple[str, ...] = KINDS, media: Optional[str] = None, min_pause_ms: int = 700, keep_pause_ms: int = 250,
           extra: Optional[list[str]] = None, strict: bool = False) -> dict[str, Any]:
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise LumiereError(f"Unknown kinds {bad}: {', '.join(KINDS)}.", code="bad_args")
    p = project_store.doc(svc, project_id)
    mids = [m for m in commands._main_media(p, media) if media_store.get(svc, m)["has_audio"]]
    if not mids:
        raise LumiereError("The main track has no clip with sound to read.", code="no_speech")
    transcripts = commands._need(svc, mids, "transcript", "Cut proposals")
    on_timeline = {w["id"]: w for w in commands.timeline_transcript(svc, p)["words"]}
    items: list[dict[str, Any]] = []
    for mid, t in transcripts.items():
        words = t["words"]
        by_id = {w["id"]: w for w in words}
        fillers: list[dict[str, Any]] = []
        if "fillers" in kinds:
            fillers = speech.find_fillers(words, t.get("language") or "es", extra=extra, repeats=True, strict=strict)
            for n, hit in enumerate(fillers, 1):
                if not any(i in on_timeline for i in hit["ids"]):
                    continue
                label = "filler word" if hit["reason"] == "filler" else "immediate repeat"
                items.append({"kind": "filler", "media": mid, "words": hit["ids"], "text": hit["text"], "confidence": filler_confidence(hit, by_id),
                              "reason": f"{label} «{hit['text']}»", "cut": speech.cut_ranges_for_words(words, set(hit["ids"])), "at": ms_to_tc(on_timeline[hit["ids"][0]]["t"])
                              if hit["ids"][0] in on_timeline else None, "status": "open"})
        taken = {i for h in fillers for i in h["ids"]}
        if "pauses" in kinds:
            for a, b in zip(words, words[1:]):
                gap = b["t0"] - a["t1"]
                if gap < min_pause_ms or a["id"] in taken or b["id"] in taken or a["id"] not in on_timeline or b["id"] not in on_timeline:
                    continue
                conf, nature = pause_confidence(gap, a["text"], min_pause_ms)
                lo, hi = a["t1"] + keep_pause_ms // 2, b["t0"] - keep_pause_ms // 2
                if hi - lo < 120:
                    continue
                items.append({"kind": "pause", "media": mid, "words": [a["id"], b["id"]], "text": f"{a['text']} … {b['text']}", "gap_ms": gap, "confidence": conf,
                              "reason": f"{gap / 1000:.1f} s of silence between «{a['text']}» and «{b['text']}»: {nature}", "cut": [[int(lo), int(hi)]],
                              "at": ms_to_tc(on_timeline[a["id"]]["t1"]), "status": "open"})
    items.sort(key=lambda i: (i["media"], i["cut"][0][0] if i["cut"] else 0))
    counters = {"filler": 0, "pause": 0}
    for item in items:
        counters[item["kind"]] += 1
        item["id"] = ("f" if item["kind"] == "filler" else "p") + str(counters[item["kind"]])
    row = project_store._row(svc, project_id)
    doc = {"id": "cp_" + uuid.uuid4().hex[:8], "project": project_id, "created_rev": row["rev"], "created_ts": time.time(), "media": mids,
           "digest": _digest(svc, mids), "kinds": list(kinds), "params": {"min_pause_ms": min_pause_ms, "keep_pause_ms": keep_pause_ms}, "items": items, "status": "open"}
    _write(_path(svc, project_id, doc["id"]), doc)
    return view(svc, doc, limit=80)


def load(svc, project_id: str, proposal: Optional[str] = None) -> dict[str, Any]:
    if proposal is None:
        files = sorted(_folder(svc, project_id).glob("cp_*.json"), key=lambda f: f.stat().st_mtime, reverse=True) if _folder(svc, project_id).is_dir() else []
        if not files:
            raise NotFound("This project has no cut proposals yet: create them with action=create.")
        path = files[0]
    else:
        path = _path(svc, project_id, proposal)
    if not path.is_file():
        raise NotFound(f"No proposal {proposal!r} in this project.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc["status"] == "open" and _digest(svc, doc["media"]) != doc["digest"]:
        doc["status"] = "expired"
        _write(path, doc)
    if doc["status"] == "open" and any(i["status"] == "accepted" for i in doc["items"]):
        # an accepted cut that was undone puts its words back on the timeline: the proposal is open again
        live = {w["id"] for w in commands.timeline_transcript(svc, project_store.doc(svc, project_id))["words"]}
        reopened = False
        for item in doc["items"]:
            if item["status"] == "accepted" and item["words"][0] in live:
                item["status"] = "open"
                item.pop("accepted_rev", None)
                reopened = True
        if reopened:
            _write(path, doc)
    return doc


def view(svc, doc: dict[str, Any], *, limit: int = 80, min_confidence: Optional[float] = None, kind: Optional[str] = None) -> dict[str, Any]:
    items = [i for i in doc["items"] if (min_confidence is None or i["confidence"] >= min_confidence) and (kind in (None, i["kind"]))]
    counts = {s: sum(1 for i in doc["items"] if i["status"] == s) for s in ("open", "accepted", "rejected")}
    out = {"proposal": doc["id"], "project": doc["project"], "status": doc["status"], "created_revision": doc["created_rev"], "counts": counts,
           "fillers": sum(1 for i in doc["items"] if i["kind"] == "filler"), "pauses": sum(1 for i in doc["items"] if i["kind"] == "pause"),
           "items": [{k: v for k, v in i.items() if k not in ("cut",)} for i in items[:limit]], "shown": min(len(items), limit), "matching": len(items)}
    if doc["status"] == "expired":
        out["expired"] = ("The transcript of this material changed after the proposals were made, so they no longer describe it. "
                          "Make them again with action=create.")
    return out


def accept(svc, project_id: str, proposal: Optional[str] = None, *, ids: Optional[list[str]] = None, min_confidence: Optional[float] = None,
           kind: Optional[str] = None, expected_revision: Optional[int] = None, dry_run: bool = False) -> dict[str, Any]:
    doc = load(svc, project_id, proposal)
    if doc["status"] == "expired":
        raise LumiereError("These proposals expired: the transcript changed after they were made. Make them again with action=create.", code="proposals_expired")
    chosen = [i for i in doc["items"] if i["status"] == "open" and (ids is None or i["id"] in ids) and (min_confidence is None or i["confidence"] >= min_confidence)
              and (kind in (None, i["kind"]))]
    unknown = sorted(set(ids or []) - {i["id"] for i in doc["items"]})
    if unknown:
        raise NotFound(f"No proposals {', '.join(unknown[:10])} in {doc['id']}.")
    if not chosen:
        raise LumiereError("No open proposal matches: nothing to accept.", code="nothing_to_accept")
    p = project_store.doc(svc, project_id)
    live = {w["id"] for w in commands.timeline_transcript(svc, p)["words"]}
    ops_by_media: dict[str, list[list[int]]] = {}
    applied, skipped = [], []
    for item in chosen:
        if not all(w in live for w in item["words"][: (1 if item["kind"] == "filler" else 2)]):
            skipped.append({"id": item["id"], "why": "its words are no longer on the timeline"})
            continue
        ops_by_media.setdefault(item["media"], []).extend(item["cut"])
        applied.append(item["id"])
    if not applied:
        raise LumiereError("None of the chosen proposals is on the timeline any more.", code="nothing_to_accept")
    ops = [{"op": "cut_source", "media": m, "ranges": sorted(r)} for m, r in ops_by_media.items()]
    before = p.duration
    saved = project_store.edit(svc, project_id, ops, label="Aceptar propuestas de corte", actor="agent", base_rev=expected_revision, dry_run=dry_run)
    removed = sum(r.get("removed_ms", 0) for r in saved["results"])
    result = {"proposal": doc["id"], "accepted": applied, "skipped": skipped, "removed_ms": removed, "duration_before": ms_to_tc(before),
              "duration_after": saved["duration"], "revision": saved["rev"], "dry_run": dry_run, "applied": not dry_run}
    if dry_run:
        return {**result, "would_be_revision": saved["would_be_rev"], "changes": saved["changes"]}
    for item in doc["items"]:
        if item["id"] in applied:
            item["status"], item["accepted_rev"] = "accepted", saved["rev"]
    _write(_path(svc, project_id, doc["id"]), doc)
    return {**result, "counts": view(svc, doc, limit=0)["counts"]}


def reject(svc, project_id: str, proposal: Optional[str] = None, *, ids: Optional[list[str]] = None, min_confidence_below: Optional[float] = None) -> dict[str, Any]:
    doc = load(svc, project_id, proposal)
    gone = []
    for item in doc["items"]:
        if item["status"] != "open":
            continue
        if (ids is not None and item["id"] in ids) or (min_confidence_below is not None and item["confidence"] < min_confidence_below):
            item["status"] = "rejected"
            gone.append(item["id"])
    if ids and not gone and set(ids) - {i["id"] for i in doc["items"]}:
        raise NotFound(f"No proposals {', '.join(sorted(set(ids) - {i['id'] for i in doc['items']})[:10])} in {doc['id']}.")
    _write(_path(svc, project_id, doc["id"]), doc)
    return {"proposal": doc["id"], "rejected": gone, "counts": view(svc, doc, limit=0)["counts"]}


def listing(svc, project_id: str) -> dict[str, Any]:
    folder = _folder(svc, project_id)
    out = []
    for f in sorted(folder.glob("cp_*.json"), key=lambda f: f.stat().st_mtime, reverse=True) if folder.is_dir() else []:
        out.append(view(svc, load(svc, project_id, f.stem), limit=0) | {"created_ts": json.loads(f.read_text(encoding="utf-8"))["created_ts"]})
    return {"project": project_id, "proposals": out}
