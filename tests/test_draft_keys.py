"""Draft generation keeps identity and structure on separate graph inputs."""

import io

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
