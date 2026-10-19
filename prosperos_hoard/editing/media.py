"""The media library: import by path (nothing is copied), uploads, probing, and the per-media cache — a browser-friendly
proxy, a poster, a filmstrip, the waveform and the analysis results."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from threading import Lock
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from . import ffmpeg as ff
from .analysis import audio as audio_an
from .errors import LumiereError, NotFound, Refused
from .util import clip, dumps, fingerprint, new_id

if TYPE_CHECKING:
    from .jobs import JobCtx
    from .services import Services

log = logging.getLogger("lumiere.media")
_MASTER_LOCK_GUARD = Lock()
_MASTER_LOCKS: dict[Path, Any] = {}

PROXY_SHORT_SIDE = 540
SPRITE_TILE_W = 160
SPRITE_MAX = 200


def cache_dir(svc: "Services", media_id: str) -> Path:
    path = svc.config.cache_dir / media_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_root(svc: "Services", path: Path) -> None:
    roots = svc.config.file_roots
    if not roots:
        return
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return
        except ValueError:
            continue
    raise Refused(f"{path} is outside the folders this app may read (LUMIERE_FILE_ROOTS).", code="outside_roots")


def view(svc: "Services", row) -> dict[str, Any]:
    path = Path(row["path"])
    probe = json.loads(row["probe"] or "{}")
    mid = row["id"]
    analyses = {r["kind"]: r["updated_ts"] for r in svc.db.query("SELECT kind, updated_ts FROM analysis WHERE media_id = ?", (mid,))}
    cdir = svc.config.cache_dir / mid
    return {
        "id": mid, "name": row["name"], "path": str(path), "kind": row["kind"], "origin": row["origin"],
        "duration_ms": row["duration_ms"], "width": row["width"], "height": row["height"], "fps": row["fps"],
        "has_video": bool(row["has_video"]), "has_audio": bool(row["has_audio"]), "bytes": row["bytes"],
        "missing": not path.exists(), "proxy": row["proxy"], "tags": json.loads(row["tags"] or "[]"),
        "analysis": sorted(analyses), "created_ts": row["created_ts"],
        "audio_streams": probe.get("audio_streams", 1 if row["has_audio"] else 0),
        # the YUV matrix the file is tagged with ("bt709", "smpte170m"...; "" = untagged, None = not probed yet): the editor's
        # accelerated preview needs it to undo the browser's YUV -> RGB conversion before it applies colour effects
        "color_space": probe.get("color_space"),
        "urls": {
            "play": f"/api/media/{mid}/play", "file": f"/api/media/{mid}/file",
            "poster": f"/api/media/{mid}/poster" if (cdir / "poster.jpg").exists() or row["kind"] == "image" else None,
            "sprite": f"/api/media/{mid}/sprite" if (cdir / "sprite.jpg").exists() else None,
            "waveform": f"/api/media/{mid}/waveform" if (cdir / "wave.bin").exists() else None,
        },
    }


def ensure_color_space(svc: "Services", media_id: str) -> None:
    """Media imported before the colour matrix was recorded: read it once from the file and keep it with the probe summary."""
    row = svc.db.one("SELECT path, probe FROM media WHERE id = ?", (media_id,))
    if not row:
        return
    probe = json.loads(row["probe"] or "{}")
    if "color_space" in probe or not Path(row["path"]).exists():
        return
    try:
        probe["color_space"] = ff.summarize(ff.probe(svc.tools(), Path(row["path"])), Path(row["path"])).get("color_space") or ""
    except Exception:  # noqa: BLE001 - a file ffprobe cannot read now is shown as untagged; the preview falls back to BT.601
        probe["color_space"] = ""
    svc.db.execute("UPDATE media SET probe = ? WHERE id = ?", (dumps(probe), media_id))


def lookup(svc: "Services", media_id: str) -> Optional[dict[str, Any]]:
    row = svc.db.one("SELECT * FROM media WHERE id = ?", (media_id,))
    return view(svc, row) if row else None


def get(svc: "Services", media_id: str) -> dict[str, Any]:
    info = lookup(svc, media_id)
    if info is None:
        raise NotFound(f"No media {media_id}.")
    return info


def list_media(svc: "Services", *, kind: Optional[str] = None, query: str = "", limit: int = 500) -> list[dict[str, Any]]:
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if query:
        where.append("(name LIKE ? OR path LIKE ?)")
        args += [f"%{query}%", f"%{query}%"]
    sql = "SELECT * FROM media" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_ts DESC LIMIT ?"
    return [view(svc, r) for r in svc.db.query(sql, (*args, limit))]


def import_path(svc: "Services", raw: str, *, origin: str = "import", name: str = "", prepare: bool = True) -> dict[str, Any]:
    path = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))).resolve()
    _check_root(svc, path)
    if not path.is_file():
        raise NotFound(f"There is no file at {path}.")
    if path.suffix.lower() not in ff.MEDIA_EXTENSIONS:
        raise LumiereError(f"{path.name} is not a video, audio or image file this app imports.")
    fp = fingerprint(path)
    row = svc.db.one("SELECT * FROM media WHERE fingerprint = ? AND path = ?", (fp, str(path)))
    if row:
        if prepare and row["proxy"] in ("pending", "failed"):
            schedule_prepare(svc, row["id"])
        return {**view(svc, row), "existing": True}
    tools = svc.tools()
    info = ff.summarize(ff.probe(tools, path), path)
    if info["kind"] == "unknown":
        raise LumiereError(f"{path.name} has no video or audio stream.")
    mid = new_id("med")
    svc.db.execute(
        "INSERT INTO media(id, path, name, kind, origin, fingerprint, bytes, duration_ms, width, height, fps, has_video, has_audio, probe, proxy, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, str(path), clip(name or path.stem, 200), info["kind"], origin, fp, path.stat().st_size, info["duration_ms"], info["width"],
         info["height"], info["fps"], int(info["has_video"]), int(info["has_audio"]), dumps(info), "pending", time.time()))
    if prepare:
        schedule_prepare(svc, mid)
    svc.emit("lumiere.media.imported", {"id": mid, "name": clip(name or path.stem, 80), "kind": info["kind"]})
    return {**get(svc, mid), "existing": False}


def import_folder(svc: "Services", raw: str, *, recursive: bool = False, limit: int = 500) -> dict[str, Any]:
    folder = Path(os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))).resolve()
    _check_root(svc, folder)
    if not folder.is_dir():
        raise NotFound(f"There is no folder at {folder}.")
    files = sorted(folder.rglob("*") if recursive else folder.iterdir())
    files = [f for f in files if f.is_file() and f.suffix.lower() in ff.MEDIA_EXTENSIONS][:limit]
    imported, errors = [], []
    for f in files:
        try:
            imported.append(import_path(svc, str(f)))
        except (LumiereError, NotFound) as error:
            errors.append({"file": f.name, "error": str(error)})
    return {"imported": [{"id": m["id"], "name": m["name"], "kind": m["kind"], "existing": m["existing"]} for m in imported], "errors": errors}


def save_upload(svc: "Services", filename: str, stream, size_limit: int) -> dict[str, Any]:
    safe = "".join(c for c in Path(filename or "upload").name if c not in '<>:"/\\|?*').strip() or "upload"
    if Path(safe).suffix.lower() not in ff.MEDIA_EXTENSIONS:
        raise LumiereError(f"{safe} is not a video, audio or image file.")
    svc.config.uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = svc.config.uploads_dir / f"{int(time.time())}-{safe}"
    written = 0
    with open(dest, "wb") as out:
        while True:
            block = stream.read(1 << 20)
            if not block:
                break
            written += len(block)
            if written > size_limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise LumiereError("The file is larger than the upload limit; import it by path instead.")
            out.write(block)
    return import_path(svc, str(dest), origin="upload", name=Path(safe).stem)


def rename(svc: "Services", media_id: str, name: str) -> dict[str, Any]:
    get(svc, media_id)
    svc.db.execute("UPDATE media SET name = ? WHERE id = ?", (clip(name, 200), media_id))
    return get(svc, media_id)


def set_tags(svc: "Services", media_id: str, tags: list[str]) -> dict[str, Any]:
    """Labels on a media (up to 30, short): what b-roll suggestions match besides the name and what the media says."""
    get(svc, media_id)
    clean: list[str] = []
    for t in tags:
        t = clip(str(t).strip(), 40)
        if t and t.lower() not in (x.lower() for x in clean):
            clean.append(t)
    svc.db.execute("UPDATE media SET tags = ? WHERE id = ?", (dumps(clean[:30]), media_id))
    return get(svc, media_id)


def relink(svc: "Services", media_id: str, raw: str) -> dict[str, Any]:
    """Point a media whose file moved at its new location (same content is not required, same kind is)."""
    info = get(svc, media_id)
    path = Path(os.path.expanduser(raw.strip().strip('"'))).resolve()
    _check_root(svc, path)
    if not path.is_file():
        raise NotFound(f"There is no file at {path}.")
    probe = ff.summarize(ff.probe(svc.tools(), path), path)
    if probe["kind"] != info["kind"]:
        raise LumiereError(f"The new file is {probe['kind']}, the media is {info['kind']}.")
    svc.db.execute("UPDATE media SET path = ?, fingerprint = ?, bytes = ?, duration_ms = ?, width = ?, height = ?, fps = ?, probe = ? WHERE id = ?",
                   (str(path), fingerprint(path), path.stat().st_size, probe["duration_ms"], probe["width"], probe["height"], probe["fps"],
                    dumps(probe), media_id))
    return get(svc, media_id)


def open_shared(svc: "Services", raw: str, *, name: str = "") -> dict[str, Any]:
    """Reuse the same media ID when its shared original changes; rebuild caches.

    Timelines keep their references. Only disposable private cache files and
    old analyses are removed; the shared original is never moved or deleted.
    """
    path = Path(raw).resolve()
    _check_root(svc, path)
    row = svc.db.one("SELECT * FROM media WHERE path = ? AND origin = 'atlas' ORDER BY created_ts LIMIT 1", (str(path),))
    if row is None:
        return import_path(svc, str(path), origin="atlas", name=name)
    if row["fingerprint"] == fingerprint(path):
        return {**get(svc, row["id"]), "existing": True}
    if svc.jobs.list(state="active", media_id=row["id"]):
        raise LumiereError("Wait for this media's running jobs before refreshing its edited original.")
    # Validate before changing metadata or deleting a disposable cache.
    info = relink(svc, row["id"], str(path))
    base = svc.config.cache_dir.resolve()
    cache = (base / row["id"]).resolve()
    if cache.parent != base or cache == base:
        raise Refused("The private media cache escaped its folder.", code="outside_roots")
    files = [p for p in cache.rglob("*") if p.is_file()] if cache.exists() else []
    if any(not p.resolve().is_relative_to(cache) for p in files):
        raise Refused("The private media cache contains an external link.", code="outside_roots")
    for disposable in files:
        disposable.unlink()
    svc.db.execute("DELETE FROM analysis WHERE media_id = ?", (row["id"],))
    svc.db.execute("UPDATE media SET proxy = 'pending' WHERE id = ?", (row["id"],))
    schedule_prepare(svc, row["id"])
    return {**get(svc, row["id"]), "existing": True, "refreshed": True}


def delete(svc: "Services", media_id: str, *, force: bool = False) -> dict[str, Any]:
    info = get(svc, media_id)
    users = [r["id"] for r in svc.db.query("SELECT id, doc FROM projects") if f'"{media_id}"' in r["doc"]]
    if users and not force:
        raise LumiereError(f"{info['name']} is used in {len(users)} project(s): {', '.join(users)}. Pass force=true to remove it anyway.",
                           code="media_in_use")
    svc.db.execute("DELETE FROM media WHERE id = ?", (media_id,))
    shutil.rmtree(svc.config.cache_dir / media_id, ignore_errors=True)
    if info["origin"] in ("upload", "derived", "frame"):
        try:
            Path(info["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": media_id, "projects_affected": users}


# ---------------------------------------------------------------- analysis store

def get_analysis(svc: "Services", media_id: str, kind: str) -> Optional[dict[str, Any]]:
    row = svc.db.one("SELECT result FROM analysis WHERE media_id = ? AND kind = ?", (media_id, kind))
    return json.loads(row["result"]) if row else None


def put_analysis(svc: "Services", media_id: str, kind: str, result: dict[str, Any], params: Optional[dict] = None) -> None:
    svc.db.execute("INSERT INTO analysis(media_id, kind, params, result, updated_ts) VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT(media_id, kind) DO UPDATE SET params = excluded.params, result = excluded.result, updated_ts = excluded.updated_ts",
                   (media_id, kind, dumps(params or {}), dumps(result), time.time()))


def rms(svc: "Services", media_id: str) -> Optional[np.ndarray]:
    path = svc.config.cache_dir / media_id / "rms.npy"
    return np.load(path) if path.exists() else None


# ---------------------------------------------------------------- preparation job

def schedule_prepare(svc: "Services", media_id: str) -> dict[str, Any]:
    info = get(svc, media_id)
    return svc.jobs.submit("prepare", {"media": media_id}, label=f"Preparar {info['name']}", media_id=media_id)


def prepare_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    """Proxy (video/audio), poster, filmstrip and waveform for one media."""
    mid = ctx.params["media"]
    info = get(svc, mid)
    src = Path(info["path"])
    if not src.exists():
        raise NotFound(f"The file is missing: {src}")
    tools = svc.tools()
    cdir = cache_dir(svc, mid)
    dur = info["duration_ms"]
    done: dict[str, Any] = {}
    if info["kind"] == "image":
        ff.run(tools, ["-i", str(src), "-frames:v", "1", "-vf", "scale='min(640,iw)':-2", "-q:v", "3", str(cdir / "poster.jpg")], timeout=60)
        svc.db.execute("UPDATE media SET proxy = 'none' WHERE id = ?", (mid,))
        return {"poster": True}
    if info["has_video"]:
        ctx.progress(0.01, "proxy")
        w, h = info["width"] or 1280, info["height"] or 720
        if min(w, h) > PROXY_SHORT_SIDE:
            scale = f"scale={'-2' if w >= h else PROXY_SHORT_SIDE}:{PROXY_SHORT_SIDE if w >= h else '-2'}"
        else:
            scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        enc = svc.tools().video_encoder(svc.config.encoder)
        venc = (["-c:v", enc, "-preset", "p1", "-rc", "vbr", "-cq", "30", "-b:v", "0"] if "nvenc" in enc
                else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"])
        tmp = cdir / "proxy.tmp.mp4"
        args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"{scale},format=yuv420p", *venc, "-g", "15",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(tmp)]
        ff.run(tools, args, duration_ms=dur, progress=lambda p: ctx.progress(0.01 + p * 0.6, "proxy"), handle=ctx.handle())
        os.replace(tmp, cdir / "proxy.mp4")
        svc.db.execute("UPDATE media SET proxy = 'ready' WHERE id = ?", (mid,))
        done["proxy"] = True
        ctx.check()
        ff.grab_frame(tools, cdir / "proxy.mp4", min(dur // 3, 3000) if dur else 0, cdir / "poster.jpg", width=480)
        # filmstrip: one tile per interval, at most SPRITE_MAX tiles
        count = max(1, min(SPRITE_MAX, dur // 1000))
        interval = max(1000, dur // count) if dur else 1000
        cols = 10
        rows = max(1, -(-count // cols))
        th = max(2, int(round(SPRITE_TILE_W * h / max(1, w) / 2)) * 2)
        ff.run(tools, ["-i", str(cdir / "proxy.mp4"), "-vf", f"fps=1000/{interval},scale={SPRITE_TILE_W}:{th},tile={cols}x{rows}", "-frames:v", "1",
                       "-q:v", "4", str(cdir / "sprite.jpg")], timeout=1800, handle=ctx.handle())
        (cdir / "sprite.json").write_text(json.dumps({"interval_ms": interval, "tile_w": SPRITE_TILE_W, "tile_h": th, "cols": cols, "count": count}),
                                           encoding="utf-8")
        done["sprite"] = count
    elif info["has_audio"]:
        tmp = cdir / "proxy.tmp.m4a"
        ff.run(tools, ["-i", str(src), "-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-movflags", "+faststart", str(tmp)],
               duration_ms=dur, progress=lambda p: ctx.progress(p * 0.5, "proxy"), handle=ctx.handle())
        os.replace(tmp, cdir / "proxy.m4a")
        svc.db.execute("UPDATE media SET proxy = 'ready' WHERE id = ?", (mid,))
        done["proxy"] = True
    if info["has_audio"]:
        ctx.progress(0.7, "waveform")
        peaks, levels = audio_an.envelope(tools, src, duration_ms=dur, progress=lambda p: ctx.progress(0.7 + p * 0.29, "waveform"),
                                          handle=ctx.handle())
        (cdir / "wave.bin").write_bytes(peaks.tobytes())
        np.save(cdir / "rms.npy", levels)
        put_analysis(svc, mid, "levels", {"noise_floor_db": round(audio_an.noise_floor(levels), 1),
                                          "auto_threshold_db": round(audio_an.auto_threshold(levels), 1),
                                          "speech_level_db": round(float(np.percentile(levels, 80)), 1) if levels.size else None})
        done["waveform"] = int(peaks.size)
    svc.emit("lumiere.media.ready", {"id": mid, "name": clip(info["name"], 80)})
    return done


def frame_path(svc: "Services", media_id: str, at_ms: int, width: int = 240) -> Path:
    """A JPEG of the picture at ``at_ms`` of the media (made by the server, so the browser needs no codec; cached by 100 ms step).
    The multicam angle viewer shows these."""
    info = get(svc, media_id)
    if not info["has_video"] and info["kind"] != "image":
        raise LumiereError(f"{info['name']} has no picture.")
    dur = info["duration_ms"] or 0
    at = max(0, min(int(at_ms), max(0, dur - 40))) // 100 * 100 if info["kind"] != "image" else 0
    width = max(64, min(int(width), 1280))
    out = cache_dir(svc, media_id) / "frames" / f"{at}_{width}.jpg"
    if not out.exists():
        src = Path(proxy_path(svc, media_id) or info["path"])
        ff.grab_frame(svc.tools(), src, at, out, width=width)
    return out


def play_path(svc: "Services", media_id: str) -> Path:
    """What the browser plays: the proxy when it exists, else the original."""
    info = get(svc, media_id)
    cdir = svc.config.cache_dir / media_id
    for name in ("proxy.mp4", "proxy.m4a"):
        if (cdir / name).exists():
            return cdir / name
    return Path(info["path"])


def proxy_path(svc: "Services", media_id: str) -> Optional[str]:
    p = svc.config.cache_dir / media_id / "proxy.mp4"
    return str(p) if p.exists() else None


def audio_master(svc: "Services", media_id: str, stream: int = 0, handle=None) -> Path:
    """48 kHz stereo FLAC of one audio stream, made once: the render reads every sound from these."""
    info = get(svc, media_id)
    cdir = cache_dir(svc, media_id)
    out = cdir / f"a{stream}.flac"
    with _MASTER_LOCK_GUARD:
        lock=_MASTER_LOCKS.setdefault(out.resolve(),Lock())
    with lock:
        if out.exists() and out.stat().st_mtime >= Path(info["path"]).stat().st_mtime:
            return out
        tmp = cdir / f"a{stream}.tmp.flac"
        ff.run(svc.tools(), ["-i", info["path"], "-map", f"0:a:{stream}", "-vn", "-ac", "2", "-ar", "48000", "-c:a", "flac", "-compression_level", "2",
                             str(tmp)], duration_ms=info["duration_ms"], handle=handle)
        os.replace(tmp, out)
    return out


def speech_wav(svc: "Services", media_id: str, stream: int = 0, handle=None) -> Path:
    info = get(svc, media_id)
    cdir = cache_dir(svc, media_id)
    out = cdir / f"speech{stream}.wav"
    if out.exists():
        return out
    tmp = cdir / f"speech{stream}.tmp.wav"
    ff.run(svc.tools(), ["-i", info["path"], "-map", f"0:a:{stream}", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)],
           duration_ms=info["duration_ms"], handle=handle)
    os.replace(tmp, out)
    return out
