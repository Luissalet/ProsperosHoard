"""FastAPI application: HTTP surface, browser-attack guard, `/api/agent/*`
(exactly what the MCP tools call, compact and id-first), `/api/backend`,
and the richer UI endpoints. See `docs/API.md` for every route.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from . import animatic as animatic_mod
from . import audio as audio_mod
from . import comfy_driver, engine, procutil
from . import dubbing as dubbing_mod
from . import productions as productions_mod
from . import qa as qa_mod
from . import recipes as recipes_mod
from . import templates as design_templates
from . import timeline as timeline_mod
from . import voice_engines as ve
from . import voice_lab
from . import voice_pipelines as vp
from . import voices as voices_mod
from .backend import Backend
from .design import DesignError
from .hoard_link.errors import BackendError, HoardLinkError, Unavailable
from .ids import new_id
from .jobs import JobQueue
from .store import NotFound, Store
from .voice_lab import VoiceLabError

logger = logging.getLogger("prosperos_hoard.api")

MAX_UPLOAD_MEDIA = engine.MAX_MEDIA_BYTES
MAX_UPLOAD_IMAGE = engine.MAX_IMAGE_BYTES
MAX_WAIT_S = 300.0
# request bodies: file uploads get the media cap, everything else (JSON) far less
MAX_JSON_BODY = 64 * 1024 * 1024
_UPLOAD_PATH_RE = re.compile(r"^/api/(projects/[^/]+/import-upload|workflows/import-file|voice/voices/upload|"
                             r"voice/transcribe/upload|voice/dictate)$")


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
            # refused before the body is read: a multipart body is spooled to
            # the system temp folder before a route could check its size
            cap = MAX_UPLOAD_MEDIA + 16 * 1024 * 1024 if _UPLOAD_PATH_RE.match(request.url.path) else MAX_JSON_BODY
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    too_big = int(length) > cap
                except ValueError:
                    return JSONResponse({"error": "bad_request", "message": "invalid Content-Length"}, status_code=400)
                if too_big:
                    return JSONResponse({"error": "too_large", "message": f"request body exceeds {cap // (1024 * 1024)} MB"},
                                        status_code=413)
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
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
        return 503, {"error": "database_busy", "message": "the database is busy; retry in a moment"}
    if isinstance(exc, subprocess.TimeoutExpired):
        return 504, {"error": "subprocess_timeout", "message": f"{Path(str(exc.cmd[0] if isinstance(exc.cmd, (list, tuple)) else exc.cmd)).name} timed out"}
    if isinstance(exc, (VoiceLabError, vp.AudiobookError, dubbing_mod.DubbingError, ve.EngineNotInstalled)):
        code = exc.code if hasattr(exc, "code") else "engine_not_installed"
        return 400, {"error": code, "message": str(exc)}
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
    render_pool: Optional[list[str]] = None  # extra ComfyUI servers, one per GPU; applied on restart


class CreateProjectBody(BaseModel):
    name: str
    brief: Optional[str] = None
    image_engine: Optional[str] = None  # "auto" (default) | "qwen21" | "flux" | "sdxl"


class UpdateProjectBody(BaseModel):
    name: Optional[str] = None
    brief: Optional[str] = None
    cover_asset_id: Optional[str] = None
    image_engine: Optional[str] = None  # "auto" | "qwen21" | "flux" | "sdxl" - see engine.IMAGE_ENGINES


class CastBody(BaseModel):
    action: str = "list"
    kind: str = "character"  # "character" | "group"
    id: Optional[str] = None
    name: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)


class CharacterBody(BaseModel):
    name: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ComposePromptBody(BaseModel):
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
    reference_asset_ids: Optional[list[str]] = None  # qwen21_edit: up to 10, image_1 first
    strength: Optional[float] = None
    template: Optional[str] = None
    checkpoint: Optional[str] = None
    engine: Optional[str] = None  # "auto" | "qwen21" | "flux" | "sdxl" - defaults to the project's
    use_character_reference: bool = False
    consistent: bool = False
    wait_s: float = 0


class TimeLyricsBody(BaseModel):
    song_asset_id: str
    lyrics: str
    name: Optional[str] = None


class ComposeSongBody(BaseModel):
    tags: str
    lyrics: str
    bpm: int = 120
    duration: float = 120.0
    key: str = "C major"
    language: str = "en"
    time_signature: int = 4
    seed: Optional[int] = None
    checkpoint: Optional[str] = None
    count: int = 1
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
    group_id: Optional[str] = None
    character_id: Optional[str] = None
    cards: Optional[list[dict[str, Any]]] = None
    set_name: Optional[str] = None
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


class ProductionCreateBody(BaseModel):
    name: str
    spec: dict[str, Any]
    settings: Optional[dict[str, Any]] = None
    project: Optional[str] = None


class ProductionShotsBody(BaseModel):
    changes: list[dict[str, Any]]
    run: bool = True


class QaRunBody(BaseModel):
    production: Optional[str] = None
    stage: str = "all"
    dry_run: bool = True
    keys: Optional[list[str]] = None
    wait_s: float = 120


class AnimaticBody(BaseModel):
    production: Optional[str] = None
    aspects: Optional[list[str]] = None
    wait_s: float = 0


class RecipeExportBody(BaseModel):
    production: str
    name: Optional[str] = None
    overwrite: bool = False


class RecipeRunBody(BaseModel):
    recipe: Optional[str] = None
    cast: dict[str, Any]
    name: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)


class RenderBody(BaseModel):
    timeline_id: str
    quality: str = "preview"
    wait_s: float = 0
    range: Optional[list[float]] = None  # [start_s, end_s]: render only that part of the edit


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


class AgentBoardBody(BaseModel):
    action: str = "list"  # list | get | create | add | set
    board_id: Optional[str] = None
    name: Optional[str] = None
    kind: Optional[str] = None  # moodboard | storyboard | shotlist
    asset_ids: Optional[list[str]] = None
    notes: Optional[dict[str, str]] = None  # asset_id -> note


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


# -------------------------------------------------------------- voice studio

class VoiceSpecBody(BaseModel):
    engine_id: Optional[str] = None
    voice_id: Optional[str] = None  # a saved library voice; supplies engine_id/sample when omitted
    voice_ref: Optional[str] = None  # an engine-native voice/speaker id (e.g. a Piper voice id)
    preset: Optional[str] = None  # a named preset on the library voice
    speed: Optional[float] = None
    pitch: Optional[float] = None
    style: Optional[str] = None
    language: Optional[str] = None


class VoiceCreateBody(BaseModel):
    name: str
    engine_id: str
    source_path: str  # an absolute path to a clean sample (see backend.import_roots)
    language: Optional[str] = None
    project: Optional[str] = None
    tags: Optional[list[str]] = None


class VoicePresetBody(BaseModel):
    name: str
    speed: Optional[float] = None
    pitch: Optional[float] = None
    style: Optional[str] = None


class VoiceUpdateBody(BaseModel):
    name: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class VoiceSpeakBody(BaseModel):
    text: str
    voice: VoiceSpecBody = Field(default_factory=VoiceSpecBody)
    project: Optional[str] = None


class VoiceInstallBody(BaseModel):
    kind: str  # "tts" | "stt"


class VoiceTranscribeBody(BaseModel):
    path: Optional[str] = None  # an absolute path (see backend.import_roots)
    asset_id: Optional[str] = None
    language: Optional[str] = None
    engine_id: Optional[str] = None
    word_timestamps: bool = True


class VoiceAudiobookBody(BaseModel):
    text: Optional[str] = None
    source_path: Optional[str] = None  # .txt/.md/.epub, an absolute path
    title: Optional[str] = None
    voice: VoiceSpecBody = Field(default_factory=VoiceSpecBody)
    format: str = "mp3"  # "mp3" | "m4b"
    project: Optional[str] = None
    wait_s: float = 0


class VoiceDubBody(BaseModel):
    source_path: Optional[str] = None  # an absolute path to a video file
    video_asset_id: Optional[str] = None
    target_language: str
    source_language: Optional[str] = None
    glossary: Optional[dict[str, str]] = None
    voice: VoiceSpecBody = Field(default_factory=VoiceSpecBody)
    stt_engine_id: Optional[str] = None
    title: Optional[str] = None
    project: Optional[str] = None
    wait_s: float = 0


class VoiceResegmentBody(BaseModel):
    text: Optional[str] = None
    voice: Optional[VoiceSpecBody] = None
    remix: bool = True


# ------------------------------------------------------------------- app

def create_app(data_dir: Path, static_dir: Optional[Path] = None, port: int = 8815, demo: bool = False) -> FastAPI:
    data_dir = Path(data_dir)
    store = Store(data_dir)
    backend = Backend(data_dir, demo=demo)
    # one GPU worker for the main ComfyUI plus one per render-pool server
    queue = JobQueue(store, gpu_targets=[None, *backend.render_pool()], bind=backend.bind_comfy,
                     target_ready=backend.pool_server_ready)
    queue.register("generate_image", lambda job, p: engine.generate_image(store, backend, job, p))
    queue.register("edit_image", lambda job, p: engine.edit_image(store, backend, job, p))
    queue.register("animate", lambda job, p: engine.animate_image(store, backend, job, p))
    queue.register("compose_song", lambda job, p: engine.compose_song(store, backend, job, p))
    queue.register("render_timeline", lambda job, p: engine.render_timeline_job(store, backend, job, p))
    queue.register("download_voice", lambda job, p: _download_voice_job(store, job, p))
    queue.register("audiobook", lambda job, p: vp.audiobook_job(store, backend, job, p))
    queue.register("dub", lambda job, p: dubbing_mod.dub_job(store, backend, job, p))
    queue.register("install_voice_engine", lambda job, p: _install_voice_engine_job(job, p))
    # production/QA handlers are registered below, next to the operations
    # they queue sub-jobs through; the workers start at the end of create_app

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
                     ArithmeticError, AssertionError, sqlite3.Error, subprocess.SubprocessError):
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
        proj = store.get_project(project)
        engine_name = engine.resolve_image_engine(engine._object_info(backend), body.engine or proj.get("image_engine"))
        extra_refs = [r for r in (body.reference_asset_ids or []) if r]
        if body.consistent:
            # "Cast -> Reference sheet": route through an edit template with
            # the canonical reference as input (image_1, for Qwen) and the
            # scene as the instruction, instead of a fresh txt2img.
            kontext = engine.build_kontext_instruction(store, project, body.prompt, engine=engine_name)
            primary = body.reference_asset_id or kontext["reference_asset_id"]
            if not primary:
                raise engine.EngineError(
                    "consistent_needs_reference",
                    "consistent=true needs a canonical reference: mention a cast member with a canonical "
                    "reference image (studio_cast update canonical_asset_id), or pass reference_asset_id",
                )
            all_refs = [primary] + [r for r in extra_refs if r != primary]
            composed = {"positive_prompt": kontext["instruction"], "negative_prompt": "", "style": None,
                       "style_defaults": {}, "matched_characters": kontext["matched_characters"],
                       "unknown_mentions": kontext["unknown_mentions"], "reference_asset_id": primary}
            template = body.template or engine.ENGINE_TEMPLATES[engine_name]["edit"]
        else:
            composed = engine.compose_prompt(store, project, body.prompt, body.negative, body.style)
            reference = body.reference_asset_id
            if not reference and body.use_character_reference:
                reference = composed["reference_asset_id"]
            all_refs = extra_refs or ([reference] if reference else [])
            template = body.template or engine.ENGINE_TEMPLATES[engine_name]["edit" if all_refs else "txt2img"]
        reference = all_refs[0] if all_refs else None
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
        for ref_id in all_refs:
            ref = store.get_asset(ref_id)
            if ref["kind"] != "image":
                raise engine.EngineError("reference_not_image", f"reference {ref_id} is {ref['kind']}, not an image")
        try:
            comfy_driver.load_template(template, store.data_dir)
        except comfy_driver.WorkflowError as exc:
            raise engine.EngineError("unknown_template", str(exc)) from None
        seed = body.seed if body.seed is not None else engine.random_seed()
        params = {
            "prompt": body.prompt, "positive_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
            "width": width, "height": height, "steps": body.steps, "cfg": body.cfg, "sampler": body.sampler,
            "scheduler": body.scheduler, "seed": seed, "count": body.count, "reference_asset_id": reference,
            "reference_asset_ids": all_refs or None,
            "strength": body.strength, "template": template, "checkpoint": body.checkpoint,
            "style": composed["style"], "style_defaults": composed["style_defaults"],
            "matched_characters": composed["matched_characters"],
        }
        job = queue.enqueue("generate_image", "gpu", params, project_id=project)
        job = wait(job, body.wait_s)
        return {"job": job, "final_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
                "matched_characters": composed["matched_characters"], "unknown_mentions": composed["unknown_mentions"],
                "template": template, "engine": engine_name, "seed": seed}

    def op_edit(body: EditImageBody) -> dict[str, Any]:
        asset = store.get_asset(body.asset_id)
        if asset["kind"] != "image":
            raise engine.EngineError("not_an_image", f"asset {body.asset_id} is {asset['kind']}; edits need an image")
        ops = ("img2img", "inpaint", "hires", "vary", "reuse")
        if body.operation not in ops:
            raise engine.EngineError("bad_operation", f"operation must be one of {', '.join(ops)}")
        if body.operation == "inpaint" and not body.mask_asset_id:
            raise engine.EngineError("mask_required", "inpaint needs mask_asset_id (white = repaint)")
        if body.operation == "hires" and (asset.get("recipe") or {}).get("template") != "sdxl_txt2img":
            raise engine.EngineError("hires_needs_recipe", "upscale re-runs an SDXL txt2img recipe at a higher resolution; "
                                                           f"asset {asset['id']} was not made that way (use img2img instead)")
        if body.operation in ("vary", "reuse") and (asset.get("recipe") or {}).get("backend") != "comfyui":
            raise engine.EngineError("not_reproducible", f"asset {asset['id']} was not generated on ComfyUI, so it has no recipe to re-run; use img2img")
        if not 1 <= body.count <= 8:
            raise engine.EngineError("bad_parameter", "count must be between 1 and 8")
        job = queue.enqueue("edit_image", "gpu", body.model_dump(exclude={"wait_s"}), project_id=asset["project_id"])
        return {"job": wait(job, body.wait_s)}

    def op_compose(project: str, body: ComposeSongBody) -> dict[str, Any]:
        store.get_project(project)
        if not body.tags.strip():
            raise engine.EngineError("empty_tags", "tags describe the sound (genre, mood, instruments, vocal style)")
        if not body.lyrics.strip():
            raise engine.EngineError("empty_lyrics", "lyrics are required (use [Section] tags in English)")
        if not 40 <= body.bpm <= 220:
            raise engine.EngineError("bad_parameter", "bpm must be between 40 and 220")
        if not 4 <= body.duration <= 240:
            raise engine.EngineError("bad_parameter", "duration must be between 4 and 240 seconds")
        if not 1 <= body.count <= 4:
            raise engine.EngineError("bad_parameter", "count must be between 1 and 4")
        job = queue.enqueue("compose_song", "gpu", body.model_dump(exclude={"wait_s"}), project_id=project)
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
        params: dict[str, Any] = {"timeline_id": body.timeline_id, "quality": body.quality}
        window = engine.render_range(tl, body.range)
        if window:
            params["range"] = window
        job = queue.enqueue("render_timeline", "cpu", params, project_id=tl["project_id"])
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
            return store.create_character(project, body.name, **engine.apply_canonical_crop(store, project, body.fields))
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
            if "canonical_crop" in fields:
                existing_refs = target.get("reference_asset_ids") or []
                fields.setdefault("reference_asset_ids", existing_refs)
                fields = engine.apply_canonical_crop(store, project, fields, target.get("canonical_asset_id"))
            return store.update_character(body.id, **fields)
        raise engine.EngineError("bad_action", f"unknown cast action '{body.action}'; use list, create or update")

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    def health():
        return {
            "service": "prosperos-hoard", "name": "Prospero's Hoard", "version": __version__, "status": "ok",
            "demo": demo, "projects": store.count_projects(),
            "active_jobs": sum(store.count_jobs(("queued", "waiting_gpu", "running")).values()),
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
        backend.set_overrides(body.faustus_url, body.faustus_token, body.comfy_url, body.vram_estimates_mb, body.import_roots,
                              body.render_pool)
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
        def run():
            p = store.create_project(body.name, body.brief)
            if body.image_engine:
                p = store.update_project(p["id"], image_engine=body.image_engine)
            return p
        p = agent("studio_create_project", body.name[:80], run)
        return {"id": p["id"], "name": p["name"], "brief": p.get("brief"), "image_engine": p.get("image_engine")}

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
        p = store.create_project(body.name, body.brief)
        return store.update_project(p["id"], image_engine=body.image_engine) if body.image_engine else p

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return store.get_project(project_id)

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, body: UpdateProjectBody):
        return store.update_project(project_id, body.name, body.brief, body.cover_asset_id, body.image_engine)

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
        character = store.get_character(character_id)
        return op_cast(character["project_id"], CastBody(action="update", kind="character", id=character_id,
                                                         name=body.name, fields=body.fields))

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
    def compose(project_id: str, body: ComposePromptBody):
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

    @app.post("/api/agent/studio_compose")
    def agent_compose(project: str, body: ComposeSongBody):
        return agent("studio_compose", body.tags[:80], lambda: {"job": job_result(op_compose(project, body)["job"])})

    @app.post("/api/projects/{project_id}/compose")
    def ui_compose(project_id: str, body: ComposeSongBody):
        return op_compose(project_id, body)

    @app.post("/api/assets/{asset_id}/animate")
    def ui_animate(asset_id: str, body: AnimateBody):
        body.asset_id = asset_id
        return op_animate(body)

    @app.get("/api/workflows")
    def workflows():
        return {"builtin": comfy_driver.list_builtin_templates(), "custom": comfy_driver.list_custom_workflows(store.data_dir)}

    @app.post("/api/workflows/import")
    def import_workflow(body: WorkflowImportBody):
        return comfy_driver.import_custom_workflow(store.data_dir, body.name, body.workflow,
                                                   object_info=lambda: engine.object_info_live_or_cached(backend))

    @app.post("/api/workflows/import-file")
    async def import_workflow_file(file: UploadFile, name: Optional[str] = None):
        raw = await file.read(comfy_driver.MAX_WORKFLOW_BYTES + 1)
        return await run_in_threadpool(comfy_driver.import_custom_workflow, store.data_dir,
                                       name or Path(file.filename or "workflow").stem, raw,
                                       object_info=lambda: engine.object_info_live_or_cached(backend))

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

    # ------------------------------------------------------------ voice studio
    # Cloning-capable TTS/STT engines, a reusable voice library, speak/
    # transcribe/dictate, and the audiobook and dubbing pipelines. Piper (the
    # character-narration TTS above) is untouched; a library voice made here
    # can also be used as a character's `voice` via {"backend": "studio",
    # "voice_id": "..."} - see `engine.voice_line`.

    def _tts_engines() -> list[Any]:
        object_info = None
        try:
            object_info = engine._object_info(backend)
        except Exception:  # ComfyUI unreachable - the ComfyTTS status entry just reports "not found"
            object_info = None
        return ve.default_tts_engines(voices_dir=store.data_dir / "voices", object_info=object_info)

    def _stt_engines() -> list[Any]:
        return ve.default_stt_engines()

    def _resolve_source_path(raw_path: str) -> Path:
        return engine.resolve_import_path(backend, store, raw_path)

    def _voice_studio_path(rel: str) -> Path:
        root = store.data_dir.resolve()
        path = (root / rel).resolve()
        if not _is_within(path, root) or not path.is_file():
            raise NotFound("file", rel)
        return path

    @app.get("/api/agent/voice_engines")
    def agent_voice_engines():
        def run():
            status = ve.list_engine_status(_tts_engines(), _stt_engines())
            return {
                "tts": [{k: v for k, v in e.items() if k != "capabilities"} | {"languages": e["capabilities"]["languages"],
                        "cloning": e["capabilities"]["cloning"]} for e in status["tts"]],
                "stt": [{k: v for k, v in e.items() if k != "capabilities"} for e in status["stt"]],
            }
        return agent("voice_engines", "", run)

    @app.get("/api/voice/engines")
    def voice_engines_full():
        return ve.list_engine_status(_tts_engines(), _stt_engines())

    def _engine_for_install(kind: str, engine_id: str) -> Any:
        engines = _tts_engines() if kind == "tts" else _stt_engines() if kind == "stt" else None
        if engines is None:
            raise engine.EngineError("bad_kind", "kind must be 'tts' or 'stt'")
        try:
            return ve.get_engine(engines, engine_id)
        except KeyError as exc:
            raise engine.EngineError("unknown_engine", str(exc)) from None

    @app.post("/api/voice/engines/{engine_id}/install")
    def voice_engine_install(engine_id: str, body: VoiceInstallBody):
        eng = _engine_for_install(body.kind, engine_id)
        if not eng.pip_packages:
            raise engine.EngineError("no_installer", f"'{engine_id}' has no known installer; see docs/VOICE.md")
        return queue.enqueue("install_voice_engine", "cpu", {"kind": body.kind, "engine_id": engine_id,
                                                              "pip_packages": eng.pip_packages})

    # -------------------------------------------------------------- library
    @app.get("/api/voice/voices")
    def voice_voices_list(project: Optional[str] = None, engine_id: Optional[str] = None):
        return {"items": [voice_lab.voice_view(v) for v in store.list_studio_voices(project, engine_id)]}

    @app.get("/api/agent/voice_list")
    def agent_voice_list(project: Optional[str] = None):
        return agent("voice_list", project or "",
                     lambda: {"items": [voice_lab.voice_view(v) for v in store.list_studio_voices(project)]})

    def op_voice_create(body: VoiceCreateBody) -> dict[str, Any]:
        src = _resolve_source_path(body.source_path)
        return voice_lab.create_voice(store, body.name, src, body.engine_id, language=body.language,
                                      project_id=body.project, stt_engine=ve.best_installed_stt(_stt_engines()),
                                      tags=body.tags)

    @app.post("/api/voice/voices")
    def voice_voices_create(body: VoiceCreateBody):
        return voice_lab.voice_view(op_voice_create(body))

    @app.post("/api/agent/voice_create")
    def agent_voice_create(body: VoiceCreateBody):
        return agent("voice_create", body.name, lambda: voice_lab.voice_view(op_voice_create(body)))

    @app.post("/api/voice/voices/upload")
    async def voice_voices_upload(file: UploadFile, name: str, engine_id: str, language: Optional[str] = None,
                                  project: Optional[str] = None):
        tmp_dir = store.data_dir / "tmp" / "uploads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ext = Path((file.filename or "sample.wav").replace("\\", "/")).suffix.lower() or ".wav"
        tmp_path = tmp_dir / f"{new_id('up')}{ext if re.fullmatch(r'[.a-z0-9]{1,6}', ext) else '.wav'}"
        total = 0
        try:
            with tmp_path.open("wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > engine.MAX_MEDIA_BYTES:
                        raise engine.EngineError("too_large", "sample exceeds the upload size limit")
                    fh.write(chunk)
            row = voice_lab.create_voice(store, name, tmp_path, engine_id, language=language, project_id=project,
                                         stt_engine=ve.best_installed_stt(_stt_engines()))
            return voice_lab.voice_view(row)
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.get("/api/voice/voices/{voice_id}")
    def voice_voice_get(voice_id: str):
        return store.get_studio_voice(voice_id)

    @app.patch("/api/voice/voices/{voice_id}")
    def voice_voice_update(voice_id: str, body: VoiceUpdateBody):
        return store.update_studio_voice(voice_id, **body.model_dump(exclude_none=True))

    @app.delete("/api/voice/voices/{voice_id}")
    def voice_voice_delete(voice_id: str):
        store.delete_studio_voice(voice_id)
        return {"ok": True, "deleted": voice_id}

    @app.post("/api/voice/voices/{voice_id}/presets")
    def voice_voice_add_preset(voice_id: str, body: VoicePresetBody):
        return store.add_voice_preset(voice_id, body.model_dump(exclude_none=True))

    @app.get("/api/voice/voices/{voice_id}/sample")
    def voice_voice_sample(voice_id: str):
        voice = store.get_studio_voice(voice_id)
        path = voice_lab.sample_path(store, voice)
        if not path:
            raise NotFound("sample", voice_id)
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/voice/voices/{voice_id}/preview")
    def voice_voice_preview(voice_id: str, body: VoiceSpeakBody):
        spec = body.voice.model_dump(exclude_none=True)
        spec["voice_id"] = voice_id
        wav, engine_id = voice_lab.synthesize_with_spec(store, _tts_engines(), spec, body.text[:500])
        return Response(wav, media_type="audio/wav", headers={"X-Engine": engine_id})

    # ------------------------------------------------------------------ speak
    def op_voice_speak(body: VoiceSpeakBody) -> dict[str, Any]:
        if not body.text.strip():
            raise engine.EngineError("empty_text", "give the text to speak")
        spec = body.voice.model_dump(exclude_none=True)
        wav, engine_id = voice_lab.synthesize_with_spec(store, _tts_engines(), spec, body.text.strip())
        if body.project:
            asset_id = new_id("a")
            dest = store.path_for_asset_file(asset_id, ".wav")
            dest.write_bytes(wav)
            duration_s = audio_mod.probe_duration_s(dest)
            asset = store.create_asset(
                project_id=body.project, kind="audio", file_path=engine._rel(store, dest), mime="audio/wav",
                duration_s=duration_s, source="generated",
                recipe={"operation": "voice_speak", "engine": engine_id, "text": body.text, "voice": spec},
                asset_id=asset_id, name=engine._clip(body.text.strip(), 80), tags=["voice-studio"],
            )
            return {"engine_id": engine_id, "asset": engine.asset_view(asset)}
        return {"engine_id": engine_id, "wav": wav}

    @app.post("/api/agent/voice_speak")
    def agent_voice_speak(body: VoiceSpeakBody):
        def run():
            result = op_voice_speak(body)
            if "asset" in result:
                return {"engine_id": result["engine_id"], **result["asset"]}
            return {"engine_id": result["engine_id"], "bytes": len(result["wav"]), "note": "pass project to save this as an audio asset"}
        return agent("voice_speak", body.text[:80], run)

    @app.post("/api/voice/speak")
    def voice_speak(body: VoiceSpeakBody):
        result = op_voice_speak(body)
        if "asset" in result:
            return result["asset"]
        return Response(result["wav"], media_type="audio/wav", headers={"X-Engine": result["engine_id"]})

    # ----------------------------------------------------- transcribe/dictate
    def _run_transcribe(path: Path, language: Optional[str], engine_id: Optional[str], word_timestamps: bool) -> dict[str, Any]:
        stt = ve.best_installed_stt(_stt_engines(), prefer=engine_id)
        if stt is None:
            raise engine.EngineError("stt_not_installed", "no speech-to-text engine is installed; "
                                                          "install faster-whisper (pip install faster-whisper)")
        return {"engine_id": stt.id, **stt.transcribe(path, language=language, word_timestamps=word_timestamps)}

    def op_voice_transcribe(body: VoiceTranscribeBody) -> dict[str, Any]:
        if body.asset_id:
            asset = store.get_asset(body.asset_id)
            path = _asset_path(store, asset["file_path"])
        elif body.path:
            path = _resolve_source_path(body.path)
        else:
            raise engine.EngineError("source_required", "give asset_id or path")
        return _run_transcribe(path, body.language, body.engine_id, body.word_timestamps)

    @app.post("/api/agent/voice_transcribe")
    def agent_voice_transcribe(body: VoiceTranscribeBody):
        def run():
            result = op_voice_transcribe(body)
            return {"engine_id": result["engine_id"], "language": result.get("language"),
                    "text": engine._clip(result.get("text"), 4000), "segment_count": len(result.get("segments") or [])}
        return agent("voice_transcribe", body.asset_id or body.path or "", run)

    @app.post("/api/voice/transcribe")
    def voice_transcribe(body: VoiceTranscribeBody):
        result = op_voice_transcribe(body)
        return {**result, "srt": ve.segments_to_srt(result["segments"]), "vtt": ve.segments_to_vtt(result["segments"]),
                "txt": ve.segments_to_txt(result["segments"])}

    @app.post("/api/voice/transcribe/upload")
    async def voice_transcribe_upload(file: UploadFile, language: Optional[str] = None, engine_id: Optional[str] = None):
        tmp_dir = store.data_dir / "tmp" / "uploads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ext = Path((file.filename or "audio.wav").replace("\\", "/")).suffix.lower() or ".wav"
        tmp_path = tmp_dir / f"{new_id('up')}{ext if re.fullmatch(r'[.a-z0-9]{1,6}', ext) else '.wav'}"
        total = 0
        try:
            with tmp_path.open("wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > engine.MAX_MEDIA_BYTES:
                        raise engine.EngineError("too_large", "file exceeds the upload size limit")
                    fh.write(chunk)
            result = _run_transcribe(tmp_path, language, engine_id, word_timestamps=True)
            return {**result, "srt": ve.segments_to_srt(result["segments"]), "vtt": ve.segments_to_vtt(result["segments"]),
                    "txt": ve.segments_to_txt(result["segments"])}
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.post("/api/voice/dictate")
    async def voice_dictate(file: UploadFile, language: Optional[str] = None):
        tmp_dir = store.data_dir / "tmp" / "uploads"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{new_id('up')}.wav"
        try:
            total = 0
            with tmp_path.open("wb") as fh:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 30 * 1024 * 1024:
                        raise engine.EngineError("too_large", "dictation clips are limited to 30 MB")
                    fh.write(chunk)
            result = _run_transcribe(tmp_path, language, None, word_timestamps=False)
            return {"text": result.get("text", ""), "language": result.get("language"), "engine_id": result["engine_id"]}
        finally:
            tmp_path.unlink(missing_ok=True)

    # -------------------------------------------------------------- audiobook
    def op_voice_audiobook(body: VoiceAudiobookBody) -> dict[str, Any]:
        if body.text and body.text.strip():
            text = body.text
        elif body.source_path:
            text = vp.extract_text_from_file(_resolve_source_path(body.source_path))
        else:
            raise engine.EngineError("text_required", "give text or source_path (.txt/.md/.epub)")
        if not text.strip():
            raise engine.EngineError("empty_text", "the source has no text to narrate")
        if body.format not in ("mp3", "m4b"):
            raise engine.EngineError("bad_format", "format must be 'mp3' or 'm4b'")
        params = {"text": text, "title": body.title, "voice": body.voice.model_dump(exclude_none=True),
                  "format": body.format, "project_id": body.project}
        job = queue.enqueue("audiobook", "cpu", params, project_id=body.project)
        return {"job": wait(job, body.wait_s)}

    @app.post("/api/agent/voice_audiobook")
    def agent_voice_audiobook(body: VoiceAudiobookBody):
        return agent("voice_audiobook", body.title or "", lambda: {"job": job_result(op_voice_audiobook(body)["job"])})

    @app.post("/api/voice/audiobook")
    def voice_audiobook(body: VoiceAudiobookBody):
        return op_voice_audiobook(body)

    @app.get("/api/voice/audiobook/{job_id}")
    def voice_audiobook_status(job_id: str):
        return job_result(store.get_job(job_id))

    @app.get("/api/voice/audiobook/{job_id}/download")
    def voice_audiobook_download(job_id: str, file: str = "final"):
        job = store.get_job(job_id)
        outputs = job.get("outputs") or {}
        key = {"final": "final_file", "srt": "srt_file", "lrc": "lrc_file"}.get(file)
        if not key or not outputs.get(key):
            raise NotFound("file", file)
        path = _voice_studio_path(outputs[key])
        return FileResponse(path, filename=path.name)

    # ------------------------------------------------------------------- dub
    def op_voice_dub(body: VoiceDubBody) -> dict[str, Any]:
        if body.source_path:
            video_path = _resolve_source_path(body.source_path)
        elif body.video_asset_id:
            asset = store.get_asset(body.video_asset_id)
            if asset["kind"] != "video":
                raise engine.EngineError("not_video", f"asset {body.video_asset_id} is {asset['kind']}, not video")
            video_path = _asset_path(store, asset["file_path"])
        else:
            raise engine.EngineError("source_required", "give source_path or video_asset_id")
        params = {
            "video_path": str(video_path), "target_language": body.target_language,
            "source_language": body.source_language, "glossary": body.glossary or {},
            "voice": body.voice.model_dump(exclude_none=True), "stt_engine_id": body.stt_engine_id,
            "title": body.title, "project_id": body.project,
        }
        job = queue.enqueue("dub", "cpu", params, project_id=body.project)
        return {"job": wait(job, body.wait_s)}

    @app.post("/api/agent/voice_dub")
    def agent_voice_dub(body: VoiceDubBody):
        return agent("voice_dub", f"{body.target_language}:{body.source_path or body.video_asset_id or ''}",
                     lambda: {"job": job_result(op_voice_dub(body)["job"])})

    @app.post("/api/voice/dub")
    def voice_dub(body: VoiceDubBody):
        return op_voice_dub(body)

    @app.get("/api/voice/dub/{job_id}")
    def voice_dub_status(job_id: str):
        return job_result(store.get_job(job_id))

    @app.get("/api/voice/dub/{job_id}/download")
    def voice_dub_download(job_id: str, file: str = "video"):
        job = store.get_job(job_id)
        outputs = job.get("outputs") or {}
        key = {"video": "final_video", "subtitles": "subtitles"}.get(file)
        if not key or not outputs.get(key):
            raise NotFound("file", file)
        path = _voice_studio_path(outputs[key])
        return FileResponse(path, filename=path.name)

    def _dub_work_dir(job_id: str) -> Path:
        job = store.get_job(job_id)
        outputs = job.get("outputs") or {}
        if not outputs.get("work_dir"):
            raise engine.EngineError("job_not_done", "the dub job has not produced a work directory yet")
        return _voice_studio_path(outputs["work_dir"] + "/manifest.json").parent

    @app.post("/api/voice/dub/{job_id}/segments/{index}/resynthesize")
    def voice_dub_resegment(job_id: str, index: int, body: VoiceResegmentBody):
        work_dir = _dub_work_dir(job_id)
        voice_spec = body.voice.model_dump(exclude_none=True) if body.voice else None
        return dubbing_mod.resynthesize_segment(store, backend, work_dir, index, new_text=body.text,
                                                voice_spec=voice_spec or None, remix=body.remix)

    @app.post("/api/agent/voice_resynthesize_segment")
    def agent_voice_resegment(job_id: str, index: int, body: VoiceResegmentBody):
        return agent("voice_resynthesize_segment", f"{job_id}:{index}",
                     lambda: voice_dub_resegment(job_id, index, body))

    # ------------------------------------------------------------ voice jobs
    @app.get("/api/agent/voice_job")
    def agent_voice_job(job_id: str, wait_s: float = 0):
        def run():
            job = queue.wait_for(job_id, min(wait_s, MAX_WAIT_S)) if wait_s > 0 else store.get_job(job_id)
            return job_result(job)
        return agent("voice_job", job_id, run)

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
            # ffprobe, thumbnails and audio decoding off the event loop
            return await run_in_threadpool(engine.import_asset, store, project_id, tmp_path, kind, original_name=original)
        finally:
            tmp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ audio
    @app.post("/api/agent/studio_time_lyrics")
    def agent_time_lyrics(project: str, body: TimeLyricsBody):
        return agent("studio_time_lyrics", body.song_asset_id,
                     lambda: engine.time_lyrics(store, project, body.song_asset_id, body.lyrics, body.name))

    @app.post("/api/projects/{project_id}/lyrics/time")
    def ui_time_lyrics(project_id: str, body: TimeLyricsBody):
        # the UI's "Auto-time" button: same operation, not logged as assistant activity
        return engine.time_lyrics(store, project_id, body.song_asset_id, body.lyrics, body.name)

    @app.post("/api/agent/studio_analyze_audio")
    def agent_analyze_audio(asset_id: str):
        return agent("studio_analyze_audio", asset_id,
                     lambda: engine.analysis_view(asset_id, engine.analyze_audio(store, asset_id)))

    @app.post("/api/assets/{asset_id}/analyze")
    def ui_analyze(asset_id: str, force: bool = False):
        return engine.analyze_audio(store, asset_id, force=force)

    @app.get("/api/assets/{asset_id}/lyrics")
    def get_lyrics(asset_id: str):
        lyrics = engine.read_lyrics(store, asset_id)
        # the editor also shows the timed [Section] markers, so saving keeps them
        return {**lyrics, "all_lines": audio_mod.parse_lrc(lyrics["text"])}

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

    def op_photocard_set(project: str, body: PhotocardSetBody) -> dict[str, Any]:
        if body.character_id or body.cards:
            if not (body.character_id and body.cards):
                raise engine.EngineError("bad_cards", "a solo set needs character_id and cards [{image_asset_id, role, message, accent}]")
            return engine.photocard_set_looks(store, project, body.character_id, body.cards, body.template_front,
                                              body.template_back, body.set_name)
        if not body.group_id:
            raise engine.EngineError("group_required", "pass group_id (one card per member) or character_id + cards (one per look)")
        return engine.photocard_set(store, project, body.group_id, body.template_front, body.template_back, body.image_asset_ids)

    @app.post("/api/agent/studio_photocard_set")
    def agent_photocard_set(project: str, body: PhotocardSetBody):
        return agent("studio_photocard_set", body.group_id or body.character_id or "", lambda: op_photocard_set(project, body))

    @app.post("/api/projects/{project_id}/photocard-set")
    def ui_photocard_set(project_id: str, body: PhotocardSetBody):
        return op_photocard_set(project_id, body)

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
        summary = f"{body.timeline_id}:{body.quality}" + (f":{body.range[0]:g}-{body.range[1]:g}" if body.range and len(body.range) == 2 else "")
        return agent("studio_render", summary, lambda: {"job": job_result(op_render(body)["job"])})

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

    # ------------------------------------------------------ agent curation
    @app.post("/api/agent/studio_asset_update")
    def agent_asset_update(asset_id: str, body: UpdateAssetBody):
        def run():
            fields = body.model_dump(exclude_none=True)
            if not fields:
                raise engine.EngineError("nothing_to_update", "pass at least one of tags, rating, favourite, notes, name")
            asset = store.update_asset(asset_id, **fields)
            return {**engine.asset_view(asset), "tags": asset.get("tags"), "rating": asset.get("rating"),
                    "favourite": bool(asset.get("favourite"))}
        return agent("studio_asset_update", asset_id, run)

    @app.post("/api/agent/studio_project_update")
    def agent_project_update(project: str, body: UpdateProjectBody):
        def run():
            p = store.update_project(project, body.name, body.brief, body.cover_asset_id, body.image_engine)
            return {"id": p["id"], "name": p["name"], "brief": engine._clip(p.get("brief"), 160),
                    "cover_asset_id": p.get("cover_asset_id"), "image_engine": p.get("image_engine")}
        return agent("studio_project_update", project, run)

    def board_view(board: dict[str, Any]) -> dict[str, Any]:
        items = board.get("items") or []
        return {"id": board["id"], "name": board["name"], "kind": board["kind"], "count": len(items),
                "items": [{k: v for k, v in {"asset_id": i.get("asset_id"), "note": engine._clip(i.get("note"), 80)}.items() if v}
                          for i in items[:60]], "has_more": len(items) > 60}

    @app.post("/api/agent/studio_board")
    def agent_board(project: str, body: AgentBoardBody):
        def run():
            if body.action == "list":
                return {"items": [{"id": b["id"], "name": b["name"], "kind": b["kind"], "count": len(b.get("items") or [])}
                                  for b in store.list_boards(project)]}
            if body.action == "create":
                board = store.create_board(project, body.name or "", body.kind or "moodboard")
            elif body.action in ("add", "set"):
                if not body.board_id:
                    raise engine.EngineError("board_required", f"action '{body.action}' needs board_id")
                board = store.get_board(body.board_id)
            elif body.action == "get":
                return board_view(store.get_board(body.board_id or ""))
            else:
                raise engine.EngineError("bad_action", f"unknown board action '{body.action}'; use list, get, create, add or set")
            if board["project_id"] != project:
                raise engine.EngineError("wrong_project", "that board belongs to another project")
            if body.asset_ids is not None or body.action == "set":
                notes = body.notes or {}
                new_items = []
                for aid in body.asset_ids or []:
                    store.get_asset(aid)
                    new_items.append({"asset_id": aid, **({"note": notes[aid]} if notes.get(aid) else {})})
                items = new_items if body.action in ("set", "create") else [*(board.get("items") or []), *new_items]
                board = store.update_board_items(board["id"], items)
            return board_view(board)
        return agent("studio_board", f"{body.action} {body.board_id or body.name or ''}".strip(), run)

    def op_retry_job(job_id: str, new_seed: bool) -> dict[str, Any]:
        """Queue a copy of a failed or cancelled job (same type, parameters
        and inputs; optionally a new seed), linked by `params.retry_of`."""
        old = store.get_job(job_id)
        if old["state"] not in ("failed", "cancelled"):
            raise engine.EngineError("not_retryable", f"job is {old['state']}; only failed or cancelled jobs can be retried")
        if old["type"] in ("production", "production_qa"):
            raise engine.EngineError("use_production_continue",
                                     "a production resumes with studio_production_continue, which keeps what is done")
        params = dict(old["params"] or {})
        if new_seed and "seed" in params:
            params["seed"] = engine.random_seed()
        params["retry_of"] = job_id
        return queue.enqueue(old["type"], old["lane"], params, old.get("inputs"), old.get("project_id"))

    @app.post("/api/agent/studio_retry_job")
    def agent_retry_job(job_id: str, new_seed: bool = False):
        return agent("studio_retry_job", job_id, lambda: engine.job_view(op_retry_job(job_id, new_seed)))

    @app.post("/api/jobs/{job_id}/retry")
    def ui_retry_job(job_id: str, new_seed: bool = False):
        return op_retry_job(job_id, new_seed)

    # ------------------------------------------------------------ productions
    # A production runs as one orchestrator job that queues ordinary
    # generate/compose/render jobs through this studio facade (so a render
    # pool spreads its frames and clips) and checkpoints into
    # data/productions/<slug>/state.json - see productions.py.
    class AppStudio:
        def generate(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
            return op_generate(project_id, GenerateImageBody(**{k: v for k, v in body.items() if v is not None}))["job"]

        def compose(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
            return op_compose(project_id, ComposeSongBody(**{k: v for k, v in body.items() if v is not None}))["job"]

        def render(self, timeline_id: str, quality: str) -> dict[str, Any]:
            return op_render(RenderBody(timeline_id=timeline_id, quality=quality))["job"]

        def job(self, job_id: str) -> dict[str, Any]:
            return store.get_job(job_id)

        def cancel(self, job_id: str) -> None:
            queue.cancel(job_id)

    studio = AppStudio()
    app.state.studio = studio
    production_hooks: dict[str, Any] = {"qa_hook": None, "stage_hooks": {}}
    app.state.production_hooks = production_hooks

    def _production_job(job: dict[str, Any], progress) -> dict[str, Any]:
        slug = job["params"]["slug"]
        return productions_mod.run_production(store, studio, slug, progress, qa_hook=production_hooks["qa_hook"],
                                              stage_hooks=production_hooks["stage_hooks"])

    queue.register("production", _production_job)

    def queue_production(slug: str) -> dict[str, Any]:
        """Queue a production's run (or return the one already queued/running)."""
        with productions_mod.lock_for(slug):
            state = productions_mod.load_state(store.data_dir, slug)
            if productions_mod.is_legacy(state):
                raise engine.EngineError("legacy_production", f"'{slug}' was made by the production script; export it as a "
                                                              "recipe (studio_recipe_export) and run the recipe instead")
            if state.get("job_id"):
                try:
                    current = store.get_job(state["job_id"])
                    if current["state"] in ("queued", "waiting_gpu", "running"):
                        return current
                except NotFound:
                    pass
            job = queue.enqueue("production", "cpu", {"slug": slug, "name": state.get("name")},
                                project_id=state.get("project_id"))
            state["job_id"] = job["id"]
            if state.get("status") != "running":
                state["status"] = "queued"
            productions_mod.save_state(store.data_dir, state)
            return job

    def vision_for_qa() -> tuple[Optional[Callable[[list[bytes], str], str]], str]:
        """The family's vision model through Hoard Link, or (None, "no vision
        model"): QA then runs its model-free checks only. Tests set
        app.state.qa_vision to a (fn, name) pair."""
        override = getattr(app.state, "qa_vision", None)
        if override is not None:
            return override
        try:
            res = backend.link.sync.resolve("vision")
        except Exception:  # noqa: BLE001 - no resolver, no model checks
            return None, "no vision model"
        if not res.resolved:
            return None, "no vision model"

        def ask(images: list[bytes], prompt: str) -> str:
            return backend.link.sync.chat(messages=[{"role": "user", "content": prompt}], images=images,
                                          capability="vision", max_tokens=240, temperature=0.0).text

        return ask, f"{res.provider or 'vision'}:{res.model or ''}".rstrip(":")

    production_hooks["qa_hook"] = lambda run, stage: qa_mod.inline_hook(run, stage, *vision_for_qa())

    def job_or_none(job_id: str) -> Optional[dict[str, Any]]:
        try:
            return store.get_job(job_id)
        except NotFound:
            return None

    def production_state(slug: str) -> dict[str, Any]:
        """The state, with the status of a production whose job was
        cancelled or failed before its run could record it."""
        return productions_mod.reconcile(productions_mod.load_state(store.data_dir, slug), job_or_none)

    def production_view(slug: str) -> dict[str, Any]:
        return productions_mod.compact_view(production_state(slug))

    def op_production_create(body: ProductionCreateBody) -> dict[str, Any]:
        if body.project:
            store.get_project(body.project)
        state = productions_mod.create_production(store.data_dir, body.name, body.spec, body.settings, project_id=body.project)
        job = queue_production(state["slug"])
        return {"production": production_view(state["slug"]), "job": engine.job_view(job)}

    def op_production_continue(slug: str) -> dict[str, Any]:
        with productions_mod.lock_for(slug):
            state = productions_mod.load_state(store.data_dir, slug)
            if state.get("status") == "awaiting_review":
                # the approval names the animatic reviewed: one made again
                # later (shots changed, QA retries) is reviewed again
                approved = productions_mod.approve_animatic(state)
                productions_mod.log(state, "review", "approved", animatic=approved)
                productions_mod.save_state(store.data_dir, state)
        job = queue_production(slug)
        return {"production": production_view(slug), "job": engine.job_view(job)}

    def op_production_shots(slug: str, body: ProductionShotsBody) -> dict[str, Any]:
        result = productions_mod.update_shots(store.data_dir, slug, body.changes, requeue=body.run)
        if body.run and result["changed"]:
            result["job"] = engine.job_view(queue_production(slug))
        return {**result, "production": production_view(slug)}

    def op_recipe_run(name: str, body: RecipeRunBody) -> dict[str, Any]:
        if (body.options or {}).get("dry_run"):
            return recipes_mod.preview_run(store, name, body.cast, body.options)
        state = recipes_mod.run_recipe(store, name, body.cast, body.name, body.options)
        job = queue_production(state["slug"])
        return {"production": production_view(state["slug"]), "job": engine.job_view(job),
                "notes": (state.get("recipe") or {}).get("notes") or []}

    def _qa_job(job: dict[str, Any], progress) -> dict[str, Any]:
        params = job["params"]
        fn, name = vision_for_qa()
        out = qa_mod.run_qa(store, studio, params["slug"], params.get("stage") or "all", bool(params.get("dry_run", True)),
                            params.get("keys"), fn, name, progress)
        if out["requeue"]:
            queue_production(params["slug"])
        return {"scorecard": qa_mod.compact_scorecard(out["scorecard"]), "requeued": out["requeue"]}

    queue.register("production_qa", _qa_job)

    def _qa_check_job(job: dict[str, Any], progress) -> dict[str, Any]:
        # a dry run reads a snapshot and regenerates nothing: an ordinary
        # cpu-lane job, so it never waits behind a running production
        params = job["params"]
        fn, name = vision_for_qa()
        out = qa_mod.check_production(store, params["slug"], params.get("stage") or "all", params.get("keys"), fn, name,
                                      progress)
        return {"scorecard": qa_mod.compact_scorecard(out["scorecard"]), "requeued": False}

    queue.register("production_qa_check", _qa_check_job)

    def op_qa_run(slug: str, body: QaRunBody) -> dict[str, Any]:
        state = productions_mod.load_state(store.data_dir, slug)
        if body.stage != "all" and body.stage not in qa_mod.CHECKED_STAGES:
            raise engine.EngineError("bad_stage", f"stage must be 'all' or one of {', '.join(qa_mod.CHECKED_STAGES)}")
        project_id = state.get("project_id") or (state.get("done", {}).get("1") or {}).get("project_id")
        job = queue.enqueue("production_qa_check" if body.dry_run else "production_qa", "cpu",
                            {"slug": slug, "stage": body.stage, "dry_run": body.dry_run, "keys": body.keys},
                            project_id=project_id)
        job = wait(job, body.wait_s)
        out: dict[str, Any] = {"job": engine.job_view(job)}
        if job["state"] == "done":
            out.update((job.get("outputs") or {}))
        return out

    def qa_report(slug: str) -> dict[str, Any]:
        state = productions_mod.load_state(store.data_dir, slug)
        card = (state.get("qa") or {}).get("last")
        if not card:
            raise engine.EngineError("no_qa_yet", f"no QA pass has run on '{slug}' yet; run studio_qa_run first")
        retries = [{k: v for k, v in e.items() if k in ("at", "stage", "event", "key", "attempt", "reason", "fix", "asset_id")}
                   for e in state.get("lineage", []) if str(e.get("event", "")).startswith("qa_")]
        return {**qa_mod.compact_scorecard(card), "retries": retries[-30:]}

    @app.post("/api/agent/studio_qa_run")
    def agent_qa_run(body: QaRunBody):
        if not body.production:
            raise engine.EngineError("production_required", "give the production slug (studio_productions)")
        return agent("studio_qa_run", f"{body.production}:{body.stage}", lambda: op_qa_run(body.production, body))

    @app.get("/api/agent/studio_qa_report")
    def agent_qa_report(production: str):
        return agent("studio_qa_report", production, lambda: qa_report(production))

    @app.post("/api/productions/{slug}/qa")
    def production_qa_run(slug: str, body: QaRunBody):
        body.wait_s = min(body.wait_s, 5)
        return op_qa_run(slug, body)

    @app.get("/api/productions/{slug}/qa")
    def production_qa_get(slug: str):
        state = productions_mod.load_state(store.data_dir, slug)
        return {"last": (state.get("qa") or {}).get("last"), "history": (state.get("qa") or {}).get("history") or []}

    def _animatic_job(job: dict[str, Any], progress) -> dict[str, Any]:
        params = job["params"]
        entry = animatic_mod.make_for_production(store, params["slug"], params.get("aspects"), progress)
        return {**entry, "asset_ids": list(entry["renders"].values())}

    queue.register("animatic", _animatic_job)

    def op_animatic(slug: str, body: AnimaticBody) -> dict[str, Any]:
        state = productions_mod.load_state(store.data_dir, slug)
        for aspect in body.aspects or []:
            if aspect not in timeline_mod.ASPECTS:
                raise engine.EngineError("bad_aspect", f"aspect must be one of {', '.join(timeline_mod.ASPECTS)}")
        project_id = state.get("project_id") or (state.get("done", {}).get("1") or {}).get("project_id")
        job = queue.enqueue("animatic", "cpu", {"slug": slug, "aspects": body.aspects}, project_id=project_id)
        job = wait(job, body.wait_s)
        out: dict[str, Any] = {"job": engine.job_view(job)}
        if job["state"] == "done":
            outputs = job.get("outputs") or {}
            out["animatic"] = {k: outputs.get(k) for k in ("renders", "plan", "contact_sheet_id") if outputs.get(k) is not None}
            if outputs.get("preview"):
                out["animatic"]["preview"] = True
                out["animatic"]["note"] = ("not every shot has its still yet (or the production is running): kept as a "
                                           "preview; the production makes and pauses at its own animatic")
        return out

    @app.post("/api/agent/studio_animatic")
    def agent_animatic(body: AnimaticBody):
        if not body.production:
            raise engine.EngineError("production_required", "give the production slug (studio_productions)")
        return agent("studio_animatic", body.production, lambda: op_animatic(body.production, body))

    @app.post("/api/productions/{slug}/animatic")
    def production_animatic_make(slug: str, body: AnimaticBody):
        return op_animatic(slug, body)

    @app.get("/api/productions/{slug}/animatic")
    def production_animatic_plan(slug: str):
        return animatic_mod.read_plan(store.data_dir, slug)

    @app.get("/api/productions")
    def productions_list():
        return {"items": productions_mod.list_productions(store.data_dir, job_or_none)}

    @app.post("/api/productions")
    def production_create(body: ProductionCreateBody):
        return op_production_create(body)

    @app.get("/api/productions/{slug}")
    def production_get(slug: str):
        state = production_state(slug)
        return {**state, "view": productions_mod.compact_view(state)}

    @app.post("/api/productions/{slug}/continue")
    def production_continue(slug: str):
        return op_production_continue(slug)

    @app.patch("/api/productions/{slug}/shots")
    def production_shots(slug: str, body: ProductionShotsBody):
        return op_production_shots(slug, body)

    @app.get("/api/productions/{slug}/report")
    def production_report(slug: str):
        path = productions_mod.production_dir(store.data_dir, slug) / "REPORT.md"
        if not path.is_file():
            state = productions_mod.load_state(store.data_dir, slug)
            if productions_mod.is_legacy(state):
                raise NotFound("report", slug)
            path = productions_mod.write_report(store, state)
        return Response(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    @app.post("/api/productions/{slug}/recipe")
    def production_to_recipe(slug: str, body: RecipeExportBody):
        return recipes_mod.recipe_summary(recipes_mod.export_recipe(store, slug, body.name, overwrite=body.overwrite))

    @app.get("/api/recipes")
    def recipes_list():
        return {"items": recipes_mod.list_recipes(store.data_dir)}

    @app.get("/api/recipes/{name}")
    def recipe_get(name: str):
        return recipes_mod.get_recipe(store.data_dir, name)

    @app.post("/api/recipes/{name}/run")
    def recipe_run(name: str, body: RecipeRunBody):
        return op_recipe_run(name, body)

    @app.get("/api/agent/studio_productions")
    def agent_productions():
        return agent("studio_productions", "", lambda: {"items": productions_mod.list_productions(store.data_dir, job_or_none)[:30]})

    @app.get("/api/agent/studio_production")
    def agent_production(production: str):
        return agent("studio_production", production, lambda: production_view(production))

    @app.post("/api/agent/studio_production_create")
    def agent_production_create(body: ProductionCreateBody):
        return agent("studio_production_create", body.name[:80], lambda: op_production_create(body))

    @app.post("/api/agent/studio_production_continue")
    def agent_production_continue(production: str):
        return agent("studio_production_continue", production, lambda: op_production_continue(production))

    @app.post("/api/agent/studio_production_shots")
    def agent_production_shots(production: str, body: ProductionShotsBody):
        return agent("studio_production_shots", production, lambda: op_production_shots(production, body))

    @app.post("/api/agent/studio_recipe_export")
    def agent_recipe_export(body: RecipeExportBody):
        def run():
            recipe = recipes_mod.export_recipe(store, body.production, body.name, overwrite=body.overwrite)
            return {**recipes_mod.recipe_summary(recipe), "cast": recipe["cast"], "warnings": recipe["warnings"][:12],
                    "notes": recipe["notes"]}
        return agent("studio_recipe_export", body.production, run)

    @app.get("/api/agent/studio_recipes_list")
    def agent_recipes_list():
        return agent("studio_recipes_list", "", lambda: {"items": recipes_mod.list_recipes(store.data_dir)[:50]})

    @app.get("/api/agent/studio_recipe_get")
    def agent_recipe_get(recipe: str):
        def run():
            data = recipes_mod.get_recipe(store.data_dir, recipe)
            spec = data.get("spec") or {}
            shots = [{k: v for k, v in {"key": s.get("key"), "lead": s.get("lead"), "prompt": engine._clip(s.get("prompt"), 140),
                                        "seed": s.get("seed"), "variants": s.get("variants"), "clips": s.get("clips") or None,
                                        "motion": s.get("motion")}.items() if v is not None}
                     for s in spec.get("shots") or []]
            return {**recipes_mod.recipe_summary(data), "cast": data.get("cast"), "placeholders": data.get("placeholders"),
                    "song": {k: engine._clip(v, 160) if isinstance(v, str) else v for k, v in (spec.get("song") or {}).items()},
                    "world": {k: engine._clip(v, 200) for k, v in (spec.get("world") or {}).items()},
                    "shot_list": shots, "timeline": {k: v for k, v in (spec.get("timeline") or {}).items() if k != "storyboard"},
                    "storyboard_sections": list(((spec.get("timeline") or {}).get("storyboard") or {}).keys()),
                    "settings": data.get("settings"), "warnings": data.get("warnings"), "notes": data.get("notes")}
        return agent("studio_recipe_get", recipe, run)

    @app.post("/api/agent/studio_recipe_run")
    def agent_recipe_run(body: RecipeRunBody):
        if not body.recipe:
            raise engine.EngineError("recipe_required", "give the recipe name (studio_recipes_list)")
        return agent("studio_recipe_run", body.recipe, lambda: op_recipe_run(body.recipe, body))

    # ------------------------------------------------------------- status --
    def _auto_image_engine() -> str:
        try:
            return engine.resolve_image_engine(engine._object_info(backend), "auto")
        except Exception:  # ComfyUI unreachable and nothing cached yet - status still renders
            return "sdxl"

    @app.get("/api/agent/studio_status")
    def agent_status():
        def run():
            status = backend.status()
            link = status["hoard_link"]
            counts = store.count_jobs(("queued", "waiting_gpu", "running"))
            return {
                "demo_backend": status["demo"],
                "capabilities": {cap: {k: v for k, v in {"state": r.get("state"), "provider": r.get("provider"),
                                                         "model": r.get("model"), "reason": engine._clip(r.get("reason"), 140)}.items() if v}
                                 for cap, r in link.items()},
                "comfyui": {k: status["comfy"].get(k) for k in ("reachable", "url", "checkpoints", "vram_free_mb", "reason")},
                "image_engine": {"available": list(engine.IMAGE_ENGINES), "auto_resolves_to": _auto_image_engine()},
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

    _housekeeping(store)
    queue.start()
    return app


# ---------------------------------------------------------------- helpers

def _housekeeping(store: Store, tmp_max_age_s: float = 24 * 3600) -> None:
    """On start-up: trim the assistant-activity log and remove scratch files
    a crash left in data/tmp (render work folders, partial uploads). Only
    entries older than a day go, so nothing a live job is using is touched."""
    import shutil

    try:
        store.prune_agent_calls()
    except sqlite3.Error:
        logger.warning("could not prune the agent call log", exc_info=True)
    tmp = store.data_dir / "tmp"
    if not tmp.is_dir():
        return
    cutoff = time.time() - tmp_max_age_s
    for entry in list(tmp.iterdir()) + (list((tmp / "uploads").iterdir()) if (tmp / "uploads").is_dir() else []):
        try:
            if entry.name == "uploads" or entry.stat().st_mtime > cutoff:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue

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


def _install_voice_engine_job(job: dict[str, Any], progress) -> dict[str, Any]:
    """The one explicit, user-triggered install action for a voice engine
    (`POST /api/voice/engines/{id}/install`): runs `pip install` for its
    packages into this app's own venv. Never triggered on its own."""
    params = job["params"]
    packages = params["pip_packages"]
    progress(0.05, f"installing {', '.join(packages)}")
    exe = sys.executable
    cmd = [exe, "-m", "pip", "install", *packages]
    proc = procutil.run(cmd, text=True, timeout=1800)
    if proc.returncode != 0:
        raise engine.EngineError("install_failed", (proc.stderr or "")[-1500:] or "pip install failed")
    return {"engine_id": params["engine_id"], "kind": params["kind"], "installed_packages": packages,
            "log_tail": (proc.stdout or "")[-1500:]}
