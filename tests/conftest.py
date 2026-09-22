from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from prosperos_hoard.backend import Backend
from prosperos_hoard.devtools.fake_comfy import FakeComfyServer
from prosperos_hoard.store import Store


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
