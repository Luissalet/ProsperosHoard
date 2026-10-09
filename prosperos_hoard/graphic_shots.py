"""Graphic shots in a production: the glue between a shot's `graphic` spec, the style cards, the song
and the cut.

A shot with ``kind: "graphic"`` is drawn by code (``motion_graphics.py``) instead of generated. It
carries a partial graphic spec (grammar, texts, mode, optional look / style card / cues / safe area);
the stretch of the song it covers is its span (``start_s``/``end_s``), which gives the duration frame
exact. This module

* validates a shot's graphic (``clean_shot_graphic``) and names the shot (``default_prompt``);
* resolves its look: shot look > shot style card > the production's default look
  (``spec.graphics``) > the lead's palette > the defaults (``resolve_look``);
* gives the auto-cut what it needs to put the shot in the cut (``cut_graphics``): the clip-mode ones
  play their *poster* still on their span (so pinned spans, the animatic and the storyboard work
  unchanged) and are turned into graphic clips afterwards (``timeline.inject_graphics``); the overlay
  ones go to the graphics track;
* draws the poster stills (``ensure_posters``) and renders a shot on its own (``render_shot``,
  ``preview_png``) as a video, a still or a video with transparency.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from . import engine, motion_graphics as mg
from . import timeline as timeline_mod
from .ids import new_id
from .jobs import JobCancelled
from .store import NotFound, Store
from .util import now_iso

POSTER_MAX_SIDE = 1920


class GraphicShotError(mg.GraphicError):
    """A graphic shot problem; the API shows it with its code like any graphic error."""


# ------------------------------------------------------------------ spec

def is_graphic(shot: dict[str, Any]) -> bool:
    return shot.get("kind") == "graphic" and isinstance(shot.get("graphic"), dict)


def is_overlay(shot: dict[str, Any]) -> bool:
    return is_graphic(shot) and (shot["graphic"].get("mode") or mg.DEFAULT_MODE[shot["graphic"]["grammar"]]) == "overlay"


def clean_shot_graphic(raw: Any, key: str = "") -> dict[str, Any]:
    """The graphic a shot stores: validated, without the fields the span decides (duration, window)."""
    try:
        graphic = mg.normalise_graphic(raw)
    except mg.GraphicError as exc:
        raise GraphicShotError(exc.code, f"shot {key}: {exc}" if key else str(exc)) from None
    graphic.pop("duration", None)
    graphic.pop("window_start_s", None)
    return graphic


def default_prompt(graphic: dict[str, Any]) -> str:
    """A readable name for a graphic shot (shots are listed, reported and exported by their prompt)."""
    data = graphic.get("data") or {}
    label = {"kinetic_lyrics": "Kinetic lyrics", "title_card": "Title card", "lower_third": "Lower third",
             "outro_card": "Outro card"}[graphic["grammar"]]
    text = data.get("title") or data.get("name") or ""
    if not text and data.get("lines"):
        first = data["lines"][0]
        text = first["text"] if isinstance(first, dict) else str(first)
    return f"{label}: {text}"[:200] if text else label


def clean_production_look(raw: Any) -> dict[str, Any]:
    """`spec.graphics`: the production's default look for its graphic shots: {style, look}."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or set(raw) - {"style", "look"}:
        raise GraphicShotError("bad_graphic", "spec.graphics is {style: style card name, look: {...}}")
    out: dict[str, Any] = {}
    if raw.get("style"):
        out["style"] = str(raw["style"]).strip()[:80]
    if raw.get("look"):
        try:
            out["look"] = mg.clean_look(raw["look"])
        except mg.GraphicError as exc:
            raise GraphicShotError(exc.code, f"spec.graphics.look: {exc}") from None
    return out


# ------------------------------------------------------------------ look

def style_card(store: Store, project_id: Optional[str], ref: Any) -> Optional[dict[str, Any]]:
    """A style card by id or by name (any case); None for an unknown or empty reference."""
    ref = str(ref or "").strip().lower()
    if not ref:
        return None
    try:
        cards = store.list_style_presets(project_id)
    except Exception:  # noqa: BLE001 - cards are an extra: without them the other layers decide
        return None
    return next((c for c in cards if c["id"].lower() == ref or c["name"].lower() == ref), None)


def lead_palette(state: dict[str, Any]) -> list[str]:
    spec = state.get("spec") or {}
    palette = (spec.get("lead") or {}).get("palette") or []
    return list(palette) if isinstance(palette, list) else []


def resolve_look(store: Store, state: dict[str, Any], graphic: dict[str, Any]) -> dict[str, Any]:
    """The complete look of a graphic: the defaults, under the lead's palette, under the production's default
    (its card, then its own look fields), under the shot's card, under the shot's own look."""
    project_id = state.get("project_id")
    default = (state.get("spec") or {}).get("graphics") or {}
    layers = [mg.look_from_palette(lead_palette(state)),
              mg.look_from_card(style_card(store, project_id, default.get("style"))),
              default.get("look"),
              mg.look_from_card(style_card(store, project_id, graphic.get("style"))),
              graphic.get("look")]
    return mg.merge_look(*layers)


def unknown_styles(store: Store, state: dict[str, Any]) -> list[str]:
    """Style card names that shots (or the production) ask for and that do not exist."""
    project_id = state.get("project_id")
    spec = state.get("spec") or {}
    wanted = {(spec.get("graphics") or {}).get("style")} | {s["graphic"].get("style") for s in spec.get("shots") or [] if is_graphic(s)}
    return sorted(str(w) for w in wanted if w and style_card(store, project_id, w) is None)


def graphic_shots(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in (state.get("spec") or {}).get("shots") or [] if is_graphic(s)]


def resolved_graphic(store: Store, state: dict[str, Any], shot: dict[str, Any]) -> dict[str, Any]:
    """The shot's graphic with its complete look, ready for `prepare_graphic`."""
    graphic = mg.normalise_graphic(shot["graphic"])
    graphic["look"] = resolve_look(store, state, shot["graphic"])
    return graphic


def cut_graphics(store: Store, state: dict[str, Any]) -> list[dict[str, Any]]:
    """What the auto-cut puts in the cut (`options["graphic_shots"]`): every graphic shot that has a span."""
    from . import productions as prod

    frames = ((state.get("done") or {}).get("frames") or {}).get("items") or {}
    out = []
    for shot in graphic_shots(state):
        span = prod.shot_span(shot)
        if not span:
            continue
        graphic = resolved_graphic(store, state, shot)
        overlay = graphic["mode"] == "overlay"
        out.append({"key": shot["key"], "poster": None if overlay else (frames.get(shot["key"]) or {}).get("best"),
                    "start_s": span[0], "end_s": span[1], "graphic": graphic})
    return out


# ---------------------------------------------------------------- posters

def frame_size(state: dict[str, Any], aspect: Optional[str] = None) -> tuple[int, int]:
    aspects = ((state.get("spec") or {}).get("timeline") or {}).get("aspects") or ["9:16"]
    return timeline_mod.ASPECTS.get(aspect or aspects[0]) or timeline_mod.ASPECTS["9:16"]


def _lyrics_lines(store: Store, state: dict[str, Any]) -> list[dict[str, Any]]:
    lyrics_id = ((state.get("done") or {}).get("lyrics") or {}).get("lyrics_asset_id")
    if not lyrics_id:
        return []
    try:
        return engine.read_lyrics(store, lyrics_id)["lines"]
    except Exception:  # noqa: BLE001 - untimed lyrics: the poster is drawn without lines
        return []


def shot_spec_for_span(store: Store, state: dict[str, Any], shot: dict[str, Any],
                       lyrics_lines: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """The shot's graphic for its own span, as the cut will draw it (lines from the sung lyrics of the stretch)."""
    from . import productions as prod

    span = prod.shot_span(shot)
    if not span:
        raise GraphicShotError("graphic_needs_span", f"shot {shot['key']} is a graphic and needs a span (start_s and end_s: "
                                                      "the stretch of the song it covers)")
    lines = _lyrics_lines(store, state) if lyrics_lines is None else lyrics_lines
    return timeline_mod.prepare_graphic(resolved_graphic(store, state, shot), span[0], span[1], lines, None)


def _poster_asset(store: Store, state: dict[str, Any], shot: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    spec = shot_spec_for_span(store, state, shot, lines)
    width, height = frame_size(state)
    scale = POSTER_MAX_SIDE / max(width, height)
    width, height = int(width * min(1.0, scale)) // 2 * 2, int(height * min(1.0, scale)) // 2 * 2
    png = mg.still_png(spec, width, height, fps=30)
    aid = new_id("a")
    dest = store.path_for_asset_file(aid, ".png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(png)
    engine.make_thumbnail(dest, store.path_for_thumb(aid))
    recipe = {"operation": "graphic_poster", "production": state.get("slug"), "shot": shot["key"], "grammar": spec["grammar"],
              "input_asset_ids": [], "created_at": now_iso()}
    return store.create_asset(project_id=state["project_id"], kind="image", file_path=engine._rel(store, dest), mime="image/png",
                              width=width, height=height, thumb_path=engine._rel(store, store.path_for_thumb(aid)),
                              source="rendered", recipe=recipe, asset_id=aid, name=f"Graphic - {default_prompt(shot['graphic'])}"[:100],
                              tags=["graphic", "poster"])


def ensure_posters(store: Store, state: dict[str, Any], *, refresh: bool = False) -> list[str]:
    """Every graphic shot gets an entry in the frames stage: a clip-mode one a poster still drawn from its
    graphic (what pinned spans, the storyboard and the editor export stand on), an overlay one an empty entry
    (nothing to generate). `refresh` draws the posters again (the lyrics got their timing). Returns the keys
    done now; the caller saves the state."""
    done = state.setdefault("done", {}).setdefault("frames", {"complete": False, "items": {}})
    items = done.setdefault("items", {})
    lines = _lyrics_lines(store, state)
    made = []
    for shot in graphic_shots(state):
        key = shot["key"]
        have = items.get(key)
        stale = refresh and shot["graphic"]["grammar"] == "kinetic_lyrics"  # only these show the sung words
        if have and have.get("graphic") and not stale and (is_overlay(shot) or _asset_exists(store, have.get("best"))):
            continue
        if is_overlay(shot):
            items[key] = {"variants": [], "best": None, "graphic": True}
        else:
            asset = _poster_asset(store, state, shot, lines)
            if have and have.get("best"):
                try:
                    store.trash_asset(have["best"])  # the old poster, unless a cut still plays it
                except Exception:  # noqa: BLE001
                    pass
            items[key] = {"variants": [asset["id"]], "best": asset["id"], "graphic": True}
        made.append(key)
    return made


def _asset_exists(store: Store, asset_id: Optional[str]) -> bool:
    if not asset_id:
        return False
    try:
        store.get_asset(asset_id)
        return True
    except NotFound:
        return False


# ------------------------------------------------------------ rendering

STILLS = ("video", "still", "alpha")


def preview_png(spec: dict[str, Any], width: int, height: int, at_s: Optional[float] = None, backdrop: bool = True) -> bytes:
    return mg.still_png(spec, width, height, at_s=at_s, fps=30, backdrop=backdrop)


def render_asset(store: Store, project_id: str, spec: dict[str, Any], *, what: str = "video", width: int, height: int, fps: int = 30,
                 name: str = "", recipe_extra: Optional[dict[str, Any]] = None, progress: Optional[Callable[[float], None]] = None,
                 should_cancel: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """A graphic (a complete spec with its duration) as an asset: an H.264 video (``video``), a PNG at the
    hero moment (``still``) or a QuickTime video with transparency for a video editor (``alpha``)."""
    if what not in STILLS:
        raise GraphicShotError("bad_render", f"what is one of {', '.join(STILLS)}")
    aid = new_id("a")
    started = time.monotonic()
    recipe = {"operation": "graphic_render", "what": what, "grammar": spec.get("grammar"), "width": width, "height": height, "fps": fps,
              "input_asset_ids": [], "created_at": now_iso(), **(recipe_extra or {})}
    label = (name or default_prompt(spec))[:90]
    if what == "still":
        png = mg.still_png(spec, width, height, fps=fps, backdrop=False)
        dest = store.path_for_asset_file(aid, ".png")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png)
        engine.make_thumbnail(dest, store.path_for_thumb(aid))
        return store.create_asset(project_id=project_id, kind="image", file_path=engine._rel(store, dest), mime="image/png", width=width,
                                  height=height, thumb_path=engine._rel(store, store.path_for_thumb(aid)), source="rendered",
                                  recipe=recipe, asset_id=aid, name=f"Graphic still - {label}"[:100], tags=["graphic"])
    alpha = what == "alpha"
    dest = store.path_for_asset_file(aid, ".mov" if alpha else ".mp4")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = mg.render_video(spec, dest, width=width, height=height, fps=fps, alpha=alpha, progress=progress, should_cancel=should_cancel)
    except mg.GraphicCancelled:
        dest.unlink(missing_ok=True)
        raise JobCancelled("cancelled") from None
    thumb = None
    try:
        if alpha:
            png = mg.still_png(spec, width, height, fps=fps, backdrop=True)
            tmp = dest.with_suffix(".png")
            tmp.write_bytes(png)
            engine.make_thumbnail(tmp, store.path_for_thumb(aid))
            tmp.unlink(missing_ok=True)
            thumb = engine._rel(store, store.path_for_thumb(aid))
        else:
            thumb = engine._video_thumbnail(dest, store.path_for_thumb(aid))
    except Exception:  # noqa: BLE001 - a missing thumbnail never fails a render
        thumb = None
    recipe["elapsed_s"] = round(time.monotonic() - started, 2)
    return store.create_asset(project_id=project_id, kind="video", file_path=engine._rel(store, dest),
                              mime="video/quicktime" if alpha else "video/mp4", width=width, height=height,
                              duration_s=info["duration_s"], thumb_path=thumb, source="rendered", recipe=recipe, asset_id=aid,
                              name=f"Graphic{' (alpha)' if alpha else ''} - {label}"[:100], tags=["graphic"] + (["alpha"] if alpha else []))


def render_shot(store: Store, state: dict[str, Any], key: str, what: str = "video", aspect: Optional[str] = None, fps: int = 30,
                progress: Optional[Callable[[float], None]] = None,
                should_cancel: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    shot = next((s for s in (state.get("spec") or {}).get("shots") or [] if s["key"] == str(key)), None)
    if shot is None:
        raise GraphicShotError("unknown_shot", f"the production has no shot '{key}'")
    if not is_graphic(shot):
        raise GraphicShotError("not_a_graphic", f"shot {key} is generated, not a graphic (set it with studio_graphic_shot first)")
    spec = shot_spec_for_span(store, state, shot)
    width, height = frame_size(state, aspect)
    if what == "alpha":
        spec["mode"] = "overlay"
    return render_asset(store, state["project_id"], spec, what=what, width=width, height=height, fps=fps,
                        name=default_prompt(shot["graphic"]), recipe_extra={"production": state.get("slug"), "shot": shot["key"]},
                        progress=progress, should_cancel=should_cancel)


# ---------------------------------------------------- previews and jobs

def preview_state(store: Store, production: Optional[str], project: Optional[str]) -> dict[str, Any]:
    """The state a preview or a standalone render resolves its look from: a production's (lead palette, default
    look), or a bare project's."""
    from . import productions as prod

    if production:
        state = prod.load_state(store.data_dir, production)
        if prod.is_legacy(state):
            raise GraphicShotError("legacy_production", "a scripted production has no graphic shots")
        return state
    if project:
        store.get_project(project)
    return {"project_id": project, "spec": {}, "done": {}}


def graphic_for(store: Store, state: dict[str, Any], graphic: Any, start_s: Optional[float], end_s: Optional[float],
                duration_s: Optional[float] = None) -> dict[str, Any]:
    """An unsaved graphic (the editor's, or an inline one) as the cut would draw it on its stretch: validated, with its
    look resolved and, for kinetic lyrics without lines, the sung lines of the stretch."""
    clean = clean_shot_graphic(graphic)
    if start_s is None or end_s is None:
        start_s, end_s = 0.0, float(duration_s or 4.0)
    if float(end_s) - float(start_s) < 0.5:
        raise GraphicShotError("bad_graphic", "a graphic lasts at least 0.5 s (end_s after start_s)")
    shot = {"key": "preview", "kind": "graphic", "graphic": clean, "start_s": float(start_s), "end_s": float(end_s)}
    return shot_spec_for_span(store, state, shot)


def render_job(store: Store, job: dict[str, Any], progress: Any) -> dict[str, Any]:
    """The `graphic_render` job: one graphic shot (or an inline graphic) as a video, a still or an alpha video."""
    p = job["params"]
    cancelled = getattr(progress, "cancelled", None)

    def report(frac: float) -> None:
        progress(frac, "drawing the graphic")

    try:
        state = preview_state(store, p.get("production"), p.get("project_id"))
        if p.get("production"):
            asset = render_shot(store, state, p["shot"], p.get("what", "video"), p.get("aspect"), int(p.get("fps") or 30), report, cancelled)
        else:
            spec = graphic_for(store, state, p["graphic"], None, None, p.get("duration_s"))
            width, height = frame_size(state, p.get("aspect"))
            if p.get("what") == "alpha":
                spec["mode"] = "overlay"
            asset = render_asset(store, p["project_id"], spec, what=p.get("what", "video"), width=width, height=height,
                                 fps=int(p.get("fps") or 30), progress=report, should_cancel=cancelled)
    except mg.GraphicCancelled:
        raise JobCancelled("cancelled") from None
    except mg.GraphicError as exc:
        raise engine.EngineError(exc.code, exc.message) from None
    return {"asset_id": asset["id"], "asset_ids": [asset["id"]], "duration_s": asset.get("duration_s")}
