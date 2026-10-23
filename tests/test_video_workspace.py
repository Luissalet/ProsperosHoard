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
    ("ltx23", "ltx23_i2v", "length", 97),
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


def test_structural_guide_reaches_ltx_with_camera_action_and_valid_frame_grid(monkeypatch):
    monkeypatch.setattr(spaces, "inputs_of", lambda *args: {
        "start": [("image", "reference")], "motion": [("video", "guide")],
    })
    graph = {"nodes": [{"id": "clip", "type": "video", "data": {
        "motion_engine": "ltx23_control", "seconds": 3.3, "prompt": "Blue jacket on the circular stage",
        "motion_prompt": "Two sharp steps and a clockwise camera arc", "motion_start_s": 1.25,
        "pose_strength": 0.75, "seed": 9,
    }}], "edges": []}
    body = spaces.plan_node(None, graph, {}, "clip")[0]["body"]
    assert body["template"] == "ltx23_control"
    assert body["driving_asset_id"] == "guide" and body["driving_start_s"] == 1.25
    assert body["template_params"] == {"length": 81, "control_strength": 0.75}
    assert body["prompt"] == "Blue jacket on the circular stage. Two sharp steps and a clockwise camera arc"
    assert body["reference_asset_id"] == "reference" and body["seed"] == 9


@pytest.mark.parametrize("appearance_length", [3890, 3900])
def test_ltx_control_preserves_the_whole_action_with_a_long_appearance(monkeypatch, appearance_length):
    monkeypatch.setattr(spaces, "inputs_of", lambda *args: {
        "start": [("image", "reference")], "motion": [("video", "guide")],
    })
    action = "Two sharp steps then a clockwise camera arc" + "!" * (3800 if appearance_length == 3900 else 0)
    appearance = "A" * appearance_length
    graph = {"nodes": [{"id": "clip", "type": "video", "data": {
        "motion_engine": "ltx23_control", "prompt": appearance, "motion_prompt": action,
    }}], "edges": []}
    prompt = spaces.plan_node(None, graph, {}, "clip")[0]["body"]["prompt"]
    assert prompt.endswith(action) and len(prompt) <= 4000
    if appearance_length == 3890:
        assert prompt == appearance + ". " + action


@pytest.mark.parametrize("appearance_length,action_length", [(20, 3950), (3950, 20), (0, 4000), (3950, 3950)])
def test_ltx_control_budgets_full_fields_only_after_combining_them(monkeypatch, appearance_length, action_length):
    monkeypatch.setattr(spaces, "inputs_of", lambda *args: {
        "start": [("image", "reference")], "motion": [("video", "guide")],
    })
    appearance, action = "A" * appearance_length, "M" * (action_length - 4) + "TURN"
    graph = {"nodes": [{"id": "clip", "type": "video", "data": {
        "motion_engine": "ltx23_control", "prompt": appearance, "motion_prompt": action,
    }}], "edges": []}
    prompt = spaces.plan_node(None, graph, {}, "clip")[0]["body"]["prompt"]
    assert len(prompt) <= 4000 and prompt.endswith(action)
    if appearance_length and appearance_length + action_length + 2 <= 4000:
        assert prompt == appearance + ". " + action


@pytest.mark.parametrize("template", ["ltx23_i2v", "ltx23_control"])
def test_ltx_checkpoint_override_reaches_all_three_loaders(template):
    from prosperos_hoard import comfy_driver
    workflow, spec = comfy_driver.load_template(template)
    applied = comfy_driver.apply_params(workflow, spec, {"checkpoint": "alternate_ltx.safetensors"})
    loaders = [n for n in applied.values() if "ckpt_name" in n["inputs"]]
    assert len(loaders) == 3
    assert all(n["inputs"]["ckpt_name"] == "alternate_ltx.safetensors" for n in loaders)


@pytest.mark.parametrize("template", ["ltx23_i2v", "ltx23_control"])
def test_ltx_lease_matches_the_renderer_class_without_resolving_an_image_engine(template, monkeypatch):
    from contextlib import contextmanager
    from prosperos_hoard import comfy_driver, gpu_lease
    from prosperos_hoard.backend import DEFAULT_VRAM_ESTIMATES_MB
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")
    _, spec = comfy_driver.load_template(template)
    job = {"id": "ltx-lease-test", "type": "generate_image", "params": {"template": template}}
    def unexpected():
        pytest.fail("A video lease must not depend on the image engine")
    requests, released = [], []
    @contextmanager
    def fake_lease(**kwargs):
        requests.append(kwargs)
        try:
            yield None
        finally:
            released.append(True)
    with gpu_lease.hold(job, lambda: DEFAULT_VRAM_ESTIMATES_MB, lease_fn=fake_lease,
                        resolve_image_engine=unexpected):
        assert requests[0]["vram_mb"] == comfy_driver.estimate_vram_mb(spec, DEFAULT_VRAM_ESTIMATES_MB) == 10000
    assert released == [True]


@pytest.mark.parametrize("override", [{"steps": 20}, {"scheduler": "karras"}, {"strength": 0.5}, {"denoise": 0.5}])
def test_fixed_ltx_sampling_does_not_record_unexecuted_settings(store, monkeypatch, override):
    from prosperos_hoard import engine
    monkeypatch.setattr(engine, "run_template", lambda *a, **k: pytest.fail("Ignored settings reached renderer"))
    with pytest.raises(engine.EngineError) as error:
        engine.generate_image(store, None, {"params": {
            "template": "ltx23_i2v", "positive_prompt": "Move", **override,
        }}, lambda *a: None)
    assert error.value.code == "fixed_sampling"


def test_ltx_effective_sampling_and_explicit_control_denoise_reach_the_graph(store, monkeypatch):
    from prosperos_hoard import engine, comfy_driver
    captured = []
    monkeypatch.setattr(engine, "run_template", lambda *a, **k: captured.append(k) or {})
    for template in ("ltx23_i2v", "ltx23_control"):
        params = {"template": template, "positive_prompt": "Move"}
        if template == "ltx23_control":
            params.update(steps=20, scheduler="simple", denoise=0.75)
        engine.generate_image(store, None, {"params": params}, lambda *a: None)
    assert captured[0]["values"]["steps"] == 8
    assert captured[0]["values"]["scheduler"] == "manual_sigmas"
    assert captured[0]["values"]["denoise"] == 1
    workflow, spec = comfy_driver.load_template("ltx23_control")
    applied = comfy_driver.apply_params(workflow, spec, captured[1]["values"])
    assert applied["129:704"]["inputs"]["steps"] == 20
    assert applied["129:704"]["inputs"]["scheduler"] == "simple"
    assert applied["129:704"]["inputs"]["denoise"] == 0.75


@pytest.mark.parametrize("template", ["ltx23_i2v", "ltx23_control"])
def test_ltx_dimensions_duration_and_fps_are_consistent_across_the_whole_graph(template):
    from prosperos_hoard import comfy_driver, engine
    workflow, spec = comfy_driver.load_template(template)
    values = engine._generation_values({"positive_prompt": "Move", "seed": 12,
        "width": 768, "height": 512}, spec["defaults"])
    values.update(length=73, fps=24)
    applied = comfy_driver.apply_params(workflow, spec, values)
    classes = {node["class_type"] for node in applied.values()}
    assert "TextGenerateLTX2Prompt" not in classes and "LoraLoader" not in classes
    if template == "ltx23_i2v":
        assert applied["320:295"]["inputs"] == {"width": 768, "height": 512, "length": 73, "batch_size": 1}
        assert applied["320:290"]["inputs"]["resize_type.width"] == 768
        assert applied["320:290"]["inputs"]["resize_type.height"] == 512
        assert applied["320:305"]["inputs"]["frames_number"] == 73
        assert applied["320:305"]["inputs"]["frame_rate"] == applied["320:310"]["inputs"]["fps"] == 24
    else:
        assert applied["control_length"]["inputs"]["value"] == 73
        assert applied["guide_trim"]["inputs"]["length"] == ["control_length", 0]
        assert applied["129:108"]["inputs"]["length"] == applied["129:101"]["inputs"]["frames_number"] == ["control_length", 0]
        assert applied["129:113"]["inputs"]["value"] == 768
        assert applied["129:98"]["inputs"]["value"] == 512
        assert applied["129:115"]["inputs"]["iclora_parameters"] == ["129:196", 0]


def test_ltx_catalog_requires_its_text_encoder_and_control_lora(backend_with_comfy):
    info = model_inventory(backend_with_comfy)
    loras = info["LoraLoaderModelOnly"]["input"]["required"]["lora_name"][0]
    loras.remove("ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors")
    entries = {e["id"]: e for e in video_engines.catalog(backend_with_comfy, info)["engines"]}
    assert entries["ltx23"]["available"] and not entries["ltx23_control"]["available"]
    info["LTXAVTextEncoderLoader"]["input"]["required"]["text_encoder"][0].clear()
    entries = {e["id"]: e for e in video_engines.catalog(backend_with_comfy, info)["engines"]}
    assert not entries["ltx23"]["available"] and not entries["ltx23_control"]["available"]


@pytest.mark.parametrize("template,height,length,code", [
    ("ltx23_control", 480, 121, "bad_video_size"),
    ("ltx23_i2v", 481, 121, "bad_video_size"),
    ("ltx23_i2v", 480, 120, "bad_video_length"),
    ("ltx23_i2v", 480, True, "bad_video_length"),
])
def test_ltx_invalid_shapes_fail_before_the_renderer(store, monkeypatch, template, height, length, code):
    from prosperos_hoard import engine
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid LTX shape reached the renderer")
    monkeypatch.setattr(engine, "run_template", unexpected)
    with pytest.raises(engine.EngineError) as error:
        engine.generate_image(store, None, {"params": {"template": template,
            "positive_prompt": "Move", "width": 832, "height": height, "length": length}}, lambda *a: None)
    assert error.value.code == code


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


def test_ltx_camera_request_reaches_the_video_prompt(client, monkeypatch):
    from prosperos_hoard import engine, cinema
    c, app, _ = client
    monkeypatch.setattr(engine, "_object_info", lambda *a, **k: model_inventory(app.state.backend))
    seen = []
    def camera_prompt(camera, video=False):
        seen.append(video)
        return "clockwise camera arc" if video else "still frame"
    monkeypatch.setattr(cinema, "camera_prompt", camera_prompt)
    pid = c.post("/api/projects", json={"name": "LTX camera"}).json()["id"]
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(image, "PNG")
    aid = c.post(f"/api/projects/{pid}/import-upload", files={"file": ("reference.png", image.getvalue(), "image/png")}).json()["id"]
    result = c.post(f"/api/projects/{pid}/generate", json={"prompt": "Walk", "reference_asset_id": aid,
        "template": "ltx23_i2v", "camera": {"move": "arc"}})
    assert result.status_code == 200, result.text
    job = app.state.store.get_job(result.json()["job"]["id"])
    assert seen == [True]
    assert "clockwise camera arc" in job["params"]["positive_prompt"]


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


def test_native_node_add_preserves_explicit_zero_coordinates(client):
    c, _, _ = client
    pid = c.post("/api/projects", json={"name": "Zero position"}).json()["id"]
    space = c.post("/api/agent/studio_spaces", params={"project": pid}, json={
        "action": "create", "template": "blank", "name": "Layout",
    }).json()
    response = c.post("/api/agent/studio_spaces", params={"project": pid}, json={
        "action": "edit", "space": space["id"], "ops": [
            {"op": "add_node", "id": "first", "type": "note", "x": 520, "y": 450, "data": {}},
            {"op": "add_node", "id": "second", "type": "note", "x": 0, "y": 450, "data": {}},
        ],
    })
    assert response.status_code == 200, response.text
    graph = c.get(f"/api/spaces/{space['id']}").json()["graph"]
    assert [(n["id"], n["x"], n["y"]) for n in graph["nodes"]] == [("first", 520, 450), ("second", 0, 450)]
