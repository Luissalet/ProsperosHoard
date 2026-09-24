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
            by_name = {t.name: t for t in tools.tools}
            expected = {"studio_status", "studio_projects", "studio_create_project", "studio_cast", "studio_generate_image",
                        "studio_edit_image", "studio_animate", "studio_voice", "studio_compose", "studio_import",
                        "studio_analyze_audio", "studio_time_lyrics", "studio_design", "studio_photocard_set", "studio_timeline", "studio_render",
                        "studio_jobs", "studio_job", "studio_cancel_job", "studio_assets", "studio_show", "studio_lineage",
                        "voice_engines", "voice_create", "voice_list", "voice_speak", "voice_transcribe",
                        "voice_audiobook", "voice_dub", "voice_resynthesize_segment", "voice_job",
                        "studio_productions", "studio_production", "studio_production_create", "studio_production_continue",
                        "studio_production_shots", "studio_recipe_export", "studio_recipes_list", "studio_recipe_get",
                        "studio_recipe_run", "studio_qa_run", "studio_qa_report"}
            assert expected <= set(by_name)
            for t in tools.tools:
                assert "Keywords:" in (t.description or ""), t.name
                assert t.annotations is not None and t.annotations.destructiveHint is False
            assert by_name["studio_show"].annotations.readOnlyHint is True
            for name in ("studio_productions", "studio_production", "studio_recipes_list", "studio_recipe_get", "studio_qa_report"):
                assert by_name[name].annotations.readOnlyHint is True, name
            for t in tools.tools:
                if t.name.startswith(("studio_production", "studio_recipe", "studio_qa")):
                    assert len((t.description or "").splitlines()[0]) <= 110, t.name
            assert by_name["studio_generate_image"].annotations.readOnlyHint is False
            assert by_name["studio_voice"].annotations.openWorldHint is True  # first use downloads a voice

            result = await session.call_tool("studio_status", {})
            status = json.loads(result.content[0].text)
            assert status["comfyui"]["reachable"] is True
            # the fake backend now serves a real ComfyUI /object_info (patched with the
            # models the production example uses), so ComfyMusic (ACE-Step) resolves.
            assert status["music_generation"][0]["name"] == "comfy_music"
            assert status["music_generation"][0]["available"] is True

            result = await session.call_tool("studio_recipes_list", {})
            assert json.loads(result.content[0].text) == {"items": []}
            result = await session.call_tool("studio_production", {"production": "nope"})
            assert result.isError and "not_found" in result.content[0].text

            result = await session.call_tool("studio_create_project", {"name": "MCP Test"})
            project_id = json.loads(result.content[0].text)["id"]
            await session.call_tool("studio_cast", {"project": project_id, "action": "create", "kind": "character",
                                                    "name": "Iris Volt", "fields": {"prompt": "platinum hair idol"}})

            result = await session.call_tool(
                "studio_generate_image",
                {"project": project_id, "prompt": "@Iris Volt on a rooftop", "count": 1, "wait_s": 20, "seed": 11},
            )
            gen = json.loads(result.content[0].text)
            assert gen["job"]["state"] == "done"
            assert "platinum hair idol" in gen["final_prompt"]
            # include_image defaults to false: a text-only model must not get an ImageContent block
            assert not any(type(c).__name__ == "ImageContent" for c in result.content)
            asset_id = gen["job"]["asset_ids"][0]
            assert len(result.content[0].text) < 4000  # compact

            result = await session.call_tool(
                "studio_generate_image",
                {"project": project_id, "prompt": "@Iris Volt on a rooftop", "count": 1, "wait_s": 20,
                 "seed": 11, "include_image": True},
            )
            assert any(type(c).__name__ == "ImageContent" for c in result.content)  # asked for explicitly

            result = await session.call_tool("studio_show", {"asset_ids": [asset_id]})
            images = [c for c in result.content if type(c).__name__ == "ImageContent"]
            assert images and len(images[0].data) * 3 / 4 < 210_000

            result = await session.call_tool("studio_lineage", {"asset_id": asset_id})
            lineage = json.loads(result.content[0].text)
            assert lineage["recipe"]["operation"] == "generate_image"
            assert lineage["recipe"]["params"]["seed"] == 11

            result = await session.call_tool("studio_design", {"project": project_id, "template": "thumbnail",
                                                               "fields": {"title": "Afterglow"}, "image_asset_id": asset_id})
            assert json.loads(result.content[0].text)["kind"] == "image"
            assert not any(type(c).__name__ == "ImageContent" for c in result.content)  # include_image default false

            result = await session.call_tool("studio_design", {"project": project_id, "template": "thumbnail",
                                                               "fields": {"title": "Afterglow"}, "image_asset_id": asset_id,
                                                               "include_image": True})
            assert any(type(c).__name__ == "ImageContent" for c in result.content)

            result = await session.call_tool("studio_compose", {
                "project": project_id, "tags": "dark trap, horror rap, heavy 808, half-time 140 bpm",
                "lyrics": "[Verse]\nCuenta las farolas, una, dos.\n", "bpm": 140, "duration": 5,
                "key": "F# minor", "language": "es", "seed": 31, "wait_s": 20,
            })
            song = json.loads(result.content[0].text)
            assert song["job"]["state"] == "done"
            song_asset_id = song["job"]["asset_ids"][0]
            result = await session.call_tool("studio_lineage", {"asset_id": song_asset_id})
            assert json.loads(result.content[0].text)["recipe"]["template"] == "ace15_song"

            result = await session.call_tool("studio_time_lyrics", {
                "project": project_id, "song_asset_id": song_asset_id,
                "lyrics": "[Verse]\nCuenta las farolas, una, dos.\n[Chorus]\nNo mires atras.\n",
            })
            timed = json.loads(result.content[0].text)
            assert timed["lines"] == 2 and [s["label"] for s in timed["sections"]] == ["Verse", "Chorus"]
            assert timed["sections"][1]["energy"] == "high" and "by ear" in timed["note"]

            result = await session.call_tool("studio_job", {"job_id": "job_does_not_exist"})
            assert result.isError is True
            assert "not_found" in result.content[0].text

            result = await session.call_tool("studio_design", {"project": project_id, "template": "thumbnail", "fields": {"titel": "x"}})
            assert result.isError is True and "unknown_field" in result.content[0].text and "title" in result.content[0].text

    calls = app.state.store.list_agent_calls(50)
    assert {c["tool"] for c in calls} >= {"studio_status", "studio_generate_image", "studio_show", "studio_design"}


@pytest.mark.asyncio
async def test_mcp_voice_tools_end_to_end(running_app, monkeypatch):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from prosperos_hoard import voice_engines as ve

    class FakeTTS(ve.TTSEngine):
        id = "fake-tts"
        capabilities = ve.EngineCapabilities(languages=["en"], cloning=False)

        def is_installed(self):
            return True

        def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
            import numpy as np

            n = max(1, len(text)) * 100
            return ve.wav_bytes_mono16((np.sin(np.linspace(0, 10, n)) * 0.2).astype("float32"), 16000)

    class FakeSTT(ve.STTEngine):
        id = "fake-stt"
        capabilities = ve.EngineCapabilities()

        def is_installed(self):
            return True

        def transcribe(self, path, language=None, word_timestamps=True):
            return {"language": "en", "text": "hello there",
                    "segments": [{"start_s": 0.0, "end_s": 1.0, "text": "hello there", "words": []}]}

    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [FakeTTS()])
    monkeypatch.setattr("prosperos_hoard.api.ve.default_stt_engines", lambda: [FakeSTT()])

    url, app = running_app
    params = StdioServerParameters(
        command=sys.executable, args=[str(REPO_ROOT / "prosperos_hoard" / "mcp_server.py")],
        env={**os.environ, "PROSPERO_URL": url},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool("voice_engines", {})
            data = json.loads(result.content[0].text)
            assert any(e["id"] == "fake-tts" for e in data["tts"])
            assert any(e["id"] == "fake-stt" for e in data["stt"])

            result = await session.call_tool("studio_create_project", {"name": "Voice MCP Test"})
            project_id = json.loads(result.content[0].text)["id"]

            result = await session.call_tool("voice_speak", {"text": "hello there", "engine_id": "fake-tts",
                                                              "project": project_id})
            spoken = json.loads(result.content[0].text)
            assert spoken["engine_id"] == "fake-tts"
            assert spoken["kind"] == "audio"
            asset_id = spoken["id"]

            result = await session.call_tool("voice_transcribe", {"asset_id": asset_id})
            transcript = json.loads(result.content[0].text)
            assert transcript["text"] == "hello there"

            result = await session.call_tool("voice_audiobook", {
                "text": "Hello world. This is a narrated test.", "engine_id": "fake-tts", "wait_s": 30,
            })
            book = json.loads(result.content[0].text)
            assert book["job"]["state"] == "done"

            result = await session.call_tool("voice_job", {"job_id": book["job"]["id"]})
            assert json.loads(result.content[0].text)["state"] == "done"

            result = await session.call_tool("voice_create", {"name": "should fail", "engine_id": "fake-tts",
                                                               "source_path": "/no/such/file.wav"})
            assert result.isError is True


@pytest.mark.asyncio
async def test_mcp_reports_app_not_running(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable, args=[str(REPO_ROOT / "prosperos_hoard" / "mcp_server.py")],
        env={**os.environ, "PROSPERO_URL": f"http://127.0.0.1:{_free_port()}"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("studio_projects", {})
            assert result.isError is True
            assert "prosperos-hoard_unavailable: Prospero's Hoard is not running" in result.content[0].text
            assert "Iniciar Prospero's Hoard.cmd" in result.content[0].text


def test_mcp_server_refuses_non_loopback_url(monkeypatch):
    import importlib

    sys.modules.pop("prosperos_hoard.mcp_server", None)
    monkeypatch.setenv("PROSPERO_URL", "http://example.com:8815")
    with pytest.raises(RuntimeError):
        importlib.import_module("prosperos_hoard.mcp_server")

    sys.modules.pop("prosperos_hoard.mcp_server", None)
    monkeypatch.setenv("PROSPERO_URL", "http://127.0.0.1:8815")
    importlib.import_module("prosperos_hoard.mcp_server")
