"""Nested sequences: a project used as a clip is rendered once into an intermediate (picture + 48 kHz sound) that the
parent's render reads like any other media, so every clip feature (fit, speed curves, masks, effects, transitions)
works on a sequence without a second compiler.

The intermediate is keyed on the content of the nested timeline, of every project nested in it and of the files its
media point at: editing the nested project (or anything inside it) makes a new one, and an undo back to an earlier
state finds the old one again. A nested project with nothing on a main track keeps its transparency (lossless FFV1 with
alpha), so nesting overlays does not hide what is below them; otherwise the picture is near-lossless H.264.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .. import ffmpeg as ff
from .. import media as media_store
from .. import projects as project_store
from ..errors import LumiereError
from ..timeline import Project
from . import compiler as C
from .ass import build_ass

if TYPE_CHECKING:
    from ..jobs import JobCtx
    from ..services import Services

FORMAT = 1  # bump when the way intermediates are made changes
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass
class SeqMaster:
    project: str
    key: str
    dir: Path
    video: Path
    audio_rel: str  # the sound, relative to the cache folder (the sound pass runs there)
    alpha: bool
    width: int
    height: int
    fps: float
    duration_ms: int


def _lock(name: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(name, threading.Lock())


def root(svc: "Services") -> Path:
    path = svc.config.cache_dir / "seq"
    path.mkdir(parents=True, exist_ok=True)
    return path


def has_alpha(p: Project) -> bool:
    """Transparent where nothing is drawn when no main track holds clips (a nested group of overlays)."""
    return not any(t.kind == "video" and t.role == "main" and t.clips for t in p.tracks)


def content_key(svc: "Services", project_id: str, _stack: tuple[str, ...] = ()) -> str:
    if project_id in _stack:
        raise LumiereError("These projects nest each other in a loop: " + " > ".join((*_stack, project_id)), code="sequence_cycle")
    p = project_store.doc(svc, project_id)
    parts: list[str] = [str(FORMAT), json.dumps(p.dump(), sort_keys=True, separators=(",", ":"))]
    for mid in sorted(p.media_ids()):
        info = media_store.lookup(svc, mid) or {}
        path = Path(info.get("path") or "")
        stamp = path.stat().st_mtime_ns if path.is_file() else 0
        parts.append(f"{mid}:{path}:{stamp}")
    for sid in sorted(p.sequence_ids()):
        parts.append(f"{sid}:{content_key(svc, sid, (*_stack, project_id))}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _folder(svc: "Services", project_id: str, key: str) -> Path:
    return root(svc) / f"{project_id}-{key}"


def _read(svc: "Services", project_id: str, key: str) -> Optional[SeqMaster]:
    folder = _folder(svc, project_id, key)
    done = folder / "done.json"
    if not done.is_file():
        return None
    meta = json.loads(done.read_text(encoding="utf-8"))
    video = folder / meta["video"]
    if not video.is_file() or not (folder / "a0.flac").is_file():
        return None
    return SeqMaster(project_id, key, folder, video, f"seq/{folder.name}/a0.flac", bool(meta["alpha"]), int(meta["width"]), int(meta["height"]),
                     float(meta["fps"]), int(meta["duration_ms"]))


def cached(svc: "Services", project_id: str) -> Optional[SeqMaster]:
    """The current intermediate of a project if it is already made (nothing is rendered here)."""
    try:
        return _read(svc, project_id, content_key(svc, project_id))
    except Exception:  # noqa: BLE001 - a loop or a missing project simply has no intermediate
        return None


def ensure(svc: "Services", project_id: str, *, stack: tuple[str, ...] = (), ctx: Optional["JobCtx"] = None) -> SeqMaster:
    """The intermediate of a nested project, made now if needed (nested sequences inside it first)."""
    if project_id in stack:
        raise LumiereError("These projects nest each other in a loop: " + " > ".join((*stack, project_id)), code="sequence_cycle")
    key = content_key(svc, project_id, stack)
    found = _read(svc, project_id, key)
    if found:
        return found
    with _lock(f"{project_id}-{key}"):
        found = _read(svc, project_id, key)
        if found:
            return found
        return _build(svc, project_id, key, stack, ctx)


def _build(svc: "Services", project_id: str, key: str, stack: tuple[str, ...], ctx: Optional["JobCtx"]) -> SeqMaster:
    from . import runner  # lazy: runner imports this module

    p = project_store.doc(svc, project_id)
    total = p.duration
    if total < 40:
        raise LumiereError(f"The nested project {project_id} is empty.", code="empty_sequence")
    final = _folder(svc, project_id, key)
    work = final.with_name(final.name + f".tmp{threading.get_ident()}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    tools = svc.tools()
    handle = ctx.handle if ctx is not None else (lambda: None)
    try:
        rc = runner.RenderContext(svc, p, work, stack=(*stack, project_id))
        rc.prepare(ctx)
        # sound
        ag = C.audio_graph(p, total, rc.media, rc.master_name)
        for mid, stream in ag.sources:
            if not mid.startswith("prj_"):
                media_store.audio_master(svc, mid, stream, handle=handle())
        (work / "audio_graph.txt").write_text(ag.graph, encoding="utf-8")
        audio_tmp = work / "a0.flac"
        ff.run(tools, runner._script_flag(tools, str(work / "audio_graph.txt")) + ["-map", "[aout]", "-c:a", "flac", "-ar", "48000", str(audio_tmp)],
               duration_ms=total, handle=handle(), cwd=svc.config.cache_dir)
        # picture
        alpha = has_alpha(p)
        fps = p.canvas.fps
        fps_expr = ff.fps_fraction(fps)
        out = C.Output(p.canvas.width, p.canvas.height, fps, fps_expr, alpha=alpha)
        cx = C.ChainCtx(out, rc.lut_name, lambda c: None)
        ass, counts = build_ass(p, rc.words_for)
        ass_name = None
        if counts["captions"] or counts["titles"]:
            (work / "subs.ass").write_text(ass, encoding="utf-8")
            ass_name = "subs.ass"
        ext = ".mkv" if alpha else ".mp4"
        vargs = (["-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuva420p"] if alpha else
                 ["-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-g", str(max(1, int(round(fps)))), "-pix_fmt", "yuv420p"])
        chunks: list[str] = []
        for i, (f0, f1) in enumerate(C.plan_chunks(p, total, fps)):
            if ctx is not None:
                ctx.check()
            g = C.chunk_graph(p, i, f0, f1, out, rc.media, cx, ass_name)
            (work / f"graph_{i:04d}.txt").write_text(g.graph, encoding="utf-8")
            name = f"chunk_{i:04d}{ext}"
            ff.run(tools, [a for inp in g.inputs for a in inp] + runner._script_flag(tools, f"graph_{i:04d}.txt") + [
                "-map", f"[{g.out_label}]", "-frames:v", str(f1 - f0), "-r", fps_expr, *vargs, "-an", name],
                duration_ms=int((f1 - f0) * 1000 / fps), handle=handle(), cwd=work)
            chunks.append(name)
        (work / "chunks.txt").write_text("".join(f"file '{c}'\n" for c in chunks), encoding="utf-8")
        ff.run(tools, ["-f", "concat", "-safe", "0", "-i", "chunks.txt", "-c", "copy", f"video{ext}"], cwd=work, handle=handle())
        for name in chunks:
            (work / name).unlink(missing_ok=True)
        meta = {"project": project_id, "key": key, "video": f"video{ext}", "alpha": alpha, "width": p.canvas.width, "height": p.canvas.height,
                "fps": fps, "duration_ms": total, "created_ts": time.time()}
        (work / "done.json").write_text(json.dumps(meta), encoding="utf-8")
        shutil.rmtree(final, ignore_errors=True)
        os.replace(work, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    _forget_old(svc, project_id, key)
    found = _read(svc, project_id, key)
    assert found is not None
    return found


def _forget_old(svc: "Services", project_id: str, keep: str) -> None:
    """Older intermediates of the same project: kept for a while (an undo may want them back), the oldest beyond three go."""
    olds = sorted((d for d in root(svc).glob(f"{project_id}-*") if d.is_dir() and d.name != f"{project_id}-{keep}" and ".tmp" not in d.name),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in olds[3:]:
        shutil.rmtree(d, ignore_errors=True)


def prepare_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    """Background job: make a nested project's intermediate now (so the live preview can play the real thing)."""
    ctx.progress(0.05, "rendering the nested sequence", force=True)
    ensure(svc, ctx.params["project"], ctx=ctx)
    return status(svc, ctx.params["project"])


def status(svc: "Services", project_id: str) -> dict[str, Any]:
    """What an assistant or the editor needs to know about a nested project's intermediate."""
    m = cached(svc, project_id)
    return {"project": project_id, "ready": bool(m), "key": m.key if m else None, "alpha": m.alpha if m else None,
            "duration_ms": m.duration_ms if m else None, "url": f"/api/projects/{project_id}/sequence.mp4" if m else None}
