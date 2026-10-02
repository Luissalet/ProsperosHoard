"""Prospero's side of the Hoard family on HTTP: the shared agent contract, the four tools other apps call, and the settings route.

`family.install_fastapi` builds `GET /api/agent/tools` and `POST /api/agent/call` from an app's POST routes by calling each endpoint
with its body model alone. Prospero's per-tool routes mix query parameters (`project`, `production`, `job_id`...) with bodies and
include GET tools, so that builder cannot serve them. This module keeps the same contract (same paths, the bearer token in
`<data>/mcp-token`, the same answers) and dispatches in-process: each tool's schema is its query parameters plus its body model, the
description is the docstring of the matching tool in `mcp_server.py`, and a call runs the very endpoint the per-tool route runs.
The per-tool routes `/api/agent/<tool>` are untouched and keep answering; the two shared routes are moved to the front of the
router so the catch-all routes (`/api/{rest}`, the single-page app) never swallow them.

Routes added: `POST /api/agent/production_export_lumiere`, `cast_import_character`, `production_from_storyboard`, `voice_tts`
(the logic is in `family_tools`) and `GET|PUT /api/family/settings` (`family_settings`).
"""

from __future__ import annotations

import ast
import inspect
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from . import engine, exporters, family_tools
from .family_settings import FamilySettings, SettingsError
from .hoard_link import family
from .store import Store

PREFIX = "/api/agent/"
SHARED = (PREFIX + "tools", PREFIX + "call")


@dataclass
class Context:
    """What the app lends this module (closures of `create_app`)."""

    store: Store
    backend: Any
    settings: FamilySettings
    agent: Callable[[str, str, Callable[[], Any]], Any]             # records an agent call in the activity view
    error_payload: Callable[[Exception], tuple[int, dict[str, str]]]
    export_lookup: exporters.Lookup
    resolve_timeline: Callable[..., str]
    casting_project: Callable[[], str]
    tts_engines: Callable[[], list[Any]]
    production_view: Callable[[str], dict[str, Any]]
    mcp_source: Path


# ---------------------------------------------------------------- bodies of the four tools

class ExportLumiereBody(BaseModel):
    production: str = Field(..., description="The production's slug (studio_productions).")
    aspect: Optional[str] = Field(None, description="Which cut when a production has several (16:9, 9:16...); default the first.")
    timeline_id: Optional[str] = Field(None, description="A timeline id instead of a production's cut.")


class CastImportBody(BaseModel):
    name: str
    description: str = ""
    look: str = ""
    images: list[str] = Field(default_factory=list, description="Reference image paths (inside the folders Prospero may import from); the first is the canonical one.")
    source_ref: str = Field("", description="hoard://... of the thing it comes from; the same name and ref again returns the same member.")
    project: Optional[str] = Field(None, description="Cast it into this project (default: the 'Casting' project).")


class StoryboardShot(BaseModel):
    text: str
    duration_s: Optional[float] = None
    image: Optional[str] = Field(None, description="An existing asset id, or an image path Prospero may import from; the shot reuses it instead of generating.")


class StoryboardBody(BaseModel):
    title: str
    shots: list[StoryboardShot]
    source_ref: str = ""
    project: Optional[str] = None
    character_id: Optional[str] = Field(None, description="The production's lead from the cast (shots do not show the lead unless you say so later).")
    lead_name: Optional[str] = None
    lead_look: Optional[str] = None
    song_asset_id: Optional[str] = Field(None, description="An existing song for the cut; without one the draft waits for it.")


class TtsBody(BaseModel):
    text: str
    voice: Optional[str] = Field(None, description="A library voice (id or name) or an engine's own voice id; empty = the best installed engine.")
    lang: Optional[str] = None
    engine: Optional[str] = Field(None, description="A speech engine id such as piper; a library voice's own engine wins. 400 unknown_engine when it is not installed here.")
    speed: Optional[float] = Field(None, description="0.5 to 2.0; 1.0 is normal.")


# ---------------------------------------------------------------- reading the app's own routes

def _tool_routes(app: FastAPI) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for route in getattr(app.router, "routes", []):
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith(PREFIX) or path in SHARED or not ({"GET", "POST"} & methods):
            continue
        name = path[len(PREFIX):]
        if not name or "/" in name or "{" in name:
            continue
        found[name] = route
    return found


def _params(route: Any) -> tuple[list[Any], Optional[tuple[str, type[BaseModel]]]]:
    """(query parameters, (body parameter name, body model)) of a route, as FastAPI resolved them."""
    dep = route.dependant
    body = None
    for field in dep.body_params:
        model = getattr(field.field_info, "annotation", None)
        if isinstance(model, type) and issubclass(model, BaseModel):
            body = (field.name, model)
            break
    return list(dep.query_params), body


def _annotation(field: Any) -> Any:
    return getattr(field.field_info, "annotation", None) or Any


def _schema(route: Any) -> dict[str, Any]:
    query, body = _params(route)
    props: dict[str, Any] = {}
    required: list[str] = []
    defs: dict[str, Any] = {}
    for q in query:
        schema = TypeAdapter(_annotation(q)).json_schema()
        defs.update(schema.pop("$defs", {}))
        props[q.name] = schema
        if q.required:
            required.append(q.name)
    if body:
        model_schema = body[1].model_json_schema()
        defs.update(model_schema.pop("$defs", {}))
        for key, value in (model_schema.get("properties") or {}).items():
            props.setdefault(key, value)
        required += [r for r in model_schema.get("required", []) if r not in required]
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    if defs:
        out["$defs"] = defs
    return out


def read_only_tools(source: Path) -> set[str]:
    """The tools `mcp_server.py` marks `readOnlyHint=True` (read with `ast`, nothing is imported)."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            for sub in ast.walk(deco):
                if isinstance(sub, ast.keyword) and sub.arg == "readOnlyHint" and isinstance(sub.value, ast.Constant) and sub.value.value is True:
                    names.add(node.name)
    return names


def tool_texts(source: Path) -> dict[str, str]:
    """The docstring of every `@tool(...)` function of the MCP adapter, read with `ast`. (The shared reader only knows `@mcp.tool`;
    Prospero's adapter wraps it in its own `tool` decorator, so it would find nothing.)"""
    try:
        tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    out = family.descriptions_from_fastmcp_source(str(source))         # also gives "__instructions__"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in node.decorator_list:
                call = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(call, ast.Name) and call.id == "tool" and ast.get_docstring(node):
                    out[node.name] = ast.get_docstring(node) or ""
    return out


def catalogue(app: FastAPI, source: Path) -> dict[str, Any]:
    texts = tool_texts(source)
    instructions = texts.pop("__instructions__", "")
    read_only = read_only_tools(source)
    tools = []
    for name, route in sorted(_tool_routes(app).items()):
        doc = texts.get(name) or (inspect.getdoc(route.endpoint) or "").strip() or name.replace("_", " ")
        tools.append({"name": name, "description": doc, "inputSchema": _schema(route),
                      "annotations": {"readOnlyHint": name in read_only or (name not in texts and "GET" in route.methods)}})
    return {"instructions": instructions, "tools": tools, "contract": "shared", "app": "prospero"}


class UnknownTool(LookupError):
    pass


async def call_tool(app: FastAPI, ctx: Context, name: str, args: dict[str, Any]) -> Any:
    route = _tool_routes(app).get(name)
    if route is None:
        raise UnknownTool(name)
    query, body = _params(route)
    kwargs: dict[str, Any] = {}
    rest = dict(args)
    for q in query:
        if q.name in rest:
            value = rest.pop(q.name)
            if value is not None:
                kwargs[q.name] = TypeAdapter(_annotation(q)).validate_python(value)
        elif q.required:
            raise engine.EngineError("missing_argument", f"{name} needs '{q.name}'")
    if body:
        known = set(body[1].model_fields)
        unknown = sorted(set(rest) - known)
        if unknown:
            raise engine.EngineError("unknown_argument", f"{name} has no argument {', '.join(unknown)}; it takes: "
                                                         + ", ".join(sorted(known | {q.name for q in query})))
        kwargs[body[0]] = body[1].model_validate(rest)
    elif rest:
        raise engine.EngineError("unknown_argument", f"{name} has no argument {', '.join(sorted(rest))}; it takes: "
                                                     + (", ".join(q.name for q in query) or "nothing"))
    if inspect.iscoroutinefunction(route.endpoint):
        return await route.endpoint(**kwargs)
    return await run_in_threadpool(route.endpoint, **kwargs)     # the per-tool routes are plain functions: off the event loop, as FastAPI runs them


# ---------------------------------------------------------------- install

def install(app: FastAPI, ctx: Context, app_id: str = "prospero") -> None:
    """Add the four tool routes and the settings route, join the family (id, token, agent-call events) and put the shared
    contract in front of every catch-all. Call it before the catch-all routes are added; it does not matter that the other
    per-tool routes exist already (they are read when a request comes)."""
    store, data_dir = ctx.store, ctx.store.data_dir
    family.install_fastapi(app, app_id, str(data_dir), contract=False)
    token_path = data_dir / "mcp-token"

    def token() -> str:
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    # ----- the four tools (per-tool routes, like every other tool)
    @app.post(PREFIX + "production_export_lumiere")
    def agent_export_lumiere(body: ExportLumiereBody):
        def run():
            return family_tools.export_to_lumiere(
                store, production=body.production, aspect=body.aspect, timeline_id=body.timeline_id, lookup=ctx.export_lookup,
                resolve_timeline=ctx.resolve_timeline, call=lambda *a, **k: family.call(*a, **k))
        return ctx.agent("production_export_lumiere", body.production[:80], run)

    @app.post(PREFIX + "cast_import_character")
    def agent_cast_import(body: CastImportBody):
        def run():
            return family_tools.import_character(
                store, ctx.backend, name=body.name, description=body.description, look=body.look, images=body.images,
                source_ref=body.source_ref, project=body.project, casting_project=ctx.casting_project)
        return ctx.agent("cast_import_character", body.name[:80], run)

    @app.post(PREFIX + "production_from_storyboard")
    def agent_from_storyboard(body: StoryboardBody):
        def run():
            return family_tools.production_from_storyboard(
                store, ctx.backend, title=body.title, shots=[s.model_dump() for s in body.shots], source_ref=body.source_ref,
                project=body.project, character_id=body.character_id, lead_name=body.lead_name, lead_look=body.lead_look,
                song_asset_id=body.song_asset_id, view=ctx.production_view)
        return ctx.agent("production_from_storyboard", body.title[:80], run)

    @app.post(PREFIX + "voice_tts")
    def agent_voice_tts(body: TtsBody):
        return ctx.agent("voice_tts", body.text[:80],
                         lambda: family_tools.tts(store, ctx.tts_engines(), text=body.text, voice=body.voice, lang=body.lang,
                                                    engine_id=body.engine, speed=body.speed))

    # ----- settings
    @app.get("/api/family/settings")
    def family_settings_get():
        return ctx.settings.all()

    @app.put("/api/family/settings")
    def family_settings_put(body: dict[str, Any]):
        try:
            return ctx.settings.update(body)
        except SettingsError as exc:
            raise engine.EngineError("bad_setting", str(exc)) from None

    # ----- the shared contract
    async def tools_route(_request: Request) -> Any:
        return catalogue(app, ctx.mcp_source)

    async def call_route(request: Request) -> Any:
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        expected = token()
        if not given or not expected or not secrets.compare_digest(given, expected):
            return JSONResponse({"ok": False, "error": "Invalid MCP token."}, status_code=401)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        name = str(payload.get("name") or payload.get("tool") or "")
        args = payload.get("arguments") or payload.get("args") or {}
        caller = str(payload.get("caller") or "")
        if not isinstance(args, dict):
            return JSONResponse({"ok": False, "error": "arguments must be an object"}, status_code=400)
        t0 = time.monotonic()
        try:
            result = await call_tool(app, ctx, name, args)
        except UnknownTool:
            return JSONResponse({"ok": False, "error": f"unknown tool: {name}", "tools": sorted(_tool_routes(app))}, status_code=404)
        except ValidationError as exc:
            msg = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()[:5])
            family.record_call(name, False, int((time.monotonic() - t0) * 1000), caller=caller, error=msg)
            return JSONResponse({"ok": False, "error": msg, "code": "invalid_arguments"}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - the same mapping the per-tool routes use
            status, payload_err = ctx.error_payload(exc)
            family.record_call(name, False, int((time.monotonic() - t0) * 1000), caller=caller, error=payload_err["message"])
            return JSONResponse({"ok": False, "error": payload_err["message"], "code": payload_err["error"]}, status_code=status)
        family.record_call(name, True, int((time.monotonic() - t0) * 1000), caller=caller)
        return result

    app.add_api_route(SHARED[0], tools_route, methods=["GET"], include_in_schema=False)
    app.add_api_route(SHARED[1], call_route, methods=["POST"], include_in_schema=False)
    routes = app.router.routes
    ours = routes[-2:]
    del routes[-2:]
    routes[0:0] = ours
