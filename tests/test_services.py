"""Standalone backends: Prospero starts ComfyUI (and the rest) without Faustus."""

from __future__ import annotations

import json
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prosperos_hoard.api import create_app
from prosperos_hoard.backend import Backend
from prosperos_hoard.hoard_link import _hubclient

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_comfy_install(root: Path) -> Path:
    """A folder that looks like a ComfyUI install; its main.py serves the
    test suite's fake ComfyUI on --port."""
    folder = root / "ComfyUI"
    (folder / "comfy").mkdir(parents=True)
    (folder / "comfy" / "cli_args.py").write_text('parser.add_argument("--disable-auto-launch")\n', encoding="utf-8")
    (folder / "main.py").write_text(textwrap.dedent(f"""
        import argparse, sys, time
        from pathlib import Path
        sys.path.insert(0, {str(REPO)!r})
        from prosperos_hoard.devtools.fake_comfy import FakeComfyServer
        ap = argparse.ArgumentParser()
        ap.add_argument("--port", type=int)
        args, _ = ap.parse_known_args()
        FakeComfyServer(Path("store")).run_in_thread(args.port)
        while True:
            time.sleep(1)
    """), encoding="utf-8")
    return folder


@pytest.fixture
def standalone(data_dir: Path, tmp_path: Path):
    """An app whose ComfyUI is off, with a startable install configured."""
    port = _free_port()
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": f"http://127.0.0.1:{port}"}}), encoding="utf-8")
    folder = _fake_comfy_install(tmp_path)
    app = create_app(data_dir, port=8815)
    with TestClient(app, base_url="http://127.0.0.1:8815") as c:
        r = c.put("/api/backend/launch", json={"comfyui_dir": str(folder), "comfyui_python": sys.executable})
        assert r.status_code == 200, r.text
        yield c, app, port
        c.post("/api/backend/services/stop", json={"id": "comfyui"})
    app.state.shutdown()


def test_services_list_main_pool_and_ollama(data_dir):
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": "http://127.0.0.1:8290"},
                                                       "render_pool": ["http://127.0.0.1:8291", "http://10.0.0.5:8188"]}),
                                           encoding="utf-8")
    svc = Backend(data_dir).services()
    by_id = {i["id"]: i for i in svc["items"]}
    assert by_id["comfyui@8290"]["role"] == "main" and by_id["comfyui@8291"]["role"] == "render_pool"
    assert "ollama" in by_id and svc["main_comfy"] == "comfyui@8290"
    assert all(not i["id"].endswith("@8188") for i in svc["items"])  # the remote pool server is not ours to start
    assert svc["autostart_comfy"] is True


def test_remote_main_comfy_cannot_be_started(data_dir):
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": "http://192.168.1.9:8188"}}), encoding="utf-8")
    b = Backend(data_dir)
    assert b.services()["main_comfy"] is None
    with pytest.raises(ValueError, match="remote"):
        b.start_service("comfyui")


def test_start_with_integer_gpu_uses_hub_and_does_not_retry_a_refusal(client, monkeypatch):
    c, _, _ = client
    monkeypatch.setattr(_hubclient, "hub_up", lambda url: True)
    monkeypatch.setattr(_hubclient, "hub_url", lambda: "http://hub.test")
    calls = []

    def fetch(url, body, **options):
        calls.append((url, body))
        return 409, {"ok": False, "error": "GPU memory is unavailable"}

    monkeypatch.setattr(_hubclient, "fetch", fetch)
    r = c.post("/api/agent/studio_service_start", json={"id": "comfyui@62829", "gpu": 1, "wait_s": 10})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "GPU memory is unavailable" in r.json()["error"]
    assert calls == [("http://hub.test/api/services/start", {"id": "comfyui@62829", "gpu": 1, "wait_s": 10})]


def test_demo_never_starts_anything(data_dir):
    b = Backend(data_dir, demo=True)
    assert b.autostart_comfy() is False
    with pytest.raises(ValueError, match="demo"):
        b.start_service("comfyui")


def test_start_passes_manual_reservation_and_gpu_job_names_the_local_service(client, monkeypatch):
    from prosperos_hoard import gpu_lease

    c, app, _ = client
    backend = app.state.backend
    monkeypatch.setattr(backend, "_comfy_service", lambda action, sid, **options: {
        "ok": True, "service": sid, **options,
    })
    r = c.post("/api/agent/studio_service_start", json={"id": "comfyui@62829", "lease_id": "manual"})
    assert r.json()["lease_id"] == "manual"
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")
    monkeypatch.setattr(backend, "comfy_gpu", lambda: 2)
    monkeypatch.setattr(backend, "comfy_resident_mb", lambda gpu: 0)
    backend.bind_comfy("http://127.0.0.1:62829")
    lease = gpu_lease.RenderLease("comfyui@62829", vram_mb=11887, gpu=2)
    monkeypatch.setattr(lease, "acquire", lambda: lease)
    monkeypatch.setattr(lease, "release", lambda: None)
    captured = []
    monkeypatch.setattr(gpu_lease, "default_lease", lambda **args: captured.append(args) or lease)
    try:
        with app.state.queue._guard({"id": "render", "lane": "gpu", "type": "generate_image",
                                     "params": {"engine": "qwen21"}}):
            assert captured[0]["service"] == lease._body()["service"] == "comfyui@62829"
            assert captured[0]["gpu"] == 2
    finally:
        backend.bind_comfy(None)


def test_launch_settings_are_validated_and_shared(client, tmp_path):
    c, _, _ = client
    bad = c.put("/api/backend/launch", json={"comfyui_dir": str(tmp_path)})
    assert bad.status_code == 400 and "not a ComfyUI folder" in bad.json()["message"]
    notpy = tmp_path / "evil.exe"
    notpy.write_text("", encoding="utf-8")
    assert c.put("/api/backend/launch", json={"comfyui_python": str(notpy)}).status_code == 400
    assert c.put("/api/backend/launch", json={"ollama_exe": str(notpy)}).status_code == 400
    folder = _fake_comfy_install(tmp_path)
    r = c.put("/api/backend/launch", json={"comfyui_dir": str(folder), "comfyui_gpu": "2", "autostart_comfy": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["comfyui"]["dir"] == str(folder) and body["comfyui"]["gpu"] == 2 and body["autostart_comfy"] is False
    shared = json.loads((tmp_path / "hoard-home" / "backends.json").read_text(encoding="utf-8"))
    assert shared["comfyui"]["dir"] == str(folder)  # family-wide, not only Prospero's
    assert c.put("/api/backend/launch", json={"comfyui_gpu": "fast"}).status_code == 400


def test_start_and_stop_comfyui_from_the_studio(standalone):
    c, _, port = standalone
    before = {i["id"]: i for i in c.get("/api/backend/services").json()["items"]}
    assert before[f"comfyui@{port}"]["state"] == "down" and before[f"comfyui@{port}"]["startable"]
    r = c.post("/api/backend/services/start", json={"id": "comfyui", "wait_s": 60})
    assert r.status_code == 200 and r.json()["ready"], r.text
    status = c.get("/api/backend").json()
    assert status["comfy"]["reachable"] is True, status["comfy"]
    item = next(i for i in status["services"]["items"] if i["id"] == f"comfyui@{port}")
    assert item["state"] == "running" and item["started_by"] == "prospero" and item["stoppable"]
    agent = c.get("/api/agent/studio_services").json()
    assert any(i["id"] == f"comfyui@{port}" and i["state"] == "running" for i in agent["items"])
    stop = c.post("/api/agent/studio_service_stop", json={"id": "comfyui"})
    assert stop.status_code == 200 and stop.json()["ok"], stop.text
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        items = {i["id"]: i for i in c.get("/api/backend/services").json()["items"]}
        if items[f"comfyui@{port}"]["state"] == "down":
            break
        time.sleep(0.5)
    assert items[f"comfyui@{port}"]["state"] == "down"


def test_a_job_starts_comfyui_by_itself(standalone):
    c, _, port = standalone
    project = c.post("/api/agent/studio_create_project", json={"name": "Auto"}).json()["id"]
    r = c.post(f"/api/agent/studio_generate_image?project={project}",
               json={"prompt": "a paper lantern at night", "template": "sdxl_txt2img", "wait_s": 120, "seed": 3})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["state"] == "done", r.json()["job"]
    item = next(i for i in c.get("/api/backend/services").json()["items"] if i["id"] == f"comfyui@{port}")
    assert item["state"] == "running" and item["started_by"] == "prospero"


def test_autostart_off_fails_with_the_reason(standalone):
    c, _, _ = standalone
    c.put("/api/backend/launch", json={"autostart_comfy": False})
    project = c.post("/api/agent/studio_create_project", json={"name": "Manual"}).json()["id"]
    r = c.post(f"/api/agent/studio_generate_image?project={project}",
               json={"prompt": "x", "template": "sdxl_txt2img", "wait_s": 30})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "image_unavailable" and "Local services" in r.json()["message"]
