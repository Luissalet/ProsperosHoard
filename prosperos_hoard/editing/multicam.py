"""Multicam editing: sync recordings of one event by their sound, build a group on the timeline, switch angles and cut between
them automatically by who is speaking.

Representation (see ``timeline.Multicam``): the picture is ordinary clips of the angle media on the main track, muted, each
tagged with the group id; the sound is one continuous clip of the master recording on its own audio track. A switch only
re-points picture clips, so the sound never jumps, renders and text-based editing treat everything as plain clips, and every
change is one undo step like any other edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from . import commands
from . import media as media_store
from . import projects as project_store
from .analysis import audio as audio_an
from .analysis import multicam as mc
from .errors import LumiereError, NotFound
from .ops import _angle_ref, _group_for, apply_ops
from .timeline import Multicam, Project
from .util import ms_to_tc

if TYPE_CHECKING:
    from .services import Services


# ---------------------------------------------------------------- sync

def levels_for(svc: "Services", media_id: str) -> np.ndarray:
    """10 ms sound levels (dB) of a media: the waveform job's cache, or computed now when it is not there yet."""
    lv = media_store.rms(svc, media_id)
    if lv is not None and lv.size:
        return lv
    info = media_store.get(svc, media_id)
    if not info["has_audio"]:
        raise LumiereError(f"{info['name']} has no sound: multicam sync matches recordings by their audio.")
    _, levels = audio_an.envelope(svc.tools(), Path(info["path"]), duration_ms=info["duration_ms"])
    return levels


def sync(svc: "Services", media_ids: list[str], *, reference: Optional[str] = None, offsets: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Offsets of recordings of the same event. Each angle gets ``start_ms`` (group time of its own time 0; the earliest is 0),
    ``confidence`` and ``correlation``; ``offsets`` {media: start_ms} overrides the measured values by hand."""
    if len(media_ids) < 2:
        raise LumiereError("Sync needs at least two recordings.")
    if len(set(media_ids)) != len(media_ids):
        raise LumiereError("Each recording once.")
    infos = [media_store.get(svc, m) for m in media_ids]
    ref = media_ids.index(reference) if reference else 0
    levels = [levels_for(svc, m) for m in media_ids]
    found = mc.sync_group(levels, reference=ref)
    out = []
    for m, info, f in zip(media_ids, infos, found):
        row = {"media": m, "name": info["name"], "start_ms": f["start_ms"], "confidence": f["confidence"], "correlation": f["correlation"],
               "reliable": f["reliable"], "audio_only": not info["has_video"], "duration_ms": info["duration_ms"], "fps": info["fps"] or None}
        out.append(row)
    manual = {}
    for key, value in (offsets or {}).items():
        if key not in media_ids:
            raise NotFound(f"{key} is not one of the recordings being synced.")
        manual[key] = float(value)
    if manual:
        # manual values are group times; recordings not given keep their measured start (the reference stays at 0)
        for r in out:
            if r["media"] in manual:
                r["start_ms"], r["confidence"], r["manual"] = manual[r["media"]], None, True
    low = min(r["start_ms"] for r in out)
    for r in out:
        r["start_ms"] = round(r["start_ms"] - low, 2)
    weakest = min((r["confidence"] for r in out if r["confidence"] is not None and r["media"] != media_ids[ref]), default=1.0)
    notes = [f"{r['name']}: the sound hardly matches the others (confidence {r['confidence']}): check by ear or set the offset by hand."
             for r in out if r["confidence"] is not None and not r["reliable"]]
    frame = min((1000.0 / r["fps"] for r in out if r["fps"]), default=33.3)
    return {"angles": out, "reference": media_ids[ref], "weakest_confidence": weakest, "frame_ms": round(frame, 2), "notes": notes,
            "method": "audio level correlation, 10 ms steps refined to sub-step"}


# ---------------------------------------------------------------- reading a group

def _group_clips(p: Project, g: Multicam):
    picture = [c for t in p.tracks if t.kind == "video" for c in t.clips if c.multicam == g.id]
    sound = [c for t in p.tracks if t.kind == "audio" for c in t.clips if c.multicam == g.id]
    return sorted(picture, key=lambda c: c.start), sorted(sound, key=lambda c: c.start)


def view(p: Project, group: Optional[str] = None) -> dict[str, Any]:
    """The groups of a project: angles (sync, confidence), the master sound and the current shots."""
    groups = [p.multicam(group)] if group else list(p.multicams)
    out = []
    for g in groups:
        picture, sound = _group_clips(p, g)
        shots = [{"clip": c.id, "start": c.start, "end": c.end, "angle": g.angle_index(c.media) + 1, "label": g.angles[g.angle_index(c.media)].label,
                  "media": c.media} for c in picture]
        out.append({"id": g.id, "name": g.name, "master": g.master + 1, "angles": [
            {"n": i + 1, "media": a.media, "label": a.label, "start_ms": a.start, "audio_only": a.audio_only, "confidence": a.confidence,
             "master": i == g.master} for i, a in enumerate(g.angles)],
            "shots": shots, "shot_count": len(shots), "from": picture[0].start if picture else None, "to": picture[-1].end if picture else None,
            "audio_clips": [c.id for c in sound]})
    return {"groups": out}


def group_times(p: Project, g: Multicam, times: np.ndarray) -> np.ndarray:
    """Group time for each timeline time in ``times`` (-1e9 where the group shows nothing)."""
    out = np.full(times.size, -1e9)
    picture, _ = _group_clips(p, g)
    for c in picture:
        m = (times >= c.start) & (times < c.end)
        if m.any():
            out[m] = c.src_in + (times[m] - c.start) * c.speed + g.angles[g.angle_index(c.media)].start
    return out


def group_time_at(p: Project, g: Multicam, t: int) -> Optional[int]:
    """Group time shown at timeline time ``t`` (through the picture clip there), or None outside the group."""
    picture, _ = _group_clips(p, g)
    for c in picture:
        if c.start <= t < c.end:
            return int(round(c.src_at(t))) + g.angles[g.angle_index(c.media)].start
    return None


def angle_frame(svc: "Services", p: Project, *, group: Optional[str], angle: Any, t: int, width: int = 240) -> Path:
    """A picture of one angle at the same moment of the event that the timeline shows at ``t`` (clamped into the group): what the
    angle viewer's thumbnails show, so clicking one cuts to what it displays."""
    g = _group_for(p, group, None)
    picture, _ = _group_clips(p, g)
    if not picture:
        raise LumiereError("The group has no picture on the timeline.")
    t = max(picture[0].start, min(int(t), picture[-1].end - 1))
    gt = group_time_at(p, g, t)
    a = g.angles[_angle_ref(g, angle, video=True)]
    return media_store.frame_path(svc, a.media, max(0, int((gt or 0) - a.start)), width)


# ---------------------------------------------------------------- commands

def multicam_create(svc: "Services", p: Project, *, media: list[str], name: str = "Multicámara", offsets: Optional[dict[str, Any]] = None,
                    master: Optional[str] = None, angle: Optional[str] = None, labels: Optional[dict[str, str]] = None,
                    at: Optional[int] = None, reference: Optional[str] = None) -> tuple[Project, dict[str, Any]]:
    """Sync 2+ recordings of one event by their audio and put them on the timeline as a multicam group (first angle shown, the
    master recording's sound heard throughout)."""
    result = sync(svc, media, reference=reference, offsets=offsets)
    angles = []
    for row in result["angles"]:
        item: dict[str, Any] = {"media": row["media"], "start": row["start_ms"], "confidence": row["confidence"]}
        if labels and row["media"] in labels:
            item["label"] = labels[row["media"]]
        angles.append(item)
    op: dict[str, Any] = {"op": "multicam_create", "angles": angles, "name": name}
    for key, value in (("master", master), ("angle", angle), ("at", at)):
        if value is not None:
            op[key] = value
    new, results = apply_ops(p, [op], project_store.media_lookup(svc))
    made = results[0]
    return new, {"group": made["group"], "sync": result, "from": ms_to_tc(made["start"]), "to": ms_to_tc(made["end"]), "duration": ms_to_tc(made["end"] - made["start"]),
                 "angles": made["angles"], "weakest_confidence": result["weakest_confidence"], "notes": result["notes"]}


def multicam_resync(svc: "Services", p: Project, *, group: Optional[str] = None, offsets: Optional[dict[str, Any]] = None) -> tuple[Project, dict[str, Any]]:
    """Measure the group's angles again (or set offsets by hand) and move the clips to match; the shots stay where they are."""
    g = _pick(p, group)
    media = [a.media for a in g.angles]
    result = sync(svc, media, reference=g.angles[g.master].media, offsets=offsets)
    # keep the master where it is: group time is anchored on it, so only the others move
    anchor = g.angles[g.master].start
    shift = anchor - next(r["start_ms"] for r in result["angles"] if r["media"] == g.angles[g.master].media)
    move = {r["media"]: int(round(r["start_ms"] + shift)) for r in result["angles"]}
    new, _ = apply_ops(p, [{"op": "multicam_set", "group": g.id, "offsets": {m: v for m, v in move.items() if v != g.angles[g.angle_index(m)].start}}],
                       project_store.media_lookup(svc))
    return new, {"group": g.id, "sync": result, "moved": {m: v for m, v in move.items() if v != g.angles[g.angle_index(m)].start}, "notes": result["notes"]}


def _pick(p: Project, group: Optional[str]) -> Multicam:
    if group:
        return p.multicam(group)
    if len(p.multicams) == 1:
        return p.multicams[0]
    if not p.multicams:
        raise LumiereError("This project has no multicam group: create one with multicam_create.")
    raise LumiereError("Several multicam groups: say which with 'group' (" + ", ".join(g.id for g in p.multicams) + ").")


def multicam_auto(svc: "Services", p: Project, *, group: Optional[str] = None, mode: str = "loudness", min_shot_ms: int = 2000, hysteresis_db: float = 4.0,
                  dwell_ms: int = 400, lead_ms: int = 150, wide: Optional[str] = None, speaker_map: Optional[dict[str, str]] = None,
                  start: Optional[int] = None, end: Optional[int] = None) -> tuple[Project, dict[str, Any]]:
    """Cut between the angles by who is speaking. mode='loudness': the camera whose own microphone is loudest above its noise floor
    (with hysteresis, a minimum shot length and a lead before the voice); mode='speakers': the diarized speakers of the master sound,
    each mapped to the camera that hears them best (or to the angle in ``speaker_map``). ``wide``: angle to return to after long silences."""
    if mode not in ("loudness", "speakers"):
        raise LumiereError("mode is 'loudness' or 'speakers'.")
    g = _pick(p, group)
    picture, sound = _group_clips(p, g)
    if not picture:
        raise LumiereError(f"{g.name} has no picture on the timeline.")
    lo = picture[0].start if start is None else max(start, picture[0].start)
    hi = picture[-1].end if end is None else min(end, picture[-1].end)
    if hi - lo < 1000:
        raise LumiereError("The stretch to cut is shorter than a second.")
    cand = [i for i, a in enumerate(g.angles) if not a.audio_only]
    if len(cand) < 2:
        raise LumiereError("Automatic switching needs at least two angles with picture.")
    hop = 100
    times = np.arange(lo, hi, hop)
    gt = group_times(p, g, times)
    levels = [levels_for(svc, g.angles[i].media) for i in cand]
    starts = [g.angles[i].start for i in cand]
    rel = mc.level_matrix(levels, starts, gt)
    rel[:, gt < 0] = -np.inf
    cur_clip = next((c for c in picture if c.start <= lo < c.end), picture[0])
    cur = cand.index(g.angle_index(cur_clip.media))
    wide_idx = cand.index(_angle_ref(g, wide, video=True)) if wide is not None else None
    info: dict[str, Any] = {}
    if mode == "loudness":
        shots = mc.choose_shots(rel, hop, start_angle=cur, min_shot_ms=min_shot_ms, hysteresis_db=hysteresis_db, dwell_ms=dwell_ms, lead_ms=lead_ms,
                                wide=wide_idx)
    else:
        shots, info = _shots_by_speakers(svc, p, g, cand, rel, times, lo, hi, speaker_map, cur, min_shot_ms, lead_ms)
    cuts = [[lo + t, cand[a] + 1] for t, a in shots]
    new, res = apply_ops(p, [{"op": "multicam_switch", "group": g.id, "cuts": cuts}], project_store.media_lookup(svc))
    share: dict[str, int] = {}
    for (t, a), nxt in zip(shots, [s[0] for s in shots[1:]] + [hi - lo]):
        share[g.angles[cand[a]].label] = share.get(g.angles[cand[a]].label, 0) + (nxt - t)
    return new, {"group": g.id, "mode": mode, "shots": len(shots), "share_ms": share, "average_shot_ms": int((hi - lo) / max(1, len(shots))),
                 "first_cuts": [{"at": ms_to_tc(c[0]), "angle": g.angles[c[1] - 1].label} for c in cuts[:20]], **info,
                 "settings": {"min_shot_ms": min_shot_ms, "hysteresis_db": hysteresis_db, "dwell_ms": dwell_ms, "lead_ms": lead_ms}}


def _shots_by_speakers(svc: "Services", p: Project, g: Multicam, cand: list[int], rel: np.ndarray, times: np.ndarray, lo: int, hi: int,
                       speaker_map: Optional[dict[str, str]], cur: int, min_shot_ms: int, lead_ms: int) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    tt = commands.timeline_transcript(svc, p)
    words = [w for w in tt["words"] if w.get("speaker") and lo <= w["t"] < hi]
    if not words:
        raise LumiereError("The master sound has no speaker-separated transcript for this stretch: run media_analyze with kinds=['speakers'] on "
                           f"{g.angles[g.master].media} first, or use mode='loudness'.", code="no_speakers")
    keys = sorted({(w["media"], w["speaker"]) for w in words})
    names = {(w["media"], w["speaker"]): w["speaker_name"] for w in words}
    mapping: dict[tuple[str, str], int] = {}
    explicit = {}
    for k, v in (speaker_map or {}).items():
        explicit[str(k).strip().lower()] = cand.index(_angle_ref(g, v, video=True))
    for key in keys:
        hit = explicit.get(key[1].lower(), explicit.get(str(names[key]).lower()))
        if hit is not None:
            mapping[key] = hit
            continue
        mine = [w for w in words if (w["media"], w["speaker"]) == key]
        score = np.zeros(len(cand))
        for w in mine:
            k0 = min(rel.shape[1] - 1, max(0, int((w["t"] - lo) // 100)))
            k1 = min(rel.shape[1], max(k0 + 1, int((w["t1"] - lo) // 100)))
            seg = np.where(np.isfinite(rel[:, k0:k1]), rel[:, k0:k1], -100.0)
            score += seg.mean(axis=1) * max(1, w["t1"] - w["t"])
        mapping[key] = int(np.argmax(score))
    turns: list[tuple[int, int, int]] = []
    run: Optional[list[Any]] = None
    for w in words:
        key = (w["media"], w["speaker"])
        if run and run[2] == key and w["t"] - run[1] < 1500:
            run[1] = w["t1"]
        else:
            if run:
                turns.append((run[0] - lo, run[1] - lo, mapping[run[2]]))
            run = [w["t"], w["t1"], key]
    if run:
        turns.append((run[0] - lo, run[1] - lo, mapping[run[2]]))
    shots = mc.shots_from_turns(turns, hi - lo, start_angle=cur, min_shot_ms=min_shot_ms, lead_ms=lead_ms)
    return shots, {"speaker_angles": {names[k]: g.angles[cand[a]].label for k, a in mapping.items()}}
