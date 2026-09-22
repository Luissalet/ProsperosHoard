"""Timeline domain logic: building an auto-cut edit from a song analysis
and a pool of assets, validating edits, and compact views. Pure Python, no
ffmpeg here (see `video.py` for the renderer) so it is fast to unit test.

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

`start_s` of visual clips is derived (the running sum of durations) and
rewritten by `normalise_tracks`, so an edit can never leave gaps.

`ken_burns` is a zoom range plus a pan direction (a "start/end
rect" simplified to what one ffmpeg `zoompan` filter expresses; see the
README boundaries).
"""

from __future__ import annotations

import random
from typing import Any, Callable, Optional

MIN_CLIP_S = 0.5
MAX_CLIP_S = 60.0
PAN_DIRECTIONS = ("left", "right", "up", "down", "none")
TRANSITIONS = ("cut", "crossfade", "dip_black", "flash_white")
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}


class TimelineError(ValueError):
    pass


# ------------------------------------------------------------- auto-cut

# a beat "belongs" to a section that starts up to this much after it: LRC
# stamps are rounded to 1/100 s, beat times to 1/1000 s
_SECTION_EPS_S = 0.06


def _energy_at(t: float, sections: list[dict[str, Any]]) -> str:
    section = _section_at(t, sections)
    return section.get("energy", "mid") if section else "mid"


def _cut_points(beat_times: list[float], downbeats: list[float], sections: list[dict[str, Any]], duration_s: float,
                options: dict[str, Any], line_times: Optional[list[float]] = None) -> list[dict[str, Any]]:
    """Cut times on beats: every `beats_low` beats (default 4) in low-energy
    sections, `beats_mid` (4) in mid, `beats_high` (2) in high. Returns
    [{"start_s", "flash"}] starting at 0.0; no clip shorter than MIN_CLIP_S,
    the last clip ends at the song's end."""
    density = {"low": int(options.get("beats_low", 4)), "mid": int(options.get("beats_mid", 4)),
               "high": int(options.get("beats_high", 2))}
    for k, v in density.items():
        if not 1 <= v <= 32:
            raise TimelineError(f"beats_{k} must be between 1 and 32")
    flash_on = bool(options.get("flash_on_strong_downbeats", True))
    # "strong" downbeats: the first beat of every 4-bar phrase, and the
    # first downbeat of each high-energy section (a flash every bar is
    # tiring to watch)
    strong = {round(d, 3) for d in downbeats[::4]}
    for s in sections or []:
        if s.get("energy") == "high":
            first = next((d for d in downbeats if d >= s["start_s"] - 0.05), None)
            if first is not None:
                strong.add(round(first, 3))
    down = strong
    beats = [b for b in beat_times if 0.0 <= b < duration_s]
    if not beats:
        # no beat grid (silence, speech): even cuts of `fallback_clip_s`
        step = float(options.get("fallback_clip_s", 2.0))
        points, t = [], 0.0
        while t < duration_s - MIN_CLIP_S:
            points.append({"start_s": round(t, 3), "flash": False})
            t += step
        return points or [{"start_s": 0.0, "flash": False}]

    # a new section always starts on a new shot (the first beat at or after
    # its start), so the edit breathes with the song's structure
    section_starts = set()
    for s in (sections or [])[1:]:
        first = next((b for b in beats if b >= s["start_s"] - 0.05), None)
        if first is not None:
            section_starts.add(round(first, 3))
    # `cut_on_lyrics`: a new shot on the beat nearest each sung line, so a
    # shot chosen for a line appears with it
    for t in line_times or []:
        if beats:
            section_starts.add(round(min(beats, key=lambda b: abs(b - t)), 3))
    points = [{"start_s": 0.0, "flash": False}]
    since = 0
    for i, b in enumerate(beats):
        if b <= 1e-6:
            continue
        since += 1
        step = density[_energy_at(b, sections)]
        forced = round(b, 3) in section_starts
        if (since >= step or forced) and b - points[-1]["start_s"] >= MIN_CLIP_S and duration_s - b >= MIN_CLIP_S:
            energy = _energy_at(b, sections)
            points.append({"start_s": round(b, 3), "flash": flash_on and energy == "high" and round(b, 3) in down})
            since = 0
    return points


def _assign_assets(pool: list[dict[str, Any]], count: int, seed: int = 0) -> list[dict[str, Any]]:
    """Walk a shuffled pool round-robin (reshuffled per pass) with no
    immediate repeats when the pool has more than one asset."""
    if not pool:
        raise TimelineError("cannot build an auto-cut timeline with an empty asset pool")
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    bag: list[dict[str, Any]] = []
    while len(out) < count:
        if not bag:
            bag = pool[:]
            rng.shuffle(bag)
            if out and len(bag) > 1 and bag[0]["id"] == out[-1]["id"]:
                bag.append(bag.pop(0))
        out.append(bag.pop(0))
    return out


def _section_at(t: float, sections: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    t = t + _SECTION_EPS_S
    for s in sections or []:
        if s["start_s"] <= t < s["end_s"]:
            return s
    return (sections or [None])[-1]


def _section_key_candidates(section: Optional[dict[str, Any]]) -> list[str]:
    if not section:
        return []
    label = str(section.get("label", "")).strip().lower()
    out = [label]
    base = label.rstrip("0123456789 ").strip()
    if base and base != label:
        out.append(base)
    if section.get("kind"):
        out.append(str(section["kind"]).lower())
    return out


def _assign_by_section(cuts: list[dict[str, Any]], sections: list[dict[str, Any]], pools: dict[str, list[dict[str, Any]]],
                       fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Storyboard assignment: each cut takes the next asset, in the order
    given, from the pool of the section it falls in (matched by exact label
    such as "Verse 2", then without its number, then by kind such as
    "chorus"); a pool keeps its place across repeats (the second chorus
    continues where the first stopped). Sections without a pool draw from
    the shuffled general pool. No asset repeats back to back."""
    pools = {k.strip().lower(): v for k, v in pools.items() if v}
    cursors: dict[str, int] = {}
    general = _assign_assets(fallback, len(cuts))
    out: list[dict[str, Any]] = []
    for i, cut in enumerate(cuts):
        section = _section_at(cut["start_s"], sections)
        key = next((k for k in _section_key_candidates(section) if k in pools), None)
        if key is None:
            pick = general[i]
        else:
            pool = pools[key]
            pos = cursors.get(key, 0)
            pick = pool[pos % len(pool)]
            if out and len(pool) > 1 and pick["id"] == out[-1]["id"]:
                pos += 1
                pick = pool[pos % len(pool)]
            cursors[key] = pos + 1
        if out and pick["id"] == out[-1]["id"] and len(fallback) > 1:
            pick = next(a for a in fallback if a["id"] != out[-1]["id"])
        out.append(pick)
    return out


def build_auto_cut(
    song_duration_s: float,
    beat_times: list[float],
    sections: list[dict[str, Any]],
    asset_pool: list[dict[str, Any]],
    options: Optional[dict[str, Any]] = None,
    lyrics_lines: Optional[list[dict[str, Any]]] = None,
    seed: int = 0,
    downbeats: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Returns `{"tracks": [...]}` ready to store on a Timeline row."""
    options = dict(options or {})
    seed = int(options.get("seed", seed))
    ken_burns_variety = bool(options.get("ken_burns_variety", True))
    if downbeats is None:
        downbeats = beat_times[0::4]

    line_times = [float(ln["time_s"]) for ln in (lyrics_lines or [])] if options.get("cut_on_lyrics") else None
    cuts = _cut_points(beat_times, downbeats, sections or [], song_duration_s, options, line_times)
    starts = [c["start_s"] for c in cuts] + [song_duration_s]
    section_pools = options.get("section_pools")
    if section_pools:
        assets = _assign_by_section(cuts, sections or [], section_pools, asset_pool)
    else:
        assets = _assign_assets(asset_pool, len(cuts), seed=seed)

    rng = random.Random(seed)
    visual_clips = []
    last_pan = None
    for i, cut in enumerate(cuts):
        duration_s = round(starts[i + 1] - starts[i], 3)
        asset = assets[i]
        transition = {"type": "flash_white", "duration_s": 0.15} if cut["flash"] and i > 0 else {"type": "cut", "duration_s": 0.0}
        clip: dict[str, Any] = {
            "asset_id": asset["id"], "kind": asset.get("kind", "image"), "start_s": round(starts[i], 3),
            "duration_s": duration_s, "trim_start_s": 0.0, "transition_in": transition,
        }
        if clip["kind"] == "image":
            if ken_burns_variety:
                choices = [p for p in PAN_DIRECTIONS[:-1] if p != last_pan]
                pan = rng.choice(choices)
                zoom_in = rng.random() < 0.7
                amount = round(rng.uniform(1.06, 1.16), 3)
                clip["ken_burns"] = {"zoom_start": 1.0 if zoom_in else amount, "zoom_end": amount if zoom_in else 1.0, "pan": pan}
                last_pan = pan
            else:
                clip["ken_burns"] = {"zoom_start": 1.0, "zoom_end": 1.08, "pan": "none"}
        visual_clips.append(clip)

    tracks: list[dict[str, Any]] = [{"type": "visual", "clips": visual_clips}]
    if lyrics_lines:
        tracks.append({"type": "lyrics", "clips": lyrics_clips_from_lines(lyrics_lines, song_duration_s,
                                                                          bool(options.get("karaoke", False)))})
    return {"tracks": tracks}


def lyrics_clips_from_lines(lines: list[dict[str, Any]], song_duration_s: float, karaoke: bool) -> list[dict[str, Any]]:
    clips = []
    ordered = sorted((ln for ln in lines if ln.get("text", "").strip()), key=lambda ln: ln["time_s"])
    for idx, line in enumerate(ordered):
        start = float(line["time_s"])
        if start >= song_duration_s:
            break
        nxt = ordered[idx + 1]["time_s"] if idx + 1 < len(ordered) else song_duration_s
        end = min(song_duration_s, nxt, start + 8.0)
        if end - start < 0.2:
            continue
        clips.append({"text": line["text"].strip()[:300], "start_s": round(start, 3), "end_s": round(end, 3), "karaoke": karaoke})
    return clips


# ------------------------------------------------------------ validation

def normalise_tracks(tracks: Any, asset_lookup: Callable[[str], Optional[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Validate an edited `tracks` value and return a clean copy (derived
    `start_s`, defaults filled). `asset_lookup(id)` returns the asset or
    None. Raises TimelineError with the index of the offending clip."""
    if not isinstance(tracks, list) or not tracks:
        raise TimelineError("tracks must be a non-empty list")
    out: list[dict[str, Any]] = []
    seen_types = set()
    for track in tracks:
        if not isinstance(track, dict) or track.get("type") not in ("visual", "lyrics"):
            raise TimelineError("each track needs type 'visual' or 'lyrics'")
        if track["type"] in seen_types:
            raise TimelineError(f"only one '{track['type']}' track is allowed")
        seen_types.add(track["type"])
        clips = track.get("clips")
        if not isinstance(clips, list) or len(clips) > 2000:
            raise TimelineError(f"{track['type']} track needs a 'clips' list (at most 2000)")
        if track["type"] == "visual":
            if not clips:
                raise TimelineError("the visual track needs at least one clip")
            clean, t = [], 0.0
            for i, clip in enumerate(clips):
                if not isinstance(clip, dict):
                    raise TimelineError(f"visual clip {i} must be an object")
                asset = asset_lookup(str(clip.get("asset_id", "")))
                if asset is None:
                    raise TimelineError(f"visual clip {i}: asset '{clip.get('asset_id')}' does not exist")
                if asset["kind"] not in ("image", "video"):
                    raise TimelineError(f"visual clip {i}: asset {asset['id']} is {asset['kind']}, not an image or video")
                try:
                    duration = float(clip.get("duration_s"))
                    trim = float(clip.get("trim_start_s") or 0.0)
                except (TypeError, ValueError):
                    raise TimelineError(f"visual clip {i}: duration_s must be a number") from None
                if not MIN_CLIP_S - 1e-6 <= duration <= MAX_CLIP_S:
                    raise TimelineError(f"visual clip {i}: duration_s must be between {MIN_CLIP_S} and {MAX_CLIP_S} seconds")
                if trim < 0 or (asset.get("duration_s") and trim >= float(asset["duration_s"])):
                    raise TimelineError(f"visual clip {i}: trim_start_s is outside the video")
                transition = clip.get("transition_in") or {"type": "cut", "duration_s": 0.0}
                if not isinstance(transition, dict) or transition.get("type", "cut") not in TRANSITIONS:
                    raise TimelineError(f"visual clip {i}: transition type must be one of {', '.join(TRANSITIONS)}")
                t_dur = float(transition.get("duration_s") or 0.0)
                if transition.get("type", "cut") != "cut" and not 0.05 <= t_dur <= min(2.0, duration / 2 + 1e-6):
                    raise TimelineError(f"visual clip {i}: transition duration must be 0.05-2 s and at most half the clip")
                c: dict[str, Any] = {
                    "asset_id": asset["id"], "kind": asset["kind"], "start_s": round(t, 3), "duration_s": round(duration, 3),
                    "trim_start_s": round(trim, 3),
                    "transition_in": {"type": transition.get("type", "cut"), "duration_s": round(t_dur, 3)},
                }
                if asset["kind"] == "image":
                    kb = clip.get("ken_burns") or {"zoom_start": 1.0, "zoom_end": 1.0, "pan": "none"}
                    try:
                        zs, ze = float(kb.get("zoom_start", 1.0)), float(kb.get("zoom_end", 1.0))
                    except (TypeError, ValueError, AttributeError):
                        raise TimelineError(f"visual clip {i}: ken_burns zoom values must be numbers") from None
                    if not (1.0 <= zs <= 2.0 and 1.0 <= ze <= 2.0):
                        raise TimelineError(f"visual clip {i}: ken_burns zoom must be between 1.0 and 2.0")
                    if kb.get("pan", "none") not in PAN_DIRECTIONS:
                        raise TimelineError(f"visual clip {i}: pan must be one of {', '.join(PAN_DIRECTIONS)}")
                    c["ken_burns"] = {"zoom_start": round(zs, 3), "zoom_end": round(ze, 3), "pan": kb.get("pan", "none")}
                clean.append(c)
                t += duration
            out.append({"type": "visual", "clips": clean})
        else:
            clean = []
            for i, clip in enumerate(clips):
                if not isinstance(clip, dict) or not isinstance(clip.get("text"), str):
                    raise TimelineError(f"lyrics clip {i} needs a 'text' string")
                try:
                    start, end = float(clip["start_s"]), float(clip["end_s"])
                except (KeyError, TypeError, ValueError):
                    raise TimelineError(f"lyrics clip {i} needs numeric start_s and end_s") from None
                if start < 0 or end <= start:
                    raise TimelineError(f"lyrics clip {i}: end_s must be after start_s")
                clean.append({"text": clip["text"][:300], "start_s": round(start, 3), "end_s": round(end, 3),
                              "karaoke": bool(clip.get("karaoke", False))})
            out.append({"type": "lyrics", "clips": sorted(clean, key=lambda c: c["start_s"])})
    if "visual" not in seen_types:
        raise TimelineError("a timeline needs a visual track")
    return out


def apply_clip_updates(tracks: list[dict[str, Any]], updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`updates`: [{"index": 3, "duration_s": 2.0, ...}] merged into visual
    clip `index`; {"index": 3, "delete": true} removes it;
    {"index": 3, "move_to": 0} reorders."""
    if not isinstance(updates, list) or len(updates) > 500:
        raise TimelineError("clip_updates must be a list of at most 500 {index, ...} objects")
    tracks = [dict(t, clips=[dict(c) for c in t["clips"]]) for t in tracks]
    visual = next(t for t in tracks if t["type"] == "visual")
    clips = visual["clips"]
    allowed = {"asset_id", "duration_s", "trim_start_s", "ken_burns", "transition_in", "kind"}
    for upd in updates:
        if not isinstance(upd, dict) or not isinstance(upd.get("index"), int):
            raise TimelineError("each clip update needs an integer 'index'")
        i = upd["index"]
        if not 0 <= i < len(clips):
            raise TimelineError(f"clip index {i} is out of range (0-{len(clips) - 1})")
        if upd.get("delete"):
            clips.pop(i)
            continue
        if "move_to" in upd:
            j = int(upd["move_to"])
            if not 0 <= j < len(clips):
                raise TimelineError(f"move_to {j} is out of range")
            clips.insert(j, clips.pop(i))
            continue
        unknown = set(upd) - allowed - {"index"}
        if unknown:
            raise TimelineError(f"unknown clip field(s): {', '.join(sorted(unknown))}")
        for k, v in upd.items():
            if k != "index":
                clips[i][k] = v
    return tracks


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
        if abs(last_end - song_duration_s) > 0.01:
            problems.append(f"timeline ends at {last_end}, song is {song_duration_s}")
    for i in range(1, len(clips)):
        if clips[i]["asset_id"] == clips[i - 1]["asset_id"]:
            problems.append(f"clip {i} immediately repeats asset {clips[i]['asset_id']}")
    return problems


def compact_view(timeline: dict[str, Any], clip_offset: int = 0, clip_limit: int = 24) -> dict[str, Any]:
    """A model-sized view: summary + one page of visual clips (index kept)."""
    visual = next((t for t in timeline["tracks"] if t["type"] == "visual"), {"clips": []})
    lyrics = next((t for t in timeline["tracks"] if t["type"] == "lyrics"), {"clips": []})
    clips = visual["clips"]
    clip_limit = max(1, min(int(clip_limit), 100))
    clip_offset = max(0, int(clip_offset))
    page = []
    for i, c in enumerate(clips[clip_offset:clip_offset + clip_limit], start=clip_offset):
        item = {"index": i, "asset_id": c["asset_id"], "kind": c["kind"], "start_s": c["start_s"], "duration_s": c["duration_s"],
                "transition": c.get("transition_in", {}).get("type", "cut")}
        if c.get("ken_burns"):
            kb = c["ken_burns"]
            item["ken_burns"] = f"{kb['zoom_start']}->{kb['zoom_end']} {kb['pan']}"
        page.append(item)
    total = sum(c["duration_s"] for c in clips)
    has_more = clip_offset + clip_limit < len(clips)
    return {
        "id": timeline["id"], "project_id": timeline["project_id"], "name": timeline["name"], "aspect": timeline["aspect"],
        "fps": timeline["fps"], "width": timeline["width"], "height": timeline["height"],
        "audio_asset_id": timeline.get("audio_asset_id"), "duration_s": round(total, 3),
        "clips_total": len(clips), "lyrics_lines": len(lyrics["clips"]), "finishing": timeline.get("finishing") or {},
        "clips": page, "has_more": has_more, "next_clip_offset": clip_offset + clip_limit if has_more else None,
        "updated_at": timeline.get("updated_at"),
    }
