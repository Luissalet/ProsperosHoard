import pytest
from fastapi.testclient import TestClient

from prosperos_hoard.api import create_app


@pytest.fixture
def client(data_dir, fake_comfy):
    _, port = fake_comfy
    (data_dir / "backend.json").write_text('{"comfy": {"url": "http://127.0.0.1:%d"}}' % port, encoding="utf-8")
    app = create_app(data_dir, static_dir=None, port=8815)
    with TestClient(app, base_url="http://127.0.0.1:8815") as c:
        yield c, app
    app.state.queue.stop()


def test_health(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "prosperos-hoard"
    assert body["status"] == "ok"


def test_guard_rejects_bad_host(client):
    c, _ = client
    r = c.get("/api/health", headers={"Host": "evil.example:9999"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_host"


def test_guard_rejects_cross_origin_write(client):
    c, _ = client
    r = c.post("/api/agent/studio_create_project", json={"name": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_guard_allows_same_origin_write(client):
    c, _ = client
    r = c.post("/api/agent/studio_create_project", json={"name": "x"},
               headers={"Origin": "http://127.0.0.1:8815"})
    assert r.status_code == 200


def test_guard_allows_plain_get_navigation(client):
    c, _ = client
    r = c.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_full_generate_flow(client):
    c, _ = client
    r = c.post("/api/agent/studio_create_project", json={"name": "Flow Test"})
    project_id = r.json()["id"]

    r = c.post(f"/api/agent/studio_cast?project={project_id}",
               json={"action": "create", "kind": "character", "name": "Aria",
                     "fields": {"prompt": "an idol with blue hair"}})
    assert r.status_code == 200

    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "@Aria on stage", "count": 1, "wait_s": 15})
    assert r.status_code == 200
    job = r.json()["job"]
    assert job["state"] == "done"
    asset_id = job["outputs"]["asset_ids"][0]

    r = c.get(f"/api/agent/studio_show?asset_ids={asset_id}")
    assert r.status_code == 200
    assert r.json()["items"][0]["kind"] == "image"


def test_asset_file_traversal_refused(client):
    c, _ = client
    r = c.get("/api/assets/../../etc/passwd/file")
    # resolved only by id lookup in the store, never by client-supplied path
    assert r.status_code in (404, 422)
    r2 = c.get("/api/assets/a_totally_made_up/file")
    assert r2.status_code == 404
    assert r2.json()["error"] == "not_found"


def test_unknown_job_returns_404(client):
    c, _ = client
    r = c.get("/api/agent/studio_job?job_id=job_nope")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_backend_endpoint_reports_comfy_reachable(client):
    c, _ = client
    r = c.get("/api/backend")
    assert r.status_code == 200
    assert r.json()["comfy"]["reachable"] is True


def test_backend_post_sets_overrides_without_leaking_token(client):
    c, _ = client
    r = c.post("/api/backend", json={"faustus_token": "secret123"})
    assert r.status_code == 200
    assert r.json()["token_set"] is True
    assert "secret123" not in r.text
