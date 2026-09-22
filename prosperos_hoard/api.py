"""FastAPI application: HTTP surface, browser-attack guard, `/api/agent/*`
(exactly what the MCP tools call), `/api/backend`, and the richer UI
endpoints. See `docs/API.md` for every route with examples.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import engine
from .backend import Backend
from .hoard_link.errors import BackendError, Unavailable
from .jobs import JobQueue
from .store import NotFound, Store
from . import __version__

MAX_VIDEO_AUDIO_BYTES = 2 * 1024 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024


class GuardMiddleware(BaseHTTPMiddleware):
    """Rejects DNS-rebinding Host headers and cross-site writes. No CORS
    headers are ever added: a plain GET from a browser tab keeps working,
    but scripted cross-origin writes are refused."""

    def __init__(self, app, port: int):
        super().__init__(app)
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in self.allowed_hosts:
            return JSONResponse({"error": "bad_host", "message": f"unexpected Host header '{host}'"}, status_code=400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            sec_fetch_site = request.headers.get("sec-fetch-site")
            if origin and origin not in (f"http://127.0.0.1:{self._port_from_host(host)}", f"http://localhost:{self._port_from_host(host)}"):
                return JSONResponse({"error": "bad_origin", "message": "cross-origin write rejected"}, status_code=403)
            if sec_fetch_site == "cross-site":
                return JSONResponse({"error": "cross_site", "message": "cross-site write rejected"}, status_code=403)
        return await call_next(request)

    @staticmethod
    def _port_from_host(host: str) -> str:
        return host.rsplit(":", 1)[-1] if ":" in host else ""


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, NotFound):
        return JSONResponse({"error": "not_found", "message": str(exc)}, status_code=404)
    if isinstance(exc, engine.EngineError):
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=400)
    if isinstance(exc, Unavailable):
        return JSONResponse({"error": f"{exc.capability}_unavailable", "message": str(exc)}, status_code=409)
    if isinstance(exc, BackendError):
        return JSONResponse({"error": "backend_error", "message": str(exc)}, status_code=502)
    if isinstance(exc, ValueError):
        return JSONResponse({"error": "bad_request", "message": str(exc)}, status_code=400)
    return JSONResponse({"error": "internal_error", "message": str(exc)}, status_code=500)



class BackendOverrides(BaseModel):
    faustus_url: Optional[str] = None
    faustus_token: Optional[str] = None
    comfy_url: Optional[str] = None
    vram_estimates_mb: Optional[dict[str, int]] = None
class CreateProjectBody(BaseModel):
    name: str
    brief: Optional[str] = None
class CastBody(BaseModel):
    action: str = "list"
    kind: str = "character"  # "character" | "group"
    id: Optional[str] = None
    name: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)
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
    lrc_text: Optional[str] = None
    lines: Optional[list[dict[str, Any]]] = None
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
class BoardBody(BaseModel):
    name: str
    kind: str = "moodboard"
class BoardItemsBody(BaseModel):
    items: list[dict[str, Any]]

def create_app(data_dir: Path, static_dir: Optional[Path] = None, port: int = 8815) -> FastAPI:
    data_dir = Path(data_dir)
    store = Store(data_dir)
    backend = Backend(data_dir)
    queue = JobQueue(store)
    queue.register("generate_image", lambda job, p: engine.generate_image(store, backend, job, p))
    queue.register("edit_image", lambda job, p: engine.edit_image(store, backend, job, p))
    queue.register("animate", lambda job, p: engine.animate_image(store, backend, job, p))
    queue.register("render_timeline", lambda job, p: engine.render_timeline_job(store, backend, job, p))
    queue.start()

    app = FastAPI(title="Prospero's Hoard")
    app.add_middleware(GuardMiddleware, port=port)
    app.state.store = store
    app.state.backend = backend
    app.state.queue = queue

    def agent_call(tool: str, args_summary: str, fn):
        start = time.monotonic()
        try:
            result = fn()
            store.record_agent_call(tool, args_summary, (time.monotonic() - start) * 1000, ok=True)
            return result
        except Exception as exc:  # noqa: BLE001
            store.record_agent_call(tool, args_summary, (time.monotonic() - start) * 1000, ok=False, error=str(exc))
            raise

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    def health():
        return {
            "service": "prosperos-hoard", "name": "Prospero's Hoard", "version": __version__, "status": "ok",
            "projects": len(store.list_projects(limit=1)["items"]),
        }

    @app.get("/api/backend")
    def get_backend():
        return backend.status()


    @app.post("/api/backend")
    def set_backend(body: BackendOverrides):
        backend.set_overrides(body.faustus_url, body.faustus_token, body.comfy_url, body.vram_estimates_mb)
        return backend.status()

    @app.get("/api/agent-calls")
    def agent_calls(limit: int = 20):
        return {"items": store.list_agent_calls(limit)}

    # ------------------------------------------------------------- projects

    @app.post("/api/agent/studio_create_project")
    def agent_create_project(body: CreateProjectBody):
        try:
            return agent_call("studio_create_project", body.name, lambda: store.create_project(body.name, body.brief))
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/agent/studio_projects")
    def agent_projects(query: Optional[str] = None, limit: int = 10):
        return agent_call("studio_projects", query or "", lambda: store.list_projects(query, limit))

    @app.get("/api/projects")
    def list_projects(query: Optional[str] = None, limit: int = 20, offset: int = 0):
        return store.list_projects(query, limit, offset)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        try:
            return store.get_project(project_id)
        except NotFound as exc:
            return _error_response(exc)

    # ------------------------------------------------------------------ cast

    @app.post("/api/agent/studio_cast")
    def agent_cast(project: str, body: CastBody):
        def run():
            if body.action == "list":
                return {"characters": store.list_characters(project), "groups": store.list_groups(project)}
            if body.kind == "group":
                if body.action == "create":
                    return store.create_group(project, body.name, **body.fields)
                if body.action == "update":
                    return store.update_group(body.id, **body.fields)
            else:
                if body.action == "create":
                    return store.create_character(project, body.name, **body.fields)
                if body.action == "update":
                    return store.update_character(body.id, **body.fields)
            raise ValueError(f"unknown cast action '{body.action}'/'{body.kind}'")
        try:
            return agent_call("studio_cast", f"{body.action}:{body.kind}", run)
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/projects/{project_id}/characters")
    def list_characters(project_id: str):
        return {"items": store.list_characters(project_id)}

    @app.get("/api/projects/{project_id}/groups")
    def list_groups(project_id: str):
        return {"items": store.list_groups(project_id)}

    @app.get("/api/style-presets")
    def style_presets(project: Optional[str] = None):
        return {"items": store.list_style_presets(project)}

    # -------------------------------------------------------------- generate

    @app.post("/api/agent/studio_generate_image")
    def agent_generate_image(project: str, body: GenerateImageBody):
        def run():
            composed = engine.compose_prompt(store, project, body.prompt, body.negative, body.style)
            template = body.template or ("sdxl_img2img" if (body.reference_asset_id or composed["reference_asset_id"]) else "sdxl_txt2img")
            width, height = body.width, body.height
            if body.aspect and not (width and height):
                width, height = {"1:1": (1024, 1024), "9:16": (896, 1600), "16:9": (1600, 896)}.get(body.aspect, (1024, 1024))
            params = {
                "positive_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
                "width": width, "height": height, "steps": body.steps, "cfg": body.cfg, "sampler": body.sampler,
                "scheduler": body.scheduler, "seed": body.seed or 0, "count": body.count,
                "reference_asset_id": body.reference_asset_id or composed["reference_asset_id"],
                "strength": body.strength, "template": template, "checkpoint": body.checkpoint,
                "style_defaults": composed["style_defaults"],
            }
            job = queue.enqueue("generate_image", "gpu", params, project_id=project)
            if body.wait_s > 0:
                job = queue.wait_for(job["id"], body.wait_s)
            return {"job": job, "matched_characters": composed["matched_characters"], "final_prompt": composed["positive_prompt"]}
        try:
            return agent_call("studio_generate_image", body.prompt[:80], run)
        except Exception as exc:
            return _error_response(exc)


    @app.post("/api/agent/studio_edit_image")
    def agent_edit_image(body: EditImageBody):
        def run():
            asset = store.get_asset(body.asset_id)
            params = body.model_dump()
            job = queue.enqueue("edit_image", "gpu", params, project_id=asset["project_id"])
            if body.wait_s > 0:
                job = queue.wait_for(job["id"], body.wait_s)
            return {"job": job}
        try:
            return agent_call("studio_edit_image", f"{body.asset_id}:{body.operation}", run)
        except Exception as exc:
            return _error_response(exc)


    @app.post("/api/agent/studio_animate")
    def agent_animate(body: AnimateBody):
        def run():
            asset = store.get_asset(body.asset_id)
            job = queue.enqueue("animate", "gpu", body.model_dump(), project_id=asset["project_id"])
            if body.wait_s > 0:
                job = queue.wait_for(job["id"], body.wait_s)
            return {"job": job}
        try:
            return agent_call("studio_animate", body.asset_id, run)
        except Exception as exc:
            return _error_response(exc)

    # ------------------------------------------------------------------ voice

    @app.post("/api/agent/studio_voice")
    def agent_voice(project: str, body: VoiceBody):
        def run():
            return engine.voice_line(store, backend, project, body.text, body.character_id, body.voice, body.speed)
        try:
            return agent_call("studio_voice", body.text[:60], run)
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/voices")
    def voices_list():
        from . import voices as voices_mod
        return {"items": voices_mod.list_curated_voices()}

    # ------------------------------------------------------------------ import

    @app.post("/api/agent/studio_import")
    def agent_import(project: str, body: ImportBody):
        def run():
            return engine.import_asset(store, project, Path(body.path), body.kind)
        try:
            return agent_call("studio_import", body.path, run)
        except Exception as exc:
            return _error_response(exc)

    @app.post("/api/projects/{project_id}/import-upload")
    async def import_upload(project_id: str, file: UploadFile, kind: Optional[str] = None):
        ext = Path(file.filename or "").suffix.lower()
        cap = MAX_IMAGE_BYTES if ext in engine.IMAGE_EXTS else MAX_VIDEO_AUDIO_BYTES
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            total = 0
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > cap:
                    Path(tmp.name).unlink(missing_ok=True)
                    return JSONResponse({"error": "too_large", "message": f"file exceeds {cap} bytes"}, status_code=400)
                tmp.write(chunk)
            tmp_path = Path(tmp.name)
        try:
            asset = engine.import_asset(store, project_id, tmp_path, kind, original_name=file.filename)
        finally:
            tmp_path.unlink(missing_ok=True)
        return asset

    # ------------------------------------------------------------------ audio
    @app.post("/api/agent/studio_analyze_audio")
    def agent_analyze_audio(asset_id: str):
        try:
            return agent_call("studio_analyze_audio", asset_id, lambda: engine.analyze_audio(store, asset_id))
        except Exception as exc:
            return _error_response(exc)


    @app.post("/api/assets/{asset_id}/lyrics")
    def save_lyrics(asset_id: str, body: LyricsBody):
        from . import audio as audio_mod
        try:
            asset = store.get_asset(asset_id)
        except NotFound as exc:
            return _error_response(exc)
        text = body.lrc_text if body.lrc_text is not None else audio_mod.to_lrc(body.lines or [])
        path = store.data_dir / asset["file_path"]
        path.write_text(text, encoding="utf-8")
        return {"asset_id": asset_id, "lrc": text}

    # --------------------------------------------------------------- design

    @app.post("/api/agent/studio_design")
    def agent_design(project: str, body: DesignBody):
        def run():
            fields = dict(body.fields)
            if body.image_asset_id:
                fields.setdefault("image", body.image_asset_id)
                fields.setdefault("cover_image", body.image_asset_id)
            return engine.render_design(store, project, body.template, fields, body.variant,
                                         print_mode=bool(body.options.get("print")))
        try:
            return agent_call("studio_design", body.template, run)
        except Exception as exc:
            return _error_response(exc)


    @app.post("/api/agent/studio_photocard_set")
    def agent_photocard_set(project: str, body: PhotocardSetBody):
        def run():
            return engine.photocard_set(store, project, body.group_id, body.template_front, body.template_back, body.image_asset_ids)
        try:
            return agent_call("studio_photocard_set", body.group_id, run)
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/design/templates")
    def design_templates_list():
        from . import templates as design_templates
        return {"items": list(design_templates.TEMPLATE_DIMENSIONS.keys())}

    # ------------------------------------------------------------- timeline

    @app.post("/api/agent/studio_timeline")
    def agent_timeline(project: str, body: TimelineBody):
        def run():
            if body.action == "auto":
                return engine.timeline_auto(store, project, body.song_asset_id, body.asset_ids, body.board_id,
                                             body.aspect, body.lyrics_asset_id, body.options)
            if body.action == "get":
                return store.get_timeline(body.timeline_id)
            if body.action == "update":
                return store.update_timeline(body.timeline_id, **body.patch)
            raise ValueError(f"unknown timeline action '{body.action}'")
        try:
            return agent_call("studio_timeline", body.action, run)
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/projects/{project_id}/timelines")
    def list_timelines(project_id: str):
        return {"items": store.list_timelines(project_id)}


    @app.post("/api/agent/studio_render")
    def agent_render(body: RenderBody):
        def run():
            tl = store.get_timeline(body.timeline_id)
            job = queue.enqueue("render_timeline", "cpu", {"timeline_id": body.timeline_id, "quality": body.quality},
                                 project_id=tl["project_id"])
            if body.wait_s > 0:
                job = queue.wait_for(job["id"], body.wait_s)
            return {"job": job}
        try:
            return agent_call("studio_render", body.timeline_id, run)
        except Exception as exc:
            return _error_response(exc)

    # ----------------------------------------------------------------- jobs
    @app.get("/api/agent/studio_jobs")
    def agent_jobs(state: Optional[str] = None, limit: int = 10):
        return agent_call("studio_jobs", state or "", lambda: store.list_jobs(state, limit))

    @app.get("/api/agent/studio_job")
    def agent_job(job_id: str, wait_s: float = 0):
        def run():
            if wait_s > 0:
                return queue.wait_for(job_id, wait_s)
            return store.get_job(job_id)
        try:
            return agent_call("studio_job", job_id, run)
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/jobs")
    def list_jobs(state: Optional[str] = None, limit: int = 30, offset: int = 0):
        return store.list_jobs(state, limit, offset)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return store.get_job(job_id)
        except NotFound as exc:
            return _error_response(exc)

    # --------------------------------------------------------------- assets
    @app.get("/api/agent/studio_assets")
    def agent_assets(project: str, kind: Optional[str] = None, query: Optional[str] = None,
                      tag: Optional[str] = None, favourite: Optional[bool] = None, limit: int = 12):
        return agent_call("studio_assets", f"{kind or ''}:{query or ''}",
                           lambda: store.list_assets(project, kind, query, tag, favourite, limit))

    @app.get("/api/projects/{project_id}/assets")
    def list_assets(project_id: str, kind: Optional[str] = None, query: Optional[str] = None,
                     tag: Optional[str] = None, favourite: Optional[bool] = None, limit: int = 60, offset: int = 0):
        return store.list_assets(project_id, kind, query, tag, favourite, limit, offset)

    @app.get("/api/assets/{asset_id}")
    def get_asset(asset_id: str):
        try:
            return store.get_asset(asset_id)
        except NotFound as exc:
            return _error_response(exc)


    @app.patch("/api/assets/{asset_id}")
    def update_asset(asset_id: str, body: UpdateAssetBody):
        try:
            return store.update_asset(asset_id, **body.model_dump(exclude_none=True))
        except NotFound as exc:
            return _error_response(exc)

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id: str):
        try:
            asset = store.get_asset(asset_id)
        except NotFound as exc:
            return _error_response(exc)
        path = store.data_dir / asset["file_path"]
        if not path.is_file():
            return JSONResponse({"error": "not_found", "message": "asset file missing on disk"}, status_code=404)
        return FileResponse(path, media_type=asset.get("mime") or mimetypes.guess_type(str(path))[0])

    @app.get("/api/assets/{asset_id}/thumb")
    def asset_thumb(asset_id: str):
        try:
            asset = store.get_asset(asset_id)
        except NotFound as exc:
            return _error_response(exc)
        if not asset.get("thumb_path"):
            return JSONResponse({"error": "no_thumb", "message": "asset has no thumbnail"}, status_code=404)
        path = store.data_dir / asset["thumb_path"]
        if not path.is_file():
            return JSONResponse({"error": "not_found", "message": "thumbnail missing on disk"}, status_code=404)
        return FileResponse(path, media_type="image/webp")

    @app.get("/api/agent/studio_show")
    def agent_show(asset_ids: str, size: int = 768):
        def run():
            ids = asset_ids.split(",")
            images = engine.show_assets(store, ids, size)
            return {"items": [{"asset_id": i["asset_id"], "kind": i["kind"], "mime": i["mime"],
                                "base64": __import__("base64").b64encode(i["bytes"]).decode("ascii")} for i in images]}
        try:
            return agent_call("studio_show", asset_ids, run)
        except Exception as exc:
            return _error_response(exc)

    @app.get("/api/agent/studio_lineage")
    def agent_lineage(asset_id: str):
        try:
            return agent_call("studio_lineage", asset_id, lambda: engine.get_lineage(store, asset_id))
        except Exception as exc:
            return _error_response(exc)

    # ---------------------------------------------------------------- boards

    @app.post("/api/projects/{project_id}/boards")
    def create_board(project_id: str, body: BoardBody):
        return store.create_board(project_id, body.name, body.kind)

    @app.get("/api/projects/{project_id}/boards")
    def list_boards(project_id: str):
        return {"items": store.list_boards(project_id)}


    @app.put("/api/boards/{board_id}/items")
    def update_board_items(board_id: str, body: BoardItemsBody):
        try:
            return store.update_board_items(board_id, body.items)
        except NotFound as exc:
            return _error_response(exc)

    # ------------------------------------------------------------- status --
    @app.get("/api/agent/studio_status")
    def agent_status():
        def run():
            status = backend.status()
            jobs_summary = store.list_jobs(limit=5)
            return {"backend": status, "recent_jobs": jobs_summary}
        try:
            return agent_call("studio_status", "", run)
        except Exception as exc:
            return _error_response(exc)

    # -------------------------------------------------------- static / SPA
    if static_dir and Path(static_dir).is_dir() and (Path(static_dir) / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets-ui", StaticFiles(directory=str(static_dir)), name="ui-assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            candidate = Path(static_dir) / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(Path(static_dir) / "index.html")
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
