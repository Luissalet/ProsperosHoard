"""Brightness and saturation keyframes: the model limits, the ffmpeg graph (a per-frame eq driven by the keys), the operation and
the assistant tool that edit them, and a real render that follows the keys."""

import numpy as np
import pytest
from pydantic import ValidationError

from conftest import LOOK, needs_ffmpeg
from prosperos_hoard.editing import agent_tools
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.ops import apply_ops, op_signature
from prosperos_hoard.editing.render import compiler as C
from prosperos_hoard.editing.render import runner
from prosperos_hoard.editing.timeline import Clip, Filter, Keyframe, new_project

OUT = C.Output(320, 180, 30.0, "30")
MEDIA = C.MediaRef("m1", "x.mp4", "video", 1280, 720, True, True, 12000, fps=30.0)
CX = C.ChainCtx(OUT, lambda f: f, lambda c: None)

EQ = {"brightness": 0.2, "contrast": 1.3, "saturation": 1.5, "gamma": 0.9}
BRIGHT = [Keyframe(t=0, v=-0.5), Keyframe(t=1000, v=0.5)]
SAT = [Keyframe(t=0, v=0.0), Keyframe(t=2000, v=3.0)]
BRIGHT_EXPR = "clip(-0.5+1*clip(t-0,0,1),-1,1)"
SAT_EXPR = "clip(0+1.5*clip(t-0,0,2),0,3)"


def chain(clip, t0=None, t1=None) -> str:
    _, text, _, _ = C.clip_chain(0, clip, MEDIA, clip.start if t0 is None else t0, clip.end if t1 is None else t1, CX, "L")
    return text


def make(filters=None, keys=None, **kw) -> Clip:
    return Clip(media="m1", src_in=0, src_out=4000, start=4000, filters=filters or [], keyframes=keys or {}, **kw)


# ---------------------------------------------------------------- model and operation

def test_key_values_are_limited_to_the_eq_ranges():
    make(keys={"brightness": [Keyframe(t=0, v=-1), Keyframe(t=10, v=1)], "saturation": [Keyframe(t=0, v=0), Keyframe(t=10, v=3)]})
    for prop, bad in (("brightness", 1.01), ("brightness", -1.5), ("saturation", 3.2), ("saturation", -0.1)):
        with pytest.raises(ValidationError, match=prop):
            make(keys={prop: [Keyframe(t=0, v=bad)]})
    # the operation refuses what the model would, and says which limits apply
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 4000}], LOOK)
    cid = res[0]["clip"]
    for prop, bad in (("brightness", 1.5), ("saturation", -1)):
        with pytest.raises(LumiereError, match=f"{prop} must be between"):
            apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": prop, "keys": [{"t": 0, "v": bad}]}], LOOK)
    with pytest.raises(LumiereError):
        apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": "hue", "keys": [{"t": 0, "v": 0}]}], LOOK)
    # the generic clip setter is checked too
    with pytest.raises(LumiereError, match="saturation"):
        apply_ops(p, [{"op": "set", "clip": cid, "props": {"keyframes": {"saturation": [{"t": 0, "v": 9}]}}}], LOOK)


def test_keys_are_added_replaced_and_removed_and_text_or_audio_clips_refuse_them():
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 4000}, {"op": "add_text", "text": "Hola", "start": 0, "length": 1000}], LOOK)
    cid, tid = res[0]["clip"], res[1]["clip"]
    p, res = apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": "brightness", "keys": [{"t": 900, "v": 0.4, "ease": "ease_out"}, {"t": 0, "v": -0.2}]}], LOOK)
    assert res[0]["keys"] == 2
    keys = p.find(cid)[1].keyframes["brightness"]
    assert [(k.t, k.v, k.ease) for k in keys] == [(0, -0.2, "linear"), (900, 0.4, "ease_out")]
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": "brightness", "keys": []}], LOOK)
    assert "brightness" not in p.find(cid)[1].keyframes
    with pytest.raises(LumiereError, match="media clips on video tracks"):
        apply_ops(p, [{"op": "keyframes", "clip": tid, "prop": "saturation", "keys": [{"t": 0, "v": 1}]}], LOOK)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m4", "src_out": 2000}], LOOK)
    with pytest.raises(LumiereError, match="media clips on video tracks"):
        apply_ops(p, [{"op": "keyframes", "clip": res[0]["clip"], "prop": "saturation", "keys": [{"t": 0, "v": 1}]}], LOOK)


def test_split_keeps_interpolated_cut_keys_on_both_halves():
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 4000}], LOOK)
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": res[0]["clip"], "prop": "saturation", "keys": [{"t": 0, "v": 0.5}, {"t": 3000, "v": 2}]}], LOOK)
    p, _ = apply_ops(p, [{"op": "split", "at": 1000}], LOOK)
    first, second = sorted(p.main_track().clips, key=lambda c: c.start)
    # linear: at 1000 ms value is 0.5 + (2-0.5)*(1000/3000) = 1.0
    assert [(k.t, k.v) for k in first.keyframes["saturation"]] == [(0, 0.5), (1000, 1.0)]
    assert [(k.t, k.v) for k in second.keyframes["saturation"]] == [(0, 1.0), (2000, 2.0)]
    # same rule for transform keys (x) and for opacity
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 4000}], LOOK)
    cid = res[0]["clip"]
    p, _ = apply_ops(p, [
        {"op": "keyframes", "clip": cid, "prop": "x", "keys": [{"t": 0, "v": 0}, {"t": 2000, "v": 1}]},
        {"op": "keyframes", "clip": cid, "prop": "opacity", "keys": [{"t": 0, "v": 1}, {"t": 2000, "v": 0}]},
    ], LOOK)
    p, _ = apply_ops(p, [{"op": "split", "at": 1000}], LOOK)
    left, right = sorted(p.main_track().clips, key=lambda c: c.start)
    assert [(k.t, k.v) for k in left.keyframes["x"]] == [(0, 0.0), (1000, 0.5)]
    assert [(k.t, k.v) for k in right.keyframes["x"]] == [(0, 0.5), (1000, 1.0)]
    assert [(k.t, round(k.v, 6)) for k in left.keyframes["opacity"]] == [(0, 1.0), (1000, 0.5)]
    assert [(k.t, round(k.v, 6)) for k in right.keyframes["opacity"]] == [(0, 0.5), (1000, 0.0)]


# ---------------------------------------------------------------- the filter graph

def test_keyed_brightness_becomes_a_per_frame_eq_and_leaves_the_static_params_alone():
    text = chain(make([Filter(type="eq", params=EQ)], {"brightness": BRIGHT}))
    assert f"eq=brightness='{BRIGHT_EXPR}':contrast=1.300:saturation=1.500:gamma=0.900:eval=frame" in text
    assert text.count("eq=") == 1


def test_keyed_saturation_and_both_together():
    text = chain(make([Filter(type="eq", params=EQ)], {"saturation": SAT}))
    assert f"eq=brightness=0.200:contrast=1.300:saturation='{SAT_EXPR}':gamma=0.900:eval=frame" in text
    both = chain(make([Filter(type="eq", params=EQ)], {"brightness": BRIGHT, "saturation": SAT}))
    assert f"eq=brightness='{BRIGHT_EXPR}':contrast=1.300:saturation='{SAT_EXPR}':gamma=0.900:eval=frame" in both


def test_the_expression_follows_the_clip_time_of_the_piece():
    # a piece that starts 1 s into the clip reads the keys 1 s later, as the opacity keys do
    clip = make([Filter(type="eq", params=EQ)], {"brightness": BRIGHT})
    assert C.eq_key_exprs(clip, 1.0) == {"brightness": "clip(-0.5+1*clip(t--1,0,1),-1,1)"}
    assert "eq=brightness='clip(-0.5+1*clip(t--1,0,1),-1,1)'" in chain(clip, 5000, 6000)
    # easing adds the same intermediate points as every other keyed prop
    eased = make(keys={"saturation": [Keyframe(t=0, v=1, ease="ease_in_out"), Keyframe(t=1000, v=2)]})
    assert C.eq_key_exprs(eased, 0.0)["saturation"].count("clip(t-") == 6


def test_a_clip_without_these_keys_compiles_exactly_as_before():
    # the strings below were produced by the compiler before the keys existed
    assert chain(make()) == ("[0:v]settb=AVTB,setpts='(0+1*clip(T-0,0,4)+1*clip(T-4,0,1.1333333))/TB',fps=30,trim=start=0,trim=end_frame=120,"
                             "setpts=PTS-STARTPTS,scale=320:180,format=yuva420p[L]")
    other_keys = make([Filter(type="eq", params=EQ), Filter(type="warm")], {"opacity": [Keyframe(t=0, v=0.5), Keyframe(t=1000, v=1)]})
    assert chain(other_keys) == (
        "[0:v]settb=AVTB,setpts='(0+1*clip(T-0,0,4)+1*clip(T-4,0,1.1333333))/TB',fps=30,trim=start=0,trim=end_frame=120,setpts=PTS-STARTPTS,"
        "scale=320:180,eq=brightness=0.200:contrast=1.300:saturation=1.500:gamma=0.900,colorbalance=rs=0.060:gs=0.015:bs=-0.060:rm=0.040:bm=-0.040,"
        "format=yuva420p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*clip(0.5+0.5*clip(T-0,0,1),0,1)'[L]")


def test_keys_without_an_eq_effect_add_one_before_the_other_effects():
    text = chain(make([Filter(type="warm")], {"saturation": SAT}))
    eq = f"eq=brightness=0.000:contrast=1.000:saturation='{SAT_EXPR}':gamma=1.000:eval=frame"
    assert eq in text and text.index(eq) < text.index("colorbalance")
    assert eq in chain(make(keys={"saturation": SAT}))


def test_keys_apply_only_through_an_enabled_eq():
    off = make([Filter(type="eq", params=EQ, enabled=False)], {"brightness": BRIGHT, "saturation": SAT})
    assert C.eq_key_exprs(off, 0.0) == {}
    assert "eq=" not in chain(off)  # switching the effect off switches the grade (and its animation) off
    # with two eq effects the keys drive the first enabled one only
    two = make([Filter(type="eq", params=EQ, enabled=False), Filter(type="eq", params={"contrast": 1.1}), Filter(type="eq", params={"contrast": 1.2})],
               {"brightness": BRIGHT})
    text = chain(two)
    assert text.count("eq=") == 2 and text.count("eval=frame") == 1
    assert text.index("contrast=1.100") < text.index("contrast=1.200")
    assert f"eq=brightness='{BRIGHT_EXPR}':contrast=1.100" in text


# ---------------------------------------------------------------- assistant tool and persistence

@pytest.fixture
def lib(services, media_dir):
    return {name.split(".")[0]: media_store.import_path(services, str(media_dir / name))["id"] for name in ("scenes.mp4", "talk.mp4")}


@needs_ffmpeg
def test_assistant_tool_adds_reads_undoes_and_refuses_keys(services, lib):
    pid = store.create(services, "Color", width=320, height=180, fps=25, media=[lib["scenes"]])["id"]
    cid = store.doc(services, pid).main_track().clips[0].id
    ops = [{"op": "filter_add", "clips": [cid], "type": "eq", "params": {"contrast": 1.2}},
           {"op": "keyframes", "clip": cid, "prop": "brightness", "keys": [{"t": 0, "v": -0.3}, {"t": 1500, "v": 0.3, "ease": "ease_in"}]},
           {"op": "keyframes", "clip": cid, "prop": "saturation", "keys": [{"t": 0, "v": 0}, {"t": 1500, "v": 2}]}]
    out = agent_tools.call_tool(services, "timeline_edit", {"project": pid, "ops": ops, "label": "Colour"})
    assert [r["keys"] for r in out["results"][1:]] == [2, 2]
    got = agent_tools.call_tool(services, "project_get", {"project": pid})
    clip = next(t for t in got["tracks"] if t["role"] == "main")["clips"][0]
    assert clip["keyframes"]["brightness"] == [{"t": 0, "v": -0.3, "ease": "linear"}, {"t": 1500, "v": 0.3, "ease": "ease_in"}]
    assert [k["v"] for k in clip["keyframes"]["saturation"]] == [0, 2]
    assert store.doc(services, pid).main_track().clips[0].keyframes["saturation"][1].v == 2  # persisted in the document
    # a bad value fails the whole call with the limits in the message, and changes nothing
    with pytest.raises(LumiereError, match="brightness must be between -1 and 1"):
        agent_tools.call_tool(services, "timeline_edit", {"project": pid, "ops": [
            {"op": "keyframes", "clip": cid, "prop": "brightness", "keys": [{"t": 0, "v": 4}]}]})
    # one undo step takes the three operations back
    agent_tools.call_tool(services, "timeline_history", {"project": pid, "action": "undo"})
    assert not store.doc(services, pid).main_track().clips[0].keyframes
    # the tool and operation descriptions list the new props
    tool = next(t for t in agent_tools.TOOLS if t.name == "timeline_edit")
    assert "brightness (-1..1)" in tool.description and "saturation (0..3)" in tool.description
    assert "brightness" in op_signature("keyframes") and "saturation" in op_signature("keyframes")


# ---------------------------------------------------------------- a real render follows the keys

def _frame(services, pid, t):
    from PIL import Image

    return np.asarray(Image.open(runner.render_frame(services, pid, t, fmt="png")).convert("RGB")).astype(int)


@needs_ffmpeg
def test_render_follows_the_brightness_and_saturation_keys(services, lib):
    pid = store.create(services, "Keys", width=320, height=180, fps=25, media=[lib["scenes"]])["id"]  # 2 s of pure red first
    cid = store.doc(services, pid).main_track().clips[0].id
    store.edit(services, pid, [{"op": "keyframes", "clip": cid, "prop": "brightness", "keys": [{"t": 0, "v": -0.4}, {"t": 1800, "v": 0.4}]}])
    dark, mid, bright = (_frame(services, pid, t).mean(axis=(0, 1)) for t in (0, 900, 1800))
    assert dark[0] < mid[0] < bright[0] and bright[0] - dark[0] > 60  # the red channel rises with the key (no eq effect was added by hand)
    store.edit(services, pid, [{"op": "keyframes", "clip": cid, "prop": "brightness", "keys": []},
                               {"op": "keyframes", "clip": cid, "prop": "saturation", "keys": [{"t": 0, "v": 0}, {"t": 1800, "v": 2}]}])
    grey, colour = _frame(services, pid, 0)[90, 160], _frame(services, pid, 1800)[90, 160]
    assert abs(grey[0] - grey[1]) < 12 and abs(grey[1] - grey[2]) < 12  # saturation 0: grey
    assert colour[0] - colour[1] > 100  # saturation 2: red
    # a static grade without keys is untouched by all this: same picture as the project with no effect at all
    store.edit(services, pid, [{"op": "keyframes", "clip": cid, "prop": "saturation", "keys": []}])
    before = _frame(services, pid, 500)
    store.edit(services, pid, [{"op": "filter_add", "clips": [cid], "type": "eq", "params": {}}])
    assert np.abs(_frame(services, pid, 500) - before).max() <= 2


@needs_ffmpeg
def test_export_with_keys_on_a_clip_split_in_chunks_is_valid(services, lib):
    pid = store.create(services, "Export", width=320, height=180, fps=25, media=[lib["talk"]])["id"]
    cid = store.doc(services, pid).main_track().clips[0].id
    store.edit(services, pid, [{"op": "trim", "clip": cid, "src_in": 0, "src_out": 3000},
                               {"op": "filter_add", "clips": [cid], "type": "eq", "params": {"contrast": 1.1}},
                               {"op": "keyframes", "clip": cid, "prop": "brightness", "keys": [{"t": 0, "v": 0}, {"t": 3000, "v": -0.3}]},
                               {"op": "keyframes", "clip": cid, "prop": "saturation", "keys": [{"t": 0, "v": 1}, {"t": 3000, "v": 0.2}]}])
    job = services.jobs.get(services.start_render(pid, preset="preview")["id"])
    assert job["state"] == "done", job["error"]
    assert job["result"]["qc"]["ok"], job["result"]["qc"]
    assert abs(job["result"]["duration_ms"] - 3000) < 70

# ---------------------------------------------------------------- split preserves ease curves

from prosperos_hoard.editing.timeline import keyframe_value, slice_keyframes


@pytest.mark.parametrize('ease', ['ease_in', 'ease_out', 'ease_in_out', 'hold', 'linear'])
def test_slice_keyframes_preserves_curve_both_halves(ease):
    keys = [Keyframe(t=0, v=0.0, ease=ease), Keyframe(t=1000, v=1.0)]
    cut = 500
    left = slice_keyframes({'opacity': keys}, 0, cut)['opacity']
    right = slice_keyframes({'opacity': keys}, cut, 1000)['opacity']
    for t in (0, 125, 250, 375, 500):
        assert abs(keyframe_value(left, t) - keyframe_value(keys, t)) < 1e-9, (ease, 'L', t)
    for t in (500, 625, 750, 875, 1000):
        assert abs(keyframe_value(right, t - cut) - keyframe_value(keys, t)) < 1e-9, (ease, 'R', t, keyframe_value(right, t - cut), keyframe_value(keys, t))


def test_slice_ease_in_cut_500_keeps_original_750():
    keys = [Keyframe(t=0, v=0.0, ease='ease_in'), Keyframe(t=1000, v=1.0)]
    assert abs(keyframe_value(keys, 750) - 0.5625) < 1e-9
    right = slice_keyframes({'opacity': keys}, 500, 1000)['opacity']
    assert abs(keyframe_value(right, 250) - 0.5625) < 1e-9
    # Off-sample fractional time from CAS4 review (not a bake grid point).
    assert abs(keyframe_value(keys, 537) - keyframe_value(right, 37)) < 1e-12
    assert right[0].ease == 'ease_in' and right[0].ease_span == 1000 and right[0].ease_into == 500


def test_slice_multipoint_ease_preserves_mid_spans():
    keys = [
        Keyframe(t=0, v=0.0, ease='ease_out'),
        Keyframe(t=400, v=0.5, ease='ease_in'),
        Keyframe(t=1000, v=1.0, ease='hold'),
        Keyframe(t=1600, v=0.2),
    ]
    for lo, hi in ((0, 400), (200, 800), (400, 1000), (700, 1400), (1000, 1600), (0, 1600)):
        sliced = slice_keyframes({'opacity': keys}, lo, hi)['opacity']
        for t in range(lo, hi + 1, 17):
            assert abs(keyframe_value(sliced, t - lo) - keyframe_value(keys, t)) < 1e-9, (lo, hi, t)


def test_slice_single_key_keeps_constant():
    keys = [Keyframe(t=500, v=0.5)]
    sliced = slice_keyframes({'opacity': keys}, 0, 1000)['opacity']
    assert sliced and sliced[0].v == 0.5
    assert abs(keyframe_value(sliced, 0) - 0.5) < 1e-12
    assert abs(keyframe_value(sliced, 800) - 0.5) < 1e-12
    before = slice_keyframes({'opacity': keys}, 0, 400)['opacity']
    after = slice_keyframes({'opacity': keys}, 600, 1000)['opacity']
    assert keyframe_value(before, 100) == 0.5 and keyframe_value(after, 0) == 0.5


def test_keyframes_op_clears_ease_domain_only_on_edited_segment():
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 2000}], LOOK)
    cid = res[0]["clip"]
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": "opacity",
                          "keys": [{"t": 0, "v": 0, "ease": "ease_in"}, {"t": 1000, "v": 1}]}], LOOK)
    p, _ = apply_ops(p, [{"op": "split", "at": 500}], LOOK)
    right = sorted(p.main_track().clips, key=lambda c: c.start)[1]
    dom = right.keyframes["opacity"][0]
    assert dom.ease_span == 1000
    # Edit the end key: domain on the (only) segment must drop.
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": right.id, "prop": "opacity",
                          "keys": [{"t": 0, "v": 0.25, "ease": "ease_in", "ease_span": 1000,
                                    "ease_into": 500, "ease_v0": 0, "ease_v1": 1},
                                   {"t": 500, "v": 0.9}]}], LOOK)
    edited = p.find(right.id)[1].keyframes["opacity"]
    assert edited[0].ease_span is None and edited[1].v == 0.9
    assert abs(keyframe_value(edited, 250) - (0.25 + (0.9 - 0.25) * 0.25)) < 1e-9


def test_edit_last_segment_keeps_first_domain_value_537():
    """Multipoint split: editing the last key must leave the first segment's 537 value exact."""
    from prosperos_hoard.editing.timeline import reconcile_key_domains
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 3000}], LOOK)
    cid = res[0]["clip"]
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": "opacity", "keys": [
        {"t": 0, "v": 0, "ease": "ease_in"},
        {"t": 1000, "v": 1, "ease": "ease_out"},
        {"t": 2000, "v": 0.2},
    ]}], LOOK)
    before_537 = keyframe_value(p.find(cid)[1].keyframes["opacity"], 537)
    assert abs(before_537 - 0.288369) < 1e-9
    p, _ = apply_ops(p, [{"op": "split", "at": 500}], LOOK)
    right = sorted(p.main_track().clips, key=lambda c: c.start)[1]
    rk = right.keyframes["opacity"]
    assert rk[0].ease_span == 1000 and rk[0].ease_into == 500
    assert abs(keyframe_value(rk, 37) - before_537) < 1e-12
    # Edit only the last key (MCP-style: plain t/v/ease, no domain fields).
    plain = [{"t": k.t, "v": k.v, "ease": k.ease} for k in rk]
    plain[-1] = {"t": rk[-1].t, "v": 0.05, "ease": "linear"}
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": right.id, "prop": "opacity", "keys": plain}], LOOK)
    after = p.find(right.id)[1].keyframes["opacity"]
    assert after[0].ease_span == 1000 and after[0].ease_into == 500
    assert abs(keyframe_value(after, 37) - before_537) < 1e-12
    assert after[-1].v == 0.05
    # Reconcile restores domain from prev when endpoints match even if the payload lies.
    lied = [Keyframe(t=after[0].t, v=after[0].v, ease=after[0].ease, ease_span=999, ease_into=0, ease_v0=0, ease_v1=1)]
    for k in after[1:]:
        lied.append(Keyframe(t=k.t, v=k.v, ease=k.ease))
    restored = reconcile_key_domains(after, lied)
    assert restored[0].ease_span == 1000 and restored[0].ease_into == 500


def test_repeated_cuts_preserve_ease_in_domain():
    keys = [Keyframe(t=0, v=0.0, ease='ease_in'), Keyframe(t=1000, v=1.0)]
    mid = slice_keyframes({'opacity': keys}, 200, 800)['opacity']
    again = slice_keyframes({'opacity': mid}, 100, 400)['opacity']  # original [300, 600]
    for t in (300, 411, 537, 600):
        assert abs(keyframe_value(again, t - 300) - keyframe_value(keys, t)) < 1e-12, t


def test_split_op_preserves_ease_in_at_750():
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 2000}], LOOK)
    cid = res[0]["clip"]
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": "opacity",
                          "keys": [{"t": 0, "v": 0, "ease": "ease_in"}, {"t": 1000, "v": 1}]}], LOOK)
    before = keyframe_value(p.find(cid)[1].keyframes["opacity"], 750)
    assert abs(before - 0.5625) < 1e-9
    p, _ = apply_ops(p, [{"op": "split", "at": 500}], LOOK)
    left, right = sorted(p.main_track().clips, key=lambda c: c.start)
    orig = [Keyframe(t=0, v=0, ease='ease_in'), Keyframe(t=1000, v=1)]
    assert abs(keyframe_value(left.keyframes["opacity"], 375) - keyframe_value(orig, 375)) < 1e-12
    assert abs(keyframe_value(right.keyframes["opacity"], 250) - 0.5625) < 1e-12
    assert abs(keyframe_value(right.keyframes["opacity"], 37) - keyframe_value(orig, 537)) < 1e-12
