"""ffmpeg-backed rendering: normalise each timeline clip to a short mp4,
concatenate (cut or xfade), burn an ASS subtitle track for lyrics, mux the
song, and produce the final file. Every ffmpeg invocation is built by a
pure `build_*_cmd()` function (argv list, no I/O) so it can be snapshot
-tested without running ffmpeg; `run_ffmpeg_with_progress` is the only
part that actually shells out.

Quality presets: `preview` = 540p short side, `-preset ultrafast`;
`final` = the timeline's native resolution, `-preset medium -crf 18`,
AAC 192k.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .backend import ffmpeg_path

QUALITY_PRESETS = {
    "preview": {"preset": "ultrafast", "crf": 28, "short_side": 540, "audio_bitrate": "128k"},
    "final": {"preset": "medium", "crf": 18, "short_side": None, "audio_bitrate": "192k"},
}

TRANSITION_MAP = {"crossfade": "fade", "dip_black": "fadeblack", "flash_white": "fadewhite"}


class RenderError(RuntimeError):
    pass


def _scaled_resolution(width: int, height: int, short_side: Optional[int]) -> tuple[int, int]:
    if not short_side:
        return width, height
    if width < height:
        scale = short_side / width
    else:
        scale = short_side / height
    w = int(round(width * scale / 2) * 2)
    h = int(round(height * scale / 2) * 2)
    return w, h


def _zoompan_expr(zoom_start: float, zoom_end: float, pan: str, n_frames: int) -> tuple[str, str, str]:
    n_frames = max(1, n_frames)
    inc = (zoom_end - zoom_start) / n_frames
    z = f"if(eq(on,1),{zoom_start},min(zoom+{inc:.6f},{zoom_end}))"
    drift = 48
    x = "iw/2-(iw/zoom/2)"
    y = "ih/2-(ih/zoom/2)"
    if pan == "left":
        x = f"(iw/2-(iw/zoom/2))-(on/{n_frames})*{drift}"
    elif pan == "right":
        x = f"(iw/2-(iw/zoom/2))+(on/{n_frames})*{drift}"
    elif pan == "up":
        y = f"(ih/2-(ih/zoom/2))-(on/{n_frames})*{drift}"
    elif pan == "down":
        y = f"(ih/2-(ih/zoom/2))+(on/{n_frames})*{drift}"
    return z, x, y


def build_image_clip_cmd(
    ffmpeg: str, src: Path, out_path: Path, width: int, height: int, fps: int, duration_s: float,
    ken_burns: Optional[dict[str, Any]] = None,
) -> list[str]:
    ken_burns = ken_burns or {"zoom_start": 1.0, "zoom_end": 1.0, "pan": "none"}
    n_frames = max(1, round(duration_s * fps))
    z, x, y = _zoompan_expr(ken_burns.get("zoom_start", 1.0), ken_burns.get("zoom_end", 1.0), ken_burns.get("pan", "none"), n_frames)
    vf = (
        f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
        f"crop={width*2}:{height*2},"
        f"zoompan=z='{z}':d={n_frames}:s={width}x{height}:fps={fps}:x='{x}':y='{y}',"
        f"format=yuv420p"
    )
    return [
        ffmpeg, "-y", "-loop", "1", "-i", str(src), "-t", f"{duration_s:.3f}",
        "-vf", vf, "-r", str(fps), "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(out_path),
    ]


def build_video_clip_cmd(
    ffmpeg: str, src: Path, out_path: Path, width: int, height: int, fps: int, duration_s: float, trim_start_s: float,
) -> list[str]:
    vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},format=yuv420p"
    return [
        ffmpeg, "-y", "-ss", f"{trim_start_s:.3f}", "-i", str(src), "-t", f"{duration_s:.3f}",
        "-vf", vf, "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path),
    ]


def build_concat_cmd(ffmpeg: str, list_file: Path, out_path: Path) -> list[str]:
    return [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out_path)]


def build_xfade_cmd(ffmpeg: str, clip_paths: list[Path], durations: list[float], transitions: list[dict[str, Any]], out_path: Path) -> list[str]:
    """`transitions[i]` is the transition used entering clip i (i=0 unused)."""
    inputs: list[str] = []
    for p in clip_paths:
        inputs += ["-i", str(p)]
    filters = []
    label = "0:v"
    cumulative = durations[0]
    for i in range(1, len(clip_paths)):
        t = transitions[i]
        xfade_type = TRANSITION_MAP.get(t.get("type", "cut"), "fade")
        dur = max(0.05, t.get("duration_s", 0.15))
        offset = max(0.0, cumulative - dur)
        out_label = f"v{i}"
        filters.append(f"[{label}][{i}:v]xfade=transition={xfade_type}:duration={dur:.3f}:offset={offset:.3f}[{out_label}]")
        label = out_label
        cumulative += durations[i] - dur
    filter_complex = ";".join(filters)
    return [
        ffmpeg, "-y", *inputs, "-filter_complex", filter_complex, "-map", f"[{label}]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path),
    ]


def build_mux_cmd(ffmpeg: str, video_path: Path, audio_path: Optional[Path], ass_path: Optional[Path],
                   out_path: Path, preset: str, crf: int, audio_bitrate: str) -> list[str]:
    vf = f"ass={ass_path.as_posix()}" if ass_path else None
    cmd = [ffmpeg, "-y", "-i", str(video_path)]
    if audio_path:
        cmd += ["-i", str(audio_path)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-map", "0:v"]
    if audio_path:
        cmd += ["-map", "1:a", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    if audio_path:
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
    cmd += ["-progress", "pipe:1", "-nostats", str(out_path)]
    return cmd


# ---------------------------------------------------------------- ASS ----

_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyrics,Space Grotesk,{fontsize},&H00FFFFFF,&H0000D8FF,&H00201018,&H80000000,1,0,1,2,1,2,60,60,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(width: int, height: int, lyric_clips: list[dict[str, Any]]) -> str:
    fontsize = max(28, height // 24)
    lines = [_ASS_HEADER.format(width=width, height=height, fontsize=fontsize)]
    for clip in lyric_clips:
        start, end, text = clip["start_s"], clip["end_s"], clip["text"]
        if clip.get("karaoke"):
            words = text.split() or [text]
            per_word_cs = max(1, int(round((end - start) * 100 / max(1, len(words)))))
            text_out = "".join(f"{{\\k{per_word_cs}}}{w} " for w in words).strip()
        else:
            text_out = text
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Lyrics,,0,0,0,,{text_out}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ execution --

def run_ffmpeg_with_progress(cmd: list[str], total_duration_s: float, on_progress: Optional[Callable[[float], None]] = None) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        m = re.match(r"out_time_ms=(\d+)", line.strip())
        if m and on_progress and total_duration_s > 0:
            out_s = int(m.group(1)) / 1_000_000.0
            on_progress(min(1.0, out_s / total_duration_s))
    stderr = proc.stderr.read() if proc.stderr else ""
    code = proc.wait()
    if code != 0:
        raise RenderError(f"ffmpeg exited {code}: {stderr[-800:]}")


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg exited {proc.returncode}: {proc.stderr[-800:]}")


def render_timeline(
    timeline: dict[str, Any],
    asset_path_for: Callable[[str], Path],
    work_dir: Path,
    out_path: Path,
    quality: str = "preview",
    progress: Optional[Callable[[float, Optional[str]], None]] = None,
) -> dict[str, Any]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderError("ffmpeg not found (install ffmpeg or the imageio-ffmpeg wheel)")

    preset_cfg = QUALITY_PRESETS[quality]
    width, height = _scaled_resolution(timeline["width"], timeline["height"], preset_cfg["short_side"])
    fps = timeline["fps"]
    work_dir.mkdir(parents=True, exist_ok=True)

    visual = next((t for t in timeline["tracks"] if t["type"] == "visual"), None)
    if not visual or not visual["clips"]:
        raise RenderError("timeline has no visual clips to render")
    clips = visual["clips"]
    total_duration = sum(c["duration_s"] for c in clips)

    def report(frac: float, msg: Optional[str] = None) -> None:
        if progress:
            progress(frac, msg)

    clip_paths: list[Path] = []
    for i, clip in enumerate(clips):
        out_clip = work_dir / f"clip_{i:03d}.mp4"
        src = asset_path_for(clip["asset_id"])
        if clip["kind"] == "video":
            cmd = build_video_clip_cmd(ffmpeg, src, out_clip, width, height, fps, clip["duration_s"], clip.get("trim_start_s", 0.0))
        else:
            cmd = build_image_clip_cmd(ffmpeg, src, out_clip, width, height, fps, clip["duration_s"], clip.get("ken_burns"))
        _run(cmd)
        clip_paths.append(out_clip)
        report(0.05 + 0.55 * (i + 1) / len(clips), f"rendered clip {i + 1}/{len(clips)}")

    transitions = [c.get("transition_in", {"type": "cut"}) for c in clips]
    all_cuts = all(t.get("type", "cut") == "cut" for t in transitions)
    concatenated = work_dir / "concatenated.mp4"
    if all_cuts:
        list_file = work_dir / "concat_list.txt"
        list_file.write_text("".join(f"file '{p.resolve()}'\n" for p in clip_paths), encoding="utf-8")
        _run(build_concat_cmd(ffmpeg, list_file, concatenated))
    else:
        durations = [c["duration_s"] for c in clips]
        _run(build_xfade_cmd(ffmpeg, clip_paths, durations, transitions, concatenated))
    report(0.65, "concatenated clips")

    lyrics_track = next((t for t in timeline["tracks"] if t["type"] == "lyrics"), None)
    ass_path = None
    if lyrics_track and lyrics_track["clips"]:
        ass_path = work_dir / "lyrics.ass"
        ass_path.write_text(build_ass(width, height, lyrics_track["clips"]), encoding="utf-8")

    audio_path = asset_path_for(timeline["audio_asset_id"]) if timeline.get("audio_asset_id") else None

    def ffmpeg_progress(frac: float) -> None:
        report(0.65 + 0.35 * frac, "muxing final render")

    run_ffmpeg_with_progress(
        build_mux_cmd(ffmpeg, concatenated, audio_path, ass_path, out_path, preset_cfg["preset"], preset_cfg["crf"], preset_cfg["audio_bitrate"]),
        total_duration, ffmpeg_progress,
    )
    report(1.0, "done")
    return {"path": str(out_path), "width": width, "height": height, "fps": fps, "duration_s": round(total_duration, 3)}
