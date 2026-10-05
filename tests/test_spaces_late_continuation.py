"""Continuations connected after the source was generated must reuse that clip."""
import pytest

from prosperos_hoard import spaces
from test_spaces import FakeStudio, _space_with


class FrameStudio(FakeStudio):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.frames_taken = []

    def last_frame(self, clip, pid):
        self.frames_taken.append(clip)
        return self.store.create_asset(pid, "image", "tmp/frame.png", source="derived")


def setup_late_wire(client, through_list=False):
    graph = {"nodes": [
        {"id": "picture", "type": "image", "data": {"prompt": "dancer", "count": 1}},
        {"id": "first", "type": "video", "data": {"prompt": "dance"}},
        {"id": "next", "type": "video", "data": {"prompt": "continue dancing"}},
    ], "edges": [{"source": "picture", "target": "first", "target_handle": "start"}]}
    if through_list:
        graph["nodes"].append({"id": "frames", "type": "list"})
    store, sid = _space_with(client, graph)
    studio = FrameStudio(store)
    spaces.run_space(store, studio, sid, "upto", ["first"], poll_s=0)
    first = store.get_space(sid)["state"]["first"]
    assert first["status"] == "done" and "last_frames" not in first
    graph["edges"].append({"source": "first", "source_handle": "last", "target": "frames" if through_list else "next", "target_handle": "items" if through_list else "start"})
    if through_list:
        graph["edges"].append({"source": "frames", "target": "next", "target_handle": "start"})
    store.save_space_graph(sid, spaces.validate_graph(graph))
    return store, sid, studio, first


@pytest.mark.parametrize("through_list", [False, True])
def test_late_wire_prepares_frame_without_rerendering_source(client, through_list):
    store, sid, studio, first = setup_late_wire(client, through_list)
    result = spaces.run_space(store, studio, sid, "node", ["next"], poll_s=0)
    assert result == {"ran": ["next"], "skipped": [], "failed": []}
    assert len(studio.jobs) == 3  # original image, original clip, only the new clip
    assert studio.frames_taken == first["outputs"]
    after = store.get_space(sid)["state"]["first"]
    for key in ("outputs", "runs", "status", "ok_hash", "jobs"):
        assert after[key] == first[key]
    assert studio.calls[-1][1]["reference_asset_id"] == after["last_frames"][first["outputs"][0]]
    spaces.run_space(store, studio, sid, "node", ["next"], poll_s=0)
    assert studio.frames_taken == first["outputs"]  # cached valid image reused


def test_full_run_skips_old_generators_but_prepares_new_continuation(client):
    store, sid, studio, first = setup_late_wire(client)
    result = spaces.run_space(store, studio, sid, "all", poll_s=0)
    assert result["skipped"] == ["picture", "first"] and result["ran"] == ["next"]
    assert len(studio.jobs) == 3 and studio.frames_taken == first["outputs"]


def test_excluded_clips_are_not_extracted_and_missing_frames_are_repaired(client):
    store, sid, studio, first = setup_late_wire(client)
    kept = first["outputs"][0]
    store.patch_space_state(sid, "first", {"outputs": [kept, "excluded_clip"], "excluded": ["excluded_clip"], "last_frames": {kept: "missing_image"}})
    spaces.run_space(store, studio, sid, "node", ["next"], poll_s=0)
    assert studio.frames_taken == [kept]
    assert store.get_space(sid)["state"]["next"]["status"] == "done"


def test_frame_failure_keeps_source_and_does_not_queue_wrong_continuation(client):
    store, sid, studio, first = setup_late_wire(client)
    def unavailable(*args):
        raise RuntimeError("cannot decode clip")
    studio.last_frame = unavailable
    result = spaces.run_space(store, studio, sid, "node", ["next"], poll_s=0)
    assert result["failed"] == ["next"] and len(studio.jobs) == 2
    state = store.get_space(sid)["state"]
    assert state["first"] == first
    assert "last frame" in state["next"]["error"] and "cannot decode clip" in state["next"]["error"]
