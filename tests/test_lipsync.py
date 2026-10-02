"""Lip sync with Wan 2.2 S2V: the still's character sings a stretch of the
song. The audio is cut and uploaded, a long line chains extend steps, and
the cut plays a singing clip from its exact second so the lips match."""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from prosperos_hoard import comfy_driver, engine, timeline
from test_new_templates import _last_prompt, _node, _run_job, _still


def _song(store, project, seconds=12.0):
    t = np.arange(int(seconds * 44100)) / 44100
    pcm = (0.3 * np.sin(2 * np.pi * 220 * t) * 32000).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(pcm.tobytes())
    path = store.data_dir / "assets" / "song_test.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    return store.create_asset(project_id=project["id"], kind="audio", file_path="assets/song_test.wav",
                              duration_s=seconds, source="generated")["id"]


def test_chunks_extend_the_graph_like_the_official_template():
    workflow, spec = comfy_driver.load_template("wan22_s2v")
    one = comfy_driver.expand_chunks(dict(workflow), spec, 1, 7)
    assert one["94"]["inputs"]["samples"] == ["3", 0]
    import copy
    three = comfy_driver.expand_chunks(copy.deepcopy(workflow), spec, 3, 7)
    exts = [k for k, v in three.items() if v["class_type"] == "WanSoundImageToVideoExtend"]
    assert len(exts) == 2
    assert three["ks1"]["inputs"]["seed"] == 8 and three["ks2"]["inputs"]["seed"] == 9
    assert three["ext2"]["inputs"]["video_latent"] == ["cat1", 0]
    assert three["94"]["inputs"]["samples"] == ["cat2", 0] and three["95"]["inputs"]["samples2"] == ["cat2", 0]
    capped = comfy_driver.expand_chunks(copy.deepcopy(workflow), spec, 9, 7)
    assert sum(v["class_type"] == "KSampler" for v in capped.values()) == spec["chunks"]["max"]


def test_s2v_needs_its_files(fake_comfy):
    server, _ = fake_comfy
    assert not engine.s2v_installed(server._object_info())
    server.motion_models = True
    assert engine.s2v_installed(server._object_info())


def test_a_lip_sync_render_uploads_the_line_and_chains_for_a_long_one(store, backend_with_comfy, fake_comfy, project):
    server, _ = fake_comfy
    server.motion_models = True
    still = _still(store, backend_with_comfy, project, 832, 480)
    song = _song(store, project)
    done = _run_job(store, backend_with_comfy, "generate_image", {
        "prompt": "she sings", "positive_prompt": "she sings", "negative_prompt": "", "seed": 11, "count": 1,
        "template": "wan22_s2v", "reference_asset_id": still, "audio_asset_id": song, "audio_start_s": 2.0,
        "audio_seconds": 7.0}, project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    load = _node(wf, "LoadAudio")
    assert load["audio"].startswith(f"prospero_{song}_2000_7000") and load["audio"].endswith(".wav")
    assert (server.input_dir / load["audio"]).is_file()
    # 7 s at 16 fps = 112 frames > 77: two generations
    assert sum(1 for v in wf.values() if v["class_type"] == "WanSoundImageToVideoExtend") == 1
    assert _node(wf, "WanSoundImageToVideo")["width"] == 832
    asset = store.get_asset(done["outputs"]["asset_ids"][0])
    assert asset["kind"] == "video" and song in asset["recipe"]["input_asset_ids"]
    with pytest.raises(Exception):
        bad = _run_job(store, backend_with_comfy, "generate_image", {
            "prompt": "x", "positive_prompt": "x", "negative_prompt": "", "seed": 1, "count": 1,
            "template": "wan22_s2v", "reference_asset_id": still}, project["id"])
        assert bad["state"] == "done"


def test_a_synced_span_plays_its_clip_from_the_exact_second():
    beats = [i * 0.5 for i in range(40)]
    sing = {"id": "a_sing", "kind": "video", "duration_s": 6.0}
    still = {"id": "a_still", "kind": "image"}
    cut = timeline.build_auto_cut(20.0, beats, [], [still], {
        "pinned_spans": [{"start_s": 4.0, "end_s": 9.0, "assets": [sing], "synced": True}],
        "video_lead_in_s": 1.0, "video_rotate_offsets": True})
    clips = [c for c in cut["tracks"][0]["clips"] if c["asset_id"] == "a_sing"]
    assert len(clips) == 1 and clips[0]["start_s"] == 4.0 and clips[0]["duration_s"] == 5.0
    assert clips[0]["trim_start_s"] == 0.0  # no lead-in skip, no rotated entry: the lips stay on the words


def test_a_singing_shot_in_a_production_renders_with_s2v_on_its_span(store, project, fake_comfy):
    from prosperos_hoard import productions as prod
    from test_productions import tiny_spec

    state = prod.create_production(store.data_dir, "Sing", prod.normalise_spec(tiny_spec()), {}, project_id=project["id"])
    prod.update_shots(store.data_dir, state["slug"], [{"key": "1", "sing": True, "span": {"start_s": 1.0, "end_s": 3.5}}])
    spec = prod.load_state(store.data_dir, state["slug"])["spec"]
    shot = next(s for s in spec["shots"] if s["key"] == "1")
    assert shot["sing"] is True and shot["clips"] == [0]
    state = prod.load_state(store.data_dir, state["slug"])
    still = store.create_asset(project_id=project["id"], kind="image", file_path="assets/x.png", width=1344, height=768,
                               source="generated")["id"]
    song = store.create_asset(project_id=project["id"], kind="audio", file_path="assets/y.wav", duration_s=8.0,
                              source="generated")["id"]
    state["project_id"] = project["id"]
    state["done"] = {"song": {"song_asset_id": song}, "frames": {"items": {"1": {"best": still, "variants": [still]}}}}
    prod.save_state(store.data_dir, state)
    run = prod.Run(store, None, state["slug"], lambda *a, **k: None)
    body = run.clip_body(shot, 0)
    assert body["template"] == "wan22_s2v" and body["audio_asset_id"] == song
    assert body["audio_start_s"] == 1.0 and body["audio_seconds"] == 2.75
    assert (body["width"], body["height"]) == (832, 480)
    spans = prod.pinned_spans(spec, lambda key: ["a_clip"])
    assert spans[0]["synced"] is True
    # moving the span drops the clip it sang; turning singing off too
    prod.update_shots(store.data_dir, state["slug"], [{"key": "1", "sing": False}])
    off = prod.load_state(store.data_dir, state["slug"])["spec"]
    assert "sing" not in next(s for s in off["shots"] if s["key"] == "1")
    with pytest.raises(prod.ProductionError):
        bare = dict(shot, start_s=None, end_s=None)
        bare.pop("start_s"); bare.pop("end_s")
        run.clip_body(bare, 0)
