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
