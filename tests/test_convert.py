"""The UI->API converter against the official ComfyUI example templates
(comfyui-workflow-templates 0.11.68) and a real `/object_info`
(ComfyUI 0.37.0, 962 node classes).

`fixtures/comfy/api_truth/` holds, for every template, the API prompt the
real ComfyUI frontend (1.53.6) produced from it (`graphToPrompt()` in a
headless browser against a real install): the converter must match
it input for input, not merely produce something that validates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prosperos_hoard.workflows import convert

FIXTURES = Path(__file__).parent / "fixtures" / "comfy"
OBJECT_INFO_PATH = Path(__file__).parent.parent / "prosperos_hoard" / "devtools" / "comfy_object_info.json"

TRUTH = FIXTURES / "api_truth"
OFFICIAL_TEMPLATES = [
    "flux_schnell.json",
    "flux_schnell_full_text_to_image.json",
    "flux_kontext_dev_basic.json",
    "video_wan2_2_5B_ti2v.json",
    "audio_ace_step_1_5_checkpoint.json",
    # subgraph-promoted widgets, dynamic combos and autogrow sockets
    "image_qwen_image_2_1_t2i.json",
    "image_qwen_image_2_1_image_edit.json",
]
# Inputs the frontend sends that carry no information for the server:
# seeds (randomised when the frontend loads a template), a legacy bare
# duplicate of SaveVideo's flattened "format.codec", and the socketless
# ImageCompare view widget (a UI-only preview).
IGNORED_INPUTS = {"seed", "noise_seed"}
FRONTEND_ONLY = {("SaveVideo", "codec"), ("ImageCompare", "compare_view")}


@pytest.fixture(scope="module")
def object_info() -> dict:
    return json.loads(OBJECT_INFO_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", OFFICIAL_TEMPLATES)
def test_official_template_converts_and_validates(name: str, object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    assert api, "conversion produced no nodes"
    problems = convert.validate_converted(api, object_info)
    assert problems == []
    # every node exists and every link resolves within the produced graph
    for node_id, node in api.items():
        assert node["class_type"] in object_info
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                assert value[0] in api, f"{node_id}: dangling link to {value[0]!r}"


@pytest.mark.parametrize("name", OFFICIAL_TEMPLATES)
def test_matches_the_real_frontend_export(name: str, object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    truth = json.loads((TRUTH / name.replace(".json", ".api.json")).read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    assert sorted(api) == sorted(truth)
    for node_id, expected in truth.items():
        ours = api[node_id]
        assert ours["class_type"] == expected["class_type"], node_id
        ctype = expected["class_type"]
        want = {k: v for k, v in expected["inputs"].items()
                if k not in IGNORED_INPUTS and (ctype, k) not in FRONTEND_ONLY}
        got = {k: v for k, v in ours["inputs"].items() if k not in IGNORED_INPUTS}
        assert got == want, f"{name} node {node_id} ({ctype})"


def test_dynamic_combo_children_are_flattened_and_required(object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / "video_wan2_2_5B_ti2v.json").read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    save = api["58"]["inputs"]
    assert save["format"] == "auto" and save["format.codec"] == "auto"
    # the server validates the chosen option's children: dropping one is caught
    broken = json.loads(json.dumps(api))
    del broken["58"]["inputs"]["format.codec"]
    assert any("format.codec" in p for p in convert.validate_converted(broken, object_info))


def test_subgraph_promoted_widgets_come_from_the_instance(object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / "image_qwen_image_2_1_image_edit.json").read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    encode = api["459:474"]["inputs"]
    assert encode["prompt"].startswith("Keep the character and pose in <image1>")
    assert encode["resolution"] == 0
    # autogrow sockets: only the linked reference images are sent
    assert encode["images.image_1"] == ["470", 0] and encode["images.image_2"] == ["475", 0]
    assert not any(k.startswith("images.image_3") for k in encode)
    # matched by name, not slot: cfg is the literal promoted value, not a width link
    assert api["459:458"]["inputs"]["cfg"] == 1


def test_flux_schnell_values(object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / "flux_schnell.json").read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    sampler = api["31"]
    assert sampler["class_type"] == "KSampler"
    assert sampler["inputs"]["seed"] == 173805153958730
    assert sampler["inputs"]["steps"] == 4
    assert sampler["inputs"]["cfg"] == 1
    assert sampler["inputs"]["sampler_name"] == "euler"
    assert sampler["inputs"]["positive"] == ["6", 0]
    checkpoint = api["30"]
    assert checkpoint["inputs"]["ckpt_name"] == "flux1-schnell-fp8.safetensors"


def test_kontext_subgraph_is_expanded_and_wired(object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / "flux_kontext_dev_basic.json").read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    # inner nodes of the subgraph instance (id 192) are prefixed
    assert "192:31" in api and api["192:31"]["class_type"] == "KSampler"
    assert api["192:37"]["inputs"]["unet_name"] == "flux1-dev-kontext_fp8_scaled.safetensors"
    assert api["192:39"]["inputs"]["vae_name"] == "ae.safetensors"
    guidance = api["192:35"]
    assert guidance["class_type"] == "FluxGuidance"
    assert guidance["inputs"]["conditioning"][0] == "192:177"
    # the first outer LoadImage feeds the subgraph's ImageStitch; the
    # second is bypassed by default in this official template (single
    # reference image), so image2 (optional) correctly stays unset
    stitch = api["192:146"]
    assert stitch["inputs"]["image1"] == ["190", 0]
    assert "192" not in api  # the instance node itself never gets an entry
    assert "image2" not in stitch["inputs"]
    assert "191" not in api  # the bypassed second LoadImage never gets one either
    # the subgraph's own output reaches the outer SaveImage
    assert api["136"]["inputs"]["images"][0] == "192:8"


def test_wan_bypassed_load_image_leaves_start_image_unset(object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / "video_wan2_2_5B_ti2v.json").read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    assert "56" not in api  # the bypassed LoadImage never gets an API entry
    latent_node = api["55"]
    assert latent_node["class_type"] == "Wan22ImageToVideoLatent"
    assert "start_image" not in latent_node["inputs"]
    assert latent_node["inputs"]["width"] == 1280
    assert latent_node["inputs"]["length"] == 121


def test_ace_step_primitive_nodes_resolve_to_literal_values(object_info: dict) -> None:
    ui_workflow = json.loads((FIXTURES / "audio_ace_step_1_5_checkpoint.json").read_text(encoding="utf-8"))
    api = convert.ui_to_api(ui_workflow, object_info)
    text_node = api["94"]
    assert text_node["class_type"] == "TextEncodeAceStepAudio1.5"
    assert text_node["inputs"]["seed"] == 31  # from PrimitiveNode 102, not a link
    assert text_node["inputs"]["duration"] == 120  # from PrimitiveNode 99
    assert text_node["inputs"]["bpm"] == 190
    assert text_node["inputs"]["language"] == "en"
    assert text_node["inputs"]["keyscale"] == "E minor"
    # PrimitiveNode and MarkdownNote never get an API entry
    assert "99" not in api and "102" not in api and "105" not in api
    ksampler = api["3"]
    assert ksampler["inputs"]["seed"] == 31  # same primitive feeds both


def test_unknown_node_class_raises() -> None:
    ui_workflow = {"nodes": [{"id": 1, "type": "TotallyMadeUpNode", "mode": 0, "inputs": [], "outputs": [],
                              "widgets_values": []}], "links": []}
    with pytest.raises(convert.ConversionError):
        convert.ui_to_api(ui_workflow, {})


def test_not_ui_format_raises() -> None:
    with pytest.raises(convert.ConversionError):
        convert.ui_to_api({"3": {"class_type": "KSampler", "inputs": {}}}, {})
