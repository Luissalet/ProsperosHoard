"""Graphic clips and overlays in the timeline and the montage."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from prosperos_hoard import timeline as tl, video

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

TITLE = {"grammar": "title_card", "duration": 2.0, "seed": 1, "data": {"title": "Noches de cristal", "subtitle": "un vídeo"}}
LOWER = {"grammar": "lower_third", "data": {"name": "Ana Gómez", "caption": "voz"}}
KINETIC = {"grammar": "kinetic_lyrics", "data": {"lines": [{"text": "hola mundo", "start_s": 0.2, "end_s": 1.8}]}}


def lookup(assets):
    return lambda aid: assets.get(aid)


IMG = {"a": {"id": "a", "kind": "image"}}


def test_a_graphic_clip_and_a_graphics_track_are_valid_and_cleaned():
    tracks = [
        {"type": "visual", "clips": [
            {"asset_id": "a", "kind": "image", "duration_s": 2.0},
            {"kind": "graphic", "graphic": TITLE, "duration_s": 2.0, "graphic_offset_s": 0.5, "asset_id": "a"},
        ]},
        {"type": "graphics", "clips": [{"start_s": 1.0, "end_s": 3.0, "graphic": LOWER}]},
    ]
    clean = tl.normalise_tracks(tracks, lookup(IMG))
    clip = clean[0]["clips"][1]
    assert clip["kind"] == "graphic" and clip["start_s"] == 2.0 and clip["asset_id"] == "a"
    assert clip["graphic"]["duration"] == 2.5  # at least the offset plus the clip
    overlay = clean[1]["clips"][0]
    assert overlay["graphic"]["mode"] == "overlay" and overlay["graphic"]["duration"] == 2.0


def test_bad_graphics_are_refused_with_the_clip_index():
    base = {"type": "visual", "clips": [{"asset_id": "a", "kind": "image", "duration_s": 2.0}]}
    with pytest.raises(tl.TimelineError, match="visual clip 1.*grammar"):
        tl.normalise_tracks([{"type": "visual", "clips": base["clips"] + [{"kind": "graphic", "graphic": {"grammar": "x"}, "duration_s": 2}]}], lookup(IMG))
    with pytest.raises(tl.TimelineError, match="overlay graphic goes on the 'graphics' track"):
        tl.normalise_tracks([{"type": "visual", "clips": [{"kind": "graphic", "graphic": LOWER, "duration_s": 2}]}], lookup(IMG))
    with pytest.raises(tl.TimelineError, match="graphics clip 0"):
        tl.normalise_tracks([base, {"type": "graphics", "clips": [{"start_s": 1, "end_s": 1.1, "graphic": LOWER}]}], lookup(IMG))
    with pytest.raises(tl.TimelineError, match="poster"):
        tl.normalise_tracks([{"type": "visual", "clips": [{"kind": "graphic", "graphic": TITLE, "duration_s": 2, "asset_id": "nope"}]}], lookup(IMG))
    with pytest.raises(tl.TimelineError):
        tl.normalise_tracks([base, {"type": "graphics", "clips": []}, {"type": "graphics", "clips": []}], lookup(IMG))


def test_a_graphic_clip_can_be_edited_and_shows_in_the_compact_view():
    tracks = tl.normalise_tracks([{"type": "visual", "clips": [{"kind": "graphic", "graphic": TITLE, "duration_s": 2.0}]},
                                  {"type": "graphics", "clips": [{"start_s": 0, "end_s": 1, "graphic": LOWER}]}], lookup(IMG))
    edited = tl.apply_clip_updates(tracks, [{"index": 0, "graphic": {**TITLE, "data": {"title": "Otro"}}}])
    assert tl.normalise_tracks(edited, lookup(IMG))[0]["clips"][0]["graphic"]["data"]["title"] == "Otro"
    view = tl.compact_view({"id": "t", "project_id": "p", "name": "n", "aspect": "9:16", "fps": 30, "width": 1080, "height": 1920,
                            "tracks": tracks})
    assert view["clips"][0]["graphic"] == "title_card" and view["clips"][0]["asset_id"] is None and view["overlays"] == 1


def test_the_mux_command_puts_the_overlay_after_the_finishing_pass_and_before_the_captions():
    cmd = video.build_mux_cmd("ffmpeg", Path("v.mp4"), Path("s.wav"), "lyrics.ass", Path("o.mp4"), "fast", 20, "128k",
                              finishing_vf="eq=contrast=1.1", overlay_path=Path("g.mov"), max_duration_s=12.0)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.index("eq=contrast") < graph.index("overlay=") < graph.index("ass=lyrics.ass")
    assert "eof_action=pass" in graph and cmd[cmd.index("-map") + 1] == "[vout]"
    assert "2:a" in cmd and cmd[cmd.index("-t") + 1] == "12.000"
    plain = video.build_mux_cmd("ffmpeg", Path("v.mp4"), None, None, Path("o.mp4"), "fast", 20, "128k", overlay_path=Path("g.mov"))
    assert "[0:v][1:v]overlay" in plain[plain.index("-filter_complex") + 1]
    no_overlay = video.build_mux_cmd("ffmpeg", Path("v.mp4"), Path("s.wav"), "lyrics.ass", Path("o.mp4"), "fast", 20, "128k")
    assert "-filter_complex" not in no_overlay and "1:a" in no_overlay


def test_captions_under_a_kinetic_graphic_are_dropped_but_a_lower_third_keeps_them():
    timeline = {"tracks": [
        {"type": "visual", "clips": [{"kind": "image", "asset_id": "a", "duration_s": 2.0},
                                     {"kind": "graphic", "graphic": KINETIC, "duration_s": 2.0}]},
        {"type": "graphics", "clips": [{"start_s": 5.0, "end_s": 6.0, "graphic": LOWER}]},
    ]}
    windows = video.graphic_caption_windows(timeline)
    assert windows == [(2.0, 4.0)]
    lines = [{"text": "a", "start_s": 0.0, "end_s": 1.9}, {"text": "b", "start_s": 2.1, "end_s": 3.5},
             {"text": "c", "start_s": 3.9, "end_s": 5.0}, {"text": "d", "start_s": 5.2, "end_s": 5.8}]
    assert [c["text"] for c in video.drop_covered_captions(lines, windows)] == ["a", "d"]


def _luma(path: Path, t: float, size: str = "64:36") -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", "-vf", f"scale={size},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).astype(np.int16)


def _frames(path: Path) -> int:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-vf", "scale=16:16", "-"],
                         capture_output=True, check=True).stdout
    return len(raw) // 256


@needs_ffmpeg
@pytest.mark.parametrize("transition", ["cut", "crossfade"])
def test_a_real_render_draws_the_graphic_clip_and_composites_the_overlay(tmp_path, transition):
    Image.new("RGB", (320, 180), (200, 60, 60)).save(tmp_path / "a.png")
    fade = {"type": "crossfade", "duration_s": 0.25} if transition == "crossfade" else {"type": "cut", "duration_s": 0.0}
    base = {
        "width": 320, "height": 180, "fps": 24, "audio_asset_id": None,
        "tracks": [{"type": "visual", "clips": [
            {"asset_id": "a", "kind": "image", "duration_s": 1.5, "trim_start_s": 0.0, "transition_in": {"type": "cut"}},
            {"kind": "graphic", "graphic": {**TITLE, "look": {"palette": ["#101820", "#f2f2f2", "#00c2ff"]}}, "duration_s": 2.0,
             "graphic_offset_s": 0.0, "transition_in": fade},
        ]}],
    }
    plain = tmp_path / "plain.mp4"
    result = video.render_timeline(base, lambda i: tmp_path / "a.png", tmp_path / "w1", plain, quality="preview")
    assert plain.is_file() and result["duration_s"] == pytest.approx(3.5)
    assert _frames(plain) in range(83, 86)
    # the picture clip is red, the graphic clip dark
    red, dark = _luma(plain, 0.5), _luma(plain, 3.0)
    assert red.mean() > dark.mean() + 30

    with_overlay = {**base, "tracks": base["tracks"] + [{"type": "graphics", "clips": [{"start_s": 0.5, "end_s": 1.4, "graphic": LOWER}]}]}
    out = tmp_path / "overlay.mp4"
    video.render_timeline(with_overlay, lambda i: tmp_path / "a.png", tmp_path / "w2", out, quality="preview")
    assert abs(_frames(out) - _frames(plain)) <= 1
    differs = np.abs(_luma(out, 1.0, "160:90") - _luma(plain, 1.0, "160:90")).max()
    quiet = np.abs(_luma(out, 2.4, "160:90") - _luma(plain, 2.4, "160:90")).max()
    assert differs > 40, "the lower third is not on the frame"
    assert quiet <= 12, "the overlay lingers after its end"


@needs_ffmpeg
def test_a_real_render_swaps_captions_for_kinetic_lyrics(tmp_path):
    Image.new("RGB", (320, 180), (30, 30, 30)).save(tmp_path / "a.png")
    timeline = {
        "width": 320, "height": 180, "fps": 24, "audio_asset_id": None,
        "tracks": [{"type": "visual", "clips": [{"asset_id": "a", "kind": "image", "duration_s": 2.0, "trim_start_s": 0.0}]},
                   {"type": "lyrics", "clips": [{"text": "hola mundo", "start_s": 0.2, "end_s": 1.8, "karaoke": False}]}],
    }
    with_captions = tmp_path / "c.mp4"
    video.render_timeline(timeline, lambda i: tmp_path / "a.png", tmp_path / "w1", with_captions, quality="preview")
    timeline["tracks"].append({"type": "graphics", "clips": [{"start_s": 0.0, "end_s": 2.0, "graphic": KINETIC}]})
    kinetic = tmp_path / "k.mp4"
    video.render_timeline(timeline, lambda i: tmp_path / "a.png", tmp_path / "w2", kinetic, quality="preview")
    assert (tmp_path / "w1" / "lyrics.ass").is_file()
    assert not (tmp_path / "w2" / "lyrics.ass").exists()
    assert np.abs(_luma(kinetic, 1.0, "160:90") - _luma(with_captions, 1.0, "160:90")).max() > 40
