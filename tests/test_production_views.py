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
