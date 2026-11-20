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
