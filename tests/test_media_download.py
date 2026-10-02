"""Links to videos (yt-dlp, faked here) and frames out of a video or GIF."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from prosperos_hoard import engine, media_download, procutil
from prosperos_hoard.backend import ffmpeg_path
from prosperos_hoard.hoard_link.media import bins


def _clip(path: Path, seconds: int = 2, gif: bool = False) -> Path:
    exe = ffmpeg_path()
    if not exe:
        pytest.skip("ffmpeg not available")
    args = [exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}"]
    if not gif:
        args += ["-pix_fmt", "yuv420p"]
    procutil.run(args + [str(path)], timeout=60)
    assert path.is_file()
    return path


def _tone(path: Path) -> Path:
    procutil.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=1", str(path)], timeout=60)
    assert path.is_file()
    return path


def test_only_public_http_links():
    assert media_download.check_url(" https://youtu.be/abc ") == "https://youtu.be/abc"
    for bad in ("ftp://x.com/v", "not a link", "http://127.0.0.1:8188/view", "http://192.168.1.4/v", "http://nas.local/v",
                "http://169.254.169.254/latest/meta-data", "http://2130706433/x", "http://[::1]/x", "https://user:pw@youtu.be/x",
                "http://localhost:8188/view", "http://metadata.google.internal/computeMetadata"):
        with pytest.raises(media_download.DownloadError) as caught:
            media_download.check_url(bad)
        assert caught.value.code == "bad_url", bad


def _fake_links(monkeypatch, src: Path, calls: list, answer=None):
    """Links Hoard answering: the clip lands in the folder Prospero asked for."""
    def fake(url, **kw):
        calls.append((url, kw))
        if answer is not None:
            return answer
        audio = kw["format"] == "audio"
        out = Path(kw["dest_dir"]) / ("Dance [abc].mp3" if audio else "Dance [abc].mp4")
        out.write_bytes((src.with_suffix(".mp3") if audio else src).read_bytes())
        return {"ok": True, "path": str(out), "title": "Dance", "uploader": "someone", "duration": 2, "via": "links"}

    monkeypatch.setattr(media_download.fam_media, "download", fake)


def test_download_goes_through_links_and_imports_the_file_with_its_link(store, project, monkeypatch, tmp_path):
    src = _clip(tmp_path / "src.mp4")
    _tone(tmp_path / "src.mp3")
    calls: list = []
    _fake_links(monkeypatch, src, calls)
    asset = media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc", start_s=1, end_s=3)
    assert asset["kind"] == "video" and asset["name"] == "Dance.mp4"
    assert asset["recipe"]["url"].endswith("v=abc") and asset["recipe"]["start_s"] == 1 and asset["recipe"]["backend"] == "links"
    kw = calls[0][1]
    assert kw["sections"] == [[1.0, 3.0]] and kw["format"] == "video" and kw["max_height"] == 1080 and kw["save_link"] is False
    assert kw["max_duration_s"] is None                                    # a cut section is not limited to 20 minutes
    assert not any((store.data_dir / "tmp" / "downloads").iterdir())
    media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc", audio_only=True)
    assert calls[1][1]["format"] == "audio" and calls[1][1]["max_duration_s"] == 1200 and calls[1][1]["sections"] is None


def test_a_download_links_failed_is_not_repeated_here(store, project, monkeypatch, tmp_path):
    calls: list = []
    _fake_links(monkeypatch, tmp_path / "unused", calls, answer={"ok": False, "kind": "tool_error", "error": "private video", "via": "links"})
    monkeypatch.setattr(media_download, "_via_ytdlp", lambda *a, **k: pytest.fail("must not fall back"))
    with pytest.raises(media_download.DownloadError, match="private video"):
        media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc")
    assert not any((store.data_dir / "tmp" / "downloads").iterdir())


def test_a_download_still_running_at_the_deadline_is_cancelled(store, project, monkeypatch, tmp_path):
    cancelled: list = []
    monkeypatch.setattr(media_download.fam_media, "cancel", lambda id: cancelled.append(id) or {"ok": True})
    _fake_links(monkeypatch, tmp_path / "unused", [], answer={"ok": False, "kind": "timeout", "id": "dl_1", "still_running": True})
    with pytest.raises(media_download.DownloadError, match="too long"):
        media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc")
    assert cancelled == ["dl_1"]


FAKE_YTDLP = """\
import json, pathlib, shutil, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index("-P") + 1])
pathlib.Path(__file__).with_name("argv.json").write_text(json.dumps(args))
audio = "-x" in args
shutil.copy(pathlib.Path(__file__).with_name("clip.mp3" if audio else "clip.mp4"), out / ("Dance [abc].mp3" if audio else "Dance [abc].mp4"))
print("LHP|50|100|NA|10|1|downloading", flush=True)
print("LHMETA|" + json.dumps({"id": "abc", "title": "Dance", "uploader": "someone", "duration": 2}), flush=True)
"""


def _fake_cli(monkeypatch, tmp_path: Path, src: Path):
    folder = tmp_path / "fakebin"
    folder.mkdir()
    (folder / "fake_ytdlp.py").write_text(FAKE_YTDLP, encoding="utf-8")
    (folder / "clip.mp4").write_bytes(src.read_bytes())
    (folder / "clip.mp3").write_bytes(src.with_suffix(".mp3").read_bytes())
    real = bins.find

    def find(name, **kw):
        if name == "ytdlp":
            return bins.Tool("ytdlp", str(folder / "fake_ytdlp.py"), [sys.executable, str(folder / "fake_ytdlp.py")], "2026.8.19", "path")
        return real(name, **kw)

    monkeypatch.setattr(media_download.bins, "find", find)
    return folder / "argv.json"


def test_without_links_the_download_runs_the_local_ytdlp(store, project, monkeypatch, tmp_path):
    src = _clip(tmp_path / "src.mp4")
    _tone(tmp_path / "src.mp3")
    for kind in ("hub_down", "app_down", "app_missing", "tool_missing"):
        _fake_links(monkeypatch, src, [], answer={"ok": False, "kind": kind, "error": "x", "via": "links"})
        argv_file = _fake_cli(monkeypatch, tmp_path / kind, src) if (tmp_path / kind).mkdir() is None else None
        asset = media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc", start_s=1, end_s=3)
        assert asset["name"] == "Dance.mp4" and asset["recipe"]["backend"] == "yt-dlp" and asset["recipe"]["start_s"] == 1
        argv = json.loads(argv_file.read_text(encoding="utf-8"))
        assert argv[argv.index("--download-sections") + 1] == "*1-3" and "--force-keyframes-at-cuts" in argv
        assert "--match-filter" not in argv and argv[-2:] == ["--", "https://www.youtube.com/watch?v=abc"]
        assert "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]" in " ".join(argv)
    asset = media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc")
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--match-filter") + 1] == "duration<?1201" and "--download-sections" not in argv   # 20 minute cap
    media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc", audio_only=True)
    assert "--audio-format" in json.loads(argv_file.read_text(encoding="utf-8"))
    assert not any((store.data_dir / "tmp" / "downloads").iterdir())


def test_a_failing_local_ytdlp_says_why_and_leaves_nothing(store, project, monkeypatch, tmp_path):
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "bad.py").write_text("import sys\nsys.stderr.write('ERROR: Video unavailable\\n')\nsys.exit(1)\n", encoding="utf-8")
    _fake_links(monkeypatch, tmp_path / "unused", [], answer={"ok": False, "kind": "hub_down", "error": "hub unreachable", "via": "links"})
    monkeypatch.setattr(media_download.bins, "find", lambda name, **kw: bins.Tool(
        name, str(folder / "bad.py"), [sys.executable, str(folder / "bad.py")], "1", "path") if name == "ytdlp" else bins.Tool(name))
    with pytest.raises(media_download.DownloadError, match="unavailable"):
        media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc")
    assert not any((store.data_dir / "tmp" / "downloads").iterdir())
    monkeypatch.setattr(media_download.bins, "find", lambda name, **kw: bins.Tool(name))
    with pytest.raises(media_download.DownloadError) as caught:
        media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc")
    assert caught.value.code == "ytdlp_missing"


def test_frames_from_a_gif(store, project, tmp_path):
    gif = _clip(tmp_path / "dance.gif", gif=True)
    video = engine.import_asset(store, project["id"], gif, None, original_name="dance.gif")
    assert video["kind"] == "video"
    frames = engine.extract_frames(store, video["id"], 3)
    assert len(frames) == 3 and all(f["kind"] == "image" for f in frames)
    assert frames[0]["recipe"]["derived_from"] == video["id"]
    with pytest.raises(engine.EngineError):
        engine.extract_frames(store, frames[0]["id"], 2)


def test_the_download_route_refuses_private_links(client):
    c, app, _ = client
    pid = app.state.store.create_project("Refs", brief="x")["id"]
    for bad in ("http://169.254.169.254/latest/meta-data", "http://10.0.0.5/v.mp4", "file:///etc/passwd"):
        r = c.post(f"/api/projects/{pid}/download", json={"url": bad})
        assert r.status_code == 400 and r.json()["error"] == "bad_url", (bad, r.text)
