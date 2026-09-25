"""A narrated short's soundtrack: the voice over a music bed that ducks
under it, loudness-normalised for phones.

`build_mix_filter` is pure (snapshot-tested); `mix` runs ffmpeg. The
music bed loops when it is shorter than the voice (`-stream_loop -1`),
starts `music_db` below full scale, fades in and out, and is pushed
further down whenever the voice speaks by `sidechaincompress` keyed on
the voice itself - a real duck that follows the words, not a fixed
volume. The result is normalised to -14 LUFS integrated (what the short
video apps normalise to) with a -1.5 dBTP ceiling, 44.1 kHz stereo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import procutil
from .backend import ffmpeg_path

TARGET_LUFS = -14.0
TRUE_PEAK = -1.5
SAMPLE_RATE = 44100
DEFAULT_MUSIC_DB = -16.0
DUCK_PRESETS = {  # threshold (linear), ratio, attack ms, release ms
    "light": (0.08, 3, 30, 500),
    "normal": (0.05, 6, 20, 450),
    "strong": (0.03, 12, 10, 400),
}


class MixError(RuntimeError):
    pass


def build_mix_filter(duration_s: float, has_music: bool, music_db: float = DEFAULT_MUSIC_DB, duck: str = "normal",
                     tail_s: float = 0.6, fade_s: float = 1.2) -> str:
    """The `-filter_complex` for input 0 = voice, input 1 = music (looped).
    Output label `[out]`."""
    total = max(0.5, duration_s + tail_s)
    fmt = f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo"
    norm = f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA=11"
    voice = f"[0:a]{fmt},apad=whole_dur={total:.3f}"
    if not has_music:
        return f"{voice},{norm}[out]"
    fade_out_at = max(0.0, total - fade_s)
    music = (f"[1:a]{fmt},atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={music_db:.1f}dB,"
             f"afade=t=in:st=0:d={min(fade_s, total / 4):.2f},afade=t=out:st={fade_out_at:.3f}:d={fade_s:.2f}")
    if duck == "off":
        return f"{voice}[v];{music}[m];[v][m]amix=inputs=2:normalize=0:duration=first,{norm}[out]"
    if duck not in DUCK_PRESETS:
        raise MixError(f"duck must be one of off, {', '.join(DUCK_PRESETS)}")
    thr, ratio, attack, release = DUCK_PRESETS[duck]
    return (f"{voice},asplit=2[v][key];{music}[m];"
            f"[m][key]sidechaincompress=threshold={thr}:ratio={ratio}:attack={attack}:release={release}:makeup=1[ducked];"
            f"[v][ducked]amix=inputs=2:normalize=0:duration=first,{norm}[out]")


def build_mix_cmd(ffmpeg: str, voice: Path, music: Optional[Path], out_path: Path, duration_s: float,
                  music_db: float = DEFAULT_MUSIC_DB, duck: str = "normal") -> list[str]:
    cmd = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-i", str(voice)]
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
    cmd += ["-filter_complex", build_mix_filter(duration_s, bool(music), music_db, duck), "-map", "[out]",
            "-ar", str(SAMPLE_RATE), "-c:a", "aac", "-b:a", "192k", str(out_path)]
    return cmd


def mix(voice: Path, music: Optional[Path], out_path: Path, duration_s: float, music_db: float = DEFAULT_MUSIC_DB,
        duck: str = "normal") -> None:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise MixError("ffmpeg not found")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = procutil.run(build_mix_cmd(ffmpeg, voice, music, out_path, duration_s, music_db, duck), timeout=900)
    if proc.returncode != 0 or not out_path.is_file():
        raise MixError(f"the soundtrack mix failed: {(proc.stderr or b'').decode('utf-8', 'replace')[:600]}")
