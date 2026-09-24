import json
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


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


def test_import_refuses_traversal_outside_roots_and_renamed_files(client, tmp_path, monkeypatch):
    c, app, allowed = client
    # The home folder is an import root by design; on Windows the pytest
    # tmp_path lives under it, so point home somewhere else first.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
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
    img = folder / "it's a photo (1) ü.png"
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
    # a UI export is converted now; a broken one fails with a readable reason
    assert r.status_code == 400 and "no node type" in r.json()["message"]
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
    # pinned to sdxl_txt2img: the default engine is "auto" (Qwen-Image 2.1
    # when installed, see engine.resolve_image_engine), which does not use
    # a single checkpoint_node at all - this test is about that cross-check.
    c, _, _ = client
    project_id = _project(c, "Ckpt")
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "x", "template": "sdxl_txt2img", "checkpoint": "dreamy_v9.safetensors", "wait_s": 20})
    job = r.json()["job"]
    assert job["state"] == "failed"
    assert "sd_xl_base_1.0.safetensors" in job["error"]
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "x", "template": "sdxl_txt2img", "sampler": "euler_a", "wait_s": 20})
    assert "euler_ancestral" in r.json()["job"]["error"]
    # a preset naming the checkpoint without its extension still resolves
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}",
               json={"prompt": "x", "template": "sdxl_txt2img", "style": "Anime cel", "wait_s": 20})
    assert r.json()["job"]["state"] == "done", r.json()


def test_backend_endpoint_reports_comfy_and_hides_token(client):
    c, _, _ = client
    body = c.get("/api/backend").json()
    assert body["comfy"]["reachable"] is True
    assert "sd_xl_base_1.0.safetensors" in body["comfy"]["checkpoints"]
    assert body["ffmpeg"]["found"] in (True, False)
    # the fake backend serves a real /object_info (patched with the models the
    # production example uses), so ComfyMusic (ACE-Step) now resolves.
    assert body["music"][0]["name"] == "comfy_music"
    assert body["music"][0]["available"] is True
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


def test_canonical_crop_keeps_one_pose_of_a_reference_sheet(client):
    c, app, _ = client
    pid = _project(c, "Crop Test")
    job = c.post(f"/api/agent/studio_generate_image?project={pid}",
                 json={"prompt": "turnaround sheet", "template": "flux_schnell_txt2img", "width": 1344, "height": 768,
                       "seed": 1, "wait_s": 20}).json()["job"]
    sheet = job["asset_ids"][0]
    made = c.post(f"/api/agent/studio_cast?project={pid}",
                  json={"action": "create", "name": "FAROL", "fields": {"prompt": "lantern head"}}).json()
    char_id = made.get("id") or made["character"]["id"]
    c.post(f"/api/agent/studio_cast?project={pid}",
           json={"action": "update", "id": char_id, "fields": {"canonical_asset_id": sheet, "canonical_crop": "left_third"}})
    char = app.state.store.get_character(char_id)
    crop = app.state.store.get_asset(char["canonical_asset_id"])
    assert crop["id"] != sheet and (crop["width"], crop["height"]) == (448, 768)
    assert crop["recipe"]["operation"] == "crop" and crop["recipe"]["derived_from"] == sheet
    assert sheet in char["reference_asset_ids"]
    # the UI route goes through the same path
    r = c.patch(f"/api/characters/{char_id}", json={"fields": {"canonical_asset_id": sheet, "canonical_crop": [0.5, 0, 0.5, 1]}})
    assert r.status_code == 200, r.text
    assert app.state.store.get_asset(r.json()["canonical_asset_id"])["width"] == 672
    bad = c.patch(f"/api/characters/{char_id}", json={"fields": {"canonical_crop": [0.9, 0, 0.5, 1]}})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_crop"


def test_solo_photocard_set_numbers_every_look(client):
    c, app, _ = client
    pid = _project(c, "Solo Set")
    ids = []
    for seed in (1, 2, 3):
        job = c.post(f"/api/agent/studio_generate_image?project={pid}",
                     json={"prompt": "idol shoot", "template": "flux_schnell_txt2img", "aspect": "2:3", "seed": seed,
                           "wait_s": 20}).json()["job"]
        ids.append(job["asset_ids"][0])
    made = c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "name": "FAROL", "fields": {}}).json()
    char_id = made.get("id") or made["character"]["id"]
    r = c.post(f"/api/agent/studio_photocard_set?project={pid}", json={
        "character_id": char_id, "set_name": "NO MIRES ATRÁS",
        "cards": [{"image_asset_id": ids[0], "role": "Visual", "message": "gracias", "accent": "#F4A7C0"},
                  {"image_asset_id": ids[1], "role": "Main Rapper"}, {"image_asset_id": ids[2]}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["front_ids"]) == 3 and len(body["back_ids"]) == 3
    back = app.state.store.get_asset(body["back_ids"][2])
    assert back["recipe"]["fields"]["serial"] == "No. 003/003"
    sheet = app.state.store.get_asset(body["contact_sheet_id"])
    assert "contact_sheet" in sheet["tags"] and sheet["recipe"]["character_id"] == char_id
    bad = c.post(f"/api/agent/studio_photocard_set?project={pid}", json={"character_id": char_id})
    assert bad.status_code == 400


def test_import_ui_format_workflow_converts_and_caches_object_info(client, data_dir):
    c, app, _ = client
    ui = (Path(__file__).parent / "fixtures" / "comfy" / "video_wan2_2_5B_ti2v.json").read_bytes()
    r = c.post("/api/workflows/import-file", files={"file": ("wan.json", ui, "application/json")})
    assert r.status_code == 200, r.text
    spec = r.json()
    assert spec["converted_from"] == "ui" and spec["template"].startswith("wf_")
    cache = data_dir / "comfy" / "object_info.json"
    assert cache.is_file() and "SaveVideo" in json.loads(cache.read_text(encoding="utf-8"))
    # ComfyUI gone: the cached node list still converts the next UI export
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": "http://127.0.0.1:9"}}), encoding="utf-8")
    app.state.backend.__init__(data_dir)
    from prosperos_hoard import engine as engine_mod

    with pytest.raises(Exception):
        engine_mod._object_info(app.state.backend)  # really unreachable now
    r2 = c.post("/api/workflows/import-file", files={"file": ("wan2.json", ui, "application/json")})
    assert r2.status_code == 200, r2.text
    assert r2.json()["converted_from"] == "ui"


def test_prompt_preview_and_song_compose_have_their_own_bodies(client):
    """Two request models once shared the name ComposeBody, so the later
    (song) one silently replaced the prompt preview's: the Generate screen's
    live final-prompt preview answered "tags: Field required"."""
    c, _, _ = client
    pid = _project(c, "Bodies")
    c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "name": "Iris Volt", "fields": {"prompt": "platinum bob"}})
    r = c.post(f"/api/projects/{pid}/compose-prompt", json={"prompt": "@Iris Volt backstage"})
    assert r.status_code == 200, r.text
    assert "platinum bob" in r.json()["positive_prompt"]
    song = c.post(f"/api/projects/{pid}/compose", json={"tags": "dark trap", "lyrics": "[Verse]\nuna", "duration": 5})
    assert song.status_code == 200, song.text


def test_oversized_request_bodies_are_refused_before_reading(client):
    c, _, _ = client
    r = c.post("/api/agent/studio_create_project", content=b"{}",
               headers={"Content-Type": "application/json", "Content-Length": str(200 * 1024 * 1024)})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


def test_curation_tools_rate_tag_board_and_update_project(client):
    c, _, _ = client
    project_id = _project(c, "Curate")
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}", json={"prompt": "a lantern", "count": 2, "wait_s": 20})
    ids = r.json()["job"]["asset_ids"]
    r = c.post(f"/api/agent/studio_asset_update?asset_id={ids[1]}", json={"rating": 5, "favourite": True, "tags": ["Best"]})
    assert r.status_code == 200, r.text
    assert r.json()["favourite"] is True and r.json()["rating"] == 5
    favs = c.get(f"/api/agent/studio_assets?project={project_id}&favourite=true").json()["items"]
    assert [a["id"] for a in favs] == [ids[1]]
    assert c.post(f"/api/agent/studio_asset_update?asset_id={ids[1]}", json={}).json()["error"] == "nothing_to_update"

    board = c.post(f"/api/agent/studio_board?project={project_id}",
                   json={"action": "create", "name": "Mood", "asset_ids": [ids[1]], "notes": {ids[1]: "the one"}}).json()
    assert board["count"] == 1 and board["items"][0]["note"] == "the one"
    board = c.post(f"/api/agent/studio_board?project={project_id}",
                   json={"action": "add", "board_id": board["id"], "asset_ids": [ids[0]]}).json()
    assert [i["asset_id"] for i in board["items"]] == [ids[1], ids[0]]
    listed = c.post(f"/api/agent/studio_board?project={project_id}", json={"action": "list"}).json()["items"]
    assert listed[0]["count"] == 2

    r = c.post(f"/api/agent/studio_project_update?project={project_id}",
               json={"cover_asset_id": ids[1], "image_engine": "sdxl"})
    assert r.status_code == 200 and r.json()["image_engine"] == "sdxl" and r.json()["cover_asset_id"] == ids[1]
    # a bad cover id changes nothing (validated before any write)
    r = c.post(f"/api/agent/studio_project_update?project={project_id}", json={"name": "Renamed", "cover_asset_id": "a_nope"})
    assert r.status_code == 404
    assert c.get(f"/api/projects/{project_id}").json()["name"] == "Curate"


def test_retry_a_cancelled_job(client):
    c, app, _ = client
    project_id = _project(c, "Retry")
    job = app.state.store.create_job("generate_image", "gpu", {"positive_prompt": "x", "seed": 7}, project_id=project_id)
    app.state.store.update_job(job["id"], state="waiting_gpu")
    r = c.post(f"/api/agent/studio_retry_job?job_id={job['id']}")
    assert r.status_code == 400 and r.json()["error"] == "not_retryable"
    c.post(f"/api/agent/studio_cancel_job?job_id={job['id']}")
    app.state.queue.stop()  # keep the copy queued so its params can be read back
    r = c.post(f"/api/agent/studio_retry_job?job_id={job['id']}&new_seed=true")
    assert r.status_code == 200, r.text
    again = app.state.store.get_job(r.json()["id"])
    assert again["params"]["retry_of"] == job["id"] and again["project_id"] == project_id
    assert again["params"]["positive_prompt"] == "x"


def test_housekeeping_sweeps_old_scratch_files(tmp_path):
    import os
    import time

    from prosperos_hoard.api import _housekeeping
    from prosperos_hoard.store import Store

    store = Store(tmp_path)
    old_dir = tmp_path / "tmp" / "render_old"
    old_dir.mkdir(parents=True)
    fresh = tmp_path / "tmp" / "render_live"
    fresh.mkdir()
    upload = tmp_path / "tmp" / "uploads" / "up_1.png"
    upload.parent.mkdir()
    upload.write_bytes(b"x")
    past = time.time() - 3 * 24 * 3600
    for p in (old_dir, upload):
        os.utime(p, (past, past))
    _housekeeping(store)
    assert not old_dir.exists() and not upload.exists()
    assert fresh.exists() and (tmp_path / "tmp" / "uploads").is_dir()
