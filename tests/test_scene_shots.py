import hashlib
import io
import json

import numpy as np
import pytest
from PIL import Image

from prosperos_hoard import engine, productions as prod, scene_shots
from test_agent_accountability import Agent
from test_qa import _clip, _production, _save


def checks(verdict="pass"):
    return [{"position": position, "checks": [{"verdict": verdict if position == "middle" else "pass",
                                               "note": "Observed continuity"}]}
            for position in scene_shots.POSITIONS]


@pytest.fixture
def draft(store, project):
    pid = project["id"]
    dossier = {"scene": {"text": "A walker reaches the landing."}, "preserveDetails": ["Keep the case on her left."],
               "subject": {"ref": "hoard://writer/writing/example"}}
    state = _production(store, project, [{"prompt": "Draft"}], {"qa": {"enabled": False}},
                        production_dossier=dossier, dossier_reference_assets={})
    still = _save(store, pid, Image.new("RGB", (96, 64), "tan"))
    anchor = _save(store, pid, Image.new("RGB", (96, 64), "sienna"))
    rows = [{"framing": framing, "action": "A short movement", "duration_s": 3.5,
             "first_frame_asset_id": still, "last_frame_asset_id": anchor} for framing in ("wide", "medium", "close")]
    return state["slug"], still, rows


class Studio:
    def __init__(self, store):
        self.store = store
        self.jobs = {}
        self.calls = []
        self.timelines = []

    def job(self, job_id):
        return self.jobs[job_id]

    def generate(self, project_id, body):
        self.calls.append(body)
        values = np.repeat(np.linspace(35, 220, 96, dtype=np.uint8), 64 * 96).reshape(96, 64, 96)
        asset = _clip(self.store, project_id, values, template=body["template"])
        return self._done(asset)

    def render(self, timeline_id, quality):
        timeline = self.store.get_timeline(timeline_id)
        self.timelines.append(timeline)
        return self._done(timeline["tracks"][0]["clips"][0]["asset_id"])

    def _done(self, aid):
        key = f"job_test_{len(self.jobs)}"
        job = self.jobs[key] = {"id": key, "state": "done", "outputs": {"asset_ids": [aid]}}
        return job


def test_native_pipeline_regenerates_one_scene_shot_and_assembles_in_order(store, project, draft):
    slug, sheet, rows = draft
    rows[0].pop("last_frame_asset_id")
    rows[0]["control_asset_id"] = _clip(store, project["id"], np.full((96, 64, 96), 42))
    plan = scene_shots.apply_plan(store, slug, sheet, "", [], rows, "16:9")
    assert plan["preserve_details"] == ["Keep the case on her left."]
    studio = Studio(store)

    def run():
        assert prod.run_production(store, studio, slug, lambda *_: None)["status"] == "done"
        return scene_shots.view(prod.load_state(store.data_dir, slug))["piece"]

    first = run()
    assert first["type"] == "video-clip" and first["duration_s"] == 10.5
    assert first["dossier_ref"] == "hoard://writer/writing/example"
    assert [c["template"] for c in studio.calls] == ["ltx23_control", "wan22_flf2v", "wan22_flf2v"]
    assert studio.calls[0]["driving_asset_id"] == rows[0]["control_asset_id"]
    assert studio.calls[1]["end_asset_id"] == rows[1]["last_frame_asset_id"]
    assert "Keep the case on her left" in studio.calls[1]["prompt"]
    original = first["shots"][1]["clip_asset_id"]
    review = scene_shots.record(store, slug, "2", original, checks("defect"), "reviewer")
    assert review["status"] == "defect"
    assert [s["position"] for s in review["samples"]] == ["first", "middle", "last"]
    pixels = [Image.open(io.BytesIO(scene_shots.evidence(store, slug, "2", original, p))).getpixel((5, 5))[0]
              for p in scene_shots.POSITIONS]
    assert pixels[0] < pixels[1] < pixels[2]
    assert prod.regenerate_unlocked(store.data_dir, slug, "clips", ["2"])["regenerated"] == ["2"]
    second = run()
    assert len(studio.calls) == 4 and studio.calls[3]["seed"] != studio.calls[1]["seed"]
    assert [s["key"] for s in second["shots"]] == ["1", "2", "3"]
    assert second["shots"][0] == first["shots"][0] and second["shots"][2] == first["shots"][2]
    assert second["shots"][1]["clip_asset_id"] != original
    assert scene_shots.view(prod.load_state(store.data_dir, slug))["shots"][1]["review"] is None
    assert scene_shots.evidence(store, slug, "2", original, "middle")
    assert [(c["asset_id"], c["source_fit"]) for c in studio.timelines[1]["tracks"][0]["clips"]] == [
        (s["clip_asset_id"], "error") for s in second["shots"]]
    for shot in second["shots"]:
        clip = store.get_asset(shot["clip_asset_id"])
        assert shot["sha256"] == hashlib.sha256((store.data_dir / clip["file_path"]).read_bytes()).hexdigest()


def test_reviews_use_the_sheet_and_distinguish_missing_evidence(store, project, draft):
    slug, sheet, rows = draft
    seen = []

    def vision(images, prompt):
        seen.append((len(images), prompt))
        return json.dumps({"samples": checks("unclear")})

    scene_shots.apply_plan(store, slug, sheet, "", [], rows, "16:9")
    prod.run_production(store, Studio(store), slug, lambda *_: None)
    assert scene_shots.review(store, slug, "1", None, "none")["status"] == "unavailable"
    result = scene_shots.review(store, slug, "1", vision, "fake")
    assert result["status"] == "unclear"
    assert len(result["samples"]) == 3 and seen[0][0] == 4
    assert "her left" in seen[0][1]
    with pytest.raises(engine.EngineError, match="current clip changed"):
        scene_shots.record(store, slug, "1", "a_superseded", checks(), "reviewer")
    with pytest.raises(engine.EngineError, match="needs a verdict"):
        scene_shots.record(store, slug, "1", result["clip_asset_id"],
                           [{"position": p, "checks": []} for p in scene_shots.POSITIONS], "reviewer")


def test_scene_agent_catalog_routes_and_undo(client):
    http, app, _ = client
    agent = Agent(http, app)
    store = agent.store
    project = store.create_project("Test scene")
    state = _production(store, project, [{"prompt": "Original"}])
    slug = state["slug"]
    aid = _save(store, project["id"], Image.new("RGB", (96, 64), "tan"))
    rows = [{"framing": "wide", "action": "Look left", "duration_s": 2,
             "first_frame_asset_id": aid, "last_frame_asset_id": aid}]
    payload = {"production": slug, "character_sheet_asset_id": aid, "shots": rows,
               "description": "A scene", "preserve_details": ["a brown suitcase"]}
    agent.ok("studio_scene_plan", payload, session="scene-plan")
    assert agent.ok("studio_scene", {"production": slug}, reason=None)["shots"][0]["framing"] == "wide"
    output = prod.run_production(store, Studio(store), slug, lambda *_: None)
    assert output["status"] == "done"
    clip = agent.ok("studio_scene", {"production": slug}, reason=None)["shots"][0]["clip_asset_id"]
    arguments = {"production": slug, "key": "1", "clip_asset_id": clip, "samples": checks("defect")}
    agent.ok("studio_scene_record_review", arguments, session="scene-review")
    response = http.get(f"/api/productions/{slug}/scene/1/sample/{clip}/last.jpg")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    agent.undone("scene-review", confirm=True)
    assert agent.ok("studio_scene", {"production": slug}, reason=None)["shots"][0]["review"] is None
    agent.ok("studio_scene_revise_shot", {"production": slug, "key": "1", "changes": {"action": "Look ahead"}},
             session="scene-revise")
    assert agent.ok("studio_scene", {"production": slug}, reason=None)["shots"][0]["clip_asset_id"] is None
    agent.undone("scene-revise", confirm=True)
    shot = agent.ok("studio_scene", {"production": slug}, reason=None)["shots"][0]
    assert shot["clip_asset_id"] == clip and shot["action"] == "Look left"


def test_scene_plan_undo_restores_dossier_draft(client):
    http, app, _ = client
    agent = Agent(http, app)
    store = agent.store
    project = store.create_project("Test scene")
    state = _production(store, project, [{"prompt": "Original"}])
    slug = state["slug"]
    aid = _save(store, project["id"], Image.new("RGB", (96, 64), "tan"))
    rows = [{"framing": "medium", "action": "Look left", "duration_s": 2,
             "first_frame_asset_id": aid, "last_frame_asset_id": aid}]
    payload = {"production": slug, "character_sheet_asset_id": aid, "shots": rows,
               "description": "A scene", "preserve_details": ["left prop"]}
    agent.ok("studio_scene_plan", payload, session="scene-plan")
    agent.undone("scene-plan", confirm=True)
    assert prod.load_state(store.data_dir, slug)["spec"]["shots"][0]["prompt"] == "Original"
