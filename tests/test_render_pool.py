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


def test_worker_lease_targets_the_owned_server_card_and_does_not_guess_remote_cards(data_dir, monkeypatch):
    backend = _pool_backend(data_dir, 8188, ["http://127.0.0.1:8190"])
    monkeypatch.setattr(backend.launcher, "get", lambda sid: sid)
    monkeypatch.setattr(backend.launcher, "status", lambda sid: {
        "state": "running", "gpu": 1 if sid == "comfyui@8188" else 3,
    })
    assert backend.comfy_gpu() == 1
    backend.bind_comfy("http://127.0.0.1:8190")
    assert backend.comfy_gpu() == 3
    backend.bind_comfy("http://example.invalid:8190")
    assert backend.comfy_gpu() is None
    backend.close()


@pytest.mark.parametrize("override,expected", [("http://127.0.0.1:8190", 3), ("http://example.invalid:8190", None)])
def test_main_lease_and_start_follow_effective_environment_url(data_dir, monkeypatch, override, expected):
    monkeypatch.setenv("HOARD_COMFY_URL", override)
    backend = _pool_backend(data_dir, 8188, ["http://127.0.0.1:8191"])
    looked_up, started = [], []
    monkeypatch.setattr(backend.launcher, "get", lambda sid: looked_up.append(sid) or sid)
    monkeypatch.setattr(backend.launcher, "status", lambda sid: {"state": "running", "gpu": 3 if sid == "comfyui@8190" else 1})
    monkeypatch.setattr(backend.launcher, "start", lambda sid, **kwargs: started.append((sid, kwargs)) or {"ok": True, "ready": True})
    monkeypatch.setattr(backend, "_after_service_change", lambda: None)
    assert backend.comfy_gpu() == expected
    assert looked_up == (["comfyui@8190"] if expected is not None else [])
    backend.bind_lease_gpu(3)
    assert backend.ensure_comfy(12) is (expected is not None)
    assert started == ([("comfyui@8190", {"gpu": 3, "wait_s": 12})] if expected is not None else [])
    # A pool worker's explicitly bound server still takes priority over main.
    backend.bind_comfy("http://127.0.0.1:8191")
    assert backend.comfy_gpu() == 1
    backend.close()


def test_unowned_running_server_gpu_stays_unknown_and_only_a_stopped_main_uses_config(data_dir, monkeypatch):
    backend = _pool_backend(data_dir, 8188, ["http://127.0.0.1:8190"])
    monkeypatch.setattr(backend.launcher, "get", lambda sid: sid)
    monkeypatch.setattr(backend.launcher, "config", lambda: {"comfyui": {"gpu": "2"}})
    state = {"state": "running", "gpu": None}
    monkeypatch.setattr(backend.launcher, "status", lambda sid: state)
    assert backend.comfy_gpu() is None
    state["state"] = "down"
    assert backend.comfy_gpu() == 2
    backend.bind_comfy("http://127.0.0.1:8190")
    assert backend.comfy_gpu() is None
    backend.close()


def test_cold_server_starts_on_the_card_granted_by_the_lease(data_dir, monkeypatch):
    backend = _pool_backend(data_dir, 8188, [])
    calls = []
    monkeypatch.setattr(backend.launcher, "start", lambda sid, **kwargs:
        calls.append((sid, kwargs)) or {"ok": True, "ready": True})
    monkeypatch.setattr(backend, "_after_service_change", lambda: None)
    backend.bind_lease_gpu(3)
    assert backend.ensure_comfy(timeout_s=12)
    assert calls == [("comfyui@8188", {"gpu": 3, "wait_s": 12})]
    backend.bind_lease_gpu(None)
    backend.close()


@pytest.mark.parametrize("fails", [False, True])
def test_native_queue_passes_the_server_card_to_the_lease_and_clears_binding(client, monkeypatch, fails):
    from prosperos_hoard import gpu_lease
    _, app, _ = client
    app.state.queue.stop()
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")
    monkeypatch.setattr(app.state.backend, "comfy_gpu", lambda: 3)
    calls, releases = [], []
    class FakeLease:
        gpu = 3
        def __enter__(self):
            return self
        def __exit__(self, *args):
            releases.append(True)
    def fake_lease(**kwargs):
        calls.append(kwargs)
        return FakeLease()
    monkeypatch.setattr(gpu_lease, "default_lease", fake_lease)
    job = {"id": "physical-card-test", "lane": "gpu", "type": "generate_image", "params": {"template": "ltx23_i2v"}}
    try:
        with app.state.queue._guard(job):
            assert calls[0]["gpu"] == 3 and calls[0]["vram_mb"] == 10000
            assert app.state.backend._bound.lease_gpu == 3
            if fails:
                raise ValueError("test render failed")
    except ValueError:
        assert fails
    assert releases == [True] and app.state.backend._bound.lease_gpu is None


def test_imported_video_reserves_its_declared_class_without_consulting_an_image_model(client, monkeypatch):
    from prosperos_hoard import comfy_driver, gpu_lease
    c, app, _ = client
    app.state.queue.stop()
    workflow, _ = comfy_driver.load_template("wan22_ti2v")
    imported = c.post("/api/workflows/import", json={"name": "Imported video class", "workflow": workflow})
    assert imported.status_code == 200, imported.text
    template = imported.json()["template"]
    patched = c.patch(f"/api/workflows/{template}", json={"vram_class": "wan"})
    assert patched.status_code == 200, patched.text
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")
    monkeypatch.setattr(app.state.backend, "comfy_gpu", lambda: 3)
    def unexpected(*args, **kwargs):
        pytest.fail("A declared workflow class needs no image-engine or live model lookup")
    monkeypatch.setattr(engine, "resolve_image_engine", unexpected)
    monkeypatch.setattr(engine, "_object_info", unexpected)
    calls, releases = [], []
    class FakeLease:
        gpu = 3
        def __enter__(self):
            return self
        def __exit__(self, *args):
            releases.append(True)
    monkeypatch.setattr(gpu_lease, "default_lease", lambda **kw: calls.append(kw) or FakeLease())
    job = {"id": "imported-video-test", "lane": "gpu", "type": "generate_image", "params": {"template": template}}
    with app.state.queue._guard(job):
        assert calls[0]["gpu"] == 3
        assert calls[0]["vram_mb"] == app.state.backend.vram_estimates_mb()["wan"] == 12000
    assert releases == [True]


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
    # the two renders overlapped on the servers' own clocks (wall time alone
    # is at the mercy of a loaded machine)
    (a_ready,), (b_ready,) = main._ready_at.values(), extra._ready_at.values()
    a_start, b_start = a_ready - main.history_delay_s, b_ready - extra.history_delay_s
    assert a_start < b_ready and b_start < a_ready, (a_start, a_ready, b_start, b_ready)
    assert elapsed < 30, elapsed
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


def test_pool_client_survives_a_settings_reload(data_dir, fake_comfy, second_comfy):
    # saving the backend settings rebuilds the Link and its event loop; the
    # pool's ComfyUI client must follow it instead of failing with "Event is
    # bound to a different event loop" on the next render
    _main, main_port = fake_comfy
    _extra, extra_port = second_comfy
    url = f"http://127.0.0.1:{extra_port}"
    backend = _pool_backend(data_dir, main_port, [url])
    first = backend._pool_client(url)
    assert backend.run_async(first.system_stats())
    backend.reload()
    second = backend._pool_client(url)
    assert second is not first
    assert backend.run_async(second.system_stats())
    assert backend._pool_client(url) is second


def test_busy_server_uses_the_saved_node_list(data_dir, fake_comfy, monkeypatch):
    # a server deep in a long render can be too slow to list its nodes; the
    # saved copy stands in so the job queues behind the render instead of failing
    import httpx

    _main, main_port = fake_comfy
    backend = _pool_backend(data_dir, main_port, [])
    live = engine._object_info(backend)  # saves the copy
    assert live and engine.object_info_cache_path(data_dir).is_file()
    comfy = engine._comfy(backend)

    async def too_busy():
        raise httpx.ReadTimeout("busy")

    monkeypatch.setattr(type(comfy), "object_info", lambda self: too_busy())
    assert engine._object_info(backend).keys() == live.keys()
    engine.object_info_cache_path(data_dir).unlink()
    with pytest.raises(Exception, match="not answering"):
        engine._object_info(backend)


def test_a_model_too_big_for_a_card_waits_for_the_bigger_one(data_dir, fake_comfy, second_comfy):
    # Wan Animate 2 asks for a 15 GB card: the 12 GB server's worker leaves it
    # to the 16 GB one, and takes everything else
    main, main_port = fake_comfy
    extra, extra_port = second_comfy
    main.vram_total_bytes = 12 * 1024**3
    extra.vram_total_bytes = 16 * 1024**3
    url = f"http://127.0.0.1:{extra_port}"
    backend = _pool_backend(data_dir, main_port, [url])
    animate = {"type": "generate_image", "params": {"template": "wan_animate2"}}
    still = {"type": "generate_image", "params": {"template": "sdxl_txt2img"}}
    backend.bind_comfy(None)
    assert backend.accepts_job(still) and not backend.accepts_job(animate)
    backend.bind_comfy(url)
    assert backend.accepts_job(animate)
    # with no card big enough anywhere, anyone takes it rather than nobody
    extra.vram_total_bytes = 12 * 1024**3
    backend._card_totals = None
    backend.bind_comfy(None)
    assert backend.accepts_job(animate)


def test_claim_skips_jobs_a_worker_does_not_accept(store, project, data_dir):
    queue = JobQueue(store, accepts=lambda job: job["params"].get("big") is not True)
    queue.register("generate_image", lambda job, p: {})
    first = queue.enqueue("generate_image", "gpu", {"big": True}, project_id=project["id"])
    second = queue.enqueue("generate_image", "gpu", {"big": False}, project_id=project["id"])
    taken = queue._claim("gpu", gpu_worker=True)
    assert taken["id"] == second["id"]
    assert store.get_job(first["id"])["state"] == "queued"


def test_main_client_held_by_a_job_survives_a_settings_reload(data_dir, fake_comfy):
    # a render keeps the ComfyUI client it started with; saving settings or
    # starting a server mid-render must not break its next call
    _main, main_port = fake_comfy
    backend = _pool_backend(data_dir, main_port, [])
    comfy = backend.comfy()
    assert backend.run_async(comfy.system_stats())
    backend.reload()
    assert backend.run_async(comfy.system_stats())
    assert backend.run_async(backend.comfy().system_stats())


def test_heavy_models_free_the_card_after_their_job_on_dedicated_servers(data_dir, fake_comfy):
    main, main_port = fake_comfy
    backend = _pool_backend(data_dir, main_port, [])
    assert engine.free_after(backend, {"free_after": True}) is False  # not dedicated: never unload
    backend.set_overrides(comfy_dedicated=True)
    assert engine.free_after(backend, {}) is False
    assert engine.free_after(backend, {"free_after": True}) is True
    assert main.frees and main.frees[-1].get("unload_models") is True
