"""Production recipes: a finished production turned into a reusable
template with a casting slot.

`export_recipe` reads a production (`data/productions/<slug>/state.json`,
made in the app or by the production script) and writes
`data/recipes/<name>.json`: every stage, prompt, seed, template and setting
of the production, with the **lead character abstracted into a `{lead}`
slot**. The lead's name becomes `{lead}`, its look `{lead.look}`, its
negative `{lead.negative}`, its palette colours `{lead.palette[i]}`, its bio
`{lead.bio}`, and the production's title `{title}`; a `cast` block says
what a slot needs. `run_recipe` fills the slot (a character from the store,
or an inline description) and creates a new production from it, reusing
what does not depend on the lead: the song (unless its lyrics name the
lead), and the stills and clips of the shots the lead is not in.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from . import productions as prod
from .store import NotFound, Store
from .util import now_iso

FORMAT = "prospero.recipe/1"
REUSABLE = ("song", "frames", "clips")
_SKIP_KEYS = {"key", "template", "variant", "engine", "aspect", "crop", "motion", "image_shot", "language", "key_signature"}
_STOPWORDS = {"about", "above", "after", "along", "always", "being", "below", "every", "front", "light", "never", "other",
              "shown", "still", "their", "there", "these", "those", "under", "where", "which", "while", "with", "without",
              "little", "closer", "cinematic", "shallow", "depth", "field", "practical", "palette", "quiet", "dread",
              "meters", "tall", "thin", "very", "made", "long", "like", "dark", "grey", "gray", "night", "urban"}


class RecipeError(prod.ProductionError):
    pass


def recipes_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "recipes"


def _recipe_path(data_dir: Path, name: str) -> Path:
    slug = prod.slugify(name, fallback="")
    if not slug:
        raise RecipeError("bad_recipe_name", "a recipe needs a name made of letters or digits")
    return recipes_dir(data_dir) / f"{slug}.json"


# ------------------------------------------------------------- abstraction

def _walk(value: Any, fn, key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        return {k: _walk(v, fn, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, fn, key) for v in value]
    if isinstance(value, str):
        return fn(value, key)
    return value


def _is_id_key(key: Optional[str]) -> bool:
    return bool(key) and (key in _SKIP_KEYS or key.endswith("_id") or key.endswith("_ids") or key.startswith("source_")
                          or key.startswith("reuse"))


def _name_pattern(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")


def _look_words(look: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{5,}", look.lower()) if w not in _STOPWORDS}


def abstract_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """(spec with placeholders, warnings, placeholder legend)."""
    lead = spec.get("lead") or {}
    name = str(lead.get("name") or "").strip()
    look = str(lead.get("look") or "").strip()
    negative = str(lead.get("negative") or "").strip()
    bio = str(lead.get("bio") or "").strip()
    palette = [str(c) for c in lead.get("palette") or []]
    title = str(spec.get("title") or "").strip()
    name_re = _name_pattern(name) if name else None

    def replace(text: str, key: Optional[str]) -> str:
        if _is_id_key(key):
            return text
        for i, colour in enumerate(palette):
            if text.strip().lower() == colour.lower():
                return f"{{lead.palette[{i}]}}"
        if look and look in text:
            text = text.replace(look, "{lead.look}")
        if negative and key in ("negative", "negative_prompt") and text.strip() == negative:
            text = "{lead.negative}"
        if bio and len(bio) > 12 and bio in text:
            text = text.replace(bio, "{lead.bio}")
        if title and len(title) >= 4 and title in text:
            text = text.replace(title, "{title}")
        if name_re is not None:
            text = name_re.sub("{lead}", text)
        return text

    body = {k: v for k, v in spec.items() if k not in ("lead", "world")}
    out = _walk(body, replace)
    world = dict(spec.get("world") or {})
    # the world look is the setting, not the lead; only the lead's own name
    # and look text inside it become placeholders
    out["world"] = {k: (replace(v, None) if k == "look" and isinstance(v, str) else v) for k, v in world.items()}
    out["lead"] = {"name": "{lead}", "look": "{lead.look}", "negative": "{lead.negative}",
                   "palette": [f"{{lead.palette[{i}]}}" for i in range(len(palette))], "bio": "{lead.bio}",
                   "role": lead.get("role") or "lead"}
    if "reference" in out and isinstance(out["reference"].get("prompt"), str):
        out["reference"]["prompt"] = out["reference"]["prompt"].replace("{lead.look}", "{look}")
    out["title"] = "{title}"

    warnings: list[str] = []
    words = _look_words(look)
    if words:
        def leaks(text: str) -> list[str]:
            found = {w for w in re.findall(r"[a-z]{5,}", text.lower()) if w in words}
            return sorted(found)

        for shot in out.get("shots") or []:
            for field in ("prompt", "motion_prompt"):
                hits = leaks(str(shot.get(field) or "")) if shot.get("lead") or field == "prompt" else []
                if hits:
                    warnings.append(f"shot {shot['key']} {field} repeats words from the lead's look ({', '.join(hits)}): "
                                    "rewrite it for a different lead, or keep it if those are props of the scene")
        for i, look_spec in enumerate((out.get("photocards") or {}).get("looks") or [], start=1):
            hits = leaks(str(look_spec.get("prompt") or ""))
            if hits:
                warnings.append(f"photocard look {i} repeats words from the lead's look ({', '.join(hits)})")
        framing = (out.get("photocards") or {}).get("framing")
        if isinstance(framing, str) and leaks(framing):
            warnings.append(f"the photocard framing mentions {', '.join(leaks(framing))}; it is kept as written")
    legend = {"{lead}": "the lead's name (also used as @{lead} in prompts)", "{lead.look}": "the lead's look description",
              "{lead.negative}": "what to avoid for the lead", "{lead.bio}": "a one-line bio",
              "{title}": "the song / production title"}
    for i in range(len(palette)):
        legend[f"{{lead.palette[{i}]}}"] = f"colour {i + 1} of the lead's palette"
    return out, warnings, legend


def _cast_block(spec: dict[str, Any], abstract: dict[str, Any]) -> dict[str, Any]:
    lead = spec.get("lead") or {}
    lead_shots = [s["key"] for s in abstract.get("shots") or [] if s.get("lead")]
    used_in = ["reference sheet" if abstract.get("reference") else None,
              f"shots {', '.join(lead_shots)}" if lead_shots else None,
              "photocards" if (abstract.get("photocards") or {}).get("looks") else None,
              "album designs" if any("{lead" in json.dumps(d) for d in abstract.get("album") or []) else None,
              "lyrics" if "{lead}" in str((abstract.get("song") or {}).get("lyrics") or "") else None]
    return {"lead": {
        "slot": "{lead}",
        "needs": {"name": "unique within the project; prompts mention it as @name",
                  "look": "one paragraph: silhouette, face/head, colours, key props, texture - what every shot keeps",
                  "negative": "optional: what to avoid", "palette": f"optional: up to {max(4, len(lead.get('palette') or []))} #hex colours",
                  "bio": "optional: one line"},
        "fill_with": "a character id from the store (studio_cast list), or {name, look, negative?, palette?, bio?}",
        "used_in": [u for u in used_in if u],
        "example": {"name": lead.get("name"), "look": lead.get("look"), "palette": lead.get("palette") or [],
                    "negative": lead.get("negative") or ""},
    }}


# ------------------------------------------------------------------ export

def _spec_of(store: Store, state: dict[str, Any]) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """(concrete spec, notes, source ids of reusable assets)."""
    if prod.is_legacy(state):
        spec, notes = prod.spec_from_legacy(store, state)
        source = {"song": (spec.get("song") or {}).get("asset_id"),
                  "lrc": (spec.get("song") or {}).get("lrc_asset_id"),
                  "frames": {s["key"]: s.get("source_asset_ids") or [] for s in spec.get("shots") or []},
                  "clips": {k: v for s in spec.get("shots") or [] for k, v in (s.get("source_clips") or {}).items()},
                  "project_id": (state.get("done", {}).get("1") or {}).get("project_id")}
        return spec, notes, source
    if state.get("status") not in ("done", "awaiting_review") and not state.get("done", {}).get("frames"):
        raise RecipeError("production_not_finished", f"production '{state['slug']}' has no finished frames yet")
    spec = json.loads(json.dumps(state["spec"]))
    done = state.get("done") or {}
    frames = (done.get("frames") or {}).get("items") or {}
    clips = (done.get("clips") or {}).get("items") or {}
    source = {"song": (done.get("song") or {}).get("song_asset_id"),
              "lrc": (done.get("lyrics") or {}).get("lyrics_asset_id") if (done.get("lyrics") or {}).get("source") == "imported" else None,
              "frames": {k: [e.get("best")] + [v for v in e.get("variants") or [] if v != e.get("best")] for k, e in frames.items()},
              "clips": dict(clips), "project_id": state.get("project_id")}
    # the lead as it was actually used (an existing character's own look)
    char_id = (done.get("character") or {}).get("character_id")
    if char_id:
        try:
            char = store.get_character(char_id)
            spec["lead"] = {**spec["lead"], "name": char["name"], "look": spec["lead"].get("look") or char.get("prompt") or "",
                            "negative": spec["lead"].get("negative") or char.get("negative") or "",
                            "palette": spec["lead"].get("palette") or char.get("palette") or [],
                            "bio": spec["lead"].get("bio") or char.get("bio") or ""}
        except NotFound:
            pass
    for shot in spec.get("shots") or []:
        for k in ("reuse_asset_ids", "reuse_clips"):
            shot.pop(k, None)
        # the frames are in the order variants were made, best first
        shot["best"] = 0
    for k in ("asset_id", "lrc_asset_id"):
        (spec.get("song") or {}).pop(k, None)
    spec["lead"].pop("character_id", None)
    return spec, [], source


def export_recipe(store: Store, slug: str, name: Optional[str] = None) -> dict[str, Any]:
    state = prod.load_state(store.data_dir, slug)
    if state.get("kind") == "short":
        raise prod.ProductionError("not_for_shorts", "recipes are for music videos; make variants of a short with "
                                                     "studio_short_create(count=...)")
    spec, notes, source = _spec_of(store, state)
    name = name or (spec.get("title") or slug)
    path = _recipe_path(store.data_dir, name)
    lead = spec.get("lead") or {}
    abstract, warnings, legend = abstract_spec(spec)
    for shot in abstract.get("shots") or []:
        for k in ("source_asset_ids", "source_clips", "reuse_asset_ids", "reuse_clips"):
            shot.pop(k, None)
    for k in ("asset_id", "lrc_asset_id"):
        (abstract.get("song") or {}).pop(k, None)
    lead_keys = {s["key"] for s in spec.get("shots") or [] if s.get("lead")}
    lyrics = str((spec.get("song") or {}).get("lyrics") or "")
    mentions = bool(lead.get("name")) and bool(_name_pattern(str(lead["name"])).search(lyrics))
    reusable = {
        "song": {"asset_id": source.get("song"), "lrc_asset_id": source.get("lrc"), "mentions_lead": mentions}
        if source.get("song") else None,
        "frames": {k: [a for a in v if a] for k, v in (source.get("frames") or {}).items() if k not in lead_keys and v},
        "clips": {k: v for k, v in (source.get("clips") or {}).items() if prod.split_key(k)[0] not in lead_keys and v},
        "project_id": source.get("project_id"),
    }
    recipe = {
        "format": FORMAT, "name": path.stem, "title": spec.get("title"), "created_at": now_iso(),
        "source": {"production": slug, "legacy": prod.is_legacy(state), "lead": lead.get("name"),
                   "project_id": source.get("project_id")},
        "cast": _cast_block(spec, abstract), "placeholders": legend, "spec": abstract,
        "settings": prod.normalise_settings(state.get("settings")) if not prod.is_legacy(state) else prod.normalise_settings(None),
        "reusable": reusable, "warnings": warnings, "notes": notes,
        "stages": list(prod.STAGES),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return recipe


def recipe_summary(recipe: dict[str, Any]) -> dict[str, Any]:
    spec = recipe.get("spec") or {}
    shots = spec.get("shots") or []
    reusable = recipe.get("reusable") or {}
    return {"name": recipe.get("name"), "title": recipe.get("title"), "created_at": recipe.get("created_at"),
            "from_production": (recipe.get("source") or {}).get("production"),
            "original_lead": (recipe.get("source") or {}).get("lead"),
            "shots": len(shots), "lead_shots": sum(1 for s in shots if s.get("lead")),
            "clips": sum(len(s.get("clips") or []) for s in shots),
            "reusable": {"song": bool(reusable.get("song")) and not (reusable.get("song") or {}).get("mentions_lead"),
                         "frames": len(reusable.get("frames") or {}), "clips": len(reusable.get("clips") or {})},
            "warnings": len(recipe.get("warnings") or [])}


def list_recipes(data_dir: Path) -> list[dict[str, Any]]:
    root = recipes_dir(data_dir)
    out = []
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            try:
                recipe = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if recipe.get("format") == FORMAT:
                out.append(recipe_summary(recipe))
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def get_recipe(data_dir: Path, name: str) -> dict[str, Any]:
    path = _recipe_path(data_dir, name)
    if not path.is_file():
        raise NotFound("recipe", name)
    try:
        recipe = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecipeError("bad_recipe", f"recipe '{name}' is unreadable: {exc}") from None
    if recipe.get("format") != FORMAT:
        raise RecipeError("bad_recipe", f"'{name}' is not a production recipe")
    return recipe


# --------------------------------------------------------------------- run

def resolve_lead(store: Store, value: Any) -> dict[str, Any]:
    """A cast value -> a concrete lead: a character id (any project) or an
    inline {name, look, negative?, palette?, bio?} (a bare string is a look
    for a lead named "Lead")."""
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if re.fullmatch(r"char_[A-Za-z0-9]+", text):
            char = store.get_character(text)
            return {"character_id": char["id"], "name": char["name"], "look": char.get("prompt") or char["name"],
                    "negative": char.get("negative") or "", "palette": char.get("palette") or [], "bio": char.get("bio") or "",
                    "role": char.get("role") or "lead"}
        return {"name": "Lead", "look": text, "negative": "", "palette": [], "bio": ""}
    if isinstance(value, dict):
        if value.get("character_id") or value.get("id"):
            return resolve_lead(store, str(value.get("character_id") or value.get("id")))
        name = str(value.get("name") or "").strip()
        look = str(value.get("look") or value.get("prompt") or "").strip()
        if not name or not look:
            raise RecipeError("bad_cast", "an inline lead needs a name and a look (or pass a character id)")
        palette = value.get("palette") or []
        if not isinstance(palette, list) or not all(isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", c) for c in palette):
            raise RecipeError("bad_cast", "palette must be a list of #RRGGBB colours")
        return {"name": name[:80], "look": look[:4000], "negative": str(value.get("negative") or ""), "palette": palette,
                "bio": str(value.get("bio") or ""), "role": str(value.get("role") or "lead")}
    raise RecipeError("bad_cast", 'cast must be {"lead": <character id or {name, look, ...}>}')


def fill(template: Any, lead: dict[str, Any], title: str) -> Any:
    palette = list(lead.get("palette") or [])

    def colour(i: int) -> str:
        if i < len(palette):
            return palette[i]
        return palette[0] if palette else "#FF4D8D"

    def sub(text: str, _key: Optional[str]) -> str:
        text = re.sub(r"\{lead\.palette\[(\d+)\]\}", lambda m: colour(int(m.group(1))), text)
        return (text.replace("{lead.look}", lead.get("look") or "").replace("{lead.negative}", lead.get("negative") or "")
                .replace("{lead.bio}", lead.get("bio") or "").replace("{lead}", lead.get("name") or "")
                .replace("{title}", title))

    return _walk(template, sub)


def plan_run(store: Store, recipe: dict[str, Any], cast: Any, options: Optional[dict[str, Any]] = None
             ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(spec, settings, recipe meta) for a new production from a recipe."""
    options = dict(options or {})
    if not isinstance(cast, dict) or "lead" not in cast:
        raise RecipeError("bad_cast", 'cast must be {"lead": <character id or {name, look, ...}>}')
    lead = resolve_lead(store, cast["lead"])
    reuse = options.get("reuse", list(REUSABLE))
    if isinstance(reuse, str):
        reuse = [r.strip() for r in reuse.split(",") if r.strip()]
    if not isinstance(reuse, list) or any(r not in REUSABLE for r in reuse):
        raise RecipeError("bad_options", f"reuse must list any of {', '.join(REUSABLE)}")
    title = str(options.get("title") or recipe.get("title") or "Production")
    template = json.loads(json.dumps(recipe["spec"]))
    lead_template = template.pop("lead", {})
    spec = fill(template, lead, title)
    spec["title"] = title
    spec["lead"] = {**{k: v for k, v in lead.items()}, "role": lead.get("role") or lead_template.get("role") or "lead"}
    notes: list[str] = []
    reusable = recipe.get("reusable") or {}
    song_src = reusable.get("song") or {}
    if "song" in reuse and song_src.get("asset_id") and spec.get("song"):
        if song_src.get("mentions_lead"):
            notes.append("the song's lyrics name the original lead, so a new song is composed")
        elif _exists(store, song_src["asset_id"]):
            spec["song"]["asset_id"] = song_src["asset_id"]
            if song_src.get("lrc_asset_id") and _exists(store, song_src["lrc_asset_id"]):
                spec["song"]["lrc_asset_id"] = song_src["lrc_asset_id"]
        else:
            notes.append("the original song is no longer in the library, so a new one is composed")
    reused_frames = []
    for shot in spec.get("shots") or []:
        if shot.get("lead"):
            continue
        key = shot["key"]
        if "frames" in reuse:
            ids = [a for a in (reusable.get("frames") or {}).get(key) or [] if _exists(store, a)]
            if ids:
                shot["reuse_asset_ids"] = ids
                shot["best"] = 0
                reused_frames.append(key)
        if "clips" in reuse and key in reused_frames:
            clips = {k: v for k, v in (reusable.get("clips") or {}).items() if prod.split_key(k)[0] == key and _exists(store, v)}
            if clips:
                shot["reuse_clips"] = clips
    if "clips" in reuse and "frames" not in reuse and reusable.get("clips"):
        notes.append("clips are only reused together with their frames (a clip starts on its own still)")
    settings = prod.normalise_settings({**(recipe.get("settings") or {}), **(options.get("settings") or {})})
    if options.get("engine"):
        spec["engine"] = options["engine"]
    spec = prod.normalise_spec(spec)
    meta = {"name": recipe.get("name"), "cast": {"lead": lead["name"], "character_id": lead.get("character_id")},
            "reuse": reuse, "reused_frames": reused_frames, "notes": notes}
    return spec, settings, meta


def _exists(store: Store, asset_id: Optional[str]) -> bool:
    if not asset_id:
        return False
    try:
        store.get_asset(asset_id)
        return True
    except NotFound:
        return False


def run_recipe(store: Store, recipe_name: str, cast: Any, name: Optional[str] = None,
               options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Create (not start) a production from a recipe; the caller queues its
    job. Returns the new production's state."""
    recipe = get_recipe(store.data_dir, recipe_name)
    spec, settings, meta = plan_run(store, recipe, cast, options)
    options = options or {}
    project_id = options.get("project")
    if project_id:
        store.get_project(project_id)
    name = name or f"{spec['title']} - {spec['lead']['name']}"
    state = prod.create_production(store.data_dir, name, spec, settings, project_id=project_id, recipe=meta)
    prod.log(state, "recipe", "created_from_recipe", recipe=recipe.get("name"), lead=spec["lead"]["name"],
             reuse=meta["reuse"], notes=meta["notes"] or None)
    prod.save_state(store.data_dir, state)
    return state
