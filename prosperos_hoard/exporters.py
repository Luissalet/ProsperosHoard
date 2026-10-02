"""Exports of a timeline for desktop editors: a Final Cut Pro 7 XML
(`xmeml` v5, which Premiere Pro and DaVinci Resolve both import) and a
CMX 3600 EDL. Pure Python: the caller passes the timeline and a way to
find each asset; nothing here touches the database or ffmpeg.

The media are referenced where they already live (absolute file URLs on
this machine), so the editor opens the same files the studio rendered -
nothing is copied. The sung lines become sequence markers in the XML and
comments in the EDL."""

from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

Lookup = Callable[[str], Optional[dict[str, Any]]]  # asset id -> {"path", "name", "kind", "duration_s", "width", "height"}


def _frames(seconds: float, fps: int) -> int:
    return int(round(float(seconds) * fps))


def file_url(path: Path) -> str:
    """file://localhost/C%3a/Users/... - the form FCP7 XML importers expect."""
    posix = Path(path).resolve().as_posix()
    if re.match(r"^[A-Za-z]:/", posix):
        posix = "/" + posix
    return "file://localhost" + quote(posix, safe="/")


def _timecode(frames: int, fps: int) -> str:
    h, rem = divmod(max(0, frames), fps * 3600)
    m, rem = divmod(rem, fps * 60)
    s, f = divmod(rem, fps)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _rate(fps: int) -> str:
    return f"<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>"


def _esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _clips(timeline: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    track = next((t for t in timeline.get("tracks") or [] if t.get("type") == kind), None)
    return list((track or {}).get("clips") or [])


def to_xmeml(timeline: dict[str, Any], lookup: Lookup, name: Optional[str] = None) -> str:
    fps = int(timeline.get("fps") or 24)
    width, height = int(timeline.get("width") or 1920), int(timeline.get("height") or 1080)
    visual = _clips(timeline, "visual")
    total = sum(_frames(c["duration_s"], fps) for c in visual)
    seq_name = name or timeline.get("name") or "Prospero cut"
    seen: set[str] = set()
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE xmeml>", '<xmeml version="5">',
           f'<sequence id="sequence-1"><name>{_esc(seq_name)}</name><duration>{total}</duration>{_rate(fps)}',
           "<timecode><string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat>" + _rate(fps) + "</timecode>",
           "<media><video><format><samplecharacteristics>" + _rate(fps)
           + f"<width>{width}</width><height>{height}</height><pixelaspectratio>square</pixelaspectratio>"
           + "</samplecharacteristics></format><track>"]

    def file_block(aid: str, info: dict[str, Any], media: str) -> str:
        if aid in seen:
            return f'<file id="file-{_esc(aid)}"/>'
        seen.add(aid)
        dur = _frames(info.get("duration_s") or 0, fps) if info.get("kind") != "image" else 0
        chars = ""
        if media == "video":
            chars = ("<media><video><samplecharacteristics>" + _rate(fps)
                     + f"<width>{int(info.get('width') or width)}</width><height>{int(info.get('height') or height)}</height>"
                     + "</samplecharacteristics></video></media>")
        else:
            chars = "<media><audio><channelcount>2</channelcount></audio></media>"
        return (f'<file id="file-{_esc(aid)}"><name>{_esc(info.get("name"))}</name>'
                f'<pathurl>{_esc(file_url(info["path"]))}</pathurl>{_rate(fps)}'
                + (f"<duration>{dur}</duration>" if dur else "") + chars + "</file>")

    start = 0
    for i, clip in enumerate(visual, 1):
        info = lookup(clip["asset_id"])
        length = _frames(clip["duration_s"], fps)
        if not info or length <= 0:
            start += max(0, length)
            continue
        src_in = _frames(clip.get("trim_start_s") or 0, fps) if info.get("kind") == "video" else 0
        src_dur = _frames(info.get("duration_s") or 0, fps) if info.get("kind") == "video" else length
        out.append(f'<clipitem id="clipitem-{i}"><name>{_esc(info.get("name"))}</name>'
                   f"<duration>{max(src_dur, src_in + length)}</duration>{_rate(fps)}"
                   f"<start>{start}</start><end>{start + length}</end><in>{src_in}</in><out>{src_in + length}</out>"
                   + file_block(clip["asset_id"], info, "video") + "</clipitem>")
        start += length
    out.append("</track></video><audio><track>")
    song_id = timeline.get("audio_asset_id")
    song = lookup(song_id) if song_id else None
    if song:
        out.append(f'<clipitem id="clipitem-audio"><name>{_esc(song.get("name"))}</name>'
                   f"<duration>{max(total, _frames(song.get('duration_s') or 0, fps))}</duration>{_rate(fps)}"
                   f"<start>0</start><end>{total}</end><in>0</in><out>{total}</out>"
                   + file_block(song_id, song, "audio")
                   + "<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack></clipitem>")
    out.append("</track></audio></media>")
    for line in _clips(timeline, "lyrics"):
        at = _frames(line.get("start_s") or 0, fps)
        if at < total:
            out.append(f"<marker><name>{_esc(line.get('text'))}</name><comment></comment><in>{at}</in><out>-1</out></marker>")
    out.append("</sequence></xmeml>")
    return "\n".join(out)


def to_edl(timeline: dict[str, Any], lookup: Lookup, name: Optional[str] = None) -> str:
    fps = int(timeline.get("fps") or 24)
    title = re.sub(r"[^\w .-]", "", name or timeline.get("name") or "Prospero cut")[:70] or "Prospero cut"
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    rec = 0
    n = 0
    for clip in _clips(timeline, "visual"):
        info = lookup(clip["asset_id"])
        length = _frames(clip["duration_s"], fps)
        if not info or length <= 0:
            rec += max(0, length)
            continue
        n += 1
        src_in = _frames(clip.get("trim_start_s") or 0, fps) if info.get("kind") == "video" else 0
        lines.append(f"{n:03d}  AX       V     C        {_timecode(src_in, fps)} {_timecode(src_in + length, fps)} "
                     f"{_timecode(rec, fps)} {_timecode(rec + length, fps)}")
        lines.append(f"* FROM CLIP NAME: {Path(info['path']).name}")
        lines.append("")
        rec += length
    song_id = timeline.get("audio_asset_id")
    song = lookup(song_id) if song_id else None
    if song and rec:
        n += 1
        lines.append(f"{n:03d}  AX       A     C        {_timecode(0, fps)} {_timecode(rec, fps)} {_timecode(0, fps)} {_timecode(rec, fps)}")
        lines.append(f"* FROM CLIP NAME: {Path(song['path']).name}")
        lines.append("")
    for line in _clips(timeline, "lyrics"):
        lines.append(f"* LYRIC {_timecode(_frames(line.get('start_s') or 0, fps), fps)} {str(line.get('text') or '').strip()}")
    return "\n".join(lines).rstrip() + "\n"


def package(timeline: dict[str, Any], lookup: Lookup, name: str) -> bytes:
    """A small zip: the XML, the EDL and a note on how to open them."""
    base = re.sub(r"[^\w-]+", "_", name).strip("_")[:60] or "cut"
    paths = sorted({str(info["path"]) for c in _clips(timeline, "visual") if (info := lookup(c["asset_id"]))})
    note = (f"{name}\n\nOpen {base}.xml in Premiere Pro (File > Import) or DaVinci Resolve (File > Import > Timeline).\n"
            f"{base}.edl is the same cut as a CMX 3600 EDL. The media are referenced where Prospero's Hoard keeps them,\n"
            "on this computer; if you move the files, relink them in the editor.\n\nMedia used:\n" + "\n".join(paths) + "\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{base}.xml", to_xmeml(timeline, lookup, name))
        z.writestr(f"{base}.edl", to_edl(timeline, lookup, name))
        z.writestr("README.txt", note)
    return buf.getvalue()
