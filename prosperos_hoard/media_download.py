"""Bring a video (or its audio) from a link into the library: YouTube, X, Instagram and the other sites yt-dlp knows. Used for
references - a dance to take poses from, a look to copy - so a clip can be cut to a time range instead of downloading a whole
concert.

The download itself is Links Hoard's (`hoard_link.fam_media.download`: one yt-dlp, its updater and cookies for the whole family). When
Links is not running the work is done here with the same yt-dlp finder the family uses (`hoard_link.media.bins`). Runs as a CPU-lane
job ("download_media"); the file lands in the project as an ordinary imported asset whose recipe records the link.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from . import engine
from .hoard_link import fam_media, proc as hlproc
from .hoard_link.media import bins
from .hoard_link.web import safety
from .ids import new_id
from .store import Store

# a reference does not need more than this; also keeps a mistaken link to a
# three-hour stream from filling the disk
MAX_DURATION_S = 20 * 60
MAX_HEIGHT = 1080
DOWNLOAD_TIMEOUT_S = 1800.0
#: Links answers with these kinds when it cannot be reached; only then is the download done here.
LOCAL_KINDS = frozenset({"hub_down", "app_down", "app_missing", "tool_missing"})


class DownloadError(engine.EngineError):
    pass


def check_url(url: str) -> str:
    """The link, trimmed, when it is a public http(s) page (the shared SSRF check: no private, loopback, link-local or metadata
    addresses, no credentials in the URL, no odd numeric hosts). A name that does not resolve here passes: the download says so."""
    url = (url or "").strip()
    reason = safety.check_url(url)
    if reason and not reason.startswith(safety.UNRESOLVABLE_PREFIX):
        if not url.lower().startswith(("http://", "https://")):
            raise DownloadError("bad_url", "paste a full http(s) link (a YouTube, X or Instagram page)")
        raise DownloadError("bad_url", f"only public video pages can be downloaded ({reason})")
    return url


def _sections(start_s: Optional[float], end_s: Optional[float]) -> Optional[list[list[float]]]:
    if start_s is None and end_s is None:
        return None
    return [[float(start_s or 0), float(end_s if end_s is not None else 24 * 3600)]]


def _via_links(url: str, tmp: Path, audio_only: bool, sections: Optional[list[list[float]]], say: Callable[..., None]) -> dict[str, Any]:
    """Links Hoard's download of `url` into `tmp`: its view (`path`, `title`, `uploader`, `duration`), or a result with `kind`."""
    say(0.05, "asking Links Hoard")
    got = fam_media.download(url, format="audio" if audio_only else "video", quality=MAX_HEIGHT, dest_dir=str(tmp), sections=sections,
                             max_duration_s=None if sections else MAX_DURATION_S, max_height=MAX_HEIGHT, save_link=False,
                             timeout_s=DOWNLOAD_TIMEOUT_S)
    if got.get("kind") == "timeout" and got.get("id"):
        fam_media.cancel(got["id"])
        got["error"] = "it is taking too long"
    return got


def _via_ytdlp(url: str, tmp: Path, audio_only: bool, sections: Optional[list[list[float]]], say: Callable[..., None]) -> dict[str, Any]:
    """The same download with the yt-dlp this machine has (`bins.find("ytdlp")`: its own binary or `python -m yt_dlp`)."""
    tool = bins.find("ytdlp")
    if not tool:
        raise DownloadError("ytdlp_missing", f"yt-dlp is not available: {tool.hint or 'pip install yt-dlp'}")
    ffmpeg = bins.find("ffmpeg")
    if sections and not ffmpeg:
        raise DownloadError("no_ffmpeg", "cutting a section needs ffmpeg")
    extra = (["--download-sections", f"*{sections[0][0]:g}-{sections[0][1]:g}", "--force-keyframes-at-cuts"] if sections
             else ["--match-filter", f"duration<?{MAX_DURATION_S + 1}"])   # short enough, or no duration known
    args = bins.build_ytdlp_args(url=url, format="audio" if audio_only else "video", quality=MAX_HEIGHT, dir=str(tmp),
                                 has_ffmpeg=bool(ffmpeg), ffmpeg_path=ffmpeg.path if ffmpeg else None, extra=extra,
                                 node_path=(bins.find("node").path or None), ytdlp_version=tool.version)
    meta: dict[str, Any] = {}
    errors: list[str] = []

    def on_line(line: str) -> None:
        event = bins.parse_ytdlp_line(line)
        if not event:
            return
        if event["type"] == "progress":
            total = event.get("total") or event.get("estimate") or 0
            if total and event.get("downloaded"):
                say(0.05 + 0.8 * min(1.0, event["downloaded"] / total), f"downloading {int(100 * event['downloaded'] / total)}%")
        elif event["type"] == "meta":
            meta.update(event["data"])

    say(0.03, "reading the link")
    try:
        code = hlproc.run_streaming(tool.command(*args), on_line, stderr_line=errors.append, timeout=DOWNLOAD_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - a timeout or a program that would not start: one readable message
        raise DownloadError("download_failed", f"could not download that link: {str(exc)[:300]}") from None
    if code != 0:
        reason = bins.classify_failure("yt-dlp", "\n".join(errors), code)
        raise DownloadError("download_failed", f"could not download that link ({reason}): {hlproc.tail_lines(chr(10).join(errors), 3, 300)}")
    return {"title": meta.get("title"), "uploader": meta.get("uploader") or meta.get("channel"), "duration": meta.get("duration")}


def download(store: Store, project_id: str, url: str, *, audio_only: bool = False, start_s: Optional[float] = None,
             end_s: Optional[float] = None, progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """Download `url` and import it into `project_id`. start_s/end_s cut a
    section (ffmpeg); audio_only keeps an mp3. Links Hoard does it when it is running, else this app does."""
    url = check_url(url)
    store.get_project(project_id)
    if start_s is not None and end_s is not None and end_s <= start_s:
        raise DownloadError("bad_range", "end_s must come after start_s")
    tmp = store.data_dir / "tmp" / "downloads" / new_id("dl")
    tmp.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda *a, **k: None)
    sections = _sections(start_s, end_s)
    info: dict[str, Any] = {}
    backend = "links"
    try:
        got = _via_links(url, tmp, audio_only, sections, say)
        if got.get("ok"):
            info = got
        elif got.get("kind") in LOCAL_KINDS:
            backend = "yt-dlp"
            info = _via_ytdlp(url, tmp, audio_only, sections, say)
        else:
            raise DownloadError("download_failed", f"could not download that link: {str(got.get('error') or 'unknown error')[:300]}")
        path = Path(info["path"]) if info.get("path") else None
        if path is None or not path.is_file():
            files = sorted((p for p in tmp.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl", ".json"))),
                           key=lambda p: p.stat().st_size, reverse=True)
            if not files:
                raise DownloadError("download_failed", "the link gave no file (too long, private or not a video)")
            path = files[0]
        title = str(info.get("title") or path.stem)[:120]
        say(0.9, "adding it to the library")
        return engine.import_asset(
            store, project_id, path, "audio" if audio_only else "video", original_name=f"{title}{path.suffix}",
            recipe={"operation": "download", "backend": backend, "url": url, "title": title,
                    "uploader": info.get("uploader"), "source_duration_s": info.get("duration"),
                    **({"start_s": start_s} if start_s is not None else {}), **({"end_s": end_s} if end_s is not None else {})},
            tags=["download"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def download_job(store: Store, job: dict[str, Any], progress) -> dict[str, Any]:
    p = job["params"]
    asset = download(store, job["project_id"], p["url"], audio_only=bool(p.get("audio_only")), start_s=p.get("start_s"),
                     end_s=p.get("end_s"), progress=progress)
    return {"asset_id": asset["id"], "asset_ids": [asset["id"]], "name": asset.get("name")}
