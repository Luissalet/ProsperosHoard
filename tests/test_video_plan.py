"""Music video from a concept: plan (local model) -> edited draft -> production."""

from __future__ import annotations

import json

from prosperos_hoard import mv_planner

REPLY = json.dumps({
    "title": "Disco Night", "world_look": "70s disco, mirror ball light, saturated magenta and teal",
    "world_negative": "text, watermark",
    "song": {"tags": "disco funk, four on the floor, male falsetto", "lyrics": "[Verse]\nlights on\n[Chorus]\ndance", "bpm": 118,
             "key": "E minor"},
    "shots": [{"prompt": "dancing on a lit floor, wide shot", "lead": True, "motion": "move", "motion_prompt": "spins",
               "section": "chorus"},
              {"prompt": "mirror ball close-up", "lead": False, "motion": "still", "section": "Intro"}],
})


def test_parse_and_normalise_a_reply_with_chatter():
    draft = mv_planner.normalise_draft(mv_planner._json_object("<think>hmm</think>Sure! ```json\n" + REPLY + "\n```"),
                                       True, "en", 90)
    assert draft["title"] == "Disco Night" and len(draft["shots"]) == 2
    assert draft["shots"][1]["section"] == "intro" and draft["shots"][1]["motion"] == "still"
    assert draft["song"]["bpm"] == 118 and draft["song"]["language"] == "en" and draft["song"]["duration"] == 90


def test_spec_from_draft_clip_choices():
    draft = mv_planner.normalise_draft(json.loads(REPLY), True, "es", 120)
    lead = {"name": "Nova", "look": "a paper lantern"}
    spec = mv_planner.spec_from_draft(draft, lead=lead, clips="all")
    assert [s["clips"] for s in spec["shots"]] == [[0], []]  # a still shot never gets a clip
    assert spec["song"]["count"] == 2 and spec["world"]["look"].startswith("70s")
    assert [s["clips"] for s in mv_planner.spec_from_draft(draft, lead=lead, clips="none")["shots"]] == [[], []]
    reuse = mv_planner.spec_from_draft(draft, lead=lead, song_asset_id="a_song")
    assert reuse["song"] == {"asset_id": "a_song"}


def test_plan_and_create_through_the_api(client):
    c, app, _ = client
    seen = {}

    def chat(messages, max_tokens, temperature):
        seen["user"] = messages[-1]["content"]
        return REPLY

    app.state.short_hooks = {"chat": chat}
    pid = c.post("/api/projects", json={"name": "MV"}).json()["id"]
    char = c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "kind": "character", "name": "Nova",
                                                                  "fields": {"prompt": "a glowing paper lantern creature"}}).json()
    char_id = char.get("id") or char.get("character", {}).get("id")
    r = c.post("/api/productions/plan", json={"concept": "a disco night", "character_id": char_id, "shots": 2,
                                              "language": "es", "genre": "disco"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lead"]["name"] == "Nova" and body["draft"]["song"]["language"] == "es"
    assert "Spanish (Spain)" in seen["user"] and "disco" in seen["user"] and "Plan 2 shots" in seen["user"]
    draft = body["draft"]
    draft["shots"][0]["prompt"] = "dancing alone on a lit floor, wide shot"  # the person edits the draft
    r = c.post("/api/productions/from-plan", json={"name": "Disco Night", "draft": draft, "character_id": char_id,
                                                   "clips": "lead", "aspects": ["9:16"]})
    assert r.status_code == 200, r.text
    prod = r.json()["production"]
    state = json.loads((app.state.store.data_dir / "productions" / prod["slug"] / "state.json").read_text(encoding="utf-8"))
    assert state["spec"]["shots"][0]["prompt"] == "dancing alone on a lit floor, wide shot"
    assert state["spec"]["lead"]["character_id"] == char_id and state["project_id"] == pid
    assert state["spec"]["timeline"]["aspects"] == ["9:16"]


def test_plan_without_a_model_says_so(client):
    c, app, _ = client

    def chat(messages, max_tokens, temperature):
        raise ConnectionError("no model resident")

    app.state.short_hooks = {"chat": chat}
    r = c.post("/api/productions/plan", json={"concept": "x", "lead_name": "A", "lead_look": "b"})
    assert r.status_code == 400 and r.json()["error"] == "no_llm" and "write the shots yourself" in r.json()["message"]


def test_lyrics_in_the_wrong_language_are_asked_again():
    english = json.loads(REPLY)
    english["song"]["lyrics"] = "[Verse]\nWalking through the night and the light in my eyes\nI am here and you are all I need\n[Chorus]\nLift me up to the sky where the colors never die"
    calls = []

    def chat(messages, max_tokens, temperature):
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return json.dumps(english)
        return json.dumps({"lyrics": "[Verse]\nCamino por la noche con la luz en los ojos\nEstoy aquí y tú eres todo\n[Chorus]\nLlévame al cielo donde nunca muere el color"})

    draft = mv_planner.plan(chat, concept="x", lead_name="A", lead_look="b", n_shots=2, language="es")
    assert len(calls) == 2 and "Spanish (Spain)" in calls[1]
    assert draft["song"]["lyrics"].startswith("[Verse]\nCamino") and "warnings" not in draft
    assert "IMPORTANT: every sung line of the lyrics is in Spanish (Spain)" in calls[0]
    assert mv_planner.looks_english(english["song"]["lyrics"]) and not mv_planner.looks_english(draft["song"]["lyrics"])
