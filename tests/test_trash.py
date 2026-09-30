"""Deleting results: the trash, references, restore and emptying."""

from __future__ import annotations

import io
import json

from PIL import Image


def _upload(c, project_id: str, name: str = "x.png") -> str:
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), (30, 90, 200)).save(buf, "PNG")
    r = c.post(f"/api/projects/{project_id}/import-upload", files={"file": (name, buf.getvalue(), "image/png")})
    r.raise_for_status()
    return r.json()["id"]


def test_delete_restore_and_empty(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Trash"}).json()["id"]
    a1, a2 = _upload(c, pid, "bad.png"), _upload(c, pid, "worse.png")
    file1 = store.data_dir / store.get_asset(a1)["file_path"]
    r = c.post("/api/assets/delete", json={"ids": [a1, a2]})
    assert r.status_code == 200 and sorted(r.json()["deleted"]) == sorted([a1, a2]), r.text
    assert c.get(f"/api/assets/{a1}").status_code == 404 and not file1.exists()
    ids = [i["id"] for i in c.get(f"/api/trash?project={pid}").json()["items"]]
    assert set(ids) == {a1, a2}
    assert c.get(f"/api/trash/{a1}/thumb").status_code == 200
    back = c.post(f"/api/assets/{a1}/restore")
    assert back.status_code == 200 and back.json()["id"] == a1 and file1.exists()
    assert c.get(f"/api/assets/{a1}").status_code == 200
    gone = c.post("/api/trash/empty", json={"project": pid}).json()
    assert gone["purged"] == [a2] and not (store.data_dir / "trash" / a2).exists()
    assert c.get(f"/api/trash?project={pid}").json()["items"] == []


def test_in_use_needs_force_and_restore_reattaches(client):
    c, app, _ = client
    pid = c.post("/api/projects", json={"name": "Refs"}).json()["id"]
    aid = _upload(c, pid)
    char = c.post(f"/api/agent/studio_cast?project={pid}", json={"action": "create", "kind": "character", "name": "Nova",
                                                                  "fields": {"canonical_asset_id": aid}}).json()
    char_id = char.get("id") or char.get("character", {}).get("id")
    r = c.delete(f"/api/assets/{aid}")
    assert r.status_code == 409 and r.json()["error"] == "asset_in_use" and "force" in r.json()["message"]
    r = c.post("/api/agent/studio_delete_assets", json={"ids": [aid]})
    assert r.json()["deleted"] == [] and r.json()["failed"][0]["error"] == "asset_in_use"
    r = c.post("/api/agent/studio_delete_assets", json={"ids": [aid], "force": True})
    assert r.json()["deleted"] == [aid] and r.json()["detached"][aid][0]["kind"] == "character_canonical"
    assert app.state.store.get_character(char_id)["canonical_asset_id"] is None
    r = c.post("/api/agent/studio_trash", json={"action": "restore", "ids": [aid]})
    assert r.json()["restored"] == [aid]
    assert app.state.store.get_character(char_id)["canonical_asset_id"] == aid


def test_timeline_use_always_blocks(client):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "TL"}).json()["id"]
    aid = _upload(c, pid)
    store.conn.execute("INSERT INTO timelines (id, project_id, name, tracks_json, created_at, updated_at) "
                       "VALUES ('tl_1', ?, 'cut', ?, 'x', 'x')", (pid, json.dumps([{"clips": [{"asset_id": aid}]}])))
    store.conn.commit()
    r = c.delete(f"/api/assets/{aid}?force=true")
    assert r.status_code == 409 and "timeline" in r.json()["message"]
