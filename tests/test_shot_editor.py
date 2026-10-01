"""Editing a production's shots one by one: insert, delete, move, section,
lead, per-shot references, the song's lyrics and where each shot plays."""

from __future__ import annotations

import pytest

from prosperos_hoard import productions as prod

from test_productions import tiny_spec


def _paused(data_dir, **spec_over):
    spec = prod.normalise_spec(tiny_spec(**spec_over))
    state = prod.create_production(data_dir, "Shot Editor", spec, {"animatic": False})
    state["status"] = "awaiting_review"
    state["done"] = {"frames": {"complete": True, "items": {"1": {"variants": ["a_1", "a_2"], "best": "a_1"},
                                                            "2": {"variants": ["a_3"], "best": "a_3"}}},
                     "clips": {"complete": True, "items": {"1": "c_1", "2": "c_2"}},
                     "animatic": {"renders": {}}, "timeline": {"complete": True, "timelines": {}}}
    prod.save_state(data_dir, state)
    return state["slug"]


def test_insert_delete_and_move(data_dir):
    slug = _paused(data_dir)
    out = prod.update_shots(data_dir, slug, [{"insert": {"after": "1", "prompt": "the crowd jumps", "lead": False,
                                                         "section": "chorus"}}])
    assert out["changed"] == ["3"]
    st = prod.load_state(data_dir, slug)
    assert [s["key"] for s in st["spec"]["shots"]] == ["1", "3", "2"]
    new = st["spec"]["shots"][1]
    assert new["section"] == "chorus" and new["clips"] == [0] and new["width"] == 512 and new["seed"] == 3030
    assert "animatic" not in st["done"] and st["status"] == "queued"
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    prod.update_shots(data_dir, slug, [{"key": "2", "after": "start"}, {"key": "1", "delete": True}])
    st = prod.load_state(data_dir, slug)
    assert [s["key"] for s in st["spec"]["shots"]] == ["2", "3"]
    assert "1" not in st["done"]["frames"]["items"] and "1" not in st["done"]["clips"]["items"]
    assert all("1" not in keys and "1v2" not in keys for keys in st["spec"]["timeline"]["storyboard"].values())
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    with pytest.raises(prod.ProductionError, match="at least one"):
        prod.update_shots(data_dir, slug, [{"key": "2", "delete": True}, {"key": "3", "delete": True}])


def test_refs_lead_and_section_regenerate_the_shot(data_dir):
    slug = _paused(data_dir)
    prod.update_shots(data_dir, slug, [
        {"key": "1", "refs": [{"asset_id": "a_pose123", "use": "copy this dance pose"},
                              {"asset_id": "a_mons456", "use": "these Pokemon in the background"}]},
        {"key": "2", "lead": True, "section": "verse"}])
    st = prod.load_state(data_dir, slug)
    one, two = st["spec"]["shots"]
    assert one["refs"][0] == {"asset_id": "a_pose123", "use": "copy this dance pose"}
    assert two["lead"] is True and two["section"] == "verse"
    assert "1" not in st["done"]["frames"]["items"] and "2" not in st["done"]["frames"]["items"]
    with pytest.raises(prod.ProductionError, match="section"):
        st["status"] = "awaiting_review"
        prod.save_state(data_dir, st)
        prod.update_shots(data_dir, slug, [{"key": "1", "section": "solo"}])


def test_shot_prompt_numbers_the_refs_after_the_lead(data_dir):
    spec = prod.normalise_spec(tiny_spec())
    spec["shots"][0]["refs"] = [{"asset_id": "a_pose123", "use": "copy this dance pose"}]
    spec["shots"][1]["refs"] = [{"asset_id": "a_place78", "use": "this disco hall"}]

    runner = object.__new__(prod.Run)
    runner.spec = spec
    lead = runner.shot_prompt(spec["shots"][0])
    assert lead["consistent"] and lead["reference_asset_ids"] == ["a_pose123"]
    assert "<image2>: copy this dance pose" in lead["prompt"] and lead["prefer_adapter"] is False
    scenery = runner.shot_prompt(spec["shots"][1])
    assert scenery["reference_asset_ids"] == ["a_place78"] and "<image1>: this disco hall" in scenery["prompt"]


def test_storyboard_from_sections_when_none_is_written():
    spec = {"timeline": {"storyboard": {}}, "shots": [{"key": "1", "section": "chorus"}, {"key": "2", "section": "prechorus"},
                                                      {"key": "3"}, {"key": "4", "section": "chorus"}]}
    assert prod.storyboard_for(spec) == {"chorus": ["1", "4"], "pre": ["2"]}
    spec["timeline"]["storyboard"] = {"Verse": ["3"]}
    assert prod.storyboard_for(spec) == {"Verse": ["3"]}


def test_lyrics_for_an_existing_song_and_their_sections(data_dir):
    slug = _paused(data_dir, song={"asset_id": "a_song0001"})
    out = prod.set_song_lyrics(data_dir, slug, "[Intro]\nDarkness falls\n[Chorus]\nBlow up the night\nHead in the air")
    assert out["status"] == "queued"
    st = prod.load_state(data_dir, slug)
    assert st["spec"]["song"]["lyrics"].startswith("[Intro]") and "animatic" not in st["done"]
    timing = prod.shot_timing(data_dir, None, st)
    assert [(s["label"], s["lines"]) for s in timing["sections"]] == [("Intro", ["Darkness falls"]),
                                                                       ("Chorus", ["Blow up the night", "Head in the air"])]
