"""Shared helpers for the API routers."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..agent_tools import call_tool
from ..services import Services


def services(request: Request) -> Services:
    return request.app.state.services


def tool(request: Request, _tool: str, **arguments: Any) -> Any:
    """Run a catalogue tool for the UI: the REST routes and the MCP tools are the same code and cannot disagree."""
    clean = {k: v for k, v in arguments.items() if v is not None}
    return call_tool(services(request), _tool, clean, caller="ui", cap=False)
