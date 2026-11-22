import hashlib
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from prosperos_hoard import regional_edit


def _image_bytes(image):
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def _import(c, folder, project, name, image):
    path = folder / name
    path.write_bytes(_image_bytes(image))
    response = c.post("/api/agent/studio_import", params={"project": project}, json={"path": str(path)})
    assert response.status_code == 200, response.text
    return response.json()["id"]


@pytest.mark.parametrize("mode", ["paste", "qwen"])
def test_regional_edit_preserves_exterior_and_has_verified_receipt(client, fake_comfy, mode):
    c, app, folder = client
    project = app.state.store.create_project("Mask")["id"]
    base = Image.new("RGBA", (96, 64), (213, 192, 167, 123))
    base.putpixel((0, 0), (1, 2, 3, 0))
    drawing = Image.new("RGB", (96, 64), (228, 208, 190))
    ImageDraw.Draw(drawing).rectangle((6, 7, 39, 26), fill=(40, 72, 115))
    source = _import(c, folder, project, "source.png", base)
    reference = _import(c, folder, project, "ink.png", drawing)
    args = {"asset_id": source, "mode": mode, "wait_s": 12, "feather_px": 3}
    if mode == "paste":
        args.update(quad=[[8, 6], [84, 10], [88, 58], [11, 52]], rotation=180, drawing_asset_id=reference,
                    saturation=0, clip_polygon=[[20, 6], [90, 6], [90, 60], [10, 60], [22, 35]])
    else:
        args.update(box=[8, 6, 84, 52], prompt="Use the blue of <image2> in <image1>",
                    reference_asset_ids=[reference], seed=24, steps=3)

    response = c.post("/api/agent/studio_regional_edit", json=args)
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["state"] == "done", job
    [asset_id] = job["asset_ids"]
    data = c.get(f"/api/assets/{asset_id}/file").content
    receipt = job["receipt"]
    mask = c.get(receipt["mask_url"])
    assert mask.status_code == 200
    alpha = np.asarray(Image.open(io.BytesIO(mask.content)))
    output = np.asarray(Image.open(io.BytesIO(data)))
    original = np.asarray(base)
    assert np.array_equal(output[alpha == 0], original[alpha == 0])
    assert np.array_equal(output[..., 3], original[..., 3])
    assert np.any(output[alpha == 255] != original[alpha == 255])
    assert np.any((alpha > 0) & (alpha < 255))
    assert receipt["outside_changed_pixels"] == receipt["outside_changed_percent"] == 0
    assert receipt["outside_pixels"] == int(np.count_nonzero(alpha == 0))
    assert receipt["output_sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt["mask_sha256"] == hashlib.sha256(mask.content).hexdigest()
    assert [i["sha256"] for i in receipt["inputs"]] == [
        hashlib.sha256(_image_bytes(i)).hexdigest() for i in (base, drawing)
    ]
    lineage = c.get("/api/agent/studio_lineage", params={"asset_id": asset_id}).json()
    assert lineage["reproduce"]["tool"] == "studio_regional_edit"
    assert lineage["reproduce"]["args"]["asset_id"] == source
    if mode == "paste":
        assert output[41, 68, 0] < 100   # the ink moves from upper left to lower right
        assert output[19, 32, 0] > 180
        assert receipt["saturation"] == lineage["reproduce"]["args"]["saturation"] == 0
        assert np.array_equal(output[41, 68, :3].astype(int) - original[41, 68, :3],
                              np.repeat(int(output[41, 68, 0]) - int(original[41, 68, 0]), 3))
    else:
        assert lineage["recipe"]["template"] == "qwen21_edit"
        server, _ = fake_comfy
        assert "images.image_2" in server.prompts_seen[-1]["459:474"]["inputs"]


@pytest.mark.parametrize("args", [
    {"box": [-1, 0, 5, 5]}, {"box": [5, 0, 3, 5]}, {"quad": [[0, 0], [5, 5], [5, 0], [0, 5]]},
    {"box": [0, 0, 2, 2], "clip_polygon": [[3, 3], [4, 3], [4, 4], [3, 4]]}, {},
])
def test_invalid_or_empty_geometry_is_rejected(args):
    with pytest.raises(ValueError):
        regional_edit.Region.create((8, 8), **args)


def test_box_uses_exclusive_right_and_bottom_pixel_edges():
    region = regional_edit.Region.create((10, 10), box=(1, 2, 7, 6))
    assert region.mask((10, 10)).getbbox() == (1, 2, 7, 6)
