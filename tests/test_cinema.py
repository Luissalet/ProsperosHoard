"""The film-language guide: search by category words in both languages,
camera terms joined to prompts (moves only for clips), and the routes."""

from __future__ import annotations

from prosperos_hoard import cinema, spaces


def test_every_entry_is_complete_and_unique():
    ids = [e["id"] for e in cinema.ENTRIES]
    assert len(ids) == len(set(ids))
    for e in cinema.ENTRIES:
        assert e["category"] in cinema.CATEGORIES
        for field in ("name", "what", "when"):
            assert e[field]["en"] and e[field]["es"]
        assert e["prompt"] and e["prompt"] == e["prompt"].strip()
    assert {e["category"] for e in cinema.ENTRIES} == set(cinema.CATEGORIES)


def test_search_by_category_word_and_accents():
    shots = cinema.search("plano")
    assert shots and {e["category"] for e in shots} == {"shot"}
    assert {e["category"] for e in cinema.search("/luz")} == {"light"}
    assert cinema.search("contrapicado")[0]["id"] == "low_angle"
    assert cinema.search("contrap")[0]["id"] == "low_angle"
    assert "worms_eye" in [e["id"] for e in cinema.search("nádir")] or "worms_eye" in [e["id"] for e in cinema.search("nadir")]
    assert cinema.search("americano")[0]["id"] == "cowboy"


def test_camera_prompt_keeps_moves_for_clips():
    cam = {"shot": "close_up", "angle": "low_angle", "move": "dolly_in", "light": "rim_light", "lens": "bogus"}
    still = cinema.camera_prompt(cam)
    assert "close-up" in still and "low angle" in still and "rim light" in still and "dolly" not in still
    assert "dolly in" in cinema.camera_prompt(cam, video=True)
    assert cinema.camera_prompt({"shot": "low_angle"}) == ""  # wrong category is ignored
    assert cinema.camera_prompt(None) == ""


def test_space_nodes_add_camera_terms(client):
    c, app, _ = client
    pid = c.post("/api/projects", json={"name": "Cam"}).json()["id"]
    sp = c.post(f"/api/projects/{pid}/spaces", json={"name": "c"}).json()
    graph = {"nodes": [{"id": "p", "type": "image", "data": {"prompt": "a singer", "camera": {"shot": "medium", "move": "orbit"}}}],
             "edges": []}
    c.put(f"/api/spaces/{sp['id']}", json={"graph": graph})
    store = app.state.store if hasattr(app.state, "store") else app.state.studio_store
    space = c.get(f"/api/spaces/{sp['id']}").json()
    body = spaces.plan_node(store, space["graph"], space["state"], "p")[0]["body"]
    assert body["prompt"].startswith("a singer, medium shot") and "orbit" not in body["prompt"]


def test_cinema_routes(client):
    c, _, _ = client
    guide = c.get("/api/cinema").json()
    assert len(guide["categories"]) == 6 and len(guide["items"]) == len(cinema.ENTRIES)
    agent = c.get("/api/agent/studio_cinema", params={"q": "primer plano"}).json()
    assert agent["items"][0]["id"] == "close_up" and agent["items"][0]["prompt"]
