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
    assert kontext["instruction"] == "the same character from the reference, now under a flickering lamp, rain"


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
