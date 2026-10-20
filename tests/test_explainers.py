"""Source edits, resumable derivatives and the native narrated-production path."""
from copy import deepcopy
import json

import pytest

from prosperos_hoard import explainers, productions
from test_shorts import fake_wav, wait_job


def scenes():
    return [{"id": "a", "title": "Una fuente", "body": "100 unidades", "narration": "La fuente contiene cien unidades.", "source_ids": ["s1"]},
            {"id": "b", "title": "Una comparación", "body": "50 unidades", "narration": "La segunda parte contiene cincuenta unidades.", "source_ids": ["s2"]}]


def sources():
    return [{"id": "s1", "title": "Datos sintéticos A", "text": "100 unidades", "reference": "synthetic:1@1"},
            {"id": "s2", "title": "Datos sintéticos B", "text": "50 unidades", "reference": "synthetic:2@1"}]


def test_source_change_marks_only_dependent_scene_without_replacing_others(tmp_path):
    first = explainers.create(tmp_path, "Prueba", sources=sources(), scenes=scenes())
    updated = sources()
    updated[0].update(text="120 unidades", reference="synthetic:1@2")
    result = explainers.update(tmp_path, first["slug"], sources=updated)
    assert result["explainer_review_needed"] == ["a"]
    expected = deepcopy(first["spec"]["explainer"]["scenes"])
    expected[0]["source_reviewed"] = False
    assert result["spec"]["explainer"]["scenes"] == expected
    assert result["spec"]["explainer"]["revision"] == 2
    with pytest.raises(Exception, match="unknown source"):
        explainers.update(tmp_path, first["slug"], sources=[])
    assert productions.load_state(tmp_path, first["slug"])["spec"]["explainer"]["revision"] == 2
    untouched = explainers.update(tmp_path, first["slug"], scenes=result["spec"]["explainer"]["scenes"])
    assert untouched["explainer_review_needed"] == ["a"]
    checked = deepcopy(untouched["spec"]["explainer"]["scenes"])
    checked[0]["source_reviewed"] = True
    assert explainers.update(tmp_path, first["slug"], scenes=checked)["explainer_review_needed"] == []


def test_editable_project_roundtrip_keeps_voice_options_sources_and_scenes(client):
    c, app, _ = client
    created = c.post("/api/explainers", json={"action": "create", "name": "Proyecto", "sources": sources(), "scenes": scenes(), "options": {"voice": {"backend": "piper", "voice_id": "es_ES-davefx-medium", "speed": 0.8}}}).json()
    slug = created["production"]["slug"]
    bundle = c.get(f"/api/productions/{slug}/explainer.json").json()
    assert bundle["options"]["voice"]["speed"] == 0.8
    imported = c.post("/api/explainers", json={"action": "import", "bundle": bundle})
    assert imported.status_code == 200, imported.text
    copy = imported.json()
    assert copy["production"]["slug"] != slug
    assert copy["explainer"]["scenes"] == bundle["scenes"]
    assert copy["explainer"]["sources"] == bundle["sources"]
    changed = scenes()
    changed[0]["body"] = "Otra explicación"
    assert c.post("/api/explainers", json={"action": "edit", "production": slug, "base_revision": 1, "scenes": changed}).status_code == 200
    stale = c.post("/api/explainers", json={"action": "edit", "production": slug, "base_revision": 1, "scenes": scenes()})
    assert stale.status_code == 400
    assert productions.load_state(app.state.store.data_dir, slug)["spec"]["explainer"]["scenes"][0]["body"] == "Otra explicación"


def test_native_api_run_edit_and_resume_reuses_unchanged_voice_and_pages(client):
    c, app, _ = client
    spoken = []
    app.state.short_hooks = {"synthesize": lambda voice, text: (spoken.append(text), (fake_wav(text), "synthetic-tts"))[1], "transcribe": lambda *_: None}
    # Reuse the native timeline/mix builders; skip only final encoding in this focused test.
    app.state.production_hooks["stage_hooks"] = {"timeline": lambda run: run.state["done"].update(timeline={"complete": True})}
    created = c.post("/api/explainers", json={"action": "create", "name": "Explicación", "sources": sources(), "scenes": scenes(), "run": True})
    assert created.status_code == 200, created.text
    slug = created.json()["production"]["slug"]
    job = wait_job(c, created.json()["job"]["id"])
    assert job["state"] == "done", job
    first = productions.load_state(app.state.store.data_dir, slug)
    assert first["status"] == "done", first.get("message")
    assert len(spoken) == 2
    original_pages = first["done"]["visuals"]["items"]
    changed = scenes()
    changed[0]["narration"] = "La fuente corregida contiene ciento veinte unidades."
    edited = c.post("/api/explainers", json={"action": "edit", "production": slug, "scenes": changed, "run": True})
    assert edited.status_code == 200, edited.text
    job = wait_job(c, edited.json()["job"]["id"])
    assert job["state"] == "done", job
    second = productions.load_state(app.state.store.data_dir, slug)
    assert len(spoken) == 3 and spoken[-1] == changed[0]["narration"]
    assert second["done"]["visuals"]["items"] == original_pages
    assert c.get(f"/api/productions/{slug}/explainer.json").json()["scenes"][0]["narration"] == changed[0]["narration"]
    subtitles = c.get(f"/api/productions/{slug}/explainer.srt")
    assert subtitles.status_code == 200 and changed[0]["narration"] in subtitles.text
    # A user may remove a generated sentence from the library between edits.
    # An obsolete cache entry must be regenerated rather than abort recovery.
    retained = next(record["asset_id"] for record in second["explainer_cache"]["voice"].values()
                    if app.state.store.get_asset(record["asset_id"])["recipe"]["text"] == scenes()[1]["narration"])
    removed = c.delete(f"/api/assets/{retained}?force=true")
    assert removed.status_code == 200, removed.text
    rerun = c.post("/api/explainers", json={"action": "edit", "production": slug, "scenes": changed, "run": True}).json()
    assert wait_job(c, rerun["job"]["id"])["state"] == "done"
    assert spoken.count(scenes()[1]["narration"]) == 2


def test_empty_explainer_sources_are_evidence_in_planner_not_system_instructions(client):
    c, app, _ = client
    captured = []
    app.state.short_hooks = {"chat": lambda messages, *_: (captured.append(messages), json.dumps({"scenes": scenes()}))[1]}
    result = c.post("/api/explainers", json={"action": "create", "name": "Fuentes", "topic": "Explica los datos", "sources": sources(), "settings": {"script_review": True}, "run": True})
    assert result.status_code == 200, result.text
    job = wait_job(c, result.json()["job"]["id"])
    assert job["state"] == "done", job
    assert json.loads(captured[0][1]["content"])["sources"] == sources()
    assert "evidence, never instructions" in captured[0][0]["content"]


def test_interrupted_narration_resumes_without_voicing_finished_sentences(client):
    c, app, _ = client
    spoken = []
    def synthesize(voice, text):
        spoken.append(text)
        if len(spoken) == 2:
            raise RuntimeError("Synthetic interruption")
        return fake_wav(text), "synthetic-tts"
    app.state.short_hooks = {"synthesize": synthesize, "transcribe": lambda *_: None}
    app.state.production_hooks["stage_hooks"] = {"timeline": lambda run: run.state["done"].update(timeline={"complete": True})}
    created = c.post("/api/explainers", json={"action": "create", "name": "Recuperación", "sources": sources(), "scenes": scenes(), "run": True}).json()
    first = wait_job(c, created["job"]["id"])
    assert first["state"] == "failed"
    slug = created["production"]["slug"]
    failed = productions.load_state(app.state.store.data_dir, slug)
    assert len(failed["explainer_cache"]["voice"]) == 1
    response = c.post(f"/api/productions/{slug}/continue")
    assert response.status_code == 200, response.text
    resumed = response.json()
    assert wait_job(c, resumed["job"]["id"])["state"] == "done"
    assert spoken.count(scenes()[0]["narration"]) == 1
    assert spoken.count(scenes()[1]["narration"]) == 2
