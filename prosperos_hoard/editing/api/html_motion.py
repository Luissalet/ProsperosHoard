"""The HTML motion workshop, sharing the service used by assistant tools."""
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from .. import family_events, projects
from ..errors import NotFound
from ..html_motion import HtmlMotion
from ..html_starters import HtmlStarterArgs, catalogue as starters_catalogue, save_args as starter_source
from ..html_motion_args import HtmlMotionSaveArgs, HtmlMotionRestoreArgs, HtmlMotionStoryboardArgs
from .deps import services

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


@router.get("/{composition}")
def get(request: Request, composition: str, revision: int | None = Query(None, ge=1)):
    store = _store(request)
    manifest = store.get(composition, revision)
    revisions = sorted(int(p.name) for p in store.folder(composition).iterdir() if p.is_dir() and p.name.isdigit())
    return {**manifest, "html": store.source(manifest), "revisions": revisions}


@router.post("/restore")
def restore(request: Request, body: HtmlMotionRestoreArgs):
    return _store(request).restore(**body.model_dump())


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
def render(request: Request, composition: str, revision: int | None = Query(None, ge=1)):
    return _store(request).queue_render(services(request), composition, revision)


@router.post('/{composition}/storyboard')
def storyboard(request: Request, composition: str, body: HtmlMotionStoryboardArgs):
    if body.composition != composition:
        raise NotFound('Storyboard composition does not match the request.')
    return _store(request).storyboard(composition, body.times, body.revision)


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
    if result["state"] != "done" or result["kind"] != "html_motion" or result["params"] != {"composition": composition, "revision": revision}:
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
