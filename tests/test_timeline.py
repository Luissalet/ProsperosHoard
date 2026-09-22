import pytest

from prosperos_hoard import timeline as tl


def _synthetic_beats(bpm: float, duration_s: float) -> list[float]:
    period = 60.0 / bpm
    beats = []
    t = 0.0
    while t < duration_s:
        beats.append(round(t, 3))
        t += period
    return beats


def test_build_auto_cut_invariants_hold_across_energy_levels():
    beats = _synthetic_beats(120.0, 40.0)
    sections = [
        {"start_s": 0, "end_s": 10, "energy": "low"},
        {"start_s": 10, "end_s": 20, "energy": "high"},
        {"start_s": 20, "end_s": 30, "energy": "mid"},
        {"start_s": 30, "end_s": 40, "energy": "high"},
    ]
    pool = [{"id": f"a{i}", "kind": "image"} for i in range(6)]
    result = tl.build_auto_cut(40.0, beats, sections, pool, options={"flash_on_strong_downbeats": True}, seed=3)
    problems = tl.validate_auto_cut_invariants(result["tracks"], beats, 40.0)
    assert problems == []


def test_high_energy_sections_cut_more_often_than_low():
    beats = _synthetic_beats(120.0, 20.0)
    sections = [{"start_s": 0, "end_s": 10, "energy": "low"}, {"start_s": 10, "end_s": 20, "energy": "high"}]
    pool = [{"id": f"a{i}", "kind": "image"} for i in range(4)]
    result = tl.build_auto_cut(20.0, beats, sections, pool, seed=1)
    clips = result["tracks"][0]["clips"]
    low_clips = [c for c in clips if c["start_s"] < 10]
    high_clips = [c for c in clips if c["start_s"] >= 10]
    assert len(high_clips) > len(low_clips)


def test_empty_pool_raises():
    with pytest.raises(ValueError):
        tl.build_auto_cut(10.0, [0, 1, 2], [{"start_s": 0, "end_s": 10, "energy": "low"}], [])


def test_lyrics_track_built_from_lrc_lines():
    beats = _synthetic_beats(100.0, 10.0)
    sections = [{"start_s": 0, "end_s": 10, "energy": "mid"}]
    pool = [{"id": "a1", "kind": "image"}, {"id": "a2", "kind": "image"}]
    lyrics = [{"time_s": 0.5, "text": "hello"}, {"time_s": 4.0, "text": "world"}]
    result = tl.build_auto_cut(10.0, beats, sections, pool, lyrics_lines=lyrics, options={"karaoke": True})
    lyric_track = next(t for t in result["tracks"] if t["type"] == "lyrics")
    assert len(lyric_track["clips"]) == 2
    assert lyric_track["clips"][0]["karaoke"] is True
    assert lyric_track["clips"][-1]["end_s"] == 10.0


def test_no_immediate_repeat_asset_with_small_pool():
    beats = _synthetic_beats(160.0, 15.0)
    sections = [{"start_s": 0, "end_s": 15, "energy": "high"}]
    pool = [{"id": "a1", "kind": "image"}, {"id": "a2", "kind": "image"}]
    result = tl.build_auto_cut(15.0, beats, sections, pool, seed=7)
    clips = result["tracks"][0]["clips"]
    for i in range(1, len(clips)):
        assert clips[i]["asset_id"] != clips[i - 1]["asset_id"]
