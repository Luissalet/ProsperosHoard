"""/api/projects — timeline documents, edits, undo, smart commands, plans, frames, subtitles and renders."""

from __future__ import annotations

from typing import Any, Optional, Literal
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from .. import commands, derive, multicam, timeline_import
from ..errors import LumiereError
from .. import subtitles as subs
from .. import plan as plan_mod
from .. import projects as store
from ..render import runner
from ..util import parse_time
from .deps import services, tool

router = APIRouter(prefix="/api")


class CreateBody(BaseModel):
    name: str = Field("Proyecto", max_length=120)
    preset: Optional[str] = None
    media: list[str] = Field(default_factory=list)
    from_project: Optional[str] = None
    template: bool = False


class ImportTimelineBody(BaseModel):
    path: str = Field(...,min_length=1,max_length=2000)
    format: Literal['otio','fcpxml','edl','auto'] = 'auto'
    title: str = Field('',max_length=120)
    media_dirs: list[str] = Field(default_factory=list)
    fps: Optional[float] = Field(None,ge=1,le=240)


class PatchBody(BaseModel):
    name: Optional[str] = None
    template: Optional[bool] = None


class ContactSheetBody(BaseModel):
    mode: Literal["overview", "boundaries", "adaptive"] = "overview"
    count: int = Field(12, ge=2, le=16)
    width: int = Field(320, ge=128, le=640)
    times: Optional[list[int]] = Field(None, min_length=1, max_length=16)


class TemplateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slots: dict[str, str] = Field(default_factory=dict)


class FromTemplateBody(BaseModel):
    name: str = ""
    slots: dict[str, Any] = Field(default_factory=dict)


class BrollBody(BaseModel):
    start: Any = None
    end: Any = None
    per_sentence: int = 3
    use_model: bool = False
    place: str = "none"


class EditBody(BaseModel):
    ops: list[dict[str, Any]]
    label: str = ""
    base_rev: Optional[int] = None
    request_id: str = Field('', max_length=100)


class CommandBody(BaseModel):
    command: str
    args: dict[str, Any] = Field(default_factory=dict)
    preview: bool = False


class TextCutBody(BaseModel):
    media: str
    word_ids: list[str] = Field(default_factory=list)
    phrase: str = ""
    keep: bool = False


class PlanBody(BaseModel):
    instruction: str
    use_model: bool = True


class PlanStepsBody(BaseModel):
    steps: list[dict[str, Any]]


class RenderBody(BaseModel):
    preset: str = "final"
    start: Any = None
    end: Any = None
    filename: str = ""
    folder: str = ""
    lufs: Any = "default"
    subtitles: bool = False
    mode: str = "render"
    formats: list[Any] = Field(default_factory=list)
    reframe: str = "auto"
    captions_language: str = ""
    captions_dual: bool = False


class TranslateBody(BaseModel):
    language: str
    glossary: dict[str, str] = Field(default_factory=dict)
    keep: list[str] = Field(default_factory=list)
    force: bool = False


class TranslationFixBody(BaseModel):
    changes: list[dict[str, Any]]


class MulticamSyncBody(BaseModel):
    media: list[str] = Field(..., min_length=2, max_length=12)
    reference: Optional[str] = None
    offsets: dict[str, float] = Field(default_factory=dict)


class FreezeBody(BaseModel):
    clip: str
    at: Any
    length: Any = 2000


class StabilizeBody(BaseModel):
    clip: str
    smoothing: int = 15


class NestBody(BaseModel):
    clips: list[str] = Field(..., min_length=1, max_length=2000)
    name: str = Field("", max_length=120)


@router.get("/projects")
def list_projects(request: Request, templates: Optional[bool] = None):
    return {"projects": store.list_projects(services(request), templates)}


@router.post("/projects")
def create(request: Request, body: CreateBody):
    return store.create(services(request), body.name, preset=body.preset, media=body.media or None, template=body.template,
                        from_project=body.from_project)


@router.get("/projects/{project_id}")
def get(request: Request, project_id: str):
    return store.view(services(request), project_id)


@router.post('/projects/import-timeline')
def import_timeline(request: Request,body: ImportTimelineBody):
    fmt=body.format
    if fmt=='auto':fmt={'.otio':'otio','.xml':'fcpxml','.edl':'edl'}.get(Path(body.path).suffix.lower())
    if not fmt:raise LumiereError('Choose an OTIO, FCP7 XML or EDL timeline.',code='bad_request')
    return timeline_import.project_from_timeline(services(request),title=body.title,media_dirs=body.media_dirs,fps=body.fps,
                                                  **{fmt+'_path':body.path},actor='ui')


@router.patch("/projects/{project_id}")
def patch(request: Request, project_id: str, body: PatchBody):
    svc = services(request)
    if body.name is not None:
        store.rename(svc, project_id, body.name)
    if body.template is not None:
        store.set_template(svc, project_id, body.template)
    return store.view(svc, project_id)


@router.delete("/projects/{project_id}")
def delete(request: Request, project_id: str):
    return store.delete(services(request), project_id)


@router.post("/projects/{project_id}/edit")
def edit(request: Request, project_id: str, body: EditBody):
    svc = services(request)
    res = store.edit(svc, project_id, body.ops, label=body.label, base_rev=body.base_rev, request_id=body.request_id)
    return {**res, "view": store.view(svc, project_id)}


@router.post("/projects/{project_id}/undo")
def undo(request: Request, project_id: str):
    svc = services(request)
    store.undo(svc, project_id)
    return store.view(svc, project_id)


@router.post("/projects/{project_id}/redo")
def redo(request: Request, project_id: str):
    svc = services(request)
    store.redo(svc, project_id)
    return store.view(svc, project_id)


@router.get("/projects/{project_id}/history")
def history(request: Request, project_id: str):
    return store.history(services(request), project_id)


@router.post("/projects/{project_id}/history/{seq}")
def restore(request: Request, project_id: str, seq: int):
    svc = services(request)
    store.restore(svc, project_id, seq)
    return store.view(svc, project_id)


@router.get("/projects/{project_id}/outline")
def outline(request: Request, project_id: str):
    return store.outline(services(request), project_id)


@router.post("/projects/{project_id}/command")
def command(request: Request, project_id: str, body: CommandBody):
    svc = services(request)
    try:
        res = commands.run(svc, project_id, body.command, body.args, preview=body.preview)
    except commands.NeedsAnalysis as need:
        return {"done": False, "needs": need.jobs, "message": str(need)}
    return {**res, "done": True, "view": None if body.preview else store.view(svc, project_id)}


@router.get("/projects/{project_id}/transcript")
def timeline_transcript(request: Request, project_id: str, track: Optional[str] = None):
    svc = services(request)
    return commands.timeline_transcript(svc, store.doc(svc, project_id), track=track)


@router.post("/projects/{project_id}/text-cut")
def text_cut(request: Request, project_id: str, body: TextCutBody):
    svc = services(request)
    try:
        res = commands.run(svc, project_id, "cut_words", {"media": body.media, "word_ids": body.word_ids or None, "text": body.phrase or None,
                                                           "keep": body.keep})
    except commands.NeedsAnalysis as need:
        return {"done": False, "needs": need.jobs, "message": str(need)}
    return {**res, "done": True, "view": store.view(svc, project_id)}


@router.post("/multicam/sync")
def multicam_sync(request: Request, body: MulticamSyncBody):
    """Measure the offsets of recordings of one event (nothing is edited): the 'Crear multicámara' dialog shows this before building the group."""
    return tool(request, "multicam_sync", media=body.media, reference=body.reference, offsets=body.offsets)


@router.get("/projects/{project_id}/multicam")
def multicam_view(request: Request, project_id: str, group: Optional[str] = None):
    svc = services(request)
    return multicam.view(store.doc(svc, project_id), group)


@router.get("/projects/{project_id}/multicam/frame")
def multicam_frame(request: Request, project_id: str, angle: str, t: int = 0, group: Optional[str] = None, width: int = 240):
    """Thumbnail of one angle at the moment of the event the timeline shows at ``t`` (server-made JPEG)."""
    svc = services(request)
    path = multicam.angle_frame(svc, store.doc(svc, project_id), group=group, angle=angle, t=t, width=width)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})


@router.get("/projects/{project_id}/frame")
def frame(request: Request, project_id: str, t: str = "0", width: int = 960):
    path = runner.render_frame(services(request), project_id, parse_time(t) if ":" in t or t.endswith("s") else int(float(t)), width=width)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/projects/{project_id}/contact-sheet")
def contact_sheet(request: Request, project_id: str, body: ContactSheetBody):
    return tool(request, "project_contact_sheet", project=project_id, **body.model_dump(), show=False)


@router.get("/projects/{project_id}/subtitles.{fmt}")
def subtitles(request: Request, project_id: str, fmt: str, language: str = "", dual: bool = False):
    text, mime = runner.subtitles_export(services(request), project_id, fmt, language=language, dual=dual)
    name = f"subtitulos{'.' + subs.resolve_language(language)[0] if language else ''}.{fmt}"
    return Response(text, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/projects/{project_id}/subtitles")
def subtitle_status(request: Request, project_id: str):
    """Translations of the project's captions: which languages, and whether each still matches the captions."""
    return subs.status(services(request), project_id)


@router.post("/projects/{project_id}/subtitles/translate")
def subtitle_translate(request: Request, project_id: str, body: TranslateBody):
    return subs.start_translation(services(request), project_id, body.language, glossary=body.glossary, keep=body.keep, force=body.force)


@router.get("/projects/{project_id}/subtitles/{language}")
def subtitle_show(request: Request, project_id: str, language: str, offset: int = 0, limit: int = 200):
    return subs.show(services(request), project_id, language, offset, limit)


@router.patch("/projects/{project_id}/subtitles/{language}")
def subtitle_fix(request: Request, project_id: str, language: str, body: TranslationFixBody):
    return subs.fix(services(request), project_id, language, body.changes)


@router.delete("/projects/{project_id}/subtitles/{language}")
def subtitle_delete(request: Request, project_id: str, language: str):
    return subs.delete(services(request), project_id, language)


@router.get("/projects/{project_id}/edl")
def edl(request: Request, project_id: str):
    return PlainTextResponse(runner.edl_export(services(request), project_id), headers={"Content-Disposition": 'attachment; filename="montaje.edl"'})


@router.get('/projects/{project_id}/otio')
def otio_export(request: Request, project_id: str):
    from ..interchange import export_project
    result = export_project(services(request), project_id)
    return Response(result['otio'],media_type='application/json',headers={'Content-Disposition':'attachment; filename="montage.otio"'})


@router.post("/projects/{project_id}/freeze")
def freeze(request: Request, project_id: str, body: FreezeBody):
    svc = services(request)
    at = parse_time(body.at) if isinstance(body.at, str) else int(body.at)
    length = parse_time(body.length) if isinstance(body.length, str) else int(body.length)
    derive.freeze_frame(svc, project_id, body.clip, at, length)
    return store.view(svc, project_id)


@router.get("/projects/{project_id}/music")
def music(request: Request, project_id: str, folder: str, recursive: bool = False, count: int = 5):
    return tool(request, "music_pick", project=project_id, folder=folder, recursive=recursive, count=count)


@router.post("/projects/{project_id}/broll")
def broll(request: Request, project_id: str, body: BrollBody):
    return tool(request, "broll_suggest", project=project_id, **body.model_dump())


@router.post("/projects/{project_id}/template")
def save_template(request: Request, project_id: str, body: TemplateBody):
    return store.save_template(services(request), project_id, body.name, body.slots)


@router.get("/templates")
def templates(request: Request):
    return {"templates": store.list_templates(services(request))}


@router.post("/templates/{template_id}/create")
def create_from_template(request: Request, template_id: str, body: FromTemplateBody):
    return store.create_from_template(services(request), body.name, template_id, body.slots)


@router.post("/projects/{project_id}/stabilize")
def stabilize(request: Request, project_id: str, body: StabilizeBody):
    return tool(request, "clip_stabilize", project=project_id, clip=body.clip, smoothing=body.smoothing)


@router.post("/projects/{project_id}/nest")
def nest(request: Request, project_id: str, body: NestBody):
    """Move the selected clips into a new project and leave one sequence clip in their place."""
    svc = services(request)
    res = store.nest(svc, project_id, body.clips, body.name)
    return {**res, "view": store.view(svc, project_id)}


@router.get("/projects/{project_id}/nesting")
def nesting(request: Request, project_id: str):
    """Nested sequences in this project (with their render state) and the projects that nest this one."""
    return tool(request, "timeline_nest", project=project_id, action="list")


@router.post("/projects/{project_id}/sequence/prepare")
def sequence_prepare(request: Request, project_id: str):
    """Render a project's intermediate now so the live preview of the projects nesting it can play it."""
    return tool(request, "timeline_nest", project=project_id, action="prepare")


@router.get("/projects/{project_id}/sequence.mp4")
def sequence_file(request: Request, project_id: str):
    from ..errors import NotFound
    from ..render import sequences

    m = sequences.cached(services(request), project_id)
    if m is None or m.alpha:
        raise NotFound("This sequence has no playable render yet.")
    return FileResponse(m.video, media_type="video/mp4")


@router.post("/projects/{project_id}/render")
def render(request: Request, project_id: str, body: RenderBody):
    svc = services(request)
    start = None if body.start in (None, "") else (parse_time(body.start) if isinstance(body.start, str) else int(body.start))
    end = None if body.end in (None, "") else (parse_time(body.end) if isinstance(body.end, str) else int(body.end))
    return svc.start_render(project_id, preset=body.preset, start=start, end=end, filename=body.filename, folder=body.folder,
                            lufs=body.lufs, subtitles=body.subtitles, mode=body.mode, formats=body.formats or None, reframe=body.reframe,
                            captions_language=body.captions_language, captions_dual=body.captions_dual)


@router.get("/projects/{project_id}/plans")
def plans(request: Request, project_id: str):
    return {"plans": plan_mod.list_plans(services(request), project_id)}


@router.post("/projects/{project_id}/plans")
def plan_create(request: Request, project_id: str, body: PlanBody):
    return plan_mod.create(services(request), project_id, body.instruction, use_model=body.use_model)


@router.patch("/plans/{plan_id}")
def plan_update(request: Request, plan_id: str, body: PlanStepsBody):
    return plan_mod.update(services(request), plan_id, body.steps)


@router.post("/plans/{plan_id}/apply")
def plan_apply(request: Request, plan_id: str):
    return tool(request, "plan_apply", plan=plan_id)


@router.post("/plans/{plan_id}/discard")
def plan_discard(request: Request, plan_id: str):
    return plan_mod.discard(services(request), plan_id)
