"""Editable image compositions, stored as immutable native layout assets.

Compiling describes a placement; it does not establish that a diffusion model
will follow it. The MOSS exchange uses its documented centered yxyx convention.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math

from . import design, engine
from .ids import new_id
from .util import write_text_atomic

FORMAT = "prospero.composition/1"


def normalize(document):
    if not isinstance(document, dict):
        raise ValueError("composition must be an object")
    result = deepcopy(document)
    if result.get("format", FORMAT) != FORMAT:
        raise ValueError("unsupported composition format")
    result["format"] = FORMAT
    for key in ("width", "height"):
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{key} must be a positive pixel integer")
    if not isinstance(result.get("caption", ""), str):
        raise ValueError("caption must be text")
    result.setdefault("caption", "")
    if not isinstance(result.get("items"), list):
        raise ValueError("items must be a list")
    seen = set()
    for item in result["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip() or item["id"] in seen:
            raise ValueError("each item needs a unique non-empty id")
        seen.add(item["id"])
        if item.get("kind") not in ("object", "text"):
            raise ValueError(f"{item['id']}: kind must be object or text")
        for key in ("caption", "text", "reference_asset_id"):
            if key in item and not isinstance(item[key], str):
                raise ValueError(f"{item['id']}: {key} must be text")
        box = item.get("box")
        if not isinstance(box, list) or len(box) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in box):
            raise ValueError(f"{item['id']}: box must be finite [x,y,width,height] pixels")
        x, y, width, height = box
        if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > result["width"] or y + height > result["height"]:
            raise ValueError(f"{item['id']}: box must fit the canvas")
        for key in ("fill", "colour"):
            if key in item:
                design._hex(item[key])
    return result


def references(store, document):
    ids = list(dict.fromkeys(item["reference_asset_id"] for item in document["items"] if item.get("reference_asset_id")))
    for aid in ids:
        asset = store.get_asset(aid)
        if asset["kind"] != "image":
            raise ValueError(f"reference {aid} must be an image")
        if not (store.data_dir / asset["file_path"]).is_file():
            raise ValueError(f"reference {aid} has no retained image file")
    return ids


def load(store, asset_id):
    asset = store.get_asset(asset_id)
    if asset["kind"] != "layout" or (asset.get("recipe") or {}).get("operation") != "spatial":
        raise ValueError("asset is not an editable image composition")
    document = normalize(json.loads((store.data_dir / asset["file_path"]).read_text(encoding="utf-8")))
    return {"id": asset_id, "project_id": asset["project_id"], "name": asset["name"],
            "parent_id": (asset["recipe"] or {}).get("derived_from"), "document": document}


def save(store, project, document, name="Composición", parent_id=None):
    store.get_project(project)
    document = normalize(document)
    refs = references(store, document)
    name = name.strip() or "Composición"
    if parent_id and load(store, parent_id)["project_id"] != project:
        raise ValueError("the parent composition belongs to another project")
    aid = new_id("a")
    dest = store.path_for_asset_file(aid, ".json")
    write_text_atomic(dest, json.dumps(document, ensure_ascii=False, indent=2))
    store.create_asset(project_id=project, kind="layout", file_path=engine._rel(store, dest), mime="application/json",
                       width=document["width"], height=document["height"], asset_id=aid, name=name, source="rendered",
                       recipe={"operation": "spatial", "derived_from": parent_id, "input_asset_ids": refs})
    return load(store, aid)


def compile_prompt(document, target="caption"):
    document = normalize(document)
    width, height = document["width"], document["height"]
    refs = list(dict.fromkeys(item["reference_asset_id"] for item in document["items"] if item.get("reference_asset_id")))
    if target == "moss":
        items = []
        for item in document["items"]:
            x, y, w, h = item["box"]
            caption = item.get("caption", "") + (f" Text colour {item['colour']}." if item.get("colour") else "")
            row = {"label": item["kind"], "caption": caption,
                   "bbox": [round(y - height/2), round(x - width/2), round(y+h-height/2), round(x+w-width/2)]}
            if item["kind"] == "text":
                row["text"] = item.get("text", "")
            items.append(row)
        layout = {"summary": {"canvas": "single", "caption": document["caption"]}, "items": items,
                  "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}
        # This format describes boxes/text, not reference-image conditioning.
        return {"prompt": json.dumps(layout, ensure_ascii=False, separators=(",", ":")), "layout": layout,
                "reference_asset_ids": [], "notes": ["MOSS layout exchange does not encode reference images."] if refs else []}
    if target != "caption":
        raise ValueError("target must be caption or moss")
    lines = [document["caption"], f"Composition on a {width} by {height} pixel canvas. Coordinates start at the top left."]
    for item in document["items"]:
        x, y, w, h = item["box"]
        words = f"Render exactly {json.dumps(item.get('text', ''), ensure_ascii=False)}. " if item["kind"] == "text" else ""
        reference = f" Use <image{refs.index(item['reference_asset_id'])+1}> as its visual reference." if item.get("reference_asset_id") else ""
        colour = f" Text colour {item['colour']}." if item.get("colour") else ""
        lines.append(f"{item['id']}: {words}{item.get('caption', '')} Bounding box left={x:g}, top={y:g}, width={w:g}, height={h:g} pixels.{reference}{colour}")
    return {"prompt": "\n".join(lines), "reference_asset_ids": refs, "notes": ["Placement is requested in the prompt; inspect the generated image."]}


def preview(store, document):
    document = normalize(document)
    references(store, document)
    width, height = document["width"], document["height"]
    layers = [{"type": "rect", "x": 0, "y": 0, "w": width, "h": height, "fill": "#ffffff"}]
    for item in document["items"]:
        x, y, w, h = item["box"]
        box = {"x": x, "y": y, "w": w, "h": h}
        if item.get("reference_asset_id"):
            layers.append({**box, "type": "image", "asset": item["reference_asset_id"]})
        else:
            layers.append({**box, "type": "rect", "fill": item.get("fill", "#e4edf3")})
        layers.append({**box, "type": "text", "text": item.get("text") if item["kind"] == "text" else item.get("caption", item["id"]),
                       "size": max(12, min(48, int(h/3))), "colour": item.get("colour", "#172c3e"), "align": "center", "valign": "middle"})
    return design.image_bytes(design.render_layout({"width": width, "height": height, "layers": layers}, {}, engine._asset_resolver(store)))


def dispatch(store, action, *, project=None, asset_id=None, document=None, name="Composición", target="caption", parent_id=None, offset=0):
    if action == "list":
        store.get_project(project)
        page = store.list_assets(project, kind="layout", limit=60, offset=offset)
        return {**page, "items": [engine.asset_view(a) for a in page["items"]
                                 if (a.get("recipe") or {}).get("operation") == "spatial"]}
    if action == "save":
        return save(store, project, document, name, parent_id)
    if asset_id:
        retained = load(store, asset_id)
        if action == "get":
            return retained
        document = retained["document"]
    if action == "compile":
        return {**compile_prompt(document, target), "width": document["width"], "height": document["height"], "source_id": asset_id}
    if action == "preview":
        if not asset_id:
            raise ValueError("save the source composition before retaining a preview")
        data = preview(store, document)
        aid = new_id("a")
        dest = store.path_for_asset_file(aid, ".png")
        dest.write_bytes(data)
        asset = store.create_asset(project_id=retained["project_id"], kind="image", file_path=engine._rel(store, dest), mime="image/png",
                                   width=document["width"], height=document["height"], name=retained["name"], asset_id=aid,
                                   source="rendered", recipe={"operation": "spatial_preview", "derived_from": asset_id,
                                                            "input_asset_ids": references(store, document)})
        return {"source_id": asset_id, "asset": engine.asset_view(asset), "notes": ["Diagrammatic preview; object boxes are not generated illustrations."]}
    raise ValueError("action must be list, get, save, compile or preview")
