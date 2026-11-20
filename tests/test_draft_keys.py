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
