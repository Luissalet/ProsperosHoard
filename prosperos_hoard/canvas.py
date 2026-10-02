"""A Spotify Canvas from a music video: a short vertical silent loop of
the chorus (3-8 s, 9:16, at least 720 px tall), cut from the production's
own render - nothing is generated again.

The loop is seamless: its tail crossfades into the stretch just before
its start (video.build_loop_cmd). The start is the first chorus of the
song's timing, else its loudest section, else a quarter of the way in."""

from __future__ import annotations

import shutil
import time
from typing import Any, Optional

from . import engine, procutil
from . import productions as prod
from . import video as video_mod
from .backend import ffmpeg_path
from .ids import new_id
from .store import Store
from .util import now_iso

MIN_S, MAX_S, FADE_S = 3.0, 8.0, 0.6


def pick_render(state: dict[str, Any]) -> tuple[str, str]:
    """(asset_id, aspect) of the cut to loop: 9:16 first, final before preview."""
    timelines = ((state.get("done") or {}).get("timeline") or {}).get("timelines") or {}
    order = sorted(timelines, key=lambda a: (a != "9:16", a))
    for aspect in order:
        renders = timelines[aspect].get("renders") or timelines[aspect].get("previous_renders") or {}
        rid = renders.get("final") or renders.get("preview")
        if rid:
            return rid, aspect
    raise prod.ProductionError("no_cut", "the production has no rendered cut yet; finish it first")


def pick_start(store: Store, state: dict[str, Any], duration_s: float, seconds: float) -> tuple[float, str]:
    """(start_s, why): the first chorus, else the loudest section, else 25 %."""
    latest = max(0.0, duration_s - seconds - FADE_S - 0.05)
    try:
        timing = prod.shot_timing(store.data_dir, store, state)
    except Exception:  # noqa: BLE001 - no timing: fall back below
        timing = {}
    for sec in timing.get("sections") or []:
        if str(sec.get("kind") or "").lower() == "chorus" or "chorus" in str(sec.get("label") or "").lower():
            return round(min(max(0.0, float(sec.get("start_s") or 0)), latest), 3), "chorus"
    song_id = ((state.get("done") or {}).get("song") or {}).get("song_asset_id")
    if song_id:
        try:
            analysis = store.get_asset(song_id).get("analysis") or {}
            loud = [s for s in analysis.get("sections") or [] if s.get("energy") == "high"]
            if loud:
                return round(min(float(loud[0]["start_s"]), latest), 3), "loudest section"
        except Exception:  # noqa: BLE001
            pass
    return round(min(duration_s * 0.25, latest), 3), "a quarter in"


def window_timeline(timeline: dict[str, Any], start_s: float, length_s: float) -> dict[str, Any]:
    """The stretch [start, start+length] of a cut as a timeline of its own:
    the clips that cover it (the first one trimmed, the last one shortened),
    no lyric captions and no audio - a Canvas plays silent behind the
    track's title, so burned-in words would only fight it. The look stays;
    the beat effects go, since they follow the song from its start."""
    visual = next((t for t in timeline["tracks"] if t["type"] == "visual"), {"clips": []})
    end = start_s + length_s
    clips, at = [], 0.0
    for clip in visual["clips"]:
        dur = float(clip["duration_s"])
        c_start, c_end = at, at + dur
        at = c_end
        if c_end <= start_s or c_start >= end:
            continue
        head = max(0.0, start_s - c_start)
        piece = dict(clip, duration_s=round(min(c_end, end) - max(c_start, start_s), 4))
        if head and clip.get("kind") == "video":
            piece["trim_start_s"] = round(float(clip.get("trim_start_s") or 0) + head, 4)
        if not clips:
            piece["transition_in"] = {"type": "cut", "duration_s": 0.0}
        if piece["duration_s"] > 0.02:
            clips.append(piece)
    finishing = {k: v for k, v in (timeline.get("finishing") or {}).items() if k not in ("beat_fx", "lyric_style")}
    return {"width": timeline["width"], "height": timeline["height"], "fps": timeline["fps"], "audio_asset_id": None,
            "finishing": finishing, "tracks": [{"type": "visual", "clips": clips}]}


def make_canvas(store: Store, slug: str, seconds: float = 8.0, start_s: Optional[float] = None,
                lyrics: bool = False) -> dict[str, Any]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise prod.ProductionError("no_ffmpeg", "ffmpeg not found")
    if not MIN_S <= float(seconds) <= MAX_S:
        raise prod.ProductionError("bad_canvas", f"a Canvas lasts between {MIN_S:g} and {MAX_S:g} seconds")
    state = prod.load_fresh(store.data_dir, slug)
    if prod.is_legacy(state):
        raise prod.ProductionError("legacy_production", "a scripted production has no cut to loop")
    rid, aspect = pick_render(state)
    src_asset = store.get_asset(rid)
    src = store.data_dir / src_asset["file_path"]
    duration = float(src_asset.get("duration_s") or 0)
    if duration < seconds + FADE_S + 0.1:
        raise prod.ProductionError("cut_too_short", f"the cut lasts {duration:.1f} s; a {seconds:g} s loop needs a longer one")
    if start_s is None:
        start, why = pick_start(store, state, duration, seconds)
    else:
        start, why = round(min(max(0.0, float(start_s)), duration - seconds - FADE_S - 0.05), 3), "chosen"
    out_id = new_id("a")
    out_path = store.path_for_asset_file(out_id, ".mp4")
    started = time.monotonic()
    work = store.data_dir / "tmp" / f"canvas_{out_id}"
    loop_src, loop_start = src, start
    src_w, src_h = int(src_asset.get("width") or 1080), int(src_asset.get("height") or 1920)
    try:
        tl_id = (((state.get("done") or {}).get("timeline") or {}).get("timelines") or {}).get(aspect, {}).get("timeline_id")
        if not lyrics and tl_id:
            # the stretch again without the burned-in lyrics (only those few seconds)
            try:
                timeline = store.get_timeline(tl_id)
            except Exception:  # noqa: BLE001 - no timeline: loop the render as it is
                timeline = None
            if timeline:
                window = window_timeline(timeline, start, float(seconds) + FADE_S + 0.1)
                if window["tracks"][0]["clips"]:
                    clean = work / "window.mp4"
                    try:
                        info = video_mod.render_timeline(window, lambda aid: store.data_dir / store.get_asset(aid)["file_path"],
                                                         work / "render", clean, quality="preview")
                        loop_src, loop_start, src_w, src_h = clean, 0.0, int(info["width"]), int(info["height"])
                    except video_mod.RenderError:
                        pass  # fall back to the render, captions and all
        cmd = video_mod.build_loop_cmd(ffmpeg, loop_src, out_path, loop_start, float(seconds), FADE_S, 720, 1280, 30,
                                       src_w, src_h)
        result = procutil.run(cmd, capture_output=True, text=True, timeout=600)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if result.returncode != 0 or not out_path.is_file():
        out_path.unlink(missing_ok=True)
        raise prod.ProductionError("canvas_failed", (result.stderr or "ffmpeg failed")[-400:])
    try:
        thumb = engine._video_thumbnail(out_path, store.path_for_thumb(out_id))
    except Exception:  # noqa: BLE001
        thumb = None
    recipe = {"operation": "canvas", "production": slug, "source_asset_id": rid, "source_aspect": aspect,
              "start_s": start, "seconds": float(seconds), "start_from": why, "input_asset_ids": [rid],
              "lyrics": bool(lyrics or loop_src == src),
              "elapsed_s": round(time.monotonic() - started, 2), "created_at": now_iso()}
    asset = store.create_asset(project_id=src_asset["project_id"], kind="video",
                               file_path=out_path.relative_to(store.data_dir).as_posix(), mime="video/mp4",
                               width=720, height=1280, duration_s=float(seconds), thumb_path=thumb, source="rendered",
                               recipe=recipe, asset_id=out_id,
                               name=f"Canvas - {state.get('name') or slug}"[:100], tags=["canvas", "loop", "9:16"])
    with prod.lock_for(slug):
        fresh = prod.load_state(store.data_dir, slug)
        fresh.setdefault("done", {}).setdefault("extras", {})["canvas"] = out_id
        prod.log(fresh, "extras", "canvas", asset_id=out_id, start_s=start, start_from=why)
        prod.save_state(store.data_dir, fresh)
    return {"asset_id": out_id, "start_s": start, "start_from": why, "seconds": float(seconds), "source_asset_id": rid,
            "lyrics": bool(lyrics or loop_src == src)}
