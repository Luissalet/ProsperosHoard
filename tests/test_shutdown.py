"""The app stops every thread it starts: nothing survives a shutdown to write to stderr while the interpreter finalizes.

A daemon thread that is still alive at that point and prints, logs or formats a traceback can abort the process on
Windows ("_enter_buffered_busy: could not acquire lock for <stderr>"), after all the tests have passed.
"""

from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from prosperos_hoard.api import create_app
from prosperos_hoard.backend import Backend
from prosperos_hoard.devtools.fake_comfy import FakeComfyServer


# What the app and its vendored link start: the job workers, Hoard Link's event-loop thread and the default executor of that
# loop (asyncio_N), the notice/event senders. The fake ComfyUI server's own request threads are the fixture's to stop.
APP_THREADS = ("job-worker-", "hoard-", "asyncio_", "prospero-")


def _started_since(before: set[threading.Thread], wait_s: float = 5.0) -> list[str]:
    """Names of the app's threads created after `before` that are still alive once they had `wait_s` to finish."""
    deadline = time.monotonic() + wait_s
    while True:
        left = [t for t in threading.enumerate() if t not in before and t.is_alive() and t.name.startswith(APP_THREADS)]
        if not left or time.monotonic() >= deadline:
            return sorted(f"{t.name} (daemon={t.daemon})" for t in left)
        time.sleep(0.05)


def _use_the_link(backend: Backend) -> None:
    """Make the backend start Hoard Link's event-loop thread, the way any request that asks the link a question does."""
    async def ask(link):
        return await link.status()

    backend.run_async(ask)


def test_app_shutdown_leaves_no_thread_alive(data_dir, fake_comfy, tmp_path):
    _, port = fake_comfy
    pool = FakeComfyServer(tmp_path / "pool_comfy")
    pool_port = pool.run_in_thread()
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": f"http://127.0.0.1:{port}"},
                                                       "render_pool": [f"http://127.0.0.1:{pool_port}"]}), encoding="utf-8")
    pool_threads = set(threading.enumerate())
    app = create_app(data_dir, static_dir=None, port=8815)
    try:
        with TestClient(app, base_url="http://127.0.0.1:8815") as c:
            assert c.get("/api/health").status_code == 200
            _use_the_link(app.state.backend)
            app.state.backend._pool_client(f"http://127.0.0.1:{pool_port}")   # a pooled client bound to the link's loop
            workers = [t.name for t in threading.enumerate() if t.name.startswith(("job-worker-", "hoard-link-sync"))]
            assert "hoard-link-sync" in workers and any(n.startswith("job-worker-") for n in workers)
        # leaving the `with` ran the server's shutdown (the lifespan): the workers and the link's loop are gone
        assert _started_since(pool_threads) == []
    finally:
        app.state.shutdown()
        pool.stop()


def test_shutdown_is_idempotent_and_works_without_a_server(data_dir, fake_comfy):
    _, port = fake_comfy
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": f"http://127.0.0.1:{port}"}}), encoding="utf-8")
    before = set(threading.enumerate())
    app = create_app(data_dir, static_dir=None, port=8815)  # no TestClient/uvicorn: nobody runs the lifespan
    _use_the_link(app.state.backend)
    assert any(t.name.startswith("job-worker-") for t in threading.enumerate() if t not in before)
    app.state.shutdown()
    app.state.shutdown()
    assert _started_since(before) == []


def test_backend_close_can_be_followed_by_new_work(data_dir, fake_comfy):
    _, port = fake_comfy
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": f"http://127.0.0.1:{port}"}}), encoding="utf-8")
    before = set(threading.enumerate())
    backend = Backend(data_dir)
    _use_the_link(backend)
    backend.close()
    assert _started_since(before) == []
    _use_the_link(backend)          # a fresh loop starts on demand
    backend.close()
    backend.close()
    assert _started_since(before) == []
