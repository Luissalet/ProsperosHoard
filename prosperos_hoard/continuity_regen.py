"""Regenerate what a continuity bible marks as stale, without redoing good material.

``plan`` lists, for every stale shot, what a new take must be made with: the shot's prompt and camera plus the current
revision of every entry it uses (descriptor, style, palette), the reference images (the shot's own and its entries'),
the start image when one is given, and the ranges that are still stale (``stale_ranges_s``: after a partial repair only
those, else the whole shot).

``apply_clip`` takes a new take of the shot (made by the studio's generators here, or anywhere else) and splices only
the stale ranges of it into the shot's accepted clip with segment repairs, so the parts that were already up to date keep
their frames; each repair is stamped with the current revisions, so the shot stops being stale, and the regeneration is
recorded in the bible's provenance (take, prompt, references, revisions, generator jobs). ``run_job`` does both for a
list of shots, asking ``generate(item, progress)`` (given by the API layer: a still, then a clip, through the normal
render queue) for each take. Nothing is accepted without a take: a shot whose take fails stays stale and says why.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import continuity as continuity_mod
from . import segment_repair
from .engine import EngineError
from .store import NotFound, Store
from .util import now_iso

Generate = Callable[[dict[str, Any], Callable[..., None]], dict[str, Any]]
_SKIP = ("identity", "timing")


def _image_refs(store: Store, ids: list[str]) -> list[str]:
    out = []
    for aid in ids:
        try:
            if store.get_asset(aid)["kind"] == "image" and aid not in out:
                out.append(aid)
        except NotFound:
            continue
    return out


def _prompt(bible: dict[str, Any], shot: dict[str, Any]) -> str:
    base = (shot.get("prompt") or shot.get("description") or "").strip()
    parts = [base] if base else []
    for eid in shot.get("uses") or []:
        entry = bible["entries"].get(eid)
        if not entry or eid in _SKIP or entry["kind"] == "voice":
            continue
        f = entry["fields"]
        desc = ", ".join(x for x in (f.get("descriptor"), f.get("style")) if x)
        if desc:
            parts.append(f"{f.get('name') or eid}: {desc}")
    ident = (bible["entries"].get("identity") or {}).get("fields") or {}
    if ident.get("style"):
        parts.append(f"style: {ident['style']}")
    if ident.get("palette"):
        parts.append("palette " + " ".join(ident["palette"]))
    if shot.get("camera"):
        parts.append(f"camera: {shot['camera']}")
    return ". ".join(p.rstrip(". ") for p in parts if p)[:2000]


def _complement(ranges: list[list[float]], start: float, end: float) -> list[list[float]]:
    out, cur = [], start
    for a, b in sorted(ranges):
        if a - cur > 1e-6:
            out.append([round(cur, 4), round(a, 4)])
        cur = max(cur, b)
    if end - cur > 1e-6:
        out.append([round(cur, 4), round(end, 4)])
    return out


def plan(store: Store, bible_id: str, shot_ids: Optional[list[str]] = None) -> dict[str, Any]:
    """What each stale shot's new take must be made with, and which ranges of it will be used."""
    bible = continuity_mod.load(store.data_dir, bible_id)
    if shot_ids:
        missing = [s for s in shot_ids if s not in bible["shots"]]
        if missing:
            raise EngineError("not_found", f"shot(s) not found: {', '.join(missing)}")
    items = []
    for m in continuity_mod.must_regenerate(bible, only=shot_ids or None):
        sid = m["shot_id"]
        shot = bible["shots"][sid]
        item: dict[str, Any] = {"shot_id": sid, "project_id": bible["project_id"], "clip_asset_id": shot["clip"]["asset_id"],
                                "reasons": m["reasons"]}
        if shot.get("start_s") is None or shot.get("end_s") is None:
            items.append({**item, "error": "the shot has no time range: give it start_s and end_s first"})
            continue
        start, end = float(shot["start_s"]), float(shot["end_s"])
        ranges = m.get("stale_ranges_s") or [[start, end]]
        refs = list(shot.get("references") or [])
        for eid in shot.get("uses") or []:
            entry = bible["entries"].get(eid)
            if entry:
                refs += [r for r in entry["fields"].get("reference_asset_ids") or [] if r not in refs]
        images = _image_refs(store, refs)
        span = [min(r[0] for r in ranges), max(r[1] for r in ranges)]
        items.append({**item, "start_s": start, "end_s": end, "ranges_s": ranges, "take_span_s": span,
                      "seconds": round(span[1] - span[0], 3), "prompt": _prompt(bible, shot), "references": images,
                      "start_image": images[0] if images else None,
                      "entries": continuity_mod._stamp(bible, shot),
                      "kept_ranges_s": _complement(ranges, start, end)})
    return {"bible_id": bible_id, "items": items, "count": len(items),
            "note": "Each take is spliced into the shot only over its stale ranges; what is already up to date is kept."}


def apply_clip(store: Store, bible_id: str, shot_id: str, clip_asset_id: str, *, clip_in_s: float = 0.0,
               ranges: Optional[list[list[float]]] = None, transition: str = "cut", crossfade_s: float = 0.3,
               generator: Optional[dict[str, Any]] = None, progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """Splice the stale ranges of a new take into the shot's accepted clip. ``clip_in_s`` is where the take's first
    stale range starts inside the take (a take made for the stale span starts at 0); later ranges keep their distance."""
    progress = progress or (lambda *a, **k: None)
    if store.get_asset(clip_asset_id)["kind"] != "video":
        raise EngineError("bad_regenerate", "the take must be a video asset")
    planned = next((i for i in plan(store, bible_id, [shot_id])["items"]), None)
    if planned is None:
        raise EngineError("not_stale", f"shot {shot_id} is not stale: nothing to regenerate")
    if planned.get("error"):
        raise EngineError("bad_regenerate", planned["error"])
    use = [[float(a), float(b)] for a, b in (ranges or planned["ranges_s"])]
    origin = min(a for a, _ in use)
    repairs = []
    for i, (a, b) in enumerate(sorted(use)):
        bible = continuity_mod.load(store.data_dir, bible_id)
        current = bible["shots"][shot_id]["clip"]["asset_id"]
        progress(0.1 + 0.7 * i / len(use), f"splicing {a:.2f}-{b:.2f} s of the new take")
        res = segment_repair.repair_segment(store, current, clip_asset_id, a, b, shot_id=shot_id, bible_id=bible_id,
                                            transition=transition, crossfade_s=crossfade_s,
                                            replacement_in_s=max(0.0, float(clip_in_s) + a - origin),
                                            references=planned["references"], review=False,
                                            name=f"{shot_id} regenerated {a:.1f}-{b:.1f} s")
        repairs.append({"range_s": res["range_s"], "asset_id": res["asset_id"], "exact_outside": res["exact_outside"],
                        "notes": res["notes"]})
    final = repairs[-1]["asset_id"]
    progress(0.85, "reviewing the seams")
    total = float(store.get_asset(final).get("duration_s") or 0)
    seams = sorted({t for a, b in use for t in (a, b) if 0 < t < total - 1e-3})
    review = segment_repair.review_join(store, final, seams) if seams else None
    record = continuity_mod.record_regeneration(store, bible_id, shot_id, {
        "take_asset_id": clip_asset_id, "result_asset_id": final, "ranges_s": use, "prompt": planned["prompt"],
        "references": planned["references"], "entries": planned["entries"], "generator": generator or {"kind": "given"},
        "repairs": [r["asset_id"] for r in repairs]})
    progress(1.0, "done")
    return {"shot_id": shot_id, "take_asset_id": clip_asset_id, "result_asset_id": final, "repairs": repairs,
            "kept_ranges_s": planned["kept_ranges_s"], "join_review": review, "up_to_date": record["up_to_date"],
            "still_stale": record["still_stale"]}


def run_job(store: Store, job: dict[str, Any], progress: Callable[..., None], generate: Optional[Generate]) -> dict[str, Any]:
    """Orchestrator job: a new take per stale shot through ``generate`` (it returns {asset_id, jobs, kind, ...}), then
    ``apply_clip`` of its stale ranges. One shot failing does not stop the others; each says what happened."""
    p = job["params"]
    if generate is None:
        raise EngineError("generator_unavailable", "no generator is available to make the new takes")
    items = [i for i in plan(store, p["bible_id"], p.get("shot_ids") or None)["items"]]
    results, asset_ids = [], []
    for k, item in enumerate(items):
        base = k / max(1, len(items))
        step = lambda f, msg=None, b=base: progress(b + f / max(1, len(items)), f"{item['shot_id']}: {msg or ''}")  # noqa: E731
        if item.get("error"):
            results.append({"shot_id": item["shot_id"], "ok": False, "error": item["error"]})
            continue
        try:
            step(0.0, "making the new take")
            take = generate(item, step)
            out = apply_clip(store, p["bible_id"], item["shot_id"], take["asset_id"], transition=p.get("transition") or "cut",
                             generator={k2: v for k2, v in take.items() if k2 != "asset_id"},
                             progress=lambda f, msg=None: step(0.5 + f / 2, msg))
            results.append({"ok": True, **out})
            asset_ids += [take["asset_id"], out["result_asset_id"]]
        except Exception as exc:  # noqa: BLE001 - the next shot still runs; this one stays stale
            code = getattr(exc, "code", type(exc).__name__)
            results.append({"shot_id": item["shot_id"], "ok": False, "error": f"{code}: {exc}"})
    progress(1.0, "done")
    return {"asset_ids": asset_ids, "regenerate": {"bible_id": p["bible_id"], "results": results,
                                                    "done": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"]),
                                                    "finished_at": now_iso()}}


def run_apply_job(store: Store, job: dict[str, Any], progress: Callable[..., None]) -> dict[str, Any]:
    p = job["params"]
    out = apply_clip(store, p["bible_id"], p["shot_id"], p["clip_asset_id"], clip_in_s=float(p.get("clip_in_s") or 0),
                     ranges=p.get("ranges"), transition=p.get("transition") or "cut", crossfade_s=p.get("crossfade_s", 0.3),
                     progress=progress)
    return {"asset_id": out["result_asset_id"], "asset_ids": [out["result_asset_id"]], "regenerate": out}
