"""Direction of an HTML animation: treatment, cues, and the checks of the picture against the sound and the reader.

A film made from a style card goes through one flow, and each step leaves a file next to the composition so the next
revision (and the next agent) starts from it:

* ``treatment.json``: before any HTML is written, three structures (and why one was chosen), the shots one by one with
  why each is there, and the cue map (what the picture does at each moment and what is heard).
* ``cues.json``: the moments both the picture and the sound hang on. The sound events of the composition are derived
  from it, so the two cannot drift apart.
* ``FACTS.md``: for informative videos, the claims the video makes and where each comes from.

Checks (all of them render the real page):

* :func:`cuecheck`: each cue must have a visual hit in the frame where it sounds, and each visual hit a cue.
* :func:`readcheck`: every text must stay fully visible long enough to be read.
* :func:`facts_check`: numbers shown in the video must be in FACTS.md.
"""
from __future__ import annotations

from io import BytesIO
import base64
import hashlib
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import motion_sound
from .errors import LumiereError, NotFound

TREATMENT_FILE = "treatment.json"
CUES_FILE = "cues.json"
FACTS_FILE = "FACTS.md"
MIN_CHANGE = 0.3          # mean grey levels (0-255) between two frames that count as the picture reacting
HIT_SHARE = 0.35         # a visual hit with no cue counts when it is this share as strong as the weakest cued hit
READ_PAD_S = 1.5          # time to find the text, on top of the time to read it
READ_LETTERS_PER_S = 15.0
READ_CJK_PER_S = 4.5
VISIBLE_OPACITY = 0.85
VISIBLE_FRACTION = 0.98
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")


# ------------------------------------------------------------------------------------------------ models
class Structure(BaseModel):
    name: str = Field(..., min_length=2, max_length=80, description="A short name for this way of telling it.")
    idea: str = Field(..., min_length=8, max_length=800, description="How the film is built if this structure is chosen.")


class Shot(BaseModel):
    t: float = Field(..., ge=0, allow_inf_nan=False, description="Where the shot starts, in seconds.")
    what: str = Field(..., min_length=3, max_length=500, description="What the viewer sees.")
    why: str = Field(..., min_length=8, max_length=500, description="Why the shot is there: what it does for the story or the style.")
    text: Optional[str] = Field(None, max_length=200, description="Caption shown on screen in this shot (the scaffold writes it; read by readcheck).")


class CueMapItem(BaseModel):
    t: float = Field(..., ge=0, allow_inf_nan=False)
    visual: str = Field(..., min_length=3, max_length=300, description="What the picture does at this moment.")
    sound: Optional[str] = Field(None, description="The sound role heard (tap, whoosh, hit, clic...), or none for a silent hit.")


class Treatment(BaseModel):
    card: Optional[str] = Field(None, description="The style card this film follows: 'source/slug' or just the slug.")
    structures: list[Structure] = Field(..., min_length=3, max_length=3, description="Exactly three different ways to tell it.")
    chosen: int = Field(..., ge=0, le=2, description="Index (0-2) of the structure that was chosen.")
    reason: str = Field(..., min_length=8, max_length=800, description="Why that structure over the other two.")
    shots: list[Shot] = Field(..., min_length=1, max_length=60, description="The shots one by one, each with why it is there.")
    cue_map: list[CueMapItem] = Field(..., min_length=1, max_length=200, description="What happens and what is heard, moment by moment.")
    draft: bool = Field(False, description="True when the treatment was drafted from the card and not written for this film.")


class Cue(BaseModel):
    id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{1,24}$", description="Stable name; omitted = c1, c2... in time order.")
    t: float = Field(..., ge=0, allow_inf_nan=False, description="Seconds: the frame where the picture hits and the sound lands.")
    visual: str = Field(..., min_length=2, max_length=300, description="What the picture does there.")
    sound: Optional[str] = Field(None, description="Sound role (tap, golpe, clic, whoosh, hit...). None: a silent visual hit.")
    gain_db: Optional[float] = Field(None, ge=-24, le=12, allow_inf_nan=False)


# ------------------------------------------------------------------------------------------------ files
def _read(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write(store, path: Path, value: dict) -> None:
    store._write(path, value)


def get_treatment(store, composition: str) -> Optional[dict]:
    return _read(store.folder(composition) / TREATMENT_FILE)


def get_cues(store, composition: str) -> dict:
    found = _read(store.folder(composition) / CUES_FILE)
    return found or {"rev": 0, "cues": []}


def cues_own_sound(store, composition: str) -> bool:
    """When cues exist they own the sound events: the two must not be edited apart."""
    return bool(get_cues(store, composition)["cues"])


# ------------------------------------------------------------------------------------------------ treatment
def check_treatment(treatment: Any, duration: float, kit: str = motion_sound.DEFAULT_KIT) -> dict:
    """The problems that make a treatment unfit to start from. A valid one has three structures, shots with why, a cue map."""
    problems: list[str] = []
    try:
        value = treatment if isinstance(treatment, Treatment) else Treatment.model_validate(treatment)
    except Exception as exc:
        errors = getattr(exc, "errors", None)
        if errors:
            for e in errors():
                problems.append(".".join(str(p) for p in e["loc"]) + ": " + e["msg"])
        else:
            problems.append(str(exc))
        return {"ok": False, "problems": problems}
    names = [s.name.strip().lower() for s in value.structures]
    ideas = [s.idea.strip().lower() for s in value.structures]
    if len(set(names)) < 3 or len(set(ideas)) < 3:
        problems.append("structures: the three structures must be different from each other.")
    times = [s.t for s in value.shots]
    if times != sorted(times) or len(set(times)) != len(times):
        problems.append("shots: start times must be strictly increasing.")
    if any(t >= duration for t in times):
        problems.append(f"shots: every shot must start before the end ({duration:g} s).")
    whys = [s.why.strip().lower() for s in value.shots]
    if len(whys) > 1 and len(set(whys)) == 1:
        problems.append("shots: every shot has the same 'why'; say what each one does.")
    for item in value.cue_map:
        if item.t > duration:
            problems.append(f"cue_map: {item.t:g} s is past the end ({duration:g} s).")
        if item.sound:
            try:
                motion_sound.canonical_role(item.sound, kit)
            except LumiereError as exc:
                problems.append(f"cue_map: {item.sound!r}: {exc}")
    return {"ok": not problems, "problems": problems}


def set_treatment(store, composition: str, treatment: Treatment) -> dict:
    manifest = store.get(composition)
    verdict = check_treatment(treatment, manifest["duration"])
    if not verdict["ok"]:
        raise LumiereError("The treatment is not usable: " + " ".join(verdict["problems"]), code="treatment")
    previous = get_treatment(store, composition)
    value = {"rev": (previous or {}).get("rev", 0) + 1, **treatment.model_dump(exclude_none=True)}
    _write(store, store.folder(composition) / TREATMENT_FILE, value)
    return {"composition": composition, "treatment": value, "check": verdict}


def first_sentence(text: str, limit: int = 140) -> str:
    plain = re.sub(r"[*`#|>]", "", str(text or ""))
    plain = re.sub(r"^\s*[-\d.)]+\s*", "", plain.strip())
    plain = re.sub(r"\s+", " ", plain)
    cut = re.split(r"(?<=[.!?])\s", plain, maxsplit=1)[0]
    return cut[:limit].strip()


def _options(text: str) -> list[tuple[str, str]]:
    out = []
    for line in str(text or "").splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*)$", line)
        if not m:
            continue
        body = re.sub(r"\*\*|`", "", m.group(1)).strip()
        head = re.split(r"[:\u2014]|\s-\s|\.\s", body, maxsplit=1)[0].strip(" .")[:60]
        if len(head) >= 2 and len(body) >= 8:
            out.append((head, body[:400]))
    return out


def draft_treatment(card: dict, duration: float, caption: Optional[str] = None, kit: str = motion_sound.DEFAULT_KIT) -> dict:
    """A treatment drafted from a card: its variation space gives the structures and its sections give the shots.

    It is a valid starting point, marked ``draft``: an agent is expected to replace it with one written for the film."""
    sections = card.get("sections") or {}
    variation = (sections.get("variation") or {}).get("text", "")
    options = _options(variation)
    generic = [("Straight line", "One idea, told start to end in the order it happens, each shot adding one fact."),
               ("Turn at the middle", "Set one expectation, then break it halfway so the second half reads differently."),
               ("Return to the start", "Open and close on the same frame, changed by what happened between.")]
    structures = []
    for name, idea in options[:3]:
        structures.append({"name": name, "idea": idea})
    for name, idea in generic:
        if len(structures) == 3:
            break
        if name.lower() not in [s["name"].lower() for s in structures]:
            structures.append({"name": name, "idea": idea})
    wanted = max(1, min(4, int(duration // 4)))
    order = ([k for k in ("essence", "materials", "color", "type", "motion") if k in sections] or list(sections))[:wanted]
    count = max(1, len(order))
    starts = [round(duration * i / count, 2) for i in range(count)]
    roles = ["whoosh", "hit", "tap", "clic"]
    shots, cue_map = [], []
    for i, key in enumerate(order):
        section = sections[key]
        first = first_sentence(section.get("text", ""))
        label = section.get("title") or key
        shots.append({"t": starts[i], "what": (f"{label}: {first}" if first else label)[:480],
                      "why": f"Shows the card's '{label}' so the film reads as this style from the start."[:480],
                      "text": ((caption or card.get("name") or label) if i == 0 else label)[:60]})
        cue_map.append({"t": max(starts[i], 0.15), "visual": f"The shot changes to {label.lower()}."[:290], "sound": roles[i % len(roles)]})
    return {"card": f"{card.get('source')}/{card.get('slug')}" if card.get("source") else card.get("slug"),
            "structures": structures, "chosen": 0,
            "reason": "Drafted from the card's variation space; the first structure is the one the card lists first.",
            "shots": shots, "cue_map": cue_map, "draft": True}


# ------------------------------------------------------------------------------------------------ cues
def _clean_cues(cues: list[Cue], duration: float, kit: str) -> list[dict]:
    out, used = [], set()
    for cue in sorted(cues, key=lambda c: c.t):
        if cue.t > duration:
            raise LumiereError(f"A cue at {cue.t:g} s is past the end ({duration:g} s).", code="cue_time")
        item = cue.model_dump(exclude_none=True)
        if cue.sound:
            item["sound"] = motion_sound.canonical_role(cue.sound, kit)
        out.append(item)
    for i, item in enumerate(out, 1):
        name = item.get("id") or f"c{i}"
        n = i
        while name in used:
            n += 1
            name = f"c{n}"
        used.add(name)
        item["id"] = name
    return out


def sound_events_from_cues(cues: list[dict]) -> list[dict]:
    return [{k: v for k, v in {"t": c["t"], "role": c["sound"], "gain_db": c.get("gain_db")}.items() if v is not None}
            for c in cues if c.get("sound")]


def set_cues(store, composition: str, cues: list[Cue], expected_revision: int) -> dict:
    """Write cues.json and derive the composition's sound events from it (a new revision, the same picture)."""
    latest = store.get(composition)
    if expected_revision != latest["revision"]:
        raise LumiereError(f"Revision {expected_revision} is not the current one ({latest['revision']}): read it again.", code="revision_conflict")
    current = dict(latest.get("sound") or {})
    kit = current.get("kit", motion_sound.DEFAULT_KIT)
    cleaned = _clean_cues(cues, latest["duration"], kit)
    previous = get_cues(store, composition)
    folder = store.folder(composition)
    if cleaned:
        _write(store, folder / CUES_FILE, {"rev": previous["rev"] + 1, "cues": cleaned})
    elif (folder / CUES_FILE).is_file():
        (folder / CUES_FILE).unlink()
    sound = dict(current)
    sound["events"] = sound_events_from_cues(cleaned)
    sound.setdefault("kit", kit)
    saved = store.save(composition=composition, expected_revision=expected_revision, html=store.source(latest),
                       sound=sound if (sound["events"] or sound.get("music")) else {},
                       **{k: latest[k] for k in ("name", "width", "height", "fps", "duration", "project_id")})
    return {"composition": composition, "revision": saved["revision"], "cues": cleaned,
            "events": len((saved.get("sound") or {}).get("events", [])), "duration": saved["duration"]}


_HTML_CUES = re.compile(r"<script\b[^>]*\bid\s*=\s*[\"']lumiere-cues[\"'][^>]*>(.*?)</script>", re.S | re.I)


def html_cues(html: str) -> Optional[list]:
    found = _HTML_CUES.search(html)
    if not found:
        return None
    try:
        value = json.loads(found.group(1))
    except ValueError:
        return None
    return value if isinstance(value, list) else None


# ------------------------------------------------------------------------------------------------ frames
def _gray(png: bytes):
    import numpy as np
    from PIL import Image
    with Image.open(BytesIO(png)) as image:
        width = min(192, image.width)
        height = max(1, round(width * image.height / image.width))
        return np.asarray(image.convert("L").resize((width, height), Image.Resampling.BOX), dtype=np.float32)


def _frame_diffs(store, manifest: dict, indices: set[int], ctx=None) -> dict[int, float]:
    """Mean absolute grey change between frame k-1 and frame k, for every k whose two frames were rendered."""
    import numpy as np
    from .html_motion import HtmlFrameWorker
    fps = manifest["fps"]
    grays: dict[int, Any] = {}
    with HtmlFrameWorker(store.pinned(manifest), ctx) as worker:
        for k in sorted(indices):
            grays[k] = _gray(worker.frame(min(k / fps, manifest["duration"])))
    return {k: float(np.mean(np.abs(grays[k] - grays[k - 1]))) for k in grays if k - 1 in grays}


def _detect(diff: dict[int, float], fps: float, t: float, window: float, min_change: float, last: int) -> dict:
    """The frame where the picture hits near cue ``t``: the start of the sharpest change in [t - window, t + window]."""
    lo = max(1, int(round((t - window) * fps)))
    hi = min(last, int(round((t + window) * fps)))
    around = [(k, diff[k]) for k in range(lo, hi + 1) if k in diff]
    before = [diff[k] for k in range(max(1, int(round((t - window - 0.4) * fps))), lo) if k in diff]
    baseline = statistics.median(before) if before else 0.0
    if not around:
        return {"status": "no_frames", "baseline": round(baseline, 3)}
    peak = max(v for _, v in around)
    if peak < min_change:
        return {"status": "no_hit", "strength": round(peak, 3), "baseline": round(baseline, 3)}
    if peak < max(2 * baseline, baseline + min_change):
        return {"status": "unclear", "strength": round(peak, 3), "baseline": round(baseline, 3)}
    strong = max(min_change, 0.5 * peak)
    starts = [k for k, v in around if v >= strong and diff.get(k - 1, 0.0) < strong]
    k = min(starts, key=lambda s: abs(s / fps - t))
    return {"hit_frame": k, "hit_t": round(k / fps, 4), "strength": round(peak, 3), "baseline": round(baseline, 3)}


def _onsets(diff: dict[int, float], floor: float) -> list[int]:
    found = []
    for k in sorted(diff):
        v = diff[k]
        prior = [diff[j] for j in range(k - 8, k) if j in diff]
        base = sum(prior) / len(prior) if prior else 0.0
        if v < max(floor, 3 * base):
            continue
        if any(diff.get(j, 0.0) > v for j in range(k - 2, k + 3) if j != k) or any(diff.get(j, 0.0) == v for j in range(k - 2, k)):
            continue
        found.append(k)
    return found


PAIR_S = 2.0  # how far a visual hit may be from a cue that found none, and still be proposed as where that cue belongs


def cuecheck(store, composition: str, revision: Optional[int] = None, *, window_s: float = 0.3, scan: Optional[bool] = None,
             min_change: float = MIN_CHANGE, retime: bool = False, expected_revision: Optional[int] = None, ctx=None) -> dict:
    """Each cue must have a visible hit in the frame where it sounds (looked for within ``window_s`` either side); with ``scan``, each visible hit must have a cue."""
    manifest = store.get(composition, revision)
    cues = get_cues(store, composition)["cues"]
    if not cues:
        raise LumiereError("This composition has no cues: set them with motion_cues first.", code="no_cues")
    fps, duration = manifest["fps"], manifest["duration"]
    last = int(round(duration * fps))
    if scan is None:
        scan = last <= 1500
    indices: set[int] = set(range(0, last + 1)) if scan else set()
    if not scan:
        for c in cues:
            lo = max(0, int(round((c["t"] - window_s - 0.4) * fps)) - 1)
            indices.update(range(lo, min(last, int(round((c["t"] + window_s) * fps)) + 1) + 1))
    diff = _frame_diffs(store, manifest, indices, ctx)
    tolerance = 1.0 / fps + 0.02
    results, owned = [], set()
    for c in cues:
        found = _detect(diff, fps, c["t"], window_s, min_change, last)
        row = {"id": c["id"], "t": c["t"], "sound": c.get("sound"), "visual": c["visual"], **found}
        if "hit_t" in found:
            error = found["hit_t"] - c["t"]
            row["offset_ms"] = round(error * 1000, 1)
            if abs(error) <= tolerance:
                row["status"] = "ok"
            else:
                row["status"] = "sound_late" if error < 0 else "sound_early"
                row["suggest_t"] = found["hit_t"]
            owned.add(found["hit_frame"])
        results.append(row)
    unmapped = []
    if scan:
        strengths = [r["strength"] for r in results if "hit_t" in r]
        floor = max(2 * min_change, HIT_SHARE * min(strengths)) if strengths else 2 * min_change
        for k in _onsets(diff, floor):
            t = k / fps
            if any(abs(t - c["t"]) <= window_s + tolerance for c in cues):
                continue
            unmapped.append({"t": round(t, 4), "strength": round(diff[k], 3)})
        # A cue with no hit near it and a hit with no cue near it are most likely the same moment, off by more than the window.
        free = list(unmapped)
        for row in sorted((r for r in results if "hit_t" not in r), key=lambda r: r["t"]):
            near = [u for u in free if abs(u["t"] - row["t"]) <= PAIR_S]
            if near:
                best = min(near, key=lambda u: abs(u["t"] - row["t"]))
                best["cue"] = row["id"]
                row["suggest_t"] = best["t"]
                free.remove(best)
    problems = [r for r in results if r["status"] != "ok"]
    result = {"composition": composition, "revision": manifest["revision"], "fps": fps, "tolerance_ms": round(tolerance * 1000, 1),
              "window_s": window_s, "scanned_frames": len(indices) if scan else None, "checked_frames": len(diff), "cues": results,
              "unmapped_hits": unmapped[:50], "ok": not problems and not unmapped}
    result["summary"] = (f"{len(results) - len(problems)}/{len(results)} cues have their hit in the frame"
                         + (f"; {len(unmapped)} visual hit(s) have no cue" if unmapped else "") + ".")
    if problems:
        result["fix"] = ("Move the picture or the cue: suggest_t is the frame where the picture really changes (retime moves the cues that "
                         "are only off by a little; a cue with no hit near it is only suggested the nearest hit with no cue). no_hit/unclear: "
                         "make the hit a sharper, single change (a flash, a cut, a pop) at the cue.")
    declared = html_cues(store.source(manifest))
    if declared is not None:
        mine = [(round(float(c["t"]), 3)) for c in cues]
        theirs = [round(float(c.get("t", -1)), 3) for c in declared if isinstance(c, dict)]
        if mine != theirs:
            result["warning"] = "The cues embedded in the page (lumiere-cues) differ from cues.json; cues.json is the one that sounds."
    if retime:
        fixable = {r["id"]: r["suggest_t"] for r in results if r.get("suggest_t") is not None and r["status"] in ("sound_late", "sound_early")}
        if fixable:
            if expected_revision is None:
                raise LumiereError("retime needs expected_revision.", code="revision_conflict")
            moved = [Cue(**{**c, "t": fixable.get(c["id"], c["t"])}) for c in cues]
            saved = set_cues(store, composition, moved, expected_revision)
            result["retimed"] = {"cues": sorted(fixable), "revision": saved["revision"]}
    return result


# ------------------------------------------------------------------------------------------------ reading
def reading_need(text: str) -> float:
    cjk = len(_CJK.findall(text))
    other = len(re.sub(r"\s+", "", _CJK.sub("", text)))
    return round(cjk / READ_CJK_PER_S + other / READ_LETTERS_PER_S + READ_PAD_S, 3)


def _samples(manifest: dict, step: Optional[float]) -> list[float]:
    fps = manifest["fps"]
    step = max(step or 1.0 / fps, manifest["duration"] / 1200)
    count = int(manifest["duration"] / step) + 1
    times = [round(i * step, 6) for i in range(count)]
    if times[-1] < manifest["duration"] - 1e-9:
        times.append(manifest["duration"])
    return times


def read_texts(store, manifest: dict, step: Optional[float] = None, ctx=None) -> dict:
    from .html_motion import HtmlFrameWorker
    times = _samples(manifest, step)
    with HtmlFrameWorker(store.pinned(manifest), ctx) as worker:
        answer = worker.call({"op": "texts", "times": times})
    return {"times": times, **answer["texts"]}


def _inside(box: tuple, width: float, height: float) -> float:
    x0, y0, x1, y1 = box
    area = max(1e-9, (x1 - x0) * (y1 - y0))
    w = max(0.0, min(x1, width) - max(x0, 0.0))
    h = max(0.0, min(y1, height) - max(y0, 0.0))
    return (w * h) / area


def readcheck(store, composition: str, revision: Optional[int] = None, *, step: Optional[float] = None, min_s: float = 0.0, ctx=None) -> dict:
    """Every text must be fully visible for long enough: letters/15 s (CJK: characters/4.5 s) plus 1.5 s to find it."""
    manifest = store.get(composition, revision)
    sample = read_texts(store, manifest, step, ctx)
    times, frames, width, height = sample["times"], sample["frames"], sample["width"], sample["height"]
    open_runs: dict[tuple, dict] = {}
    all_runs: dict[tuple, list] = {}
    seen: dict[tuple, dict] = {}
    for i, texts in enumerate(frames):
        for item in texts:
            text = re.sub(r"\s+", " ", item["text"]).strip()
            if not text:
                continue
            key = (item["id"], text)
            box = (item["x0"], item["y0"], item["x1"], item["y1"])
            fraction = min(item.get("frac", 1.0), _inside(box, width, height))
            opacity = item.get("opacity", 1.0)
            best = seen.setdefault(key, {"opacity": 0.0, "fraction": 0.0, "first": times[i]})
            best["opacity"] = max(best["opacity"], opacity)
            best["fraction"] = max(best["fraction"], fraction)
            if opacity >= VISIBLE_OPACITY and fraction >= VISIBLE_FRACTION:
                run = open_runs.get(key)
                if run is None or run["end_i"] != i - 1:
                    run = {"start_i": i, "end_i": i}
                    open_runs[key] = run
                    all_runs.setdefault(key, []).append(run)
                run["end_i"] = i
    fails, rows = [], []
    covered = set(all_runs)
    for key, key_runs in all_runs.items():
        for run in key_runs:
            start = times[run["start_i"]]
            next_t = times[run["end_i"] + 1] if run["end_i"] + 1 < len(times) else manifest["duration"]
            dwell = next_t - start
            need = max(min_s, reading_need(key[1]))
            row = {"id": key[0], "text": key[1][:120], "from": round(start, 3), "to": round(next_t, 3), "dwell_s": round(dwell, 3),
                   "need_s": need, "ok": dwell + 1e-6 >= need}
            if run["end_i"] == len(times) - 1:
                row["until_end"] = True
            rows.append(row)
            if not row["ok"]:
                row["short_by_s"] = round(need - dwell, 3)
                fails.append(row)
    never = []
    for key, best in seen.items():
        if key in covered:
            continue
        if best["fraction"] < VISIBLE_FRACTION:
            reason = "it is never fully inside the frame or its mask"
        elif best["opacity"] < VISIBLE_OPACITY:
            reason = f"it never gets past {best['opacity']:.0%} opacity"
        else:
            reason = "it is never fully visible"
        never.append({"id": key[0], "text": key[1][:120], "from": round(best["first"], 3), "reason": reason})
    rows.sort(key=lambda r: (r["from"], r["id"]))
    result = {"composition": composition, "revision": manifest["revision"], "source": sample["source"], "samples": len(times),
              "rule": f"letters/{READ_LETTERS_PER_S:g} s (CJK characters/{READ_CJK_PER_S:g} s) + {READ_PAD_S:g} s", "texts": rows[:300],
              "too_short": fails[:100], "never_readable": never[:100], "ok": not fails and not never}
    if sample["source"] == "dom" and not rows and not never:
        result["note"] = ("No text was found in the page. If it draws text in a canvas, define window.TEXTS(t) returning "
                          "[{id, text, x0, y0, x1, y1}] (page pixels) for what is on screen at t.")
    result["summary"] = (f"{len(rows) - len(fails)}/{len(rows)} text appearances stay long enough" +
                         (f"; {len(never)} never fully visible" if never else "") + ".")
    return result


# ------------------------------------------------------------------------------------------------ facts
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")


def numbers_in(text: str) -> set[str]:
    return {n.strip(".,") for n in _NUMBER.findall(text or "")}


def get_facts(store, composition: str) -> Optional[str]:
    path = store.folder(composition) / FACTS_FILE
    return path.read_text(encoding="utf-8") if path.is_file() else None


def set_facts(store, composition: str, content: str) -> dict:
    store.get(composition)
    path = store.folder(composition) / FACTS_FILE
    if not content.strip():
        if path.is_file():
            path.unlink()
        return {"composition": composition, "facts": None}
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content.strip() + "\n", encoding="utf-8")
    temp.replace(path)
    return {"composition": composition, "facts": content.strip(), "numbers": sorted(numbers_in(content))}


def facts_check(store, composition: str, revision: Optional[int] = None, ctx=None) -> dict:
    """The numbers the video shows must be in FACTS.md, and the facts listed must be shown somewhere."""
    manifest = store.get(composition, revision)
    facts = get_facts(store, composition)
    if facts is None:
        raise LumiereError("No FACTS.md yet: write the claims and where each comes from with motion_facts.", code="no_facts")
    sample = read_texts(store, manifest, 0.25, ctx)
    shown: dict[str, list] = {}
    for t, texts in zip(sample["times"], sample["frames"]):
        for item in texts:
            if item.get("opacity", 1.0) < 0.5:
                continue
            for number in numbers_in(item["text"]):
                shown.setdefault(number, []).append(t)
    listed = numbers_in(facts)
    unsourced = [{"number": n, "first_seen": round(min(ts), 2)} for n, ts in shown.items() if n not in listed]
    unused = sorted(n for n in listed if n not in shown)
    return {"composition": composition, "revision": manifest["revision"], "numbers_shown": sorted(shown), "unsourced": sorted(unsourced, key=lambda r: r["first_seen"]),
            "never_shown": unused, "ok": not unsourced,
            "summary": f"{len(shown) - len(unsourced)}/{len(shown)} numbers shown are in FACTS.md" + (f"; {len(unused)} listed fact number(s) are not shown" if unused else "") + "."}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
