"""Derived media: a stabilized copy of the part of a source a clip uses, and freeze frames. The clip is then pointed at
the new file, so the render stays a plain cut of it (stabilization needs the whole span analysed in one pass)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import ffmpeg as ff
from . import media as media_store
from . import projects as project_store
from .errors import LumiereError

if TYPE_CHECKING:
    from .jobs import JobCtx
    from .services import Services

HANDLE_MS = 500


def stabilize_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    pid, clip_id = ctx.params["project"], ctx.params["clip"]
    smoothing = int(ctx.params.get("smoothing", 15))
    shakiness = int(ctx.params.get("shakiness", 6))
    p = project_store.doc(svc, pid)
    _, c = p.find(clip_id)
    if c.type != "media":
        raise LumiereError("Only video clips can be stabilized.")
    info = media_store.get(svc, c.media)
    if info["kind"] != "video":
        raise LumiereError("Only video clips can be stabilized.")
    tools = svc.tools()
    if not tools.has_filter("vidstabdetect"):
        raise LumiereError("This ffmpeg has no vid.stab filters (vidstabdetect).", code="no_vidstab")
    a = max(0, c.src_in - HANDLE_MS)
    b = min(info["duration_ms"] or c.src_out + HANDLE_MS, c.src_out + HANDLE_MS)
    key = hashlib.sha1(f"{c.media}:{a}:{b}:{smoothing}:{shakiness}".encode()).hexdigest()[:10]
    cdir = media_store.cache_dir(svc, c.media)
    out = cdir / f"stab_{key}.mp4"
    trf = f"stab_{key}.trf"
    if not out.exists():
        ctx.progress(0.02, "analysing motion", force=True)
        ff.run(tools, ["-ss", ff.seconds(a), "-t", ff.seconds(b - a), "-i", info["path"], "-an", "-vf",
                       f"vidstabdetect=shakiness={shakiness}:accuracy=15:result={trf}", "-f", "null", "-"],
               duration_ms=b - a, progress=lambda x: ctx.progress(0.02 + 0.45 * x, "analysing motion"), handle=ctx.handle(), cwd=cdir)
        venc = svc.tools().video_encoder(svc.config.encoder)
        vq = ["-c:v", venc, "-preset", "p6", "-rc", "vbr", "-cq", "16", "-b:v", "0"] if "nvenc" in venc else ["-c:v", "libx264", "-preset", "medium", "-crf", "16"]
        tmp = cdir / f"stab_{key}.tmp.mp4"
        ff.run(tools, ["-ss", ff.seconds(a), "-t", ff.seconds(b - a), "-i", info["path"], "-map", "0:v:0", "-map", "0:a?", "-vf",
                       f"vidstabtransform=input={trf}:smoothing={smoothing}:optzoom=1:interpol=bicubic,unsharp=5:5:0.6:3:3:0.3,format=yuv420p",
                       *vq, "-c:a", "aac", "-b:a", "256k", str(tmp)], duration_ms=b - a,
               progress=lambda x: ctx.progress(0.5 + 0.45 * x, "stabilizing"), handle=ctx.handle(), cwd=cdir)
        os.replace(tmp, out)
        (cdir / trf).unlink(missing_ok=True)
    derived = media_store.import_path(svc, str(out), origin="derived", name=f"{info['name']} (estabilizado)")
    shift = a
    project_store.edit(svc, pid, [{"op": "replace_media", "clip": clip_id, "media": derived["id"]},
                                  {"op": "trim", "clip": clip_id, "src_in": c.src_in - shift, "src_out": c.src_out - shift, "ripple": False}],
                       label="Estabilizar", actor="tool")
    return {"clip": clip_id, "media": derived["id"], "file": str(out)}


def freeze_frame(svc: "Services", project_id: str, clip_id: str, at_ms: int, length_ms: int = 2000) -> dict[str, Any]:
    """Insert a still of the frame at ``at_ms`` (timeline time) for ``length_ms``: split, still, rest of the clip."""
    p = project_store.doc(svc, project_id)
    track, c = p.find(clip_id)
    if c.type != "media":
        raise LumiereError("Freeze frames come from video clips.")
    if not c.start < at_ms < c.end:
        raise LumiereError("That time is not inside the clip.")
    info = media_store.get(svc, c.media)
    src = int(c.src_at(at_ms))
    cdir = media_store.cache_dir(svc, c.media)
    still = cdir / f"frame_{src}.png"
    if not still.exists():
        ff.grab_frame(svc.tools(), Path(info["path"]), src, still)
    frame = media_store.import_path(svc, str(still), origin="frame", name=f"{info['name']} · {src / 1000:.2f}s")
    ops: list[dict[str, Any]] = [{"op": "split", "clip": clip_id, "at": at_ms},
                                 {"op": "add_media", "media": frame["id"], "track": track.id, "at": at_ms, "length": length_ms, "mode": "insert",
                                  "fit": c.transform.fit}]
    res = project_store.edit(svc, project_id, ops, label="Congelar imagen")
    return {"media": frame["id"], **res}
