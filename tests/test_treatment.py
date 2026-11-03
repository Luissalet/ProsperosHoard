"""Treatment-first productions: the draft (model or rules), edits, the approval gate and the storyboard sheet."""
from __future__ import annotations

import json
from types import SimpleNamespace

from prosperos_hoard import productions as productions_mod
from prosperos_hoard import treatment as tm
from prosperos_hoard.store import Store

SPEC = {"title": "Neon Rain", "brief": "a courier crosses a flooded city at night",
        "lead": {"name": "Ana", "look": "red raincoat"}, "world": {"look": "neon, rain"},
        "shots": [{"key": "1", "prompt": "wide establishing shot of a flooded neon street", "section": "intro"},
                  {"key": "2", "prompt": "close-up of Ana's face in the rain", "section": "verse", "motion": "still"},
                  {"key": "3", "prompt": "Ana runs across a bridge", "section": "chorus"}]}
REPLY = {"logline": "A courier races the tide.", "structures": [{"name": "A", "summary": "s", "beats": ["b"]},
         {"name": "B", "summary": "s"}, {"name": "C", "summary": "s"}], "pick": {"index": 2, "why": "contrast"},
         "benchmark": {"learn": ["x"], "avoid": ["y"]},
         "shot_list": [{"key": "1", "framing": "wide", "camera": "crane down", "duration_s": 4, "why": "set the world"},
                       {"key": "2", "framing": "close-up", "camera": "locked", "duration_s": 99999, "why": "her fear"}],
         "beat_sheet": [{"t_s": 12, "section": "chorus", "cue": "run"}, {"t_s": 0, "section": "intro", "cue": "rain"}]}


def _state():
    return {"slug": "neon", "name": "Neon", "spec": json.loads(json.dumps(SPEC)), "done": {}, "review": {}}


def test_rules_draft_follows_the_planned_shots():
    t = tm.rules_draft(_state())
    assert [s["key"] for s in t["shot_list"]] == ["1", "2", "3"]
    assert t["shot_list"][0]["framing"] == "extreme wide" and t["shot_list"][1]["framing"] == "close-up"
    assert t["shot_list"][1]["camera"] == "locked off"
    assert len(t["structures"]) == 3 and t["beat_sheet"][0]["section"] == "intro"


def test_model_draft_is_normalised_and_a_failing_model_falls_back():
    t = tm.draft(lambda m, n, temp: "```json\n" + json.dumps(REPLY) + "\n```", _state())
    assert t["source"] == "model" and t["pick"]["index"] == 2
    assert t["shot_list"][1]["duration_s"] == 120                   # clamped
    assert [b["t_s"] for b in t["beat_sheet"]] == [0, 12]           # sorted

    def down(*_a):
        raise RuntimeError("no model answers")
    t = tm.draft(down, _state())
    assert t["source"] == "rules" and "no model answers" in t["note"]


def test_the_treatment_stage_pauses_until_approved():
    state = _state()
    assert "treatment" not in productions_mod.stages_for(state)
    state["treatment"] = tm.draft(None, state)
    assert productions_mod.stages_for(state)[0] == "treatment"
    run = SimpleNamespace(state=state, log=lambda *a, **k: None)
    assert productions_mod.Run.stage_treatment(run) == "pause"
    tm.approve(state)
    assert productions_mod.Run.stage_treatment(run) is None
    assert productions_mod.stage_status(state, "treatment") == "done"
    edited = dict(state["treatment"], logline="New logline")
    tm.set_treatment(state, edited)                                  # an edit after approval needs approving again
    assert state["treatment"]["status"] == "draft" and productions_mod.stage_status(state, "treatment") == "pending"


def test_sheet_mixes_stills_and_placeholders():
    t = tm.rules_draft(_state())
    from PIL import Image
    sheet = tm.sheet_image(t, {"2": Image.new("RGB", (640, 360), (200, 30, 30))}, "Neon")
    assert sheet.width > 3 * 480 and sheet.height > 270
    many = dict(t, shot_list=[dict(t["shot_list"][0], key=str(i)) for i in range(20)])
    assert [s["key"] for s in tm.key_shots(many)][0] == "0" and len(tm.key_shots(many)) == 9
    assert tm.key_shots(many)[-1]["key"] == "19"


def test_routes_draft_edit_approve_and_storyboard(client, data_dir):
    c, app, _ = client
    app.state.short_hooks = {"chat": lambda m, n, temp: json.dumps(REPLY)}
    store = Store(data_dir)
    project = store.create_project("Neon", brief="b")
    productions_mod.create_production(data_dir, "neon", SPEC, project_id=project["id"])
    r = c.post("/api/agent/production_treatment_draft", params={"production": "neon"}, json={"action": "draft"})
    assert r.status_code == 200, r.text
    assert r.json()["treatment"]["source"] == "model"
    view = c.get("/api/agent/studio_production", params={"production": "neon"}).json()
    assert view["stages"]["treatment"] == "pending" and view["treatment"]["shots"] == 2
    t = r.json()["treatment"]
    t["logline"] = "Edited"
    r = c.post("/api/productions/neon/treatment", json={"action": "update", "treatment": t})
    assert r.json()["treatment"]["logline"] == "Edited"
    r = c.post("/api/productions/neon/storyboard-sheet", json={"generate": False})
    assert r.status_code == 200, r.text
    sheet = r.json()
    assert sheet["shots"] == 2 and sheet["placeholders"] == 2 and store.get_asset(sheet["asset_id"])["kind"] == "image"
    r = c.post("/api/productions/neon/treatment", json={"action": "approve", "run": False})
    assert r.json()["treatment"]["status"] == "approved"
    state = productions_mod.load_state(data_dir, "neon")
    assert state["done"]["treatment"]["shots"] == 2 and state["treatment"]["storyboard"]["asset_id"] == sheet["asset_id"]
