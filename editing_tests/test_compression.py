"""Size limits are measured on the actual file; originals and the sound mix survive."""
import hashlib
import subprocess
from pathlib import Path

import pytest

from prosperos_hoard.editing import compression, ffmpeg as ff
from prosperos_hoard.editing.agent_tools import call_tool, tool_catalog
from conftest import HAS_FFMPEG, _ff


@pytest.fixture
def source(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg required")
    path = tmp_path / ("A long video filename with spaces and Unicode á " * 2 + ".mp4")
    _ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-c:a", "aac", "-b:a", "128k", "-shortest", str(path))
    return path


def test_actual_limit_dimensions_duration_audio_and_original(services, source):
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    job = call_tool(services, "video_compress", {"path": str(source), "max_mb": .15})
    assert job["state"] == "done", job
    result = job["result"]
    path = Path(result["path"])
    assert path.suffix == ".mp4" and job["id"] in path.name and path != source
    assert 0 < path.stat().st_size == result["bytes"] <= 150000
    assert (result["width"], result["height"]) == (320, 240)
    assert abs(result["duration_ms"] - 3000) < 100 and result["has_audio"]
    assert result["audio_mode"] == "copy" and result["fits"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    def sound(p):
        return subprocess.check_output([services.tools().ffmpeg, "-v", "error", "-i", str(p), "-map", "0:a:0", "-c:a", "copy", "-f", "adts", "-"])
    assert sound(path) == sound(source), "AAC packets must preserve the chosen sound mix exactly"
    second = call_tool(services, "video_compress", {"path": str(source), "max_mb": .15})
    assert second["state"] == "done" and second["result"]["path"] != result["path"]
    assert path.is_file()


def test_already_under_limit_returns_original_without_encoding(services, source, monkeypatch):
    monkeypatch.setattr(ff, "run", lambda *a, **kw: pytest.fail("An already fitting video should not be recompressed"))
    result = compression.start(services, str(source), 25)["result"]
    assert result["path"] == str(source) and not result["compressed"] and result["fits"]


@pytest.mark.parametrize("maximum", [0, -1, "NaN", "Infinity"])
def test_invalid_limit_creates_no_job(client, maximum):
    response = client.post("/api/video/compress", json={"path": "missing.mp4", "max_mb": maximum})
    assert response.status_code in (400, 422)
    assert client.svc.jobs.list() == []


def test_missing_video_is_refused_and_tool_is_discoverable(client):
    assert "video_compress" in [t["name"] for t in tool_catalog()]
    response = client.post("/api/video/compress", json={"path": "no-such-video.mp4"})
    assert response.status_code == 404


def test_size_failure_does_not_publish_or_modify_original(services, tmp_path, monkeypatch):
    source = tmp_path / "oversize.mp4"
    source.write_bytes(b"original" * 1000)
    monkeypatch.setattr(ff, "probe", lambda *a: {"streams": []})
    monkeypatch.setattr(ff, "summarize", lambda *a: {"has_video": True, "has_audio": False, "duration_ms": 100, "width": 320, "height": 240})
    calls = []
    def run(tools, args, **kw):
        calls.append(args)
        if args[args.index("-pass") + 1] == "2":
            Path(args[-1]).write_bytes(b"x" * 2000)
    monkeypatch.setattr(ff, "run", run)
    job = compression.start(services, str(source), .001)
    assert job["state"] == "failed" and "exceeds" in job["error"]
    assert len(calls) == 4 and not list(tmp_path.glob("*_under-*.mp4"))
    assert source.read_bytes() == b"original" * 1000


def test_ui_route_downloads_only_a_finished_compression(client, source):
    response = client.post("/api/video/compress", json={"path": str(source), "max_mb": .15})
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["state"] == "done", job
    download = client.get(job["result"]["url"])
    assert download.status_code == 200 and len(download.content) <= 150000


def test_publish_when_app_work_and_output_are_on_different_drives(source, tmp_path):
    import tempfile
    from conftest import make_services
    # Windows CI/local worktrees commonly live on another volume than pytest's temp.
    workspace = Path.cwd()
    if workspace.anchor.lower() == tmp_path.anchor.lower():
        pytest.skip("This regression requires two mounted volumes")
    cache = workspace / ".pytest_cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compression-drive-", dir=cache) as data:
        svc = make_services(Path(data))
        svc.start()
        try:
            assert svc.config.work_dir.anchor.lower() != source.anchor.lower()
            job = compression.start(svc, str(source), .15)
            assert job["state"] == "done", job
            result = Path(job["result"]["path"])
            assert result.parent == source.parent and result.stat().st_size <= 150000
        finally:
            svc.stop()
