"""Tests for timecoded shot reviews, revision tracking, and selective dependent regeneration.

Verifies:
1. Comments linked to instant (time_s) and shot revision.
2. Comments survive reopening and preserve their historical revision.
3. Selective regeneration redoes only the target shot and downstream dependent chain,
   strictly preserving approved/locked and independent shots.
4. Stale comment detection against current shot revision.
"""

from __future__ import annotations

from pathlib import Path
from copy import deepcopy

import pytest

from prosperos_hoard.productions import create_production, load_state
from prosperos_hoard.shot_review import (
    add_shot_comment,
    get_shot_revision,
    list_shot_comments,
    load_shot_reviews,
    regenerate_dependent_shots,
    resolve_shot_comment,
)


def test_ordinary_shot_edit_invalidates_comment_identity(sample_production):
    from prosperos_hoard import productions as prod
    data_dir, slug = sample_production
    before = add_shot_comment(data_dir, slug, "2", 4, "Change the camera")
    prod.update_shots(data_dir, slug, [{"key": "2", "motion_prompt": "Slow camera pan"}])
    comment = list_shot_comments(data_dir, slug)[0]
    assert comment["id"] == before["id"]
    assert comment["revision_id"] != comment["current_revision_id"]
    assert comment["is_stale"] is True


def test_retake_retains_actual_previous_takes(sample_production):
    from prosperos_hoard import productions as prod
    data_dir, slug = sample_production
    regenerate_dependent_shots(data_dir, slug, ["2"], stage="frames")
    takes = prod.shot_takes(prod.load_state(data_dir, slug), "2")
    assert any("asset_f2" in t["variants"] for t in takes["stills"])
    assert any(t["asset_id"] == "asset_c2" for t in takes["clips"]["2"])


def test_pipeline_keeps_an_approved_continuation_after_source_changes(sample_production):
    from prosperos_hoard import productions as prod
    data_dir, slug = sample_production
    state = prod.load_state(data_dir, slug)
    state["spec"]["shots"][2]["locked"] = True
    state["done"]["clips"]["items"]["2"] = "new_source_take"
    assert prod.drop_stale_chains(state) == []
    assert state["done"]["clips"]["items"]["3"] == "asset_c3"


def test_apply_profile_preserves_approved_takes_and_unrelated_shot_fields(sample_production):
    from prosperos_hoard import productions as prod
    from prosperos_hoard.style_profiles import apply_style_profile_to_production
    data_dir, slug = sample_production
    state = prod.load_state(data_dir, slug)
    state["spec"]["shots"][1]["refs"] = [{"asset_id": "a_12345678", "use": "costume"}]
    prod.save_state(data_dir, state)
    before = deepcopy(state)
    profile = {"id": "sp_test", "name": "Timed profile", "shots": [
        {"relative_duration": 1, "camera": {"shot_type": "wide"}},
        {"relative_duration": 2, "camera": {"shot_type": "close_up"}}]}
    apply_style_profile_to_production(data_dir, slug, profile, {"target_duration_s": 12})
    after = prod.load_state(data_dir, slug)
    assert len(after["spec"]["shots"]) == 4
    for i in (0, 3):
        assert after["spec"]["shots"][i] == before["spec"]["shots"][i]
        key = str(i + 1)
        assert after["done"]["clips"]["items"][key] == before["done"]["clips"]["items"][key]
    assert after["spec"]["shots"][1]["refs"] == before["spec"]["shots"][1]["refs"]
    assert after["spec"]["shots"][2]["continue_from"] == "2"
    assert after["spec"]["shots"][1]["start_s"] == 3
    assert after["spec"]["shots"][2]["end_s"] == 9
    assert "2" not in after["done"]["frames"]["items"]
    assert prod.shot_takes(after, "2")["clips"]["2"][0]["asset_id"] == "asset_c2"


@pytest.mark.parametrize("instant", [-1, float("nan"), float("inf"), 8])
def test_comment_requires_an_instant_in_its_shot(sample_production, instant):
    from prosperos_hoard.shot_review import ShotReviewError
    data_dir, slug = sample_production
    with pytest.raises(ShotReviewError, match="timestamp"):
        add_shot_comment(data_dir, slug, "2", instant, "Invalid time")


@pytest.fixture
def sample_production(tmp_path: Path) -> tuple[Path, str]:
    data_dir = tmp_path / "data"
    slug = "test-video-review"
    spec = {
        "title": "Reviewable Production",
        "lead": {"name": "Protagonist", "look": "Sci-fi explorer with futuristic visor"},
        "shots": [
            {
                "key": "1",
                "prompt": "Shot 1 intro (approved)",
                "seed": 1001,
                "clip_seed": 2001,
                "locked": True,
                "start_s": 0.0, "end_s": 3.0,
                "clips": [0],
            },
            {
                "key": "2",
                "prompt": "Shot 2 action",
                "seed": 1002,
                "clip_seed": 2002,
                "locked": False,
                "start_s": 3.0, "end_s": 6.0,
                "clips": [0],
            },
            {
                "key": "3",
                "prompt": "Shot 3 follow-through continuation",
                "seed": 1003,
                "clip_seed": 2003,
                "locked": False,
                "continue_from": "2",
                "start_s": 6.0, "end_s": 9.0,
                "clips": [0],
            },
            {
                "key": "4",
                "prompt": "Shot 4 finale (approved)",
                "seed": 1004,
                "clip_seed": 2004,
                "locked": True,
                "start_s": 9.0, "end_s": 12.0,
                "clips": [0],
            },
        ],
    }
    create_production(data_dir, "Test Video Review", spec, slug=slug)

    # Populate state with finished items for frames and clips
    state = load_state(data_dir, slug)
    state["done"]["frames"] = {
        "complete": True,
        "items": {
            "1": {"best": "asset_f1", "variants": ["asset_f1"]},
            "2": {"best": "asset_f2", "variants": ["asset_f2"]},
            "3": {"best": "asset_f3", "variants": ["asset_f3"]},
            "4": {"best": "asset_f4", "variants": ["asset_f4"]},
        },
    }
    state["done"]["clips"] = {
        "complete": True,
        "items": {
            "1": "asset_c1",
            "2": "asset_c2",
            "3": "asset_c3",
            "4": "asset_c4",
        },
        "chained": {
            "3": {"from": "asset_c2", "start": "frame_start_3"},
        },
    }
    from prosperos_hoard.productions import save_state
    save_state(data_dir, state)

    return data_dir, slug


def test_shot_comments_persist_and_survive_reopen(sample_production: tuple[Path, str]):
    data_dir, slug = sample_production

    # 1. Add timecoded comment to shot 1 at 1.5s
    c1 = add_shot_comment(
        data_dir,
        slug,
        shot_key="1",
        time_s=1.5,
        content="Tone looks great, keep this lighting.",
        author="director",
        kind="approved",
    )
    assert c1["shot_key"] == "1"
    assert c1["time_s"] == 1.5
    assert c1["shot_revision"] == 1
    assert c1["resolved"] is False

    # 2. Add timecoded comment to shot 2 at 4.2s requesting change
    c2 = add_shot_comment(
        data_dir,
        slug,
        shot_key="2",
        time_s=4.2,
        content="Subject moves too fast, slow down motion.",
        author="editor",
        kind="request_change",
    )
    assert c2["shot_key"] == "2"
    assert c2["time_s"] == 4.2
    assert c2["shot_revision"] == 1

    # 3. Simulate reopen by reading fresh from disk
    reviews = load_shot_reviews(data_dir, slug)
    comments = reviews["comments"]
    assert len(comments) == 2
    assert comments[0]["id"] == c1["id"]
    assert comments[0]["shot_revision"] == 1
    assert comments[1]["id"] == c2["id"]
    assert comments[1]["shot_revision"] == 1

    # 4. Resolve comment 1
    res = resolve_shot_comment(data_dir, slug, c1["id"], note="Acknowledged by team")
    assert res["resolved"] is True
    assert res["resolution_note"] == "Acknowledged by team"

    # Verify resolution persists on reopen
    reloaded_comments = list_shot_comments(data_dir, slug)
    c1_reloaded = next(c for c in reloaded_comments if c["id"] == c1["id"])
    assert c1_reloaded["resolved"] is True


def test_selective_dependent_regeneration_preserves_approved_shots(sample_production: tuple[Path, str]):
    """Verifies that correcting shot 2 regenerates shot 2 and its dependent continuation shot 3,
    while leaving approved/locked shots 1 and 4 completely untouched.
    """
    data_dir, slug = sample_production

    # Add comment to shot 2
    add_shot_comment(data_dir, slug, shot_key="2", time_s=4.0, content="Recolor background")

    # Initial revisions
    assert get_shot_revision(data_dir, slug, "1") == 1
    assert get_shot_revision(data_dir, slug, "2") == 1
    assert get_shot_revision(data_dir, slug, "3") == 1
    assert get_shot_revision(data_dir, slug, "4") == 1

    # Regenerate dependent shots for target "2"
    result = regenerate_dependent_shots(data_dir, slug, target_keys=["2"], reason="Address background comment")

    # Target: 2. Dependent: 3 (because 3 has continue_from="2")
    assert result["targets"] == ["2"]
    assert set(result["regenerated_shots"]) == {"2", "3"}
    assert set(result["preserved_shots"]) == {"1", "4"}

    state = load_state(data_dir, slug)
    shots = {s["key"]: s for s in state["spec"]["shots"]}
    clips_items = state["done"]["clips"]["items"]

    # Approved/independent shots 1 and 4 are UNTOUCHED
    assert shots["1"]["seed"] == 1001
    assert shots["1"]["clip_seed"] == 2001
    assert clips_items["1"] == "asset_c1"

    assert shots["4"]["seed"] == 1004
    assert shots["4"]["clip_seed"] == 2004
    assert clips_items["4"] == "asset_c4"

    # Regenerated shots 2 and 3 had seeds changed and items evicted
    assert shots["2"]["clip_seed"] == 3002  # 2002 + 1000
    assert "2" not in clips_items

    assert shots["3"]["clip_seed"] == 3003  # 2003 + 1000
    assert "3" not in clips_items

    # Revision counter incremented for regenerated shots
    assert get_shot_revision(data_dir, slug, "2") == 2
    assert get_shot_revision(data_dir, slug, "3") == 2
    assert get_shot_revision(data_dir, slug, "1") == 1
    assert get_shot_revision(data_dir, slug, "4") == 1

    # Comment on shot 2 should now be marked stale because shot revision advanced
    comments_shot_2 = list_shot_comments(data_dir, slug, shot_key="2")
    assert len(comments_shot_2) == 1
    assert comments_shot_2[0]["shot_revision"] == 1
    assert comments_shot_2[0]["current_shot_revision"] == 2
    assert comments_shot_2[0]["is_stale"] is True
