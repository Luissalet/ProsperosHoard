"""Character kit: everything that keeps a character the same across shots,
engines and projects, on top of the plain `characters` row.

- **model sheet**: the canonical image re-drawn from set angles and
  expressions by the edit engine (Qwen-Image 2.1 or Flux Kontext), tagged
  per view, collected in a contact sheet;
- **dataset**: the images an adapter learns the character from (canonical,
  references, sheet views, good takes), each with a caption that names the
  trigger token and describes only what changes from image to image;
- **adapters**: LoRA files per architecture (trained here, imported, or
  carried in a `.hoardchar` pack), injected automatically whenever the
  character is mentioned and the render engine matches;
- **takes**: every generated image or clip of the character, grouped by
  shot, scored for identity, promotable to reference/dataset/canonical;
- **history**: what changed and when.

Pure logic (no FastAPI). The long-running parts (sheet renders, training)
are job handlers registered by `api.py`.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageOps

from . import comfy_driver
from . import engine
from . import identity as identity_mod
from . import trainers as trainers_mod
from .backend import Backend
from .ids import new_id
from .jobs import WaitingForResources
from .store import NotFound, Store
from .util import now_iso


class KitError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


HISTORY_CAP = 100
MAX_DATASET = 200

# The model sheet. Each view is one edit instruction; the phrasing keeps the
# framing explicit so the set covers what an adapter needs to learn: the
# whole figure from four sides, the face up close, a few expressions.
SHEET_VIEWS: dict[str, str] = {
    "front": "full body, standing straight, facing the camera, arms relaxed, plain light grey studio background",
    "three_quarter": "full body, three-quarter view turned to the left, plain light grey studio background",
    "profile": "full body in side profile facing right, plain light grey studio background",
    "back": "full body seen from behind, plain light grey studio background",
    "closeup": "close-up portrait of the face, looking straight at the camera, soft even light, plain background",
    "happy": "head and shoulders, laughing openly, plain background",
    "angry": "head and shoulders, furious expression, plain background",
    "scared": "head and shoulders, terrified wide-eyed expression, plain background",
    "surprised": "head and shoulders, surprised expression, plain background",
    "action": "full body in a dynamic action pose, mid-movement, plain background",
    "sitting": "full body, sitting on a simple stool, plain background",
    "night": "full body standing at night, lit by a single warm light from the side, dark background",
}
DEFAULT_SHEET = ["front", "three_quarter", "profile", "back", "closeup", "happy", "scared", "action"]

# engine name (engine.ENGINE_TEMPLATES) -> adapter architecture
ENGINE_ARCH = {"qwen21": "qwen_image", "flux": "flux1", "sdxl": "sdxl"}


# ------------------------------------------------------------------ kit --

def default_trigger(name: str) -> str:
    """A rare-ish token for the LoRA: the name folded to ascii letters and
    digits plus "chr" ("Farol" -> "farolchr")."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "", text)[:20] or "char"
    return f"{text}chr"


def kit_of(char: dict[str, Any]) -> dict[str, Any]:
    """The character's kit with every key present (older rows have {})."""
    kit = dict(char.get("kit") or {})
    kit.setdefault("trigger", default_trigger(char.get("name") or ""))
    kit.setdefault("use_adapters", True)
    kit.setdefault("adapters", [])
    kit.setdefault("dataset", [])
    kit.setdefault("sheet", {})
    kit.setdefault("identity", {"threshold": 6.5})
    kit.setdefault("good_seeds", [])
    kit.setdefault("history", [])
    return kit


def save_kit(store: Store, character_id: str, kit: dict[str, Any], event: Optional[str] = None,
             detail: Optional[str] = None) -> dict[str, Any]:
    if event:
        history = list(kit.get("history") or [])
        history.append({"at": now_iso(), "event": event, **({"detail": detail[:300]} if detail else {})})
        kit["history"] = history[-HISTORY_CAP:]
    return store.set_character_kit(character_id, kit)


def update_settings(store: Store, character_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """trigger / use_adapters / identity threshold / good_seeds."""
    char = store.get_character(character_id)
    kit = kit_of(char)
    changed = []
    if "trigger" in patch:
        trig = str(patch["trigger"] or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", trig):
            raise KitError("bad_trigger", "the trigger must be 3-32 letters, digits or _ (one rare word, e.g. farolchr)")
        kit["trigger"] = trig
        changed.append("trigger")
    if "use_adapters" in patch:
        kit["use_adapters"] = bool(patch["use_adapters"])
        changed.append("use_adapters")
    if "identity_threshold" in patch:
        value = float(patch["identity_threshold"])
        if not 0 <= value <= 10:
            raise KitError("bad_threshold", "identity_threshold is 0-10")
        kit["identity"] = {**(kit.get("identity") or {}), "threshold": value}
        changed.append("identity_threshold")
    if "good_seeds" in patch:
        seeds = patch["good_seeds"]
        if not isinstance(seeds, list) or not all(isinstance(s, int) for s in seeds):
            raise KitError("bad_seeds", "good_seeds must be a list of integers")
        kit["good_seeds"] = seeds[-50:]
        changed.append("good_seeds")
    return save_kit(store, character_id, kit, "settings" if changed else None, ", ".join(changed) or None)


def char_tag(character_id: str) -> str:
    return f"char:{character_id}"


def _add_tags(store: Store, asset_id: str, tags: list[str]) -> None:
    asset = store.get_asset(asset_id)
    merged = sorted(set(asset.get("tags") or []) | {t.lower() for t in tags})[:30]
    store.update_asset(asset_id, tags=merged)


def summary(store: Store, char: dict[str, Any]) -> dict[str, Any]:
    """Compact status of the kit for the agent and the UI header."""
    kit = kit_of(char)
    included = [d for d in kit["dataset"] if d.get("include", True)]
    return {
        "trigger": kit["trigger"], "use_adapters": kit["use_adapters"],
        "adapters": [adapter_view(a) for a in kit["adapters"]],
        "dataset": {"items": len(kit["dataset"]), "included": len(included)},
        "sheet": {"views": len(kit["sheet"].get("asset_ids") or []),
                  "contact_sheet_id": kit["sheet"].get("contact_sheet_id")},
        "references": len(identity_mod.reference_ids(char)),
        "identity_threshold": (kit.get("identity") or {}).get("threshold", 6.5),
        "library": kit.get("library"),
        "last_change": (kit["history"] or [{}])[-1],
    }


# ------------------------------------------------------------- adapters --

def adapter_view(a: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "arch", "lora_name", "strength", "trigger", "enabled", "installed", "source", "created_at")
    view = {k: a.get(k) for k in keys if a.get(k) is not None}
    if a.get("trained"):
        view["trained"] = {k: a["trained"].get(k) for k in ("steps", "rank", "images", "resolution", "trainer")}
    if a.get("eval"):
        view["eval"] = a["eval"]
    return view


def attach_adapter(store: Store, character_id: str, *, lora_name: str, arch: str, strength: float = 1.0,
                   trigger: Optional[str] = None, source: str = "imported", installed: bool = True,
                   file_rel: Optional[str] = None, trained: Optional[dict[str, Any]] = None,
                   notes: Optional[str] = None) -> dict[str, Any]:
    if arch not in trainers_mod.ARCH_PRESETS:
        raise KitError("bad_arch", f"arch must be one of {', '.join(trainers_mod.ARCHS)}")
    if not lora_name or len(lora_name) > 200 or ".." in lora_name:
        raise KitError("bad_lora", "lora_name is the file name ComfyUI lists (e.g. prospero/farolchr_qwen_image.safetensors)")
    if not 0 < float(strength) <= 2:
        raise KitError("bad_strength", "strength is between 0 and 2 (1 = as trained)")
    char = store.get_character(character_id)
    kit = kit_of(char)
    adapter = {"id": new_id("ad"), "arch": arch, "lora_name": lora_name.replace("\\", "/"), "strength": float(strength),
               "trigger": trigger or kit["trigger"], "enabled": True, "installed": bool(installed), "source": source,
               "created_at": now_iso(), **({"file_rel": file_rel} if file_rel else {}),
               **({"trained": trained} if trained else {}), **({"notes": notes[:300]} if notes else {})}
    # one enabled adapter per architecture: the newest wins, older ones stay (disabled) to go back to
    for other in kit["adapters"]:
        if other["arch"] == arch:
            other["enabled"] = False
    kit["adapters"].append(adapter)
    save_kit(store, character_id, kit, "adapter_added", f"{arch}: {adapter['lora_name']} ({source})")
    return adapter


def update_adapter(store: Store, character_id: str, adapter_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    char = store.get_character(character_id)
    kit = kit_of(char)
    adapter = next((a for a in kit["adapters"] if a["id"] == adapter_id), None)
    if adapter is None:
        raise KitError("unknown_adapter", f"character {char['name']} has no adapter {adapter_id}")
    if "strength" in patch:
        s = float(patch["strength"])
        if not 0 < s <= 2:
            raise KitError("bad_strength", "strength is between 0 and 2")
        adapter["strength"] = s
    if "trigger" in patch and patch["trigger"]:
        adapter["trigger"] = str(patch["trigger"])[:40]
    if "enabled" in patch:
        adapter["enabled"] = bool(patch["enabled"])
        if adapter["enabled"]:
            for other in kit["adapters"]:
                if other is not adapter and other["arch"] == adapter["arch"]:
                    other["enabled"] = False
    if "installed" in patch:
        adapter["installed"] = bool(patch["installed"])
    save_kit(store, character_id, kit, "adapter_updated", f"{adapter['lora_name']}: {', '.join(sorted(patch))}")
    return adapter


def remove_adapter(store: Store, character_id: str, adapter_id: str) -> dict[str, Any]:
    char = store.get_character(character_id)
    kit = kit_of(char)
    before = len(kit["adapters"])
    kit["adapters"] = [a for a in kit["adapters"] if a["id"] != adapter_id]
    if len(kit["adapters"]) == before:
        raise KitError("unknown_adapter", f"character {char['name']} has no adapter {adapter_id}")
    save_kit(store, character_id, kit, "adapter_removed", adapter_id)
    return {"removed": adapter_id}


def resolve_adapters(store: Store, project_id: str, matched_names: list[str], character_ids: list[str],
                     arch: Optional[str], available: Optional[list[str]]) -> dict[str, Any]:
    """The LoRAs a render should load for the characters in it.

    Characters come from the prompt's @mentions (`matched_names`) and from
    explicit ids (a production's clip of the lead). For each: its enabled
    adapter for `arch`, if the kit allows adapters. A file ComfyUI does not
    list (`available`, None = unknown -> trusted) is skipped with a note.
    Two or more character LoRAs in one frame fight each other; each is
    scaled to 0.8 then, and at most three are loaded."""
    out: dict[str, Any] = {"loras": [], "triggers": [], "used": [], "notes": []}
    if not arch:
        return out
    chars: list[dict[str, Any]] = []
    by_name = {c["name"]: c for c in store.list_characters(project_id)} if matched_names else {}
    for name in matched_names:
        if name in by_name and by_name[name] not in chars:
            chars.append(by_name[name])
    for cid in character_ids or []:
        try:
            c = store.get_character(cid)
        except NotFound:
            out["notes"].append(f"unknown character {cid}")
            continue
        if c not in chars:
            chars.append(c)
    picks = []
    for c in chars:
        kit = kit_of(c)
        if not kit["use_adapters"]:
            continue
        adapter = next((a for a in reversed(kit["adapters"]) if a.get("enabled") and a["arch"] == arch), None)
        if adapter is None:
            continue
        if available is not None and adapter["lora_name"] not in available:
            out["notes"].append(f"{c['name']}'s {arch} adapter '{adapter['lora_name']}' is not in ComfyUI's loras folder; "
                                "rendering without it")
            continue
        picks.append((c, adapter))
    if len(picks) > 3:
        out["notes"].append(f"{len(picks)} character adapters in one frame; only the first 3 are loaded")
        picks = picks[:3]
    scale = 0.8 if len(picks) > 1 else 1.0
    for c, adapter in picks:
        strength = round(float(adapter.get("strength", 1.0)) * scale, 3)
        out["loras"].append({"name": adapter["lora_name"], "strength": strength})
        trig = adapter.get("trigger") or kit_of(c)["trigger"]
        if trig and trig not in out["triggers"]:
            out["triggers"].append(trig)
        out["used"].append({"character": c["name"], "adapter_id": adapter["id"], "lora_name": adapter["lora_name"],
                            "strength": strength})
    return out


def with_triggers(prompt: str, triggers: list[str]) -> str:
    missing = [t for t in triggers if t and t.lower() not in prompt.lower()]
    return (", ".join(missing) + ", " + prompt) if missing else prompt


# --------------------------------------------------------------- sheet --

def sheet_instruction(view: str, engine_name: str) -> str:
    phrase = SHEET_VIEWS[view]
    if engine_name == "qwen21":
        return (f"Keep the character from <image1> exactly the same (face, hair, silhouette, colours, outfit, props); "
                f"redraw them {phrase}")
    return (f"the same character from the reference image, with exactly the same design, proportions, colours, outfit "
            f"and props, now {phrase}")


def check_views(views: Optional[list[str]]) -> list[str]:
    views = list(views or DEFAULT_SHEET)
    bad = [v for v in views if v not in SHEET_VIEWS]
    if bad:
        raise KitError("bad_view", f"unknown view(s) {', '.join(bad)}; choose from {', '.join(SHEET_VIEWS)}")
    if not views or len(views) > len(SHEET_VIEWS):
        raise KitError("bad_view", "pick 1-12 views")
    return list(dict.fromkeys(views))


def sheet_job(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    """Render the model sheet: one edit per view from the canonical image,
    each tagged `char:<id>`, `sheet`, `view:<name>`; then a labelled contact
    sheet; everything is added to the kit (sheet + dataset, captions from
    the view)."""
    params = job["params"]
    char = store.get_character(params["character_id"])
    canonical = char.get("canonical_asset_id")
    if not canonical:
        raise engine.EngineError("no_canonical", f"{char['name']} has no canonical image to draw the sheet from")
    object_info = engine._object_info(backend)
    engine_name = engine.resolve_image_engine(object_info, params.get("engine"))
    if engine_name == "sdxl":
        raise engine.EngineError("sheet_needs_edit_engine",
                                 "the model sheet needs an edit engine that keeps identity (Qwen-Image 2.1 or Flux "
                                 "Kontext); neither is installed in ComfyUI")
    template = engine.ENGINE_TEMPLATES[engine_name]["edit"]
    _, spec = comfy_driver.load_template(template, store.data_dir)
    views = check_views(params.get("views"))
    seed = int(params.get("seed") or engine.random_seed())
    made: list[dict[str, Any]] = []
    kit = kit_of(char)
    arch = ENGINE_ARCH.get(engine_name)
    adapters = resolve_adapters(store, char["project_id"], [], [char["id"]], arch,
                                comfy_driver.lora_choices(object_info) if object_info else None)
    for i, view in enumerate(views):
        base = i / len(views)

        def sub(fraction: float, message: Optional[str] = None, _base=base, _view=view) -> None:
            progress(0.02 + 0.9 * (_base + fraction / len(views)), f"{_view}: {message or ''}".strip(": "))

        sub.check_cancel = getattr(progress, "check_cancel", lambda: None)  # type: ignore[attr-defined]
        instruction = sheet_instruction(view, engine_name)
        if adapters["triggers"]:
            instruction = with_triggers(instruction, adapters["triggers"])
        values = engine._generation_values({"positive_prompt": instruction, "negative_prompt": "", "seed": seed + i,
                                            "reference_asset_id": canonical}, spec.get("defaults"))
        if adapters["loras"]:
            values["loras"] = adapters["loras"]
        if params.get("width") and params.get("height"):
            values["width"], values["height"] = int(params["width"]), int(params["height"])
            if template == "qwen21_edit":
                values["custom_size"] = True
        elif spec.get("size_from_reference"):
            ref = store.get_asset(canonical)
            values["width"], values["height"] = engine._size_from_reference(
                spec["size_from_reference"], ref.get("width") or 1024, ref.get("height") or 1024)
        result = engine.run_template(store, backend, job, sub, template_name=template, values=values,
                                     operation="character_sheet", reference_asset_id=canonical,
                                     reference_asset_ids=[canonical],
                                     extra_recipe={"prompt": f"@{char['name']} {SHEET_VIEWS[view]}", "view": view,
                                                   "matched_characters": [char["name"]], "image_engine": engine_name,
                                                   "character_id": char["id"]},
                                     name=f"{char['name']} · {view}")
        for aid in result["asset_ids"]:
            _add_tags(store, aid, [char_tag(char["id"]), "sheet", f"view:{view}"])
            made.append({"asset_id": aid, "view": view})
    progress(0.94, "contact sheet")
    paths = [store.data_dir / store.get_asset(m["asset_id"])["file_path"] for m in made]
    sheet_img = engine.contact_sheet(paths, cols=4, cell=320, labels=[m["view"] for m in made])
    sheet_asset = engine._save_sheet(store, char["project_id"], sheet_img,
                                     {"operation": "character_sheet", "backend": "local", "input_asset_ids": [m["asset_id"] for m in made],
                                      "character_id": char["id"], "created_at": now_iso()},
                                     f"{char['name']} · model sheet")
    _add_tags(store, sheet_asset["id"], [char_tag(char["id"]), "sheet"])
    char = store.get_character(char["id"])
    kit = kit_of(char)
    prev = [a for a in kit["sheet"].get("asset_ids") or []]
    kit["sheet"] = {"asset_ids": prev + [m["asset_id"] for m in made], "contact_sheet_id": sheet_asset["id"],
                    "views": sorted(set((kit["sheet"].get("views") or []) + views)), "job_id": job.get("id"),
                    "engine": engine_name}
    have = {d["asset_id"] for d in kit["dataset"]}
    trig = kit["trigger"]
    for m in made:
        if m["asset_id"] not in have and len(kit["dataset"]) < MAX_DATASET:
            kit["dataset"].append({"asset_id": m["asset_id"], "caption": f"{trig}, {SHEET_VIEWS[m['view']]}",
                                   "include": True, "view": m["view"], "source": "sheet"})
    save_kit(store, char["id"], kit, "sheet", f"{len(made)} views with {engine_name}")
    progress(0.99, "sheet ready")
    return {"asset_ids": [m["asset_id"] for m in made] + [sheet_asset["id"]], "contact_sheet_id": sheet_asset["id"],
            "views": [m["view"] for m in made], "engine": engine_name}


# ------------------------------------------------------------- dataset --

def _dhash(img: Image.Image) -> int:
    small = np.asarray(img.convert("L").resize((9, 8), Image.BILINEAR), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).ravel()
    return int("".join("1" if b else "0" for b in bits), 2)


def _sharpness(img: Image.Image) -> float:
    g = np.asarray(img.convert("L").resize((256, 256)), dtype=np.float32)
    lap = g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var())


def clean_caption(text: str, char: dict[str, Any], trigger: str) -> str:
    """A render prompt turned into a training caption: the @mention and the
    character's own look description are removed (the trigger stands for
    them), whitespace tidied, the trigger first."""
    t = text or ""
    look = (char.get("prompt") or "").strip()
    if look:
        t = t.replace(look, "")
    t = re.sub(rf"@{re.escape(char['name'])}\b", "", t, flags=re.IGNORECASE)
    t = re.sub(r"Keep the character from <image1>[^;]*;\s*redraw them", "", t)
    t = re.sub(r"the same character from the reference image[^,]*(,[^,]*){0,4}, now", "", t)
    t = re.sub(r"\s*,(\s*,)+", ",", t)
    t = re.sub(r"\s+", " ", t).strip(" ,")
    return f"{trigger}, {t}" if t else trigger


def dataset_view(store: Store, char: dict[str, Any]) -> dict[str, Any]:
    kit = kit_of(char)
    items = []
    for d in kit["dataset"]:
        try:
            a = store.get_asset(d["asset_id"])
        except NotFound:
            continue
        ident = identity_mod.cached_score(a, char["id"])
        items.append({**d, "width": a.get("width"), "height": a.get("height"), "thumb": a.get("thumb_path"),
                      **({"identity": ident.get("score"), "identity_method": ident.get("method")} if ident else {})})
    return {"character_id": char["id"], "trigger": kit["trigger"], "items": items, "report": dataset_report(store, char)}


def dataset_build(store: Store, character_id: str, sources: Optional[list[str]] = None,
                  min_identity: Optional[float] = None, replace: bool = False) -> dict[str, Any]:
    """Collect candidate images: `canonical`, `references`, `sheet`, `takes`
    (generated images of the character, kept only at or above
    `min_identity` when they have a cached score, and never rejected ones).
    Existing items keep their captions; new ones get one from their recipe."""
    char = store.get_character(character_id)
    kit = kit_of(char)
    sources = sources or ["canonical", "references", "sheet"]
    allowed = {"canonical", "references", "sheet", "takes"}
    if set(sources) - allowed:
        raise KitError("bad_source", f"sources are {', '.join(sorted(allowed))}")
    current = [] if replace else list(kit["dataset"])
    have = {d["asset_id"] for d in current}
    trig = kit["trigger"]

    def add(aid: str, source: str, caption: Optional[str] = None, view: Optional[str] = None) -> None:
        if aid in have or len(current) >= MAX_DATASET:
            return
        try:
            a = store.get_asset(aid)
        except NotFound:
            return
        if a["kind"] != "image" or "take:rejected" in (a.get("tags") or []):
            return
        if caption is None:
            rec = a.get("recipe") or {}
            caption = clean_caption(rec.get("prompt") or (rec.get("params") or {}).get("positive_prompt") or "", char, trig)
        current.append({"asset_id": aid, "caption": caption, "include": True, "source": source,
                        **({"view": view} if view else {})})
        have.add(aid)

    if "canonical" in sources and char.get("canonical_asset_id"):
        add(char["canonical_asset_id"], "canonical", f"{trig}, reference image")
    if "references" in sources:
        for aid in char.get("reference_asset_ids") or []:
            if "contact_sheet" in (store.get_asset(aid).get("tags") or []):
                continue
            add(aid, "reference", f"{trig}, reference image")
    if "sheet" in sources:
        for aid in kit["sheet"].get("asset_ids") or []:
            try:
                view = next((t[5:] for t in store.get_asset(aid).get("tags") or [] if t.startswith("view:")), None)
            except NotFound:
                continue
            add(aid, "sheet", f"{trig}, {SHEET_VIEWS[view]}" if view in SHEET_VIEWS else None, view)
    if "takes" in sources:
        for take in list_takes(store, character_id, limit=200)["items"]:
            if take["kind"] != "image" or take.get("rejected") or take.get("is_sheet"):
                continue
            if min_identity is not None and take.get("identity") is not None and take["identity"] < min_identity:
                continue
            add(take["asset_id"], "take")
    kit["dataset"] = current
    save_kit(store, character_id, kit, "dataset_built", f"{len(current)} items from {', '.join(sources)}")
    return dataset_view(store, store.get_character(character_id))


def dataset_update(store: Store, character_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Patch items by asset_id: caption, include, or remove=true; unknown
    asset ids that are images of the project are added."""
    char = store.get_character(character_id)
    kit = kit_of(char)
    by_id = {d["asset_id"]: d for d in kit["dataset"]}
    for patch in items or []:
        aid = patch.get("asset_id")
        if not aid:
            raise KitError("bad_item", "each dataset item needs asset_id")
        if patch.get("remove"):
            by_id.pop(aid, None)
            continue
        item = by_id.get(aid)
        if item is None:
            a = store.get_asset(aid)
            if a["kind"] != "image" or a["project_id"] != char["project_id"]:
                raise KitError("bad_item", f"{aid} is not an image of this project")
            item = {"asset_id": aid, "caption": kit["trigger"], "include": True, "source": "import"}
            by_id[aid] = item
            if len(by_id) > MAX_DATASET:
                raise KitError("dataset_full", f"a dataset holds at most {MAX_DATASET} images")
        if "caption" in patch:
            cap = re.sub(r"\s+", " ", str(patch["caption"] or "")).strip()[:600]
            item["caption"] = cap or kit["trigger"]
        if "include" in patch:
            item["include"] = bool(patch["include"])
    order = [d["asset_id"] for d in kit["dataset"]]
    kit["dataset"] = [by_id[a] for a in order if a in by_id] + [v for k, v in by_id.items() if k not in order]
    save_kit(store, character_id, kit, "dataset_edited", f"{len(items or [])} change(s)")
    return dataset_view(store, store.get_character(character_id))


def dataset_report(store: Store, char: dict[str, Any]) -> dict[str, Any]:
    """Readiness for training: counts, small images, blurry ones, near
    duplicates (dHash distance <= 4), captions without the trigger, missing
    views. `ready` is True with 8+ usable images and no blocking problem."""
    kit = kit_of(char)
    items = [d for d in kit["dataset"] if d.get("include", True)]
    warnings: list[str] = []
    small, blurry, no_trigger = [], [], []
    hashes: list[tuple[str, int]] = []
    dupes: list[list[str]] = []
    for d in items:
        try:
            a = store.get_asset(d["asset_id"])
            with Image.open(store.data_dir / a["file_path"]) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                if min(img.size) < 512:
                    small.append(d["asset_id"])
                if _sharpness(img) < 40:
                    blurry.append(d["asset_id"])
                h = _dhash(img)
        except (NotFound, OSError):
            continue
        for other, oh in hashes:
            if bin(h ^ oh).count("1") <= 4:
                dupes.append([other, d["asset_id"]])
                break
        hashes.append((d["asset_id"], h))
        if kit["trigger"].lower() not in (d.get("caption") or "").lower():
            no_trigger.append(d["asset_id"])
    n = len(items)
    if n < 8:
        warnings.append(f"only {n} images; 12-30 varied images train a steadier adapter (8 is the minimum)")
    if small:
        warnings.append(f"{len(small)} image(s) under 512 px on the short side")
    if blurry:
        warnings.append(f"{len(blurry)} image(s) look blurry")
    if dupes:
        warnings.append(f"{len(dupes)} near-duplicate pair(s): duplicates teach the pose, not the character")
    if no_trigger:
        warnings.append(f"{len(no_trigger)} caption(s) do not contain the trigger '{kit['trigger']}'")
    views = {d.get("view") for d in items if d.get("view")}
    missing = [v for v in ("front", "profile", "back", "closeup") if v not in views]
    if missing and n < 16:
        warnings.append("no sheet view for: " + ", ".join(missing) + " (render the model sheet to cover them)")
    return {"images": n, "excluded": len(kit["dataset"]) - n, "small": small, "blurry": blurry,
            "duplicates": dupes, "captions_without_trigger": no_trigger, "warnings": warnings,
            "ready": n >= 8 and not no_trigger}


def export_dataset(store: Store, char: dict[str, Any], dest: Path, resolution: int = 1024) -> dict[str, Any]:
    """Write the included items as NNN.png + NNN.txt (the layout every
    trainer reads), longest side capped at `resolution`."""
    kit = kit_of(char)
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for d in kit["dataset"]:
        if not d.get("include", True):
            continue
        try:
            a = store.get_asset(d["asset_id"])
            with Image.open(store.data_dir / a["file_path"]) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((resolution, resolution))
                written += 1
                img.save(dest / f"{written:03d}.png")
        except (NotFound, OSError):
            continue
        (dest / f"{written:03d}.txt").write_text(d.get("caption") or kit["trigger"], encoding="utf-8")
    return {"dir": dest, "images": written}


CAPTION_PROMPT = (
    "Write one training caption for this image of the character called {trigger}. Start with the word {trigger}. "
    "Describe only what can change between pictures: pose, framing, camera angle, expression, action, background, "
    "lighting, and any clothing that differs from usual. Do NOT describe the character's permanent look (face, "
    "hair, body, colours, signature outfit or props): the word {trigger} stands for that. One line, at most 40 words, "
    "no quotes."
)


def auto_caption(store: Store, character_id: str, vision: Optional[identity_mod.VisionFn],
                 only_missing: bool = False, progress: Optional[Callable[[float, str], None]] = None) -> dict[str, Any]:
    """Caption the dataset with the vision model; without one, keep/derive
    captions from recipes and say so."""
    char = store.get_character(character_id)
    kit = kit_of(char)
    trig = kit["trigger"]
    done, skipped = 0, 0
    items = [d for d in kit["dataset"] if d.get("include", True)]
    for i, d in enumerate(items):
        if only_missing and d.get("caption") and d["caption"].strip() != trig:
            skipped += 1
            continue
        if progress:
            progress(i / max(1, len(items)), f"caption {i + 1}/{len(items)}")
        if vision is None:
            a = store.get_asset(d["asset_id"])
            rec = a.get("recipe") or {}
            base = rec.get("prompt") or (rec.get("params") or {}).get("positive_prompt") or ""
            if d.get("view") in SHEET_VIEWS:
                d["caption"] = f"{trig}, {SHEET_VIEWS[d['view']]}"
            elif base:
                d["caption"] = clean_caption(base, char, trig)
            continue
        img = identity_mod.asset_image(store, d["asset_id"], side=768)
        if img is None:
            continue
        try:
            text = vision([identity_mod._jpeg(img)], CAPTION_PROMPT.format(trigger=trig))
        except Exception:  # noqa: BLE001 - keep the old caption
            continue
        text = re.sub(r"\s+", " ", (text or "").strip().strip('"')).strip()[:600]
        if not text:
            continue
        if not text.lower().startswith(trig.lower()):
            text = f"{trig}, {text}"
        d["caption"] = text
        done += 1
    save_kit(store, character_id, kit, "captions", f"{done} by vision model" if vision else "from recipes (no vision model)")
    view = dataset_view(store, store.get_character(character_id))
    view["captioned"] = done
    view["method"] = "vision" if vision else "recipes (no vision model)"
    return view


# --------------------------------------------------------------- takes --

def _take_key(asset: dict[str, Any]) -> str:
    rec = asset.get("recipe") or {}
    basis = rec.get("prompt") or (rec.get("params") or {}).get("positive_prompt") or asset.get("name") or ""
    view = rec.get("view") or ""
    return hashlib.sha1(f"{rec.get('template')}|{view}|{basis.strip().lower()}".encode("utf-8")).hexdigest()[:10]


def list_takes(store: Store, character_id: str, limit: int = 60, sort: str = "recent",
               kind: Optional[str] = None) -> dict[str, Any]:
    """Every generated image/clip of the character (tagged for it, or whose
    recipe matched its @mention), grouped into takes of the same shot."""
    char = store.get_character(character_id)
    tag = char_tag(char["id"])
    found: dict[str, dict[str, Any]] = {}
    for query_kind in ([kind] if kind else ["image", "video"]):
        offset = 0
        while offset < 600:
            page = store.list_assets(project_id=char["project_id"], kind=query_kind, query=char["name"], limit=60,
                                     offset=offset, exclude_sources=("import",))
            for a in page["items"]:
                found[a["id"]] = a
            if not page.get("has_more"):
                break
            offset = page["next_offset"]
        page = store.list_assets(project_id=char["project_id"], kind=query_kind, tag=tag, limit=60)
        for a in page["items"]:
            found[a["id"]] = a
    refs = set(identity_mod.reference_ids(char)) | set(char.get("reference_asset_ids") or [])
    kit = kit_of(char)
    in_dataset = {d["asset_id"] for d in kit["dataset"]}
    items = []
    for a in found.values():
        rec = a.get("recipe") or {}
        tags = a.get("tags") or []
        mine = tag in tags or char["name"] in (rec.get("matched_characters") or [])
        if not mine or "contact_sheet" in tags:
            continue
        ident = identity_mod.cached_score(a, char["id"])
        items.append({
            "asset_id": a["id"], "kind": a["kind"], "name": a.get("name"), "created_at": a["created_at"],
            "take_key": _take_key(a), "seed": (rec.get("params") or {}).get("seed"),
            "prompt": engine._clip(rec.get("prompt") or (rec.get("params") or {}).get("positive_prompt"), 160),
            "adapters": [l.get("name") for l in (rec.get("params") or {}).get("loras") or []],
            "identity": ident.get("score") if ident else None, "identity_method": ident.get("method") if ident else None,
            "is_reference": a["id"] in refs, "is_canonical": a["id"] == char.get("canonical_asset_id"),
            "in_dataset": a["id"] in in_dataset, "rejected": "take:rejected" in tags, "is_sheet": "sheet" in tags,
            "rating": a.get("rating") or 0, "favourite": a.get("favourite"),
        })
    items.sort(key=lambda t: t["created_at"])
    counters: dict[str, int] = {}
    groups: dict[str, int] = {}
    for t in items:
        counters[t["take_key"]] = counters.get(t["take_key"], 0) + 1
        t["take"] = counters[t["take_key"]]
        groups[t["take_key"]] = counters[t["take_key"]]
    for t in items:
        t["takes_in_shot"] = groups[t["take_key"]]
    if sort == "identity":
        items.sort(key=lambda t: (t["identity"] is None, -(t["identity"] or 0), t["created_at"]))
    else:
        items.sort(key=lambda t: t["created_at"], reverse=True)
    total = len(items)
    return {"character_id": char["id"], "name": char["name"], "total": total, "shots": len(groups),
            "items": items[:max(1, min(int(limit), 200))]}


def take_action(store: Store, character_id: str, asset_id: str, action: str) -> dict[str, Any]:
    """reference | canonical | dataset | reject | unreject | unreference."""
    char = store.get_character(character_id)
    a = store.get_asset(asset_id)
    if a["project_id"] != char["project_id"]:
        raise KitError("wrong_project", f"asset {asset_id} belongs to another project")
    if action in ("reference", "canonical", "dataset") and a["kind"] != "image":
        raise KitError("not_an_image", f"only images can be used as {action}")
    kit = kit_of(char)
    if action == "reference":
        refs = list(char.get("reference_asset_ids") or [])
        if asset_id not in refs:
            refs.append(asset_id)
        store.update_character(character_id, reference_asset_ids=refs[-12:])
    elif action == "unreference":
        store.update_character(character_id, reference_asset_ids=[r for r in char.get("reference_asset_ids") or []
                                                                  if r != asset_id])
    elif action == "canonical":
        refs = list(char.get("reference_asset_ids") or [])
        old = char.get("canonical_asset_id")
        if old and old not in refs:
            refs.append(old)
        store.update_character(character_id, canonical_asset_id=asset_id, reference_asset_ids=refs[-12:])
    elif action == "dataset":
        return dataset_update(store, character_id, [{"asset_id": asset_id, "include": True, "caption": clean_caption(
            (a.get("recipe") or {}).get("prompt") or "", char, kit["trigger"])}])
    elif action in ("reject", "unreject"):
        tags = set(a.get("tags") or [])
        (tags.add if action == "reject" else tags.discard)("take:rejected")
        store.update_asset(asset_id, tags=sorted(tags))
        if action == "reject":
            kit["dataset"] = [d for d in kit["dataset"] if d["asset_id"] != asset_id]
    else:
        raise KitError("bad_action", "take actions: reference, unreference, canonical, dataset, reject, unreject")
    save_kit(store, character_id, kit_of(store.get_character(character_id)) if action != "reject" else kit,
             f"take_{action}", asset_id)
    return {"asset_id": asset_id, "action": action}


def score_takes(store: Store, character_id: str, asset_ids: Optional[list[str]], vision: Optional[identity_mod.VisionFn],
                vision_name: Optional[str] = None, force: bool = False, limit: int = 24) -> dict[str, Any]:
    char = store.get_character(character_id)
    if not asset_ids:
        asset_ids = [t["asset_id"] for t in list_takes(store, character_id, limit=200)["items"]
                     if t["identity"] is None or force][:limit]
    results = [identity_mod.score_asset(store, char, aid, vision, vision_name, force) for aid in asset_ids[:limit]]
    threshold = (kit_of(char).get("identity") or {}).get("threshold", 6.5)
    scored = [r for r in results if r.get("score") is not None]
    return {"character_id": character_id, "threshold": threshold, "results": results,
            "below_threshold": [r["asset_id"] for r in scored if r["score"] < threshold],
            "method": "vision" if vision else "rough (no vision model)"}


# ------------------------------------------------------------ training --

def training_config(backend: Backend) -> dict[str, Any]:
    return backend.training()


def lora_dir(backend: Backend) -> Optional[Path]:
    value = training_config(backend).get("lora_dir")
    if not value:
        return None
    p = Path(str(value)).expanduser()
    return p if p.is_dir() else None


def pick_trainer(cfg: dict[str, Any], name: Optional[str], arch: str) -> dict[str, Any]:
    status = {s["name"]: s for s in trainers_mod.trainer_status(cfg)}
    entries = [t for t in cfg.get("trainers") or [] if isinstance(t, dict)]
    for t in entries:
        t.setdefault("name", t.get("kind"))
    if name:
        chosen = next((t for t in entries if t["name"] == name), None)
        if chosen is None:
            raise KitError("unknown_trainer", f"no trainer called '{name}'; configured: {', '.join(t['name'] for t in entries) or 'none'}")
        candidates = [chosen]
    else:
        candidates = entries
    for t in candidates:
        st = status.get(t["name"]) or {}
        if st.get("ok") and arch in (st.get("archs") or []):
            return t
    if not entries:
        raise KitError("no_trainer", "no LoRA trainer is configured; add one in Settings -> Training (backend.json "
                                     "'training.trainers') - see docs/CHARACTERS.md")
    reasons = "; ".join(f"{t['name']}: {(status.get(t['name']) or {}).get('reason') or 'does not train ' + arch}"
                        for t in candidates)
    raise KitError("trainer_unavailable", f"no configured trainer can train {arch} right now ({reasons})")


def _choose_gpu(cfg: dict[str, Any], need_mb: int) -> Optional[int]:
    """The GPU the trainer should see: `training.gpu` when set (an index),
    else the card with the most free memory - which must have `need_mb`."""
    from .hoard_link.gpu import gpu_free_mb

    wanted = cfg.get("gpu")
    gpus = gpu_free_mb()
    if wanted not in (None, "", "auto"):
        idx = int(wanted)
        g = next((g for g in gpus if g.index == idx), None)
        if g is not None and g.free_mb < need_mb:
            raise WaitingForResources(f"GPU {idx} has {g.free_mb} MB free; training needs about {need_mb} MB")
        return idx
    if not gpus:
        return None
    best = max(gpus, key=lambda g: g.free_mb)
    if best.free_mb < need_mb:
        raise WaitingForResources(f"the freest GPU ({best.index}) has {best.free_mb} MB free; training needs about "
                                  f"{need_mb} MB - stop other models or use Backends > Free ComfyUI memory")
    return best.index


def plan_for(store: Store, backend: Backend, character_id: str, arch: str,
             overrides: Optional[dict[str, Any]] = None, trainer_name: Optional[str] = None) -> dict[str, Any]:
    """What a training run would do, without running it."""
    char = store.get_character(character_id)
    report = dataset_report(store, char)
    try:
        plan = trainers_mod.plan_training(arch, report["images"], overrides or {})
    except trainers_mod.TrainingError as exc:
        raise KitError(exc.code, exc.message) from None
    cfg = training_config(backend)
    trainer, trainer_error = None, None
    try:
        trainer = pick_trainer(cfg, trainer_name, arch)
    except KitError as exc:
        trainer_error = exc.message
    base = (cfg.get("base_models") or {}).get(arch)
    return {"character_id": character_id, "arch": arch, "plan": plan, "dataset": report,
            "trainer": trainer.get("name") if trainer else None, "trainer_problem": trainer_error,
            "base_model": base, "base_hint": trainers_mod.ARCH_PRESETS[arch].get("base_hint"),
            "lora_dir": str(lora_dir(backend) or "") or None,
            "ready": bool(trainer) and report["ready"] and (bool(base) or (trainer or {}).get("kind") in ("fake", "custom"))}


def train_job(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    """Export the dataset, run the trainer, install the LoRA into ComfyUI's
    loras folder (`training.lora_dir`, subfolder `prospero/`) and attach it
    to the character as its adapter for `arch`."""
    params = job["params"]
    char = store.get_character(params["character_id"])
    kit = kit_of(char)
    arch = params["arch"]
    cfg = training_config(backend)
    trainer = pick_trainer(cfg, params.get("trainer"), arch)
    report = dataset_report(store, char)
    try:
        plan = trainers_mod.plan_training(arch, report["images"], params.get("overrides") or {})
    except trainers_mod.TrainingError as exc:
        raise engine.EngineError(exc.code, exc.message) from None
    base = (cfg.get("base_models") or {}).get(arch)
    gpu = None
    if trainer.get("kind") != "fake":
        gpu = _choose_gpu(cfg, int(plan.get("est_vram_mb") or 0))
    run_id = params.get("run_id") or new_id("tr")
    root = store.data_dir / "training" / run_id
    progress(0.01, "exporting dataset")
    exported = export_dataset(store, char, root / "dataset", resolution=max(1024, int(plan["resolution"])))
    if exported["images"] < 4:
        raise engine.EngineError("too_few_images", f"the dataset has {exported['images']} usable image(s); add at least 8")
    name = f"{kit['trigger']}_{arch}_{run_id[-6:].lower()}"
    env = {}
    if gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    log_path = root / "train.log"
    try:
        out = trainers_mod.run_training(
            trainer, plan, dataset_dir=root / "dataset", output_dir=root / "output", name=name, trigger=kit["trigger"],
            base_model=base, log_path=log_path, progress=lambda f, m: progress(0.03 + 0.92 * f, m),
            should_cancel=getattr(progress, "cancelled", lambda: False), env=env or None)
    except trainers_mod.TrainingError as exc:
        if exc.code == "cancelled":
            from .jobs import JobCancelled
            raise JobCancelled("cancelled") from None
        raise engine.EngineError(exc.code, exc.message) from None
    progress(0.96, "installing the adapter")
    kept = store.data_dir / "adapters" / f"{name}.safetensors"
    kept.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out, kept)
    ldir = lora_dir(backend)
    installed = False
    lora_name = f"prospero/{name}.safetensors"
    if ldir is not None:
        (ldir / "prospero").mkdir(parents=True, exist_ok=True)
        shutil.copy2(out, ldir / "prospero" / f"{name}.safetensors")
        installed = True
    adapter = attach_adapter(store, char["id"], lora_name=lora_name, arch=arch, source="trained", installed=installed,
                             file_rel=kept.relative_to(store.data_dir).as_posix(),
                             trained={"steps": plan["steps"], "rank": plan["rank"], "lr": plan["lr"],
                                      "resolution": plan["resolution"], "images": exported["images"],
                                      "job_id": job.get("id"), "trainer": trainer.get("name"), "run_id": run_id,
                                      "gpu": gpu})
    shutil.rmtree(root / "dataset", ignore_errors=True)
    progress(0.99, "adapter ready" if installed else "adapter saved (set Training -> LoRA folder to install it)")
    return {"adapter": adapter_view(adapter), "installed": installed, "run_id": run_id,
            "log": log_path.relative_to(store.data_dir).as_posix(),
            **({} if installed else {"note": "training.lora_dir is not set, so ComfyUI cannot load it yet; the file is "
                                              f"in data/{kept.relative_to(store.data_dir).as_posix()}"})}


def training_log_tail(store: Store, run_id: str, lines: int = 40) -> list[str]:
    if not re.fullmatch(r"tr_[0-9A-Za-z]{10,40}", run_id or ""):
        raise KitError("bad_run", "unknown training run")
    path = store.data_dir / "training" / run_id / "train.log"
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [l[-300:] for l in text[-max(1, min(lines, 400)):]]


def install_adapter_file(backend: Backend, src: Path, file_name: str) -> tuple[str, bool]:
    """Copy an adapter file into `lora_dir/prospero/`; returns (lora_name,
    installed)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_name)[:120]
    if not safe.endswith(".safetensors"):
        safe += ".safetensors"
    ldir = lora_dir(backend)
    if ldir is None:
        return f"prospero/{safe}", False
    (ldir / "prospero").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, ldir / "prospero" / safe)
    return f"prospero/{safe}", True


__all__ = [n for n in dir() if not n.startswith("_") and n not in ("annotations", "sys")]
