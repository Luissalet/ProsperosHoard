"""ffmpeg-backed rendering: normalise each timeline clip to a short mp4,
concatenate (cut or xfade), burn an ASS subtitle track for lyrics, mux the
song, and produce the final file. Every ffmpeg invocation is built by a
pure `build_*_cmd()` function (argv list, no I/O) so it can be snapshot
-tested without running ffmpeg; `run_ffmpeg_with_progress` is the only
part that actually shells out.

Quality presets: `preview` = 540p short side, `-preset ultrafast`;
`final` = the timeline's native resolution, `-preset medium -crf 18`,
AAC 192k; `animatic` = 720p short side, `-preset veryfast -crf 26` (a
production's animatic, see `animatic.py`).
"""

from __future__ import annotations

import random
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
    # a production's animatic: the stills with Ken Burns and crossfades, cheap to make
    "animatic": {"preset": "veryfast", "crf": 26, "short_side": 720, "audio_bitrate": "128k"},
}

TRANSITION_MAP = {"crossfade": "fade", "dip_black": "fadeblack", "flash_white": "fadewhite"}

# Finishing: an optional grade/texture pass applied once over the whole cut
# (not per clip), plus a lyric caption style. All keys are optional; an
# empty/absent `finishing` dict renders exactly as before this feature.
#
#   {"color_grade": "teal_orange"|"sodium_night"|"bleach_bypass",
#    "grain": 0.0-1.0, "vignette": bool, "letterbox": bool,
#    "glitch_on_downbeats": bool, "lyric_style": "default"|"horror"}
#
# Colour grades are single-pass `eq`/`colorbalance`/`curves` approximations,
# not a real 3D LUT - good enough for a fast local render, not a colourist's
# tool. `curves=preset=strong_contrast` is one of ffmpeg's built-in curve
# presets (see the `curves` filter docs), used as-is for bleach bypass.
# The RGB-only filters (`colorbalance`, `curves`) sit in an explicit packed
# `rgb24` island: left to itself, ffmpeg negotiates planar `gbrp` for them
# and the following `noise`/`vignette`, and ffmpeg 8's gbrp <-> yuv420p
# conversion blacks out the right-most 8 columns of a frame whose width is
# not a multiple of 16 (1080 is not) - a dark red stripe once the grade
# tints it. Packed RGB and YUV keep every column.
COLOR_GRADE_PRESETS = {
    "teal_orange": "eq=contrast=1.12:saturation=1.12,format=rgb24,"
                   "colorbalance=rs=-0.12:gs=0.02:bs=0.16:rm=0.04:bm=-0.02:rh=0.18:gh=0.02:bh=-0.14,format=yuv420p",
    "sodium_night": "eq=brightness=-0.04:contrast=1.08:saturation=0.55,format=rgb24,"
                    "colorbalance=rs=0.1:bs=-0.22:rm=0.18:gm=0.03:bm=-0.22:rh=0.1:bh=-0.12,format=yuv420p",
    "bleach_bypass": "format=rgb24,curves=preset=strong_contrast,format=yuv420p,eq=saturation=0.35:contrast=1.18",
}
FINISHING_KEYS = {"color_grade", "grain", "vignette", "letterbox", "glitch_on_downbeats", "lyric_style"}
LYRIC_STYLES = ("default", "horror")


def validate_finishing(finishing: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Returns a clean copy, or raises RenderError for a bad value. `None`
    and `{}` both mean "no finishing", so old timelines keep rendering as
    before."""
    if not finishing:
        return {}
    if not isinstance(finishing, dict):
        raise RenderError("finishing must be an object")
    unknown = set(finishing) - FINISHING_KEYS
    if unknown:
        raise RenderError(f"unknown finishing field(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    grade = finishing.get("color_grade")
    if grade:
        if grade not in COLOR_GRADE_PRESETS:
            raise RenderError(f"color_grade must be one of {', '.join(COLOR_GRADE_PRESETS)}")
        out["color_grade"] = grade
    if "grain" in finishing and finishing["grain"]:
        try:
            grain = float(finishing["grain"])
        except (TypeError, ValueError):
            raise RenderError("grain must be a number between 0 and 1") from None
        if not 0.0 < grain <= 1.0:
            raise RenderError("grain must be between 0 and 1")
        out["grain"] = grain
    if finishing.get("vignette"):
        out["vignette"] = True
    if finishing.get("letterbox"):
        out["letterbox"] = True
    if finishing.get("glitch_on_downbeats"):
        out["glitch_on_downbeats"] = True
    style = finishing.get("lyric_style")
    if style and style != "default":
        if style not in LYRIC_STYLES:
            raise RenderError(f"lyric_style must be one of {', '.join(LYRIC_STYLES)}")
        out["lyric_style"] = style
    return out


def _escape_enable_arg(value: str) -> str:
    """Commas inside a filter's own function args (e.g. `between(t,0,1)`)
    must be backslash-escaped once the filter sits in a comma-joined
    filtergraph string."""
    return value.replace(",", "\\,")


def build_finishing_vf(finishing: Optional[dict[str, Any]], width: int, height: int,
                        glitch_points: Optional[list[tuple[float, float]]] = None) -> str:
    """Builds the finishing portion of a `-vf` chain (no leading/trailing
    comma), applied to the whole joined cut before the lyric captions are
    burned on top. Pure and snapshot-testable: no I/O."""
    finishing = finishing or {}
    parts: list[str] = []
    grade = finishing.get("color_grade")
    if grade:
        parts.append(COLOR_GRADE_PRESETS[grade])
    grain = finishing.get("grain")
    if grain:
        # luma-only temporal grain: film grain lives in brightness, and noise
        # on the chroma planes of a yuv420p frame is both uglier (coloured
        # speckle) and far harder to encode - it made a 2 min 1080p final
        # eight times bigger at the same CRF
        parts.append(f"noise=c0s={float(grain) * 24:.1f}:c0f=t+u")
    if finishing.get("vignette"):
        parts.append("vignette=PI/5")
    if finishing.get("letterbox"):
        bar = max(2, int(round(height * 0.10 / 2) * 2))
        parts.append(f"drawbox=x=0:y=0:w={width}:h={bar}:color=black:t=fill")
        parts.append(f"drawbox=x=0:y={height - bar}:w={width}:h={bar}:color=black:t=fill")
    if finishing.get("glitch_on_downbeats"):
        for start, dur in glitch_points or []:
            end = start + max(0.08, min(dur, 0.22))
            enable = _escape_enable_arg(f"between(t,{start:.3f},{end:.3f})")
            # chromashift, not rgbashift: rgbashift only takes planar RGB, and
            # the gbrp round trip blacks out the right edge on ffmpeg 8 (see
            # COLOR_GRADE_PRESETS) on every frame, glitch window or not.
            parts.append(f"chromashift=crh=4:cbv=-4:enable='{enable}'")
    return ",".join(parts)


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
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-loop", "1", "-i", str(src), "-t", f"{duration_s + 0.5 / fps:.3f}",
        "-vf", vf, "-r", str(fps), "-frames:v", str(n_frames), "-an", "-c:v", "libx264", "-preset", "ultrafast",
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
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-ss", f"{trim_start_s:.3f}", "-i", str(src),
        "-t", f"{duration_s + 0.5 / fps:.3f}", "-vf", vf, "-frames:v", str(max(1, round(duration_s * fps))),
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path),
    ]


def build_concat_cmd(ffmpeg: str, list_file: Path, out_path: Path) -> list[str]:
    return [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out_path)]


def concat_list_text(clip_paths: list[Path]) -> str:
    """Entries relative to the list file (all clips live next to it), so
    the concat demuxer never has to parse the absolute path - which in the
    Windows install folder contains an apostrophe ("Prospero's Hoard")."""
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
    # Every input is pinned to the timeline's constant frame rate first:
    # without it the xfade chain's output carries no frame rate, the encoder
    # falls back to the stream's 1/12288 time base and drops everything after
    # the second clip (a two-minute cut came out 6.9 s long).
    filters = [f"[{i}:v]fps={fps},setpts=PTS-STARTPTS[s{i}]" for i in range(len(clip_paths))]
    label = "s0"
    start = 0.0
    for i in range(1, len(clip_paths)):
        start += durations[i - 1]
        t = transitions[i] or {}
        xfade_type = TRANSITION_MAP.get(t.get("type", "cut"), "fade")
        dur = transition_duration(t, fps)
        out_label = f"v{i}"
        filters.append(f"[{label}][s{i}]xfade=transition={xfade_type}:duration={dur:.3f}:offset={start:.3f}[{out_label}]")
        label = out_label
    filter_complex = ";".join(filters)
    return [
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", *inputs, "-filter_complex", filter_complex, "-map", f"[{label}]",
        "-r", str(fps), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path),
    ]


_FADE_COLOR = {"fadeblack": "black", "fadewhite": "white"}


def build_overlay_fade_cmd(ffmpeg: str, clip_paths: list[Path], durations: list[float], transitions: list[dict[str, Any]],
                           out_path: Path, fps: int = 30) -> list[str]:
    """The same join as `build_xfade_cmd` for an ffmpeg without `xfade`
    (added in 4.3; the imageio-ffmpeg fallback binary is 4.2): each incoming
    clip is padded to its offset with `tpad` and the chain so far is laid
    over it, fading its alpha out across the overlap. Every transition is a
    crossfade; a dip to black/white also fades both sides through that
    colour. Same offsets, so the output has the same length."""
    inputs: list[str] = []
    for p in clip_paths:
        inputs += ["-i", str(p)]
    filters = [f"[{i}:v]fps={fps},setpts=PTS-STARTPTS[s{i}]" for i in range(len(clip_paths))]
    label = "s0"
    start = 0.0
    for i in range(1, len(clip_paths)):
        start += durations[i - 1]
        t = transitions[i] or {}
        kind = TRANSITION_MAP.get(t.get("type", "cut"), "fade")
        dur = transition_duration(t, fps)
        color = _FADE_COLOR.get(kind)
        incoming = f"[s{i}]"
        outgoing = f"[{label}]"
        if color:
            incoming += f"fade=t=in:st=0:d={dur:.3f}:color={color},"
            outgoing += f"fade=t=out:st={start:.3f}:d={dur:.3f}:color={color},"
        else:
            incoming += "null,"
            outgoing += "null,"
        filters.append(f"{incoming}tpad=start_duration={start:.3f}[p{i}]")
        filters.append(f"{outgoing}format=yuva420p,fade=t=out:st={start:.3f}:d={dur:.3f}:alpha=1[a{i}]")
        out_label = f"v{i}"
        filters.append(f"[p{i}][a{i}]overlay=eof_action=pass:format=yuv420,format=yuv420p[{out_label}]")
        label = out_label
    filter_complex = ";".join(filters)
    return [
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", *inputs, "-filter_complex", filter_complex, "-map", f"[{label}]",
        "-r", str(fps), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out_path),
    ]


_FILTER_CACHE: dict[tuple[str, str], bool] = {}


def ffmpeg_has_filter(ffmpeg: str, name: str) -> bool:
    """Whether this ffmpeg build ships the filter `name` (cached per binary).
    If the listing itself fails, assume yes and let the real command say why."""
    key = (ffmpeg, name)
    if key not in _FILTER_CACHE:
        try:
            proc = procutil.run([ffmpeg, "-hide_banner", "-filters"], text=True, timeout=30)
            listing = proc.stdout or ""
            _FILTER_CACHE[key] = proc.returncode != 0 or re.search(rf"^\s*\S+\s+{re.escape(name)}\s", listing, re.M) is not None
        except (OSError, procutil.subprocess.SubprocessError):
            _FILTER_CACHE[key] = True
    return _FILTER_CACHE[key]


def build_mux_cmd(ffmpeg: str, video_path: Path, audio_path: Optional[Path], ass_name: Optional[str],
                   out_path: Path, preset: str, crf: int, audio_bitrate: str, finishing_vf: str = "") -> list[str]:
    """`ass_name` is a bare file name inside the ffmpeg working directory
    (`render_timeline` runs this with `cwd=work_dir`): the `ass=` filter
    argument is parsed by ffmpeg's filter-graph syntax, where the drive
    colon of a Windows path and the apostrophe in the install folder name
    are both special. A plain name like `lyrics.ass` needs no escaping.
    `finishing_vf` (from `build_finishing_vf`) runs first, so the grade/
    grain/vignette/glitch pass sits under the captions, not over them."""
    cmd = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-i", str(video_path)]
    if audio_path:
        cmd += ["-i", str(audio_path)]
    vf_parts = [finishing_vf] if finishing_vf else []
    if ass_name:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", ass_name):
            raise ValueError(f"unsafe subtitle file name for the ass filter: {ass_name!r}")
        vf_parts.append(f"ass={ass_name}:fontsdir=fonts")
    if vf_parts:
        cmd += ["-vf", ",".join(vf_parts)]
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
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Spacing, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyrics,{fontname},{fontsize},{primary},{secondary},{outline},{back},{bold},0,1,{border},{shadow},{spacing},2,{margin_h},{margin_h},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# "horror" lyric style: a condensed display face (Bebas Neue, already
# bundled) set fully uppercase; a line appears in dim fog grey and each word
# lights up bone white as it is sung (karaoke), with a near-black soft
# outline that stays legible over sodium-orange footage; a short fade in and
# out; and a small per-line rotation/shear "jitter" seeded from the line's
# own text so the same lyrics always render the same wobble
# (snapshot-testable) without every line looking identically stamped.
# ASS colours are &HAABBGGRR (AA = transparency).
_LYRIC_STYLE_ASS = {
    "default": {"fontname": "Inter", "primary": "&H00FFFFFF", "secondary": "&H0000D8FF", "outline": "&H00201018",
                "back": "&H80000000", "bold": 1, "border": 2, "shadow": 1, "spacing": 0},
    "horror": {"fontname": "Bebas Neue", "primary": "&H00DAE6ED", "secondary": "&H5099908A", "outline": "&H00100C0B",
               "back": "&H90000000", "bold": 0, "border": 3, "shadow": 1, "spacing": 2},
}


def _horror_jitter(seed_text: str) -> tuple[float, float]:
    """(z-rotation degrees, x-shear factor) for one lyric line, deterministic
    per line so re-rendering the same song produces byte-identical output."""
    rng = random.Random(f"horror-jitter:{seed_text}")
    return round(rng.uniform(-2.0, 2.0), 2), round(rng.uniform(-0.05, 0.05), 3)


_VOWEL_GROUPS = re.compile(r"[aeiouáéíóúüy]+", re.IGNORECASE)


def _word_weight(word: str) -> int:
    """Rough syllable count (vowel groups), so a long word takes longer to
    light up than "la" - closer to how a line is actually sung than an even
    split per word."""
    return max(1, len(_VOWEL_GROUPS.findall(word)))


# the whole line lights up within this long at most: a sung line rarely
# takes more than two bars, even when its caption stays up longer
KARAOKE_MAX_FILL_S = 3.6


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


def build_ass(width: int, height: int, lyric_clips: list[dict[str, Any]], style: str = "default") -> str:
    if style not in LYRIC_STYLES:
        raise RenderError(f"lyric_style must be one of {', '.join(LYRIC_STYLES)}")
    style_vars = _LYRIC_STYLE_ASS[style]
    if style == "horror":
        # sized from the short side so 16:9 captions are not tiny; lifted
        # above the bottom fifth on vertical video, where the short-video
        # apps draw their own buttons and captions
        fontsize = max(28, min(width, height) // 13)
        margin_v = int(height * (0.2 if height > width else 0.09))
        # 8% side margins: a long line wraps onto two instead of touching
        # the edges of a phone screen
        margin_h = int(width * 0.08)
    else:
        fontsize = max(28, height // 24)
        margin_v, margin_h = 80, 60
    lines = [_ASS_HEADER.format(width=width, height=height, fontsize=fontsize, margin_v=margin_v, margin_h=margin_h,
                                **style_vars)]
    for clip in lyric_clips:
        start, end = float(clip["start_s"]), float(clip["end_s"])
        text = str(clip.get("text", ""))
        if end <= start or not text.strip():
            continue
        if style == "horror":
            text = text.upper()
        if clip.get("karaoke"):
            words = text.split() or [text]
            if style == "horror":
                fill_cs = max(len(words), int(round(min(0.92 * (end - start), KARAOKE_MAX_FILL_S) * 100)))
                weights = [_word_weight(w) for w in words]
                total_w = sum(weights)
                parts, used = [], 0
                for k, (w, wt) in enumerate(zip(words, weights)):
                    cs = fill_cs - used if k == len(words) - 1 else max(1, int(round(fill_cs * wt / total_w)))
                    used += cs
                    parts.append(f"{{\\k{cs}}}{ass_escape(w)}")
                text_out = " ".join(parts)
            else:
                total_cs = max(1, int(round((end - start) * 100)))
                per_word_cs = max(1, total_cs // len(words))
                text_out = " ".join(f"{{\\k{per_word_cs}}}{ass_escape(w)}" for w in words)
        else:
            text_out = ass_escape(text)
        if style == "horror":
            frz, fax = _horror_jitter(f"{start}:{text}")
            fade_out = 120 if end - start > 0.6 else 0
            text_out = f"{{\\frz{frz}\\fax{fax}\\blur1.2\\fad(90,{fade_out})}}{text_out}"
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
    finishing = validate_finishing(timeline.get("finishing"))

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

    # Everything on the frame grid: each clip starts on the frame nearest its
    # beat and lasts a whole number of frames, and (with transitions) is
    # rendered one overlap + one spare frame longer. Float seconds drift as
    # they accumulate, and an xfade whose outgoing clip ends a fraction of a
    # frame before the overlap does silently ends the whole chain.
    start_frames, acc = [], 0.0
    for c in clips:
        start_frames.append(int(round(acc * fps)))
        acc += float(c["duration_s"])
    start_frames.append(int(round(acc * fps)))
    clip_frames = [max(1, start_frames[i + 1] - start_frames[i]) for i in range(len(clips))]
    overlap_frames = [max(1, int(round(transition_duration(t, fps) * fps))) for t in transitions]
    grid_durations = [n / fps for n in clip_frames]
    grid_transitions = [dict(t, duration_s=overlap_frames[i] / fps) if t.get("type", "cut") != "cut" else t
                        for i, t in enumerate(transitions)]

    clip_paths: list[Path] = []
    for i, clip in enumerate(clips):
        check_cancel()
        out_clip = work_dir / f"clip_{i:03d}.mp4"
        src = asset_path_for(clip["asset_id"])
        frames = clip_frames[i]
        if not all_cuts and i + 1 < len(clips):
            frames += overlap_frames[i + 1] + 1
        duration = frames / fps
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
        join = build_xfade_cmd if ffmpeg_has_filter(ffmpeg, "xfade") else build_overlay_fade_cmd
        _run(join(ffmpeg, clip_paths, grid_durations, grid_transitions, concatenated, fps=fps))
    report(0.65, "joined clips")

    lyrics_track = next((t for t in timeline["tracks"] if t["type"] == "lyrics"), None)
    ass_name = None
    if lyrics_track and lyrics_track["clips"]:
        ass_name = "lyrics.ass"
        lyric_style = finishing.get("lyric_style", "default")
        (work_dir / ass_name).write_text(build_ass(width, height, lyrics_track["clips"], style=lyric_style), encoding="utf-8")
        fonts_out = work_dir / "fonts"
        fonts_out.mkdir(exist_ok=True)
        for ttf in FONTS_DIR.glob("*/*.ttf"):
            shutil.copyfile(ttf, fonts_out / ttf.name)

    audio_path = asset_path_for(timeline["audio_asset_id"]) if timeline.get("audio_asset_id") else None

    # `start_s` is derived (not required on the caller's clip dicts - see
    # the module docstring), so it is recomputed here from durations rather
    # than trusted from the clip, exactly like `total_duration` above.
    glitch_points: list[tuple[float, float]] = []
    running = 0.0
    for i, c in enumerate(clips):
        if transitions[i].get("type") == "flash_white":
            glitch_points.append((running, float(c["duration_s"])))
        running += float(c["duration_s"])
    finishing_vf = build_finishing_vf(finishing, width, height, glitch_points)

    def ffmpeg_progress(frac: float) -> None:
        report(0.65 + 0.35 * frac, "encoding with audio and lyrics")

    run_ffmpeg_with_progress(
        build_mux_cmd(ffmpeg, concatenated, audio_path.resolve() if audio_path else None, ass_name, out_path.resolve(),
                      preset_cfg["preset"], preset_cfg["crf"], preset_cfg["audio_bitrate"], finishing_vf=finishing_vf),
        total_duration, ffmpeg_progress, cwd=work_dir, should_cancel=should_cancel,
    )
    report(1.0, "done")
    return {"path": str(out_path), "width": width, "height": height, "fps": fps, "duration_s": round(total_duration, 3)}
