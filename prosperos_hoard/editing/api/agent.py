"""/api/agent/* — the bridge used by mcp_server.py (Bearer token from <DATA_DIR>/mcp-token, or an agent token from agent_tokens.json).

The routes come from Hoard Link's ``make_agent_router``: a tool that changes something needs a ``reason``, every such call is kept in
the agent journal (``<DATA_DIR>/agent_journal.jsonl``) and a whole agent session can be taken back with ``POST /api/agent/undo``
(see agent_undo.py). The web interface does not pass through here: it runs the same tools through ``deps.tool`` and is exempt.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from ..agent_tools import AGENT_INSTRUCTIONS, TOOLS, call_tool, tool_catalog
from ..errors import Conflict, LumiereError, Refused
from ..hoard_link.agentkit import AppError, make_agent_router
from .deps import services


def _call(name: str, arguments: dict[str, Any] | None, request: Request) -> Any:
    try:
        return call_tool(services(request), name, arguments, cap=True)
    except Refused as error:
        raise AppError(error.code or "refused", str(error), status=403) from error
    except Conflict as error:
        raise AppError(error.code or "version_conflict", str(error), status=409) from error
    except LumiereError as error:
        raise AppError(error.code or "invalid", str(error), status=400) from error


router = make_agent_router(
    tools_fn=tool_catalog,
    call_fn=_call,
    token_fn=lambda request: services(request).token,
    instructions=AGENT_INSTRUCTIONS,
    app_name="lumiere",
    reasons=True,                                          # an agent says why for every write; the web UI is not an agent and is exempt
    data_dir=lambda request: services(request).config.data_dir,
    tools=TOOLS,
    ctx_fn=lambda request: services(request),
)
