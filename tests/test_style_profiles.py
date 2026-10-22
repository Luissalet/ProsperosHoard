"""Tests for style profile extraction from video and application to productions.

Verifies:
1. Deterministic metadata extraction from a synthetic test video (duration, cuts, palette).
2. Unobserved fields (camera movement, semantic transitions) are marked unobserved, not facts.
3. Two different productions reuse the same editable style profile without reusing source footage.
4. Profile editing, listing, updating, and persistence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prosperos_hoard import backend, procutil
from prosperos_hoard.productions import create_production, load_state
from prosperos_hoard.store import Store
from prosperos_hoard.style_profiles import (
    apply_style_profile_to_production,
    delete_style_profile,
    extract_style_profile_from_video,
    list_style_profiles,
    load_style_profile,
    save_style_profile,
    update_style_profile,
)


def test_profile_reaches_native_prompts_duration_and_timeline_transition(tmp_path, monkeypatch):
    from prosperos_hoard import productions as prod, engine
    from prosperos_hoard.style_profiles import apply_timeline_transitions
    store = Store(tmp_path / "studio")
    project = store.create_project("Style pipeline regression")
    image = store.create_asset(project["id"], "image", "synthetic.png", mime="image/png", width=64, height=64)
    song = store.create_asset(project["id"], "audio", "synthetic.wav", mime="audio/wav", duration_s=10)
    prod.create_production(store.data_dir, "Profile pipeline", {"lead": {"name": "Shape", "look": "Simple shape"},
        "shots": [{"key": "1", "prompt": "An original subject", "lead": False}]}, slug="pipeline", project_id=project["id"])
    profile = {"id": "sp_local", "name": "Local", "palette": ["#112233"], "shots": [{"relative_duration": 1,
        "camera": {"shot_type": "wide", "movement": "slow_pan"}, "transition_in": {"type": "crossfade"}}]}
    apply_style_profile_to_production(store.data_dir, "pipeline", profile, {"target_duration_s": 6})
    runner = prod.Run(store, None, "pipeline", lambda *a, **k: None)
    shot = runner.spec["shots"][0]
    runner.state["done"]["frames"]["items"]["1"] = {"best": image["id"], "variants": [image["id"]]}
    assert "wide" in runner.shot_prompt(shot)["prompt"]
    assert "#112233" in runner.shot_prompt(shot)["prompt"]
    assert "slow pan" in runner._clip_body(shot, 0)["prompt"]
    assert prod.shot_span(shot) == (0, 6)
    monkeypatch.setattr(engine, "analyze_audio", lambda *a: {"duration_s": 10, "sections": [], "beat_times": [0, 2, 4, 6, 8], "downbeats": [0, 4, 8]})
    timeline = engine.timeline_auto(store, project["id"], song["id"], [image["id"]], None, "16:9", None,
                                    runner.spec["timeline"]["options"])
    apply_timeline_transitions(store, runner.state, timeline["id"])
    visual = next(t for t in store.get_timeline(timeline["id"])["tracks"] if t["type"] == "visual")
    assert sum(c["duration_s"] for c in visual["clips"]) == 6
    assert all(c["transition_in"]["type"] == "crossfade" for c in visual["clips"])


def test_zero_duration_is_rejected_instead_of_replaced(tmp_path):
    from prosperos_hoard.style_profiles import StyleProfileError
    with pytest.raises(StyleProfileError, match="duration"):
        apply_style_profile_to_production(tmp_path, "missing", {"shots": [{"relative_duration": 1}]}, {"target_duration_s": 0})


@pytest.fixture
def synthetic_video(tmp_path: Path) -> Path:
    """Creates a deterministic synthetic video with known cuts:
    - 0.0s to 1.0s: Red
    - 1.0s to 2.5s: Blue
    - 2.5s to 4.0s: Green
    Total duration: 4.0s. Cuts at 1.0s and 2.5s.
    """
    ffmpeg = backend.ffmpeg_path()
    assert ffmpeg, "ffmpeg is required for test"

    c1 = tmp_path / "c1.mp4"
    c2 = tmp_path / "c2.mp4"
    c3 = tmp_path / "c3.mp4"
    out_video = tmp_path / "synthetic_ref.mp4"

    cmd1 = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1.0", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(c1)]
    cmd2 = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=1.5", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(c2)]
    cmd3 = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=green:s=320x240:d=1.5", "-r", "25", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(c3)]

    for cmd in (cmd1, cmd2, cmd3):
        res = procutil.run(cmd, text=True)
        assert res.returncode == 0, f"ffmpeg failed: {res.stderr}"

    list_file = tmp_path / "concat.txt"
    list_file.write_text(f"file '{c1.name}'\nfile '{c2.name}'\nfile '{c3.name}'\n", encoding="utf-8")

    concat_cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_video)]
    res = procutil.run(concat_cmd, text=True, cwd=str(tmp_path))
    assert res.returncode == 0, f"concat failed: {res.stderr}"
    assert out_video.is_file()
    return out_video


def test_extract_style_profile_matches_synthetic_video(synthetic_video: Path):
    profile = extract_style_profile_from_video(synthetic_video, name="Test Reference", scene_threshold=0.25)

    # 1. Measured facts match the synthetic video
    measured = profile["measured_facts"]
    assert abs(measured["duration_s"] - 4.0) < 0.2
    assert measured["shot_count"] == 3

    # Check cut timestamps are close to 1.0s and 2.5s
    cuts = measured["cuts_s"]
    assert len(cuts) == 2
    assert abs(cuts[0] - 1.0) < 0.15
    assert abs(cuts[1] - 2.5) < 0.15

    # Check resolution
    assert measured["resolution"]["width"] == 320
    assert measured["resolution"]["height"] == 240
    assert measured["fps"] == 25.0

    # Check shot durations
    shot_durs = measured["shot_durations_s"]
    assert len(shot_durs) == 3
    assert abs(shot_durs[0] - 1.0) < 0.15
    assert abs(shot_durs[1] - 1.5) < 0.15
    assert abs(shot_durs[2] - 1.5) < 0.15

    # Check color palettes: shot 1 is red, shot 2 is blue, shot 3 is green
    shots = profile["shots"]
    assert len(shots) == 3

    # Shot 1 color: high red component
    p1 = shots[0]["palette"][0]
    r1 = int(p1[1:3], 16)
    assert r1 > 180

    # Shot 2 color: high blue component
    p2 = shots[1]["palette"][0]
    b2 = int(p2[5:7], 16)
    assert b2 > 180

    # Shot 3 color: high green component
    p3 = shots[2]["palette"][0]
    g3 = int(p3[3:5], 16)
    r3 = int(p3[1:3], 16)
    b3 = int(p3[5:7], 16)
    assert g3 > 100
    assert r3 < 50
    assert b3 < 50

    # 2. Unobserved fields are NOT presented as facts (camera & semantic transitions)
    for s in shots:
        cam = s["camera"]
        assert cam["observed"] is False
        assert cam["shot_type"] == "unknown"
        assert cam["angle"] == "unknown"
        assert cam["movement"] == "unknown"
        assert "not observed" in cam["note"].lower()

    # Transition into shot 1 is none; into shots 2 and 3 are measured cuts
    assert shots[0]["transition_in"]["measured"] is False
    assert shots[1]["transition_in"]["measured"] is True
    assert shots[1]["transition_in"]["type"] == "cut"
    assert shots[2]["transition_in"]["measured"] is True
    assert shots[2]["transition_in"]["type"] == "cut"


def test_profile_crud_and_persistence(tmp_path: Path, synthetic_video: Path):
    data_dir = tmp_path / "data"
    profile = extract_style_profile_from_video(synthetic_video, name="Color Flow")
    saved = save_style_profile(data_dir, profile)
    pid = saved["id"]

    # Load
    loaded = load_style_profile(data_dir, pid)
    assert loaded["name"] == "Color Flow"
    assert loaded["measured_facts"]["shot_count"] == 3

    # List
    all_profiles = list_style_profiles(data_dir)
    assert len(all_profiles) == 1
    assert all_profiles[0]["id"] == pid

    # Update editable fields
    updated = update_style_profile(data_dir, pid, {"name": "Updated Flow", "palette": ["#112233", "#445566"]})
    assert updated["name"] == "Updated Flow"
    assert updated["palette"] == ["#112233", "#445566"]

    # Reload to verify persistence
    reloaded = load_style_profile(data_dir, pid)
    assert reloaded["name"] == "Updated Flow"

    # Delete
    assert delete_style_profile(data_dir, pid) is True
    assert len(list_style_profiles(data_dir)) == 0


def test_two_different_productions_reuse_same_editable_profile(tmp_path: Path, synthetic_video: Path):
    """Verifies that two different productions can reuse the same editable profile,
    adapting to their respective target durations without copying or reusing the reference footage.
    """
    data_dir = tmp_path / "data"
    Store(data_dir)

    # 1. Create editable profile from synthetic reference
    profile = extract_style_profile_from_video(synthetic_video, name="Shared Rhythm")
    # Edit the profile to specify creative camera and note
    profile["shots"][0]["camera"]["shot_type"] = "wide"
    profile["shots"][1]["camera"]["shot_type"] = "close_up"
    profile["shots"][2]["camera"]["shot_type"] = "medium_shot"
    saved_profile = save_style_profile(data_dir, profile)
    pid = saved_profile["id"]

    # 2. Production A: Target duration 10.0s (e.g. short teaser)
    spec_a = {
        "title": "Teaser A",
        "lead": {"name": "Hero A", "look": "Warrior in cyber armor"},
        "shots": [
            {"key": "1", "prompt": "Intro scene", "seed": 100, "clip_seed": 200},
        ],
    }
    create_production(data_dir, "Prod A", spec_a, slug="prod-a")

    # Apply profile to Production A with target duration 10s
    res_a = apply_style_profile_to_production(data_dir, "prod-a", pid, options={"target_duration_s": 10.0})
    assert res_a["shot_count"] == 3
    assert res_a["target_duration_s"] == 10.0

    state_a = load_state(data_dir, "prod-a")
    shots_a = state_a["spec"]["shots"]
    assert len(shots_a) == 3
    # Check that durations scaled proportionally to 10s (approx 2.5s, 3.75s, 3.75s)
    assert shots_a[0]["start_s"] == 0.0
    assert shots_a[0]["camera"]["shot"] == "wide"
    assert shots_a[1]["camera"]["shot"] == "close_up"
    assert shots_a[2]["camera"]["shot"] == "medium_shot"
    assert shots_a[2]["end_s"] == 10.0
    assert state_a["style_profile"]["id"] == pid

    # 3. Production B: Target duration 20.0s (e.g. full clip)
    spec_b = {
        "title": "Music Video B",
        "lead": {"name": "Singer B", "look": "Pop star with neon jacket"},
        "shots": [
            {"key": "1", "prompt": "Opening performance", "seed": 500, "clip_seed": 600},
        ],
    }
    create_production(data_dir, "Prod B", spec_b, slug="prod-b")

    # Apply the EXACT SAME profile to Production B with target duration 20s
    res_b = apply_style_profile_to_production(data_dir, "prod-b", pid, options={"target_duration_s": 20.0})
    assert res_b["shot_count"] == 3
    assert res_b["target_duration_s"] == 20.0

    state_b = load_state(data_dir, "prod-b")
    shots_b = state_b["spec"]["shots"]
    assert len(shots_b) == 3
    assert shots_b[0]["start_s"] == 0.0
    assert shots_b[0]["camera"]["shot"] == "wide"
    assert shots_b[1]["camera"]["shot"] == "close_up"
    assert shots_b[2]["camera"]["shot"] == "medium_shot"
    assert shots_b[2]["end_s"] == 20.0
    assert state_b["style_profile"]["id"] == pid

    # Verify neither production copied or referenced synthetic_video file path in their assets
    assert "source_path" not in state_a["spec"]
    assert "source_path" not in state_b["spec"]
