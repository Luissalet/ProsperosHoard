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
from .util import now_iso

FORMAT = "prospero.production/1"
STAGES = ("character", "song", "frames", "lyrics", "clips", "photocards", "album", "timeline", "report")
ITEM_STAGES = ("frames", "clips", "photocards")  # checkpointed item by item
STATUSES = ("queued", "running", "awaiting_review", "done", "failed", "cancelled")
SUB_JOB_POLL_S = 0.5

DEFAULT_SETTINGS: dict[str, Any] = {
    "animatic": True,
    "animatic_autocontinue": False,
    "qa": {"enabled": False, "thresholds": {}, "max_retries": 2},
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


def save_state(data_dir: Path, state: dict[str, Any]) -> None:
    folder = production_dir(data_dir, state["slug"])
    folder.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp = folder / f"state.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / "state.json")


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
            state = load_state(data_dir, folder.name)
        except (ProductionError, NotFound):
            continue
        out.append(summary_view(state))
    out.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return out


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
    if spec["engine"] not in engine.IMAGE_ENGINES:
        raise ProductionError("bad_spec", f"spec.engine must be one of {', '.join(engine.IMAGE_ENGINES)}")
    world = spec.setdefault("world", {})
    world.setdefault("look", "")
    world.setdefault("negative", "")
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
        if not (shot.get("width") and shot.get("height")):
            shot.setdefault("aspect", "16:9")
    clip_settings = spec.setdefault("clip_settings", {})
    clip_settings.setdefault("template", "wan22_ti2v")
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
                "updated_at": state.get("updated_at"), "stages_done": sorted(done, key=lambda k: int(k) if k.isdigit() else 99)}
    return {"slug": state["slug"], "name": state.get("name"), "status": state.get("status"), "stage": state.get("stage"),
            "project_id": state.get("project_id"), "updated_at": state.get("updated_at"),
            "recipe": (state.get("recipe") or {}).get("name"), "message": state.get("message"),
            "animatic": bool((state.get("done", {}).get("animatic") or {}).get("renders"))}


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
        total = max(1, len(pending))
        failures: list[str] = []
        while pending:
            try:
                self.progress.check_cancel() if hasattr(self.progress, "check_cancel") else None
            except JobCancelled:
                for job_id in list(pending.values()):
                    try:
                        self.studio.cancel(job_id)
                    except Exception:  # noqa: BLE001 - best effort
                        pass
                raise
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
                if outcome == "pause":
                    state["status"] = "awaiting_review"
                    state["message"] = f"paused after {stage} for review"
                    self.log("paused_for_review")
                    self.save()
                    return {"slug": self.slug, "status": "awaiting_review", "stage": stage}
                if self.qa_hook and (state.get("settings", {}).get("qa") or {}).get("enabled"):
                    self.qa_hook(self, stage)
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
        pid = self.project_id
        existing = next((c for c in self.store.list_characters(pid) if c["name"].lower() == src["name"].lower()), None)
        if existing:
            return existing
        canonical = copy_asset(self.store, src["canonical_asset_id"], pid)["id"] if _asset_ok(self.store, src.get("canonical_asset_id")) else None
        char = self.store.create_character(pid, src["name"], role=src.get("role"), bio=src.get("bio"), prompt=src.get("prompt"),
                                           negative=src.get("negative"), palette=src.get("palette") or [],
                                           canonical_asset_id=canonical, voice=src.get("voice"))
        self.log("copied_character", character_id=char["id"], from_character_id=src["id"], canonical_asset_id=canonical)
        return char

    def stage_song(self) -> None:
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
        if shot.get("lead"):
            body.update({"prompt": f"@{lead['name']} {text}", "consistent": True})
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

    def clip_body(self, shot: dict[str, Any], variant: int) -> dict[str, Any]:
        settings = self.spec.get("clip_settings") or {}
        key = shot_key(shot["key"], variant)
        body: dict[str, Any] = {"template": settings.get("template") or "wan22_ti2v", "reference_asset_id": self.still_for(key),
                                "prompt": shot.get("motion_prompt") or "subtle motion",
                                "seed": shot["clip_seed"] + (100 * variant)}
        negative = shot.get("clip_negative") or (settings.get("still_negative") if shot.get("motion") == "still" else None)
        if negative:
            body["negative"] = negative
        for k in ("sampler", "scheduler", "steps", "cfg"):
            if shot.get(f"clip_{k}") is not None:
                body[k] = shot[f"clip_{k}"]
        return body

    def stage_clips(self) -> None:
        pid = self.project_id
        items = self.items("clips")
        pending = self.pending("clips")
        for shot in self.spec.get("shots") or []:
            for variant in shot.get("clips") or []:
                key = shot_key(shot["key"], variant)
                if key in items or key in pending:
                    continue
                reuse = (shot.get("reuse_clips") or {}).get(key)
                if reuse and _asset_ok(self.store, reuse):
                    items[key] = copy_asset(self.store, reuse, pid)["id"]
                    self.log("reused_clip", key=key, asset_id=items[key])
                    continue
                body = self.clip_body(shot, variant)
                if not body["reference_asset_id"]:
                    raise ProductionError("missing_frame", f"shot {key} has no still to animate")
                pending[key] = self.studio.generate(pid, body)["id"]
                self.save()

        def done(key: str, job: dict[str, Any]) -> None:
            ids = _asset_ids(job)
            if ids:
                items[key] = ids[0]
                self.log("clip", key=key, asset_id=ids[0])

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
        stills = [e.get("best") for e in self.items("frames").values() if e.get("best")]
        pool = stills + [c for c in clips.values() if c]
        board = (self.spec.get("timeline") or {}).get("storyboard") or {}
        pools = None
        if board:
            pools = {section: [a for a in (clips.get(k) or self.still_for(k) for k in keys) if a]
                     for section, keys in board.items()}
        return pool, pools

    def cut_options(self, prefer_clips: bool) -> dict[str, Any]:
        tl = self.spec.get("timeline") or {}
        options = dict(tl.get("options") or {})
        if prefer_clips and self.items("clips"):
            clip_settings = self.spec.get("clip_settings") or {}
            options.setdefault("video_lead_in_s", clip_settings.get("lead_in_s", 1.0))
            options.setdefault("video_rotate_offsets", clip_settings.get("rotate_offsets", True))
        return options

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
                if pools:
                    options["section_pools"] = pools
                built = engine.timeline_auto(self.store, self.project_id, song_id, pool, None, aspect, lyrics_id, options)
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
    return Run(store, studio, slug, progress, qa_hook=qa_hook, stage_hooks=stage_hooks).run()


# ----------------------------------------------------------- change shots

def update_shots(data_dir: Path, slug: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """"Change shots": per shot key, pick another variant as the best still
    (`best`: a variant index or one of its asset ids), turn its clip on or
    off (`clip`), rewrite it (`prompt`, `motion_prompt`, `seed`) or just
    `regenerate` it with a new seed. Whatever depends on a changed shot is
    invalidated (its clip, the animatic, the cut, the report) so the next
    run rebuilds exactly that. Returns what changed."""
    if not isinstance(changes, list) or not changes or len(changes) > 80:
        raise ProductionError("bad_changes", "changes must be a list of 1-80 {key, best|clip|prompt|motion_prompt|seed|regenerate}")
    with lock_for(slug):
        state = load_state(data_dir, slug)
        if is_legacy(state):
            raise ProductionError("legacy_production", "a scripted production cannot be edited here; run it from a recipe")
        if state.get("status") == "running":
            raise ProductionError("production_running", "the production is running; wait for it to pause or finish")
        spec = state["spec"]
        shots = {s["key"]: s for s in spec.get("shots") or []}
        frames = (state["done"].get("frames") or {}).get("items") or {}
        clips = (state["done"].get("clips") or {}).get("items") or {}
        changed: list[str] = []
        for change in changes:
            if not isinstance(change, dict) or str(change.get("key")) not in shots:
                raise ProductionError("bad_changes", f"unknown shot key '{(change or {}).get('key') if isinstance(change, dict) else change}'")
            key = str(change["key"])
            shot = shots[key]
            regenerate = bool(change.get("regenerate"))
            for field in ("prompt", "motion_prompt"):
                if change.get(field) is not None:
                    if not str(change[field]).strip():
                        raise ProductionError("bad_changes", f"shot {key}: {field} cannot be empty")
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
        return {"slug": slug, "changed": changed, "status": state["status"]}


# ----------------------------------------------------------------- report

def _clip_text(text: Any, n: int = 90) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def write_report(store: Store, state: dict[str, Any]) -> Path:
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
