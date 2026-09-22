import json
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from prosperos_hoard.api import create_app


@pytest.fixture
def client(data_dir, fake_comfy, tmp_path):
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


def _project(c, name="Flow Test"):
    return c.post("/api/agent/studio_create_project", json={"name": name}).json()["id"]


def _wav(path: Path, seconds: float = 2.0):
    sr = 22050
    sig = (0.2 * np.sin(2 * np.pi * 220 * np.arange(int(sr * seconds)) / sr) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(sig.tobytes())


def test_health(client):
    c, _, _ = client
    body = c.get("/api/health").json()
    assert body["service"] == "prosperos-hoard"
    assert body["status"] == "ok"
    assert body["name"] == "Prospero's Hoard"
    assert "active_jobs" in body


def test_guard_rejects_bad_host(client):
    c, _, _ = client
    r = c.get("/api/health", headers={"Host": "evil.example:9999"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_host"


def test_guard_rejects_cross_origin_and_cross_site_writes(client):
    c, _, _ = client
    assert c.post("/api/agent/studio_create_project", json={"name": "x"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/agent/studio_create_project", json={"name": "x"}, headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/agent/studio_create_project", json={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/agent/studio_create_project", json={"name": "x"}, headers={"Origin": "http://127.0.0.1:8815"}).status_code == 200


def test_guard_allows_plain_get_navigation(client):
    c, _, _ = client
    assert c.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_errors_are_json_with_code_and_message(client):
    c, _, _ = client
    r = c.post("/api/agent/studio_create_project", json={})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_arguments"
    assert "name" in r.json()["message"]
    r = c.get("/api/agent/studio_job?job_id=job_nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = c.get("/api/no/such/route")
    assert r.status_code == 404 and r.json()["error"] == "not_found"


def test_full_generate_flow_with_mentions(client):
    c, _, _ = client
    project_id = _project(c)
    r = c.post(f"/api/agent/studio_cast?project={project_id}",
               json={"action": "create", "kind": "character", "name": "Aria Moon", "fields": {"prompt": "an idol with blue hair"}})
    assert r.status_code == 200, r.text
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "@Aria Moon on stage, @Nobody waves", "count": 2, "wait_s": 20, "style": "Studio portrait"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "an idol with blue hair" in body["final_prompt"]
    assert body["matched_characters"] == ["Aria Moon"]
    assert body["unknown_mentions"] == ["Nobody"]
    assert isinstance(body["seed"], int)
    job = body["job"]
    assert job["state"] == "done", job
    assert len(job["asset_ids"]) == 2
    assert job["assets"][0]["recipe"]["seed"] == body["seed"]
    assert "file_path" not in json.dumps(job)  # compact agent view
    r = c.get(f"/api/agent/studio_show?asset_ids={job['asset_ids'][0]}")
    assert r.status_code == 200
    assert r.json()["items"][0]["kind"] == "image"


def test_agent_calls_are_recorded_including_failures(client):
    c, _, _ = client
    _project(c, "Audit")
    c.get("/api/agent/studio_job?job_id=job_missing")
    calls = c.get("/api/agent-calls").json()["items"]
    tools = [(x["tool"], x["ok"]) for x in calls]
    assert ("studio_create_project", True) in tools
    assert ("studio_job", False) in tools
    failed = next(x for x in calls if x["tool"] == "studio_job")
    assert failed["error"].startswith("not_found")


def test_asset_file_traversal_refused(client):
    c, _, _ = client
    for path in ("/api/assets/../../etc/passwd/file", "/api/assets/..%2F..%2Fetc%2Fpasswd/file",
                 "/api/assets/a_totally_made_up/file", "/api/assets/%2e%2e/thumb"):
        r = c.get(path)
        # either refused, or normalised by the client into a plain SPA route
        assert r.status_code in (404, 422) or r.text == "<html>spa</html>", path
        assert "root:" not in r.text


def test_spa_fallback_never_serves_files_outside_dist(client):
    c, _, _ = client
    assert c.get("/").text == "<html>spa</html>"
    assert c.get("/library/anything").text == "<html>spa</html>"
    assert c.get("/assets/app.js").text == "console.log(1)"
    for evil in ("/%2e%2e/%2e%2e/%2e%2e/etc/passwd", "/..%2f..%2fprosperos_hoard/api.py", "/%2e%2e%2fbackend.json"):
        r = c.get(evil)
        assert "root:" not in r.text and "def create_app" not in r.text and "comfy" not in r.text


def test_import_refuses_traversal_outside_roots_and_renamed_files(client, tmp_path):
    c, app, allowed = client
    project_id = _project(c, "Imports")
    outside = tmp_path / "secret.png"
    Image.new("RGB", (8, 8)).save(outside)
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(outside)})
    assert r.status_code == 400 and r.json()["error"] == "outside_import_folders"
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(allowed / ".." / "secret.png")})
    assert r.json()["error"] == "outside_import_folders"
    try:
        (allowed / "link.png").symlink_to(outside)
    except OSError:  # Windows without symlink rights
        pass
    else:
        r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(allowed / "link.png")})
        assert r.json()["error"] == "outside_import_folders"
    fake = allowed / "notes.png"
    fake.write_text("id_rsa contents", encoding="utf-8")
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(fake)})
    assert r.json()["error"] == "invalid_image"
    key = allowed / "id_rsa"
    key.write_text("PRIVATE", encoding="utf-8")
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(key), "kind": "lyrics"})
    assert r.json()["error"] == "kind_mismatch"
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(app.state.store.data_dir / "prosperos.sqlite3")})
    assert r.status_code == 400
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(allowed / "missing.wav")})
    assert r.json()["error"] == "file_not_found"


def test_import_real_files_with_hostile_names(client):
    c, _, allowed = client
    project_id = _project(c, "Names")
    folder = allowed / "Prospero's Hoard ñ"
    folder.mkdir()
    img = folder / "it's a \"photo\" ü.png"
    Image.new("RGB", (64, 48), (200, 10, 10)).save(img)
    song = folder / "canción 'uno'.wav"
    _wav(song)
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(img)})
    assert r.status_code == 200, r.text
    asset = r.json()
    assert asset["width"] == 64 and asset["name"] == img.name
    full = c.get(f"/api/assets/{asset['id']}").json()
    assert full["mime"] == "image/png"
    assert c.get(f"/api/assets/{asset['id']}/file").content[:4] == b"\x89PNG"
    r = c.post(f"/api/agent/studio_import?project={project_id}", json={"path": str(song)})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "audio"
    assert c.get(f"/api/assets/{r.json()['id']}").json()["mime"] == "audio/wav"


def test_upload_sanitises_name_and_checks_content(client):
    c, _, _ = client
    project_id = _project(c, "Upload")
    r = c.post(f"/api/projects/{project_id}/import-upload",
               files={"file": ("../../evil.png", b"not an image", "image/png")})
    assert r.status_code == 400 and r.json()["error"] == "invalid_image"
    import io
    buf = io.BytesIO()
    Image.new("RGB", (32, 32)).save(buf, format="PNG")
    r = c.post(f"/api/projects/{project_id}/import-upload", files={"file": ("..\\..\\ok.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "ok.png"
    assert r.json()["file_path"].startswith("assets/")


def test_lyrics_are_only_written_to_lyrics_assets(client):
    c, _, _ = client
    project_id = _project(c, "Lyrics")
    lyr = c.post(f"/api/projects/{project_id}/lyrics", json={"text": "[00:01.00]hello", "name": "L"}).json()
    img = c.post(f"/api/agent/studio_design?project={project_id}",
                 json={"template": "lyric_card", "fields": {"quote": "q"}}).json()
    r = c.put(f"/api/assets/{img['id']}/lyrics", json={"text": "overwrite"})
    assert r.status_code == 400 and r.json()["error"] == "not_lyrics"
    r = c.put(f"/api/assets/{lyr['id']}/lyrics", json={"lines": [{"time_s": 2.5, "text": "tapped"}]})
    assert r.status_code == 200
    assert r.json()["lines"] == [{"time_s": 2.5, "text": "tapped"}]


def test_design_errors_are_actionable_and_preview_renders(client):
    c, _, _ = client
    project_id = _project(c, "Design")
    r = c.post(f"/api/agent/studio_design?project={project_id}", json={"template": "poster9", "fields": {}})
    assert r.json()["error"] == "unknown_template" and "photocard_front" in r.json()["message"]
    r = c.post(f"/api/agent/studio_design?project={project_id}", json={"template": "thumbnail", "fields": {"titel": "x"}})
    assert r.json()["error"] == "unknown_field" and "title" in r.json()["message"]
    r = c.post(f"/api/agent/studio_design?project={project_id}", json={"template": "thumbnail", "fields": {"title": "x", "accent": "pink"}})
    assert r.json()["error"] == "bad_colour"
    r = c.post("/api/design/preview", json={"template": "photocard_back", "fields": {"member_name": "Mika"}})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    with Image.open(__import__("io").BytesIO(r.content)) as im:
        assert max(im.size) <= 720


def test_hostile_workflow_imports_are_rejected(client):
    c, _, _ = client
    ui = {"nodes": [{"id": 1}], "links": []}
    r = c.post("/api/workflows/import", json={"name": "ui", "workflow": ui})
    assert r.status_code == 400 and "API Format" in r.json()["message"]
    deep: dict = {"1": {"class_type": "SaveImage", "inputs": {"x": []}}}
    cur = deep["1"]["inputs"]["x"]
    for _ in range(30):
        cur.append([])
        cur = cur[0]
    assert c.post("/api/workflows/import", json={"name": "deep", "workflow": deep}).status_code == 400
    many = {str(i): {"class_type": "SaveImage", "inputs": {}} for i in range(500)}
    assert c.post("/api/workflows/import", json={"name": "many", "workflow": many}).status_code == 400
    dangling = {"1": {"class_type": "SaveImage", "inputs": {"images": ["99", 0]}}}
    assert "missing node" in c.post("/api/workflows/import", json={"name": "d", "workflow": dangling}).json()["message"]
    no_output = {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}
    assert "output node" in c.post("/api/workflows/import", json={"name": "n", "workflow": no_output}).json()["message"]
    r = c.post(f"/api/agent/studio_generate_image?project={_project(c, 'T')}",
               json={"prompt": "x", "template": "../../prosperos_hoard/workflows/sdxl_txt2img"})
    assert r.status_code == 400 and r.json()["error"] == "unknown_template"


def test_custom_workflow_import_edit_and_generate(client):
    c, _, _ = client
    wf = json.loads((Path(__file__).resolve().parent.parent / "prosperos_hoard" / "workflows" / "sdxl_txt2img.json").read_text(encoding="utf-8"))
    r = c.post("/api/workflows/import", json={"name": "My SDXL", "workflow": wf})
    assert r.status_code == 200, r.text
    spec = r.json()
    assert spec["map"]["positive_prompt"] == "6.text"
    assert spec["map"]["negative_prompt"] == "7.text"
    bad = dict(spec["map"], seed="42.seed")
    assert c.patch(f"/api/workflows/{spec['template']}", json={"map": bad}).status_code == 400
    ok = c.patch(f"/api/workflows/{spec['template']}", json={"name": "Renamed"})
    assert ok.json()["name"] == "Renamed"
    project_id = _project(c, "Custom")
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "a neon skyline", "template": spec["template"], "wait_s": 20, "seed": 5})
    assert r.json()["job"]["state"] == "done", r.json()
    assert r.json()["job"]["assets"][0]["recipe"]["template"] == spec["template"]


def test_unknown_checkpoint_and_sampler_fail_with_available_options(client):
    c, _, _ = client
    project_id = _project(c, "Ckpt")
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "x", "checkpoint": "dreamy_v9.safetensors", "wait_s": 20})
    job = r.json()["job"]
    assert job["state"] == "failed"
    assert "sd_xl_base_1.0.safetensors" in job["error"]
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}", json={"prompt": "x", "sampler": "euler_a", "wait_s": 20})
    assert "euler_ancestral" in r.json()["job"]["error"]
    # a preset naming the checkpoint without its extension still resolves
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "x", "style": "Anime cel", "wait_s": 20})
    assert r.json()["job"]["state"] == "done", r.json()


def test_backend_endpoint_reports_comfy_and_hides_token(client):
    c, _, _ = client
    body = c.get("/api/backend").json()
    assert body["comfy"]["reachable"] is True
    assert "sd_xl_base_1.0.safetensors" in body["comfy"]["checkpoints"]
    assert body["ffmpeg"]["found"] in (True, False)
    assert body["music"][0]["available"] is False
    r = c.post("/api/backend", json={"faustus_token": "secret123"})
    assert r.status_code == 200
    assert r.json()["token_set"] is True
    assert "secret123" not in r.text
    assert "secret123" not in c.get("/api/backend").text
    r = c.post("/api/backend", json={"vram_estimates_mb": {"sdxl": 5}})
    assert r.status_code == 400


def test_cancel_queued_job_via_api(client):
    c, app, _ = client
    project_id = _project(c, "Cancel")
    job = app.state.store.create_job("generate_image", "gpu", {"positive_prompt": "x"}, project_id=project_id)
    app.state.store.update_job(job["id"], state="waiting_gpu")
    r = c.post(f"/api/agent/studio_cancel_job?job_id={job['id']}")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
