from __future__ import annotations

import io
from PIL import Image
from prosperos_hoard import productions as prod
from test_productions import tiny_spec


def test_animation_uses_active_project_and_honours_wan5_duration(client):
    c, app, _ = client
    source = app.state.store.create_project("Reference library")
    target = app.state.store.create_project("Active studio")
    image = io.BytesIO()
    Image.new("RGB", (128, 72), "orange").save(image, "PNG")
    imported = c.post(f"/api/projects/{source['id']}/import-upload", files={"file": ("reference.png", image.getvalue(), "image/png")})
    assert imported.status_code == 200, imported.text
    asset = imported.json()
    result = c.post(f"/api/assets/{asset['id']}/animate", json={"asset_id": asset["id"], "engine": "wan", "seconds": 3,
                  "project_id": target["id"], "prompt": "walk forward"})
    assert result.status_code == 200, result.text
    job = app.state.store.get_job(result.json()["job"]["id"])
    assert job["project_id"] == target["id"]
    assert job["params"]["length"] == 73


def test_script_editor_and_agent_route_share_saved_segments(client):
    c, app, _ = client
    state = prod.create_production(app.state.store.data_dir, "Optional script", tiny_spec())
    slug = state["slug"]
    result = c.post(f"/api/agent/studio_production_segments?production={slug}", json={"text": "First phrase\nSecond phrase"})
    assert result.status_code == 200, result.text
    result = result.json()
    assert result["count"] == 2 and result["timed"] == 0
    saved = prod.load_state(app.state.store.data_dir, slug)["spec"]["script_segments"]
    saved[0].update(shot_key="1", start_s=1, end_s=2)
    changed = c.put(f"/api/productions/{slug}/segments", json={"segments": saved})
    assert changed.status_code == 200, changed.text
    assert changed.json()["retimed"] == ["1"]
