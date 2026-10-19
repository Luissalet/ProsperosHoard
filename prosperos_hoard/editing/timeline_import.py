"""Bring an edit made elsewhere into Lumiere: a timeline as FCP7 XML (``xmeml``), a CMX 3600 EDL, or a plain plan.

``project_from_timeline`` is the entry point (tool, and the hub calls it for sibling apps such as the video studio, whose exports it
reads exactly: one video track of contiguous clips, one audio clip with the song, the sung lines as markers or ``* LYRIC`` comments).

All three formats are first parsed into one small description (:class:`Timeline`: clips with a media reference, times in ms, markers) and
then built into a project in one undo step, so a bad clip never leaves a half project behind. Media are referenced where they live
(nothing is copied) and must be inside the folders the app may read. A clip whose file cannot be found is skipped and listed under
``skipped`` (its place stays empty), so the rest of the cut still arrives.

Times: XML and EDL count frames, so the frame rate comes from the XML (``timebase``) or from ``fps`` (an EDL carries none; 24 when not
given). Drop-frame timecode is read as non-drop.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import unquote, urlsplit

from . import media as media_store
from . import projects as project_store
from .errors import LumiereError, NotFound, Refused
from .util import clip as clip_text

if TYPE_CHECKING:
    from .services import Services

MAX_BYTES = 20_000_000      # a timeline file is text; anything bigger is not one
MAX_CLIPS = 3000
AUDIO_ONLY = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
TC = r"\d{2}[:;]\d{2}[:;]\d{2}[:;]\d{2}"
EVENT_RE = re.compile(rf"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(?:(\d+)\s+)?({TC})\s+({TC})\s+({TC})\s+({TC})\s*$")


@dataclass
class Item:
    """One clip of the timeline to build."""

    ref: str                       # a path (absolute), or only a file name when the format has no path (EDL)
    kind: str                      # "video" (picture, goes on a video track) or "audio"
    start_ms: int                  # where it sits on the timeline
    in_ms: int                     # first source millisecond shown
    out_ms: Optional[int]          # end of the source span (None = to the end of the media)
    track: int = 0                 # 0 = the first track of its kind
    label: str = ""


@dataclass
class Timeline:
    source: str                    # xmeml | edl | plan
    title: str = ""
    fps: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    items: list[Item] = field(default_factory=list)
    markers: list[tuple[int, str]] = field(default_factory=list)


# ------------------------------------------------------------------ helpers

def _read_text(path: str, svc: "Services") -> tuple[str, Path]:
    p = Path(os.path.expandvars(os.path.expanduser(path.strip().strip('"')))).resolve()
    media_store._check_root(svc, p)
    if not p.is_file():
        raise NotFound(f"There is no file at {p}.")
    if p.stat().st_size > MAX_BYTES:
        raise LumiereError(f"{p.name} is too big to be a timeline file.")
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc), p
        except UnicodeError:
            continue
    raise LumiereError(f"{p.name} is not text.")  # pragma: no cover


def url_to_path(url: str) -> str:
    """``file://localhost/C%3a/Users/Ana/a%20b.mp4`` -> ``C:/Users/Ana/a b.mp4`` (also plain paths and ``file:///...``)."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() != "file":
        return unquote(url) if parts.scheme in ("", ) or re.match(r"^[A-Za-z]:[\\/]", url) else url
    if parts.netloc not in ("", "localhost"):
        return f"//{parts.netloc}{unquote(parts.path)}"       # a network share
    path = unquote(parts.path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path


def _ms(frames: float, fps: float) -> int:
    return int(round(float(frames) * 1000.0 / fps))


def _tc_frames(tc: str, fps: float) -> int:
    h, m, s, f = (int(x) for x in re.split(r"[:;]", tc))
    return int(round((h * 3600 + m * 60 + s) * fps)) + f


def _kind_of(path: str, default: str) -> str:
    return "audio" if Path(path).suffix.lower() in AUDIO_ONLY else default


# ------------------------------------------------------------------ FCP7 XML

def _text(node: Optional[ET.Element], tag: str, default: str = "") -> str:
    found = node.find(tag) if node is not None else None
    return (found.text or "").strip() if found is not None and found.text else default


def _int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_xmeml(text: str) -> Timeline:
    if "<!ENTITY" in text:
        raise LumiereError("This XML declares entities; it is not a timeline export.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise LumiereError(f"The XML could not be read: {error}.") from error
    seq = root if root.tag == "sequence" else root.find(".//sequence")
    if seq is None:
        raise LumiereError("The XML has no <sequence>: export the timeline as FCP7 XML (xmeml).")
    fps = float(_int(_text(seq.find("rate"), "timebase"), 0) or 0)
    if fps and _text(seq.find("rate"), "ntsc").upper() == "TRUE":
        fps = round(fps * 1000 / 1001, 3)
    sample = seq.find("media/video/format/samplecharacteristics")
    tl = Timeline("xmeml", title=_text(seq, "name"), fps=fps or None, width=_int(_text(sample, "width")) or None,
                  height=_int(_text(sample, "height")) or None)
    rate = fps or 24.0
    # a <file> is described once (with its pathurl); later clips point at it by id
    paths: dict[str, str] = {}
    for f in seq.iter("file"):
        url = _text(f, "pathurl")
        if f.get("id") and url:
            paths[f.get("id")] = url_to_path(url)

    def items_of(kind: str, tracks: list[ET.Element]) -> list[Item]:
        out: list[Item] = []
        for ti, track in enumerate(tracks):
            cursor = 0
            for ci in track.findall("clipitem"):
                if _text(ci, "enabled").upper() == "FALSE":
                    continue
                start, end = _int(_text(ci, "start"), -1), _int(_text(ci, "end"), -1)
                src_in, src_out = _int(_text(ci, "in")), _int(_text(ci, "out"), -1)
                if start < 0:                       # inside a transition the editor stores -1: continue where the track was
                    start = cursor
                if end < 0 and src_out >= 0:
                    end = start + (src_out - src_in)
                cursor = max(cursor, end)
                f = ci.find("file")
                path = ""
                if f is not None:
                    path = url_to_path(_text(f, "pathurl")) or paths.get(f.get("id") or "", "")
                name = _text(ci, "name") or (Path(path).name if path else "")
                if not path and not name:
                    continue
                length = (src_out - src_in) if src_out > src_in else (end - start)
                out.append(Item(ref=path or name, kind=_kind_of(path or name, kind), start_ms=_ms(start, rate), in_ms=_ms(src_in, rate),
                                out_ms=_ms(src_in + length, rate) if length > 0 else None, track=ti, label=name))
        return out

    video = items_of("video", seq.findall("media/video/track"))
    audio = items_of("audio", seq.findall("media/audio/track"))
    # an editor writes each picture clip's sound as a linked audio clip of the same file: Lumiere's video clips carry their own sound
    pictures = {(i.ref, i.start_ms, i.in_ms) for i in video}
    audio = [a for a in audio if (a.ref, a.start_ms, a.in_ms) not in pictures]
    tl.items = [*video, *audio]
    for m in seq.findall("marker"):
        label = _text(m, "name") or _text(m, "comment")
        if label:
            tl.markers.append((_ms(_int(_text(m, "in")), rate), label))
    return tl


# ------------------------------------------------------------------ EDL

def parse_edl(text: str, fps: float = 24.0) -> Timeline:
    tl = Timeline("edl", fps=fps)
    last: Optional[dict[str, Any]] = None
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.upper().startswith("TITLE:"):
            tl.title = s.split(":", 1)[1].strip()
            continue
        if s.upper().startswith("FCM:"):
            continue
        hit = EVENT_RE.match(line)
        if hit:
            if len(events) >= MAX_CLIPS:
                raise LumiereError(f"More than {MAX_CLIPS} events: split the edit first.")
            _, reel, track, _, _, a, b, c, d = hit.groups()
            last = {"reel": reel, "track": track.upper(), "src_in": a, "src_out": b, "rec_in": c, "rec_out": d, "name": ""}
            events.append(last)
            continue
        if s.startswith("*"):
            body = s[1:].strip()
            upper = body.upper()
            if upper.startswith("FROM CLIP NAME:") and last is not None:
                last["name"] = body.split(":", 1)[1].strip()
            elif upper.startswith("LYRIC "):
                m = re.match(rf"^LYRIC\s+({TC})\s*(.*)$", body, re.I)
                if m and m.group(2).strip():
                    tl.markers.append((_ms(_tc_frames(m.group(1), fps), fps), m.group(2).strip()))
            elif upper.startswith("MARKER:") or upper.startswith("LOC:"):
                m = re.search(rf"({TC})\s*(?:\S+\s+)?(.*)$", body)
                if m and m.group(2).strip():
                    tl.markers.append((_ms(_tc_frames(m.group(1), fps), fps), m.group(2).strip()))
    for e in events:
        name = e["name"] or e["reel"]
        if e["name"] == "" and e["reel"] in ("AX", "BL"):
            continue                                   # a black or an auxiliary event with no clip name has nothing to bring
        kind = "audio" if e["track"].startswith("A") else "video"
        src_in, src_out = _tc_frames(e["src_in"], fps), _tc_frames(e["src_out"], fps)
        rec_in = _tc_frames(e["rec_in"], fps)
        if src_out <= src_in:
            continue
        tl.items.append(Item(ref=name, kind=_kind_of(name, kind), start_ms=_ms(rec_in, fps), in_ms=_ms(src_in, fps), out_ms=_ms(src_out, fps),
                             track=0, label=name))
    return tl


# ------------------------------------------------------------------ plan

def _secs(value: Any, what: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError) as error:
        raise LumiereError(f"{what} must be a number of seconds.") from error


def parse_plan(plan: Any) -> Timeline:
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError as error:
            raise LumiereError(f"plan is not valid JSON: {error}") from error
    if not isinstance(plan, dict) or not isinstance(plan.get("clips"), list) or not plan["clips"]:
        raise LumiereError("plan needs clips: {clips: [{path, in_s, out_s, track}], markers: [{t, text}]}.")
    if len(plan["clips"]) > MAX_CLIPS:
        raise LumiereError(f"More than {MAX_CLIPS} clips: split the plan.")
    tl = Timeline("plan", title=str(plan.get("title") or ""), fps=float(plan["fps"]) if plan.get("fps") else None,
                  width=int(plan["width"]) if plan.get("width") else None, height=int(plan["height"]) if plan.get("height") else None)
    cursors: dict[tuple[str, int], int] = {}
    for n, c in enumerate(plan["clips"], 1):
        if not isinstance(c, dict) or not (c.get("path") or c.get("name")):
            raise LumiereError(f"clip {n} needs a path.")
        ref = str(c.get("path") or c.get("name"))
        kind = "audio" if c.get("kind") == "audio" else _kind_of(ref, "video")
        track = int(c.get("track") or 0)
        if track < 0 or track > 22:
            raise LumiereError(f"clip {n}: track must be between 0 and 22.")
        a, b = _secs(c.get("in_s"), "in_s") or 0, _secs(c.get("out_s"), "out_s")
        if b is not None and b <= a:
            raise LumiereError(f"clip {n}: out_s must be after in_s.")
        at = _secs(c.get("start_s", c.get("at_s")), "start_s")
        key = (kind, track)
        start = cursors.get(key, 0) if at is None else at
        if b is not None:
            cursors[key] = start + (b - a)
        tl.items.append(Item(ref=ref, kind=kind, start_ms=start, in_ms=a, out_ms=b, track=track, label=str(c.get("label") or Path(ref).name)))
    for m in plan.get("markers") or []:
        if not isinstance(m, dict):
            continue
        t = _secs(m.get("t", m.get("t_s")), "marker t")
        label = str(m.get("text") or m.get("label") or "").strip()
        if t is not None and label:
            tl.markers.append((t, label))
    return tl


# ------------------------------------------------------------------ resolving media

def _find_by_name(name: str, dirs: list[tuple[Path, bool]]) -> Optional[Path]:
    """The file called ``name`` in the folders (``(folder, search subfolders too)``)."""
    for d, deep in dirs:
        direct = d / name
        if direct.is_file():
            return direct
        if not deep:
            continue
        try:
            for hit in d.rglob(Path(name).name):
                if hit.is_file():
                    return hit
        except OSError:
            continue
    return None


class Resolver:
    """Finds each referenced file once and imports it into the library (reusing a library item with the same file)."""

    def __init__(self, svc: "Services", dirs: list[tuple[Path, bool]]):
        self.svc, self.dirs = svc, dirs
        self.cache: dict[str, tuple[Optional[dict[str, Any]], str]] = {}

    def get(self, ref: str) -> tuple[Optional[dict[str, Any]], str]:
        if ref in self.cache:
            return self.cache[ref]
        self.cache[ref] = self._resolve(ref)
        return self.cache[ref]

    def _resolve(self, ref: str) -> tuple[Optional[dict[str, Any]], str]:
        candidates: list[str] = []
        looks_like_path = bool(re.match(r"^(?:[A-Za-z]:[\\/]|/|\\\\|~)", ref)) or "/" in ref or "\\" in ref
        if looks_like_path:
            candidates.append(ref)
            try:
                present = Path(ref).is_file()
            except OSError as error:
                return None, f'The media cannot be read: {error}'
            if not present:
                found = _find_by_name(Path(ref.replace("\\", "/")).name, self.dirs)     # the file moved: look for it by name
                if found:
                    candidates.append(str(found))
        else:
            found = _find_by_name(ref, self.dirs)
            if found:
                candidates.append(str(found))
            else:
                try:
                    return media_store.get(self.svc, project_store.resolve_media(self.svc, ref)), ""
                except (LumiereError, NotFound):
                    pass
        reason = "file not found"
        for cand in candidates:
            try:
                return media_store.import_path(self.svc, cand), ""
            except Refused as error:
                reason = str(error)
            except (LumiereError, NotFound, OSError) as error:
                reason = str(error)
        return None, reason


# ------------------------------------------------------------------ build

def _canvas(tl: Timeline, first_video: Optional[dict[str, Any]]) -> tuple[int, int, float]:
    fps = tl.fps or (30.0 if tl.source == "plan" else 24.0)
    if tl.width and tl.height:
        return tl.width, tl.height, fps
    if first_video and first_video.get("width") and first_video.get("height"):
        w, h = int(first_video["width"]), int(first_video["height"])
        return (1080, 1920, fps) if h > w else (1920, 1080, fps)
    return 1920, 1080, fps


def build(svc: "Services", tl: Timeline, title: str, dirs: Optional[list[tuple[Path, bool]]] = None, actor: str = "family") -> dict[str, Any]:
    if not tl.items:
        raise LumiereError("The timeline has no clips to bring.")
    resolver = Resolver(svc, dirs or [])
    placed: list[tuple[Item, dict[str, Any]]] = []
    skipped: list[dict[str, str]] = []
    for item in sorted(tl.items, key=lambda i: (i.kind != "video", i.track, i.start_ms)):
        info, reason = resolver.get(item.ref)
        if info is None:
            skipped.append({"name": item.label or Path(item.ref).name, "reason": reason})
            continue
        placed.append((item, info))
    if not placed:
        raise LumiereError("None of the clips' files could be found: " + "; ".join(f"{s['name']} ({s['reason']})" for s in skipped[:4])
                           + ". Put the media in a folder the app may read and pass it as media_dirs.", code="media_missing")
    first_video = next((info for item, info in placed if item.kind == "video" and info.get("has_video")), None)
    width, height, fps = _canvas(tl, first_video)
    name = clip_text(title or tl.title or "Importado", 120)
    created = project_store.create(svc, name, width=width, height=height, fps=fps)
    pid = created["id"]
    try:
        p = project_store.doc(svc, pid)
        track_ids = {"video": [t.id for t in p.tracks if t.kind == "video"], "audio": [t.id for t in p.tracks if t.kind == "audio"]}
        ops: list[dict[str, Any]] = []
        for kind in ("video", "audio"):
            need = max((i.track for i, _ in placed if i.kind == kind), default=-1) + 1
            while len(track_ids[kind]) < need:
                tid = f"{'v' if kind == 'video' else 'a'}{len(track_ids[kind]) + 1}x"
                ops.append({"op": "track_add", "kind": kind, "role": "overlay" if kind == "video" else None, "id": tid,
                            "name": f"{'V' if kind == 'video' else 'A'}{len(track_ids[kind]) + 1}"})
                track_ids[kind].append(tid)
        count = 0
        for item, info in sorted(placed, key=lambda x: (x[0].kind != "video", x[0].track, x[0].start_ms)):
            if info.get("kind") == "image":
                length = max(40, (item.out_ms or item.in_ms + 4000) - item.in_ms)
                ops.append({"op": "add_media", "media": info["id"], "track": track_ids[item.kind][item.track], "at": item.start_ms,
                            "length": length, "mode": "overwrite", "label": item.label})
                count += 1
                continue
            duration = int(info.get("duration_ms") or 0)
            a = item.in_ms
            b = item.out_ms if item.out_ms is not None else (duration or None)
            if duration and b is not None:
                b = min(b, duration)
                a = min(a, max(0, duration - 40))
            if b is not None and b - a < 40:
                skipped.append({"name": item.label or info["name"], "reason": "empty after trimming to the file's length"})
                continue
            if item.kind == "video" and not (info.get("has_video") or info.get("kind") == "image"):
                skipped.append({"name": item.label or info["name"], "reason": "has no picture for a video track"})
                continue
            op = {"op": "add_media", "media": info["id"], "track": track_ids[item.kind][item.track], "at": item.start_ms, "src_in": a,
                  "mode": "overwrite", "label": item.label}
            if b is not None:
                op["src_out"] = b
            ops.append(op)
            count += 1
        if count == 0:
            raise LumiereError("Every clip was empty once trimmed to its file.", code="media_missing")
        for t, label in sorted(tl.markers):
            ops.append({"op": "marker_add", "t": t, "label": label[:120], "kind": "note"})
        res = project_store.edit(svc, pid, ops, label=f"Importar línea de tiempo ({tl.source})", actor=actor)
    except Exception:
        project_store.delete(svc, pid)      # nothing half-made is left behind
        raise
    view = project_store.view(svc, pid)
    return {"ok": True, "project_id": pid, "id": pid, "name": view["name"], "url": f"{svc.base_url()}/#/p/{pid}", "source": tl.source,
            "clips": count, "markers": len(tl.markers), "skipped": skipped, "duration_ms": res["duration_ms"], "duration": res["duration"],
            "canvas": view["canvas"], "issues": res["issues"][:5]}


def project_from_timeline(svc: "Services", *, title: str = "", fcpxml_path: str = "", edl_path: str = "", otio_path: str = "", plan: Any = None, fps: Optional[float] = None,
                          media_dirs: Optional[list[str]] = None, actor: str = "family") -> dict[str, Any]:
    """A new project from exactly one XML, EDL, OTIO file or native plan."""
    given = [n for n, v in (("fcpxml_path", fcpxml_path), ("edl_path", edl_path), ("otio_path", otio_path), ("plan", plan)) if v not in (None, "", {}, [])]
    if len(given) != 1:
        raise LumiereError("Give exactly one of fcpxml_path, edl_path, otio_path or plan.", code="bad_request")
    if otio_path:
        from .interchange import import_project
        return import_project(svc,otio_path,title=title,media_dirs=media_dirs,actor=actor)
    if fps is not None and not 1 <= float(fps) <= 240:
        raise LumiereError("fps must be between 1 and 240.", code="bad_request")
    folders = [Path(os.path.expandvars(os.path.expanduser(d))).resolve() for d in (media_dirs or []) if d]
    for d in folders:
        media_store._check_root(svc, d)
        if not d.is_dir():
            raise NotFound(f"The folder {d} does not exist.")
    dirs = [(d, True) for d in folders]
    if fcpxml_path:
        text, where = _read_text(fcpxml_path, svc)
        tl = parse_xmeml(text)
        if fps and not tl.fps:
            tl.fps = float(fps)
    elif edl_path:
        text, where = _read_text(edl_path, svc)
        tl = parse_edl(text, float(fps or 24))
    else:
        where = None
        tl = parse_plan(plan)
        if fps and not tl.fps:
            tl.fps = float(fps)
    if where is not None:
        dirs.append((where.parent, False))   # the editor's files usually sit next to the media they list
    return build(svc, tl, title, dirs, actor=actor)
