"""ComfyUI UI-format -> API-format workflow converter.

ComfyUI's node editor exports two shapes: the **UI format** it edits
(``{"nodes": [...], "links": [...], "definitions": {"subgraphs": [...]}}``,
each node carrying its widget values positionally) and the **API format**
``/prompt`` accepts (``{node_id: {"class_type", "inputs"}}``, every value
either a literal or a ``[source_node_id, source_slot]`` link). This module
turns the first into the second using an ``object_info`` schema (live from
ComfyUI, or a cached copy) to know each node's input order and to tell a
widget value from a socket.

Handled, in order of how often real templates use them:

- **Widgets vs. links**: a schema input is a *widget* (occupies a slot in
  ``widgets_values``) when its type is ``INT``/``FLOAT``/``STRING``/
  ``BOOLEAN`` or an inline choice list; anything else (``MODEL``, ``CLIP``,
  ``CONDITIONING``, ...) is a pure socket. A widget that has been
  "converted to input" (dragged into a link) still occupies its
  ``widgets_values`` slot in ComfyUI's own export -- the link wins, the
  stale value is skipped -- so slots are always consumed positionally,
  linked or not.
- **``control_after_generate``**: ComfyUI's UI adds one extra
  ``widgets_values`` entry (``"fixed"``/``"randomize"``/...) right after
  every ``seed``/``noise_seed`` widget slot; it is consumed and discarded.
- **Collapsed "advanced" widgets**: a schema field whose ``object_info``
  config has ``"advanced": true`` (ACE-Step's ``cfg_scale``, ``top_k``, ...)
  is often left at its default and not saved to ``widgets_values`` at all.
  When the positional slots run out before the schema does, each remaining
  field falls back to its own ``default`` from ``object_info``.
- **PrimitiveNode / Reroute**: not real nodes (absent from
  ``object_info``); a link that traces back to one resolves to the
  primitive's literal value (Reroute forwards whatever feeds it).
- **Bypassed (`mode: 4`) / muted (`mode: 2`) nodes**: never get an API
  entry. A muted node's outputs resolve to nothing (the input stays
  unset -- fine when it is optional). A bypassed node passes an input of
  the *same type* straight through its matching output slot when one
  exists (ComfyUI's own bypass rule); otherwise it also resolves to
  nothing.
- **Subgraphs** (``definitions.subgraphs``): an instance node's ``type`` is
  the subgraph's id. It is expanded in place: inner node ids become
  ``"<instance_id>:<inner_id>"``, the subgraph's declared inputs
  (``origin_id == -10`` inside its own link list) are wired to whatever
  feeds the instance node in the *outer* graph, and its declared outputs
  (``target_id == -20``) become the values downstream nodes see on the
  instance's own output slots. Nesting (a subgraph instance inside another
  subgraph) is supported by recursion. A subgraph input is matched to
  the instance's socket **by name** (the instance lists only the inputs
  that have sockets, so slot numbers differ); an unlinked promoted widget
  takes its value from the instance's own ``widgets_values``, which hold
  one entry per widget-typed subgraph input in declaration order.
- **Dynamic combos** (``COMFY_DYNAMICCOMBO_V3``, e.g. ``SaveVideo.format``):
  the chosen option's own inputs follow the combo's value in
  ``widgets_values`` and are emitted flattened as ``"<combo>.<child>"``
  (``"format.codec"``), recursively -- exactly what the server validates.
- **Autogrow sockets** (``COMFY_AUTOGROW_V3``, a variable list of e.g.
  reference images): every linked ``"<name>.<slot>"`` socket on the node
  is emitted under that same flattened name.
- **Socketless widgets** (an image comparer's view) are frontend-only and
  never emitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

BYPASS_MODE = 4
MUTE_MODE = 2
DECORATIVE_TYPES = {"MarkdownNote", "Note"}
PASSTHROUGH_TYPES = {"PrimitiveNode", "Reroute"}
_WIDGET_SCALARS = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COMFY_DYNAMICCOMBO_V3"}
DYNAMIC_COMBO = "COMFY_DYNAMICCOMBO_V3"
AUTOGROW = "COMFY_AUTOGROW_V3"
SEED_NAMES = ("seed", "noise_seed")
SUBGRAPH_INPUT_ORIGIN = -10
SUBGRAPH_OUTPUT_TARGET = -20


class ConversionError(ValueError):
    """A UI workflow that cannot be converted (message is actionable)."""


Resolved = tuple  # ("link", node_id, slot) | ("value", literal)


@dataclass
class _Scope:
    nodes: dict[Any, dict[str, Any]]
    links: dict[Any, dict[str, Any]]
    subgraphs: dict[str, dict[str, Any]]
    prefix: str
    external_resolver: Optional[Callable[[int], Optional[Resolved]]]


def _prefixed(prefix: str, node_id: Any) -> str:
    return f"{prefix}{node_id}"


def _links_from_array(raw_links: list) -> dict[Any, dict[str, Any]]:
    """Top-level `links`: `[link_id, origin_id, origin_slot, target_id, target_slot, type]`."""
    out: dict[Any, dict[str, Any]] = {}
    for entry in raw_links or []:
        link_id, origin_id, origin_slot, target_id, target_slot, ltype = entry
        out[link_id] = {"origin_id": origin_id, "origin_slot": origin_slot,
                         "target_id": target_id, "target_slot": target_slot, "type": ltype}
    return out


def _links_from_subgraph(raw_links: list) -> dict[Any, dict[str, Any]]:
    """Subgraph `links`: already a list of `{id, origin_id, origin_slot, target_id, target_slot, type}`."""
    return {entry["id"]: entry for entry in raw_links or []}


def _is_widget_type(type_: Any) -> bool:
    return isinstance(type_, list) or type_ in _WIDGET_SCALARS


def _resolve(scope: _Scope, node_id: Any, slot: int) -> Optional[Resolved]:
    if node_id == SUBGRAPH_INPUT_ORIGIN:
        if scope.external_resolver is None:
            return None
        return scope.external_resolver(slot)
    node = scope.nodes.get(node_id)
    if node is None:
        return None
    ctype = node.get("type")
    if ctype in scope.subgraphs:
        return _resolve_subgraph_output(scope, node, ctype, slot)
    if ctype == "PrimitiveNode":
        values = node.get("widgets_values") or [None]
        return ("value", values[0])
    if ctype == "Reroute":
        ins = node.get("inputs") or []
        if not ins or ins[0].get("link") is None:
            return None
        link = scope.links.get(ins[0]["link"])
        if link is None:
            return None
        return _resolve(scope, link["origin_id"], link["origin_slot"])
    if ctype in DECORATIVE_TYPES:
        return None
    mode = node.get("mode", 0)
    if mode == MUTE_MODE:
        return None
    if mode == BYPASS_MODE:
        outputs = node.get("outputs") or []
        if slot >= len(outputs):
            return None
        out_type = outputs[slot].get("type")
        for inp in node.get("inputs") or []:
            if inp.get("type") == out_type and inp.get("link") is not None:
                link = scope.links.get(inp["link"])
                if link is None:
                    continue
                return _resolve(scope, link["origin_id"], link["origin_slot"])
        return None
    return ("link", _prefixed(scope.prefix, node_id), slot)


def _make_inner_scope(scope: _Scope, instance_node: dict, subgraph_id: str) -> _Scope:
    sg = scope.subgraphs[subgraph_id]
    inner_nodes = {n["id"]: n for n in sg.get("nodes", [])}
    inner_links = _links_from_subgraph(sg.get("links", []))
    instance_prefix = f"{_prefixed(scope.prefix, instance_node['id'])}:"

    sg_inputs = sg.get("inputs") or []
    instance_inputs = {i.get("name"): i for i in (instance_node.get("inputs") or [])}
    widget_names = [i.get("name") for i in sg_inputs if _is_widget_type(i.get("type"))]
    widget_values = list(instance_node.get("widgets_values") or [])
    promoted = dict(zip(widget_names, widget_values)) if len(widget_values) == len(widget_names) else {}

    def external_resolver(input_slot: int) -> Optional[Resolved]:
        if input_slot >= len(sg_inputs):
            return None
        name = sg_inputs[input_slot].get("name")
        ui_in = instance_inputs.get(name)
        if ui_in is not None and ui_in.get("link") is not None:
            link = scope.links.get(ui_in["link"])
            if link is not None:
                return _resolve(scope, link["origin_id"], link["origin_slot"])
        if name in promoted:
            return ("value", promoted[name])
        return None  # the inner node keeps its own widget value

    return _Scope(nodes=inner_nodes, links=inner_links, subgraphs=scope.subgraphs,
                  prefix=instance_prefix, external_resolver=external_resolver)


def _resolve_subgraph_output(scope: _Scope, instance_node: dict, subgraph_id: str, out_slot: int) -> Optional[Resolved]:
    inner_scope = _make_inner_scope(scope, instance_node, subgraph_id)
    for link in inner_scope.links.values():
        if link["target_id"] == SUBGRAPH_OUTPUT_TARGET and link["target_slot"] == out_slot:
            return _resolve(inner_scope, link["origin_id"], link["origin_slot"])
    return None


def _assign(inputs: dict, name: str, resolved: Resolved) -> None:
    if resolved[0] == "link":
        inputs[name] = [resolved[1], resolved[2]]
    else:
        inputs[name] = resolved[1]


def _entry(raw: Any) -> tuple[Any, dict]:
    """`[type, config]` schema entry -> (type, config dict)."""
    type_ = raw[0] if isinstance(raw, (list, tuple)) and raw else None
    cfg = raw[1] if isinstance(raw, (list, tuple)) and len(raw) > 1 and isinstance(raw[1], dict) else {}
    return type_, cfg


def _ordered_entries(spec: dict) -> list[tuple[str, Any]]:
    return list((spec.get("required") or {}).items()) + list((spec.get("optional") or {}).items())


def _build_inputs(scope: _Scope, node: dict, class_type: str, object_info: dict) -> dict[str, Any]:
    spec = (object_info.get(class_type) or {}).get("input") or {}
    ui_inputs_by_name = {i["name"]: i for i in (node.get("inputs") or [])}
    widget_values = list(node.get("widgets_values") or [])
    cursor = {"i": 0}
    out: dict[str, Any] = {}

    def resolve_link(link_id: Any) -> Optional[Resolved]:
        link = scope.links.get(link_id)
        if link is None:
            return None
        return _resolve(scope, link["origin_id"], link["origin_slot"])

    def take() -> tuple[bool, Any]:
        if cursor["i"] < len(widget_values):
            value = widget_values[cursor["i"]]
            cursor["i"] += 1
            return True, value
        return False, None

    def walk(entries: list[tuple[str, Any]], prefix: str) -> None:
        for short, raw in entries:
            name = f"{prefix}{short}"
            type_, cfg = _entry(raw)
            ui_in = ui_inputs_by_name.get(name)
            linked = ui_in is not None and ui_in.get("link") is not None
            if type_ == AUTOGROW:
                grow_prefix = f"{name}."
                for ui_name, candidate in ui_inputs_by_name.items():
                    if ui_name.startswith(grow_prefix) and candidate.get("link") is not None:
                        resolved = resolve_link(candidate["link"])
                        if resolved is not None:
                            _assign(out, ui_name, resolved)
                continue
            if cfg.get("socketless"):
                continue
            if not _is_widget_type(type_):
                if linked:
                    resolved = resolve_link(ui_in["link"])
                    if resolved is not None:
                        _assign(out, name, resolved)
                continue
            consumed, value = take()
            if consumed and (short in SEED_NAMES or cfg.get("control_after_generate")) \
                    and cursor["i"] < len(widget_values) and isinstance(widget_values[cursor["i"]], str):
                cursor["i"] += 1  # the UI-only control_after_generate widget ("fixed"/"randomize"...)
            if linked:
                resolved = resolve_link(ui_in["link"])
                if resolved is not None:
                    _assign(out, name, resolved)
                elif consumed:
                    # a promoted (subgraph or converted-to-input) socket with
                    # nothing actually feeding it falls back to the stale
                    # widget value ComfyUI kept for it, same as the live app.
                    out[name] = value
                elif "default" in cfg:
                    out[name] = cfg["default"]
            elif consumed:
                out[name] = value
            elif "default" in cfg:
                out[name] = cfg["default"]
            if type_ == DYNAMIC_COMBO and name in out and not isinstance(out[name], list):
                chosen = next((o for o in cfg.get("options") or [] if o.get("key") == out[name]), None)
                if chosen is not None:
                    walk(_ordered_entries(chosen.get("inputs") or {}), f"{name}.")

    walk(_ordered_entries(spec), "")
    return out


def _emit_graph(scope: _Scope, api: dict[str, Any], object_info: dict) -> None:
    for node_id, node in scope.nodes.items():
        ctype = node.get("type")
        if ctype in scope.subgraphs:
            inner_scope = _make_inner_scope(scope, node, ctype)
            _emit_graph(inner_scope, api, object_info)
            continue
        if not isinstance(ctype, str) or not ctype:
            raise ConversionError(f"node {node_id!r} has no node type; is this a complete ComfyUI export?")
        if ctype in DECORATIVE_TYPES or ctype in PASSTHROUGH_TYPES:
            continue
        if node.get("mode", 0) in (MUTE_MODE, BYPASS_MODE):
            continue
        if ctype not in object_info:
            raise ConversionError(f"node {node_id!r} uses class '{ctype}', which is not in this ComfyUI's /object_info")
        pid = _prefixed(scope.prefix, node_id)
        api[pid] = {"class_type": ctype, "inputs": _build_inputs(scope, node, ctype, object_info)}


def ui_to_api(ui_workflow: dict[str, Any], object_info: dict[str, Any]) -> dict[str, Any]:
    """Convert a ComfyUI UI-format export to API format. Raises
    `ConversionError` when a node's class is not in `object_info` (so the
    caller can say exactly which custom node pack is missing)."""
    if not isinstance(ui_workflow, dict) or not isinstance(ui_workflow.get("nodes"), list):
        raise ConversionError("not a UI-format workflow: expected {'nodes': [...], 'links': [...]}")
    subgraphs = {sg["id"]: sg for sg in (ui_workflow.get("definitions") or {}).get("subgraphs", [])}
    top_nodes = {n["id"]: n for n in ui_workflow["nodes"]}
    top_links = _links_from_array(ui_workflow.get("links", []))
    root = _Scope(nodes=top_nodes, links=top_links, subgraphs=subgraphs, prefix="", external_resolver=None)
    api: dict[str, Any] = {}
    _emit_graph(root, api, object_info)
    return api


def _required_names(spec: dict, values: dict, prefix: str = "") -> list[tuple[str, Any]]:
    """Required inputs the server will check, including the chosen option's
    children of every dynamic combo (flattened as ``"<combo>.<child>"``)."""
    out: list[tuple[str, Any]] = []
    for section in ("required", "optional"):
        for short, raw in (spec.get(section) or {}).items():
            name = f"{prefix}{short}"
            type_, cfg = _entry(raw)
            if section == "required":
                out.append((name, raw))
            if type_ == DYNAMIC_COMBO:
                chosen = next((o for o in cfg.get("options") or [] if o.get("key") == values.get(name)), None)
                if chosen is not None:
                    out += _required_names(chosen.get("inputs") or {}, values, f"{name}.")
    return out


def validate_converted(api_workflow: dict[str, Any], object_info: dict[str, Any]) -> list[str]:
    """Structural checks on a converted prompt: every class exists,
    every required input present (dynamic-combo children included), every
    link points to an existing node and one of its output slots.
    Returns a list of problems (empty when the workflow is sound)."""
    problems: list[str] = []
    for node_id, node in api_workflow.items():
        class_type = node.get("class_type")
        if class_type not in object_info:
            problems.append(f"node {node_id}: class '{class_type}' not in object_info")
            continue
        inputs = node.get("inputs", {})
        for name, raw in _required_names(object_info[class_type].get("input") or {}, inputs):
            type_, cfg = _entry(raw)
            if cfg.get("socketless"):
                continue
            if type_ == AUTOGROW:
                have = sum(1 for key in inputs if key.startswith(f"{name}."))
                minimum = int((cfg.get("template") or {}).get("min") or 0)
                if have < minimum:
                    problems.append(f"node {node_id} ({class_type}): '{name}' needs at least {minimum} inputs, has {have}")
                continue
            if name not in inputs:
                problems.append(f"node {node_id} ({class_type}): missing required input '{name}'")
        for name, value in inputs.items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                src_id, src_slot = value
                if src_id not in api_workflow:
                    problems.append(f"node {node_id} input '{name}': link points to missing node '{src_id}'")
                    continue
                outputs = (object_info.get(api_workflow[src_id].get("class_type")) or {}).get("output") or []
                if not isinstance(src_slot, int) or src_slot >= len(outputs):
                    problems.append(f"node {node_id} input '{name}': slot {src_slot} does not exist on node '{src_id}'")
    return problems


def _options(type_: Any, cfg: dict) -> Optional[list]:
    if isinstance(type_, list):
        return type_
    if type_ == "COMBO" and isinstance(cfg.get("options"), list):
        return cfg["options"]
    return None


def validate_values(api_workflow: dict[str, Any], object_info: dict[str, Any]) -> list[str]:
    """What ComfyUI's `/prompt` rejects on top of `validate_converted`'s
    structure: a literal that is not one of a combo's options (a model file
    that is not installed, a sampler name that does not exist, a
    dynamic-combo key) or a number outside the input's min/max. Upload
    combos (LoadImage's file list) are skipped - the file arrives with the
    job. Returns human-readable problems, empty when the prompt would queue."""
    problems = list(validate_converted(api_workflow, object_info))
    for node_id, node in api_workflow.items():
        class_type = node.get("class_type")
        spec = (object_info.get(class_type) or {}).get("input") or {}
        inputs = node.get("inputs", {})

        def check(entries: list[tuple[str, Any]], prefix: str) -> None:
            for short, raw in entries:
                name = f"{prefix}{short}"
                type_, cfg = _entry(raw)
                if name not in inputs or cfg.get("image_upload"):
                    continue
                value = inputs[name]
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    continue  # a link, checked structurally
                if type_ == DYNAMIC_COMBO:
                    keys = [o.get("key") for o in cfg.get("options") or []]
                    if value not in keys:
                        problems.append(f"node {node_id} ({class_type}): '{name}' = {value!r} is not one of {keys}")
                        continue
                    chosen = next(o for o in cfg["options"] if o.get("key") == value)
                    check(_ordered_entries(chosen.get("inputs") or {}), f"{name}.")
                    continue
                options = _options(type_, cfg)
                if options is not None and value not in options:
                    shown = ", ".join(str(o) for o in options[:8]) + (" ..." if len(options) > 8 else "")
                    problems.append(f"node {node_id} ({class_type}): '{name}' = {value!r} is not available (choices: {shown})")
                elif type_ in ("INT", "FLOAT") and isinstance(value, (int, float)) and not isinstance(value, bool):
                    if "min" in cfg and value < cfg["min"]:
                        problems.append(f"node {node_id} ({class_type}): '{name}' = {value} is below the minimum {cfg['min']}")
                    if "max" in cfg and value > cfg["max"]:
                        problems.append(f"node {node_id} ({class_type}): '{name}' = {value} is above the maximum {cfg['max']}")

        check(_ordered_entries(spec), "")
    return problems
