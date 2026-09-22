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
