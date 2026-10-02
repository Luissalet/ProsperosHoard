"""InfiniteTalk for long lines, a clip that ends on a given frame (Wan 2.2
14B first+last frame), pose and depth maps, and lip-sync renders trimmed to
the line."""

from __future__ import annotations

import subprocess

import pytest

from prosperos_hoard import comfy_driver, engine
from prosperos_hoard.backend import ffmpeg_path
from test_lipsync import _song
from test_new_templates import _last_prompt, _node, _run_job, _still


def test_talk_chunks_cover_the_line_without_repeated_frames():
    _, spec = comfy_driver.load_template("wan21_infinitetalk", None)
    assert comfy_driver.talk_chunks_for(spec, 3.0, 25, 81, 9) == 1
    # 10 s = 250 frames: 81 + 72 * 3 = 297 >= 250
    assert comfy_driver.talk_chunks_for(spec, 10.0, 25, 81, 9) == 4
    wf, _ = comfy_driver.load_template("wan21_infinitetalk", None)
    comfy_driver.expand_talk_chunks(wf, spec, 3, 7)
    assert wf["129_x1"]["inputs"]["previous_frames"] == ["119", 0]
    assert wf["129_x2"]["inputs"]["previous_frames"] == ["join_x1", 0]
    assert wf["trim_x2"]["inputs"]["batch_index"] == ["129_x2", 4]  # the context frames are dropped
    assert wf["193_x2"]["inputs"]["noise_seed"] == 9
    assert wf["140"]["inputs"]["images"] == ["join_x2", 0]


def test_sing_template_picks_infinitetalk_for_long_lines():
    from prosperos_hoard.devtools.fake_comfy import real_object_info, with_motion_models
    info = with_motion_models(real_object_info())
    assert engine.infinitetalk_installed(info) and engine.flf_installed(info)
    assert engine.sing_template(info, 4.0) == "wan22_s2v"
    assert engine.sing_template(info, 30.0) == "wan21_infinitetalk"
    assert engine.sing_template(info, 4.0, "infinitetalk") == "wan21_infinitetalk"
    assert not engine.infinitetalk_installed(real_object_info())
    assert engine.sing_template(real_object_info(), 30.0) == "wan22_s2v"


def test_an_infinitetalk_render_chains_and_is_trimmed(store, backend_with_comfy, fake_comfy, project):
    server, _ = fake_comfy
    server.motion_models = True
    still = _still(store, backend_with_comfy, project, 832, 480)
    song = _song(store, project, 20.0)
    done = _run_job(store, backend_with_comfy, "generate_image", {
        "prompt": "she sings", "positive_prompt": "she sings", "negative_prompt": "", "seed": 3, "count": 1,
        "template": "wan21_infinitetalk", "reference_asset_id": still, "audio_asset_id": song, "audio_start_s": 1.0,
        "audio_seconds": 6.0}, project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    assert sum(1 for v in wf.values() if v["class_type"] == "WanInfiniteTalkToVideo") == 2  # 150 frames > 81
    assert _node(wf, "ModelPatchLoader")["name"] == "wan2.1_infiniteTalk_single_fp16.safetensors"
    assert _node(wf, "LoadAudio")["audio"].startswith(f"prospero_{song}_1000_6000")


def test_a_clip_can_end_on_a_given_frame(client, fake_comfy):
    c, app, _ = client
    server, _ = fake_comfy
    server.motion_models = True
    pid = c.post("/api/projects", json={"name": "FLF"}).json()["id"]
    import io

    from PIL import Image

    def up(color):
        buf = io.BytesIO()
        Image.new("RGB", (832, 480), color).save(buf, "PNG")
        return c.post(f"/api/projects/{pid}/import-upload", files={"file": ("x.png", buf.getvalue(), "image/png")}).json()["id"]

    a, b = up((200, 20, 20)), up((20, 20, 200))
    r = c.post(f"/api/projects/{pid}/generate", json={"prompt": "the door opens", "template": "auto_clip",
                                                       "reference_asset_id": a, "end_asset_id": b}).json()
    assert r["job"]["params"]["template"] == "wan22_flf2v" and r["job"]["params"]["end_asset_id"] == b
    bad = c.post(f"/api/projects/{pid}/generate", json={"prompt": "x", "template": "auto_clip", "reference_asset_id": a,
                                                         "end_asset_id": pid})
    assert bad.status_code >= 400


@pytest.mark.parametrize("operation,node", [("pose_map", "SDPoseDrawKeypoints"), ("depth_map", "DA3Render")])
def test_pose_and_depth_maps(store, backend_with_comfy, fake_comfy, project, operation, node):
    server, _ = fake_comfy
    server.motion_models = True
    still = _still(store, backend_with_comfy, project, 640, 640)
    done = _run_job(store, backend_with_comfy, "edit_image", {"asset_id": still, "operation": operation}, project["id"])
    assert done["state"] == "done", done
    assert _node(_last_prompt(fake_comfy), node) is not None
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["kind"] == "image" and asset["recipe"]["operation"] == f"edit_image:{operation}"


def test_a_lip_sync_video_is_trimmed_to_the_line(tmp_path):
    exe = ffmpeg_path()
    if not exe:
        pytest.skip("no ffmpeg")
    src = tmp_path / "v.mp4"
    subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=4:r=16",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    out = engine._trim_video_bytes(src.read_bytes(), 1.5)
    dst = tmp_path / "o.mp4"
    dst.write_bytes(out)
    probe = subprocess.run([exe, "-hide_banner", "-i", str(dst)], capture_output=True, text=True).stderr
    assert "Duration: 00:00:01.5" in probe
    assert engine._trim_video_bytes(src.read_bytes(), 10) == src.read_bytes()


def test_the_gpu_lease_is_sized_by_operation_and_template():
    from prosperos_hoard import gpu_lease
    est = {"control": 3000, "esrgan": 2500, "wan14b": 10000, "wan_s2v": 12000, "qwen21": 12000}
    edit = lambda op: {"type": "edit_image", "params": {"operation": op}}
    gen = lambda tpl: {"type": "generate_image", "params": {"template": tpl}}
    assert gpu_lease.estimate_mb(edit("pose_map"), est) == 3000
    assert gpu_lease.estimate_mb(edit("upscale"), est) == 2500
    assert gpu_lease.estimate_mb(edit("img2img"), est) == 12000
    assert gpu_lease.estimate_mb(gen("wan21_infinitetalk"), est) == 10000
    assert gpu_lease.estimate_mb(gen("auto_sing"), est) == 12000
    assert gpu_lease.vram_class({"type": "generate_image", "params": {"engine": "qwen21"}}) == "qwen21"


def test_a_retake_redoes_only_its_stretch(store, backend_with_comfy, fake_comfy, project):
    import numpy as np
    from prosperos_hoard import engine
    from test_new_templates import _last_prompt, _run_job
    from test_qa import _clip
    server, _ = fake_comfy
    server.motion_models = True
    plan = engine.retake_plan(5.0, 2.0, 3.0)
    assert plan["a"] == 32 and plan["b"] == 48 and plan["start"] == 24 and (plan["length"] - 1) % 4 == 0 and plan["length"] >= 32
    with pytest.raises(engine.EngineError):
        engine.retake_plan(10.0, 1.0, 8.0)
    frames = np.zeros((80, 72, 128), dtype=np.uint8)
    frames[:, :, :] = np.arange(80, dtype=np.uint8)[:, None, None] * 3
    clip = _clip(store, project["id"], frames)  # 80 frames at 24 fps
    asset = store.get_asset(clip)
    done = _run_job(store, backend_with_comfy, "retake", {"asset_id": clip, "start_s": 1.0, "end_s": 2.0,
                                                          "prompt": "she turns", "quality": "draft"}, project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    vace = next(n for n in wf.values() if n["class_type"] == "WanVaceToVideo")["inputs"]
    assert (vace["length"] - 1) % 4 == 0 and vace["control_video"] and vace["control_masks"]
    loads = [n["inputs"]["file"] for n in wf.values() if n["class_type"] == "LoadVideo"]
    assert any(f.endswith("_control.mp4") for f in loads) and any(f.endswith("_mask.mp4") for f in loads)
    out = store.get_asset(done["outputs"]["asset_ids"][0])
    assert out["kind"] == "video" and out["recipe"]["operation"] == "retake" and out["recipe"]["retake_of"] == clip
    assert abs(float(out.get("duration_s") or 0) - asset["duration_s"]) < 0.25  # same length: only the stretch changed


def test_the_final_retake_uses_both_experts(store, backend_with_comfy, fake_comfy, project):
    import numpy as np
    from test_new_templates import _last_prompt, _run_job
    from test_qa import _clip
    server, _ = fake_comfy
    server.motion_models = True
    clip = _clip(store, project["id"], np.full((48, 72, 128), 50, dtype=np.uint8))
    done = _run_job(store, backend_with_comfy, "retake", {"asset_id": clip, "start_s": 0.0, "end_s": 1.0,
                                                          "quality": "final", "seed": 5}, project["id"])
    assert done["state"] == "done", done
    names = {n["inputs"].get("unet_name") for n in _last_prompt(fake_comfy).values() if n["class_type"] == "UNETLoader"}
    assert names == {"wan2.2_fun_vace_high_noise_14B_fp8_scaled.safetensors", "wan2.2_fun_vace_low_noise_14B_fp8_scaled.safetensors"}


def test_retake_over_http_checks_the_stretch(client):
    import numpy as np
    from test_qa import _clip
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Retake"}).json()["id"]
    clip = _clip(store, pid, np.full((48, 72, 128), 50, dtype=np.uint8))
    assert c.post("/api/agent/studio_retake", json={"asset_id": clip, "start_s": 1.5, "end_s": 1.0}).status_code == 400
    assert c.post(f"/api/assets/{clip}/retake", json={"asset_id": clip, "start_s": 0.2, "end_s": 1.2,
                                                      "quality": "best"}).status_code == 400
