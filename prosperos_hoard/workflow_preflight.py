"""Workflow preflight: before a run uses a ComfyUI workflow, check every node class against ComfyUI's /object_info and
every model file the workflow names (checkpoints, LoRAs, diffusion models, VAEs, text encoders...) against the
loader's option list, and say what is missing and where it goes (the ComfyUI ``models/<folder>``), plus the node
pack that usually provides a missing node.

Pure logic over an API-format workflow and an object_info dict; informational only (it never blocks a run more
than the existing checks do). The pattern follows D3voz/openjutsu (MIT); no code was copied.
"""

from __future__ import annotations

from typing import Any, Optional

from . import comfy_driver

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".sft")

# input name -> ComfyUI models/<folder>; (class, input) overrides first
FOLDER_BY_CLASS_INPUT = {
    ("CLIPVisionLoader", "clip_name"): "clip_vision",
    ("UpscaleModelLoader", "model_name"): "upscale_models",
    ("UnetLoaderGGUF", "unet_name"): "unet",
    ("CLIPLoaderGGUF", "clip_name"): "text_encoders",
    ("ModelPatchLoader", "name"): "model_patches",
    ("LoadBackgroundRemovalModel", "bg_removal_name"): "background_removal",
    ("LTXAVTextEncoderLoader", "text_encoder"): "text_encoders",
}
FOLDER_BY_INPUT = {
    "ckpt_name": "checkpoints", "lora_name": "loras", "unet_name": "diffusion_models", "vae_name": "vae",
    "clip_name": "text_encoders", "clip_name1": "text_encoders", "clip_name2": "text_encoders",
    "clip_name3": "text_encoders", "clip_name4": "text_encoders", "control_net_name": "controlnet",
    "style_model_name": "style_models", "audio_encoder_name": "audio_encoders", "model_name": "models",
    "upscale_model": "upscale_models", "gligen_name": "gligen", "hypernetwork_name": "hypernetworks",
    "embedding_name": "embeddings", "photomaker_model_name": "photomaker", "ipadapter_file": "ipadapter",
}

# node class pattern -> the custom node pack that provides it (the usual source; ComfyUI Manager finds the rest by name)
NODE_PACKS = (
    ("VHS_", "ComfyUI-VideoHelperSuite", "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite"),
    ("WanVideo", "ComfyUI-WanVideoWrapper", "https://github.com/kijai/ComfyUI-WanVideoWrapper"),
    ("GGUF", "ComfyUI-GGUF", "https://github.com/city96/ComfyUI-GGUF"),
    ("IPAdapter", "ComfyUI_IPAdapter_plus", "https://github.com/cubiq/ComfyUI_IPAdapter_plus"),
    ("Florence2", "ComfyUI-Florence2", "https://github.com/kijai/ComfyUI-Florence2"),
    ("Preprocessor", "comfyui_controlnet_aux", "https://github.com/Fannovel16/comfyui_controlnet_aux"),
    ("DepthAnything", "comfyui_controlnet_aux", "https://github.com/Fannovel16/comfyui_controlnet_aux"),
    (" VFI", "ComfyUI-Frame-Interpolation", "https://github.com/Fannovel16/ComfyUI-Frame-Interpolation"),
    ("(rgthree)", "rgthree-comfy", "https://github.com/rgthree/rgthree-comfy"),
    ("easy ", "ComfyUI-Easy-Use", "https://github.com/yolain/ComfyUI-Easy-Use"),
    ("ReActor", "ComfyUI-ReActor", "https://github.com/Gourieff/ComfyUI-ReActor"),
    ("KJ", "ComfyUI-KJNodes", "https://github.com/kijai/ComfyUI-KJNodes"),
)


def node_pack(class_type: str) -> Optional[dict[str, str]]:
    for needle, name, url in NODE_PACKS:
        if (class_type.startswith(needle) if needle.endswith("_") else needle in class_type):
            return {"name": name, "url": url}
    return None


def model_folder(class_type: str, input_name: str) -> Optional[str]:
    return FOLDER_BY_CLASS_INPUT.get((class_type, input_name)) or FOLDER_BY_INPUT.get(input_name)


def _is_combo(object_info: dict[str, Any], class_type: str, input_name: str) -> bool:
    try:
        spec = object_info[class_type]["input"]
        entry = (spec.get("required") or {}).get(input_name) or (spec.get("optional") or {}).get(input_name)
        return isinstance(entry[0], list) or entry[0] in ("COMBO", "COMFY_COMBO")
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def _norm(name: str) -> str:
    return str(name).replace("\\", "/").strip().lower()


def model_inputs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Every literal model file an API-format workflow names: known loader inputs, or any string ending in a model
    file extension."""
    out = []
    for node_id, node in sorted(workflow.items(), key=lambda kv: str(kv[0])):
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        for name, value in (node.get("inputs") or {}).items():
            if not isinstance(value, str) or not value.strip():
                continue
            folder = model_folder(class_type, name)
            if folder or _norm(value).endswith(MODEL_EXTS):
                out.append({"node_id": str(node_id), "class_type": class_type, "input": name, "file": value,
                            "folder": folder or "models"})
    return out


def check(workflow: dict[str, Any], object_info: Optional[dict[str, Any]], source: str = "live",
          label: Optional[str] = None) -> dict[str, Any]:
    """Nodes and model files of one API-format workflow against /object_info. `source` says where object_info came
    from (live, cache or none); with none every item is unknown and `ok` is None."""
    info = object_info or {}
    known = bool(info)
    classes: dict[str, list[str]] = {}
    for node_id, node in workflow.items():
        if isinstance(node, dict) and node.get("class_type"):
            classes.setdefault(str(node["class_type"]), []).append(str(node_id))
    nodes = []
    for class_type, ids in sorted(classes.items()):
        present = class_type in info if known else None
        nodes.append({"class_type": class_type, "node_ids": sorted(ids), "ok": present,
                      **({"pack": node_pack(class_type)} if present is False else {})})
    models = []
    for m in model_inputs(workflow):
        entry = dict(m)
        if not known:
            entry["ok"] = None
        elif m["class_type"] not in info:
            entry["ok"], entry["note"] = None, "its node is missing"
        elif not _is_combo(info, m["class_type"], m["input"]):
            continue  # a free-text input that happens to end in .pt: not a file ComfyUI lists
        else:
            choices = comfy_driver.combo_choices(info, m["class_type"], m["input"])
            have = {_norm(c) for c in choices}
            entry["ok"] = _norm(m["file"]) in have
            if not entry["ok"] and m["input"] == "ckpt_name":
                alt = comfy_driver.resolve_checkpoint(None, list(choices), None, "sdxl")
                if alt:
                    entry["fallback"] = alt  # Prospero picks another installed checkpoint when the default is absent
        models.append(entry)
    missing_nodes = [n for n in nodes if n["ok"] is False]
    missing_models = [m for m in models if m["ok"] is False]
    ok = None if not known else not missing_nodes and not any(not m.get("fallback") for m in missing_models)
    report = {"workflow": label, "source": source, "ok": ok, "nodes": nodes, "models": models,
              "missing_nodes": len(missing_nodes), "missing_models": len(missing_models)}
    report["copy_text"] = copy_text([report])
    return report


def copy_text(reports: list[dict[str, Any]]) -> str:
    """What is missing, as plain lines to paste into a note or a ComfyUI Manager search."""
    nodes: dict[str, Optional[dict[str, str]]] = {}
    files: dict[str, dict[str, Any]] = {}
    for r in reports:
        for n in r.get("nodes") or []:
            if n.get("ok") is False:
                nodes[n["class_type"]] = n.get("pack")
        for m in r.get("models") or []:
            if m.get("ok") is False:
                files.setdefault(m["file"], m)
    lines = []
    sources = {r.get("source") for r in reports}
    if "none" in sources:
        lines.append("ComfyUI unavailable. Requirements could not be checked for every workflow. / "
                     "ComfyUI no está disponible. No se han podido comprobar todos los flujos.")
    if "cache" in sources:
        lines.append("Cached node list. Current engine availability is unverified. / "
                     "Lista de nodos guardada. La disponibilidad actual está sin verificar.")
    if not nodes and not files:
        return "\n".join(lines) or ("Nothing missing." if reports else "No workflows to check. / No hay flujos que comprobar.")
    if nodes:
        lines.append("Missing nodes:")
        lines += [f"- {c} -> " + (f"install {p['name']} ({p['url']})" if p else "search it in ComfyUI Manager")
                  for c, p in sorted(nodes.items())]
    if files:
        lines.append("Missing model files:")
        lines += [f"- {f} -> ComfyUI/models/{m['folder']}/" + (f" (or Prospero uses {m['fallback']})" if m.get("fallback") else "")
                  for f, m in sorted(files.items())]
    return "\n".join(lines)


def check_template(name: str, data_dir: Any, object_info: Optional[dict[str, Any]], source: str = "live") -> dict[str, Any]:
    """A built-in template or an imported workflow (wf_...) by name."""
    workflow, spec = comfy_driver.load_template(name, data_dir)
    return check(workflow, object_info, source, label=spec.get("label") or spec.get("name") or name) | {"template": name}


def summary(reports: list[dict[str, Any]], source: str) -> dict[str, Any]:
    verdicts = [r["ok"] for r in reports]
    ok = False if False in verdicts else (True if verdicts and all(verdicts) else None)
    return {"ok": ok, "source": source, "workflows": reports,
            "missing_nodes": sum(r["missing_nodes"] for r in reports), "missing_models": sum(r["missing_models"] for r in reports),
            "copy_text": copy_text(reports)}


def report_source(reports: list[dict[str, Any]], default: str) -> str:
    sources = {report["source"] for report in reports}
    return "mixed" if len(sources) > 1 else next(iter(sources), default)
