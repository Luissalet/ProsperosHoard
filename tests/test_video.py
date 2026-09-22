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
    assert cmd[-6:] == ["concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)][-6:]
    # entries are relative names; an apostrophe is escaped the concat-demuxer way
    text = video.concat_list_text([Path("/x/Prospero's Hoard/clip_000.mp4"), Path("/y/it's.mp4")])
    assert text == "file 'clip_000.mp4'\nfile 'it'\\''s.mp4'\n"


def test_build_xfade_cmd_offsets():
    clips = [Path("c0.mp4"), Path("c1.mp4"), Path("c2.mp4")]
    durations = [2.0, 2.0, 2.0]
    transitions = [
        {"type": "cut", "duration_s": 0.0},
        {"type": "crossfade", "duration_s": 0.3},
        {"type": "flash_white", "duration_s": 0.2},
    ]
    cmd = video.build_xfade_cmd("ffmpeg", clips, durations, transitions, Path("out.mp4"), fps=25)
    filter_complex = cmd[cmd.index("-filter_complex") + 1]
    # each transition starts on the incoming clip's nominal start (the beat),
    # so cuts do not drift earlier as transitions accumulate
    assert "xfade=transition=fade:duration=0.300:offset=2.000" in filter_complex
    assert "xfade=transition=fadewhite:duration=0.200:offset=4.000" in filter_complex
    assert video.transition_duration({"type": "cut"}, 25) == pytest.approx(0.04)


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


def test_ass_escaping_makes_lyrics_inert():
    hostile = "{\\pos(1,1)\\c&H0000FF&}hi \\N x\nDialogue: 0,0:00:00.00,0:09:00.00,Lyrics,,0,0,0,,INJECTED"
    ass_text = video.build_ass(540, 960, [{"text": hostile, "start_s": 0.0, "end_s": 1.0}])
    events = [ln for ln in ass_text.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == 1  # the newline did not create a second event
    body = events[0].split(",,0,0,0,,", 1)[1]
    assert "{" not in body.replace("\\{", "")  # every brace escaped
    assert "\\N x" not in body  # the literal \N is broken with a word joiner
    assert "\\NDialogue" in body  # the real newline became an ASS line break


def test_mux_cmd_uses_bare_subtitle_name():
    cmd = video.build_mux_cmd("ffmpeg", Path("C:/Users/me/Prospero's Hoard/v.mp4"), None, "lyrics.ass", Path("o.mp4"), "ultrafast", 28, "128k")
    assert "ass=lyrics.ass:fontsdir=fonts" in cmd
    with pytest.raises(ValueError):
        video.build_mux_cmd("ffmpeg", Path("v.mp4"), None, "C:/x/it's.ass", Path("o.mp4"), "ultrafast", 28, "128k")


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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_render_with_apostrophe_unicode_paths_lyrics_and_transitions(tmp_path):
    import subprocess
    import wave

    import numpy as np
    from PIL import Image

    root = tmp_path / "Prospero's Hoard ñ" / "data it's"
    root.mkdir(parents=True)
    Image.new("RGB", (400, 700), (60, 30, 90)).save(root / "img 'a'.png")
    Image.new("RGB", (400, 700), (20, 90, 60)).save(root / "img b.png")
    sr = 22050
    sig = (0.1 * np.sin(2 * np.pi * 220 * np.linspace(0, 3, sr * 3))).astype(np.float32)
    with wave.open(str(root / "canción.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    paths = {"a": root / "img 'a'.png", "b": root / "img b.png", "s": root / "canción.wav"}
    timeline = {"width": 540, "height": 960, "fps": 20, "audio_asset_id": "s", "tracks": [
        {"type": "visual", "clips": [
            {"asset_id": "a", "kind": "image", "duration_s": 1.5, "ken_burns": {"zoom_start": 1, "zoom_end": 1.1, "pan": "left"},
             "transition_in": {"type": "cut"}},
            {"asset_id": "b", "kind": "image", "duration_s": 1.5, "transition_in": {"type": "crossfade", "duration_s": 0.3}}]},
        {"type": "lyrics", "clips": [{"text": "{\\b1}it's \"quoted\" ñ", "start_s": 0.2, "end_s": 2.8, "karaoke": True}]}]}
    out = root / "out it's.mp4"
    result = video.render_timeline(timeline, lambda i: paths[i], root / "work 'x'", out, quality="preview")
    assert out.is_file()
    probe = subprocess.run(["ffmpeg", "-i", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = __import__("re").search(r"Duration: 00:00:(\d+\.\d+)", probe.stderr)
    assert m and abs(float(m.group(1)) - 3.0) < 0.15  # transitions do not shorten the video
    assert result["width"] == 540


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_animated_webp_is_converted_to_mp4(tmp_path):
    from PIL import Image

    frames = [Image.new("RGB", (65, 49), (i * 20, 0, 0)) for i in range(6)]
    src = tmp_path / "anim.webp"
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=125, loop=0)
    dest = tmp_path / "anim.mp4"
    n = video.animated_webp_to_mp4(src, dest, 8, tmp_path / "work")
    assert n == 6 and dest.is_file() and dest.stat().st_size > 0
