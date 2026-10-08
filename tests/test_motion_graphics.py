"""The code-rendered graphic shots: spec validation, timing, determinism, safe areas, video output."""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from prosperos_hoard import motion_graphics as mg
from prosperos_hoard.backend import ffmpeg_path

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_path(), reason="ffmpeg not installed")

LYRICS = {"lines": [
    {"text": "Ciudad de neón y lluvia", "start_s": 0.4, "end_s": 2.2},
    {"text": "me pierdo en tu reflejo", "start_s": 2.4, "end_s": 3.8},
]}


def kinetic(**extra):
    spec = {"grammar": "kinetic_lyrics", "duration": 4.0, "seed": 7, "data": dict(LYRICS)}
    spec.update(extra)
    return spec


def title(**extra):
    spec = {"grammar": "title_card", "duration": 3.0, "seed": 3, "data": {"title": "Noches de cristal", "subtitle": "un vídeo musical", "kicker": "ESTRENO"}}
    spec.update(extra)
    return spec


# ---------------------------------------------------------------- validation

def test_a_graphic_needs_a_known_grammar_and_its_text():
    with pytest.raises(mg.GraphicError) as exc:
        mg.normalise_graphic({"grammar": "spinning_logo", "data": {}})
    assert exc.value.code == "bad_graphic"
    with pytest.raises(mg.GraphicError):
        mg.normalise_graphic({"grammar": "title_card", "data": {}})
    with pytest.raises(mg.GraphicError):
        mg.normalise_graphic({"grammar": "lower_third", "data": {"name": "Ana"}, "mode": "clip"})
    with pytest.raises(mg.GraphicError, match="unknown graphic field"):
        mg.normalise_graphic({"grammar": "title_card", "data": {"title": "x"}, "colour": "red"})


def test_a_standalone_graphic_needs_a_duration_but_a_shot_span_supplies_it():
    with pytest.raises(mg.GraphicError, match="duration"):
        mg.normalise_graphic(title() | {"duration": None}, need_duration=True)
    spec = mg.normalise_graphic({"grammar": "title_card", "data": {"title": "x"}})
    assert "duration" not in spec


def test_lower_third_defaults_to_overlay_and_the_rest_to_clip():
    assert mg.normalise_graphic({"grammar": "lower_third", "data": {"name": "Ana"}})["mode"] == "overlay"
    assert mg.normalise_graphic(title())["mode"] == "clip"
    assert mg.normalise_graphic(kinetic(mode="overlay"))["mode"] == "overlay"


def test_the_look_is_validated_and_hex_colours_are_normalised():
    look = mg.clean_look({"palette": ["#000", "#ffffff", "#ff2d95"], "motion": {"easing": "out_expo"}, "transition": "glitch"})
    assert look["palette"][0] == "#000" or look["palette"][0].lower() == "#000000"
    with pytest.raises(mg.GraphicError):
        mg.clean_look({"palette": ["red", "blue"]})
    with pytest.raises(mg.GraphicError):
        mg.clean_look({"transition": "teleport"})
    with pytest.raises(mg.GraphicError):
        mg.clean_motion({"easing": "bounce_all_over"})


def test_a_style_card_becomes_a_look_and_a_bad_card_never_breaks_a_render():
    card = {"palette": ["#101010", "#f0f0f0", "#ff0066"], "motion": {"easing": "out_expo"}, "signature_transition": "glitch",
            "typography": {"fonts": {"display": "bebas-neue"}, "case": "upper"}}
    look = mg.look_from_card(card)
    assert look["transition"] == "glitch" and look["motion"]["easing"] == "out_expo"
    assert mg.look_from_card({"palette": ["nonsense"]}) == {}
    assert mg.look_from_card(None) == {}


def test_card_fields_are_cleaned():
    out = mg.clean_card_fields({"technique": " papel ", "quality": 3, "signature_transition": "wipe", "palette": ["#111", "#eee"]})
    assert out["technique"] == "papel" and out["quality"] == 3
    with pytest.raises(mg.GraphicError):
        mg.clean_card_fields({"quality": 9})
    with pytest.raises(mg.GraphicError):
        mg.clean_card_fields({"signature_transition": "nope"})


# -------------------------------------------------------------------- timing

def test_words_are_spread_by_syllable_inside_the_line_window():
    words = mg.spread_words("Ciudad de neón y lluvia", 1.0, 3.0)
    assert [w["text"] for w in words] == ["Ciudad", "de", "neón", "y", "lluvia"]
    assert words[0]["start_s"] == 1.0
    assert all(a["end_s"] <= b["start_s"] + 0.002 for a, b in zip(words, words[1:]))
    assert words[-1]["end_s"] <= 3.0
    # the same fill window the karaoke captions use: a long line does not stretch past 3.6 s
    long = mg.spread_words("uno dos tres cuatro", 0.0, 20.0)
    assert long[-1]["end_s"] <= mg.KARAOKE_MAX_FILL_S + 0.01


def test_given_word_times_win_over_the_spread():
    spec = mg.normalise_graphic(kinetic(data={"lines": [{"text": "a b", "start_s": 1, "end_s": 3,
                                                          "words": [{"text": "a", "start_s": 1.5, "end_s": 1.9}, {"text": "b", "start_s": 2.2, "end_s": 2.6}]}]}))
    lines = mg.timed_lines(spec["data"])
    assert [w["start_s"] for w in lines[0]["words"]] == [1.5, 2.2]


def test_beat_snapping_moves_close_onsets_and_keeps_order():
    data = {"lines": [{"text": "uno dos tres", "start_s": 1.0, "end_s": 4.0}], "snap_to_beats": True, "beats": [1.04, 2.0, 2.01, 3.5]}
    plain = mg.timed_lines({**data, "snap_to_beats": False})[0]["words"]
    snapped = mg.timed_lines(data)[0]["words"]
    assert snapped[0]["start_s"] == 1.04 != plain[0]["start_s"]
    starts = [w["start_s"] for w in snapped]
    assert starts == sorted(starts)


def test_lines_match_the_lrc_timing_in_the_rendered_boxes():
    r = mg.Renderer(mg.normalise_graphic(kinetic()), 1080, 1920, 30)
    boxes = r.boxes()
    first = [b for b in boxes if "Ciudad" in b.text or b.text.startswith("CIUDAD")]
    assert first, [b.text for b in boxes]
    assert min(b.t0 for b in first) >= 0.4 - 0.01  # nothing of the first line shows before its LRC time


# ---------------------------------------------------------------- determinism

@pytest.mark.parametrize("spec", [kinetic(), title(),
                                  {"grammar": "outro_card", "duration": 3, "data": {"title": "Fin", "credits": [{"role": "Dirección", "name": "Ana"}]}},
                                  {"grammar": "lower_third", "duration": 3, "data": {"name": "Ana Gómez", "caption": "voz"}}])
def test_the_same_spec_gives_the_same_pixels(spec):
    clean = mg.normalise_graphic(spec)
    a, b = mg.Renderer(clean, 540, 960, 30), mg.Renderer(clean, 540, 960, 30)
    for t in (0.3, 1.2, 2.5):
        assert mg.frame_hash(a.frame(t)) == mg.frame_hash(b.frame(t))
    assert mg.determinism_check(clean, 540, 960, 30)["deterministic"] is True


def test_the_seed_changes_a_seeded_look_and_a_frame_does_not_depend_on_render_order():
    look = {"look": {"background": {"kind": "paper", "grain": 0.5}, "fx": {"jitter_deg": 1.5, "scanlines": 0.3}}}
    one, two = mg.normalise_graphic(title(seed=1, **look)), mg.normalise_graphic(title(seed=2, **look))
    assert mg.frame_hash(mg.Renderer(one, 360, 640, 30).frame(1.0)) != mg.frame_hash(mg.Renderer(two, 360, 640, 30).frame(1.0))
    r = mg.Renderer(one, 360, 640, 30)
    late_first = mg.frame_hash(r.frame(2.0))
    r.frame(0.5)
    assert mg.frame_hash(r.frame(2.0)) == late_first
    assert mg.frame_hash(mg.Renderer(one, 360, 640, 30).frame(2.0)) == late_first


def test_frames_have_the_requested_size_and_clip_frames_are_opaque_overlays_are_not():
    clip = mg.Renderer(mg.normalise_graphic(title()), 360, 640, 30).frame(1.0)
    assert clip.size == (360, 640) and clip.getchannel("A").getextrema() == (255, 255)
    over = mg.Renderer(mg.normalise_graphic({"grammar": "lower_third", "duration": 3, "data": {"name": "Ana"}}), 360, 640, 30).frame(1.0)
    lo, hi = over.getchannel("A").getextrema()
    assert lo == 0 and hi > 0


def test_a_title_fades_in_and_out_with_its_signature_transition():
    r = mg.Renderer(mg.normalise_graphic(title(look={"transition": "scale_pop"})), 360, 640, 30)
    first, mid, last = r.frame(0.0), r.frame(1.5), r.frame(2.99)
    assert mg.frame_hash(first) != mg.frame_hash(mid) != mg.frame_hash(last)


def test_stepped_motion_holds_frames():
    spec = mg.normalise_graphic(kinetic(look={"motion": {"stepped_fps": 6}}))
    r = mg.Renderer(spec, 360, 640, 30)
    assert mg.frame_hash(r.frame(1.00)) == mg.frame_hash(r.frame(1.05))


# ---------------------------------------------------------------- safe areas

def test_long_text_is_fitted_inside_the_safe_area_at_both_aspects():
    long_title = "Una canción larguísima sobre una ciudad que nunca duerme y que sigue llamando"
    for w, h in ((1080, 1920), (1920, 1080), (1080, 1080)):
        spec = mg.normalise_graphic(title(data={"title": long_title, "subtitle": "con un subtítulo también muy largo para probar el ajuste"}))
        problems = [p for p in mg.check_geometry(spec, w, h) if p["code"] in ("text_clipped", "outside_safe")]
        assert problems == [], (w, h, problems)


def test_geometry_flags_text_pushed_outside_the_frame_or_the_safe_area():
    spec = mg.normalise_graphic(title(safe={"top": 0.4, "bottom": 0.4, "side": 0.4}, data={"title": "Título bastante largo para la caja", "subtitle": "x"}))
    problems = mg.check_geometry(spec, 1080, 1920)
    assert {p["code"] for p in problems} & {"outside_safe", "text_clipped", "text_truncated"}


def test_text_in_the_subtitle_band_is_flagged_only_while_captions_are_on_screen():
    band = mg.subtitle_band_top(1080, 1920, "default")
    assert 0.5 < band < 1.0
    low = mg.normalise_graphic({"grammar": "lower_third", "duration": 3, "data": {"name": "Ana Gómez", "caption": "voz"}, "safe": {"bottom": 0.02}})
    flagged = mg.check_geometry(low, 1080, 1920, band_top=band, captions=[(0.0, 3.0)])
    assert any(p["code"] == "text_in_subtitle_band" for p in flagged)
    assert not any(p["code"] == "text_in_subtitle_band" for p in mg.check_geometry(low, 1080, 1920, band_top=band, captions=[(5.0, 6.0)]))
    assert not any(p["code"] == "text_in_subtitle_band" for p in mg.check_geometry(low, 1080, 1920, band_top=band, captions=[]))
    quiet = mg.normalise_graphic(kinetic())
    assert mg.captions_suppressed(quiet) is True  # kinetic lyrics replace the captions


def test_still_png_decodes_at_the_requested_size():
    png = mg.still_png(mg.normalise_graphic(title()), 540, 960)
    img = Image.open(io.BytesIO(png))
    assert img.size == (540, 960) and img.format == "PNG"
    over = mg.still_png(mg.normalise_graphic({"grammar": "lower_third", "duration": 3, "data": {"name": "Ana"}}), 540, 960, backdrop=False)
    assert Image.open(io.BytesIO(over)).mode == "RGBA"


# --------------------------------------------------------------------- video

def _probe(path: Path) -> dict:
    out = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    return {"text": out}


@needs_ffmpeg
def test_a_clip_graphic_renders_to_h264_with_the_exact_frame_count(tmp_path):
    out = tmp_path / "t.mp4"
    info = mg.render_video(mg.normalise_graphic(title(duration=1.0)), out, width=270, height=480, fps=24)
    assert info["frames"] == 24 and out.exists() and out.stat().st_size > 1000
    text = _probe(out)["text"]
    assert "h264" in text and "yuv420p" in text and "270x480" in text
    raw = subprocess.run([ffmpeg_path(), "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True)
    assert raw.returncode == 0 and len(raw.stdout) == 24 * 270 * 480


@needs_ffmpeg
def test_an_overlay_graphic_renders_with_alpha_and_composites_over_a_video_with_ffmpeg(tmp_path):
    ff = ffmpeg_path()
    over = tmp_path / "over.mov"
    spec = mg.normalise_graphic({"grammar": "lower_third", "duration": 2.0, "data": {"name": "Ana Gómez", "caption": "voz"}})
    mg.render_video(spec, over, width=320, height=568, fps=24)
    base = tmp_path / "base.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x204060:s=320x568:r=24:d=2", "-pix_fmt", "yuv420p", str(base)], check=True)
    merged = tmp_path / "merged.png"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(base), "-i", str(over), "-filter_complex",
                    "[0:v][1:v]overlay=format=auto:eof_action=pass,select=eq(n\\,36)", "-frames:v", "1", str(merged)], check=True)
    plain = tmp_path / "plain.png"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(base), "-vf", "select=eq(n\\,36)", "-frames:v", "1", str(plain)], check=True)
    a, b = Image.open(merged).convert("RGB"), Image.open(plain).convert("RGB")
    assert a.size == b.size == (320, 568)
    delta = np.abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16)).max(axis=2)
    diff = int((delta > 24).sum())
    assert diff > 300, "the lower third did not reach the composite"
    assert diff < a.width * a.height * 0.5, "an overlay must leave most of the picture untouched"
    # the very top of the frame carries nothing: transparent stays transparent
    assert int(delta[:80].max()) <= 4


@needs_ffmpeg
def test_the_overlay_track_is_transparent_outside_the_graphics(tmp_path):
    out = tmp_path / "track.mov"
    g = {"grammar": "lower_third", "data": {"name": "Ana"}}
    info = mg.render_overlay_track([{"start_s": 1.0, "end_s": 2.0, "graphic": g}], out, width=216, height=384, fps=24)
    assert info["frames"] == 48
    first = tmp_path / "f0.png"
    subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(out), "-frames:v", "1", "-pix_fmt", "rgba", str(first)], check=True)
    assert Image.open(first).convert("RGBA").getchannel("A").getextrema() == (0, 0)


@needs_ffmpeg
def test_rendering_can_be_cancelled_and_reports_progress(tmp_path):
    seen: list[float] = []
    out = tmp_path / "c.mp4"
    with pytest.raises(mg.GraphicCancelled):
        mg.render_video(mg.normalise_graphic(title(duration=2.0)), out, width=216, height=384, fps=24,
                        progress=seen.append, should_cancel=lambda: len(seen) >= 2)
    assert seen and not out.exists()
    done: list[float] = []
    mg.render_video(mg.normalise_graphic(title(duration=1.0)), tmp_path / "ok.mp4", width=216, height=384, fps=24, progress=done.append)
    assert done[-1] == 1.0


@needs_ffmpeg
def test_two_renders_of_the_same_spec_give_the_same_frames(tmp_path):
    spec = mg.normalise_graphic(kinetic(duration=1.5))
    hashes = []
    for name in ("a", "b"):
        out = tmp_path / f"{name}.mp4"
        mg.render_video(spec, out, width=216, height=384, fps=24)
        png = tmp_path / f"{name}.png"
        subprocess.run([ffmpeg_path(), "-y", "-loglevel", "error", "-i", str(out), "-vf", "select=eq(n\\,20)", "-frames:v", "1", str(png)], check=True)
        hashes.append(png.read_bytes())
    assert hashes[0] == hashes[1]


def test_odd_frame_sizes_are_refused():
    if not ffmpeg_path():
        pytest.skip("ffmpeg not installed")
    with pytest.raises(mg.GraphicError, match="even"):
        mg.render_video(mg.normalise_graphic(title()), Path("/tmp/never.mp4"), width=271, height=480, fps=24)
