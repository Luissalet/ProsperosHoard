"""Deleting a project: to the trash (hidden everywhere, its productions
along), restored as it was, or deleted for good with its files."""

from __future__ import annotations

import pytest
from PIL import Image

from prosperos_hoard import productions as prod
from prosperos_hoard.ids import new_id
from prosperos_hoard.store import ProjectBusy
from test_productions import tiny_spec


def _image(store, pid):
    aid = new_id("x")
    path = store.data_dir / "assets" / f"{aid}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), (90, 20, 20)).save(path)
    return store.create_asset(project_id=pid, kind="image", file_path=path.relative_to(store.data_dir).as_posix(),
                              source="generated", recipe={})


def test_trash_restore_and_purge_in_the_store(store):
    keep = store.create_project("Keep")
    gone = store.create_project("Gone")
    a = _image(store, gone["id"])
    _image(store, keep["id"])
    store.trash_project(gone["id"])
    assert [p["id"] for p in store.list_projects()["items"]] == [keep["id"]]
    assert all(x["project_id"] == keep["id"] for x in store.list_assets(None, limit=60)["items"])
    assert store.list_trashed_projects()[0]["id"] == gone["id"]
    store.restore_project(gone["id"])
    assert {p["id"] for p in store.list_projects()["items"]} == {keep["id"], gone["id"]}
    with pytest.raises(ValueError, match="not in the trash"):
        store.purge_project(gone["id"])
    store.trash_project(gone["id"])
    out = store.purge_project(gone["id"])
    assert out["purged"] and out["assets"] == 1 and out["files_deleted"] >= 1
    assert not (store.data_dir / a["file_path"]).exists()
    assert store.list_trashed_projects() == []
    assert store.list_assets(None, limit=60)["items"][0]["project_id"] == keep["id"]


def test_a_project_with_a_live_job_is_not_deleted(store):
    p = store.create_project("Busy")
    store.create_job("generate_image", "gpu", {}, project_id=p["id"])
    with pytest.raises(ProjectBusy):
        store.trash_project(p["id"])
    assert store.list_projects()["items"][0]["id"] == p["id"]


def test_delete_project_takes_its_productions_and_brings_them_back(client):
    c, app, _ = client
    store = app.state.store
    p = store.create_project("Video")
    state = prod.create_production(store.data_dir, "Clip", prod.normalise_spec(tiny_spec()), {}, project_id=p["id"])
    preview = c.get(f"/api/projects/{p['id']}/delete-preview").json()
    assert [x["slug"] for x in preview["productions"]] == [state["slug"]]
    r = c.delete(f"/api/projects/{p['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["productions"] == [state["slug"]]
    assert all(x["slug"] != state["slug"] for x in c.get("/api/productions").json()["items"])
    assert all(x["id"] != p["id"] for x in c.get("/api/projects").json()["items"])
    listed = c.post("/api/agent/studio_trash", json={"action": "list"}).json()
    assert listed["projects"][0]["id"] == p["id"] and listed["projects"][0]["productions"] == [state["slug"]]
    r = c.post("/api/agent/studio_trash", json={"action": "restore", "projects": [p["id"]]})
    assert r.status_code == 200, r.text
    assert any(x["slug"] == state["slug"] for x in c.get("/api/productions").json()["items"])
    # the agent tool, then deleted for good
    assert c.post("/api/agent/studio_delete_project", json={"project": p["id"]}).status_code == 200
    r = c.post("/api/agent/studio_trash", json={"action": "empty", "projects": [p["id"]]})
    assert r.status_code == 200, r.text
    assert r.json()["projects"][0]["productions_deleted"] == 1
    assert c.get(f"/api/projects/{p['id']}").status_code == 404
    assert c.post(f"/api/projects/{p['id']}/restore").status_code == 404
