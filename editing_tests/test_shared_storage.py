from pathlib import Path

from PIL import Image
import pytest

from conftest import needs_ffmpeg
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing.agent_tools import SharedMediaArgs, run_media_shared, tool_catalog
from prosperos_hoard.editing.hoard_link import fam_workspace


@needs_ffmpeg
def test_shared_png_stays_at_original_path_and_refresh_keeps_media_id(services, tmp_path, monkeypatch):
    source = tmp_path / "shared.png"
    Image.new("RGB", (80, 60), "red").save(source)
    revision = "sha256:first-fixture"
    monkeypatch.setattr(fam_workspace, "resolve", lambda uid: {"ok": True, "file": {
        "id": uid, "path": str(source), "exists": True, "live_file": True, "revision": revision, "title": "Fixture"}})
    first = run_media_shared(services, SharedMediaArgs(file_id="fixture-id"))
    assert Path(first["path"]) == source and first["live_file"]
    cache = services.config.cache_dir / first["id"]
    (cache / "stale.txt").write_text("old preview", encoding="utf-8")
    media_store.put_analysis(services, first["id"], "fixture", {"old": True})
    Image.new("RGB", (100, 70), "blue").save(source)
    revision = "sha256:second-fixture"
    refreshed = run_media_shared(services, SharedMediaArgs(file_id="fixture-id"))
    assert refreshed["id"] == first["id"] and refreshed["refreshed"]
    assert Path(refreshed["path"]) == source
    assert not (cache / "stale.txt").exists()
    assert media_store.get_analysis(services, first["id"], "fixture") is None
    assert media_store.get(services, first["id"])["width"] == 100
    assert Image.open(source).getpixel((0, 0)) == (0, 0, 255)
    media_store.delete(services, first["id"])
    assert source.is_file()  # removing a library reference preserves the SSD original.


def test_shared_tool_refuses_missing_or_unavailable_original(services, monkeypatch):
    monkeypatch.setattr(fam_workspace, "resolve", lambda uid: {"ok": False, "error": "not a member"})
    with pytest.raises(Exception, match="not a member"):
        run_media_shared(services, SharedMediaArgs(file_id="fixture-id"))
    assert "media_shared" in {tool["name"] for tool in tool_catalog()}


@needs_ffmpeg
def test_shared_resolution_does_not_bypass_local_folder_restrictions(services, tmp_path, monkeypatch):
    source = tmp_path / "outside.png"
    Image.new("RGB", (32, 32), "red").save(source)
    services.config.file_roots = (tmp_path / "allowed",)
    monkeypatch.setattr(fam_workspace, "resolve", lambda uid: {"ok": True, "file": {
        "path": str(source), "exists": True, "live_file": True, "revision": "fixture"}})
    with pytest.raises(Exception, match="outside the folders"):
        run_media_shared(services, SharedMediaArgs(file_id="fixture-id"))
