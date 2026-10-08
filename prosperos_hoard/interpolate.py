"""Independent clip finishing with FFmpeg's motion-compensated interpolation.

Inspired by YAW 6.7's standalone FILM stage; this is a CPU method, not FILM.
The shared media runner owns probing, subprocesses, timeout and cancellation.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .hoard_link.media.ffmpeg import FFmpeg
from .hoard_link.proc import Cancelled
from .ids import new_id
from .jobs import JobCancelled
from .util import now_iso


def validate_fps(fps: float) -> float:
    value = float(fps)
    if not math.isfinite(value) or not 1 <= value <= 120:
        raise ValueError("fps must be a finite number between 1 and 120")
    return value


def command(src: Path, dst: Path, fps: float, source_fps: float, duration: float, video_start: float) -> list[str]:
    # minterpolate needs future frames. Pad only its look-ahead, then trim
    # to the actual video duration; without that, it drops the final frames.
    vf = (f"setpts=PTS-({video_start:.9f})/TB,tpad=stop_mode=clone:stop_duration={3 / source_fps:.9f},"
          f"minterpolate=fps={fps:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bilat:vsbmc=1:scd=fdiff,"
          f"trim=duration={duration:.9f},setpts=PTS-STARTPTS,format=yuv420p")
    # Both streams share the video's origin. Resetting each STARTPTS would
    # make a delayed audio track start too early and break lip sync.
    return ["-copyts", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-af",
            f"asetpts=PTS-({video_start:.9f})/TB,atrim=start=0",
            "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.9f}", "-movflags", "+faststart", str(dst)]


class _CancelCheck:
    def __init__(self, progress):
        self.progress = progress

    def is_set(self) -> bool:
        check = getattr(self.progress, "cancelled", None)
        return bool(check and check())


def source_plan(store, source: dict[str, Any], fps: float):
    """Probe before queueing so an invalid rate is a direct tool error.

    The worker probes again: queued files may have changed meanwhile.
    """
    from .engine import EngineError
    if source["kind"] != "video":
        raise EngineError("not_video", "interpolation needs a video asset")
    try:
        fps = validate_fps(fps)
    except (ValueError, TypeError) as exc:
        raise EngineError("bad_fps", str(exc)) from exc
    media = FFmpeg()
    src = store.data_dir / source["file_path"]
    info = media.probe(src)
    summary = media.summarize(info)
    stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
                   and not (s.get("disposition") or {}).get("attached_pic")), {})
    source_fps = float(summary.get("fps") or 0)
    duration = float(stream.get("duration") or summary.get("duration_s") or 0)
    video_start = float(stream.get("start_time") or 0)
    if not all(math.isfinite(v) for v in (source_fps, duration, video_start)) or source_fps <= 0 or duration <= 0:
        raise EngineError("bad_video", "the video has no usable frame rate or duration")
    if duration * source_fps < 3:
        raise EngineError("clip_too_short", "interpolation needs at least three source frames")
    if fps <= source_fps + 1e-6:
        raise EngineError("bad_fps", f"choose fps above the source rate ({source_fps:g})")
    return media, src, summary, fps, source_fps, duration, video_start


def run(store, job: dict[str, Any], progress) -> dict[str, Any]:
    from .engine import _rel, _video_thumbnail, EngineError

    p = job["params"]
    source = store.get_asset(p["asset_id"])
    media, src, summary, fps, source_fps, duration, video_start = source_plan(store, source, p.get("fps", 48))
    aid = new_id("a")
    dst = store.path_for_asset_file(aid, ".mp4")
    thumb_path = store.path_for_thumb(aid)
    progress(0.05, f"interpolating {source_fps:g} to {fps:g} fps")
    try:
        media.run(command(src, dst, fps, source_fps, duration, video_start), duration_s=duration,
                  progress=lambda f: progress(0.05 + 0.85 * f, "interpolating motion"),
                  cancel=_CancelCheck(progress), timeout=3600, low_priority=True)
        progress(0.92, "checking the finished clip")
        result = media.summarize(media.probe(dst))
        if abs(float(result.get("fps") or 0) - fps) > 0.01:
            raise EngineError("interpolation_failed", "the result has an unexpected frame rate")
        if abs(float(result.get("duration_s") or 0) - duration) > max(0.1, 2 / fps):
            raise EngineError("interpolation_failed", "the result did not preserve the video duration")
        thumb = _video_thumbnail(dst, thumb_path)
        progress(0.98, "saving the interpolated take")
        recipe = {"operation": "interpolate", "method": "ffmpeg_minterpolate", "backend": "local",
                  "input_asset_ids": [source["id"]], "derived_from": source["id"],
                  "source_fps": source_fps, "fps": fps, "audio": "aac" if summary.get("audio_streams") else "none",
                  "created_at": now_iso()}
        asset = store.create_asset(project_id=source["project_id"], kind="video", file_path=_rel(store, dst),
                                   mime="video/mp4", width=result["width"], height=result["height"],
                                   duration_s=result["duration_s"], thumb_path=thumb, source="derived", recipe=recipe,
                                   asset_id=aid, name=f"{source.get('name') or source['id']} {fps:g} fps"[:100],
                                   tags=["interpolated"])
        return {"asset_id": asset["id"], "asset_ids": [asset["id"]], "fps": fps,
                "source_fps": source_fps, "method": "ffmpeg_minterpolate"}
    except BaseException as exc:
        dst.unlink(missing_ok=True)
        thumb_path.unlink(missing_ok=True)
        if isinstance(exc, Cancelled):
            raise JobCancelled("interpolation cancelled") from exc
        raise
