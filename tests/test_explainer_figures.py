"""Portable snapshots stay exact across edits, cached assets and a new profile."""
import base64
from copy import deepcopy
import io
import json

from PIL import Image
import pytest

from prosperos_hoard import explainers, figures, productions
from prosperos_hoard.api import create_app
from fastapi.testclient import TestClient
from test_shorts import fake_wav, wait_job


def figure(state="closed"):
    png = io.BytesIO()
    Image.new("RGB", (64, 40), "#376c8b").save(png, format="PNG")
    return {"format": "vitruvius.figure/1", "document_id": "123456abcdef", "revision": 7,
            "state_id": state, "title": "Synthetic mechanism", "content": {
                "diagram": {"type": "isometric", "nodes": []},
                "mechanism": {"parts": [{"id": "lid", "description": "Retained notes"}],
                              "states": [{"id": "closed"}, {"id": "open"}]}},
            "origin": {"editor_url": "http://127.0.0.1:5191/design/#/diagrams?document=123456abcdef"},
            "preview": {"mime": "image/png", "data": base64.b64encode(png.getvalue()).decode("ascii")}}


def scenes():
    return [{"id": "first", "title": "First", "narration": "Primera escena.", "source_ids": [], "figure": figure()},
            {"id": "second", "title": "Second", "narration": "Segunda escena.", "source_ids": [], "body": "Texto"}]


def hooks(app, spoken):
    app.state.short_hooks = {"synthesize": lambda voice, text: (spoken.append(text), (fake_wav(text), "synthetic-tts"))[1],
                             "transcribe": lambda *_: None}
    def timeline(run):
        tracks = run.build_tracks("16:9")
        assert "ken_burns" not in tracks[0]["clips"][0]
        assert run._finishing()["framing"] == "fit"
        run.state["done"].update(timeline={"complete": True})
    app.state.production_hooks["stage_hooks"] = {"timeline": timeline}


def state_after(c, app, body):
    response = c.post("/api/explainers", json={**body, "run": True})
    assert response.status_code == 200, response.text
    result = response.json()
    job = wait_job(c, result["job"]["id"])
    assert job["state"] == "done", job
    return productions.load_state(app.state.store.data_dir, result["production"]["slug"])


def test_capture_replacement_keeps_voice_other_page_and_exact_snapshot(client, tmp_path):
    c, app, _ = client
    spoken = []
    hooks(app, spoken)
    first = state_after(c, app, {"action": "create", "name": "Portable", "scenes": scenes()})
    before = first["done"]["visuals"]["items"]
    asset = app.state.store.get_asset(before["first"]["asset_id"])
    assert asset["recipe"]["figure"] == figure()
    assert (app.state.store.data_dir / asset["file_path"]).read_bytes() == base64.b64decode(figure()["preview"]["data"])
    assert (asset["width"], asset["height"]) == (64, 40)
    modified = scenes()
    modified[0]["figure"] = figure("open")  # Same pixels/source; distinct captured state.
    second = state_after(c, app, {"action": "edit", "production": first["slug"], "base_revision": 1, "scenes": modified})
    after = second["done"]["visuals"]["items"]
    assert after["first"]["asset_id"] != before["first"]["asset_id"]
    assert after["second"] == before["second"]
    assert spoken == ["Primera escena.", "Segunda escena."]
    reverted = state_after(c, app, {"action": "edit", "production": first["slug"], "scenes": scenes()})
    assert reverted["done"]["visuals"]["items"] == before
    bundle = c.get(f"/api/productions/{first['slug']}/explainer.json").json()
    bundle["scenes"][0]["asset_id"] = "a_nonexistent_in_other_profile"
    # A genuinely separate store, not a second import into the same image library.
    other = create_app(tmp_path / "other-profile", port=8815)
    try:
        hooks(other, [])
        with TestClient(other, base_url="http://127.0.0.1:8815") as peer:
            imported = state_after(peer, other, {"action": "import", "bundle": bundle})
            scene = imported["spec"]["explainer"]["scenes"][0]
            assert scene["figure"] == figure() and "asset_id" not in scene
            fresh_id = imported["done"]["visuals"]["items"]["first"]["asset_id"]
            assert fresh_id != before["first"]["asset_id"]
            fresh = other.state.store.get_asset(fresh_id)
            assert fresh["recipe"]["figure"] == figure()
            assert (other.state.store.data_dir / fresh["file_path"]).read_bytes() == base64.b64decode(figure()["preview"]["data"])
    finally:
        other.state.shutdown()


@pytest.mark.parametrize("change", [
    lambda f: f.update(format="other/1"),
    lambda f: f.update(document_id="../other"),
    lambda f: f.update(revision=True),
    lambda f: f.update(revision=0),
    lambda f: f.update(content={}),
    lambda f: f.update(state_id="missing"),
    lambda f: f["content"]["diagram"].update(type="flowchart"),
    lambda f: f.update(page_id="page-1"),
    lambda f: f.update(origin={"editor_url": "javascript:alert(1)"}),
    lambda f: f.update(origin={"editor_url": "https://user:secret@example.com"}),
    lambda f: f["preview"].update(mime="image/jpeg"),
    lambda f: f["preview"].update(data="not base64"),
    lambda f: f["preview"].update(data=base64.b64encode(b"not a PNG").decode()),
])
def test_invalid_capture_never_changes_saved_project(tmp_path, change):
    first = explainers.create(tmp_path, "Atomic", scenes=scenes())
    path = productions.production_dir(tmp_path, first["slug"]) / "state.json"
    before = path.read_bytes()
    changed = scenes()
    change(changed[0]["figure"])
    with pytest.raises(Exception, match="Scene 'first'"):
        explainers.update(tmp_path, first["slug"], scenes=changed)
    assert path.read_bytes() == before


def test_legacy_page_and_future_optional_metadata_are_preserved():
    page = figure()
    del page["state_id"]
    page["page_id"] = "page-1"
    page["content"]["view"] = {"pages": [{"id": "page-1", "width": 800, "height": 600}]}
    page["future_metadata"] = {"note": "se conserva íntegro"}
    expected = deepcopy(page)
    result = figures.validate(page)
    assert result == expected
    result["future_metadata"]["note"] = "changed"
    assert page == expected
