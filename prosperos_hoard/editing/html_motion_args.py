"""Shared HTML motion arguments for REST and MCP."""
from typing import Annotated, Any, Literal, Optional
from pydantic import BaseModel, Field

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
