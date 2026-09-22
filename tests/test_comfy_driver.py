import pytest

from prosperos_hoard import comfy_driver


def test_load_and_apply_params_round_trip():
    workflow, spec = comfy_driver.load_template("sdxl_txt2img")
    values = {
        "checkpoint": "sd_xl_base_1.0.safetensors", "positive_prompt": "a cat", "negative_prompt": "blurry",
        "width": 768, "height": 768, "seed": 42, "steps": 25, "cfg": 6.0, "sampler": "euler_a", "scheduler": "normal",
    }
    wf = comfy_driver.apply_params(workflow, spec, values)
    assert wf["6"]["inputs"]["text"] == "a cat"
    assert wf["7"]["inputs"]["text"] == "blurry"
    assert wf["5"]["inputs"]["width"] == 768
    assert wf["3"]["inputs"]["seed"] == 42
    assert wf["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    # original template untouched (apply_params must deep copy)
    assert workflow["6"]["inputs"]["text"] == ""


def test_linked_seeds_hires():
    workflow, spec = comfy_driver.load_template("sdxl_hires")
    wf = comfy_driver.apply_params(workflow, spec, {"seed": 99})
    assert wf["3"]["inputs"]["seed"] == 99
    assert wf["13"]["inputs"]["seed"] == 99


def test_validate_against_object_info_rejects_unknown_checkpoint():
    _, spec = comfy_driver.load_template("sdxl_txt2img")
    object_info = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["only_this_one.safetensors"]]}}}}
    with pytest.raises(comfy_driver.ValidationError) as exc:
        comfy_driver.validate_against_object_info(spec, {"checkpoint": "missing.safetensors"}, object_info)
    assert "missing.safetensors" in str(exc.value)
    assert "only_this_one.safetensors" in str(exc.value)


def test_validate_against_object_info_accepts_known_checkpoint():
    _, spec = comfy_driver.load_template("sdxl_txt2img")
    object_info = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["sd_xl_base_1.0.safetensors"]]}}}}
    comfy_driver.validate_against_object_info(spec, {"checkpoint": "sd_xl_base_1.0.safetensors"}, object_info)


def test_ui_format_workflow_rejected_on_propose():
    ui_workflow = {"nodes": [{"id": 1}], "links": []}
    with pytest.raises(comfy_driver.WorkflowError):
        comfy_driver.propose_param_map(ui_workflow)


def test_propose_param_map_detects_known_nodes():
    custom = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "hello", "clip": ["1", 1]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20, "cfg": 7, "sampler_name": "euler",
                                                    "scheduler": "normal", "denoise": 1.0, "model": ["1", 0],
                                                    "positive": ["2", 0], "negative": ["2", 0], "latent_image": ["4", 0]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
    }
    spec = comfy_driver.propose_param_map(custom)
    assert spec["auto_detected"] is True
    assert spec["map"]["checkpoint"] == "1.ckpt_name"
    assert spec["map"]["seed"] == "3.seed"
    assert spec["output_node"] == "9"
    assert spec["checkpoint_node"] == "CheckpointLoaderSimple"


def test_estimate_vram_mb_uses_table():
    _, spec = comfy_driver.load_template("svd_img2vid")
    assert comfy_driver.estimate_vram_mb(spec, {"svd": 10000, "sdxl": 7000, "sd15": 3500}) == 10000


def test_template_names_cannot_escape_the_workflow_folders(tmp_path):
    for name in ("../api", "..\\x", "/etc/passwd", "sdxl_txt2img.json", "wf_../../x", "", "A" * 60):
        with pytest.raises(comfy_driver.WorkflowError):
            comfy_driver.load_template(name, tmp_path)


def test_checkpoint_resolution_by_stem_and_family():
    avail = ["sd_xl_base_1.0.safetensors", "v1-5-pruned-emaonly-fp16.safetensors"]
    assert comfy_driver.resolve_checkpoint("sd_xl_base_1.0", avail, None, "sdxl") == "sd_xl_base_1.0.safetensors"
    assert comfy_driver.resolve_checkpoint("SD_XL_BASE_1.0.SAFETENSORS", avail, None, "sdxl") == "sd_xl_base_1.0.safetensors"
    assert comfy_driver.resolve_checkpoint(None, avail, "missing.safetensors", "sd15") == "v1-5-pruned-emaonly-fp16.safetensors"
    assert comfy_driver.resolve_checkpoint("dreamy.safetensors", avail, None, "sdxl") is None


def test_validation_names_missing_nodes_and_bad_sampler():
    workflow, spec = comfy_driver.load_template("sdxl_txt2img")
    info = {k: {"input": {"required": {}}} for k in ("KSampler", "CLIPTextEncode", "EmptyLatentImage", "VAEDecode", "SaveImage")}
    info["CheckpointLoaderSimple"] = {"input": {"required": {"ckpt_name": [["sd_xl_base_1.0.safetensors"]]}}}
    info["KSampler"] = {"input": {"required": {"sampler_name": [["euler", "euler_ancestral"]], "scheduler": [["normal"]]}}}
    out = comfy_driver.validate_against_object_info(spec, {"sampler": "euler"}, info, workflow)
    assert out["checkpoint"] == "sd_xl_base_1.0.safetensors"
    with pytest.raises(comfy_driver.ValidationError) as exc:
        comfy_driver.validate_against_object_info(spec, {"sampler": "euler_a"}, info, workflow)
    assert "euler_ancestral" in str(exc.value)
    del info["VAEDecode"]
    with pytest.raises(comfy_driver.ValidationError) as exc:
        comfy_driver.validate_against_object_info(spec, {}, info, workflow)
    assert "VAEDecode" in str(exc.value)


def test_propose_map_classifies_positive_and_negative_prompts():
    workflow, _ = comfy_driver.load_template("sdxl_txt2img")
    spec = comfy_driver.propose_param_map(workflow)
    assert spec["map"]["positive_prompt"] == "6.text"
    assert spec["map"]["negative_prompt"] == "7.text"
    assert spec["map"]["width"] == "5.width"
    svd, _ = comfy_driver.load_template("svd_img2vid")
    s2 = comfy_driver.propose_param_map(svd)
    assert s2["kind"] == "video" and s2["vram_class"] == "svd" and s2["reference_node"] == "1.image"


def test_template_hash_changes_with_the_graph():
    workflow, spec = comfy_driver.load_template("sdxl_txt2img")
    h1 = comfy_driver.template_hash(workflow, spec)
    workflow["3"]["inputs"]["steps"] = 99
    assert comfy_driver.template_hash(workflow, spec) != h1
