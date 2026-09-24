"""Qwen-Image 2.1 int8: the two new built-in templates (qwen21_txt2img,
qwen21_edit) against the fake backend, and the `auto|qwen21|flux|sdxl`
image engine choice (spec item 3 of prospero-qwen21.md)."""

from __future__ import annotations

from prosperos_hoard import comfy_driver, engine
from prosperos_hoard.jobs import JobQueue


def _run_job(store, backend, type_, params, project_id):
    queue = JobQueue(store)
    handlers = {"generate_image": engine.generate_image, "edit_image": engine.edit_image}
    queue.register(type_, lambda job, p: handlers[type_](store, backend, job, p))
    queue.start()
    try:
        job = queue.enqueue(type_, "gpu", params, project_id=project_id)
        return queue.wait_for(job["id"], 60)
    finally:
        queue.stop()


def _still(store, backend, project, seed=1):
    return _run_job(store, backend, "generate_image",
                    {"prompt": "a portrait", "positive_prompt": "a portrait", "negative_prompt": "",
                     "width": 512, "height": 512, "seed": seed, "count": 1, "template": "sdxl_txt2img"}, project["id"])


# ------------------------------------------------------------- txt2img

def test_qwen21_txt2img_generates_an_image(store, backend_with_comfy, fake_comfy, project):
    params = {"prompt": "FAROL under a paper lantern, night market", "positive_prompt": "FAROL under a paper lantern, night market",
              "negative_prompt": "", "width": 1024, "height": 1024, "seed": 11, "count": 1, "template": "qwen21_txt2img"}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["kind"] == "image" and asset["width"] == 1024 and asset["height"] == 1024
    assert asset["recipe"]["template"] == "qwen21_txt2img"
    assert asset["recipe"]["image_engine"] == "qwen21"
    server, _ = fake_comfy
    wf = server.prompts_seen[-1]
    assert wf["459:451"]["inputs"]["unet_name"] == "qwen_image_2.1_int8_convrot.safetensors"
    assert wf["459:458"]["inputs"]["sampler_name"] == "euler" and wf["459:458"]["inputs"]["steps"] == 25


def test_qwen21_txt2img_defaults_match_the_spec(store, backend_with_comfy, project):
    workflow, spec = comfy_driver.load_template("qwen21_txt2img")
    assert spec["defaults"]["steps"] == 25 and spec["defaults"]["cfg"] == 1
    assert spec["defaults"]["sampler"] == "euler" and spec["defaults"]["scheduler"] == "simple"


# --------------------------------------------------------------- edit

def test_qwen21_edit_with_two_references(store, backend_with_comfy, fake_comfy, project):
    a = _still(store, backend_with_comfy, project, seed=1)
    b = _still(store, backend_with_comfy, project, seed=2)
    ref1, ref2 = a["outputs"]["asset_ids"][0], b["outputs"]["asset_ids"][0]
    params = {"prompt": "Keep the character from <image1> exactly the same, wearing the jacket from <image2>",
              "positive_prompt": "Keep the character from <image1> exactly the same, wearing the jacket from <image2>",
              "negative_prompt": "", "seed": 21, "count": 1, "template": "qwen21_edit",
              "reference_asset_ids": [ref1, ref2]}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["recipe"]["template"] == "qwen21_edit"
    assert asset["recipe"]["input_asset_ids"] == [ref1, ref2]
    server, _ = fake_comfy
    wf = server.prompts_seen[-1]
    encode = wf["459:474"]["inputs"]
    assert "images.image_1" in encode and "images.image_2" in encode
    assert "images.image_3" not in encode


def test_vary_a_two_reference_edit_keeps_both_references(store, backend_with_comfy, fake_comfy, project):
    a = _still(store, backend_with_comfy, project, seed=5)
    b = _still(store, backend_with_comfy, project, seed=6)
    ref1, ref2 = a["outputs"]["asset_ids"][0], b["outputs"]["asset_ids"][0]
    params = {"prompt": "<image1> wearing the jacket from <image2>", "positive_prompt": "<image1> wearing the jacket from <image2>",
              "negative_prompt": "", "seed": 31, "count": 1, "template": "qwen21_edit", "reference_asset_ids": [ref1, ref2]}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    varied = _run_job(store, backend_with_comfy, "edit_image",
                      {"asset_id": done["outputs"]["asset_ids"][0], "operation": "vary", "seed": 32}, project["id"])
    assert varied["state"] == "done", varied
    asset = store.get_asset(varied["outputs"]["asset_ids"][0])
    assert asset["recipe"]["input_asset_ids"] == [ref1, ref2]
    server, _ = fake_comfy
    encode = server.prompts_seen[-1]["459:474"]["inputs"]
    assert "images.image_1" in encode and "images.image_2" in encode


def test_qwen21_edit_with_a_single_reference_drops_the_unused_slot(store, backend_with_comfy, fake_comfy, project):
    a = _still(store, backend_with_comfy, project, seed=3)
    ref1 = a["outputs"]["asset_ids"][0]
    params = {"prompt": "Keep the character from <image1> exactly the same, now smiling",
              "positive_prompt": "Keep the character from <image1> exactly the same, now smiling",
              "negative_prompt": "", "seed": 22, "count": 1, "template": "qwen21_edit", "reference_asset_ids": [ref1]}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    server, _ = fake_comfy
    wf = server.prompts_seen[-1]
    assert "475" not in wf  # image_2's LoadImage node was dropped, not left dangling
    assert "images.image_2" not in wf["459:474"]["inputs"]


def test_qwen21_edit_needs_at_least_one_reference(store, backend_with_comfy, project):
    params = {"prompt": "x", "positive_prompt": "x", "negative_prompt": "", "seed": 1, "count": 1, "template": "qwen21_edit"}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "failed"
    assert "reference" in done["message"]


def test_qwen21_edit_custom_size_switches_the_canvas(store, backend_with_comfy, fake_comfy, project):
    a = _still(store, backend_with_comfy, project, seed=4)
    ref1 = a["outputs"]["asset_ids"][0]
    params = {"prompt": "Keep the character from <image1> exactly the same, wide shot", "positive_prompt": "x",
              "negative_prompt": "", "seed": 23, "count": 1, "template": "qwen21_edit",
              "reference_asset_ids": [ref1], "width": 1280, "height": 720}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    server, _ = fake_comfy
    wf = server.prompts_seen[-1]
    assert wf["459:468"]["inputs"]["switch"] is True
    assert wf["459:456"]["inputs"]["width"] == 1280 and wf["459:456"]["inputs"]["height"] == 720


# ---------------------------------------------------- engine resolution

def test_edits_need_the_kontext_model_for_flux():
    flux_only = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["flux1-schnell-fp8.safetensors"]]}}}}
    assert engine.resolve_image_engine(flux_only, "auto", "txt2img") == "flux"
    assert engine.resolve_image_engine(flux_only, "auto", "edit") == "sdxl"
    assert engine.resolve_image_engine(flux_only, "flux", "edit") == "sdxl"
    kontext = dict(flux_only, UNETLoader={"input": {"required": {"unet_name": [["flux1-dev-kontext_fp8_scaled.safetensors"]]}}})
    assert engine.resolve_image_engine(kontext, "flux", "edit") == "flux"
    assert engine.resolve_image_engine(kontext, "auto", "edit") == "flux"


def test_resolve_image_engine_auto_prefers_qwen_then_flux_then_sdxl():
    assert engine.resolve_image_engine({}, "auto") == "sdxl"
    flux_only = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["flux1-schnell-fp8.safetensors"]]}}}}
    assert engine.resolve_image_engine(flux_only, "auto") == "flux"
    both = {
        "TextEncodeQwenImage21": {},
        "UNETLoader": {"input": {"required": {"unet_name": [["qwen_image_2.1_int8_convrot.safetensors"]]}}},
        "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["flux1-schnell-fp8.safetensors"]]}}},
    }
    assert engine.resolve_image_engine(both, "auto") == "qwen21"
    assert engine.resolve_image_engine(both, None) == "qwen21"  # "auto" is the default


def test_resolve_image_engine_explicit_choice_falls_back_when_not_installed():
    # asked for qwen21 but only flux is on disk -> flux, not a crash
    flux_only = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["flux1-schnell-fp8.safetensors"]]}}}}
    assert engine.resolve_image_engine(flux_only, "qwen21") == "flux"
    assert engine.resolve_image_engine({}, "qwen21") == "sdxl"
    assert engine.resolve_image_engine({}, "flux") == "sdxl"
    assert engine.resolve_image_engine({}, "sdxl") == "sdxl"
    assert engine.resolve_image_engine({}, "not_a_real_engine") == "sdxl"  # unknown -> auto -> sdxl


def test_generate_image_auto_picks_qwen_when_the_fake_backend_has_it(store, backend_with_comfy, project):
    # the fake backend's real_object_info() reports Qwen-Image 2.1 installed
    # (devtools/fake_comfy.py), so "auto" (the default) reaches for it first
    params = {"prompt": "a lantern", "positive_prompt": "a lantern", "negative_prompt": "", "seed": 5, "count": 1}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["recipe"]["template"] == "qwen21_txt2img" and asset["recipe"]["image_engine"] == "qwen21"


def test_generate_image_engine_param_pins_flux(store, backend_with_comfy, project):
    params = {"prompt": "a lantern", "positive_prompt": "a lantern", "negative_prompt": "", "seed": 5, "count": 1,
              "engine": "flux"}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["recipe"]["template"] == "flux_schnell_txt2img" and asset["recipe"]["image_engine"] == "flux"


# ---------------------------------------------------------- API surface

def test_project_image_engine_setting_drives_the_default_template(client):
    c, _, _ = client
    project_id = c.post("/api/projects", json={"name": "Qwen project"}).json()["id"]
    c.patch(f"/api/projects/{project_id}", json={"image_engine": "sdxl"}).raise_for_status()
    r = c.post(f"/api/agent/studio_generate_image?project={project_id}", json={"prompt": "a lantern", "wait_s": 20})
    body = r.json()
    assert body["engine"] == "sdxl" and body["template"] == "sdxl_txt2img"
    assert body["job"]["state"] == "done", body


def test_project_image_engine_rejects_unknown_value(client):
    c, _, _ = client
    project_id = c.post("/api/projects", json={"name": "Bad engine"}).json()["id"]
    r = c.patch(f"/api/projects/{project_id}", json={"image_engine": "not-an-engine"})
    assert r.status_code >= 400


def test_qwen21_edit_samples_at_full_denoise_even_with_a_reference(store, backend_with_comfy, fake_comfy, project):
    # The references reach Qwen-Image 2.1 through its text encoder and the
    # sampler starts from a fresh latent; the SDXL img2img fallback of 0.6
    # turned a real 1344x768 still into noise texture on the owner's GPU.
    a = _still(store, backend_with_comfy, project, seed=5)
    ref1 = a["outputs"]["asset_ids"][0]
    params = {"prompt": "Keep the character from <image1> exactly the same, now at night", "positive_prompt": "x",
              "negative_prompt": "", "seed": 24, "count": 1, "template": "qwen21_edit",
              "reference_asset_id": ref1, "reference_asset_ids": [ref1], "width": 1344, "height": 768}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    server, _ = fake_comfy
    assert server.prompts_seen[-1]["459:458"]["inputs"]["denoise"] == 1


def test_an_explicit_strength_still_sets_the_qwen_edit_denoise(store, backend_with_comfy, fake_comfy, project):
    a = _still(store, backend_with_comfy, project, seed=6)
    ref1 = a["outputs"]["asset_ids"][0]
    params = {"prompt": "Keep the character from <image1>", "positive_prompt": "x", "negative_prompt": "", "seed": 25,
              "count": 1, "template": "qwen21_edit", "reference_asset_id": ref1, "reference_asset_ids": [ref1],
              "strength": 0.7}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    server, _ = fake_comfy
    assert server.prompts_seen[-1]["459:458"]["inputs"]["denoise"] == 0.7
