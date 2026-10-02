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

from .store import NotFound, Store
from .util import now_iso

NODE_TYPES = ("text", "asset", "cast", "image", "video", "music", "list", "note")
GENERATORS = ("image", "video", "music")
INPUTS: dict[str, dict[str, str]] = {
    "image": {"prompt": "text", "refs": "image"},
    "video": {"start": "image", "prompt": "text", "motion": "video", "audio": "audio"},
    "music": {"prompt": "text"},
    "list": {"items": "any"},
}
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
    if t in ("image", "video"):
        return t
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
        if dst["type"] == "video" and handle in ("motion", "audio"):
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
    if t in GENERATORS:
        st = state.get(node_id) or {}
        excluded = set(st.get("excluded") or [])
        kind = "audio" if t == "music" else t
        return [(kind, a) for a in st.get("outputs") or [] if a not in excluded]
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
        texts = _texts(ins.get("prompt", [])) + ([own] if own else [])
        refs = list(dict.fromkeys(v for k, v in ins.get("refs", []) if k == "image"))[:10]
        if not texts and not refs:
            raise SpaceError("needs_prompt", "write what the picture shows or wire a text into its prompt")
        prompt = ". ".join(texts) or "the subject of the reference image"
        if data.get("preset") == "sheet":
            prompt = f"{prompt}. {SHEET_SUFFIX}"
        if refs and "<image" not in prompt:
            prompt += _refs_note(len(refs))
        body: dict[str, Any] = {"prompt": prompt[:3900], "count": max(1, min(int(data.get("count") or 1), 8)),
                                "aspect": data.get("aspect") or ("16:9" if data.get("preset") == "sheet" else "1:1"),
                                "engine": data.get("engine") or None}
        if data.get("seed") not in (None, ""):
            body["seed"] = int(data["seed"])
        if refs:
            body["reference_asset_ids"] = refs
        return [{"op": "generate", "body": body}]
    if t == "video":
        starts = list(dict.fromkeys(v for k, v in ins.get("start", []) if k == "image"))
        if not starts:
            raise SpaceError("needs_start", "a clip starts from a picture: wire an image into its start")
        texts = _texts(ins.get("prompt", [])) + ([own] if own else [])
        prompt = ". ".join(texts) or "subtle natural motion"
        audio = next((v for k, v in ins.get("audio", []) if k == "audio"), None)
        motion = next((v for k, v in ins.get("motion", []) if k == "video"), None)
        quality = data.get("quality") or "final"
        ops = []
        for i, start in enumerate(starts[:12]):
            body = {"prompt": prompt[:3900], "reference_asset_id": start, "count": 1}
            if data.get("seed") not in (None, ""):
                body["seed"] = int(data["seed"]) + i
            if audio:
                seconds = max(1.0, min(float(data.get("seconds") or 4.8), 19.0))
                body.update({"template": "wan22_s2v", "audio_asset_id": audio,
                             "audio_start_s": max(0.0, float(data.get("audio_start_s") or 0)), "audio_seconds": seconds})
            elif motion:
                body.update({"template": "auto_clip", "driving_asset_id": motion,
                             "driving_start_s": max(0.0, float(data.get("motion_start_s") or 0))})
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


def run_space(store: Store, studio: Any, space_id: str, mode: str = "node", node_ids: Optional[list[str]] = None,
              progress: Optional[Callable[..., None]] = None, force: bool = False,
              poll_s: float = 1.0, timeout_s: float = 6 * 3600) -> dict[str, Any]:
    """Run generators of a space one after the other (each waits for its
    inputs' jobs, so a picture made upstream feeds the clip downstream in
    the same run). "all" without `force` skips a node whose plan did not
    change since its last good run. Returns {"ran", "skipped", "failed"}."""
    space = store.get_space(space_id)
    graph = space["graph"]
    order = run_order(graph, mode, node_ids)
    ran, skipped, failed = [], [], []
    for i, nid in enumerate(order):
        state = store.get_space(space_id)["state"]
        try:
            ops = plan_node(store, graph, state, nid)
        except SpaceError as exc:
            store.patch_space_state(space_id, nid, {"status": "failed", "error": exc.message, "jobs": []})
            failed.append(nid)
            continue
        h = plan_hash(ops)
        prev = state.get(nid) or {}
        if mode == "all" and not force and prev.get("ok_hash") == h and prev.get("outputs"):
            # nothing it depends on changed since its last good run
            store.patch_space_state(space_id, nid, {"status": "done", "error": None})
            skipped.append(nid)
            continue
        if progress:
            progress(i / max(1, len(order)), f"running {nid} ({i + 1}/{len(order)})")
        jobs = []
        try:
            for op in ops:
                job = studio.generate(space["project_id"], op["body"]) if op["op"] == "generate" \
                    else studio.compose(space["project_id"], op["body"])
                jobs.append(job["id"])
        except Exception as exc:  # noqa: BLE001 - a refused job fails this node, not the run
            store.patch_space_state(space_id, nid, {"status": "failed", "error": str(getattr(exc, "message", exc))[:400],
                                                    "jobs": jobs})
            failed.append(nid)
            continue
        store.patch_space_state(space_id, nid, {"status": "running", "jobs": jobs, "error": None, "started_at": now_iso()})
        outputs, errors = _wait(studio, jobs, poll_s, timeout_s)
        history = list(prev.get("runs") or [])[-19:]
        if outputs:
            history.append({"at": now_iso(), "outputs": outputs, "jobs": jobs, "hash": h})
        store.patch_space_state(space_id, nid, {
            "status": "done" if outputs and not errors else ("failed" if not outputs else "partial"),
            "outputs": outputs or prev.get("outputs") or [], "excluded": [], "runs": history,
            "error": "; ".join(errors)[:400] or None, "hash": h,
            "ok_hash": h if outputs and not errors else None, "finished_at": now_iso()})
        (ran if outputs else failed).append(nid)
    return {"ran": ran, "skipped": skipped, "failed": failed}


def _wait(studio: Any, jobs: list[str], poll_s: float, timeout_s: float) -> tuple[list[str], list[str]]:
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
            time.sleep(poll_s)
    if pending:
        errors.append("timed out")
    return outputs, errors


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
    elif name == "blank":
        nodes, edges = [], []
    else:
        raise SpaceError("unknown_template", "templates: blank, reference_film, singing_shot")
    for i, e in enumerate(edges):
        e["id"] = f"e{i}"
    return validate_graph({"nodes": nodes, "edges": edges})


TEMPLATES = ("blank", "reference_film", "singing_shot")
