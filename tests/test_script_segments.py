from __future__ import annotations

import math
import pytest
from prosperos_hoard import script_segments as segments, productions as prod
from test_productions import tiny_spec


def test_plain_text_is_optional_untimed_and_subtitle_ends_are_preserved():
    rows = segments.parse_text("[Verse]\nFirst phrase\nSecond phrase")
    assert [r["text"] for r in rows] == ["First phrase", "Second phrase"]
    assert all(r["start_s"] is None and r["shot_key"] is None for r in rows)
    rows = segments.parse_text("WEBVTT\n\n00:01.500 --> 00:03.000\nHello\nworld\n\n00:04.000 --> 00:05.200\nAgain")
    assert [(r["start_s"], r["end_s"]) for r in rows] == [(1.5, 3), (4, 5.2)]
    assert rows[0]["text"] == "Hello\nworld"


def test_repeated_lrc_phrases_keep_their_own_positions():
    rows = segments.parse_text("[ar:Test]\n[00:02.00][00:06.00]Same phrase\n[00:04.00]Different phrase")
    assert [(r["text"], r["start_s"], r["end_s"]) for r in rows] == [
        ("Same phrase", 2, 4), ("Different phrase", 4, 6), ("Same phrase", 6, None)]


@pytest.mark.parametrize("start,end", [(math.nan, 3), (0, math.inf), (True, 3), (-1, 3), (3, 2), (None, 3)])
def test_invalid_times_cannot_retime_shots(start, end):
    with pytest.raises(ValueError):
        segments.validate([{"text": "A", "start_s": start, "end_s": end}], set())


def test_script_timing_links_existing_shots_without_discarding_frames_and_rolls_back_overlap(data_dir):
    state = prod.create_production(data_dir, "Segments", tiny_spec())
    slug = state["slug"]
    state["done"]["frames"] = {"1": {"best": "retained_frame"}}
    prod.save_state(data_dir, state)
    imported = prod.set_script_segments(data_dir, slug, text="[00:01.00]First\n[00:03.00]Second\n[00:05.00]Third")
    rows = imported["segments"]
    rows[0]["shot_key"] = "1"
    rows[1]["shot_key"] = "2"
    result = prod.set_script_segments(data_dir, slug, segments=rows)
    assert result["retimed"] == ["1", "2"]
    saved = prod.load_state(data_dir, slug)
    assert prod.shot_span(saved["spec"]["shots"][0]) == (1, 3)
    assert saved["done"]["frames"]["1"]["best"] == "retained_frame"
    rows[1]["start_s"] = 2
    with pytest.raises(prod.ProductionError):
        prod.set_script_segments(data_dir, slug, segments=rows)
    assert prod.load_state(data_dir, slug)["spec"]["script_segments"] == saved["spec"]["script_segments"]


def test_timed_script_cannot_change_an_approved_shot(data_dir):
    state = prod.create_production(data_dir, "Locked script", tiny_spec())
    state["spec"]["shots"][0]["locked"] = True
    prod.save_state(data_dir, state)
    with pytest.raises(prod.ProductionError, match="approved"):
        prod.set_script_segments(data_dir, state["slug"], segments=[
            {"id": "line1", "text": "New timing", "shot_key": "1", "start_s": 1, "end_s": 3}])
    saved = prod.load_state(data_dir, state["slug"])
    assert "script_segments" not in saved["spec"]
    assert prod.shot_span(saved["spec"]["shots"][0]) == prod.shot_span(state["spec"]["shots"][0])
