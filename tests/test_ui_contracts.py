"""Server behaviour the web UI relies on (found in a UI audit)."""
from __future__ import annotations


def test_character_canonical_clears_with_explicit_null(store, project):
    asset = store.create_asset(project["id"], "image", "assets/c.png", source="import")
    char = store.create_character(project["id"], "Aria", canonical_asset_id=asset["id"])
    assert char["canonical_asset_id"] == asset["id"]
    # other None fields still mean "unchanged"
    kept = store.update_character(char["id"], bio=None, canonical_asset_id=asset["id"])
    assert kept["canonical_asset_id"] == asset["id"]
    cleared = store.update_character(char["id"], canonical_asset_id=None)
    assert cleared["canonical_asset_id"] is None
    # an empty string clears too (never stored as "")
    store.update_character(char["id"], canonical_asset_id=asset["id"])
    assert store.update_character(char["id"], canonical_asset_id="")["canonical_asset_id"] is None


def test_character_canonical_clear_through_the_api(client):
    c, app, _allowed = client
    pid = c.post("/api/projects", json={"name": "UI"}).json()["id"]
    char = c.post(f"/api/projects/{pid}/characters", json={"name": "Nova", "fields": {"bio": "x"}}).json()
    asset = app.state.store.create_asset(pid, "image", "assets/n.png", source="import")
    r = c.patch(f"/api/characters/{char['id']}", json={"fields": {"canonical_asset_id": asset["id"]}})
    assert r.status_code == 200 and r.json()["canonical_asset_id"] == asset["id"]
    r = c.patch(f"/api/characters/{char['id']}", json={"fields": {"canonical_asset_id": None, "bio": "y"}})
    assert r.status_code == 200
    assert r.json()["canonical_asset_id"] is None
    assert r.json()["bio"] == "y"


def test_backend_status_lists_user_import_roots_separately(client):
    c, _app, allowed = client
    overrides = c.get("/api/backend").json()["overrides"]
    # the Settings form edits only these; the full list also has the built-in roots
    assert overrides["import_roots_user"] == [str(allowed)]
    assert len(overrides["import_roots"]) >= 1
    r = c.post("/api/backend", json={"import_roots": overrides["import_roots_user"]})
    assert r.status_code == 200
    assert c.get("/api/backend").json()["overrides"]["import_roots_user"] == [str(allowed)]
