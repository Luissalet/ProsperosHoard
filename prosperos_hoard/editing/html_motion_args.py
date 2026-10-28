"""Shared HTML motion arguments for REST and MCP."""
from typing import Annotated, Any, Literal, Optional
from pydantic import BaseModel, Field

from .motion_direction import Cue, Treatment
from .motion_sound import SoundEvent, SoundMusic


class HtmlMotionSaveArgs(BaseModel):
    html: str = Field(..., min_length=1, description="Complete HTML source defining window.seek(t), t in seconds. Keep every frame a pure function of time.")
    name: str = Field("HTML motion", min_length=1)
    width: int = Field(1280, gt=0)
    height: int = Field(720, gt=0)
    fps: float = Field(30, gt=0, allow_inf_nan=False)
    duration: float = Field(3, gt=0, allow_inf_nan=False, description="Duration in seconds.")
    composition: str | None = Field(None, pattern=r"^[0-9a-f]{32}$", description="Omit to create; include to save a new revision.")
    expected_revision: int | None = Field(None, ge=1, description="Current revision when editing an existing composition.")
    project_id: str | None = None
    sound: dict[str, Any] | None = Field(None, description="Sound declaration {kit, events:[{t, role}], music:{media|path}, lufs, true_peak_db}. Omitted = "
                                         "the one in the HTML's lumiere-sound block, else the one the composition already had; {} removes it. See motion_sound_events.")


class HtmlMotionRefArgs(BaseModel):
    composition: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    revision: int | None = Field(None, ge=1, description="Omit for the current revision.")


class HtmlMotionGetArgs(HtmlMotionRefArgs):
    offset: int = Field(0, ge=0, description="Character offset into the complete editable source.")
    limit: int = Field(12000, ge=1, le=12000)


class HtmlMotionFrameArgs(HtmlMotionRefArgs):
    at: float = Field(0, ge=0, allow_inf_nan=False, description="Exact composition time in seconds.")
    show: bool = Field(True, description="Include the rendered PNG as an MCP image.")


class HtmlMotionStoryboardArgs(HtmlMotionRefArgs):
    times: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = Field(None, min_length=1,
        description="Exact times in seconds; omit for four evenly spaced moments including start/end.")
    show: bool = Field(True, description="Include the actual contact sheet as one MCP PNG image.")
    mode: Literal["sheet", "strip", "crop-at"] = Field("sheet", description="sheet: the given moments, shrunk. strip: every frame of span at the composition's frame rate "
        "(up to 48, thinned evenly), to see the motion between two moments. crop-at: the given moments cropped around (x, y) or a CSS selector and enlarged, to see detail.")
    span: Optional[list[Annotated[float, Field(ge=0, allow_inf_nan=False)]]] = Field(None, min_length=2, max_length=2, description="strip: [start_s, end_s]; default the whole composition.")
    x: Optional[float] = Field(None, allow_inf_nan=False, description="crop-at: centre x in composition pixels.")
    y: Optional[float] = Field(None, allow_inf_nan=False, description="crop-at: centre y in composition pixels.")
    selector: Optional[str] = Field(None, max_length=300, description="crop-at: a CSS selector to follow instead of a fixed point (its box at each moment).")
    size: Optional[list[Annotated[int, Field(gt=0, le=4096)]]] = Field(None, min_length=2, max_length=2, description="crop-at with (x, y): [width, height] of the crop in composition pixels; default a quarter of the canvas.")
    scale: float = Field(2.0, gt=0, le=8, allow_inf_nan=False, description="crop-at: enlargement of each crop (the tile is at most 640 px wide).")


class HtmlMotionRestoreArgs(BaseModel):
    composition: str = Field(..., pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(..., ge=1, description="The saved revision to restore as a new editable revision.")
    expected_revision: int = Field(..., ge=1)


class HtmlMotionRenderArgs(HtmlMotionRefArgs):
    audio: bool = Field(True, description="With the sound the composition declares (events, music); false exports the picture only.")


class MotionSoundRolesArgs(BaseModel):
    kit: str = Field("launch", description="Kit preset: launch, calm, crisp or minimal.")


class MotionSoundEventsArgs(HtmlMotionRefArgs):
    action: Literal["get", "set", "add", "remove", "clear"] = Field("get", description="get: read the declared sound; set: replace the events; add: append events; "
                                                                   "remove: drop the events at the given times (t) or of the given role; clear: no sound. "
                                                                   "Changing saves a new revision of the composition (same picture).")
    events: list[SoundEvent] | None = Field(None, max_length=400, description="set/add: [{t: seconds, role: tap|golpe|clic|whoosh|hit|pop|key|bell..., gain_db, take, offset_ms, pitch}].")
    times: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = Field(None, description="remove: the times (s) of the events to drop.")
    role: str | None = Field(None, description="remove: drop every event of this role.")
    music: SoundMusic | None = Field(None, description="Music bed: {media: id | path, gain_db, start_s, loop, stop_down}. The music steps down before big hits.")
    clear_music: bool | None = Field(None, description="true drops the music bed and keeps the effects.")
    kit: str | None = None
    lufs: float | None = Field(None, ge=-30, le=-8, allow_inf_nan=False)
    true_peak_db: float | None = Field(None, ge=-6, le=-0.1, allow_inf_nan=False)
    audio: bool | None = Field(None, description="false keeps the declaration but exports without sound.")
    expected_revision: int | None = Field(None, ge=1, description="Current revision (required to change something).")


class MotionSoundPreviewArgs(HtmlMotionRefArgs):
    pass


class AudioSyncPointsArgs(BaseModel):
    media: str | None = Field(None, max_length=40, description="A media id of the library (its sound).")
    path: str | None = Field(None, max_length=2000, description="Or a file the user named (not imported).")
    force: bool = Field(False, description="Measure again even when the library has the points.")


class MotionStylesArgs(BaseModel):
    action: Literal["search", "get"] = Field("search", description="search: find style cards by what they are for; get: read one card (or one of its sections).")
    query: Optional[str] = Field(None, max_length=200, description="search: words about the look, the use or the name; omit to list.")
    source: Optional[Literal["lemo-opuscar", "mg-styles-15"]] = None
    category: Optional[str] = Field(None, max_length=80)
    id: Optional[str] = Field(None, max_length=120, description="get: 'source/slug' or the slug of a card.")
    section: Optional[str] = Field(None, max_length=40, description="get: one section only (essence, materials, color, type, motion, camera, sound, moves, pitfalls, variation...).")
    limit: int = Field(20, ge=1, le=100)


class MotionTreatmentArgs(BaseModel):
    action: Literal["get", "set", "check", "draft"] = Field("get", description="get: read the saved treatment; set: save one (checked); check: verify without saving; "
        "draft: a starting treatment from a card (not saved).")
    composition: Optional[str] = Field(None, pattern=r"^[0-9a-f]{32}$")
    treatment: Optional[Treatment] = Field(None, description="set/check: three structures, the chosen one and why, shots with why each is there, and the cue map.")
    card: Optional[str] = Field(None, max_length=120, description="draft: the style card ('source/slug' or slug).")
    duration: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="draft/check without a composition: the length in seconds.")
    caption: Optional[str] = Field(None, max_length=60, description="draft: the first caption shown.")


class MotionCuesArgs(HtmlMotionRefArgs):
    action: Literal["get", "set", "clear"] = Field("get", description="get: the cues; set: replace them (and derive the sound events from them); clear: no cues.")
    cues: Optional[list[Cue]] = Field(None, max_length=400, description="set: [{t, visual, sound (role or none), gain_db}] - the moments picture and sound hang on.")
    expected_revision: Optional[int] = Field(None, ge=1, description="Current revision (required to change).")


class MotionCuecheckArgs(HtmlMotionRefArgs):
    window_s: float = Field(0.3, gt=0, le=2, allow_inf_nan=False, description="How far from a cue, either side, its picture hit is looked for.")
    scan: Optional[bool] = Field(None, description="Render every frame to find visual hits that have no cue (default: yes for up to 1500 frames).")
    min_change: float = Field(0.3, gt=0, le=50, allow_inf_nan=False, description="Mean grey change between two frames that counts as the picture reacting.")
    retime: bool = Field(False, description="Move cues that are a frame or more away to the frame where the picture really hits (needs expected_revision).")
    expected_revision: Optional[int] = Field(None, ge=1)


class MotionReadcheckArgs(HtmlMotionRefArgs):
    step: Optional[float] = Field(None, gt=0, allow_inf_nan=False, description="Sampling step in seconds (default: every frame, at most 1200 samples).")
    min_s: float = Field(0.0, ge=0, le=30, allow_inf_nan=False, description="Shortest time any text must stay, on top of the reading rule.")


class MotionFactsArgs(HtmlMotionRefArgs):
    action: Literal["get", "set", "check"] = Field("get", description="get: FACTS.md; set: write it; check: every number the video shows must be in it.")
    content: Optional[str] = Field(None, max_length=60000, description="set: the claims, one per line, each with where it comes from. Empty removes the file.")


class MotionFromStyleArgs(BaseModel):
    card: str = Field(..., max_length=120, description="The style card ('source/slug' or slug); find it with motion_styles.")
    name: Optional[str] = Field(None, max_length=120)
    width: int = Field(1280, gt=0)
    height: int = Field(720, gt=0)
    fps: float = Field(30, gt=0, allow_inf_nan=False)
    duration: float = Field(8, gt=0, allow_inf_nan=False)
    treatment: Optional[Treatment] = Field(None, description="Required: three structures, shots with why, the cue map. See motion_treatment.")
    draft_treatment: bool = Field(False, description="Accept a treatment drafted from the card instead of writing one (marked draft).")
    caption: Optional[str] = Field(None, max_length=60)
    project_id: Optional[str] = None
