"""Bring a video (or its audio) from a link into the library: YouTube, X,
Instagram and the other sites yt-dlp knows. Used for references - a dance
to take poses from, a look to copy - so a clip can be cut to a time range
instead of downloading a whole concert.

Runs as a CPU-lane job ("download_media"); the file lands in the project as
an ordinary imported asset whose recipe records the link.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from . import engine
from .backend import ffmpeg_path
from .ids import new_id
from .store import Store

# a reference does not need more than this; also keeps a mistaken link to a
# three-hour stream from filling the disk
MAX_DURATION_S = 20 * 60
MAX_HEIGHT = 1080


class DownloadError(engine.EngineError):
    pass


def check_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DownloadError("bad_url", "paste a full http(s) link (a YouTube, X or Instagram page)")
    host = parts.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or re.fullmatch(r"[\d.]+", host):
        raise DownloadError("bad_url", "only public video pages can be downloaded")
    return url


def _ytdlp():
    try:
        import yt_dlp  # noqa: PLC0415 - optional, only for this feature
    except ImportError:
        raise DownloadError("ytdlp_missing", "yt-dlp is not installed in Prospero's environment: "
                                             "pip install yt-dlp") from None
    return yt_dlp


def download(store: Store, project_id: str, url: str, *, audio_only: bool = False, start_s: Optional[float] = None,
             end_s: Optional[float] = None, progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """Download `url` and import it into `project_id`. start_s/end_s cut a
    section (ffmpeg); audio_only keeps an mp3."""
    url = check_url(url)
    store.get_project(project_id)
    if start_s is not None and end_s is not None and end_s <= start_s:
        raise DownloadError("bad_range", "end_s must come after start_s")
    yt_dlp = _ytdlp()
    tmp = store.data_dir / "tmp" / "downloads" / new_id("dl")
    tmp.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_path()
    say = progress or (lambda *a, **k: None)

    def hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                say(0.05 + 0.8 * min(1.0, done / total), f"downloading {int(100 * done / total)}%")

    opts: dict[str, Any] = {
        "outtmpl": str(tmp / "%(title).80s [%(id)s].%(ext)s"), "noplaylist": True, "quiet": True, "no_warnings": True,
        "restrictfilenames": True, "progress_hooks": [hook], "noprogress": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration < {MAX_DURATION_S + 1} | !duration")
        if start_s is None else None,
    }
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    if audio_only:
        opts["format"] = "bestaudio/best"
        if ffmpeg:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif ffmpeg:
        opts["format"] = (f"bv*[height<={MAX_HEIGHT}][ext=mp4]+ba[ext=m4a]/b[height<={MAX_HEIGHT}][ext=mp4]/"
                          f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b")
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = f"b[height<={MAX_HEIGHT}][ext=mp4]/b"
    if start_s is not None or end_s is not None:
        if not ffmpeg:
            raise DownloadError("no_ffmpeg", "cutting a section needs ffmpeg")
        opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(float(start_s or 0), float(end_s or 1e9))])
        opts["force_keyframes_at_cuts"] = True
    opts = {k: v for k, v in opts.items() if v is not None}
    say(0.03, "reading the link")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types; one readable message
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("download_failed", f"could not download that link: {str(exc)[:300]}") from None
    files = sorted((p for p in tmp.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DownloadError("download_failed", "the link gave no file (too long, private or not a video)")
    path: Path = files[0]
    title = str((info or {}).get("title") or path.stem)[:120]
    say(0.9, "adding it to the library")
    try:
        asset = engine.import_asset(
            store, project_id, path, "audio" if audio_only else "video", original_name=f"{title}{path.suffix}",
            recipe={"operation": "download", "backend": "yt-dlp", "url": url, "title": title,
                    "uploader": (info or {}).get("uploader"), "source_duration_s": (info or {}).get("duration"),
                    **({"start_s": start_s} if start_s is not None else {}), **({"end_s": end_s} if end_s is not None else {})},
            tags=["download"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return asset


def download_job(store: Store, job: dict[str, Any], progress) -> dict[str, Any]:
    p = job["params"]
    asset = download(store, job["project_id"], p["url"], audio_only=bool(p.get("audio_only")), start_s=p.get("start_s"),
                     end_s=p.get("end_s"), progress=progress)
    return {"asset_id": asset["id"], "asset_ids": [asset["id"]], "name": asset.get("name")}
