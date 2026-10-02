"""The four operations other Hoard apps call on Prospero through the family hub, as plain functions (the routes are in
`family_api.py`):

- `production_export_lumiere`: writes a production's cut (FCP7 XML + EDL, as `exporters` makes them) into the data folder and asks
  the video editor to open it as a project (`project_from_timeline` through the hub).
- `cast_import_character`: a cast member from a name, a description, a look and reference images, from another app.
- `production_from_storyboard`: a production draft from a list of shots (text, duration, image); it is not queued, because a
  production still needs a song before it can run.
- `voice_tts`: speaks a text and returns the path of the audio file (the existing speech engines and library voices).

Pure logic: no FastAPI here; what the app owns (store, backend, the lookups of `api.py`) comes in as arguments.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional

from . import engine, exporters, productions, voice_engines as ve, voice_lab
from .ids import new_id
from .store import NotFound, Store
from .util import write_text_atomic

DEFAULT_SHOT_S = 4.0
MAX_SHOTS = 80
SOURCE_PREFIX = "source_ref: "


def _slug(text: str) -> str:
    return re.sub(r"[^\w-]+", "_", text).strip("_")[:60] or "cut"


# ---------------------------------------------------------------- production_export_lumiere

def export_to_lumiere(store: Store, *, production: str, aspect: Optional[str], timeline_id: Optional[str], lookup: exporters.Lookup,
                      resolve_timeline: Callable[..., str], call: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """Export a production's cut (or one timeline) to files and hand it to the video editor.

    `resolve_timeline(timeline_id=, production=, aspect=)` is the app's own rule for which timeline a production means;
    `call(app, tool, arguments)` is `family.call`. The answer says `ok` and, when the editor took it, `project_id` and `url`;
    when it could not (not running, a folder it may not read) `ok` is false with its reason, and the files are still there."""
    tid = resolve_timeline(timeline_id=timeline_id, production=production, aspect=aspect)
    timeline = store.get_timeline(tid)
    state = productions.load_state(store.data_dir, production) if production else {}
    title = str(state.get("name") or timeline.get("name") or tid)[:100]
    if aspect or timeline.get("aspect"):
        title = f"{title} {aspect or timeline.get('aspect')}".strip()
    folder = store.data_dir / "exports" / "lumiere" / _slug(f"{production or 'timeline'}-{tid}")
    folder.mkdir(parents=True, exist_ok=True)
    base = _slug(title)
    xml_path, edl_path = folder / f"{base}.xml", folder / f"{base}.edl"
    write_text_atomic(xml_path, exporters.to_xmeml(timeline, lookup, title))
    write_text_atomic(edl_path, exporters.to_edl(timeline, lookup, title))
    visual = next((t for t in timeline.get("tracks") or [] if t.get("type") == "visual"), {"clips": []})
    out: dict[str, Any] = {"ok": False, "timeline_id": tid, "clips": len(visual["clips"]), "fps": timeline.get("fps"),
                           "files": {"xml": str(xml_path), "edl": str(edl_path)}}
    res = call("lumiere", "project_from_timeline", {"title": title, "fcpxml_path": str(xml_path)})
    result = res.get("result") if isinstance(res.get("result"), dict) else {}
    if res.get("ok") and result.get("ok", True) and result.get("project_id"):
        out.update(ok=True, project_id=result["project_id"], url=result.get("url"), imported_clips=result.get("clips"),
                   skipped=result.get("skipped") or [])
    else:
        out["error"] = str(res.get("error") or result.get("error") or "the video editor did not take the cut")[:300]
        out["hint"] = ("Open Lumiere's Hoard and try again; if it only reads some folders (LUMIERE_FILE_ROOTS), allow Prospero's data "
                       "folder. The XML and EDL files above can also be opened in it by hand.")
    return out


# ---------------------------------------------------------------- cast_import_character

def import_character(store: Store, backend: Any, *, name: str, description: str, look: str, images: list[str], source_ref: str,
                     project: Optional[str], casting_project: Callable[[], str]) -> dict[str, Any]:
    """A cast member made from what another app knows about it. The same name and `source_ref` again returns the member that
    exists (`existing: true`) instead of failing, so a hub rule that fires twice does no harm."""
    name = (name or "").strip()
    if not name:
        raise engine.EngineError("name_required", "a character needs a name")
    pid = project or casting_project()
    store.get_project(pid)
    marker = f"{SOURCE_PREFIX}{source_ref.strip()}" if source_ref.strip() else ""
    for c in store.list_characters(pid):
        if c["name"].lower() == name[:80].lower():
            if marker and marker in (c.get("notes") or ""):
                return {"ok": True, "character_id": c["id"], "project_id": pid, "name": c["name"], "existing": True,
                        "images": len(c.get("reference_asset_ids") or [])}
            raise engine.EngineError("name_taken", f"this project already has a character called '{c['name']}'; "
                                                   "give another name or another project")
    asset_ids: list[str] = []
    for raw in images[:12]:
        path = engine.resolve_import_path(backend, store, raw)
        asset_ids.append(engine.import_asset(store, pid, path, "image")["id"])
    fields: dict[str, Any] = {"prompt": (look or description).strip()[:2000] or None, "bio": description.strip()[:2000] or None,
                              "notes": marker or None, "reference_asset_ids": asset_ids,
                              "canonical_asset_id": asset_ids[0] if asset_ids else None}
    char = store.create_character(pid, name, **{k: v for k, v in fields.items() if v not in (None, [])})
    return {"ok": True, "character_id": char["id"], "project_id": pid, "name": char["name"], "existing": False, "images": len(asset_ids),
            "canonical_asset_id": char.get("canonical_asset_id")}


# ---------------------------------------------------------------- production_from_storyboard

def storyboard_spec(shots: list[dict[str, Any]], *, title: str, lead: dict[str, Any], source_ref: str,
                    reuse: dict[int, list[str]]) -> dict[str, Any]:
    """The production spec for a storyboard: one shot per entry, prompts = the text, spans from the durations (when any is given)."""
    if not shots:
        raise engine.EngineError("shots_required", "give at least one shot: {text, duration_s?, image?}")
    if len(shots) > MAX_SHOTS:
        raise engine.EngineError("too_many_shots", f"at most {MAX_SHOTS} shots; split the storyboard")
    timed = any(s.get("duration_s") for s in shots)
    out: list[dict[str, Any]] = []
    cursor = 0.0
    for i, shot in enumerate(shots, 1):
        text = str(shot.get("text") or shot.get("prompt") or "").strip()
        if not text:
            raise engine.EngineError("bad_shot", f"shot {i} needs text")
        item: dict[str, Any] = {"key": str(i), "prompt": text[:2000], "lead": False}
        if reuse.get(i):
            item["reuse_asset_ids"] = reuse[i]
        if timed:
            try:
                length = float(shot.get("duration_s") or DEFAULT_SHOT_S)
            except (TypeError, ValueError):
                raise engine.EngineError("bad_shot", f"shot {i}: duration_s must be a number of seconds") from None
            if not 0.2 <= length <= 600:
                raise engine.EngineError("bad_shot", f"shot {i}: duration_s must be between 0.2 and 600")
            item["start_s"], item["end_s"] = round(cursor, 3), round(cursor + length, 3)
            cursor += length
        out.append(item)
    spec: dict[str, Any] = {"title": title, "lead": lead, "shots": out}
    if source_ref.strip():
        spec["source_ref"] = source_ref.strip()[:300]
    return spec


def production_from_storyboard(store: Store, backend: Any, *, title: str, shots: list[dict[str, Any]], source_ref: str,
                               project: Optional[str], character_id: Optional[str], lead_name: Optional[str], lead_look: Optional[str],
                               song_asset_id: Optional[str], view: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise engine.EngineError("title_required", "a production needs a title")
    if character_id:
        lead = {"character_id": store.get_character(character_id)["id"]}
    else:
        lead = {"name": (lead_name or "Lead").strip()[:80], "look": (lead_look or "the main subject of the storyboard").strip()[:600]}
    if project:
        store.get_project(project)
    pid = project or store.create_project(title[:120], f"Storyboard from another app{': ' + source_ref if source_ref else ''}")["id"]
    reuse: dict[int, list[str]] = {}
    for i, shot in enumerate(shots[:MAX_SHOTS], 1):
        image = str(shot.get("image") or "").strip()
        if not image:
            continue
        try:
            asset = store.get_asset(image)
            reuse[i] = [asset["id"]]
        except NotFound:
            path = engine.resolve_import_path(backend, store, image)
            reuse[i] = [engine.import_asset(store, pid, path, "image")["id"]]
    spec = storyboard_spec(shots, title=title, lead=lead, source_ref=source_ref, reuse=reuse)
    if song_asset_id:
        spec["song"] = {"asset_id": store.get_asset(song_asset_id)["id"]}
    state = productions.create_production(store.data_dir, title, spec, None, project_id=pid)
    productions.adopt_lead(store, state)
    out = {"ok": True, "production": state["slug"], "project_id": pid, "shots": len(spec["shots"]), "queued": False,
           "view": view(state["slug"])}
    if not spec.get("song"):
        out["next"] = ("a draft: add the song (studio_production_song with asset_id, or compose it) and then "
                       "studio_production_continue to run it")
    else:
        out["next"] = "studio_production_continue to run it"
    return out


# ---------------------------------------------------------------- voice_tts

def tts(store: Store, tts_engines: list[Any], *, text: str, voice: Optional[str], lang: Optional[str],
        engine_id: Optional[str] = None, speed: Optional[float] = None) -> dict[str, Any]:
    """Speak `text` with a library voice (id or name), an engine's own voice id, or the best engine installed; the audio is a WAV file
    in the data folder and `path` says where. `engine_id` picks the engine (a library voice's own engine wins; `unknown_engine` when
    it is not one of ours) and `speed` (0.5..2.0) is passed to the engine."""
    text = (text or "").strip()
    if not text:
        raise engine.EngineError("empty_text", "give the text to speak")
    if len(text) > 20000:
        raise engine.EngineError("text_too_long", "at most 20000 characters per call; split the text")
    spec: dict[str, Any] = {}
    if lang:
        spec["language"] = lang.strip()[:10]
    if speed is not None:
        try:
            spec["speed"] = float(speed)
        except (TypeError, ValueError):
            raise engine.EngineError("bad_speed", "speed is a number from 0.5 to 2.0") from None
        if not 0.5 <= spec["speed"] <= 2.0:
            raise engine.EngineError("bad_speed", "speed is a number from 0.5 to 2.0 (1.0 is normal)")
    wanted_engine = (engine_id or "").strip()
    wanted = (voice or "").strip()
    if wanted:
        library = None
        try:
            library = store.get_studio_voice(wanted)
        except NotFound:
            library = next((v for v in store.list_studio_voices() if v["name"].strip().lower() == wanted.lower()), None)
        if library is not None:
            spec["voice_id"] = library["id"]
        else:
            spec["voice_ref"] = wanted
    if "voice_id" not in spec and wanted_engine:
        try:
            chosen = ve.get_engine(tts_engines, wanted_engine)
        except KeyError:
            chosen = None
        if chosen is None or not chosen.is_installed():
            raise engine.EngineError("unknown_engine", f"'{wanted_engine}' is not an installed speech engine here (Voice > Engines lists them)")
        spec["engine_id"] = chosen.id
    if "voice_id" not in spec and "engine_id" not in spec:
        picked = ve.best_installed_tts(tts_engines)
        if picked is None:
            raise engine.EngineError("tts_not_installed", "no text-to-speech engine is installed (Voice > Engines installs Piper or another)")
        spec["engine_id"] = picked.id
    wav, used_engine = voice_lab.synthesize_with_spec(store, tts_engines, spec, text)
    folder = store.data_dir / "exports" / "tts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{new_id('tts')}.wav"
    path.write_bytes(wav)
    return {"ok": True, "path": str(path), "engine_id": used_engine, "bytes": len(wav)}
