import shutil
from pathlib import Path

import pytest

from prosperos_hoard import video


def test_build_image_clip_cmd_snapshot():
    cmd = video.build_image_clip_cmd(
        "ffmpeg", Path("/data/img.png"), Path("/tmp/clip_000.mp4"), 1080, 1920, 30, 2.0,
        ken_burns={"zoom_start": 1.0, "zoom_end": 1.1, "pan": "left"},
    )
    assert cmd[0] == "ffmpeg"
    assert "-loop" in cmd and "1" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "zoompan" in vf
    assert "crop=2160:3840" in vf
    assert str(Path("/tmp/clip_000.mp4")) in cmd


def test_build_video_clip_cmd_snapshot():
    cmd = video.build_video_clip_cmd("ffmpeg", Path("/data/vid.mp4"), Path("/tmp/clip_001.mp4"), 720, 1280, 24, 3.0, 1.5)
    assert "-ss" in cmd
    assert cmd[cmd.index("-ss") + 1] == "1.500"
    assert "-t" in cmd
    assert cmd[cmd.index("-t") + 1] == "3.000"


def test_build_concat_cmd_snapshot(tmp_path):
    list_file = tmp_path / "list.txt"
    out = tmp_path / "out.mp4"
    cmd = video.build_concat_cmd("ffmpeg", list_file, out)
    assert cmd == ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)]


def test_build_xfade_cmd_offsets():
    clips = [Path("c0.mp4"), Path("c1.mp4"), Path("c2.mp4")]
    durations = [2.0, 2.0, 2.0]
    transitions = [
        {"type": "cut", "duration_s": 0.0},
        {"type": "crossfade", "duration_s": 0.3},
        {"type": "flash_white", "duration_s": 0.2},
    ]
    cmd = video.build_xfade_cmd("ffmpeg", clips, durations, transitions, Path("out.mp4"))
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    assert "xfade=transition=fade:duration=0.300:offset=1.700" in filter_complex
    assert "xfade=transition=fadewhite:duration=0.200:offset=3.500" in filter_complex


def test_build_ass_karaoke_timing():
    clips = [
        {"text": "hello world", "start_s": 1.0, "end_s": 3.0, "karaoke": True},
        {"text": "plain line", "start_s": 3.5, "end_s": 5.0, "karaoke": False},
    ]
    ass_text = video.build_ass(1080, 1920, clips)
    assert "[V4+ Styles]" in ass_text
    assert r"{\k100}hello" in ass_text  # 2s / 2 words = 100 centiseconds each
    assert "plain line" in ass_text
    assert "0:00:01.00,0:00:03.00" in ass_text


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_render_end_to_end(tmp_path):
    from PIL import Image
    import wave
    import numpy as np

    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    for i in range(2):
        Image.new("RGB", (400, 300), (40 * i, 80, 120)).save(assets_dir / f"img{i}.png")
    sr = 44100
    sig = (0.1 * np.sin(2 * np.pi * 220 * np.linspace(0, 3, sr * 3))).astype(np.float32)
    with wave.open(str(assets_dir / "song.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())

    timeline = {
        "width": 480, "height": 854, "fps": 20, "audio_asset_id": "song",
        "tracks": [{"type": "visual", "clips": [
            {"asset_id": "img0", "kind": "image", "start_s": 0.0, "duration_s": 1.5, "trim_start_s": 0.0,
             "ken_burns": {"zoom_start": 1.0, "zoom_end": 1.1, "pan": "none"},
             "transition_in": {"type": "cut", "duration_s": 0.0}},
            {"asset_id": "img1", "kind": "image", "start_s": 1.5, "duration_s": 1.5, "trim_start_s": 0.0,
             "ken_burns": {"zoom_start": 1.0, "zoom_end": 1.1, "pan": "none"},
             "transition_in": {"type": "cut", "duration_s": 0.0}},
        ]}],
    }

    def asset_path_for(asset_id):
        ext = ".png" if asset_id.startswith("img") else ".wav"
        return assets_dir / f"{asset_id}{ext}"

    out = tmp_path / "out.mp4"
    progress_calls = []
    result = video.render_timeline(timeline, asset_path_for, tmp_path / "work", out, quality="preview",
                                    progress=lambda f, m=None: progress_calls.append(f))
    assert out.is_file()
    assert result["duration_s"] == pytest.approx(3.0)
    assert progress_calls[-1] == 1.0
