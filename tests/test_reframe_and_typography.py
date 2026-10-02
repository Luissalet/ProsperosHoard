"""Reframing pictures and clips into other shapes without generating them
again, the cut's framing of clips of another shape, and the typographic
lyric styles (pop, pulse, typewriter, handwritten, cinema)."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from prosperos_hoard import engine, video
from test_qa import _clip, _save


def test_typographic_lyric_styles_make_their_events():
    clip = {"start_s": 1.0, "end_s": 3.0, "text": "neon rain falls", "karaoke": True}
    pop = video.build_ass(1080, 1920, [clip], style="pop")
    events = [ln for ln in pop.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == 3 and "NEON" in events[0] and "\\fscx135" in events[0]
    assert ",5," in [ln for ln in pop.splitlines() if ln.startswith("Style:")][0]  # middle of the screen
    pulse = video.build_ass(1920, 1080, [clip], style="pulse", beats=[0.5, 1.5, 2.0, 2.99])
    line = [ln for ln in pulse.splitlines() if ln.startswith("Dialogue:")][0]
    assert line.count("\\fscx114") == 2 and "NEON RAIN FALLS" in line  # the beats inside the line only
    typed = video.build_ass(1080, 1080, [clip], style="typewriter")
    assert typed.count("\\ko") == len("neonrainfalls") and "Special Elite" in typed
    for style in ("handwritten", "cinema"):
        assert "neon rain falls" in video.build_ass(1080, 1920, [clip], style=style)
    with pytest.raises(video.RenderError):
        video.build_ass(1080, 1920, [clip], style="comic")


def test_framing_is_part_of_the_finishing():
    assert video.validate_finishing({"framing": "blur"}) == {"framing": "blur"}
    assert video.validate_finishing({"framing": "fill"}) == {}
    with pytest.raises(video.RenderError):
        video.validate_finishing({"framing": "stretch"})
    assert "boxblur" in video.framing_vf(1080, 1920, "blur")
    assert "pad=1080:1920" in video.framing_vf(1080, 1920, "fit")
    assert "crop=1080:1920:(iw-1080)*0.200" in video.framing_vf(1080, 1920, "fill", 0.2)


def test_the_subject_is_found_where_the_detail_is(store, project):
    img = Image.new("RGB", (640, 360), (40, 40, 48))
    d = ImageDraw.Draw(img)
    for i in range(0, 120, 6):  # a busy, sharp subject on the right
        d.line([(480 + i % 60, 90 + i), (560, 270 - i)], fill=(250, 240, 200), width=2)
    aid = _save(store, project["id"], img)
    fx, fy = engine.subject_focus(store, store.get_asset(aid))
    assert fx > 0.65 and 0.35 < fy < 0.65


def _run(store, params):
    job = {"id": "job_x", "params": params}
    return engine.reframe_job(store, job, lambda *a, **k: None)


def test_reframe_a_picture_and_a_clip(store, project):
    img = Image.new("RGB", (640, 360), (10, 10, 10))
    ImageDraw.Draw(img).rectangle([500, 120, 600, 260], fill=(255, 255, 255))
    aid = _save(store, project["id"], img)
    out = _run(store, {"asset_id": aid, "aspect": "9:16", "framing": "fill"})
    a = store.get_asset(out["asset_id"])
    assert (a["width"], a["height"]) == (1080, 1920) and a["recipe"]["operation"] == "reframe"
    assert a["recipe"]["focus"][0] > 0.6  # the crop went to the white box
    with Image.open(store.data_dir / a["file_path"]) as im:
        px = np.asarray(im.convert("L"))
    assert px.max() > 200  # the subject is in the crop
    blur = store.get_asset(_run(store, {"asset_id": aid, "aspect": "1:1", "framing": "blur"})["asset_id"])
    assert (blur["width"], blur["height"]) == (1080, 1080)
    frames = np.zeros((24, 72, 128), dtype=np.uint8)
    frames[:, 20:50, 90:120] = 255
    vid = _clip(store, project["id"], frames)
    v = store.get_asset(_run(store, {"asset_id": vid, "aspect": "9:16", "framing": "fit", "quality": "preview"})["asset_id"])
    assert v["kind"] == "video" and (v["width"], v["height"]) == (720, 1280)


def test_reframe_over_http(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Reframe"}).json()["id"]
    aid = _save(store, pid, Image.new("RGB", (320, 180), (90, 30, 30)))
    r = c.post("/api/agent/studio_reframe", json={"asset_id": aid, "aspect": "4:5", "framing": "blur", "wait_s": 30})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["state"] == "done"
    assert c.post(f"/api/assets/{aid}/reframe", json={"asset_id": aid, "aspect": "5:4"}).status_code == 400


def test_a_production_gets_new_shapes_without_new_clips(store, project):
    from prosperos_hoard import productions as prod
    from test_productions import tiny_spec

    state = prod.create_production(store.data_dir, "Shapes", prod.normalise_spec(tiny_spec()), {}, project_id=project["id"])
    state["status"] = "done"
    state["done"] = {"timeline": {"complete": True, "timelines": {}}}
    prod.save_state(store.data_dir, state)
    out = prod.reframe(store, state["slug"], ["16:9", "9:16"], "blur")
    assert out["new"] == ["16:9"] and out["aspects"] == ["9:16", "16:9"]
    after = prod.load_state(store.data_dir, state["slug"])
    assert after["spec"]["timeline"]["finishing"]["framing"] == "blur"
    assert after["status"] == "queued" and after["done"]["timeline"]["complete"] is False
    with pytest.raises(prod.ProductionError):
        prod.reframe(store, state["slug"], ["4:3"])
