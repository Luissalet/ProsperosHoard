"""The proof the plugin works: spawns the real app in a background thread
and drives `mcp_server.py` **through the actual MCP protocol** (stdio
subprocess + `mcp.client.stdio`), not by importing its functions directly.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from prosperos_hoard.api import create_app
from prosperos_hoard.devtools.fake_comfy import FakeComfyServer

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def running_app(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fake = FakeComfyServer(data_dir / "fake_comfy")
    comfy_port = fake.run_in_thread()
    (data_dir / "backend.json").write_text('{"comfy": {"url": "http://127.0.0.1:%d"}}' % comfy_port, encoding="utf-8")

    port = _free_port()
    app = create_app(data_dir, static_dir=None, port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not getattr(server, "started", False) and time.monotonic() < deadline:
        time.sleep(0.05)

    url = f"http://127.0.0.1:{port}"
    yield url, app

    app.state.queue.stop()
    server.should_exit = True
    thread.join(timeout=5)
    fake.stop()


@pytest.mark.asyncio
async def test_mcp_protocol_end_to_end(running_app):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    url, app = running_app

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(REPO_ROOT / "prosperos_hoard" / "mcp_server.py")],
        env={**os.environ, "PROSPERO_URL": url},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "studio_status" in names
            assert "studio_generate_image" in names
            assert "studio_show" in names
            assert len(names) >= 15

            result = await session.call_tool("studio_create_project", {"name": "MCP Test"})
            project = json.loads(result.content[0].text)
            project_id = project["id"]

            result = await session.call_tool(
                "studio_generate_image",
                {"project": project_id, "prompt": "a synthwave skyline", "count": 1, "wait_s": 20},
            )
            gen = json.loads(result.content[0].text)
            assert gen["job"]["state"] == "done"
            asset_id = gen["job"]["outputs"]["asset_ids"][0]

            result = await session.call_tool("studio_show", {"asset_ids": [asset_id]})
            kinds = [type(c).__name__ for c in result.content]
            assert "ImageContent" in kinds

            result = await session.call_tool("studio_lineage", {"asset_id": asset_id})
            lineage = json.loads(result.content[0].text)
            assert lineage["recipe"]["operation"] == "generate_image"

            result = await session.call_tool("studio_job", {"job_id": "job_does_not_exist"})
            assert result.isError is True


def test_mcp_server_refuses_non_loopback_url(monkeypatch):
    import importlib

    sys.modules.pop("prosperos_hoard.mcp_server", None)
    monkeypatch.setenv("PROSPERO_URL", "http://example.com:8815")
    with pytest.raises(RuntimeError):
        importlib.import_module("prosperos_hoard.mcp_server")

    sys.modules.pop("prosperos_hoard.mcp_server", None)
    monkeypatch.setenv("PROSPERO_URL", "http://127.0.0.1:8815")
    importlib.import_module("prosperos_hoard.mcp_server")
