from __future__ import annotations

import pytest
from prosperos_hoard import audio, procutil, qa, video
from prosperos_hoard.hoard_link.media import bins
from prosperos_hoard import timeline as timeline_mod


def test_small_shortfall_is_retimed_without_a_frozen_tail(tmp_path):
    exe = bins.find("ffmpeg").path
    if not exe:
        pytest.skip("ffmpeg unavailable")
    src, out = tmp_path / "moving.mp4", tmp_path / "out.mp4"
    procutil.run([str(exe), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                  "testsrc2=size=160x90:rate=24:duration=1.8", "-pix_fmt", "yuv420p", str(src)], check=True)
    timeline = {"width": 160, "height": 90, "fps": 24, "tracks": [{"type": "visual", "clips": [
        {"asset_id": "v", "kind": "video", "duration_s": 2.5, "source_fit": "stretch"}]}]}
    video.render_timeline(timeline, lambda _: src, tmp_path / "work", out, quality="preview")
    assert abs(audio.probe_duration_s(out) - 2.5) < 0.08
    frames = qa.video_gray_frames(out)
    assert qa.frozen_intervals(frames, len(frames) / audio.probe_duration_s(out)) == []
    timeline["tracks"][0]["clips"][0]["duration_s"] = 4
    with pytest.raises(video.RenderError):
        video.render_timeline(timeline, lambda _: src, tmp_path / "work2", tmp_path / "bad.mp4")


def test_moving_clip_with_frozen_tail_cannot_hide_in_average_motion():
    import numpy as np
    moving = np.stack([np.full((8, 8), i * 4, dtype=np.float32) for i in range(24)])
    frames = np.concatenate([moving, np.repeat(moving[-1:], 24, axis=0)])
    assert qa.clip_motion(frames)["energy"] > qa.DEFAULT_THRESHOLDS["move_motion_min"]
    holds = qa.frozen_intervals(frames, 24)
    assert holds and holds[0]["duration_s"] >= 1


def test_hold_can_be_saved_through_the_editor_without_retiming_synced_speech():
    tracks = [{"type": "visual", "clips": [{"asset_id": "v", "kind": "video", "duration_s": 3}]}]
    edited = timeline_mod.apply_clip_updates(tracks, [{"index": 0, "source_fit": "hold"}])
    lookup = lambda _: {"id": "v", "kind": "video", "duration_s": 2}
    clean = timeline_mod.normalise_tracks(edited, lookup)
    assert clean[0]["clips"][0]["source_fit"] == "hold"
    tracks[0]["clips"][0].update(synced=True, source_fit="error")
    clean = timeline_mod.normalise_tracks(tracks, lookup)
    assert clean[0]["clips"][0]["synced"] and clean[0]["clips"][0]["source_fit"] == "error"
