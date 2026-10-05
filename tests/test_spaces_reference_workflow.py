"""Reference roles and clip settings must survive graph execution, not just the editor."""
import io
import math

import pytest
from PIL import Image

from prosperos_hoard import engine, spaces


class ReferenceStore:
    def get_asset(self, aid):
        return {"id": aid, "kind": "video" if aid == "dance" else "image"}


def reference_graph():
    graph = spaces.template_graph("character_outfit_motion")
    graph["nodes"][0]["data"]["asset_ids"] = ["person_ref"]
    graph["nodes"][1]["data"]["asset_ids"] = ["clothes_ref"]
    return graph


def test_roles_follow_reference_order_through_lists_and_survive_long_prompts():
    graph = reference_graph()
    graph["nodes"].append({"id": "refs", "type": "list", "data": {}})
    for edge in graph["edges"]:
        if edge["target"] == "frame":
            edge.update(target="refs", target_handle="items")
    graph["edges"].append({"source": "refs", "target": "frame", "target_handle": "refs"})
    frame = next(n for n in graph["nodes"] if n["id"] == "frame")
    frame["data"]["prompt"] = "scene " * 1000
    body = spaces.plan_node(ReferenceStore(), graph, {}, "frame")[0]["body"]
    assert body["reference_asset_ids"] == ["person_ref", "clothes_ref"]
    assert "<image1>: preserve this person's face" in body["prompt"]
    assert "<image2>: use only this clothing" in body["prompt"]
    assert len(body["prompt"]) <= 3900
    body = spaces.plan_node(ReferenceStore(), graph, {"refs": {}}, "frame")[0]["body"]
    assert len(body["reference_asset_ids"]) == 2


def test_references_are_not_silently_dropped():
    graph = reference_graph()
    graph["nodes"][0]["data"]["asset_ids"] = [f"person_{i}" for i in range(10)]
    with pytest.raises(spaces.SpaceError, match="at most 10"):
        spaces.plan_node(ReferenceStore(), graph, {}, "frame")


@pytest.mark.parametrize("quality,expected", [("draft", {"length": 73}), ("final", {"seconds": 3.0})])
def test_duration_reaches_the_selected_video_model(quality, expected):
    graph = reference_graph()
    clip = next(n for n in graph["nodes"] if n["id"] == "clip")
    clip["data"]["quality"] = quality
    body = spaces.plan_node(ReferenceStore(), graph, {"frame": {"outputs": ["frame_ref"]}}, "clip")[0]["body"]
    assert body["template_params"] == expected


def test_motion_duration_and_conflicting_guides_are_explicit():
    graph = reference_graph()
    next(n for n in graph["nodes"] if n["id"] == "motion")["data"]["asset_ids"] = ["dance"]
    state = {"frame": {"outputs": ["frame_ref"]}}
    body = spaces.plan_node(ReferenceStore(), graph, state, "clip")[0]["body"]
    assert body["driving_asset_id"] == "dance" and body["template_params"]["length"] == 73
    graph["edges"].append({"source": "person", "target": "clip", "target_handle": "end"})
    with pytest.raises(spaces.SpaceError, match="one guide"):
        spaces.plan_node(ReferenceStore(), graph, state, "clip")


@pytest.mark.parametrize("seconds", [0, 21, math.inf, math.nan, True, "invalid"])
def test_invalid_durations_fail_before_queueing(seconds):
    graph = reference_graph()
    next(n for n in graph["nodes"] if n["id"] == "clip")["data"]["seconds"] = seconds
    with pytest.raises(spaces.SpaceError, match="duration"):
        spaces.plan_node(ReferenceStore(), graph, {"frame": {"outputs": ["frame_ref"]}}, "clip")


@pytest.mark.parametrize("template,expected", [("wan22_i2v_14b", ("seconds", 3)), ("wan22_ti2v", ("length", 73))])
def test_auto_clip_preserves_duration_when_resolving_the_installed_model(client, monkeypatch, template, expected):
    c, app, _ = client
    monkeypatch.setattr(engine, "clip_template", lambda *args: template)
    project = app.state.store.create_project("Reference workflow")
    image = io.BytesIO()
    Image.new("RGB", (128, 72), "orange").save(image, "PNG")
    asset = c.post(f"/api/projects/{project['id']}/import-upload", files={"file": ("ref.png", image.getvalue(), "image/png")}).json()
    result = c.post(f"/api/projects/{project['id']}/generate", json={"prompt": "full turn", "template": "auto_clip", "reference_asset_id": asset["id"], "template_params": {"seconds": 3}})
    assert result.status_code == 200, result.text
    job = app.state.store.get_job(result.json()["job"]["id"])
    assert job["params"][expected[0]] == expected[1]
