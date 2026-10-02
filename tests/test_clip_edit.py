"""Editing a clip from an instruction (Bernini-R): the task and the
renderer's text, references named image0.., the instruction rewrite, the
window of a long clip spliced back, the takes, and the first frame to edit
before propagating."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from prosperos_hoard import clip_edit as ce
from prosperos_hoard import engine
from test_new_templates import _last_prompt, _node, _run_job
from test_qa import _clip, _save


def test_the_task_and_the_renderer_text():
    assert ce.resolve_mode("auto", 0, False) == "v2v"
    assert ce.resolve_mode("auto", 2, False) == "rv2v"
    assert ce.resolve_mode("auto", 1, True) == "vi2v"
    assert ce.resolve_mode("restyle", 0, False) == "mv2v"
    for mode, refs, first, code in (("propagate", 0, False, "first_frame_required"),
                                    ("reference", 0, False, "reference_required"), ("paint", 0, False, "bad_mode")):
        with pytest.raises(ce.ClipEditError) as err:
            ce.resolve_mode(mode, refs, first)
        assert err.value.code == code
    # exactly how Bernini's pipeline joins them: system line + instruction, no separator
    assert ce.positive_text("v2v", "  Make it\nnight. ") == "You are a helpful assistant specialized in video editing.Make it night."
    assert ce.positive_text("vi2v", "").endswith(ce.PROPAGATE_PROMPT)
    with pytest.raises(ce.ClipEditError):
        ce.positive_text("v2v", " ")


def test_cast_members_become_numbered_references():
    vera = {"id": "c1", "name": "Vera", "element": "character", "canonical_asset_id": "a_vera"}
    bar = {"id": "c2", "name": "Bar", "element": "location", "canonical_asset_id": "a_bar"}
    ghost = {"id": "c3", "name": "Ghost", "element": "character", "prompt": "a pale man in grey"}
    text, used = ce.name_references(["Replace the woman with ", vera, " and put ", vera, " in ", bar, " next to ", ghost], 1)
    assert used == [vera, bar]
    assert text == ("Replace the woman with the person in image1 and put the person in image1 in the place in image2 "
                    "next to the person, a pale man in grey,")
    many = [{"id": f"c{i}", "name": f"N{i}", "canonical_asset_id": f"a{i}"} for i in range(6)]
    text, used = ce.name_references([x for m in many for x in (m, " ")], 0)
    assert len(used) == ce.MAX_REFERENCES and "image4" not in text


def test_the_rewrite_is_cleaned():
    assert ce.clean_rewrite('"Prompt: Add a red umbrella (bright) to the woman."', "x") == "Add a red umbrella to the woman."
    assert ce.clean_rewrite('{"rewritten_text": "Replace the car with a horse, keep the street."}', "x") == \
        "Replace the car with a horse, keep the street."
    assert ce.clean_rewrite("ok", "make it night") == "make it night"
    msgs = ce.enhance_messages("rv2v", "put image0 on her", scene="a woman on a rooftop", references=1)
    assert "image0" in msgs[0]["content"] and "rooftop" in msgs[1]["content"] and "image0" in msgs[1]["content"]
    with_frames = ce.enhance_messages("v2v", "make it night", scene="ignored", with_frames=True)
    assert "frames of the source video" in with_frames[1]["content"] and "ignored" not in with_frames[1]["content"]


def test_the_window_of_a_clip():
    assert ce.window(5.0) == {"start": 0, "length": 77, "total": 80}
    assert ce.window(12.0, 4.0) == {"start": 64, "length": 81, "total": 192}
    with pytest.raises(ce.ClipEditError):
        ce.window(0.4)
    with pytest.raises(ce.ClipEditError):
        ce.window(5.0, 4.9)


def test_a_whole_clip_is_edited_and_keeps_its_length(store, backend_with_comfy, fake_comfy, project):
    server, _ = fake_comfy
    server.motion_models = True
    frames = np.zeros((96, 72, 128), dtype=np.uint8)
    frames[:] = np.arange(96, dtype=np.uint8)[:, None, None] * 2
    clip = _clip(store, project["id"], frames)  # 4 s at 24 fps
    ref = _save(store, project["id"], Image.new("RGB", (64, 96), (200, 30, 30)))
    done = _run_job(store, backend_with_comfy, "clip_edit", {
        "asset_id": clip, "prompt": "Dress the person in the coat from image0.", "task": "rv2v",
        "reference_asset_ids": [ref], "quality": "draft", "seed": 3, "instruction": "red coat"}, project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    cond = _node(wf, "BerniniConditioning")
    assert (cond["width"], cond["height"]) == (848, 480) and cond["length"] == 61 and (cond["length"] - 1) % 4 == 0
    assert cond["reference_images.reference_image_0"] and "reference_images.reference_image_1" not in cond
    text = wf[cond["positive"][0]]["inputs"]["text"]
    assert text == ce.SYSTEM_LINES["rv2v"] + "Dress the person in the coat from image0."
    assert _node(wf, "LoadVideo")["file"].endswith("_source.mp4")
    assert _node(wf, "UNETLoader")["unet_name"] == "wan2.1_bernini_1.3B_fp16.safetensors"
    out = store.get_asset(done["outputs"]["asset_ids"][0])
    assert out["recipe"]["operation"] == "clip_edit" and out["recipe"]["edit_of"] == clip
    assert out["recipe"]["task"] == "rv2v" and ref in out["recipe"]["input_asset_ids"]
    assert abs(float(out["duration_s"]) - 4.0) < 0.2


def test_a_long_clip_is_edited_a_window_at_a_time(store, backend_with_comfy, fake_comfy, project):
    server, _ = fake_comfy
    server.motion_models = True
    clip = _clip(store, project["id"], np.full((192, 72, 128), 60, dtype=np.uint8))  # 8 s
    done = _run_job(store, backend_with_comfy, "clip_edit", {
        "asset_id": clip, "prompt": "Make it night.", "task": "v2v", "quality": "final", "start_s": 2.0, "seed": 1},
        project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    assert _node(wf, "BerniniConditioning")["length"] == 81
    samplers = [n["inputs"] for n in wf.values() if n["class_type"] == "SamplerCustom"]
    assert len(samplers) == 2 and {s["cfg"] for s in samplers} == {1.0} and [s["add_noise"] for s in samplers] == [True, False]
    out = store.get_asset(done["outputs"]["asset_ids"][0])
    assert abs(float(out["duration_s"]) - 8.0) < 0.25 and out["recipe"]["start_s"] == 2.0
    assert out["recipe"]["end_s"] == pytest.approx(2.0 + 81 / 16, abs=0.01) and "edited 2.0-" in out["name"]


def test_the_frame_to_edit_before_propagating(store, project):
    frames = np.zeros((48, 72, 128), dtype=np.uint8)
    frames[24:] = 220
    clip = _clip(store, project["id"], frames)
    first = engine.frame_at(store, clip, 0.0)
    later = engine.frame_at(store, clip, 1.5)
    with Image.open(store.data_dir / first["file_path"]) as a, Image.open(store.data_dir / later["file_path"]) as b:
        assert np.asarray(a.convert("L")).mean() < 30 and np.asarray(b.convert("L")).mean() > 180
    assert first["recipe"]["operation"] == "frame" and first["recipe"]["derived_from"] == clip
    with pytest.raises(engine.EngineError):
        engine.frame_at(store, clip, 9.0)


def _project_with_vera(c, store):
    pid = c.post("/api/projects", json={"name": "Edit"}).json()["id"]
    face = _save(store, pid, Image.new("RGB", (64, 96), (220, 180, 160)))
    store.create_character(pid, "Vera", canonical_asset_id=face, prompt="a woman with a silver bob")
    raw = store.get_asset(_clip(store, pid, np.full((48, 72, 128), 50, dtype=np.uint8)))
    clip = store.create_asset(project_id=pid, kind="video", file_path=raw["file_path"], duration_s=raw["duration_s"],
                              width=128, height=72, source="generated",
                              recipe={"operation": "generate", "params": {"positive_prompt": "a woman on a neon rooftop"}})["id"]
    return pid, face, clip


def test_the_preview_names_the_cast_and_rewrites(client):
    c, app, _ = client
    store = app.state.store
    pid, face, clip = _project_with_vera(c, store)
    seen = {}

    def chat(messages, max_tokens, temperature):
        seen["chat"] = messages
        return '"Dress the person in image0 in a long red leather coat, keeping the rooftop, the light and her motion."'

    app.state.short_hooks = {"chat": chat}
    app.state.edit_vision = False  # no vision model: the clip's own prompt describes it
    r = c.post("/api/agent/studio_clip_edit?preview=true", json={"asset_id": clip, "prompt": "put @Vera in a red coat"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["task"] == "rv2v" and plan["reference_asset_ids"] == [face] and plan["cast"] == ["Vera"]
    assert plan["enhanced"] and plan["prompt"].startswith("Dress the person in image0") and plan["how"] == "language"
    assert "neon rooftop" in seen["chat"][1]["content"] and "the person in image0" in seen["chat"][1]["content"]
    # exact: as written (mentions still become references)
    plan = c.post(f"/api/assets/{clip}/clip-edit/prompt", json={"asset_id": clip, "prompt": "put @Vera in a red coat",
                                                               "exact": True}).json()
    assert plan["prompt"] == "put the person in image0 in a red coat" and not plan["enhanced"] and plan["how"] == "written"
    # no model at all: as written, and it says so
    app.state.short_hooks = {"chat": lambda *a: (_ for _ in ()).throw(RuntimeError("llm unavailable"))}
    plan = c.post("/api/agent/studio_clip_edit?preview=true", json={"asset_id": clip, "prompt": "make it night"}).json()
    assert plan["how"] == "no_model" and "llm unavailable" in plan["how_detail"] and plan["prompt"] == "make it night"


def test_the_vision_model_sees_frames_and_references(client):
    c, app, _ = client
    store = app.state.store
    pid, face, clip = _project_with_vera(c, store)
    got = {}

    def vision(messages, images):
        got["n"] = len(images)
        got["text"] = messages[1]["content"]
        return "Replace the person with the person in image0, keeping everything else unchanged."

    app.state.edit_vision = vision
    plan = c.post("/api/agent/studio_clip_edit?preview=true", json={"asset_id": clip, "prompt": "swap her for @Vera"}).json()
    assert got["n"] == 3 + 1 and "frames of the source video" in got["text"] and plan["how"] == "vision"


def test_clip_edit_over_http(client):
    c, app, _ = client
    store = app.state.store
    pid, face, clip = _project_with_vera(c, store)
    app.state.edit_vision = False
    app.state.short_hooks = {"chat": lambda *a: ""}  # the model answers nothing: the instruction as written
    # the fake ComfyUI lists no Bernini models until the motion models are on
    r = c.post("/api/agent/studio_clip_edit", json={"asset_id": clip, "prompt": "make it night"})
    assert r.status_code == 400 and "no_bernini" in r.text
    assert c.post("/api/agent/studio_clip_edit", json={"asset_id": clip, "prompt": "x", "quality": "best"}).status_code == 400
    r = c.post("/api/agent/studio_clip_edit?preview=true", json={"asset_id": clip, "mode": "propagate"})
    assert r.status_code == 400 and "first_frame_required" in r.text
    first = c.post("/api/agent/studio_video_frames", json={"asset_id": clip, "at_s": 0}).json()["items"][0]["id"]
    plan = c.post("/api/agent/studio_clip_edit?preview=true", json={"asset_id": clip, "mode": "propagate",
                                                                    "first_frame_asset_id": first}).json()
    assert plan["task"] == "vi2v" and plan["prompt"] == ce.PROPAGATE_PROMPT and plan["how"] == "propagation"
    assert c.post("/api/agent/studio_clip_edit?preview=true", json={"asset_id": face, "prompt": "x"}).status_code == 400


def test_an_edit_of_a_shot_clip_is_one_of_its_takes(client):
    from test_production_takes import ClipStudio, _clips, _ready
    from prosperos_hoard import productions as prod
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Edit takes"}).json()["id"]
    slug, _ = _ready(store, {"id": pid})
    state = _clips(store, slug, ClipStudio(store))
    clip = state["done"]["clips"]["items"]["2"]
    src = store.get_asset(clip)
    edited = store.create_asset(project_id=pid, kind="video", file_path=src["file_path"], duration_s=src.get("duration_s"),
                                source="generated", recipe={"operation": "clip_edit", "edit_of": clip, "quality": "draft"})["id"]
    takes = c.get(f"/api/productions/{slug}/takes?key=2").json()["shots"][0]["clips"]["2"]
    assert [t.get("edit", False) for t in takes] == [False, True] and takes[1]["asset_id"] == edited
    r = c.patch(f"/api/productions/{slug}/shots", json={"changes": [{"key": "2", "take": edited}], "run": False})
    assert r.status_code == 200, r.text
    assert prod.load_state(store.data_dir, slug)["done"]["clips"]["items"]["2"] == edited


def test_the_lease_and_the_installed_check():
    from prosperos_hoard import gpu_lease
    from prosperos_hoard.devtools.fake_comfy import real_object_info, with_motion_models
    assert gpu_lease.vram_class({"type": "clip_edit", "params": {"quality": "final"}}) == "wan14b"
    assert gpu_lease.vram_class({"type": "clip_edit", "params": {"quality": "draft"}}) == "wan"
    assert engine.clip_edit_installed(with_motion_models(real_object_info())) == {"draft": True, "final": True}
    assert engine.clip_edit_installed(real_object_info()) == {"draft": False, "final": False}
    # the lookup is not fooled by the case of a file name (a draft retake was refused before)
    assert engine.retake_installed(with_motion_models(real_object_info()))["draft"] is True


def test_a_space_node_edits_every_wired_clip(client):
    from prosperos_hoard import spaces
    from test_spaces import FakeStudio, _space_with

    class EditStudio(FakeStudio):
        def clip_edit(self, body):
            return self._job("clip_edit", body)

    store, sid = _space_with(client, {"nodes": [
        {"id": "clips", "type": "asset", "data": {"kind": "video", "asset_ids": []}},
        {"id": "look", "type": "asset", "data": {"kind": "image", "asset_ids": []}},
        {"id": "night", "type": "clip_edit", "data": {"prompt": "make it night with rain", "quality": "final", "seed": 4}},
        {"id": "join", "type": "combine"}],
        "edges": [{"source": "clips", "target": "night", "target_handle": "clip"},
                  {"source": "look", "target": "night", "target_handle": "refs"},
                  {"source": "night", "target": "join", "target_handle": "clips"}]})
    pid = store.get_space(sid)["project_id"]
    c1, c2 = (_clip(store, pid, np.full((24, 36, 64), v, dtype=np.uint8)) for v in (30, 90))
    ref = _save(store, pid, Image.new("RGB", (32, 32), (1, 2, 3)))
    g = store.get_space(sid)["graph"]
    for n in g["nodes"]:
        if n["id"] == "clips":
            n["data"]["asset_ids"] = [c1, c2]
        if n["id"] == "look":
            n["data"]["asset_ids"] = [ref]
    store.save_space_graph(sid, g, None, None)
    ops = spaces.plan_node(store, g, {}, "night")
    assert [o["op"] for o in ops] == ["clip_edit", "clip_edit"] and [o["body"]["asset_id"] for o in ops] == [c1, c2]
    assert ops[1]["body"]["seed"] == 5 and ops[0]["body"]["reference_asset_ids"] == [ref] and ops[0]["body"]["quality"] == "final"
    studio = EditStudio()
    spaces.run_space(store, studio, sid, "upto", ["night"], poll_s=0)
    assert [k for k, _ in studio.calls] == ["clip_edit", "clip_edit"]
    est = next(n for n in spaces.estimate(store, sid, force=True)["nodes"] if n["node"] == "night")
    assert est["templates"] == ["wan22_bernini_edit"] and est["renders"] == 2
    assert "Bernini-R 14B (video editing)" in spaces.needed_models(g)
    with pytest.raises(spaces.SpaceError):
        spaces.plan_node(store, {**g, "edges": []}, {}, "night")
    assert spaces.output_type({"type": "clip_edit"}) == "video"
