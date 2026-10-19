import pytest

from conftest import LOOK
from prosperos_hoard.editing.errors import LumiereError, NotFound
from prosperos_hoard.editing.ops import apply_ops, source_to_timeline
from prosperos_hoard.editing.timeline import new_project, validate


def build(ops, p=None):
    p = p or new_project(1920, 1080, 30)
    return apply_ops(p, ops, LOOK)


def main_clips(p):
    return sorted(p.main_track().clips, key=lambda c: c.start)


def test_add_media_appends_in_order_and_fits():
    p, res = build([{"op": "add_media", "media": "m1"}, {"op": "add_media", "media": "m2", "src_in": 1000, "src_out": 3000},
                    {"op": "add_media", "media": "m3", "length": 2500}])
    clips = main_clips(p)
    assert [(c.start, c.end) for c in clips] == [(0, 12000), (12000, 14000), (14000, 16500)]
    assert clips[0].transform.fit == "cover"  # same orientation and aspect
    assert clips[1].transform.fit == "contain"  # vertical on a horizontal canvas
    assert p.duration == 16500
    assert res[0]["clip"] == clips[0].id


def test_audio_media_goes_to_an_audio_track_and_not_on_video():
    p, res = build([{"op": "add_media", "media": "m4", "src_out": 5000}])
    t = p.track(res[0]["track"])
    assert t.kind == "audio" and t.role == "music"
    with pytest.raises(LumiereError):
        build([{"op": "add_media", "media": "m4", "track": p.main_track().id}], p)


def test_unknown_media_and_op_fail_without_changing_anything():
    p, _ = build([{"op": "add_media", "media": "m1"}])
    with pytest.raises(NotFound):
        apply_ops(p, [{"op": "split", "at": 1000}, {"op": "add_media", "media": "nope"}], LOOK)
    assert len(main_clips(p)) == 1
    with pytest.raises(LumiereError, match="Unknown operation"):
        apply_ops(p, [{"op": "explode"}], LOOK)


def test_split_and_ripple_delete():
    p, _ = build([{"op": "add_media", "media": "m1"}, {"op": "add_media", "media": "m2"}])
    p, res = build([{"op": "split", "at": "0:04"}], p)
    clips = main_clips(p)
    assert [(c.start, c.end, c.src_in, c.src_out) for c in clips[:2]] == [(0, 4000, 0, 4000), (4000, 12000, 4000, 12000)]
    p, _ = build([{"op": "delete", "clips": [clips[0].id]}], p)
    clips = main_clips(p)
    assert clips[0].start == 0 and clips[0].src_in == 4000
    assert clips[1].start == 8000
    assert p.duration == 14000


def test_delete_range_ripples_every_track_and_markers():
    p, res = build([{"op": "add_media", "media": "m1"}, {"op": "add_media", "media": "m4", "at": 0, "src_out": 12000},
                    {"op": "marker_add", "t": 5000, "label": "a"}, {"op": "marker_add", "t": 9000, "label": "b"}])
    p, _ = build([{"op": "delete_range", "start": 4000, "end": 6000}], p)
    assert p.duration == 10000
    music = p.track(res[1]["track"])
    assert [(c.start, c.end) for c in sorted(music.clips, key=lambda c: c.start)] == [(0, 4000), (4000, 10000)]
    assert [m.t for m in p.markers] == [7000]


def test_cut_source_maps_through_speed_and_order():
    p, _ = build([{"op": "add_media", "media": "m1", "src_in": 0, "src_out": 6000}, {"op": "add_media", "media": "m1", "src_in": 6000, "src_out": 12000}])
    second = main_clips(p)[1]
    p, _ = build([{"op": "speed", "clip": second.id, "speed": 2.0}], p)
    assert p.duration == 9000
    assert source_to_timeline(p, "m1", [(7000, 9000)]) == [(6500, 7500)]
    p, res = build([{"op": "cut_source", "media": "m1", "ranges": [[1000, 2000], [7000, 9000]]}], p)
    assert res[0]["cuts"] == 2
    assert p.duration == 9000 - 1000 - 1000


def test_keep_source_keeps_only_those_ranges():
    p, _ = build([{"op": "add_media", "media": "m1"}])
    p, _ = build([{"op": "keep_source", "media": "m1", "ranges": [[2000, 4000], [8000, 9000]]}], p)
    clips = main_clips(p)
    assert [(c.src_in, c.src_out) for c in clips] == [(2000, 4000), (8000, 9000)]
    assert p.duration == 3000


def test_transition_overlaps_and_removal_restores():
    p, _ = build([{"op": "add_media", "media": "m1", "src_out": 4000}, {"op": "add_media", "media": "m2", "src_out": 4000}])
    b = main_clips(p)[1]
    p, _ = build([{"op": "transition", "clip": b.id, "type": "wipe_left", "dur": 500}], p)
    clips = main_clips(p)
    assert clips[1].start == 3500 and clips[1].transition_in.type == "wipe_left"
    assert not [i for i in validate(p, LOOK) if i["level"] == "error"]
    p, _ = build([{"op": "transition", "clip": b.id, "type": None}], p)
    assert main_clips(p)[1].start == 4000 and main_clips(p)[1].transition_in is None


def test_transition_on_all_cuts_and_first_clip_fades_in():
    p, _ = build([{"op": "add_media", "media": "m1", "src_out": 3000}, {"op": "add_media", "media": "m1", "src_in": 3000, "src_out": 6000},
                  {"op": "add_media", "media": "m1", "src_in": 6000, "src_out": 9000}])
    p, res = build([{"op": "transition", "all_cuts": True, "type": "crossfade", "dur": 400}], p)
    assert len(res[0]["clips"]) == 2
    assert p.duration == 9000 - 800


def test_overlap_is_reported_by_validate():
    p, _ = build([{"op": "add_media", "media": "m1", "src_out": 4000}, {"op": "add_media", "media": "m2", "src_out": 4000}])
    clips = main_clips(p)
    clips[1].start = 3000
    issues = validate(p, LOOK)
    assert any(i["code"] == "overlap" for i in issues)


def test_trim_with_ripple_moves_followers():
    p, _ = build([{"op": "add_media", "media": "m1", "src_out": 4000}, {"op": "add_media", "media": "m2", "src_out": 4000}])
    a = main_clips(p)[0]
    p, _ = build([{"op": "trim", "clip": a.id, "src_out": 3000}], p)
    assert main_clips(p)[1].start == 3000
    with pytest.raises(LumiereError):
        build([{"op": "trim", "clip": a.id, "src_in": 2990, "src_out": 3000}], p)


def test_text_clip_and_set_merge_style():
    p, res = build([{"op": "add_text", "text": "Hola", "start": 500, "length": "2s", "style": {"size": 100}}])
    cid = res[0]["clip"]
    p, _ = build([{"op": "set", "clip": cid, "props": {"style": {"color": "#FF0000"}, "transform": {"y": -0.3}}}], p)
    _, c = p.find(cid)
    assert c.style.size == 100 and c.style.color == "#FF0000" and c.transform.y == -0.3 and c.length == 2000
    with pytest.raises(LumiereError, match="Not settable"):
        build([{"op": "set", "clip": cid, "props": {"evil": 1}}], p)


def test_filters_are_checked():
    p, res = build([{"op": "add_media", "media": "m1"}])
    p, _ = build([{"op": "filter_add", "type": "eq", "params": {"saturation": 1.5}}], p)
    assert main_clips(p)[0].filters[0].params["saturation"] == 1.5
    with pytest.raises(LumiereError):
        build([{"op": "filter_add", "type": "eq", "params": {"saturation": 50}}], p)
    with pytest.raises(LumiereError):
        build([{"op": "filter_add", "type": "eq", "params": {"x": "y"}}], p)


def test_canvas_preset_and_locked_track():
    p, _ = build([{"op": "add_media", "media": "m1"}, {"op": "canvas", "preset": "reels", "fit": "cover"}])
    assert (p.canvas.width, p.canvas.height) == (1080, 1920)
    main = p.main_track()
    p, _ = build([{"op": "track_set", "track": main.id, "props": {"locked": True}}], p)
    with pytest.raises(LumiereError, match="locked"):
        build([{"op": "split", "clip": main.clips[0].id, "at": 1000}], p)


def test_detach_audio_and_close_gaps_and_duplicate():
    p, res = build([{"op": "add_media", "media": "m1", "src_out": 3000}, {"op": "add_media", "media": "m2", "at": 5000, "src_out": 2000}])
    a = res[0]["clip"]
    p, r2 = build([{"op": "detach_audio", "clip": a}], p)
    assert p.find(a)[1].mute
    assert p.track(r2[0]["track"]).kind == "audio"
    p, _ = build([{"op": "close_gaps", "track": p.main_track().id}], p)
    assert [c.start for c in main_clips(p)] == [0, 3000]
    p, r3 = build([{"op": "duplicate", "clip": a}], p)
    assert p.find(r3[0]["clip"])[1].start == 3000


def test_keyframes_sorted_and_removed():
    p, res = build([{"op": "add_media", "media": "m3", "length": 3000}])
    cid = res[0]["clip"]
    p, _ = build([{"op": "keyframes", "clip": cid, "prop": "scale", "keys": [{"t": 3000, "v": 1.3}, {"t": 0, "v": 1.0}]}], p)
    assert [k.t for k in p.find(cid)[1].keyframes["scale"]] == [0, 3000]
    p, _ = build([{"op": "keyframes", "clip": cid, "prop": "scale", "keys": []}], p)
    assert "scale" not in p.find(cid)[1].keyframes


def test_length_mode_main_cuts_music_tail():
    p, _ = build([{"op": "add_media", "media": "m1", "src_out": 5000}, {"op": "add_media", "media": "m4", "at": 0}])
    assert p.duration == 5000 and p.content_end == 60000
    p, _ = build([{"op": "canvas", "length_mode": "longest"}], p)
    assert p.duration == 60000


def test_slip_roll_and_paste():
    p, res = build([{"op": "add_media", "media": "m1", "src_in": 2000, "src_out": 5000}, {"op": "add_media", "media": "m1", "src_in": 6000, "src_out": 9000}])
    a, b = res[0]["clip"], res[1]["clip"]
    p, r = build([{"op": "slip", "clip": a, "delta": 1000}], p)
    _, ca = p.find(a)
    assert (ca.start, ca.src_in, ca.src_out) == (0, 3000, 6000)
    p, r = build([{"op": "slip", "clip": a, "delta": -9000}], p)
    assert p.find(a)[1].src_in == 0 and r[0]["applied_ms"] == -3000
    p, r = build([{"op": "roll", "clip": b, "delta": 500}], p)
    ca, cb = p.find(a)[1], p.find(b)[1]
    assert ca.end == cb.start == 3500 and cb.src_in == 6500 and p.duration == 6000
    copied = [p.find(a)[1].model_dump()]
    p, r = build([{"op": "insert_clips", "clips": copied, "at": 6000}], p)
    new = p.find(r[0]["clips"][0])[1]
    assert new.id != a and new.start == 6000 and new.src_in == ca.src_in


def test_blur_fit_is_valid():
    p, res = build([{"op": "add_media", "media": "m1", "fit": "blur"}, {"op": "canvas", "preset": "reels"}])
    assert main_clips(p)[0].transform.fit == "blur"


def test_op_aliases_and_argument_guesses():
    from prosperos_hoard.editing.ops import parse_op

    op = parse_op({"op": "add_title", "text": "Hola", "at": 0, "duration_ms": 3000})
    assert op.op == "add_text" and op.start == 0 and op.length == 3000
    op = parse_op({"op": "remove_clip", "clip_ids": ["c1"]})
    assert op.op == "delete" and op.clips == ["c1"]


def test_op_errors_explain_the_fields():
    import pytest

    from prosperos_hoard.editing.errors import LumiereError
    from prosperos_hoard.editing.ops import OP_NAMES, op_reference, parse_op

    with pytest.raises(LumiereError) as error:
        parse_op({"op": "addtext", "text": "x"})
    assert "add_text {text" in str(error.value)
    with pytest.raises(LumiereError) as error:
        parse_op({"op": "split"})
    assert "Expected: split {at, clip?}" in str(error.value)
    assert len(op_reference().splitlines()) == len(OP_NAMES) == 40


def _find_track(p, track_id):
    return next(t for t in p.tracks if t.id == track_id)


def test_add_overlay_puts_muted_cover_broll_on_a_free_track():
    p, res = build([{"op": "add_media", "media": "m1", "src_out": 10000},
                    {"op": "add_overlay", "media": "m2", "start": 2000, "length": 3000, "src_in": 500},
                    {"op": "add_overlay", "media": "m3", "start": 6000, "length": 1500}])
    first, second = res[1], res[2]
    track = _find_track(p, first["track"])
    assert track.kind == "video" and track.role == "overlay" and track.name == "B-roll" and p.tracks.index(track) == len(p.tracks) - 1
    assert second["track"] == first["track"]  # a disjoint range reuses the free overlay track
    c = p.find(first["clip"])[1]
    assert (c.start, c.end, c.src_in, c.src_out, c.mute, c.transform.fit) == (2000, 5000, 500, 3500, True, "cover")
    img = p.find(second["clip"])[1]
    assert (img.start, img.end) == (6000, 7500) and img.mute
    assert p.duration == 10000  # overlays never lengthen the edit
    # an overlapping range does not clear the first: it goes to a new track
    p, r = build([{"op": "add_overlay", "media": "m2", "start": 3000, "length": 1000}], p)
    assert r[0]["track"] != first["track"] and p.find(first["clip"])[1].end == 5000
    # a media shorter than the range covers what it can and says so
    p, r = build([{"op": "add_overlay", "media": "m2", "start": 8000, "length": 4000, "src_in": 4000}], p)
    assert r[0]["end"] - r[0]["start"] == 2000 and r[0]["short_by"] == 2000
    with pytest.raises(LumiereError, match="no picture"):
        build([{"op": "add_overlay", "media": "m4", "start": 0, "length": 1000}], p)
    with pytest.raises(LumiereError, match="video track"):
        build([{"op": "add_overlay", "media": "m2", "start": 0, "length": 1000, "track": next(t.id for t in p.tracks if t.kind == "audio")}], p)


def _slot_project():
    p, res = build([{"op": "add_media", "media": "m1", "src_in": 0, "src_out": 2000},       # intro (keeps its 2 s)
                    {"op": "add_media", "media": "m1", "src_in": 2000, "src_out": 6000},    # main (takes its media's whole length)
                    {"op": "add_media", "media": "m1", "src_in": 8000, "src_out": 10000},   # outro
                    {"op": "add_text", "text": "Gracias", "start": 6000, "length": 2000},   # sits on the outro
                    {"op": "add_text", "text": "Hola", "start": 200, "length": 1000},       # sits on the intro
                    {"op": "marker_add", "t": 6000, "label": "Fin"},
                    {"op": "add_media", "media": "m4", "at": 0, "src_in": 0, "src_out": 8000}])
    ids = [r["clip"] for r in res[:3]]
    p, _ = build([{"op": "set", "clip": ids[0], "props": {"slot": "intro"}}, {"op": "set", "clip": ids[1], "props": {"slot": "main"}},
                  {"op": "set", "clip": ids[2], "props": {"slot": "outro"}}], p)
    return p, ids


def test_slot_names_one_clip_and_fill_slot_applies_the_length_rules():
    p, ids = _slot_project()
    with pytest.raises(LumiereError, match="already on clip"):
        build([{"op": "set", "clip": ids[0], "props": {"slot": "main"}}], p)
    with pytest.raises(NotFound, match="Slots here: intro, main, outro"):
        build([{"op": "fill_slot", "slot": "nope", "media": "m1"}], p)
    # main: a 6 s vertical media replaces a 4 s slot -> 6 s, everything after moves by 2 s (clips, titles, markers)
    q, res = build([{"op": "fill_slot", "slot": "main", "media": "m2"}], p)
    main = q.find(ids[1])[1]
    assert (main.media, main.start, main.end, main.src_in, main.src_out) == ("m2", 2000, 8000, 0, 6000) and res[0]["delta_ms"] == 2000
    outro = q.find(ids[2])[1]
    assert outro.start == 8000 and q.duration == 10000
    texts = {c.text: c.start for _, c in q.all_clips() if c.type == "text"}
    assert texts == {"Hola": 200, "Gracias": 8000}  # the title over the intro stays, the one over the outro moved
    assert q.markers[0].t == 8000
    # the music that ran to the old end follows the new one (its file is long enough)
    music = next(c for t in q.tracks if t.role == "music" for c in t.clips)
    assert music.end == 10000
    # intro keeps its length: an image fills the 2 s slot, a 1 s request shortens it and pulls the rest in
    q2, _ = build([{"op": "fill_slot", "slot": "intro", "media": "m3"}], p)
    intro = q2.find(ids[0])[1]
    assert (intro.media, intro.start, intro.end) == ("m3", 0, 2000) and q2.find(ids[1])[1].start == 2000
    q3, r3 = build([{"op": "fill_slot", "slot": "intro", "media": "m2", "length": 1000}], p)
    assert q3.find(ids[0])[1].end == 1000 and q3.find(ids[1])[1].start == 1000 and r3[0]["rule"] == "keep"
    # the clip keeps its look
    p, _ = build([{"op": "set", "clip": ids[1], "props": {"volume_db": -6, "transform": {"scale": 1.2}}},
                  {"op": "filter_add", "clips": [ids[1]], "type": "grayscale"}], p)
    q, _ = build([{"op": "fill_slot", "slot": "main", "media": "m2"}], p)
    c = q.find(ids[1])[1]
    assert c.volume_db == -6 and c.transform.scale == 1.2 and [f.type for f in c.filters] == ["grayscale"] and c.reframe is None
    # an audio-only media cannot fill a picture slot
    with pytest.raises(LumiereError, match="no picture"):
        build([{"op": "fill_slot", "slot": "main", "media": "m4"}], p)
    # the explicit rule beats the name
    q, _ = build([{"op": "fill_slot", "slot": "outro", "media": "m2", "rule": "full"}], p)
    assert q.find(ids[2])[1].duration == 6000 and q.duration == 12000
