"""Smart edits built on the analysis: remove silences and filler words, cut by transcript, split at scenes, reframe to
vertical, captions, cut to the beat, highlights, loudness matching. Each command takes a Project and returns a new one
plus a summary, so a plan can chain several and save them as one undo step. When an analysis is missing the command
raises NeedsAnalysis with the jobs it queued."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable, Optional

import numpy as np

from . import analyze
from . import media as media_store
from . import projects as project_store
from . import speakers as speakers_mod
from .analysis import audio as audio_an
from .analysis import speech
from .analysis import video as video_an
from .errors import LumiereError, NotFound
from .ops import PRESETS, apply_ops, source_to_timeline
from .timeline import Clip, Project, Reframe
from .util import ms_to_tc

if TYPE_CHECKING:
    from .services import Services


class NeedsAnalysis(LumiereError):
    code = "needs_analysis"

    def __init__(self, message: str, jobs: list[dict[str, Any]]):
        super().__init__(message)
        self.jobs = jobs


def _main_media(p: Project, media: Optional[str]) -> list[str]:
    if media:
        return [media]
    main = p.main_track()
    seen: list[str] = []
    for c in (main.clips if main else []):
        if c.media and c.type == "media" and c.media not in seen:
            seen.append(c.media)
    return seen


def _need(svc: "Services", media_ids: list[str], kind: str, what: str, **kw) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for mid in media_ids:
        res = media_store.get_analysis(svc, mid, kind)
        if res is None:
            missing.append(mid)
        else:
            found[mid] = res
    if missing:
        jobs = []
        for mid in missing:
            jobs += analyze.schedule(svc, mid, [kind], **kw)
        names = ", ".join(media_store.get(svc, m)["name"] for m in missing)
        raise NeedsAnalysis(f"{what} needs the {kind} of {names}; it is running now (jobs: {', '.join(j.get('job', '') for j in jobs)}). "
                            "Repeat when they finish.", jobs)
    return found


def _apply(svc: "Services", p: Project, ops: list[dict[str, Any]]) -> Project:
    if not ops:
        return p
    new, _ = apply_ops(p, ops, project_store.media_lookup(svc))
    return new


# ---------------------------------------------------------------- silences / fillers / words

def remove_silences(svc: "Services", p: Project, *, media: Optional[str] = None, threshold_db: Optional[float] = None, min_silence_ms: int = 500,
                    margin_ms: int = 150, mode: str = "cut", speed: float = 4.0) -> tuple[Project, dict[str, Any]]:
    before = p.duration
    total_cuts = 0
    for mid in _main_media(p, media):
        info = media_store.get(svc, mid)
        if not info["has_audio"]:
            continue
        s = analyze.silences_for(svc, mid, threshold_db=threshold_db, min_silence_ms=min_silence_ms, margin_ms=margin_ms)
        if not s["ranges"]:
            continue
        if mode == "cut":
            p = _apply(svc, p, [{"op": "cut_source", "media": mid, "ranges": s["ranges"]}])
            total_cuts += s["count"]
        else:
            p = _speed_ranges(svc, p, mid, s["ranges"], speed)
            total_cuts += s["count"]
    return p, {"cuts": total_cuts, "before": ms_to_tc(before), "after": ms_to_tc(p.duration), "saved_ms": before - p.duration, "mode": mode}


def _speed_ranges(svc: "Services", p: Project, media_id: str, ranges: list[list[int]], speed: float) -> Project:
    """Play the given source ranges faster instead of cutting them (split around each one, then speed the middle)."""
    for a, b in sorted(ranges, reverse=True):
        for t0, t1 in reversed(source_to_timeline(p, media_id, [(a, b)])):
            main = p.main_track()
            hit = next((c for t in p.tracks for c in t.clips if c.media == media_id and c.start <= t0 < c.end and not t.locked), None)
            if hit is None or t1 - t0 < 200:
                continue
            ops: list[dict[str, Any]] = []
            if t0 - hit.start > 40:
                ops.append({"op": "split", "clip": hit.id, "at": t0})
            p2, res = apply_ops(p, ops, project_store.media_lookup(svc)) if ops else (p, [])
            middle_id = res[0]["new_clips"][0] if ops else hit.id
            _, mid_clip = p2.find(middle_id)
            ops2: list[dict[str, Any]] = []
            if mid_clip.end - t1 > 40:
                ops2.append({"op": "split", "clip": middle_id, "at": t1})
            ops2.append({"op": "speed", "clip": middle_id, "speed": min(16.0, mid_clip.speed * speed)})
            p = _apply(svc, p2, ops2)
            del main
    return p


def remove_fillers(svc: "Services", p: Project, *, media: Optional[str] = None, extra: Optional[list[str]] = None, repeats: bool = True,
                   strict: bool = False) -> tuple[Project, dict[str, Any]]:
    mids = [m for m in _main_media(p, media) if media_store.get(svc, m)["has_audio"]]
    transcripts = _need(svc, mids, "transcript", "Removing filler words")
    before = p.duration
    found: list[dict[str, Any]] = []
    for mid, t in transcripts.items():
        hits = speech.find_fillers(t["words"], t.get("language") or "es", extra=extra, repeats=repeats, strict=strict)
        if not hits:
            continue
        ids = {i for h in hits for i in h["ids"]}
        ranges = speech.cut_ranges_for_words(t["words"], ids)
        p = _apply(svc, p, [{"op": "cut_source", "media": mid, "ranges": ranges}])
        found += [{"media": mid, "text": h["text"], "at": ms_to_tc(h["t0"]), "reason": h["reason"]} for h in hits]
    return p, {"removed": len(found), "examples": found[:30], "before": ms_to_tc(before), "after": ms_to_tc(p.duration)}


def cut_words(svc: "Services", p: Project, *, media: str, word_ids: Optional[list[str]] = None, text: Optional[str] = None,
              keep: bool = False) -> tuple[Project, dict[str, Any]]:
    """Text-based editing: remove (or keep only) words of a media's transcript by id or by a phrase."""
    t = _need(svc, [media], "transcript", "Editing by text")[media]
    words = t["words"]
    ids: set[str] = set(word_ids or [])
    if text:
        target = [speech.norm(x) for x in text.split() if speech.norm(x)]
        normed = [speech.norm(w["text"]) for w in words]
        hits = 0
        for i in range(len(words) - len(target) + 1):
            if normed[i: i + len(target)] == target:
                ids.update(w["id"] for w in words[i: i + len(target)])
                hits += 1
        if not hits:
            raise NotFound(f"The phrase «{text}» is not in the transcript.")
    if not ids:
        raise LumiereError("Give word ids or a phrase.")
    unknown = ids - {w["id"] for w in words}
    if unknown:
        raise NotFound(f"Unknown word ids: {', '.join(sorted(unknown)[:10])}.")
    before = p.duration
    if keep:
        kept = [w for w in words if w["id"] in ids]
        ranges = []
        for w in kept:
            if ranges and w["t0"] - ranges[-1][1] < 400:
                ranges[-1][1] = w["t1"] + 120
            else:
                ranges.append([max(0, w["t0"] - 120), w["t1"] + 120])
        p = _apply(svc, p, [{"op": "keep_source", "media": media, "ranges": ranges}])
    else:
        p = _apply(svc, p, [{"op": "cut_source", "media": media, "ranges": speech.cut_ranges_for_words(words, ids)}])
    return p, {"words": len(ids), "before": ms_to_tc(before), "after": ms_to_tc(p.duration), "mode": "keep" if keep else "cut"}


def timeline_transcript(svc: "Services", p: Project, *, track: Optional[str] = None) -> dict[str, Any]:
    """The words heard on the timeline, in timeline order, with their timeline times and their media / word ids — what a text
    editor shows. Media without a transcript are listed in ``missing``. With a speaker-separated transcript every word also
    carries its speaker (``speaker`` id, ``speaker_name``, ``speaker_color``) and ``speakers`` lists them. The picture of a
    multicam group is muted angle clips: its words are read from the group's master sound instead."""
    t = p.track(track) if track else p.main_track()
    if t is None:
        return {"words": [], "missing": [], "speakers": []}
    out: list[dict[str, Any]] = []
    missing: list[str] = []
    cache: dict[str, Optional[dict]] = {}
    tracks = [x for x in p.tracks if x.id in p.with_multicam_sound({t.id})]
    for tr in tracks:
        for c in sorted(tr.clips, key=lambda c: c.start):
            if c.type != "media" or c.reverse or (c.multicam and tr.kind == "video"):
                continue
            if c.media not in cache:
                cache[c.media] = analyze.transcript(svc, c.media)
            tr_ = cache[c.media]
            if tr_ is None:
                if c.media not in missing and media_store.get(svc, c.media)["has_audio"]:
                    missing.append(c.media)
                continue
            for w in tr_["words"]:
                mid = (w["t0"] + w["t1"]) / 2
                if c.src_in <= mid < c.src_out:
                    item = {"id": w["id"], "media": c.media, "clip": c.id, "text": w["text"], "t": int(c.timeline_at(w["t0"])),
                            "t1": int(c.timeline_at(w["t1"])), "p": w.get("p")}
                    if w.get("speaker"):
                        item.update(speaker=w["speaker"], speaker_name=speakers_mod.name_of(tr_, w["speaker"]),
                                    speaker_color=speakers_mod.color_of(tr_, w["speaker"]))
                    out.append(item)
    if len(tracks) > 1:
        out.sort(key=lambda w: w["t"])
    listed = [{"media": m, "id": sid, "name": info.get("name") or sid, "color": info.get("color", "")}
              for m, tr_ in cache.items() if tr_ for sid, info in (tr_.get("speakers") or {}).items()]
    return {"track": t.id, "words": out, "missing": missing, "speakers": listed}


def speaker_cut(svc: "Services", p: Project, *, speakers: list[str], media: Optional[str] = None, keep: bool = False) -> tuple[Project, dict[str, Any]]:
    """Remove (or keep only) everything the given speakers say. Names or ids from the speaker list of the transcript
    (media_analyze kinds=['speakers'] separates them)."""
    if not speakers:
        raise LumiereError("Give the speakers (names or ids like S1).")
    if media:
        mids = [media]
    else:
        main = p.main_track()
        ids = p.with_multicam_sound({main.id}) if main else set()
        mids = []
        for tr in p.tracks:
            if tr.id not in ids:
                continue
            for c in tr.clips:
                if c.media and not (c.multicam and tr.kind == "video") and c.media not in mids and media_store.get(svc, c.media)["has_audio"]:
                    mids.append(c.media)
    transcripts = _need(svc, mids, "transcript", "Cutting by speaker")
    before = p.duration
    total = 0
    found: list[str] = []
    for mid, t in transcripts.items():
        if not t.get("speakers"):
            continue
        try:
            words = speakers_mod.words_of(t, speakers)
        except NotFound:
            continue
        if not words:
            continue
        p, _ = cut_words(svc, p, media=mid, word_ids=[w["id"] for w in words], keep=keep)
        total += len(words)
        found.append(mid)
    if not found:
        raise LumiereError("No transcript on the timeline has those speakers: run media_analyze with kinds=['speakers'] first, and check the names "
                           "with speakers_get.", code="no_speakers")
    return p, {"words": total, "speakers": speakers, "mode": "keep" if keep else "cut", "media": found, "before": ms_to_tc(before), "after": ms_to_tc(p.duration)}


# ---------------------------------------------------------------- scenes

def split_scenes(svc: "Services", p: Project, *, media: Optional[str] = None, mode: str = "split") -> tuple[Project, dict[str, Any]]:
    mids = [m for m in _main_media(p, media) if media_store.get(svc, m)["has_video"]]
    found = _need(svc, mids, "scenes", "Splitting at scene changes")
    n = 0
    for mid, res in found.items():
        cuts = [c["t"] for c in res.get("cuts", [])]
        for t_src in cuts:
            for t0, _ in source_to_timeline(p, mid, [(t_src, t_src + 1)]):
                if mode == "markers":
                    p = _apply(svc, p, [{"op": "marker_add", "t": t0, "label": "Escena", "kind": "scene", "color": "#3BA4F5"}])
                    n += 1
                else:
                    clip_hit = next((c for t in p.tracks for c in t.clips if c.media == mid and c.start + 40 < t0 < c.end - 40), None)
                    if clip_hit:
                        p = _apply(svc, p, [{"op": "split", "clip": clip_hit.id, "at": t0}])
                        n += 1
    return p, {"scenes": n, "mode": mode}


# ---------------------------------------------------------------- reframe

def aspect_canvas(aspect: str, current: Project) -> dict[str, Any]:
    r = video_an.aspect_of(aspect)
    fps = current.canvas.fps
    if abs(r - 9 / 16) < 0.01:
        return {"width": 1080, "height": 1920, "fps": fps}
    if abs(r - 1) < 0.01:
        return {"width": 1080, "height": 1080, "fps": fps}
    if abs(r - 4 / 5) < 0.01:
        return {"width": 1080, "height": 1350, "fps": fps}
    if abs(r - 16 / 9) < 0.01:
        return {"width": 1920, "height": 1080, "fps": fps}
    h = 1920 if r < 1 else 1080
    w = int(round(h * r / 2)) * 2
    return {"width": w, "height": h, "fps": fps}


def reframe(svc: "Services", p: Project, *, aspect: str = "9:16", mode: str = "auto", clips: Optional[list[str]] = None) -> tuple[Project, dict[str, Any]]:
    """Change the canvas aspect and keep the subject in frame: every horizontal video clip on the main track (or the given
    clips) is cropped with a camera path from the focus analysis (faces, else motion and detail), still within a scene
    when the subject does not move, panning smoothly otherwise. mode: auto | track | stable | center | blur (the whole frame over
    a blurred fill, nothing cropped)."""
    size = aspect_canvas(aspect, p)
    p = _apply(svc, p, [{"op": "canvas", **size}])
    targets: list[Clip] = []
    if clips:
        targets = [p.find(c)[1] for c in clips]
    else:
        main = p.main_track()
        targets = [c for c in (main.clips if main else []) if c.type == "media"]
    out_aspect = size["width"] / size["height"]
    video_ids = sorted({c.media for c in targets if media_store.get(svc, c.media)["has_video"] and media_store.get(svc, c.media)["kind"] == "video"})
    found = _need(svc, video_ids, "focus", "Reframing") if mode not in ("center", "blur") else {}
    scenes: dict[str, list[int]] = {}
    for mid in video_ids:
        sc = media_store.get_analysis(svc, mid, "scenes")
        scenes[mid] = [c["t"] for c in (sc or {}).get("cuts", [])]
        if sc is None and mode not in ("center", "blur"):
            analyze.schedule(svc, mid, ["scenes"])  # better next time; the path still works without cuts
    changed = 0
    ops: list[dict[str, Any]] = []
    for c in targets:
        info = media_store.get(svc, c.media)
        if mode == "blur":
            ops.append({"op": "set", "clip": c.id, "props": {"reframe": None, "transform": {"fit": "blur", "scale": 1.0, "x": 0, "y": 0}}})
            changed += 1
            continue
        ops.append({"op": "set", "clip": c.id, "props": {"transform": {"fit": "cover", "scale": 1.0, "x": 0, "y": 0}}})
        if info["kind"] != "video" or mode == "center":
            ops.append({"op": "set", "clip": c.id, "props": {"reframe": None, "transform": {"focus_x": 0.5, "focus_y": 0.5}}})
            changed += 1
            continue
        in_aspect = info["width"] / max(1, info["height"])
        samples = [s for s in found[c.media]["samples"] if c.src_in - 2000 <= s[0] <= c.src_out + 2000]
        path = video_an.smooth_path(samples, scenes.get(c.media, []), aspect_in=in_aspect, aspect_out=out_aspect,
                                    mode=mode if mode in ("track", "stable") else "auto")
        if path:
            ops.append({"op": "set", "clip": c.id, "props": {"reframe": Reframe(path=path, mode="track" if mode == "auto" else mode).model_dump()}})
        changed += 1
    p = _apply(svc, p, ops)
    faces = any(found[m].get("faces") for m in found) if found else False
    names = sorted({found[m].get("detector") or ("faces" if found[m].get("faces") else "saliency") for m in found}) if found else []
    summary = {"canvas": f"{size['width']}x{size['height']}", "clips": changed, "mode": mode, "detector": "faces+saliency" if faces else "saliency"}
    if any(n not in ("faces", "saliency") for n in names):  # analyses made since face detectors are named say which one ran
        summary["face_detector"] = ", ".join(names)
        summary["detector"] = ("+".join(n for n in names if n != "saliency") + "+saliency") if faces else "saliency"
    notes = sorted({str(found[m].get("detector_info", {}).get("reason")) for m in found if found[m].get("detector_info", {}).get("reason")})
    if notes:
        summary["detector_note"] = "; ".join(notes)
    return p, summary


# ---------------------------------------------------------------- captions

def captions(svc: "Services", p: Project, *, enabled: bool = True, style: Optional[str] = None, props: Optional[dict[str, Any]] = None,
             language: str = "") -> tuple[Project, dict[str, Any]]:
    op: dict[str, Any] = {"op": "captions", "enabled": enabled, "props": props or {}}
    if style:
        op["style"] = style
    p = _apply(svc, p, [op])
    if enabled:
        tracks = p.captions.tracks or ([p.main_track().id] if p.main_track() else [])
        mids = sorted({c.media for t in p.tracks if t.id in tracks for c in t.clips if c.media and c.type == "media" and media_store.get(svc, c.media)["has_audio"]})
        _need(svc, mids, "transcript", "Captions", language=language)
    return p, {"captions": p.captions.model_dump()}


# ---------------------------------------------------------------- beat sync

def beat_sync(svc: "Services", p: Project, *, music: str, source: Optional[str] = None, beats_per_cut: int = 2, start_ms: int = 0,
              length_ms: Optional[int] = None, mode: str = "scenes") -> tuple[Project, dict[str, Any]]:
    """Rebuild the main track as cuts on the beat of ``music``: each cut lasts ``beats_per_cut`` beats. The pictures come from
    the main track's clips in order (mode=clips) or from the scenes of ``source`` (mode=scenes), each starting at its scene."""
    beats = _need(svc, [music], "beats", "Cutting to the beat")[music]
    grid = [b for b in beats.get("beats", []) if b >= start_ms]
    if len(grid) < 2:
        raise LumiereError("No beat grid found in that music.")
    music_info = media_store.get(svc, music)
    end_limit = start_ms + (length_ms or music_info["duration_ms"])
    grid = [b for b in grid if b <= end_limit]
    step = max(1, int(beats_per_cut))
    cuts = grid[::step]
    if len(cuts) < 2:
        raise LumiereError("The music is too short for that many beats per cut.")
    segments: list[tuple[str, int]] = []  # (media, src_in)
    main = p.main_track()
    if mode == "clips" or not source:
        pool = [c for c in (main.clips if main else []) if c.type == "media"]
        if not pool:
            raise LumiereError("The main track is empty: add clips or give a source video.")
        segments = [(c.media, c.src_in) for c in pool]
    else:
        info = media_store.get(svc, source)
        sc = _need(svc, [source], "scenes", "Cutting to the beat")[source]
        starts = [0] + [c["t"] for c in sc.get("cuts", [])]
        segments = [(source, s) for s in starts if s < info["duration_ms"] - 500]
    first_beat = cuts[0]
    new_clips: list[dict[str, Any]] = []
    look = project_store.media_lookup(svc)
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        mid, src_in = segments[i % len(segments)]
        info = look(mid) or {}
        length = b - a
        if info.get("kind") == "image":
            new_clips.append({"media": mid, "length": length})
            continue
        dur = int(info.get("duration_ms") or 0)
        lap = i // len(segments)
        s = src_in + lap * length
        if dur and s + length > dur:
            s = max(0, dur - length)
        new_clips.append({"media": mid, "src_in": s, "src_out": s + length})
    ops: list[dict[str, Any]] = []
    if main is not None and main.clips:
        ops.append({"op": "delete", "clips": [c.id for c in main.clips], "ripple": False})
    music_track = next((t for t in p.tracks if t.kind == "audio" and t.role == "music"), None)
    if music_track is None:
        ops.append({"op": "track_add", "kind": "audio", "name": "Música", "role": "music", "id": "trk_music"})
        music_track_id = "trk_music"
    else:
        music_track_id = music_track.id
        if music_track.clips:
            ops.append({"op": "delete", "clips": [c.id for c in music_track.clips], "ripple": False})
    p = _apply(svc, p, ops)
    main = p.main_track()
    if main is None:
        p = _apply(svc, p, [{"op": "track_add", "kind": "video", "name": "Principal", "role": "main"}])
        main = p.main_track()
    p = _apply(svc, p, [{"op": "sequence", "track": main.id, "items": new_clips},
                        {"op": "add_media", "media": music, "track": music_track_id, "at": 0, "src_in": first_beat, "src_out": cuts[-1], "mode": "overwrite"}])
    for t in p.tracks:
        if t.id == music_track_id:
            t.duck = False
    return p, {"cuts": len(new_clips), "bpm": beats.get("bpm"), "beats_per_cut": step, "length": ms_to_tc(cuts[-1] - first_beat)}


# ---------------------------------------------------------------- highlights

def highlights(svc: "Services", media: str, *, count: int = 5, length_ms: int = 30000, min_gap_ms: int = 20000,
               use_transcript: bool = True, mode: str = "signals") -> dict[str, Any]:
    """The most eventful windows of a long video: loud moments and sudden peaks in the sound, lots of motion, many scene
    changes, and (with a transcript) dense speech with exclamations. Returns ranked windows with the reasons.

    mode='model' also reads the transcript with the local model, which marks the moments that stand on their own (a hook, a
    punchline, a complete thought); those and the sound/motion windows are merged into one ranked list. Without a transcript
    or a reachable model the result is the signals-only list, with ``model.reason`` saying why."""
    if mode not in ("signals", "model"):
        raise LumiereError("mode must be 'signals' or 'model'.")
    info = media_store.get(svc, media)
    dur = info["duration_ms"]
    secs = max(1, dur // 1000)
    score = np.zeros(secs, dtype=np.float64)
    reasons: list[list[str]] = [[] for _ in range(secs)]
    levels = media_store.rms(svc, media)
    if levels is not None and levels.size:
        e = audio_an.energy_per_second(levels)[:secs]
        z = (e - np.median(e)) / (np.std(e) + 1e-6)
        peak = np.zeros_like(z)
        peak[1:] = np.maximum(0, np.diff(e))
        pz = (peak - peak.mean()) / (peak.std() + 1e-6)
        score[: z.size] += np.clip(z, -2, 4) * 1.0 + np.clip(pz, 0, 4) * 0.6
        for i in np.flatnonzero(z > 1.5):
            if i < secs:
                reasons[i].append("loud")
    motion = media_store.get_analysis(svc, media, "motion")
    if motion is None and info["has_video"]:
        analyze.schedule(svc, media, ["motion"])
    elif motion:
        m = np.array(motion["per_second"][:secs], dtype=np.float64)
        mz = (m - np.median(m)) / (np.std(m) + 1e-6)
        score[: mz.size] += np.clip(mz, -2, 4) * 0.8
        for i in np.flatnonzero(mz > 1.5):
            if i < secs:
                reasons[i].append("motion")
    sc = media_store.get_analysis(svc, media, "scenes")
    if sc:
        for c in sc.get("cuts", []):
            i = c["t"] // 1000
            if i < secs:
                score[i] += 0.5
                reasons[i].append("cut")
    tr = media_store.get_analysis(svc, media, "transcript") if (use_transcript or mode == "model") else None
    if tr:
        for w in tr["words"]:
            i = w["t0"] // 1000
            if i < secs:
                score[i] += 0.08 + (0.6 if w["text"].endswith("!") else 0)
                if w["text"].endswith("!"):
                    reasons[i].append("exclamation")
    meta: Optional[dict[str, Any]] = None
    moments: list[dict[str, Any]] = []
    if mode == "model":
        if tr is None and info["has_audio"]:
            analyze.schedule(svc, media, ["transcript"])
        moments, meta = _model_moments(svc, media, tr, length_ms)
    win = max(3, length_ms // 1000)
    if secs <= win:
        out = {"media": media, "highlights": [{"start_ms": 0, "end_ms": dur, "score": 0, "reasons": []}], "signals": _signals(levels, motion, sc, tr)}
        if meta is not None:
            out["model"] = meta
        return out
    smooth = np.convolve(score, np.ones(win) / win, mode="valid")
    gap = max(win, min_gap_ms // 1000)
    picked = _signal_windows(smooth, reasons, win, gap, count * (2 if moments else 1), tr, dur, length_ms)
    picked = _merge_moments(picked, moments, score, reasons, smooth, count, dur) if moments else picked[:count]
    for x in picked:
        x.pop("_i", None)
        x["range"] = f"{ms_to_tc(x['start_ms'])}–{ms_to_tc(x['end_ms'])}"
    signals = _signals(levels, motion, sc, tr)
    if meta is not None and meta.get("used"):
        signals.append("model")
    out = {"media": media, "highlights": picked, "signals": signals}
    if meta is not None:
        out["model"] = meta
    return out


def _signal_windows(smooth: np.ndarray, reasons: list[list[str]], win: int, gap: int, count: int, tr: Optional[dict[str, Any]], dur: int,
                    length_ms: int) -> list[dict[str, Any]]:
    """The best ``count`` windows of the sound/motion/cut/speech score, at least ``gap`` seconds apart."""
    picked: list[dict[str, Any]] = []
    for i in np.argsort(-smooth):
        if len(picked) >= count:
            break
        if any(abs(int(i) - p["_i"]) < gap for p in picked):
            continue
        rs: dict[str, int] = {}
        for r in reasons[int(i): int(i) + win]:
            for x in r:
                rs[x] = rs.get(x, 0) + 1
        start = int(i) * 1000
        # start on a sentence boundary when there is a transcript
        if tr:
            prev = [w for w in tr["words"] if w["t1"] <= start + 1500 and w["t0"] >= start - 6000]
            for a, b in zip(prev, prev[1:]):
                if b["t0"] - a["t1"] > 500 and abs(b["t0"] - start) < 4000:
                    start = b["t0"] - 150
        picked.append({"_i": int(i), "start_ms": max(0, start), "end_ms": min(dur, start + length_ms), "score": round(float(smooth[i]), 3),
                       "reasons": sorted(rs, key=lambda k: -rs[k])[:4]})
    return picked


def _model_moments(svc: "Services", media: str, tr: Optional[dict[str, Any]], length_ms: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Moments the local model says stand on their own, as [{start_ms, end_ms, strength, kind, why}], and a note on what happened.
    The model's sentence ranges are cached per transcript, so asking again (or for another length or count) does not read it again;
    the time window of each (widened to stand alone, at least min(8 s, the clip length)) is worked out on every call."""
    import hashlib

    from .analysis import moments as mo
    from .errors import ModelUnavailable
    from .generate import chat_text

    meta: dict[str, Any] = {"requested": True, "used": False}
    if not tr or not tr.get("words"):
        meta["reason"] = "no transcript yet (queued): the sound and motion signals were used"
        return [], meta
    sents = mo.sentences_of(tr)
    if not sents:
        meta["reason"] = "the transcript has no sentences"
        return [], meta
    key = hashlib.sha1(("v1|" + "\n".join(f"{s['t0']}:{s['text']}" for s in sents)).encode()).hexdigest()
    cached = media_store.get_analysis(svc, media, "highlights_llm")
    min_ms = min(8000, max(2000, int(length_ms)))

    def windows(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in raw:
            a, b = mo.moment_window(sents, m["first"], m["last"], min_ms=min_ms)
            out.append({"start_ms": a, "end_ms": b, "strength": m["strength"], "kind": m["kind"], "why": m["why"]})
        return out

    if cached and cached.get("key") == key:
        meta.update(used=bool(cached["moments"]), model=cached.get("model"), moments=len(cached["moments"]), chunks=cached.get("chunks"), cached=True)
        if not cached["moments"]:
            meta["reason"] = "the model found no moment that stands on its own"
        return windows(cached["moments"]), meta
    chunks = mo.chunks_of(sents)
    meta["truncated"] = len(chunks) > mo.MAX_CHUNKS
    found: list[dict[str, Any]] = []
    model_name: Optional[str] = None
    bad = 0
    asked = 0
    for chunk in chunks[: mo.MAX_CHUNKS]:
        try:
            text, model_name = chat_text(svc, mo.prompt_for(chunk, n=3), max_tokens=500, effort="off")
        except ModelUnavailable as error:
            if not asked:
                meta["reason"] = f"the local model is not reachable ({str(error)[:120]}): the sound and motion signals were used"
                return [], meta
            meta["partial"] = True
            break
        asked += 1
        try:
            found += mo.parse_answer(text, {s["i"] for s in chunk})
        except ValueError:
            bad += 1
    if not found:
        meta["reason"] = ("the model did not answer in the expected form" if bad else "the model found no moment that stands on its own") + \
            ": the sound and motion signals were used"
        return [], meta
    found.sort(key=lambda m: (m["first"], -m["strength"]))
    merged: list[dict[str, Any]] = []
    for m in found:  # chunks overlap by a couple of sentences: keep the stronger of two moments that share sentences
        if merged and m["first"] <= merged[-1]["last"]:
            if m["strength"] > merged[-1]["strength"]:
                merged[-1] = m
            continue
        merged.append(m)
    media_store.put_analysis(svc, media, "highlights_llm", {"key": key, "model": model_name, "moments": merged, "chunks": asked}, {"moments": len(merged)})
    meta.update(used=True, model=model_name, moments=len(merged), chunks=asked, cached=False)
    if bad:
        meta["bad_chunks"] = bad
    return windows(merged), meta


def _merge_moments(windows: list[dict[str, Any]], moments: list[dict[str, Any]], score: np.ndarray, reasons: list[list[str]], smooth: np.ndarray,
                   count: int, dur: int) -> list[dict[str, Any]]:
    """One ranked list from the model's moments and the sound/motion windows. A moment's rank is mostly the model's strength
    (65%) plus how eventful its own seconds are (35%); a window that no moment covers ranks on its signals alone (up to 35%),
    so strong moments lead and the loudest uncovered window can still beat a weak one."""
    ref = max(float(smooth.max()) if smooth.size else 0.0, 1e-6)
    secs = score.size
    cands: list[dict[str, Any]] = []
    for m in moments:
        a, b = int(m["start_ms"]), min(int(m["end_ms"]), dur)
        lo = min(secs - 1, a // 1000)
        hi = min(secs, max(lo + 1, -(-b // 1000)))
        seg = score[lo:hi]
        norm = float(np.clip((seg.mean() if seg.size else 0.0) / ref, 0, 1))
        rs: dict[str, int] = {}
        for r in reasons[lo:hi]:
            for x in r:
                rs[x] = rs.get(x, 0) + 1
        loud = [k for k in sorted(rs, key=lambda k: -rs[k]) if rs[k] >= 2][:2]
        cands.append({"start_ms": a, "end_ms": b, "score": round(0.35 * norm + 0.65 * m["strength"] / 5, 3), "reasons": [m["kind"], *loud],
                      "source": "both" if loud else "model", "why": m["why"], "strength": m["strength"]})
    for w in windows:
        half = 0.5 * (w["end_ms"] - w["start_ms"])
        if any(min(w["end_ms"], c["end_ms"]) - max(w["start_ms"], c["start_ms"]) > half for c in cands):
            continue  # a moment already covers most of this window
        cands.append({"start_ms": w["start_ms"], "end_ms": w["end_ms"], "score": round(0.35 * float(np.clip(w["score"] / ref, 0, 1)), 3),
                      "reasons": w["reasons"], "source": "signals"})
    cands.sort(key=lambda c: -c["score"])
    picked: list[dict[str, Any]] = []
    for c in cands:
        if len(picked) >= count:
            break
        if any(min(c["end_ms"], p["end_ms"]) - max(c["start_ms"], p["start_ms"]) > 0 for p in picked):
            continue
        picked.append(c)
    return picked


def _signals(levels, motion, scenes, tr) -> list[str]:
    out = []
    if levels is not None:
        out.append("sound")
    if motion:
        out.append("motion")
    if scenes:
        out.append("scenes")
    if tr:
        out.append("transcript")
    return out


# ---------------------------------------------------------------- loudness matching

def match_loudness(svc: "Services", p: Project, *, target_lufs: float = -16.0, track: Optional[str] = None) -> tuple[Project, dict[str, Any]]:
    """Set each clip's gain so every source sits at the same loudness (dialogue from different mics, game captures)."""
    tracks = [p.track(track)] if track else [t for t in p.tracks if t.kind in ("video", "audio") and t.role != "music"]
    mids = sorted({c.media for t in tracks for c in t.clips if c.media and c.type == "media" and media_store.get(svc, c.media)["has_audio"]})
    found = _need(svc, mids, "loudness", "Matching loudness")
    ops = []
    for t in tracks:
        for c in t.clips:
            res = found.get(c.media or "")
            if not res or res.get("integrated_lufs") is None:
                continue
            gain = max(-20.0, min(20.0, target_lufs - float(res["integrated_lufs"])))
            ops.append({"op": "set", "clip": c.id, "props": {"volume_db": round(gain, 1)}, "ripple": False})
    p = _apply(svc, p, ops)
    return p, {"clips": len(ops), "target_lufs": target_lufs}


# ---------------------------------------------------------------- zoom on cuts

def zoom_cuts(svc: "Services", p: Project, *, scale: float = 1.12, every: int = 2, track: Optional[str] = None) -> tuple[Project, dict[str, Any]]:
    """The talking-head trick: alternate framings across jump cuts (every ``every``-th clip punched in by ``scale``) so the
    cuts read as camera changes."""
    if not 1.0 <= scale <= 2.0:
        raise LumiereError("scale goes from 1.0 to 2.0.")
    t = p.track(track) if track else p.main_track()
    if t is None:
        raise LumiereError("No video track.")
    clips = [c for c in sorted(t.clips, key=lambda c: c.start) if c.type == "media"]
    ops = []
    for i, c in enumerate(clips):
        target = scale if every > 0 and i % max(1, every) == max(1, every) - 1 else 1.0
        if abs(c.transform.scale - target) > 1e-3:
            ops.append({"op": "set", "clip": c.id, "props": {"transform": {"scale": target}}, "ripple": False})
    p = _apply(svc, p, ops)
    return p, {"clips": len(ops), "scale": scale, "every": every}


# ---------------------------------------------------------------- script assembly

def _meaning_alignment(svc: "Services", media: str, segments: list, transcript: dict[str, Any]) -> dict[str, Any]:
    """Ask the local model which sentences of a free talk deliver each script part (cached per script text)."""
    import hashlib

    sents = [s for s in transcript.get("segments") or [] if s.get("text")]
    if not sents:
        raise LumiereError("The transcript has no sentences.")
    key = hashlib.sha1(("\n".join(s.text for s in segments) + f"|{len(sents)}").encode()).hexdigest()
    cached = media_store.get_analysis(svc, media, "script_align")
    if cached and cached.get("key") == key:
        return cached
    lines = [f"{i}\t{s['text']}" for i, s in enumerate(sents)]
    script_lines = [f"{seg.index + 1}. {seg.title}: {seg.text}" for seg in segments]
    system = ("You align a talk recorded in front of a camera to the script it follows freely. For each script part, give the sentence "
              "ranges of the recording that deliver it, in the order to play them. When a part was attempted several times, keep only the "
              "last attempt that reaches its end. Leave out false starts, comments to the camera crew or to oneself ('vamos allá', 'otra "
              "vez', 'qué nervios'), and anything that belongs to no part. A sentence belongs to one part at most; merge neighbouring "
              "sentences into one range. Answer with one line per script part and nothing else, exactly like:\n"
              "1: 2-5, 9-12\n2: 13-20\n3: -\n"
              "('-' when the part was never said). Then optionally one last line starting with 'NOTE:' (short, in the language of the talk).")
    user = "SCRIPT\n" + "\n".join(script_lines) + "\n\nRECORDING (index, sentence)\n" + "\n".join(lines)
    if len(user) > 60000:
        raise LumiereError("The recording is too long for the model to align at once; cut it into parts first.")

    def parse(text: str) -> dict[str, Any]:
        out = []
        notes = ""
        for line in text.splitlines():
            line = line.strip().strip("`*").strip()
            if not line:
                continue
            if line.upper().startswith("NOTE"):
                notes = line.split(":", 1)[-1].strip()
                continue
            m = re.match(r"^(\d+)\s*[:.)-]\s*(.*)$", line)
            if not m:
                continue
            seg = int(m.group(1))
            ranges = []
            for x, y in re.findall(r"(\d+)\s*(?:-|–|to|a)\s*(\d+)", m.group(2)):
                x, y = int(x), int(y)
                if x > y:
                    x, y = y, x
                if y < len(sents):
                    ranges.append([x, y])
            for single in re.findall(r"(?<![\d-])(\d+)(?![\d]*\s*(?:-|–))", re.sub(r"\d+\s*(?:-|–|to|a)\s*\d+", "", m.group(2))):
                k = int(single)
                if k < len(sents):
                    ranges.append([k, k])
            out.append({"segment": seg, "ranges": sorted(ranges)})
        if not out:
            raise ValueError("no 'N: a-b' lines in the answer")
        claimed: set[int] = set()
        for part in sorted(out, key=lambda x: x["segment"]):
            clean = []
            for x, y in part["ranges"]:
                free = [k for k in range(x, y + 1) if k not in claimed]
                if not free:
                    continue
                run = [free[0], free[0]]
                for k in free[1:]:
                    if k == run[1] + 1:
                        run[1] = k
                    else:
                        clean.append(run)
                        run = [k, k]
                clean.append(run)
                claimed.update(free)
            part["ranges"] = clean
        return {"parts": out, "notes": notes[:600]}

    from .generate import chat_text

    text, model = chat_text(svc, [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=900, effort="off")
    try:
        result = parse(text)
    except ValueError as error:
        raise LumiereError(f"The model did not answer in the expected form ({error}).", code="generation_failed") from error
    result.update({"key": key, "model": model, "sentences": [[s["t0"], s["t1"]] for s in sents]})
    media_store.put_analysis(svc, media, "script_align", result, {"segments": len(segments)})
    return result


def script_assemble(svc: "Services", p: Project, *, media: str, script: str = "", script_path: str = "", take: str = "last",
                    min_coverage: float = 0.6, markers: bool = True, mode: str = "auto") -> tuple[Project, dict[str, Any]]:
    """Rough cut from a script: the recording becomes the main track, one take per script segment in script order, with a
    chapter marker per segment. mode words: matches the script word for word (teleprompter readings, retakes); meaning: the
    local model finds each part in a talk that follows the script freely; auto: words, then meaning when words find too little.
    take: last | best (words mode)."""
    from pathlib import Path

    from .analysis import script as script_an

    if script_path and not script:
        path = Path(script_path).expanduser()
        media_store._check_root(svc, path)
        if not path.is_file():
            raise NotFound(f"There is no script at {path}.")
        script = path.read_text(encoding="utf-8", errors="replace")
    segments = script_an.parse_script(script)
    if not segments:
        raise LumiereError("The script has no text: paste it (Markdown with ## sections, a plan JSON or paragraphs).")
    transcript = _need(svc, [media], "transcript", "Assembling from the script")[media]
    words = transcript["words"]
    if not words:
        raise LumiereError("The transcript of that recording is empty: is there speech in it?")
    items: list[dict[str, Any]] = []
    used_mode = "words"
    missing: list[dict[str, Any]] = []
    notes = ""
    res = script_an.assemble(segments, words, take="best" if take == "best" else "last", min_coverage=min_coverage) if mode != "meaning" else None
    if res is not None and (mode == "words" or len(res["segments"]) * 2 > len(segments)):
        if not res["segments"]:
            best = 0.0
            tokens = [speech.norm(w["text"]) for w in words]
            for seg in segments[:12]:
                for t in script_an.find_takes(seg, words, tokens, min_coverage=0.0, max_takes=1):
                    best = max(best, t["coverage"])
            raise LumiereError(f"No segment of the script was found word for word in the recording (best match {round(best * 100)}% of a "
                               "segment's words). This mode is for recordings read from the script or a teleprompter; for a talk that "
                               "follows the script freely use mode='meaning' (the local model finds each part).", code="script_not_found")
        for s in res["segments"]:
            items.append({"segment": s["segment"], "title": s["title"], "ranges": [[s["src_in"], s["src_out"]]], "takes": s["takes"],
                          "coverage": s["coverage"]})
        missing = res["missing"]
    else:
        used_mode = "meaning"
        align = _meaning_alignment(svc, media, segments, transcript)
        sents = align["sentences"]
        by_seg = {part["segment"]: part["ranges"] for part in align["parts"]}
        notes = align.get("notes", "")
        for seg in segments:
            ranges = by_seg.get(seg.index + 1) or []
            if not ranges:
                missing.append({"segment": seg.index + 1, "title": seg.title, "text": seg.text[:120]})
                continue
            src = []
            for x, y in ranges:
                a = max(0, sents[x][0] - 150)
                b = sents[y][1] + 250
                if x > 0:
                    a = max(a, sents[x - 1][1])
                if y + 1 < len(sents):
                    b = min(b, max(sents[y + 1][0], sents[y][1]))
                if src and a <= src[-1][1] + 40 and a >= src[-1][0]:
                    src[-1][1] = max(src[-1][1], b)  # contiguous sentences play as one clip
                else:
                    src.append([a, b])
            items.append({"segment": seg.index + 1, "title": seg.title, "ranges": src, "takes": None, "coverage": None})
    if not items:
        raise LumiereError("No part of the script was found in the recording.", code="script_not_found")
    main = p.main_track()
    ops: list[dict[str, Any]] = []
    if main is not None and main.clips:
        ops.append({"op": "delete", "clips": [c.id for c in main.clips], "ripple": False})
    ops.append({"op": "marker_delete", "kind": "chapter"})
    p = _apply(svc, p, ops)
    main = p.main_track()
    seq = [{"media": media, "src_in": a, "src_out": b, "label": it["title"][:120]} for it in items for a, b in it["ranges"]]
    p = _apply(svc, p, [{"op": "sequence", "track": main.id if main else None, "items": seq}])
    if markers:
        cursor = 0
        mops = []
        for it in items:
            mops.append({"op": "marker_add", "t": cursor, "label": it["title"][:120], "kind": "chapter", "color": "#7FE3A0"})
            cursor += sum(b - a for a, b in it["ranges"])
        p = _apply(svc, p, mops)
    summary = {"mode": used_mode, "segments": len(items), "of": len(segments), "missing": missing, "duration": ms_to_tc(p.duration), "notes": notes,
               "retakes": sum(max(0, (it["takes"] or 1) - 1) for it in items),
               "chosen": [{"segment": it["segment"], "title": it["title"],
                           "at": ", ".join(f"{ms_to_tc(a)}–{ms_to_tc(b)}" for a, b in it["ranges"]), "takes": it["takes"],
                           "coverage": it["coverage"]} for it in items]}
    return p, summary


# ---------------------------------------------------------------- background music

def music_add(svc: "Services", p: Project, *, path: str = "", media: str = "", start_ms: Optional[int] = None, volume_db: float = -8.0,
              duck: bool = True, fade_in_ms: int = 600, fade_out_ms: int = 2500, replace: bool = True) -> tuple[Project, dict[str, Any]]:
    """Put a track under the whole edit on the music track: it starts on its first beat (or at ``start_ms``), is trimmed to the
    length of the edit, fades in and out, and ducks under speech. ``path`` is imported into the library when it is not there
    (nothing is copied); ``media`` is a library audio. ``replace`` clears the music track first."""
    from pathlib import Path

    from . import music

    if not path and not media:
        raise LumiereError("Give the audio file (path) or a library audio (media).")
    if media:
        info = media_store.get(svc, media)
        file = Path(info["path"])
    else:
        file = Path(path).expanduser()
        info = media_store.import_path(svc, str(file))
        media = info["id"]
        file = Path(info["path"])
    if not info["has_audio"]:
        raise LumiereError(f"{info['name']} has no sound.")
    total = p.duration
    if total <= 0:
        raise LumiereError("The project is empty: there is nothing to put music under.")
    grid = music.analyze_track(svc, file)
    src_in = int(grid["first_beat_ms"] if start_ms is None else start_ms)
    avail = int(info["duration_ms"])
    if src_in >= avail - 500:
        src_in = 0
    src_out = min(avail, src_in + total)
    length = src_out - src_in
    music_track = next((t for t in p.tracks if t.kind == "audio" and t.role == "music"), None)
    ops: list[dict[str, Any]] = []
    if music_track is None:
        ops.append({"op": "track_add", "kind": "audio", "name": "Música", "role": "music", "id": "trk_music"})
        track_id = "trk_music"
    else:
        track_id = music_track.id
        if replace and music_track.clips:
            ops.append({"op": "delete", "clips": [c.id for c in music_track.clips], "ripple": False})
    ops.append({"op": "add_media", "media": media, "track": track_id, "at": 0, "src_in": src_in, "src_out": src_out, "mode": "overwrite"})
    p = _apply(svc, p, ops)
    new_clip = next(c for c in p.track(track_id).clips if c.media == media and c.start == 0)
    p = _apply(svc, p, [
        {"op": "set", "clip": new_clip.id, "props": {"audio_fade_in": min(fade_in_ms, length // 4), "audio_fade_out": min(fade_out_ms, length // 3)}, "ripple": False},
        {"op": "track_set", "track": track_id, "props": {"duck": duck, "volume_db": volume_db}}])
    return p, {"track": track_id, "media": media, "name": info["name"], "bpm": grid["bpm"], "starts_at": ms_to_tc(src_in), "length": ms_to_tc(length),
               "ends_early_ms": max(0, total - length), "ducking": duck, "volume_db": volume_db}


# ---------------------------------------------------------------- registry

def short_from_range(svc: "Services", media: str, start_ms: int, end_ms: int, *, name: str, preset: str = "reels", reframe_mode: str = "auto",
                     with_captions: bool = True) -> dict[str, Any]:
    """A new vertical project from one range of a long video (for highlights): reframed and with captions when available."""
    proj = project_store.create(svc, name, preset=preset)
    pid = proj["id"]
    project_store.edit(svc, pid, [{"op": "add_media", "media": media, "src_in": start_ms, "src_out": end_ms}], label="Fragmento", actor="tool")
    notes = []
    p = project_store.doc(svc, pid)
    try:
        p, _ = reframe(svc, p, aspect=f"{PRESETS[preset]['width']}:{PRESETS[preset]['height']}", mode=reframe_mode)
    except NeedsAnalysis as need:
        notes.append(str(need))
    if with_captions:
        try:
            p, _ = captions(svc, p, enabled=True, style="pop")
        except NeedsAnalysis as need:
            p = _apply(svc, p, [{"op": "captions", "enabled": True, "style": "pop"}])
            notes.append(str(need))
    project_store.save(svc, pid, p, "Corto automático", actor="tool")
    return {"project": pid, "notes": notes}


def _multicam_command(name: str) -> Callable[..., tuple[Project, dict[str, Any]]]:
    """Multicam commands live in multicam.py, which reads this module (timeline_transcript): bind them late."""
    def run(svc: "Services", p: Project, **kwargs: Any) -> tuple[Project, dict[str, Any]]:
        from . import multicam

        return getattr(multicam, name)(svc, p, **kwargs)
    return run


COMMANDS: dict[str, Callable[..., tuple[Project, dict[str, Any]]]] = {
    "remove_silences": remove_silences, "remove_fillers": remove_fillers, "cut_words": cut_words, "split_scenes": split_scenes,
    "reframe": reframe, "captions": captions, "beat_sync": beat_sync, "match_loudness": match_loudness, "script_assemble": script_assemble,
    "zoom_cuts": zoom_cuts, "speaker_cut": speaker_cut,
    "multicam_create": _multicam_command("multicam_create"), "multicam_resync": _multicam_command("multicam_resync"),
    "multicam_auto": _multicam_command("multicam_auto"), "music_add": music_add,
}


def run(svc: "Services", project_id: str, name: str, args: dict[str, Any], *, actor: str = "ui", preview: bool = False) -> dict[str, Any]:
    fn = COMMANDS.get(name)
    if fn is None:
        raise LumiereError(f"Unknown command {name!r}. Known: {', '.join(COMMANDS)}.")
    p = project_store.doc(svc, project_id)
    before = p.duration
    try:
        new, summary = fn(svc, p, **args)
    except TypeError as error:
        raise LumiereError(f"{name}: {error}") from error
    if preview:
        return {"project": project_id, "preview": True, "summary": summary, "duration_before": ms_to_tc(before), "duration_after": ms_to_tc(new.duration)}
    label = {"remove_silences": "Quitar silencios", "remove_fillers": "Quitar muletillas", "cut_words": "Editar por texto",
             "split_scenes": "Cortar por escenas", "reframe": "Reencuadre", "captions": "Subtítulos", "beat_sync": "Corte al ritmo",
             "match_loudness": "Igualar volumen", "script_assemble": "Montaje desde guion", "zoom_cuts": "Zoom en los cortes", "speaker_cut": "Cortar por hablante",
             "multicam_create": "Crear multicámara", "multicam_resync": "Resincronizar multicámara", "multicam_auto": "Cambio automático de cámara",
             "music_add": "Música de fondo"}[name]
    rev = project_store.save(svc, project_id, new, label, actor)
    return {"project": project_id, "rev": rev, "summary": summary, "duration": ms_to_tc(new.duration), "duration_ms": new.duration}
