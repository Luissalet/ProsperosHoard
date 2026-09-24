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


def test_fallback_clip_length_is_validated_and_bounded():
    pool = [{"id": "a1", "kind": "image"}, {"id": "a2", "kind": "image"}]
    for bad in (0, -1, 0.1, 61, float("nan"), "abc"):
        with pytest.raises(tl.TimelineError, match="fallback_clip_s"):
            tl.build_auto_cut(10.0, [], [], pool, options={"fallback_clip_s": bad})
    clips = tl.build_auto_cut(10.0, [], [], pool, options={"fallback_clip_s": 0.5})["tracks"][0]["clips"]
    assert all(c["duration_s"] >= tl.MIN_CLIP_S - 1e-6 for c in clips)
    assert sum(c["duration_s"] for c in clips) == pytest.approx(10.0)
    # a 60 s step on a 120.4 s song leaves a 60.4 s remainder: split, never past MAX_CLIP_S
    clips = tl.build_auto_cut(120.4, [], [], pool, options={"fallback_clip_s": 60})["tracks"][0]["clips"]
    assert all(tl.MIN_CLIP_S <= c["duration_s"] <= tl.MAX_CLIP_S for c in clips)
    assert sum(c["duration_s"] for c in clips) == pytest.approx(120.4)


def test_song_shorter_than_one_clip_is_refused():
    pool = [{"id": "a1", "kind": "image"}]
    for beats in ([], [0.0, 0.2]):
        with pytest.raises(tl.TimelineError, match="too short"):
            tl.build_auto_cut(0.3, beats, [], pool)
    clips = tl.build_auto_cut(0.5, [], [], pool)["tracks"][0]["clips"]
    assert [c["duration_s"] for c in clips] == [0.5]


def _lookup(assets):
    table = {a["id"]: a for a in assets}
    return lambda aid: table.get(aid)


def test_normalise_tracks_rejects_bad_edits_with_clip_index():
    assets = [{"id": "a1", "kind": "image"}, {"id": "s1", "kind": "audio"}]
    good = [{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2.0}, {"asset_id": "a1", "duration_s": 1.0,
                                                                                "transition_in": {"type": "crossfade", "duration_s": 0.3}}]}]
    clean = tl.normalise_tracks(good, _lookup(assets))
    assert [c["start_s"] for c in clean[0]["clips"]] == [0.0, 2.0]
    assert clean[0]["clips"][1]["transition_in"] == {"type": "crossfade", "duration_s": 0.3}
    # the first clip's transition is never rendered: stored as a cut
    first = [{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2.0,
                                           "transition_in": {"type": "dip_black", "duration_s": 1.5}}]}]
    assert tl.normalise_tracks(first, _lookup(assets))[0]["clips"][0]["transition_in"] == {"type": "cut", "duration_s": 0.0}
    bad_cases = [
        ([{"type": "visual", "clips": [{"asset_id": "s1", "duration_s": 2.0}]}], "audio"),
        ([{"type": "visual", "clips": [{"asset_id": "zz", "duration_s": 2.0}]}], "does not exist"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 0.1}]}], "between"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2, "transition_in": {"type": "spin"}}]}], "transition"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2, "ken_burns": {"zoom_start": 9}}]}], "zoom"),
        ([{"type": "lyrics", "clips": []}], "visual track"),
        # JSON bodies can carry NaN/Infinity, which pass every range comparison
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2, "trim_start_s": float("nan")}]}], "trim_start_s"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2, "trim_start_s": float("inf")}]}], "trim_start_s"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": float("nan")}]}], "duration_s"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2,
                                        "transition_in": {"type": "cut", "duration_s": float("nan")}}]}], "transition"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2,
                                        "transition_in": {"type": "crossfade", "duration_s": "slow"}}]}], "transition"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2, "ken_burns": {"zoom_end": float("nan")}}]}], "zoom"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2}]},
          {"type": "lyrics", "clips": [{"text": "x", "start_s": float("nan"), "end_s": 2}]}], "lyrics clip 0"),
        ([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2}]},
          {"type": "lyrics", "clips": [{"text": "x", "start_s": 1, "end_s": float("inf")}]}], "lyrics clip 0"),
    ]
    for tracks, fragment in bad_cases:
        with pytest.raises(tl.TimelineError) as exc:
            tl.normalise_tracks(tracks, _lookup(assets))
        assert fragment in str(exc.value)


def test_clip_updates_edit_move_and_delete():
    tracks = [{"type": "visual", "clips": [{"asset_id": f"a{i}", "kind": "image", "duration_s": 1.0} for i in range(4)]}]
    out = tl.apply_clip_updates(tracks, [{"index": 0, "duration_s": 3.0}, {"index": 3, "move_to": 0}, {"index": 1, "delete": True}])
    # every index names the clip as it was before the batch
    assert [c["asset_id"] for c in out[0]["clips"]] == ["a3", "a0", "a2"]
    assert out[0]["clips"][1]["duration_s"] == 3.0
    assert tracks[0]["clips"][0]["duration_s"] == 1.0  # the input is not mutated
    with pytest.raises(tl.TimelineError):
        tl.apply_clip_updates(tracks, [{"index": 9, "duration_s": 1}])
    with pytest.raises(tl.TimelineError):
        tl.apply_clip_updates(tracks, [{"index": 0, "file_path": "/etc/passwd"}])


def test_clip_updates_resolve_indexes_against_the_original_list():
    tracks = [{"type": "visual", "clips": [{"asset_id": f"a{i}", "kind": "image", "duration_s": 1.0} for i in range(5)]}]
    ids = lambda out: [c["asset_id"] for c in out[0]["clips"]]  # noqa: E731
    # two deletes: the second must not shift onto a neighbour
    assert ids(tl.apply_clip_updates(tracks, [{"index": 1, "delete": True}, {"index": 3, "delete": True}])) == ["a0", "a2", "a4"]
    # a delete then an edit of a later clip edits the clip the caller meant
    out = tl.apply_clip_updates(tracks, [{"index": 0, "delete": True}, {"index": 4, "duration_s": 2.5}])
    assert ids(out) == ["a1", "a2", "a3", "a4"] and out[0]["clips"][-1]["duration_s"] == 2.5
    # a move plus an edit on the same clip; move_to past the shortened end is clamped
    out = tl.apply_clip_updates(tracks, [{"index": 0, "move_to": 4, "duration_s": 2.0}, {"index": 2, "delete": True}])
    assert ids(out) == ["a1", "a3", "a4", "a0"] and out[0]["clips"][-1]["duration_s"] == 2.0
    # a deleted clip is not resurrected by a move
    assert ids(tl.apply_clip_updates(tracks, [{"index": 2, "delete": True}, {"index": 2, "move_to": 0}])) == ["a0", "a1", "a3", "a4"]
    with pytest.raises(tl.TimelineError):
        tl.apply_clip_updates(tracks, [{"index": 0, "move_to": 5}])


def test_auto_cut_on_real_demo_analysis_covers_song_on_beats(tmp_path):
    from prosperos_hoard import audio
    from prosperos_hoard.devtools.demo_seed import _make_synthetic_song

    path = tmp_path / "s.wav"
    _make_synthetic_song(path)
    a = audio.analyze_samples(audio.decode_to_mono(path))
    pool = [{"id": f"a{i}", "kind": "image"} for i in range(5)]
    built = tl.build_auto_cut(a["duration_s"], a["beat_times"], a["sections"], pool, downbeats=a["downbeats"])
    assert tl.validate_auto_cut_invariants(built["tracks"], a["beat_times"], a["duration_s"]) == []
    clips = built["tracks"][0]["clips"]
    loud = [c for c in clips if 8.5 <= c["start_s"] < 23.5]
    quiet = [c for c in clips if c["start_s"] < 7.5]
    assert all(c["duration_s"] <= 1.05 for c in loud)  # 2 beats at 120 BPM in the loud section
    assert all(c["duration_s"] >= 1.9 for c in quiet)  # 4 beats in the quiet intro
    compact = tl.compact_view({"id": "t", "project_id": "p", "name": "n", "aspect": "9:16", "fps": 30, "width": 1080,
                               "height": 1920, "tracks": built["tracks"]}, clip_limit=5)
    assert len(compact["clips"]) == 5 and compact["has_more"] and compact["clips_total"] == len(clips)


def test_every_section_starts_on_a_new_shot():
    beats = _synthetic_beats(140.0, 30.0)
    # a section boundary that is not a multiple of the 8-beat low-energy cut step
    sections = [{"label": "Intro", "start_s": 0, "end_s": 9.0, "energy": "low"},
                {"label": "Verse 1", "start_s": 9.0, "end_s": 30.0, "energy": "mid"}]
    pool = [{"id": f"a{i}", "kind": "image"} for i in range(4)]
    result = tl.build_auto_cut(30.0, beats, sections, pool, options={"beats_low": 8}, seed=2)
    starts = [c["start_s"] for c in result["tracks"][0]["clips"]]
    first_verse_beat = next(b for b in beats if b >= 9.0 - 0.05)
    assert first_verse_beat in starts


def test_section_pools_tell_the_story_in_order():
    beats = _synthetic_beats(140.0, 40.0)
    sections = [{"label": "Verse 1", "kind": "verse", "start_s": 0, "end_s": 20.0, "energy": "mid"},
                {"label": "Chorus", "kind": "chorus", "start_s": 20.0, "end_s": 30.0, "energy": "high"},
                {"label": "Chorus", "kind": "chorus", "start_s": 30.0, "end_s": 40.0, "energy": "high"}]
    verse = [{"id": f"v{i}", "kind": "image"} for i in range(4)]
    chorus = [{"id": f"c{i}", "kind": "image"} for i in range(3)]
    pool = verse + chorus
    result = tl.build_auto_cut(40.0, beats, sections, pool,
                               options={"section_pools": {"Verse": verse, "chorus": chorus}}, seed=1)
    clips = result["tracks"][0]["clips"]
    verse_ids = [c["asset_id"] for c in clips if c["start_s"] < 20.0]
    assert verse_ids[:4] == ["v0", "v1", "v2", "v3"]  # "Verse 1" found the "Verse" pool, in order
    chorus_ids = [c["asset_id"] for c in clips if c["start_s"] >= 20.0]
    assert set(chorus_ids) <= {"c0", "c1", "c2"}
    assert chorus_ids[:3] == ["c0", "c1", "c2"]
    assert all(a != b for a, b in zip([c["asset_id"] for c in clips], [c["asset_id"] for c in clips][1:]))
    assert tl.validate_auto_cut_invariants(result["tracks"], beats, 40.0) == []


def test_cut_on_lyrics_starts_a_shot_with_each_line():
    beats = _synthetic_beats(140.0, 30.0)
    sections = [{"label": "Verse", "start_s": 0, "end_s": 30.0, "energy": "low"}]
    lines = [{"time_s": 3.1, "text": "a"}, {"time_s": 7.75, "text": "b"}, {"time_s": 12.0, "text": "c"}]
    pool = [{"id": f"a{i}", "kind": "image"} for i in range(4)]
    result = tl.build_auto_cut(30.0, beats, sections, pool, options={"beats_low": 16, "cut_on_lyrics": True},
                               lyrics_lines=lines, seed=1)
    starts = [c["start_s"] for c in result["tracks"][0]["clips"]]
    for ln in lines:
        assert min(beats, key=lambda b: abs(b - ln["time_s"])) in starts
    assert tl.validate_auto_cut_invariants(result["tracks"], beats, 30.0) == []


def test_a_beat_a_hair_before_a_rounded_section_start_opens_that_section():
    beats = _synthetic_beats(140.0, 20.0)
    beat = beats[23]  # e.g. 9.857 s; an LRC marker for it reads 9.86
    sections = [{"label": "Verse", "start_s": 0, "end_s": round(beat, 2), "energy": "mid"},
                {"label": "Chorus", "start_s": round(beat, 2), "end_s": 20.0, "energy": "high"}]
    pool = [{"id": "v", "kind": "image"}, {"id": "c", "kind": "image"}, {"id": "x", "kind": "image"}]
    result = tl.build_auto_cut(20.0, beats, sections, pool,
                               options={"section_pools": {"Verse": [pool[0], pool[2]], "Chorus": [pool[1], pool[2]]}})
    clip = next(c for c in result["tracks"][0]["clips"] if c["start_s"] == beat)
    assert clip["asset_id"] == "c"


def test_video_cuts_skip_the_still_opening_and_rotate_through_the_clip():
    beats = _synthetic_beats(140.0, 20.0)
    sections = [{"label": "Chorus", "kind": "chorus", "start_s": 0, "end_s": 20.0, "energy": "high"}]
    clip = {"id": "v1", "kind": "video", "duration_s": 5.04}
    pool = [clip, {"id": "i1", "kind": "image"}]
    plain = tl.build_auto_cut(20.0, beats, sections, pool, seed=1)
    assert all(c["trim_start_s"] == 0.0 for c in plain["tracks"][0]["clips"])  # the default is unchanged
    built = tl.build_auto_cut(20.0, beats, sections, pool, seed=1,
                              options={"video_lead_in_s": 1.0, "video_rotate_offsets": True})
    videos = [c for c in built["tracks"][0]["clips"] if c["asset_id"] == "v1"]
    assert len(videos) >= 3
    trims = [c["trim_start_s"] for c in videos]
    assert all(t >= 1.0 - 1e-6 for t in trims)  # never the still-frame opening
    assert all(t + c["duration_s"] <= 5.04 + 1e-6 for t, c in zip(trims, videos))  # always inside the clip
    assert len(set(trims)) >= 2  # reuses show different moments
    # the edited tracks still validate
    lookup = {a["id"]: a for a in pool}
    tl.normalise_tracks(built["tracks"], lambda i: lookup.get(i))


def test_video_trim_falls_back_to_zero_when_the_clip_is_short_or_unknown():
    uses: dict = {}
    assert tl._video_trim({"id": "x", "duration_s": 1.0}, 1.5, uses, 1.0, True) == 0.0
    assert tl._video_trim({"id": "y"}, 1.0, uses, 1.0, True) == 0.0
    assert tl._video_trim({"id": "z", "duration_s": 2.0}, 1.5, uses, 1.0, False) == 0.5
