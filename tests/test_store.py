from prosperos_hoard.store import NotFound
import pytest


def test_project_roundtrip(store, project):
    fetched = store.get_project(project["id"])
    assert fetched["name"] == "Test Project"
    listed = store.list_projects()
    assert any(p["id"] == project["id"] for p in listed["items"])


def test_asset_roundtrip(store, project, tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"fake")
    asset = store.create_asset(project["id"], "image", "assets/x.png", tags=["a", "b"], source="import")
    fetched = store.get_asset(asset["id"])
    assert fetched["tags"] == ["a", "b"]
    updated = store.update_asset(asset["id"], rating=4, favourite=True)
    assert updated["rating"] == 4
    assert updated["favourite"] is True


def test_asset_not_found(store):
    with pytest.raises(NotFound):
        store.get_asset("a_doesnotexist")


def test_character_and_group(store, project):
    char = store.create_character(project["id"], "Aria", prompt="an idol", palette=["#ff0000"])
    assert char["palette"] == ["#ff0000"]
    group = store.create_group(project["id"], "The Group", member_ids=[char["id"]])
    assert group["member_ids"] == [char["id"]]
    updated = store.update_character(char["id"], bio="new bio")
    assert updated["bio"] == "new bio"


def test_style_presets_builtin_seeded(store):
    presets = store.list_style_presets()
    names = {p["name"] for p in presets}
    assert "Studio portrait" in names
    assert "Album art minimal" in names
    assert len(presets) >= 6


def test_job_lifecycle(store):
    job = store.create_job("generate_image", "gpu", {"prompt": "x"})
    assert job["state"] == "queued"
    updated = store.update_job(job["id"], state="running", progress=0.5)
    assert updated["state"] == "running"
    assert updated["progress"] == 0.5
    done = store.update_job(job["id"], state="done", outputs={"asset_ids": ["a1"]})
    assert done["outputs"]["asset_ids"] == ["a1"]


def test_requeue_running_jobs_on_boot(store):
    job = store.create_job("generate_image", "gpu", {})
    store.update_job(job["id"], state="running")
    n = store.requeue_running_jobs()
    assert n == 1
    assert store.get_job(job["id"])["state"] == "queued"


def test_store_is_safe_across_threads(store, project):
    import threading

    errors = []

    def worker(n):
        try:
            for i in range(25):
                job = store.create_job("t", "cpu", {"n": n, "i": i}, project_id=project["id"])
                store.update_job(job["id"], state="done", progress=1.0)
                store.list_jobs(limit=5)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.list_jobs(state="done", limit=50)["items"]) == 50


def test_schema_upgrade_adds_new_columns_to_old_database(tmp_path):
    import sqlite3

    from prosperos_hoard.store import Store

    d = tmp_path / "old"
    d.mkdir()
    conn = sqlite3.connect(d / "prosperos.sqlite3")
    conn.execute("CREATE TABLE assets (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, file_path TEXT NOT NULL, "
                 "mime TEXT, width INTEGER, height INTEGER, duration_s REAL, thumb_path TEXT, waveform_json TEXT, "
                 "tags_json TEXT NOT NULL DEFAULT '[]', rating INTEGER NOT NULL DEFAULT 0, favourite INTEGER NOT NULL DEFAULT 0, "
                 "notes TEXT, source TEXT NOT NULL, recipe_json TEXT, created_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    s = Store(d)
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(assets)")}
    assert {"name", "analysis_json"} <= cols


def test_asset_search_escapes_like_wildcards(store, project):
    store.create_asset(project["id"], "image", "assets/a.png", name="100% idol", source="import")
    store.create_asset(project["id"], "image", "assets/b.png", name="plain", source="import")
    assert [a["name"] for a in store.list_assets(project["id"], query="100%")["items"]] == ["100% idol"]
    assert store.list_assets(project["id"], query="%")["items"][0]["name"] == "100% idol"
    assert len(store.list_assets(project["id"], query="_")["items"]) == 0


def test_ids_sort_in_creation_order_within_a_millisecond():
    from prosperos_hoard.ids import new_id

    ids = [new_id("job") for _ in range(5000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
