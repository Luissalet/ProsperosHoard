from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prosperos_hoard.api import create_app
from prosperos_hoard.backend import Backend
from prosperos_hoard.devtools.fake_comfy import FakeComfyServer
from prosperos_hoard.store import Store


@pytest.fixture(autouse=True)
def _private_hoard_home(tmp_path: Path, monkeypatch):
    """The launcher's family-wide files (~/.hoard/backends.json, the pids it
    started) go to a temp folder in every test, never the real one."""
    monkeypatch.setenv("HOARD_HOME", str(tmp_path / "hoard-home"))
    monkeypatch.delenv("COMFYUI_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_family_hub(monkeypatch):
    """No test talks to a real hub (events, notices, GPU lease, tool calls): the family is switched off unless a test fakes it."""
    from prosperos_hoard.hoard_link import fam_notify, family

    monkeypatch.setenv("HOARD_EVENTS", "0")
    monkeypatch.setenv("HOARD_HUB_AUTOSTART", "0")
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "0")
    monkeypatch.setattr(fam_notify, "hub_available", lambda timeout=1.0: False)
    monkeypatch.setattr(fam_notify, "notify", lambda *a, **k: {"ok": False, "error": "hub unreachable"})
    monkeypatch.setattr(family, "call", lambda app, tool, arguments=None, **k: {"ok": False, "app": app, "tool": tool, "status": None,
                                                                              "error": "hub not reachable"})


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def fake_comfy(data_dir: Path):
    server = FakeComfyServer(data_dir / "fake_comfy")
    port = server.run_in_thread()
    yield server, port
    server.stop()


@pytest.fixture
def backend_with_comfy(data_dir: Path, fake_comfy) -> Backend:
    _, port = fake_comfy
    (data_dir / "backend.json").write_text('{"comfy": {"url": "http://127.0.0.1:%d"}}' % port, encoding="utf-8")
    return Backend(data_dir)


@pytest.fixture
def store(data_dir: Path) -> Store:
    return Store(data_dir)


@pytest.fixture
def project(store: Store):
    return store.create_project("Test Project", brief="a test project")


@pytest.fixture
def client(data_dir, fake_comfy, tmp_path):
    """A full FastAPI app over the fake ComfyUI backend, for the handful of
    tests that need real HTTP request/response shapes (validation errors,
    static file serving, ...) rather than calling `engine.*` directly.
    Yields `(TestClient, app, allowed_import_dir)`."""
    _, port = fake_comfy
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": f"http://127.0.0.1:{port}"},
                                                       "import_roots": [str(tmp_path / "allowed")]}), encoding="utf-8")
    (tmp_path / "allowed").mkdir()
    static = tmp_path / "dist"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    app = create_app(data_dir, static_dir=static, port=8815)
    with TestClient(app, base_url="http://127.0.0.1:8815") as c:
        yield c, app, tmp_path / "allowed"
    app.state.queue.stop()
