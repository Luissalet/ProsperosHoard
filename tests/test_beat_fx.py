"""Beat effects in the cut's finishing: a punch-in zoom, a flash and a shake
on the song's hits (bass drum, beats or downbeats), applied per clip."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import audio, video

SR = audio.SAMPLE_RATE


def _kick_track(seconds: float = 4.0, every: float = 0.5, offset: float = 0.25) -> np.ndarray:
    """A bass drum (55 Hz thump with a fast decay) every `every` seconds
    from `offset`, with a quiet hi-hat noise between them."""
    t = np.arange(int(seconds * SR)) / SR
    out = np.zeros_like(t, dtype=np.float32)
    rng = np.random.default_rng(1)
    k = offset
    while k < seconds:
        i = int(k * SR)
        n = int(0.25 * SR)
        seg = np.arange(min(n, len(t) - i)) / SR
        out[i:i + len(seg)] += 0.9 * np.sin(2 * np.pi * 55 * seg) * np.exp(-seg / 0.06)
        h = int((k + every / 2) * SR)
        if h < len(t):
            m = min(int(0.03 * SR), len(t) - h)
            out[h:h + m] += 0.08 * rng.standard_normal(m) * np.exp(-np.arange(m) / (0.01 * SR))
        k += every
    return out


def test_kick_hits_land_on_the_bass_drum():
    hits = audio.beat_hits(_kick_track(), "kick")
    times = [t for t, _ in hits]
    expected = [0.25 + 0.5 * i for i in range(8)]
    assert len(times) == len(expected), hits
    for got, want in zip(times, expected):
        assert abs(got - want) < 0.05
    assert all(0 < a <= 1 for _, a in hits)


def test_silence_gives_no_hits_and_sources_are_checked():
    assert audio.beat_hits(np.zeros(SR * 2, dtype=np.float32), "kick") == []
    with pytest.raises(ValueError):
        audio.beat_hits(_kick_track(), "snare")
    beats = audio.beat_hits(_kick_track(), "beats")
    downs = audio.beat_hits(_kick_track(), "downbeats")
    assert beats and downs and len(downs) < len(beats) and all(a == 1.0 for _, a in downs)


def test_finishing_validates_beat_fx():
    assert video.validate_finishing({"beat_fx": {"source": "beats", "zoom": 0.5, "flash": 0}}) == \
        {"beat_fx": {"source": "beats", "zoom": 0.5}}
    assert video.validate_finishing({"beat_fx": {"zoom": 0}}) == {}  # nothing on: no effect at all
    for bad in ({"source": "snare", "zoom": 1}, {"zoom": 2}, {"glow": 1}, "loud"):
        with pytest.raises(video.RenderError):
            video.validate_finishing({"beat_fx": bad})


def test_the_filter_only_carries_the_clips_own_hits():
    hits = [(0.2, 1.0), (1.4, 0.5), (2.9, 1.0), (7.0, 1.0)]
    local = video.clip_hits(hits, 1.3, 1.5)  # a clip from 1.3 s to 2.8 s
    assert local == [(0.1, 0.5)]
    vf = video.build_beat_fx_vf({"source": "kick", "zoom": 1, "flash": 1, "shake": 1}, local, 640, 360, 24)
    assert vf.startswith("zoompan=") and "eq=brightness=" in vf and vf.endswith("format=yuv420p")
    assert "2.900" not in vf and "7.000" not in vf
    assert video.build_beat_fx_vf({"zoom": 1}, [], 640, 360, 24) == ""
    # a whole song's worth of hits in one clip still stays far under the
    # Windows command line limit
    many = [(i * 0.25, 1.0) for i in range(40)]
    assert len(video.build_beat_fx_vf({"zoom": 1, "flash": 1, "shake": 1}, many, 1920, 1080, 30)) < 30000


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32000).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def _frame_luma(video_path: Path, t: float) -> float:
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video_path), "-frames:v", "1",
                          "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return float(np.frombuffer(out, dtype=np.uint8).mean())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_real_render_flashes_on_the_kick(tmp_path):
    from PIL import Image

    Image.new("RGB", (320, 180), (70, 70, 90)).save(tmp_path / "a.png")
    Image.new("RGB", (320, 180), (70, 70, 90)).save(tmp_path / "b.png")
    _write_wav(tmp_path / "song.wav", _kick_track(seconds=2.0, every=1.0, offset=0.5))
    timeline = {
        "width": 320, "height": 180, "fps": 24, "audio_asset_id": "song",
        "finishing": {"beat_fx": {"source": "kick", "flash": 1.0, "zoom": 0.6, "shake": 0.5}},
        "tracks": [{"type": "visual", "clips": [
            {"asset_id": "a", "kind": "image", "duration_s": 1.0, "trim_start_s": 0.0, "transition_in": {"type": "cut"}},
            {"asset_id": "b", "kind": "image", "duration_s": 1.0, "trim_start_s": 0.0, "transition_in": {"type": "cut"}},
        ]}],
    }
    paths = {"a": tmp_path / "a.png", "b": tmp_path / "b.png", "song": tmp_path / "song.wav"}
    out = tmp_path / "out.mp4"
    result = video.render_timeline(timeline, lambda i: paths[i], tmp_path / "work", out, quality="preview")
    assert out.is_file() and result["duration_s"] == pytest.approx(2.0)
    on_hit, between = _frame_luma(out, 0.52), _frame_luma(out, 0.95)
    assert on_hit > between + 8, (on_hit, between)
    on_second, before_second = _frame_luma(out, 1.52), _frame_luma(out, 1.40)
    assert on_second > before_second + 8, (on_second, before_second)


def test_a_productions_look_reaches_its_cut_and_re_renders_only_the_cut(store, project):
    from prosperos_hoard import productions as prod
    from test_productions import _asset, tiny_spec

    state = prod.create_production(store.data_dir, "Look", prod.normalise_spec(tiny_spec()), {}, project_id=project["id"])
    # a finished run: one 9:16 cut with its preview rendered
    still = _asset(store, project["id"])
    tracks = [{"type": "visual", "clips": [{"asset_id": still, "kind": "image", "duration_s": 2.0, "trim_start_s": 0.0}]}]
    tl_id = store.create_timeline(project["id"], "cut", "9:16", tracks=tracks)["id"]
    state = prod.load_state(store.data_dir, state["slug"])
    state["status"], state["stage"] = "done", None
    state["done"] = {"timeline": {"complete": True, "timelines": {"9:16": {"timeline_id": tl_id, "renders": {"preview": "a_old"}}}},
                     "report": {"path": "REPORT.md"}}
    prod.save_state(store.data_dir, state)
    out = prod.set_finishing(store, state["slug"], {"beat_fx": {"source": "downbeats", "flash": 0.7}, "vignette": True})
    assert out == {"finishing": {"vignette": True, "beat_fx": {"source": "downbeats", "flash": 0.7}}, "rerender": ["9:16"]}
    after = prod.load_state(store.data_dir, state["slug"])
    assert after["spec"]["timeline"]["finishing"]["beat_fx"]["flash"] == 0.7
    assert after["status"] == "queued" and prod.stage_status(after, "timeline") == "partial" and "report" not in after["done"]
    assert after["done"]["timeline"]["timelines"]["9:16"]["renders"] == {}
    assert store.get_timeline(tl_id)["finishing"]["vignette"] is True
    with pytest.raises(prod.ProductionError):
        prod.set_finishing(store, state["slug"], {"beat_fx": {"zoom": 5}})


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_a_canvas_is_a_seamless_vertical_loop_of_the_cut(store, project):
    from prosperos_hoard import canvas, productions as prod
    from prosperos_hoard.ids import new_id
    from test_productions import tiny_spec

    cut = store.path_for_asset_file(new_id("a"), ".mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=14",
                    "-pix_fmt", "yuv420p", str(cut)], check=True)
    rendered = store.create_asset(project_id=project["id"], kind="video", file_path=cut.relative_to(store.data_dir).as_posix(),
                                  mime="video/mp4", width=640, height=360, duration_s=14.0, source="rendered")
    state = prod.create_production(store.data_dir, "Loop", prod.normalise_spec(tiny_spec()), {}, project_id=project["id"])
    state["status"] = "done"
    state["done"] = {"timeline": {"complete": True, "timelines": {"16:9": {"timeline_id": "tl_x", "renders": {"preview": rendered["id"]}}}}}
    prod.save_state(store.data_dir, state)
    out = canvas.make_canvas(store, state["slug"], seconds=6, start_s=2.0)
    asset = store.get_asset(out["asset_id"])
    assert asset["width"] == 720 and asset["height"] == 1280 and asset["duration_s"] == 6.0
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-show_entries",
                            "format=duration", "-of", "json", str(store.data_dir / asset["file_path"])],
                           capture_output=True, text=True, check=True).stdout
    import json
    info = json.loads(probe)
    assert [s["codec_type"] for s in info["streams"]] == ["video"]  # silent
    assert info["streams"][0]["width"] == 720 and abs(float(info["format"]["duration"]) - 6.0) < 0.1
    assert prod.load_state(store.data_dir, state["slug"])["done"]["extras"]["canvas"] == out["asset_id"]
    with pytest.raises(prod.ProductionError):
        canvas.make_canvas(store, state["slug"], seconds=20)
    # no explicit start and no timing: a quarter of the way in
    auto = canvas.make_canvas(store, state["slug"], seconds=4)
    assert auto["start_from"] in ("chorus", "loudest section", "a quarter in")


def test_the_canvas_window_drops_captions_and_trims_the_first_clip():
    from prosperos_hoard import canvas
    tl = {"width": 1080, "height": 1920, "fps": 30, "audio_asset_id": "song",
          "finishing": {"color_grade": "teal_orange", "beat_fx": {"zoom": 1}, "lyric_style": "bold"},
          "tracks": [{"type": "visual", "clips": [
              {"asset_id": "a", "kind": "video", "duration_s": 4.0, "trim_start_s": 1.0, "transition_in": {"type": "cut"}},
              {"asset_id": "b", "kind": "image", "duration_s": 3.0, "transition_in": {"type": "crossfade", "duration_s": 0.3}},
              {"asset_id": "c", "kind": "image", "duration_s": 5.0, "transition_in": {"type": "cut"}},
              {"asset_id": "d", "kind": "image", "duration_s": 2.0}]},
              {"type": "lyrics", "clips": [{"text": "la", "start_s": 0, "end_s": 2}]}]}
    w = canvas.window_timeline(tl, 2.5, 6.0)
    clips = w["tracks"][0]["clips"]
    assert [c["asset_id"] for c in clips] == ["a", "b", "c"]
    assert clips[0]["trim_start_s"] == 3.5 and clips[0]["duration_s"] == 1.5 and clips[0]["transition_in"]["type"] == "cut"
    assert clips[1]["duration_s"] == 3.0 and clips[2]["duration_s"] == 1.5
    assert len(w["tracks"]) == 1 and w["audio_asset_id"] is None
    assert w["finishing"] == {"color_grade": "teal_orange"}
