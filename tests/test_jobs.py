import time

from prosperos_hoard import jobs as jobs_mod
from prosperos_hoard.jobs import JobQueue, WaitingForResources


def test_gpu_job_waits_then_succeeds(store, monkeypatch):
    monkeypatch.setattr(jobs_mod, "GPU_WAIT_POLL_S", 0.05)
    calls = {"n": 0}

    def handler(job, progress):
        calls["n"] += 1
        if calls["n"] < 3:
            raise WaitingForResources("waiting for 7000 MB VRAM (2000 MB free)")
        return {"ok": True}

    queue = JobQueue(store)
    queue.register("test_job", handler)
    queue.start()
    try:
        job = queue.enqueue("test_job", "gpu", {})
        final = queue.wait_for(job["id"], timeout_s=5.0)
        assert final["state"] == "done"
        assert final["outputs"] == {"ok": True}
        assert calls["n"] == 3
    finally:
        queue.stop()


def test_gpu_job_times_out_waiting(store, monkeypatch):
    monkeypatch.setattr(jobs_mod, "GPU_WAIT_POLL_S", 0.02)
    monkeypatch.setattr(jobs_mod, "GPU_WAIT_TIMEOUT_S", 0.05)

    def handler(job, progress):
        raise WaitingForResources("never enough VRAM")

    queue = JobQueue(store)
    queue.register("test_job", handler)
    queue.start()
    try:
        job = queue.enqueue("test_job", "gpu", {})
        final = queue.wait_for(job["id"], timeout_s=5.0)
        assert final["state"] == "failed"
        assert "never enough VRAM" in final["message"]
    finally:
        queue.stop()


def test_job_persistence_across_restart(store):
    job = store.create_job("generate_image", "gpu", {"prompt": "x"})
    store.update_job(job["id"], state="running")

    # Simulate a fresh process boot against the same store/db.
    n_requeued = store.requeue_running_jobs()
    assert n_requeued == 1
    assert store.get_job(job["id"])["state"] == "queued"

    queue = JobQueue(store)
    seen = []
    queue.register("generate_image", lambda j, p: seen.append(j["id"]) or {})
    queue.start()
    try:
        final = queue.wait_for(job["id"], timeout_s=5.0)
        assert final["state"] == "done"
        assert seen == [job["id"]]
    finally:
        queue.stop()


def test_failed_job_records_message(store):
    def handler(job, progress):
        raise RuntimeError("boom")

    queue = JobQueue(store)
    queue.register("boom_job", handler)
    queue.start()
    try:
        job = queue.enqueue("boom_job", "cpu", {})
        final = queue.wait_for(job["id"], timeout_s=5.0)
        assert final["state"] == "failed"
        assert "boom" in final["message"]
    finally:
        queue.stop()


def test_waiting_state_and_reason_are_visible_then_cancel(store, monkeypatch):
    monkeypatch.setattr(jobs_mod, "GPU_WAIT_POLL_S", 5.0)

    def handler(job, progress):
        raise WaitingForResources("waiting for 7000 MB of free VRAM (2000 MB free now)")

    queue = JobQueue(store)
    queue.register("test_job", handler)
    queue.start()
    try:
        job = queue.enqueue("test_job", "gpu", {})
        deadline = time.monotonic() + 3
        while store.get_job(job["id"])["state"] != "waiting_gpu" and time.monotonic() < deadline:
            time.sleep(0.05)
        waiting = store.get_job(job["id"])
        assert waiting["state"] == "waiting_gpu"
        assert "2000 MB free" in waiting["message"]
        queue.cancel(job["id"])
        final = queue.wait_for(job["id"], timeout_s=3.0)
        assert final["state"] == "cancelled"
    finally:
        queue.stop()


def test_running_job_stops_at_next_progress_checkpoint(store):
    started = {"n": 0}

    def handler(job, progress):
        for i in range(200):
            started["n"] = i
            progress(i / 200, f"step {i}")
            time.sleep(0.02)
        return {"ok": True}

    queue = JobQueue(store)
    queue.register("slow", handler)
    queue.start()
    try:
        job = queue.enqueue("slow", "cpu", {})
        while store.get_job(job["id"])["state"] != "running":
            time.sleep(0.02)
        time.sleep(0.1)
        queue.cancel(job["id"])
        final = queue.wait_for(job["id"], timeout_s=5.0)
        assert final["state"] == "cancelled"
        assert started["n"] < 150
    finally:
        queue.stop()


def test_vram_check_uses_estimate_table_and_free_memory(data_dir):
    import pytest

    from prosperos_hoard import comfy_driver, engine
    from prosperos_hoard.backend import Backend

    backend = Backend(data_dir)
    _, spec = comfy_driver.load_template("sdxl_txt2img")
    backend.vram_free_mb = lambda: 2000
    with pytest.raises(WaitingForResources) as exc:
        engine.check_vram_or_wait(backend, spec)
    assert "7000 MB" in exc.value.reason and "2000 MB free" in exc.value.reason
    backend.set_overrides(vram_estimates_mb={"sdxl": 1500})
    backend.vram_free_mb = lambda: 2000
    engine.check_vram_or_wait(backend, spec)  # fits now
    backend.vram_free_mb = lambda: None  # no GPU information at all: do not block
    engine.check_vram_or_wait(backend, spec)


def test_queue_order_survives_restart_and_nothing_runs_twice(store):
    ids = [store.create_job("t", "gpu", {"i": i})["id"] for i in range(4)]
    store.update_job(ids[1], state="running")
    store.update_job(ids[2], state="waiting_gpu")
    store.update_job(ids[3], state="done")
    assert store.requeue_running_jobs() == 2
    seen = []
    queue = JobQueue(store)
    queue.register("t", lambda j, p: seen.append(j["params"]["i"]) or {})
    queue.start()
    try:
        for jid in ids[:3]:
            assert queue.wait_for(jid, 5)["state"] == "done"
    finally:
        queue.stop()
    assert seen == [0, 1, 2]
