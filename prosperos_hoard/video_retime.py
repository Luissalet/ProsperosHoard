"""Slow parts of a video down (and the matching audio), leaving the rest at
normal speed. Used by dubbing's `fit_mode="smart"`: a translated line that is
too long for its slot may get a slower picture instead of a chipmunk voice.

The video is cut into consecutive chunks that cover it end to end; a chunk is
either untouched (factor 1.0) or stretched (`factor` > 1.0 is a duration
multiplier, so 2.0 is half speed). Every stretch pushes everything after it
later, so `map_time` turns a time of the source into a time of the result;
dubbing uses it to place the clips and the subtitles.

`build_chunks`, `map_time`, `batches` and the filter builders are pure;
`retime_video` / `retime_audio` run ffmpeg. A long list of chunks is processed
in batches (each batch reads only its own stretch of the source) and the parts
are joined with the concat demuxer without re-encoding.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Iterable, Optional

from . import procutil
from .hoard_link.media.ffmpeg import atempo_chain

MIN_FACTOR = 1.05       # a slow-down below this is not cut in
MIN_CHUNK_S = 0.04      # shorter pieces are dropped (and a retime window that small is ignored)
BATCH_SIZE = 40
DRIFT_PAD_S = 1.0       # most the picture is held on its last frame to make up for rounding


class RetimeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ pure --

def build_chunks(retimes: Iterable[dict[str, Any]], total_s: float, min_factor: float = MIN_FACTOR) -> list[dict[str, Any]]:
    """Consecutive chunks covering [0, total_s] of the source. `retimes` are
    {start_s, end_s, factor} ranges of the source to slow down (any order). A
    range under `min_factor`, empty or outside the video is ignored; one that
    overlaps the previous range starts where that one ended. Each chunk is
    {src_start, src_end, factor, out_start, out_end} (the last two are
    positions in the retimed video)."""
    total = max(0.0, float(total_s))
    ranges = []
    for r in sorted(retimes, key=lambda x: float(x["start_s"])):
        factor = float(r.get("factor") or 1.0)
        if factor < min_factor:
            continue
        ranges.append((max(0.0, float(r["start_s"])), min(total, float(r["end_s"])), factor))
    chunks: list[dict[str, Any]] = []
    cursor = 0.0
    out = 0.0

    def add(a: float, b: float, factor: float) -> None:
        nonlocal out
        length = (b - a) * factor
        chunks.append({"src_start": round(a, 4), "src_end": round(b, 4), "factor": round(factor, 4),
                       "out_start": round(out, 4), "out_end": round(out + length, 4)})
        out += length

    for start, end, factor in ranges:
        start = max(start, cursor)
        if end - start < MIN_CHUNK_S:
            continue
        if start - cursor >= MIN_CHUNK_S:
            add(cursor, start, 1.0)
        else:
            start = cursor  # a sliver in between belongs to the stretch
        add(start, end, factor)
        cursor = end
    if total - cursor >= MIN_CHUNK_S or not chunks:
        add(cursor, total, 1.0)
    elif total > cursor:  # a sliver at the end: the last chunk runs to the end of the source
        last = chunks[-1]
        last["out_end"] = round(last["out_end"] + (total - last["src_end"]) * last["factor"], 4)
        last["src_end"] = round(total, 4)
    return chunks


def needs_retime(chunks: list[dict[str, Any]]) -> bool:
    return any(c["factor"] > 1.0 for c in chunks)


def new_duration(chunks: list[dict[str, Any]]) -> float:
    return round(chunks[-1]["out_end"], 4) if chunks else 0.0


def added_seconds(chunks: list[dict[str, Any]]) -> float:
    return round(sum((c["src_end"] - c["src_start"]) * (c["factor"] - 1.0) for c in chunks), 4)


def map_time(chunks: list[dict[str, Any]], t: float) -> float:
    """Where source time `t` lands in the retimed video (past the end, at normal speed)."""
    t = float(t)
    if not chunks or t <= 0:
        return max(0.0, t)
    for c in chunks:
        if t <= c["src_end"]:
            return round(c["out_start"] + (t - c["src_start"]) * c["factor"], 4)
    return round(chunks[-1]["out_end"] + (t - chunks[-1]["src_end"]), 4)


def batches(chunks: list[dict[str, Any]], size: int = BATCH_SIZE) -> list[list[dict[str, Any]]]:
    size = max(1, int(size))
    return [chunks[i:i + size] for i in range(0, len(chunks), size)]


def chunks_key(chunks: list[dict[str, Any]]) -> str:
    """A short hash of the chunk list, to know whether a cached retime is still valid."""
    return hashlib.sha256(json.dumps(chunks, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _fmt(x: float) -> str:
    return f"{x:.4f}"


def video_filter(batch: list[dict[str, Any]], fps: Optional[float] = None) -> str:
    """The filter_complex for one batch of chunks, read from an input that was
    cut to start at the batch's first chunk (times are relative to it). The
    result is held on its last frame if rounding left it short and cut to the
    exact length the chunks add up to."""
    base = batch[0]["src_start"]
    expected = batch[-1]["out_end"] - batch[0]["out_start"]
    parts = []
    for i, c in enumerate(batch):
        chain = f"[0:v]trim=start={_fmt(c['src_start'] - base)}:end={_fmt(c['src_end'] - base)},setpts=PTS-STARTPTS"
        if c["factor"] != 1.0:
            chain += f",setpts=PTS*{c['factor']:.6f}"
        if fps:
            chain += f",fps={fps:.6f}"
        parts.append(f"{chain}[v{i}]")
    labels = "".join(f"[v{i}]" for i in range(len(batch)))
    parts.append(f"{labels}concat=n={len(batch)}:v=1:a=0,tpad=stop_mode=clone:stop_duration={DRIFT_PAD_S},"
                 f"trim=end={_fmt(expected)},setpts=PTS-STARTPTS[v]")
    return ";".join(parts)


def audio_filter(batch: list[dict[str, Any]]) -> str:
    """The same for audio: each chunk is cut and slowed with `atempo` (1 / factor)."""
    base = batch[0]["src_start"]
    expected = batch[-1]["out_end"] - batch[0]["out_start"]
    parts = []
    for i, c in enumerate(batch):
        chain = f"[0:a]atrim=start={_fmt(c['src_start'] - base)}:end={_fmt(c['src_end'] - base)},asetpts=PTS-STARTPTS"
        stages = atempo_chain(1.0 / c["factor"]) if c["factor"] != 1.0 else []
        if stages:
            chain += "," + ",".join(stages)
        parts.append(f"{chain}[a{i}]")
    labels = "".join(f"[a{i}]" for i in range(len(batch)))
    parts.append(f"{labels}concat=n={len(batch)}:v=0:a=1,apad=whole_dur={_fmt(expected)},"
                 f"atrim=end={_fmt(expected)},asetpts=PTS-STARTPTS[a]")
    return ";".join(parts)


# ---------------------------------------------------------------- ffmpeg --

def _run(cmd: list[str], timeout: float, what: str) -> None:
    proc = procutil.run(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise RetimeError(f"{what}_failed", (proc.stderr or b"").decode("utf-8", "replace")[:800])


def _concat_copy(ffmpeg: str, parts: list[Path], dest: Path, list_file: Path) -> None:
    lines = []
    for p in parts:
        escaped = str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _run([ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_file),
          "-c", "copy", str(dest)], 1800, "concat")


def _run_batches(ffmpeg: str, src: Path, chunks: list[dict[str, Any]], dest: Path, kind: str, size: int,
                 fps: Optional[float], crf: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    groups = batches(chunks, size)
    tmp_dir = Path(tempfile.mkdtemp(prefix="retime_", dir=str(dest.parent))) if len(groups) > 1 else None
    try:
        parts: list[Path] = []
        for n, group in enumerate(groups):
            out = (tmp_dir / f"part{n:04d}{dest.suffix}") if tmp_dir else dest
            start = group[0]["src_start"]
            length = group[-1]["src_end"] - start
            cmd = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-ss", _fmt(start), "-t", _fmt(length), "-i", str(src)]
            if kind == "video":
                cmd += ["-filter_complex", video_filter(group, fps), "-map", "[v]", "-an", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
            else:
                cmd += ["-filter_complex", audio_filter(group), "-map", "[a]"]
            cmd.append(str(out))
            _run(cmd, 3600, f"retime_{kind}")
            if not out.is_file():
                raise RetimeError(f"retime_{kind}_failed", f"ffmpeg wrote no {kind} part")
            parts.append(out)
        if tmp_dir:
            _concat_copy(ffmpeg, parts, dest, tmp_dir / "parts.txt")
    finally:
        if tmp_dir:
            for f in tmp_dir.iterdir():
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()
    if not dest.is_file():
        raise RetimeError(f"retime_{kind}_failed", f"no {kind} was written")


def retime_video(ffmpeg: str, src: Path, chunks: list[dict[str, Any]], dest: Path, *, fps: Optional[float] = None,
                 batch_size: int = BATCH_SIZE, crf: int = 18) -> None:
    """The source video (no audio) with every stretched chunk slowed down;
    `new_duration(chunks)` seconds long to within a frame."""
    _run_batches(ffmpeg, src, chunks, dest, "video", batch_size, fps, crf)


def retime_audio(ffmpeg: str, src: Path, chunks: list[dict[str, Any]], dest: Path, *, batch_size: int = BATCH_SIZE) -> None:
    """An audio track kept in step with `retime_video`: the same chunks, slowed
    with `atempo`, written as WAV."""
    _run_batches(ffmpeg, src, chunks, dest, "audio", batch_size, None, 0)
