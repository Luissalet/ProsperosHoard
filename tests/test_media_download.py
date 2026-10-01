"""Links to videos (yt-dlp, faked here) and frames out of a video or GIF."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from prosperos_hoard import engine, media_download, procutil
from prosperos_hoard.backend import ffmpeg_path


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


def test_only_public_http_links():
    assert media_download.check_url(" https://youtu.be/abc ") == "https://youtu.be/abc"
    for bad in ("ftp://x.com/v", "not a link", "http://127.0.0.1:8188/view", "http://192.168.1.4/v", "http://nas.local/v"):
        with pytest.raises(media_download.DownloadError):
            media_download.check_url(bad)


def test_download_imports_the_file_with_its_link(store, project, monkeypatch, tmp_path):
    src = _clip(tmp_path / "src.mp4")
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen.update(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            out = Path(seen["outtmpl"]).parent / "Dance [abc].mp4"
            out.write_bytes(src.read_bytes())
            return {"title": "Dance", "uploader": "someone", "duration": 2}

    fake = types.SimpleNamespace(YoutubeDL=FakeYDL, utils=types.SimpleNamespace(
        match_filter_func=lambda expr: ("filter", expr), download_range_func=lambda a, ranges: ("ranges", ranges)))
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    asset = media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc", start_s=1, end_s=3)
    assert asset["kind"] == "video" and asset["name"] == "Dance.mp4"
    assert asset["recipe"]["url"].endswith("v=abc") and asset["recipe"]["start_s"] == 1
    assert seen["download_ranges"] == ("ranges", [(1.0, 3.0)]) and seen["merge_output_format"] == "mp4"
    assert not any((store.data_dir / "tmp" / "downloads").iterdir())


def test_frames_from_a_gif(store, project, tmp_path):
    gif = _clip(tmp_path / "dance.gif", gif=True)
    video = engine.import_asset(store, project["id"], gif, None, original_name="dance.gif")
    assert video["kind"] == "video"
    frames = engine.extract_frames(store, video["id"], 3)
    assert len(frames) == 3 and all(f["kind"] == "image" for f in frames)
    assert frames[0]["recipe"]["derived_from"] == video["id"]
    with pytest.raises(engine.EngineError):
        engine.extract_frames(store, frames[0]["id"], 2)


def test_whole_video_length_filter_with_the_real_ytdlp(store, project, monkeypatch):
    yt_dlp = pytest.importorskip("yt_dlp")
    seen = {}

    class StopYDL:
        def __init__(self, opts):
            seen.update(opts)

        def __enter__(self):
            raise RuntimeError("stop here")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(yt_dlp, "YoutubeDL", StopYDL)
    with pytest.raises(media_download.DownloadError):
        media_download.download(store, project["id"], "https://www.youtube.com/watch?v=abc")
    keep = seen["match_filter"]
    # short enough or of unknown length passes; a long stream is skipped -
    # and none of it trips over comparing a number with text
    assert keep({"duration": 120, "id": "a", "title": "t"}) is None
    assert keep({"id": "a", "title": "t"}) is None
    assert keep({"duration": 3 * 3600, "id": "a", "title": "t"})
