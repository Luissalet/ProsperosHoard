"""Shared HTML motion arguments for REST and MCP."""
from typing import Annotated
from pydantic import BaseModel, Field


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
