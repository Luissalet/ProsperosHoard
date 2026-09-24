"""Drives ComfyUI: built-in workflow templates, custom workflow import,
parameter mapping, and pre-flight validation against `/object_info`.

No FastAPI here. Workflow templates are JSON files in ComfyUI's **API
format** in `workflows/`, each with a sidecar `<name>.params.json` holding
a flat `friendly_name -> "node_id.input"` map, so the rest of the app never
touches raw node ids. Custom workflows imported by the user live in
`<data>/workflows/<wf_id>.json` + `.params.json` with the same shape.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .ids import new_id
from .util import now_iso

WORKFLOWS_DIR = Path(__file__).parent / "workflows"

BUILTIN_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
CUSTOM_ID_RE = re.compile(r"^wf_[0-9A-Z]{26}$")
FRIENDLY_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,40}$")

MAX_WORKFLOW_BYTES = 2 * 1024 * 1024
MAX_WORKFLOW_NODES = 400
MAX_JSON_DEPTH = 12

OUTPUT_CLASSES = {"SaveImage": "image", "SaveImageAdvanced": "image", "SaveAnimatedWEBP": "video",
                  "VHS_VideoCombine": "video", "SaveVideo": "video", "SaveAudioMP3": "audio", "SaveAudio": "audio"}
VRAM_CLASSES = ("sdxl", "sd15", "svd", "flux", "kontext", "wan", "ace", "qwen21")

# Inputs we know how to recognise when importing a custom workflow, and the
# friendly parameter each maps to. Text-encoder prompts are resolved to
# positive/negative by following the sampler's/guider's conditioning links
# upstream (see propose_param_map).
_KNOWN_INPUTS: dict[str, dict[str, str]] = {
    "CheckpointLoaderSimple": {"ckpt_name": "checkpoint"},
    "ImageOnlyCheckpointLoader": {"ckpt_name": "checkpoint"},
    "KSampler": {
        "seed": "seed", "steps": "steps", "cfg": "cfg",
        "sampler_name": "sampler", "scheduler": "scheduler", "denoise": "denoise",
    },
    "KSamplerAdvanced": {
        "noise_seed": "seed", "steps": "steps", "cfg": "cfg",
        "sampler_name": "sampler", "scheduler": "scheduler",
    },
    "SamplerCustom": {"noise_seed": "seed", "cfg": "cfg"},
    "RandomNoise": {"noise_seed": "seed"},
    "CFGGuider": {"cfg": "cfg"},
    "KSamplerSelect": {"sampler_name": "sampler"},
    "BasicScheduler": {"scheduler": "scheduler", "steps": "steps", "denoise": "denoise"},
    "EmptyLatentImage": {"width": "width", "height": "height", "batch_size": "batch_size"},
    "EmptySD3LatentImage": {"width": "width", "height": "height", "batch_size": "batch_size"},
    "LoadImage": {"image": "reference_image"},
    "SaveAnimatedWEBP": {"fps": "fps"},
    "VHS_VideoCombine": {"frame_rate": "fps"},
}


class WorkflowError(ValueError):
    """A workflow file or import that cannot be used (message is actionable)."""


class ValidationError(ValueError):
    def __init__(self, message: str, missing_checkpoint: str | None = None, available_checkpoints: list[str] | None = None):
        super().__init__(message)
        self.missing_checkpoint = missing_checkpoint
        self.available_checkpoints = available_checkpoints or []


def _looks_like_ui_format(workflow: Any) -> bool:
    return isinstance(workflow, dict) and (isinstance(workflow.get("nodes"), list) or isinstance(workflow.get("links"), list))


_HASHED_SPEC_KEYS = ("linked_params", "reference_group", "output_node")


def template_hash(workflow: dict[str, Any], spec: dict[str, Any]) -> str:
    """Version hash recorded in every recipe: changes whenever the workflow
    graph or anything that decides how values land in it changes (the
    parameter map, linked seeds/params, the reference group, the output
    node)."""
    payload = {"workflow": workflow, "map": spec.get("map", {}), "linked_seeds": spec.get("linked_seeds", []),
               "v": 2, **{k: spec.get(k) for k in _HASHED_SPEC_KEYS}}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _template_hash_v1(workflow: dict[str, Any], spec: dict[str, Any]) -> str:
    """The hash recipes recorded before linked params, reference groups and
    the output node were part of it."""
    blob = json.dumps({"workflow": workflow, "map": spec.get("map", {}), "linked_seeds": spec.get("linked_seeds", [])},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def template_hash_matches(recorded: Optional[str], workflow: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Does a recipe's recorded hash still describe this template? Either
    hash version counts, so assets made before the hash grew are not all
    reported as changed."""
    if not recorded:
        return True
    return recorded in (template_hash(workflow, spec), _template_hash_v1(workflow, spec))


# ---------------------------------------------------------------- templates

def list_builtin_templates() -> list[dict[str, Any]]:
    out = []
    for params_file in sorted(WORKFLOWS_DIR.glob("*.params.json")):
        spec = json.loads(params_file.read_text(encoding="utf-8"))
        spec["builtin"] = True
        out.append(spec)
    return out


def _custom_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "workflows"


def list_custom_workflows(data_dir: Path) -> list[dict[str, Any]]:
    d = _custom_dir(data_dir)
    if not d.is_dir():
        return []
    out = []
    for params_file in sorted(d.glob("wf_*.params.json")):
        try:
            spec = json.loads(params_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        spec["builtin"] = False
        out.append(spec)
    return out


def load_template(name: str, data_dir: Optional[Path] = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Built-in template by name (`sdxl_txt2img`) or a custom workflow by id
    (`wf_...`). Names are validated before touching the filesystem, so a
    client-supplied template can never read an arbitrary `.json` file."""
    if not isinstance(name, str):
        raise WorkflowError("template must be a string")
    if CUSTOM_ID_RE.match(name):
        if data_dir is None:
            raise WorkflowError(f"custom workflow '{name}' needs the app data folder")
        base = _custom_dir(data_dir)
    elif BUILTIN_NAME_RE.match(name):
        base = WORKFLOWS_DIR
    else:
        raise WorkflowError(f"invalid template name '{name[:60]}'")
    wf_path = base / f"{name}.json"
    params_path = base / f"{name}.params.json"
    if not wf_path.is_file() or not params_path.is_file():
        known = [t["template"] for t in list_builtin_templates()]
        raise WorkflowError(f"unknown workflow template '{name}'; built-in templates: {', '.join(known)}")
    workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    spec = json.loads(params_path.read_text(encoding="utf-8"))
    return workflow, spec


# ------------------------------------------------------------ custom import

def _json_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        return depth
    if isinstance(value, dict):
        return max([depth] + [_json_depth(v, depth + 1) for v in value.values()])
    if isinstance(value, list):
        return max([depth] + [_json_depth(v, depth + 1) for v in value])
    return depth


def validate_api_workflow(workflow: Any) -> dict[str, Any]:
    """Structural checks on an imported workflow. Raises WorkflowError with
    a message the user (or the model) can act on."""
    if _looks_like_ui_format(workflow):
        raise WorkflowError(
            "This is the ComfyUI UI export ({'nodes': [...], 'links': [...]}), not the API format. "
            "In ComfyUI enable the dev mode options and use 'Save (API Format)', then import that file."
        )
    if not isinstance(workflow, dict) or not workflow:
        raise WorkflowError("a workflow must be a JSON object of node id -> {class_type, inputs}")
    if len(workflow) > MAX_WORKFLOW_NODES:
        raise WorkflowError(f"workflow has {len(workflow)} nodes; the limit is {MAX_WORKFLOW_NODES}")
    if _json_depth(workflow) > MAX_JSON_DEPTH:
        raise WorkflowError(f"workflow JSON is nested deeper than {MAX_JSON_DEPTH} levels")
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            raise WorkflowError(f"invalid node id {str(node_id)[:40]!r}")
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), dict):
            raise WorkflowError(f"node '{node_id}' must be an object with a string 'class_type' and an 'inputs' object")
        class_type = node["class_type"]
        if not (1 <= len(class_type) <= 128) or not class_type.isprintable():
            raise WorkflowError(f"node '{node_id}' has an invalid class_type")
        for input_name, value in node["inputs"].items():
            if not isinstance(input_name, str) or len(input_name) > 80:
                raise WorkflowError(f"node '{node_id}' has an invalid input name")
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                if value[0] not in workflow:
                    raise WorkflowError(f"node '{node_id}' input '{input_name}' links to missing node '{value[0][:40]}'")
            if isinstance(value, str) and len(value) > 20000:
                raise WorkflowError(f"node '{node_id}' input '{input_name}' is longer than 20000 characters")
    return workflow


def _link_target(value: Any) -> Optional[str]:
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
        return value[0]
    return None


# Nodes whose conditioning inputs decide which prompt is positive and which
# negative: input name -> role.
_CONDITIONING_ROLES: dict[str, dict[str, str]] = {
    "KSampler": {"positive": "positive", "negative": "negative"},
    "KSamplerAdvanced": {"positive": "positive", "negative": "negative"},
    "SamplerCustom": {"positive": "positive", "negative": "negative"},
    "CFGGuider": {"positive": "positive", "negative": "negative"},
    "DualCFGGuider": {"cond1": "positive", "cond2": "positive", "negative": "negative"},
    "BasicGuider": {"conditioning": "positive"},
}
# The prompt-bearing input of a text encoder, in order of preference; the
# others listed for the same encoder get the same text (linked_params).
_PROMPT_INPUTS = ("text", "prompt", "t5xxl", "text_g", "clip_l", "text_l")


def _is_text_encoder(class_type: str) -> bool:
    return class_type.startswith("CLIPTextEncode") or class_type.startswith("TextEncode")


def _prompt_inputs(node: dict[str, Any]) -> list[str]:
    inputs = node.get("inputs") or {}
    return [name for name in _PROMPT_INPUTS if isinstance(inputs.get(name), str)]


def _encoders_upstream(workflow: dict[str, Any], start: Optional[str], limit: int = 200) -> list[str]:
    """Text encoders feeding `start` through any chain of nodes
    (FluxGuidance, ConditioningCombine, ReferenceLatent, ...), nearest
    first; the walk stops at each encoder."""
    found: list[str] = []
    queue = [start] if start else []
    seen: set[str] = set()
    while queue and len(seen) < limit:
        node_id = queue.pop(0)
        if node_id in seen or node_id not in workflow:
            continue
        seen.add(node_id)
        node = workflow[node_id]
        if _is_text_encoder(str(node.get("class_type"))) and _prompt_inputs(node):
            found.append(node_id)
            continue
        for value in (node.get("inputs") or {}).values():
            target = _link_target(value)
            if target:
                queue.append(target)
    return found


def _vram_class_for(workflow: dict[str, Any]) -> Optional[str]:
    """The VRAM class a model file name implies (UNet or checkpoint)."""
    names = []
    for node in workflow.values():
        inputs = node.get("inputs") or {}
        for key in ("unet_name", "ckpt_name"):
            if isinstance(inputs.get(key), str):
                names.append(inputs[key].lower())
    for tag, vram_class in (("kontext", "kontext"), ("wan", "wan"), ("qwen", "qwen21"), ("flux", "flux"),
                            ("ace_step", "ace"), ("svd", "svd")):
        if any(tag in name for name in names):
            return vram_class
    return None


def propose_param_map(workflow: dict[str, Any]) -> dict[str, Any]:
    """Best-effort friendly-parameter map for an imported API workflow. The
    prompt nodes are classified by walking upstream from every sampler's or
    guider's `positive`/`negative` conditioning to the text encoder that
    produces it, so the same `positive_prompt`/`negative_prompt` names work
    as for the built-in templates. A text encoder neither side reaches is
    offered as `text_1`, `text_2`... Extra seeds (a second sampler pass)
    follow the main one (`linked_seeds`), so "vary" varies every pass. The
    user can edit the map."""
    validate_api_workflow(workflow)
    friendly_map: dict[str, str] = {}
    linked_params: dict[str, list[str]] = {}
    linked_seeds: list[str] = []
    output_node = None
    kind = "image"
    checkpoint_node = None
    vram_class = "sdxl"
    reference_node = None

    roles: dict[str, str] = {}
    for sid in sorted(workflow, key=lambda k: (len(k), k)):
        node = workflow[sid]
        for input_name, role in _CONDITIONING_ROLES.get(node["class_type"], {}).items():
            for enc in _encoders_upstream(workflow, _link_target(node["inputs"].get(input_name))):
                if roles.get(enc) != "positive":  # used on both sides: it is the prompt
                    roles[enc] = role

    def put(friendly: str, target: str) -> None:
        key = friendly
        n = 2
        while key in friendly_map:
            key = f"{friendly}_{n}"
            n += 1
        friendly_map[key] = target

    def put_prompt(friendly: str, node_id: str) -> None:
        names = _prompt_inputs(workflow[node_id])
        targets = [f"{node_id}.{name}" for name in names]
        if friendly not in friendly_map:
            friendly_map[friendly] = targets[0]
            rest = targets[1:]
        else:
            rest = targets
        if rest:
            linked_params.setdefault(friendly, []).extend(rest)

    text_n = 0
    for node_id in sorted(workflow, key=lambda k: (len(k), k)):
        node = workflow[node_id]
        class_type = node["class_type"]
        inputs = node["inputs"]
        if class_type in OUTPUT_CLASSES and output_node is None:
            output_node = node_id
            kind = OUTPUT_CLASSES[class_type]
        if class_type in ("CheckpointLoaderSimple", "ImageOnlyCheckpointLoader") and checkpoint_node is None:
            checkpoint_node = class_type
            ckpt = str(inputs.get("ckpt_name", "")).lower()
            if class_type == "ImageOnlyCheckpointLoader" or "svd" in ckpt:
                vram_class = "svd"
            elif "xl" not in ckpt and ("1-5" in ckpt or "1.5" in ckpt or "v1" in ckpt or "sd15" in ckpt):
                vram_class = "sd15"
        if _is_text_encoder(class_type) and _prompt_inputs(node):
            role = roles.get(node_id)
            if role == "positive":
                put_prompt("positive_prompt", node_id)
            elif role == "negative":
                put_prompt("negative_prompt", node_id)
            else:
                for name in _prompt_inputs(node):
                    text_n += 1
                    friendly_map[f"text_{text_n}"] = f"{node_id}.{name}"
            continue
        if class_type == "LoadImage" and reference_node is None and "image" in inputs:
            reference_node = f"{node_id}.image"
            continue
        for input_name, friendly in _KNOWN_INPUTS.get(class_type, {}).items():
            if input_name in inputs and not _link_target(inputs[input_name]):
                if friendly == "seed" and "seed" in friendly_map:
                    linked_seeds.append(f"{node_id}.{input_name}")
                    continue
                put(friendly, f"{node_id}.{input_name}")
    if output_node is None:
        raise WorkflowError(
            "the workflow has no output node ComfyUI would save (SaveImage, SaveAnimatedWEBP, VHS_VideoCombine); "
            "add one in ComfyUI and export again"
        )
    # a model family named in a UNet/checkpoint file (Wan, Flux, Kontext,
    # Qwen-Image, ACE-Step) beats the SD-version guess from the name above
    vram_class = _vram_class_for(workflow) or vram_class
    spec: dict[str, Any] = {
        "kind": kind,
        "vram_class": vram_class,
        "map": friendly_map,
        "output_node": output_node,
        "checkpoint_node": checkpoint_node,
        "auto_detected": True,
    }
    if linked_seeds:
        spec["linked_seeds"] = linked_seeds
    if linked_params:
        spec["linked_params"] = linked_params
    if reference_node:
        spec["requires_reference"] = True
        spec["reference_node"] = reference_node
    return spec


def validate_param_map(workflow: dict[str, Any], spec: dict[str, Any]) -> None:
    mapping = spec.get("map")
    if not isinstance(mapping, dict) or len(mapping) > 60:
        raise WorkflowError("'map' must be an object of at most 60 friendly_name -> 'node_id.input' entries")
    for friendly, target in mapping.items():
        if not isinstance(friendly, str) or not FRIENDLY_RE.match(friendly):
            raise WorkflowError(f"invalid parameter name {str(friendly)[:40]!r}: use lowercase letters, digits and _")
        if not isinstance(target, str) or "." not in target:
            raise WorkflowError(f"parameter '{friendly}' must point to 'node_id.input_name'")
        node_id, _, input_name = target.partition(".")
        if node_id not in workflow:
            raise WorkflowError(f"parameter '{friendly}' points to node '{node_id[:40]}', which is not in the workflow")
        if input_name not in workflow[node_id]["inputs"]:
            raise WorkflowError(f"node '{node_id}' has no input '{input_name[:40]}' (parameter '{friendly}')")
    if spec.get("vram_class") not in VRAM_CLASSES:
        raise WorkflowError(f"vram_class must be one of {', '.join(VRAM_CLASSES)}")
    if spec.get("kind") not in ("image", "video", "audio"):
        raise WorkflowError("kind must be 'image', 'video' or 'audio'")
    if spec.get("output_node") not in workflow:
        raise WorkflowError("output_node must be a node id of the workflow")
    ref = spec.get("reference_node")
    if ref:
        node_id, _, input_name = str(ref).partition(".")
        if node_id not in workflow or input_name not in workflow[node_id]["inputs"]:
            raise WorkflowError("reference_node must be 'node_id.input' of a LoadImage node")


def import_custom_workflow(data_dir: Path, name: str, raw: bytes | str | dict,
                           object_info: Optional[Callable[[], dict[str, Any]]] = None) -> dict[str, Any]:
    """Import an API-format workflow, or a UI-format one (what ComfyUI's
    plain "Save"/"Export" writes) converted with `workflows.convert` against
    `object_info()` - the live `/object_info`, or the cached copy."""
    if isinstance(raw, (bytes, str)):
        if len(raw) > MAX_WORKFLOW_BYTES:
            raise WorkflowError(f"workflow file is larger than {MAX_WORKFLOW_BYTES // (1024 * 1024)} MB")
        try:
            workflow = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise WorkflowError(f"not valid JSON: {exc}") from exc
    else:
        workflow = raw
        if len(json.dumps(workflow)) > MAX_WORKFLOW_BYTES:
            raise WorkflowError(f"workflow is larger than {MAX_WORKFLOW_BYTES // (1024 * 1024)} MB")
    name = (name or "").strip()[:80] or "Imported workflow"
    converted_from_ui = False
    if _looks_like_ui_format(workflow):
        from .workflows import convert

        if object_info is None:
            raise WorkflowError("this is a ComfyUI UI export; converting it needs ComfyUI's /object_info")
        try:
            info = object_info()
        except WorkflowError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure to reach ComfyUI reads the same to the user
            raise WorkflowError(
                f"this is a ComfyUI UI export and converting it needs ComfyUI's node list, but ComfyUI is not "
                f"reachable and none is cached yet ({exc}). Start ComfyUI once, or import the 'Export (API)' file."
            ) from exc
        try:
            workflow = convert.ui_to_api(workflow, info)
        except convert.ConversionError as exc:
            raise WorkflowError(f"could not convert the UI export: {exc}") from exc
        problems = convert.validate_converted(workflow, info)
        if problems:
            raise WorkflowError("the converted workflow would not run: " + "; ".join(problems[:3]))
        converted_from_ui = True
    spec = propose_param_map(workflow)
    if converted_from_ui:
        spec["converted_from"] = "ui"
    wf_id = new_id("wf")
    spec.update({"template": wf_id, "name": name, "created_at": now_iso()})
    d = _custom_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{wf_id}.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=1), encoding="utf-8")
    (d / f"{wf_id}.params.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    spec["builtin"] = False
    return spec


def update_custom_workflow(data_dir: Path, wf_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    if not CUSTOM_ID_RE.match(wf_id or ""):
        raise WorkflowError("only imported workflows (wf_...) can be edited")
    workflow, spec = load_template(wf_id, data_dir)
    for key in ("map", "vram_class", "kind", "output_node", "reference_node", "name"):
        if key in changes and changes[key] is not None:
            spec[key] = changes[key]
    if spec.get("reference_node"):
        spec["requires_reference"] = True
    validate_param_map(workflow, spec)
    spec["auto_detected"] = False
    (_custom_dir(data_dir) / f"{wf_id}.params.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return spec


# --------------------------------------------------------------- applying

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
        if values.get("seed") is not None and node_id in wf:
            wf[node_id]["inputs"][input_name] = values["seed"]
    # one friendly value that several nodes must agree on (e.g. ACE-Step's
    # song duration lives on both the text encoder and the empty latent)
    for friendly, targets in (spec.get("linked_params") or {}).items():
        if values.get(friendly) is None:
            continue
        for linked in targets:
            node_id, _, input_name = linked.partition(".")
            if node_id in wf:
                wf[node_id].setdefault("inputs", {})[input_name] = values[friendly]
    return wf


def wire_reference_group(workflow: dict[str, Any], spec: dict[str, Any], filenames: list[str]) -> dict[str, Any]:
    """Mutate `workflow` in place for a multi-reference template's
    `reference_group` (an autogrow socket like Qwen-Image 2.1's
    `images.image_1..N`, see `workflows/convert.py`): add or reuse a
    `LoadImage` node per filename, wired into the encoder's `images.image_i`
    input in order, and drop any of the template's declared slots this call
    does not use (both the node and its autogrow link) so the graph only
    ever carries as many references as it was actually given. Returns
    `workflow` for convenience."""
    group = spec.get("reference_group")
    if not group:
        return workflow
    encode_node, prefix, nodes = group["encode_node"], group["prefix"], list(group.get("nodes") or [])
    max_count = int(group.get("max", len(nodes)))
    while len(nodes) < max_count:
        nodes.append(f"{encode_node}_ref{len(nodes) + 1}")
    encode_inputs = workflow[encode_node]["inputs"]
    for i, filename in enumerate(filenames[:max_count]):
        node_id = nodes[i]
        if node_id in workflow:
            workflow[node_id]["inputs"]["image"] = filename
        else:
            workflow[node_id] = {"class_type": "LoadImage", "inputs": {"image": filename}}
        encode_inputs[f"{prefix}{i + 1}"] = [node_id, 0]
    for i in range(len(filenames), max_count):
        workflow.pop(nodes[i], None)
        encode_inputs.pop(f"{prefix}{i + 1}", None)
    return workflow


def default_value(workflow: dict[str, Any], spec: dict[str, Any], friendly: str) -> Any:
    target = spec.get("map", {}).get(friendly)
    if not target:
        return None
    node_id, _, input_name = target.partition(".")
    return (workflow.get(node_id) or {}).get("inputs", {}).get(input_name)


def _choices(object_info: dict[str, Any], class_type: str, input_name: str) -> list[str]:
    try:
        spec = object_info[class_type]["input"]
        entry = (spec.get("required") or {}).get(input_name) or (spec.get("optional") or {}).get(input_name)
        first = entry[0]
        return list(first) if isinstance(first, list) else []
    except (KeyError, IndexError, TypeError, AttributeError):
        return []


_MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")


def _strip_model_ext(name: str) -> str:
    low = name.strip().lower().replace("\\", "/")
    for ext in _MODEL_EXTS:
        if low.endswith(ext):
            return low[: -len(ext)]
    return low


def resolve_checkpoint(requested: Optional[str], available: list[str], template_default: Optional[str], vram_class: str) -> Optional[str]:
    """Exact name, else the same name with/without extension (a style preset
    saying `sd_xl_base_1.0` finds `sd_xl_base_1.0.safetensors`), else the
    template's own default when installed. None when nothing matches."""
    if not available:
        return requested or template_default
    if requested:
        if requested in available:
            return requested
        want = _strip_model_ext(requested)
        for name in available:
            if _strip_model_ext(name) == want:
                return name
        return None
    if template_default and template_default in available:
        return template_default
    family = {"sdxl": ("xl",), "sd15": ("1-5", "1.5", "v1"), "svd": ("svd",)}.get(vram_class, ())
    for name in available:
        if any(tag in name.lower() for tag in family):
            return name
    return None


def validate_against_object_info(spec: dict[str, Any], values: dict[str, Any], object_info: dict[str, Any],
                                  workflow: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Raise ValidationError with an actionable message when the workflow
    would fail on the live ComfyUI: a node class that is not installed, a
    checkpoint, sampler or scheduler it does not know. Returns `values` with
    the checkpoint resolved to the exact installed file name.

    `values["checkpoint_preferred"]` (a style preset's checkpoint) is only
    a preference: used when installed, else the template's default or a
    same-family checkpoint is used instead. It is never returned."""
    values = dict(values)
    preferred = values.pop("checkpoint_preferred", None)
    if not object_info:
        if preferred and not values.get("checkpoint"):
            values["checkpoint"] = preferred
        return values
    if workflow:
        missing = sorted({n["class_type"] for n in workflow.values() if n["class_type"] not in object_info})
        if missing:
            raise ValidationError(
                f"ComfyUI does not have these nodes installed: {', '.join(missing)}. "
                "Install the custom node pack that provides them, or use a built-in template."
            )
    checkpoint_node = spec.get("checkpoint_node")
    if checkpoint_node:
        if checkpoint_node not in object_info:
            raise ValidationError(f"ComfyUI does not have the '{checkpoint_node}' node installed")
        available = _choices(object_info, checkpoint_node, "ckpt_name")
        requested = values.get("checkpoint")
        default = default_value(workflow or {}, spec, "checkpoint")
        resolved = None
        if not requested and preferred:
            resolved = resolve_checkpoint(preferred, available, None, spec.get("vram_class", "sdxl"))
        if resolved is None:
            resolved = resolve_checkpoint(requested, available, default, spec.get("vram_class", "sdxl"))
        if resolved is None:
            raise ValidationError(
                f"checkpoint '{requested or default}' not in ComfyUI; you have: {', '.join(available) or 'none'}",
                missing_checkpoint=requested or default, available_checkpoints=available,
            )
        values["checkpoint"] = resolved
    for friendly, input_name in (("sampler", "sampler_name"), ("scheduler", "scheduler")):
        value = values.get(friendly)
        target = spec.get("map", {}).get(friendly)
        if not value or not target or not workflow:
            continue
        node_id = target.partition(".")[0]
        class_type = (workflow.get(node_id) or {}).get("class_type")
        options = _choices(object_info, class_type, input_name) if class_type else []
        if options and value not in options:
            raise ValidationError(f"{friendly} '{value}' is not available in ComfyUI; choose one of: {', '.join(options[:30])}")
    return values


_WANTS_RE = re.compile(r"""wants ['"]([^'"]+)['"]""")


def template_readiness(object_info: dict[str, Any]) -> dict[str, Any]:
    """For every built-in template: "ready" when its default prompt would
    queue on this ComfyUI, else {"missing": [...]} naming the node classes
    or model files it lacks (or {"invalid": [...]}, the first problems, for
    anything else). A checkpoint template counts as ready when any
    checkpoint of its family is installed, as a job would pick it. Empty
    when there is no node list to check against."""
    from .workflows import convert

    if not object_info:
        return {}
    out: dict[str, Any] = {}
    for listed in list_builtin_templates():
        name = listed.get("template")
        try:
            workflow, spec = load_template(name)
        except (WorkflowError, TypeError):
            continue
        classes = sorted({n.get("class_type") for n in workflow.values() if n.get("class_type") not in object_info})
        if classes:
            out[name] = {"missing": classes[:6]}
            continue
        missing: list[str] = []
        values = dict(spec.get("defaults") or {})
        try:
            values = validate_against_object_info(spec, values, object_info, workflow)
        except ValidationError as exc:
            if exc.missing_checkpoint:
                missing.append(exc.missing_checkpoint)
        wf = apply_params(workflow, spec, values)
        group = spec.get("reference_group")
        if group:
            wire_reference_group(wf, spec, ["reference.png"] * max(1, int(group.get("min", 1))))
        invalid: list[str] = []
        for problem in convert.validate_values(wf, object_info):
            wanted = _WANTS_RE.search(problem) if problem.startswith("model not installed") else None
            if wanted:
                if wanted.group(1) not in missing:
                    missing.append(wanted.group(1))
            else:
                invalid.append(problem[:160])
        if missing:
            out[name] = {"missing": missing[:6]}
        elif invalid:
            out[name] = {"invalid": invalid[:2]}
        else:
            out[name] = "ready"
    return out


def estimate_vram_mb(spec: dict[str, Any], vram_table: dict[str, int]) -> int:
    return int(vram_table.get(spec.get("vram_class", "sdxl"), 7000))
