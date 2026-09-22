"""Drives ComfyUI: built-in workflow templates, custom workflow import,
parameter mapping, and pre-flight VRAM/checkpoint validation.

No FastAPI here. `Driver` is used both by the HTTP API's job runner and by
tests. Workflow templates are JSON files (API format) in `workflows/`, each
with a sidecar `<name>.params.json` describing a flat `friendly_name ->
"node_id.input"` map, so the rest of the app never touches raw node ids.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

from .hoard_link.errors import BackendError

WORKFLOWS_DIR = Path(__file__).parent / "workflows"

# Node classes we know how to recognise when importing a custom workflow,
# and the friendly parameter each maps to (best effort; the user can always
# edit the proposed map before saving it).
_KNOWN_NODES: dict[str, dict[str, str]] = {
    "CheckpointLoaderSimple": {"ckpt_name": "checkpoint"},
    "ImageOnlyCheckpointLoader": {"ckpt_name": "checkpoint"},
    "CLIPTextEncode": {"text": "prompt"},
    "KSampler": {
        "seed": "seed", "steps": "steps", "cfg": "cfg",
        "sampler_name": "sampler", "scheduler": "scheduler", "denoise": "denoise",
    },
    "EmptyLatentImage": {"width": "width", "height": "height", "batch_size": "batch_size"},
    "LoadImage": {"image": "reference_image"},
    "SaveImage": {},
    "SaveAnimatedWEBP": {"fps": "fps"},
    "VHS_VideoCombine": {"frame_rate": "fps"},
}


class WorkflowError(ValueError):
    pass


class ValidationError(ValueError):
    def __init__(self, message: str, missing_checkpoint: str | None = None, available_checkpoints: list[str] | None = None):
        super().__init__(message)
        self.missing_checkpoint = missing_checkpoint
        self.available_checkpoints = available_checkpoints or []


def _looks_like_ui_format(workflow: dict) -> bool:
    return "nodes" in workflow and "links" in workflow


def list_builtin_templates() -> list[dict[str, Any]]:
    out = []
    for params_file in sorted(WORKFLOWS_DIR.glob("*.params.json")):
        spec = json.loads(params_file.read_text(encoding="utf-8"))
        out.append(spec)
    return out


def load_template(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    wf_path = WORKFLOWS_DIR / f"{name}.json"
    params_path = WORKFLOWS_DIR / f"{name}.params.json"
    if not wf_path.is_file() or not params_path.is_file():
        raise WorkflowError(f"unknown built-in workflow template '{name}'")
    workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    spec = json.loads(params_path.read_text(encoding="utf-8"))
    return workflow, spec


def propose_param_map(workflow: dict[str, Any]) -> dict[str, Any]:
    """Best-effort friendly-parameter map for an imported custom workflow."""
    if _looks_like_ui_format(workflow):
        raise WorkflowError(
            "This is the ComfyUI UI export ({'nodes': [...], 'links': [...]}), "
            "not the API format. In ComfyUI's web UI use 'Save (API Format)'."
        )
    friendly_map: dict[str, str] = {}
    output_node = None
    checkpoint_node = None
    for node_id, node in workflow.items():
        class_type = node.get("class_type")
        recognised = _KNOWN_NODES.get(class_type)
        if recognised is None:
            continue
        if class_type in ("SaveImage", "SaveAnimatedWEBP", "VHS_VideoCombine"):
            output_node = output_node or node_id
        if class_type in ("CheckpointLoaderSimple", "ImageOnlyCheckpointLoader"):
            checkpoint_node = checkpoint_node or class_type
        for input_name, friendly in recognised.items():
            if input_name not in node.get("inputs", {}):
                continue
            key = friendly
            suffix = 2
            while key in friendly_map and friendly_map[key] != f"{node_id}.{input_name}":
                key = f"{friendly}_{node_id}"
                suffix += 1
            friendly_map[key] = f"{node_id}.{input_name}"
    return {
        "template": "custom",
        "kind": "image",
        "vram_class": "sdxl",
        "map": friendly_map,
        "output_node": output_node,
        "checkpoint_node": checkpoint_node,
        "auto_detected": True,
    }


def apply_params(workflow: dict[str, Any], spec: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of `workflow` with `values` written per `spec["map"]`."""
    wf = copy.deepcopy(workflow)
    friendly_map = spec.get("map", {})
    for friendly, value in values.items():
        if value is None:
            continue
        target = friendly_map.get(friendly)
        if target is None:
            continue
        node_id, _, input_name = target.partition(".")
        if node_id not in wf:
            continue
        wf[node_id].setdefault("inputs", {})[input_name] = value
    for linked in spec.get("linked_seeds", []):
        node_id, _, input_name = linked.partition(".")
        if "seed" in values and node_id in wf:
            wf[node_id]["inputs"][input_name] = values["seed"]
    return wf


def validate_against_object_info(spec: dict[str, Any], values: dict[str, Any], object_info: dict[str, Any]) -> None:
    """Raise ValidationError with an actionable message if `values` would
    fail against the live ComfyUI (missing node class, unknown checkpoint)."""
    checkpoint_node = spec.get("checkpoint_node")
    checkpoint = values.get("checkpoint")
    if checkpoint_node and checkpoint:
        node_info = object_info.get(checkpoint_node)
        if node_info is None:
            raise ValidationError(f"ComfyUI does not have the '{checkpoint_node}' node installed")
        try:
            available = node_info["input"]["required"]["ckpt_name"][0]
        except (KeyError, IndexError, TypeError):
            available = []
        if available and checkpoint not in available:
            raise ValidationError(
                f"checkpoint '{checkpoint}' not in ComfyUI; you have: {', '.join(available)}",
                missing_checkpoint=checkpoint,
                available_checkpoints=available,
            )
    for friendly, target in spec.get("map", {}).items():
        node_id, _, _ = target.partition(".")
        # nothing to check beyond node-class presence for the checkpoint
        # loader above; a full schema check per node is out of scope.
        _ = node_id


def estimate_vram_mb(spec: dict[str, Any], vram_table: dict[str, int]) -> int:
    return vram_table.get(spec.get("vram_class", "sdxl"), 7000)
