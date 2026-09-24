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
    assert cmd[cmd.index("-t") + 1] == "3.021"  # half a frame of slack...
    assert cmd[cmd.index("-frames:v") + 1] == "72"  # ...and an exact frame count (3 s at 24 fps)


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


def test_build_overlay_fade_cmd_matches_the_xfade_offsets():
    """The join for an ffmpeg without xfade (imageio-ffmpeg ships 4.2):
    same offsets, each incoming clip padded to its start, the chain faded
    out over it."""
    clips = [Path("c0.mp4"), Path("c1.mp4"), Path("c2.mp4")]
    transitions = [None, {"type": "crossfade", "duration_s": 0.3}, {"type": "dip_black", "duration_s": 0.2}]
    cmd = video.build_overlay_fade_cmd("ffmpeg", clips, [2.0, 2.0, 2.0], transitions, Path("out.mp4"), fps=25)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "xfade" not in fc
    assert "tpad=start_duration=2.000[p1]" in fc
    assert "fade=t=out:st=2.000:d=0.300:alpha=1[a1]" in fc
    assert "fade=t=in:st=0:d=0.200:color=black,tpad=start_duration=4.000[p2]" in fc
    assert cmd[cmd.index("-map") + 1] == "[v2]"


@pytest.mark.skipif(not video.ffmpeg_path(), reason="ffmpeg not available")
def test_ffmpeg_has_filter_reads_the_listing():
    ff = video.ffmpeg_path()
    assert video.ffmpeg_has_filter(ff, "scale")
    assert not video.ffmpeg_has_filter(ff, "no_such_filter_here")


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


def test_validate_finishing_rejects_bad_values():
    assert video.validate_finishing(None) == {}
    assert video.validate_finishing({}) == {}
    with pytest.raises(video.RenderError):
        video.validate_finishing({"color_grade": "sepia"})
    with pytest.raises(video.RenderError):
        video.validate_finishing({"grain": 4})
    with pytest.raises(video.RenderError):
        video.validate_finishing({"lyric_style": "spooky"})
    with pytest.raises(video.RenderError):
        video.validate_finishing({"nonsense": True})
    clean = video.validate_finishing({"color_grade": "teal_orange", "grain": 0.5, "vignette": True,
                                       "letterbox": True, "glitch_on_downbeats": True, "lyric_style": "horror"})
    assert clean == {"color_grade": "teal_orange", "grain": 0.5, "vignette": True, "letterbox": True,
                      "glitch_on_downbeats": True, "lyric_style": "horror"}


def test_finishing_vf_builds_each_effect():
    assert video.build_finishing_vf(None, 1080, 1920) == ""
    assert video.build_finishing_vf({}, 1080, 1920) == ""

    grade_vf = video.build_finishing_vf({"color_grade": "sodium_night"}, 1080, 1920)
    assert grade_vf == video.COLOR_GRADE_PRESETS["sodium_night"]

    bleach_vf = video.build_finishing_vf({"color_grade": "bleach_bypass"}, 1080, 1920)
    assert "curves=preset=strong_contrast" in bleach_vf

    grain_vf = video.build_finishing_vf({"grain": 0.5}, 1080, 1920)
    assert "noise=c0s=12.0:c0f=t+u" in grain_vf  # luma only: chroma grain bloats the encode

    vignette_vf = video.build_finishing_vf({"vignette": True}, 1080, 1920)
    assert vignette_vf == "vignette=PI/5"

    letterbox_vf = video.build_finishing_vf({"letterbox": True}, 1080, 1920)
    parts = letterbox_vf.split(",")
    assert len(parts) == 2 and all(p.startswith("drawbox=") for p in parts)
    assert "y=0:w=1080:h=192" in parts[0]
    assert "y=1728" in parts[1]  # 1920 - 192

    glitch_vf = video.build_finishing_vf({"glitch_on_downbeats": True}, 1080, 1920, glitch_points=[(4.0, 0.15)])
    assert "chromashift=crh=4:cbv=-4:enable='between(t\\,4.000\\,4.150)'" in glitch_vf

    # combined: order is grade, grain, vignette, letterbox, glitch
    combo = video.build_finishing_vf(
        {"color_grade": "teal_orange", "grain": 0.2, "vignette": True, "letterbox": True, "glitch_on_downbeats": True},
        200, 400, glitch_points=[(1.0, 0.1)],
    )
    assert combo.index("eq=") < combo.index("noise=") < combo.index("vignette=") < combo.index("drawbox=") < combo.index("chromashift=")


def test_rgb_only_grade_filters_run_on_packed_rgb():
    """colorbalance/curves must not be left to negotiate planar gbrp: ffmpeg
    8's gbrp round trip blacks out the right edge of a 1080-wide frame."""
    for name, chain in video.COLOR_GRADE_PRESETS.items():
        filters = chain.split(",")
        for i, f in enumerate(filters):
            if f.startswith(("colorbalance=", "curves=")):
                assert filters[i - 1] == "format=rgb24" and filters[i + 1] == "format=yuv420p", name


def test_glitch_windows_leave_the_frame_edges_intact():
    """A real render of the whole finishing chain (grade, grain, vignette,
    many glitch windows) on a 1080-wide frame: the edge columns keep the
    same colour as the rest of the frame."""
    import subprocess

    import numpy as np

    from prosperos_hoard.backend import ffmpeg_path

    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    vf = video.build_finishing_vf({"color_grade": "sodium_night", "grain": 0.3, "vignette": True,
                                   "glitch_on_downbeats": True}, 1080, 1920,
                                  glitch_points=[(5.0 + i, 0.15) for i in range(30)])
    raw = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=gray:s=1080x1920:d=0.2",
                          "-vf", f"format=yuv420p,{vf}", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(1920, 1080, 3).astype(int)
    row = frame[960]  # the vignette darkens the edges symmetrically; compare the two sides
    left, right = row[:8].mean(axis=0), row[-8:].mean(axis=0)
    assert abs(left - right).max() < 12, (left, right)
    assert row[-8:, 1].mean() > 20  # not a black (or red-only) stripe


def test_build_ass_horror_style_uppercases_and_jitters():
    clips = [{"text": "walk home alone", "start_s": 1.0, "end_s": 2.5}]
    default_ass = video.build_ass(1080, 1920, clips, style="default")
    horror_ass = video.build_ass(1080, 1920, clips, style="horror")
    assert "WALK HOME ALONE" in horror_ass
    assert "walk home alone" not in horror_ass
    assert "Bebas Neue" in horror_ass
    assert "Bebas Neue" not in default_ass
    assert r"\frz" in horror_ass and r"\fax" in horror_ass
    # deterministic: same input renders byte-identical
    assert horror_ass == video.build_ass(1080, 1920, clips, style="horror")
    with pytest.raises(video.RenderError):
        video.build_ass(1080, 1920, clips, style="creepy")


def test_mux_cmd_uses_bare_subtitle_name():
    cmd = video.build_mux_cmd("ffmpeg", Path("C:/Users/me/Prospero's Hoard/v.mp4"), None, "lyrics.ass", Path("o.mp4"), "ultrafast", 28, "128k")
    assert "ass=lyrics.ass:fontsdir=fonts" in cmd
    with pytest.raises(ValueError):
        video.build_mux_cmd("ffmpeg", Path("v.mp4"), None, "C:/x/it's.ass", Path("o.mp4"), "ultrafast", 28, "128k")


def test_mux_cmd_finishing_runs_before_captions():
    cmd = video.build_mux_cmd("ffmpeg", Path("v.mp4"), None, "lyrics.ass", Path("o.mp4"), "ultrafast", 28, "128k",
                               finishing_vf="vignette=PI/5")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "vignette=PI/5,ass=lyrics.ass:fontsdir=fonts"
    # no lyrics, still applies the grade
    cmd2 = video.build_mux_cmd("ffmpeg", Path("v.mp4"), None, None, Path("o.mp4"), "ultrafast", 28, "128k",
                                finishing_vf="vignette=PI/5")
    assert cmd2[cmd2.index("-vf") + 1] == "vignette=PI/5"
    # neither: no -vf at all (unchanged from before this feature)
    cmd3 = video.build_mux_cmd("ffmpeg", Path("v.mp4"), None, None, Path("o.mp4"), "ultrafast", 28, "128k")
    assert "-vf" not in cmd3


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


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_render_with_finishing_applies_grade_grain_vignette_letterbox_and_glitch(tmp_path):
    from PIL import Image

    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    for i in range(3):
        Image.new("RGB", (400, 700), (40 * i, 80, 120)).save(assets_dir / f"img{i}.png")

    timeline = {
        "width": 400, "height": 700, "fps": 15, "audio_asset_id": None,
        "finishing": {"color_grade": "sodium_night", "grain": 0.4, "vignette": True, "letterbox": True,
                      "glitch_on_downbeats": True, "lyric_style": "horror"},
        "tracks": [
            {"type": "visual", "clips": [
                {"asset_id": "img0", "kind": "image", "duration_s": 1.0, "trim_start_s": 0.0,
                 "transition_in": {"type": "cut", "duration_s": 0.0}},
                {"asset_id": "img1", "kind": "image", "duration_s": 1.0, "trim_start_s": 0.0,
                 "transition_in": {"type": "flash_white", "duration_s": 0.15}},
                {"asset_id": "img2", "kind": "image", "duration_s": 1.0, "trim_start_s": 0.0,
                 "transition_in": {"type": "cut", "duration_s": 0.0}},
            ]},
            {"type": "lyrics", "clips": [{"text": "it followed me home", "start_s": 0.1, "end_s": 1.5}]},
        ],
    }

    def asset_path_for(asset_id):
        return assets_dir / f"{asset_id}.png"

    out = tmp_path / "out.mp4"
    result = video.render_timeline(timeline, asset_path_for, tmp_path / "work", out, quality="preview")
    assert out.is_file() and out.stat().st_size > 0
    assert result["duration_s"] == pytest.approx(3.0)


def test_render_timeline_rejects_bad_finishing(tmp_path):
    timeline = {
        "width": 400, "height": 700, "fps": 15, "audio_asset_id": None,
        "finishing": {"color_grade": "sepia-tone-that-does-not-exist"},
        "tracks": [{"type": "visual", "clips": [
            {"asset_id": "img0", "kind": "image", "duration_s": 1.0, "trim_start_s": 0.0,
             "transition_in": {"type": "cut", "duration_s": 0.0}},
        ]}],
    }
    with pytest.raises(video.RenderError):
        video.render_timeline(timeline, lambda _i: Path("/nonexistent.png"), tmp_path / "work", tmp_path / "out.mp4")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_short_video_clip_is_padded_to_its_slot(tmp_path):
    import subprocess

    src = tmp_path / "anim.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=8:duration=1",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    timeline = {"width": 360, "height": 640, "fps": 20, "audio_asset_id": None, "tracks": [
        {"type": "visual", "clips": [{"asset_id": "v", "kind": "video", "duration_s": 2.5, "trim_start_s": 0.0,
                                      "transition_in": {"type": "cut"}}]}]}
    out = tmp_path / "o.mp4"
    video.render_timeline(timeline, lambda _i: src, tmp_path / "w", out, quality="preview")
    probe = subprocess.run(["ffmpeg", "-i", str(out)], capture_output=True, text=True)
    import re

    m = re.search(r"Duration: 00:00:(\d+\.\d+)", probe.stderr)
    assert m and abs(float(m.group(1)) - 2.5) < 0.15


def test_horror_karaoke_lights_words_by_syllable_within_two_bars():
    import re as _re

    clips = [{"text": "la luz que te sigue", "start_s": 10.0, "end_s": 16.0, "karaoke": True}]
    ass = video.build_ass(1080, 1920, clips, style="horror")
    ks = [int(x) for x in _re.findall(r"\\k(\d+)", ass)]
    assert len(ks) == 5
    assert sum(ks) == int(video.KARAOKE_MAX_FILL_S * 100)  # a 6 s caption still lights up within ~2 bars
    assert ks[4] > ks[0]  # "SIGUE" (2 syllables) takes longer than "LA"
    assert r"\fad(90,120)" in ass
    style = next(ln for ln in ass.splitlines() if ln.startswith("Style: Lyrics"))
    # above the short-video apps' own UI, and off the phone's edges
    assert style.endswith(f",2,{int(1080 * 0.08)},{int(1080 * 0.08)},{int(1920 * 0.2)},1")
    landscape = next(ln for ln in video.build_ass(1920, 1080, clips, style="horror").splitlines() if ln.startswith("Style:"))
    assert landscape.split(",")[2] == str(1080 // 13) and landscape.endswith(f",{int(1080 * 0.09)},1")


def test_render_with_transitions_keeps_the_full_length(tmp_path):
    """Many short clips joined with xfade (flashes on the downbeats): the
    chain used to end after a few clips once float offsets drifted past the
    frame-quantised clip lengths. Clip lengths that are not frame multiples
    at 24 fps are the worst case."""
    from PIL import Image as PILImage

    img = tmp_path / "a.png"
    PILImage.new("RGB", (64, 64), (200, 40, 40)).save(img)
    img2 = tmp_path / "b.png"
    PILImage.new("RGB", (64, 64), (40, 40, 200)).save(img2)
    # beat-aligned lengths from a real 140 bpm cut: several round *down* to
    # whole frames at 24 fps, and the shortfall adds up
    durations = [3.413, 3.437, 3.425, 3.425, 1.718, 1.289, 1.706, 1.715, 0.857, 0.861]
    clips = []
    for i, d in enumerate(durations):
        clips.append({"asset_id": "a" if i % 2 == 0 else "b", "kind": "image", "duration_s": d, "trim_start_s": 0.0,
                      "ken_burns": {"zoom_start": 1.0, "zoom_end": 1.0, "pan": "none"},
                      "transition_in": {"type": "flash_white", "duration_s": 0.15} if i == 9 else {"type": "cut"}})
    timeline = {"width": 160, "height": 284, "fps": 24, "tracks": [{"type": "visual", "clips": clips}]}
    out = tmp_path / "out.mp4"
    video.render_timeline(timeline, lambda aid: img if aid == "a" else img2, tmp_path / "work", out, quality="final")
    from prosperos_hoard import audio as audio_mod

    assert abs(audio_mod.probe_duration_s(out) - sum(durations)) < 0.1
