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
  subgraph) is supported by recursion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

BYPASS_MODE = 4
MUTE_MODE = 2
DECORATIVE_TYPES = {"MarkdownNote", "Note"}
PASSTHROUGH_TYPES = {"PrimitiveNode", "Reroute"}
_WIDGET_SCALARS = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COMFY_DYNAMICCOMBO_V3"}
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


def _type_tuple(object_info: dict, class_type: str, name: str) -> tuple[Any, dict]:
    spec = (object_info.get(class_type) or {}).get("input") or {}
    entry = (spec.get("required") or {}).get(name)
    if entry is None:
        entry = (spec.get("optional") or {}).get(name)
    if entry is None:
        return None, {}
    type_ = entry[0]
    cfg = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
    return type_, cfg


def _schema_names(object_info: dict, class_type: str) -> list[str]:
    spec = (object_info.get(class_type) or {}).get("input") or {}
    names = list((spec.get("required") or {}).keys())
    names += list((spec.get("optional") or {}).keys())
    return names


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

    def external_resolver(input_slot: int) -> Optional[Resolved]:
        ins = instance_node.get("inputs") or []
        if input_slot >= len(ins) or ins[input_slot].get("link") is None:
            return None
        link = scope.links.get(ins[input_slot]["link"])
        if link is None:
            return None
        return _resolve(scope, link["origin_id"], link["origin_slot"])

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


def _build_inputs(scope: _Scope, node: dict, class_type: str, object_info: dict) -> dict[str, Any]:
    schema = _schema_names(object_info, class_type)
    ui_inputs_by_name = {i["name"]: i for i in (node.get("inputs") or [])}
    widget_values = list(node.get("widgets_values") or [])
    wv_i = 0
    out: dict[str, Any] = {}

    def resolve_link(link_id: Any) -> Optional[Resolved]:
        link = scope.links.get(link_id)
        if link is None:
            return None
        return _resolve(scope, link["origin_id"], link["origin_slot"])

    for name in schema:
        type_, cfg = _type_tuple(object_info, class_type, name)
        ui_in = ui_inputs_by_name.get(name)
        linked = ui_in is not None and ui_in.get("link") is not None
        if not _is_widget_type(type_):
            if linked:
                resolved = resolve_link(ui_in["link"])
                if resolved is not None:
                    _assign(out, name, resolved)
            continue
        value_from_widgets = None
        consumed = False
        if wv_i < len(widget_values):
            value_from_widgets = widget_values[wv_i]
            wv_i += 1
            consumed = True
            if name in ("seed", "noise_seed") and wv_i < len(widget_values):
                wv_i += 1  # control_after_generate
        if linked:
            resolved = resolve_link(ui_in["link"])
            if resolved is not None:
                _assign(out, name, resolved)
            elif consumed:
                # a promoted (subgraph or converted-to-input) socket with
                # nothing actually feeding it falls back to the stale
                # widget value ComfyUI kept for it, same as the live app.
                out[name] = value_from_widgets
            elif "default" in cfg:
                out[name] = cfg["default"]
            continue
        if consumed:
            out[name] = value_from_widgets
        elif "default" in cfg:
            out[name] = cfg["default"]
    return out


def _emit_graph(scope: _Scope, api: dict[str, Any], object_info: dict) -> None:
    for node_id, node in scope.nodes.items():
        ctype = node.get("type")
        if ctype in scope.subgraphs:
            inner_scope = _make_inner_scope(scope, node, ctype)
            _emit_graph(inner_scope, api, object_info)
            continue
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


def validate_converted(api_workflow: dict[str, Any], object_info: dict[str, Any]) -> list[str]:
    """Structural checks on a converted prompt: every class exists,
    every required input present, every link points to an existing output.
    Returns a list of problems (empty when the workflow is sound)."""
    problems: list[str] = []
    for node_id, node in api_workflow.items():
        class_type = node.get("class_type")
        if class_type not in object_info:
            problems.append(f"node {node_id}: class '{class_type}' not in object_info")
            continue
        required = list(((object_info[class_type].get("input") or {}).get("required") or {}).keys())
        inputs = node.get("inputs", {})
        for name in required:
            if name not in inputs:
                problems.append(f"node {node_id} ({class_type}): missing required input '{name}'")
        for name, value in inputs.items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                src_id, src_slot = value
                if src_id not in api_workflow:
                    problems.append(f"node {node_id} input '{name}': link points to missing node '{src_id}'")
    return problems
