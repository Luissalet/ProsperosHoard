"""/api/family — the contract with the other Hoard apps: which events Lumiere sends and accepts, and the inbox for the ones it accepts."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import family_events
from .deps import services

router = APIRouter(prefix="/api/family")


class EventBody(BaseModel):
    type: str = Field(..., min_length=1, max_length=100)
    source: str = Field("", max_length=80, description="The app that sends it (its id).")
    data: dict[str, Any] = Field(default_factory=dict)


@router.get("/contract")
def contract():
    """The events this app sends and accepts (no secrets: the hub and other apps read it to know what to listen for)."""
    return family_events.contract()


@router.post("/events")
def receive(request: Request, body: EventBody):
    """An event from a sibling app. Same token as the agent route; unknown event types are acknowledged and ignored."""
    svc = services(request)
    header = request.headers.get("authorization", "")
    given = header[7:].strip() if header.startswith("Bearer ") else ""
    if not given or not secrets.compare_digest(given, svc.token):
        raise HTTPException(401, "Invalid MCP token.")
    return family_events.handle_event(svc, body.type, body.source, body.data)
