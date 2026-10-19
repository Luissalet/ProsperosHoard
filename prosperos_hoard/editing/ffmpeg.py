"""Lumiere's names for ffmpeg, over the shared ``hoard_link.media.ffmpeg``.

Everything that touches the binaries (finding them, probing, running with progress and cancellation, the encoder list, PCM and frame
extraction, loudness) is the commons' ``FFmpeg``. What stays here is the app's vocabulary, so the thirty-odd call sites keep reading
the same: times in **milliseconds**, a ``Tools`` object, a ``RunHandle`` a job cancels, and the app's errors (``FfmpegMissing`` /
``FfmpegFailed``, which the API maps to HTTP codes).

Cancelling a ``RunHandle`` kills the whole ffmpeg process tree (a filter script that spawned helpers no longer leaves them behind).
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .errors import FfmpegFailed, FfmpegMissing, LumiereError
from .hoard_link import proc as hlproc
from .hoard_link.errors import Unavailable
from .hoard_link.media import ffmpeg as shared
from .hoard_link.media.ffmpeg import (  # noqa: F401 - re-exported: the app imports these from here
    IMAGE_CODECS, MEDIA_EXTENSIONS, FFmpeg, FFmpegError, filter_path, filter_text, fps_fraction)


class Cancelled(LumiereError):
    code = "canceled"


class RunHandle:
    """Lets a job cancel the ffmpeg it started. ``cancel()`` sets the event the shared runner watches (which kills the tree) and
    kills the process of a streaming reader that registered itself with :meth:`attach`."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.proc: Optional[subprocess.Popen] = None

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    @cancelled.setter
    def cancelled(self, value: bool) -> None:
        if value:
            self.event.set()
        else:
            self.event.clear()

    def attach(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        if self.cancelled:
            self.cancel()

    def cancel(self) -> None:
        self.event.set()
        proc = self.proc
        if proc is not None and proc.poll() is None:
            hlproc.kill_tree(proc, grace_s=1.0)


def _event(handle: Optional[RunHandle]) -> Optional[threading.Event]:
    return handle.event if handle is not None else None


def _wrapped(call: Callable[[], Any], handle: Optional[RunHandle] = None) -> Any:
    """Run a shared call and answer in the app's errors."""
    try:
        result = call()
    except hlproc.Cancelled as error:
        raise Cancelled("Canceled.") from error
    except FFmpegError as error:
        raise FfmpegFailed(str(error)) from error
    except Unavailable as error:
        raise FfmpegMissing(str(error)) from error
    if handle is not None and handle.cancelled:
        raise Cancelled("Canceled.")
    return result


@dataclass(eq=False)
class Tools:
    """The ffmpeg / ffprobe of this machine: the paths and capability lists the app shows, over one shared ``FFmpeg``."""

    ffmpeg: str
    ffprobe: str
    version: str = ""
    shared: FFmpeg = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.shared is None:
            self.shared = FFmpeg({"ffmpeg": [self.ffmpeg], "ffprobe": [self.ffprobe]})

    @property
    def filters(self) -> frozenset[str]:
        return self.shared.filters

    @property
    def encoders(self) -> frozenset[str]:
        return self.shared.encoders

    def has_filter(self, name: str) -> bool:
        return self.shared.has_filter(name)

    def video_encoder(self, preference: str = "auto", codec: str = "h264") -> str:
        """h264_nvenc / hevc_nvenc when the GPU encoder really works (tested once), else libx264 / libx265."""
        try:
            return self.shared.video_encoder(preference, codec)
        except FFmpegError as error:
            raise LumiereError("The NVIDIA encoder was requested (LUMIERE_ENCODER=nvenc) but it does not work here.",
                               code=error.code) from error

    def nvenc_works(self) -> bool:
        return self.shared.video_encoder("auto") == "h264_nvenc"


def _explicit(name: str, value: str) -> Optional[str]:
    if not value:
        return None
    if Path(value).is_file() or hlproc.which(value):
        return value
    raise FfmpegMissing(f"{name} not found at {value}.")


def discover(ffmpeg: str = "", ffprobe: str = "") -> Tools:
    """ffmpeg and ffprobe: the configured paths if any, else the shared lookup (``HOARD_FFMPEG``, ``LUMIERE_FFMPEG``, the folder
    of a WinGet install, PATH ...). Each candidate is run once to prove it works."""
    given = {k: v for k, v in (("ffmpeg", _explicit("ffmpeg", ffmpeg)), ("ffprobe", _explicit("ffprobe", ffprobe))) if v}
    engine = FFmpeg(given or None)
    try:
        paths = {"ffmpeg": engine.ffmpeg, "ffprobe": engine.ffprobe}
    except Unavailable as error:
        raise FfmpegMissing(f"{error}. Install ffmpeg (winget install Gyan.FFmpeg) or set LUMIERE_FFMPEG.") from error
    try:
        version = engine.version
    except Exception:  # noqa: BLE001 - informative only
        version = ""
    return Tools(paths["ffmpeg"][0], paths["ffprobe"][0], version, engine)


# ---------------------------------------------------------------- probing

def probe(tools: Tools, path: Path) -> dict[str, Any]:
    return _wrapped(lambda: tools.shared.probe(path))


def summarize(info: dict[str, Any], path: Path) -> dict[str, Any]:
    """The fields the app stores for a media file (milliseconds)."""
    out = dict(FFmpeg.summarize(info))
    out.pop("duration_s", None)
    return out


def keyframes(tools: Tools, path: Path, limit_s: Optional[float] = None) -> list[int]:
    """Keyframe times in ms (packet flags), for cuts without re-encoding."""
    args = [*tools.shared.ffprobe, "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
            "frame=pts_time,best_effort_timestamp_time", "-of", "csv=p=0", str(path)]
    if limit_s:
        args[len(tools.shared.ffprobe):len(tools.shared.ffprobe)] = ["-read_intervals", f"%+{limit_s}"]
    done = hlproc.run(args, timeout=600)
    out: list[int] = []
    for line in done.stdout.splitlines():
        for raw in line.split(","):
            try:
                out.append(int(round(float(raw) * 1000)))
                break
            except ValueError:
                continue
    return sorted(set(out))


# ---------------------------------------------------------------- running

def run(tools: Tools, args: list[str], *, duration_ms: int = 0, progress: Optional[Callable[[float], None]] = None,
        handle: Optional[RunHandle] = None, timeout: Optional[float] = None, cwd: Optional[Path] = None) -> str:
    """Run ffmpeg with ``-progress pipe:1``; report 0..1 through ``progress``; raise FfmpegFailed with the log tail."""
    return _wrapped(lambda: tools.shared.run(args, duration_s=duration_ms / 1000.0, progress=progress, cancel=_event(handle),
                                             timeout=timeout, cwd=cwd), handle)


def capture(tools: Tools, args: list[str], *, timeout: float = 3600, handle: Optional[RunHandle] = None, binary: bool = False) -> tuple[Any, str]:
    """Run ffmpeg and return (stdout, stderr): analysis filters that print to the log, raw PCM and frames."""
    return _wrapped(lambda: tools.shared.capture(args, binary=binary, timeout=timeout, cancel=_event(handle)), handle)


def spawn(tools: Tools, args: list[str], handle: Optional[RunHandle] = None) -> subprocess.Popen:
    """ffmpeg started with its stdout open for the caller to read (raw PCM or frames, block by block). The process is registered with
    ``handle`` so cancelling kills it with its children; the caller kills it in ``finally``."""
    cmd = [*tools.shared.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *(os.fspath(a) for a in args)]
    proc = hlproc.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if handle is not None:
        handle.attach(proc)
    return proc


# ---------------------------------------------------------------- escaping and small helpers

def loudness(tools: Tools, path: Path, *, stream: int = 0, handle: Optional[RunHandle] = None) -> dict[str, Any]:
    """EBU R128 integrated loudness, range and true peak of one audio stream."""
    got = _wrapped(lambda: tools.shared.loudness(path, stream=stream), handle)
    return {"integrated_lufs": got["integrated_lufs"], "range_lu": got["lra"], "true_peak_dbtp": got["true_peak_dbfs"]}


def loudnorm_measure(tools: Tools, path: Path, target: float, tp: float = -1.5, lra: float = 11.0,
                     handle: Optional[RunHandle] = None) -> dict[str, float]:
    """First pass of two-pass loudnorm (``input_i``, ``input_tp``, ``input_lra``, ``input_thresh``, ``target_offset``); ``{}`` when
    ffmpeg printed no measurement (no audio to measure)."""
    try:
        return _wrapped(lambda: tools.shared.loudnorm_measure(path, target_i=target, target_tp=tp, target_lra=lra), handle)
    except FfmpegFailed as error:
        if "no measurement" in str(error):
            return {}
        raise


def loudnorm_filter(measured: dict[str, float], target: float, tp: float = -1.5, lra: float = 11.0) -> str:
    """The second-pass ``loudnorm=`` filter built from :func:`loudnorm_measure`."""
    return FFmpeg.loudnorm_second_pass(measured, target_i=target, target_tp=tp, target_lra=lra)


def seconds(ms: int | float) -> str:
    return shared.seconds(max(0.0, ms) / 1000)


def extract_pcm(tools: Tools, path: Path, *, rate: int = 16000, start_ms: int = 0, duration_ms: int = 0, stream: int = 0,
                handle: Optional[RunHandle] = None) -> bytes:
    """Mono s16le PCM of one audio stream."""
    return _wrapped(lambda: tools.shared.extract_pcm(path, rate=rate, start_s=start_ms / 1000.0, duration_s=duration_ms / 1000.0,
                                                     stream=stream, cancel=_event(handle)), handle)


def extract_frames(tools: Tools, path: Path, *, width: int, fps: float, start_ms: int = 0, duration_ms: int = 0,
                   gray: bool = False, handle: Optional[RunHandle] = None) -> tuple[bytes, int, int]:
    """Raw frames (rgb24 or gray) scaled to ``width`` at ``fps``; returns (bytes, width, height)."""
    return _wrapped(lambda: tools.shared.extract_frames(path, width=width, fps=fps, start_s=start_ms / 1000.0,
                                                        duration_s=duration_ms / 1000.0, gray=gray, cancel=_event(handle)), handle)


def grab_frame(tools: Tools, path: Path, at_ms: int, out: Path, width: int = 0) -> Path:
    """One frame as PNG/JPEG (by extension)."""
    return _wrapped(lambda: tools.shared.grab_frame(path, at_ms / 1000.0, out, width))


def media_files(paths: Iterable[Path]) -> list[Path]:
    return [p for p in paths if p.suffix.lower() in MEDIA_EXTENSIONS]
