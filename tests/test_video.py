from pathlib import Path

import pytest

from prosperos_hoard import video

# the app's own ffmpeg (a system one, else the imageio-ffmpeg 4.2 wheel)
FFMPEG = video.ffmpeg_path()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


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


@needs_ffmpeg
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


@needs_ffmpeg
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
    probe = subprocess.run([FFMPEG, "-i", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = __import__("re").search(r"Duration: 00:00:(\d+\.\d+)", probe.stderr)
    assert m and abs(float(m.group(1)) - 3.0) < 0.15  # transitions do not shorten the video
    assert result["width"] == 540


@needs_ffmpeg
def test_animated_webp_is_converted_to_mp4(tmp_path):
    from PIL import Image

    frames = [Image.new("RGB", (65, 49), (i * 20, 0, 0)) for i in range(6)]
    src = tmp_path / "anim.webp"
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=125, loop=0)
    dest = tmp_path / "anim.mp4"
    n = video.animated_webp_to_mp4(src, dest, 8, tmp_path / "work")
    assert n == 6 and dest.is_file() and dest.stat().st_size > 0


@needs_ffmpeg
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


@needs_ffmpeg
def test_short_video_clip_is_padded_to_its_slot(tmp_path):
    import subprocess

    src = tmp_path / "anim.mp4"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=8:duration=1",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    timeline = {"width": 360, "height": 640, "fps": 20, "audio_asset_id": None, "tracks": [
        {"type": "visual", "clips": [{"asset_id": "v", "kind": "video", "duration_s": 2.5, "trim_start_s": 0.0,
                                      "transition_in": {"type": "cut"}}]}]}
    out = tmp_path / "o.mp4"
    video.render_timeline(timeline, lambda _i: src, tmp_path / "w", out, quality="preview")
    probe = subprocess.run([FFMPEG, "-i", str(out)], capture_output=True, text=True)
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


def _gray_frames(path, width, height):
    import subprocess

    import numpy as np

    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width)


def _square_width(frame):
    """Width in pixels of the white square in a frame (the zoom indicator)."""
    import numpy as np

    cols = np.where((frame > 128).sum(axis=0) > 0)[0]
    return int(cols.max() - cols.min() + 1) if cols.size else 0


@needs_ffmpeg
@pytest.mark.parametrize("zs,ze", [(1.5, 1.0), (1.2, 1.5)])
def test_ken_burns_zoom_runs_from_start_to_end(tmp_path, zs, ze):
    """The zoom is exactly zoom_start on the first frame and zoom_end on the
    last, in both directions (a zoom-out used to snap to 1.0 after one
    frame; a zoom-in started at 1.0 whatever zoom_start said)."""
    import subprocess

    from PIL import Image, ImageDraw

    w, h = 160, 90
    src = tmp_path / "sq.png"
    img = Image.new("RGB", (w, h), (0, 0, 0))
    ImageDraw.Draw(img).rectangle([w // 2 - 20, h // 2 - 10, w // 2 + 19, h // 2 + 9], fill=(255, 255, 255))
    img.save(src)
    out = tmp_path / "kb.mp4"
    subprocess.run(video.build_image_clip_cmd(FFMPEG, src, out, w, h, 10, 1.0,
                                              {"zoom_start": zs, "zoom_end": ze, "pan": "left"}), check=True)
    widths = [_square_width(f) / 40 for f in _gray_frames(out, w, h)]
    assert len(widths) == 10
    assert widths[0] == pytest.approx(zs, abs=0.06)
    assert widths[-1] == pytest.approx(ze, abs=0.06)
    step = 1 if ze > zs else -1
    assert all(step * (b - a) >= -0.03 for a, b in zip(widths, widths[1:]))  # monotonic, no jump


def _solid_stills(tmp_path, colours):
    from PIL import Image

    paths = {}
    for k, rgb in enumerate(colours):
        paths[f"i{k}"] = tmp_path / f"still{k}.png"
        Image.new("RGB", (64, 64), rgb).save(paths[f"i{k}"])
    return paths


def _still_clip(asset_id, duration_s, transition=None):
    return {"asset_id": asset_id, "kind": "image", "duration_s": duration_s, "trim_start_s": 0.0,
            "ken_burns": {"zoom_start": 1.0, "zoom_end": 1.0, "pan": "none"},
            "transition_in": transition or {"type": "cut", "duration_s": 0.0}}


def _rgb_frames(path, width, height):
    import subprocess

    import numpy as np

    raw = subprocess.run([FFMPEG, "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3).astype(int)


def _no_blending_join(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("a blending join was used where the concat demuxer should do")

    monkeypatch.setattr(video, "build_xfade_cmd", boom)
    monkeypatch.setattr(video, "build_overlay_fade_cmd", boom)


@needs_ffmpeg
def test_dips_and_flashes_are_clip_fades_joined_by_concat(tmp_path, monkeypatch):
    """Cuts, dips and flashes stay on the concat (-c copy) path: the dip is
    a fade out at the end of the outgoing clip and a fade in at the start
    of the incoming one, the darkest/brightest frames on the cut itself."""
    _no_blending_join(monkeypatch)
    paths = _solid_stills(tmp_path, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])
    timeline = {"width": 64, "height": 64, "fps": 10, "tracks": [{"type": "visual", "clips": [
        _still_clip("i0", 1.0),
        _still_clip("i1", 1.0, {"type": "dip_black", "duration_s": 0.4}),
        _still_clip("i2", 1.0, {"type": "flash_white", "duration_s": 0.4}),
    ]}], "finishing": {"glitch_on_downbeats": True}}
    out = tmp_path / "out.mp4"
    glitches = []
    real_finishing_vf = video.build_finishing_vf

    def spy(finishing, w, h, glitch_points=None):
        glitches.extend(glitch_points or [])
        return real_finishing_vf(finishing, w, h, glitch_points)

    monkeypatch.setattr(video, "build_finishing_vf", spy)
    result = video.render_timeline(timeline, lambda a: paths[a], tmp_path / "work", out, quality="final")
    frames = _rgb_frames(out, 64, 64)
    assert len(frames) == 30 and result["duration_s"] == pytest.approx(3.0)
    luma = frames.mean(axis=(1, 2, 3))
    steady = luma[5]
    # a dip to black centred on the cut at frame 10: darkest on the cut itself
    assert luma[9] < steady * 0.7 and luma[10] < 10 and luma[11] < steady * 0.7 and luma[8] > steady * 0.9
    assert frames[5, 32, 32, 0] > 150 and frames[15, 32, 32, 1] > 150  # clips keep their colour mid-way
    assert luma[19] > steady * 1.5 and luma[20] > 245 and luma[21] > steady * 1.5  # the flash, on the cut at 20
    assert frames[25, 32, 32, 2] > 150
    assert glitches == [(2.0, 1.0)]  # the flash still gets its glitch


@needs_ffmpeg
def test_crossfade_with_a_dip_keeps_length_and_dips_on_the_cut(tmp_path):
    """With a crossfade the clips go through the blending join; a dip in the
    same timeline is still the clip fades (a one-frame blend at its cut)."""
    paths = _solid_stills(tmp_path, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])
    timeline = {"width": 64, "height": 64, "fps": 10, "tracks": [{"type": "visual", "clips": [
        _still_clip("i0", 1.0),
        _still_clip("i1", 1.0, {"type": "crossfade", "duration_s": 0.3}),
        _still_clip("i2", 1.0, {"type": "dip_black", "duration_s": 0.4}),
    ]}]}
    out = tmp_path / "out.mp4"
    video.render_timeline(timeline, lambda a: paths[a], tmp_path / "work", out, quality="final")
    frames = _rgb_frames(out, 64, 64)
    luma = frames.mean(axis=(1, 2, 3))
    assert len(frames) == 30
    assert frames[5, 32, 32, 0] > 150 and frames[15, 32, 32, 1] > 150 and frames[25, 32, 32, 2] > 150
    assert luma[20] < 15 and luma[17] > luma[5] * 0.9  # black on the cut, not before


@needs_ffmpeg
def test_first_clip_transition_does_not_force_a_blending_join(tmp_path, monkeypatch):
    """transition_in of clip 0 is never rendered, so it must not push an
    all-cut timeline onto the slow blending join."""
    _no_blending_join(monkeypatch)
    paths = _solid_stills(tmp_path, [(200, 40, 40), (40, 200, 40)])
    timeline = {"width": 64, "height": 64, "fps": 10, "tracks": [{"type": "visual", "clips": [
        _still_clip("i0", 1.0, {"type": "crossfade", "duration_s": 0.3}), _still_clip("i1", 1.0)]}]}
    video.render_timeline(timeline, lambda a: paths[a], tmp_path / "work", tmp_path / "o.mp4")
    assert (tmp_path / "o.mp4").is_file()


def _write_tone(path, seconds, sr=22050):
    import wave

    import numpy as np

    sig = (0.1 * np.sin(2 * np.pi * 220 * np.arange(int(sr * seconds)) / sr) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(sig.tobytes())


@needs_ffmpeg
def test_edit_longer_than_the_song_keeps_its_last_shots(tmp_path):
    """-shortest cut the video at the song's end while duration_s reported
    the edit's length; the song is now padded with silence instead."""
    from prosperos_hoard import audio as audio_mod

    paths = _solid_stills(tmp_path, [(200, 40, 40), (40, 200, 40)])
    paths["song"] = tmp_path / "song.wav"
    _write_tone(paths["song"], 1.2)
    timeline = {"width": 64, "height": 64, "fps": 10, "audio_asset_id": "song", "tracks": [{"type": "visual", "clips": [
        _still_clip("i0", 1.0), _still_clip("i1", 1.5)]}]}
    out = tmp_path / "o.mp4"
    result = video.render_timeline(timeline, lambda a: paths[a], tmp_path / "work", out, quality="final")
    assert result["duration_s"] == pytest.approx(2.5)
    assert audio_mod.probe_duration_s(out) == pytest.approx(2.5, abs=0.1)
    assert len(_rgb_frames(out, 64, 64)) == 25


def test_mux_cmd_pads_the_song_and_seeks_it_for_a_range():
    cmd = video.build_mux_cmd("ffmpeg", Path("v.mp4"), Path("song.wav"), None, Path("o.mp4"), "ultrafast", 28, "128k",
                              duration_s=12.5, audio_start_s=30.0)
    assert "-shortest" not in cmd
    assert cmd[cmd.index("-af") + 1] == "apad" and cmd[cmd.index("-t") + 1] == "12.500"
    ss = cmd.index("-ss")
    assert cmd[ss + 1] == "30.000" and cmd[ss + 2:ss + 4] == ["-i", "song.wav"]  # an input option on the song only
    assert cmd.index("-i") < ss  # the video input is not seeked


@needs_ffmpeg
def test_range_render_trims_clips_shifts_lyrics_and_seeks_the_song(tmp_path):
    paths = _solid_stills(tmp_path, [(200, 40, 40), (40, 200, 40), (40, 40, 200)])
    paths["song"] = tmp_path / "song.wav"
    _write_tone(paths["song"], 3.0)
    timeline = {"width": 64, "height": 64, "fps": 10, "audio_asset_id": "song", "tracks": [
        {"type": "visual", "clips": [_still_clip("i0", 1.0), _still_clip("i1", 1.0), _still_clip("i2", 1.0)]},
        {"type": "lyrics", "clips": [{"text": "before", "start_s": 0.0, "end_s": 0.4},
                                     {"text": "chorus", "start_s": 1.2, "end_s": 2.8}]}]}
    out = tmp_path / "o.mp4"
    work = tmp_path / "work"
    result = video.render_timeline(timeline, lambda a: paths[a], work, out, quality="final", time_range=[0.5, 2.3])
    assert result["duration_s"] == pytest.approx(1.8) and result["range"] == [0.5, 2.3]
    frames = _rgb_frames(out, 64, 64)
    assert len(frames) == 18
    colour = [int(f[32, 32].argmax()) for f in frames]
    assert colour == [0] * 5 + [1] * 10 + [2] * 3
    ass = (work / "lyrics.ass").read_text(encoding="utf-8")
    assert "before" not in ass and "Dialogue: 0,0:00:00.70,0:00:01.80,Lyrics,,0,0,0,,chorus" in ass


def test_range_is_validated():
    with pytest.raises(video.RenderError):
        video.range_window([2.0, 1.0], 10.0)
    with pytest.raises(video.RenderError):
        video.range_window([float("nan"), 1.0], 10.0)
    with pytest.raises(video.RenderError):
        video.range_window([9.95, 20.0], 10.0)  # less than MIN_RANGE_S left
    with pytest.raises(video.RenderError):
        video.range_window("chorus", 10.0)
    assert video.range_window([0, 99], 10.0) is None  # the whole edit
    assert video.range_window([2, 99], 10.0) == (2.0, 10.0)


def test_zoompan_progress_window_continues_the_move():
    z, _x, _y = video._zoompan_expr(1.0, 1.2, "none", 11, progress_range=(0.5, 1.0))
    assert z == "1.000000+(0.200000)*(0.500000+0.500000*on/10)"


@needs_ffmpeg
def test_progress_callback_error_kills_ffmpeg(tmp_path, monkeypatch):
    """on_progress raising (a job cancelled through its progress callback)
    used to leave ffmpeg running and writing the output."""
    procs = []
    real_popen = video.procutil.popen

    def spy(*a, **k):
        procs.append(real_popen(*a, **k))
        return procs[-1]

    monkeypatch.setattr(video.procutil, "popen", spy)
    out = tmp_path / "long.mp4"
    cmd = [FFMPEG, "-y", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=600",
           "-c:v", "libx264", "-preset", "ultrafast", "-progress", "pipe:1", "-nostats", str(out)]

    def explode(_frac):
        raise RuntimeError("job cancelled")

    with pytest.raises(RuntimeError, match="job cancelled"):
        video.run_ffmpeg_with_progress(cmd, 600.0, explode)
    assert procs and procs[0].poll() is not None


@needs_ffmpeg
def test_failed_render_leaves_no_partial_output(tmp_path):
    paths = _solid_stills(tmp_path, [(200, 40, 40)])
    timeline = {"width": 64, "height": 64, "fps": 10, "tracks": [{"type": "visual", "clips": [_still_clip("i0", 2.0)]}]}
    out = tmp_path / "o.mp4"

    def progress(_frac, msg=None):
        if msg == "encoding with audio and lyrics":
            out.write_bytes(b"partial")  # whatever ffmpeg had written so far
            raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError):
        video.render_timeline(timeline, lambda a: paths[a], tmp_path / "work", out, progress=progress)
    assert not out.exists()


def test_upright_still_bakes_the_exif_orientation(tmp_path):
    from PIL import Image

    img = Image.new("RGB", (80, 40), (200, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6
    rotated = tmp_path / "phone.jpg"
    img.save(rotated, exif=exif)
    plain = tmp_path / "plain.png"
    img.save(plain)
    out = video.upright_still(rotated, tmp_path, "still_000")
    assert out != rotated and Image.open(out).size == (40, 80)
    assert video.upright_still(plain, tmp_path, "still_001") == plain
    assert video.upright_still(tmp_path / "missing.png", tmp_path, "x") == tmp_path / "missing.png"


def test_ass_time_carries_instead_of_printing_sixty_seconds():
    assert video._ass_time(59.996) == "0:01:00.00"
    assert video._ass_time(3599.999) == "1:00:00.00"
    assert video._ass_time(61.25) == "0:01:01.25"
    assert video._ass_time(-2) == "0:00:00.00"


def test_zoompan_expr_is_a_closed_form_and_clamps_the_pan():
    z, x, y = video._zoompan_expr(1.2, 1.0, "right", 31)
    assert z == "1.200000+(-0.200000)*(on/30)"
    assert x.startswith("max(0,min(iw-iw/zoom,") and "(on/30)*48" in x
    assert y == "max(0,min(ih-ih/zoom,ih/2-(ih/zoom/2)))"


def test_render_range_is_checked_before_the_job_is_queued():
    from prosperos_hoard import engine

    tl = {"tracks": [{"type": "visual", "clips": [{"duration_s": 2.0}, {"duration_s": 3.0}]}]}
    assert engine.render_range(tl, None) is None
    assert engine.render_range(tl, [0, 5]) is None  # the whole edit
    assert engine.render_range(tl, [1.25, 9]) == [1.25, 5.0]
    for bad in ([3, 1], [6, 8], [1], ["a", 2]):
        with pytest.raises(engine.EngineError) as exc:
            engine.render_range(tl, bad)
        assert exc.value.code == "bad_range"
