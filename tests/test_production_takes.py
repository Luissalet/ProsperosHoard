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
