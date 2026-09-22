"""FastAPI application: HTTP surface, browser-attack guard, `/api/agent/*`
(exactly what the MCP tools call, compact and id-first), `/api/backend`,
and the richer UI endpoints. See `docs/API.md` for every route.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from . import audio as audio_mod
from . import comfy_driver, engine
from . import templates as design_templates
from . import timeline as timeline_mod
from . import voices as voices_mod
from .backend import Backend
from .design import DesignError
from .hoard_link.errors import BackendError, HoardLinkError, Unavailable
from .ids import new_id
from .jobs import JobQueue
from .store import NotFound, Store

logger = logging.getLogger("prosperos_hoard.api")

MAX_UPLOAD_MEDIA = engine.MAX_MEDIA_BYTES
MAX_UPLOAD_IMAGE = engine.MAX_IMAGE_BYTES
MAX_WAIT_S = 300.0


# ------------------------------------------------------------------ guard

class GuardMiddleware(BaseHTTPMiddleware):
    """Rejects DNS-rebinding Host headers and cross-site writes. No CORS
    headers are ever added: a plain GET from a browser tab keeps working,
    but scripted cross-origin writes are refused."""

    def __init__(self, app, port: int):
        super().__init__(app)
        self.port = port
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in self.allowed_hosts:
            return JSONResponse({"error": "bad_host", "message": f"unexpected Host header '{host[:80]}'"}, status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and origin not in self.allowed_origins:
                return JSONResponse({"error": "bad_origin", "message": "cross-origin write rejected"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "cross_site", "message": "cross-site write rejected"}, status_code=403)
        return await call_next(request)


# ----------------------------------------------------------------- errors

def error_payload(exc: Exception) -> tuple[int, dict[str, str]]:
    if isinstance(exc, NotFound):
        return 404, {"error": "not_found", "message": str(exc)}
    if isinstance(exc, engine.EngineError):
        return 400, {"error": exc.code, "message": exc.message}
    if isinstance(exc, Unavailable):
        return 409, {"error": f"{exc.capability}_unavailable", "message": str(exc)}
    if isinstance(exc, BackendError):
        return 502, {"error": "backend_error", "message": str(exc)[:600]}
    if isinstance(exc, (comfy_driver.WorkflowError, comfy_driver.ValidationError)):
        return 400, {"error": "bad_workflow", "message": str(exc)}
    if isinstance(exc, timeline_mod.TimelineError):
        return 400, {"error": "bad_timeline", "message": str(exc)}
    if isinstance(exc, DesignError):
        return 400, {"error": "bad_design", "message": str(exc)}
    if isinstance(exc, voices_mod.VoiceError):
        return 400, {"error": exc.code, "message": str(exc)}
    if isinstance(exc, ValueError):
        return 400, {"error": "bad_request", "message": str(exc)}
    return 500, {"error": "internal_error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"}


# ----------------------------------------------------------------- bodies

class BackendOverrides(BaseModel):
    faustus_url: Optional[str] = None
    faustus_token: Optional[str] = None
    comfy_url: Optional[str] = None
    vram_estimates_mb: Optional[dict[str, int]] = None
    import_roots: Optional[list[str]] = None


class CreateProjectBody(BaseModel):
    name: str
    brief: Optional[str] = None


class UpdateProjectBody(BaseModel):
    name: Optional[str] = None
    brief: Optional[str] = None
    cover_asset_id: Optional[str] = None


class CastBody(BaseModel):
    action: str = "list"
    kind: str = "character"  # "character" | "group"
    id: Optional[str] = None
    name: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CharacterBody(BaseModel):
    name: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ComposeBody(BaseModel):
    prompt: str
    negative: Optional[str] = None
    style: Optional[str] = None


class GenerateImageBody(BaseModel):
    prompt: str
    style: Optional[str] = None
    negative: Optional[str] = None
    aspect: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    steps: Optional[int] = None
    cfg: Optional[float] = None
    sampler: Optional[str] = None
    scheduler: Optional[str] = None
    seed: Optional[int] = None
    count: int = 1
    reference_asset_id: Optional[str] = None
    strength: Optional[float] = None
    template: Optional[str] = None
    checkpoint: Optional[str] = None
    use_character_reference: bool = False
    wait_s: float = 0


class EditImageBody(BaseModel):
    asset_id: str
    operation: str
    prompt: Optional[str] = None
    strength: Optional[float] = None
    mask_asset_id: Optional[str] = None
    count: int = 1
    seed: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    wait_s: float = 0


class AnimateBody(BaseModel):
    asset_id: str
    frames: int = 14
    fps: int = 7
    motion: int = 127
    seed: Optional[int] = None
    wait_s: float = 0


class VoiceBody(BaseModel):
    text: str
    character_id: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None


class ImportBody(BaseModel):
    path: str
    kind: Optional[str] = None


class LyricsBody(BaseModel):
    text: Optional[str] = None
    lrc_text: Optional[str] = None
    lines: Optional[list[dict[str, Any]]] = None
    name: Optional[str] = None


class DesignBody(BaseModel):
    template: str
    fields: dict[str, Any] = Field(default_factory=dict)
    image_asset_id: Optional[str] = None
    variant: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)


class PhotocardSetBody(BaseModel):
    group_id: str
    template_front: str = "photocard_front"
    template_back: str = "photocard_back"
    image_asset_ids: Optional[dict[str, str]] = None


class TimelineBody(BaseModel):
    action: str = "auto"
    song_asset_id: Optional[str] = None
    asset_ids: Optional[list[str]] = None
    board_id: Optional[str] = None
    aspect: str = "9:16"
    lyrics_asset_id: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)
    timeline_id: Optional[str] = None
    patch: dict[str, Any] = Field(default_factory=dict)


class RenderBody(BaseModel):
    timeline_id: str
    quality: str = "preview"
    wait_s: float = 0


class UpdateAssetBody(BaseModel):
    tags: Optional[list[str]] = None
    rating: Optional[int] = None
    favourite: Optional[bool] = None
    notes: Optional[str] = None
    name: Optional[str] = None


class BoardBody(BaseModel):
    name: str
    kind: str = "moodboard"


class BoardUpdateBody(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None


class BoardItemsBody(BaseModel):
    items: list[dict[str, Any]]


class WorkflowImportBody(BaseModel):
    name: str = "Imported workflow"
    workflow: dict[str, Any]


class WorkflowUpdateBody(BaseModel):
    name: Optional[str] = None
    map: Optional[dict[str, str]] = None
    vram_class: Optional[str] = None
    kind: Optional[str] = None
    output_node: Optional[str] = None
    reference_node: Optional[str] = None


class PreviewBody(BaseModel):
    template: str
    fields: dict[str, Any] = Field(default_factory=dict)
    variant: Optional[str] = None


# ------------------------------------------------------------------- app

def create_app(data_dir: Path, static_dir: Optional[Path] = None, port: int = 8815, demo: bool = False) -> FastAPI:
    data_dir = Path(data_dir)
    store = Store(data_dir)
    backend = Backend(data_dir, demo=demo)
    queue = JobQueue(store)
    queue.register("generate_image", lambda job, p: engine.generate_image(store, backend, job, p))
    queue.register("edit_image", lambda job, p: engine.edit_image(store, backend, job, p))
    queue.register("animate", lambda job, p: engine.animate_image(store, backend, job, p))
    queue.register("render_timeline", lambda job, p: engine.render_timeline_job(store, backend, job, p))
    queue.register("download_voice", lambda job, p: _download_voice_job(store, job, p))
    queue.start()

    app = FastAPI(title="Prospero's Hoard", version=__version__)
    app.add_middleware(GuardMiddleware, port=port)
    app.state.store = store
    app.state.backend = backend
    app.state.queue = queue

    async def _any_error(request: Request, exc: Exception):
        status, payload = error_payload(exc)
        if status >= 500:
            logger.error("unhandled error on %s", request.url.path, exc_info=exc)
        return JSONResponse(payload, status_code=status)

    # Registered per class (not for bare Exception, which Starlette routes to
    # its server-error middleware and re-raises): every failure a handler can
    # hit becomes {"error", "message"} JSON.
    for exc_type in (HoardLinkError, ValueError, LookupError, RuntimeError, OSError, TypeError, AttributeError,
                     ArithmeticError, AssertionError):
        app.add_exception_handler(exc_type, _any_error)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):  # noqa: ARG001
        parts = []
        for err in exc.errors()[:5]:
            loc = ".".join(str(x) for x in err.get("loc", []) if x not in ("body", "query"))
            parts.append(f"{loc}: {err.get('msg')}")
        return JSONResponse({"error": "invalid_arguments", "message": "; ".join(parts) or "invalid request"}, status_code=422)

    def agent(tool: str, args_summary: str, fn: Callable[[], Any]) -> Any:
        """Run an agent operation and record it in `agent_calls` (the
        'What the assistant did' view), including failures."""
        start = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            _, payload = error_payload(exc)
            store.record_agent_call(tool, args_summary, (time.monotonic() - start) * 1000, ok=False,
                                    error=f"{payload['error']}: {payload['message']}")
            raise
        store.record_agent_call(tool, args_summary, (time.monotonic() - start) * 1000, ok=True)
        return result

    def wait(job: dict[str, Any], wait_s: float) -> dict[str, Any]:
        if wait_s and wait_s > 0:
            return queue.wait_for(job["id"], min(float(wait_s), MAX_WAIT_S))
        return job

    def job_result(job: dict[str, Any]) -> dict[str, Any]:
        view = engine.job_view(job)
        if job["state"] == "done" and view.get("asset_ids"):
            view["assets"] = [engine.asset_view(store.get_asset(a)) for a in view["asset_ids"][:8]]
        return view

    # ---------------------------------------------------------- operations
    # shared by the agent endpoints (compact) and the UI endpoints (full)

    def op_generate(project: str, body: GenerateImageBody) -> dict[str, Any]:
        composed = engine.compose_prompt(store, project, body.prompt, body.negative, body.style)
        reference = body.reference_asset_id
        if not reference and body.use_character_reference:
            reference = composed["reference_asset_id"]
        template = body.template or ("sdxl_img2img" if reference else "sdxl_txt2img")
        width, height = body.width, body.height
        if body.aspect and not (width and height):
            if body.aspect not in engine.ASPECT_SIZES:
                raise engine.EngineError("bad_aspect", f"aspect must be one of {', '.join(engine.ASPECT_SIZES)}")
            width, height = engine.ASPECT_SIZES[body.aspect]
        for name, value, lo, hi in (("width", width, 256, 2048), ("height", height, 256, 2048), ("steps", body.steps, 1, 150),
                                    ("count", body.count, 1, 8)):
            if value is not None and not lo <= value <= hi:
                raise engine.EngineError("bad_parameter", f"{name} must be between {lo} and {hi}")
        if body.cfg is not None and not 0 <= body.cfg <= 30:
            raise engine.EngineError("bad_parameter", "cfg must be between 0 and 30")
        if body.strength is not None and not 0 < body.strength <= 1:
            raise engine.EngineError("bad_parameter", "strength must be between 0 and 1")
        if reference:
            ref = store.get_asset(reference)
            if ref["kind"] != "image":
                raise engine.EngineError("reference_not_image", f"reference {reference} is {ref['kind']}, not an image")
        try:
            comfy_driver.load_template(template, store.data_dir)
        except comfy_driver.WorkflowError as exc:
            raise engine.EngineError("unknown_template", str(exc)) from None
        seed = body.seed if body.seed is not None else engine.random_seed()
        params = {
            "prompt": body.prompt, "positive_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
            "width": width, "height": height, "steps": body.steps, "cfg": body.cfg, "sampler": body.sampler,
            "scheduler": body.scheduler, "seed": seed, "count": body.count, "reference_asset_id": reference,
            "strength": body.strength, "template": template, "checkpoint": body.checkpoint,
            "style": composed["style"], "style_defaults": composed["style_defaults"],
            "matched_characters": composed["matched_characters"],
        }
        job = queue.enqueue("generate_image", "gpu", params, project_id=project)
        job = wait(job, body.wait_s)
        return {"job": job, "final_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
                "matched_characters": composed["matched_characters"], "unknown_mentions": composed["unknown_mentions"],
                "template": template, "seed": seed}

    def op_edit(body: EditImageBody) -> dict[str, Any]:
        asset = store.get_asset(body.asset_id)
        if asset["kind"] != "image":
            raise engine.EngineError("not_an_image", f"asset {body.asset_id} is {asset['kind']}; edits need an image")
        ops = ("img2img", "inpaint", "hires", "vary", "reuse")
        if body.operation not in ops:
            raise engine.EngineError("bad_operation", f"operation must be one of {', '.join(ops)}")
        if body.operation == "inpaint" and not body.mask_asset_id:
            raise engine.EngineError("mask_required", "inpaint needs mask_asset_id (white = repaint)")
        if body.operation in ("vary", "reuse") and (asset.get("recipe") or {}).get("backend") != "comfyui":
            raise engine.EngineError("not_reproducible", f"asset {asset['id']} was not generated on ComfyUI, so it has no recipe to re-run; use img2img")
        if not 1 <= body.count <= 8:
            raise engine.EngineError("bad_parameter", "count must be between 1 and 8")
        job = queue.enqueue("edit_image", "gpu", body.model_dump(exclude={"wait_s"}), project_id=asset["project_id"])
        return {"job": wait(job, body.wait_s)}

    def op_animate(body: AnimateBody) -> dict[str, Any]:
        asset = store.get_asset(body.asset_id)
        if asset["kind"] != "image":
            raise engine.EngineError("not_an_image", f"asset {body.asset_id} is {asset['kind']}; animate needs an image")
        job = queue.enqueue("animate", "gpu", body.model_dump(exclude={"wait_s"}), project_id=asset["project_id"])
        return {"job": wait(job, body.wait_s)}

    def op_render(body: RenderBody) -> dict[str, Any]:
        tl = store.get_timeline(body.timeline_id)
        if body.quality not in ("preview", "final"):
            raise engine.EngineError("bad_quality", "quality must be 'preview' or 'final'")
        job = queue.enqueue("render_timeline", "cpu", {"timeline_id": body.timeline_id, "quality": body.quality},
                            project_id=tl["project_id"])
        return {"job": wait(job, body.wait_s)}

    def op_timeline(project: str, body: TimelineBody) -> dict[str, Any]:
        if body.action == "auto":
            return engine.timeline_auto(store, project, body.song_asset_id, body.asset_ids, body.board_id,
                                        body.aspect, body.lyrics_asset_id, body.options)
        if not body.timeline_id:
            raise engine.EngineError("timeline_required", f"action '{body.action}' needs timeline_id")
        tl = store.get_timeline(body.timeline_id)
        if tl["project_id"] != project:
            raise engine.EngineError("wrong_project", f"timeline {body.timeline_id} belongs to another project")
        if body.action == "get":
            return tl
        if body.action == "update":
            return engine.update_timeline(store, body.timeline_id, body.patch)
        raise engine.EngineError("bad_action", f"unknown timeline action '{body.action}'; use auto, get or update")

    def op_design(project: str, body: DesignBody) -> dict[str, Any]:
        fields = dict(body.fields)
        if body.image_asset_id:
            main = "cover_image" if "cover_image" in design_templates.TEMPLATE_FIELDS.get(body.template, {}) else "image"
            fields.setdefault(main, body.image_asset_id)
        return engine.render_design(store, project, body.template, fields, body.variant,
                                    print_mode=bool(body.options.get("print")))

    def op_cast(project: str, body: CastBody) -> Any:
        store.get_project(project)
        if body.kind not in ("character", "group"):
            raise engine.EngineError("bad_kind", "kind must be 'character' or 'group'")
        if body.action == "list":
            return {"characters": store.list_characters(project), "groups": store.list_groups(project)}
        if body.action == "create":
            if not body.name:
                raise engine.EngineError("name_required", f"creating a {body.kind} needs a name")
            if body.kind == "group":
                return store.create_group(project, body.name, **body.fields)
            return store.create_character(project, body.name, **body.fields)
        if body.action == "update":
            if not body.id:
                raise engine.EngineError("id_required", f"updating a {body.kind} needs its id")
            fields = dict(body.fields)
            if body.name:
                fields["name"] = body.name
            target = store.get_group(body.id) if body.kind == "group" else store.get_character(body.id)
            if target["project_id"] != project:
                raise engine.EngineError("wrong_project", f"{body.kind} {body.id} belongs to another project")
            if body.kind == "group":
                return store.update_group(body.id, **fields)
            return store.update_character(body.id, **fields)
        raise engine.EngineError("bad_action", f"unknown cast action '{body.action}'; use list, create or update")

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    def health():
        active = store.list_jobs(state="active", limit=50)["items"]
        return {
            "service": "prosperos-hoard", "name": "Prospero's Hoard", "version": __version__, "status": "ok",
            "demo": demo, "projects": len(store.list_projects(limit=50)["items"]),
            "active_jobs": len(active),
        }

    @app.get("/api/backend")
    def get_backend():
        return backend.status()

    @app.post("/api/backend")
    def set_backend(body: BackendOverrides):
        if body.comfy_url:
            _check_loopback_or_lan_url(body.comfy_url)
        if body.faustus_url:
            _check_loopback_or_lan_url(body.faustus_url)
        backend.set_overrides(body.faustus_url, body.faustus_token, body.comfy_url, body.vram_estimates_mb, body.import_roots)
        return backend.status()

    @app.post("/api/backend/comfy/free")
    def free_comfy():
        return backend.free_comfy_memory()

    @app.get("/api/agent-calls")
    def agent_calls(limit: int = 50):
        return {"items": store.list_agent_calls(limit)}

    # ------------------------------------------------------------ projects
    @app.post("/api/agent/studio_create_project")
    def agent_create_project(body: CreateProjectBody):
        p = agent("studio_create_project", body.name[:80], lambda: store.create_project(body.name, body.brief))
        return {"id": p["id"], "name": p["name"], "brief": p.get("brief")}

    @app.get("/api/agent/studio_projects")
    def agent_projects(query: Optional[str] = None, limit: int = 10, offset: int = 0):
        def run():
            res = store.list_projects(query, limit, offset)
            res["items"] = [{"id": p["id"], "name": p["name"], "brief": engine._clip(p.get("brief"), 160),
                             "counts": p["counts"], "updated_at": p["updated_at"]} for p in res["items"]]
            return res
        return agent("studio_projects", query or "", run)

    @app.get("/api/projects")
    def list_projects(query: Optional[str] = None, limit: int = 50, offset: int = 0):
        return store.list_projects(query, limit, offset)

    @app.post("/api/projects")
    def create_project(body: CreateProjectBody):
        return store.create_project(body.name, body.brief)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return store.get_project(project_id)

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, body: UpdateProjectBody):
        return store.update_project(project_id, body.name, body.brief, body.cover_asset_id)

    # ------------------------------------------------------------------ cast
    @app.post("/api/agent/studio_cast")
    def agent_cast(project: str, body: CastBody):
        def run():
            result = op_cast(project, body)
            if body.action == "list":
                return {
                    "characters": [_character_view(c) for c in result["characters"]],
                    "groups": result["groups"],
                    "hint": "mention characters in prompts as @Name",
                }
            return _character_view(result) if body.kind == "character" else result
        return agent("studio_cast", f"{body.action}:{body.kind}:{body.name or body.id or ''}", run)

    @app.get("/api/projects/{project_id}/characters")
    def list_characters(project_id: str):
        return {"items": store.list_characters(project_id)}

    @app.post("/api/projects/{project_id}/characters")
    def create_character(project_id: str, body: CharacterBody):
        return op_cast(project_id, CastBody(action="create", kind="character", name=body.name, fields=body.fields))

    @app.patch("/api/characters/{character_id}")
    def update_character(character_id: str, body: CharacterBody):
        fields = dict(body.fields)
        if body.name:
            fields["name"] = body.name
        return store.update_character(character_id, **fields)

    @app.get("/api/projects/{project_id}/groups")
    def list_groups(project_id: str):
        return {"items": store.list_groups(project_id)}

    @app.post("/api/projects/{project_id}/groups")
    def create_group(project_id: str, body: CharacterBody):
        return op_cast(project_id, CastBody(action="create", kind="group", name=body.name, fields=body.fields))

    @app.patch("/api/groups/{group_id}")
    def update_group(group_id: str, body: CharacterBody):
        fields = dict(body.fields)
        if body.name:
            fields["name"] = body.name
        return store.update_group(group_id, **fields)

    @app.get("/api/style-presets")
    def style_presets(project: Optional[str] = None):
        return {"items": store.list_style_presets(project)}

    @app.post("/api/projects/{project_id}/compose-prompt")
    def compose(project_id: str, body: ComposeBody):
        return engine.compose_prompt(store, project_id, body.prompt, body.negative, body.style)

    # -------------------------------------------------------------- generate
    @app.post("/api/agent/studio_generate_image")
    def agent_generate_image(project: str, body: GenerateImageBody):
        def run():
            res = op_generate(project, body)
            res["job"] = job_result(res["job"])
            return res
        return agent("studio_generate_image", body.prompt[:120], run)

    @app.post("/api/projects/{project_id}/generate")
    def ui_generate(project_id: str, body: GenerateImageBody):
        return op_generate(project_id, body)

    @app.post("/api/agent/studio_edit_image")
    def agent_edit_image(body: EditImageBody):
        return agent("studio_edit_image", f"{body.asset_id}:{body.operation}",
                     lambda: {"job": job_result(op_edit(body)["job"])})

    @app.post("/api/assets/{asset_id}/edit")
    def ui_edit(asset_id: str, body: EditImageBody):
        body.asset_id = asset_id
        return op_edit(body)

    @app.post("/api/agent/studio_animate")
    def agent_animate(body: AnimateBody):
        return agent("studio_animate", body.asset_id, lambda: {"job": job_result(op_animate(body)["job"])})

    @app.post("/api/assets/{asset_id}/animate")
    def ui_animate(asset_id: str, body: AnimateBody):
        body.asset_id = asset_id
        return op_animate(body)

    @app.get("/api/workflows")
    def workflows():
        return {"builtin": comfy_driver.list_builtin_templates(), "custom": comfy_driver.list_custom_workflows(store.data_dir)}

    @app.post("/api/workflows/import")
    def import_workflow(body: WorkflowImportBody):
        return comfy_driver.import_custom_workflow(store.data_dir, body.name, body.workflow)

    @app.post("/api/workflows/import-file")
    async def import_workflow_file(file: UploadFile, name: Optional[str] = None):
        raw = await file.read(comfy_driver.MAX_WORKFLOW_BYTES + 1)
        return comfy_driver.import_custom_workflow(store.data_dir, name or Path(file.filename or "workflow").stem, raw)

    @app.patch("/api/workflows/{wf_id}")
    def update_workflow(wf_id: str, body: WorkflowUpdateBody):
        return comfy_driver.update_custom_workflow(store.data_dir, wf_id, body.model_dump(exclude_none=True))

    # ------------------------------------------------------------------ voice
    @app.post("/api/agent/studio_voice")
    def agent_voice(project: str, body: VoiceBody):
        def run():
            asset = engine.voice_line(store, backend, project, body.text, body.character_id, body.voice, body.speed)
            return {**engine.asset_view(asset), "provider": (asset.get("recipe") or {}).get("provider")}
        return agent("studio_voice", body.text[:80], run)

    @app.post("/api/projects/{project_id}/voice")
    def ui_voice(project_id: str, body: VoiceBody):
        return engine.voice_line(store, backend, project_id, body.text, body.character_id, body.voice, body.speed)

    @app.get("/api/voices")
    def voices_list():
        return {"items": voices_mod.list_curated_voices(store.data_dir / "voices"), "piper_installed": voices_mod.piper_installed()}

    @app.post("/api/voices/{voice_id}/download")
    def voice_download(voice_id: str):
        if not any(v["id"] == voice_id for v in voices_mod.CURATED_VOICES):
            raise engine.EngineError("unknown_voice", f"unknown voice '{voice_id}'")
        return queue.enqueue("download_voice", "cpu", {"voice_id": voice_id})

    # ------------------------------------------------------------------ import
    @app.post("/api/agent/studio_import")
    def agent_import(project: str, body: ImportBody):
        def run():
            path = engine.resolve_import_path(backend, store, body.path)
            return engine.asset_view(engine.import_asset(store, project, path, body.kind))
        return agent("studio_import", body.path[:200], run)

    @app.post("/api/projects/{project_id}/import-path")
    def ui_import_path(project_id: str, body: ImportBody):
        path = engine.resolve_import_path(backend, store, body.path)
        return engine.import_asset(store, project_id, path, body.kind)

    @app.post("/api/projects/{project_id}/import-upload")
    async def import_upload(project_id: str, file: UploadFile, kind: Optional[str] = None):
        store.get_project(project_id)
        original = Path((file.filename or "upload").replace("\\", "/")).name[:200] or "upload"
        ext = Path(original).suffix.lower()
        cap = MAX_UPLOAD_IMAGE if ext in engine.IMAGE_EXTS else MAX_UPLOAD_MEDIA
        tmp_dir = store.data_dir / "tmp" / "uploads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{new_id('up')}{ext if re.fullmatch(r'[.a-z0-9]{1,6}', ext) else ''}"
        total = 0
        try:
            with tmp_path.open("wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > cap:
                        raise engine.EngineError("too_large", f"file exceeds {cap // (1024 * 1024)} MB")
                    fh.write(chunk)
            return engine.import_asset(store, project_id, tmp_path, kind, original_name=original)
        finally:
            tmp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ audio
    @app.post("/api/agent/studio_analyze_audio")
    def agent_analyze_audio(asset_id: str):
        return agent("studio_analyze_audio", asset_id,
                     lambda: engine.analysis_view(asset_id, engine.analyze_audio(store, asset_id)))

    @app.post("/api/assets/{asset_id}/analyze")
    def ui_analyze(asset_id: str, force: bool = False):
        return engine.analyze_audio(store, asset_id, force=force)

    @app.get("/api/assets/{asset_id}/lyrics")
    def get_lyrics(asset_id: str):
        return engine.read_lyrics(store, asset_id)

    @app.put("/api/assets/{asset_id}/lyrics")
    def save_lyrics(asset_id: str, body: LyricsBody):
        text = _lyrics_text(body)
        return engine.save_lyrics(store, asset_id, text)

    @app.post("/api/projects/{project_id}/lyrics")
    def create_lyrics(project_id: str, body: LyricsBody):
        return engine.create_lyrics(store, project_id, _lyrics_text(body), body.name)

    # --------------------------------------------------------------- design
    @app.post("/api/agent/studio_design")
    def agent_design(project: str, body: DesignBody):
        return agent("studio_design", body.template, lambda: engine.asset_view(op_design(project, body)))

    @app.post("/api/projects/{project_id}/design")
    def ui_design(project_id: str, body: DesignBody):
        return op_design(project_id, body)

    @app.post("/api/design/preview")
    def design_preview(body: PreviewBody):
        return Response(engine.preview_design(store, body.template, body.fields, body.variant), media_type="image/jpeg")

    @app.post("/api/agent/studio_photocard_set")
    def agent_photocard_set(project: str, body: PhotocardSetBody):
        return agent("studio_photocard_set", body.group_id, lambda: engine.photocard_set(
            store, project, body.group_id, body.template_front, body.template_back, body.image_asset_ids))

    @app.post("/api/projects/{project_id}/photocard-set")
    def ui_photocard_set(project_id: str, body: PhotocardSetBody):
        return engine.photocard_set(store, project_id, body.group_id, body.template_front, body.template_back, body.image_asset_ids)

    @app.get("/api/design/templates")
    def design_templates_list():
        return {"items": design_templates.describe_templates()}

    # ------------------------------------------------------------- timeline
    @app.post("/api/agent/studio_timeline")
    def agent_timeline(project: str, body: TimelineBody):
        def run():
            tl = op_timeline(project, body)
            opts = body.options or {}
            return timeline_mod.compact_view(tl, clip_offset=int(opts.get("clip_offset", 0)), clip_limit=int(opts.get("clip_limit", 16)))
        return agent("studio_timeline", f"{body.action}:{body.timeline_id or body.song_asset_id or ''}", run)

    @app.get("/api/projects/{project_id}/timelines")
    def list_timelines(project_id: str):
        return {"items": store.list_timelines(project_id)}

    @app.post("/api/projects/{project_id}/timelines/auto")
    def ui_timeline_auto(project_id: str, body: TimelineBody):
        body.action = "auto"
        return op_timeline(project_id, body)

    @app.get("/api/timelines/{timeline_id}")
    def get_timeline(timeline_id: str):
        return store.get_timeline(timeline_id)

    @app.patch("/api/timelines/{timeline_id}")
    def patch_timeline(timeline_id: str, body: dict[str, Any]):
        return engine.update_timeline(store, timeline_id, body)

    @app.post("/api/agent/studio_render")
    def agent_render(body: RenderBody):
        return agent("studio_render", f"{body.timeline_id}:{body.quality}", lambda: {"job": job_result(op_render(body)["job"])})

    @app.post("/api/timelines/{timeline_id}/render")
    def ui_render(timeline_id: str, body: RenderBody):
        body.timeline_id = timeline_id
        return op_render(body)

    # ----------------------------------------------------------------- jobs
    @app.get("/api/agent/studio_jobs")
    def agent_jobs(state: Optional[str] = None, limit: int = 10, offset: int = 0):
        def run():
            res = store.list_jobs(state, limit, offset)
            res["items"] = [engine.job_view(j) for j in res["items"]]
            return res
        return agent("studio_jobs", state or "", run)

    @app.get("/api/agent/studio_job")
    def agent_job(job_id: str, wait_s: float = 0):
        def run():
            job = queue.wait_for(job_id, min(wait_s, MAX_WAIT_S)) if wait_s > 0 else store.get_job(job_id)
            return job_result(job)
        return agent("studio_job", job_id, run)

    @app.post("/api/agent/studio_cancel_job")
    def agent_cancel(job_id: str):
        return agent("studio_cancel_job", job_id, lambda: engine.job_view(queue.cancel(job_id)))

    @app.get("/api/jobs")
    def list_jobs(state: Optional[str] = None, limit: int = 30, offset: int = 0, project: Optional[str] = None):
        return store.list_jobs(state, limit, offset, project_id=project)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return store.get_job(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        return queue.cancel(job_id)

    # --------------------------------------------------------------- assets
    @app.get("/api/agent/studio_assets")
    def agent_assets(project: str, kind: Optional[str] = None, query: Optional[str] = None,
                     tag: Optional[str] = None, favourite: Optional[bool] = None, limit: int = 12, offset: int = 0):
        def run():
            store.get_project(project)
            res = store.list_assets(project, kind, query, tag, favourite, min(limit, 30), offset)
            res["items"] = [engine.asset_view(a) for a in res["items"]]
            return res
        return agent("studio_assets", f"{kind or ''}:{query or ''}:{tag or ''}", run)

    @app.get("/api/projects/{project_id}/assets")
    def list_assets(project_id: str, kind: Optional[str] = None, query: Optional[str] = None,
                    tag: Optional[str] = None, favourite: Optional[bool] = None, limit: int = 60, offset: int = 0,
                    source: Optional[str] = None, min_rating: Optional[int] = None):
        res = store.list_assets(project_id, kind, query, tag, favourite, limit, offset, source=source, min_rating=min_rating)
        for a in res["items"]:
            a.pop("analysis", None)
            a.pop("waveform", None)
        return res

    @app.get("/api/assets/{asset_id}")
    def get_asset(asset_id: str):
        return store.get_asset(asset_id)

    @app.patch("/api/assets/{asset_id}")
    def update_asset(asset_id: str, body: UpdateAssetBody):
        return store.update_asset(asset_id, **body.model_dump(exclude_none=True))

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id: str, download: bool = False):
        asset = store.get_asset(asset_id)
        path = _asset_path(store, asset["file_path"])
        filename = _download_name(asset, path) if download else None
        return FileResponse(path, media_type=asset.get("mime") or mimetypes.guess_type(path.name)[0], filename=filename)

    @app.get("/api/assets/{asset_id}/thumb")
    def asset_thumb(asset_id: str):
        asset = store.get_asset(asset_id)
        if not asset.get("thumb_path"):
            return JSONResponse({"error": "no_thumb", "message": "asset has no thumbnail"}, status_code=404)
        return FileResponse(_asset_path(store, asset["thumb_path"]), media_type="image/webp")

    @app.get("/api/agent/studio_show")
    def agent_show(asset_ids: str, size: int = 768):
        def run():
            images = engine.show_assets(store, asset_ids.split(","), size)
            return {"items": [{"asset_id": i["asset_id"], "kind": i["kind"], "mime": i["mime"],
                               "base64": base64.b64encode(i["bytes"]).decode("ascii"), **({"order": i["order"]} if "order" in i else {})}
                              for i in images]}
        return agent("studio_show", asset_ids[:200], run)

    @app.get("/api/agent/studio_lineage")
    def agent_lineage(asset_id: str):
        return agent("studio_lineage", asset_id, lambda: engine.get_lineage(store, asset_id))

    @app.get("/api/assets/{asset_id}/lineage")
    def ui_lineage(asset_id: str):
        return engine.get_lineage(store, asset_id)

    # ---------------------------------------------------------------- boards
    @app.post("/api/projects/{project_id}/boards")
    def create_board(project_id: str, body: BoardBody):
        return store.create_board(project_id, body.name, body.kind)

    @app.get("/api/projects/{project_id}/boards")
    def list_boards(project_id: str):
        return {"items": store.list_boards(project_id)}

    @app.patch("/api/boards/{board_id}")
    def update_board(board_id: str, body: BoardUpdateBody):
        return store.update_board(board_id, body.name, body.kind)

    @app.delete("/api/boards/{board_id}")
    def delete_board(board_id: str):
        store.delete_board(board_id)
        return {"ok": True, "deleted": board_id}

    @app.put("/api/boards/{board_id}/items")
    def update_board_items(board_id: str, body: BoardItemsBody):
        return store.update_board_items(board_id, body.items)

    # ------------------------------------------------------------- status --
    @app.get("/api/agent/studio_status")
    def agent_status():
        def run():
            status = backend.status()
            link = status["hoard_link"]
            counts = {s: len(store.list_jobs(state=s, limit=50)["items"]) for s in ("queued", "waiting_gpu", "running")}
            return {
                "demo_backend": status["demo"],
                "capabilities": {cap: {k: v for k, v in {"state": r.get("state"), "provider": r.get("provider"),
                                                         "model": r.get("model"), "reason": engine._clip(r.get("reason"), 140)}.items() if v}
                                 for cap, r in link.items()},
                "comfyui": {k: status["comfy"].get(k) for k in ("reachable", "url", "checkpoints", "vram_free_mb", "reason")},
                "ffmpeg": status["ffmpeg"]["found"],
                "piper_tts": status["piper"]["installed"],
                "music_generation": [{"name": m["name"], "available": m["available"], "reason": m["reason"]} for m in status["music"]],
                "vram_estimates_mb": status["vram_estimates_mb"],
                "queue": counts,
                "recent_jobs": [engine.job_view(j) for j in store.list_jobs(limit=5)["items"]],
            }
        return agent("studio_status", "", run)

    # -------------------------------------------------------- static / SPA
    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_not_found(rest: str):
        return JSONResponse({"error": "not_found", "message": f"no API route /api/{rest[:100]}"}, status_code=404)

    if static_dir and Path(static_dir).is_dir() and (Path(static_dir) / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles

        static_root = Path(static_dir).resolve()
        if (static_root / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=str(static_root / "assets")), name="ui-assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path:
                candidate = (static_root / full_path).resolve()
                if candidate.is_file() and _is_within(candidate, static_root):
                    return FileResponse(candidate)
            return FileResponse(static_root / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        @app.get("/")
        def no_ui():
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;background:#141018;color:#eee;padding:2rem'>"
                "<h1>Prospero's Hoard</h1>"
                "<p>No built frontend found. Build it with:</p>"
                "<pre>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</pre>"
                "<p>The API is live at <a style='color:#ff4d8d' href='/api/health'>/api/health</a>.</p>"
                "</body></html>"
            )

    return app


# ---------------------------------------------------------------- helpers

def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _asset_path(store: Store, rel: str) -> Path:
    """Files are only ever served by asset id: the stored relative path is
    re-checked to stay inside the data folder."""
    root = store.data_dir.resolve()
    path = (root / rel).resolve()
    if not _is_within(path, root) or not path.is_file():
        raise NotFound("file", rel)
    return path


def _download_name(asset: dict[str, Any], path: Path) -> str:
    base = re.sub(r"[^\w\- .]+", "", asset.get("name") or asset["id"]).strip()[:80] or asset["id"]
    return base if base.lower().endswith(path.suffix.lower()) else f"{base}{path.suffix}"


def _lyrics_text(body: LyricsBody) -> str:
    if body.text is not None:
        return body.text
    if body.lrc_text is not None:
        return body.lrc_text
    if body.lines is not None:
        clean = []
        for ln in body.lines:
            try:
                clean.append({"time_s": float(ln["time_s"]), "text": str(ln.get("text", ""))[:300]})
            except (KeyError, TypeError, ValueError):
                raise engine.EngineError("bad_lyrics", "each line needs time_s (seconds) and text") from None
        return audio_mod.to_lrc(clean)
    raise engine.EngineError("bad_lyrics", "send text (LRC or plain) or lines [{time_s, text}]")


def _character_view(c: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in {
        "id": c["id"], "name": c["name"], "role": c.get("role"), "prompt": engine._clip(c.get("prompt"), 240),
        "negative": engine._clip(c.get("negative"), 160), "palette": c.get("palette") or None,
        "canonical_asset_id": c.get("canonical_asset_id"), "voice": c.get("voice"), "bio": engine._clip(c.get("bio"), 200),
    }.items() if v is not None}


def _check_loopback_or_lan_url(url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise engine.EngineError("bad_url", f"'{url[:100]}' is not an http(s) URL")


def _download_voice_job(store: Store, job: dict[str, Any], progress) -> dict[str, Any]:
    voice_id = job["params"]["voice_id"]
    progress(0.1, f"downloading {voice_id} from Hugging Face")
    voices_mod.download_voice(store.data_dir / "voices", voice_id)
    return {"voice_id": voice_id}
