"""The two model-based image edits that run on ComfyUI core nodes:
`upscale` (ESRGAN-family model, x2 or x4) and `remove_background`
(BiRefNet matting to a transparent PNG), against the fake ComfyUI."""

from __future__ import annotations

import json

import pytest
from PIL import Image

from prosperos_hoard import comfy_driver, engine
from prosperos_hoard.backend import DEFAULT_VRAM_ESTIMATES_MB
from prosperos_hoard.jobs import JobQueue


def _no_progress(*_a, **_k):
    return None


def _run_job(store, backend, params, project_id):
    queue = JobQueue(store)
    queue.register("edit_image", lambda job, p: engine.edit_image(store, backend, job, p))
    queue.start()
    try:
        job = queue.enqueue("edit_image", "gpu", params, project_id=project_id)
        return queue.wait_for(job["id"], 60)
    finally:
        queue.stop()


def _source(store, project, size=(320, 200), mode="RGB", name="src.png"):
    path = store.data_dir / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new(mode, size, (200, 60, 90, 255) if mode == "RGBA" else (200, 60, 90))
    img.save(path)
    return engine.import_asset(store, project["id"], path)


def _asset_image(store, asset_id):
    return Image.open(store.data_dir / store.get_asset(asset_id)["file_path"])


def _graph(fake):
    return fake.prompts_seen[-1]


def _node(graph, class_type):
    found = [(nid, n) for nid, n in graph.items() if n["class_type"] == class_type]
    assert len(found) == 1, f"{class_type}: {len(found)} nodes"
    return found[0]


# ------------------------------------------------------------- templates

def test_templates_are_declared_and_consistent():
    for name, vram in (("esrgan_upscale", "esrgan"), ("birefnet_remove_background", "birefnet")):
        workflow, spec = comfy_driver.load_template(name)
        comfy_driver.validate_api_workflow(workflow)
        comfy_driver.validate_param_map(workflow, spec)
        assert spec["vram_class"] == vram and spec["requires_reference"] is True
        assert spec["reference_node"] == "1.image" and workflow["1"]["class_type"] == "LoadImage"
    assert {"esrgan", "birefnet"} <= set(comfy_driver.VRAM_CLASSES)
    assert DEFAULT_VRAM_ESTIMATES_MB["esrgan"] == 2500 and DEFAULT_VRAM_ESTIMATES_MB["birefnet"] == 3500
    _, spec = comfy_driver.load_template("esrgan_upscale")
    assert comfy_driver.estimate_vram_mb(spec, DEFAULT_VRAM_ESTIMATES_MB) == 2500
    assert spec["map"] == {"upscale_model": "2.model_name", "scale_by": "4.scale_by"}
    _, bg_spec = comfy_driver.load_template("birefnet_remove_background")
    assert bg_spec["map"] == {"bg_model": "2.bg_removal_name"}


def test_template_graphs_match_the_specified_nodes():
    up, _ = comfy_driver.load_template("esrgan_upscale")
    assert [up[k]["class_type"] for k in sorted(up)] == ["LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel",
                                                         "ImageScaleBy", "SaveImage"]
    assert up["2"]["inputs"] == {"model_name": "RealESRGAN_x4plus.safetensors"}
    assert up["4"]["inputs"]["upscale_method"] == "lanczos" and up["4"]["inputs"]["scale_by"] == 0.5
    assert up["5"]["inputs"]["filename_prefix"] == "prospero"
    bg, _ = comfy_driver.load_template("birefnet_remove_background")
    assert [bg[k]["class_type"] for k in sorted(bg)] == ["LoadImage", "LoadBackgroundRemovalModel", "RemoveBackground",
                                                         "InvertMask", "JoinImageWithAlpha", "SaveImage"]
    assert bg["2"]["inputs"] == {"bg_removal_name": "birefnet.safetensors"}


# --------------------------------------------------------------- upscale

@pytest.mark.parametrize("scale,scale_by,size", [(2, 0.5, (640, 400)), (4, 1.0, (1280, 800))])
def test_upscale_wires_scale_by_and_model_and_sizes_the_output(store, backend_with_comfy, fake_comfy, project,
                                                               scale, scale_by, size):
    fake, _ = fake_comfy
    src = _source(store, project)
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale", "scale": scale}, project["id"])
    assert done["state"] == "done", done
    assert len(done["outputs"]["asset_ids"]) == 1
    graph = _graph(fake)
    assert _node(graph, "ImageScaleBy")[1]["inputs"]["scale_by"] == scale_by
    assert _node(graph, "ImageScaleBy")[1]["inputs"]["upscale_method"] == "lanczos"
    assert _node(graph, "UpscaleModelLoader")[1]["inputs"]["model_name"] == "RealESRGAN_x4plus.safetensors"
    out = store.get_asset(done["outputs"]["asset_ids"][0])
    assert (out["width"], out["height"]) == size
    recipe = out["recipe"]
    assert recipe["operation"] == "edit_image:upscale" and recipe["template"] == "esrgan_upscale"
    assert recipe["derived_from"] == src["id"] and recipe["input_asset_ids"] == [src["id"]]
    assert recipe["params"]["scale"] == scale and recipe["params"]["scale_by"] == scale_by
    assert isinstance(recipe["params"]["seed"], int)  # injected by run_template, unmapped, still recorded
    assert out["name"] == f"upscaled x{scale}: {src['name']}"


def test_upscale_defaults_to_x2_and_always_makes_one_image(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    src = _source(store, project)
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale", "count": 3}, project["id"])
    assert done["state"] == "done", done
    assert len(done["outputs"]["asset_ids"]) == 1
    assert _node(_graph(fake), "ImageScaleBy")[1]["inputs"]["scale_by"] == 0.5


def test_upscale_seed_is_unmapped_and_harmless(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    src = _source(store, project)
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale", "seed": 1234}, project["id"])
    assert done["state"] == "done", done
    assert store.get_asset(done["outputs"]["asset_ids"][0])["recipe"]["params"]["seed"] == 1234
    assert "seed" not in json.dumps(_graph(fake))


def _cutout_source(store, project, size=(160, 100)):
    """An RGBA image: an opaque square in the middle, transparent around it,
    with a bright green hidden under the transparent part."""
    path = store.data_dir / "inbox" / "cutout.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", size, (0, 255, 0, 0))
    w, h = size
    for x in range(w // 4, 3 * w // 4):
        for y in range(h // 4, 3 * h // 4):
            img.putpixel((x, y), (200, 60, 90, 255))
    img.save(path)
    return engine.import_asset(store, project["id"], path)


def test_upscale_of_a_cut_out_keeps_its_transparency(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    src = _cutout_source(store, project)
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale", "scale": 2}, project["id"])
    assert done["state"] == "done", done
    graph = _graph(fake)
    join_id, join = _node(graph, "JoinImageWithAlpha")
    load_id, _ = _node(graph, "LoadImage")
    assert join["inputs"]["alpha"] == [load_id, 1]  # LoadImage's MASK, not an image
    assert join["inputs"]["image"] == [_node(graph, "ImageScaleBy")[0], 0]
    assert _node(graph, "SaveImage")[1]["inputs"]["images"] == [join_id, 0]
    out = store.get_asset(done["outputs"]["asset_ids"][0])
    assert out["recipe"]["template"] == "esrgan_upscale_alpha"
    assert (out["width"], out["height"]) == (320, 200)
    with _asset_image(store, out["id"]) as img:
        assert "A" in img.getbands()
        assert img.getpixel((5, 5))[3] == 0  # the corner stays transparent
        assert img.getpixel((160, 100))[3] == 255  # the subject stays opaque


def test_upscale_of_an_opaque_rgba_image_uses_the_plain_graph(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    src = _source(store, project, mode="RGBA", name="opaque.png")
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale"}, project["id"])
    assert done["state"] == "done", done
    assert "JoinImageWithAlpha" not in {n["class_type"] for n in _graph(fake).values()}
    assert store.get_asset(done["outputs"]["asset_ids"][0])["recipe"]["template"] == "esrgan_upscale"


def test_alpha_upscale_template_is_consistent():
    workflow, spec = comfy_driver.load_template("esrgan_upscale_alpha")
    comfy_driver.validate_api_workflow(workflow)
    comfy_driver.validate_param_map(workflow, spec)
    plain, plain_spec = comfy_driver.load_template("esrgan_upscale")
    assert spec["map"] == plain_spec["map"] and spec["vram_class"] == "esrgan"
    assert spec["reference_node"] == "1.image" and spec["output_node"] == "5"
    assert {k: v for k, v in workflow.items() if k not in ("5", "6")} == {k: v for k, v in plain.items() if k != "5"}


def test_upscale_reuse_reproduces_the_same_pixels(store, backend_with_comfy, project):
    src = _source(store, project)
    first = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale", "scale": 4}, project["id"])
    up_id = first["outputs"]["asset_ids"][0]
    again = _run_job(store, backend_with_comfy, {"asset_id": up_id, "operation": "reuse"}, project["id"])
    assert again["state"] == "done", again
    a, b = _asset_image(store, up_id), _asset_image(store, again["outputs"]["asset_ids"][0])
    assert a.size == b.size == (1280, 800) and a.tobytes() == b.tobytes()


@pytest.mark.parametrize("bad", [0, 1, 3, 8, -2])
def test_bad_scale_is_refused(store, backend_with_comfy, project, bad):
    src = _source(store, project)
    with pytest.raises(engine.EngineError) as exc:
        engine.edit_image(store, backend_with_comfy, {"id": "j", "project_id": project["id"],
                                                      "params": {"asset_id": src["id"], "operation": "upscale", "scale": bad}},
                          _no_progress)
    assert exc.value.code == "bad_parameter" and "2 or 4" in exc.value.message


def test_too_large_output_is_refused_with_source_size_and_maximum(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    src = _source(store, project, size=(2100, 900), name="wide.png")
    with pytest.raises(engine.EngineError) as exc:
        engine.check_upscale_request(store, store.get_asset(src["id"]), 4)
    assert exc.value.code == "too_large"
    assert "2100x900" in exc.value.message and "8192" in exc.value.message
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale", "scale": 4}, project["id"])
    assert done["state"] == "failed" and "2100x900" in done["message"] and "8192" in done["message"]
    assert not fake.prompts_seen  # refused before anything reached ComfyUI
    # the same source is fine at x2 (4200 px); exactly 8192 is allowed
    assert engine.check_upscale_request(store, store.get_asset(src["id"]), 2) == 2
    edge = _source(store, project, size=(2048, 100), name="edge.png")
    assert engine.check_upscale_request(store, store.get_asset(edge["id"]), 4) == 4


# ------------------------------------------------------ remove_background

def test_remove_background_wiring_and_alpha_kept(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    src = _source(store, project, size=(256, 192))
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "remove_background", "count": 4},
                    project["id"])
    assert done["state"] == "done", done
    assert len(done["outputs"]["asset_ids"]) == 1
    graph = _graph(fake)
    load_id, _ = _node(graph, "LoadImage")
    model_id, model = _node(graph, "LoadBackgroundRemovalModel")
    remove_id, remove = _node(graph, "RemoveBackground")
    invert_id, invert = _node(graph, "InvertMask")
    join_id, join = _node(graph, "JoinImageWithAlpha")
    assert model["inputs"]["bg_removal_name"] == "birefnet.safetensors"
    assert remove["inputs"]["bg_removal_model"] == [model_id, 0] and remove["inputs"]["image"] == [load_id, 0]
    assert invert["inputs"]["mask"] == [remove_id, 0]          # InvertMask sits between RemoveBackground ...
    assert join["inputs"]["alpha"] == [invert_id, 0]           # ... and JoinImageWithAlpha
    assert join["inputs"]["image"] == [load_id, 0]
    save = _node(graph, "SaveImage")[1]
    assert save["inputs"]["images"] == [join_id, 0]

    out = store.get_asset(done["outputs"]["asset_ids"][0])
    assert out["recipe"]["operation"] == "edit_image:remove_background"
    assert out["recipe"]["template"] == "birefnet_remove_background"
    assert out["recipe"]["derived_from"] == src["id"] and out["recipe"]["input_asset_ids"] == [src["id"]]
    assert out["recipe"]["params"]["bg_model"] == "birefnet.safetensors"
    assert out["name"] == f"background removed: {src['name']}"
    with _asset_image(store, out["id"]) as img:
        assert img.mode == "RGBA" and img.size == (256, 192)
        alpha = img.getchannel("A")
        assert alpha.getpixel((128, 96)) == 255      # the subject stays opaque
        assert alpha.getpixel((2, 2)) == 0           # the background is transparent
    assert (store.data_dir / out["thumb_path"]).is_file()
    assert out["mime"] == "image/png"
    assert 0.2 < done["outputs"]["foreground_share"] < 0.6 and "warning" not in done["outputs"]


def test_remove_background_accepts_an_rgba_source(store, backend_with_comfy, project):
    src = _source(store, project, size=(128, 128), mode="RGBA", name="rgba.png")
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "remove_background"}, project["id"])
    assert done["state"] == "done", done
    with _asset_image(store, done["outputs"]["asset_ids"][0]) as img:
        assert img.mode == "RGBA"


def test_bg_removal_model_can_be_named_in_backend_json(store, data_dir, fake_comfy, backend_with_comfy, project):
    fake, port = fake_comfy
    fake.bg_removal_models = ["birefnet_lite.safetensors"]
    src = _source(store, project)
    missing = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "remove_background"}, project["id"])
    assert missing["state"] == "failed" and "birefnet.safetensors" in missing["message"]
    (data_dir / "backend.json").write_text(json.dumps({"comfy": {"url": f"http://127.0.0.1:{port}"},
                                                       "bg_removal_model": "birefnet_lite.safetensors"}), encoding="utf-8")
    backend_with_comfy.reload()
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "remove_background"}, project["id"])
    assert done["state"] == "done", done
    assert _node(_graph(fake), "LoadBackgroundRemovalModel")[1]["inputs"]["bg_removal_name"] == "birefnet_lite.safetensors"


# ------------------------------------------------------- missing models

def _edit(store, backend, project, src, **params):
    return engine.edit_image(store, backend, {"id": "j", "project_id": project["id"],
                                              "params": {"asset_id": src["id"], **params}}, _no_progress)


def test_missing_upscale_model_names_folder_and_installed_options(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    fake.upscale_models = ["4x-UltraSharp.pth", "RealESRGAN_x2plus.pth"]
    src = _source(store, project)
    with pytest.raises(engine.EngineError) as exc:
        _edit(store, backend_with_comfy, project, src, operation="upscale", scale=2)
    assert exc.value.code == "model_missing"
    message = exc.value.message
    assert "RealESRGAN_x4plus.safetensors" in message and "models/upscale_models" in message
    assert "4x-UltraSharp.pth" in message and "RealESRGAN_x2plus.pth" in message
    assert "Real-ESRGAN_repackaged" in message
    assert not fake.prompts_seen
    # the API's job view carries the same text
    failed = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale"}, project["id"])
    assert failed["state"] == "failed" and "models/upscale_models" in failed["message"]


def test_missing_model_with_nothing_installed_says_none(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    fake.upscale_models = []
    fake.bg_removal_models = []
    src = _source(store, project)
    with pytest.raises(engine.EngineError) as up:
        _edit(store, backend_with_comfy, project, src, operation="upscale")
    assert up.value.code == "model_missing" and "installed files: none" in up.value.message
    with pytest.raises(engine.EngineError) as bg:
        _edit(store, backend_with_comfy, project, src, operation="remove_background")
    assert bg.value.code == "model_missing" and "models/background_removal" in bg.value.message
    assert "BiRefNet" in bg.value.message and "installed files: none" in bg.value.message


def test_missing_background_model_lists_installed_options(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    fake.bg_removal_models = ["rmbg_2.safetensors"]
    src = _source(store, project)
    with pytest.raises(engine.EngineError) as exc:
        _edit(store, backend_with_comfy, project, src, operation="remove_background")
    assert exc.value.code == "model_missing"
    assert "birefnet.safetensors" in exc.value.message and "rmbg_2.safetensors" in exc.value.message


def test_upscale_model_can_be_chosen_among_the_installed_ones(store, backend_with_comfy, fake_comfy, project):
    fake, _ = fake_comfy
    fake.upscale_models = ["RealESRGAN_x4plus.safetensors", "4x-UltraSharp.safetensors"]
    src = _source(store, project)
    done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "upscale", "scale": 2,
                                                "model": "4x-UltraSharp.safetensors"}, project["id"])
    assert done["state"] == "done", done
    assert _node(_graph(fake), "UpscaleModelLoader")[1]["inputs"]["model_name"] == "4x-UltraSharp.safetensors"
    with pytest.raises(engine.EngineError) as exc:
        _edit(store, backend_with_comfy, project, src, operation="upscale", model="not_installed.pth")
    assert exc.value.code == "model_missing" and "4x-UltraSharp.safetensors" in exc.value.message


def test_a_non_image_source_is_refused(store, backend_with_comfy, project):
    import wave

    path = store.data_dir / "inbox" / "tone.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    src = engine.import_asset(store, project["id"], path)
    with pytest.raises(engine.EngineError) as exc:
        _edit(store, backend_with_comfy, project, src, operation="remove_background")
    assert exc.value.code == "not_an_image"


# ------------------------------------------------------- REST / agent

def _api_project(c):
    return c.post("/api/agent/studio_create_project", json={"name": "Model edits"}).json()["id"]


def _api_source(c, tmp_path, size=(200, 120)):
    allowed = tmp_path / "allowed"
    Image.new("RGB", size, (30, 120, 200)).save(allowed / "s.png")
    project_id = _api_project(c)
    r = c.post("/api/agent/studio_import", json={"path": str(allowed / "s.png")}, params={"project": project_id})
    assert r.status_code == 200, r.text
    body = r.json()
    return project_id, body["id"]


def test_rest_and_agent_routes_run_both_operations(client, tmp_path):
    c, app, _ = client
    _, asset_id = _api_source(c, tmp_path)
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "upscale", "scale": 4, "wait_s": 30})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["state"] == "done", job
    up = app.state.store.get_asset(job["outputs"]["asset_ids"][0])
    assert (up["width"], up["height"]) == (800, 480)
    r = c.post("/api/agent/studio_edit_image", json={"asset_id": asset_id, "operation": "remove_background", "wait_s": 30})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["state"] == "done", job
    cut = app.state.store.get_asset(job["asset_ids"][0])
    assert cut["recipe"]["operation"] == "edit_image:remove_background"
    with _asset_image(app.state.store, cut["id"]) as img:
        assert img.mode == "RGBA"


def test_rest_refuses_bad_scale_too_large_and_unknown_operations(client, tmp_path):
    c, _, _ = client
    _, asset_id = _api_source(c, tmp_path, size=(2100, 100))
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "upscale", "scale": 3})
    assert r.status_code == 400 and r.json()["error"] == "bad_parameter"
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "upscale", "scale": 4})
    assert r.status_code == 400 and r.json()["error"] == "too_large"
    assert "2100x100" in r.json()["message"] and "8192" in r.json()["message"]
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "sharpen"})
    assert r.status_code == 400 and r.json()["error"] == "bad_operation"
    for op in ("img2img", "inpaint", "hires", "vary", "reuse", "upscale", "remove_background"):
        assert op in r.json()["message"]


def test_existing_operations_keep_their_validation(client, tmp_path):
    c, _, _ = client
    _, asset_id = _api_source(c, tmp_path)
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "inpaint"})
    assert r.status_code == 400 and r.json()["error"] == "mask_required"
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "hires"})
    assert r.status_code == 400 and r.json()["error"] == "hires_needs_recipe"
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "reuse"})
    assert r.status_code == 400 and r.json()["error"] == "not_reproducible"
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "img2img", "prompt": "neon", "wait_s": 30})
    assert r.status_code == 200 and r.json()["job"]["state"] == "done", r.text
    # a scale sent with another operation is ignored, not an error
    r = c.post(f"/api/assets/{asset_id}/edit", json={"asset_id": asset_id, "operation": "img2img", "scale": 7, "wait_s": 30})
    assert r.status_code == 200 and r.json()["job"]["state"] == "done", r.text


def test_an_older_comfyui_without_the_nodes_is_reported_not_crashed(store, backend_with_comfy, project, monkeypatch):
    real = engine._object_info(backend_with_comfy)
    old = {k: v for k, v in real.items() if k not in ("LoadBackgroundRemovalModel", "RemoveBackground")}
    monkeypatch.setattr(engine, "_object_info", lambda backend: old)
    src = _source(store, project)
    with pytest.raises(engine.EngineError) as exc:
        _edit(store, backend_with_comfy, project, src, operation="remove_background")
    assert exc.value.code == "comfy_validation"
    assert "LoadBackgroundRemovalModel" in exc.value.message and "RemoveBackground" in exc.value.message


def test_remove_background_says_when_nothing_was_kept(store, backend_with_comfy, fake_comfy, project):
    """Seen on a real street scene: the model kept no pixel, and a fully transparent picture looked like a result."""
    fake, _ = fake_comfy
    fake.cutout_empty = True
    try:
        src = _source(store, project, size=(128, 96))
        done = _run_job(store, backend_with_comfy, {"asset_id": src["id"], "operation": "remove_background"}, project["id"])
    finally:
        fake.cutout_empty = False
    assert done["state"] == "done", done
    assert done["outputs"]["foreground_share"] == 0
    assert done["outputs"]["warning"].startswith("no_subject_found")
