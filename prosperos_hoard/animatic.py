"""The animatic: a production's cut made only from what is already cheap -
the stills, the chosen song take and its lyric/section timing - so the
edit can be judged before the expensive clips are rendered.

The real run rendered 48 stills in 57 min and every 5 s Wan clip in about
9.5 min, and only at the very end did the cut turn out to "look like a
slideshow". An animatic shows the same cut first: `engine.auto_cut` with
the same song, lyrics and options as the final (so the cut points are the
final ones), each shot's still with a Ken Burns move and a crossfade into
it, captions and finishing, rendered with ffmpeg at 720p in each aspect the
production targets, plus a `plan.json` that lists every cut and every shot
(screen time, the still used, whether it will become a Wan clip) and the
estimated GPU time of the clips still to make.

As a production stage (after frames and lyrics, before clips) it pauses the
production at `awaiting_review` unless `settings.animatic_autocontinue`;
`studio_production_continue` goes on to the clips.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import engine
from . import productions as prod
from . import timeline as timeline_mod
from . import video as video_mod
from .ids import new_id
from .jobs import JobCancelled
from .store import NotFound, Store
from .util import now_iso

# From the real run (docs/examples/no-mires-atras.md): 48 stills in 57 min on
# one 16 GB card, a 5 s Wan 2.2 TI2V clip in ~9.5 min, a 1080p final render
# ~1.75 min (four renders in ~7 min, CPU). Override with settings.gpu_minutes.
DEFAULT_GPU_MINUTES = {"clip": 9.5, "still": round(57 / 48, 2), "final_render": 1.75}
CROSSFADE_S = 0.35


def _still_for(state: dict[str, Any], key: str) -> Optional[str]:
    base, variant = prod.split_key(key)
    entry = ((state.get("done", {}).get("frames") or {}).get("items") or {}).get(base)
    if not entry:
        return None
    if variant == 0:
        return entry.get("best")
    others = [a for a in entry.get("variants") or [] if a != entry.get("best")]
    return others[variant - 1] if len(others) >= variant else entry.get("best")


def _project_id(state: dict[str, Any]) -> str:
    return state.get("project_id") or (state.get("done", {}).get("1") or {}).get("project_id")


def cut_inputs(state: dict[str, Any], prefer_clips: bool) -> tuple[list[str], dict[str, list[str]], dict[str, str]]:
    """(pool, storyboard pools, asset -> shot key) for the auto-cut, with
    each storyboard entry resolved to its clip (final cut) or its still
    (animatic)."""
    spec = state.get("spec") or {}
    frames = (state.get("done", {}).get("frames") or {}).get("items") or {}
    clips = ((state.get("done", {}).get("clips") or {}).get("items") or {}) if prefer_clips else {}
    owner: dict[str, str] = {}
    for key, entry in frames.items():
        for i, aid in enumerate([entry.get("best")] + [v for v in entry.get("variants") or [] if v != entry.get("best")]):
            if aid:
                owner.setdefault(aid, prod.shot_key(key, i))
    owner.update({aid: key for key, aid in clips.items() if aid})
    pool = [e["best"] for e in frames.values() if e.get("best")] + [c for c in clips.values() if c]
    board = (spec.get("timeline") or {}).get("storyboard") or {}
    pools = {section: [a for a in (clips.get(k) or _still_for(state, k) for k in keys) if a] for section, keys in board.items()}
    return pool, pools, owner


def cut_options(state: dict[str, Any]) -> dict[str, Any]:
    options = dict(((state.get("spec") or {}).get("timeline") or {}).get("options") or {})
    options.setdefault("fps", 24)
    return options


def build(store: Store, state: dict[str, Any]) -> dict[str, Any]:
    """The animatic's cut (the final cut's cut points, stills only) and its
    plan. Needs the song and the stills."""
    done = state.get("done") or {}
    song_id = (done.get("song") or {}).get("song_asset_id")
    if not song_id:
        raise prod.ProductionError("animatic_needs_song", "the animatic needs the song first (the song stage)")
    pool, pools, owner = cut_inputs(state, prefer_clips=False)
    if not pool:
        raise prod.ProductionError("animatic_needs_frames", "the animatic needs the stills first (the frames stage)")
    options = cut_options(state)
    if any(pools.values()):
        options["section_pools"] = {k: v for k, v in pools.items() if v}
    lyrics_id = (done.get("lyrics") or {}).get("lyrics_asset_id")
    cut = engine.auto_cut(store, _project_id(state), song_id, pool, None, lyrics_id, options)
    visual = next(t for t in cut["tracks"] if t["type"] == "visual")
    # one crossfade into every shot (a third of it at most), the Ken Burns
    # move the auto-cut already gave each still
    for i, clip in enumerate(visual["clips"]):
        if i > 0:
            clip["transition_in"] = {"type": "crossfade", "duration_s": round(min(CROSSFADE_S, clip["duration_s"] / 3,
                                                                                  visual["clips"][i - 1]["duration_s"] / 3), 3)}
        clip.setdefault("ken_burns", {"zoom_start": 1.0, "zoom_end": 1.08, "pan": "none"})
    return {"tracks": cut["tracks"], "fps": cut["fps"], "sections": cut["sections"], "duration_s": cut["duration_s"],
            "owner": owner, "song_asset_id": song_id}


def plan(state: dict[str, Any], cut: dict[str, Any], gpu_minutes: Optional[dict[str, float]] = None) -> dict[str, Any]:
    spec = state.get("spec") or {}
    minutes = {**DEFAULT_GPU_MINUTES, **(gpu_minutes or (state.get("settings") or {}).get("gpu_minutes") or {})}
    visual = next(t for t in cut["tracks"] if t["type"] == "visual")
    sections = cut.get("sections") or []
    cuts = []
    per_shot: dict[str, dict[str, Any]] = {}
    for i, clip in enumerate(visual["clips"]):
        key = cut["owner"].get(clip["asset_id"], "?")
        section = next((s.get("label") for s in sections
                        if s["start_s"] <= clip["start_s"] + 0.06 < (s["end_s"] if s.get("end_s") is not None else float("inf"))), None)
        cuts.append({"index": i, "start_s": clip["start_s"], "duration_s": clip["duration_s"], "shot": key,
                     "still": clip["asset_id"], "section": section})
        entry = per_shot.setdefault(key, {"screen_time_s": 0.0, "cuts": 0})
        entry["screen_time_s"] += clip["duration_s"]
        entry["cuts"] += 1
    existing = (state.get("done", {}).get("clips") or {}).get("items") or {}
    shots = []
    to_render = []
    for shot in spec.get("shots") or []:
        clip_keys = [prod.shot_key(shot["key"], v) for v in shot.get("clips") or []]
        pending = [k for k in clip_keys if k not in existing and not (shot.get("reuse_clips") or {}).get(k)]
        to_render += pending
        used = {k: v for k, v in per_shot.items() if prod.split_key(k)[0] == shot["key"]}
        shots.append({"key": shot["key"], "lead": bool(shot.get("lead")), "prompt": shot.get("prompt"),
                      "motion": shot.get("motion"), "still": _still_for(state, shot["key"]),
                      "screen_time_s": round(sum(v["screen_time_s"] for v in used.values()), 2),
                      "cuts": sum(v["cuts"] for v in used.values()),
                      "will_be_clip": bool(clip_keys), "clip_keys": clip_keys, "clips_to_render": pending})
    unused = [s["key"] for s in shots if s["cuts"] == 0]
    renders = len((spec.get("timeline") or {}).get("aspects") or []) * len((spec.get("timeline") or {}).get("qualities") or [])
    return {
        "production": state.get("slug"), "made_at": now_iso(), "song_asset_id": cut["song_asset_id"],
        "duration_s": round(cut["duration_s"], 2), "fps": cut["fps"], "cuts_total": len(cuts),
        "cuts": cuts, "shots": shots, "unused_shots": unused,
        "clips_planned": len(to_render), "clips_to_render": to_render,
        "gpu_minutes": round(len(to_render) * minutes["clip"], 1),
        "cpu_minutes_renders": round(renders * minutes["final_render"], 1),
        "minutes_per": minutes,
        "note": "cut points are the final cut's: same song, lyrics and options; the clips replace the stills",
    }


def render(store: Store, state: dict[str, Any], cut: dict[str, Any], aspect: str, progress: Optional[Callable[..., None]] = None,
           should_cancel: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """One aspect's animatic as a video asset (720p, the stills, crossfades,
    captions and the finishing pass)."""
    if aspect not in timeline_mod.ASPECTS:
        raise prod.ProductionError("bad_aspect", f"aspect must be one of {', '.join(timeline_mod.ASPECTS)}")
    width, height = timeline_mod.ASPECTS[aspect]
    finishing = ((state.get("spec") or {}).get("timeline") or {}).get("finishing") or {}
    timeline = {"name": f"Animatic {aspect}", "width": width, "height": height, "fps": cut["fps"],
                "audio_asset_id": cut["song_asset_id"], "tracks": cut["tracks"], "finishing": finishing,
                "project_id": _project_id(state)}

    def asset_path_for(asset_id: str) -> Path:
        return store.data_dir / store.get_asset(asset_id)["file_path"]

    out_id = new_id("a")
    out_path = store.path_for_asset_file(out_id, ".mp4")
    work_dir = store.data_dir / "tmp" / f"animatic_{out_id}"
    started = time.monotonic()
    try:
        result = video_mod.render_timeline(timeline, asset_path_for, work_dir, out_path, quality="animatic",
                                           progress=progress, should_cancel=should_cancel)
    except video_mod.RenderCancelled:
        out_path.unlink(missing_ok=True)
        raise JobCancelled("cancelled") from None
    except video_mod.RenderError as exc:
        out_path.unlink(missing_ok=True)
        raise prod.ProductionError("animatic_failed", str(exc)) from None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    try:
        thumb = engine._video_thumbnail(out_path, store.path_for_thumb(out_id))
    except Exception:  # noqa: BLE001 - a missing thumbnail never fails the animatic
        thumb = None
    recipe = {"operation": "animatic", "production": state.get("slug"), "aspect": aspect, "quality": "animatic",
              "input_asset_ids": sorted({c["asset_id"] for c in next(t for t in cut["tracks"] if t["type"] == "visual")["clips"]})[:50],
              "elapsed_s": round(time.monotonic() - started, 2), "created_at": now_iso()}
    asset = store.create_asset(project_id=_project_id(state), kind="video", file_path=out_path.relative_to(store.data_dir).as_posix(),
                               mime="video/mp4", width=result["width"], height=result["height"], duration_s=result["duration_s"],
                               thumb_path=thumb, source="rendered", recipe=recipe, asset_id=out_id,
                               name=f"Animatic {aspect} - {state.get('name') or state.get('slug')}"[:100], tags=["animatic", aspect])
    return asset


def shot_sheet(store: Store, state: dict[str, Any]) -> Optional[str]:
    """A contact sheet of every shot's best still, labelled with its key
    ("clip" on the shots that become Wan clips), as an image asset: one
    look shows the whole shot list at the review pause. None when there is
    no still to show."""
    spec = state.get("spec") or {}
    frames = (state.get("done", {}).get("frames") or {}).get("items") or {}
    paths: list[Path] = []
    labels: list[str] = []
    order: list[str] = []
    for shot in spec.get("shots") or []:
        still = (frames.get(shot["key"]) or {}).get("best")
        if not still:
            continue
        try:
            path = store.data_dir / store.get_asset(still)["file_path"]
        except NotFound:
            continue
        if not path.is_file():
            continue
        paths.append(path)
        order.append(still)
        labels.append(f"{shot['key']}{' - clip' if shot.get('clips') else ''}{' - lead' if shot.get('lead') else ''}")
    if not paths:
        return None
    cell = 320 if len(paths) <= 24 else 240
    try:
        sheet = engine.contact_sheet(paths, cols=min(4, len(paths)), cell=cell, labels=labels)
    except OSError:
        return None  # an unreadable still never fails the animatic
    recipe = {"operation": "animatic_shot_sheet", "production": state.get("slug"), "input_asset_ids": order[:80],
              "labels": labels[:80], "created_at": now_iso()}
    asset = engine._save_sheet(store, _project_id(state), sheet, recipe,
                               f"Shots - {state.get('name') or state.get('slug')}"[:100])
    return asset["id"]


def _made_at() -> str:
    # to the microsecond: an approval names the animatic it approves, and
    # two animatics made within one second must not share a name
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def make(store: Store, state: dict[str, Any], aspects: Optional[list[str]] = None,
         progress: Optional[Callable[..., None]] = None, should_cancel: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Build, render every aspect, make the shot contact sheet and write
    `plan.json`; returns the `done["animatic"]` entry."""
    report = progress or (lambda *_a, **_k: None)
    aspects = aspects or ((state.get("spec") or {}).get("timeline") or {}).get("aspects") or ["9:16"]
    report(0.02, "cutting the animatic")
    cut = build(store, state)
    the_plan = plan(state, cut)
    the_plan["made_at"] = _made_at()
    renders: dict[str, str] = {}
    for i, aspect in enumerate(aspects):
        def sub(frac: float, msg: Optional[str] = None, i: int = i) -> None:
            report(0.05 + 0.9 * (i + frac) / len(aspects), f"animatic {aspect}: {msg or ''}".strip())
        renders[aspect] = render(store, state, cut, aspect, sub, should_cancel)["id"]
    the_plan["renders"] = renders
    sheet_id = shot_sheet(store, state)
    the_plan["contact_sheet_id"] = sheet_id
    folder = prod.production_dir(store.data_dir, state["slug"]) / "animatic"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.json").write_text(json.dumps(the_plan, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"renders": renders, "plan": {k: the_plan[k] for k in ("cuts_total", "clips_planned", "gpu_minutes",
                                                                   "cpu_minutes_renders", "unused_shots", "duration_s")},
            "plan_path": "animatic/plan.json", "made_at": the_plan["made_at"], "contact_sheet_id": sheet_id}


def read_plan(data_dir: Path, slug: str) -> dict[str, Any]:
    path = prod.production_dir(data_dir, slug) / "animatic" / "plan.json"
    if not path.is_file():
        raise NotFound("animatic", slug)
    return json.loads(path.read_text(encoding="utf-8"))


def make_for_production(store: Store, slug: str, aspects: Optional[list[str]] = None,
                        progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """`studio_animatic`: (re)make the animatic of a production - one made
    in the app (its stills so far) or by the production script (read
    through `state_from_legacy`) - and record it in its state.

    It becomes the production's animatic (`done["animatic"]`, which the
    run's review gate then holds at until it is approved) only when every
    shot has its still and the production is not running; otherwise it is
    kept as `animatic_preview`, and the production makes its own animatic
    (and pauses at it) once the frames are done."""
    raw = prod.load_state(store.data_dir, slug)
    legacy = prod.is_legacy(raw)
    view = prod.state_from_legacy(store, raw) if legacy else raw
    if not legacy and raw.get("status") == "running":
        raise prod.ProductionError("production_running", "the production is running; its own animatic stage makes one")
    cancelled = getattr(progress, "cancelled", None)
    entry = make(store, view, aspects, progress, cancelled)
    with prod.lock_for(slug):
        # this job runs on the cpu lane and can overlap a production run
        # that started meanwhile: that run owns `done` (its save keeps only
        # `review`, `animatic_preview` and `qa` from the file)
        current = prod.load_state(store.data_dir, slug)
        if legacy:
            current["animatic"] = entry
        elif current.get("status") == "running" or not frames_complete(current):
            current["animatic_preview"] = entry
            entry = {**entry, "preview": True}
            prod.log(current, "animatic", "animatic_preview", renders=entry["renders"])
        else:
            current.setdefault("done", {})["animatic"] = entry
            current.pop("animatic_preview", None)
            prod.log(current, "animatic", "animatic", renders=entry["renders"], gpu_minutes=entry["plan"]["gpu_minutes"])
        prod.save_state(store.data_dir, current)
    return entry


def frames_complete(state: dict[str, Any]) -> bool:
    """Every shot of the spec has its still (the frames stage is done)."""
    if prod.stage_status(state, "frames") != "done":
        return False
    items = ((state.get("done") or {}).get("frames") or {}).get("items") or {}
    return all((items.get(s["key"]) or {}).get("best") for s in (state.get("spec") or {}).get("shots") or [])
