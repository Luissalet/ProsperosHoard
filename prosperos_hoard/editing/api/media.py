"""/api/media — library, import, upload, playback files, filmstrip, waveform, transcript, analyses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .. import analyze, commands, speakers as speakers_mod
from .. import media as media_store
from ..errors import LumiereError, NotFound
from .deps import services, tool

router = APIRouter(prefix="/api/media")


class ImportBody(BaseModel):
    path: str = ""
    folder: str = ""
    recursive: bool = False


class PatchBody(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    relink: Optional[str] = Field(None, max_length=2000)
    tags: Optional[list[str]] = Field(None, max_length=30)


class AnalyzeBody(BaseModel):
    kinds: list[str]
    force: bool = False
    language: str = ""
    num_speakers: Optional[int] = Field(None, ge=1, le=12)
    speakers: bool = False
    engine: str = Field("auto", pattern="^(auto|builtin|embeddings)$")


class SpeakersBody(BaseModel):
    action: str = Field(..., pattern="^(diarize|rename|assign|merge|clear)$")
    num_speakers: Optional[int] = Field(None, ge=1, le=12)
    engine: str = Field("auto", pattern="^(auto|builtin|embeddings)$")
    names: dict[str, str] = Field(default_factory=dict)
    speaker: str = ""
    word_ids: list[str] = Field(default_factory=list)
    from_ms: Optional[int] = None
    to_ms: Optional[int] = None
    source: str = ""
    into: str = ""


class WordsBody(BaseModel):
    changes: list[dict[str, str]]


@router.get("")
def list_media(request: Request, kind: Optional[str] = None, q: str = ""):
    return {"media": media_store.list_media(services(request), kind=kind, query=q)}


@router.post("/import")
def import_media(request: Request, body: ImportBody):
    svc = services(request)
    if body.folder:
        return media_store.import_folder(svc, body.folder, recursive=body.recursive)
    if not body.path:
        raise LumiereError("Give a path or a folder.")
    return media_store.import_path(svc, body.path)


@router.post("/upload")
async def upload(request: Request, file: UploadFile):
    svc = services(request)
    return media_store.save_upload(svc, file.filename or "upload", file.file, svc.config.max_upload_bytes)


@router.get("/{media_id}")
def get_media(request: Request, media_id: str):
    svc = services(request)
    info = media_store.get(svc, media_id)
    sprite = svc.config.cache_dir / media_id / "sprite.json"
    info["sprite_info"] = json.loads(sprite.read_text(encoding="utf-8")) if sprite.exists() else None
    info["levels"] = media_store.get_analysis(svc, media_id, "levels")
    info["jobs"] = svc.jobs.list(media_id=media_id, limit=10)
    return info


@router.patch("/{media_id}")
def patch_media(request: Request, media_id: str, body: PatchBody):
    svc = services(request)
    if body.relink:
        media_store.relink(svc, media_id, body.relink)
    if body.name:
        media_store.rename(svc, media_id, body.name)
    if body.tags is not None:
        media_store.set_tags(svc, media_id, body.tags)
    return media_store.get(svc, media_id)


@router.delete("/{media_id}")
def delete_media(request: Request, media_id: str, force: bool = False):
    return media_store.delete(services(request), media_id, force=force)


@router.post("/{media_id}/prepare")
def prepare(request: Request, media_id: str):
    return media_store.schedule_prepare(services(request), media_id)


def _file(path: Path, media_type: Optional[str] = None) -> FileResponse:
    if not path.exists():
        raise NotFound("The file is not there (yet).")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@router.get("/{media_id}/play")
def play(request: Request, media_id: str):
    return _file(media_store.play_path(services(request), media_id))


@router.get("/{media_id}/file")
def original(request: Request, media_id: str):
    return _file(Path(media_store.get(services(request), media_id)["path"]))


@router.get("/{media_id}/poster")
def poster(request: Request, media_id: str):
    svc = services(request)
    p = svc.config.cache_dir / media_id / "poster.jpg"
    if not p.exists() and media_store.get(svc, media_id)["kind"] == "image":
        return _file(Path(media_store.get(svc, media_id)["path"]))
    return _file(p, "image/jpeg")


@router.get("/{media_id}/sprite")
def sprite(request: Request, media_id: str):
    return _file(services(request).config.cache_dir / media_id / "sprite.jpg", "image/jpeg")


@router.get("/{media_id}/waveform")
def waveform(request: Request, media_id: str):
    path = services(request).config.cache_dir / media_id / "wave.bin"
    if not path.exists():
        raise NotFound("No waveform yet.")
    return Response(path.read_bytes(), media_type="application/octet-stream", headers={"Cache-Control": "no-cache", "X-Rate": "100"})


@router.get("/{media_id}/transcript")
def transcript(request: Request, media_id: str):
    t = analyze.transcript(services(request), media_id)
    if t is None:
        return JSONResponse({"media": media_id, "words": None})
    return {"media": media_id, **t}


@router.patch("/{media_id}/transcript")
def fix_words(request: Request, media_id: str, body: WordsBody):
    return analyze.update_words(services(request), media_id, body.changes)


@router.post("/{media_id}/analyze")
def run_analyze(request: Request, media_id: str, body: AnalyzeBody):
    return {"jobs": analyze.schedule(services(request), media_id, body.kinds, force=body.force, language=body.language,
                                     num_speakers=body.num_speakers, speakers=body.speakers, engine=body.engine)}


@router.get("/{media_id}/speakers")
def get_speakers(request: Request, media_id: str):
    """Speakers of the transcript; before there is a transcript, an empty answer (with the engine status) instead of an error."""
    svc = services(request)
    try:
        return speakers_mod.summary(svc, media_id)
    except LumiereError as error:
        if error.code != "no_transcript":
            raise
        return {"media": media_id, "diarized": False, "speakers": [], "turns": 0, "turns_list": [], "transcribed": False, "status": speakers_mod.status()}


@router.post("/{media_id}/speakers")
def edit_speakers(request: Request, media_id: str, body: SpeakersBody):
    return tool(request, "speakers_edit", media=media_id, **body.model_dump(exclude_none=True))


@router.get("/{media_id}/frame")
def media_frame(request: Request, media_id: str, t: int = 0, width: int = 240):
    return _file(media_store.frame_path(services(request), media_id, t, width), "image/jpeg")


@router.get("/{media_id}/analysis/{kind}")
def get_analysis(request: Request, media_id: str, kind: str):
    res = media_store.get_analysis(services(request), media_id, kind)
    if res is None:
        raise NotFound(f"No {kind} analysis yet.")
    return res


@router.get("/{media_id}/silences")
def silences(request: Request, media_id: str, threshold_db: Optional[float] = None, min_silence_ms: int = 500, margin_ms: int = 150):
    return analyze.silences_for(services(request), media_id, threshold_db=threshold_db, min_silence_ms=min_silence_ms, margin_ms=margin_ms)


@router.get("/{media_id}/highlights")
def highlights(request: Request, media_id: str, count: int = 5, length_s: float = 30, mode: str = "signals"):
    return commands.highlights(services(request), media_id, count=count, length_ms=int(length_s * 1000), mode=mode)


class ShortBody(BaseModel):
    start: Any
    end: Any
    name: str = "Corto"
    preset: str = "reels"
    captions: bool = True


@router.post("/{media_id}/short")
def short(request: Request, media_id: str, body: ShortBody):
    return tool(request, "short_from_range", media=media_id, **body.model_dump())
