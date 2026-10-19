"""Standalone size-limited MP4 exports, using the ordinary cancellable job queue."""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from . import ffmpeg as ff
from . import media
from .errors import LumiereError, NotFound
from .util import safe_filename


class CompressArgs(BaseModel):
    path: str = Field(..., min_length=1, max_length=2000, description="Local video path; the original is kept.")
    max_mb: float = Field(25, gt=0, allow_inf_nan=False, description="Maximum output size in decimal MB (1 MB = 1,000,000 bytes).")
    folder: str = Field("", max_length=2000, description="Output folder; empty writes beside the original.")


def start(svc, path: str, max_mb: float = 25, folder: str = "") -> dict:
    args = CompressArgs(path=path, max_mb=max_mb, folder=folder)
    source = Path(args.path).expanduser().resolve()
    media._check_root(svc, source)
    if not source.is_file():
        raise NotFound("The source video does not exist.")
    destination = Path(folder).expanduser().resolve() if folder else source.parent
    media._check_root(svc, destination)
    return svc.jobs.submit("video_compress", {"path": str(source), "max_mb": args.max_mb, "folder": str(destination)},
                           label=f"Compress: {source.name}", dedupe=False)


def run(svc, ctx) -> dict:
    source = Path(ctx.params["path"])
    tools = svc.tools()
    info = ff.probe(tools, source)
    summary = ff.summarize(info, source)
    seconds = float(summary.get("duration_ms") or 0) / 1000
    if not summary.get("has_video") or not math.isfinite(seconds) or seconds <= 0:
        raise LumiereError("Compression needs a video with a readable duration.")
    limit = int(float(ctx.params["max_mb"]) * 1_000_000)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    copy_audio = bool(audio and audio.get("codec_name") == "aac")
    audio_rate = max(1, int(audio.get("bit_rate") or 192000)) if copy_audio else (128000 if audio else 0)
    source_bytes = source.stat().st_size
    if source_bytes <= limit:
        return {**summary, "path": str(source), "bytes": source_bytes, "max_bytes": limit, "fits": True, "compressed": False,
                "url": f"/api/jobs/{ctx.id}/file"}
    video_rate = int(limit * 8 * .96 / seconds - audio_rate)
    if video_rate <= 0:
        raise LumiereError("The size limit cannot hold the audio. Choose a larger maximum.", code="size_limit")
    folder = Path(ctx.params["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_filename(f"{source.stem[:80]}_under-{ctx.params['max_mb']:g}MB_{ctx.id}.mp4", limit=180)
    destination = folder / name
    # A job ID gives every result its own filename; no existing file is overwritten.
    if destination.exists():
        raise LumiereError("The output already exists; start a new compression job.")
    handle = ctx.handle()
    # Keep the encoded temporary on the destination volume for atomic publication.
    with tempfile.TemporaryDirectory(prefix="compress-", dir=folder) as scratch:
        work = Path(scratch)
        encoded = work / "output.mp4"
        for attempt in range(2):
            ctx.check()
            common = ["-y", "-i", str(source), "-map", "0:v:0", "-c:v", "libx264", "-preset", "fast", "-threads", "4",
                      "-b:v", str(video_rate), "-pix_fmt", "yuv420p", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                      "-passlogfile", str(work / f"pass-{attempt}")]
            base, span = (0.02, 0.86) if attempt == 0 else (0.89, 0.09)
            ff.run(tools, [*common, "-pass", "1", "-an", "-f", "null", os.devnull], duration_ms=round(seconds * 1000),
                   handle=handle, progress=lambda x: ctx.progress(base + span * x / 2, "Video analysis"))
            sound = ["-map", "0:a:0", "-c:a", "copy"] if copy_audio else (["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"] if audio else ["-an"])
            ff.run(tools, [*common, "-pass", "2", *sound, "-map_metadata", "0", "-movflags", "+faststart", str(encoded)],
                   duration_ms=round(seconds * 1000), handle=handle,
                   progress=lambda x: ctx.progress(base + span * (.5 + x / 2), "Encoding size-limited MP4"))
            ctx.check()
            size = encoded.stat().st_size
            if size <= limit:
                break
            video_rate = int(video_rate * limit / size * .95)
        if size > limit:
            raise LumiereError("The encoded file still exceeds the maximum; no oversized output was published.", code="size_limit")
        result_summary = ff.summarize(ff.probe(tools, encoded), encoded)
        if abs(float(result_summary.get("duration_ms") or 0) / 1000 - seconds) > .5 or bool(result_summary.get("has_audio")) != bool(audio):
            raise LumiereError("The compressed video failed its duration or sound check.")
        ctx.check()
        encoded.replace(destination)
    ctx.progress(1, "Compressed video ready", force=True)
    return {**result_summary, "path": str(destination), "bytes": destination.stat().st_size, "max_bytes": limit,
            "fits": True, "compressed": True, "source_bytes": source_bytes,
            "audio_mode": "copy" if copy_audio else "aac" if audio else "none", "url": f"/api/jobs/{ctx.id}/file"}
