import numpy as np
import pytest

from conftest import needs_ffmpeg
from prosperos_hoard.editing import analyze
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing.analysis import audio as audio_an
from prosperos_hoard.editing.analysis import speech
from prosperos_hoard.editing.analysis import video as video_an
from prosperos_hoard.editing.render.compiler import piecewise, plan_chunks
from prosperos_hoard.editing.timeline import Keyframe, new_project
from prosperos_hoard.editing.ops import apply_ops
from conftest import LOOK


def eval_expr(expr: str, t: float) -> float:
    return eval(expr.replace("clip(", "_clip("), {"_clip": lambda x, a, b: max(a, min(b, x)), "t": t})


def test_piecewise_is_linear_interpolation():
    pts = [(0.0, 0.2), (2.0, 0.8), (3.0, 0.5)]
    e = piecewise(pts)
    for t, want in [(-1, 0.2), (0, 0.2), (1, 0.5), (2, 0.8), (2.5, 0.65), (3, 0.5), (9, 0.5)]:
        assert abs(eval_expr(e, t) - want) < 1e-3


def test_chunks_never_cut_a_transition_or_a_fade():
    p = new_project(1920, 1080, 30)
    ops = [{"op": "add_media", "media": "m1", "src_in": i * 1000, "src_out": i * 1000 + 1000} for i in range(12)]
    ops += [{"op": "add_media", "media": "m1"}, {"op": "add_media", "media": "m1"}]
    p, _ = apply_ops(p, ops, LOOK)
    p, _ = apply_ops(p, [{"op": "transition", "all_cuts": True, "dur": 600}], LOOK)
    chunks = plan_chunks(p, p.duration, 30)
    assert chunks[0][0] == 0 and chunks[-1][1] == round(p.duration * 30 / 1000)
    assert all(a < b for a, b in chunks) and all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))
    spans = [(c.start * 30 / 1000, (c.start + c.transition_in.dur) * 30 / 1000) for c in p.main_track().clips if c.transition_in]
    for a, b in chunks:
        for x, y in spans:
            assert not (x < b < y), (b, x, y)


def test_silences_with_margins():
    rms = np.full(1000, -20.0, dtype=np.float32)
    rms[200:400] = -70  # 2 s of silence at 2-4 s
    rms[600:605] = -70  # 50 ms dip: not a silence
    ranges, thr = audio_an.silences(rms, min_silence_ms=500, margin_ms=150)
    assert ranges == [[2150, 3850]]
    assert -60 < thr < -20


def test_beat_tracker_finds_120_bpm():
    rate = 16000
    t = np.arange(rate * 12) / rate
    pcm = np.where((t % 0.5) < 0.02, np.sin(2 * np.pi * 1000 * t) * 0.9, 0) * 32767
    env, fps = audio_an.onset_envelope(pcm.astype(np.int16))
    bpm = audio_an.tempo(env, fps)
    assert abs(bpm - 120) < 3
    beats = audio_an.beat_track(env, fps, bpm)
    times = beats / fps
    assert len(times) >= 20 and np.median(np.diff(times)) == pytest.approx(0.5, abs=0.03)


def test_fillers_and_cut_ranges():
    words = [{"id": "w1", "t0": 0, "t1": 300, "text": "Bueno,"}, {"id": "w2", "t0": 700, "t1": 1000, "text": "eh"},
             {"id": "w3", "t0": 1100, "t1": 1400, "text": "hoy"}, {"id": "w4", "t0": 1450, "t1": 1700, "text": "vamos"},
             {"id": "w5", "t0": 1750, "t1": 1900, "text": "vamos"}, {"id": "w6", "t0": 1950, "t1": 2300, "text": "a"},
             {"id": "w7", "t0": 2350, "t1": 2800, "text": "editar"}]
    hits = speech.find_fillers(words, "es")
    found = {(h["text"], h["reason"]) for h in hits}
    assert ("eh", "filler") in found and ("vamos", "repeat") in found and ("Bueno,", "filler") in found
    ranges = speech.cut_ranges_for_words(words, {"w2"})
    a, b = ranges[0]
    assert 300 < a <= 700 and 1000 <= b < 1100


def test_smooth_path_stable_within_scene_and_clamped():
    samples = [[i * 333, 0.7 + 0.01 * ((i % 3) - 1), 0.5, 1.0, 1] for i in range(30)]
    path = video_an.smooth_path(samples, [], aspect_in=16 / 9, aspect_out=9 / 16)
    xs = {round(p[1], 3) for p in path}
    assert len(xs) == 1 and 0.6 < xs.pop() < 0.8
    far = [[i * 333, 0.99, 0.5, 1.0, 1] for i in range(10)]
    clamped = video_an.smooth_path(far, [], aspect_in=16 / 9, aspect_out=9 / 16)
    assert max(p[1] for p in clamped) <= 1 - (9 / 16) / (16 / 9) / 2 + 1e-4


@needs_ffmpeg
def test_analysis_jobs_on_real_media(services, media_dir):
    scenes = media_store.import_path(services, str(media_dir / "scenes.mp4"))
    clicks = media_store.import_path(services, str(media_dir / "clicks.wav"))
    talk = media_store.import_path(services, str(media_dir / "talk.mp4"))
    jobs = analyze.schedule(services, scenes["id"], ["scenes", "motion", "focus"])
    assert all(services.jobs.get(j["job"])["state"] == "done" for j in jobs)
    cuts = [c["t"] for c in media_store.get_analysis(services, scenes["id"], "scenes")["cuts"]]
    assert len(cuts) == 2 and abs(cuts[0] - 2000) < 100 and abs(cuts[1] - 4000) < 100
    assert media_store.get_analysis(services, scenes["id"], "focus")["samples"]
    analyze.schedule(services, clicks["id"], ["beats", "loudness"])
    beats = media_store.get_analysis(services, clicks["id"], "beats")
    assert abs(beats["bpm"] - 120) < 3
    loud = media_store.get_analysis(services, clicks["id"], "loudness")
    assert loud["integrated_lufs"] is not None
    s = analyze.silences_for(services, talk["id"])
    starts = [r[0] for r in s["ranges"]]
    assert any(abs(x - 3150) < 200 for x in starts) and any(abs(x - 8150) < 200 for x in starts)
    assert analyze.schedule(services, scenes["id"], ["scenes"])[0]["state"] == "cached"
    with pytest.raises(Exception):
        analyze.schedule(services, scenes["id"], ["transcript"])  # no audio


def test_keyframe_ease_points_monotonic():
    from prosperos_hoard.editing.render.compiler import _ease_points

    pts = _ease_points([Keyframe(t=0, v=0, ease="ease_in_out"), Keyframe(t=1000, v=1)])
    vals = [v for _, v in pts]
    assert vals == sorted(vals) and vals[0] == 0 and vals[-1] == 1 and len(pts) > 3


def test_filter_list_parsing_for_old_and_new_ffmpeg(monkeypatch):
    from prosperos_hoard.editing import ffmpeg as ff
    from prosperos_hoard.editing.hoard_link import proc as hlproc

    outputs = {
        "-filters": "Filters:\n  T.. = Timeline support\n ---\n TS allpass           A->A       Apply\n .. ass               V->V       Render ASS\n"
                    " ..C sidechaincompress AA->A      Sidechain\n ... vidstabdetect     V->V       Extract\n",
        "-encoders": "Encoders:\n ------\n V....D h264_nvenc           NVIDIA NVENC H.264 encoder\n A....D aac                  AAC\n",
    }

    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    monkeypatch.setattr(hlproc, "run", lambda args, **kw: R(outputs[args[2]]))
    t = ff.Tools("ffmpeg", "ffprobe", "8.0.1-full_build")
    assert {"allpass", "ass", "sidechaincompress", "vidstabdetect"} <= t.filters
    assert "h264_nvenc" in t.encoders and t.version.startswith("8.0.1")
