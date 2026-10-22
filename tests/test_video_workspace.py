import json
import io

import pytest
from PIL import Image

from prosperos_hoard import productions, spaces, video_engines


def test_quick_video_is_a_connected_editable_graph():
    graph = spaces.template_graph("quick_video")
    assert [(n["id"], n["type"]) for n in graph["nodes"]] == [("reference", "asset"), ("clip", "video")]
    assert graph["edges"][0]["target_handle"] == "start"
    assert graph["nodes"][1]["data"]["prompt"] == ""
    assert spaces.template_graph("quick_video") == graph


@pytest.mark.parametrize("engine,template,parameter,expected", [
    ("wan14b", "wan22_i2v_14b", "seconds", 4),
    ("wan", "wan22_ti2v", "length", 97),
    ("svd", "svd_img2vid", "fps", 6.25),
])
def test_node_engine_and_duration_reach_the_renderer(monkeypatch, engine, template, parameter, expected):
    monkeypatch.setattr(spaces, "inputs_of", lambda *args: {"start": [("image", "reference")]})
    graph = {"nodes": [{"id": "clip", "type": "video", "data": {"engine": engine, "seconds": 4, "prompt": "walk and wave", "lightning": False}}], "edges": []}
    result = spaces.plan_node(None, graph, {}, "clip")[0]["body"]
    assert result["template"] == template
    assert result["template_params"][parameter] == expected
    assert result["prompt"] == "walk and wave"
    if engine == "wan14b":
        assert result["template_params"]["lightning"] is False


def model_inventory(backend):
    from prosperos_hoard import comfy_driver
    info = {}
    for _, _, name, _ in video_engines.CHOICES:
        workflow, _ = comfy_driver.load_template(name, backend.data_dir)
        for node in workflow.values():
            required = info.setdefault(node["class_type"], {"input": {"required": {}}})["input"]["required"]
            for field, value in node.get("inputs", {}).items():
                if isinstance(value, str) and (field in video_engines.MODEL_FIELDS or node["class_type"] == "ModelPatchLoader" and field == "name"):
                    choices = required.setdefault(field, [[]])[0]
                    if value not in choices:
                        choices.append(value)
    return info


def test_catalog_checks_both_experts_and_never_loads_a_model(backend_with_comfy, monkeypatch):
    backend = backend_with_comfy
    info = model_inventory(backend)
    def get_info(_backend, autostart=None):
        assert autostart is False
        return info
    monkeypatch.setattr(video_engines.engine, "_object_info", get_info)
    first = video_engines.catalog(backend)
    assert first["auto_resolves_to"] == "wan14b"
    low = next(x for x in info["UNETLoader"]["input"]["required"]["unet_name"][0] if "i2v_low_noise" in x.lower())
    info["UNETLoader"]["input"]["required"]["unet_name"][0].remove(low)
    second = video_engines.catalog(backend)
    assert second["auto_resolves_to"] == "wan"
    wan14b = next(x for x in second["engines"] if x["id"] == "wan14b")
    assert not wan14b["available"] and low in wan14b["missing"]


def test_cached_inventory_is_not_live_availability(backend_with_comfy, monkeypatch):
    backend = backend_with_comfy
    info = model_inventory(backend)
    path = video_engines.engine.object_info_cache_path(backend.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info), encoding="utf-8")
    def offline(*args, **kwargs):
        raise ConnectionError("ComfyUI is offline")
    monkeypatch.setattr(video_engines.engine, "_object_info", offline)
    result = video_engines.catalog(backend)
    assert not result["live"] and result["auto_resolves_to"] is None
    assert any(x["installed"] for x in result["engines"])
    assert not any(x["available"] for x in result["engines"])


def test_auto_executes_the_same_complete_engine_the_catalog_announces(client, monkeypatch):
    from prosperos_hoard import engine
    c, app, _ = client
    info = model_inventory(app.state.backend)
    names = info["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0]
    names[:] = [name for name in names if "low" not in name.lower()]
    monkeypatch.setattr(engine, "_object_info", lambda *a, **k: info)
    assert c.get("/api/video-engines").json()["auto_resolves_to"] == "wan"
    pid = c.post("/api/projects", json={"name": "Complete engine"}).json()["id"]
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(image, "PNG")
    aid = c.post(f"/api/projects/{pid}/import-upload", files={"file": ("reference.png", image.getvalue(), "image/png")}).json()["id"]
    result = c.post(f"/api/projects/{pid}/generate", json={"prompt": "walk", "reference_asset_id": aid, "template": "auto_clip"})
    assert result.status_code == 200, result.text
    job = app.state.store.get_job(result.json()["job"]["id"])
    assert job["params"]["template"] == "wan22_ti2v"


def test_new_productions_continue_without_review_and_review_stays_opt_in():
    assert productions.DEFAULT_SETTINGS["animatic_autocontinue"] is True
    assert productions.DEFAULT_SETTINGS["song_review"] is False
    assert productions.normalise_settings({"song_review": True})["song_review"] is True


def test_failed_render_keeps_the_actual_error_in_the_node():
    class FailedStudio:
        def job(self, _id):
            return {"state": "failed", "message": "Video model ran out of memory"}
    outputs, errors = spaces._wait(FailedStudio(), ["job1"], 0, 1)
    assert outputs == [] and errors == ["Video model ran out of memory"]


def test_review_ids_are_unique_in_one_clock_tick(data_dir, monkeypatch):
    import time
    from prosperos_hoard import shot_review
    from prosperos_hoard.hoard_link import ids
    productions.create_production(data_dir, "Review", {"lead": {"name": "Review", "look": "A blue shape"},
        "shots": [{"key": "1", "prompt": "Subject", "start_s": 0, "end_s": 3}]}, slug="review")
    monkeypatch.setattr(ids.time, "time", lambda: 2000000000)
    first = shot_review.add_shot_comment(data_dir, "review", "1", 1, "First")
    second = shot_review.add_shot_comment(data_dir, "review", "1", 2, "Second")
    assert first["id"] != second["id"]
    shot_review.resolve_shot_comment(data_dir, "review", second["id"])
    comments = shot_review.list_shot_comments(data_dir, "review")
    assert [(c["content"], c["resolved"]) for c in comments] == [("First", False), ("Second", True)]


def test_duration_change_rebuilds_cut_without_regenerating_approved_shots(data_dir):
    from prosperos_hoard import style_profiles
    productions.create_production(data_dir, "Review", {"lead": {"name": "Review", "look": "A blue shape"},
        "shots": [{"key": "1", "prompt": "Subject", "start_s": 0, "end_s": 3, "locked": True}]}, slug="review")
    state = productions.load_state(data_dir, "review")
    state["done"]["timeline"] = {"16:9": {"timeline_id": "old-3-second-cut"}}
    state["done"]["clips"] = {"items": {"1": "approved-clip"}, "complete": True}
    state["status"] = "done"
    productions.save_state(data_dir, state)
    result = style_profiles.apply_style_profile_to_production(data_dir, "review", {"id": "sp_review", "shots": [{"relative_duration": 1}]}, {"target_duration_s": 6})
    after = productions.load_state(data_dir, "review")
    assert result["status"] == "queued" and not result["changed"]
    assert "timeline" not in after["done"]
    assert after["done"]["clips"]["items"] == {"1": "approved-clip"}
    assert after["spec"]["shots"][0]["end_s"] == 3


def test_api_creates_quick_video_and_exposes_real_catalog(client):
    c, _, _ = client
    pid = c.post("/api/projects", json={"name": "Quick video"}).json()["id"]
    response = c.post(f"/api/projects/{pid}/spaces", json={"name": "Test", "template": "quick_video"})
    assert response.status_code == 200, response.text
    space = response.json()
    assert len(space["graph"]["nodes"]) == 2
    saved = c.get(f"/api/spaces/{space['id']}").json()
    assert saved["graph"] == space["graph"]
    catalog = c.get("/api/video-engines")
    assert catalog.status_code == 200
    assert {x["id"] for x in catalog.json()["engines"]} >= {"wan14b", "wan", "svd", "animate"}
