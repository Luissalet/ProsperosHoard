"""Local Lumiere Motion and Lumiere Edit workflows and their generated artifacts."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import media as media_store, projects as project_store
from ..creative_engines import CreativeEngineError, CreativeEngines
from ..errors import LumiereError, NotFound
from .deps import services

router = APIRouter(prefix="/api/creative", tags=["creative engines"])


@router.get("")
def list_projects(request: Request, engine: str | None = None, project_id: str | None = None):
    try:
        return _engine(request).list(engine,project_id)
    except CreativeEngineError as exc:
        raise LumiereError(str(exc),code="creative_engine") from None


class TitleCardBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=300)
    width: int = Field(1280, ge=16, le=8192)
    height: int = Field(720, ge=16, le=8192)
    fps: float = Field(24, ge=1, le=120)
    duration: float = Field(3, ge=0.5, le=30)
    project_id: Optional[str] = Field(None, max_length=32)


class FilmSequenceBody(BaseModel):
    media_id: str = Field(..., min_length=1, max_length=32)
    project_id: Optional[str] = Field(None, max_length=32)
    width: Optional[int] = Field(None, ge=16, le=8192)
    height: Optional[int] = Field(None, ge=16, le=8192)
    fps: Optional[float] = Field(None, ge=1, le=120)


class NativeCallBody(BaseModel):
    calls: list[dict[str, Any]] = Field(..., min_length=1, max_length=32)


def _engine(request: Request) -> CreativeEngines:
    svc = services(request)
    return CreativeEngines(svc.config.data_dir, port=svc.config.port)


def _link_project(request: Request, project_id: str | None) -> None:
    if project_id:
        project_store.doc(services(request), project_id)


def _artifact(request: Request, creative_id: str, key: str, suffixes: set[str]) -> Path:
    engine = _engine(request)
    try:
        manifest = engine.get(creative_id)
    except CreativeEngineError as exc:
        raise NotFound(str(exc)) from None
    raw = manifest.get(key)
    if not raw:
        raise NotFound("This creative project has no such artifact.")
    path = Path(raw).resolve()
    folder = engine._project_dir(creative_id).resolve()
    if not path.is_relative_to(folder) or path.suffix.lower() not in suffixes or not path.is_file():
        raise NotFound("This creative project artifact is missing.")
    return path


@router.get("/status")
def status(request: Request):
    return _engine(request).status()


@router.get("/tools/{engine}")
def tools(request: Request, engine: str):
    try:
        return _engine(request).tools(engine)
    except CreativeEngineError as exc:
        raise LumiereError(str(exc), code="creative_engine") from None


@router.post("/title-card")
def title_card(request: Request, body: TitleCardBody):
    _link_project(request, body.project_id)
    try:
        return _engine(request).create_title_card(text=body.text, width=body.width, height=body.height,
                                                  fps=body.fps, duration=body.duration, project_id=body.project_id)
    except CreativeEngineError as exc:
        raise LumiereError(str(exc), code="creative_engine") from None


@router.post("/film-sequence")
def film_sequence(request: Request, body: FilmSequenceBody):
    svc = services(request)
    _link_project(request, body.project_id)
    media = media_store.get(svc, body.media_id)
    if media["kind"] != "video" or not media["has_video"]:
        raise LumiereError("Lumiere Edit sequence import requires a video with a video stream.", code="bad_media_kind")
    width = body.width or int(media.get("width") or 1920)
    height = body.height or int(media.get("height") or 1080)
    fps = body.fps or float(media.get("fps") or 24)
    try:
        return _engine(request).create_film_sequence(Path(media["path"]), source_media_id=body.media_id,
                                                      width=width, height=height, fps=fps,
                                                      project_id=body.project_id)
    except CreativeEngineError as exc:
        raise LumiereError(str(exc), code="creative_engine") from None


@router.get("/{creative_id}")
def get_project(request: Request, creative_id: str):
    try:
        return _engine(request).get(creative_id)
    except CreativeEngineError as exc:
        raise NotFound(str(exc)) from None


@router.post("/{creative_id}/render-video")
def render_video(request: Request, creative_id: str):
    try:
        return _engine(request).render_title_video(creative_id)
    except CreativeEngineError as exc:
        raise LumiereError(str(exc), code="creative_render") from None


@router.post("/{creative_id}/call")
def native_call(request: Request, creative_id: str, body: NativeCallBody):
    try:
        return _engine(request).call(creative_id, body.calls)
    except CreativeEngineError as exc:
        raise LumiereError(str(exc), code="creative_engine") from None


@router.get("/{creative_id}/preview")
def preview(request: Request, creative_id: str):
    return FileResponse(_artifact(request, creative_id, "preview_path", {".png"}), media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@router.get("/{creative_id}/project")
def native_project(request: Request, creative_id: str):
    _engine(request).get(creative_id)
    path = _artifact(request, creative_id, "native_path", {".lumotion", ".lumedit"})
    return FileResponse(path, filename=path.name, headers={"Cache-Control": "no-store"})


@router.get("/{creative_id}/render")
def render(request: Request, creative_id: str):
    path = _artifact(request, creative_id, "render_path", {".mp4", ".mov", ".mxf"})
    return FileResponse(path, filename=path.name, media_type="video/mp4", headers={"Cache-Control": "no-store"})
