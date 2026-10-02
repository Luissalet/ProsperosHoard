"""Spaces: a node canvas for making pictures, clips and songs by wiring
references into generators - the open, local take on the infinite-canvas
studios.

A space has a `graph` the person draws and a `state` runs write (see
store.create_space):

    graph = {"nodes": [{"id", "type", "x", "y", "data": {...}}],
             "edges": [{"id", "source", "source_handle", "target", "target_handle"}],
             "viewport": {...}}
    state = {node_id: {"status", "jobs", "outputs", "runs", "error", "hash", ...}}

Node types, their inputs (handle -> accepted type) and output type:

    text   - a prompt or a style text                      -> text
    asset  - images, clips or songs of the library         -> their kind
    cast   - a character, place or object of the cast       -> image (its reference) + text ("@Name")
    image  - prompt (text, many), refs (image, many)        -> image   (Qwen-Image multi-reference)
    video  - start (image, many = one clip each), prompt (text, many),
             motion (video: copy its moves), audio (audio: sing it)
                                                            -> video   (Wan 2.2 / Animate / S2V)
    music  - prompt (text, many)                            -> audio   (ACE-Step)
    list   - items (anything, many)                         -> what it holds, minus unticked items
    note   - a sticky note                                  -> nothing
    assistant - prompt (text, many): the local language model writes
             a text, or a list of texts (one per line)       -> text (a list fans out downstream)
    edit   - image (image, many): upscale x2/x4 or remove the
             background of each                             -> image
    combine - clips (video, many) joined in order, audio (audio)
                                                            -> video
  A clip node also hands on the last frame of each clip ("last" handle,
  image), so shot N+1 can start where shot N ended. Texts that come from
  a list or a list-mode assistant into a picture's or a clip's prompt fan
  out: one render per item (with the other texts in common).

Running a node turns its resolved inputs into the studio's own operations
(generate, compose), waits for their jobs and records the outputs; "all"
runs every generator in dependency order and skips the ones whose inputs
and settings did not change since their last good run. Pure logic over
a `Studio` (the same adapter productions use): no FastAPI here."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable, Optional

from . import cinema
from .jobs import JobCancelled
from .store import NotFound, Store
from .util import now_iso

NODE_TYPES = ("text", "asset", "cast", "image", "video", "music", "list", "note", "assistant", "edit", "combine",
              "variations", "group")
GENERATORS = ("image", "video", "music", "assistant", "edit", "combine", "variations")
INPUTS: dict[str, dict[str, str]] = {
    "image": {"prompt": "text", "refs": "image", "pose": "image", "layout": "image"},
    "video": {"start": "image", "end": "image", "prompt": "text", "motion": "video", "audio": "audio"},
    "music": {"prompt": "text"},
    "list": {"items": "any"},
    "assistant": {"prompt": "text"},
    "edit": {"image": "image"},
    "combine": {"clips": "video", "audio": "audio"},
    "variations": {"image": "image", "prompt": "text"},
}
SINGLE_INPUTS = {("video", "motion"), ("video", "audio"), ("video", "end"), ("combine", "audio"), ("image", "pose"),
                 ("image", "layout")}
VARIATIONS: dict[str, list[str]] = {
    "angles": ["front view, facing the camera", "three-quarter view from the left", "profile view from the right",
               "seen from behind", "low angle looking up at it", "high angle looking down at it",
               "close-up of the face", "full body wide shot", "over-the-shoulder view"],
    "expressions": ["a warm smile", "laughing out loud", "surprised, eyes wide", "angry, frowning", "sad, eyes wet",
                    "scared", "thoughtful, looking away", "determined", "a smirk"],
    "ages": ["as a child of about eight", "as a teenager", "in their twenties", "in their forties", "in their sixties",
             "in their eighties", "as a toddler", "in their thirties", "in their fifties"],
    "lighting": ["at golden hour", "at night under neon light", "in soft overcast daylight", "lit by a single candle",
                 "in harsh midday sun", "under cold blue moonlight", "backlit, rim light", "in a foggy morning",
                 "under stage spotlights"],
    "storyboard": ["the opening moment", "a moment later", "the action builds", "the turning point", "the reaction",
                   "the climax", "the aftermath", "a quiet beat", "the final image"],
}
MAX_FANOUT = 24
MAX_NODES, MAX_EDGES = 300, 900
SHEET_SUFFIX = ("character turnaround reference sheet: full body front view, full body side view and a face close-up, "
                "side by side on a plain light grey studio background, the same character in all three, even light")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class SpaceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------ validation

def output_type(node: dict[str, Any], store: Optional[Store] = None, handle: Optional[str] = None) -> Optional[str]:
    t = node.get("type")
    if t == "text":
        return "text"
    if t == "cast":
        return "text" if handle == "text" else "image"
    if t == "video":
        return "image" if handle == "last" else "video"
    if t in ("image", "edit", "variations"):
        return "image"
    if t == "assistant":
        return "text"
    if t == "combine":
        return "video"
    if t == "music":
        return "audio"
    if t == "asset":
        return (node.get("data") or {}).get("kind") or "image"
    if t == "list":
        return "any"
    return None


def validate_graph(graph: Any) -> dict[str, Any]:
    """A clean copy of a drawn graph, or SpaceError: known node types,
    unique ids, wires between existing nodes into a real input of a type it
    accepts, one wire per single input."""
    if not isinstance(graph, dict):
        raise SpaceError("bad_graph", "graph must be an object with nodes and edges")
    nodes, edges = graph.get("nodes") or [], graph.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise SpaceError("bad_graph", "nodes and edges must be lists")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise SpaceError("bad_graph", f"a space holds at most {MAX_NODES} nodes and {MAX_EDGES} wires")
    by_id: dict[str, dict[str, Any]] = {}
    clean_nodes = []
    for n in nodes:
        if not isinstance(n, dict) or not _ID.match(str(n.get("id") or "")) or n.get("type") not in NODE_TYPES:
            raise SpaceError("bad_node", f"each node needs an id and a type ({', '.join(NODE_TYPES)})")
        if n["id"] in by_id:
            raise SpaceError("bad_node", f"two nodes are called {n['id']}")
        data = n.get("data") if isinstance(n.get("data"), dict) else {}
        if len(json.dumps(data)) > 20000:
            raise SpaceError("bad_node", f"node {n['id']} carries too much data")
        clean = {"id": n["id"], "type": n["type"], "x": float(n.get("x") or 0), "y": float(n.get("y") or 0), "data": data}
        for k in ("w", "h"):
            if n.get(k) is not None:
                clean[k] = float(n[k])
        by_id[n["id"]] = clean
        clean_nodes.append(clean)
    clean_edges, single_taken = [], set()
    for e in edges:
        if not isinstance(e, dict):
            raise SpaceError("bad_edge", "each wire is an object")
        src, dst = by_id.get(str(e.get("source"))), by_id.get(str(e.get("target")))
        handle = str(e.get("target_handle") or "")
        if src is None or dst is None or src["id"] == dst["id"]:
            raise SpaceError("bad_edge", "a wire joins two different nodes of the space")
        accepts = INPUTS.get(dst["type"], {}).get(handle)
        if accepts is None:
            raise SpaceError("bad_edge", f"{dst['type']} nodes have no input '{handle}'")
        produced = output_type(src, handle=e.get("source_handle"))
        if produced is None:
            raise SpaceError("bad_edge", f"{src['type']} nodes have no output")
        if accepts != "any" and produced not in (accepts, "any"):
            raise SpaceError("bad_edge", f"the {handle} input of a {dst['type']} node takes {accepts}, not {produced}")
        if (dst["type"], handle) in SINGLE_INPUTS:
            if (dst["id"], handle) in single_taken:
                raise SpaceError("bad_edge", f"the {handle} input takes one wire")
            single_taken.add((dst["id"], handle))
        clean_edges.append({"id": str(e.get("id") or f"{src['id']}-{dst['id']}-{handle}")[:120], "source": src["id"],
                            "source_handle": e.get("source_handle"), "target": dst["id"], "target_handle": handle})
    _check_acyclic(clean_nodes, clean_edges)
    out = {"nodes": clean_nodes, "edges": clean_edges}
    if isinstance(graph.get("viewport"), dict):
        out["viewport"] = {k: float(v) for k, v in graph["viewport"].items() if k in ("x", "y", "zoom")}
    return out


def _check_acyclic(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    order = topo_order(nodes, edges)
    if len(order) != len(nodes):
        raise SpaceError("cycle", "the wires make a loop; a node cannot feed itself")


def topo_order(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
    incoming = {n["id"]: 0 for n in nodes}
    children: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        incoming[e["target"]] += 1
        children[e["source"]].append(e["target"])
    ready = [n["id"] for n in nodes if incoming[n["id"]] == 0]
    out = []
    while ready:
        nid = ready.pop(0)
        out.append(nid)
        for c in children[nid]:
            incoming[c] -= 1
            if incoming[c] == 0:
                ready.append(c)
    return out


def downstream(graph: dict[str, Any], node_id: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for e in graph["edges"]:
        children.setdefault(e["source"], []).append(e["target"])
    seen, todo = {node_id}, [node_id]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return seen


# ------------------------------------------------------------ resolution

def node_outputs(store: Store, graph: dict[str, Any], state: dict[str, Any], node_id: str,
                 handle: Optional[str] = None) -> list[tuple[str, str]]:
    """What a node hands downstream, as (type, value): text, or an asset id
    with its kind. A generator gives its current outputs minus the ones
    unticked (`state.excluded`)."""
    node = next((n for n in graph["nodes"] if n["id"] == node_id), None)
    if node is None:
        return []
    data = node.get("data") or {}
    t = node["type"]
    if t == "text":
        text = str(data.get("text") or "").strip()
        return [("text", text)] if text else []
    if t == "cast":
        cid = data.get("character_id")
        try:
            char = store.get_character(cid) if cid else None
        except NotFound:
            char = None
        if not char:
            return []
        if handle == "text":
            return [("text", f"@{char['name']}")]
        return [("image", char["canonical_asset_id"])] if char.get("canonical_asset_id") else []
    if t == "asset":
        out = []
        for aid in data.get("asset_ids") or []:
            try:
                a = store.get_asset(aid)
            except NotFound:
                continue
            out.append((a["kind"], a["id"]))
        return out
    if t == "assistant":
        st = state.get(node_id) or {}
        excluded = set(st.get("excluded") or [])
        return [("text", x) for x in st.get("texts") or [] if x not in excluded]
    if t in GENERATORS:
        st = state.get(node_id) or {}
        excluded = set(st.get("excluded") or [])
        kept = [a for a in st.get("outputs") or [] if a not in excluded]
        if t == "video" and handle == "last":
            frames = st.get("last_frames") or {}
            return [("image", frames[a]) for a in kept if frames.get(a)]
        kind = {"music": "audio", "edit": "image", "combine": "video", "variations": "image"}.get(t, t)
        return [(kind, a) for a in kept]
    if t == "list":
        items: list[tuple[str, str]] = []
        for e in graph["edges"]:
            if e["target"] == node_id:
                items += node_outputs(store, graph, state, e["source"], e.get("source_handle"))
        unticked = set(data.get("unticked") or [])
        seen, out = set(), []
        for kind, v in items:
            if v in unticked or (kind, v) in seen:
                continue
            seen.add((kind, v))
            out.append((kind, v))
        return out
    return []


def inputs_of(store: Store, graph: dict[str, Any], state: dict[str, Any], node_id: str) -> dict[str, list[tuple[str, str]]]:
    got: dict[str, list[tuple[str, str]]] = {}
    for e in graph["edges"]:
        if e["target"] != node_id:
            continue
        values = node_outputs(store, graph, state, e["source"], e.get("source_handle"))
        accepts = INPUTS.get(next(n["type"] for n in graph["nodes"] if n["id"] == node_id), {}).get(e["target_handle"])
        if accepts and accepts != "any":
            values = [(k, v) for k, v in values if k == accepts]
        got.setdefault(e["target_handle"], []).extend(values)
    return got


def _texts(values: list[tuple[str, str]]) -> list[str]:
    return [v for k, v in values if k == "text" and v]


def _fans_out(graph: dict[str, Any], node_id: str) -> bool:
    node = next((n for n in graph["nodes"] if n["id"] == node_id), None)
    if not node:
        return False
    if node["type"] == "list":
        return True
    return node["type"] == "assistant" and bool((node.get("data") or {}).get("as_list"))


def prompt_parts(store: Store, graph: dict[str, Any], state: dict[str, Any], node_id: str) -> tuple[list[str], list[str]]:
    """A generator's prompt texts split into the ones every render shares
    and the ones it fans out over (one render each): texts that arrive from
    a list or from a list-mode assistant."""
    common: list[str] = []
    items: list[str] = []
    for e in graph["edges"]:
        if e["target"] != node_id or e["target_handle"] != "prompt":
            continue
        texts = _texts(node_outputs(store, graph, state, e["source"], e.get("source_handle")))
        (items if _fans_out(graph, e["source"]) else common).extend(texts)
    return common, list(dict.fromkeys(items))[:MAX_FANOUT]


def _refs_note(n: int) -> str:
    if not n:
        return ""
    tags = ", ".join(f"<image{i + 1}>" for i in range(n))
    return (f" Use the reference images {tags}: keep each person, creature, place and object exactly as drawn in them "
            "(same design, colours and proportions).")


def plan_node(store: Store, graph: dict[str, Any], state: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    """The operations one run of a generator node makes: [{"op": "generate"
    | "compose", "body": {...}}]. Raises SpaceError when an input it needs
    is missing."""
    node = next((n for n in graph["nodes"] if n["id"] == node_id), None)
    if node is None:
        raise SpaceError("no_node", f"there is no node {node_id}")
    data = node.get("data") or {}
    ins = inputs_of(store, graph, state, node_id)
    t = node["type"]
    own = str(data.get("prompt") or "").strip()
    if t == "image":
        common, items = prompt_parts(store, graph, state, node_id)
        refs = list(dict.fromkeys(v for k, v in ins.get("refs", []) if k == "image"))[:8]
        pose = next((v for k, v in ins.get("pose", []) if k == "image"), None)
        layout = next((v for k, v in ins.get("layout", []) if k == "image"), None)
        if not common and not items and not own and not refs:
            raise SpaceError("needs_prompt", "write what the picture shows or wire a text into its prompt")
        ops = []
        for item in items or [None]:
            texts = common + ([item] if item else []) + ([own] if own else [])
            prompt = ". ".join(texts) or "the subject of the reference image"
            cam = cinema.camera_prompt(data.get("camera"), video=False)
            if cam and data.get("preset") != "sheet":
                prompt = f"{prompt}, {cam}"
            if data.get("preset") == "sheet":
                prompt = f"{prompt}. {SHEET_SUFFIX}"
            if refs and "<image" not in prompt:
                prompt += _refs_note(len(refs))
            all_refs = list(refs)
            if pose:
                all_refs.append(pose)
                prompt += (f" <image{len(all_refs)}> is a pose map: give the person exactly that body pose, head angle and "
                           "hand positions, and take nothing else from it.")
            if layout:
                all_refs.append(layout)
                prompt += (f" <image{len(all_refs)}> is a depth map of the layout: keep that composition, camera angle and "
                           "the depth of every shape, and take no colours from it.")
            body: dict[str, Any] = {"prompt": prompt[:3900], "count": max(1, min(int(data.get("count") or 1), 8)),
                                    "aspect": data.get("aspect") or ("16:9" if data.get("preset") == "sheet" else "1:1"),
                                    "engine": data.get("engine") or None}
            if data.get("seed") not in (None, ""):
                body["seed"] = int(data["seed"])
            if all_refs:
                body["reference_asset_ids"] = all_refs
            ops.append({"op": "generate", "body": body})
        return ops
    if t == "video":
        starts = list(dict.fromkeys(v for k, v in ins.get("start", []) if k == "image"))
        if not starts:
            raise SpaceError("needs_start", "a clip starts from a picture: wire an image into its start")
        common, items = prompt_parts(store, graph, state, node_id)
        cam = cinema.camera_prompt(data.get("camera"), video=True)
        audio = next((v for k, v in ins.get("audio", []) if k == "audio"), None)
        motion = next((v for k, v in ins.get("motion", []) if k == "video"), None)
        end = next((v for k, v in ins.get("end", []) if k == "image"), None)
        quality = data.get("quality") or "final"
        ops = []
        pairs = [(st_, it) for st_ in starts for it in (items or [None])][:MAX_FANOUT]
        for i, (start, item) in enumerate(pairs):
            texts = common + ([item] if item else []) + ([own] if own else [])
            prompt = ". ".join(texts) or "subtle natural motion"
            if cam:
                prompt = f"{prompt}, {cam}"
            body = {"prompt": prompt[:3900], "reference_asset_id": start, "count": 1}
            if data.get("seed") not in (None, ""):
                body["seed"] = int(data["seed"]) + i
            if audio:
                engine_pick = data.get("sing_engine") or "auto"
                longest = 19.0 if engine_pick == "s2v" else 90.0
                seconds = max(1.0, min(float(data.get("seconds") or 4.8), longest))
                body.update({"template": "auto_sing", "audio_asset_id": audio,
                             "audio_start_s": max(0.0, float(data.get("audio_start_s") or 0)), "audio_seconds": seconds,
                             **({"template_params": {"sing_engine": engine_pick}} if engine_pick != "auto" else {})})
            elif motion:
                body.update({"template": "auto_clip", "driving_asset_id": motion,
                             "driving_start_s": max(0.0, float(data.get("motion_start_s") or 0))})
            elif end:
                body.update({"template": "auto_clip", "end_asset_id": end})
            else:
                body["template"] = "wan22_ti2v" if quality == "draft" else "auto_clip"
            ops.append({"op": "generate", "body": body})
        return ops
    if t == "music":
        texts = _texts(ins.get("prompt", []))
        tags = str(data.get("tags") or "").strip() or ". ".join(texts)
        if not tags:
            raise SpaceError("needs_prompt", "describe the sound (genre, tempo, instruments, voice)")
        body = {"tags": tags[:1000], "lyrics": str(data.get("lyrics") or "[Instrumental]")[:4000],
                "duration": max(10, min(int(float(data.get("duration") or 60)), 240)),
                "count": max(1, min(int(data.get("count") or 1), 4))}
        if data.get("bpm"):
            body["bpm"] = int(data["bpm"])
        return [{"op": "compose", "body": body}]
    if t == "assistant":
        texts = _texts(ins.get("prompt", [])) + ([own] if own else [])
        if not texts:
            raise SpaceError("needs_prompt", "tell the assistant what to write, or wire texts into it")
        return [{"op": "chat", "body": {"instruction": "\n\n".join(texts)[:6000], "as_list": bool(data.get("as_list")),
                                         "items": max(1, min(int(data.get("items") or 5), MAX_FANOUT))}}]
    if t == "edit":
        images = list(dict.fromkeys(v for k, v in ins.get("image", []) if k == "image"))[:MAX_FANOUT]
        if not images:
            raise SpaceError("needs_image", "wire pictures into it")
        op = data.get("operation") or "upscale"
        if op not in ("upscale", "remove_background", "pose_map", "depth_map"):
            raise SpaceError("bad_operation", "edit nodes upscale, remove the background or make a pose or depth map")
        body_extra = {"scale": 4 if str(data.get("scale")) == "4" else 2} if op == "upscale" else {}
        return [{"op": "edit", "body": {"asset_id": a, "operation": op, **body_extra}} for a in images]
    if t == "combine":
        clips = [v for k, v in ins.get("clips", []) if k == "video"]
        if not clips:
            raise SpaceError("needs_clips", "wire clips into it, in the order they play")
        audio = next((v for k, v in ins.get("audio", []) if k == "audio"), None)
        return [{"op": "combine", "body": {"clips": clips[:60], "audio_asset_id": audio,
                                            "audio_start_s": max(0.0, float(data.get("audio_start_s") or 0))}}]
    if t == "variations":
        images = list(dict.fromkeys(v for k, v in ins.get("image", []) if k == "image"))[:4]
        if not images:
            raise SpaceError("needs_image", "wire the picture to vary")
        mode = data.get("mode") or "angles"
        extra = ". ".join(_texts(ins.get("prompt", [])) + ([own] if own else []))
        if mode == "custom":
            wanted = [x.strip() for x in str(data.get("custom") or "").splitlines() if x.strip()]
            if not wanted:
                raise SpaceError("needs_prompt", "write one change per line")
        else:
            wanted = VARIATIONS.get(mode) or VARIATIONS["angles"]
        count = max(1, min(int(data.get("count") or 4), len(wanted), 9))
        ops = []
        for img in images:
            for i, change in enumerate(wanted[:count]):
                if mode == "storyboard":
                    what = f"storyboard panel {i + 1} of {count}, {change}" + (f": {extra}" if extra else "")
                else:
                    what = change + (f". {extra}" if extra else "")
                prompt = (f"Keep everything about <image1> exactly the same - the identity, design, clothes, colours and "
                          f"art style - and change only this: {what}.")
                ops.append({"op": "generate", "body": {"prompt": prompt[:3900], "count": 1, "reference_asset_ids": [img],
                                                       "aspect": data.get("aspect") or None,
                                                       **({"seed": int(data["seed"]) + i} if data.get("seed") not in (None, "") else {})}})
        return ops
    raise SpaceError("not_runnable", f"{t} nodes do not run")


def plan_hash(ops: list[dict[str, Any]]) -> str:
    return hashlib.sha1(json.dumps(ops, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- runs

def run_order(graph: dict[str, Any], mode: str, node_ids: Optional[list[str]] = None) -> list[str]:
    """The generators to run, in dependency order: the given nodes only
    ("node"), them and everything fed by them ("downstream"), or the whole
    space ("all")."""
    types = {n["id"]: n["type"] for n in graph["nodes"]}
    order = topo_order(graph["nodes"], graph["edges"])
    if mode == "all":
        wanted = set(types)
    elif mode == "downstream":
        wanted = set().union(*(downstream(graph, n) for n in node_ids or [])) if node_ids else set()
    else:
        wanted = set(node_ids or [])
    missing = [n for n in node_ids or [] if n not in types]
    if missing:
        raise SpaceError("no_node", f"no node {', '.join(missing)} in this space")
    return [n for n in order if n in wanted and types[n] in GENERATORS]


def _levels(graph: dict[str, Any], order: list[str]) -> list[list[str]]:
    """The generators to run grouped in waves: a node runs in the wave after
    the last generator it depends on (through any chain of other nodes), so
    independent pictures, songs and clips are queued together and a render
    pool works on them at once."""
    parents: dict[str, set[str]] = {n["id"]: set() for n in graph["nodes"]}
    for e in graph["edges"]:
        parents[e["target"]].add(e["source"])
    gens = {n["id"] for n in graph["nodes"] if n["type"] in GENERATORS}
    wanted = set(order)
    memo: dict[str, int] = {}

    def level(nid: str) -> int:
        if nid in memo:
            return memo[nid]
        best = 0
        for p in parents.get(nid, ()):
            up = level(p)
            best = max(best, up + 1 if (p in gens and p in wanted) else up)
        memo[nid] = best
        return best

    waves: dict[int, list[str]] = {}
    for nid in order:
        waves.setdefault(level(nid), []).append(nid)
    return [waves[k] for k in sorted(waves)]


def _wants_last_frames(graph: dict[str, Any], node_id: str) -> bool:
    return any(e["source"] == node_id and e.get("source_handle") == "last" for e in graph["edges"])


def _split_lines(text: str, limit: int) -> list[str]:
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", line).strip().strip('"')
        if line:
            out.append(line)
    return out[:limit]


def run_space(store: Store, studio: Any, space_id: str, mode: str = "node", node_ids: Optional[list[str]] = None,
              progress: Optional[Callable[..., None]] = None, force: bool = False,
              poll_s: float = 1.0, timeout_s: float = 6 * 3600) -> dict[str, Any]:
    """Run generators of a space wave by wave (see _levels): every node of a
    wave is planned and queued, then the wave waits for its jobs, so what a
    wave makes feeds the next one in the same run. "all" without `force`
    skips a node whose plan did not change since its last good run. A
    cancelled run cancels the jobs it queued. Returns {"ran", "skipped",
    "failed"}."""
    space = store.get_space(space_id)
    graph = space["graph"]
    pid = space["project_id"]
    order = run_order(graph, mode, node_ids)
    waves = _levels(graph, order)
    ran: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    started: dict[str, dict[str, Any]] = {}

    def tick(msg: str) -> None:
        if progress:
            done = len(ran) + len(skipped) + len(failed)
            progress(done / max(1, len(order)), msg)

    try:
        for wave_no, wave in enumerate(waves):
            tick(f"wave {wave_no + 1}/{len(waves)}: {', '.join(wave)}")
            started.clear()
            for nid in wave:
                state = store.get_space(space_id)["state"]
                try:
                    ops = plan_node(store, graph, state, nid)
                except SpaceError as exc:
                    store.patch_space_state(space_id, nid, {"status": "failed", "error": exc.message, "jobs": []})
                    failed.append(nid)
                    continue
                h = plan_hash(ops)
                prev = state.get(nid) or {}
                if mode == "all" and not force and prev.get("ok_hash") == h and (prev.get("outputs") or prev.get("texts")):
                    store.patch_space_state(space_id, nid, {"status": "done", "error": None})
                    skipped.append(nid)
                    continue
                jobs: list[str] = []
                local: list[dict[str, Any]] = []
                try:
                    for op in ops:
                        if op["op"] == "generate":
                            jobs.append(studio.generate(pid, op["body"])["id"])
                        elif op["op"] == "compose":
                            jobs.append(studio.compose(pid, op["body"])["id"])
                        elif op["op"] == "edit":
                            jobs.append(studio.edit(op["body"])["id"])
                        else:
                            local.append(op)
                except Exception as exc:  # noqa: BLE001 - a refused job fails this node, not the run
                    store.patch_space_state(space_id, nid, {"status": "failed", "jobs": jobs,
                                                            "error": str(getattr(exc, "message", exc))[:400]})
                    failed.append(nid)
                    continue
                store.patch_space_state(space_id, nid, {"status": "running", "jobs": jobs, "error": None,
                                                        "started_at": now_iso()})
                started[nid] = {"jobs": jobs, "local": local, "hash": h, "prev": prev}
            # local work (the assistant, joining clips) runs here, the jobs meanwhile on the GPUs
            results: dict[str, tuple[list[str], list[str], list[str]]] = {}
            for nid, run in started.items():
                outputs: list[str] = []
                texts: list[str] = []
                errors: list[str] = []
                for op in run["local"]:
                    try:
                        if op["op"] == "chat":
                            b = op["body"]
                            ask = b["instruction"]
                            if b["as_list"]:
                                ask += (f"\n\nAnswer with exactly {b['items']} lines, one item per line, no numbering, "
                                        "no preamble.")
                            reply = studio.chat([{"role": "system", "content": "You write prompts and ideas for image, "
                                                  "video and music generation. Answer with the result only."},
                                                 {"role": "user", "content": ask}], 900, 0.7, effort="off").strip()
                            texts += _split_lines(reply, b["items"]) if b["as_list"] else ([reply] if reply else [])
                            if not texts:
                                errors.append("the assistant gave no answer")
                        elif op["op"] == "combine":
                            outputs.append(studio.combine(pid, op["body"])["id"])
                    except Exception as exc:  # noqa: BLE001
                        errors.append(str(getattr(exc, "message", exc))[:300])
                results[nid] = (outputs, texts, errors)
            for nid, run in started.items():
                outputs, texts, errors = results[nid]
                if run["jobs"]:
                    got, errs = _wait(studio, run["jobs"], poll_s, timeout_s, tick)
                    outputs += got
                    errors += errs
                patch: dict[str, Any] = {}
                if outputs and _wants_last_frames(graph, nid):
                    frames = {}
                    for clip in outputs:
                        try:
                            frames[clip] = studio.last_frame(clip, pid)["id"]
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"last frame: {str(getattr(exc, 'message', exc))[:120]}")
                    patch["last_frames"] = frames
                prev = run["prev"]
                good = bool(outputs or texts)
                history = list(prev.get("runs") or [])[-19:]
                if good:
                    history.append({"at": now_iso(), "outputs": outputs, "jobs": run["jobs"], "hash": run["hash"],
                                    **({"texts": texts} if texts else {})})
                patch.update({
                    "status": "done" if good and not errors else ("failed" if not good else "partial"),
                    "outputs": outputs or (prev.get("outputs") or [] if not texts else []),
                    "excluded": [], "runs": history,
                    "error": "; ".join(errors)[:400] or None, "hash": run["hash"],
                    "ok_hash": run["hash"] if good and not errors else None, "finished_at": now_iso()})
                if texts or not good:
                    patch["texts"] = texts or prev.get("texts") or []
                store.patch_space_state(space_id, nid, patch)
                (ran if good else failed).append(nid)
    except JobCancelled:
        for nid, run in started.items():
            for jid in run["jobs"]:
                try:
                    studio.cancel(jid)
                except Exception:  # noqa: BLE001
                    pass
        state = store.get_space(space_id)["state"]
        for nid in order:
            st = state.get(nid) or {}
            if st.get("status") in ("queued", "running"):
                store.patch_space_state(space_id, nid, {"status": "done" if st.get("outputs") else "failed",
                                                        "error": None if st.get("outputs") else "stopped"})
        raise
    return {"ran": ran, "skipped": skipped, "failed": failed}


def _wait(studio: Any, jobs: list[str], poll_s: float, timeout_s: float,
          tick: Optional[Callable[[str], None]] = None) -> tuple[list[str], list[str]]:
    deadline = time.monotonic() + timeout_s
    pending = list(jobs)
    outputs: list[str] = []
    errors: list[str] = []
    while pending and time.monotonic() < deadline:
        for jid in list(pending):
            job = studio.job(jid)
            if job["state"] in ("done", "failed", "cancelled"):
                pending.remove(jid)
                out = job.get("outputs") or {}
                outputs += list(out.get("asset_ids") or ([out["asset_id"]] if out.get("asset_id") else []))
                if job["state"] != "done":
                    err = job.get("error") or {}
                    errors.append(str(err.get("message") if isinstance(err, dict) else err or job["state"])[:200])
        if pending:
            if tick:
                tick(f"waiting for {len(pending)} job(s)")  # raises JobCancelled when the run is stopped
            time.sleep(poll_s)
    if pending:
        errors.append("timed out")
    return outputs, errors


# ----------------------------------------------------------- estimate

# seconds per render when this computer has no history of the template yet
DEFAULT_SECONDS = {"qwen21_txt2img": 60, "qwen21_edit": 90, "wan22_ti2v": 180, "wan22_i2v_14b": 300, "wan22_flf2v": 320,
                   "wan22_s2v": 380, "wan21_infinitetalk": 700, "ace15_song": 60, "esrgan_upscale": 15,
                   "birefnet_remove_background": 15, "control_pose": 20, "control_depth": 20}
TEMPLATE_VRAM = {"qwen21_txt2img": "qwen21", "qwen21_edit": "qwen21", "wan22_ti2v": "wan", "wan22_i2v_14b": "wan14b",
                 "wan22_flf2v": "wan14b", "wan22_s2v": "wan_s2v", "wan21_infinitetalk": "wan14b", "ace15_song": "ace",
                 "esrgan_upscale": "esrgan", "birefnet_remove_background": "birefnet", "control_pose": "control",
                 "control_depth": "control"}
_EDIT_OP_TEMPLATE = {"upscale": "esrgan_upscale", "remove_background": "birefnet_remove_background",
                     "pose_map": "control_pose", "depth_map": "control_depth"}


def _op_template(op: dict[str, Any]) -> tuple[str, float]:
    """The template an op will most likely run on, and how many of its
    usual lengths it takes (a 15 s line is three 5 s renders)."""
    b = op["body"]
    if op["op"] == "compose":
        return "ace15_song", max(1, int(b.get("count") or 1)) * max(0.3, float(b.get("duration") or 60) / 60)
    if op["op"] == "edit":
        return _EDIT_OP_TEMPLATE.get(b.get("operation") or "upscale", "esrgan_upscale"), 1
    if op["op"] in ("chat", "combine"):
        return op["op"], 1
    t = b.get("template")
    n = max(1, int(b.get("count") or 1))
    if t == "auto_sing":
        secs = float(b.get("audio_seconds") or 4.8)
        pick = (b.get("template_params") or {}).get("sing_engine")
        tpl = "wan21_infinitetalk" if pick == "infinitetalk" or (pick != "s2v" and secs > 10) else "wan22_s2v"
        return tpl, n * max(1.0, secs / (10.0 if tpl == "wan21_infinitetalk" else 4.8))
    if t == "auto_clip":
        return ("wan22_flf2v" if b.get("end_asset_id") else "wan22_i2v_14b"), n
    if t:
        return t, n
    return ("qwen21_edit" if b.get("reference_asset_ids") or b.get("reference_asset_id") else "qwen21_txt2img"), n


def _rough_estimate(graph: dict[str, Any], nid: str, per_node: dict[str, int]) -> Optional[tuple[str, float]]:
    """A node that can't be planned yet because what feeds it hasn't run:
    its likely template and how many renders, from what this run will give
    it (the renders counted for the nodes upstream). None when nothing
    upstream will run, so the planning error stands."""
    node = next((n for n in graph["nodes"] if n["id"] == nid), None)
    if not node:
        return None
    wires = [e for e in graph["edges"] if e["target"] == nid]
    if not any(e["source"] in per_node for e in wires):
        return None
    d = node.get("data") or {}

    def fan(handle: str) -> int:
        return max(1, sum(per_node.get(e["source"], 1) for e in wires if e.get("target_handle") == handle))

    handles = {e.get("target_handle") for e in wires}
    kind = node["type"]
    if kind == "image":
        tpl = "qwen21_edit" if handles & {"refs", "pose", "layout"} else "qwen21_txt2img"
        return tpl, max(1, int(d.get("count") or 1))
    if kind == "video":
        if "audio" in handles:
            secs = float(d.get("seconds") or 4.8)
            pick = d.get("sing_engine") or "auto"
            tpl = "wan21_infinitetalk" if pick == "infinitetalk" or (pick != "s2v" and secs > 10) else "wan22_s2v"
            return tpl, fan("start") * max(1.0, secs / (10.0 if tpl == "wan21_infinitetalk" else 4.8))
        return ("wan22_flf2v" if "end" in handles else "wan22_i2v_14b"), fan("start")
    if kind == "variations":
        n = len([x for x in str(d.get("custom") or "").splitlines() if x.strip()]) if d.get("mode") == "custom" else int(d.get("count") or 4)
        return "qwen21_edit", max(1, n) * fan("image")
    if kind == "edit":
        return _EDIT_OP_TEMPLATE.get(d.get("operation") or "upscale", "esrgan_upscale"), fan("image")
    if kind == "music":
        return "ace15_song", max(1, int(d.get("count") or 1)) * max(0.3, float(d.get("duration") or 60) / 60)
    if kind in ("assistant", "combine"):
        return ("chat" if kind == "assistant" else "combine"), 1
    return None


def estimate(store: Store, space_id: str, mode: str = "all", node_ids: Optional[list[str]] = None,
             force: bool = False, vram_mb: Optional[dict[str, int]] = None) -> dict[str, Any]:
    """Before a run: per generator, how many renders it queues, the seconds
    they should take on this computer (median of its recent renders of the
    same template, else a default) and the VRAM they need; nodes "all"
    would skip are counted as skipped. `minutes` adds every render up (GPU
    time); a render pool shares it out."""
    space = store.get_space(space_id)
    graph, state = space["graph"], space["state"]
    timings = store.render_timings()
    medians = {k: sorted(v)[len(v) // 2] for k, v in timings.items() if v}
    nodes = []
    total = 0.0
    renders = 0
    peak = 0
    per_node: dict[str, int] = {}
    for nid in run_order(graph, mode, node_ids):
        try:
            ops = plan_node(store, graph, state, nid)
        except SpaceError as exc:
            rough = _rough_estimate(graph, nid, per_node)
            if not rough:
                nodes.append({"node": nid, "error": exc.message})
                continue
            tpl, units = rough
            if tpl == "chat":
                secs, n_r = 15.0, 0
            elif tpl == "combine":
                secs, n_r = 10.0, 0
            else:
                secs = (medians.get(tpl) or DEFAULT_SECONDS.get(tpl, 120)) * units
                n_r = max(1, int(round(units)))
                peak = max(peak, int((vram_mb or {}).get(TEMPLATE_VRAM.get(tpl, ""), 0)))
            per_node[nid] = max(1, n_r)
            nodes.append({"node": nid, "renders": n_r, "seconds": round(secs), "templates": [tpl] if n_r else [],
                          "measured": tpl in medians, "rough": True})
            total += secs
            renders += n_r
            continue
        prev = state.get(nid) or {}
        if mode == "all" and not force and prev.get("ok_hash") == plan_hash(ops) and (prev.get("outputs") or prev.get("texts")):
            nodes.append({"node": nid, "skipped": True, "renders": 0, "seconds": 0})
            continue
        secs = 0.0
        node_renders = 0
        templates = set()
        for op in ops:
            tpl, units = _op_template(op)
            if tpl == "chat":
                secs += 15
                continue
            if tpl == "combine":
                secs += 3 * len(op["body"].get("clips") or [])
                continue
            per = medians.get(tpl) or DEFAULT_SECONDS.get(tpl, 120)
            secs += per * units
            node_renders += max(1, int(round(units))) if op["op"] != "compose" else max(1, int(op["body"].get("count") or 1))
            templates.add(tpl)
            peak = max(peak, int((vram_mb or {}).get(TEMPLATE_VRAM.get(tpl, ""), 0)))
        per_node[nid] = max(1, node_renders or len(ops))
        nodes.append({"node": nid, "renders": node_renders, "seconds": round(secs), "templates": sorted(templates),
                      "measured": all(t in medians for t in templates)})
        total += secs
        renders += node_renders
    return {"nodes": nodes, "renders": renders, "seconds": round(total), "minutes": round(total / 60, 1),
            "vram_mb": peak or None}


# ---------------------------------------------------------------- apps

APP_INPUT_TYPES = ("text", "asset", "cast")


def app_view(store: Store, space: dict[str, Any]) -> dict[str, Any]:
    """A space as an app: the nodes marked as inputs (texts, media, cast
    with `data.app_input`) become form fields and the generators marked
    `data.app_output` (else the last generators of the graph) its results."""
    graph, state = space["graph"], space["state"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    inputs = []
    for n in graph["nodes"]:
        d = n.get("data") or {}
        if n["type"] in APP_INPUT_TYPES and d.get("app_input"):
            field: dict[str, Any] = {"node": n["id"], "type": n["type"], "label": str(d.get("app_label") or n["id"])}
            if n["type"] == "text":
                field["value"] = d.get("text") or ""
            elif n["type"] == "asset":
                field.update({"kind": d.get("kind") or "image", "value": d.get("asset_ids") or []})
            else:
                field["value"] = d.get("character_id")
            inputs.append(field)
    outs = [n["id"] for n in graph["nodes"] if n["type"] in GENERATORS and (n.get("data") or {}).get("app_output")]
    if not outs:
        feeding = {e["source"] for e in graph["edges"]}
        outs = [n["id"] for n in graph["nodes"] if n["type"] in GENERATORS and n["id"] not in feeding]
    outputs = []
    for nid in outs:
        st = state.get(nid) or {}
        kind = output_type(nodes[nid]) or "image"
        excluded = set(st.get("excluded") or [])
        outputs.append({"node": nid, "label": str((nodes[nid].get("data") or {}).get("app_label") or nid),
                        "kind": "audio" if kind == "audio" else kind, "status": st.get("status"),
                        "outputs": [a for a in st.get("outputs") or [] if a not in excluded],
                        **({"texts": st.get("texts")} if st.get("texts") else {}), "error": st.get("error")})
    return {"id": space["id"], "name": space["name"], "inputs": inputs, "outputs": outputs,
            "description": str((graph.get("app") or {}).get("description") or "") if isinstance(graph.get("app"), dict) else ""}


def apply_app_values(store: Store, graph: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """The graph with an app's form values written into its input nodes;
    SpaceError for a node that is not an input or a value of the wrong shape."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    for nid, value in (values or {}).items():
        n = nodes.get(nid)
        if not n or n["type"] not in APP_INPUT_TYPES or not (n.get("data") or {}).get("app_input"):
            raise SpaceError("not_an_input", f"{nid} is not an input of this app")
        d = dict(n.get("data") or {})
        if n["type"] == "text":
            d["text"] = str(value or "")[:6000]
        elif n["type"] == "asset":
            ids = [value] if isinstance(value, str) else list(value or [])
            for aid in ids:
                a = store.get_asset(aid)
                if a["kind"] != (d.get("kind") or "image"):
                    raise SpaceError("wrong_kind", f"{nid} takes {d.get('kind') or 'image'}, not {a['kind']}")
            d["asset_ids"] = ids[:20]
        else:
            store.get_character(str(value))
            d["character_id"] = str(value)
        n["data"] = d
    return graph


# ------------------------------------------------------------ assistant

BUILD_GUIDE = """You design node graphs for a local AI film studio. Answer with JSON only:
{"note": "one sentence for the user", "nodes": [...]}
Each node is an object with an "id" (short, letters/digits), a "type" and fields:
- {"id","type":"text","text"}: a prompt or a STYLE line shared by several nodes
- {"id","type":"cast","name"}: a character, place or object of the project's cast by name
- {"id","type":"image","prompt","aspect":"1:1|16:9|9:16","count":1-4,"sheet":true|false,
   "prompt_from":[ids of text/cast/assistant/list nodes],"refs":[ids of image/cast/variations nodes]}
- {"id","type":"variations","mode":"angles|expressions|ages|lighting|storyboard","count":4,"image":[ids]}
- {"id","type":"video","prompt","quality":"draft|final","start":[ids of image nodes],"after":"id of a video node to continue from its last frame","end":"id of an image node to end on","audio":"id of a music node","prompt_from":[ids]}
- {"id","type":"music","tags","lyrics","duration":10-240}
- {"id","type":"assistant","prompt","as_list":true,"items":3}: writes ideas; wire it into an image's prompt_from to make one picture per idea
- {"id","type":"combine","clips":[ids of video nodes in order],"audio":"id of a music node"}
- {"id","type":"note","text"}
Rules: reuse the cast names the user mentions; use a reference sheet image ("sheet": true) for each new character
and wire it as refs into the scene pictures; one picture feeds each clip's start; keep it small (at most 14 nodes)."""


def _cast_by_name(store: Store, project_id: str) -> dict[str, str]:
    return {c["name"].lower(): c["id"] for c in store.list_characters(project_id)}


def plan_to_ops(store: Store, project_id: str, plan: dict[str, Any], existing: set[str]) -> list[dict[str, Any]]:
    """The assistant's compact plan as graph edit ops: nodes laid out in
    columns by dependency, wires from its fields; unknown ids, unknown cast
    names and wires of the wrong kind are dropped rather than failing."""
    raw = [n for n in (plan.get("nodes") or []) if isinstance(n, dict) and n.get("type") in NODE_TYPES]
    cast = _cast_by_name(store, project_id)
    ids: dict[str, str] = {}
    for n in raw:
        base = re.sub(r"[^A-Za-z0-9_-]", "", str(n.get("id") or n["type"]))[:30] or n["type"]
        nid, k = base, 2
        while nid in existing or nid in ids.values():
            nid, k = f"{base}{k}", k + 1
        ids[str(n.get("id") or base)] = nid
    ops: list[dict[str, Any]] = []
    wires: list[tuple[str, Optional[str], str, str]] = []
    types = {ids[str(n.get("id") or n["type"])]: n["type"] for n in raw}
    data_keys = {"text": ("text",), "image": ("prompt", "aspect", "count"), "video": ("prompt", "quality"),
                 "music": ("tags", "lyrics", "duration", "bpm"), "assistant": ("prompt", "as_list", "items"),
                 "variations": ("mode", "count"), "note": ("text",), "combine": ("audio_start_s",)}
    kept = []
    for n in raw:
        nid = ids[str(n.get("id") or n["type"])]
        t = n["type"]
        data = {k: n[k] for k in data_keys.get(t, ()) if n.get(k) not in (None, "")}
        if t == "image" and n.get("sheet"):
            data["preset"] = "sheet"
        if t == "cast":
            cid = cast.get(str(n.get("name") or "").lower())
            if not cid:
                continue
            data = {"character_id": cid}
        if t == "group":
            continue
        kept.append(nid)
        ops.append({"op": "add_node", "id": nid, "type": t, "data": data})

        def ref(x: Any) -> Optional[str]:
            return ids.get(str(x)) if x is not None else None
        for src in n.get("prompt_from") or []:
            if ref(src):
                wires.append((ref(src), "text" if types.get(ref(src)) == "cast" else None, nid, "prompt"))
        for src in n.get("refs") or []:
            if ref(src):
                wires.append((ref(src), "image" if types.get(ref(src)) == "cast" else None, nid, "refs"))
        for src in n.get("start") or []:
            if ref(src):
                wires.append((ref(src), None, nid, "start"))
        for src in n.get("image") or []:
            if ref(src):
                wires.append((ref(src), None, nid, "image"))
        if t == "video" and ref(n.get("after")):
            wires.append((ref(n.get("after")), "last", nid, "start"))
        if t == "video" and ref(n.get("end")):
            wires.append((ref(n.get("end")), None, nid, "end"))
        if ref(n.get("audio")):
            wires.append((ref(n.get("audio")), None, nid, "audio"))
        for src in n.get("clips") or []:
            if ref(src):
                wires.append((ref(src), None, nid, "clips"))
    kept_set = set(kept) | existing
    for src, sh, dst, th in wires:
        if src in kept_set and dst in kept_set:
            ops.append({"op": "connect", "source": src, "source_handle": sh, "target": dst, "target_handle": th})
    return ops


def layout_new_nodes(graph: dict[str, Any], new_ids: set[str], origin_x: float = 0, origin_y: float = 0) -> None:
    """Columns by dependency depth, rows in order, for nodes just added."""
    parents: dict[str, set[str]] = {n["id"]: set() for n in graph["nodes"]}
    for e in graph["edges"]:
        parents[e["target"]].add(e["source"])
    depth: dict[str, int] = {}

    def d(nid: str, seen: frozenset = frozenset()) -> int:
        if nid in depth:
            return depth[nid]
        if nid in seen:
            return 0
        depth[nid] = 0 if not parents[nid] else 1 + max(d(p, seen | {nid}) for p in parents[nid])
        return depth[nid]
    rows: dict[int, int] = {}
    for n in graph["nodes"]:
        if n["id"] in new_ids:
            col = d(n["id"])
            row = rows.get(col, 0)
            rows[col] = row + 1
            n["x"], n["y"] = origin_x + col * 420, origin_y + row * 380


# ------------------------------------------------------------ templates

def template_graph(name: str) -> dict[str, Any]:
    """Starter spaces. "reference_film": character, creature, place and prop
    sheets wired into a picture and a clip (the reference-driven short);
    "singing_shot": a cast member + a song into a lip-synced clip;
    "blank": nothing."""
    if name == "reference_film":
        nodes = [
            {"id": "style", "type": "text", "x": 0, "y": -140, "data": {"text": "STYLE: realistic live-action feature film, "
                                                                            "natural light, 35mm lens, shallow depth of field"}},
            {"id": "hero", "type": "image", "x": 0, "y": 140, "data": {"preset": "sheet", "prompt": "a young man in a teal t-shirt and jeans"}},
            {"id": "buddy", "type": "image", "x": 0, "y": 600, "data": {"preset": "sheet", "prompt": "a scruffy white dog"}},
            {"id": "place", "type": "image", "x": 0, "y": 1060, "data": {"aspect": "16:9", "prompt": "a misty pine forest clearing at dawn, empty"}},
            {"id": "frame", "type": "image", "x": 520, "y": 480, "data": {"aspect": "16:9", "prompt": "medium shot: the man kneels and offers the dog a piece of food, the dog sniffs it"}},
            {"id": "clip", "type": "video", "x": 1040, "y": 480, "data": {"prompt": "slow push-in, the dog takes the food and wags its tail", "quality": "draft"}},
            {"id": "tip", "type": "note", "x": 520, "y": -140, "data": {"text": "Make the three sheets, untick the takes you do not want, then run the frame and the clip. Draft clips are quick; switch to final for the keeper."}},
        ]
        edges = [
            {"source": "style", "target": "frame", "target_handle": "prompt"},
            {"source": "hero", "target": "frame", "target_handle": "refs"},
            {"source": "buddy", "target": "frame", "target_handle": "refs"},
            {"source": "place", "target": "frame", "target_handle": "refs"},
            {"source": "frame", "target": "clip", "target_handle": "start"},
            {"source": "style", "target": "clip", "target_handle": "prompt"},
        ]
    elif name == "singing_shot":
        nodes = [
            {"id": "singer", "type": "cast", "x": 0, "y": 0, "data": {}},
            {"id": "frame", "type": "image", "x": 420, "y": 0, "data": {"aspect": "16:9", "prompt": "medium close-up, singing into a vintage microphone on a small stage, warm spotlight"}},
            {"id": "song", "type": "asset", "x": 420, "y": 360, "data": {"kind": "audio", "asset_ids": []}},
            {"id": "sing", "type": "video", "x": 900, "y": 120, "data": {"prompt": "sings with natural lip movement, sways to the beat", "seconds": 4.8, "audio_start_s": 0}},
        ]
        edges = [
            {"source": "singer", "source_handle": "image", "target": "frame", "target_handle": "refs"},
            {"source": "frame", "target": "sing", "target_handle": "start"},
            {"source": "song", "target": "sing", "target_handle": "audio"},
        ]
    elif name == "short_film":
        nodes = [
            {"id": "idea", "type": "assistant", "x": 0, "y": 0, "data": {"as_list": True, "items": 3, "prompt":
                "Three consecutive shots for a 15-second mysterious short film: a lighthouse keeper finds a glowing "
                "bottle on the shore at night. One line per shot, describing only what we see."}},
            {"id": "style", "type": "text", "x": 0, "y": 360, "data": {"text": "STYLE: cinematic live action, 35mm, "
                                                                              "moonlight and a warm lantern, light fog"}},
            {"id": "shots", "type": "image", "x": 420, "y": 120, "data": {"aspect": "16:9", "count": 1,
                                                                          "camera": {"shot": "wide", "light": "low_key"}}},
            {"id": "clips", "type": "video", "x": 840, "y": 120, "data": {"quality": "draft", "prompt": "slow, subtle motion",
                                                                          "camera": {"move": "dolly_in"}}},
            {"id": "film", "type": "combine", "x": 1260, "y": 160, "data": {}},
            {"id": "tip", "type": "note", "x": 420, "y": -200, "data": {"text": "The assistant writes one line per shot; "
                "each line becomes a picture and a clip. Untick a line you do not like before running the pictures, "
                "then join the clips. Wire a clip's last frame into another clip's start to continue a shot."}},
        ]
        edges = [
            {"source": "idea", "target": "shots", "target_handle": "prompt"},
            {"source": "style", "target": "shots", "target_handle": "prompt"},
            {"source": "shots", "target": "clips", "target_handle": "start"},
            {"source": "clips", "target": "film", "target_handle": "clips"},
        ]
    elif name == "blank":
        nodes, edges = [], []
    else:
        raise SpaceError("unknown_template", f"templates: {', '.join(TEMPLATES)}")
    for i, e in enumerate(edges):
        e["id"] = f"e{i}"
    return validate_graph({"nodes": nodes, "edges": edges})


TEMPLATES = ("blank", "reference_film", "singing_shot", "short_film")
