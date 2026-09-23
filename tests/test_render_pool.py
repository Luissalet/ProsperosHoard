"""Render pool: one ComfyUI per GPU, all taking jobs from the one GPU queue."""
from __future__ import annotations

import json
import time

import pytest

from prosperos_hoard import engine
from prosperos_hoard.backend import Backend
from prosperos_hoard.devtools.fake_comfy import FakeComfyServer
from prosperos_hoard.jobs import JobQueue


@pytest.fixture
def second_comfy(data_dir):
    server = FakeComfyServer(data_dir / "fake_comfy_2")
    port = server.run_in_thread()
    yield server, port
    server.stop()


def _pool_backend(data_dir, main_port, pool_urls):
    (data_dir / "backend.json").write_text(json.dumps({
        "comfy": {"url": f"http://127.0.0.1:{main_port}"}, "render_pool": pool_urls,
    }), encoding="utf-8")
    return Backend(data_dir)


def _queue(store, backend):
    queue = JobQueue(store, gpu_targets=[None, *backend.render_pool()], bind=backend.bind_comfy,
                     target_ready=backend.pool_server_ready)
    queue.register("generate_image", lambda job, p: engine.generate_image(store, backend, job, p))
    return queue


def _params(seed):
    return {"prompt": "street", "positive_prompt": "street", "negative_prompt": "", "width": 512, "height": 512,
            "seed": seed, "count": 1, "template": "sdxl_txt2img"}


def test_two_servers_render_two_jobs_at_once(store, project, data_dir, fake_comfy, second_comfy):
    main, main_port = fake_comfy
    extra, extra_port = second_comfy
    main.history_delay_s = extra.history_delay_s = 2.0
    backend = _pool_backend(data_dir, main_port, [f"http://127.0.0.1:{extra_port}"])
    queue = _queue(store, backend)
    queue.start()
    try:
        started = time.monotonic()
        jobs = [queue.enqueue("generate_image", "gpu", _params(seed), project_id=project["id"]) for seed in (1, 2)]
        finals = [queue.wait_for(j["id"], 30) for j in jobs]
        elapsed = time.monotonic() - started
    finally:
        queue.stop()
    assert [f["state"] for f in finals] == ["done", "done"], finals
    # each server rendered one of them, and they overlapped
    assert len(main.prompts_seen) == 1 and len(extra.prompts_seen) == 1
    assert elapsed < 3.8, elapsed
    for f in finals:
        asset = store.get_asset(f["outputs"]["asset_ids"][0])
        assert (store.data_dir / asset["file_path"]).is_file()


def test_a_pool_server_that_is_off_leaves_the_queue_to_the_others(store, project, data_dir, fake_comfy):
    main, main_port = fake_comfy
    backend = _pool_backend(data_dir, main_port, ["http://127.0.0.1:9"])  # nothing listens there
    assert backend.pool_server_ready("http://127.0.0.1:9") is False
    queue = _queue(store, backend)
    queue.start()
    try:
        jobs = [queue.enqueue("generate_image", "gpu", _params(seed), project_id=project["id"]) for seed in (3, 4, 5)]
        finals = [queue.wait_for(j["id"], 30) for j in jobs]
    finally:
        queue.stop()
    assert all(f["state"] == "done" for f in finals), finals
    assert len(main.prompts_seen) == 3


def test_render_pool_setting_is_validated_and_reported(data_dir, fake_comfy, second_comfy):
    _, main_port = fake_comfy
    _, extra_port = second_comfy
    backend = _pool_backend(data_dir, main_port, [])
    with pytest.raises(ValueError, match="http"):
        backend.set_overrides(render_pool=["127.0.0.1:8189"])
    with pytest.raises(ValueError, match="at most 7"):
        backend.set_overrides(render_pool=[f"http://127.0.0.1:{9000 + i}" for i in range(8)])
    backend.set_overrides(render_pool=[f"http://127.0.0.1:{extra_port}/", f"http://127.0.0.1:{extra_port}"])
    assert backend.render_pool() == [f"http://127.0.0.1:{extra_port}"]  # trailing slash and duplicate folded
    status = backend.status()
    assert status["render_pool"] == [{"url": f"http://127.0.0.1:{extra_port}", "reachable": True}]
    assert status["overrides"]["render_pool"] == [f"http://127.0.0.1:{extra_port}"]


def test_a_worker_bound_to_a_pool_server_gates_vram_on_that_server(data_dir, fake_comfy, second_comfy):
    main, main_port = fake_comfy
    extra, extra_port = second_comfy
    main.vram_free_bytes = 20_000 * 1024 * 1024
    extra.vram_free_bytes = 3_000 * 1024 * 1024
    backend = _pool_backend(data_dir, main_port, [f"http://127.0.0.1:{extra_port}"])
    assert backend.vram_free_mb() == 20_000
    backend.bind_comfy(f"http://127.0.0.1:{extra_port}")
    try:
        assert backend.vram_free_mb() == 3_000
    finally:
        backend.bind_comfy(None)
