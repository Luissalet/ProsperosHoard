"""Fixes from using the app end to end on the real studio: names that read
well, a cover for every project, a song's real sections, the director's
pass as its own request, and a fast status page when a render server is off."""

from __future__ import annotations

import json
import socket
import time

from PIL import Image

from prosperos_hoard import engine
from prosperos_hoard import productions as prod
from prosperos_hoard.backend import _port_open
from test_lipsync import _song
from test_qa import _save


def test_names_are_cut_between_words():
    tags = "funky disco pop, groovy slap bassline, four on the floor drums, falsetto"
    cut = engine._clip(tags, 60)
    assert cut.endswith("…") and len(cut) <= 60 and "dru…" not in cut and cut.startswith("funky disco pop")
    assert engine._clip("short", 60) == "short"
    assert engine._clip("x" * 80, 20) == "x" * 19 + "…"  # no space to cut at: a hard cut


def test_a_production_shows_a_readable_name():
    assert prod.display_name({"slug": "dont_look_back"}) == "Dont look back"
    assert prod.display_name({"slug": "x", "name": "x", "spec": {"title": "DON'T LOOK BACK"}}) == "DON'T LOOK BACK"
    assert prod.display_name({"slug": "lantern_groove", "name": "Lantern Groove"}) == "Lantern Groove"


def test_projects_without_a_cover_show_their_best_picture(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Covers"}).json()["id"]
    assert next(p for p in c.get("/api/projects").json()["items"] if p["id"] == pid)["auto_cover_asset_id"] is None
    first = _save(store, pid, Image.new("RGB", (32, 32), (10, 10, 10)))
    fav = _save(store, pid, Image.new("RGB", (32, 32), (200, 10, 10)))
    _save(store, pid, Image.new("RGB", (32, 32), (10, 200, 10)))
    store.update_asset(fav, favourite=True)
    p = next(p for p in c.get("/api/projects").json()["items"] if p["id"] == pid)
    assert p["auto_cover_asset_id"] == fav
    c.patch(f"/api/projects/{pid}", json={"cover_asset_id": first})
    p = c.get(f"/api/projects/{pid}").json()
    assert p["cover_asset_id"] == first and "auto_cover_asset_id" not in p


def test_a_song_with_timed_lyrics_shows_its_real_sections(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Sections"}).json()["id"]
    song = _song(store, {"id": pid}, 12.0)
    plain = c.post(f"/api/assets/{song}/analyze").json()
    assert all(s["label"].startswith("section") for s in plain["sections"]) and "sections_source" not in plain
    lrc = "[00:00.00][Verse]\n[00:00.50]first line\n[00:04.00][Chorus]\n[00:04.20]the hook\n[00:08.00][Outro]\n"
    engine.create_lyrics(store, pid, lrc, "timed", recipe={"operation": "time_lyrics", "derived_from": song,
                                                           "input_asset_ids": [song]})
    out = c.post(f"/api/assets/{song}/analyze").json()
    assert out["sections_source"] == "lyrics" and [s["label"] for s in out["sections"]] == ["Verse", "Chorus", "Outro"]
    assert out["sections"][1]["start_s"] == 4.0 and out["sections"][-1]["end_s"] == out["duration_s"]
    assert "timed lyrics" in out["notes"]
    agent = c.post(f"/api/agent/studio_analyze_audio?asset_id={song}").json()
    assert [s["label"] for s in agent["sections"]] == ["Verse", "Chorus", "Outro"]


def test_the_director_reviews_a_plan_on_its_own_request(client):
    c, app, _ = client
    seen = {}

    def chat(messages, max_tokens, temperature):
        seen["user"] = messages[-1]["content"]
        return json.dumps({"issues": ["el plano 2 repite el 1"], "shots": [
            {"prompt": "@Vera walks in the rain", "lead": True, "motion": "move", "motion_prompt": "slow walk"},
            {"prompt": "@Vera turns to the camera under a neon sign", "lead": True, "motion": "move",
             "motion_prompt": "turns"}]})

    app.state.short_hooks = {"chat": chat}
    draft = {"shots": [{"prompt": "@Vera walks in the rain", "lead": True, "motion": "move", "motion_prompt": "walk"},
                       {"prompt": "@Vera walks in the rain again", "lead": True, "motion": "move", "motion_prompt": "walk"}]}
    r = c.post("/api/productions/plan/critique", json={"concept": "a rainy night", "draft": draft, "lead_name": "Vera",
                                                       "lead_look": "silver bob", "notes_language": "es"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["revised"] and len(out["shots"]) == 2 and "el plano 2 repite el 1" in out["issues"]
    assert "Spanish" in seen["user"]


def test_a_render_server_that_is_off_is_known_at_once(backend_with_comfy):
    backend = backend_with_comfy
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    t0 = time.monotonic()
    assert _port_open(f"http://127.0.0.1:{port}") is False
    assert backend.pool_server_ready(f"http://127.0.0.1:{port}", ttl_s=0.0) is False
    assert time.monotonic() - t0 < 2.0
