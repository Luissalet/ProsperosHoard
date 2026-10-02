"""Productions: a whole music-video production run inside the app, as one
resumable, checkpointed pipeline (the in-app form of
`scripts/productions/no_mires_atras.py`).

A production lives in `data/productions/<slug>/`:

- `state.json`: the **spec** (the lead character, the world look, the song,
  every shot with its prompt, seed, variants and clip settings, photocard
  looks, album designs, the cut's storyboard, finishing and renders), the
  **settings** (`animatic`, `qa`), and the progress: `done[stage]` for every
  finished stage, per-item checkpoints inside the long stages, the sub-jobs
  in flight, timings and a **lineage** log (what was made, reused, retried
  and why).
- `REPORT.md`: a readable report written at the end (and on demand).

Stages run in `STAGES` order; each skips when its `done` entry is complete,
so a production resumes where it stopped (a crash, a cancel, a pause for
review). The runner never touches ComfyUI or ffmpeg itself: GPU/CPU work is
queued as ordinary jobs through a `Studio` (see `api.py` for the real one,
the tests for a fake), and the synchronous steps (cast, lyric timing, the
auto-cut, designs) call `engine` directly.

Scripted productions (the `state.json` the production script writes, with
numbered steps) are read too: `spec_from_legacy` rebuilds a spec from the
ids it recorded and each asset's own recipe (lineage), so a finished
scripted run can be exported as a recipe (see `recipes.py`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from . import engine
from .ids import new_id
from .jobs import JobCancelled
from .store import NotFound, Store
from .util import now_iso, replace_with_retry

FORMAT = "prospero.production/1"
STAGES = ("character", "song", "frames", "lyrics", "animatic", "clips", "photocards", "album", "timeline", "report")
ITEM_STAGES = ("frames", "clips", "photocards")  # checkpointed item by item
STATUSES = ("queued", "running", "awaiting_review", "done", "failed", "cancelled")
SUB_JOB_POLL_S = 0.5

DEFAULT_SETTINGS: dict[str, Any] = {
    "animatic": True,
    "animatic_autocontinue": False,
    "qa": {"enabled": False, "thresholds": {}, "max_retries": 2},
    "clip_quality": "final",
}

# Wan's stock negative lists "static" and "motionless frame" among the things
# to avoid, which makes a still figure walk (the real run's "FAROL walked"
# lesson): shots whose subject must stay still use the stock negative without
# those terms, plus walking.
STILL_NEGATIVE = ("色调艳丽，过曝，细节模糊不清，字幕，风格，作品，画作，画面，整体发灰，最差质量，低质量，"
                  "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，"
                  "形态畸形的肢体，手指融合，杂乱的背景，三条腿，背景人很多，倒着走，走路，迈步，"
                  "walking, stepping, striding, moving figure, turning around")
HEADROOM = "full-length portrait, the whole figure in frame with clear empty space above the head"

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class ProductionError(engine.EngineError):
    pass


class Studio(Protocol):
    """What the runner needs from the app to queue work. Each call returns
    the queued job (a dict with at least `id` and `state`)."""

    def generate(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def compose(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def render(self, timeline_id: str, quality: str) -> dict[str, Any]: ...

    def job(self, job_id: str) -> dict[str, Any]: ...

    def cancel(self, job_id: str) -> None: ...


# ------------------------------------------------------------------ paths

def productions_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "productions"


def slugify(name: str, fallback: str = "production") -> str:
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:60]
    return text or fallback


def _check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", slug):
        raise ProductionError("bad_production", f"'{str(slug)[:80]}' is not a production name (lowercase letters, digits, _ and -)")
    return slug


def production_dir(data_dir: Path, slug: str) -> Path:
    return productions_dir(data_dir) / _check_slug(slug)


def lock_for(slug: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(slug, threading.RLock())


# Who answers "is this job still alive?" - the app sets it (store.get_job);
# without it a "running" state is taken at its word.
_job_probe: Optional[Callable[[str], Optional[str]]] = None
_LIVE_JOB_STATES = ("queued", "waiting_gpu", "running")


def set_job_probe(probe: Optional[Callable[[str], Optional[str]]]) -> None:
    global _job_probe
    _job_probe = probe


def reconcile_status(state: dict[str, Any]) -> bool:
    """A state left "running" by a run that died without writing its end
    (the app closed, a write failed) becomes "failed", so it can be edited
    and resumed. Returns True when it changed the state (not saved)."""
    # only "running": "queued" is also what an edit leaves behind (waiting
    # for a continue), with the previous run's finished job still recorded
    if state.get("status") != "running" or _job_probe is None:
        return False
    job_id = state.get("job_id")
    try:
        job_state = _job_probe(job_id) if job_id else None
    except Exception:  # noqa: BLE001 - a missing job is a dead job
        job_state = None
    if job_state in _LIVE_JOB_STATES:
        return False
    state["status"] = "failed"
    state["message"] = state.get("message") if job_state == "failed" and state.get("message") else (
        "the run stopped without finishing (the app closed or the job ended); resume it")
    log(state, state.get("stage") or "", "stale_run_recovered", job_state=job_state)
    return True


def is_running(state: dict[str, Any], data_dir: Optional[Path] = None) -> bool:
    """True while a run really owns the state; a stale "running" is fixed
    (and saved when `data_dir` is given) on the way."""
    if state.get("status") != "running":
        return False
    if reconcile_status(state):
        if data_dir is not None:
            save_state(data_dir, state)
        return False
    return True


def load_state(data_dir: Path, slug: str) -> dict[str, Any]:
    path = production_dir(data_dir, slug) / "state.json"
    if not path.is_file():
        raise NotFound("production", slug)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionError("bad_production", f"production '{slug}' has an unreadable state.json: {exc}") from None
    state.setdefault("slug", slug)
    return state


def load_fresh(data_dir: Path, slug: str) -> dict[str, Any]:
    """load_state for readers (lists, the detail view): a stale "running"
    left by a dead run is turned into "failed" and saved first."""
    state = load_state(data_dir, slug)
    if state.get("status") == "running" and _job_probe is not None:
        with lock_for(slug):
            state = load_state(data_dir, slug)
            if reconcile_status(state):
                save_state(data_dir, state)
    return state


def save_state(data_dir: Path, state: dict[str, Any]) -> None:
    folder = production_dir(data_dir, state["slug"])
    folder.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp = folder / f"state.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    replace_with_retry(tmp, folder / "state.json")


def is_legacy(state: dict[str, Any]) -> bool:
    """A state written by the production script (numbered steps, no spec)."""
    return state.get("format") != FORMAT and isinstance(state.get("done"), dict) and "1" in state["done"]


def list_productions(data_dir: Path) -> list[dict[str, Any]]:
    root = productions_dir(data_dir)
    out = []
    if not root.is_dir():
        return out
    for folder in sorted(root.iterdir()):
        if not (folder / "state.json").is_file() or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", folder.name):
            continue
        try:
            state = load_fresh(data_dir, folder.name)
        except (ProductionError, NotFound, OSError):
            continue
        out.append(summary_view(state))
    out.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return out


def productions_led_by(data_dir: Path, character_id: str, unfinished: bool = True) -> list[str]:
    """Names of the productions whose lead is this cast entry (by default
    only the ones that have not finished: those still read it)."""
    root = productions_dir(data_dir)
    names = []
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        if not (folder / "state.json").is_file() or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", folder.name):
            continue
        try:
            state = load_state(data_dir, folder.name)
        except (ProductionError, NotFound, OSError):
            continue
        if is_legacy(state) or (unfinished and state.get("status") == "done"):
            continue
        if ((state.get("spec") or {}).get("lead") or {}).get("character_id") == character_id:
            names.append(state.get("name") or state["slug"])
    return names


def productions_of_project(data_dir: Path, project_id: str) -> list[dict[str, Any]]:
    """The productions whose pictures, clips and cut live in `project_id`."""
    return [p for p in list_productions(data_dir) if p.get("project_id") == project_id]


def _stash_dir(data_dir: Path, project_id: str) -> Path:
    return data_dir / "trash" / "projects" / re.sub(r"[^A-Za-z0-9_]", "_", project_id) / "productions"


def stash_productions(data_dir: Path, project_id: str) -> list[str]:
    """A project going to the trash takes its productions with it (moved
    under data/trash/projects/<id>/productions, back on restore)."""
    moved = []
    dest = _stash_dir(data_dir, project_id)
    for p in productions_of_project(data_dir, project_id):
        src = production_dir(data_dir, p["slug"])
        dest.mkdir(parents=True, exist_ok=True)
        with lock_for(p["slug"]):
            shutil.move(str(src), str(dest / p["slug"]))
        moved.append(p["slug"])
    return moved


def unstash_productions(data_dir: Path, project_id: str) -> list[str]:
    src = _stash_dir(data_dir, project_id)
    back = []
    if src.is_dir():
        for folder in sorted(src.iterdir()):
            target = productions_dir(data_dir) / folder.name
            if target.exists():  # a new production took the slug meanwhile
                target = productions_dir(data_dir) / unique_slug(data_dir, folder.name)
                state_path = folder / "state.json"
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["slug"] = target.name
                    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            productions_dir(data_dir).mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(target))
            back.append(target.name)
        shutil.rmtree(src.parent, ignore_errors=True)
    return back


def purge_stashed_productions(data_dir: Path, project_id: str) -> int:
    src = _stash_dir(data_dir, project_id)
    n = len(list(src.iterdir())) if src.is_dir() else 0
    shutil.rmtree(src.parent, ignore_errors=True)
    return n


def unique_slug(data_dir: Path, name: str) -> str:
    base = slugify(name)
    slug, n = base, 2
    while (productions_dir(data_dir) / slug).exists():
        slug = f"{base}_{n}"
        n += 1
    return slug


# ------------------------------------------------------------------- spec

def _int(value: Any, where: str, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ProductionError("bad_spec", f"{where} must be a whole number") from None
    if not lo <= number <= hi:
        raise ProductionError("bad_spec", f"{where} must be between {lo} and {hi}")
    return number


def shot_key(n: Any, variant: int = 0) -> str:
    return f"{n}" if variant == 0 else f"{n}v{variant + 1}"


def split_key(key: Any) -> tuple[str, int]:
    """"3" -> ("3", 0); "3v2" -> ("3", 1) (the runner-up variant)."""
    text = str(key)
    m = re.fullmatch(r"(.+?)v(\d+)", text)
    if m and int(m.group(2)) >= 2:
        return m.group(1), int(m.group(2)) - 1
    return text, 0


# a shot's song section (what the planner writes) -> the section kind the
# timed lyrics carry, which the auto-cut matches storyboard pools against
SHOT_SECTIONS = ("intro", "verse", "prechorus", "chorus", "bridge", "breakdown", "outro")
_SECTION_TO_KIND = {"prechorus": "pre", "breakdown": "bridge"}


def motion_pacing(spec: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Moving shots need time to move: when the cut will use clips, cut every
    ~`clip_settings.cut_s` seconds (4) on whole bars instead of every couple
    of beats (a dance chopped every beat reads as a slideshow). Explicit
    `beats_*` options win. The animatic uses it too, so its cut points stay
    those of the final cut."""
    tl = spec.get("timeline") or {}
    moving = any(s.get("clips") and s.get("motion", "move") == "move" for s in spec.get("shots") or [])
    if not (moving and tl.get("prefer_clips", True)):
        return options
    bpm = float((spec.get("song") or {}).get("bpm") or 120)
    target = float((spec.get("clip_settings") or {}).get("cut_s", 4.0))
    beats = int(max(4, min(16, 4 * round(target * bpm / 60 / 4))))
    options.setdefault("beats_low", beats)
    options.setdefault("beats_mid", beats)
    options.setdefault("beats_high", max(4, beats // 2))
    return options


def storyboard_for(spec: dict[str, Any]) -> dict[str, list[str]]:
    """The cut's storyboard: the one written in the spec, else one built from
    each shot's `section` (shots of the chorus play over the chorus, in their
    order). Empty when no shot names a section: the cut then draws freely."""
    board = (spec.get("timeline") or {}).get("storyboard") or {}
    if board:
        return board
    out: dict[str, list[str]] = {}
    for shot in spec.get("shots") or []:
        section = str(shot.get("section") or "").lower()
        if section in SHOT_SECTIONS:
            out.setdefault(_SECTION_TO_KIND.get(section, section), []).append(shot["key"])
    return out


def shot_span(shot: dict[str, Any]) -> Optional[tuple[float, float]]:
    """(start_s, end_s) of a shot placed on its stretch of the song, or None."""
    start, end = shot.get("start_s"), shot.get("end_s")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
        return float(start), float(end)
    return None


def _parse_span(value: Any, key: str) -> Optional[tuple[float, float]]:
    if value in (None, {}, []):
        return None
    if not isinstance(value, dict):
        raise ProductionError("bad_changes", f"shot {key}: span is {{start_s, end_s}} in seconds, or null to free it")
    try:
        start, end = round(float(value["start_s"]), 3), round(float(value["end_s"]), 3)
    except (KeyError, TypeError, ValueError):
        raise ProductionError("bad_changes", f"shot {key}: span needs numeric start_s and end_s") from None
    if start < 0 or end > 3600 or end - start < 0.5:
        raise ProductionError("bad_changes", f"shot {key}: span must start at 0 s or later and last at least 0.5 s")
    return start, end


def _set_span(shot: dict[str, Any], span: Optional[tuple[float, float]]) -> None:
    if span is None:
        shot.pop("start_s", None)
        shot.pop("end_s", None)
    else:
        shot["start_s"], shot["end_s"] = span


def _check_spans(spec: dict[str, Any]) -> None:
    placed = sorted(((shot_span(s), s["key"]) for s in spec.get("shots") or [] if shot_span(s)), key=lambda x: x[0][0])
    for (a, ka), (b, kb) in zip(placed, placed[1:]):
        if b[0] < a[1] - 1e-6:
            raise ProductionError("bad_changes", f"shot {kb} ({b[0]:.1f}-{b[1]:.1f} s) overlaps shot {ka} "
                                                 f"({a[0]:.1f}-{a[1]:.1f} s); each stretch of the song plays one shot")


def pinned_spans(spec: dict[str, Any], assets_for: Callable[[str], list[str]]) -> list[dict[str, Any]]:
    """The cut's pinned spans: every shot placed on its stretch of the song,
    with the assets that stand for it (its clips, else its still)."""
    out = []
    for shot in spec.get("shots") or []:
        span = shot_span(shot)
        ids = [a for a in assets_for(shot["key"]) if a] if span else []
        if span and ids:
            entry = {"start_s": span[0], "end_s": span[1], "asset_ids": list(dict.fromkeys(ids)), "shot": shot["key"]}
            if shot.get("sing"):
                entry["synced"] = True  # its clip sings this very stretch: played from its first second
            out.append(entry)
    return sorted(out, key=lambda x: x["start_s"])


def apply_pins(options: dict[str, Any], pool: list[str], pools: Optional[dict[str, list[str]]],
               spans: list[dict[str, Any]]) -> tuple[list[str], Optional[dict[str, list[str]]]]:
    """Put the pinned spans into the cut options and keep their assets out of
    the free pool and the section pools (a placed shot plays only where it
    was placed), unless that would leave the free pool empty."""
    if not spans:
        return pool, pools
    options["pinned_spans"] = [{k: v for k, v in sp.items() if k != "shot"} for sp in spans]
    pinned = {a for sp in spans for a in sp["asset_ids"]}
    free = [a for a in pool if a not in pinned]
    if pools:
        pools = {k: [a for a in v if a not in pinned] for k, v in pools.items()}
        pools = {k: v for k, v in pools.items() if v} or None
    return (free or pool), pools


def _shot_refs(value: Any, key: str) -> list[dict[str, str]]:
    """A shot's extra references: [{asset_id, use}] - a pose to copy, the
    characters to show in the background, a place, a prop."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 6:
        raise ProductionError("bad_changes", f"shot {key}: refs is a list of at most 6 {{asset_id, use}}")
    out = []
    for r in value:
        if isinstance(r, str):
            r = {"asset_id": r}
        if not isinstance(r, dict) or not re.fullmatch(r"a_[A-Za-z0-9]{6,40}", str(r.get("asset_id") or "")):
            raise ProductionError("bad_changes", f"shot {key}: each ref needs an asset_id")
        out.append({"asset_id": str(r["asset_id"]), "use": str(r.get("use") or "").strip()[:240]})
    return out


CAST_MAX = 24
CAST_PER_SHOT_MAX = 6


def normalise_cast(value: Any) -> list[dict[str, str]]:
    """The production's background cast: [{asset_id, name, note}]. Crowd
    shots draw their background characters only from it, from its images,
    so nobody in the background is made up."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > CAST_MAX:
        raise ProductionError("bad_cast", f"the background cast is a list of at most {CAST_MAX} {{asset_id, name}}")
    out: list[dict[str, str]] = []
    names: set[str] = set()
    for i, member in enumerate(value):
        if not isinstance(member, dict) or not re.fullmatch(r"a_[A-Za-z0-9]{6,40}", str(member.get("asset_id") or "")):
            raise ProductionError("bad_cast", f"cast[{i}] needs the asset_id of its image")
        name = re.sub(r"\s+", " ", str(member.get("name") or "")).strip()[:60]
        if not name:
            raise ProductionError("bad_cast", f"cast[{i}] needs a name")
        if name.lower() in names:
            raise ProductionError("bad_cast", f"the cast has two members called '{name}'")
        names.add(name.lower())
        out.append({"asset_id": str(member["asset_id"]), "name": name,
                    "note": re.sub(r"\s+", " ", str(member.get("note") or "")).strip()[:200]})
    return out


def _shot_cast_names(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > CAST_PER_SHOT_MAX or any(not isinstance(v, str) for v in value):
        raise ProductionError("bad_changes", f"shot {key}: cast is a list of at most {CAST_PER_SHOT_MAX} cast names")
    return [v.strip()[:60] for v in value if v.strip()]


def cast_for_shot(spec: dict[str, Any], shot: dict[str, Any]) -> list[dict[str, str]]:
    """Who stands in a shot's background: the names the shot lists, else -
    for a crowd shot - a rotating group of `cast_per_shot`, so every member
    shows up across the video and each crowd differs from the last."""
    cast = spec.get("cast") or []
    if not cast:
        return []
    by_name = {m["name"].lower(): m for m in cast}
    named = [by_name[n.lower()] for n in shot.get("cast") or [] if n.lower() in by_name]
    if named:
        return named
    if not shot.get("crowd"):
        return []
    per = max(1, min(int(spec.get("cast_per_shot") or 3), CAST_PER_SHOT_MAX, len(cast)))
    # keyed on the shot's own number, not its place among the crowd shots,
    # so adding or removing another shot never reshuffles this one's crowd
    digits = re.sub(r"\D", "", str(shot.get("key") or ""))
    turn = int(digits) - 1 if digits else sum(map(ord, str(shot.get("key") or "")))
    start = (turn * per) % len(cast)
    return [cast[(start + i) % len(cast)] for i in range(per)]


def normalise_spec(spec: Any) -> dict[str, Any]:
    """Validate a production spec and fill defaults. Raises ProductionError
    with the offending field."""
    if not isinstance(spec, dict):
        raise ProductionError("bad_spec", "spec must be an object")
    spec = json.loads(json.dumps(spec))  # a private copy
    lead = spec.get("lead")
    if not isinstance(lead, dict) or not (lead.get("character_id") or (str(lead.get("name") or "").strip()
                                                                       and str(lead.get("look") or "").strip())):
        raise ProductionError("bad_spec", "spec.lead needs a character_id, or a name and a look")
    lead.setdefault("palette", [])
    if not isinstance(lead["palette"], list):
        raise ProductionError("bad_spec", "spec.lead.palette must be a list of #hex colours")
    spec.setdefault("title", lead.get("name") or "Production")
    spec.setdefault("engine", "auto")
    spec.setdefault("lead_route", "auto")
    if spec["lead_route"] not in ("auto", "reference"):
        raise ProductionError("bad_spec", "spec.lead_route is 'auto' (adapter when the lead has one) or 'reference'")
    if spec["engine"] not in engine.IMAGE_ENGINES:
        raise ProductionError("bad_spec", f"spec.engine must be one of {', '.join(engine.IMAGE_ENGINES)}")
    world = spec.setdefault("world", {})
    world.setdefault("look", "")
    world.setdefault("negative", "")
    spec["cast"] = normalise_cast(spec.get("cast"))
    spec["cast_per_shot"] = _int(spec.get("cast_per_shot", 3), "spec.cast_per_shot", 1, CAST_PER_SHOT_MAX)
    ref = spec.get("reference")
    if ref is not None:
        if not isinstance(ref, dict):
            raise ProductionError("bad_spec", "spec.reference must be an object")
        ref.setdefault("prompt", "{look}, character turnaround reference sheet, three full-body poses side by side on "
                                 "a neutral grey seamless studio backdrop: front view, three-quarter view, back view, "
                                 "identical design and proportions in every pose, even studio lighting")
        ref["seed"] = _int(ref.get("seed", 1001), "spec.reference.seed", 0, 2**31 - 2)
        ref["count"] = _int(ref.get("count", 2), "spec.reference.count", 1, 8)
        ref["pick"] = _int(ref.get("pick", 0), "spec.reference.pick", 0, ref["count"] - 1)
        ref.setdefault("aspect", "16:9")
        ref.setdefault("crop", "left_third")
    song = spec.get("song")
    if song is not None:
        if not isinstance(song, dict):
            raise ProductionError("bad_spec", "spec.song must be an object")
        if not song.get("asset_id") and not (str(song.get("tags") or "").strip() and str(song.get("lyrics") or "").strip()):
            raise ProductionError("bad_spec", "spec.song needs tags and lyrics (or an existing asset_id)")
        song["bpm"] = _int(song.get("bpm", 120), "spec.song.bpm", 40, 220)
        song["count"] = _int(song.get("count", 1), "spec.song.count", 1, 4)
        song["take"] = _int(song.get("take", 1), "spec.song.take", 1, song["count"])
        song["seed"] = _int(song.get("seed", 2001), "spec.song.seed", 0, 2**31 - 2)
        song.setdefault("duration", 120.0)
        song.setdefault("key", "C major")
        song.setdefault("language", "en")
        song.setdefault("time_signature", 4)
    shots = spec.get("shots") or []
    if not isinstance(shots, list) or len(shots) > 80:
        raise ProductionError("bad_spec", "spec.shots must be a list of at most 80 shots")
    seen: set[str] = set()
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict) or not str(shot.get("prompt") or "").strip():
            raise ProductionError("bad_spec", f"spec.shots[{i}] needs a prompt")
        key = str(shot.get("key") or shot.get("n") or i + 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", key) or re.fullmatch(r".+v\d+", key):
            raise ProductionError("bad_spec", f"spec.shots[{i}].key '{key}' must be short letters/digits (not ending in v<number>)")
        if key in seen:
            raise ProductionError("bad_spec", f"spec.shots has two shots with key '{key}'")
        seen.add(key)
        shot["key"] = key
        shot["lead"] = bool(shot.get("lead", False))
        number = int(re.sub(r"\D", "", key) or i + 1)
        shot["seed"] = _int(shot.get("seed", 3000 + 10 * number), f"spec.shots[{i}].seed", 0, 2**31 - 2)
        shot["variants"] = _int(shot.get("variants", 1), f"spec.shots[{i}].variants", 1, 8)
        shot["best"] = _int(shot.get("best", 0), f"spec.shots[{i}].best", 0, shot["variants"] - 1)
        clips = shot.get("clips")
        if clips is None:
            clips = [0] if shot.get("clip") else []
        if not isinstance(clips, list) or any(not isinstance(v, int) or not 0 <= v < shot["variants"] for v in clips):
            raise ProductionError("bad_spec", f"spec.shots[{i}].clips must list variant indices (0 = the best still)")
        shot["clips"] = sorted(set(clips))
        shot.pop("clip", None)
        if shot.get("motion", "move") not in ("still", "move"):
            raise ProductionError("bad_spec", f"spec.shots[{i}].motion must be 'still' or 'move'")
        shot.setdefault("motion", "move")
        shot.setdefault("motion_prompt", "subtle motion")
        shot["clip_seed"] = _int(shot.get("clip_seed", 5000 + number), f"spec.shots[{i}].clip_seed", 0, 2**31 - 2)
        shot["crowd"] = bool(shot.get("crowd", False))
        if shot.get("locked"):
            shot["locked"] = True
        else:
            shot.pop("locked", None)
        cont = shot.get("continue_from")
        if cont not in (None, ""):
            cont = str(cont)
            if cont == key or cont not in seen:
                raise ProductionError("bad_spec", f"spec.shots[{i}].continue_from must name an earlier shot")
            shot["continue_from"] = cont
            shot["motion"] = "move"
            if not shot.get("clips"):
                shot["clips"] = [0]
        else:
            shot.pop("continue_from", None)
        if shot.get("sing"):
            shot["sing"] = True
            if not shot.get("clips"):
                shot["clips"] = [0]
        else:
            shot.pop("sing", None)
        names = _shot_cast_names(shot.get("cast"), key)
        known = {m["name"].lower() for m in spec["cast"]}
        unknown = [n for n in names if n.lower() not in known]
        if unknown:
            raise ProductionError("bad_spec", f"spec.shots[{i}].cast names {', '.join(unknown)}, not in spec.cast")
        if names:
            shot["cast"] = names
        else:
            shot.pop("cast", None)
        if not (shot.get("width") and shot.get("height")):
            shot.setdefault("aspect", "16:9")
        if shot.get("start_s") is not None or shot.get("end_s") is not None:
            try:
                _set_span(shot, _parse_span({"start_s": shot.get("start_s"), "end_s": shot.get("end_s")}, key))
            except ProductionError as exc:
                raise ProductionError("bad_spec", exc.message) from None
        else:
            _set_span(shot, None)
    try:
        _check_spans(spec)
    except ProductionError as exc:
        raise ProductionError("bad_spec", exc.message) from None
    clip_settings = spec.setdefault("clip_settings", {})
    clip_settings.setdefault("template", "auto_clip")
    clip_settings.setdefault("still_negative", STILL_NEGATIVE)
    looks = (spec.get("photocards") or {}).get("looks") or []
    if not isinstance(looks, list) or len(looks) > 12:
        raise ProductionError("bad_spec", "spec.photocards.looks must be a list of at most 12 looks")
    for i, look in enumerate(looks):
        if not isinstance(look, dict) or not str(look.get("prompt") or "").strip():
            raise ProductionError("bad_spec", f"spec.photocards.looks[{i}] needs a prompt")
        look["seed"] = _int(look.get("seed", 4001 + i), f"spec.photocards.looks[{i}].seed", 0, 2**31 - 2)
    album = spec.get("album") or []
    if not isinstance(album, list) or len(album) > 12:
        raise ProductionError("bad_spec", "spec.album must be a list of at most 12 designs")
    tl = spec.setdefault("timeline", {})
    if not isinstance(tl, dict):
        raise ProductionError("bad_spec", "spec.timeline must be an object")
    tl.setdefault("aspects", ["9:16"])
    tl.setdefault("qualities", ["preview"])
    tl.setdefault("options", {})
    tl.setdefault("finishing", {})
    tl.setdefault("storyboard", {})
    tl.setdefault("prefer_clips", True)
    for aspect in tl["aspects"]:
        if aspect not in engine.timeline_mod.ASPECTS:
            raise ProductionError("bad_spec", f"spec.timeline.aspects: '{aspect}' is not one of {', '.join(engine.timeline_mod.ASPECTS)}")
    for quality in tl["qualities"]:
        if quality not in ("preview", "final"):
            raise ProductionError("bad_spec", "spec.timeline.qualities holds 'preview' and/or 'final'")
    for section, keys in (tl["storyboard"] or {}).items():
        if not isinstance(keys, list):
            raise ProductionError("bad_spec", f"spec.timeline.storyboard['{section}'] must be a list of shot keys")
        for k in keys:
            base, variant = split_key(k)
            shot = next((s for s in shots if s["key"] == base), None)
            if shot is None or variant >= shot["variants"]:
                raise ProductionError("bad_spec", f"spec.timeline.storyboard['{section}'] names unknown shot '{k}'")
    return spec


def normalise_settings(settings: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    if settings is None:
        return out
    if not isinstance(settings, dict):
        raise ProductionError("bad_settings", "settings must be an object")
    for key in ("animatic", "animatic_autocontinue"):
        if key in settings:
            out[key] = bool(settings[key])
    if "qa" in settings:
        qa = settings["qa"]
        if not isinstance(qa, dict):
            raise ProductionError("bad_settings", "settings.qa must be {enabled, thresholds, max_retries}")
        out["qa"]["enabled"] = bool(qa.get("enabled", out["qa"]["enabled"]))
        if qa.get("thresholds") is not None:
            if not isinstance(qa["thresholds"], dict):
                raise ProductionError("bad_settings", "settings.qa.thresholds must be an object")
            out["qa"]["thresholds"] = qa["thresholds"]
        if qa.get("max_retries") is not None:
            out["qa"]["max_retries"] = _int(qa["max_retries"], "settings.qa.max_retries", 0, 5)
    if settings.get("clip_quality") is not None:
        if settings["clip_quality"] not in ("draft", "final"):
            raise ProductionError("bad_settings", "settings.clip_quality is 'draft' (fast 5B clips) or 'final'")
        out["clip_quality"] = settings["clip_quality"]
    for key, value in settings.items():
        if key not in out:
            out[key] = value  # forward-compatible extras (kept, not interpreted)
    return out


def new_state(slug: str, name: str, spec: dict[str, Any], settings: dict[str, Any], project_id: Optional[str] = None,
              recipe: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {
        "format": FORMAT, "slug": slug, "name": name, "project_id": project_id, "status": "queued", "stage": None,
        "created_at": now_iso(), "updated_at": now_iso(), "recipe": recipe, "spec": spec, "settings": settings,
        "done": {}, "partial": {}, "timings": {}, "lineage": [], "review": {}, "job_id": None, "message": None,
    }


def create_production(data_dir: Path, name: str, spec: dict[str, Any], settings: Optional[dict[str, Any]] = None,
                      project_id: Optional[str] = None, recipe: Optional[dict[str, Any]] = None,
                      slug: Optional[str] = None) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise ProductionError("name_required", "a production needs a name")
    spec = normalise_spec(spec)
    settings = normalise_settings(settings)
    slug = _check_slug(slug) if slug else unique_slug(data_dir, name)
    if (productions_dir(data_dir) / slug).exists():
        raise ProductionError("production_exists", f"a production called '{slug}' already exists")
    state = new_state(slug, name.strip()[:120], spec, settings, project_id, recipe)
    save_state(data_dir, state)
    return state


def log(state: dict[str, Any], stage: str, event: str, **detail: Any) -> None:
    entry = {"at": now_iso(), "stage": stage, "event": event}
    entry.update({k: v for k, v in detail.items() if v is not None})
    state.setdefault("lineage", []).append(entry)
    if len(state["lineage"]) > 2000:
        del state["lineage"][:-2000]


# ----------------------------------------------------------------- views

def stage_status(state: dict[str, Any], stage: str) -> str:
    done = state.get("done", {}).get(stage)
    if done is None:
        return "pending"
    if isinstance(done, dict) and done.get("complete") is False:
        return "partial"
    return "done"


def stages_for(state: dict[str, Any]) -> list[str]:
    return list(state.get("stages") or STAGES)


def summary_view(state: dict[str, Any]) -> dict[str, Any]:
    if is_legacy(state):
        done = state.get("done", {})
        return {"slug": state["slug"], "name": state.get("name") or state["slug"], "legacy": True,
                "status": "done" if "9" in done else "partial", "project_id": done.get("1", {}).get("project_id"),
                "updated_at": state.get("updated_at"), "stages_done": sorted(done, key=lambda k: int(k) if k.isdigit() else 99),
                "cover": _legacy_cover(done), "shot_count": len((done.get("4") or {}).get("stills") or {})}
    stages = stages_for(state)
    return {"slug": state["slug"], "name": state.get("name"), "status": state.get("status"), "stage": state.get("stage"),
            "project_id": state.get("project_id"), "updated_at": state.get("updated_at"),
            "recipe": (state.get("recipe") or {}).get("name"), "message": state.get("message"),
            "animatic": bool((state.get("done", {}).get("animatic") or {}).get("renders")),
            "kind": state.get("kind") or "music_video", "cover": _cover(state),
            "progress": {"done": sum(1 for s in stages if stage_status(state, s) == "done"), "total": len(stages)},
            "shot_count": len((state.get("spec") or {}).get("shots") or [])}


def _legacy_cover(done: dict[str, Any]) -> Optional[dict[str, str]]:
    """A scripted production's cover: its final cut, else its album cover."""
    timelines = (done.get("8") or {}).get("timelines") or {}
    for aspect in ("16:9", "9:16", "1:1"):
        renders = (timelines.get(aspect) or {}).get("renders") or {}
        rid = renders.get("final") or renders.get("preview")
        if rid:
            return {"asset_id": rid, "kind": "video"}
    cover = (done.get("7") or {}).get("cover_id")
    return {"asset_id": cover, "kind": "image"} if cover else None


def _cover(state: dict[str, Any]) -> Optional[dict[str, str]]:
    """The picture a production shows in a list: its final cut, else its
    animatic, else its first still."""
    done = state.get("done") or {}
    for info in ((done.get("timeline") or {}).get("timelines") or {}).values():
        renders = info.get("renders") or {}
        rid = renders.get("final") or renders.get("preview")
        if rid:
            return {"asset_id": rid, "kind": "video"}
    for rid in ((done.get("animatic") or {}).get("renders") or {}).values():
        if rid:
            return {"asset_id": rid, "kind": "video"}
    frames = (done.get("frames") or {}).get("items") or {}
    for shot in (state.get("spec") or {}).get("shots") or []:
        best = (frames.get(shot.get("key")) or {}).get("best")
        if best:
            return {"asset_id": best, "kind": "image"}
    return None


def compact_view(state: dict[str, Any]) -> dict[str, Any]:
    """What an agent sees: status, each stage's state, the ids that matter,
    the animatic, the last QA summary and what to do next."""
    view = summary_view(state)
    if view.get("legacy"):
        view["note"] = "made by the production script; export it as a recipe with studio_recipe_export"
        return view
    spec = state.get("spec") or {}
    done = state.get("done", {})
    view["stages"] = {s: stage_status(state, s) for s in stages_for(state)}
    if state.get("kind") == "short":
        from .shorts import compact_extras

        view.update(compact_extras(state))
        timeline = done.get("timeline") or {}
        if timeline.get("timelines"):
            view["renders"] = {aspect: info.get("renders") for aspect, info in timeline["timelines"].items()}
            stale = {aspect: info["previous_renders"] for aspect, info in timeline["timelines"].items()
                     if info.get("previous_renders") and not info.get("renders")}
            if stale:
                view["previous_renders"] = stale
        animatic = done.get("animatic") or {}
        if animatic.get("renders"):
            view["animatic"] = {"renders": animatic["renders"], "clips_planned": (animatic.get("plan") or {}).get("clips_planned"),
                                "gpu_minutes_estimate": (animatic.get("plan") or {}).get("gpu_minutes")}
        if state.get("status") == "failed":
            view["next"] = "fix the cause in the message, then studio_production_continue(production) resumes where it stopped"
        return view
    view["lead"] = (spec.get("lead") or {}).get("name")
    view["shots"] = len(spec.get("shots") or [])
    if done.get("character"):
        view["character_id"] = done["character"].get("character_id")
    if done.get("song"):
        view["song_asset_id"] = done["song"].get("song_asset_id")
    animatic = done.get("animatic") or {}
    if animatic.get("renders"):
        view["animatic"] = {"renders": animatic["renders"], "gpu_minutes_estimate": (animatic.get("plan") or {}).get("gpu_minutes"),
                            "clips_planned": (animatic.get("plan") or {}).get("clips_planned")}
    timeline = done.get("timeline") or {}
    if timeline.get("timelines"):
        view["renders"] = {aspect: info.get("renders") for aspect, info in timeline["timelines"].items()}
    qa = (state.get("qa") or {}).get("last")
    if qa:
        view["qa"] = {"stage": qa.get("stage"), "passed": qa.get("passed"), "failed": qa.get("failed"),
                      "skipped": qa.get("skipped"), "at": qa.get("at")}
    retries = [e for e in state.get("lineage", []) if e.get("event") == "qa_retry"]
    if retries:
        view["qa_retries"] = len(retries)
    if state.get("status") == "awaiting_review":
        view["next"] = ("look at the animatic, then studio_production_continue(production) to render the clips, "
                        "or studio_production_shots(...) to change shots first")
    elif state.get("status") == "failed":
        view["next"] = "fix the cause in the message, then studio_production_continue(production) resumes where it stopped"
    return view


# ------------------------------------------------------------ asset copies

def copy_asset(store: Store, asset_id: str, project_id: str) -> dict[str, Any]:
    """Reuse an asset of another project in `project_id`: the file is copied
    (a project owns its files) and the copy's recipe records where it came
    from. An asset already in the project is returned as is."""
    src = store.get_asset(asset_id)
    if src["project_id"] == project_id:
        return src
    new = new_id("a")
    src_path = store.data_dir / src["file_path"]
    dest = store.path_for_asset_file(new, src_path.suffix or ".bin")
    shutil.copyfile(src_path, dest)
    thumb = None
    if src.get("thumb_path") and (store.data_dir / src["thumb_path"]).is_file():
        thumb_dest = store.path_for_thumb(new)
        thumb_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(store.data_dir / src["thumb_path"], thumb_dest)
        thumb = thumb_dest.relative_to(store.data_dir).as_posix()
    recipe = dict(src.get("recipe") or {})
    recipe.update({"copied_from": src["id"], "copied_at": now_iso()})
    asset = store.create_asset(project_id=project_id, kind=src["kind"], file_path=dest.relative_to(store.data_dir).as_posix(),
                               mime=src.get("mime"), width=src.get("width"), height=src.get("height"),
                               duration_s=src.get("duration_s"), thumb_path=thumb, waveform=src.get("waveform"),
                               tags=src.get("tags") or [], source=src["source"], recipe=recipe, asset_id=new,
                               name=src.get("name"))
    if src.get("analysis"):
        store.set_asset_media(new, analysis=src["analysis"])
    return asset


def _asset_ok(store: Store, asset_id: Optional[str]) -> bool:
    if not asset_id:
        return False
    try:
        store.get_asset(asset_id)
        return True
    except NotFound:
        return False


def copy_character(store: Store, src: dict[str, Any], project_id: str) -> tuple[dict[str, Any], bool]:
    """Bring a character from another project into `project_id` (its cast):
    same name, look, palette, voice and canonical image; adapters are files
    shared by every project, so the copy keeps them and the trigger, not the
    dataset/sheet, whose images belong to the other project. A character of
    that name already in the project is reused. Returns (character, copied)."""
    existing = next((c for c in store.list_characters(project_id) if c["name"].lower() == src["name"].lower()), None)
    if existing:
        return existing, False
    canonical = copy_asset(store, src["canonical_asset_id"], project_id)["id"] if _asset_ok(store, src.get("canonical_asset_id")) else None
    char = store.create_character(project_id, src["name"], role=src.get("role"), bio=src.get("bio"), prompt=src.get("prompt"),
                                  element=src.get("element") or "character",
                                  negative=src.get("negative"), palette=src.get("palette") or [],
                                  canonical_asset_id=canonical, voice=src.get("voice"))
    src_kit = src.get("kit") or {}
    if src_kit:
        kit = {k: src_kit[k] for k in ("trigger", "use_adapters", "identity", "good_seeds", "library") if k in src_kit}
        kit["adapters"] = [dict(a) for a in src_kit.get("adapters") or []]
        kit["history"] = [{"at": now_iso(), "event": "copied", "detail": f"from {src['id']}"}]
        char = store.set_character_kit(char["id"], kit)
    return char, True


def adopt_lead(store: Store, state: dict[str, Any]) -> Optional[dict[str, Any]]:
    """When a production made in an existing project takes its lead from
    another project's cast, copy the lead into this project right away, so
    the project's Cast shows who the video is about (and @mentions there
    resolve) before the run reaches its character stage. Saves the state."""
    lead = (state.get("spec") or {}).get("lead") or {}
    pid = state.get("project_id")
    if not pid or not lead.get("character_id") or (state.get("done") or {}).get("character"):
        return None
    try:
        src = store.get_character(lead["character_id"])
    except NotFound:
        return None
    if src["project_id"] == pid:
        return None
    char, copied = copy_character(store, src, pid)
    lead["character_id"] = char["id"]
    if copied:
        log(state, "character", "copied_character", character_id=char["id"], from_character_id=src["id"],
            canonical_asset_id=char.get("canonical_asset_id"))
    save_state(store.data_dir, state)
    return char


# ----------------------------------------------------------------- runner

class Run:
    """One pass of the pipeline over a production's state."""

    def __init__(self, store: Store, studio: Studio, slug: str, progress: Callable[..., None],
                 qa_hook: Optional[Callable[["Run", str], None]] = None,
                 stage_hooks: Optional[dict[str, Callable[["Run"], Any]]] = None):
        self.store = store
        self.studio = studio
        self.slug = slug
        self.progress = progress
        self.qa_hook = qa_hook
        self.stage_hooks = stage_hooks or {}
        self.state = load_state(store.data_dir, slug)
        if is_legacy(self.state):
            raise ProductionError("legacy_production", f"'{slug}' was made by the production script; run it again from "
                                                       "a recipe (studio_recipe_export, then studio_recipe_run)")
        self.spec = self.state["spec"]
        self.stage = ""

    # -- bookkeeping
    def save(self) -> None:
        with lock_for(self.slug):
            # a concurrent change from the API (change shots) is merged by
            # re-reading only what the API may touch: review flags and settings
            try:
                current = load_state(self.store.data_dir, self.slug)
                self.state["review"] = current.get("review", self.state.get("review", {}))
                if current.get("settings"):
                    self.state["settings"] = current["settings"]  # changed from the page mid-run
            except (NotFound, ProductionError):
                pass
            save_state(self.store.data_dir, self.state)

    def log(self, event: str, **detail: Any) -> None:
        log(self.state, self.stage, event, **detail)

    def items(self, stage: str) -> dict[str, Any]:
        entry = self.state["done"].setdefault(stage, {"complete": False, "items": {}})
        entry.setdefault("items", {})
        return entry["items"]

    def pending(self, stage: str) -> dict[str, str]:
        return self.state.setdefault("partial", {}).setdefault(stage, {}).setdefault("pending", {})

    @property
    def project_id(self) -> str:
        return self.state["project_id"]

    def tick(self, fraction: float, message: str) -> None:
        self.progress(max(0.0, min(0.99, fraction)), message)

    # -- sub-jobs
    def wait_jobs(self, stage: str, on_done: Callable[[str, dict[str, Any]], None], label: str) -> None:
        """Wait for every sub-job recorded in `partial[stage].pending`,
        calling `on_done(key, job)` as each finishes. A failed sub-job fails
        the production (after the others finish or are cancelled); a
        cancelled production cancels what it queued."""
        pending = self.pending(stage)
        try:
            self._wait_loop(stage, pending, on_done, label)
        except JobCancelled:
            for job_id in list(pending.values()):
                try:
                    self.studio.cancel(job_id)
                except Exception:  # noqa: BLE001 - best effort
                    pass
            pending.clear()  # cancelled: a resume queues them again
            self.save()
            raise

    def _wait_loop(self, stage: str, pending: dict[str, str], on_done: Callable[[str, dict[str, Any]], None],
                   label: str) -> None:
        total = max(1, len(pending))
        failures: list[str] = []
        while pending:
            if hasattr(self.progress, "check_cancel"):
                self.progress.check_cancel()
            for key, job_id in list(pending.items()):
                try:
                    job = self.studio.job(job_id)
                except NotFound:
                    job = {"id": job_id, "state": "failed", "message": "the job no longer exists"}
                if job["state"] in ("queued", "waiting_gpu", "running"):
                    continue
                pending.pop(key, None)
                if job["state"] == "done":
                    on_done(key, job)
                else:
                    failures.append(f"{key}: {job.get('message') or job['state']}")
                    self.log("sub_job_failed", key=key, job_id=job_id, message=job.get("message"))
                self.save()
            if pending:
                done_n = total - len(pending)
                self.tick(self.stage_fraction(done_n / total), f"{label}: {done_n}/{total} done")
                time.sleep(SUB_JOB_POLL_S)
        if failures:
            raise ProductionError("sub_job_failed", f"{label} failed for {'; '.join(failures)[:600]}")

    def stage_fraction(self, inner: float) -> float:
        stages = stages_for(self.state)
        i = stages.index(self.stage) if self.stage in stages else 0
        return (i + inner) / len(stages)

    # -- pipeline
    def run(self) -> dict[str, Any]:
        state = self.state
        state["status"] = "running"
        state["message"] = None
        self.save()
        try:
            self._ensure_project()
            for stage in stages_for(state):
                self.stage = stage
                state["stage"] = stage
                if stage_status(state, stage) == "done":
                    continue
                fn = self.stage_hooks.get(stage) or getattr(self, f"stage_{stage}", None)
                if fn is None:
                    continue
                self.tick(self.stage_fraction(0.0), f"{stage}")
                started = time.monotonic()
                outcome = fn(self) if stage in self.stage_hooks else fn()
                state.setdefault("timings", {})[stage] = round(state.get("timings", {}).get(stage, 0)
                                                               + time.monotonic() - started, 1)
                self.save()
                if self.qa_hook and (state.get("settings", {}).get("qa") or {}).get("enabled"):
                    self.qa_hook(self, stage)
                if outcome == "pause":
                    state["status"] = "awaiting_review"
                    state["message"] = f"paused after {stage} for review"
                    self.log("paused_for_review")
                    self.save()
                    return {"slug": self.slug, "status": "awaiting_review", "stage": stage}
                self.save()
            state["status"] = "done"
            state["stage"] = None
            state["message"] = "done"
            self.save()
            return {"slug": self.slug, "status": "done"}
        except JobCancelled:
            state["status"] = "cancelled"
            state["message"] = f"cancelled during {self.stage}"
            self.save()
            raise
        except Exception as exc:
            state["status"] = "failed"
            state["message"] = f"{self.stage}: {getattr(exc, 'message', None) or exc}"[:800]
            self.log("failed", message=str(exc)[:400])
            self.save()
            raise

    def _ensure_project(self) -> None:
        if self.state.get("project_id"):
            try:
                self.store.get_project(self.state["project_id"])
                return
            except NotFound:
                pass
        brief = self.spec.get("brief") or f"Production '{self.state['name']}'"
        project = self.store.create_project(self.spec.get("title") or self.state["name"], brief)
        if self.spec.get("engine") and self.spec["engine"] != "auto":
            self.store.update_project(project["id"], image_engine=self.spec["engine"])
        self.state["project_id"] = project["id"]
        log(self.state, "project", "created_project", project_id=project["id"])
        self.save()

    # -- stages
    def stage_character(self) -> None:
        lead = self.spec["lead"]
        pid = self.project_id
        char: Optional[dict[str, Any]] = None
        if lead.get("character_id"):
            src = self.store.get_character(lead["character_id"])
            char = src if src["project_id"] == pid else self._copy_character(src)
        else:
            char = next((c for c in self.store.list_characters(pid) if c["name"].lower() == lead["name"].strip().lower()), None)
            if char is None:
                char = self.store.create_character(pid, lead["name"].strip(), role=lead.get("role") or "lead",
                                                   prompt=lead["look"], negative=lead.get("negative") or None,
                                                   palette=lead.get("palette") or [], bio=lead.get("bio"))
                self.log("created_character", character_id=char["id"], name=char["name"])
        # the spec now carries the character's own look (an existing
        # character's prompt is the source of truth for its look)
        lead["name"] = char["name"]
        lead.setdefault("look", char.get("prompt") or "")
        if not lead.get("look"):
            lead["look"] = char.get("prompt") or char["name"]
        entry: dict[str, Any] = {"character_id": char["id"]}
        if char.get("canonical_asset_id"):
            entry["canonical_asset_id"] = char["canonical_asset_id"]
            self.log("kept_canonical_reference", asset_id=char["canonical_asset_id"])
        elif self.spec.get("reference"):
            ref = self.spec["reference"]
            prompt = ref["prompt"].replace("{look}", lead["look"])
            pending = self.pending("character")
            if "sheet" not in pending and "reference_asset_ids" not in self.state["partial"].get("character", {}):
                job = self.studio.generate(pid, {"prompt": prompt, "negative": lead.get("negative") or None,
                                                 "engine": self.spec.get("engine"), "aspect": ref.get("aspect"),
                                                 "count": ref["count"], "seed": ref["seed"]})
                pending["sheet"] = job["id"]
                self.save()

            def done(_key: str, job: dict[str, Any]) -> None:
                self.state["partial"]["character"]["reference_asset_ids"] = _asset_ids(job)

            self.wait_jobs("character", done, "reference sheet")
            refs = self.state["partial"]["character"].get("reference_asset_ids") or []
            if not refs:
                raise ProductionError("no_reference", "the reference sheet produced no image")
            sheet = refs[min(ref["pick"], len(refs) - 1)]
            fields = engine.apply_canonical_crop(self.store, pid, {"canonical_asset_id": sheet,
                                                                   "canonical_crop": ref.get("crop") or "full",
                                                                   "reference_asset_ids": char.get("reference_asset_ids") or []})
            char = self.store.update_character(char["id"], **fields)
            entry.update({"reference_asset_ids": refs, "sheet_asset_id": sheet, "canonical_asset_id": char["canonical_asset_id"],
                          "canonical_crop": ref.get("crop")})
            self.log("canonical_reference", asset_id=char["canonical_asset_id"], sheet_asset_id=sheet)
        self.state["done"]["character"] = entry

    def _copy_character(self, src: dict[str, Any]) -> dict[str, Any]:
        char, copied = copy_character(self.store, src, self.project_id)
        if copied:
            self.log("copied_character", character_id=char["id"], from_character_id=src["id"],
                     canonical_asset_id=char.get("canonical_asset_id"))
        return char

    def stage_song(self) -> Optional[str]:
        song = self.spec.get("song")
        if not song:
            raise ProductionError("no_song", "the spec has no song; add spec.song (tags + lyrics) or an asset_id")
        pid = self.project_id
        if song.get("asset_id") and _asset_ok(self.store, song["asset_id"]):
            asset = copy_asset(self.store, song["asset_id"], pid)
            self.state["done"]["song"] = {"song_asset_ids": [asset["id"]], "song_asset_id": asset["id"],
                                          "duration_s": asset.get("duration_s"), "reused": song["asset_id"]}
            self.log("reused_song", asset_id=asset["id"], from_asset_id=song["asset_id"])
            return
        pending = self.pending("song")
        partial = self.state["partial"]["song"]
        if "takes" not in pending and not partial.get("song_asset_ids"):
            body = {k: song.get(k) for k in ("tags", "lyrics", "bpm", "duration", "key", "language", "time_signature", "seed")}
            body["count"] = song["count"]
            pending["takes"] = self.studio.compose(pid, body)["id"]
            self.save()

        def done(_key: str, job: dict[str, Any]) -> None:
            partial["song_asset_ids"] = _asset_ids(job)

        self.wait_jobs("song", done, "song")
        takes = partial.get("song_asset_ids") or []
        if not takes:
            raise ProductionError("no_song", "the song job produced no audio")
        if (len(takes) > 1 and (self.state.get("settings") or {}).get("song_review")
                and not (self.state.get("review") or {}).get("song_approved")):
            # the person listens to the takes and picks one (continue with
            # take=N) before any still is cut to it
            partial["awaiting_take"] = True
            self.log("song_takes_for_review", asset_ids=takes)
            return "pause"
        partial.pop("awaiting_take", None)
        chosen = takes[min(song["take"], len(takes)) - 1]
        asset = self.store.get_asset(chosen)
        self.state["done"]["song"] = {"song_asset_ids": takes, "song_asset_id": chosen, "duration_s": asset.get("duration_s"),
                                      "take": song["take"]}
        self.log("song_takes", asset_ids=takes, chosen=chosen)

    def shot_prompt(self, shot: dict[str, Any]) -> dict[str, Any]:
        """The generate call for a shot: the lead in frame -> an edit from
        the lead's canonical reference (`consistent`); otherwise a fresh
        txt2img in the same world look with the world negative."""
        world = self.spec.get("world") or {}
        look = world.get("look") or ""
        lead = self.spec["lead"]
        body: dict[str, Any] = {"engine": self.spec.get("engine"), "seed": shot["seed"], "count": shot["variants"]}
        if shot.get("width") and shot.get("height"):
            body["width"], body["height"] = int(shot["width"]), int(shot["height"])
        else:
            body["aspect"] = shot.get("aspect") or "16:9"
        text = shot["prompt"] + (f", {look}" if look else "")
        refs = [r for r in shot.get("refs") or [] if r.get("asset_id")]
        # the shot's own references follow the lead's canonical image (which
        # is <image1> of a lead shot), each with what to take from it
        first = 2 if shot.get("lead") else 1
        notes = [f"<image{first + i}>: {r.get('use') or 'use as a reference for this shot'}" for i, r in enumerate(refs)]
        if notes:
            text += ". " + "; ".join(notes)
        # the background cast: each member's image goes in as a reference
        # and the text pins the background to exactly them, as drawn
        crowd = cast_for_shot(self.spec, shot)[:max(0, 10 - first + 1 - len(refs))]
        if crowd:
            start = first + len(refs)
            who = ", ".join(f"{m['name']} (<image{start + i}>{', ' + m['note'] if m.get('note') else ''})"
                            for i, m in enumerate(crowd))
            text += (f". In the background only these characters appear, each exactly as drawn in its image, same "
                     f"design, colours and proportions: {who}. No other creatures or characters, nobody invented: "
                     "no people, no audience, no silhouettes besides them")
            refs = refs + [{"asset_id": m["asset_id"], "use": m["name"]} for m in crowd]
        if refs:
            body["reference_asset_ids"] = [r["asset_id"] for r in refs]
        if shot.get("lead"):
            body.update({"prompt": f"@{lead['name']} {text}", "consistent": True,
                         # a lead with an adapter for this engine renders
                         # txt2img + LoRA (free poses); without one, the
                         # canonical edit as before - see spec.lead_route.
                         # References need the edit, which reads them.
                         "prefer_adapter": self.spec.get("lead_route", "auto") != "reference" and not refs})
        else:
            body.update({"prompt": text, "negative": shot.get("negative") or world.get("negative") or None})
        for key in ("strength", "sampler", "scheduler", "steps", "cfg"):
            if shot.get(key) is not None:
                body[key] = shot[key]
        return body

    def stage_frames(self) -> None:
        pid = self.project_id
        items = self.items("frames")
        pending = self.pending("frames")
        for shot in self.spec.get("shots") or []:
            key = shot["key"]
            if key in items or key in pending:
                continue
            reuse = [a for a in (shot.get("reuse_asset_ids") or []) if _asset_ok(self.store, a)]
            if reuse:
                copies = [copy_asset(self.store, a, pid)["id"] for a in reuse]
                items[key] = {"variants": copies, "best": copies[min(shot.get("best", 0), len(copies) - 1)], "reused": True}
                self.log("reused_frame", key=key, asset_ids=copies)
                continue
            pending[key] = self.studio.generate(pid, self.shot_prompt(shot))["id"]
            self.save()

        def done(key: str, job: dict[str, Any]) -> None:
            ids = _asset_ids(job)
            shot = next((s for s in self.spec["shots"] if s["key"] == key), {})
            items[key] = {"variants": ids, "best": ids[min(shot.get("best", 0), len(ids) - 1)] if ids else None,
                          "seed": shot.get("seed")}
            self.log("frame", key=key, asset_ids=ids)

        self.wait_jobs("frames", done, "frames")
        self.state["done"]["frames"]["complete"] = True

    def still_for(self, key: str) -> Optional[str]:
        """The still a shot key stands for ("3" = shot 3's best, "3v2" = its
        second variant, the runner-up)."""
        base, variant = split_key(key)
        entry = self.items("frames").get(base)
        if not entry:
            return None
        if variant == 0:
            return entry.get("best")
        others = [a for a in entry.get("variants") or [] if a != entry.get("best")]
        return others[variant - 1] if len(others) >= variant else entry.get("best")

    def stage_lyrics(self) -> None:
        song = self.spec.get("song") or {}
        song_id = self.state["done"]["song"]["song_asset_id"]
        if song.get("lrc_asset_id") and _asset_ok(self.store, song["lrc_asset_id"]):
            asset = copy_asset(self.store, song["lrc_asset_id"], self.project_id)
            lyrics = engine.read_lyrics(self.store, asset["id"])
            self.state["done"]["lyrics"] = {"lyrics_asset_id": asset["id"], "source": "imported",
                                            "sections": lyrics["sections"], "lines": len(lyrics["lines"])}
            return
        if not str(song.get("lyrics") or "").strip():
            self.state["done"]["lyrics"] = {"lyrics_asset_id": None, "source": "none"}
            return
        timed = engine.time_lyrics(self.store, self.project_id, song_id, song["lyrics"])
        self.state["done"]["lyrics"] = {"lyrics_asset_id": timed["id"], "source": "estimated", "sections": timed["sections"],
                                        "lines": timed["lines"]}
        self.log("timed_lyrics", asset_id=timed["id"], lines=timed["lines"])

    def stage_animatic(self) -> Optional[str]:
        """The cheap preview before the expensive clips: the stills cut like
        the final (see animatic.py). Pauses the production for review unless
        it was approved or `animatic_autocontinue` is on."""
        settings = self.state.get("settings") or {}
        if not settings.get("animatic", True):
            self.state["done"]["animatic"] = {"skipped": True}
            return None
        from . import animatic as animatic_mod

        entry = animatic_mod.make(self.store, self.state, None,
                                  lambda frac, msg=None: self.tick(self.stage_fraction(frac), msg or "animatic"),
                                  getattr(self.progress, "cancelled", None))
        self.state["done"]["animatic"] = entry
        self.log("animatic", renders=entry["renders"], gpu_minutes=entry["plan"]["gpu_minutes"],
                 clips_planned=entry["plan"]["clips_planned"])
        if settings.get("animatic_autocontinue") or (self.state.get("review") or {}).get("animatic_approved"):
            return None
        return "pause"

    def clip_body(self, shot: dict[str, Any], variant: int, start: Optional[str] = None) -> dict[str, Any]:
        """The generate body of one clip. `start` (the last frame of the clip
        this shot continues) replaces the still as the first frame; the still
        then becomes the frame it ends on, when the end-frame model is in."""
        body = self._clip_body(shot, variant)
        draft = (self.state.get("settings") or {}).get("clip_quality") == "draft"
        plain = body["template"] == "auto_clip" and not body.get("driving_asset_id")
        if start and body["template"] not in ("wan22_s2v", "auto_sing"):
            own = body["reference_asset_id"]
            body["reference_asset_id"] = start
            if plain and not draft and own:
                body.update({"end_asset_id": own, "end_optional": True})
            body["prompt"] += ". It continues the previous shot without a cut"
        elif start:
            body["reference_asset_id"] = start
        if draft and plain:
            body["template"] = "wan22_ti2v"  # fast drafts; promote re-renders them on the 14B model
            body.pop("end_asset_id", None)
            body.pop("end_optional", None)
        return body

    def _clip_body(self, shot: dict[str, Any], variant: int) -> dict[str, Any]:
        settings = self.spec.get("clip_settings") or {}
        key = shot_key(shot["key"], variant)
        template = settings.get("template") or "auto_clip"
        if template in ("wan22_ti2v", "auto") and not settings.get("template_pinned"):
            template = "auto_clip"  # the old default: the best clip model installed now
        text = shot.get("motion_prompt") or "subtle motion"
        if cast_for_shot(self.spec, shot):
            # the still already holds the background cast; the clip must not
            # add anybody to it
            text += (". Only the characters already in the picture, unchanged; no new characters, people or "
                     "creatures appear")
        body: dict[str, Any] = {"template": template, "reference_asset_id": self.still_for(key), "prompt": text,
                                "seed": shot["clip_seed"] + (100 * variant)}
        span = shot_span(shot)
        song_id = ((self.state.get("done") or {}).get("song") or {}).get("song_asset_id")
        if shot.get("sing"):
            # lip sync (Wan 2.2 S2V): the still's character sings exactly the
            # stretch of the song the shot is placed on
            if not span:
                raise ProductionError("sing_needs_span", f"shot {shot['key']} sings: place it on its lines in the "
                                                         "song track first (a span)")
            if not song_id:
                raise ProductionError("no_song", "a singing shot needs the song")
            lines = self.lines_in(span)
            sung = f' The character sings: "{lines}".' if lines else " The character sings."
            body.update({"template": "wan22_s2v", "audio_asset_id": song_id, "audio_start_s": span[0],
                         "audio_seconds": round(min(19.0, span[1] - span[0] + 0.25), 3),
                         "prompt": f"{shot.get('motion_prompt') or shot['prompt']}.{sung} Natural lip movement in time "
                                   "with the words, expressive face, the camera and the body move a little."})
            body.update(self.s2v_size(self.still_for(key)))
            return body
        motion = shot.get("motion_ref") or {}
        if motion.get("asset_id") and template == "auto_clip":
            # copy the motion of a video (a dance, a stunt) onto the still's
            # character; the background comes from the shot's own text
            lead = self.spec.get("lead") or {}
            look = "the character in the reference image, same design, colours and proportions"
            if not shot.get("lead"):
                look = "the characters in the reference image, same designs and colours"
            elif lead.get("name"):
                look = f"{lead['name']}, {look}"
            world = (self.spec.get("world") or {}).get("look") or ""
            crowd = cast_for_shot(self.spec, shot)
            behind = (f", in the background only {', '.join(m['name'] for m in crowd)} as in the reference image, "
                      "no other characters") if crowd else ""
            body.update({"prompt": f"Character appearance description: {look}. Background description: {shot['prompt']}"
                                   + (f", {world}" if world else "") + behind + ".",
                         "driving_asset_id": motion["asset_id"], "driving_start_s": float(motion.get("start_s") or 0),
                         "template_params": {"pose_prompt": motion.get("prompt") or shot.get("motion_prompt") or "a person dancing"}})
        char_id = ((self.state.get("done") or {}).get("character") or {}).get("character_id")
        if shot.get("lead") and char_id:
            body["characters"] = [char_id]  # a video adapter of the lead, if it has one
        negative = shot.get("clip_negative") or (settings.get("still_negative") if shot.get("motion") == "still" else None)
        if negative:
            body["negative"] = negative
        for k in ("sampler", "scheduler", "steps", "cfg"):
            if shot.get(f"clip_{k}") is not None:
                body[k] = shot[f"clip_{k}"]
        return body

    def lines_in(self, span: tuple[float, float]) -> str:
        """The sung words inside a stretch of the song (from the timed lyrics)."""
        try:
            timing = shot_timing(self.store.data_dir, self.store, self.state)
        except Exception:  # noqa: BLE001 - untimed lyrics: sing without the words in the prompt
            return ""
        words = [str(ln.get("text") or "").strip() for ln in timing.get("lines") or []
                 if span[0] - 0.05 <= float(ln.get("time_s") or 0) < span[1] - 0.05 and not str(ln.get("text") or "").startswith("[")]
        return " / ".join(w for w in words if w)[:300]

    def s2v_size(self, still_id: Optional[str]) -> dict[str, int]:
        """480p in the still's orientation (S2V 14B on a 16 GB card)."""
        try:
            a = self.store.get_asset(still_id) if still_id else {}
        except NotFound:
            a = {}
        w, h = int(a.get("width") or 16), int(a.get("height") or 9)
        if abs(w - h) < 0.1 * max(w, h):
            return {"width": 640, "height": 640}
        return {"width": 832, "height": 480} if w > h else {"width": 480, "height": 832}

    def stage_clips(self) -> None:
        """Every shot's clips. A shot that continues another (`continue_from`)
        waits for that clip and starts on its last frame; one whose source
        clip never comes falls back to its own still."""
        pid = self.project_id
        items = self.items("clips")
        pending = self.pending("clips")
        entry = self.state["done"]["clips"]
        quality = entry.setdefault("quality", {})
        chained = entry.setdefault("chained", {})
        drop_stale_chains(self.state)
        draft = (self.state.get("settings") or {}).get("clip_quality") == "draft"
        shots = {s["key"]: s for s in self.spec.get("shots") or []}
        tried: set[str] = set()

        def submit(fallback: bool) -> bool:
            sent = False
            for shot in self.spec.get("shots") or []:
                for variant in shot.get("clips") or []:
                    key = shot_key(shot["key"], variant)
                    if key in items or key in pending or key in tried:
                        continue
                    reuse = (shot.get("reuse_clips") or {}).get(key)
                    if reuse and _asset_ok(self.store, reuse):
                        items[key] = copy_asset(self.store, reuse, pid)["id"]
                        self.log("reused_clip", key=key, asset_id=items[key])
                        continue
                    start = None
                    src = shot.get("continue_from") if variant == 0 else None
                    if src and src in shots and 0 in (shots[src].get("clips") or []):
                        src_key = shot_key(src, 0)
                        if src_key in items:
                            start = engine.last_frame(self.store, items[src_key], pid)["id"]
                            chained[key] = {"from": items[src_key], "start": start}
                        elif not fallback:
                            continue  # its source clip comes first
                        else:
                            self.log("chain_fallback", key=key, source=src_key)
                    body = self.clip_body(shot, variant, start)
                    if not body["reference_asset_id"]:
                        raise ProductionError("missing_frame", f"shot {key} has no still to animate")
                    pending[key] = self.studio.generate(pid, body)["id"]
                    quality[key] = "draft" if body["template"] == "wan22_ti2v" and draft else "final"
                    if not start:
                        chained.pop(key, None)
                    tried.add(key)
                    sent = True
                    self.save()
            return sent

        def done(key: str, job: dict[str, Any]) -> None:
            ids = _asset_ids(job)
            if ids:
                items[key] = ids[0]
                self.log("clip", key=key, asset_id=ids[0])

        while True:
            sent = submit(False)
            if pending:
                self.wait_jobs("clips", done, "clips")
                continue
            if not sent and not submit(True) and not pending:
                break
            if pending:
                self.wait_jobs("clips", done, "clips")
        self.state["done"]["clips"]["complete"] = True

    def stage_photocards(self) -> None:
        cards_spec = self.spec.get("photocards") or {}
        looks = cards_spec.get("looks") or []
        if not looks:
            self.state["done"]["photocards"] = {"skipped": True}
            return
        pid = self.project_id
        lead = self.spec["lead"]
        items = self.items("photocards")
        pending = self.pending("photocards")
        for i, look in enumerate(looks, start=1):
            key = str(i)
            if key in items or key in pending:
                continue
            framing = "" if look.get("strip") or look.get("framing") is False else f", {cards_spec.get('framing') or HEADROOM}"
            body = {"prompt": f"@{lead['name']} {look['prompt']}{framing}", "consistent": True,
                    "aspect": look.get("aspect") or "2:3", "count": 1, "seed": look["seed"], "engine": self.spec.get("engine")}
            pending[key] = self.studio.generate(pid, body)["id"]
            self.save()

        def done(key: str, job: dict[str, Any]) -> None:
            ids = _asset_ids(job)
            if ids:
                items[key] = ids[0]

        self.wait_jobs("photocards", done, "photocards")
        char_id = self.state["done"]["character"]["character_id"]
        cards = [{"image_asset_id": items[str(i)], "role": look.get("role") or "", "message": look.get("message") or "",
                  "accent": look.get("accent")} for i, look in enumerate(looks, start=1) if str(i) in items]
        result = engine.photocard_set_looks(self.store, pid, char_id, [{k: v for k, v in c.items() if v} for c in cards],
                                            set_name=cards_spec.get("set_name") or self.spec.get("title"))
        entry = self.state["done"]["photocards"]
        entry.update({"complete": True, "photo_ids": [c["image_asset_id"] for c in cards], "front_ids": result["front_ids"],
                      "back_ids": result["back_ids"], "contact_sheet_id": result["contact_sheet_id"]})

    def stage_album(self) -> None:
        designs = self.spec.get("album") or []
        if not designs:
            self.state["done"]["album"] = {"skipped": True}
            return
        made = []
        for design in designs:
            fields = dict(design.get("fields") or {})
            image = design.get("image_asset_id") or (self.still_for(str(design["image_shot"])) if design.get("image_shot") else None)
            if image:
                main = "cover_image" if "cover_image" in engine.design_templates.TEMPLATE_FIELDS.get(design["template"], {}) else "image"
                fields.setdefault(main, image)
            asset = engine.render_design(self.store, self.project_id, design["template"], fields, design.get("variant"))
            made.append({"template": design["template"], "asset_id": asset["id"]})
        self.state["done"]["album"] = {"designs": made}
        self.log("album", asset_ids=[m["asset_id"] for m in made])

    def cut_pools(self, prefer_clips: bool) -> tuple[list[str], Optional[dict[str, list[str]]]]:
        """(pool asset ids, storyboard section pools) for the auto-cut:
        every best still (and clip when preferred), and each storyboard
        entry resolved to its clip or its still."""
        clips = self.items("clips") if prefer_clips else {}
        # a shot with a clip enters the general pool as its clip, not also as
        # its still (a still with a slow zoom next to the same shot moving)
        pool = [clips.get(k) or e.get("best") for k, e in self.items("frames").items() if clips.get(k) or e.get("best")]
        pool += [c for k, c in clips.items() if c and split_key(k)[0] not in self.items("frames") and c not in pool]
        board = storyboard_for(self.spec)
        pools = None
        if board:
            pools = {section: [a for a in (clips.get(k) or self.still_for(k) for k in keys) if a]
                     for section, keys in board.items()}
        return pool, pools

    def pinned(self, prefer_clips: bool) -> list[dict[str, Any]]:
        clips = self.items("clips") if prefer_clips else {}

        def assets_for(key: str) -> list[str]:
            own = [c for k, c in clips.items() if c and split_key(k)[0] == key]
            return own or [self.still_for(key)]

        return pinned_spans(self.spec, assets_for)

    def cut_options(self, prefer_clips: bool) -> dict[str, Any]:
        tl = self.spec.get("timeline") or {}
        options = dict(tl.get("options") or {})
        if prefer_clips and self.items("clips"):
            clip_settings = self.spec.get("clip_settings") or {}
            options.setdefault("video_lead_in_s", clip_settings.get("lead_in_s", 1.0))
            options.setdefault("video_rotate_offsets", clip_settings.get("rotate_offsets", True))
        return motion_pacing(self.spec, options)

    def stage_timeline(self) -> None:
        tl = self.spec.get("timeline") or {}
        lyrics_id = (self.state["done"].get("lyrics") or {}).get("lyrics_asset_id")
        song_id = self.state["done"]["song"]["song_asset_id"]
        prefer = bool(tl.get("prefer_clips", True))
        pool, pools = self.cut_pools(prefer)
        entry = self.state["done"].setdefault("timeline", {"complete": False, "timelines": {}})
        timelines = entry.setdefault("timelines", {})
        pending = self.pending("timeline")
        for aspect in tl.get("aspects") or ["9:16"]:
            info = timelines.get(aspect)
            if info is None:
                options = self.cut_options(prefer)
                free, free_pools = apply_pins(options, pool, pools, self.pinned(prefer))
                if free_pools:
                    options["section_pools"] = free_pools
                built = engine.timeline_auto(self.store, self.project_id, song_id, free, None, aspect, lyrics_id, options)
                if tl.get("finishing"):
                    engine.update_timeline(self.store, built["id"], {"finishing": tl["finishing"]})
                info = timelines[aspect] = {"timeline_id": built["id"], "renders": {}}
                self.log("timeline", aspect=aspect, timeline_id=built["id"])
                self.save()
            for quality in tl.get("qualities") or ["preview"]:
                key = f"{aspect}|{quality}"
                if quality in info["renders"] or key in pending:
                    continue
                pending[key] = self.studio.render(info["timeline_id"], quality)["id"]
                self.save()

        def done(key: str, job: dict[str, Any]) -> None:
            aspect, quality = key.split("|", 1)
            ids = _asset_ids(job)
            if ids:
                timelines[aspect]["renders"][quality] = ids[0]
                self.log("render", aspect=aspect, quality=quality, asset_id=ids[0])

        self.wait_jobs("timeline", done, "renders")
        entry["complete"] = True

    def stage_report(self) -> None:
        path = write_report(self.store, self.state)
        self.state["done"]["report"] = {"path": path.name}


def _asset_ids(job: dict[str, Any]) -> list[str]:
    outputs = job.get("outputs") or {}
    return list(outputs.get("asset_ids") or ([outputs["asset_id"]] if outputs.get("asset_id") else []))


def run_production(store: Store, studio: Studio, slug: str, progress: Callable[..., None],
                   qa_hook: Optional[Callable[[Run, str], None]] = None,
                   stage_hooks: Optional[dict[str, Callable[[Run], Any]]] = None) -> dict[str, Any]:
    if load_state(store.data_dir, slug).get("kind") == "short":
        from .shorts import ShortRun  # a narrated short (shorts.py)

        return ShortRun(store, studio, slug, progress, qa_hook=None, stage_hooks=stage_hooks).run()
    return Run(store, studio, slug, progress, qa_hook=qa_hook, stage_hooks=stage_hooks).run()


# ----------------------------------------------------------- change shots

def drop_stale_chains(state: dict[str, Any]) -> list[str]:
    """Clips that continue another one start on its last frame: when that
    clip changed (regenerated, promoted, deleted), theirs goes too, down the
    whole chain. Returns the clip keys dropped."""
    entry = (state.get("done") or {}).get("clips") or {}
    clips = entry.get("items") or {}
    chained = entry.get("chained") or {}
    shots = {s["key"]: s for s in (state.get("spec") or {}).get("shots") or []}
    dropped: list[str] = []
    changed = True
    while changed:
        changed = False
        for key, shot in shots.items():
            ck = shot_key(key, 0)
            src = shot.get("continue_from")
            link = chained.get(ck)
            if ck not in clips:
                continue
            stale = (src and link and clips.get(shot_key(src, 0)) != link.get("from")) or (not src and link) \
                or (src and not link and shot_key(src, 0) in clips)
            if stale:
                clips.pop(ck, None)
                chained.pop(ck, None)
                dropped.append(ck)
                changed = True
    return dropped


def _requeue(state: dict[str, Any], message: str) -> None:
    """After shots or clips were dropped: the stages that depend on them are
    rebuilt on the next run."""
    spec = state["spec"]
    frames = (state["done"].get("frames") or {}).get("items") or {}
    if "frames" in state["done"]:
        state["done"]["frames"]["complete"] = all(s["key"] in frames for s in spec.get("shots") or [])
    if "clips" in state["done"]:
        state["done"]["clips"]["complete"] = False
    for stage in ("animatic", "album", "timeline", "report"):
        state["done"].pop(stage, None)
    state.get("partial", {}).pop("timeline", None)
    state["review"] = {}
    state["status"] = "queued"
    state["message"] = message


def _editable(data_dir: Path, slug: str) -> dict[str, Any]:
    state = load_state(data_dir, slug)
    if is_legacy(state):
        raise ProductionError("legacy_production", "a scripted production cannot be edited here; run it from a recipe")
    if is_running(state, data_dir):
        raise ProductionError("production_running", "the production is running; wait for it to pause or finish")
    return state


def regenerate_unlocked(data_dir: Path, slug: str, stage: str = "clips", keys: Optional[list[str]] = None) -> dict[str, Any]:
    """"Redo everything I didn't approve": every shot not `locked` (or just
    `keys` among them) gets a new seed for its still (stage "frames", which
    also redoes its clips) or for its clips (stage "clips"). Approved shots
    and what they made stay. Returns the shots redone and those kept."""
    if stage not in ("frames", "clips"):
        raise ProductionError("bad_stage", "stage is 'frames' (stills and their clips) or 'clips'")
    with lock_for(slug):
        state = _editable(data_dir, slug)
        shots = state["spec"].get("shots") or []
        frames = (state["done"].get("frames") or {}).get("items") or {}
        clips = (state["done"].get("clips") or {}).get("items") or {}
        wanted = {str(k) for k in keys} if keys else None
        unknown = sorted((wanted or set()) - {s["key"] for s in shots})
        if unknown:
            raise ProductionError("bad_changes", f"unknown shot key(s) {', '.join(unknown)}")
        redone, kept = [], []
        for shot in shots:
            key = shot["key"]
            if wanted is not None and key not in wanted:
                continue
            if shot.get("locked"):
                kept.append(key)
                continue
            mine = [k for k in clips if split_key(k)[0] == key]
            if stage == "frames":
                if key not in frames and not mine:
                    continue
                shot["seed"] = int(shot["seed"]) + 1000
                frames.pop(key, None)
            elif not mine:
                continue
            shot["clip_seed"] = int(shot["clip_seed"]) + 1000
            for ck in mine:
                clips.pop(ck, None)
            redone.append(key)
        chain = drop_stale_chains(state)
        if redone or chain:
            _requeue(state, f"regenerating {stage} of shot(s) {', '.join(redone)}")
            log(state, "review", "regenerated_unlocked", what=stage, keys=redone, kept=kept, chained=chain)
        save_state(data_dir, state)
        return {"slug": slug, "stage": stage, "regenerated": redone, "kept": kept, "chained": chain,
                "status": state["status"]}


def promote_clips(data_dir: Path, slug: str, keys: Optional[list[str]] = None) -> dict[str, Any]:
    """Draft clips (fast 5B) to final: switches `clip_quality` to final and
    drops the draft clips (all, or those of the shots in `keys`) so the next
    run renders them again on the 14B model with the same seeds."""
    with lock_for(slug):
        state = _editable(data_dir, slug)
        entry = state["done"].get("clips") or {}
        clips = entry.get("items") or {}
        quality = entry.get("quality") or {}
        wanted = {str(k) for k in keys} if keys else None
        drafts = [k for k in clips if quality.get(k) == "draft" and (wanted is None or split_key(k)[0] in wanted)]
        state["settings"] = normalise_settings({**(state.get("settings") or {}), "clip_quality": "final"})
        for k in drafts:
            clips.pop(k, None)
        chain = drop_stale_chains(state)
        if drafts or chain:
            _requeue(state, f"rendering {len(drafts)} draft clip(s) as final")
        log(state, "review", "promoted_clips", keys=drafts, chained=chain)
        save_state(data_dir, state)
        return {"slug": slug, "promoted": drafts, "chained": chain, "status": state["status"]}


def update_shots(data_dir: Path, slug: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """"Change shots": per shot key, pick another variant as the best still
    (`best`: a variant index or one of its asset ids), turn its clip on or
    off (`clip`), rewrite it (`prompt`, `motion_prompt`, `seed`) or just
    `regenerate` it with a new seed. Whatever depends on a changed shot is
    invalidated (its clip, the animatic, the cut, the report) so the next
    run rebuilds exactly that. Returns what changed."""
    if not isinstance(changes, list) or not changes or len(changes) > 80:
        raise ProductionError("bad_changes", "changes must be a list of 1-80 {key, best|clip|prompt|motion_prompt|motion|"
                                             "seed|regenerate|lead|section|span|refs|crowd|cast|negative|after|delete} or {insert: {...}}")
    with lock_for(slug):
        state = load_state(data_dir, slug)
        if is_legacy(state):
            raise ProductionError("legacy_production", "a scripted production cannot be edited here; run it from a recipe")
        if state.get("kind") == "short":
            raise ProductionError("not_for_shorts", "a narrated short changes through its script (studio_production_script)")
        if is_running(state, data_dir):
            raise ProductionError("production_running", "the production is running; wait for it to pause or finish")
        spec = state["spec"]
        shots = {s["key"]: s for s in spec.get("shots") or []}
        frames = (state["done"].get("frames") or {}).get("items") or {}
        clips = (state["done"].get("clips") or {}).get("items") or {}
        changed: list[str] = []
        order = spec.setdefault("shots", [])
        board = (spec.get("timeline") or {}).get("storyboard") or {}

        def position_after(after: Any) -> int:
            if after in (None, "", "start"):
                return 0
            idx = next((i for i, s in enumerate(order) if s["key"] == str(after)), None)
            if idx is None:
                raise ProductionError("bad_changes", f"unknown shot key '{after}' to insert after")
            return idx + 1

        for change in changes:
            if not isinstance(change, dict):
                raise ProductionError("bad_changes", f"each change must be an object, got {change!r}")
            if change.get("insert") is not None:
                new = change["insert"]
                if not isinstance(new, dict) or not str(new.get("prompt") or "").strip():
                    raise ProductionError("bad_changes", "insert needs a prompt")
                if len(order) >= 80:
                    raise ProductionError("bad_changes", "a production holds at most 80 shots")
                number = max([int(k) for k in shots if k.isdigit()] or [0]) + 1
                key = str(number)
                at = position_after(new.get("after", change.get("after")))
                near = order[at - 1] if at > 0 else (order[0] if order else {})
                section = str(new.get("section") or near.get("section") or "").lower()
                shot = {"key": key, "prompt": str(new["prompt"]).strip()[:2000], "lead": bool(new.get("lead", True)),
                        "motion": "still" if new.get("motion") == "still" else "move",
                        "motion_prompt": str(new.get("motion_prompt") or "subtle motion")[:2000],
                        "seed": 3000 + 10 * number, "clip_seed": 5000 + number,
                        "variants": int(near.get("variants") or 1), "best": 0,
                        "clips": [0] if new.get("clip", new.get("motion") != "still") else [],
                        **({"section": section} if section in SHOT_SECTIONS else {}),
                        "crowd": bool(new.get("crowd", False)),
                        **({"sing": True} if new.get("sing") else {}),
                        **({"refs": _shot_refs(new.get("refs"), key)} if new.get("refs") else {}),
                        **({"motion_ref": {"asset_id": str(new["motion_ref"]["asset_id"]),
                                           "start_s": max(0.0, float(new["motion_ref"].get("start_s") or 0)),
                                           "prompt": str(new["motion_ref"].get("prompt") or "")[:300]}}
                           if isinstance(new.get("motion_ref"), dict) and new["motion_ref"].get("asset_id") else {})}
                _set_span(shot, _parse_span(new.get("span"), key))
                if near.get("width") and near.get("height"):
                    shot["width"], shot["height"] = near["width"], near["height"]
                else:
                    shot["aspect"] = near.get("aspect") or "16:9"
                order.insert(at, shot)
                shots[key] = shot
                changed.append(key)
                log(state, "review", "inserted_shot", key=key, after=new.get("after", change.get("after")))
                continue
            if str(change.get("key")) not in shots:
                raise ProductionError("bad_changes", f"unknown shot key '{change.get('key')}'")
            key = str(change["key"])
            shot = shots[key]
            if change.get("locked") is not None:
                if change["locked"]:
                    shot["locked"] = True
                else:
                    shot.pop("locked", None)
                log(state, "review", "locked_shot" if change["locked"] else "unlocked_shot", key=key)
            touches = set(change) - {"key", "locked", "section", "after"}
            if shot.get("locked") and touches:
                raise ProductionError("shot_locked", f"shot {key} is approved (locked); unlock it to change "
                                                     f"{', '.join(sorted(touches))}")
            if "continue_from" in change:
                cont = change["continue_from"]
                cont = str(cont) if cont not in (None, "") else None
                if cont is not None and (cont == key or cont not in shots):
                    raise ProductionError("bad_changes", f"shot {key}: continue_from must name another shot")
                if cont != shot.get("continue_from"):
                    if cont:
                        shot["continue_from"] = cont
                        shot["motion"] = "move"
                        if not shot.get("clips"):
                            shot["clips"] = [0]
                    else:
                        shot.pop("continue_from", None)
                    clips.pop(shot_key(key, 0), None)
            if change.get("delete"):
                if len(order) <= 1:
                    raise ProductionError("bad_changes", "a production needs at least one shot")
                order[:] = [s for s in order if s["key"] != key]
                shots.pop(key)
                frames.pop(key, None)
                for other in order:
                    if other.get("continue_from") == key:
                        other.pop("continue_from")  # it starts on its own still again
                for ck in [k for k in clips if split_key(k)[0] == key]:
                    clips.pop(ck, None)
                for section_keys in board.values():
                    section_keys[:] = [k for k in section_keys if split_key(k)[0] != key]
                changed.append(key)
                log(state, "review", "deleted_shot", key=key)
                continue
            if "after" in change:
                order[:] = [s for s in order if s["key"] != key]
                order.insert(position_after(change["after"]), shot)
                changed.append(key)
            regenerate = bool(change.get("regenerate"))
            if "span" in change:
                before = shot_span(shot)
                _set_span(shot, _parse_span(change["span"], key))
                if shot.get("sing") and shot_span(shot) != before:
                    # it sang the old stretch: a new one is a new clip
                    for ck in [k for k in clips if split_key(k)[0] == key]:
                        clips.pop(ck, None)
            if change.get("sing") is not None and bool(change["sing"]) != bool(shot.get("sing")):
                if change["sing"]:
                    shot["sing"] = True
                    shot["motion"] = "move"
                    if not shot.get("clips"):
                        shot["clips"] = [0]
                else:
                    shot.pop("sing", None)
                for ck in [k for k in clips if split_key(k)[0] == key]:
                    clips.pop(ck, None)
            if change.get("lead") is not None and bool(change["lead"]) != bool(shot.get("lead")):
                shot["lead"] = bool(change["lead"])
                regenerate = True
            if change.get("section") is not None:
                section = str(change["section"]).lower()
                if section and section not in SHOT_SECTIONS:
                    raise ProductionError("bad_changes", f"shot {key}: section must be one of {', '.join(SHOT_SECTIONS)}")
                if section:
                    shot["section"] = section
                else:
                    shot.pop("section", None)
            if change.get("refs") is not None:
                shot["refs"] = _shot_refs(change["refs"], key)
                if not shot["refs"]:
                    shot.pop("refs")
                regenerate = True
            if change.get("negative") is not None:
                shot["negative"] = str(change["negative"])[:600] or None
                regenerate = True
            if change.get("crowd") is not None and bool(change["crowd"]) != bool(shot.get("crowd")):
                shot["crowd"] = bool(change["crowd"])
                regenerate = True
            if change.get("cast") is not None:
                names = _shot_cast_names(change["cast"], key)
                known = {m["name"].lower(): m["name"] for m in spec.get("cast") or []}
                unknown = [n for n in names if n.lower() not in known]
                if unknown:
                    raise ProductionError("bad_changes", f"shot {key}: {', '.join(unknown)} not in the background cast")
                names = [known[n.lower()] for n in names]
                if names != (shot.get("cast") or []):
                    if names:
                        shot["cast"] = names
                    else:
                        shot.pop("cast", None)
                    regenerate = True
            if "motion_ref" in change:
                ref = change["motion_ref"]
                if not ref:
                    shot.pop("motion_ref", None)
                else:
                    if not isinstance(ref, dict) or not re.fullmatch(r"a_[A-Za-z0-9]{6,40}", str(ref.get("asset_id") or "")):
                        raise ProductionError("bad_changes", f"shot {key}: motion_ref is {{asset_id, start_s, prompt}}")
                    shot["motion_ref"] = {"asset_id": str(ref["asset_id"]),
                                          "start_s": max(0.0, float(ref.get("start_s") or 0)),
                                          "prompt": str(ref.get("prompt") or "").strip()[:300]}
                    if shot.get("motion") == "still":
                        shot["motion"] = "move"
                    if not shot.get("clips"):
                        shot["clips"] = [0]
                for ck in [k for k in clips if split_key(k)[0] == key]:
                    clips.pop(ck, None)
            for field in ("prompt", "motion_prompt"):
                if change.get(field) is not None:
                    if not str(change[field]).strip():
                        raise ProductionError("bad_changes", f"shot {key}: {field} cannot be empty")
                    if field == "motion_prompt" and str(change[field])[:2000] != shot.get(field):
                        # a new motion is a new clip (the still stays)
                        for ck in [k for k in clips if split_key(k)[0] == key]:
                            clips.pop(ck, None)
                    shot[field] = str(change[field])[:2000]
                    regenerate = regenerate or field == "prompt"
            if change.get("seed") is not None:
                shot["seed"] = _int(change["seed"], f"shot {key} seed", 0, 2**31 - 2)
                regenerate = True
            if change.get("motion") is not None:
                if change["motion"] not in ("still", "move"):
                    raise ProductionError("bad_changes", f"shot {key}: motion must be 'still' or 'move'")
                shot["motion"] = change["motion"]
                for ck in [k for k in clips if split_key(k)[0] == key]:
                    clips.pop(ck, None)
            if regenerate and change.get("seed") is None:
                shot["seed"] = int(shot["seed"]) + 1000
            if regenerate:
                frames.pop(key, None)
                for ck in [k for k in clips if split_key(k)[0] == key]:
                    clips.pop(ck, None)
            elif change.get("best") is not None and key in frames:
                variants = frames[key].get("variants") or []
                best = change["best"]
                pick = variants[best] if isinstance(best, int) and 0 <= best < len(variants) else best if best in variants else None
                if pick is None:
                    raise ProductionError("bad_changes", f"shot {key}: best must be a variant index or one of {variants}")
                if pick != frames[key].get("best"):
                    frames[key]["best"] = pick
                    for ck in [k for k in clips if split_key(k)[0] == key]:
                        clips.pop(ck, None)
            if change.get("clip") is not None:
                shot["clips"] = [0] if change["clip"] else []
                if not change["clip"]:
                    for ck in [k for k in clips if split_key(k)[0] == key]:
                        clips.pop(ck, None)
            changed.append(key)
            log(state, "review", "changed_shot", key=key, change={k: v for k, v in change.items() if k != "key"})
        _check_spans(spec)
        if drop_stale_chains(state) and not changed:
            changed.append("chain")
        if changed:
            if "frames" in state["done"]:
                state["done"]["frames"]["complete"] = all(s["key"] in frames for s in spec.get("shots") or [])
            if "clips" in state["done"]:
                state["done"]["clips"]["complete"] = False
            for stage in ("animatic", "album", "timeline", "report"):
                state["done"].pop(stage, None)
            state.get("partial", {}).pop("timeline", None)
            state["review"] = {}
            state["status"] = "queued"
            state["message"] = f"changed shot(s) {', '.join(changed)}"
        save_state(data_dir, state)
        changed = list(dict.fromkeys(changed))
        return {"slug": slug, "changed": changed, "status": state["status"]}


def set_song_lyrics(data_dir: Path, slug: str, lyrics: str) -> dict[str, Any]:
    """Give the production's song its lyrics (with [Verse]/[Chorus] tags):
    the lyrics stage times them to the song, the shots follow their sections
    in the cut and the karaoke shows them. Rebuilds what depends on them."""
    if not isinstance(lyrics, str) or len(lyrics) > 20000:
        raise ProductionError("bad_lyrics", "lyrics must be text (at most 20000 characters)")
    with lock_for(slug):
        state = load_state(data_dir, slug)
        if is_running(state, data_dir):
            raise ProductionError("production_running", "the production is running; wait for it to pause or finish")
        song = state["spec"].get("song")
        if not isinstance(song, dict):
            raise ProductionError("no_song", "this production has no song")
        song["lyrics"] = lyrics.strip()
        for stage in ("lyrics", "animatic", "timeline", "report"):
            state["done"].pop(stage, None)
        state.get("partial", {}).pop("timeline", None)
        state["review"] = {}
        state["status"] = "queued"
        state["message"] = "lyrics changed"
        log(state, "review", "changed_lyrics", lines=len([l for l in lyrics.splitlines() if l.strip()]))
        save_state(data_dir, state)
        return {"slug": slug, "status": state["status"]}


def update_settings(data_dir: Path, slug: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Change a production's settings (animatic review, autocontinue, song
    review, QA) between runs; a running one picks them up at its next pause."""
    if not isinstance(patch, dict):
        raise ProductionError("bad_settings", "settings must be an object")
    with lock_for(slug):
        state = load_state(data_dir, slug)
        if is_legacy(state):
            raise ProductionError("legacy_production", "a scripted production has no settings to change")
        merged = dict(state.get("settings") or {})
        for key, value in patch.items():
            if key == "qa" and isinstance(value, dict):
                merged["qa"] = {**(merged.get("qa") or {}), **value}
            else:
                merged[key] = value
        state["settings"] = normalise_settings(merged)
        if "song_review" in patch:
            state["settings"]["song_review"] = bool(patch["song_review"])
        log(state, "review", "changed_settings", **{k: v for k, v in patch.items() if k != "qa"})
        save_state(data_dir, state)
        return state["settings"]


def set_finishing(store: Store, slug: str, finishing: Any) -> dict[str, Any]:
    """The production's look: colour grade, grain, vignette, bands, lyric
    style and beat effects (see video.validate_finishing). Saved in the
    spec (the animatic and the cut read it) and on its cut timelines;
    a cut already rendered is marked to render again, which the next run
    (studio_production_continue) does without touching anything else.
    Returns {"finishing", "rerender": [aspects]}."""
    from . import video as video_mod  # pure: validation only

    try:
        clean = video_mod.validate_finishing(finishing or {})
    except video_mod.RenderError as exc:
        raise ProductionError("bad_finishing", str(exc)) from None
    with lock_for(slug):
        state = load_state(store.data_dir, slug)
        if is_legacy(state):
            raise ProductionError("legacy_production", "a scripted production has no look to change; export it as a recipe")
        if is_running(state, store.data_dir):
            raise ProductionError("busy", "the production is running; pause it first or wait until it stops")
        spec = state["spec"]
        spec.setdefault("timeline", {})["finishing"] = clean
        rerender: list[str] = []
        entry = (state.get("done") or {}).get("timeline")
        if entry:
            for aspect, info in (entry.get("timelines") or {}).items():
                try:
                    engine.update_timeline(store, info["timeline_id"], {"finishing": clean})
                except (NotFound, engine.EngineError):
                    continue
                if info.get("renders"):
                    # the old cut stays watchable until the new one lands
                    info["previous_renders"] = info["renders"]
                    info["renders"] = {}
                    rerender.append(aspect)
            if rerender:
                entry["complete"] = False
                state["done"].pop("report", None)
                if state.get("status") == "done":
                    state["status"] = "queued"
                    state["stage"] = "timeline"
        log(state, "timeline", "changed_finishing", finishing=clean, rerender=rerender or None)
        save_state(store.data_dir, state)
        return {"finishing": clean, "rerender": rerender}


SONG_COMPOSE_FIELDS = ("tags", "lyrics", "bpm", "duration", "key", "language", "time_signature", "seed", "count")


def _song_asset_lyrics(asset: dict[str, Any]) -> Optional[str]:
    params = (asset.get("recipe") or {}).get("params") or {}
    text = params.get("lyrics") or (asset.get("recipe") or {}).get("lyrics")
    return str(text).strip() if isinstance(text, str) and text.strip() else None


def set_song(store: Store, slug: str, *, asset_id: Optional[str] = None, take: Optional[int] = None,
             compose: Optional[dict[str, Any]] = None, lyrics: Optional[str] = None) -> dict[str, Any]:
    """Change a music video's song: `asset_id` (an audio asset of any project
    - from the library or just uploaded - used as is), `take` (another take
    of the ones already composed) or `compose` (new tags/bpm/duration/key/
    language/lyrics/count: composed again on the next run). The stills and
    clips stay; the lyrics timing, the animatic and the cut are redone."""
    given = [x for x in (asset_id, take, compose) if x not in (None, "", {})]
    if len(given) != 1:
        raise ProductionError("bad_song", "give exactly one of asset_id, take or compose")
    if lyrics is not None and (not isinstance(lyrics, str) or len(lyrics) > 20000):
        raise ProductionError("bad_lyrics", "lyrics must be text (at most 20000 characters)")
    data_dir = store.data_dir
    with lock_for(slug):
        state = load_state(data_dir, slug)
        if is_legacy(state):
            raise ProductionError("legacy_production", "a scripted production cannot be edited here; run it from a recipe")
        if state.get("kind") == "short":
            raise ProductionError("not_for_shorts", "a narrated short's music changes through studio_short")
        if is_running(state, data_dir):
            raise ProductionError("production_running", "the production is running; wait for it to pause or finish")
        spec = state["spec"]
        song = spec.setdefault("song", {"count": 1, "take": 1})
        done = state.setdefault("done", {})
        partial = state.setdefault("partial", {})
        review = state.get("review") or {}
        lyrics_source = "kept"
        if take is not None:
            takes = (done.get("song") or {}).get("song_asset_ids") or (partial.get("song") or {}).get("song_asset_ids") or []
            if not takes:
                raise ProductionError("no_takes", "there are no composed takes yet; compose the song first")
            n = _int(take, "take", 1, len(takes))
            chosen = takes[n - 1]
            song["take"] = n
            done["song"] = {"song_asset_ids": takes, "song_asset_id": chosen,
                            "duration_s": store.get_asset(chosen).get("duration_s"), "take": n}
            (partial.get("song") or {}).pop("awaiting_take", None)
            review = {"song_approved": True}
            what = f"take {n}"
        elif asset_id:
            asset = store.get_asset(str(asset_id))
            if asset["kind"] != "audio":
                raise ProductionError("not_audio", f"{asset_id} is {asset['kind']}, not a song (audio)")
            pid = state.get("project_id")
            use = copy_asset(store, asset["id"], pid) if pid and asset.get("project_id") != pid else asset
            song["asset_id"] = asset["id"]
            song.pop("lrc_asset_id", None)
            done["song"] = {"song_asset_ids": [use["id"]], "song_asset_id": use["id"],
                            "duration_s": use.get("duration_s") or asset.get("duration_s"), "reused": asset["id"]}
            partial.pop("song", None)
            own = _song_asset_lyrics(asset)
            if lyrics is None and own:
                song["lyrics"] = own
                lyrics_source = "song"
            elif lyrics is None:
                lyrics_source = "previous"  # the old song's words: check them
            review = {"song_approved": True}
            what = asset.get("name") or asset["id"]
        else:
            if not isinstance(compose, dict):
                raise ProductionError("bad_song", "compose is {tags, bpm, duration, key, language, lyrics, count}")
            unknown = set(compose) - set(SONG_COMPOSE_FIELDS)
            if unknown:
                raise ProductionError("bad_song", f"unknown compose field(s): {', '.join(sorted(unknown))}")
            if "tags" in compose and not str(compose.get("tags") or "").strip():
                raise ProductionError("bad_song", "tags (the style) cannot be empty")
            clean: dict[str, Any] = {}
            for k, v in compose.items():
                if v in (None, ""):
                    continue
                if k in ("bpm", "duration", "time_signature", "seed", "count"):
                    lo, hi = {"bpm": (40, 240), "duration": (10, 600), "time_signature": (2, 7),
                              "seed": (0, 2**31 - 2), "count": (1, 4)}[k]
                    clean[k] = _int(v, k, lo, hi)
                else:
                    clean[k] = str(v)[:20000 if k == "lyrics" else 600].strip()
            song.pop("asset_id", None)
            song.pop("lrc_asset_id", None)
            song.update(clean)
            if "seed" not in clean:
                song["seed"] = int(song.get("seed") or 2000) + 1
            song["take"] = 1
            song.setdefault("count", 1)
            done.pop("song", None)
            partial.pop("song", None)
            if song["count"] > 1:
                state.setdefault("settings", {})["song_review"] = True
            review = {}
            lyrics_source = "compose" if "lyrics" in clean else "kept"
            what = "compose"
        if lyrics is not None:
            song["lyrics"] = lyrics.strip()
            lyrics_source = "given"
        for stage in ("lyrics", "animatic", "timeline", "report"):
            done.pop(stage, None)
        partial.pop("timeline", None)
        state["review"] = review
        state["status"] = "queued"
        state["message"] = "song changed"
        log(state, "review", "changed_song", song=what, lyrics=lyrics_source)
        save_state(data_dir, state)
        return {"slug": slug, "status": state["status"], "song_asset_id": (done.get("song") or {}).get("song_asset_id"),
                "lyrics_source": lyrics_source, "composes_on_run": "song" not in done}


def time_lyrics_now(store: Store, slug: str) -> dict[str, Any]:
    """Time the production's lyrics to its song right away (the lyrics
    stage, without a run): the vertical lyric line in the editor needs the
    times to place shots on their words."""
    data_dir = store.data_dir
    with lock_for(slug):
        state = load_state(data_dir, slug)
        if is_running(state, data_dir):
            raise ProductionError("production_running", "the production is running; it times the lyrics itself")
        song_id = ((state.get("done") or {}).get("song") or {}).get("song_asset_id")
        if not song_id:
            raise ProductionError("no_song_yet", "the song is not there yet: pick one or let the production compose it")
        text = str(((state.get("spec") or {}).get("song") or {}).get("lyrics") or "").strip()
        if not text:
            state["done"]["lyrics"] = {"lyrics_asset_id": None, "source": "none"}
        else:
            timed = engine.time_lyrics(store, state["project_id"], song_id, text)
            state["done"]["lyrics"] = {"lyrics_asset_id": timed["id"], "source": "estimated",
                                       "sections": timed["sections"], "lines": timed["lines"]}
            log(state, "lyrics", "timed_lyrics", asset_id=timed["id"], lines=timed["lines"])
        save_state(data_dir, state)
        return {"slug": slug, "lyrics_asset_id": state["done"]["lyrics"].get("lyrics_asset_id"),
                "lines": state["done"]["lyrics"].get("lines", 0)}


def set_cast(data_dir: Path, slug: str, cast: Any, per_shot: Optional[int] = None) -> dict[str, Any]:
    """Set the production's background cast ([{asset_id, name, note}]) and
    how many of them stand behind each crowd shot. The stills of every shot
    whose crowd changed are dropped, so the next run redraws exactly those."""
    members = normalise_cast(cast)
    with lock_for(slug):
        state = load_state(data_dir, slug)
        if is_legacy(state):
            raise ProductionError("legacy_production", "a scripted production cannot be edited here; run it from a recipe")
        if is_running(state, data_dir):
            raise ProductionError("production_running", "the production is running; wait for it to pause or finish")
        spec = state["spec"]
        before = {s["key"]: [m["asset_id"] for m in cast_for_shot(spec, s)] for s in spec.get("shots") or []}
        spec["cast"] = members
        if per_shot is not None:
            spec["cast_per_shot"] = _int(per_shot, "per_shot", 1, CAST_PER_SHOT_MAX)
        known = {m["name"].lower(): m["name"] for m in members}
        for shot in spec.get("shots") or []:
            if shot.get("cast"):
                kept = [known[n.lower()] for n in shot["cast"] if n.lower() in known]
                if kept:
                    shot["cast"] = kept
                else:
                    shot.pop("cast")
        frames = (state["done"].get("frames") or {}).get("items") or {}
        clips = (state["done"].get("clips") or {}).get("items") or {}
        redraw = [s["key"] for s in spec.get("shots") or []
                  if [m["asset_id"] for m in cast_for_shot(spec, s)] != before.get(s["key"])]
        for key in redraw:
            if key in frames:
                frames.pop(key)
            for ck in [k for k in clips if split_key(k)[0] == key]:
                clips.pop(ck, None)
        if redraw:
            if "frames" in state["done"]:
                state["done"]["frames"]["complete"] = False
            if "clips" in state["done"]:
                state["done"]["clips"]["complete"] = False
            for stage in ("animatic", "album", "timeline", "report"):
                state["done"].pop(stage, None)
            state.get("partial", {}).pop("timeline", None)
            state["review"] = {}
            state["status"] = "queued"
            state["message"] = f"background cast changed: redraw shot(s) {', '.join(redraw)}"
        log(state, "review", "changed_cast", members=[m["name"] for m in members], redraw=redraw)
        save_state(data_dir, state)
        return {"slug": slug, "cast": members, "cast_per_shot": spec.get("cast_per_shot", 3), "redraw": redraw,
                "status": state["status"]}


def shot_timing(data_dir: Path, store: Any, state: dict[str, Any]) -> dict[str, Any]:
    """For the shot editor: where each shot plays in the song (from the
    animatic's cut, once there is one) and the song's sections with their
    lyrics (timed once the lyrics stage ran, else straight from the text)."""
    out: dict[str, Any] = {"shots": {}, "sections": []}
    try:
        from . import animatic as animatic_mod

        plan = animatic_mod.read_plan(data_dir, state["slug"])
        for c in plan.get("cuts") or []:
            base = split_key(c.get("shot"))[0]
            out["shots"].setdefault(base, []).append({"start_s": c.get("start_s"), "duration_s": c.get("duration_s"),
                                                       "section": c.get("section")})
        out["duration_s"] = plan.get("duration_s")
    except Exception:  # noqa: BLE001 - no animatic yet
        pass
    song = (state.get("done") or {}).get("song") or {}
    out["song_asset_id"] = song.get("song_asset_id")
    if out.get("duration_s") is None and song.get("duration_s"):
        out["duration_s"] = song["duration_s"]
    out["spans"] = {s["key"]: {"start_s": sp[0], "end_s": sp[1]}
                    for s in (state.get("spec") or {}).get("shots") or [] if (sp := shot_span(s))}
    out["timed"] = False
    lyrics_id = ((state.get("done") or {}).get("lyrics") or {}).get("lyrics_asset_id")
    if lyrics_id:
        try:
            lyr = engine.read_lyrics(store, lyrics_id)
            total = out.get("duration_s")
            sung = sorted(lyr["lines"], key=lambda l: l["time_s"])
            timed_lines = []
            for i, line in enumerate(sung):
                nxt = sung[i + 1]["time_s"] if i + 1 < len(sung) else None
                sec = next((x for x in lyr["sections"] if x["start_s"] <= line["time_s"] + 0.06
                            and (x.get("end_s") is None or line["time_s"] < x["end_s"])), None)
                ends = [v for v in (nxt, sec.get("end_s") if sec else None, total) if v is not None]
                timed_lines.append({"time_s": round(line["time_s"], 2), "end_s": round(min(ends), 2) if ends else None,
                                    "text": line["text"], "section": sec["label"] if sec else None})
            for sec in lyr["sections"]:
                end = sec.get("end_s")
                lines = [l["text"] for l in lyr["lines"]
                         if l["time_s"] >= sec["start_s"] and (end is None or l["time_s"] < end)]
                out["sections"].append({"label": sec["label"], "kind": sec.get("kind"), "start_s": sec["start_s"],
                                        "end_s": end if end is not None else total, "lines": lines})
            out["lines"] = timed_lines
            out["timed"] = True
            return out
        except Exception:  # noqa: BLE001
            pass
    text = str(((state.get("spec") or {}).get("song") or {}).get("lyrics") or "")
    current: Optional[dict[str, Any]] = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.fullmatch(r"\[([^\]]+)\]", line)
        if m:
            current = {"label": m.group(1).strip(), "kind": None, "lines": []}
            out["sections"].append(current)
        elif line:
            if current is None:
                current = {"label": "", "kind": None, "lines": []}
                out["sections"].append(current)
            current["lines"].append(line)
    return out


# ----------------------------------------------------------------- report

def _clip_text(text: Any, n: int = 90) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def write_report(store: Store, state: dict[str, Any]) -> Path:
    if state.get("kind") == "short":
        from .shorts import write_report as short_report

        return short_report(store, state)
    spec = state.get("spec") or {}
    done = state.get("done") or {}
    lead = spec.get("lead") or {}
    lines = [f"# {spec.get('title') or state.get('name')} - production report", "",
             f"Production `{state['slug']}` - project `{state.get('project_id')}` - status **{state.get('status')}**", ""]
    if state.get("recipe"):
        lines += [f"Made from the recipe **{state['recipe'].get('name')}** with the lead cast as **{lead.get('name')}**"
                  f" (reused: {', '.join(state['recipe'].get('reuse') or []) or 'nothing'}).", ""]
    lines += ["Every id is a Prospero asset id; `studio_lineage(asset_id)` gives its full recipe.", ""]
    if state.get("timings"):
        lines += ["## Timings", "", "| Stage | seconds |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in state["timings"].items()]
        lines.append("")
    char = done.get("character") or {}
    lines += ["## Lead", "", f"- **{lead.get('name')}** (`{char.get('character_id')}`), canonical reference "
              f"`{char.get('canonical_asset_id')}`", f"- Look: {_clip_text(lead.get('look'), 400)}", ""]
    song = done.get("song") or {}
    if song:
        lines += ["## Song", "", f"- Takes: {song.get('song_asset_ids')}; used `{song.get('song_asset_id')}`"
                  f"{' (reused)' if song.get('reused') else ''}", ""]
    frames = (done.get("frames") or {}).get("items") or {}
    clips = (done.get("clips") or {}).get("items") or {}
    if spec.get("shots"):
        lines += ["## Shots", "", "| Shot | lead | prompt | seed | best | clips |", "| --- | --- | --- | --- | --- | --- |"]
        for shot in spec["shots"]:
            key = shot["key"]
            shot_clips = [f"{k}: `{v}`" for k, v in clips.items() if split_key(k)[0] == key]
            lines.append(f"| {key} | {'yes' if shot.get('lead') else 'no'} | {_clip_text(shot.get('prompt'))} | {shot.get('seed')} | "
                         f"`{(frames.get(key) or {}).get('best', '-')}` | {', '.join(shot_clips) or '-'} |")
        lines.append("")
    animatic = done.get("animatic") or {}
    if animatic.get("renders"):
        plan = animatic.get("plan") or {}
        lines += ["## Animatic", "", f"- Renders: {animatic['renders']}",
                  f"- Clips planned: {plan.get('clips_planned')} - estimated GPU time {plan.get('gpu_minutes')} min", ""]
    timeline = done.get("timeline") or {}
    if timeline.get("timelines"):
        lines += ["## Cut", ""]
        for aspect, info in timeline["timelines"].items():
            lines.append(f"- {aspect}: timeline `{info.get('timeline_id')}`, renders {info.get('renders')}")
        lines.append("")
    qa = (state.get("qa") or {}).get("last")
    if qa:
        lines += ["## QA", "", f"- Last pass ({qa.get('stage')}, {qa.get('at')}): {qa.get('passed')} passed, "
                  f"{qa.get('failed')} failed, {qa.get('skipped')} skipped"]
        for item in (qa.get("items") or [])[:40]:
            if item.get("verdict") == "fail":
                lines.append(f"  - {item.get('stage')} {item.get('key')}: {'; '.join(item.get('reasons') or [])}")
        lines.append("")
    retries = [e for e in state.get("lineage", []) if e.get("event") in ("qa_retry", "qa_gave_up")]
    if retries:
        lines += ["## QA retries", ""]
        for e in retries[-40:]:
            lines.append(f"- {e.get('at')} {e.get('stage')} {e.get('key')}: {e.get('event')} - {e.get('reason')}"
                         f"{' (fix: ' + str(e.get('fix')) + ')' if e.get('fix') else ''}")
        lines.append("")
    path = production_dir(store.data_dir, state["slug"]) / "REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ------------------------------------------------------ scripted (legacy)

def _longest_common_suffix(texts: list[str]) -> str:
    if not texts:
        return ""
    rev = [t[::-1] for t in texts]
    prefix = os.path.commonprefix(rev)[::-1]
    # cut at a ", " boundary so a shot prompt never loses half a word
    idx = prefix.find(", ")
    return prefix[idx + 2:] if idx >= 0 else ""


def _recipe(store: Store, asset_id: Optional[str]) -> dict[str, Any]:
    if not asset_id:
        return {}
    try:
        return store.get_asset(asset_id).get("recipe") or {}
    except NotFound:
        return {}


def spec_from_legacy(store: Store, state: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Rebuild a production spec from a scripted production's state.json:
    the ids each step recorded, and every asset's own recipe (prompt, seed,
    size, template). Returns (spec, notes on what could not be recovered)."""
    done = state.get("done") or {}
    notes: list[str] = []
    char_id = (done.get("2") or {}).get("character_id")
    if not char_id:
        raise ProductionError("legacy_incomplete", "the scripted production has no character step (2) to take the lead from")
    char = store.get_character(char_id)
    lead = {"name": char["name"], "look": char.get("prompt") or "", "negative": char.get("negative") or "",
            "palette": char.get("palette") or [], "bio": char.get("bio") or "", "role": char.get("role") or "lead"}
    spec: dict[str, Any] = {"title": state.get("name") or state.get("slug"), "lead": lead, "engine": "auto"}
    d2 = done["2"]
    refs = d2.get("reference_asset_ids") or []
    if refs:
        r0 = _recipe(store, refs[0])
        ref_prompt = str(r0.get("prompt") or (r0.get("params") or {}).get("positive_prompt") or "")
        if lead["look"] and lead["look"] in ref_prompt:
            ref_prompt = ref_prompt.replace(lead["look"], "{look}")
        spec["reference"] = {"prompt": ref_prompt or "{look}, character turnaround reference sheet",
                             "seed": int((r0.get("params") or {}).get("seed") or 1001), "count": len(refs),
                             "pick": refs.index(d2["sheet_asset_id"]) if d2.get("sheet_asset_id") in refs else 0,
                             "aspect": "16:9", "crop": d2.get("canonical_crop") or "full"}
        engine_name = r0.get("image_engine")
        if engine_name in engine.IMAGE_ENGINES:
            spec["engine"] = engine_name
    d3 = done.get("3") or {}
    if d3.get("song_asset_id"):
        sr = _recipe(store, d3["song_asset_id"])
        params = sr.get("params") or {}
        takes = d3.get("song_asset_ids") or [d3["song_asset_id"]]
        spec["song"] = {"tags": sr.get("tags") or params.get("tags") or "", "lyrics": params.get("lyrics") or "",
                        "bpm": int(sr.get("bpm") or params.get("bpm") or 120), "key": sr.get("key") or params.get("key") or "C major",
                        "language": sr.get("language") or params.get("language") or "en",
                        "time_signature": int(str(params.get("timesignature") or 4)),
                        "duration": float(params.get("duration") or d3.get("duration_s") or 120),
                        "seed": int(_recipe(store, takes[0]).get("params", {}).get("seed") or params.get("seed") or 2001),
                        "count": min(4, len(takes)), "take": takes.index(d3["song_asset_id"]) + 1 if d3["song_asset_id"] in takes else 1,
                        "asset_id": d3["song_asset_id"]}
        if not spec["song"]["lyrics"]:
            notes.append("the song's lyrics were not in its recipe")
    stills = (done.get("4") or {}).get("stills") or {}
    shot_prompts: dict[str, str] = {}
    shots: list[dict[str, Any]] = []
    still_to_key: dict[str, str] = {}
    mention = f"@{lead['name']}"
    for key, entry in sorted(stills.items(), key=lambda kv: (int(kv[0]) if kv[0].isdigit() else 999, kv[0])):
        variants = entry.get("aspect_16_9") or ([entry["best"]] if entry.get("best") else [])
        if not variants:
            continue
        rec = _recipe(store, variants[0])
        params = rec.get("params") or {}
        prompt = str(rec.get("prompt") or params.get("positive_prompt") or "")
        is_lead = prompt.startswith(mention) or lead["name"] in (rec.get("matched_characters") or [])
        if prompt.startswith(mention):
            prompt = prompt[len(mention):].strip()
        shot_prompts[key] = prompt
        best_idx = variants.index(entry["best"]) if entry.get("best") in variants else 0
        shot = {"key": str(key), "lead": bool(is_lead), "prompt": prompt, "seed": int(params.get("seed") or 3000),
                "variants": len(variants), "best": best_idx, "clips": [], "source_asset_ids": variants}
        if params.get("width") and params.get("height"):
            shot["width"], shot["height"] = int(params["width"]), int(params["height"])
        if not is_lead and params.get("negative_prompt"):
            shot["negative"] = params["negative_prompt"]
        shots.append(shot)
        for i, aid in enumerate([entry["best"]] + [v for v in variants if v != entry.get("best")]):
            still_to_key[aid] = shot_key(key, i)
    suffix = _longest_common_suffix(list(shot_prompts.values())) if len(shot_prompts) > 1 else ""
    world_negatives = {s.get("negative") for s in shots if s.get("negative")}
    spec["world"] = {"look": suffix, "negative": world_negatives.pop() if len(world_negatives) == 1 else ""}
    for shot in shots:
        if suffix and shot["prompt"].endswith(suffix):
            shot["prompt"] = shot["prompt"][: -len(suffix)].rstrip(" ,")
        if shot.get("negative") == spec["world"]["negative"]:
            shot.pop("negative")
    clips = (done.get("5") or {}).get("clips") or {}
    by_key = {s["key"]: s for s in shots}
    source_clips: dict[str, str] = {}
    for ckey, clip_id in clips.items():
        rec = _recipe(store, clip_id)
        params = rec.get("params") or {}
        still = (rec.get("input_asset_ids") or [None])[0]
        key = still_to_key.get(still) or str(ckey)
        base, variant = split_key(key)
        shot = by_key.get(base)
        if not shot:
            continue
        shot["clips"] = sorted(set(shot["clips"]) | {variant})
        source_clips[shot_key(base, variant)] = clip_id
        if variant == 0 or "motion_prompt" not in shot:
            shot["motion_prompt"] = str(rec.get("prompt") or params.get("positive_prompt") or "subtle motion")
            shot["clip_seed"] = int(params.get("seed") or 5000) - 100 * variant
            negative = str(params.get("negative_prompt") or "")
            shot["motion"] = "still" if "walking" in negative else "move"
        if rec.get("template"):
            spec.setdefault("clip_settings", {})["template"] = rec["template"]
    for shot in shots:
        shot["source_clips"] = {k: v for k, v in source_clips.items() if split_key(k)[0] == shot["key"]}
    spec["shots"] = shots
    d6 = done.get("6") or {}
    if d6.get("photo_ids"):
        looks, framing_text = [], None
        fronts, backs = d6.get("front_ids") or [], d6.get("back_ids") or []
        for i, photo in enumerate(d6["photo_ids"]):
            rec = _recipe(store, photo)
            prompt = str(rec.get("prompt") or "")
            if prompt.startswith(mention):
                prompt = prompt[len(mention):].strip()
            look: dict[str, Any] = {}
            cut = prompt.find(", full-length portrait")
            if cut >= 0:
                framing_text = framing_text or prompt[cut + 2:]
                prompt = prompt[:cut]
            else:
                look["framing"] = False  # e.g. a photo-booth strip: no full-length framing
            front = _recipe(store, fronts[i] if i < len(fronts) else None).get("fields") or {}
            back = _recipe(store, backs[i] if i < len(backs) else None).get("fields") or {}
            look.update({"prompt": prompt, "seed": int((rec.get("params") or {}).get("seed") or 4001 + i),
                         "role": front.get("role") or "", "message": back.get("message") or "", "aspect": "2:3"})
            if front.get("accent"):
                look["accent"] = front["accent"]
            looks.append(look)
        spec["photocards"] = {"looks": looks}
        if framing_text:
            spec["photocards"]["framing"] = framing_text
    d7 = done.get("7") or {}
    album = []
    for key in ("cover_id", "tracklist_back_id", "teaser_poster_id", "lyric_card_id"):
        rec = _recipe(store, d7.get(key))
        if not rec.get("template"):
            continue
        fields = dict(rec.get("fields") or {})
        design: dict[str, Any] = {"template": rec["template"], "variant": rec.get("variant")}
        for fname in ("image", "cover_image"):
            if fields.get(fname):
                image = fields.pop(fname)
                if image in still_to_key:
                    design["image_shot"] = still_to_key[image]
                else:
                    design["image_asset_id"] = image
        design["fields"] = fields
        album.append(design)
    if album:
        spec["album"] = album
        cover = next((d for d in album if d["template"] == "album_cover" and d["fields"].get("title")), None)
        if cover:
            spec["title"] = cover["fields"]["title"]
    d8 = done.get("8") or {}
    timeline: dict[str, Any] = {"aspects": [], "qualities": [], "finishing": d8.get("finishing") or {},
                                "options": {"karaoke": True, "cut_on_lyrics": True}, "storyboard": {}}
    clip_to_key = {v: k for k, v in source_clips.items()}
    for aspect, info in (d8.get("timelines") or {}).items():
        timeline["aspects"].append(aspect)
        for q in info.get("renders") or {}:
            if q not in timeline["qualities"]:
                timeline["qualities"].append(q)
        try:
            row = store.get_timeline(info["timeline_id"])
        except NotFound:
            continue
        timeline["options"]["fps"] = row.get("fps") or 24
        if not timeline["storyboard"] and d8.get("lyrics_asset_id"):
            try:
                sections = engine.read_lyrics(store, d8["lyrics_asset_id"])["sections"]
            except (NotFound, engine.EngineError):
                sections = []
            visual = next((t for t in row["tracks"] if t["type"] == "visual"), {"clips": []})
            board: dict[str, list[str]] = {}
            for clip in visual["clips"]:
                key = clip_to_key.get(clip["asset_id"]) or still_to_key.get(clip["asset_id"])
                if not key:
                    continue
                section = next((s["label"] for s in sections if s["start_s"] <= clip["start_s"] + 0.06 <
                                (s["end_s"] if s["end_s"] is not None else float("inf"))), None)
                if section is None:
                    continue
                seq = board.setdefault(section, [])
                if key not in seq:
                    seq.append(key)
            timeline["storyboard"] = board
    if d8.get("lyrics_asset_id") and str(d8.get("lyrics_source", "")).startswith("imported") and spec.get("song"):
        spec["song"]["lrc_asset_id"] = d8["lyrics_asset_id"]
    timeline["aspects"] = timeline["aspects"] or ["9:16"]
    timeline["qualities"] = timeline["qualities"] or ["preview"]
    spec["timeline"] = timeline
    notes.append("cut density (beats per shot per section energy) is not recorded by the script; the recipe uses the "
                 "defaults with a cut on every sung line - adjust spec.timeline.options if the pace differs")
    return spec, notes


def state_from_legacy(store: Store, state: dict[str, Any]) -> dict[str, Any]:
    """A read-only, app-shaped view of a scripted production (spec rebuilt
    by `spec_from_legacy`, outputs mapped onto the app's stage names), for
    checks that only read (the QA director's dry run)."""
    spec, _ = spec_from_legacy(store, state)
    done = state.get("done") or {}
    frames = {}
    for shot in spec.get("shots") or []:
        variants = shot.get("source_asset_ids") or []
        if variants:
            best = variants[min(shot.get("best", 0), len(variants) - 1)]
            frames[shot["key"]] = {"variants": variants, "best": best}
    clips = {k: v for s in spec.get("shots") or [] for k, v in (s.get("source_clips") or {}).items()}
    d8 = done.get("8") or {}
    view_done: dict[str, Any] = {
        "character": {"character_id": (done.get("2") or {}).get("character_id"),
                      "canonical_asset_id": (done.get("2") or {}).get("canonical_asset_id")},
        "frames": {"complete": True, "items": frames},
        "clips": {"complete": True, "items": clips},
        "timeline": {"complete": True, "timelines": d8.get("timelines") or {}},
    }
    if (done.get("3") or {}).get("song_asset_id"):
        view_done["song"] = {"song_asset_id": done["3"]["song_asset_id"]}
    if d8.get("lyrics_asset_id"):
        view_done["lyrics"] = {"lyrics_asset_id": d8["lyrics_asset_id"],
                               "source": "imported" if str(d8.get("lyrics_source", "")).startswith("imported") else "estimated"}
    photos = (done.get("6") or {}).get("photo_ids") or []
    if photos:
        view_done["photocards"] = {"complete": True, "items": {str(i): p for i, p in enumerate(photos, start=1)}}
    if spec.get("song"):
        spec["song"].pop("asset_id", None)  # the plan it was composed with, not a reuse
    return {"format": FORMAT, "slug": state["slug"], "name": state.get("name") or state["slug"], "spec": spec,
            "settings": normalise_settings(None), "done": view_done, "legacy": True}
