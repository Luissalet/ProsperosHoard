"""Continuity plans and sampled reviews for native storyboard productions."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from PIL import Image

from . import audio, director, engine, productions as prod, qa
from .store import Store

FPS = 24
POSITIONS = ("first", "middle", "last")
VERDICTS = ("pass", "defect", "unclear")
SHOT_SOURCES = {"last_frame_asset_id": "image", "control_asset_id": "video", "rendered_clip_asset_id": "video"}


def _scene(state: dict) -> dict:
    scene = state.get("spec", {}).get("scene_shots")
    if scene is None:
        raise engine.EngineError("no_scene_plan", "prepare a scene shot plan first")
    return scene


def _asset(store: Store, aid: str, kind: str, project: str) -> dict:
    item = store.get_asset(aid)
    if item["kind"] != kind or item["project_id"] != project or item.get("deleted_at"):
        raise engine.EngineError("bad_scene_asset", f"{aid} must be an active {kind} in the production's project")
    return item


def _source(state: dict, description: str) -> tuple[str, list[str]]:
    dossier = state.get("spec", {}).get("production_dossier") or {}
    scene = dossier.get("scene") or {}
    text = description.strip() or scene.get("text") or scene.get("title")
    if not text:
        raise engine.EngineError("no_scene_description", "give a scene description or use a received dossier")
    return text, dossier.get("preserveDetails", [])


def _text(value: Any, name: str, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise engine.EngineError("bad_scene_plan", f"{name} needs text of up to {limit} characters")
    return value.strip()


def _duration(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 2 <= value <= 5:
        raise engine.EngineError("bad_scene_plan", "each shot must last between 2 and 5 seconds")
    return round(value * FPS) / FPS


def _vision_image(path: Any) -> bytes:
    with Image.open(path) as image:
        return qa._jpeg(image.convert("RGB"), 768)


def suggest(store: Store, slug: str, sheet_id: str, description: str, duration_s: float, count: int,
            language: str, vision: Any, model: str) -> dict:
    state = prod.load_state(store.data_dir, slug)
    sheet = _asset(store, sheet_id, "image", state["project_id"])
    text, details = _source(state, description)
    if not 2 * count <= duration_s <= 5 * count:
        raise engine.EngineError("bad_scene_plan", "choose a total duration of 2 to 5 seconds per shot")
    if vision is None:
        raise engine.EngineError("vision_unavailable", "the configured vision model is unavailable")
    prompt = (
        f"Plan exactly {count} shots for this scene, totalling about {duration_s} seconds. "
        f"Use the attached character sheet as the identity reference. Each shot is 2 to 5 seconds. "
        f"Use one of these framing values: {', '.join(director.FRAMINGS)}. "
        "Design each camera angle so the continuity details remain visible whenever the action permits. "
        "Describe the first and last pose; describe one visible action and use cuts between angles. "
        f"Write the descriptions in {language}. Return only a JSON object with a shots array. "
        'Each element has framing, action, duration_s, first_frame and last_frame.\n'
        f"Scene: {text}\nDetails to preserve: {json.dumps(details, ensure_ascii=False)}"
    )
    try:
        response = _object(vision([_vision_image(store.data_dir / sheet["file_path"])], prompt))
        shots = response["shots"]
        if not isinstance(shots, list) or len(shots) != count:
            raise ValueError("wrong number of shots")
        result = [{"framing": _text(s["framing"], "framing", 80), "action": _text(s["action"], "action"),
                   "duration_s": _duration(s["duration_s"]), "first_frame": _text(s["first_frame"], "first frame"),
                   "last_frame": _text(s["last_frame"], "last frame")} for s in shots]
    except Exception as exc:
        raise engine.EngineError("scene_plan_failed", f"the vision model did not return a usable plan: {str(exc)[:220]}") from exc
    return {"production": slug, "character_sheet_asset_id": sheet_id, "vision": model, "shots": result,
            "preserve_details": details}


def apply_plan(store: Store, slug: str, sheet_id: str, description: str, details: list[str],
               shots: list[dict], aspect: str) -> dict:
    """Replace the shot list of an unrendered storyboard with anchored shots."""
    with prod.lock_for(slug):
        state = prod._editable(store.data_dir, slug)
        spec = state["spec"]
        completed = set(state["done"])
        started = completed - {"frames"} if spec.get("scene_shots") else completed
        if state.get("kind") == "short" or spec.get("song") or started:
            raise engine.EngineError("scene_needs_draft", "use a scene dossier or an unrendered storyboard without a song")
        pid = state["project_id"]
        _asset(store, sheet_id, "image", pid)
        text, inherited = _source(state, description)
        preserved = list(dict.fromkeys(_text(d, "continuity detail", 500) for d in [*inherited, *details]))
        if not preserved or len(preserved) > 20:
            raise engine.EngineError("bad_scene_plan", "give one to twenty details to preserve")
        plan, frames = [], {}
        for n, row in enumerate(shots, 1):
            key = str(n)
            control = _control(store, pid, key, row)
            start = control["first_frame_asset_id"]
            plan.append({"key": key, "aspect": aspect, "prompt": f"{control['framing']}. {control['action']}",
                         "motion_prompt": control["action"], "refs": [{"asset_id": sheet_id, "use": "Character identity"}],
                         "clips": [0], "scene_control": control})
            frames[key] = _frame_entry(start)
        state["spec"] = prod.normalise_spec({**spec, "shots": plan, "scene_shots": {
            "type": "video-clip", "description": text, "character_sheet_asset_id": sheet_id,
            "preserve_details": preserved, "aspect": aspect,
        }})
        state["stages"] = ["frames", "clips", "timeline"]
        state["kind"] = "scene"
        prod._requeue(state, "scene shot plan ready")
        state["done"]["frames"] = {"items": frames, "complete": True}
        prod.log(state, "frames", "scene_plan", keys=list(frames), sheet=sheet_id)
        prod.save_state(store.data_dir, state)
        return view(state)


def _control(store: Store, pid: str, key: str, row: dict) -> dict:
    framing = _text(row.get("framing"), "framing", 80)
    action = _text(row.get("action"), "action")
    duration = _duration(row.get("duration_s"))
    start = row.get("first_frame_asset_id")
    sources = {field: row[field] for field in SHOT_SOURCES if row.get(field)}
    if (not isinstance(start, str) or not start or not sources
            or ("rendered_clip_asset_id" in sources and len(sources) != 1)
            or not all(isinstance(aid, str) for aid in sources.values())):
        raise engine.EngineError("bad_scene_plan", f"shot {key} needs a first image and a last image, "
                                 "a control video, both, or a rendered clip on its own")
    _asset(store, start, "image", pid)
    for field, aid in sources.items():
        if field == "rendered_clip_asset_id":
            rendered_clip(store, pid, aid, duration)
        else:
            _asset(store, aid, SHOT_SOURCES[field], pid)
    return {"framing": framing, "action": action, "duration_s": duration, "first_frame_asset_id": start, **sources}


def rendered_clip(store: Store, project: str, aid: str, duration: float) -> dict:
    """Resolve a supplied take and verify it covers the entire cut."""
    clip = _asset(store, aid, "video", project)
    path = store.data_dir / clip["file_path"]
    seconds = (clip.get("duration_s") or audio.probe_duration_s(path)) if path.is_file() else None
    if seconds is None or seconds + 0.001 < duration:
        raise engine.EngineError("bad_scene_asset", f"{aid} must cover the full {duration:g} s cut")
    return clip


def _frame_entry(aid: str) -> dict:
    return {"best": aid, "variants": [aid], "reused": True}


def revise(store: Store, slug: str, key: str, change: dict) -> dict:
    """Replace the controls of one shot and leave it ready to render."""
    with prod.lock_for(slug):
        state = prod._editable(store.data_dir, slug)
        _scene(state)
        shot = next((s for s in state["spec"]["shots"] if s["key"] == key and s.get("scene_control")), None)
        if shot is None:
            raise engine.EngineError("no_scene_shot", f"scene shot {key} does not exist")
        if shot.get("locked"):
            raise engine.EngineError("shot_locked", f"shot {key} is approved; unlock it to change the controls")
        old = shot["scene_control"]
        if set(change) - (set(old) | SHOT_SOURCES.keys()):
            raise engine.EngineError("bad_scene_plan", "give only framing, action, duration or control assets")
        control = _control(store, state["project_id"], key, {**old, **change})
        if control != old:
            shot.update(scene_control=control, prompt=f"{control['framing']}. {control['action']}",
                        motion_prompt=control["action"], clip_seed=int(shot["clip_seed"]) + 1000, clips=[0])
            frames = state["done"].setdefault("frames", {"items": {}})["items"]
            frames[key] = _frame_entry(control["first_frame_asset_id"])
            clips = state["done"].get("clips", {}).get("items", {})
            for name in [name for name in clips if prod.split_key(name)[0] == key]:
                clips.pop(name)
            prod._requeue(state, f"updated controls of scene shot {key}")
            prod.log(state, "clips", "revised_scene_shot", key=key)
            prod.save_state(store.data_dir, state)
        return view(state)


def clip_template(control: dict) -> str | None:
    if control.get("control_asset_id"):
        return "ltx23_control"
    return "wan22_flf2v" if control.get("last_frame_asset_id") else None


def clip_body(state: dict, shot: dict, start: str, seed: int) -> dict:
    control = shot["scene_control"]
    details = state["spec"]["scene_shots"]["preserve_details"]
    duration = control["duration_s"]
    prompt = (f"{control['framing']}. {shot['motion_prompt']}\n"
              + "Preserve throughout: " + "; ".join(details))[:4000]
    body = {"reference_asset_id": start, "prompt": prompt, "seed": seed}
    end = control.get("last_frame_asset_id")
    template = clip_template(control)
    body["template"] = template
    if end:
        body["end_asset_id"] = end
    if template == "ltx23_control":
        width, height = {"16:9": (896, 512), "9:16": (512, 896), "1:1": (512, 512)}[state["spec"]["scene_shots"]["aspect"]]
        return {**body, "driving_asset_id": control["control_asset_id"],
                "width": width, "height": height,
                "template_params": {"length": math.ceil(duration * FPS / 8) * 8 + 1}}
    return {**body, "template_params": {"seconds": round(duration + 0.25, 3), "lightning": True}}


def _object(reply: str) -> dict:
    start, end = reply.find("{"), reply.rfind("}")
    return json.loads(reply[start:end + 1])


def _shot_take(state: dict, key: str) -> str:
    aid = state.get("done", {}).get("clips", {}).get("items", {}).get(key)
    if not aid or not any(s["key"] == key and s.get("scene_control") for s in state["spec"]["shots"]):
        raise engine.EngineError("no_scene_take", "this scene shot has no current clip")
    return aid


def _shot_duration(state: dict, key: str) -> float:
    return next(s["scene_control"]["duration_s"] for s in state["spec"]["shots"] if s["key"] == key)


def _sample_frames(duration: float) -> tuple[int, int, int]:
    length = round(duration * FPS)
    return 0, length // 2, length - 1


def _samples(store: Store, aid: str, project: str, duration: float) -> tuple[list[bytes], list[dict]]:
    clip = _asset(store, aid, "video", project)
    path = store.data_dir / clip["file_path"]
    images, frames = [], []
    for position, index in zip(POSITIONS, _sample_frames(duration)):
        frame = qa.video_frame(path, index / FPS, output_fps=FPS)
        if frame is None:
            raise engine.EngineError("no_scene_frames", f"could not decode the {position} frame")
        images.append(qa._jpeg(frame, 768))
        frames.append({"position": position, "at_s": index / FPS})
    return images, frames


def _checks(value: Any, count: int) -> list[dict]:
    if not isinstance(value, list) or len(value) != len(POSITIONS):
        raise ValueError("three sample results required")
    out = []
    for position, sample in zip(POSITIONS, value):
        if not isinstance(sample, dict) or sample.get("position") != position:
            raise ValueError(f"expected {position} sample")
        checks = sample.get("checks")
        if not isinstance(checks, list) or len(checks) != count:
            raise ValueError(f"each detail needs a verdict in the {position} frame")
        row = []
        for check in checks:
            if not isinstance(check, dict) or check.get("verdict") not in VERDICTS:
                raise ValueError("each verdict must be pass, defect or unclear")
            note = check.get("note", "")
            if not isinstance(note, str) or not note.strip():
                raise ValueError("describe the evidence for each verdict")
            row.append({"verdict": check["verdict"], "note": note.strip()[:400]})
        out.append({"position": position, "checks": row})
    return out


def review(store: Store, slug: str, key: str, vision: Any, model: str) -> dict:
    state = prod.load_state(store.data_dir, slug)
    scene = _scene(state)
    aid = _shot_take(state, key)
    images, frames = _samples(store, aid, state["project_id"], _shot_duration(state, key))
    details = scene["preserve_details"]
    out = {"production": slug, "key": key, "clip_asset_id": aid, "samples": frames, "preserve_details": details}
    if vision is None:
        return {**out, "status": "unavailable", "vision": model}
    sheet = _asset(store, scene["character_sheet_asset_id"], "image", state["project_id"])
    images.append(_vision_image(store.data_dir / sheet["file_path"]))
    prompt = (
        "Review continuity. Images 1, 2 and 3 are the first, middle and last frames of a shot. "
        "Image 4 is the character sheet. For each detail in the given order, in each frame, "
        "use pass only when visible and correct, defect when visibly wrong, unclear when hidden or unreadable. "
        "For left and right use the character's anatomical sides. Report only what is visible. "
        f"Details: {json.dumps(details, ensure_ascii=False)}. "
        'Return only JSON: {"samples":[{"position":"first","checks":[{"verdict":"pass","note":"visible evidence"}]},'
        '{"position":"middle","checks":[...]},{"position":"last","checks":[...]}]}.'
    )
    try:
        checks = _checks(_object(vision(images, prompt))["samples"], len(details))
    except Exception as exc:
        return {**out, "status": "unavailable", "vision": model, "note": str(exc)[:220]}
    return {**out, "status": _verdict(checks), "vision": model,
            "samples": [{**frame, **check} for frame, check in zip(frames, checks)]}


def _verdict(samples: list[dict]) -> str:
    verdicts = {check["verdict"] for sample in samples for check in sample["checks"]}
    return "defect" if "defect" in verdicts else "unclear" if "unclear" in verdicts else "pass"


def record(store: Store, slug: str, key: str, aid: str, samples: list[dict], reviewer: str) -> dict:
    with prod.lock_for(slug):
        state = prod._editable(store.data_dir, slug)
        scene = _scene(state)
        if _shot_take(state, key) != aid:
            raise engine.EngineError("stale_scene_take", "the shot's current clip changed; inspect the new take")
        try:
            checks = _checks(samples, len(scene["preserve_details"]))
        except ValueError as exc:
            raise engine.EngineError("bad_scene_review", str(exc)) from exc
        duration = _shot_duration(state, key)
        _, frames = _samples(store, aid, state["project_id"], duration)
        result = {"clip_asset_id": aid, "status": _verdict(checks), "reviewer": reviewer, "sample_window_s": duration,
                  "samples": [{**frame, **check} for frame, check in zip(frames, checks)]}
        state.setdefault("scene_reviews", {}).setdefault(key, {})[aid] = result
        prod.save_state(store.data_dir, state)
        return result


def evidence(store: Store, slug: str, key: str, aid: str, position: str) -> bytes:
    state = prod.load_state(store.data_dir, slug)
    _scene(state)
    if position not in POSITIONS:
        raise engine.EngineError("no_scene_frames", "sample position must be first, middle or last")
    review = state.get("scene_reviews", {}).get(key, {}).get(aid)
    current = state["done"].get("clips", {}).get("items", {}).get(key) == aid
    if not review and not current:
        raise engine.EngineError("stale_scene_take", "the clip is not a reviewed take of this shot")
    clip = _asset(store, aid, "video", state["project_id"])
    path = store.data_dir / clip["file_path"]
    index = POSITIONS.index(position)
    if review:
        at = review["samples"][index]["at_s"]
        image = qa.video_frame(path, at, output_fps=FPS if "sample_window_s" in review else None)
    else:
        frame = _sample_frames(_shot_duration(state, key))[index]
        image = qa.video_frame(path, frame / FPS, output_fps=FPS)
    if image is None:
        raise engine.EngineError("no_scene_frames", f"could not decode the {position} frame")
    return qa._jpeg(image, 768)


def view(state: dict) -> dict:
    scene = _scene(state)
    clips = state["done"].get("clips", {}).get("items", {})
    reviews = state.get("scene_reviews", {})
    timeline = state["done"].get("timeline", {}).get("timelines", {}).get(scene["aspect"], {})
    shots = []
    for shot in state["spec"]["shots"]:
        control = shot.get("scene_control")
        if not control:
            continue
        key = shot["key"]
        aid = clips.get(key)
        review = reviews.get(key, {}).get(aid)
        shots.append({"key": key, **control, "clip_asset_id": aid, "review": review})
    piece = timeline.get("piece")
    if piece and piece.get("asset_id"):
        checked = [(s["key"], s["clip_asset_id"], s["duration_s"]) for s in shots
                   if s["review"] and s["review"]["status"] == "pass"
                   and s["review"].get("sample_window_s") == s["duration_s"]]
        rendered = [(s["key"], s["clip_asset_id"], s["duration_s"]) for s in piece["shots"]]
        piece = {**piece, "review_status": "checked" if checked == rendered else "provisional"}
    return {**scene, "production": state["slug"], "project_id": state["project_id"], "shots": shots, "piece": piece}


def timeline_inputs(store: Store, state: dict) -> tuple[list[dict], list[dict]]:
    scene = view(state)
    clips, provenance = [], []
    for shot in scene["shots"]:
        aid = _shot_take(state, shot["key"])
        clip = _asset(store, aid, "video", state["project_id"])
        with (store.data_dir / clip["file_path"]).open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        duration = shot["duration_s"]
        clips.append({"asset_id": aid, "duration_s": duration, "kind": "video", "trim_start_s": 0,
                      "source_fit": "error"})
        recipe = clip.get("recipe") or {}
        provenance.append({**{k: v for k, v in shot.items() if k != "review"}, "sha256": digest,
                           "render_template": recipe.get("template"), "input_asset_ids": recipe.get("input_asset_ids", [])})
    return [{"type": "visual", "clips": clips}], provenance
