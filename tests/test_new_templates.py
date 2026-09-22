"""The four new built-in templates (flux_schnell_txt2img, flux_kontext_edit,
wan22_ti2v, ace15_song) against the fake backend, which now serves the
real object_info (see devtools/fake_comfy.py): checkpoint/model
validation, generation, the character-consistency route (`consistent=true`
-> Kontext), and ACE-Step composing a real, analysable song.
"""

from __future__ import annotations

from PIL import Image

from prosperos_hoard import audio as audio_mod
from prosperos_hoard import comfy_driver, engine
from prosperos_hoard.jobs import JobQueue


def _run_job(store, backend, type_, params, project_id):
    queue = JobQueue(store)
    handlers = {
        "generate_image": engine.generate_image,
        "edit_image": engine.edit_image,
        "animate": engine.animate_image,
        "compose_song": engine.compose_song,
    }
    queue.register(type_, lambda job, p: handlers[type_](store, backend, job, p))
    queue.start()
    try:
        job = queue.enqueue(type_, "gpu", params, project_id=project_id)
        return queue.wait_for(job["id"], 60)
    finally:
        queue.stop()


# ------------------------------------------------------------ flux schnell

def test_flux_schnell_txt2img_generates_an_image(store, backend_with_comfy, project):
    params = {
        "prompt": "FAROL under a flickering sodium lamp, rain, fog", "positive_prompt": "FAROL under a flickering sodium lamp",
        "negative_prompt": "", "width": 1024, "height": 1024, "seed": 5, "count": 1, "template": "flux_schnell_txt2img",
        "steps": 4, "cfg": 1,
    }
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["kind"] == "image" and asset["width"] == 1024 and asset["height"] == 1024
    assert asset["recipe"]["template"] == "flux_schnell_txt2img"
    assert asset["recipe"]["checkpoint"] == "flux1-schnell-fp8.safetensors"


def test_flux_schnell_unknown_checkpoint_is_rejected(store, backend_with_comfy, project):
    params = {"prompt": "x", "positive_prompt": "x", "negative_prompt": "", "seed": 1, "count": 1,
              "template": "flux_schnell_txt2img", "checkpoint": "not_a_real_file.safetensors"}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "failed"
    assert "not_a_real_file.safetensors" in done["message"]
    assert "flux1-schnell-fp8.safetensors" in done["message"]


# --------------------------------------------------------- flux kontext edit

def test_kontext_edit_with_explicit_reference(store, backend_with_comfy, project):
    base = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "FAROL portrait", "positive_prompt": "FAROL portrait", "negative_prompt": "",
                     "width": 768, "height": 768, "seed": 3, "count": 1, "template": "sdxl_txt2img"},
                    project["id"])
    reference = base["outputs"]["asset_ids"][0]
    edited = _run_job(store, backend_with_comfy, "generate_image",
                      {"prompt": "now standing in a rainy bus shelter", "positive_prompt": "the same character from the reference, now standing in a rainy bus shelter",
                       "negative_prompt": "", "seed": 4, "count": 1, "template": "flux_kontext_edit",
                       "reference_asset_id": reference},
                      project["id"])
    assert edited["state"] == "done", edited
    asset = store.get_asset(edited["outputs"]["asset_ids"][0])
    assert asset["recipe"]["template"] == "flux_kontext_edit"
    assert asset["recipe"]["input_asset_ids"] == [reference]


def test_consistent_generation_routes_through_kontext(store, backend_with_comfy, project):
    char = store.create_character(project["id"], "FAROL", prompt="an urban night creature, paper lantern head")
    base = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "@FAROL turnaround, front view", "positive_prompt": "an urban night creature, paper lantern head turnaround, front view",
                     "negative_prompt": "", "seed": 1, "count": 1, "template": "sdxl_txt2img"}, project["id"])
    canonical = base["outputs"]["asset_ids"][0]
    store.update_character(char["id"], canonical_asset_id=canonical)

    kontext = engine.build_kontext_instruction(store, project["id"], "@FAROL under a flickering lamp, rain")
    assert kontext["reference_asset_id"] == canonical
    assert kontext["matched_characters"] == ["FAROL"]
    assert kontext["instruction"] == ("the same character from the reference image, with exactly the same design, "
                                      "proportions and colours, now under a flickering lamp, rain")


def test_kontext_needs_reference(store, backend_with_comfy, project):
    result = engine.build_kontext_instruction(store, project["id"], "a street at night")
    assert result["reference_asset_id"] is None


# ------------------------------------------------------------- wan22_ti2v

def test_wan_image_to_video_produces_mp4(store, backend_with_comfy, project):
    still = _run_job(store, backend_with_comfy, "generate_image",
                     {"prompt": "empty street, sodium lamp", "positive_prompt": "empty street, sodium lamp",
                      "negative_prompt": "", "width": 1024, "height": 576, "seed": 2, "count": 1,
                      "template": "sdxl_txt2img"}, project["id"])
    still_id = still["outputs"]["asset_ids"][0]
    clip = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "rain, flicker, slow push-in", "positive_prompt": "rain, flicker, slow push-in",
                     "negative_prompt": "low quality", "width": 1280, "height": 704, "seed": 6, "count": 1,
                     "template": "wan22_ti2v", "reference_asset_id": still_id},
                    project["id"])
    assert clip["state"] == "done", clip
    asset = store.get_asset(clip["outputs"]["asset_ids"][0])
    assert asset["kind"] == "video"
    assert asset["file_path"].endswith(".mp4")
    assert asset["recipe"]["template"] == "wan22_ti2v"


# --------------------------------------------------------------- ace15_song

_TAGS = "dark trap, horror rap, eerie music box melody, heavy 808, half-time 140 bpm, male rap vocals, spanish, minor key"
_LYRICS = "[Intro]\n(shh...)\n\n[Verse 1]\nCuenta las farolas, una, dos.\n\n[Chorus]\nNo mires atras, no mires atras.\n"


def test_compose_song_produces_analysable_audio(store, backend_with_comfy, project):
    params = {"tags": _TAGS, "lyrics": _LYRICS, "bpm": 140, "duration": 8, "key": "F# minor",
              "language": "es", "time_signature": 4, "seed": 31, "count": 1}
    done = _run_job(store, backend_with_comfy, "compose_song", params, project["id"])
    assert done["state"] == "done", done
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["kind"] == "audio"
    assert asset["recipe"]["template"] == "ace15_song"
    assert asset["recipe"]["checkpoint"] == "ace_step_1.5_turbo_aio.safetensors"
    assert 7.5 <= asset["duration_s"] <= 8.5
    path = store.data_dir / asset["file_path"]
    analysis = audio_mod.analyze_file(path)
    assert 120 <= analysis["tempo_bpm"] <= 160  # 140 bpm brief, a real trackable beat, not silence
    assert len(analysis["beat_times"]) > 4


def test_compose_song_reproducible_same_seed(store, backend_with_comfy, project):
    params = {"tags": _TAGS, "lyrics": _LYRICS, "bpm": 140, "duration": 5, "key": "F# minor",
              "language": "es", "time_signature": 4, "seed": 99, "count": 1}
    a = _run_job(store, backend_with_comfy, "compose_song", params, project["id"])
    b = _run_job(store, backend_with_comfy, "compose_song", dict(params), project["id"])
    asset_a = store.get_asset(a["outputs"]["asset_ids"][0])
    asset_b = store.get_asset(b["outputs"]["asset_ids"][0])
    bytes_a = (store.data_dir / asset_a["file_path"]).read_bytes()
    bytes_b = (store.data_dir / asset_b["file_path"]).read_bytes()
    assert bytes_a == bytes_b


def test_compose_song_rejects_empty_lyrics(store, backend_with_comfy, project):
    params = {"tags": _TAGS, "lyrics": "", "bpm": 120, "duration": 5, "count": 1}
    done = _run_job(store, backend_with_comfy, "compose_song", params, project["id"])
    assert done["state"] == "failed"
    assert "lyrics" in done["message"]


# ---------------------------------------------------------- checkpoint_node

def test_ace15_song_uses_checkpoint_node_validation(store, backend_with_comfy):
    workflow, spec = comfy_driver.load_template("ace15_song")
    assert spec["checkpoint_node"] == "CheckpointLoaderSimple"
    object_info = backend_with_comfy.run_async(backend_with_comfy.comfy().object_info())
    values = comfy_driver.validate_against_object_info(
        spec, {"checkpoint": "ace_step_1.5_turbo_aio.safetensors"}, object_info, workflow
    )
    assert values["checkpoint"] == "ace_step_1.5_turbo_aio.safetensors"


# ------------------------------------ what the real ComfyUI actually receives

def _last_prompt(fake_comfy) -> dict:
    server, _ = fake_comfy
    return server.prompts_seen[-1]


def _node(workflow: dict, class_type: str) -> dict:
    return next(n for n in workflow.values() if n["class_type"] == class_type)["inputs"]


def test_every_builtin_template_passes_server_side_validation():
    """With its own defaults applied, every built-in template is a prompt
    a real ComfyUI 0.37 accepts as-is (required inputs including
    dynamic-combo children, combo choices, number ranges)."""
    from prosperos_hoard.devtools.fake_comfy import real_object_info
    from prosperos_hoard.workflows.convert import validate_values

    for entry in comfy_driver.list_builtin_templates():
        workflow, spec = comfy_driver.load_template(entry["template"])
        values = dict(spec.get("defaults") or {})
        if spec.get("checkpoint_node"):
            values["checkpoint"] = comfy_driver.default_value(workflow, spec, "checkpoint")
        wf = comfy_driver.apply_params(workflow, spec, values)
        assert validate_values(wf, real_object_info()) == [], entry["template"]


def test_flux_schnell_without_explicit_settings_uses_its_own_defaults(store, backend_with_comfy, fake_comfy, project):
    params = {"prompt": "a lantern", "positive_prompt": "a lantern", "negative_prompt": "", "seed": 5, "count": 1,
              "template": "flux_schnell_txt2img"}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    sampler = _node(_last_prompt(fake_comfy), "KSampler")
    assert (sampler["steps"], sampler["cfg"], sampler["sampler_name"], sampler["scheduler"]) == (4, 1, "euler", "simple")


def test_style_preset_does_not_leak_sdxl_sampling_into_flux(store, backend_with_comfy, fake_comfy, project):
    params = {"prompt": "a lantern", "positive_prompt": "a lantern", "negative_prompt": "", "seed": 5, "count": 1,
              "template": "flux_schnell_txt2img",
              "style_defaults": {"steps": 32, "cfg": 7, "sampler": "dpmpp_2m", "scheduler": "karras", "width": 1344, "height": 768}}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    assert _node(wf, "KSampler")["cfg"] == 1 and _node(wf, "KSampler")["steps"] == 4
    assert (_node(wf, "EmptySD3LatentImage")["width"], _node(wf, "EmptySD3LatentImage")["height"]) == (1344, 768)


def _still(store, backend, project, width, height, seed=2):
    done = _run_job(store, backend, "generate_image",
                    {"prompt": "street", "positive_prompt": "street", "negative_prompt": "", "width": width,
                     "height": height, "seed": seed, "count": 1, "template": "sdxl_txt2img"}, project["id"])
    return done["outputs"]["asset_ids"][0]


def test_wan_defaults_and_size_follow_the_start_frame(store, backend_with_comfy, fake_comfy, project):
    for (w, h), expected in (((1344, 768), (1280, 704)), ((768, 1344), (704, 1280))):
        still_id = _still(store, backend_with_comfy, project, w, h)
        clip = _run_job(store, backend_with_comfy, "generate_image",
                        {"prompt": "rain", "positive_prompt": "rain", "negative_prompt": "", "seed": 6, "count": 1,
                         "template": "wan22_ti2v", "reference_asset_id": still_id}, project["id"])
        assert clip["state"] == "done", clip
        wf = _last_prompt(fake_comfy)
        latent = _node(wf, "Wan22ImageToVideoLatent")
        assert (latent["width"], latent["height"], latent["length"]) == (*expected, 121)
        sampler = _node(wf, "KSampler")
        assert (sampler["steps"], sampler["cfg"], sampler["sampler_name"], sampler["scheduler"]) == (20, 5, "uni_pc", "simple")
        assert _node(wf, "ModelSamplingSD3")["shift"] == 8
        assert _node(wf, "CreateVideo")["fps"] == 24
        assert _node(wf, "SaveVideo")["format.codec"] == "auto"
        # no negative given: Wan keeps the official negative prompt it was trained with
        workflow, _ = comfy_driver.load_template("wan22_ti2v")
        assert wf["7"]["inputs"]["text"] == workflow["7"]["inputs"]["text"] != ""


def test_kontext_output_size_is_the_requested_one(store, backend_with_comfy, fake_comfy, project):
    sheet = _still(store, backend_with_comfy, project, 1344, 768)
    post = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "x", "positive_prompt": "the same character from the reference, now at a bus stop",
                     "negative_prompt": "", "width": 896, "height": 1120, "seed": 4, "count": 1,
                     "template": "flux_kontext_edit", "reference_asset_id": sheet}, project["id"])
    assert post["state"] == "done", post
    asset = store.get_asset(post["outputs"]["asset_ids"][0])
    assert (asset["width"], asset["height"]) == (896, 1120)
    sampler = _node(_last_prompt(fake_comfy), "KSampler")
    assert sampler["steps"] == 20 and sampler["cfg"] == 1
    assert _node(_last_prompt(fake_comfy), "FluxGuidance")["guidance"] == 2.5
    # no size given: the reference's aspect at about one megapixel
    same = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "x", "positive_prompt": "the same character from the reference, now in fog",
                     "negative_prompt": "", "seed": 5, "count": 1, "template": "flux_kontext_edit",
                     "reference_asset_id": sheet}, project["id"])
    latent = _node(_last_prompt(fake_comfy), "EmptySD3LatentImage")
    assert latent["width"] > latent["height"] and latent["width"] % 16 == 0
    assert abs(latent["width"] / latent["height"] - 1344 / 768) < 0.03


def test_compose_song_duration_reaches_both_ace_nodes(store, backend_with_comfy, fake_comfy, project):
    params = {"tags": _TAGS, "lyrics": _LYRICS, "bpm": 140, "duration": 9, "key": "F# minor",
              "language": "es", "time_signature": 4, "seed": 31, "count": 1}
    done = _run_job(store, backend_with_comfy, "compose_song", params, project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    assert _node(wf, "TextEncodeAceStepAudio1.5")["duration"] == 9
    assert _node(wf, "EmptyAceStep1.5LatentAudio")["seconds"] == 9


def test_fake_comfy_rejects_what_the_real_server_rejects(fake_comfy):
    import httpx

    _, port = fake_comfy
    workflow, spec = comfy_driver.load_template("wan22_ti2v")
    broken = comfy_driver.apply_params(workflow, spec, spec["defaults"])
    del broken["58"]["inputs"]["format.codec"]
    resp = httpx.post(f"http://127.0.0.1:{port}/prompt", json={"prompt": broken, "client_id": "t"})
    assert resp.status_code == 400
    assert "format.codec" in resp.text


def test_missing_unet_file_is_reported_before_queueing(store, backend_with_comfy, fake_comfy, project):
    """UNETLoader/VAELoader-based templates get the same model check as
    checkpoints: the whole prompt is validated like ComfyUI's /prompt."""
    still_id = _still(store, backend_with_comfy, project, 1344, 768)
    server, _ = fake_comfy
    seen = len(server.prompts_seen)
    done = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "x", "positive_prompt": "x", "negative_prompt": "", "seed": 1, "count": 1,
                     "template": "wan22_ti2v", "reference_asset_id": still_id}, project["id"])
    assert done["state"] == "done"
    from prosperos_hoard import engine as engine_mod

    original = engine_mod._object_info

    def without_wan(backend):
        info = original(backend)
        patched = dict(info)
        unet = dict(patched["UNETLoader"])
        unet["input"] = {"required": {"unet_name": [["flux1-dev-kontext_fp8_scaled.safetensors"], {}],
                                      "weight_dtype": info["UNETLoader"]["input"]["required"]["weight_dtype"]}}
        patched["UNETLoader"] = unet
        return patched

    engine_mod._object_info = without_wan
    try:
        failed = _run_job(store, backend_with_comfy, "generate_image",
                          {"prompt": "x", "positive_prompt": "x", "negative_prompt": "", "seed": 2, "count": 1,
                           "template": "wan22_ti2v", "reference_asset_id": still_id}, project["id"])
    finally:
        engine_mod._object_info = original
    assert failed["state"] == "failed"
    assert "wan2.2_ti2v_5B_fp16.safetensors" in failed["message"] and "not available" in failed["message"]
    assert len(server.prompts_seen) == seen + 1  # the failing one never reached ComfyUI
