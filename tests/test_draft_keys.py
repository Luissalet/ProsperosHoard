"""Draft generation keeps identity and structure on separate graph inputs."""

import io
import json

import numpy as np
from PIL import Image
import pytest

from prosperos_hoard import engine


def test_depth_control_and_rerun_keep_identity_and_depth_separate(store, project, monkeypatch, tmp_path):
    identity = tmp_path / "identity.png"
    Image.new("RGB", (256, 256), "green").save(identity)
    depth = tmp_path / "guide.png"
    levels = np.repeat(np.array([[0, 32768, 65535]], dtype=np.uint16), 128, axis=0)
    Image.fromarray(np.repeat(levels, 64, axis=1)).save(depth)
    first = engine.import_asset(store, project["id"], identity)["id"]
    guide = engine.import_asset(store, project["id"], depth)["id"]
    calls = []
    monkeypatch.setattr(engine, "check_vram_or_wait", lambda *_: None)
    monkeypatch.setattr(engine, "_object_info", lambda _: {})
    monkeypatch.setattr(engine, "_run_comfy_workflow",
                        lambda _backend, graph, uploads, _progress, **_: calls.append((graph, uploads)) or [{}])
    monkeypatch.setattr(engine, "_download_output", lambda *_: identity.read_bytes())
    params = {"template": "qwen21_depth", "positive_prompt": "Redraw <image1> holding the map",
              "reference_asset_id": first, "depth_asset_id": guide, "seed": 51}
    job = {"project_id": project["id"], "params": params}
    out = engine.generate_image(store, object(), job, lambda *_: None)
    asset = store.get_asset(out["asset_ids"][0])
    assert asset["recipe"]["input_asset_ids"] == [first, guide]
    assert asset["recipe"]["depth_asset_id"] == guide
    assert asset["recipe"]["params"]["width"] == 512
    assert asset["recipe"]["params"]["height"] == 336
    engine.rerun_recipe(store, object(), job, lambda *_: None, asset, vary=False)
    for graph, uploads in calls:
        assert graph["sample"]["inputs"]["model"] == ["control", 0]
        assert graph["control"]["inputs"]["image"] == ["depth", 0]
        assert graph["encode"]["inputs"]["images.image_1"] == ["identity", 0]
        assert "images.image_2" not in graph["encode"]["inputs"]
        assert graph["depth"]["inputs"]["image"] == uploads[1][1]
        converted = Image.open(io.BytesIO(uploads[1][0]))
        assert converted.getpixel((256, 0)) == (128, 128, 128)
        assert converted.getpixel((0, 0)) == (0, 0, 0)
        assert converted.getpixel((511, 0)) == (255, 255, 255)
    with pytest.raises(engine.EngineError) as exc:
        engine.generate_image(store, object(), {"project_id": project["id"],
                              "params": {**params, "width": 512, "height": 512}}, lambda *_: None)
    assert exc.value.code == "bad_depth_aspect"


def test_draft_objects_keep_projected_pixels_and_rerun_order(store, project, monkeypatch, tmp_path):
    def media(name, image):
        path = tmp_path / f"{name}.png"
        image.save(path)
        return engine.import_asset(store, project["id"], path)["id"]

    size = (256, 256)
    identity = media("identity", Image.new("RGB", size, "white"))
    depth = media("depth", Image.new("L", size, 128))
    paper = Image.new("RGBA", size)
    paper.putpixel((31, 17), (223, 185, 130, 255))
    paper.putpixel((31, 18), (223, 185, 130, 255))
    tube = Image.new("RGBA", size)
    tube.putpixel((31, 18), (89, 45, 18, 255))
    objects = [media("map", paper), media("tube", tube)]
    seen_uploads = []
    monkeypatch.setattr(engine, "check_vram_or_wait", lambda *_: None)
    monkeypatch.setattr(engine, "_object_info", lambda _: {})
    monkeypatch.setattr(engine, "_run_comfy_workflow",
                        lambda _backend, _graph, uploads, *_args, **_kw: seen_uploads.append(uploads) or [{}])
    monkeypatch.setattr(engine, "_download_output", lambda *_: (tmp_path / "identity.png").read_bytes())
    params = {"template": "qwen21_depth", "positive_prompt": "Draw the figure from <image1>",
              "reference_asset_id": identity, "depth_asset_id": depth,
              "object_asset_ids": objects, "width": 256, "height": 256, "seed": 55}
    job = {"project_id": project["id"], "params": params}
    result = engine.generate_image(store, object(), job, lambda *_: None)
    first = store.get_asset(result["asset_ids"][0])
    replay = engine.rerun_recipe(store, object(), job, lambda *_: None, first, vary=False)
    for asset in (first, store.get_asset(replay["asset_ids"][0])):
        assert asset["recipe"]["input_asset_ids"] == [identity, depth, *objects]
        assert asset["recipe"]["object_asset_ids"] == objects
        with Image.open(store.data_dir / asset["file_path"]) as result_image:
            assert result_image.getpixel((30, 17)) == (255, 255, 255, 255)
            assert result_image.getpixel((31, 17)) == (223, 185, 130, 255)
            assert result_image.getpixel((31, 18)) == (89, 45, 18, 255)
    assert [len(uploads) for uploads in seen_uploads] == [2, 2]

    params["object_asset_ids"] = [identity]
    with pytest.raises(engine.EngineError) as error:
        engine.generate_image(store, object(), job, lambda *_: None)
    assert error.value.code == "bad_draft_layer"
    assert len(seen_uploads) == 2


def test_grip_pass_uses_visible_parts_and_replays_both_graphs(store, project, monkeypatch, tmp_path):
    from PIL import ImageDraw, ImageEnhance

    size = (128, 128)

    def media(name, image):
        path = tmp_path / f"{name}.png"
        image.save(path)
        return engine.import_asset(store, project["id"], path)["id"]

    identity = media("identity", Image.new("RGB", size, "gray"))
    depth = media("depth", Image.new("I;16", size, 30000))
    color = media("color", Image.new("RGB", size, "orange"))
    labels = Image.new("L", size)
    ImageDraw.Draw(labels).rectangle((30, 50, 55, 60), fill=8)
    ImageDraw.Draw(labels).rectangle((36, 38, 43, 45), fill=9)
    label_id = media("parts", labels)
    prop = Image.new("RGBA", size)
    ImageDraw.Draw(prop).rectangle((53, 42, 88, 72), fill="tan")
    full_mask = media("full-paper", prop.getchannel("A"))
    # A front finger cuts a hole in the visible prop, and the rest of the paper
    # in front of the cuff stays in front during the local repaint.
    ImageDraw.Draw(prop).rectangle((53, 50, 55, 60), fill=(0, 0, 0, 0))
    object_id = media("paper", prop)
    requests = []

    def execute(_backend, graph, uploads, _progress, **kwargs):
        if "control" in graph:
            output = engine.grip_mod.png_bytes(Image.new("RGB", (256, 256), "gray"))
        else:
            guide = Image.open(io.BytesIO(uploads[0][0]))
            muted = ImageEnhance.Color(Image.new("RGB", (1, 1), "orange")).enhance(0.35).getpixel((0, 0))
            assert muted in set(guide.getdata())
            assert (210, 180, 140) in set(guide.getdata())
            output = engine.grip_mod.png_bytes(Image.new("RGB", guide.size, "pink"))
        requests.append((graph, uploads))
        return [output]

    monkeypatch.setattr(engine, "check_vram_or_wait", lambda *_: None)
    monkeypatch.setattr(engine, "_object_info", lambda _: {})
    monkeypatch.setattr(engine, "_run_comfy_workflow", execute)
    monkeypatch.setattr(engine, "_download_output", lambda _, output: output)
    grip = {"color_asset_id": color, "part_ids_asset_id": label_id, "grip_ids": [8],
            "prop_mask_asset_ids": [full_mask], "preserve_ids": [9]}
    params = {"template": "qwen21_depth", "positive_prompt": "Draw the figure from <image1>",
              "reference_asset_id": identity, "depth_asset_id": depth, "object_asset_ids": [object_id],
              "draft_grip": grip, "width": 256, "height": 256, "seed": 55}
    job = {"project_id": project["id"], "params": params}
    output = engine.generate_image(store, object(), job, lambda *_: None)
    first = store.get_asset(output["asset_ids"][0])
    replay = engine.rerun_recipe(store, object(), job, lambda *_: None, first, vary=False)
    for asset in (first, store.get_asset(replay["asset_ids"][0])):
        assert asset["recipe"]["input_asset_ids"] == [identity, depth, color, label_id, full_mask, object_id]
        assert asset["recipe"]["draft_grip"] == grip
        assert asset["recipe"]["grip_template_hash"]
        with Image.open(store.data_dir / asset["file_path"]) as result:
            embedded = json.loads(result.info["prospero.draft_recipe"])
            assert embedded["draft_grip"] == grip
            assert embedded["input_asset_ids"] == asset["recipe"]["input_asset_ids"]
            assert embedded["params"]["seed"] == 55
            assert result.getpixel((108, 110)) == (255, 192, 203, 255)
            assert result.getpixel((112, 110))[0] > 245
            assert result.getpixel((122, 110)) == (210, 180, 140, 255)
            assert result.getpixel((10, 150)) == (128, 128, 128, 255)
            assert result.getpixel((80, 90)) == (128, 128, 128, 255)
    assert requests[:2] == requests[2:]

    params["draft_grip"] = {**grip, "paint_props": True}
    output = engine.generate_image(store, object(), job, lambda *_: None)
    painted = store.get_asset(output["asset_ids"][0])
    with Image.open(store.data_dir / painted["file_path"]) as result:
        assert result.getpixel((150, 110)) == (255, 192, 203, 255)
        assert result.getpixel((177, 110)) == (210, 180, 140, 255)
        assert result.getpixel((178, 110)) == (128, 128, 128, 255)

    ImageDraw.Draw(prop).rectangle((53, 50, 55, 60), fill="tan")
    params["object_asset_ids"] = [media("unoccluded-paper", prop)]
    with pytest.raises(engine.EngineError, match="accessory covers visible gripping parts"):
        engine.generate_image(store, object(), job, lambda *_: None)
    assert len(requests) == 6

def test_textured_key_tracks_contacts_rejects_drift_and_reuses_selected_seed(store, project, monkeypatch, tmp_path):
    from PIL import ImageDraw

    size = (256, 256)

    def media(name, image):
        path = tmp_path / f"{name}.png"
        image.save(path)
        return engine.import_asset(store, project["id"], path)["id"]

    identity = media("reference", Image.new("RGB", (80, 100), "gray"))
    depth = media("depth", Image.new("L", size, 128))
    color = media("color", Image.new("RGB", size, "bisque"))
    labels = Image.new("L", size)
    draw = ImageDraw.Draw(labels)
    draw.rectangle((82, 38, 102, 68), fill=3)
    draw.rectangle((65, 90, 84, 103), fill=8)
    draw.rectangle((161, 95, 177, 107), fill=9)
    parts = media("parts", labels)
    paper = Image.new("RGBA", size)
    ImageDraw.Draw(paper).polygon([(80, 75), (140, 75), (140, 106), (131, 115), (80, 115)], fill="white")
    paper_mask = media("paper-mask", paper.getchannel("A"))
    ImageDraw.Draw(paper).rectangle((80, 90, 84, 103), fill=(0, 0, 0, 0))
    tube = Image.new("RGBA", size)
    ImageDraw.Draw(tube).rectangle((151, 70, 168, 119), fill="brown")
    tube_mask = media("tube-mask", tube.getchannel("A"))
    ImageDraw.Draw(tube).rectangle((161, 95, 168, 107), fill=(0, 0, 0, 0))
    texture = Image.new("RGB", (120, 80), "#e0ac61")
    ImageDraw.Draw(texture).rectangle((10, 8, 45, 36), fill="#2a5771")
    ink = media("ink", texture)
    layers = [media("paper", paper), media("tube", tube)]
    calls, reviews = [], []

    def render(_backend, graph, uploads, _progress, **kwargs):
        if "control" in graph:
            calls.append(graph["sample"]["inputs"]["seed"])
            return [engine.grip_mod.png_bytes(Image.new("RGB", size, "gray"))]
        _, spec = engine.comfy_driver.load_template("qwen21_edit", store.data_dir)
        node, _, field = spec["map"]["positive_prompt"].partition(".")
        prompt = graph[node]["inputs"][field]
        assert "left hand must curl around the tube" in prompt
        assert "right hand must curl around the sheet" in prompt
        with Image.open(io.BytesIO(uploads[0][0])) as guide:
            return [engine.grip_mod.png_bytes(Image.new("RGB", guide.size, "pink"))]

    def vision(images, prompt):
        assert len(images) == 2 and "dark braid" in prompt
        with Image.open(io.BytesIO(images[1])) as frame:
            assert frame.width < size[0] and frame.height < size[1]
        reviews.append(prompt)
        return '{"identity": 4, "why": "drift"}' if len(reviews) == 1 else '{"identity": 8, "why": "match"}'

    monkeypatch.setattr(engine, "check_vram_or_wait", lambda *_: None)
    monkeypatch.setattr(engine, "_object_info", lambda _: {})
    monkeypatch.setattr(engine, "_run_comfy_workflow", render)
    monkeypatch.setattr(engine, "_download_output", lambda _, sample: sample)
    grip = {"color_asset_id": color, "part_ids_asset_id": parts, "grip_ids": [8, 9],
            "prop_mask_asset_ids": [paper_mask, tube_mask], "preserve_ids": [3], "paint_props": True,
            "paper": {"asset_id": ink, "layer": 0, "quad": [[80, 75], [141, 75], [141, 116], [80, 116]]},
            "contacts": [
                {"hand": "left", "layer": 1, "target": "tube", "part_ids": [9], "pixel": [166, 100]},
                {"hand": "right", "layer": 0, "target": "sheet", "part_ids": [8], "pixel": [81, 96]},
            ],
            "identity": {"threshold": 6.5, "max_attempts": 2, "part_ids": [3], "notes": "Keep the dark braid."}}
    job = {"project_id": project["id"], "params": {"template": "qwen21_depth", "positive_prompt": "Same character",
           "reference_asset_id": identity, "depth_asset_id": depth, "object_asset_ids": layers, "draft_grip": grip,
           "width": size[0], "height": size[1], "seed": 55}}
    provider = lambda: (vision, "test vision")
    result = engine.generate_image(store, object(), job, lambda *_: None, vision_provider=provider)
    assert calls == [55, 56]
    assert result["draft_identity"]["0"]["attempts"] == [{"seed": 55, "score": 4}, {"seed": 56, "score": 8}]
    assert len(result["asset_ids"]) == 1
    key = store.get_asset(result["asset_ids"][0])
    assert key["recipe"]["draft_identity"]["status"] == "accepted"
    assert ink in key["recipe"]["input_asset_ids"]
    with Image.open(store.data_dir / key["file_path"]) as frame:
        assert frame.getpixel((94, 88))[2] > frame.getpixel((94, 88))[0]
        assert frame.getpixel((126, 100))[0] > frame.getpixel((126, 100))[2]
        assert frame.getpixel((82, 97)) == (255, 192, 203, 255)
        assert frame.getpixel((139, 109)) == (128, 128, 128, 255)
        assert frame.getpixel((88, 55)) == (128, 128, 128, 255)
    replay = engine.rerun_recipe(store, object(), job, lambda *_: None, key, vary=False, vision_provider=provider)
    reproduced = store.get_asset(replay["asset_ids"][0])
    assert calls == [55, 56, 56]
    assert (store.data_dir / key["file_path"]).read_bytes() == (store.data_dir / reproduced["file_path"]).read_bytes()

    rejected = engine.generate_image(store, object(), job, lambda *_: None,
                                     vision_provider=lambda: (lambda *_: '{"identity": 2}', "test vision"))
    assert not rejected["asset_ids"]
    assert rejected["draft_identity"]["0"]["status"] == "below_threshold"
    assert calls == [55, 56, 56, 55, 56]

    with pytest.raises(engine.EngineError, match="family vision model"):
        engine.generate_image(store, object(), job, lambda *_: None)
    assert calls == [55, 56, 56, 55, 56]


def test_paper_projection_flips_the_drawing_without_changing_visibility():
    from PIL import ImageDraw
    from prosperos_hoard.draft_art import paper_in_light, project_paper

    ink = Image.new("RGB", (24, 24), "tan")
    ImageDraw.Draw(ink).rectangle((0, 0, 10, 10), fill="navy")
    size = (50, 50)
    quad = [[10, 10], [40, 10], [40, 40], [10, 40]]
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rectangle((10, 10, 39, 39), fill=255)
    mask.putpixel((16, 16), 0)
    shade = Image.new("RGB", size, "#9a9a9a")
    upright = paper_in_light(project_paper(ink, size, quad), mask, shade)
    inverted = paper_in_light(project_paper(ink, size, quad[2:] + quad[:2]), mask, shade)
    assert upright.getchannel("A").tobytes() == inverted.getchannel("A").tobytes()
    assert upright.getpixel((18, 18))[0] < inverted.getpixel((18, 18))[0]
    assert upright.getpixel((33, 33))[0] > inverted.getpixel((33, 33))[0]
    assert not upright.getpixel((16, 16))[3] and not upright.getpixel((9, 20))[3]
    with pytest.raises(ValueError, match="convex"):
        project_paper(ink, size, [quad[0], quad[2], quad[1], quad[3]])


def test_continuity_preserves_fixed_pixels_and_shades_visible_tube():
    from PIL import ImageDraw
    from prosperos_hoard.draft_art import light_cylinder
    from prosperos_hoard.draft_grip import GripRegion, png_bytes

    size = (128, 128)
    labels = Image.new("L", size)
    ImageDraw.Draw(labels).rectangle((58, 18, 70, 35), fill=2)
    ImageDraw.Draw(labels).rectangle((68, 53, 74, 65), fill=9)
    previous = labels.copy()
    ImageDraw.Draw(previous).rectangle((30, 53, 46, 65), fill=21)
    full = Image.new("L", size)
    ImageDraw.Draw(full).rectangle((75, 44, 84, 90), fill=255)
    visible = full.copy()
    ImageDraw.Draw(visible).rectangle((75, 55, 77, 61), fill=0)
    tube = Image.new("RGBA", size, "#975f3e")
    tube.putalpha(visible)
    shaded = light_cylinder(tube, full)
    assert shaded.getchannel("A").tobytes() == visible.tobytes()
    assert shaded.getpixel((80, 67))[0] > shaded.getpixel((75, 67))[0]
    anchor = Image.new("RGBA", size, "#65616b")
    ImageDraw.Draw(anchor).rectangle((30, 53, 46, 65), fill="#fd836a")
    ImageDraw.Draw(labels).rectangle((75, 44, 84, 90), fill=22)
    base = Image.new("RGBA", size, "#ababab")
    region = GripRegion(base, labels, [9], [tube], [full], size, True, [2],
                        cylinder_layers=[0], continuity=(anchor, previous))
    frame, _ = region.prepare(png_bytes(base))
    painted = Image.new("RGB", region.box_size, "#ababab")
    with Image.open(io.BytesIO(region.finish(frame, painted, {}))) as result:
        assert result.getpixel((62, 24)) == anchor.getpixel((62, 24))
        assert result.getpixel((50, 108)) == anchor.getpixel((50, 108))
        assert result.getpixel((36, 58)) == base.getpixel((36, 58))
        assert result.getpixel((80, 67)) == shaded.getpixel((80, 67))
