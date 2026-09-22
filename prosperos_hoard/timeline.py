"""Timeline domain logic: building an auto-cut edit from a song analysis
and a pool of assets. Pure Python / no ffmpeg here (see `video.py` for the
renderer) so the cut logic is fast to unit test.

Clip schema (stored inside `timelines.tracks_json`, one "visual" track and
optionally one "lyrics" track):

    {"type": "visual", "clips": [
        {"asset_id": "a_...", "kind": "image"|"video",
         "start_s": 0.0, "duration_s": 1.6, "trim_start_s": 0.0,
         "ken_burns": {"zoom_start": 1.0, "zoom_end": 1.12, "pan": "left"},
         "transition_in": {"type": "cut"|"crossfade"|"dip_black"|"flash_white", "duration_s": 0.0}}
    ]}
    {"type": "lyrics", "clips": [
        {"text": "...", "start_s": 1.2, "end_s": 3.4, "karaoke": true}
    ]}

`ken_burns` is Prospero's own simplified, ffmpeg-`zoompan`-shaped
parameterisation of a "start/end rect" pan (see README
boundaries): a zoom range plus a pan direction, which maps directly onto a
single `zoompan` filter invocation instead of arbitrary per-frame crop
rectangles.
"""

from __future__ import annotations

import random
from typing import Any, Optional

MIN_CLIP_S = 0.5
PAN_DIRECTIONS = ("left", "right", "up", "down", "none")


def _cut_points(beat_times: list[float], sections: list[dict[str, Any]], flash_on_downbeats: bool) -> list[dict[str, Any]]:
    """Every element: {"start_s", "flash": bool}. Cuts land on beat times,
    density depends on the section's energy, and no clip is shorter than
    MIN_CLIP_S."""
    if not beat_times:
        return [{"start_s": 0.0, "flash": False}]

    points: list[dict[str, Any]] = []
    for section in sections:
        step = 2 if section.get("energy") == "high" else 4
        in_section = [t for t in beat_times if section["start_s"] <= t < section["end_s"]]
        for i in range(0, len(in_section), step):
            t = in_section[i]
            is_downbeat = beat_times.index(t) % 4 == 0 if t in beat_times else False
            points.append({"start_s": t, "flash": flash_on_downbeats and is_downbeat and section.get("energy") == "high"})
    if not points or points[0]["start_s"] > 0.0001:
        points.insert(0, {"start_s": 0.0, "flash": False})
    points.sort(key=lambda p: p["start_s"])

    # enforce min clip length by dropping cuts that are too close together
    filtered = [points[0]]
    for p in points[1:]:
        if p["start_s"] - filtered[-1]["start_s"] >= MIN_CLIP_S:
            filtered.append(p)
    return filtered


def _assign_assets(pool: list[dict[str, Any]], count: int, seed: int = 0) -> list[dict[str, Any]]:
    if not pool:
        raise ValueError("cannot build an auto-cut timeline with an empty asset pool")
    rng = random.Random(seed)
    shuffled = pool[:]
    rng.shuffle(shuffled)
    out: list[dict[str, Any]] = []
    last: Optional[str] = None
    i = 0
    while len(out) < count:
        candidate = shuffled[i % len(shuffled)]
        if candidate["id"] == last and len(pool) > 1:
            i += 1
            candidate = shuffled[i % len(shuffled)]
        out.append(candidate)
        last = candidate["id"]
        i += 1
    return out


def build_auto_cut(
    song_duration_s: float,
    beat_times: list[float],
    sections: list[dict[str, Any]],
    asset_pool: list[dict[str, Any]],
    options: Optional[dict[str, Any]] = None,
    lyrics_lines: Optional[list[dict[str, Any]]] = None,
    seed: int = 0,
) -> dict[str, Any]:
    """Returns `{"tracks": [...]}` ready to store on a Timeline row."""
    options = options or {}
    flash = bool(options.get("flash_on_strong_downbeats", True))
    ken_burns_variety = bool(options.get("ken_burns_variety", True))

    cuts = _cut_points(beat_times, sections or [], flash)
    starts = [c["start_s"] for c in cuts]
    flashes = [c["flash"] for c in cuts]
    starts.append(song_duration_s)  # sentinel so the last clip covers the tail

    assets = _assign_assets(asset_pool, len(starts) - 1, seed=seed)

    rng = random.Random(seed)
    visual_clips = []
    for i in range(len(starts) - 1):
        start_s = round(starts[i], 3)
        duration_s = round(max(MIN_CLIP_S, starts[i + 1] - starts[i]), 3)
        asset = assets[i]
        transition_type = "flash_white" if flashes[i] and i > 0 else ("cut" if i == 0 else "cut")
        pan = rng.choice(PAN_DIRECTIONS[:-1]) if ken_burns_variety else "none"
        clip: dict[str, Any] = {
            "asset_id": asset["id"],
            "kind": asset.get("kind", "image"),
            "start_s": start_s,
            "duration_s": duration_s,
            "trim_start_s": 0.0,
            "transition_in": {"type": transition_type, "duration_s": 0.0 if transition_type == "cut" else 0.15},
        }
        if asset.get("kind", "image") == "image":
            clip["ken_burns"] = {
                "zoom_start": 1.0,
                "zoom_end": round(rng.uniform(1.08, 1.18), 3) if ken_burns_variety else 1.1,
                "pan": pan,
            }
        visual_clips.append(clip)

    tracks: list[dict[str, Any]] = [{"type": "visual", "clips": visual_clips}]

    if lyrics_lines:
        lyric_clips = []
        for idx, line in enumerate(lyrics_lines):
            end = lyrics_lines[idx + 1]["time_s"] if idx + 1 < len(lyrics_lines) else song_duration_s
            lyric_clips.append({
                "text": line["text"], "start_s": line["time_s"], "end_s": round(end, 3),
                "karaoke": bool(options.get("karaoke", False)),
            })
        tracks.append({"type": "lyrics", "clips": lyric_clips})

    return {"tracks": tracks}


def validate_auto_cut_invariants(tracks: list[dict[str, Any]], beat_times: list[float], song_duration_s: float) -> list[str]:
    """Returns a list of violated-invariant messages (empty = all good)."""
    problems = []
    visual = next((t for t in tracks if t["type"] == "visual"), None)
    if visual is None:
        return ["no visual track"]
    clips = visual["clips"]
    beat_set = {round(b, 3) for b in beat_times}
    for i, clip in enumerate(clips):
        if clip["duration_s"] < MIN_CLIP_S - 1e-6:
            problems.append(f"clip {i} shorter than {MIN_CLIP_S}s")
        if i > 0 and round(clip["start_s"], 3) not in beat_set and clip["start_s"] != 0.0:
            problems.append(f"clip {i} does not start on a beat")
    if clips:
        last_end = clips[-1]["start_s"] + clips[-1]["duration_s"]
        if abs(last_end - song_duration_s) > 0.5:
            problems.append(f"timeline ends at {last_end}, song is {song_duration_s}")
    for i in range(1, len(clips)):
        if clips[i]["asset_id"] == clips[i - 1]["asset_id"]:
            problems.append(f"clip {i} immediately repeats asset {clips[i]['asset_id']}")
    return problems
