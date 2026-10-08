"""FastAPI application: HTTP surface, browser-attack guard, `/api/agent/*`
(exactly what the MCP tools call, compact and id-first), `/api/backend`,
and the richer UI endpoints. See `docs/API.md` for every route.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import FastAPI, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from . import mv_planner
from . import media_download
from . import animatic as animatic_mod
from . import audio as audio_mod
from . import charkit
from . import charpack
from . import clip_edit as clip_edit_mod
from . import interpolate as interpolate_mod
from . import comfy_driver, engine, procutil
from . import dubbing as dubbing_mod
from . import exporters, family_api, gpu_lease, jobevents
from . import productions as productions_mod
from . import cinema
from . import spaces as spaces_mod
from . import qa as qa_mod
from . import recipes as recipes_mod
from . import shorts as shorts_mod
from . import stock as stock_mod
from . import templates as design_templates
from . import trainers as trainers_mod
from . import timeline as timeline_mod
from . import voice_engines as ve
from . import voice_lab
from . import voice_pipelines as vp
from . import voices as voices_mod
from .backend import Backend, ffmpeg_path
from .design import DesignError
from .hoard_link import family
from .hoard_link.guard import install_guard
from .hoard_link.waiting import MAX_WAIT_S
from .hoard_link.errors import BackendError, HoardLinkError, Unavailable
from .family_settings import FamilySettings
from .ids import new_id
from .jobs import JobQueue
from .store import AssetInUse, NotFound, ProjectBusy, Store
from .voice_lab import VoiceLabError

logger = logging.getLogger("prosperos_hoard.api")

MAX_UPLOAD_MEDIA = engine.MAX_MEDIA_BYTES
MAX_UPLOAD_IMAGE = engine.MAX_IMAGE_BYTES


# ------------------------------------------------------------------ guard

# The request guard (Host, Origin and Fetch Metadata checks, HTTP and websockets) is `hoard_link.guard`, installed in create_app.


# ----------------------------------------------------------------- errors

def error_payload(exc: Exception) -> tuple[int, dict[str, str]]:
    if isinstance(exc, spaces_mod.SpaceError):
        return 400, {"error": exc.code, "message": exc.message}
    if isinstance(exc, ValueError) and str(exc).startswith("stale:"):
        return 409, {"error": "stale", "message": str(exc)}
    if isinstance(exc, AssetInUse):
        return 409, {"error": "asset_in_use", "message": str(exc)}
    if isinstance(exc, ProjectBusy):
        return 409, {"error": "project_busy", "message": str(exc)}
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
    if isinstance(exc, (VoiceLabError, vp.AudiobookError, dubbing_mod.DubbingError, ve.EngineNotInstalled)):
        code = exc.code if hasattr(exc, "code") else "engine_not_installed"
        return 400, {"error": code, "message": str(exc)}
    if isinstance(exc, (charkit.KitError, charpack.PackError, trainers_mod.TrainingError)):
        return 400, {"error": exc.code, "message": exc.message}
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
    comfy_dedicated: Optional[bool] = None  # the ComfyUI servers are Prospero's: an idle one may drop its models for the next job


class ServiceBody(BaseModel):
    id: str  # "comfyui" (the main one), "comfyui@8189", "render_pool", "ollama", "cmd:<id>"
    gpu: Optional[str] = None  # "auto" or a GPU index, ComfyUI only
    wait_s: float = 0.0


class LaunchBody(BaseModel):
    comfyui_dir: Optional[str] = None
    comfyui_python: Optional[str] = None
    comfyui_gpu: Optional[str] = None  # "auto" or a GPU index
    comfyui_args: Optional[list[str]] = None
    ollama_exe: Optional[str] = None
    autostart_comfy: Optional[bool] = None


def _gpu_arg(value: Optional[str]) -> Any:
    if value is None or str(value).strip() in ("", "auto"):
        return None if value is None else "auto"
    try:
        return int(str(value).strip())
    except ValueError:
        raise ValueError(f"gpu must be \"auto\" or a GPU index, got {value!r}") from None


class DeleteAssetsBody(BaseModel):
    ids: list[str]
    force: bool = False  # detach it from covers, canonical/reference images, logos and boards first


class TrashBody(BaseModel):
    action: str = "list"  # list | restore | empty
    ids: Optional[list[str]] = None
    project: Optional[str] = None
    projects: Optional[list[str]] = None  # trashed projects to restore / delete for good


class SpaceCreateBody(BaseModel):
    name: str = "Space"
    template: str = "blank"   # blank | reference_film | singing_shot | short_film


class SpaceSaveBody(BaseModel):
    graph: dict[str, Any]
    version: Optional[int] = None  # the version it was loaded at: a newer save in between is refused (409 stale)
    name: Optional[str] = None


class SpaceRunBody(BaseModel):
    mode: str = "node"            # node | downstream | all
    node_ids: list[str] = Field(default_factory=list)
    force: bool = False           # "all": run even the nodes whose inputs did not change


class SpaceAppRunBody(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)  # {input node id: text | asset id(s) | character id}


class SpaceBuildBody(BaseModel):
    request: str


class SpaceNodeStateBody(BaseModel):
    excluded: Optional[list[str]] = None   # outputs unticked so they do not flow downstream
    outputs: Optional[list[str]] = None    # pick an earlier run's outputs back


class SpaceAgentBody(BaseModel):
    action: str = "list"          # list | get | create | edit | run | stop | delete | restore | estimate | build | app | app_run
    space: Optional[str] = None
    name: Optional[str] = None
    template: str = "blank"
    ops: list[dict[str, Any]] = Field(default_factory=list)  # edit: add_node / set / connect / disconnect / remove / move
    request: Optional[str] = None  # build: what to make, in words (the local model draws the graph)
    values: dict[str, Any] = Field(default_factory=dict)  # app_run: {input node id: text | asset id(s) | character id}
    group: Optional[str] = None    # export: only this group of the space
    bundle: Optional[dict[str, Any]] = None  # import: a technique from action=export
    mode: str = "node"
    node_ids: list[str] = Field(default_factory=list)
    force: bool = False


class SpaceImportBody(BaseModel):
    bundle: dict[str, Any]          # a technique (GET /api/spaces/{id}/export)
    name: Optional[str] = None
    space: Optional[str] = None     # add it to this space instead of making a new one


class EnhancePromptBody(BaseModel):
    text: str
    kind: str = "image"   # image | video | music
    project: Optional[str] = None  # its cast's looks keep the @Names as designed


class CanvasBody(BaseModel):
    seconds: float = 8.0               # 3-8 s
    start_s: Optional[float] = None    # default: the first chorus
    lyrics: bool = False               # keep the burned-in lyric captions


class ProductionFinishingBody(BaseModel):
    finishing: dict[str, Any] = Field(default_factory=dict)  # see video.validate_finishing; {} = no look
    render: bool = True  # queue the run that re-renders the cut when one was already rendered


class ProductionSettingsBody(BaseModel):
    autopilot: Optional[bool] = None            # no pauses: the first take, no animatic review
    animatic: Optional[bool] = None
    animatic_autocontinue: Optional[bool] = None
    song_review: Optional[bool] = None
    qa: Optional[dict[str, Any]] = None
    clip_quality: Optional[str] = None          # draft (fast 5B clips) | final


class ExportTimelineBody(BaseModel):
    timeline_id: Optional[str] = None
    production: Optional[str] = None
    aspect: Optional[str] = None


class DeleteProjectBody(BaseModel):
    project: str


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
    action: str = "list"     # list | create | update | delete | restore | deleted
    kind: str = "character"  # "character" | "location" | "prop" | "group"
    id: Optional[str] = None
    name: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)
    force: bool = False      # delete: even when a production still to run uses it as its lead


class CharacterBody(BaseModel):
    name: Optional[str] = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ComposePromptBody(BaseModel):
    prompt: str
    negative: Optional[str] = None
    style: Optional[str] = None
    engine: Optional[str] = None   # the engine the render will use: on "qwen21" places/props add their images
    references: int = 0            # how many reference images the call brings (they number first)


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
    # the model file for the chosen engine: a Qwen-Image diffusion model
    # (UNETLoader) for qwen21, else a checkpoint; see /api/image-engines
    model: Optional[str] = None
    # motion transfer (template wan_animate2): the video whose motion the
    # reference's character performs, from driving_start_s seconds in
    driving_asset_id: Optional[str] = None
    driving_start_s: Optional[float] = None
    template_params: Optional[dict[str, Any]] = None  # the template's own knobs (see its params.json map)
    engine: Optional[str] = None  # "auto" | "qwen21" | "flux" | "sdxl" - defaults to the project's
    use_character_reference: bool = False
    # mentioned locations/props with a reference image go in as numbered
    # references (an edit) on engines that read several (Qwen-Image 2.1)
    use_element_references: bool = True
    # lip sync (template wan22_s2v): the stretch of a song the character
    # sings - audio_seconds from audio_start_s
    audio_asset_id: Optional[str] = None
    audio_start_s: Optional[float] = None
    audio_seconds: Optional[float] = None
    # first and last frame (template wan22_flf2v): the image the clip ends on
    end_asset_id: Optional[str] = None
    end_optional: bool = False                  # drop the end frame (not fail) when that model is missing
    # film language (cinema.py): {shot, angle, move, lens, light, composition} ids whose terms join the prompt
    camera: Optional[dict[str, str]] = None
    consistent: bool = False
    # character adapters (LoRAs): injected for every @mentioned character
    # (and every id in `characters`) that has one for the render's
    # architecture, unless use_adapters=false
    characters: Optional[list[str]] = None
    use_adapters: bool = True
    # consistent=true + every mentioned character has an adapter: render
    # txt2img with the adapter instead of an edit of the canonical image
    # (free poses and framing, no drift toward the reference's pose)
    prefer_adapter: bool = False
    wait_s: float = 0


class CharPackBody(BaseModel):
    action: str = "export"  # export | import | inspect
    character_id: Optional[str] = None
    path: Optional[str] = None  # import/inspect: a .hoardchar on disk (inside the import roots)
    rename: Optional[str] = None
    include_dataset: bool = True
    include_adapters: bool = True


class CharLibraryBody(BaseModel):
    action: str = "list"  # list | save | use | history | delete
    character_id: Optional[str] = None
    id: Optional[str] = None
    version: Optional[int] = None
    note: Optional[str] = None
    query: Optional[str] = None
    rename: Optional[str] = None
    include_adapters: bool = True


class CharSheetBody(BaseModel):
    character_id: str = ""  # the UI routes take it from the path
    views: Optional[list[str]] = None
    engine: Optional[str] = None
    seed: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    wait_s: float = 0


class CharDatasetBody(BaseModel):
    character_id: str = ""  # the UI routes take it from the path
    action: str = "get"  # get | build | update | caption | report
    sources: Optional[list[str]] = None
    min_identity: Optional[float] = None
    replace: bool = False
    items: Optional[list[dict[str, Any]]] = None
    only_missing: bool = False
    wait_s: float = 0


class CharTrainBody(BaseModel):
    character_id: Optional[str] = None
    action: str = "plan"  # plan | start | status | log | trainers | settings
    arch: Optional[str] = None
    trainer: Optional[str] = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    run_id: Optional[str] = None
    job_id: Optional[str] = None
    training: Optional[dict[str, Any]] = None
    wait_s: float = 0


class CharAdaptersBody(BaseModel):
    character_id: str = ""  # the UI routes take it from the path
    action: str = "list"  # list | attach | update | remove | settings | available
    adapter_id: Optional[str] = None
    lora_name: Optional[str] = None
    arch: Optional[str] = None
    strength: Optional[float] = None
    trigger: Optional[str] = None
    enabled: Optional[bool] = None
    settings: dict[str, Any] = Field(default_factory=dict)


class CharTakesBody(BaseModel):
    character_id: str = ""  # the UI routes take it from the path
    action: str = "list"  # list | score | act
    sort: str = "recent"
    kind: Optional[str] = None
    limit: int = 40
    asset_ids: Optional[list[str]] = None
    asset_id: Optional[str] = None
    take_action: Optional[str] = None
    force: bool = False
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
    name: Optional[str] = None                  # the song's name (default: its sound tags)
    wait_s: float = 0


class OutpaintBody(BaseModel):
    asset_id: str
    width: int = Field(..., ge=64, le=4096, multiple_of=8)
    height: int = Field(..., ge=64, le=4096, multiple_of=8)
    anchor_x: float = Field(0.5, ge=0, le=1, allow_inf_nan=False)
    anchor_y: float = Field(0.5, ge=0, le=1, allow_inf_nan=False)
    prompt: str = Field(..., min_length=1, max_length=4000)
    seed: Optional[int] = None
    wait_s: float = Field(0, ge=0, le=60)


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
    scale: Optional[int] = None
    model: Optional[str] = None
    wait_s: float = 0


class AnimateBody(BaseModel):
    asset_id: str
    project_id: Optional[str] = None  # save in the active project when reusing another project's reference
    # "auto": with a driving video, Wan Animate 2 (the motion of that video);
    # else Wan 2.2 14B (real motion and camera moves) when installed, else
    # the 5B; "wan14b" | "wan" (5B) | "animate" | "svd" force one
    engine: str = "auto"
    prompt: Optional[str] = None  # Wan: what moves (camera and subject); Animate: the background
    driving_asset_id: Optional[str] = None  # a video whose motion the image's character performs
    driving_start_s: float = 0
    pose_prompt: Optional[str] = None  # Animate: the motion in words ("a person dancing")
    seconds: Optional[float] = None  # 14B: clip length (default 5)
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


class VideoPlanBody(BaseModel):
    concept: str
    character_id: Optional[str] = None  # the lead from the cast (its canonical image keeps the look)
    lead_name: Optional[str] = None     # ... or a new lead: name + look
    lead_look: Optional[str] = None
    shots: int = 10
    language: str = "en"               # the lyrics' language
    song_asset_id: Optional[str] = None  # an existing song; otherwise it is composed
    lyrics: Optional[str] = None       # the existing song's lyrics, for the shot plan
    genre: Optional[str] = None
    duration_s: float = 120
    project: Optional[str] = None  # its places and objects are offered to the planner as @Name
    critic: bool = True            # a second pass that reviews the shot list and rewrites the weak shots
    notes_language: Optional[str] = None  # the language of the critic's notes (the app's language; default English)


class VideoCritiqueBody(BaseModel):
    concept: str
    draft: dict[str, Any]                # the plan as the planner wrote it (or as the person edited it)
    character_id: Optional[str] = None
    lead_name: Optional[str] = None
    lead_look: Optional[str] = None
    notes_language: Optional[str] = None


class VideoFromPlanBody(BaseModel):
    name: str
    draft: dict[str, Any]
    character_id: Optional[str] = None
    lead_name: Optional[str] = None
    lead_look: Optional[str] = None
    song_asset_id: Optional[str] = None
    clips: str = "all"          # all | lead | none
    aspects: Optional[list[str]] = None
    song_takes: int = 2
    engine: str = "auto"
    project: Optional[str] = None
    brief: Optional[str] = None
    settings: Optional[dict[str, Any]] = None
    lyrics: Optional[str] = None  # an existing song's lyrics: timed, they tie the shots to their sections


class ShortCreateBody(BaseModel):
    name: Optional[str] = None
    topic: Optional[str] = None
    script: Optional[Any] = None
    options: dict[str, Any] = Field(default_factory=dict)
    settings: Optional[dict[str, Any]] = None
    count: int = 1
    project: Optional[str] = None


class ShortScriptBody(BaseModel):
    production: Optional[str] = None
    script: Optional[Any] = None
    run: bool = True


class StockSearchBody(BaseModel):
    query: str
    kind: str = "video"
    aspect: Optional[str] = None
    orientation: Optional[str] = None
    providers: Optional[list[str]] = None
    per_page: int = 12
    page: int = 1
    min_duration_s: float = 0
    project: Optional[str] = None
    take: int = 0
    refs: Optional[list[str]] = None


class StockKeysBody(BaseModel):
    pexels: Optional[str] = None
    pixabay: Optional[str] = None


class ProductionSegmentsBody(BaseModel):
    text: Optional[str] = None
    segments: Optional[list[dict[str, Any]]] = None


class ProductionLyricsBody(BaseModel):
    lyrics: str
    run: bool = False


class ProductionSongBody(BaseModel):
    asset_id: Optional[str] = None          # an audio asset of any project (library or uploaded)
    take: Optional[int] = None              # another of the takes already composed (1-based)
    compose: Optional[dict[str, Any]] = None  # {tags, bpm, duration, key, language, lyrics, count}
    lyrics: Optional[str] = None
    time_lyrics: bool = True                # time the lyrics to the new song right away
    run: bool = False


class ProductionCastBody(BaseModel):
    cast: list[dict[str, Any]] = Field(default_factory=list)  # [{asset_id, name, note}]
    per_shot: Optional[int] = None  # how many stand behind each crowd shot (1-6)
    run: bool = False


class DownloadMediaBody(BaseModel):
    url: str
    audio_only: bool = False
    start_s: Optional[float] = None
    end_s: Optional[float] = None


class VideoFramesBody(BaseModel):
    asset_id: str
    count: int = 6
    at_s: Optional[float] = None                # one frame at this time (0 = the first) instead of `count` spread ones


class ProductionShotsBody(BaseModel):
    changes: list[dict[str, Any]]
    run: bool = True


class ReframeBody(BaseModel):
    asset_id: str
    aspect: str = "9:16"                        # 9:16, 16:9, 1:1, 4:5, 2:3, 3:2, 21:9
    framing: str = "fill"                       # fill (crop around the subject) | blur | fit
    focus_x: Optional[float] = None             # 0-1, where the subject is (found on its own when missing)
    focus_y: Optional[float] = None
    quality: str = "final"                      # final | preview (720p, faster)
    project: Optional[str] = None
    wait_s: float = 0


class InterpolateBody(BaseModel):
    asset_id: str
    fps: float = 48
    wait_s: float = 0


class RetakeBody(BaseModel):
    asset_id: str
    start_s: float                              # the stretch to redo, in the clip's own seconds
    end_s: float
    prompt: Optional[str] = None                # what happens in it (default: the scene continues naturally)
    quality: str = "draft"                      # draft (Wan 2.1 VACE 1.3B, fast) | final (Wan 2.2 Fun VACE 14B)
    seed: Optional[int] = None
    negative: Optional[str] = None
    wait_s: float = 0


class ClipEditBody(BaseModel):
    asset_id: str
    prompt: str = ""                            # the instruction ("make it night, wet streets"); @Name for cast members
    mode: str = "auto"                          # auto | edit | restyle | reference | propagate
    quality: str = "draft"                      # draft (Bernini-R 1.3B) | final (Bernini-R Wan 2.2 A14B, 6 steps)
    reference_asset_ids: list[str] = []         # pictures the instruction calls image0, image1... (after the first frame)
    first_frame_asset_id: Optional[str] = None  # propagate: the window's first frame, edited as a picture
    start_s: float = 0.0                        # a clip longer than 5 s is edited 5 s at a time from here
    enhance: bool = True                        # rewrite the instruction in the detailed shape the model was trained on
    exact: bool = False                         # send `prompt` as it is (already rewritten, e.g. reviewed in the app)
    seed: Optional[int] = None
    negative: Optional[str] = None
    wait_s: float = 0


class StemsBody(BaseModel):
    asset_id: str
    force: bool = False                         # split again even if the stems exist
    device: Optional[str] = None                # "cpu" or "cuda:N" (default: the card with most free memory, else cpu)
    wait_s: float = 0


class ProductionReframeBody(BaseModel):
    aspects: list[str]
    framing: Optional[str] = None               # fill | blur | fit (for clips of another shape)
    run: bool = True


class ProductionRegenerateBody(BaseModel):
    stage: str = "clips"                        # frames (stills and their clips) | clips
    keys: Optional[list[str]] = None            # only these shots (locked ones are still kept)
    run: bool = True


class ProductionPromoteBody(BaseModel):
    keys: Optional[list[str]] = None            # only the drafts of these shots
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


class RecipeRunBody(BaseModel):
    recipe: Optional[str] = None
    cast: dict[str, Any]
    name: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)


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
    # the family: events for the hub, notices when a production needs the person, the hub's GPU lease (family_api.install below)
    family_settings = FamilySettings(data_dir)
    job_events = jobevents.JobEvents(store, family.emit, base_url=lambda: f"http://127.0.0.1:{port}",
                                     notifier=jobevents.Notifier(store, family_settings))
    queue = JobQueue(store, gpu_targets=[None, *backend.render_pool()], bind=backend.bind_comfy,
                     target_ready=backend.pool_server_ready, accepts=backend.accepts_job, events=job_events,
                     gpu_guard=None if demo else (lambda job: gpu_lease.hold(
                         job, backend.vram_estimates_mb, on_gpu=job_events.set_gpu,
                         resolve_image_engine=lambda: engine.resolve_image_engine(engine._object_info(backend), "auto"))))
    queue.register("generate_image", lambda job, p: engine.generate_image(store, backend, job, p))
    queue.register("edit_image", lambda job, p: engine.edit_image(store, backend, job, p))
    queue.register("animate", lambda job, p: engine.animate_image(store, backend, job, p))
    queue.register("compose_song", lambda job, p: engine.compose_song(store, backend, job, p))
    queue.register("render_timeline", lambda job, p: engine.render_timeline_job(store, backend, job, p))
    queue.register("download_voice", lambda job, p: _download_voice_job(store, job, p))
    queue.register("audiobook", lambda job, p: vp.audiobook_job(store, backend, job, p))
    queue.register("dub", lambda job, p: dubbing_mod.dub_job(store, backend, job, p))
    queue.register("install_voice_engine", lambda job, p: _install_voice_engine_job(job, p))
    queue.register("download_media", lambda job, p: media_download.download_job(store, job, p))
    queue.register("reframe", lambda job, p: engine.reframe_job(store, job, p))
    queue.register("interpolate", lambda job, p: interpolate_mod.run(store, job, p))
    queue.register("retake", lambda job, p: engine.retake_job(store, backend, job, p))
    queue.register("clip_edit", lambda job, p: engine.clip_edit_job(store, backend, job, p))

    def _stems_job(job: dict[str, Any], progress) -> dict[str, Any]:
        from . import stems as stems_mod
        p = job["params"]
        try:
            out = stems_mod.separate(store, p["asset_id"], backend.launcher.comfy_install()[1], progress,
                                     device=p.get("device"), force=bool(p.get("force")))
        except stems_mod.StemsError as exc:
            raise engine.EngineError(exc.code, exc.message) from None
        return {**out, "asset_ids": list(out["stems"].values())}

    queue.register("stems", _stems_job)
    # production/QA handlers are registered below, next to the operations
    # they queue sub-jobs through; the workers start at the end of create_app

    app = FastAPI(title="Prospero's Hoard", version=__version__)
    # strict_ports keeps the old rule: a Host that names a port must be this app's; PROSPERO_ALLOWED_HOSTS adds LAN / tailnet names.
    install_guard(app, port_getter=lambda: port, allowed_env="PROSPERO_ALLOWED_HOSTS", strict_ports=True)
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
        proj = store.get_project(project)
        if body.camera:
            is_clip = any(k in (body.template or "") for k in ("wan", "svd", "clip", "animate", "s2v"))
            terms = cinema.camera_prompt(body.camera, video=is_clip)
            if terms:
                body = body.model_copy(update={"prompt": f"{body.prompt.rstrip(' .,')}, {terms}", "camera": None})
        object_info = engine._object_info(backend, autostart=True)  # queuing a render: start ComfyUI if it is off
        engine_name = engine.resolve_image_engine(object_info, body.engine or proj.get("image_engine"))
        if body.template == "auto_clip":
            # a production's clip: the best video model installed (motion
            # transfer when the call brings a driving video)
            if body.driving_asset_id and not engine.animate2_installed(object_info or {}):
                raise engine.EngineError("animate_missing", "motion transfer needs Wan Animate 2; install it or disconnect the motion guide")
            body = body.model_copy(update={"template": engine.clip_template(object_info or {}, bool(body.driving_asset_id))})
            if body.template != "wan_animate2":
                params = {k: v for k, v in (body.template_params or {}).items() if k in ("seconds", "length", "lightning")}
                if body.template == "wan22_ti2v" and "seconds" in params:
                    params["length"] = int(round(float(params.pop("seconds")) * 24)) + 1
                body = body.model_copy(update={"driving_asset_id": None, "driving_start_s": None,
                                               "template_params": params or None})
        if body.template == "auto_sing":
            # lip sync: InfiniteTalk for long lines when installed, else S2V
            body = body.model_copy(update={"template": engine.sing_template(object_info or {}, float(body.audio_seconds or 0),
                                                                            (body.template_params or {}).get("sing_engine")),
                                           "template_params": None})
        if body.end_asset_id and body.template in ("auto_clip", "wan22_i2v_14b", "wan22_ti2v"):
            if engine.flf_installed(object_info or {}):
                body = body.model_copy(update={"template": "wan22_flf2v", "driving_asset_id": None, "driving_start_s": None})
            elif body.end_optional:
                body = body.model_copy(update={"end_asset_id": None})
            else:
                raise engine.EngineError("no_flf", "a clip that ends on a given frame needs Wan 2.2 14B image-to-video "
                                                   "(both experts and the 4-step LoRAs) in ComfyUI")
        extra_refs = [r for r in (body.reference_asset_ids or []) if r]
        available_loras = comfy_driver.lora_choices(object_info) if object_info else None
        adapter_route = False
        kontext: Optional[dict[str, Any]] = None
        if body.consistent:
            kontext = engine.build_kontext_instruction(store, project, body.prompt, engine=engine_name,
                                                       extra_count=len(extra_refs))
            if not (body.reference_asset_id or kontext["reference_asset_id"]) and kontext["reference_asset_ids"]:
                # only places/objects have images: no character to keep,
                # so it is an ordinary render that reads their references
                kontext = None
        if kontext is not None and body.prefer_adapter and body.use_adapters and not body.template \
                and not kontext["reference_asset_ids"]:
            txt_template = engine.ENGINE_TEMPLATES[engine_name]["txt2img"]
            names = kontext["matched_characters"]
            if names:
                probe = charkit.resolve_adapters(store, project, names, [], comfy_driver.template_arch(txt_template),
                                                 available_loras)
                adapter_route = len(probe["used"]) == len(names)
        if kontext is not None and not adapter_route:
            # "Cast -> Reference sheet": route through an edit template with
            # the canonical reference as input (image_1, for Qwen) and the
            # scene as the instruction, instead of a fresh txt2img; other
            # mentioned characters, places and props with an image follow
            # the caller's own references, numbered in the instruction.
            primary = body.reference_asset_id or kontext["reference_asset_id"]
            if not primary:
                raise engine.EngineError(
                    "consistent_needs_reference",
                    "consistent=true needs a canonical reference: mention a cast member with a canonical "
                    "reference image (studio_cast update canonical_asset_id), or pass reference_asset_id",
                )
            all_refs = [primary] + [r for r in extra_refs if r != primary]
            all_refs += [r for r in kontext["reference_asset_ids"] if r not in all_refs][:max(0, 10 - len(all_refs))]
            composed = {"positive_prompt": kontext["instruction"],
                        "negative_prompt": ", ".join(x for x in (body.negative, kontext.get("negative_extra")) if x),
                        "style": None,
                       "style_defaults": {}, "matched_characters": kontext["matched_characters"],
                       "matched_elements": kontext["matched_elements"],
                       "unknown_mentions": kontext["unknown_mentions"], "reference_asset_id": primary}
            template = body.template or engine.ENGINE_TEMPLATES[engine_name]["edit"]
        else:
            composed = engine.compose_prompt(store, project, body.prompt, body.negative, body.style)
            reference = body.reference_asset_id
            if not reference and body.use_character_reference:
                reference = composed["reference_asset_id"]
            all_refs = extra_refs or ([reference] if reference else [])
            added = engine.add_element_references(composed, all_refs, engine_name, body.template,
                                                  body.use_element_references)
            all_refs = all_refs + [r["asset_id"] for r in added]
            template = body.template or engine.ENGINE_TEMPLATES[engine_name]["edit" if all_refs else "txt2img"]
        reference = all_refs[0] if all_refs else None
        strength = body.strength
        if template in ("qwen21_edit", "flux_kontext_edit"):
            # these edits read the references through the text encoder and
            # sample a fresh latent: a partial denoise only turns the empty
            # canvas into noise, so an img2img strength never applies here
            strength = None
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
        adapters = {"loras": [], "triggers": [], "used": [], "notes": []}
        if body.use_adapters:
            try:
                _, tspec = comfy_driver.load_template(template, store.data_dir)
            except comfy_driver.WorkflowError:
                tspec = {}
            adapters = charkit.resolve_adapters(store, project, composed["matched_characters"], body.characters or [],
                                                comfy_driver.template_arch(template, tspec), available_loras)
            if adapters["triggers"]:
                composed["positive_prompt"] = charkit.with_triggers(composed["positive_prompt"], adapters["triggers"])
        seed = body.seed if body.seed is not None else engine.random_seed()
        params = {
            "prompt": body.prompt, "positive_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
            "width": width, "height": height, "steps": body.steps, "cfg": body.cfg, "sampler": body.sampler,
            "scheduler": body.scheduler, "seed": seed, "count": body.count, "reference_asset_id": reference,
            "reference_asset_ids": all_refs or None,
            "strength": strength, "template": template,
            "checkpoint": body.checkpoint or (body.model if engine.TEMPLATE_TO_ENGINE.get(template) != "qwen21" else None),
            **({"unet_name": body.model} if body.model and engine.TEMPLATE_TO_ENGINE.get(template) == "qwen21" else {}),
            "style": composed["style"], "style_defaults": composed["style_defaults"],
            "matched_characters": composed["matched_characters"],
            **({"matched_elements": composed["matched_elements"]} if composed.get("matched_elements") else {}),
            **({"loras": adapters["loras"]} if adapters["loras"] else {}),
        }
        if body.audio_asset_id:
            if store.get_asset(body.audio_asset_id)["kind"] not in ("audio", "video"):
                raise engine.EngineError("audio_not_audio", "audio_asset_id must be a song or a sound")
            longest = 90 if template == "wan21_infinitetalk" else 20
            if body.audio_seconds is not None and not 0.5 <= float(body.audio_seconds) <= longest:
                raise engine.EngineError("bad_parameter", f"audio_seconds must be between 0.5 and {longest} for {template}")
            params["audio_asset_id"] = body.audio_asset_id
            params["audio_start_s"] = max(0.0, float(body.audio_start_s or 0))
            if body.audio_seconds:
                params["audio_seconds"] = float(body.audio_seconds)
        if body.end_asset_id:
            if store.get_asset(body.end_asset_id)["kind"] != "image":
                raise engine.EngineError("end_not_image", "end_asset_id must be an image (the frame the clip ends on)")
            params["end_asset_id"] = body.end_asset_id
        if body.driving_asset_id:
            if store.get_asset(body.driving_asset_id)["kind"] != "video":
                raise engine.EngineError("driving_not_video", "driving_asset_id must be a video (the motion to copy)")
            params["driving_asset_id"] = body.driving_asset_id
            params["driving_start_s"] = max(0.0, float(body.driving_start_s or 0))
        if body.template_params:
            # a template's own knobs (pose_prompt, seconds, lightning...): only
            # the ones its map names, never the core keys set above
            _, tspec = comfy_driver.load_template(template, store.data_dir)
            allowed = set(tspec.get("map") or {}) - {"positive_prompt", "negative_prompt", "seed", "width", "height"}
            unknown = sorted(set(body.template_params) - allowed)
            if unknown:
                raise engine.EngineError("bad_parameter", f"template '{template}' has no {', '.join(unknown)} "
                                                          f"(it takes {', '.join(sorted(allowed)) or 'nothing extra'})")
            params.update(body.template_params)
        job = queue.enqueue("generate_image", "gpu", params, project_id=project)
        job = wait(job, body.wait_s)
        return {"job": job, "final_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
                "matched_characters": composed["matched_characters"], "unknown_mentions": composed["unknown_mentions"],
                **({"matched_elements": composed["matched_elements"]} if composed.get("matched_elements") else {}),
                "template": template, "engine": engine_name, "seed": seed,
                **({"adapters": adapters["used"]} if adapters["used"] else {}),
                **({"adapter_notes": adapters["notes"]} if adapters["notes"] else {}),
                **({"route": "adapter"} if adapter_route else {})}

    def op_edit(body: EditImageBody) -> dict[str, Any]:
        asset = store.get_asset(body.asset_id)
        if asset["kind"] != "image":
            raise engine.EngineError("not_an_image", f"asset {body.asset_id} is {asset['kind']}; edits need an image")
        ops = ("img2img", "inpaint", "hires", "vary", "reuse", "upscale", "remove_background", "pose_map", "depth_map")
        if body.operation not in ops:
            raise engine.EngineError("bad_operation", f"operation must be one of {', '.join(ops)}")
        if body.operation == "inpaint" and not body.mask_asset_id:
            raise engine.EngineError("mask_required", "inpaint needs mask_asset_id (white = repaint)")
        if body.operation == "hires" and (asset.get("recipe") or {}).get("template") != "sdxl_txt2img":
            raise engine.EngineError("hires_needs_recipe", "upscale re-runs an SDXL txt2img recipe at a higher resolution; "
                                                           f"asset {asset['id']} was not made that way (use img2img instead)")
        if body.operation in ("vary", "reuse") and (asset.get("recipe") or {}).get("backend") != "comfyui":
            raise engine.EngineError("not_reproducible", f"asset {asset['id']} was not generated on ComfyUI, so it has no recipe to re-run; use img2img")
        if body.operation == "upscale":
            engine.check_upscale_request(store, asset, body.scale)
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

    def _wan_installed() -> bool:
        try:
            info = engine.object_info_live_or_cached(backend)
        except Exception:  # noqa: BLE001 - nothing known yet: let the render say what is missing
            return True
        return "Wan22ImageToVideoLatent" in info and engine._has_model_file(info, "UNETLoader", "unet_name", "wan")

    def op_animate(body: AnimateBody) -> dict[str, Any]:
        asset = store.get_asset(body.asset_id)
        project_id = body.project_id or asset["project_id"]
        store.get_project(project_id)
        if asset["kind"] != "image":
            raise engine.EngineError("not_an_image", f"asset {body.asset_id} is {asset['kind']}; animate needs an image")
        if body.engine not in ("auto", "wan", "wan14b", "animate", "svd"):
            raise ValueError("engine is auto, wan14b, wan, animate or svd")
        try:
            info = engine.object_info_live_or_cached(backend)
        except Exception:  # noqa: BLE001
            info = {}
        if body.engine == "animate" or (body.engine == "auto" and body.driving_asset_id):
            if not body.driving_asset_id:
                raise engine.EngineError("driving_video_required", "animate copies the motion of a video: pass driving_asset_id")
            if info and not engine.animate2_installed(info):
                raise engine.EngineError("animate_missing", "Wan Animate 2 is not installed in ComfyUI "
                                                            "(wan_animate_2_distill_int8_convrot.safetensors + clip_vision_h)")
            prompt = (body.prompt or "").strip()
            if not prompt.lower().startswith("character appearance"):
                prompt = ("Character appearance description: the character in the reference image, same design and colours. "
                          f"Background description: {prompt or 'the same place as the reference image'}.")
            res = op_generate(project_id, GenerateImageBody(
                prompt=prompt, template="wan_animate2", reference_asset_id=asset["id"], seed=body.seed, count=1,
                driving_asset_id=body.driving_asset_id, driving_start_s=body.driving_start_s,
                template_params={"pose_prompt": body.pose_prompt or "a person dancing",
                                 # Animate runs at 24 fps: N seconds = 24N+1 frames (at most ~5 s)
                                 **({"length": min(121, int(round(body.seconds * 24)) + 1)} if body.seconds else {})},
                wait_s=body.wait_s))
            return {"job": res["job"], "engine": "animate"}
        if body.engine == "wan14b" or (body.engine == "auto" and info and engine.wan14b_installed(info)):
            res = op_generate(project_id, GenerateImageBody(
                prompt=(body.prompt or "natural motion, the camera slowly orbits around the subject").strip(),
                template="wan22_i2v_14b", reference_asset_id=asset["id"], seed=body.seed, count=1,
                template_params={"seconds": body.seconds} if body.seconds else None, wait_s=body.wait_s))
            return {"job": res["job"], "engine": "wan14b"}
        if body.engine == "wan" or (body.engine == "auto" and _wan_installed()):
            res = op_generate(project_id, GenerateImageBody(
                prompt=(body.prompt or "subtle natural motion, gentle camera push-in").strip(), template="wan22_ti2v",
                reference_asset_id=asset["id"], seed=body.seed, count=1,
                template_params={"length": int(round(body.seconds * 24)) + 1} if body.seconds else None,
                wait_s=body.wait_s))
            return {"job": res["job"], "engine": "wan"}
        job = queue.enqueue("animate", "gpu", body.model_dump(exclude={"wait_s", "engine", "prompt", "project_id"}), project_id=project_id)
        return {"job": wait(job, body.wait_s), "engine": "svd"}

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
        if body.kind not in ("character", "location", "prop", "group"):
            raise engine.EngineError("bad_kind", "kind must be 'character', 'location', 'prop' or 'group'")
        if body.kind in ("location", "prop"):
            # places and objects are cast entries too (@mention, look,
            # reference image): a character row with its element set
            body = body.model_copy(update={"kind": "character",
                                           "fields": {**body.fields, **({"element": body.kind} if body.action == "create" else {})}})
        if body.action == "list":
            return {"characters": store.list_characters(project), "groups": store.list_groups(project)}
        if body.action == "deleted":
            return {"characters": store.list_characters(project, deleted=True), "groups": []}
        if body.action in ("delete", "restore"):
            if not body.id:
                raise engine.EngineError("id_required", f"{body.action} needs the {body.kind}'s id")
            target = store.get_group(body.id) if body.kind == "group" else store.get_character(body.id)
            if target["project_id"] != project:
                raise engine.EngineError("wrong_project", f"{body.kind} {body.id} belongs to another project")
            if body.kind == "group":
                if body.action == "restore":
                    raise engine.EngineError("bad_action", "a deleted group cannot be restored; make it again")
                store.delete_group(body.id)
                return {"deleted": body.id, "name": target["name"]}
            if body.action == "restore":
                return store.restore_character(body.id)
            users = productions_mod.productions_led_by(store.data_dir, body.id)
            if users and not body.force:
                raise engine.EngineError("in_use", f"{target['name']} is the lead of {', '.join(users)}, which has not "
                                                   "finished. Deleting it anyway is safe: the production keeps its look.")
            store.delete_character(body.id)
            return {"deleted": body.id, "name": target["name"], "restore": "studio_cast action=restore"}
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
        raise engine.EngineError("bad_action", f"unknown cast action '{body.action}'; use list, create, update, delete, "
                                               "restore or deleted")

    # ---------------------------------------------------------------- health
    @app.get("/api/health")
    def health():
        active = store.list_jobs(state="active", limit=50)["items"]
        return {
            "service": "prosperos-hoard", "name": "Prospero's Hoard", "version": __version__, "status": "ok",
            "demo": demo, "projects": len(store.list_projects(limit=50)["items"]),
            "active_jobs": len(active), "hoard_link": family.health_block(),
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
                              body.render_pool, body.comfy_dedicated)
        return backend.status()

    @app.post("/api/backend/comfy/free")
    def free_comfy():
        return backend.free_comfy_memory()

    @app.get("/api/image-engines")
    def image_engines(project: Optional[str] = None):
        """What the Generate screen offers: which engines are installed and
        what "auto" resolves to (from the live or last known node list; never
        starts ComfyUI), plus the project's own default."""
        live = True
        try:
            info = engine._object_info(backend)
        except Exception:  # noqa: BLE001 - ComfyUI off: the last known state
            live = False
            try:
                info = engine.object_info_live_or_cached(backend)
            except Exception:  # noqa: BLE001
                info = {}
        installed = {name: engine.resolve_image_engine(info, name) == name for name in ("qwen21", "flux", "sdxl")} \
            if info else {"qwen21": False, "flux": False, "sdxl": True}
        default = "auto"
        if project:
            default = (store.get_project(project).get("image_engine") or "auto")
        def files(cls: str, field: str) -> list[str]:
            try:
                entry = info[cls]["input"]["required"][field][0]
                return [str(x) for x in entry] if isinstance(entry, list) else []
            except (KeyError, IndexError, TypeError):
                return []
        ckpts = files("CheckpointLoaderSimple", "ckpt_name")
        not_image = ("svd", "ace_step", "ace-step", "wan", "ltx", "stable_audio")
        models = {
            "qwen21": [u for u in files("UNETLoader", "unet_name") if "qwen" in u.lower() and "edit" not in u.lower()],
            "flux": [c for c in ckpts if "flux" in c.lower()],
            "sdxl": [c for c in ckpts if "flux" not in c.lower() and not any(k in c.lower() for k in not_image)
                     and "v1-5" not in c.lower() and "sd15" not in c.lower() and "sd_1" not in c.lower()],
        }
        est = backend.vram_estimates_mb()
        return {"engines": list(engine.IMAGE_ENGINES), "auto_resolves_to": engine.resolve_image_engine(info or {}, "auto"),
                "installed": installed, "project_default": default, "live": live,
                "templates": engine.ENGINE_TEMPLATES, "models": models,
                "vram_mb": {"qwen21": est.get("qwen21"), "flux": est.get("flux"), "sdxl": est.get("sdxl"),
                            "sd15": est.get("sd15"), "kontext": est.get("kontext")}}

    @app.get("/api/backend/memory")
    def get_memory():
        return backend.memory()

    @app.get("/api/backend/services")
    def get_services():
        return backend.services()

    @app.post("/api/backend/services/start")
    def start_service(body: ServiceBody):
        return backend.start_service(body.id, gpu=_gpu_arg(body.gpu), wait_s=min(max(body.wait_s, 0.0), MAX_WAIT_S))

    @app.post("/api/backend/services/stop")
    def stop_service(body: ServiceBody):
        return backend.stop_service(body.id)

    @app.put("/api/backend/launch")
    def set_launch(body: LaunchBody):
        gpu = body.comfyui_gpu
        if gpu is not None and gpu.strip() not in ("", "auto"):
            gpu = _gpu_arg(gpu)
        return backend.set_launch(body.comfyui_dir, body.comfyui_python, gpu, body.comfyui_args, body.ollama_exe,
                                  body.autostart_comfy)

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

    def _with_auto_cover(p: dict[str, Any]) -> dict[str, Any]:
        """A project without a chosen cover shows its best picture: the
        newest favourite, else the newest image (`auto_cover_asset_id`)."""
        if p.get("cover_asset_id"):
            return p
        try:
            pick = (store.list_assets(p["id"], kind="image", favourite=True, limit=1)["items"]
                    or store.list_assets(p["id"], kind="image", limit=1)["items"])
        except Exception:  # noqa: BLE001 - no cover is fine
            pick = []
        return {**p, "auto_cover_asset_id": pick[0]["id"] if pick else None}

    @app.get("/api/projects")
    def list_projects(query: Optional[str] = None, limit: int = 50, offset: int = 0):
        out = store.list_projects(query, limit, offset)
        return {**out, "items": [_with_auto_cover(p) for p in out["items"]]}

    @app.post("/api/projects")
    def create_project(body: CreateProjectBody):
        return store.create_project(body.name, body.brief)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return _with_auto_cover(store.get_project(project_id))

    # ------------------------------------------------- project trash
    def project_delete_preview(project_id: str) -> dict[str, Any]:
        p = store.get_project(project_id)
        prods = productions_mod.productions_of_project(store.data_dir, project_id)
        return {"id": p["id"], "name": p["name"], "counts": p["counts"], "deleted_at": p.get("deleted_at"),
                "productions": [{"slug": x["slug"], "name": x.get("name"), "status": x.get("status")} for x in prods],
                "live_jobs": store.project_live_jobs(project_id)}

    def op_delete_project(project_id: str) -> dict[str, Any]:
        preview = project_delete_preview(project_id)
        store.trash_project(project_id)  # refuses with live jobs, before anything moves
        moved = productions_mod.stash_productions(store.data_dir, project_id)
        return {"id": project_id, "name": preview["name"], "trashed": True, "counts": preview["counts"],
                "productions": moved,
                "note": "in the trash: studio_trash(action='restore', projects=[id]) brings it back until it is emptied"}

    def op_restore_project(project_id: str) -> dict[str, Any]:
        p = store.restore_project(project_id)
        back = productions_mod.unstash_productions(store.data_dir, project_id)
        return {"id": p["id"], "name": p["name"], "restored": True, "productions": back}

    def op_purge_project(project_id: str) -> dict[str, Any]:
        out = store.purge_project(project_id)
        out["productions_deleted"] = productions_mod.purge_stashed_productions(store.data_dir, project_id)
        return out

    def trashed_projects() -> list[dict[str, Any]]:
        out = []
        for p in store.list_trashed_projects():
            stash = productions_mod._stash_dir(store.data_dir, p["id"])
            out.append({"id": p["id"], "name": p["name"], "brief": p.get("brief"), "deleted_at": p["deleted_at"],
                        "counts": p["counts"], "cover_asset_id": p.get("cover_asset_id"),
                        "productions": sorted(f.name for f in stash.iterdir()) if stash.is_dir() else []})
        return out

    @app.get("/api/trash/projects")
    def list_project_trash():
        return {"items": trashed_projects()}

    @app.get("/api/projects/{project_id}/delete-preview")
    def project_delete_preview_route(project_id: str):
        return project_delete_preview(project_id)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str):
        return op_delete_project(project_id)

    @app.post("/api/projects/{project_id}/restore")
    def restore_project(project_id: str):
        return op_restore_project(project_id)

    @app.post("/api/projects/{project_id}/purge")
    def purge_project(project_id: str):
        return op_purge_project(project_id)

    @app.post("/api/agent/studio_delete_project")
    def agent_delete_project(body: DeleteProjectBody):
        return agent("studio_delete_project", body.project, lambda: op_delete_project(body.project))

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
                    "hint": "mention characters, places (element=location) and objects (element=prop) in prompts as @Name; "
                            "their reference images go in as numbered references on Qwen-Image 2.1",
                }
            if body.action == "deleted":
                return {"characters": [_character_view(c) for c in result["characters"]]}
            if body.action == "delete" or body.kind == "group":
                return result
            return _character_view(result)
        return agent("studio_cast", f"{body.action}:{body.kind}:{body.name or body.id or ''}", run)

    @app.get("/api/projects/{project_id}/characters")
    def list_characters(project_id: str):
        return {"items": [{**c, "kit": charkit.kit_of(c)} for c in store.list_characters(project_id)]}

    @app.get("/api/characters/{character_id}")
    def get_character(character_id: str):
        c = store.get_character(character_id)
        return {**c, "kit": charkit.kit_of(c)}

    @app.post("/api/projects/{project_id}/characters")
    def create_character(project_id: str, body: CharacterBody):
        return op_cast(project_id, CastBody(action="create", kind="character", name=body.name, fields=body.fields))

    @app.delete("/api/characters/{character_id}")
    def delete_character(character_id: str, force: bool = False):
        character = store.get_character(character_id)
        return op_cast(character["project_id"], CastBody(action="delete", id=character_id, force=force))

    @app.post("/api/characters/{character_id}/restore")
    def restore_character(character_id: str):
        character = store.get_character(character_id)
        return op_cast(character["project_id"], CastBody(action="restore", id=character_id))

    @app.get("/api/projects/{project_id}/characters/deleted")
    def deleted_characters(project_id: str):
        return {"items": store.list_characters(project_id, deleted=True)}

    @app.delete("/api/groups/{group_id}")
    def delete_group(group_id: str):
        group = store.get_group(group_id)
        return op_cast(group["project_id"], CastBody(action="delete", kind="group", id=group_id))

    @app.patch("/api/characters/{character_id}")
    def update_character(character_id: str, body: CharacterBody):
        character = store.get_character(character_id)
        return op_cast(character["project_id"], CastBody(action="update", kind="character", id=character_id,
                                                         name=body.name, fields=body.fields))

    # ------------------------------------------------------- character kit
    # The character kit (charkit.py / charpack.py / trainers.py): model
    # sheet, dataset, local LoRA training, adapters, takes, portable
    # .hoardchar packs and the global casting library.

    def _char(character_id: Optional[str]) -> dict[str, Any]:
        if not character_id:
            raise engine.EngineError("character_required", "pass character_id (studio_cast list shows them)")
        return store.get_character(character_id)

    def _kit_view(char: dict[str, Any]) -> dict[str, Any]:
        return {"character_id": char["id"], "name": char["name"], **charkit.summary(store, char)}

    def op_char_pack(project: Optional[str], body: CharPackBody) -> dict[str, Any]:
        if body.action == "export":
            char = _char(body.character_id)
            built = charpack.export_to(store, backend, char["id"], include_dataset=body.include_dataset,
                                       include_adapters=body.include_adapters)
            return {**built, "download": f"/api/character-packs/{built['file']}"}
        if body.action in ("import", "inspect"):
            if not body.path:
                raise engine.EngineError("path_required", "import/inspect need path (a .hoardchar file); the UI uploads instead")
            path = engine.resolve_import_path(backend, store, body.path)
            if body.action == "inspect":
                return charpack.inspect_pack(path)
            if not project:
                raise engine.EngineError("project_required", "import needs the project to cast the character into")
            return charpack.import_pack(store, backend, project, path, body.rename)
        raise engine.EngineError("bad_action", "pack actions: export, import, inspect")

    def _casting_project() -> str:
        for p in store.list_projects("Casting", 50)["items"]:
            if p["name"] == "Casting":
                return p["id"]
        return store.create_project("Casting", "Characters cast from the library for recipes and productions")["id"]

    def op_char_library(project: Optional[str], body: CharLibraryBody) -> dict[str, Any]:
        if body.action == "list":
            return {"items": charpack.library_list(store.data_dir, body.query)}
        if body.action == "save":
            char = _char(body.character_id)
            return charpack.library_save(store, backend, char["id"], body.note, body.include_adapters)
        if not body.id:
            raise engine.EngineError("id_required", f"library action '{body.action}' needs id (a lib_... id from list)")
        if body.action == "use":
            return charpack.library_use(store, backend, project or _casting_project(), body.id, body.version, body.rename)
        if body.action == "history":
            return charpack.library_history(store.data_dir, body.id)
        if body.action == "delete":
            return charpack.library_delete(store.data_dir, body.id)
        raise engine.EngineError("bad_action", "library actions: list, save, use, history, delete")

    def op_char_sheet(body: CharSheetBody) -> dict[str, Any]:
        char = _char(body.character_id)
        if not char.get("canonical_asset_id"):
            raise engine.EngineError("no_canonical", f"{char['name']} needs a canonical image first (Cast -> edit -> reference)")
        views = charkit.check_views(body.views)
        for name, value in (("width", body.width), ("height", body.height)):
            if value is not None and not 256 <= value <= 2048:
                raise engine.EngineError("bad_parameter", f"{name} must be between 256 and 2048")
        job = queue.enqueue("character_sheet", "gpu", {**body.model_dump(exclude={"wait_s"}), "views": views},
                            project_id=char["project_id"])
        return {"job": wait(job, body.wait_s), "views": views}

    def op_char_dataset(body: CharDatasetBody) -> dict[str, Any]:
        char = _char(body.character_id)
        if body.action == "get":
            return charkit.dataset_view(store, char)
        if body.action == "report":
            return charkit.dataset_report(store, char)
        if body.action == "build":
            return charkit.dataset_build(store, char["id"], body.sources, body.min_identity, body.replace)
        if body.action == "update":
            return charkit.dataset_update(store, char["id"], body.items or [])
        if body.action == "caption":
            job = queue.enqueue("character_caption", "cpu", {"character_id": char["id"], "only_missing": body.only_missing},
                                project_id=char["project_id"])
            return {"job": wait(job, body.wait_s)}
        raise engine.EngineError("bad_action", "dataset actions: get, report, build, update, caption")

    def op_char_train(body: CharTrainBody) -> dict[str, Any]:
        if body.action == "trainers":
            cfg = backend.training()
            return {"trainers": trainers_mod.trainer_status(cfg), "lora_dir": cfg.get("lora_dir"),
                    "lora_dir_ok": charkit.lora_dir(backend) is not None, "gpu": cfg.get("gpu", "auto"),
                    "base_models": cfg.get("base_models") or {},
                    "archs": {a: {k: p.get(k) for k in ("label", "est_vram_mb", "sec_per_step", "base_hint", "trainers")}
                              for a, p in trainers_mod.ARCH_PRESETS.items()}}
        if body.action == "settings":
            if body.training is None:
                return backend.training()
            return backend.set_training(body.training)
        if body.action == "log":
            if not body.run_id:
                raise engine.EngineError("run_required", "log needs run_id")
            return {"run_id": body.run_id, "lines": charkit.training_log_tail(store, body.run_id)}
        if body.action == "status":
            char = _char(body.character_id)
            jobs = [j for j in store.list_jobs(limit=50, project_id=char["project_id"])["items"]
                    if j["type"] == "train_lora" and (j.get("params") or {}).get("character_id") == char["id"]]
            return {"character_id": char["id"],
                    "runs": [{**engine.job_view(j), "run_id": (j.get("params") or {}).get("run_id"),
                              "arch": (j.get("params") or {}).get("arch")} for j in jobs[:10]],
                    "adapters": [charkit.adapter_view(a) for a in charkit.kit_of(char)["adapters"]]}
        char = _char(body.character_id)
        arch = body.arch or _default_arch(char["project_id"])
        if arch not in trainers_mod.ARCH_PRESETS:
            raise engine.EngineError("bad_arch", f"arch must be one of {', '.join(trainers_mod.ARCHS)}")
        plan = charkit.plan_for(store, backend, char["id"], arch, body.overrides, body.trainer)
        if body.action == "plan":
            return plan
        if body.action == "start":
            if plan["trainer_problem"]:
                raise engine.EngineError("trainer_unavailable", plan["trainer_problem"])
            if not plan["dataset"]["ready"]:
                raise engine.EngineError("dataset_not_ready", "; ".join(plan["dataset"]["warnings"]) or "the dataset is not ready")
            run_id = new_id("tr")  # known up front so the log can be tailed while it runs
            job = queue.enqueue("train_lora", "gpu", {"character_id": char["id"], "arch": arch, "trainer": body.trainer,
                                                      "overrides": body.overrides, "run_id": run_id},
                                project_id=char["project_id"])
            return {"job": wait(job, body.wait_s), "plan": plan["plan"], "trainer": plan["trainer"], "run_id": run_id}
        raise engine.EngineError("bad_action", "train actions: trainers, settings, plan, start, status, log")

    def _default_arch(project_id: str) -> str:
        proj = store.get_project(project_id)
        try:
            info = engine.object_info_live_or_cached(backend)
        except (Unavailable, RuntimeError):
            info = {}
        name = engine.resolve_image_engine(info or {}, proj.get("image_engine"))
        return charkit.ENGINE_ARCH.get(name, "qwen_image")

    def op_char_adapters(body: CharAdaptersBody) -> dict[str, Any]:
        char = _char(body.character_id)
        if body.action == "list":
            return _kit_view(char)
        if body.action == "available":
            try:
                info, live = engine._object_info(backend), True
            except Unavailable:
                try:
                    info = engine.object_info_live_or_cached(backend)
                except RuntimeError:
                    info = {}
                live = False
            return {"loras": comfy_driver.lora_choices(info) if info else [], "comfy": live,
                    **({} if live else {"note": "ComfyUI is off: this is the list from its last known state"})}
        if body.action == "settings":
            return _kit_view(charkit.update_settings(store, char["id"], body.settings))
        if body.action == "attach":
            if not body.lora_name or not body.arch:
                raise engine.EngineError("bad_adapter", "attach needs lora_name (as ComfyUI lists it) and arch")
            try:
                info = engine._object_info(backend)
            except Unavailable:
                info = None  # ComfyUI off: trust the name; renders check it again
            available = comfy_driver.lora_choices(info) if info else None
            a = charkit.attach_adapter(store, char["id"], lora_name=body.lora_name, arch=body.arch,
                                       strength=body.strength or 1.0, trigger=body.trigger,
                                       installed=available is None or body.lora_name in available)
            return {"adapter": charkit.adapter_view(a),
                    **({"note": "ComfyUI does not list this file yet"} if not a["installed"] else {})}
        if not body.adapter_id:
            raise engine.EngineError("adapter_required", f"'{body.action}' needs adapter_id")
        if body.action == "update":
            patch = {k: v for k, v in {"strength": body.strength, "trigger": body.trigger, "enabled": body.enabled}.items()
                     if v is not None}
            return {"adapter": charkit.adapter_view(charkit.update_adapter(store, char["id"], body.adapter_id, patch))}
        if body.action == "remove":
            return charkit.remove_adapter(store, char["id"], body.adapter_id)
        raise engine.EngineError("bad_action", "adapter actions: list, available, attach, update, remove, settings")

    def op_char_takes(body: CharTakesBody) -> dict[str, Any]:
        char = _char(body.character_id)
        if body.action == "list":
            if body.sort not in ("recent", "identity"):
                raise engine.EngineError("bad_sort", "sort is recent or identity")
            return charkit.list_takes(store, char["id"], body.limit, body.sort, body.kind)
        if body.action == "score":
            job = queue.enqueue("character_identity", "cpu", {"character_id": char["id"], "asset_ids": body.asset_ids,
                                                              "force": body.force, "limit": min(max(body.limit, 1), 60)},
                                project_id=char["project_id"])
            return {"job": wait(job, body.wait_s)}
        if body.action == "act":
            if not body.asset_id or not body.take_action:
                raise engine.EngineError("bad_take", "act needs asset_id and take_action")
            return charkit.take_action(store, char["id"], body.asset_id, body.take_action)
        raise engine.EngineError("bad_action", "take actions: list, score, act")

    def _caption_job(job: dict[str, Any], progress) -> dict[str, Any]:
        fn, name = vision_for_qa()
        out = charkit.auto_caption(store, job["params"]["character_id"], fn, bool(job["params"].get("only_missing")),
                                   progress)
        return {"captioned": out["captioned"], "method": out["method"] if fn else "recipes (no vision model)",
                "model": name}

    def _identity_job(job: dict[str, Any], progress) -> dict[str, Any]:
        fn, name = vision_for_qa()
        p = job["params"]
        progress(0.05, "scoring identity")
        out = charkit.score_takes(store, p["character_id"], p.get("asset_ids"), fn, name, bool(p.get("force")),
                                  int(p.get("limit") or 24))
        out["results"] = [{k: (engine._clip(v, 100) if k == "why" else v) for k, v in r.items() if k != "cached"}
                          for r in out["results"]]
        return {**out, "model": name if fn else None}

    queue.register("character_sheet", lambda job, p: charkit.sheet_job(store, backend, job, p))
    queue.register("train_lora", lambda job, p: charkit.train_job(store, backend, job, p))
    queue.register("character_caption", _caption_job)
    queue.register("character_identity", _identity_job)

    @app.post("/api/agent/studio_character_pack")
    def agent_char_pack(body: CharPackBody, project: Optional[str] = None):
        def run():
            out = op_char_pack(project, body)
            return {k: v for k, v in out.items() if k != "notes" or v}
        return agent("studio_character_pack", f"{body.action}:{body.character_id or body.path or ''}"[:120], run)

    @app.post("/api/agent/studio_character_library")
    def agent_char_library(body: CharLibraryBody, project: Optional[str] = None):
        return agent("studio_character_library", f"{body.action}:{body.id or body.character_id or body.query or ''}",
                     lambda: op_char_library(project, body))

    @app.post("/api/agent/studio_character_sheet")
    def agent_char_sheet(body: CharSheetBody):
        def run():
            res = op_char_sheet(body)
            return {"job": job_result(res["job"]), "views": res["views"]}
        return agent("studio_character_sheet", body.character_id, run)

    @app.post("/api/agent/studio_character_dataset")
    def agent_char_dataset(body: CharDatasetBody):
        def run():
            out = op_char_dataset(body)
            if "items" in out and isinstance(out["items"], list):
                out = {**out, "items": [{k: v for k, v in i.items() if k != "thumb"} for i in out["items"][:60]]}
            if "job" in out:
                out["job"] = job_result(out["job"])
            return out
        return agent("studio_character_dataset", f"{body.action}:{body.character_id}", run)

    @app.post("/api/agent/studio_character_train")
    def agent_char_train(body: CharTrainBody):
        def run():
            out = op_char_train(body)
            if "job" in out:
                out["job"] = job_result(out["job"])
            return out
        return agent("studio_character_train", f"{body.action}:{body.character_id or ''}:{body.arch or ''}", run)

    @app.post("/api/agent/studio_character_adapters")
    def agent_char_adapters(body: CharAdaptersBody):
        return agent("studio_character_adapters", f"{body.action}:{body.character_id}", lambda: op_char_adapters(body))

    @app.post("/api/agent/studio_character_takes")
    def agent_char_takes(body: CharTakesBody):
        def run():
            out = op_char_takes(body)
            if "job" in out:
                out["job"] = job_result(out["job"])
            return out
        return agent("studio_character_takes", f"{body.action}:{body.character_id}", run)

    # UI routes (same operations, full payloads)
    @app.get("/api/characters/{character_id}/kit")
    def ui_char_kit(character_id: str):
        char = store.get_character(character_id)
        kit = charkit.kit_of(char)
        return {**_kit_view(char), "history": kit["history"][-30:], "sheet_asset_ids": kit["sheet"].get("asset_ids") or [],
                "views": list(charkit.SHEET_VIEWS), "default_views": charkit.DEFAULT_SHEET}

    @app.post("/api/characters/{character_id}/sheet")
    def ui_char_sheet(character_id: str, body: CharSheetBody):
        body.character_id = character_id
        return op_char_sheet(body)

    @app.post("/api/characters/{character_id}/dataset")
    def ui_char_dataset(character_id: str, body: CharDatasetBody):
        body.character_id = character_id
        return op_char_dataset(body)

    @app.post("/api/characters/{character_id}/train")
    def ui_char_train(character_id: str, body: CharTrainBody):
        body.character_id = character_id
        return op_char_train(body)

    @app.post("/api/training")
    def ui_training(body: CharTrainBody):
        if body.action not in ("trainers", "settings", "log"):
            raise engine.EngineError("bad_action", "this route takes trainers, settings or log")
        return op_char_train(body)

    @app.post("/api/characters/{character_id}/adapters")
    def ui_char_adapters(character_id: str, body: CharAdaptersBody):
        body.character_id = character_id
        return op_char_adapters(body)

    @app.post("/api/characters/{character_id}/takes")
    def ui_char_takes(character_id: str, body: CharTakesBody):
        body.character_id = character_id
        return op_char_takes(body)

    @app.post("/api/characters/{character_id}/pack")
    def ui_char_pack_export(character_id: str, body: CharPackBody):
        body.character_id, body.action = character_id, "export"
        return op_char_pack(None, body)

    @app.get("/api/character-packs/{file_name}")
    def ui_char_pack_download(file_name: str):
        path = charpack.export_path(store.data_dir, file_name)
        return FileResponse(path, media_type="application/zip", filename=path.name)

    async def _read_pack_upload(file: UploadFile) -> Path:
        tmp_dir = store.data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = tmp_dir / f"{new_id('up')}.hoardchar"
        total = 0
        with tmp_path.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > charpack.MAX_PACK_BYTES:
                    fh.close()
                    tmp_path.unlink(missing_ok=True)
                    raise engine.EngineError("too_large", "the pack is larger than 2 GB")
                fh.write(chunk)
        return tmp_path

    @app.post("/api/projects/{project_id}/character-packs")
    async def ui_char_pack_import(project_id: str, file: UploadFile, rename: Optional[str] = None):
        store.get_project(project_id)
        tmp_path = await _read_pack_upload(file)
        try:
            return charpack.import_pack(store, backend, project_id, tmp_path, rename)
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.post("/api/character-packs/inspect")
    async def ui_char_pack_inspect(file: UploadFile):
        tmp_path = await _read_pack_upload(file)
        try:
            return charpack.inspect_pack(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.post("/api/library/characters")
    def ui_library(body: CharLibraryBody, project: Optional[str] = None):
        return op_char_library(project, body)

    @app.get("/api/library/characters/{lib_id}/preview")
    def ui_library_preview(lib_id: str):
        charpack.library_history(store.data_dir, lib_id)  # validates the id
        path = charpack.library_root(store.data_dir) / lib_id / "preview.png"
        if not path.is_file():
            raise NotFound("preview", lib_id)
        return FileResponse(path, media_type="image/png")

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

    @app.get("/api/cinema")
    def cinema_guide(q: str = "", category: Optional[str] = None):
        return {"categories": [{"id": k, "name": {"en": v["en"], "es": v["es"]}, "aliases": v["aliases"]}
                               for k, v in cinema.CATEGORIES.items()],
                "items": cinema.search(q, category)}

    @app.get("/api/agent/studio_cinema")
    def agent_cinema(q: str = "", category: Optional[str] = None):
        def run():
            items = cinema.search(q, category)
            return {"items": [{"id": e["id"], "category": e["category"], "name": e["name"]["en"], "es": e["name"]["es"],
                               "use": e["when"]["en"], "prompt": e["prompt"], **({"clips_only": True} if e["video_only"] else {})}
                              for e in items],
                    "camera_keys": list(cinema.CAMERA_KEYS)}
        return agent("studio_cinema", f"{category or ''}:{q}"[:80], run)

    @app.get("/api/style-presets")
    def style_presets(project: Optional[str] = None):
        return {"items": store.list_style_presets(project)}

    @app.post("/api/projects/{project_id}/compose-prompt")
    def compose(project_id: str, body: ComposePromptBody):
        composed = engine.compose_prompt(store, project_id, body.prompt, body.negative, body.style)
        added = engine.add_element_references(composed, ["_"] * max(0, min(body.references, 10)), body.engine or "")
        return {**composed, "added_references": [{**r, "index": max(0, min(body.references, 10)) + 1 + i}
                                                 for i, r in enumerate(added)]}

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

    def op_outpaint(body: OutpaintBody):
        from .outpaint import prepare
        prepared = prepare(store, body.asset_id, body.width, body.height, body.anchor_x, body.anchor_y)
        result = op_edit(EditImageBody(asset_id=prepared['canvas']['id'], operation='inpaint', prompt=body.prompt,
                                      mask_asset_id=prepared['mask']['id'], strength=1.0, seed=body.seed, wait_s=body.wait_s))
        return {**prepared, **result}

    @app.post("/api/agent/studio_outpaint")
    def agent_outpaint(body: OutpaintBody):
        def run():
            result=op_outpaint(body)
            result['job']=job_result(result['job'])
            return result
        return agent('studio_outpaint',body.asset_id,run)

    @app.post("/api/assets/{asset_id}/outpaint")
    def ui_outpaint(asset_id: str, body: OutpaintBody):
        return op_outpaint(body.model_copy(update={'asset_id':asset_id}))

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
        return comfy_driver.import_custom_workflow(store.data_dir, name or Path(file.filename or "workflow").stem, raw,
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
            return engine.import_asset(store, project_id, tmp_path, kind, original_name=original)
        finally:
            tmp_path.unlink(missing_ok=True)

    # ------------------------------------------------------------------ audio
    @app.post("/api/agent/studio_time_lyrics")
    def agent_time_lyrics(project: str, body: TimeLyricsBody):
        return agent("studio_time_lyrics", body.song_asset_id,
                     lambda: engine.time_lyrics(store, project, body.song_asset_id, body.lyrics, body.name))

    def _with_lyric_sections(asset_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        """The song's sections from its own timed lyrics ([Verse], [Chorus]...)
        when it has some: they are what the song really is, the audio-only
        sections are guesses from energy changes."""
        try:
            song = store.get_asset(asset_id)
            items = store.list_assets(song["project_id"], kind="lyrics", query=asset_id, limit=20)["items"]
        except Exception:  # noqa: BLE001 - no lyrics: the audio sections as they are
            return analysis
        for lyr in items:
            if (lyr.get("recipe") or {}).get("derived_from") != asset_id:
                continue
            try:
                lines = audio_mod.parse_lrc(engine.read_lyrics(store, lyr["id"])["text"])
            except Exception:  # noqa: BLE001
                continue
            _, sections = audio_mod.lrc_sections(lines, float(analysis.get("duration_s") or 0))
            if len(sections) >= 2:
                return {**analysis, "sections": sections, "sections_source": "lyrics", "lyrics_asset_id": lyr["id"],
                        "notes": "sections from the song's timed lyrics; downbeats are estimates"}
        return analysis

    @app.post("/api/agent/studio_analyze_audio")
    def agent_analyze_audio(asset_id: str):
        return agent("studio_analyze_audio", asset_id,
                     lambda: engine.analysis_view(asset_id, _with_lyric_sections(asset_id, engine.analyze_audio(store, asset_id))))

    @app.post("/api/assets/{asset_id}/analyze")
    def ui_analyze(asset_id: str, force: bool = False):
        return _with_lyric_sections(asset_id, engine.analyze_audio(store, asset_id, force=force))

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

    # ------------------------------------------------ export to editors
    def _export_lookup(asset_id: str) -> Optional[dict[str, Any]]:
        try:
            a = store.get_asset(asset_id)
            path = _asset_path(store, a["file_path"])
        except (NotFound, KeyError):
            return None
        return {"path": path, "name": a.get("name") or path.name, "kind": a["kind"], "duration_s": a.get("duration_s"),
                "width": a.get("width"), "height": a.get("height")}

    def _export_timeline_id(body: ExportTimelineBody) -> str:
        if body.timeline_id:
            return body.timeline_id
        if not body.production:
            raise engine.EngineError("timeline_required", "give timeline_id, or production (and aspect)")
        state = productions_mod.load_state(store.data_dir, body.production)
        timelines = ((state.get("done") or {}).get("timeline") or {}).get("timelines") or {}
        if not timelines:
            raise engine.EngineError("no_cut_yet", "this production has no cut yet: continue it to the cut stage")
        aspect = body.aspect if body.aspect in timelines else next(iter(timelines))
        return timelines[aspect]["timeline_id"]

    @app.get("/api/timelines/{timeline_id}/export")
    def export_timeline(timeline_id: str, format: str = "zip", name: Optional[str] = None):
        tl = store.get_timeline(timeline_id)
        name = (name or "").strip()[:100] or tl.get("name") or timeline_id
        base = re.sub(r"[^\w-]+", "_", name).strip("_")[:60] or "cut"
        if format == "xml":
            return Response(exporters.to_xmeml(tl, _export_lookup, name), media_type="application/xml",
                            headers={"Content-Disposition": exporters.download_disposition(f'{base}.xml')})
        if format == "edl":
            return Response(exporters.to_edl(tl, _export_lookup, name), media_type="text/plain; charset=utf-8",
                            headers={"Content-Disposition": exporters.download_disposition(f'{base}.edl')})
        if format in ("srt", "vtt"):
            return Response(exporters.to_subtitles(tl, format),
                            media_type="text/vtt" if format == "vtt" else "application/x-subrip",
                            headers={"Content-Disposition": exporters.download_disposition(f'{base}.{format}')})
        if format != "zip":
            raise engine.EngineError("bad_format", "format must be zip, xml, edl, srt or vtt")
        return Response(exporters.package(tl, _export_lookup, name), media_type="application/zip",
                        headers={"Content-Disposition": exporters.download_disposition(f'{base}_for_editors.zip')})

    @app.post("/api/agent/studio_export_timeline")
    def agent_export_timeline(body: ExportTimelineBody):
        def run():
            tid = _export_timeline_id(body)
            tl = store.get_timeline(tid)
            visual = next((t for t in tl["tracks"] if t.get("type") == "visual"), {"clips": []})
            return {"timeline_id": tid, "clips": len(visual["clips"]), "fps": tl.get("fps"),
                    "download": f"/api/timelines/{tid}/export?format=zip",
                    "xml": f"/api/timelines/{tid}/export?format=xml", "edl": f"/api/timelines/{tid}/export?format=edl",
                    "srt": f"/api/timelines/{tid}/export?format=srt", "vtt": f"/api/timelines/{tid}/export?format=vtt",
                    "captions": len(exporters.caption_cues(tl)),
                    "note": "Open the XML in Premiere or Resolve and import SRT/VTT separately for timed captions; media stay on this computer. Caption styling is not transferred."}
        return agent("studio_export_timeline", body.production or body.timeline_id or "", run)

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

    @app.get("/api/assets")
    def list_all_assets(kind: Optional[str] = None, query: Optional[str] = None, limit: int = 60, offset: int = 0,
                        project: Optional[str] = None):
        """Assets across every project (the pickers' "all projects" scope),
        each with its project's name."""
        res = store.list_assets(project or None, kind, query, None, None, limit, offset)
        names: dict[str, str] = {}
        for a in res["items"]:
            a.pop("analysis", None)
            a.pop("waveform", None)
            pid = a.get("project_id")
            if pid and pid not in names:
                try:
                    names[pid] = store.get_project(pid)["name"]
                except NotFound:
                    names[pid] = ""
            a["project_name"] = names.get(pid, "")
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

    def op_delete_assets(ids: list[str], force: bool) -> dict[str, Any]:
        if not ids or len(ids) > 200:
            raise ValueError("ids: 1 to 200 asset ids")
        deleted, failed = [], []
        for aid in dict.fromkeys(ids):
            try:
                deleted.append(store.trash_asset(aid, force=force))
            except (AssetInUse, NotFound) as exc:
                failed.append({"id": aid, "error": "asset_in_use" if isinstance(exc, AssetInUse) else "not_found",
                               "message": str(exc), **({"references": exc.references} if isinstance(exc, AssetInUse) else {})})
        return {"deleted": [d["id"] for d in deleted], "detached": {d["id"]: d["detached"] for d in deleted if d["detached"]},
                "failed": failed, "note": "in the trash: restore with studio_trash(action='restore') until it is emptied"}

    def op_trash(body: TrashBody) -> dict[str, Any]:
        if body.action == "list":
            return {"items": store.list_trash(body.project), "projects": trashed_projects()}
        if body.action == "restore":
            if not body.ids and not body.projects:
                raise ValueError("restore needs ids (assets) or projects")
            out: dict[str, Any] = {}
            if body.projects:
                out["projects"] = [op_restore_project(p) for p in body.projects]
            if body.ids:
                out["restored"] = [store.restore_asset(i)["id"] for i in body.ids]
            return out
        if body.action == "empty":
            if body.projects:
                return {"projects": [op_purge_project(p) for p in body.projects]}
            return store.empty_trash(body.project, body.ids)
        raise ValueError("action must be list, restore or empty")

    @app.delete("/api/assets/{asset_id}")
    def delete_asset(asset_id: str, force: bool = False):
        return store.trash_asset(asset_id, force=force)

    @app.post("/api/assets/delete")
    def delete_assets(body: DeleteAssetsBody):
        return op_delete_assets(body.ids, body.force)

    @app.post("/api/assets/{asset_id}/restore")
    def restore_asset(asset_id: str):
        return store.restore_asset(asset_id)

    @app.get("/api/trash")
    def list_trash(project: Optional[str] = None):
        return {"items": store.list_trash(project)}

    @app.get("/api/trash/{asset_id}/thumb")
    def trash_thumb(asset_id: str):
        path = store.trash_file(asset_id)
        if not path or not path.is_file():
            return JSONResponse({"error": "no_thumb", "message": "no preview kept"}, status_code=404)
        return FileResponse(path)

    @app.post("/api/trash/empty")
    def empty_trash(body: TrashBody):
        return store.empty_trash(body.project, body.ids)

    @app.post("/api/agent/studio_delete_assets")
    def agent_delete_assets(body: DeleteAssetsBody):
        return agent("studio_delete_assets", ",".join(body.ids)[:200], lambda: op_delete_assets(body.ids, body.force))

    @app.post("/api/agent/studio_trash")
    def agent_trash(body: TrashBody):
        return agent("studio_trash", body.action, lambda: op_trash(body))

    @app.get("/api/assets/{asset_id}/thumb")
    def asset_thumb(asset_id: str):
        asset = store.get_asset(asset_id)
        thumb = asset.get("thumb_path") or engine.ensure_thumbnail(store, asset)
        if not thumb:
            return JSONResponse({"error": "no_thumb", "message": "asset has no thumbnail"}, status_code=404)
        return FileResponse(_asset_path(store, thumb), media_type="image/webp")

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

        # -- spaces (spaces.py): edits queue jobs, the rest is local work
        def edit(self, body: dict[str, Any]) -> dict[str, Any]:
            return op_edit(EditImageBody(**{k: v for k, v in body.items() if v is not None}))["job"]

        def last_frame(self, asset_id: str, project_id: str) -> dict[str, Any]:
            return engine.last_frame(store, asset_id, project_id)

        def combine(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
            return engine.combine_clips(store, project_id, body["clips"], body.get("audio_asset_id"),
                                        float(body.get("audio_start_s") or 0))

        def composite(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
            return engine.composite_media(store, project_id, body["background"], body["layers"])

        def clip_edit(self, body: dict[str, Any]) -> dict[str, Any]:
            return op_clip_edit(ClipEditBody(**{k: v for k, v in body.items() if v is not None}))["job"]

        def cancel(self, job_id: str) -> None:
            queue.cancel(job_id)

        # -- narrated shorts (shorts.py); tests swap these through
        # app.state.short_hooks = {"chat", "synthesize", "transcribe", "stock_client", "stock_keys"}
        def _hook(self, name: str) -> Any:
            return (getattr(app.state, "short_hooks", None) or {}).get(name)

        def chat(self, messages: list[dict[str, Any]], max_tokens: int, temperature: float,
                 effort: Optional[str] = None) -> str:
            hook = self._hook("chat")
            if hook:
                return hook(messages, max_tokens, temperature)
            return backend.link.sync.chat(messages=messages, max_tokens=max_tokens, temperature=temperature,
                                          effort=effort).text

        def synthesize(self, voice: dict[str, Any], text: str) -> tuple[bytes, str]:
            hook = self._hook("synthesize")
            if hook:
                return hook(voice, text)
            if voice.get("engine_id") or str(voice.get("voice_id") or "").startswith("voice_"):  # a voice-studio voice
                try:
                    return voice_lab.synthesize_with_spec(store, _tts_engines(), voice, text)
                except VoiceLabError as exc:
                    raise shorts_mod.ShortError(getattr(exc, "code", "tts_failed"), str(exc)) from None
            try:
                return voices_mod.synthesize(backend, store.data_dir / "voices", text,
                                             {k: voice[k] for k in ("backend", "voice_id", "speed") if voice.get(k)})
            except voices_mod.VoiceError as exc:
                raise shorts_mod.ShortError(exc.code, str(exc)) from None

        def transcribe(self, path: Path, language: Optional[str]) -> Optional[list[dict[str, Any]]]:
            hook = self._hook("transcribe")
            if hook:
                return hook(path, language)
            stt = ve.best_installed_stt(_stt_engines())
            if stt is None:
                return None
            result = stt.transcribe(path, language=language, word_timestamps=True)
            return [{"text": w.get("word") or w.get("text") or "", "start_s": w["start_s"], "end_s": w["end_s"]}
                    for seg in result.get("segments") or [] for w in seg.get("words") or []]

        def stock_keys(self) -> dict[str, str]:
            hook = self._hook("stock_keys")
            return hook() if hook else backend.stock_keys()

        def stock_client(self) -> Any:
            hook = self._hook("stock_client")
            return hook() if hook else None

    studio = AppStudio()
    app.state.studio = studio
    production_hooks: dict[str, Any] = {"qa_hook": None, "stage_hooks": {}}
    app.state.production_hooks = production_hooks

    def _production_job(job: dict[str, Any], progress) -> dict[str, Any]:
        slug = job["params"]["slug"]
        return productions_mod.run_production(store, studio, slug, progress, qa_hook=production_hooks["qa_hook"],
                                              stage_hooks=production_hooks["stage_hooks"])

    queue.register("production", _production_job)

    # ---------------------------------------------------------------- spaces
    # The node canvas (spaces.py): graph saved by the UI or an agent, runs
    # as an orchestrator job that waits for the generate/compose jobs.

    def _space_job(job: dict[str, Any], progress) -> dict[str, Any]:
        p = job["params"]
        return spaces_mod.run_space(store, studio, p["space_id"], p.get("mode") or "node", p.get("node_ids") or [],
                                    progress=progress, force=bool(p.get("force")))

    queue.register("space_run", _space_job)

    def space_view(space: dict[str, Any], compact: bool = False) -> dict[str, Any]:
        state = space["state"]
        live = {}
        for nid, st in state.items():
            st = dict(st)
            if st.get("status") == "running":
                jobs = []
                for jid in st.get("jobs") or []:
                    try:
                        j = store.get_job(jid)
                        jobs.append({"id": jid, "state": j["state"], "progress": j.get("progress") or 0})
                    except NotFound:
                        continue
                st["job_states"] = jobs
            if compact:
                st = {k: v for k, v in st.items() if k in ("status", "outputs", "error", "excluded")}
            live[nid] = st
        out = {"id": space["id"], "project_id": space["project_id"], "name": space["name"], "version": space["version"],
               "updated_at": space["updated_at"], "graph": space["graph"], "state": live}
        if space.get("deleted_at"):
            out["deleted_at"] = space["deleted_at"]
        return out

    def _space_cover(space: dict[str, Any]) -> Optional[str]:
        for st in space["state"].values():
            for aid in reversed(st.get("outputs") or []):
                return aid
        return None

    @app.get("/api/projects/{project_id}/spaces")
    def list_spaces(project_id: str, deleted: bool = False):
        return {"items": [{"id": sp["id"], "name": sp["name"], "updated_at": sp["updated_at"], "version": sp["version"],
                           "nodes": len(sp["graph"].get("nodes") or []), "cover": _space_cover(sp),
                           "app": any((n.get("data") or {}).get("app_input") for n in sp["graph"].get("nodes") or []),
                           **({"deleted_at": sp["deleted_at"]} if sp.get("deleted_at") else {})}
                          for sp in store.list_spaces(project_id, deleted=deleted)]}

    def op_space_import(project_id: str, bundle: Any, name: Optional[str] = None, into: Optional[str] = None) -> dict[str, Any]:
        store.get_project(project_id)
        if into:
            space = store.get_space(into)
            if space["project_id"] != project_id:
                raise spaces_mod.SpaceError("wrong_project", "that space belongs to another project")
            graph, report = spaces_mod.import_technique(store, project_id, bundle, space["graph"])
            saved = store.save_space_graph(space["id"], graph, None, None)
        else:
            graph, report = spaces_mod.import_technique(store, project_id, bundle)
            saved = store.create_space(project_id, (name or str((bundle or {}).get("name") or "Technique"))[:120], graph)
        return {**report, "space": space_view(saved, compact=True)}

    @app.get("/api/spaces/{space_id}/export")
    def space_export(space_id: str, group: Optional[str] = None):
        return spaces_mod.export_technique(store, store.get_space(space_id), group)

    @app.post("/api/projects/{project_id}/spaces/import")
    def space_import(project_id: str, body: SpaceImportBody):
        return op_space_import(project_id, body.bundle, body.name, body.space)

    @app.post("/api/projects/{project_id}/spaces")
    def create_space(project_id: str, body: SpaceCreateBody):
        if body.template not in spaces_mod.TEMPLATES:
            raise spaces_mod.SpaceError("unknown_template", f"templates: {', '.join(spaces_mod.TEMPLATES)}")
        return space_view(store.create_space(project_id, body.name, spaces_mod.template_graph(body.template)))

    @app.get("/api/spaces/{space_id}")
    def get_space(space_id: str):
        return space_view(store.get_space(space_id))

    @app.put("/api/spaces/{space_id}")
    def save_space(space_id: str, body: SpaceSaveBody):
        graph = spaces_mod.validate_graph(body.graph)
        return space_view(store.save_space_graph(space_id, graph, body.version, body.name))

    @app.delete("/api/spaces/{space_id}")
    def delete_space(space_id: str):
        return space_view(store.delete_space(space_id), compact=True)

    @app.post("/api/spaces/{space_id}/restore")
    def restore_space(space_id: str):
        return space_view(store.restore_space(space_id), compact=True)

    def op_space_run(space_id: str, body: SpaceRunBody) -> dict[str, Any]:
        space = store.get_space(space_id)
        if body.mode not in ("node", "downstream", "upto", "all"):
            raise spaces_mod.SpaceError("bad_mode", "mode is node, downstream, upto or all")
        order = spaces_mod.run_order(space["graph"], body.mode, body.node_ids)
        if not order:
            raise spaces_mod.SpaceError("nothing_to_run", "no picture, clip or song node to run there")
        for nid in order:
            store.patch_space_state(space_id, nid, {"status": "queued", "error": None})
        job = queue.enqueue("space_run", "cpu", {"space_id": space_id, "mode": body.mode, "node_ids": body.node_ids,
                                                 "force": body.force, "name": space["name"]},
                            project_id=space["project_id"])
        return {"job": engine.job_view(job), "nodes": order}

    @app.post("/api/spaces/{space_id}/run")
    def run_space(space_id: str, body: SpaceRunBody):
        return op_space_run(space_id, body)

    def op_space_stop(space_id: str) -> dict[str, Any]:
        """Stop a space's run: its orchestrator job is cancelled (which cancels
        the renders it queued) and nodes still waiting go back to idle."""
        space = store.get_space(space_id)
        stopped = []
        for j in store.list_jobs(state="active", limit=50)["items"]:
            if j.get("type") == "space_run" and (j.get("params") or {}).get("space_id") == space_id:
                queue.cancel(j["id"])
                stopped.append(j["id"])
        for nid, st in space["state"].items():
            if st.get("status") == "queued":
                store.patch_space_state(space_id, nid, {"status": "done" if st.get("outputs") or st.get("texts") else None,
                                                        "error": None})
        return {"stopped": stopped, "space": space_view(store.get_space(space_id), compact=True)}

    @app.post("/api/spaces/{space_id}/stop")
    def stop_space(space_id: str):
        return op_space_stop(space_id)

    def op_space_estimate(space_id: str, mode: str = "all", node_ids: Optional[list[str]] = None,
                          force: bool = False) -> dict[str, Any]:
        return spaces_mod.estimate(store, space_id, mode, node_ids or [], force, backend.vram_estimates_mb())

    @app.get("/api/spaces/{space_id}/estimate")
    def space_estimate(space_id: str, mode: str = "all", node_ids: str = "", force: bool = False):
        return op_space_estimate(space_id, mode, [x for x in node_ids.split(",") if x], force)

    def op_space_app(space_id: str) -> dict[str, Any]:
        return spaces_mod.app_view(store, store.get_space(space_id))

    def op_space_app_run(space_id: str, values: dict[str, Any]) -> dict[str, Any]:
        space = store.get_space(space_id)
        view = spaces_mod.app_view(store, space)
        if not view["inputs"] and values:
            raise spaces_mod.SpaceError("no_inputs", "this space has no app inputs: mark text, media or cast nodes as inputs")
        graph = spaces_mod.validate_graph(spaces_mod.apply_app_values(store, space["graph"], values))
        saved = store.save_space_graph(space_id, graph, None, None)
        run = op_space_run(space_id, SpaceRunBody(mode="all"))
        return {**run, "version": saved["version"], "app": spaces_mod.app_view(store, store.get_space(space_id))}

    @app.get("/api/spaces/{space_id}/app")
    def space_app(space_id: str):
        return op_space_app(space_id)

    @app.post("/api/spaces/{space_id}/app/run")
    def space_app_run(space_id: str, body: SpaceAppRunBody):
        return op_space_app_run(space_id, body.values)

    def op_space_build(space_id: str, request: str) -> dict[str, Any]:
        """The local model draws (or extends) a space from a request in
        words: a compact plan turned into edit ops, laid out next to what
        is already there, validated, saved."""
        space = store.get_space(space_id)
        text = (request or "").strip()
        if not text:
            raise spaces_mod.SpaceError("empty_request", "say what to make")
        cast = [f"{c['name']} ({c.get('element') or 'character'})" for c in store.list_characters(space["project_id"])][:40]
        existing = [f"{n['id']} ({n['type']})" for n in space["graph"]["nodes"]][:60]
        songs = [f"{a['id']} \"{(a.get('name') or '')[:50]}\" ({round(float(a.get('duration_s') or 0))} s)"
                 for a in store.list_assets(space["project_id"], kind="audio", limit=8)["items"]
                 if (a.get("recipe") or {}).get("operation") not in ("stems",)][:5]
        ask = (f"Project cast: {', '.join(cast) or 'none'}.\nProject songs, newest first: {'; '.join(songs) or 'none'}.\n"
               f"Nodes already on the canvas (you may wire to them by id): "
               f"{', '.join(existing) or 'none'}.\n\nRequest: {text}")
        messages = [{"role": "system", "content": spaces_mod.BUILD_GUIDE}, {"role": "user", "content": ask}]
        last_error = ""
        for attempt in range(2):
            if last_error:
                messages = messages + [{"role": "user", "content": f"That was not valid ({last_error}). Answer with the JSON only."}]
            reply = studio.chat(messages, 1800, 0.2, effort="off")
            m = re.search(r"\{.*\}", reply or "", re.S)
            try:
                plan = json.loads(m.group(0)) if m else None
            except ValueError as exc:
                plan, last_error = None, f"bad JSON: {exc}"
            if not isinstance(plan, dict) or not plan.get("nodes"):
                last_error = last_error or "no nodes"
                continue
            existing_ids = {n["id"] for n in space["graph"]["nodes"]}
            ops = spaces_mod.plan_to_ops(store, space["project_id"], plan, existing_ids)
            if not any(o["op"] == "add_node" for o in ops):
                last_error = "no usable nodes"
                continue
            try:
                graph = _apply_space_ops(space["graph"], ops)
            except spaces_mod.SpaceError as exc:
                last_error = exc.message
                continue
            new_ids = {o["id"] for o in ops if o["op"] == "add_node"}
            right = max([n["x"] for n in space["graph"]["nodes"]] or [-460]) + 460
            spaces_mod.layout_new_nodes(graph, new_ids, right if space["graph"]["nodes"] else 0, 0)
            saved = store.save_space_graph(space_id, spaces_mod.validate_graph(graph), None, None)
            return {"note": str(plan.get("note") or "")[:300], "added": sorted(new_ids),
                    "wires": sum(1 for o in ops if o["op"] == "connect"), "space": space_view(saved, compact=True)}
        raise spaces_mod.SpaceError("build_failed", f"the local model did not give a usable graph ({last_error}); "
                                                    "try again with simpler words, or build it with edit ops")

    @app.post("/api/spaces/{space_id}/build")
    def space_build(space_id: str, body: SpaceBuildBody):
        return op_space_build(space_id, body.request)

    @app.patch("/api/spaces/{space_id}/nodes/{node_id}")
    def space_node_state(space_id: str, node_id: str, body: SpaceNodeStateBody):
        st = (store.get_space(space_id)["state"].get(node_id)) or {}
        patch: dict[str, Any] = {}
        if body.excluded is not None:
            mine = set(st.get("outputs") or []) | set(st.get("texts") or [])
            patch["excluded"] = [a for a in body.excluded if a in mine]
        if body.outputs is not None:
            known = {a for r in st.get("runs") or [] for a in r.get("outputs") or []} | set(st.get("outputs") or [])
            unknown = [a for a in body.outputs if a not in known]
            if unknown:
                raise spaces_mod.SpaceError("bad_outputs", "outputs must come from this node's runs")
            patch.update({"outputs": list(body.outputs), "excluded": [], "status": "done"})
        return space_view({**store.get_space(space_id), "state": store.patch_space_state(space_id, node_id, patch)})

    def op_enhance_prompt(body: EnhancePromptBody) -> dict[str, Any]:
        text = body.text.strip()
        if not text:
            raise engine.EngineError("empty_prompt", "write something to improve first")
        what = {"image": "a still image (subject, setting, framing, lens, light, colour)",
                "video": "a short video clip (subject, action, camera move, light; one continuous shot)",
                "music": "a song for a music model (genre, tempo, instruments, voice, mood, as comma-separated tags)"}.get(body.kind, "an image")
        cast = ""
        if body.project:
            # the mentioned cast as designed, so the rewrite does not invent a face on a faceless design
            _, matched, _ = engine._scan_mentions(store, body.project, text)
            lines = []
            for m in matched:
                look = str(m.get("prompt") or "").strip()
                avoid = str(m.get("negative") or "").strip()
                if look or avoid:
                    lines.append(f"- @{m['name']}: {look or 'as in its reference'}" + (f" (it must NOT have: {avoid})" if avoid else ""))
            if lines:
                cast = ("\n\nThe @names are designed like this; describe them only with these traits and never add "
                        "features they lack:\n" + "\n".join(lines))
        messages = [{"role": "system", "content": "You rewrite prompts for local image, video and music models. Answer with the "
                                                  "improved prompt only, in English, one paragraph, no quotes, no preamble."},
                    {"role": "user", "content": f"Improve this prompt for {what}. Keep every name written as @Name and every "
                                                f"<imageN> tag exactly as it is; keep the idea, add concrete visual detail "
                                                f"about the scene, camera and light.{cast}\n\n{text}"}]
        # a rewrite needs no reasoning: a thinking model would spend the budget thinking and answer nothing
        out = studio.chat(messages, 600, 0.6, effort="off").strip().strip('"')
        if not out:
            raise engine.EngineError("empty_answer", "the local model gave no answer: try again or use another model")
        return {"text": out}

    @app.post("/api/prompt/enhance")
    def enhance_prompt(body: EnhancePromptBody):
        return op_enhance_prompt(body)

    def _apply_space_ops(graph: dict[str, Any], ops: list[dict[str, Any]]) -> dict[str, Any]:
        """Agent edits of a graph: add_node {id?, type, x?, y?, data?},
        set {id, data} (merged), move {id, x, y}, connect {source,
        source_handle?, target, target_handle}, disconnect {source, target,
        target_handle?}, remove {id}."""
        nodes = {n["id"]: n for n in graph.get("nodes") or []}
        edges = list(graph.get("edges") or [])
        for i, op in enumerate(ops):
            kind = op.get("op")
            if kind == "add_node":
                nid = str(op.get("id") or f"{op.get('type', 'node')}{len(nodes) + 1}")
                while nid in nodes:
                    nid += "x"
                nodes[nid] = {"id": nid, "type": op.get("type"), "x": float(op.get("x") or 80 * len(nodes)),
                              "y": float(op.get("y") or 0), "data": dict(op.get("data") or {})}
            elif kind == "set":
                n = nodes.get(str(op.get("id")))
                if not n:
                    raise spaces_mod.SpaceError("no_node", f"op {i}: no node {op.get('id')}")
                n["data"] = {**(n.get("data") or {}), **dict(op.get("data") or {})}
            elif kind == "move":
                n = nodes.get(str(op.get("id")))
                if not n:
                    raise spaces_mod.SpaceError("no_node", f"op {i}: no node {op.get('id')}")
                n["x"], n["y"] = float(op.get("x") or 0), float(op.get("y") or 0)
            elif kind == "connect":
                edges.append({"id": f"e{len(edges)}-{op.get('source')}-{op.get('target')}", "source": op.get("source"),
                              "source_handle": op.get("source_handle"), "target": op.get("target"),
                              "target_handle": op.get("target_handle")})
            elif kind == "disconnect":
                edges = [e for e in edges if not (e["source"] == op.get("source") and e["target"] == op.get("target")
                                                  and (not op.get("target_handle") or e["target_handle"] == op.get("target_handle")))]
            elif kind == "remove":
                nid = str(op.get("id"))
                nodes.pop(nid, None)
                edges = [e for e in edges if nid not in (e["source"], e["target"])]
            else:
                raise spaces_mod.SpaceError("bad_op", f"op {i}: use add_node, set, move, connect, disconnect or remove")
        return spaces_mod.validate_graph({"nodes": list(nodes.values()), "edges": edges, "viewport": graph.get("viewport")})

    @app.post("/api/agent/studio_spaces")
    def agent_spaces(project: str, body: SpaceAgentBody):
        def run():
            if body.action == "list":
                return list_spaces(project)
            if body.action == "create":
                return space_view(store.create_space(project, body.name or "Space", spaces_mod.template_graph(body.template)),
                                  compact=True)
            if body.action == "import":
                return op_space_import(project, body.bundle, body.name, body.space)
            if not body.space:
                raise spaces_mod.SpaceError("space_required", "pass the space id (action=list shows them)")
            space = store.get_space(body.space)
            if space["project_id"] != project:
                raise spaces_mod.SpaceError("wrong_project", "that space belongs to another project")
            if body.action == "get":
                return space_view(space, compact=True)
            if body.action == "edit":
                graph = _apply_space_ops(space["graph"], body.ops)
                return space_view(store.save_space_graph(space["id"], graph, None, body.name), compact=True)
            if body.action == "run":
                return op_space_run(space["id"], SpaceRunBody(mode=body.mode, node_ids=body.node_ids, force=body.force))
            if body.action == "stop":
                return op_space_stop(space["id"])
            if body.action == "estimate":
                return op_space_estimate(space["id"], body.mode if body.mode != "node" or body.node_ids else "all",
                                         body.node_ids, body.force)
            if body.action == "build":
                return op_space_build(space["id"], body.request or "")
            if body.action == "app":
                return op_space_app(space["id"])
            if body.action == "app_run":
                return op_space_app_run(space["id"], body.values)
            if body.action == "export":
                return spaces_mod.export_technique(store, space, body.group)
            if body.action == "delete":
                return space_view(store.delete_space(space["id"]), compact=True)
            if body.action == "restore":
                return space_view(store.restore_space(space["id"]), compact=True)
            raise spaces_mod.SpaceError("bad_action", "actions: list, get, create, import, edit, run, stop, estimate, build, "
                                                      "app, app_run, export, delete, restore")
        return agent("studio_spaces", f"{body.action}:{body.space or body.name or ''}", run)

    @app.post("/api/agent/studio_prompt_enhance")
    def agent_prompt_enhance(body: EnhancePromptBody):
        return agent("studio_prompt_enhance", body.kind, lambda: op_enhance_prompt(body))

    def _job_state(job_id: str) -> Optional[str]:
        try:
            return store.get_job(job_id)["state"]
        except NotFound:
            return None

    # a production left "running" by a run that died (app closed, a failed
    # write) is shown and edited as failed instead of staying locked
    productions_mod.set_job_probe(_job_state)

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

    def production_view(slug: str) -> dict[str, Any]:
        return productions_mod.compact_view(productions_mod.load_fresh(store.data_dir, slug))

    def op_production_create(body: ProductionCreateBody) -> dict[str, Any]:
        if body.project:
            store.get_project(body.project)
        state = productions_mod.create_production(store.data_dir, body.name, body.spec, body.settings, project_id=body.project)
        productions_mod.adopt_lead(store, state)
        job = queue_production(state["slug"])
        return {"production": production_view(state["slug"]), "job": engine.job_view(job)}

    def _plan_lead(character_id: Optional[str], name: Optional[str], look: Optional[str]) -> dict[str, Any]:
        if character_id:
            char = store.get_character(character_id)
            return {"character_id": char["id"], "name": char["name"], "look": char.get("prompt") or ""}
        if not (name or "").strip() or not (look or "").strip():
            raise ValueError("pick a lead from the cast (character_id) or give lead_name and lead_look")
        return {"name": name.strip(), "look": look.strip()}

    def _renders_on_llm_gpus() -> str:
        """Which ComfyUI servers are rendering on a GPU the language model
        also uses: the usual reason it stops answering on this machine."""
        mem = backend.memory()
        services = mem.get("services") or []
        llm_gpus = {g for s in services if s.get("kind") in ("command", "ollama") for g in (s.get("gpus") or [])}
        notes = []
        for s in services:
            sid = str(s.get("id") or "")
            if not sid.startswith("comfyui@"):
                continue
            try:
                q = httpx.get(f"http://127.0.0.1:{sid.split('@', 1)[1]}/queue", timeout=2.0).json()
            except (httpx.HTTPError, ValueError):
                continue
            if not q.get("queue_running"):
                continue
            shared = sorted(set(s.get("gpus") or []) & llm_gpus)
            notes.append(f"ComfyUI :{sid.split('@', 1)[1]} is rendering"
                         + (f" on GPU {', '.join(str(g) for g in shared)}, which the language model shares" if shared else ""))
        return ("; ".join(notes) + ".") if notes else ""

    def _keep_bad_plan_reply(text: str) -> None:
        """The last reply the planner could not read, to see what the model did."""
        try:
            path = store.data_dir / "logs" / "plan_last_bad_reply.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(text or ""), encoding="utf-8")
        except OSError:
            pass

    def op_video_plan(body: VideoPlanBody) -> dict[str, Any]:
        lead = _plan_lead(body.character_id, body.lead_name, body.lead_look)
        chat = ((getattr(app.state, "short_hooks", None) or {}).get("chat")
                or mv_planner.writer_chat(backend, busy=_renders_on_llm_gpus))
        project = body.project or (store.get_character(body.character_id)["project_id"] if body.character_id else None)
        elements = [{"name": c["name"], "element": c["element"], "look": c.get("prompt") or ""}
                    for c in (store.list_characters(project) if project else []) if c["element"] != "character"]
        draft = mv_planner.plan(chat, concept=body.concept, lead_name=lead["name"], lead_look=lead.get("look") or "",
                                n_shots=body.shots, language=body.language, compose_song=not body.song_asset_id,
                                duration_s=body.duration_s, lyrics=body.lyrics, genre=body.genre,
                                on_bad_reply=_keep_bad_plan_reply, elements=elements)
        if body.critic:
            review = mv_planner.critique(chat, draft, concept=body.concept, lead_name=lead["name"],
                                         lead_look=lead.get("look") or "", notes_language=body.notes_language)
            if review.get("revised"):
                draft["first_shots"] = draft["shots"]
                draft["shots"] = review.pop("shots")
            draft["critique"] = review
        return {"draft": draft, "lead": lead, **({"elements": [e["name"] for e in elements]} if elements else {})}

    def op_video_from_plan(body: VideoFromPlanBody) -> dict[str, Any]:
        lead = _plan_lead(body.character_id, body.lead_name, body.lead_look)
        spec = mv_planner.spec_from_draft(body.draft, lead=lead, song_asset_id=body.song_asset_id, clips=body.clips,
                                          aspects=body.aspects, song_takes=body.song_takes, engine=body.engine,
                                          brief=body.brief, lyrics=body.lyrics)
        project = body.project
        if not project and body.character_id:
            project = store.get_character(body.character_id)["project_id"]
        settings = dict(body.settings or {})
        if not body.song_asset_id and body.song_takes > 1:
            settings.setdefault("song_review", True)  # listen to the takes and pick one before the stills
        return op_production_create(ProductionCreateBody(name=body.name, spec=spec, settings=settings, project=project))

    def op_production_continue(slug: str, take: Optional[int] = None) -> dict[str, Any]:
        with productions_mod.lock_for(slug):
            state = productions_mod.load_state(store.data_dir, slug)
            if take is not None:
                song = (state.get("spec") or {}).get("song") or {}
                takes = ((state.get("partial") or {}).get("song") or {}).get("song_asset_ids") or []
                if not song or not 1 <= int(take) <= max(1, len(takes) or int(song.get("count") or 1)):
                    raise ValueError(f"take must be between 1 and {max(1, len(takes))}")
                song["take"] = int(take)
            if state.get("status") == "awaiting_review":
                what = ("script_approved" if state.get("stage") == "script" else
                        "song_approved" if state.get("stage") == "song" else "animatic_approved")
                state.setdefault("review", {})[what] = True
                productions_mod.log(state, "review", "approved", what=what)
                productions_mod.save_state(store.data_dir, state)
        job = queue_production(slug)
        return {"production": production_view(slug), "job": engine.job_view(job)}

    def op_production_shots(slug: str, body: ProductionShotsBody) -> dict[str, Any]:
        for change in body.changes:
            refs = (change.get("refs") if isinstance(change, dict) else None) or []
            if isinstance(change, dict) and isinstance(change.get("insert"), dict):
                refs = list(refs) + list(change["insert"].get("refs") or [])
            for r in refs:
                aid = r.get("asset_id") if isinstance(r, dict) else r
                if store.get_asset(str(aid))["kind"] != "image":
                    raise engine.EngineError("bad_reference", f"reference {aid} is not an image (take frames out of a "
                                                              "video or GIF first)")
        for change in body.changes:
            if not isinstance(change, dict):
                continue
            motions = [change.get("motion_ref")] + ([change["insert"].get("motion_ref")] if isinstance(change.get("insert"), dict) else [])
            for m in motions:
                if isinstance(m, dict) and m.get("asset_id") and store.get_asset(str(m["asset_id"]))["kind"] != "video":
                    raise engine.EngineError("bad_motion_ref", f"motion reference {m['asset_id']} is not a video")
        for change in body.changes:
            # a retake chosen as a take joins its clip's history first
            if isinstance(change, dict) and change.get("take") and change.get("key") is not None:
                try:
                    a = store.get_asset(str(change["take"]))
                except NotFound:
                    continue
                r = a.get("recipe") or {}
                src = (r.get("retake_of") or r.get("edit_of")) if a["kind"] == "video" else None
                if src:
                    with productions_mod.lock_for(slug):
                        state = productions_mod.load_state(store.data_dir, slug)
                        for ck, takes in productions_mod.shot_takes(state, str(change["key"]))["clips"].items():
                            if any(t["asset_id"] == src for t in takes):
                                productions_mod.remember_take(state, "clips", ck, {"asset_id": a["id"], "retake_of": src,
                                                                                 "quality": (a.get("recipe") or {}).get("quality") or "final"})
                                productions_mod.save_state(store.data_dir, state)
                                break
        result = productions_mod.update_shots(store.data_dir, slug, body.changes)
        if body.run and result["changed"]:
            result["job"] = engine.job_view(queue_production(slug))
        return {**result, "production": production_view(slug)}

    def op_recipe_run(name: str, body: RecipeRunBody) -> dict[str, Any]:
        cast = dict(body.cast)
        lead = cast.get("lead")
        lib_id = lead if isinstance(lead, str) and lead.startswith("lib_") else \
            (lead.get("library") if isinstance(lead, dict) else None)
        if lib_id:
            # a library character: cast it (with its adapters) into the
            # Casting project, then the production copies it as usual
            version = lead.get("version") if isinstance(lead, dict) else None
            cast["lead"] = charpack.library_use(store, backend, _casting_project(), lib_id, version)["character_id"]
        state = recipes_mod.run_recipe(store, name, cast, body.name, body.options)
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

    def op_qa_run(slug: str, body: QaRunBody) -> dict[str, Any]:
        state = productions_mod.load_state(store.data_dir, slug)
        if body.stage != "all" and body.stage not in qa_mod.CHECKED_STAGES:
            raise engine.EngineError("bad_stage", f"stage must be 'all' or one of {', '.join(qa_mod.CHECKED_STAGES)}")
        project_id = state.get("project_id") or (state.get("done", {}).get("1") or {}).get("project_id")
        job = queue.enqueue("production_qa", "cpu", {"slug": slug, "stage": body.stage, "dry_run": body.dry_run,
                                                     "keys": body.keys}, project_id=project_id)
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
            out["animatic"] = {k: outputs.get(k) for k in ("renders", "plan")}
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
        return {"items": productions_mod.list_productions(store.data_dir)}

    @app.post("/api/productions")
    def production_create(body: ProductionCreateBody):
        return op_production_create(body)

    @app.post("/api/productions/plan")
    def production_plan(body: VideoPlanBody):
        return op_video_plan(body)

    @app.post("/api/productions/plan/critique")
    def production_plan_critique(body: VideoCritiqueBody):
        """The director's pass over a plan already shown to the person (the
        app asks for it after the plan, so the shot list is editable at once):
        {issues, revised, shots?} - shots only when it rewrote weak ones."""
        lead = _plan_lead(body.character_id, body.lead_name, body.lead_look)
        chat = ((getattr(app.state, "short_hooks", None) or {}).get("chat")
                or mv_planner.writer_chat(backend, busy=_renders_on_llm_gpus))
        return mv_planner.critique(chat, body.draft, concept=body.concept, lead_name=lead["name"],
                                   lead_look=lead.get("look") or "", notes_language=body.notes_language)

    @app.post("/api/productions/from-plan")
    def production_from_plan(body: VideoFromPlanBody):
        return op_video_from_plan(body)

    @app.post("/api/agent/studio_video_plan")
    def agent_video_plan(body: VideoPlanBody):
        return agent("studio_video_plan", body.concept[:120], lambda: op_video_plan(body))

    @app.post("/api/agent/studio_video_from_plan")
    def agent_video_from_plan(body: VideoFromPlanBody):
        return agent("studio_video_from_plan", body.name[:80], lambda: op_video_from_plan(body))

    @app.get("/api/productions/{slug}")
    def production_get(slug: str):
        state = productions_mod.load_fresh(store.data_dir, slug)
        timing = None if productions_mod.is_legacy(state) else productions_mod.shot_timing(store.data_dir, store, state)
        return {**state, "view": productions_mod.compact_view(state), "timing": timing}

    @app.put("/api/productions/{slug}/segments")
    def production_segments(slug: str, body: ProductionSegmentsBody):
        return productions_mod.set_script_segments(store.data_dir, slug, text=body.text, segments=body.segments)

    @app.post("/api/agent/studio_production_segments")
    def agent_production_segments(production: str, body: ProductionSegmentsBody):
        def run():
            result = productions_mod.set_script_segments(store.data_dir, production, text=body.text, segments=body.segments)
            return {"production": result["slug"], "count": len(result["segments"]),
                    "timed": result["timed"], "retimed": result["retimed"]}
        return agent("studio_production_segments", production, run)

    @app.put("/api/productions/{slug}/lyrics")
    def production_lyrics(slug: str, body: ProductionLyricsBody):
        out = productions_mod.set_song_lyrics(store.data_dir, slug, body.lyrics)
        if body.run:
            out["job"] = engine.job_view(queue_production(slug))
        return {**out, "production": production_view(slug)}

    def op_production_song(slug: str, body: ProductionSongBody) -> dict[str, Any]:
        out = productions_mod.set_song(store, slug, asset_id=body.asset_id, take=body.take, compose=body.compose,
                                       lyrics=body.lyrics)
        if body.time_lyrics and out.get("song_asset_id"):
            try:
                out["timed"] = productions_mod.time_lyrics_now(store, slug)
            except (engine.EngineError, productions_mod.ProductionError) as exc:
                out["timing_note"] = getattr(exc, "message", None) or str(exc)
        if body.run:
            out["job"] = engine.job_view(queue_production(slug))
        return {**out, "production": production_view(slug)}

    def op_production_settings(slug: str, body: ProductionSettingsBody) -> dict[str, Any]:
        patch: dict[str, Any] = {k: v for k, v in body.model_dump().items() if v is not None and k != "autopilot"}
        if body.autopilot is not None:
            patch["animatic_autocontinue"] = bool(body.autopilot)
            patch["song_review"] = not body.autopilot
        settings = productions_mod.update_settings(store.data_dir, slug, patch)
        return {"production": slug, "settings": settings,
                "autopilot": bool(settings.get("animatic_autocontinue")) and not settings.get("song_review")}

    @app.patch("/api/productions/{slug}/settings")
    def production_settings(slug: str, body: ProductionSettingsBody):
        return op_production_settings(slug, body)

    def op_production_finishing(slug: str, body: ProductionFinishingBody) -> dict[str, Any]:
        out = productions_mod.set_finishing(store, slug, body.finishing)
        if body.render and out["rerender"]:
            out["job"] = engine.job_view(queue_production(slug))
        return {**out, "production": slug}

    def op_canvas(slug: str, body: CanvasBody) -> dict[str, Any]:
        from . import canvas as canvas_mod
        out = canvas_mod.make_canvas(store, slug, body.seconds, body.start_s, body.lyrics)
        return {**out, "production": slug, "download": f"/api/assets/{out['asset_id']}/file?download=true"}

    @app.post("/api/productions/{slug}/canvas")
    def production_canvas(slug: str, body: CanvasBody):
        return op_canvas(slug, body)

    @app.post("/api/agent/studio_canvas")
    def agent_canvas(production: str, body: CanvasBody):
        return agent("studio_canvas", production, lambda: op_canvas(production, body))

    @app.patch("/api/productions/{slug}/finishing")
    def production_finishing(slug: str, body: ProductionFinishingBody):
        return op_production_finishing(slug, body)

    @app.post("/api/agent/studio_production_finishing")
    def agent_production_finishing(production: str, body: ProductionFinishingBody):
        return agent("studio_production_finishing", production, lambda: op_production_finishing(production, body))

    @app.post("/api/agent/studio_production_settings")
    def agent_production_settings(production: str, body: ProductionSettingsBody):
        return agent("studio_production_settings", production, lambda: op_production_settings(production, body))

    def op_reframe(body: ReframeBody) -> dict[str, Any]:
        asset = store.get_asset(body.asset_id)
        if asset["kind"] not in ("image", "video"):
            raise engine.EngineError("not_visual", "only pictures and clips can be reframed")
        if body.aspect not in engine.REFRAME_ASPECTS:
            raise engine.EngineError("bad_aspect", f"aspect must be one of {', '.join(engine.REFRAME_ASPECTS)}")
        if body.framing not in engine.video_mod.FRAMINGS:
            raise engine.EngineError("bad_framing", f"framing must be one of {', '.join(engine.video_mod.FRAMINGS)}")
        for v in (body.focus_x, body.focus_y):
            if v is not None and not 0.0 <= v <= 1.0:
                raise engine.EngineError("bad_focus", "focus_x and focus_y go from 0 (left/top) to 1 (right/bottom)")
        pid = body.project or asset["project_id"]
        job = queue.enqueue("reframe", "cpu", {**body.model_dump(exclude={"wait_s", "project"}), "project_id": pid},
                            project_id=pid)
        if body.wait_s:
            job = queue.wait_for(job["id"], body.wait_s)
        return {"job": engine.job_view(job)}

    @app.post("/api/assets/{asset_id}/reframe")
    def asset_reframe(asset_id: str, body: ReframeBody):
        return op_reframe(body.model_copy(update={"asset_id": asset_id}))

    @app.post("/api/agent/studio_reframe")
    def agent_reframe(body: ReframeBody):
        return agent("studio_reframe", body.asset_id, lambda: op_reframe(body))

    def op_interpolate(body: InterpolateBody) -> dict[str, Any]:
        clip = store.get_asset(body.asset_id)
        interpolate_mod.source_plan(store, clip, body.fps)
        job = queue.enqueue("interpolate", "cpu", body.model_dump(exclude={"wait_s"}), project_id=clip["project_id"])
        if body.wait_s:
            job = queue.wait_for(job["id"], body.wait_s)
        return {"job": engine.job_view(job)}

    @app.post("/api/assets/{asset_id}/interpolate")
    def asset_interpolate(asset_id: str, body: InterpolateBody):
        return op_interpolate(body.model_copy(update={"asset_id": asset_id}))

    @app.post("/api/agent/studio_interpolate")
    def agent_interpolate(body: InterpolateBody):
        return agent("studio_interpolate", body.asset_id, lambda: op_interpolate(body))

    def op_retake(body: RetakeBody) -> dict[str, Any]:
        clip = store.get_asset(body.asset_id)
        if clip["kind"] != "video":
            raise engine.EngineError("not_video", "a retake redoes part of a clip")
        if body.quality not in ("draft", "final"):
            raise engine.EngineError("bad_quality", "quality is draft or final")
        if not 0 <= body.start_s < body.end_s:
            raise engine.EngineError("bad_range", "start_s must be before end_s")
        engine.retake_plan(float(clip.get("duration_s") or body.end_s), body.start_s, body.end_s)
        info = engine._object_info(backend) or {}
        if info and not engine.retake_installed(info)[body.quality]:
            raise engine.EngineError("no_vace", ("a draft retake needs wan2.1_vace_1.3B_fp16" if body.quality == "draft" else
                                                 "a final retake needs both wan2.2_fun_vace 14B experts and the t2v 4-step LoRAs")
                                     + " in ComfyUI/models")
        job = queue.enqueue("retake", "gpu", {**body.model_dump(exclude={"wait_s"})}, project_id=clip["project_id"])
        if body.wait_s:
            job = queue.wait_for(job["id"], body.wait_s)
        return {"job": engine.job_view(job)}

    @app.post("/api/assets/{asset_id}/retake")
    def asset_retake(asset_id: str, body: RetakeBody):
        return op_retake(body.model_copy(update={"asset_id": asset_id}))

    @app.post("/api/agent/studio_retake")
    def agent_retake(body: RetakeBody):
        return agent("studio_retake", body.asset_id, lambda: op_retake(body))

    def _edit_frames(clip: dict[str, Any], start_s: float, n: int = 3) -> list[bytes]:
        """JPEG frames of the window a clip edit will touch, for the vision model."""
        exe = ffmpeg_path()
        if not exe:
            return []
        src = store.data_dir / clip["file_path"]
        duration = float(clip.get("duration_s") or 0) or 5.0
        end = min(duration, start_s + 5.0)
        out: list[bytes] = []
        for i in range(n):
            t = start_s + (end - start_s) * (i + 0.5) / n
            res = procutil.run([exe, "-nostdin", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                                "-vf", "scale=512:-2", "-f", "image2", "-c:v", "mjpeg", "-"], timeout=60)
            if res.returncode == 0 and res.stdout:
                out.append(bytes(res.stdout))
        return out

    def _edit_vision():
        """(chat with images) through the family's vision model, or None.
        Tests set app.state.edit_vision to a callable(messages, images)."""
        override = getattr(app.state, "edit_vision", None)
        if override is not None:
            return override or None
        try:
            if not backend.link.sync.resolve("vision").resolved:
                return None
        except Exception:  # noqa: BLE001 - no resolver: text only
            return None
        return lambda messages, images: backend.link.sync.chat(messages=messages, images=images, capability="vision",
                                                               max_tokens=700, temperature=0.3, effort="off").text

    def op_clip_edit_prompt(body: ClipEditBody) -> dict[str, Any]:
        """What a clip edit will send: the task, the references in order and
        the renderer's text (the instruction rewritten unless `exact`)."""
        clip = store.get_asset(body.asset_id)
        if clip["kind"] != "video":
            raise engine.EngineError("not_video", "only a clip can be edited this way")
        explicit = [r for r in body.reference_asset_ids if r][:clip_edit_mod.MAX_REFERENCES]
        for rid in explicit + ([body.first_frame_asset_id] if body.first_frame_asset_id else []):
            if store.get_asset(rid)["kind"] != "image":
                raise engine.EngineError("reference_not_image", f"reference {rid} is not a picture")
        offset = len(explicit) + (1 if body.first_frame_asset_id else 0)
        pieces, _, unknown = engine._scan_mentions(store, clip["project_id"], body.prompt or "")
        text, cast = clip_edit_mod.name_references(pieces, offset)
        refs = explicit + [c["canonical_asset_id"] for c in cast]
        try:
            task = clip_edit_mod.resolve_mode(body.mode, len(refs), bool(body.first_frame_asset_id))
        except clip_edit_mod.ClipEditError as exc:
            raise engine.EngineError(exc.code, exc.message) from None
        enhanced, how, detail = False, "written", None   # how: written | propagation | language | vision | no_model
        if task == "vi2v":
            text, how = clip_edit_mod.PROPAGATE_PROMPT, "propagation"
        elif not text.strip():
            raise engine.EngineError("empty_prompt", "say what to change in the clip")
        elif body.enhance and not body.exact:
            recipe = clip.get("recipe") or {}
            scene = str((recipe.get("params") or {}).get("positive_prompt") or recipe.get("prompt") or "")
            vision = _edit_vision()
            images = _edit_frames(clip, body.start_s) if vision else []
            if vision and images:
                images += [(store.data_dir / store.get_asset(r)["file_path"]).read_bytes() for r in refs]
            msgs = clip_edit_mod.enhance_messages(task, text, scene, len(refs), with_frames=bool(vision and images))
            try:
                answer = vision(msgs, images) if (vision and images) else studio.chat(msgs, 700, 0.3, effort="off")
                new = clip_edit_mod.clean_rewrite(answer, text)
                if new != text:
                    text, enhanced, how = new, True, ("vision" if (vision and images) else "language")
            except Exception as exc:  # noqa: BLE001 - no model answered: the instruction as written
                how, detail = "no_model", str(exc)[:160]
        return {"asset_id": clip["id"], "task": task, "prompt": text, "enhanced": enhanced, "how": how, "how_detail": detail,
                "reference_asset_ids": refs, "cast": [c["name"] for c in cast], "unknown_mentions": unknown,
                "first_frame_asset_id": body.first_frame_asset_id}

    def op_clip_edit(body: ClipEditBody) -> dict[str, Any]:
        if body.quality not in ("draft", "final"):
            raise engine.EngineError("bad_quality", "quality is draft or final")
        clip = store.get_asset(body.asset_id)
        if clip["kind"] != "video":
            raise engine.EngineError("not_video", "only a clip can be edited this way")
        try:
            clip_edit_mod.window(float(clip.get("duration_s") or 5.0), body.start_s)
        except clip_edit_mod.ClipEditError as exc:
            raise engine.EngineError(exc.code, exc.message) from None
        info = engine._object_info(backend) or {}
        if info and not engine.clip_edit_installed(info)[body.quality]:
            raise engine.EngineError("no_bernini", ("a draft edit needs wan2.1_bernini_1.3B_fp16" if body.quality == "draft" else
                                                    "a final edit needs both wan2.2_bernini_r experts and the "
                                                    "lightx2v_T2V_14B_cfg_step_distill_v2 LoRA") + " in ComfyUI/models")
        plan = op_clip_edit_prompt(body)
        params = {"asset_id": clip["id"], "prompt": plan["prompt"], "task": plan["task"], "instruction": body.prompt,
                  "enhanced": plan["enhanced"], "reference_asset_ids": [r for r in plan["reference_asset_ids"]],
                  "first_frame_asset_id": body.first_frame_asset_id, "quality": body.quality, "start_s": body.start_s,
                  "seed": body.seed, "negative": body.negative}
        job = queue.enqueue("clip_edit", "gpu", params, project_id=clip["project_id"])
        if body.wait_s:
            job = queue.wait_for(job["id"], body.wait_s)
        return {"job": engine.job_view(job), "plan": plan}

    @app.post("/api/assets/{asset_id}/clip-edit/prompt")
    def asset_clip_edit_prompt(asset_id: str, body: ClipEditBody):
        return op_clip_edit_prompt(body.model_copy(update={"asset_id": asset_id}))

    @app.post("/api/assets/{asset_id}/clip-edit")
    def asset_clip_edit(asset_id: str, body: ClipEditBody):
        return op_clip_edit(body.model_copy(update={"asset_id": asset_id}))

    @app.post("/api/agent/studio_clip_edit")
    def agent_clip_edit(body: ClipEditBody, preview: bool = False):
        return agent("studio_clip_edit", body.asset_id,
                     lambda: op_clip_edit_prompt(body) if preview else op_clip_edit(body))

    def op_stems(body: StemsBody) -> dict[str, Any]:
        from . import stems as stems_mod
        asset = store.get_asset(body.asset_id)
        if asset["kind"] not in ("audio", "video"):
            raise engine.EngineError("not_audio", "stems come out of a song (or a video's sound)")
        have = stems_mod.existing(store, asset["id"])
        if not body.force and all(k in have for k in stems_mod.STEMS):
            if "instrumental" not in have:  # split before the instrumental existed
                try:
                    have["instrumental"] = stems_mod.mix_instrumental(store, asset, have)
                except stems_mod.StemsError as exc:
                    raise engine.EngineError(exc.code, exc.message) from None
            return {"stems": have, "reused": True}
        if body.device and not re.fullmatch(r"cpu|cuda:\d", body.device):
            raise engine.EngineError("bad_device", "device is cpu or cuda:N")
        job = queue.enqueue("stems", "cpu", {"asset_id": asset["id"], "force": body.force, "device": body.device},
                            project_id=asset["project_id"])
        if body.wait_s:
            job = queue.wait_for(job["id"], body.wait_s)
        return {"job": engine.job_view(job), "stems": have}

    @app.get("/api/assets/{asset_id}/stems")
    def asset_stems(asset_id: str):
        from . import stems as stems_mod
        store.get_asset(asset_id)
        return {"stems": stems_mod.existing(store, asset_id)}

    @app.post("/api/assets/{asset_id}/stems")
    def asset_stems_make(asset_id: str, body: StemsBody):
        return op_stems(body.model_copy(update={"asset_id": asset_id}))

    @app.post("/api/agent/studio_stems")
    def agent_stems(body: StemsBody):
        return agent("studio_stems", body.asset_id, lambda: op_stems(body))

    def op_production_reframe(slug: str, body: ProductionReframeBody) -> dict[str, Any]:
        out = productions_mod.reframe(store, slug, body.aspects, body.framing)
        if body.run and out["rerender"]:
            out["job"] = engine.job_view(queue_production(slug))
        return {**out, "production": production_view(slug)}

    @app.post("/api/productions/{slug}/reframe")
    def production_reframe(slug: str, body: ProductionReframeBody):
        return op_production_reframe(slug, body)

    @app.post("/api/agent/studio_production_reframe")
    def agent_production_reframe(production: str, body: ProductionReframeBody):
        return agent("studio_production_reframe", production, lambda: op_production_reframe(production, body))

    def op_production_takes(slug: str, key: Optional[str] = None) -> dict[str, Any]:
        state = productions_mod.load_state(store.data_dir, slug)
        keys = [key] if key else [s["key"] for s in (state.get("spec") or {}).get("shots") or []]
        known = {s["key"] for s in (state.get("spec") or {}).get("shots") or []}
        if key and key not in known:
            raise productions_mod.ProductionError("bad_changes", f"unknown shot key '{key}'")
        shots = [productions_mod.shot_takes(state, k) for k in keys]
        for sh in shots:
            # retakes of a clip (studio_retake) are takes of its shot too
            for ck, takes in sh["clips"].items():
                seen = {t["asset_id"] for t in takes}
                for t in list(takes):
                    for a in _retakes_of(state, t["asset_id"]):
                        if a["id"] not in seen:
                            seen.add(a["id"])
                            edit = (a.get("recipe") or {}).get("operation") == "clip_edit"
                            takes.append({"asset_id": a["id"], "quality": (a.get("recipe") or {}).get("quality"),
                                          ("edit" if edit else "retake"): True, "current": False})
        return {"production": slug, "shots": shots,
                "hint": "put one back with studio_production_shots([{\"key\": K, \"take\": asset_id}])"}

    def _retakes_of(state: dict[str, Any], clip_id: str) -> list[dict[str, Any]]:
        pid = state.get("project_id")
        if not pid:
            return []
        items = store.list_assets(pid, kind="video", query=clip_id, limit=20)["items"]
        return [a for a in items if ((a.get("recipe") or {}).get("operation") == "retake"
                                     and (a.get("recipe") or {}).get("retake_of") == clip_id)
                or ((a.get("recipe") or {}).get("operation") == "clip_edit"
                    and (a.get("recipe") or {}).get("edit_of") == clip_id)]

    @app.get("/api/productions/{slug}/takes")
    def production_takes(slug: str, key: Optional[str] = None):
        return op_production_takes(slug, key)

    @app.post("/api/agent/studio_production_takes")
    def agent_production_takes(production: str, key: Optional[str] = None):
        return agent("studio_production_takes", production, lambda: op_production_takes(production, key))

    def op_production_regenerate(slug: str, body: ProductionRegenerateBody) -> dict[str, Any]:
        out = productions_mod.regenerate_unlocked(store.data_dir, slug, body.stage, body.keys)
        if body.run and (out["regenerated"] or out["chained"]):
            out["job"] = engine.job_view(queue_production(slug))
        return {**out, "production": production_view(slug)}

    @app.post("/api/productions/{slug}/regenerate")
    def production_regenerate(slug: str, body: ProductionRegenerateBody):
        return op_production_regenerate(slug, body)

    @app.post("/api/agent/studio_production_regenerate")
    def agent_production_regenerate(production: str, body: ProductionRegenerateBody):
        return agent("studio_production_regenerate", production, lambda: op_production_regenerate(production, body))

    def op_production_promote(slug: str, body: ProductionPromoteBody) -> dict[str, Any]:
        out = productions_mod.promote_clips(store.data_dir, slug, body.keys)
        if body.run and (out["promoted"] or out["chained"]):
            out["job"] = engine.job_view(queue_production(slug))
        return {**out, "production": production_view(slug)}

    @app.post("/api/productions/{slug}/promote")
    def production_promote(slug: str, body: ProductionPromoteBody):
        return op_production_promote(slug, body)

    @app.post("/api/agent/studio_production_promote")
    def agent_production_promote(production: str, body: ProductionPromoteBody):
        return agent("studio_production_promote", production, lambda: op_production_promote(production, body))

    def production_preflight(slug: str) -> dict[str, Any]:
        """What would stop the production's next run, before it starts:
        ComfyUI down (and not started by itself), no music model for a song
        still to compose, no ffmpeg, the GPUs held by another model."""
        state = productions_mod.load_fresh(store.data_dir, slug)
        stages = productions_mod.stages_for(state)
        pending = [s for s in stages if productions_mod.stage_status(state, s) != "done"]
        items: list[dict[str, Any]] = []
        if not pending:
            return {"ok": True, "items": []}
        status = backend.status()
        comfy_needed = any(s in pending for s in ("character", "song", "frames", "clips", "photocards", "visuals", "music"))
        if comfy_needed and not status["comfy"].get("reachable"):
            svc = next((i for i in status["services"]["items"] if i.get("role") == "main" or i.get("kind") == "comfyui"), None)
            startable = bool(svc and svc.get("startable"))
            if status["services"].get("autostart_comfy") and startable:
                items.append({"level": "info", "code": "comfy_autostart",
                              "message": "ComfyUI is not running; it starts by itself when the run needs it (about a minute)"})
            else:
                items.append({"level": "error", "code": "comfy_down", "startable": startable,
                              "message": "ComfyUI is not running" + ("" if startable else
                                                                    f": {(svc or {}).get('problem') or 'set its folder in Backends'}")})
        song = (state.get("spec") or {}).get("song") or {}
        if "song" in pending and state.get("kind") != "short" and not song.get("asset_id") and status["comfy"].get("reachable"):
            if not any(m.get("available") for m in status.get("music") or []):
                items.append({"level": "error", "code": "no_music_model",
                              "message": "no music model is installed in ComfyUI to compose the song: pick a song of yours (Change song)"})
        singing = [sh["key"] for sh in (state.get("spec") or {}).get("shots") or [] if sh.get("sing")]
        if singing and "clips" in pending and status["comfy"].get("reachable"):
            if not engine.s2v_installed(engine._object_info(backend) or {}):
                items.append({"level": "error", "code": "no_s2v",
                              "message": f"shot {', '.join(singing)} sings, but the lip-sync model (Wan 2.2 S2V, its audio encoder "
                                         "and 4-step LoRA) is not installed in ComfyUI"})
            unplaced = [sh["key"] for sh in state["spec"]["shots"] if sh.get("sing") and not productions_mod.shot_span(sh)]
            if unplaced:
                items.append({"level": "error", "code": "sing_needs_span",
                              "message": f"shot {', '.join(unplaced)} sings but is not placed on its lines in the song track"})
        if not status["ffmpeg"]["found"]:
            items.append({"level": "error", "code": "no_ffmpeg", "message": "ffmpeg was not found: the animatic and the cut need it"})
        try:
            mem = backend.memory()
            need = int((mem.get("vram_estimates_mb") or {}).get("wan" if "clips" in pending and "frames" not in pending else "qwen21") or 0)
            gpus = mem.get("gpus") or []
            if gpus and need and max(int(g.get("free_mb") or 0) for g in gpus) < need:
                holders = sorted({str(x.get("label") or x.get("id")) for x in mem.get("services") or []
                                  if x.get("held_mb") and "comfy" not in str(x.get("id"))})
                items.append({"level": "warn", "code": "gpu_busy",
                              "message": f"no GPU has the ~{need // 1024} GB this needs free"
                                         + (f"; {', '.join(holders)} holds memory (stop it in the header's GPU panel)" if holders else ""),
                              "holders": holders})
        except Exception:  # noqa: BLE001 - the memory view is a hint, never a blocker
            pass
        return {"ok": not any(i["level"] == "error" for i in items), "items": items}

    @app.get("/api/productions/{slug}/preflight")
    def production_preflight_route(slug: str):
        return production_preflight(slug)

    @app.put("/api/productions/{slug}/song")
    def production_song(slug: str, body: ProductionSongBody):
        return op_production_song(slug, body)

    @app.post("/api/agent/studio_production_song")
    def agent_production_song(production: str, body: ProductionSongBody):
        return agent("studio_production_song", production, lambda: op_production_song(production, body))

    @app.post("/api/productions/{slug}/time-lyrics")
    def production_time_lyrics(slug: str):
        return {**productions_mod.time_lyrics_now(store, slug), "production": production_view(slug)}

    @app.get("/api/agent/studio_production_timing")
    def agent_production_timing(production: str):
        def run() -> dict[str, Any]:
            state = productions_mod.load_fresh(store.data_dir, production)
            timing = productions_mod.shot_timing(store.data_dir, store, state)
            return {"production": production, "duration_s": timing.get("duration_s"), "timed": timing.get("timed"),
                    "song_asset_id": timing.get("song_asset_id"), "lines": timing.get("lines") or [],
                    "sections": [{k: v for k, v in sec.items() if k != "lines"} for sec in timing.get("sections") or []],
                    "spans": timing.get("spans") or {},
                    "shots": {k: [{"start_s": c["start_s"], "duration_s": c["duration_s"]} for c in v]
                              for k, v in (timing.get("shots") or {}).items()}}
        return agent("studio_production_timing", production, run)

    @app.put("/api/productions/{slug}/cast")
    def production_cast(slug: str, body: ProductionCastBody):
        for member in body.cast:
            aid = str(member.get("asset_id") or "") if isinstance(member, dict) else ""
            if aid and store.get_asset(aid)["kind"] != "image":
                raise engine.EngineError("bad_cast", f"cast member {aid} is not an image (take a frame out of a video first)")
        out = productions_mod.set_cast(store.data_dir, slug, body.cast, body.per_shot)
        if body.run and out["redraw"]:
            out["job"] = engine.job_view(queue_production(slug))
        return {**out, "production": production_view(slug)}

    def op_download(project_id: str, body: DownloadMediaBody) -> dict[str, Any]:
        store.get_project(project_id)
        url = media_download.check_url(body.url)
        for v in (body.start_s, body.end_s):
            if v is not None and not 0 <= v <= 6 * 3600:
                raise engine.EngineError("bad_range", "start_s/end_s are seconds into the video")
        job = queue.enqueue("download_media", "cpu", {"url": url, "audio_only": body.audio_only, "start_s": body.start_s,
                                                       "end_s": body.end_s}, project_id=project_id)
        return {"job": engine.job_view(job)}

    @app.post("/api/projects/{project_id}/download")
    def project_download(project_id: str, body: DownloadMediaBody):
        return op_download(project_id, body)

    @app.post("/api/agent/studio_download_media")
    def agent_download_media(project: str, body: DownloadMediaBody):
        return agent("studio_download_media", body.url[:120], lambda: op_download(project, body))

    @app.post("/api/assets/{asset_id}/frames")
    def asset_frames(asset_id: str, count: int = 6, at_s: Optional[float] = None):
        if at_s is not None:
            return {"items": [engine.frame_at(store, asset_id, at_s)]}
        return {"items": engine.extract_frames(store, asset_id, count)}

    @app.post("/api/agent/studio_video_frames")
    def agent_video_frames(body: VideoFramesBody):
        if body.at_s is not None:
            return agent("studio_video_frames", body.asset_id,
                         lambda: {"items": [engine.frame_at(store, body.asset_id, float(body.at_s))]})
        return agent("studio_video_frames", body.asset_id, lambda: {"items": engine.extract_frames(store, body.asset_id,
                                                                                                    body.count)})

    @app.post("/api/productions/{slug}/continue")
    def production_continue(slug: str, take: Optional[int] = None):
        return op_production_continue(slug, take)

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
        return recipes_mod.recipe_summary(recipes_mod.export_recipe(store, slug, body.name))

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
        return agent("studio_productions", "", lambda: {"items": productions_mod.list_productions(store.data_dir)[:30]})

    @app.get("/api/agent/studio_production")
    def agent_production(production: str):
        return agent("studio_production", production, lambda: production_view(production))

    @app.post("/api/agent/studio_production_create")
    def agent_production_create(body: ProductionCreateBody):
        return agent("studio_production_create", body.name[:80], lambda: op_production_create(body))

    @app.post("/api/agent/studio_production_continue")
    def agent_production_continue(production: str, take: Optional[int] = None):
        return agent("studio_production_continue", production, lambda: op_production_continue(production, take))

    @app.post("/api/agent/studio_production_shots")
    def agent_production_shots(production: str, body: ProductionShotsBody):
        return agent("studio_production_shots", production, lambda: op_production_shots(production, body))

    # ------------------------------------------------------ narrated shorts
    def op_short_create(body: ShortCreateBody) -> dict[str, Any]:
        spec = dict(body.options or {})
        if body.topic:
            spec["topic"] = body.topic
        if body.script is not None:
            spec["script"] = body.script
        if body.project:
            store.get_project(body.project)
        name = (body.name or spec.get("title") or spec.get("topic") or "Short").strip()[:80] or "Short"
        states = shorts_mod.create_batch(store.data_dir, name, spec, body.settings, project_id=body.project, count=body.count)
        out = []
        for state in states:
            job = queue_production(state["slug"])
            out.append({"production": production_view(state["slug"]), "job": engine.job_view(job)})
        return out[0] if len(out) == 1 else {"items": out}

    def op_short_script(slug: str, body: ShortScriptBody) -> dict[str, Any]:
        if body.script is None:
            state = productions_mod.load_state(store.data_dir, slug)
            if not shorts_mod.is_short(state):
                raise engine.EngineError("not_a_short", f"'{slug}' is not a narrated short")
            script = (state.get("done") or {}).get("script") or state["spec"].get("script")
            return {"production": slug, "script": script}
        shorts_mod.replace_script(store.data_dir, slug, body.script)
        out: dict[str, Any] = {"production": production_view(slug)}
        if body.run:
            out["job"] = engine.job_view(queue_production(slug))
        return out

    @app.post("/api/agent/studio_short_create")
    def agent_short_create(body: ShortCreateBody):
        return agent("studio_short_create", (body.topic or body.name or "script")[:80], lambda: op_short_create(body))

    @app.post("/api/shorts")
    def short_create(body: ShortCreateBody):
        return op_short_create(body)

    @app.post("/api/agent/studio_production_script")
    def agent_short_script(body: ShortScriptBody):
        if not body.production:
            raise engine.EngineError("production_required", "give the short's production slug (studio_productions)")
        return agent("studio_production_script", body.production, lambda: op_short_script(body.production, body))

    @app.put("/api/productions/{slug}/script")
    def short_script_put(slug: str, body: ShortScriptBody):
        return op_short_script(slug, body)

    @app.get("/api/productions/{slug}/publish")
    def short_publish(slug: str):
        state = productions_mod.load_state(store.data_dir, slug)
        if not shorts_mod.is_short(state):
            raise engine.EngineError("not_a_short", f"'{slug}' is not a narrated short")
        return Response(shorts_mod.publish_text(store, state), media_type="text/plain; charset=utf-8")

    # ---------------------------------------------------------- stock
    _stock_cache: dict[str, dict[str, Any]] = {}

    def op_stock_search(body: StockSearchBody) -> dict[str, Any]:
        keys = studio.stock_keys()
        client = studio.stock_client()
        orientation = body.orientation or stock_mod.aspect_orientation(body.aspect)
        out: dict[str, Any] = {}
        if body.refs:
            if not body.project:
                raise engine.EngineError("project_required", "importing stock needs a project")
            missing = [r for r in body.refs if r not in _stock_cache]
            if missing:
                raise engine.EngineError("unknown_ref", f"search first; unknown ref(s): {', '.join(missing[:5])}")
            items = [_stock_cache[r] for r in body.refs[:10]]
        else:
            found = stock_mod.search(keys, body.query, body.kind, orientation, body.providers, body.per_page, body.page,
                                     body.min_duration_s, client=client)
            for it in found["items"]:
                _stock_cache[it["ref"]] = it
            while len(_stock_cache) > 600:
                _stock_cache.pop(next(iter(_stock_cache)))
            out = {"query": found["query"], "providers": found["providers"], "errors": found["errors"] or None,
                   "items": [stock_mod.compact_item(it) for it in found["items"]]}
            items = found["items"][:max(0, min(10, body.take))] if body.project else []
        if items:
            imported = [engine.asset_view(stock_mod.fetch(store, body.project, it, query=body.query, client=client)) for it in items]
            out["imported"] = imported
        return out

    @app.post("/api/agent/studio_stock_search")
    def agent_stock_search(body: StockSearchBody):
        return agent("studio_stock_search", body.query[:80], lambda: op_stock_search(body))

    @app.post("/api/stock/search")
    def stock_search(body: StockSearchBody):
        return op_stock_search(body)

    @app.get("/api/backend/stock")
    def stock_status():
        return stock_mod.status(studio.stock_keys())

    @app.put("/api/backend/stock")
    def stock_set(body: StockKeysBody):
        try:
            backend.set_stock_keys(body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise engine.EngineError("bad_stock_keys", str(exc)) from None
        return stock_mod.status(backend.stock_keys())

    @app.post("/api/agent/studio_recipe_export")
    def agent_recipe_export(body: RecipeExportBody):
        def run():
            recipe = recipes_mod.export_recipe(store, body.production, body.name)
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

    @app.get("/api/agent/studio_services")
    def agent_services():
        def run():
            svc = backend.services()
            return {
                "items": [{k: v for k, v in {"id": i["id"], "label": i["label"], "state": i["state"], "role": i.get("role"),
                                             "capabilities": i["capabilities"], "url": i["url"], "startable": i["startable"],
                                             "stoppable": i["stoppable"], "started_by": i["started_by"], "gpu": i["gpu"],
                                             "problem": i["problem"]}.items() if v not in (None, [], "")}
                          for i in svc["items"]],
                "gpus": [{"index": g["index"], "name": g["name"], "free_mb": g["free_mb"]} for g in svc["gpus"]],
                "autostart_comfy": svc["autostart_comfy"],
            }
        return agent("studio_services", "", run)

    @app.get("/api/agent/studio_gpu_memory")
    def agent_gpu_memory():
        def run():
            mem = backend.memory()
            return {
                "gpus": [{"index": g["index"], "name": g["name"], "free_mb": g["free_mb"], "total_mb": g["total_mb"],
                          "held_by": g["services"], "other_processes": g["others"]} for g in mem["gpus"]],
                "servers": [{k: v for k, v in {"id": x["id"], "label": x["label"], "gpus": x["gpus"],
                                               "models": [m.get("name") for m in x["models"]] or None,
                                               "held_mb": x["held_mb"], "stoppable": x["stoppable"]}.items() if v not in (None, [])}
                            for x in mem["services"]],
                "vram_needed_mb": {k: v for k, v in mem["vram_estimates_mb"].items() if k in ("qwen21", "flux", "sdxl", "wan", "ace")},
            }
        return agent("studio_gpu_memory", "", run)

    @app.post("/api/agent/studio_service_start")
    def agent_service_start(body: ServiceBody):
        wait_s = min(max(body.wait_s, 0.0), MAX_WAIT_S)
        return agent("studio_service_start", body.id,
                     lambda: backend.start_service(body.id, gpu=_gpu_arg(body.gpu), wait_s=wait_s))

    @app.post("/api/agent/studio_service_stop")
    def agent_service_stop(body: ServiceBody):
        return agent("studio_service_stop", body.id, lambda: backend.stop_service(body.id))

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
                "services": [{"id": i["id"], "state": i["state"], "startable": i["startable"]}
                             for i in status["services"]["items"]],
                "autostart_comfy": status["services"]["autostart_comfy"],
                "image_engine": {"available": list(engine.IMAGE_ENGINES), "auto_resolves_to": _auto_image_engine()},
                "ffmpeg": status["ffmpeg"]["found"],
                "piper_tts": status["piper"]["installed"],
                "music_generation": [{"name": m["name"], "available": m["available"], "reason": m["reason"]} for m in status["music"]],
                "vram_estimates_mb": status["vram_estimates_mb"],
                "queue": counts,
                "recent_jobs": [engine.job_view(j) for j in store.list_jobs(limit=5)["items"]],
            }
        return agent("studio_status", "", run)

    # ------------------------------------------------------------ family
    # the shared agent contract (/api/agent/tools and /api/agent/call), the four tools other apps call and the family settings;
    # the two shared routes go to the front of the router, so the catch-all routes below never swallow them
    family_api.install(app, family_api.Context(
        store=store, backend=backend, settings=family_settings, agent=agent, error_payload=error_payload, export_lookup=_export_lookup,
        resolve_timeline=lambda **kw: _export_timeline_id(ExportTimelineBody(**kw)), casting_project=_casting_project,
        tts_engines=_tts_engines, production_view=production_view, mcp_source=Path(__file__).with_name("mcp_server.py")))

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

    queue.start()
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
        "id": c["id"], "name": c["name"], "element": None if c.get("element") in (None, "character") else c["element"],
        "role": c.get("role"), "prompt": engine._clip(c.get("prompt"), 240),
        "negative": engine._clip(c.get("negative"), 160), "palette": c.get("palette") or None,
        "canonical_asset_id": c.get("canonical_asset_id"), "voice": c.get("voice"), "bio": engine._clip(c.get("bio"), 200),
        "adapters": [f"{a['arch']}{'' if a.get('enabled') else ' (off)'}" for a in (c.get("kit") or {}).get("adapters") or []]
        or None,
        "library": (c.get("kit") or {}).get("library"),
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
