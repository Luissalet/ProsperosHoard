"""Speed curves, nested sequences and shape masks: the model, the operations and real renders checked on frames and sound."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import LOOK, VIDEO, media_lookup_from, needs_ffmpeg
from prosperos_hoard.editing import agent_tools
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.ops import apply_ops, nest_plan
from prosperos_hoard.editing.render import runner, sequences
from prosperos_hoard.editing.timeline import Clip, SpeedKey, new_project

# ---------------------------------------------------------------- model and operations (no ffmpeg)


def _one_clip(src_out=12000):
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": src_out}], LOOK)
    return p, res[0]["clip"]


def _integral(keys, a, b, n=200000):
    from prosperos_hoard.editing.timeline import speed_value

    s = np.linspace(a, b, n + 1)
    v = np.array([speed_value(keys, x) for x in s])
    return float(np.sum((1 / v[:-1] + 1 / v[1:]) / 2 * np.diff(s)))


def test_speed_curve_duration_is_the_integral_and_split_keeps_it():
    keys = [SpeedKey(t=0, v=1, ease="ease_in_out"), SpeedKey(t=4000, v=3), SpeedKey(t=8000, v=0.5, ease="ease_in")]
    c = Clip(media="m1", src_in=0, src_out=10000, speed_keys=keys)
    assert abs(c.duration - _integral(keys, 0, 10000)) <= 1
    # timeline <-> source round trip, and speed at the playhead
    for t in (0, 700, 2500, c.duration // 2, c.duration - 5):
        assert abs(c.timeline_at(c.src_at(t)) - t) < 0.01
    assert abs(c.speed_at(c.timeline_at(4000) + 1) - 3) < 0.05
    rev = c.model_copy(update={"reverse": True})
    assert rev.duration == c.duration and abs(rev.src_at(0) - 10000) < 1e-6 and abs(rev.src_at(rev.duration) - 0) < 2
    # splitting a curved clip: both halves keep the curve, the total stays the same (+-1 ms)
    p, cid = _one_clip()
    p, _ = apply_ops(p, [{"op": "speed_ramp", "clip": cid, "keys": [{"t": 0, "v": 1, "ease": "ease_in_out"}, {"t": 4000, "v": 3},
                                                                     {"t": 8000, "v": 0.5}]}], LOOK)
    before = p.main_track().clips[0].duration
    p2, res = apply_ops(p, [{"op": "split", "at": 1500}], LOOK)
    halves = sorted(p2.main_track().clips, key=lambda c: c.start)
    assert len(halves) == 2 and all(h.speed_keys for h in halves)
    assert abs(sum(h.duration for h in halves) - before) <= 1
    assert halves[1].start == halves[0].end


def test_speed_ramp_op_presets_ripple_and_limits():
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 6000}, {"op": "add_media", "media": "m2", "src_out": 3000}], LOOK)
    a, b = res[0]["clip"], res[1]["clip"]
    p2, out = apply_ops(p, [{"op": "speed_ramp", "clip": a, "preset": "hit", "speed": 0.25}], LOOK)
    ca = p2.find(a)[1]
    assert ca.duration > 6000 and len(ca.speed_keys) == 4 and min(k.v for k in ca.speed_keys) == 0.25
    assert p2.find(b)[1].start == ca.end  # ripple: the next clip follows the longer clip
    assert out[0]["keys"][0]["t"] == ca.speed_keys[0].t  # reported relative to the clip's in point (src_in is 0 here)
    p3, _ = apply_ops(p2, [{"op": "speed_ramp", "clip": a, "preset": "ease_in_out", "speed": 2}], LOOK)
    assert p3.find(a)[1].duration < 6000 and p3.find(b)[1].start == p3.find(a)[1].end
    p4, _ = apply_ops(p3, [{"op": "speed", "clip": a, "speed": 1}], LOOK)  # a constant speed replaces the curve
    assert not p4.find(a)[1].speed_keys and p4.find(a)[1].duration == 6000
    p5, _ = apply_ops(p3, [{"op": "speed_ramp", "clip": a, "preset": "clear"}], LOOK)
    assert p5.find(a)[1].duration == 6000
    # relative keys count from the in point; aliases reach the op
    p6, _ = apply_ops(p, [{"op": "trim", "clip": a, "src_in": 1000, "ripple": False}, {"op": "ramp", "clip": a, "keys": [{"t": 0, "v": 2}]}], LOOK)
    assert p6.find(a)[1].speed_keys[0].t == 1000 and p6.find(a)[1].duration == 2500
    pic = apply_ops(p, [{"op": "add_media", "media": "m3", "length": 2000}], LOOK)
    with pytest.raises(LumiereError):
        apply_ops(pic[0], [{"op": "speed_ramp", "clip": pic[1][0]["clip"], "preset": "speed_up"}], LOOK)
    with pytest.raises(LumiereError):
        apply_ops(p, [{"op": "speed_ramp", "clip": a, "keys": [{"t": 0, "v": 2}, {"t": 0, "v": 3}]}], LOOK)


def test_mask_op_set_and_keyframes():
    p, cid = _one_clip()
    p, res = apply_ops(p, [{"op": "mask", "clip": cid, "shape": "rounded", "w": 0.5, "h": 0.4, "feather": 0.05}], LOOK)
    m = p.find(cid)[1].mask
    assert (m.shape, m.w, m.h, m.feather, m.x) == ("rounded", 0.5, 0.4, 0.05, 0.5)
    p, _ = apply_ops(p, [{"op": "set", "clip": cid, "props": {"mask": {"invert": True}}}], LOOK)
    assert p.find(cid)[1].mask.invert and p.find(cid)[1].mask.shape == "rounded"
    p, _ = apply_ops(p, [{"op": "keyframes", "clip": cid, "prop": "mask_x", "keys": [{"t": 0, "v": 0.2}, {"t": 2000, "v": 0.8}]}], LOOK)
    assert len(p.find(cid)[1].keyframes["mask_x"]) == 2
    p, _ = apply_ops(p, [{"op": "mask", "clip": cid, "remove": True}], LOOK)
    assert p.find(cid)[1].mask is None and "mask_x" not in p.find(cid)[1].keyframes
    p, res = apply_ops(p, [{"op": "add_text", "text": "Hola", "start": 0, "length": 1000}], LOOK)
    with pytest.raises(LumiereError):
        apply_ops(p, [{"op": "mask", "clip": res[0]["clip"], "shape": "ellipse"}], LOOK)


def test_nest_plan_and_cycles_are_refused_by_the_ops():
    p = new_project(1280, 720, 30)
    p, res = apply_ops(p, [{"op": "add_media", "media": "m1", "src_out": 3000}, {"op": "add_media", "media": "m1", "src_in": 5000, "src_out": 8000},
                           {"op": "add_media", "media": "m2", "src_out": 2000}], LOOK)
    a, b, c = (r["clip"] for r in res)
    nested, where = nest_plan(p, [b, c])
    assert where["start"] == 3000 and where["length"] == 5000 and nested.length_mode == "longest"
    assert [x.start for x in nested.main_track().clips] == [0, 3000]
    with pytest.raises(LumiereError):
        nest_plan(p, [a, c])  # b sits between them on the same track
    seq = {"id": "prj_inner", "name": "inner", "kind": "sequence", "duration_ms": 4000, "width": 1280, "height": 720, "has_audio": True,
           "has_video": True, "contains": ["prj_outer"]}
    look = media_lookup_from({"m1": VIDEO, "prj_inner": seq})
    with pytest.raises(LumiereError) as error:
        apply_ops(p, [{"op": "add_sequence", "project": "prj_inner"}], look, project_id="prj_outer")
    assert "loop" in str(error.value)
    with pytest.raises(LumiereError):
        apply_ops(p, [{"op": "add_media", "media": "prj_inner"}], look, project_id="prj_inner")
    ok, out = apply_ops(p, [{"op": "add_sequence", "project": "prj_inner", "src_in": 500}], look, project_id="prj_other")
    clip = ok.find(out[0]["clip"])[1]
    assert clip.type == "sequence" and clip.duration == 3500


# ---------------------------------------------------------------- renders

def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def engine_media(tmp_path_factory):
    d = tmp_path_factory.mktemp("engine")
    # a frame counter: three blocks whose grey level spells the frame index in base 8, plus a steady tone
    counter = ("color=s=192x64:r=30:d=12,geq=lum='if(lt(X,64),mod(N,8)*32+16,if(lt(X,128),mod(floor(N/8),8)*32+16,floor(N/64)*32+16))'"
               ":cb=128:cr=128")
    _ff("-f", "lavfi", "-i", counter, "-f", "lavfi", "-i", "sine=f=440:r=48000:d=12", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8",
        "-g", "30", "-c:a", "aac", "-shortest", str(d / "counter.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=6", "-f", "lavfi", "-i", "sine=f=330:r=48000:d=6", "-c:v", "libx264", "-preset",
        "ultrafast", "-c:a", "aac", "-shortest", str(d / "red.mp4"))
    _ff("-f", "lavfi", "-i", "color=c=0x0000ff:s=320x180", "-frames:v", "1", str(d / "blue.png"))
    return d


@pytest.fixture
def emedia(services, engine_media, media_dir):
    out = {}
    for name in ("counter.mp4", "red.mp4", "blue.png"):
        out[name.split(".")[0]] = media_store.import_path(services, str(engine_media / name))["id"]
    for name in ("talk.mp4", "scenes.mp4"):
        out[name.split(".")[0]] = media_store.import_path(services, str(media_dir / name))["id"]
    return out


def _render(services, pid, **kw):
    job = services.jobs.get(services.start_render(pid, **kw)["id"])
    assert job["state"] == "done", job["error"]
    return job["result"]


def _frames(path: Path, w: int, h: int) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(int)


def _counter_index(frame: np.ndarray) -> int:
    h, w = frame.shape
    digits = [int(round((frame[h // 4: 3 * h // 4, x - w // 12: x + w // 12].mean() - 16) / 32)) for x in (w // 6, w // 2, 5 * w // 6)]
    return digits[0] + 8 * digits[1] + 64 * digits[2]


def _expected_frame(clip, f: int, fps: float = 30) -> int:
    """The source frame (of a 30 fps source) the timeline shows in output frame f: the last one, in playback order, that
    the timeline reaches before the middle of the output frame (fast parts skip source frames, slow parts repeat them).
    Reversed, a frame is reached at its end (source time k + 1)."""
    limit = (f + 0.5) * 1000 / fps
    guess = int(clip.src_at(f * 1000 / fps) * 30 / 1000)
    shown = [k for k in range(max(0, guess - 8), guess + 9) if clip.timeline_at((k + clip.reverse) * 1000 / 30) < limit]
    return (min(shown) if clip.reverse else max(shown)) if shown else guess


def _rgb(path: Path):
    from PIL import Image

    return np.asarray(Image.open(path).convert("RGB")).astype(int)


def _audio_ms(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) * 1000


@needs_ffmpeg
@pytest.mark.parametrize("reverse", [False, True])
def test_speed_ramp_render_shows_the_expected_source_frames_and_sound_follows(services, emedia, reverse):
    pid = store.create(services, "Rampa", width=192, height=64, fps=30, media=[emedia["counter"]])["id"]
    cid = store.doc(services, pid).main_track().clips[0].id
    store.edit(services, pid, [{"op": "trim", "clip": cid, "src_in": 500, "src_out": 9000}, {"op": "set", "clip": cid, "props": {"reverse": reverse}},
                               {"op": "speed_ramp", "clip": cid, "keys": [{"t": 0, "v": 1, "ease": "ease_in_out"}, {"t": 3000, "v": 3},
                                                                           {"t": 6000, "v": 3, "ease": "ease_in_out"}, {"t": 7500, "v": 0.5}]}])
    clip = store.doc(services, pid).main_track().clips[0]
    expected_ms = clip.duration
    assert abs(expected_ms - _integral(clip.speed_keys, 500, 9000)) <= 1
    res = _render(services, pid, preset="final", lufs=None)
    out = Path(res["path"])
    frames = _frames(out, 192, 64)
    assert abs(len(frames) - round(expected_ms * 30 / 1000)) <= 1  # +-1 frame
    got = [_counter_index(f) for f in frames]
    want = [_expected_frame(clip, f) for f in range(len(frames))]
    off = [abs(g - w) for g, w in zip(got, want)]
    assert max(off) <= 1 and sum(1 for x in off if x) <= len(off) // 50, list(zip(got, want))  # every source frame where the curve puts it
    for t in (0, 1000, 2000, 2600, 3300):  # slow, accelerating, fast: the counter moves 1x, faster, 3x
        f = int(t * 30 / 1000)
        assert abs(got[f] - want[f]) <= 1
    assert abs(_audio_ms(out) - expected_ms) <= 40  # the sound lasts what the picture lasts (one AAC frame of slack)
    # the exact frame agrees with the export
    still = runner.render_frame(services, pid, 2600, fmt="png")
    from PIL import Image

    grey = np.asarray(Image.open(still).convert("L")).astype(int)
    assert abs(_counter_index(grey) - _expected_frame(clip, int(2600 * 30 / 1000))) <= 1


@needs_ffmpeg
def test_speed_ramp_sound_stays_in_sync_with_the_curve(services, tmp_path):
    """A click every source second must be heard when the curve shows that second (within a frame), slow and fast parts."""
    _ff("-f", "lavfi", "-i", "color=s=192x64:r=30:d=12", "-f", "lavfi", "-i", "aevalsrc='if(lt(mod(t,1),0.01),sin(2*PI*2000*t)*0.9,0)':s=48000:d=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le", "-shortest", str(tmp_path / "clicks.mov"))
    mid = media_store.import_path(services, str(tmp_path / "clicks.mov"))["id"]
    pid = store.create(services, "Sincronía", width=192, height=64, fps=30, media=[mid])["id"]
    cid = store.doc(services, pid).main_track().clips[0].id
    store.edit(services, pid, [{"op": "trim", "clip": cid, "src_in": 500, "src_out": 10500},
                               {"op": "speed_ramp", "clip": cid, "keys": [{"t": 0, "v": 0.5, "ease": "ease_in_out"}, {"t": 4000, "v": 3},
                                                                           {"t": 7000, "v": 3, "ease": "ease_in_out"}, {"t": 9000, "v": 0.6}]}])
    clip = store.doc(services, pid).main_track().clips[0]
    res = _render(services, pid, preset="audio_wav", lufs=None)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", res["path"], "-f", "s16le", "-ac", "1", "-"], capture_output=True).stdout
    pcm = np.abs(np.frombuffer(raw, np.int16).astype(float))
    env = np.array([pcm[i:i + 48].max() for i in range(0, len(pcm) - 48, 48)])  # peak per ms
    onsets, i = [], 0
    while i < len(env):
        if env[i] > 3000:
            onsets.append(i)
            i += 150
        else:
            i += 1
    want = [clip.timeline_at(k * 1000) - clip.start for k in range(1, 11)]
    assert len(onsets) == len(want), onsets
    assert max(abs(a - b) for a, b in zip(onsets, want)) <= 33, list(zip(onsets, want))


@needs_ffmpeg
def test_speed_ramp_sound_is_continuous(services, emedia):
    pid = store.create(services, "Rampa audio", width=192, height=64, fps=30, media=[emedia["counter"]])["id"]
    cid = store.doc(services, pid).main_track().clips[0].id
    store.edit(services, pid, [{"op": "trim", "clip": cid, "src_out": 4000}, {"op": "speed_ramp", "clip": cid, "preset": "speed_up", "speed": 2}])
    res = _render(services, pid, preset="audio_wav", lufs=None)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", res["path"], "-f", "s16le", "-ac", "1", "-"], capture_output=True).stdout
    pcm = np.frombuffer(raw, np.int16).astype(float)
    dur = store.doc(services, pid).main_track().clips[0].duration
    assert abs(len(pcm) / 48 - dur) <= 2
    rms = [np.sqrt(np.mean(pcm[i:i + 2400] ** 2)) for i in range(0, len(pcm) - 2400, 2400)]
    assert min(rms[1:-1]) > 0.5 * np.median(rms)  # no step of the ramp drops out


@needs_ffmpeg
def test_nested_sequence_renders_like_the_original_and_follows_edits(services, emedia):
    pid = store.create(services, "Padre", width=320, height=180, fps=30, media=[emedia["talk"], emedia["scenes"]])["id"]
    store.edit(services, pid, [{"op": "add_text", "text": "Hola", "start": 500, "length": 2000}])
    main = sorted(store.doc(services, pid).main_track().clips, key=lambda c: c.start)
    times = (500, 4000, 13000)
    before = [_rgb(runner.render_frame(services, pid, t, fmt="png")) for t in times]
    duration = store.doc(services, pid).duration
    res = store.nest(services, pid, [c.id for c in main], name="Grupo")
    sid = res["sequence"]
    p = store.doc(services, pid)
    assert [c.type for c in p.main_track().clips] == ["sequence"] and p.duration == duration
    after = [_rgb(runner.render_frame(services, pid, t, fmt="png")) for t in times]
    for x, y in zip(before, after):
        assert np.abs(x - y).mean() < 3.0  # the same picture, within compression
    # the nested project cannot take its parent
    with pytest.raises(LumiereError) as error:
        store.edit(services, sid, [{"op": "add_sequence", "project": pid}])
    assert "loop" in str(error.value)
    with pytest.raises(LumiereError):
        store.edit(services, pid, [{"op": "add_sequence", "project": pid}])
    # editing the nested project makes a new intermediate and the parent shows the change
    key1 = sequences.cached(services, sid).key
    store.edit(services, sid, [{"op": "filter_add", "type": "grayscale"}])
    assert sequences.cached(services, sid) is None
    grey = _rgb(runner.render_frame(services, pid, 4000, fmt="png"))
    assert sequences.cached(services, sid).key != key1
    assert np.abs(grey[..., 0] - grey[..., 2]).mean() < 4 and np.abs(before[1][..., 0] - before[1][..., 2]).mean() > 20
    # the export is real: frame-exact length, sound present
    out = _render(services, pid, preset="preview")
    assert out["qc"]["ok"], out["qc"]
    assert abs(out["duration_ms"] - duration) < 70
    # un-nest brings the clips back
    seq_clip = store.doc(services, pid).main_track().clips[0]
    store.edit(services, sid, [{"op": "filter_remove", "clip": c.id} for c in store.doc(services, sid).main_track().clips])
    store.edit(services, pid, [{"op": "unnest", "clip": seq_clip.id}])
    back = store.doc(services, pid)
    assert [c.media for c in sorted(back.main_track().clips, key=lambda c: c.start)] == [emedia["talk"], emedia["scenes"]]
    assert back.duration == duration and any(c.type == "text" for t in back.tracks for c in t.clips)


@needs_ffmpeg
def test_nested_overlays_stay_transparent(services, emedia):
    pid = store.create(services, "Capas", width=320, height=180, fps=30, media=[emedia["red"]])["id"]
    track = store.edit(services, pid, [{"op": "track_add", "kind": "video", "name": "PiP"}])["results"][0]["track"]
    cid = store.edit(services, pid, [{"op": "add_media", "media": emedia["blue"], "track": track, "at": 0, "length": 3000}])["results"][0]["clip"]
    store.edit(services, pid, [{"op": "set", "clip": cid, "props": {"transform": {"scale": 0.4, "fit": "contain"}}}])
    before = _rgb(runner.render_frame(services, pid, 1000, fmt="png"))
    res = store.nest(services, pid, [cid])
    assert sequences.has_alpha(store.doc(services, res["sequence"]))
    after = _rgb(runner.render_frame(services, pid, 1000, fmt="png"))
    assert after[5, 5, 0] > 200 and after[5, 5, 2] < 60  # the red base still shows around the nested overlay
    assert np.abs(before - after).mean() < 3.0


@needs_ffmpeg
def test_shape_masks_cut_the_picture(services, emedia):
    pid = store.create(services, "Máscaras", width=320, height=180, fps=30, media=[emedia["red"]])["id"]
    track = store.edit(services, pid, [{"op": "track_add", "kind": "video", "name": "PiP"}])["results"][0]["track"]
    cid = store.edit(services, pid, [{"op": "add_media", "media": emedia["blue"], "track": track, "at": 0, "length": 4000}])["results"][0]["clip"]
    store.edit(services, pid, [{"op": "set", "clip": cid, "props": {"transform": {"fit": "fill"}}},
                               {"op": "mask", "clip": cid, "shape": "ellipse", "w": 0.5, "h": 0.5, "feather": 0.1}])

    def frame(t):
        return _rgb(runner.render_frame(services, pid, t, fmt="png"))

    img = frame(1000)
    inside, outside, edge = img[90, 160], img[10, 10], img[90, 160 + 80]  # ellipse half width = 80 px
    assert inside[2] > 200 and inside[0] < 60  # the clip
    assert outside[0] > 200 and outside[2] < 60  # the track below
    assert 60 < edge[2] < 200 and 60 < edge[0] < 200  # feather: a mix
    store.edit(services, pid, [{"op": "mask", "clip": cid, "invert": True, "feather": 0}])
    img = frame(1000)
    assert img[90, 160][0] > 200 and img[10, 10][2] > 200
    store.edit(services, pid, [{"op": "mask", "clip": cid, "invert": False, "shape": "rounded", "w": 0.5, "h": 0.5, "radius": 0.5}])
    img = frame(1000)
    assert img[90, 160][2] > 200 and img[10, 10][0] > 200
    # animated: the mask travels from the left to the right
    store.edit(services, pid, [{"op": "mask", "clip": cid, "shape": "rectangle", "w": 0.2, "h": 0.4},
                               {"op": "keyframes", "clip": cid, "prop": "mask_x", "keys": [{"t": 0, "v": 0.2}, {"t": 3000, "v": 0.8}]}])
    early, late = frame(0), frame(2999)
    assert early[90, 64][2] > 200 and early[90, 256][0] > 200
    assert late[90, 256][2] > 200 and late[90, 64][0] > 200
    res = _render(services, pid, preset="preview", end=2000)
    assert res["qc"]["ok"], res["qc"]


@needs_ffmpeg
def test_assistant_tools_reach_the_new_features(services, emedia):
    pid = store.create(services, "Herramientas", width=320, height=180, fps=30, media=[emedia["talk"], emedia["scenes"]])["id"]
    clips = sorted(store.doc(services, pid).main_track().clips, key=lambda c: c.start)
    out = agent_tools.call_tool(services, "timeline_edit", {"project": pid, "ops": [{"op": "speed_ramp", "clip": clips[0].id, "preset": "hit"}]})
    assert out["results"][0]["duration"] > 12000
    got = agent_tools.call_tool(services, "project_get", {"project": pid})
    main = next(t for t in got["tracks"] if t["role"] == "main")
    assert main["clips"][0]["speed_curve"]
    nested = agent_tools.call_tool(services, "timeline_nest", {"project": pid, "action": "nest", "clips": [clips[1].id], "name": "Escenas"})
    assert nested["sequence"].startswith("prj_")
    info = agent_tools.call_tool(services, "timeline_nest", {"project": pid, "action": "list"})
    assert info["sequences"][0]["name"] == "Escenas" and info["sequences"][0]["ready"] is False
    back = agent_tools.call_tool(services, "timeline_nest", {"project": pid, "action": "unnest", "clip": nested["clip"]})
    assert back["results"][0]["clips"]


@needs_ffmpeg
def test_nesting_routes_for_the_editor(client, media_dir):
    talk = client.post("/api/media/import", json={"path": str(media_dir / "talk.mp4")}).json()["id"]
    scenes = client.post("/api/media/import", json={"path": str(media_dir / "scenes.mp4")}).json()["id"]
    pid = client.post("/api/projects", json={"name": "Rutas", "preset": "hd720", "media": [talk, scenes]}).json()["id"]
    clips = client.get(f"/api/projects/{pid}").json()["doc"]["tracks"][0]["clips"]
    r = client.post(f"/api/projects/{pid}/nest", json={"clips": [c["id"] for c in clips], "name": "Todo"})
    assert r.status_code == 200, r.text
    body = r.json()
    seq = body["sequence"]
    assert body["view"]["media"][seq]["kind"] == "sequence" and body["view"]["media"][seq]["urls"]["play"] is None
    assert client.get(f"/api/projects/{pid}/nesting").json()["sequences"][0]["project"] == seq
    assert client.get(f"/api/projects/{seq}/nesting").json()["used_by"] == [{"id": pid, "name": "Rutas"}]
    assert client.get(f"/api/projects/{seq}/sequence.mp4").status_code == 404
    job = client.post(f"/api/projects/{seq}/sequence/prepare").json()
    assert client.get(f"/api/jobs/{job['job']}").json()["state"] == "done"
    assert client.get(f"/api/projects/{pid}").json()["media"][seq]["urls"]["play"]
    video = client.get(f"/api/projects/{seq}/sequence.mp4")
    assert video.status_code == 200 and len(video.content) > 1000
    bad = client.post(f"/api/projects/{seq}/edit", json={"ops": [{"op": "add_sequence", "project": pid}]})
    assert bad.status_code == 400 and "loop" in bad.json()["error"]
