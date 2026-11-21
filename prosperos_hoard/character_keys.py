"""Canonical source and per-detail comparison for character keys."""

from __future__ import annotations

import hashlib
import io
import json

from PIL import Image, ImageOps

from . import identity
from .global_model import operation


TEMPLATE = "qwen21_character_key"
PARAMS = {"sheet_asset_id", "anchor_box", "identity_checks"}
DEFAULT_CHECKS = {
    "face": "The same facial features and proportions as the character on the sheet.",
    "silhouette": "The same clothing silhouette, lengths and proportions.",
    "props": "The same signature props, proportions, materials and sides when visible.",
}


def validate(store, project, params, references):
    from .engine import EngineError

    sheet_id = params.get("sheet_asset_id")
    if not isinstance(sheet_id, str) or not sheet_id:
        raise EngineError("character_sheet_required",
                          "Declare the kept character sheet / Declara la hoja de personaje conservada")
    if len(references) != 2 or references[0] != sheet_id or references[1] == sheet_id:
        raise EngineError("character_key_references",
                          "Pass the sheet first and a separate pose image second / "
                          "Pasa primero la hoja y después una imagen de pose distinta")
    if params.get("depth_asset_id") in references or not params.get("depth_asset_id"):
        raise EngineError("character_key_depth",
                          "Pass a separate depth guide / Pasa una guía de profundidad independiente")
    sheet = store.get_asset(sheet_id)
    if sheet["kind"] != "image" or sheet["project_id"] != project:
        raise EngineError("character_sheet_invalid",
                          "Import the sheet as an image in this project / Importa la hoja como imagen en este proyecto")
    box = params.get("anchor_box")
    if (not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box)
            or not (0 <= box[0] < box[2] <= sheet["width"] and 0 <= box[1] < box[3] <= sheet["height"])):
        raise EngineError("character_sheet_crop",
                          "Select one view on the sheet as [left, top, right, bottom] in pixels / "
                          "Selecciona una vista de la hoja como [izquierda, arriba, derecha, abajo] en píxeles")
    checks = params.get("identity_checks", {})
    if (not isinstance(checks, dict) or len(set(checks) | set(DEFAULT_CHECKS)) > 8 or any(
            not isinstance(key, str) or not key.strip() or len(key) > 40
            or not isinstance(detail, str) or not detail.strip() or len(detail) > 300
            for key, detail in checks.items())):
        raise EngineError("character_key_checks",
                          "Use up to eight named identity checks / Usa hasta ocho comprobaciones de identidad con nombre")
    return sheet, box, {**DEFAULT_CHECKS, **checks}


def prepare(store, project, params, references, size):
    from .engine import EngineError

    sheet, box, checks = validate(store, project, params, references)
    path = store.data_dir / sheet["file_path"]
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as source:
            view = source.crop(box).convert("RGB")
    except OSError as exc:
        raise EngineError("character_sheet_unreadable",
                          "Cannot read the character sheet / No se puede leer la hoja de personaje") from exc
    digest = hashlib.sha256(data).hexdigest()
    piece = (sheet.get("recipe") or {}).get("hub_piece")
    if piece and piece.get("sha256") != digest:
        raise EngineError("character_sheet_changed",
                          "The sheet differs from the imported piece / La hoja difiere de la pieza importada")
    canvas = ImageOps.pad(view, size, Image.Resampling.LANCZOS, color=view.getpixel((0, 0)))
    encoded = io.BytesIO()
    canvas.save(encoded, "PNG")
    anchor = encoded.getvalue()
    receipt = {"sheet_asset_id": sheet["id"], "sheet_sha256": digest, "anchor_box": box,
               "anchor_sha256": hashlib.sha256(anchor).hexdigest(), "pose_asset_id": references[1],
               "checks": checks}
    if piece:
        receipt["hub_piece"] = piece
    return anchor, view, receipt


@operation
def review_asset(store, asset_id, vision_provider):
    from .engine import EngineError

    asset = store.get_asset(asset_id)
    recipe = asset.get("recipe") or {}
    anchor = recipe.get("character_key")
    if not anchor:
        raise EngineError("character_key_required",
                          "Select a key generated from a sheet / Selecciona una clave generada desde una hoja")
    references = [anchor["sheet_asset_id"], anchor["pose_asset_id"]]
    params = {**anchor, "depth_asset_id": recipe.get("depth_asset_id"), "identity_checks": anchor["checks"]}
    _, view, receipt = prepare(store, asset["project_id"], params, references, (asset["width"], asset["height"]))
    if receipt["sheet_sha256"] != anchor["sheet_sha256"]:
        raise EngineError("character_sheet_changed",
                          "The sheet differs from the rendered source / La hoja difiere de la usada en la clave")
    data = (store.data_dir / asset["file_path"]).read_bytes()
    return {"asset_id": asset_id, "asset_sha256": hashlib.sha256(data).hexdigest(), **receipt,
            "comparison": compare(view, data, receipt["checks"], vision_provider)}


def compare(view, data, checks, vision_provider):
    vision, model = vision_provider() if vision_provider else (None, None)
    if vision is None:
        return {"status": "unavailable", "reason": "No vision model / Sin modelo de visión"}
    prompt = (
        "Compare image 2, a generated character key, with image 1, its canonical character view. "
        "Assess every named criterion separately. Mark pass only when the visible details match. Use fail for "
        "a visible difference and unclear for an obscured or ambiguous detail. Compare the character, not the "
        "pose or background. Return only a JSON object keyed by the exact criterion names. Every value must "
        'have a "status" (pass, fail or unclear) and an "observation" citing what is visible in both images.\n'
        + json.dumps(checks, ensure_ascii=False)
    )
    try:
        with Image.open(io.BytesIO(data)) as result:
            reply = vision([identity._jpeg(view, side=1024), identity._jpeg(result.convert("RGB"), side=1024)], prompt)
        start, end = reply.index("{"), reply.rindex("}") + 1
        findings = json.loads(reply[start:end])
    except Exception:
        return {"status": "unavailable", "model": model,
                "reason": "No usable vision comparison / Sin un cotejo visual utilizable"}
    details = {}
    for name, expectation in checks.items():
        value = findings.get(name, {})
        status = value.get("status") if isinstance(value, dict) else None
        observation = value.get("observation") if isinstance(value, dict) else None
        if status not in ("pass", "fail", "unclear") or not isinstance(observation, str) or not observation.strip():
            status, observation = "unclear", "No observation returned / Sin observación"
        details[name] = {"status": status, "expected": expectation, "observation": observation[:600]}
    statuses = {detail["status"] for detail in details.values()}
    status = "pass" if statuses == {"pass"} else "fail" if "fail" in statuses else "unclear"
    return {"status": status, "model": model, "details": details}
