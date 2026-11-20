"""Advisory identity, anatomy and prop checks on a motion-guided take."""

from __future__ import annotations

import json
import re
from typing import Any

from PIL import Image, ImageOps

from . import qa, scene_shots
from .store import Store


IDENTITY_THRESHOLD = 6.5
REGIONS = ("left_hand", "right_hand", "props")
LABELS = ("first", "quarter", "middle", "last")
WARNINGS = {
    "warning": {"en": "Check identity, hands and props before using this take.",
                "es": "Revisa la identidad, las manos y el atrezo antes de usar esta toma."},
    "unavailable": {"en": "The guided take could not be checked.",
                    "es": "No se ha podido comprobar la toma guiada."},
}


def sample_indices(duration: float) -> tuple[int, int, int, int]:
    frames = round(duration * scene_shots.FPS)
    return 0, frames // 4, frames // 2, frames - 1


def _parsed(response: str) -> list[dict]:
    start = re.search(r'\{\s*"samples"\s*:|\[\s*\{', response)
    if start is None:
        raise ValueError("expected a JSON object with control samples")
    obj, _ = json.JSONDecoder().raw_decode(response[start.start():])
    samples = obj["samples"] if isinstance(obj, dict) else obj
    if not isinstance(samples, list) or len(samples) != len(LABELS):
        raise ValueError("expected four control samples")
    result = []
    for label, row in zip(LABELS, samples):
        if not isinstance(row, dict) or row.get("position") != label:
            raise ValueError(f"expected the {label} control sample")
        score = row.get("identity")
        why = row.get("why")
        if type(score) not in (int, float) or not 0 <= score <= 10 or not isinstance(why, str) or not why.strip():
            raise ValueError(f"expected identity score and evidence for {label}")
        sample = {"position": label, "identity": {"score": score, "note": why.strip()[:300],
                                                    "passed": score >= IDENTITY_THRESHOLD}}
        for region in REGIONS:
            check = row.get(region)
            if not isinstance(check, dict) or check.get("verdict") not in scene_shots.VERDICTS:
                raise ValueError(f"expected a verdict for {region} at {label}")
            note = check.get("note")
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"expected evidence for {region} at {label}")
            sample[region] = {"verdict": check["verdict"], "note": note.strip()[:300]}
        result.append(sample)
    return result


def inspect(store: Store, state: dict, shot: dict, aid: str, vision: Any, model: str) -> dict:
    scene = state["spec"]["scene_shots"]
    control = shot["scene_control"]
    duration = control["duration_s"]
    indices = sample_indices(duration)
    frames = [{"position": label, "frame": index, "at_s": index / scene_shots.FPS}
              for label, index in zip(LABELS, indices)]
    out = {"clip_asset_id": aid, "control_asset_id": control["control_asset_id"], "sample_window_s": duration,
           "vision": model, "identity_threshold": IDENTITY_THRESHOLD}
    if vision is None:
        return {**out, "samples": frames, "status": "unavailable", "warning": WARNINGS["unavailable"]}
    try:
        project = state["project_id"]
        clip = scene_shots._asset(store, aid, "video", project)
        guide = scene_shots._asset(store, control["control_asset_id"], "video", project)
        clip_path = store.data_dir / clip["file_path"]
        guide_path = store.data_dir / guide["file_path"]
        sheet = scene_shots._asset(store, scene["character_sheet_asset_id"], "image", project)
        images = [scene_shots._vision_image(store.data_dir / sheet["file_path"])]
        for sample in frames:
            pair = Image.new("RGB", (1024, 768), "#202020")
            for left, size, path in ((0, 256, guide_path), (256, 768, clip_path)):
                frame = qa.video_frame(path, sample["at_s"], output_fps=scene_shots.FPS)
                if frame is None:
                    source = "guide" if left == 0 else "take"
                    raise ValueError(f"could not decode {source} frame {sample['frame']}")
                tile = ImageOps.contain(frame, (size, size))
                pair.paste(tile, (left + (size - tile.width) // 2, (768 - tile.height) // 2))
            images.append(qa._jpeg(pair, 1024))
        prompt = (
            "Image 1 is the character sheet. Images 2 through 5 are the first, quarter, middle and last "
            "frame pairs. In each pair, the left panel is the motion guide and the right panel is the take. "
            "Score only the take on the right. Use the guide to locate arms, hands and intended contact points. "
            "Score the same character in each frame from 0 to 10 based on face, hair, silhouette and clothing. "
            "Give at most 6 if one of those visible features clearly contradicts the sheet. "
            "Assess the two hands separately, using anatomical left and right. Trace each visible hand through the "
            "wrist to a continuous visible forearm. A hand appearing at an edge with the forearm hidden by paper "
            "is unclear even if a hidden arm would be physically possible. Distinguish that from a disconnected "
            "blob or fused hand, which is a defect. A lowered hand and sleeve can pass during a release. "
            "For props, check the required shape and artwork, especially at the last frame. Use unclear if hidden or "
            "too small to tell. Report what is visible in each frame and do not borrow evidence from its neighbors. "
            "Evaluate the relevant visible props only. If the shot has none, say so in the props note. "
            f"Scene: {scene['description']}\nShot action: {control['action']}\n"
            f"Details: {json.dumps(scene['preserve_details'], ensure_ascii=False)}\n"
            'Return only a JSON object {"samples": [...]} with samples ordered first, quarter, middle, last. '
            'Each sample has position, identity '
            '(a number 0-10), why, left_hand, right_hand and props. Each region has verdict ("pass", "defect", '
            'or "unclear") and a short note. Keep every note and identity reason to at most five words.'
        )
        checks = _parsed(vision(images, prompt))
    except Exception as exc:
        return {**out, "samples": frames, "status": "unavailable", "warning": WARNINGS["unavailable"],
                "note": str(exc)[:220]}
    samples = [{**frame, **check} for frame, check in zip(frames, checks)]
    passed = all(row["identity"]["passed"] and all(row[name]["verdict"] == "pass" for name in REGIONS)
                 for row in samples)
    return {**out, "samples": samples, "status": "pass" if passed else "warning",
            **({} if passed else {"warning": WARNINGS["warning"]})}


def record(run, vision_provider) -> None:
    items = run.items("clips")
    reviews = run.state.setdefault("scene_control_reviews", {})
    provider = None
    for shot in run.spec["shots"]:
        key = shot["key"]
        control = shot.get("scene_control") or {}
        aid = items.get(key)
        if not control.get("control_asset_id") or not aid:
            continue
        previous = reviews.get(key)
        if (previous and previous["status"] != "unavailable" and previous["clip_asset_id"] == aid
                and previous["control_asset_id"] == control["control_asset_id"]
                and previous["sample_window_s"] == control["duration_s"]):
            continue
        if provider is None:
            provider = vision_provider()
        vision, model = provider
        run.tick(run.stage_fraction(0.98), "Checking guided shot / Comprobando el plano guiado")
        result = inspect(run.store, run.state, shot, aid, vision, model or "no vision model")
        reviews[key] = result
        run.log("scene_control_review", key=key, asset_id=aid, status=result["status"])
        run.save()
