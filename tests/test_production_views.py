"""What a production shows in a list: a cover (final cut, else animatic,
else its first still) and how many stages are done."""

from __future__ import annotations

from prosperos_hoard import productions as prod
from test_productions import tiny_spec


def test_summary_has_cover_progress_and_shot_count(data_dir):
    state = prod.create_production(data_dir, "Covers", prod.normalise_spec(tiny_spec()), {})
    view = prod.summary_view(state)
    assert view["cover"] is None and view["progress"]["done"] == 0 and view["progress"]["total"] == len(prod.stages_for(state))
    assert view["shot_count"] == 2
    state["done"] = {"character": {}, "frames": {"complete": True, "items": {"2": {"best": "a_still2"}, "1": {"best": "a_still1"}}}}
    assert prod.summary_view(state)["cover"] == {"asset_id": "a_still1", "kind": "image"}  # shot order, not dict order
    assert prod.summary_view(state)["progress"]["done"] == 2
    state["done"]["animatic"] = {"renders": {"9:16": "a_anim"}}
    assert prod.summary_view(state)["cover"] == {"asset_id": "a_anim", "kind": "video"}
    state["done"]["timeline"] = {"timelines": {"9:16": {"renders": {"preview": "a_prev", "final": "a_final"}}}}
    assert prod.summary_view(state)["cover"] == {"asset_id": "a_final", "kind": "video"}


def test_a_scripted_production_shows_its_cut_or_album_cover():
    legacy = {"slug": "old", "done": {"1": {"project_id": "p"}, "4": {"stills": {"1": {}, "2": {}}},
                                      "7": {"cover_id": "a_cover"}}}
    view = prod.summary_view(legacy)
    assert view["legacy"] and view["shot_count"] == 2 and view["cover"] == {"asset_id": "a_cover", "kind": "image"}
    legacy["done"]["8"] = {"timelines": {"9:16": {"renders": {"final": "a_cut"}}}}
    assert prod.summary_view(legacy)["cover"] == {"asset_id": "a_cut", "kind": "video"}


def test_a_lead_from_another_project_joins_this_projects_cast_at_creation(store):
    from test_productions import _asset
    home = store.create_project("Home", "where the lead was made")
    video = store.create_project("Video", "where the video is made")
    canon_id = _asset(store, home["id"])
    lead = store.create_character(home["id"], "WISP", prompt="a moth spirit", palette=["#F28C28"], canonical_asset_id=canon_id)
    spec = tiny_spec()
    spec["lead"] = {"character_id": lead["id"], "name": "WISP", "look": "a moth spirit"}
    state = prod.create_production(store.data_dir, "Adopt", prod.normalise_spec(spec), {}, project_id=video["id"])
    char = prod.adopt_lead(store, state)
    cast = store.list_characters(video["id"])
    assert [c["name"] for c in cast] == ["WISP"] and char["id"] == cast[0]["id"] != lead["id"]
    assert cast[0]["canonical_asset_id"] and cast[0]["canonical_asset_id"] != canon_id  # its own copy of the image
    saved = prod.load_state(store.data_dir, state["slug"])
    assert saved["spec"]["lead"]["character_id"] == char["id"]
    assert any(e["event"] == "copied_character" for e in saved["lineage"])
    assert prod.adopt_lead(store, saved) is None  # already in this project: nothing to do
    assert len(store.list_characters(video["id"])) == 1
