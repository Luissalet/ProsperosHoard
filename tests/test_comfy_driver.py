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


def test_ui_format_import_is_converted_and_mapped(tmp_path):
    import json
    from pathlib import Path

    from prosperos_hoard.devtools.fake_comfy import real_object_info

    ui = (Path(__file__).parent / "fixtures" / "comfy" / "flux_schnell.json").read_text(encoding="utf-8")
    spec = comfy_driver.import_custom_workflow(tmp_path, "Flux from the UI", ui, object_info=real_object_info)
    assert spec["converted_from"] == "ui"
    assert spec["map"]["positive_prompt"] == "6.text" and spec["map"]["seed"] == "31.seed"
    saved = json.loads((tmp_path / "workflows" / f"{spec['template']}.json").read_text(encoding="utf-8"))
    assert saved["31"]["class_type"] == "KSampler" and "nodes" not in saved
    # ComfyUI off and nothing cached: an actionable message, not a stack trace
    def unreachable():
        raise RuntimeError("connection refused")
    with pytest.raises(comfy_driver.WorkflowError, match="Export \\(API\\)"):
        comfy_driver.import_custom_workflow(tmp_path, "x", ui, object_info=unreachable)


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


def test_template_hash_covers_linked_params_and_still_accepts_old_recipes():
    workflow, spec = comfy_driver.load_template("ace15_song")
    old = comfy_driver._template_hash_v1(workflow, spec)
    assert comfy_driver.template_hash_matches(old, workflow, spec)  # recorded before the hash grew
    assert comfy_driver.template_hash_matches(comfy_driver.template_hash(workflow, spec), workflow, spec)
    changed = dict(spec, linked_params={"duration": []})
    assert comfy_driver.template_hash(workflow, changed) != comfy_driver.template_hash(workflow, spec)
    assert not comfy_driver.template_hash_matches(comfy_driver.template_hash(workflow, spec), workflow, changed)


def test_style_checkpoint_is_a_preference_an_explicit_one_a_requirement():
    workflow, spec = comfy_driver.load_template("sdxl_txt2img")
    info = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["juggernautXL_v9.safetensors"]]}}}}
    out = comfy_driver.validate_against_object_info(spec, {"checkpoint": None, "checkpoint_preferred": "sd_xl_base_1.0.safetensors"},
                                                    info, None)
    assert out["checkpoint"] == "juggernautXL_v9.safetensors" and "checkpoint_preferred" not in out
    info2 = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["a.safetensors", "sd_xl_base_1.0.safetensors"]]}}}}
    out = comfy_driver.validate_against_object_info(spec, {"checkpoint_preferred": "sd_xl_base_1.0"}, info2, None)
    assert out["checkpoint"] == "sd_xl_base_1.0.safetensors"
    with pytest.raises(comfy_driver.ValidationError):
        comfy_driver.validate_against_object_info(spec, {"checkpoint": "sd_xl_base_1.0.safetensors"}, info, None)


# ---------------------------------------------- param map for custom imports

def test_propose_map_walks_through_guidance_nodes_and_infers_vram():
    kontext, _ = comfy_driver.load_template("flux_kontext_edit")
    spec = comfy_driver.propose_param_map(kontext)
    assert spec["map"]["positive_prompt"] == "192:6.text"  # KSampler <- FluxGuidance <- ReferenceLatent <- encoder
    assert spec["vram_class"] == "kontext"
    qwen, _ = comfy_driver.load_template("qwen21_txt2img")
    spec = comfy_driver.propose_param_map(qwen)
    assert spec["map"]["positive_prompt"] == "459:452.prompt" and spec["vram_class"] == "qwen21"
    wan, _ = comfy_driver.load_template("wan22_ti2v")
    spec = comfy_driver.propose_param_map(wan)
    assert spec["vram_class"] == "wan" and spec["map"]["positive_prompt"] == "6.text"
    assert spec["map"]["negative_prompt"] == "7.text"
    song, _ = comfy_driver.load_template("ace15_song")
    spec = comfy_driver.propose_param_map(song)
    assert spec["kind"] == "audio" and spec["vram_class"] == "ace"
    comfy_driver.validate_param_map(song, spec)  # an audio workflow is a valid custom workflow


def test_propose_map_understands_custom_sampler_guiders_and_extra_seeds():
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPTextEncodeFlux", "inputs": {"clip_l": "a cat", "t5xxl": "a cat", "guidance": 3.5,
                                                              "clip": ["9", 0]}},
        "3": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["2", 0]}},
        "4": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
        "5": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["4", 0], "guider": ["3", 0], "sampler": ["6", 0],
                                                                 "sigmas": ["7", 0], "latent_image": ["8", 0]}},
        "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "7": {"class_type": "BasicScheduler", "inputs": {"model": ["1", 0], "scheduler": "simple", "steps": 20, "denoise": 1.0}},
        "8": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "9": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "a", "clip_name2": "b", "type": "flux"}},
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": 2}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "unused", "clip": ["9", 0]}},
        "20": {"class_type": "SaveImage", "inputs": {"images": ["5", 0], "filename_prefix": "x"}},
    }
    spec = comfy_driver.propose_param_map(wf)
    m = spec["map"]
    assert m["positive_prompt"] == "2.t5xxl" and spec["linked_params"]["positive_prompt"] == ["2.clip_l"]
    assert m["seed"] == "4.noise_seed" and spec["linked_seeds"] == ["10.noise_seed"] and "seed_2" not in m
    assert m["sampler"] == "6.sampler_name" and m["scheduler"] == "7.scheduler" and m["width"] == "8.width"
    assert m["text_1"] == "11.text" and "prompt" not in m  # never collides with the job's raw prompt
    assert spec["vram_class"] == "flux"
    applied = comfy_driver.apply_params(wf, spec, {"positive_prompt": "a dog", "seed": 77})
    assert applied["2"]["inputs"]["t5xxl"] == applied["2"]["inputs"]["clip_l"] == "a dog"
    assert applied["4"]["inputs"]["noise_seed"] == applied["10"]["inputs"]["noise_seed"] == 77


def test_custom_workflow_without_a_positive_prompt_refuses_to_ignore_the_prompt(store, project):
    from prosperos_hoard import engine

    workflow, _ = comfy_driver.load_template("sdxl_txt2img")
    spec = comfy_driver.import_custom_workflow(store.data_dir, "no prompt", workflow)
    mapping = {k: v for k, v in spec["map"].items() if k != "positive_prompt"}
    mapping["text_1"] = spec["map"]["positive_prompt"]
    comfy_driver.update_custom_workflow(store.data_dir, spec["template"], {"map": mapping})
    job = {"id": "j", "project_id": project["id"],
           "params": {"prompt": "a cat", "positive_prompt": "a cat", "template": spec["template"]}}
    with pytest.raises(engine.EngineError, match="positive_prompt"):
        engine.generate_image(store, None, job, lambda *a, **k: None)
