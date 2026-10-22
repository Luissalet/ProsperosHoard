"""Style profiles extracted from video references (rhythm, shot cuts, color palette,
camera grammar, and transitions) and applied to productions.

Deterministic analysis using local ffprobe/ffmpeg engines without external runtimes,
proprietary APIs, or heavy models. Separates factual measured observations
(duration, exact cuts, color palette) from unobserved/editable grammar (camera movement,
shot framing, semantic transitions).

Provenance and reference attribution:
- ReelMimic (commit daa15e177605cd26363549d6aa0b9b1de8720e20, MIT)
- OpenDesign (commit e03d5c23f308c39f4de7c057568df66c29a84385, Apache-2.0)
Pinned in research-inbox/refs-20261022/sources.json.
"""

from __future__ import annotations

import io
import json
import math
from copy import deepcopy
import re
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from . import backend, procutil
from .ids import new_id
from .util import now_iso, write_text_atomic

FORMAT = "prospero.style_profile/1"
DEFAULT_SCENE_THRESHOLD = 0.3


def _profile_id(value: str) -> str:
    if not re.fullmatch(r"sp_[A-Za-z0-9_-]{1,64}", str(value)):
        raise StyleProfileError("bad_profile_id", "Invalid style profile identifier")
    return str(value)


class StyleProfileError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def style_profiles_dir(data_dir: Path) -> Path:
    p = Path(data_dir) / "style_profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_fps(fps_str: str) -> float:
    if not fps_str:
        return 24.0
    if "/" in fps_str:
        num, den = fps_str.split("/", 1)
        try:
            d = float(den)
            return round(float(num) / d, 3) if d != 0 else 24.0
        except ValueError:
            return 24.0
    try:
        return float(fps_str)
    except ValueError:
        return 24.0


def inspect_video_stream(video_path: Path) -> dict[str, Any]:
    ffprobe = backend.ffprobe_path()
    if not ffprobe:
        raise StyleProfileError("ffprobe_missing", "ffprobe binary not found on system PATH")
    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration,size,bit_rate:stream=width,height,r_frame_rate,duration,codec_name,codec_type",
        "-of", "json",
        str(video_path),
    ]
    proc = procutil.run(cmd, text=True)
    if proc.returncode != 0:
        raise StyleProfileError("probe_failed", f"ffprobe failed: {proc.stderr}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise StyleProfileError("probe_invalid_json", f"ffprobe output not json: {exc}")

    streams = data.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video_stream:
        raise StyleProfileError("no_video_stream", "File does not contain a video stream")

    format_info = data.get("format") or {}
    duration_s = float(format_info.get("duration") or video_stream.get("duration") or 0.0)
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    fps = _parse_fps(video_stream.get("r_frame_rate") or "24/1")

    aspect = "16:9"
    if width > 0 and height > 0:
        ratio = width / height
        if abs(ratio - 16 / 9) < 0.1:
            aspect = "16:9"
        elif abs(ratio - 9 / 16) < 0.1:
            aspect = "9:16"
        elif abs(ratio - 1.0) < 0.1:
            aspect = "1:1"
        elif abs(ratio - 4 / 3) < 0.1:
            aspect = "4:3"
        elif abs(ratio - 21 / 9) < 0.1:
            aspect = "21:9"
        else:
            aspect = f"{width}:{height}"

    return {
        "duration_s": round(duration_s, 3),
        "width": width,
        "height": height,
        "aspect": aspect,
        "fps": fps,
        "codec": video_stream.get("codec_name") or "unknown",
        "size_bytes": int(format_info.get("size") or 0),
    }


def detect_scene_cuts(video_path: Path, scene_threshold: float = DEFAULT_SCENE_THRESHOLD) -> list[float]:
    ffmpeg = backend.ffmpeg_path()
    if not ffmpeg:
        raise StyleProfileError("ffmpeg_missing", "ffmpeg binary not found on system PATH")

    # Use select filter with scene score threshold and showinfo to capture cut timestamps
    filter_expr = f"select='gt(scene\\,{scene_threshold:.2f})',showinfo"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i", str(video_path),
        "-filter:v", filter_expr,
        "-f", "null",
        "-",
    ]
    proc = procutil.run(cmd, text=True)
    # ffmpeg outputs showinfo to stderr
    if proc.returncode != 0:
        raise StyleProfileError("cut_analysis_failed", f"ffmpeg could not analyze scene changes: {proc.stderr}")
    output = proc.stderr or ""
    pts_matches = re.findall(r"pts_time:([0-9\.]+)", output)
    cuts: list[float] = []
    for m in pts_matches:
        try:
            t = round(float(m), 3)
            # Filter cuts too close to each other (debounce 0.15s)
            if not cuts or (t - cuts[-1]) >= 0.15:
                cuts.append(t)
        except ValueError:
            continue
    return cuts


def extract_frame_image(video_path: Path, at_s: float) -> Optional[Image.Image]:
    ffmpeg = backend.ffmpeg_path()
    if not ffmpeg:
        return None
    cmd = [
        ffmpeg,
        "-ss", f"{at_s:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-f", "image2",
        "-vcodec", "png",
        "-",
    ]
    proc = procutil.run(cmd, text=False)
    if proc.returncode == 0 and proc.stdout:
        try:
            return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
        except Exception:
            return None
    return None


def extract_dominant_palette(image: Optional[Image.Image], max_colors: int = 5) -> list[str]:
    if not image:
        return []
    try:
        thumb = image.resize((64, 64), Image.Resampling.BILINEAR)
        quantized = thumb.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
        colors = quantized.getcolors() or []
        palette = quantized.getpalette() or []
        if not colors or not palette:
            return []
        colors.sort(reverse=True, key=lambda x: x[0])
        hex_list = []
        for _count, idx in colors[:max_colors]:
            base = idx * 3
            if base + 2 < len(palette):
                r, g, b = palette[base], palette[base + 1], palette[base + 2]
                hex_list.append(f"#{r:02x}{g:02x}{b:02x}")
        return hex_list
    except Exception:
        return []


def extract_style_profile_from_video(
    video_path: str | Path,
    name: str = "",
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    max_palette_colors: int = 5,
) -> dict[str, Any]:
    path = Path(video_path).resolve()
    if not math.isfinite(scene_threshold) or not 0 < scene_threshold <= 1:
        raise StyleProfileError("bad_threshold", "Scene threshold must be finite and between 0 and 1")
    if not path.is_file():
        raise StyleProfileError("file_not_found", f"Video file not found: {path}")

    meta = inspect_video_stream(path)
    duration_s = meta["duration_s"]
    if duration_s <= 0.05:
        raise StyleProfileError("video_too_short", f"Video duration {duration_s}s is too short for analysis")

    raw_cuts = detect_scene_cuts(path, scene_threshold=scene_threshold)
    # Ensure cuts are strictly within (0, duration_s)
    valid_cuts = [c for c in raw_cuts if 0.05 < c < (duration_s - 0.05)]
    valid_cuts.sort()

    # Build shots intervals
    shot_spans: list[tuple[float, float]] = []
    prev = 0.0
    for cut in valid_cuts:
        shot_spans.append((prev, cut))
        prev = cut
    shot_spans.append((prev, duration_s))

    shot_count = len(shot_spans)
    shot_durations = [round(e - s, 3) for s, e in shot_spans]
    avg_duration = round(sum(shot_durations) / shot_count, 3) if shot_count else 0.0
    pacing_bpm = round((shot_count / duration_s) * 60.0, 2) if duration_s > 0 else 0.0

    # Extract global palette from the middle of the whole video
    mid_video_frame = extract_frame_image(path, at_s=duration_s / 2.0)
    global_palette = extract_dominant_palette(mid_video_frame, max_colors=max_palette_colors)

    measured_shots: list[dict[str, Any]] = []
    editable_shots: list[dict[str, Any]] = []

    for idx, (s, e) in enumerate(shot_spans, start=1):
        key = str(idx)
        dur = round(e - s, 3)
        mid_t = round(s + (dur / 2.0), 3)
        shot_frame = extract_frame_image(path, at_s=mid_t)
        shot_palette = extract_dominant_palette(shot_frame, max_colors=max_palette_colors)

        # Measured facts strictly observed
        measured_shots.append({
            "key": key,
            "start_s": round(s, 3),
            "end_s": round(e, 3),
            "duration_s": dur,
            "sample_time_s": mid_t,
            "palette": shot_palette,
            "cut_transition_measured": True if idx > 1 else False,
        })

        # Relative duration for scaling to other productions
        rel_dur = round(dur / duration_s, 4) if duration_s > 0 else (1.0 / shot_count)

        # Energy estimation based on shot brevity
        energy = "fast" if dur < 2.0 else ("medium" if dur < 5.0 else "slow")

        # Editable grammar: camera and semantic transitions are marked unobserved
        editable_shots.append({
            "key": key,
            "relative_duration": rel_dur,
            "span": {"start_s": round(s, 3), "end_s": round(e, 3), "duration_s": dur},
            "palette": shot_palette,
            "camera": {
                "observed": False,
                "shot_type": "unknown",
                "angle": "unknown",
                "movement": "unknown",
                "note": "Field not observed deterministically from cut analysis; editable.",
            },
            "transition_in": {
                "type": "cut" if idx > 1 else "none",
                "observed": True if idx > 1 else False,
                "measured": True if idx > 1 else False,
            },
            "motion_energy": energy,
            "motion_energy_method": "suggested from shot duration; subject movement is unobserved",
            "notes": "",
        })

    profile_id = f"sp_{new_id()}"
    display_name = name.strip() or f"Style from {path.stem}"

    profile = {
        "format": FORMAT,
        "id": profile_id,
        "name": display_name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "source_reference": {
            "source_path": str(path),
            "filename": path.name,
            "duration_s": duration_s,
            "width": meta["width"],
            "height": meta["height"],
            "aspect": meta["aspect"],
            "fps": meta["fps"],
            "codec": meta["codec"],
        },
        "measured_facts": {
            "cut_detection": {"method": "ffmpeg scene-change score; estimated boundaries", "threshold": scene_threshold},
            "palette_method": "quantized midpoint frame per shot; not every frame of the video",
            "duration_s": duration_s,
            "shot_count": shot_count,
            "cuts_s": valid_cuts,
            "shot_durations_s": shot_durations,
            "fps": meta["fps"],
            "resolution": {"width": meta["width"], "height": meta["height"], "aspect": meta["aspect"]},
            "dominant_palette": global_palette,
            "shots": measured_shots,
        },
        "pacing": {
            "shot_count": shot_count,
            "average_shot_duration_s": avg_duration,
            "pacing_bpm": pacing_bpm,
            "rhythm_durations_s": shot_durations,
        },
        "shots": editable_shots,
        "palette": global_palette,
        "provenance": {
            "inspired_by": ["ReelMimic", "OpenDesign"],
            "source_commit_reelmimic": "daa15e177605cd26363549d6aa0b9b1de8720e20",
            "source_commit_opendesign": "e03d5c23f308c39f4de7c057568df66c29a84385",
            "method": "native deterministic extraction via ffprobe/ffmpeg, no external API runtime",
        },
    }
    return profile


def save_style_profile(data_dir: Path, profile: dict[str, Any]) -> dict[str, Any]:
    pid = _profile_id(profile.get("id") or f"sp_{new_id()}")
    profile["id"] = pid
    profile["updated_at"] = now_iso()
    out_dir = style_profiles_dir(data_dir)
    file_path = out_dir / f"{pid}.json"
    content = json.dumps(profile, indent=2, ensure_ascii=False) + "\n"
    write_text_atomic(file_path, content)
    return profile


def load_style_profile(data_dir: Path, profile_id: str) -> dict[str, Any]:
    pid = _profile_id(profile_id)
    file_path = style_profiles_dir(data_dir) / f"{pid}.json"
    if not file_path.is_file():
        raise StyleProfileError("profile_not_found", f"Style profile {profile_id} not found")
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StyleProfileError("corrupted_profile", f"Style profile {profile_id} invalid JSON: {exc}")


def list_style_profiles(data_dir: Path) -> list[dict[str, Any]]:
    out_dir = style_profiles_dir(data_dir)
    profiles = []
    for file_path in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if data.get("format") == FORMAT:
                profiles.append({
                    "id": data.get("id", file_path.stem),
                    "name": data.get("name", file_path.stem),
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "shot_count": len(data.get("shots") or []),
                    "duration_s": (data.get("measured_facts") or {}).get("duration_s"),
                    "pacing_bpm": (data.get("pacing") or {}).get("pacing_bpm"),
                    "palette": data.get("palette") or [],
                })
        except Exception:
            continue
    return profiles


def update_style_profile(data_dir: Path, profile_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    profile = load_style_profile(data_dir, profile_id)
    if "name" in patch and patch["name"]:
        profile["name"] = str(patch["name"]).strip()
    if "palette" in patch and isinstance(patch["palette"], list):
        profile["palette"] = [str(c) for c in patch["palette"]]
    if "shots" in patch and isinstance(patch["shots"], list):
        # Update editable shots while retaining format
        profile["shots"] = patch["shots"]
    if "notes" in patch:
        profile["notes"] = str(patch["notes"])
    return save_style_profile(data_dir, profile)


def delete_style_profile(data_dir: Path, profile_id: str) -> bool:
    pid = _profile_id(profile_id)
    file_path = style_profiles_dir(data_dir) / f"{pid}.json"
    if file_path.is_file():
        file_path.unlink()
        return True
    return False


def prompt_hints(shot: dict[str, Any], moving: bool = False) -> str:
    grammar = shot.get("style_profile_grammar") or {}
    camera = grammar.get("camera") or {}
    fields = ("shot", "angle", "move") if moving else ("shot", "angle")
    hints = [str(camera[k]).replace("_", " ") for k in fields if camera.get(k)]
    palette = grammar.get("palette") or []
    if palette:
        hints.append("Color palette " + ", ".join(palette))
    return ". ".join(hints)


def apply_timeline_transitions(store, state: dict[str, Any], timeline_id: str) -> None:
    from . import engine, productions as prod
    by_asset = {}
    done = state.get("done") or {}
    for shot in state["spec"].get("shots", []):
        transition = (shot.get("style_profile_grammar") or {}).get("transition_in")
        if not transition:
            continue
        assets = list((((done.get("frames") or {}).get("items") or {}).get(shot["key"]) or {}).get("variants") or [])
        assets += [v for k, v in ((done.get("clips") or {}).get("items") or {}).items() if prod.split_key(k)[0] == shot["key"]]
        for asset in assets:
            by_asset[asset] = transition
    if not by_asset:
        return
    timeline = store.get_timeline(timeline_id)
    visual = next((t for t in timeline["tracks"] if t["type"] == "visual"), {})
    updates = []
    for i, clip in enumerate(visual.get("clips", [])):
        if clip.get("asset_id") not in by_asset:
            continue
        transition = dict(by_asset[clip["asset_id"]])
        if transition["type"] != "cut":
            transition["duration_s"] = min(transition.get("duration_s", 0.35), clip["duration_s"] / 2)
        updates.append({"index": i, "transition_in": transition})
    if updates:
        engine.update_timeline(store, timeline_id, {"clip_updates": updates})


def apply_style_profile_to_production(data_dir: Path, slug: str, profile_or_id: str | dict[str, Any],
                                      options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    from . import productions as prod
    profile = load_style_profile(data_dir, profile_or_id) if isinstance(profile_or_id, str) else deepcopy(profile_or_id)
    templates = profile.get("shots") or []
    if not templates or not all(isinstance(s, dict) for s in templates):
        raise StyleProfileError("empty_profile", "Style profile requires editable shots")
    raw_duration = (options or {}).get("target_duration_s")
    duration = float(raw_duration if raw_duration is not None else (profile.get("measured_facts") or {}).get("duration_s") or 15)
    if not math.isfinite(duration) or duration <= 0:
        raise StyleProfileError("bad_duration", "Target duration must be finite and positive")
    with prod.lock_for(slug):
        state = prod._editable(data_dir, slug)
        spec = deepcopy(state["spec"])
        existing = spec.get("shots") or []
        count = max(len(existing), len(templates))
        shots, weights = [], []
        used = {s["key"] for s in existing}
        for i in range(count):
            template = templates[i % len(templates)]
            if i < len(existing):
                shot = deepcopy(existing[i])
            else:
                key = str(max([int(k) for k in used if k.isdigit()] or [0]) + 1)
                used.add(key)
                shot = {"key": key, "prompt": f"New shot {key}", "seed": 1000 + i * 7,
                        "clip_seed": 2000 + i * 7, "clips": [0]}
            weight = float(template.get("relative_duration", 1))
            if not math.isfinite(weight) or weight <= 0:
                raise StyleProfileError("bad_weight", "Shot relative durations must be finite and positive")
            weights.append(weight)
            if not shot.get("locked"):
                camera = template.get("camera") or {}
                cam = {out: camera[src] for out, src in (("shot", "shot_type"), ("angle", "angle"), ("move", "movement"))
                       if camera.get(src) and camera[src] != "unknown"}
                transition = template.get("transition_in") or {}
                kind = transition.get("type", "cut")
                if kind == "none":
                    kind = "cut"
                from .timeline import TRANSITIONS
                if kind not in TRANSITIONS:
                    raise StyleProfileError("bad_transition", "Transition must be cut, crossfade, dip_black or flash_white")
                transition_duration = float(transition.get("duration_s", 0.35)) if kind != "cut" else 0
                if not math.isfinite(transition_duration) or (kind != "cut" and not 0.05 <= transition_duration <= 2):
                    raise StyleProfileError("bad_transition_duration", "Transition duration must be finite and between 0.05 and 2 seconds")
                shot["camera"] = {**shot.get("camera", {}), **cam}
                shot["style_profile_grammar"] = {"camera": cam, "palette": template.get("palette") or profile.get("palette") or [],
                    "transition_in": {"type": kind, "duration_s": transition_duration}}
            shots.append(shot)
        # Approved intervals anchor the retiming; unapproved intervals fill the gaps.
        begin, cursor = 0, 0.0
        for i in range(count + 1):
            if i < count and not shots[i].get("locked"):
                continue
            span = prod.shot_span(shots[i]) if i < count else None
            if i < count and span is None:
                raise StyleProfileError("approved_timing_required", "Approved shots need a timed interval before applying a rhythm")
            end = span[0] if span else duration
            if end < cursor or (span and span[1] > duration):
                raise StyleProfileError("approved_timing_conflict", "Target duration conflicts with approved shot intervals")
            total = sum(weights[begin:i])
            start = cursor
            for j in range(begin, i):
                finish = end if j == i - 1 else start + (end - cursor) * weights[j] / total
                prod._set_span(shots[j], prod._parse_span({"start_s": round(start, 3), "end_s": round(finish, 3)}, shots[j]["key"]))
                start = finish
            begin, cursor = i + 1, span[1] if span else duration
        timeline_changed = (spec.get("timeline", {}).get("options", {}).get("duration_s") != duration)
        spec["shots"] = shots
        spec.setdefault("timeline", {}).setdefault("options", {})["duration_s"] = duration
        if spec.get("song") and not (state["done"].get("song") or {}).get("song_asset_id") and not spec["song"].get("asset_id"):
            spec["song"]["duration"] = duration
        spec = prod.normalise_spec(spec)
        changed = [s["key"] for i, s in enumerate(spec["shots"]) if not s.get("locked") and (i >= len(existing) or s != existing[i])]
        state["spec"] = spec
        frames = state["done"].setdefault("frames", {"items": {}, "complete": False})
        clips = state["done"].setdefault("clips", {"items": {}, "complete": False})
        for key in changed:
            old = frames.setdefault("items", {}).pop(key, None)
            if old:
                prod.remember_take(state, "frames", key, old)
            for ck in [k for k in clips.setdefault("items", {}) if prod.split_key(k)[0] == key]:
                prod.remember_take(state, "clips", ck, {"asset_id": clips["items"].pop(ck), "quality": (clips.get("quality") or {}).get(ck, "final")})
                (clips.get("quality") or {}).pop(ck, None)
                (clips.get("chained") or {}).pop(ck, None)
        state["style_profile"] = {"id": profile.get("id"), "name": profile.get("name"), "applied_at": now_iso(),
                                  "target_duration_s": duration, "shot_count": len(shots)}
        if changed or timeline_changed:
            prod._requeue(state, f"Applied style profile to {len(changed)} shots")
        prod.log(state, "style_profile", "applied", profile_id=profile.get("id"), keys=changed)
        prod.save_state(data_dir, state)
        return {"slug": slug, "style_profile_id": profile.get("id"), "name": profile.get("name"), "shot_count": len(shots),
                "target_duration_s": duration, "changed": changed, "status": state["status"]}
