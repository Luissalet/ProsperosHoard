"""Production takes: a shot can continue another one's clip from its last
frame (one continuous take), clips can be fast drafts that are promoted to
final later, and approved (locked) shots are kept when the rest is redone."""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from PIL import Image

from prosperos_hoard import productions as prod
from test_productions import tiny_spec
from test_qa import _clip, _save


class ClipStudio:
    """Every generate call makes one short clip at once and records its body."""

    def __init__(self, store):
        self.store, self.calls, self.jobs = store, [], {}
        self.ids = itertools.count(1)

    def generate(self, project_id, body):
        self.calls.append(body)
        frames = np.full((12, 64, 112), 40 + 10 * len(self.calls), dtype=np.uint8)
        aid = _clip(self.store, project_id, frames, body.get("reference_asset_id"))
        job = {"id": f"job_{next(self.ids)}", "state": "done", "outputs": {"asset_ids": [aid]}}
        self.jobs[job["id"]] = job
        return job

    def job(self, job_id):
        return self.jobs[job_id]

    def cancel(self, job_id):
        pass


def _ready(store, project, shots=None, settings=None):
    spec = tiny_spec()
    spec["shots"].append({"key": "3", "lead": True, "prompt": "the moth flies on", "width": 512, "height": 288,
                          "clips": [0], "motion": "move", "motion_prompt": "it keeps flying", "continue_from": "2"})
    if shots:
        spec["shots"] = shots
    state = prod.create_production(store.data_dir, f"Takes {len(list(store.data_dir.iterdir()))}",
                                   prod.normalise_spec(spec), settings or {}, project_id=project["id"])
    stills = {s["key"]: _save(store, project["id"], Image.new("RGB", (112, 64), (30 * i, 60, 90)))
              for i, s in enumerate(state["spec"]["shots"])}
    state["done"] = {"frames": {"complete": True, "items": {k: {"best": a, "variants": [a]} for k, a in stills.items()}}}
    prod.save_state(store.data_dir, state)
    return state["slug"], stills


def _clips(store, slug, studio):
    run = prod.Run(store, studio, slug, lambda *a, **k: None)
    run.stage = "clips"
    run.stage_clips()
    run.save()
    return prod.load_state(store.data_dir, slug)


def test_a_shot_continues_the_last_frame_of_another(store, project):
    slug, stills = _ready(store, project)
    studio = ClipStudio(store)
    state = _clips(store, slug, studio)
    entry = state["done"]["clips"]
    assert set(entry["items"]) == {"1", "2", "3"}
    third = next(b for b in studio.calls if "continues the previous shot" in b["prompt"])
    start = third["reference_asset_id"]
    assert start != stills["3"] and store.get_asset(start)["recipe"]["operation"] == "last_frame"
    assert store.get_asset(start)["recipe"]["derived_from"] == entry["items"]["2"]
    # it lands on its own still when the end-frame model is there (dropped otherwise)
    assert third["end_asset_id"] == stills["3"] and third["end_optional"] is True
    assert entry["chained"]["3"]["from"] == entry["items"]["2"]
    # a new take of shot 2 redoes shot 3, which started on its old last frame
    prod.regenerate_unlocked(store.data_dir, slug, "clips", ["2"])
    after = prod.load_state(store.data_dir, slug)["done"]["clips"]["items"]
    assert set(after) == {"1"}


def test_continue_from_must_point_back(store, project):
    spec = tiny_spec()
    spec["shots"][0]["continue_from"] = "2"
    with pytest.raises(prod.ProductionError):
        prod.normalise_spec(spec)
    slug, _ = _ready(store, project)
    with pytest.raises(prod.ProductionError):
        prod.update_shots(store.data_dir, slug, [{"key": "2", "continue_from": "2"}])
    prod.update_shots(store.data_dir, slug, [{"key": "2", "delete": True}])
    shots = prod.load_state(store.data_dir, slug)["spec"]["shots"]
    assert "continue_from" not in next(s for s in shots if s["key"] == "3")


def test_locked_shots_are_kept_when_the_rest_is_redone(store, project):
    slug, _ = _ready(store, project)
    state = _clips(store, slug, ClipStudio(store))
    before = dict(state["done"]["clips"]["items"])
    prod.update_shots(store.data_dir, slug, [{"key": "1", "locked": True}])
    with pytest.raises(prod.ProductionError) as err:
        prod.update_shots(store.data_dir, slug, [{"key": "1", "prompt": "something else"}])
    assert err.value.code == "shot_locked"
    out = prod.regenerate_unlocked(store.data_dir, slug, "frames")
    assert out["kept"] == ["1"] and set(out["regenerated"]) == {"2", "3"}
    state = prod.load_state(store.data_dir, slug)
    assert state["done"]["clips"]["items"] == {"1": before["1"]}
    assert set(state["done"]["frames"]["items"]) == {"1"} and state["status"] == "queued"
    seeds = {s["key"]: s["seed"] for s in state["spec"]["shots"]}
    assert seeds["2"] == 41 + 1000
    # unlocking makes it editable again
    prod.update_shots(store.data_dir, slug, [{"key": "1", "locked": False, "motion_prompt": "slower"}])


def test_draft_clips_then_promote_them_to_final(store, project):
    slug, _ = _ready(store, project, settings={"clip_quality": "draft"})
    studio = ClipStudio(store)
    state = _clips(store, slug, studio)
    assert {b["template"] for b in studio.calls} == {"wan22_ti2v"}
    assert not any(b.get("end_asset_id") for b in studio.calls)
    assert set(state["done"]["clips"]["quality"].values()) == {"draft"}
    out = prod.promote_clips(store.data_dir, slug, ["1"])
    assert out["promoted"] == ["1"]
    state = prod.load_state(store.data_dir, slug)
    assert state["settings"]["clip_quality"] == "final" and set(state["done"]["clips"]["items"]) == {"2", "3"}
    studio.calls.clear()
    _clips(store, slug, studio)
    assert [b["template"] for b in studio.calls] == ["auto_clip"]
    with pytest.raises(prod.ProductionError):
        prod.normalise_settings({"clip_quality": "best"})


def test_regenerate_and_promote_over_http(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Takes"}).json()["id"]
    project = {"id": pid}
    slug, _ = _ready(store, project, settings={"clip_quality": "draft"})
    _clips(store, slug, ClipStudio(store))
    r = c.post(f"/api/agent/studio_production_regenerate?production={slug}", json={"stage": "clips", "run": False})
    assert r.status_code == 200, r.text
    assert set(r.json()["regenerated"]) == {"1", "2", "3"}
    r = c.post(f"/api/productions/{slug}/promote", json={"run": False})
    assert r.status_code == 200 and r.json()["promoted"] == []
    s = c.post(f"/api/agent/studio_production_settings?production={slug}", json={"clip_quality": "draft"}).json()
    assert s["settings"]["clip_quality"] == "draft"


def test_earlier_takes_are_kept_and_can_come_back(store, project):
    slug, stills = _ready(store, project)
    studio = ClipStudio(store)
    first = dict(_clips(store, slug, studio)["done"]["clips"]["items"])
    prod.regenerate_unlocked(store.data_dir, slug, "clips", ["1"])
    second = _clips(store, slug, studio)["done"]["clips"]["items"]
    assert second["1"] != first["1"]
    state = prod.load_state(store.data_dir, slug)
    takes = prod.shot_takes(state, "1")
    assert [t["asset_id"] for t in takes["clips"]["1"]] == [first["1"], second["1"]]
    assert [t["current"] for t in takes["clips"]["1"]] == [False, True]
    assert takes["stills"][0]["current"] and takes["stills"][0]["variants"] == [stills["1"]]
    prod.update_shots(store.data_dir, slug, [{"key": "1", "take": first["1"]}])
    state = prod.load_state(store.data_dir, slug)
    assert state["done"]["clips"]["items"]["1"] == first["1"] and state["status"] == "queued"
    with pytest.raises(prod.ProductionError):
        prod.update_shots(store.data_dir, slug, [{"key": "1", "take": "a_nope"}])
    # a continuation picked by hand stays even though shot 2's clip is not the one it started from
    prod.regenerate_unlocked(store.data_dir, slug, "clips", ["3"])
    _clips(store, slug, studio)
    state = prod.load_state(store.data_dir, slug)
    old3 = prod.shot_takes(state, "3")["clips"]["3"][0]["asset_id"]
    prod.update_shots(store.data_dir, slug, [{"key": "3", "take": old3}])
    assert prod.load_state(store.data_dir, slug)["done"]["clips"]["items"]["3"] == old3


def test_a_still_take_brings_its_set_back(store, project):
    slug, stills = _ready(store, project)
    state = prod.load_state(store.data_dir, slug)
    prod.remember_take(state, "frames", "2", {"variants": ["a_old1", "a_old2"], "seed": 7})
    prod.save_state(store.data_dir, state)
    prod.update_shots(store.data_dir, slug, [{"key": "2", "take": "a_old2"}])
    frames = prod.load_state(store.data_dir, slug)["done"]["frames"]["items"]
    assert frames["2"] == {"variants": ["a_old1", "a_old2"], "best": "a_old2", "seed": 7}


def test_takes_over_http(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Takes http"}).json()["id"]
    slug, _ = _ready(store, {"id": pid})
    _clips(store, slug, ClipStudio(store))
    r = c.post(f"/api/agent/studio_production_takes?production={slug}&key=2")
    assert r.status_code == 200, r.text
    assert r.json()["shots"][0]["clips"]["2"][0]["current"] is True
    assert c.get(f"/api/productions/{slug}/takes").json()["shots"][2]["key"] == "3"
    assert c.get(f"/api/productions/{slug}/takes?key=9").status_code == 400


def test_a_retake_of_a_clip_is_a_take_of_its_shot(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Retake takes"}).json()["id"]
    slug, _ = _ready(store, {"id": pid})
    state = _clips(store, slug, ClipStudio(store))
    clip = state["done"]["clips"]["items"]["1"]
    src = store.get_asset(clip)
    fixed = store.create_asset(project_id=pid, kind="video", file_path=src["file_path"], duration_s=src.get("duration_s"),
                               source="generated", recipe={"operation": "retake", "retake_of": clip, "quality": "draft"})["id"]
    takes = c.get(f"/api/productions/{slug}/takes?key=1").json()["shots"][0]["clips"]["1"]
    assert [t.get("retake", False) for t in takes] == [False, True] and takes[1]["asset_id"] == fixed
    r = c.patch(f"/api/productions/{slug}/shots", json={"changes": [{"key": "1", "take": fixed}], "run": False})
    assert r.status_code == 200, r.text
    assert prod.load_state(store.data_dir, slug)["done"]["clips"]["items"]["1"] == fixed
