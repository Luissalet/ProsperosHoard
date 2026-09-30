"""Real SQLite + local fake ComfyUI; never submit uncertain jobs twice."""
import sqlite3
import json

import pytest

from prosperos_hoard import engine
from prosperos_hoard.jobs import JobQueue, Progress


def image_job(store, project, type_="generate_image"):
    return store.create_job(type_, "gpu", {
        "positive_prompt": "synthetic fixture", "width": 256, "height": 256,
        "seed": 77, "template": "sdxl_txt2img",
    }, project_id=project["id"], state="running")


def test_real_render_commits_intent_before_post_and_accepted_receipt(store, project, backend_with_comfy, fake_comfy, monkeypatch):
    server, _ = fake_comfy
    job = image_job(store, project)
    original = server._process_prompt
    seen = []

    def render(prompt_id, workflow):
        with sqlite3.connect(store.data_dir / "prosperos.sqlite3") as conn:
            raw = conn.execute("SELECT comfy_submission_json FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]
        receipt = json.loads(raw)
        assert receipt["phase"] == "intent" and receipt["client_id"]
        seen.append(receipt)
        return original(prompt_id, workflow)

    monkeypatch.setattr(server, "_process_prompt", render)
    result = engine.generate_image(store, backend_with_comfy, job, Progress(store, job["id"]))
    assert result["asset_ids"] and len(seen) == 1
    receipt = store.get_job(job["id"])["comfy_submission"]
    assert receipt["phase"] == "accepted" and receipt["prompt_id"] in server.history
    assert receipt["client_id"] == seen[0]["client_id"]
    assert receipt["endpoint"].startswith("http://127.0.0.1:")
    assert len(receipt["workflow_sha256"]) == 64
    # Crash after render/asset creation but before final job completion:
    # startup must not run the image handler a second time.
    queue = JobQueue(store)
    calls = []
    queue.register("generate_image", lambda j, p: calls.append(j) or {})
    queue.start()
    try:
        recovered = store.get_job(job["id"])
        assert recovered["state"] == "failed" and "outcome_unknown" in recovered["message"]
        assert recovered["comfy_submission"] == receipt
        assert calls == [] and len(server.prompts_seen) == 1
    finally:
        queue.stop()


def test_failed_intent_commit_prevents_real_post(store, project, backend_with_comfy, fake_comfy, monkeypatch):
    job = image_job(store, project)
    class FailCommit:
        def __getattr__(self, name):
            return getattr(store.conn, name)
        def commit(self):
            raise sqlite3.OperationalError("synthetic commit failure")
    real_conn = store.conn
    # Store.conn is a property; intercept its connection seam only for this store.
    monkeypatch.setattr(type(store), "conn", property(lambda self: FailCommit()))
    # Avoid recursion through the shim's delegated connection.
    monkeypatch.setattr(FailCommit, "__getattr__", lambda self, name: getattr(real_conn, name))
    with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
        engine.generate_image(store, backend_with_comfy, job, Progress(store, job["id"]))
    assert fake_comfy[0].prompts_seen == []
    assert real_conn.execute("SELECT comfy_submission_json FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] is None


@pytest.mark.parametrize("type_", ["generate_image", "edit_image"])
@pytest.mark.parametrize("phase", ["intent", "accepted"])
def test_startup_holds_uncertain_image_receipts_idempotently(store, project, type_, phase):
    job = image_job(store, project, type_)
    receipt = {"phase": phase, "client_id": "synthetic", **({"prompt_id": "fixture"} if phase == "accepted" else {})}
    store.record_comfy_submission(job["id"], receipt)
    store.requeue_running_jobs()
    first = store.get_job(job["id"])
    store.requeue_running_jobs()
    assert store.get_job(job["id"]) == first
    assert first["state"] == "failed" and first["comfy_submission"] == receipt


def test_waiting_image_without_intent_and_other_jobs_keep_legacy_requeue(store, project):
    waiting = image_job(store, project)
    store.update_job(waiting["id"], state="waiting_gpu")
    cpu = store.create_job("test_cpu", "cpu", {}, state="running")
    assert store.requeue_running_jobs() == 2
    assert store.get_job(waiting["id"])["state"] == "queued"
    assert store.get_job(cpu["id"])["state"] == "queued"


def test_accepted_receipt_write_failure_retains_committed_intent(store, project, backend_with_comfy, fake_comfy, monkeypatch):
    job = image_job(store, project)
    original = store.record_comfy_submission
    def record(job_id, receipt):
        if receipt["phase"] == "accepted":
            raise sqlite3.OperationalError("synthetic receipt failure")
        original(job_id, receipt)
    monkeypatch.setattr(store, "record_comfy_submission", record)
    with pytest.raises(sqlite3.OperationalError, match="synthetic receipt failure"):
        engine.generate_image(store, backend_with_comfy, job, Progress(store, job["id"]))
    assert len(fake_comfy[0].prompts_seen) == 1
    assert store.get_job(job["id"])["comfy_submission"]["phase"] == "intent"
    store.requeue_running_jobs()
    assert "outcome_unknown" in store.get_job(job["id"])["message"]


def test_lost_response_after_real_post_does_not_resubmit(store, project, backend_with_comfy, fake_comfy, monkeypatch):
    from prosperos_hoard.hoard_link._comfy import ComfyClient
    original = ComfyClient.queue
    async def queue(self, workflow, client_id):
        await original(self, workflow, client_id)
        raise TimeoutError("synthetic lost response")
    monkeypatch.setattr(ComfyClient, "queue", queue)
    job = image_job(store, project)
    with pytest.raises(TimeoutError, match="lost response"):
        engine.generate_image(store, backend_with_comfy, job, Progress(store, job["id"]))
    assert len(fake_comfy[0].prompts_seen) == 1
    store.requeue_running_jobs()
    assert store.get_job(job["id"])["state"] == "failed"
    assert store.get_job(job["id"])["comfy_submission"]["phase"] == "intent"
    assert store.next_queued_job("gpu") is None


def test_cancelled_and_done_image_jobs_are_not_changed_on_restart(store, project):
    for state in ("cancelled", "done"):
        job = image_job(store, project)
        store.record_comfy_submission(job["id"], {"phase": "accepted", "prompt_id": "fixture"})
        store.update_job(job["id"], state=state, outputs={"asset_ids": ["fixture"]})
        before = store.get_job(job["id"])
        store.requeue_running_jobs()
        assert store.get_job(job["id"]) == before


def test_waiting_image_with_intent_is_not_safe_to_requeue(store, project):
    job = image_job(store, project)
    store.record_comfy_submission(job["id"], {"phase": "intent"})
    store.update_job(job["id"], state="waiting_gpu", cancel_requested=True)
    assert store.requeue_running_jobs() == 0
    held = store.get_job(job["id"])
    assert held["state"] == "failed" and held["cancel_requested"] is True


def test_receipt_column_migrates_existing_database(tmp_path):
    from prosperos_hoard.db import connect
    path = tmp_path / "prosperos.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, project_id TEXT, type TEXT, lane TEXT, "
                 "params_json TEXT DEFAULT '{}', inputs_json TEXT DEFAULT '{}', state TEXT, "
                 "progress REAL, message TEXT, outputs_json TEXT, backend TEXT, log_excerpt TEXT, "
                 "created_at TEXT, updated_at TEXT, started_at TEXT, finished_at TEXT)")
    conn.execute("INSERT INTO jobs(id,type,lane,state) VALUES ('old','generate_image','gpu','running')")
    conn.commit(); conn.close()
    migrated = connect(tmp_path)
    assert "comfy_submission_json" in {row[1] for row in migrated.execute("PRAGMA table_info(jobs)")}
    assert migrated.execute("SELECT id FROM jobs").fetchone()[0] == "old"
