"""Workflow preflight: node classes and model files of a ComfyUI workflow against /object_info (fixture object_info)."""
from __future__ import annotations

from prosperos_hoard import productions as productions_mod
from prosperos_hoard import workflow_preflight as wp
from prosperos_hoard.store import Store

INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["sd_xl_base_1.0.safetensors"]]}}},
    "LoraLoader": {"input": {"required": {"lora_name": [["styles\\ink.safetensors"]], "strength_model": ["FLOAT", {}]}}},
    "UNETLoader": {"input": {"required": {"unet_name": ["COMBO", {"options": ["qwen_image.safetensors"]}]}}},
    "SaveImage": {"input": {"required": {"filename_prefix": ["STRING", {}]}}},
    "Note": {"input": {"required": {"text": ["STRING", {}]}}},
}
WORKFLOW = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "dreamy_v9.safetensors"}},
    "2": {"class_type": "LoraLoader", "inputs": {"lora_name": "styles/ink.safetensors", "model": ["1", 0]}},
    "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan2.2_i2v_high_noise.safetensors"}},
    "4": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": 16}},
    "5": {"class_type": "WanVideoSampler", "inputs": {}},
    "6": {"class_type": "Note", "inputs": {"text": "weights/readme.pt"}},
}


def test_missing_nodes_and_models_with_their_pack_and_folder():
    r = wp.check(WORKFLOW, INFO, "live", label="test")
    nodes = {n["class_type"]: n for n in r["nodes"]}
    assert nodes["VHS_VideoCombine"]["ok"] is False and nodes["VHS_VideoCombine"]["pack"]["name"] == "ComfyUI-VideoHelperSuite"
    assert nodes["WanVideoSampler"]["pack"]["name"] == "ComfyUI-WanVideoWrapper"
    assert nodes["LoraLoader"]["ok"] is True and "pack" not in nodes["LoraLoader"]
    models = {m["input"]: m for m in r["models"]}
    assert models["lora_name"]["ok"] is True                       # Windows backslashes in ComfyUI's list still match
    assert models["unet_name"]["ok"] is False and models["unet_name"]["folder"] == "diffusion_models"
    assert models["ckpt_name"]["ok"] is False and models["ckpt_name"]["fallback"] == "sd_xl_base_1.0.safetensors"
    assert "text" not in models                                     # a free-text input ending in .pt is not a model file
    assert r["ok"] is False and r["missing_nodes"] == 2 and r["missing_models"] == 2
    text = r["copy_text"]
    assert "wan2.2_i2v_high_noise.safetensors -> ComfyUI/models/diffusion_models/" in text
    assert "VHS_VideoCombine -> install ComfyUI-VideoHelperSuite" in text


def test_without_object_info_everything_is_unknown():
    r = wp.check(WORKFLOW, {}, "none")
    assert r["ok"] is None and all(n["ok"] is None for n in r["nodes"]) and all(m["ok"] is None for m in r["models"])
    assert wp.summary([r], "none")["ok"] is None
    assert "could not be checked" in r["copy_text"]


def test_all_present_is_ok():
    wf = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
          "2": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
    r = wp.check(wf, INFO)
    assert r["ok"] is True and r["copy_text"] == "Nothing missing."


def test_routes_check_a_template_and_a_production(client, data_dir):
    c, _app, _ = client
    r = c.post("/api/agent/workflow_preflight", json={"template": "sdxl_txt2img"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflows"][0]["template"] == "sdxl_txt2img" and body["source"] in ("live", "cache")
    assert isinstance(body["copy_text"], str)
    store = Store(data_dir)
    project = store.create_project("Pre", brief="b")
    spec = {"lead": {"name": "Ana", "look": "red coat"}, "shots": [{"prompt": "Ana walks", "clips": [0]}]}
    productions_mod.create_production(data_dir, "pre", spec, project_id=project["id"])
    r = c.post("/api/workflows/preflight", json={"production": "pre"})
    assert r.status_code == 200, r.text
    names = [w["template"] for w in r.json()["workflows"]]
    assert names and all(isinstance(n, str) for n in names)
    pre = c.get("/api/productions/pre/preflight").json()
    assert "workflows" in pre and pre["workflows"]["workflows"]
    assert all(i["level"] != "error" or i["code"] != "workflow_missing" for i in pre["items"])  # informational only
    assert c.post("/api/workflows/preflight", json={}).status_code == 400
