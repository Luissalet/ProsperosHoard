"""The HTML motion workshop, sharing the service used by assistant tools."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .. import family_events, projects
from ..errors import NotFound
from ..html_motion import HtmlMotion
from ..html_starters import HtmlStarterArgs, catalogue as starters_catalogue, save_args as starter_source
from ..html_motion_args import (HtmlMotionSaveArgs, HtmlMotionRestoreArgs, HtmlMotionStoryboardArgs, MotionSoundEventsArgs, MotionTreatmentArgs,
                                MotionCuesArgs, MotionCuecheckArgs, MotionReadcheckArgs, MotionFactsArgs, MotionFromStyleArgs, MotionLintArgs)
from .deps import services, tool

router = APIRouter(prefix="/api/html-motion", tags=["HTML motion"])


def _store(request: Request) -> HtmlMotion:
    return HtmlMotion(services(request).config.data_dir)


@router.get("")
def list_compositions(request: Request):
    return _store(request).list()


@router.post("")
def save(request: Request, body: HtmlMotionSaveArgs):
    if body.project_id:
        projects.doc(services(request), body.project_id)
    return _store(request).save(**body.model_dump())


@router.get("/starters")
def starters():
    return starters_catalogue()


@router.post("/starters")
def create_starter(request: Request, body: HtmlStarterArgs):
    return save(request, starter_source(body))


@router.get("/styles")
def styles(request: Request, q: str | None = Query(None), source: str | None = Query(None), category: str | None = Query(None),
           limit: int = Query(20, ge=1, le=100)):
    return tool(request, "motion_styles", action="search", query=q, source=source, category=category, limit=limit)


@router.get("/styles/{card_id:path}")
def style(request: Request, card_id: str, section: str | None = Query(None)):
    return tool(request, "motion_styles", action="get", id=card_id, section=section)


@router.post("/from-style")
def from_style(request: Request, body: MotionFromStyleArgs):
    return tool(request, "motion_from_style", **body.model_dump(exclude_none=True))


@router.get("/{composition}")
def get(request: Request, composition: str, revision: int | None = Query(None, ge=1)):
    store = _store(request)
    manifest = store.get(composition, revision)
    revisions = sorted(int(p.name) for p in store.folder(composition).iterdir() if p.is_dir() and p.name.isdigit())
    return {**manifest, "html": store.source(manifest), "revisions": revisions}


@router.post("/restore")
def restore(request: Request, body: HtmlMotionRestoreArgs):
    return _store(request).restore(**body.model_dump())


@router.delete("/{composition}")
def delete(request: Request, composition: str):
    return tool(request, "html_motion_delete", composition=composition)


@router.get("/{composition}/revisions/{revision}/source")
def source(request: Request, composition: str, revision: int):
    store = _store(request)
    manifest = store.get(composition, revision)
    return FileResponse(manifest["source_path"], media_type="text/html", filename="composition.html", headers={"Cache-Control": "no-store"})


@router.get("/{composition}/revisions/{revision}/frame")
def frame(request: Request, composition: str, revision: int, at: float = Query(0, ge=0)):
    data, info = _store(request).frame(composition, at, revision)
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store", "X-Frame-Sha256": info["frame_sha256"]})


@router.post("/{composition}/render")
def render(request: Request, composition: str, revision: int | None = Query(None, ge=1), audio: bool = Query(True)):
    return _store(request).queue_render(services(request), composition, revision, audio=audio)


@router.get("/sound/roles")
def sound_roles(request: Request, kit: str = Query("launch")):
    return tool(request, "motion_sound_roles", kit=kit)


@router.get("/{composition}/sound")
def sound_get(request: Request, composition: str, revision: int | None = Query(None, ge=1)):
    return tool(request, "motion_sound_events", composition=composition, revision=revision)


@router.post("/{composition}/sound")
def sound_change(request: Request, composition: str, body: MotionSoundEventsArgs):
    if body.composition != composition:
        raise NotFound("Sound composition does not match the request.")
    return tool(request, "motion_sound_events", **body.model_dump(exclude_none=True))


@router.post("/{composition}/sound/preview")
def sound_preview(request: Request, composition: str, revision: int | None = Query(None, ge=1)):
    return tool(request, "motion_sound_preview", composition=composition, revision=revision)


@router.get("/{composition}/revisions/{revision}/sound/{digest}/audio")
def sound_audio(request: Request, composition: str, revision: int, digest: str):
    path = _store(request).sound_file(composition, revision, digest)
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.post('/{composition}/storyboard')
def storyboard(request: Request, composition: str, body: HtmlMotionStoryboardArgs):
    if body.composition != composition:
        raise NotFound('Storyboard composition does not match the request.')
    return _store(request).storyboard(composition, body.times, body.revision, mode=body.mode, span=tuple(body.span) if body.span else None,
                                      x=body.x, y=body.y, selector=body.selector, size=tuple(body.size) if body.size else None, scale=body.scale)


@router.get("/{composition}/treatment")
def treatment_get(request: Request, composition: str):
    return tool(request, "motion_treatment", action="get", composition=composition)


@router.post("/{composition}/treatment")
def treatment_change(request: Request, composition: str, body: MotionTreatmentArgs):
    return tool(request, "motion_treatment", **{**body.model_dump(exclude_none=True), "composition": composition})


@router.get("/{composition}/cues")
def cues_get(request: Request, composition: str):
    return tool(request, "motion_cues", composition=composition)


@router.post("/{composition}/cues")
def cues_change(request: Request, composition: str, body: MotionCuesArgs):
    if body.composition != composition:
        raise NotFound("Cues composition does not match the request.")
    return tool(request, "motion_cues", **body.model_dump(exclude_none=True))


@router.post("/{composition}/cuecheck")
def cuecheck(request: Request, composition: str, body: MotionCuecheckArgs):
    if body.composition != composition:
        raise NotFound("Cuecheck composition does not match the request.")
    return tool(request, "motion_cuecheck", **body.model_dump(exclude_none=True))


@router.post("/{composition}/readcheck")
def readcheck(request: Request, composition: str, body: MotionReadcheckArgs):
    if body.composition != composition:
        raise NotFound("Readcheck composition does not match the request.")
    return tool(request, "motion_readcheck", **body.model_dump(exclude_none=True))


@router.post("/{composition}/lint")
def lint(request: Request, composition: str, body: MotionLintArgs):
    if body.composition != composition:
        raise NotFound("Lint composition does not match the request.")
    return tool(request, "motion_lint", **body.model_dump(exclude_none=True))


@router.get("/{composition}/facts")
def facts_get(request: Request, composition: str):
    return tool(request, "motion_facts", action="get", composition=composition)


@router.post("/{composition}/facts")
def facts_change(request: Request, composition: str, body: MotionFactsArgs):
    if body.composition != composition:
        raise NotFound("Facts composition does not match the request.")
    return tool(request, "motion_facts", **body.model_dump(exclude_none=True))


@router.get('/{composition}/revisions/{revision}/storyboards/{storyboard}/image')
def storyboard_image(request: Request, composition: str, revision: int, storyboard: str):
    data = _store(request).storyboard_image(composition, revision, storyboard)
    return Response(data, media_type='image/png', headers={'Cache-Control':'no-store'})


@router.get('/{composition}/revisions/{revision}/storyboards/{storyboard}/frames/{frame}')
def storyboard_frame(request: Request, composition: str, revision: int, storyboard: str, frame: int):
    data = _store(request).storyboard_image(composition, revision, storyboard, frame)
    return Response(data, media_type='image/png', headers={'Cache-Control':'no-store'})


@router.get('/{composition}/revisions/{revision}/storyboards/{storyboard}/source')
def storyboard_source(request: Request, composition: str, revision: int, storyboard: str):
    data = _store(request).storyboard_source(composition, revision, storyboard)
    return Response(data, media_type='text/html', headers={'Cache-Control':'no-store',
        'Content-Disposition':'attachment; filename="composition-reviewed.html"'})


@router.get("/{composition}/revisions/{revision}/render/{job}")
def video(request: Request, composition: str, revision: int, job: str):
    store = _store(request)
    store.get(composition, revision)
    result = services(request).jobs.get(job)
    if result["state"] != "done" or result["kind"] != "html_motion" or {k: v for k, v in result["params"].items() if k != "audio"} != {"composition": composition, "revision": revision}:
        raise NotFound("This HTML video export is not ready.")
    path = store.folder(composition) / str(revision) / f"render-{job}.mp4"
    if not path.is_file():
        raise NotFound("HTML video export is missing.")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


class ReceiveBody(BaseModel):
    project: str = ""
    revision: int | None = None


@router.post("/{composition}/receive")
def receive(request: Request, composition: str, body: ReceiveBody):
    manifest = _store(request).get(composition, body.revision)
    if not manifest.get("render_path"):
        raise NotFound("Render the current HTML revision before adding it to the library.")
    return family_events.receive_media(services(request), path=manifest["render_path"], name=manifest["name"],
                                       project=body.project, source="lumiere")
