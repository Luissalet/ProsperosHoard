"""Director films: the catalog, the forgiving shot-list reader, prompt
composition, the clip bodies per engine, and a whole film through the real
API and job queue (fake ComfyUI, fake LLM/TTS), then edited shot by shot."""

from __future__ import annotations

import json

import pytest

from prosperos_hoard import director
from test_shorts import fake_wav, wait_job

PLAN_REPLY = """Here is the plan:
```json
{"title": "El faro", "description": "Una farera y la tormenta.", "hashtags": ["#faro", "cine"],
 "shots": [
  {"text": "Cada noche, Marta subía al faro.", "visual": "an old lighthouse on a cliff at dusk, a woman climbing the stairs",
   "action": "the woman climbs the spiral stairs", "framing": "wide", "move": "crane rise"},
  {"text": "Aquella noche, la tormenta llegó antes.", "visual": "storm clouds over a black sea, lighthouse beam",
   "action": "waves crash against the rocks", "framing": "Plano medio", "move": "slow dolly in",},
  {"text": "Y la luz no se apagó.", "visual": "close-up of a woman's face lit by the lighthouse lamp",
   "action": "she turns toward the light", "framing": "close", "move": "static"},
 ]}
```"""


def test_catalog_has_the_presets_axes_eras_and_engines():
    cat = director.catalog("es")
    assert len(cat["presets"]) == 28
    assert {p["category"] for p in cat["presets"]} == {"camera", "epic", "vfx", "mood"}
    assert "push-in" in cat["moves"] and "fog-roll" in cat["effects"] and "golden" in cat["effects"]
    names = {p["id"]: p["name"] for p in cat["presets"]}
    assert names["dolly-zoom"] == "Vértigo" and names["neon-rain"] == "Lluvia de neón"
    axes = {a["id"]: a for a in cat["axes"]}
    assert set(axes) == {"genre", "lens", "framing", "light", "film", "look", "grade"}
    noir = next(o for o in axes["genre"]["options"] if o["id"] == "noir")
    assert noir["name"] == "Cine negro" and noir["sets"]["film"] == "bw"
    assert [e["year"] for e in cat["eras"]] == [1960, 1970, 1980, 1990, 2000, 2010, 2020]
    assert {e["id"] for e in cat["engines"]} == {"auto", "ltx23", "wan14b", "wan"}
    assert director.catalog("en")["axes"][0]["name"] == "Genre"


def test_shot_reply_parsing_forgives_slips_and_snaps_to_the_catalog():
    out = director.parse_shot_reply(PLAN_REPLY)
    assert out["title"] == "El faro" and len(out["shots"]) == 3
    a, b, c = out["shots"]
    assert [s["id"] for s in out["shots"]] == ["p1", "p2", "p3"]
    assert (a["framing"], a["move"]) == ("wide", "crane-up")
    assert (b["framing"], b["move"]) == ("medium", "push-in")  # Spanish label and a loose move
    assert (c["framing"], c["move"]) == ("close", "static")
    # a reply that broke off still gives the shots it finished
    cut = '{"shots": [{"text": "Uno.", "visual": "a red door", "move": "orbit"}, {"text": "Dos.", "visual": "a bl'
    partial = director.parse_shot_reply(cut)
    assert [s["text"] for s in partial["shots"]] == ["Uno."] and partial["shots"][0]["move"] == "orbit-left"
    with pytest.raises(director.DirectorError):
        director.parse_shot_reply("no json here at all")


def test_genre_fills_the_auto_axes_and_prompts_compose_in_order():
    d = director.normalise_director({"picks": {"genre": "noir", "light": "neon"}, "era": 1983})
    assert d["picks"] == {"genre": "noir", "light": "neon", "film": "bw", "look": "noir", "grade": "mono"}
    shot = director.normalise_shots([{"text": "Hola.", "visual": "a detective under a streetlamp", "action": "he lights a match",
                                      "framing": "low", "move": "whip-pan", "effects": ["neon-rain", "nope"], "cast": ["@Marta"]}])[0]
    assert shot["effects"] == ["neon-rain"] and shot["cast"] == ["Marta"]
    still = director.still_prompt(shot, d["picks"], d["era"], "cinematic")
    assert still.startswith("Low-angle shot looking up, heroic. @Marta. A detective under a streetlamp.")
    # the genre reaches the still through the axes it set, not its words (they add figures to empty shots)
    assert "film noir" not in still.lower() and "black and white 400" in still and "Saturated neon light" in still
    assert "1980s look" in still and still.endswith("Cinematic.")
    clip = director.clip_prompt(shot, d["picks"], d["era"])
    assert clip.startswith("A fast whip pan with motion blur that lands on the subject. Heavy rain falls")
    assert "He lights a match." in clip and "Film noir" in clip and "Low-angle" not in clip
    for bad in ({"picks": {"genre": "western"}}, {"picks": {"framing": "wide"}}, {"era": 1900}, {"engine": "svd"},
                {"takes": 9}, {"move": "fog-roll"}):
        with pytest.raises(director.DirectorError):
            director.normalise_director(bad)


def test_split_story_without_a_model_and_plan_falls_back():
    out = director.plan(None, "Un gato despierta. Mira la ventana. Salta al tejado.", "es", 15, {})
    assert out["source"] == "split" and [s["text"] for s in out["shots"]] == ["Un gato despierta.", "Mira la ventana.",
                                                                              "Salta al tejado."]

    def broken(messages, max_tokens, temperature):
        raise RuntimeError("no model resident")

    out = director.plan(broken, "Una idea. Otra.", "es", 10, {})
    assert out["source"] == "split" and "no model resident" in out["note"]
    out = director.plan(lambda m, n, t: PLAN_REPLY, "faro", "es", 15, {"picks": {"genre": "drama"}})
    assert out["source"] == "llm" and len(out["shots"]) == 3
    messages = director.plan_messages("faro", "es", 15, director.normalise_director({"picks": {"genre": "drama"}, "cast": ["Marta"]}))
    assert "Spanish (Spain)" in messages[1]["content"] and "intimate character drama" in messages[1]["content"]
    assert "Marta" in messages[1]["content"] and "crane-up" in messages[1]["content"]


def test_video_body_per_engine():
    ltx = director.video_body("ltx23", "a_1", "p", 3.0, 7)
    assert ltx["template"] == "ltx23_i2v" and (ltx["template_params"]["length"] - 1) % 8 == 0
    wan = director.video_body("wan", "a_1", "p", 3.0, 7)
    assert wan["template"] == "wan22_ti2v" and wan["template_params"]["length"] == 73
    big = director.video_body("wan14b", "a_1", "p", 30, 7)
    assert big["template"] == "wan22_i2v_14b" and big["template_params"]["seconds"] == director.MAX_CLIP_S
    end = director.video_body("auto", "a_1", "p", 3.0, 7, end="a_2")
    assert end["template"] == "auto_clip" and end["end_asset_id"] == "a_2" and end["end_optional"] is True
    # LTX has no last-frame model: the end frame is dropped
    assert "end_asset_id" not in director.video_body("ltx23", "a_1", "p", 3.0, 7, end="a_2")


def _state(store, slug):
    return json.loads((store.data_dir / "productions" / slug / "state.json").read_text(encoding="utf-8"))


def test_director_film_end_to_end_and_shot_edits(client):
    c, app, _ = client
    calls = {"chat": 0, "tts": []}

    def fake_chat(messages, max_tokens, temperature):
        calls["chat"] += 1
        return PLAN_REPLY

    def fake_tts(voice, text):
        calls["tts"].append(text)
        return fake_wav(text), "fake-tts"

    app.state.short_hooks = {"chat": fake_chat, "synthesize": fake_tts, "transcribe": lambda path, lang: None,
                             "stock_keys": lambda: {}, "stock_client": lambda: None}

    cat = c.post("/api/agent/studio_director_catalog", json={"action": "catalog"}).json()
    assert len(cat["presets"]) == 28
    assert c.get("/api/director/catalog?lang=en").json()["axes"][0]["name"] == "Genre"

    plan = c.post("/api/agent/studio_director", json={"action": "plan", "idea": "una farera y una tormenta",
                                                      "director": {"picks": {"genre": "drama"}}}).json()
    assert plan["source"] == "llm" and len(plan["shots"]) == 3 and calls["chat"] == 1
    shots = plan["shots"]
    shots[2]["effects"] = ["golden"]

    r = c.post("/api/agent/studio_director", json={
        "action": "create", "idea": "una farera y una tormenta", "name": "El faro", "shots": shots,
        "director": {"picks": {"genre": "drama"}, "engine": "wan", "takes": 2, "sfx": True},
        "settings": {"animatic_autocontinue": True},
        "options": {"music": {"mode": "none"}, "timeline": {"aspects": ["16:9"], "qualities": ["preview"]}}})
    assert r.status_code == 200, r.text
    slug = r.json()["production"]["slug"]
    job = wait_job(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    film = c.post("/api/agent/studio_director_catalog", json={"action": "get", "production": slug}).json()["director"]
    assert film["status"] == "done", film
    assert calls["chat"] == 1  # the plan given is not rewritten
    assert [s["id"] for s in film["shots"]] == ["p1", "p2", "p3"]
    assert all(s["still"] and s["clip"] and len(s["takes"]) == 2 for s in film["shots"])
    assert film["renders"]["16:9"]["preview"] and film["title"] == "El faro"
    store = app.state.store
    state = _state(store, slug)
    assert [p["key"] for p in state["done"]["visuals"]["plan"]] == ["p1", "p2", "p3"]
    still_job = store.get_asset(film["shots"][0]["still"])["recipe"]
    assert "Wide establishing shot" in still_job["prompt"] and "character drama" not in still_job["prompt"]
    assert "large-format digital cinema camera" in still_job["prompt"]
    clip = store.get_asset(film["shots"][2]["clip"])
    assert clip["kind"] == "video" and "Warm low golden-hour sun" in clip["recipe"]["prompt"]
    assert state["done"]["mix"]["sfx"] is True
    tl = store.get_timeline(state["done"]["timeline"]["timelines"]["16:9"]["timeline_id"])
    visual = next(t for t in tl["tracks"] if t["type"] == "visual")["clips"]
    assert len(visual) == 3 and all(v["kind"] == "video" for v in visual)
    assert next(t for t in tl["tracks"] if t["type"] == "lyrics")["clips"][0]["text"].startswith("Cada")
    view = c.get(f"/api/agent/studio_production?production={slug}").json()
    assert view["director"] == {"shots": 3, "engine": "wan"}

    # a new move redoes only that clip; a new picture its still and clip; the rest stays
    before = {s["id"]: (s["still"], s["clip"]) for s in film["shots"]}
    n_tts = len(calls["tts"])
    r = c.post("/api/agent/studio_director", json={"action": "edit", "production": slug, "changes": [
        {"id": "p1", "move": "orbit-right"}, {"id": "p2", "visual": "a lighthouse beam cutting through heavy rain"}]})
    assert r.status_code == 200, r.text
    job = wait_job(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    film = c.post("/api/agent/studio_director_catalog", json={"action": "get", "production": slug}).json()["director"]
    after = {s["id"]: (s["still"], s["clip"]) for s in film["shots"]}
    assert after["p1"][0] == before["p1"][0] and after["p1"][1] != before["p1"][1]
    assert after["p2"][0] != before["p2"][0] and after["p2"][1] != before["p2"][1]
    assert after["p3"] == before["p3"]
    assert len(calls["tts"]) == n_tts  # the voice was not redone

    # an earlier take back in use; a new line and a new shot redo the voice and keep the title
    other = next(a for a in film["shots"][2]["takes"] if a != film["shots"][2]["still"])
    r = c.post("/api/agent/studio_director", json={"action": "edit", "production": slug, "changes": [
        {"id": "p3", "take": other}, {"id": "p1", "text": "Cada noche, Marta subía sola al faro."},
        {"after": "p3", "text": "Al amanecer, el mar estaba en calma.", "visual": "a calm sea at dawn", "move": "aerial pull"}]})
    assert r.status_code == 200, r.text
    assert wait_job(c, r.json()["job"]["id"])["state"] == "done"
    film = c.post("/api/agent/studio_director_catalog", json={"action": "get", "production": slug}).json()["director"]
    assert [s["id"] for s in film["shots"]] == ["p1", "p2", "p3", "p4"] and film["shots"][3]["move"] == "aerial-pull"
    assert film["shots"][2]["still"] == other and film["status"] == "done" and film["title"] == "El faro"
    assert len(calls["tts"]) > n_tts

    # the short's script editor writes the lines into the shots (one per shot) and the title
    lines = [s["text"] for s in film["shots"]]
    lines[1] = "Aquella noche, la tormenta llegó sin avisar."
    r = c.post("/api/agent/studio_production_script", json={"production": slug, "script": {
        "title": "La farera", "segments": [{"text": t} for t in lines]}})
    assert r.status_code == 200, r.text
    assert wait_job(c, r.json()["job"]["id"])["state"] == "done"
    film = c.post("/api/agent/studio_director_catalog", json={"action": "get", "production": slug}).json()["director"]
    assert film["shots"][1]["text"] == lines[1] and film["title"] == "La farera" and film["status"] == "done"
    r = c.post("/api/agent/studio_production_script", json={"production": slug, "script": "Solo una frase."})
    assert r.status_code == 400 and "shot_count_changed" in r.text

    # guards
    assert c.post("/api/agent/studio_director", json={"action": "edit", "production": slug,
                                                       "changes": [{"id": "nope", "text": "x"}]}).status_code == 400
    assert c.post("/api/agent/studio_director", json={"action": "create", "idea": "x",
                                                       "director": {"engine": "svd"}}).status_code == 400
    assert c.post("/api/agent/studio_director_catalog", json={"action": "edit", "production": slug}).status_code == 400
