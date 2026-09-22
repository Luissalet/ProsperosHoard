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
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from . import procutil
from .backend import ffmpeg_path

FONTS_DIR = Path(__file__).parent / "fonts"

QUALITY_PRESETS = {
    "preview": {"preset": "ultrafast", "crf": 28, "short_side": 540, "audio_bitrate": "128k"},
    "final": {"preset": "medium", "crf": 18, "short_side": None, "audio_bitrate": "192k"},
}

TRANSITION_MAP = {"crossfade": "fade", "dip_black": "fadeblack", "flash_white": "fadewhite"}


class RenderError(RuntimeError):
    pass


class RenderCancelled(RenderError):
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
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-loop", "1", "-i", str(src), "-t", f"{duration_s:.3f}",
        "-vf", vf, "-r", str(fps), "-an", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", str(out_path),
    ]


def build_video_clip_cmd(
    ffmpeg: str, src: Path, out_path: Path, width: int, height: int, fps: int, duration_s: float, trim_start_s: float,
) -> list[str]:
    # tpad clones the last frame when the source is shorter than the clip
    # (a 2 s SVD animation placed on a 3 s beat slot) so every clip has the
    # exact length the timeline says.
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},"
          f"tpad=stop_mode=clone:stop_duration={duration_s:.3f},format=yuv420p")
    return [
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-ss", f"{trim_start_s:.3f}", "-i", str(src), "-t", f"{duration_s:.3f}",
        "-vf", vf, "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path),
    ]


def build_concat_cmd(ffmpeg: str, list_file: Path, out_path: Path) -> list[str]:
    return [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out_path)]


def concat_list_text(clip_paths: list[Path]) -> str:
    """Entries relative to the list file (all clips live next to it), so
    the concat demuxer never has to parse the absolute path - which on the
    typical Windows install contains an apostrophe ("Prospero's Hoard")."""
    lines = []
    for p in clip_paths:
        name = p.name.replace("'", "'\\''")
        lines.append(f"file '{name}'\n")
    return "".join(lines)


def transition_duration(transition: Optional[dict[str, Any]], fps: int) -> float:
    """Length of the overlap entering a clip. A plain cut inside a timeline
    that also has real transitions is a one-frame blend (visually a cut),
    because xfade is what keeps every clip on its beat-aligned start."""
    t = transition or {}
    if t.get("type", "cut") == "cut":
        return 1.0 / max(1, fps)
    return max(0.05, float(t.get("duration_s") or 0.15))


def build_xfade_cmd(ffmpeg: str, clip_paths: list[Path], durations: list[float], transitions: list[dict[str, Any]],
                     out_path: Path, fps: int = 30) -> list[str]:
    """`transitions[i]` is the transition entering clip i (i=0 unused).

    Clip i-1 is rendered `transition_duration(i)` longer than its nominal
    duration (see `render_timeline`), so each xfade starts exactly at the
    nominal start of the incoming clip: cuts stay on the beat and the output
    is as long as the timeline instead of drifting earlier per transition.
    """
    inputs: list[str] = []
    for p in clip_paths:
        inputs += ["-i", str(p)]
    filters = []
    label = "0:v"
    start = 0.0
    for i in range(1, len(clip_paths)):
        start += durations[i - 1]
        t = transitions[i] or {}
        xfade_type = TRANSITION_MAP.get(t.get("type", "cut"), "fade")
        dur = transition_duration(t, fps)
        out_label = f"v{i}"
        filters.append(f"[{label}][{i}:v]xfade=transition={xfade_type}:duration={dur:.3f}:offset={start:.3f}[{out_label}]")
        label = out_label
    filter_complex = ";".join(filters)
    return [
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", *inputs, "-filter_complex", filter_complex, "-map", f"[{label}]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path),
    ]


def build_mux_cmd(ffmpeg: str, video_path: Path, audio_path: Optional[Path], ass_name: Optional[str],
                   out_path: Path, preset: str, crf: int, audio_bitrate: str) -> list[str]:
    """`ass_name` is a bare file name inside the ffmpeg working directory
    (`render_timeline` runs this with `cwd=work_dir`): the `ass=` filter
    argument is parsed by ffmpeg's filter-graph syntax, where the drive
    colon of a Windows path and the apostrophe in the install folder name
    are both special. A plain name like `lyrics.ass` needs no escaping."""
    cmd = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-i", str(video_path)]
    if audio_path:
        cmd += ["-i", str(audio_path)]
    if ass_name:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", ass_name):
            raise ValueError(f"unsafe subtitle file name for the ass filter: {ass_name!r}")
        cmd += ["-vf", f"ass={ass_name}:fontsdir=fonts"]
    cmd += ["-map", "0:v"]
    if audio_path:
        cmd += ["-map", "1:a", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
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
Style: Lyrics,Inter,{fontsize},&H00FFFFFF,&H0000D8FF,&H00201018,&H80000000,1,0,1,2,1,2,60,60,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    """Make arbitrary lyric text inert inside an ASS Dialogue line: no
    override blocks (`{\\pos..}`), no `\\N`/`\\h` escapes, no line breaks that
    could start a new `Dialogue:` event. A backslash is kept visible by
    following it with an invisible word joiner; braces use libass's `\\{`
    `\\}` escapes; real newlines become ASS line breaks."""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\u2060")
    text = text.replace("{", "\\{").replace("}", "\\}")
    text = "\\N".join(part.strip() for part in text.split("\n"))
    return text[:500]


def build_ass(width: int, height: int, lyric_clips: list[dict[str, Any]]) -> str:
    fontsize = max(28, height // 24)
    lines = [_ASS_HEADER.format(width=width, height=height, fontsize=fontsize)]
    for clip in lyric_clips:
        start, end = float(clip["start_s"]), float(clip["end_s"])
        text = str(clip.get("text", ""))
        if end <= start or not text.strip():
            continue
        if clip.get("karaoke"):
            words = text.split() or [text]
            total_cs = max(1, int(round((end - start) * 100)))
            per_word_cs = max(1, total_cs // len(words))
            text_out = " ".join(f"{{\\k{per_word_cs}}}{ass_escape(w)}" for w in words)
        else:
            text_out = ass_escape(text)
        lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Lyrics,,0,0,0,,{text_out}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ execution --

def run_ffmpeg_with_progress(cmd: list[str], total_duration_s: float, on_progress: Optional[Callable[[float], None]] = None,
                              cwd: Optional[Path] = None, should_cancel: Optional[Callable[[], bool]] = None) -> None:
    """Runs ffmpeg with `-progress pipe:1`, reporting a 0-1 fraction.
    stderr goes to a temporary file (a full stderr pipe nobody reads would
    block ffmpeg forever)."""
    with tempfile.TemporaryFile(mode="w+b") as err:
        proc = procutil.popen(cmd, stdout=procutil.subprocess.PIPE, stderr=err, text=True, cwd=str(cwd) if cwd else None)
        assert proc.stdout is not None
        for line in proc.stdout:
            if should_cancel and should_cancel():
                proc.kill()
                proc.wait()
                raise RenderCancelled("render cancelled")
            m = re.match(r"out_time_(?:ms|us)=(\d+)", line.strip())
            if m and on_progress and total_duration_s > 0:
                out_s = int(m.group(1)) / 1_000_000.0
                on_progress(min(1.0, out_s / total_duration_s))
        code = proc.wait()
        if code != 0:
            err.seek(0)
            stderr = err.read().decode("utf-8", "replace")
            raise RenderError(f"ffmpeg exited {code}: {stderr[-800:]}")


def _run(cmd: list[str], cwd: Optional[Path] = None) -> None:
    proc = procutil.run(cmd, text=True, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg exited {proc.returncode}: {proc.stderr[-800:]}")


def animated_webp_to_mp4(src: Path, dest: Path, fps: float, work_dir: Path) -> int:
    """ComfyUI's SaveAnimatedWEBP output -> H.264 mp4. ffmpeg's webp decoder
    does not read animated WebP, so Pillow extracts the frames and ffmpeg
    encodes the PNG sequence. Returns the frame count."""
    from PIL import Image, ImageSequence

    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderError("ffmpeg not found (install ffmpeg or the imageio-ffmpeg wheel)")
    frames_dir = work_dir / f"frames_{src.stem}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    try:
        n = 0
        with Image.open(src) as im:
            for frame in ImageSequence.Iterator(im):
                rgb = frame.convert("RGB")
                if rgb.width % 2 or rgb.height % 2:
                    rgb = rgb.crop((0, 0, rgb.width - rgb.width % 2, rgb.height - rgb.height % 2))
                rgb.save(frames_dir / f"f{n:05d}.png")
                n += 1
        if n == 0:
            raise RenderError(f"{src.name} has no frames")
        _run([ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-framerate", f"{max(1.0, fps):g}", "-i", "f%05d.png",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest.resolve())], cwd=frames_dir)
        return n
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)


def render_timeline(
    timeline: dict[str, Any],
    asset_path_for: Callable[[str], Path],
    work_dir: Path,
    out_path: Path,
    quality: str = "preview",
    progress: Optional[Callable[[float, Optional[str]], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict[str, Any]:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RenderError("ffmpeg not found (install ffmpeg or the imageio-ffmpeg wheel)")
    if quality not in QUALITY_PRESETS:
        raise ValueError(f"unknown quality '{quality}'; use 'preview' or 'final'")

    preset_cfg = QUALITY_PRESETS[quality]
    width, height = _scaled_resolution(timeline["width"], timeline["height"], preset_cfg["short_side"])
    fps = int(timeline["fps"])
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir = work_dir.resolve()

    visual = next((t for t in timeline["tracks"] if t["type"] == "visual"), None)
    if not visual or not visual["clips"]:
        raise RenderError("timeline has no visual clips to render")
    clips = visual["clips"]
    total_duration = sum(float(c["duration_s"]) for c in clips)

    def report(frac: float, msg: Optional[str] = None) -> None:
        if progress:
            progress(frac, msg)

    def check_cancel() -> None:
        if should_cancel and should_cancel():
            raise RenderCancelled("render cancelled")

    transitions = [c.get("transition_in") or {"type": "cut"} for c in clips]
    all_cuts = all(t.get("type", "cut") == "cut" for t in transitions)

    clip_paths: list[Path] = []
    for i, clip in enumerate(clips):
        check_cancel()
        out_clip = work_dir / f"clip_{i:03d}.mp4"
        src = asset_path_for(clip["asset_id"])
        duration = float(clip["duration_s"])
        if not all_cuts and i + 1 < len(clips):
            duration += transition_duration(transitions[i + 1], fps)
        if clip["kind"] == "video":
            cmd = build_video_clip_cmd(ffmpeg, src, out_clip, width, height, fps, duration, float(clip.get("trim_start_s", 0.0)))
        else:
            cmd = build_image_clip_cmd(ffmpeg, src, out_clip, width, height, fps, duration, clip.get("ken_burns"))
        _run(cmd)
        clip_paths.append(out_clip)
        report(0.05 + 0.55 * (i + 1) / len(clips), f"rendered clip {i + 1}/{len(clips)}")

    check_cancel()
    concatenated = work_dir / "concatenated.mp4"
    if all_cuts:
        list_file = work_dir / "concat_list.txt"
        list_file.write_text(concat_list_text(clip_paths), encoding="utf-8")
        _run(build_concat_cmd(ffmpeg, list_file, concatenated))
    else:
        durations = [float(c["duration_s"]) for c in clips]
        _run(build_xfade_cmd(ffmpeg, clip_paths, durations, transitions, concatenated, fps=fps))
    report(0.65, "joined clips")

    lyrics_track = next((t for t in timeline["tracks"] if t["type"] == "lyrics"), None)
    ass_name = None
    if lyrics_track and lyrics_track["clips"]:
        ass_name = "lyrics.ass"
        (work_dir / ass_name).write_text(build_ass(width, height, lyrics_track["clips"]), encoding="utf-8")
        fonts_out = work_dir / "fonts"
        fonts_out.mkdir(exist_ok=True)
        for ttf in FONTS_DIR.glob("*/*.ttf"):
            shutil.copyfile(ttf, fonts_out / ttf.name)

    audio_path = asset_path_for(timeline["audio_asset_id"]) if timeline.get("audio_asset_id") else None

    def ffmpeg_progress(frac: float) -> None:
        report(0.65 + 0.35 * frac, "encoding with audio and lyrics")

    run_ffmpeg_with_progress(
        build_mux_cmd(ffmpeg, concatenated, audio_path.resolve() if audio_path else None, ass_name, out_path.resolve(),
                      preset_cfg["preset"], preset_cfg["crf"], preset_cfg["audio_bitrate"]),
        total_duration, ffmpeg_progress, cwd=work_dir, should_cancel=should_cancel,
    )
    report(1.0, "done")
    return {"path": str(out_path), "width": width, "height": height, "fps": fps, "duration_s": round(total_duration, 3)}
