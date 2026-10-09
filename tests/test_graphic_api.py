"""Graphic shots and style cards through the HTTP API (what the MCP tools call)."""
from __future__ import annotations

import io

from PIL import Image

from prosperos_hoard import productions as prod

from test_productions import tiny_spec, wait_job

TITLE = {"grammar": "title_card", "data": {"title": "Noche de lámparas", "subtitle": "WISP"}}


def _paused(app, **over):
    data_dir = app.state.store.data_dir
    spec = prod.normalise_spec(tiny_spec(**over))
    project = app.state.store.create_project("Graphic API")
    state = prod.create_production(data_dir, "Graphic API", spec, {"animatic": False}, project_id=project["id"])
    state["status"] = "awaiting_review"
    state["done"] = {"frames": {"complete": True, "items": {"1": {"variants": ["a_1", "a_2"], "best": "a_1"},
                                                            "2": {"variants": ["a_3"], "best": "a_3"}}},
                     "clips": {"complete": True, "items": {"1": "c_1", "2": "c_2"}}}
    prod.save_state(data_dir, state)
    return state["slug"]


def test_style_cards_can_be_listed_created_from_a_builtin_changed_and_deleted(client):
    c, app, _ = client
    cards = {x["name"]: x for x in c.get("/api/style-presets").json()["items"]}
    assert {"Neón nocturno", "Papel recortado", "VHS terror", "Tipografía suiza"} <= set(cards)
    assert cards["VHS terror"]["signature_transition"] == "glitch" and len(cards["VHS terror"]["palette"]) == 5
    listed = c.post("/api/agent/studio_style_cards", json={"action": "list"}).json()["items"]
    assert any(x["name"] == "VHS terror" and x["builtin"] and x["quality"] == 2 for x in listed)
    got = c.post("/api/agent/studio_style_cards", json={"action": "get", "id": "vhs terror"}).json()
    assert got["technique"] and got["pitfalls"] and got["typography"]["background"]["kind"] == "scanlines"
    made = c.post("/api/agent/studio_style_cards", json={"action": "create", "name": "Mi VHS", "from_card": "VHS terror", "quality": 3,
                                                          "palette": ["#000000", "#ffffff", "#ff0000"]})
    assert made.status_code == 200, made.text
    mine = made.json()
    assert not mine["builtin"] and mine["palette"][2] == "#ff0000" and mine["signature_transition"] == "glitch" and mine["quality"] == 3
    assert mine["typography"]["background"]["kind"] == "scanlines"
    changed = c.patch(f"/api/style-presets/{mine['id']}", json={"signature_transition": "wipe", "pitfalls": "no sobre vídeo claro"})
    assert changed.json()["signature_transition"] == "wipe" and changed.json()["pitfalls"] == "no sobre vídeo claro"
    assert c.post("/api/agent/studio_style_cards", json={"action": "create", "name": "Mi VHS"}).status_code == 400   # name taken
    assert c.patch(f"/api/style-presets/{cards['VHS terror']['id']}", json={"quality": 1}).status_code == 400            # read-only
    assert c.post("/api/agent/studio_style_cards", json={"action": "update", "id": mine["id"], "signature_transition": "teleport"}).status_code == 400
    assert c.post("/api/agent/studio_style_cards", json={"action": "update", "id": mine["id"], "quality": 7}).status_code == 400
    assert c.delete(f"/api/style-presets/{mine['id']}").json()["deleted"] == mine["id"]
    assert c.post("/api/agent/studio_style_cards", json={"action": "get", "id": mine["id"]}).status_code == 404


def test_the_preview_draws_an_unsaved_graphic_as_a_png(client):
    c, app, _ = client
    pid = c.post("/api/agent/studio_create_project", json={"name": "Graphics"}).json()["id"]
    r = c.post("/api/graphics/preview", json={"project": pid, "graphic": TITLE, "duration_s": 3, "aspect": "9:16", "max_side": 640})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (360, 640)
    wide = c.post("/api/graphics/preview", json={"project": pid, "graphic": TITLE, "duration_s": 3, "aspect": "16:9", "max_side": 640})
    assert Image.open(io.BytesIO(wide.content)).size == (640, 360)
    other = c.post("/api/graphics/preview", json={"project": pid, "graphic": {**TITLE, "look": {"palette": ["#ffffff", "#000000", "#ff0000"]}},
                                                  "duration_s": 3, "max_side": 640})
    assert other.content != r.content
    bad = c.post("/api/graphics/preview", json={"project": pid, "graphic": {"grammar": "nope"}, "duration_s": 3})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_graphic"
    assert c.get("/api/graphics/options").json()["grammars"] == ["kinetic_lyrics", "title_card", "lower_third", "outro_card"]


def test_a_graphic_shot_is_set_changed_and_rendered_through_the_api(client):
    c, app, _ = client
    slug = _paused(app)
    made = c.post("/api/agent/studio_graphic_shot", json={"production": slug, "grammar": "title_card", "data": {"title": "Hola"},
                                                           "start_s": 0, "end_s": 2.5, "style": "Neón nocturno", "run": False})
    assert made.status_code == 200, made.text
    shot = made.json()["shot"]
    assert shot["kind"] == "graphic" and shot["span"] == {"start_s": 0.0, "end_s": 2.5} and shot["graphic"]["style"] == "Neón nocturno"
    key = shot["key"]
    assert shot["prompt"] == "Title card: Hola"
    # change only the subtitle and the look: the rest stays
    state = prod.load_state(app.state.store.data_dir, slug)
    state["status"] = "awaiting_review"
    prod.save_state(app.state.store.data_dir, state)
    again = c.post("/api/agent/studio_graphic_shot", json={"production": slug, "key": key, "data": {"subtitle": "otra"},
                                                            "look": {"transition": "glitch"}, "run": False})
    assert again.status_code == 200, again.text
    graphic = again.json()["shot"]["graphic"]
    assert graphic["data"]["title"] == "Hola" and graphic["data"]["subtitle"] == "otra" and graphic["look"] == {"transition": "glitch"}
    # a still of it, as a job that finishes inside the call
    r = c.post("/api/agent/studio_graphic_render", json={"production": slug, "shot": key, "what": "still", "wait_s": 30})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["state"] == "done", job.get("message")
    asset = app.state.store.get_asset(job["asset_ids"][0])
    assert asset["kind"] == "image" and (asset["width"], asset["height"]) == (1080, 1920) and asset["recipe"]["shot"] == key
    # a video: frame exact from its span (2.5 s at 30 fps)
    r = c.post("/api/agent/studio_graphic_render", json={"production": slug, "shot": key, "what": "video", "aspect": "16:9", "wait_s": 60})
    job = r.json()["job"]
    assert job["state"] == "done", job
    video = app.state.store.get_asset(job["asset_ids"][0])
    assert video["kind"] == "video" and (video["width"], video["height"]) == (1920, 1080) and abs(video["duration_s"] - 2.5) < 0.05
    # and an overlay for an editor, with alpha
    r = c.post("/api/agent/studio_graphic_render", json={"production": slug, "shot": key, "what": "alpha", "wait_s": 60})
    alpha = app.state.store.get_asset(r.json()["job"]["asset_ids"][0])
    assert alpha["file_path"].endswith(".mov") and "alpha" in alpha["tags"]


def test_graphic_shot_and_render_errors_say_what_is_wrong(client):
    c, app, _ = client
    slug = _paused(app)
    post = lambda path, body: c.post("/api/agent/" + path, json=body)  # noqa: E731
    assert post("studio_graphic_shot", {"production": slug, "grammar": "title_card", "data": {"title": "x"}}).json()["error"] == "graphic_needs_span"
    r = post("studio_graphic_shot", {"production": slug, "grammar": "title_card", "data": {}, "start_s": 0, "end_s": 2, "run": False})
    assert r.status_code == 400 and "title" in r.json()["message"]
    assert post("studio_graphic_shot", {"production": slug, "key": "nope", "data": {"title": "x"}}).json()["error"] == "unknown_shot"
    assert post("studio_graphic_shot", {"production": "missing", "grammar": "title_card", "start_s": 0, "end_s": 2}).status_code == 404
    assert post("studio_graphic_render", {"production": slug, "shot": "1"}).json()["error"] == "not_a_graphic"
    assert post("studio_graphic_render", {"production": slug}).json()["error"] == "bad_render"
    assert post("studio_graphic_render", {"production": slug, "shot": "1", "what": "gif"}).json()["error"] == "bad_render"
    assert post("studio_graphic_render", {"production": slug, "shot": "1", "aspect": "5:4"}).json()["error"] == "bad_aspect"
    pid = c.post("/api/agent/studio_create_project", json={"name": "P"}).json()["id"]
    assert post("studio_graphic_render", {"project": pid, "graphic": TITLE}).json()["error"] == "bad_render"   # no duration


def test_an_inline_graphic_renders_without_a_production(client):
    c, app, _ = client
    pid = c.post("/api/agent/studio_create_project", json={"name": "Reel"}).json()["id"]
    r = c.post("/api/agent/studio_graphic_render", json={"project": pid, "graphic": TITLE, "duration_s": 2, "what": "video", "wait_s": 60})
    job = r.json()["job"]
    assert job["state"] == "done", job
    video = app.state.store.get_asset(job["asset_ids"][0])
    assert video["project_id"] == pid and abs(video["duration_s"] - 2.0) < 0.05
    lower = {"grammar": "lower_third", "data": {"name": "Ana"}}
    r = c.post("/api/agent/studio_graphic_render", json={"project": pid, "graphic": lower, "duration_s": 2, "what": "alpha", "wait_s": 60})
    assert r.json()["job"]["state"] == "done"
    assert wait_job(c, r.json()["job"]["id"])["state"] == "done"
