"""Picture analysis: scene changes, motion per second and the focus path used to reframe a horizontal video into a
vertical one (faces when OpenCV is installed, otherwise where the motion and the detail are)."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .. import ffmpeg as ff
from ..ffmpeg import Cancelled, RunHandle, Tools
from . import faces


def _hsv(frame: np.ndarray) -> np.ndarray:
    """RGB uint8 (h, w, 3) -> HSV float32 in 0..255 (vectorised)."""
    rgb = frame.astype(np.float32) / 255.0
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    delta = mx - mn
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    h = np.zeros_like(mx)
    nz = delta > 1e-6
    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & ~rm & ~gm
    h[rm] = ((g - b)[rm] / delta[rm]) % 6
    h[gm] = (b - r)[gm] / delta[gm] + 2
    h[bm] = (r - g)[bm] / delta[bm] + 4
    h = h / 6.0
    s_ = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0)
    return np.stack([h, s_, mx], axis=2) * 255.0


def scenes(tools: Tools, path: Path, *, threshold: float = 27.0, min_scene_ms: int = 800, duration_ms: int = 0, src_w: int = 0, src_h: int = 0,
           fps: float = 0.0, progress: Optional[Callable[[float], None]] = None, handle: Optional[RunHandle] = None) -> list[dict[str, Any]]:
    """Scene cuts by content change: mean difference of hue, saturation and value between consecutive frames of a 96 px copy
    (0..255; 27 is a clear cut). Colour-aware, so a red shot followed by a green one of the same brightness still counts."""
    rate = fps if 0 < fps <= 60 else 25.0
    cuts: list[dict[str, Any]] = []
    prev = None
    for i, frame in read_frames(tools, path, width=96, fps=rate, gray=False, src_w=src_w or 16, src_h=src_h or 9, handle=handle):
        cur = _hsv(frame)
        if prev is not None:
            dh = np.abs(cur[..., 0] - prev[..., 0])
            dh = np.minimum(dh, 255 - dh)  # hue wraps around
            score = float((dh.mean() + np.abs(cur[..., 1] - prev[..., 1]).mean() + np.abs(cur[..., 2] - prev[..., 2]).mean()) / 3)
            t = int(round(i / rate * 1000))
            if score >= threshold and (not cuts or t - cuts[-1]["t"] >= min_scene_ms) and t >= min_scene_ms // 2:
                cuts.append({"t": t, "score": round(score, 1)})
        prev = cur
        if progress and duration_ms and i % 100 == 0:
            progress(min(0.99, i / rate * 1000 / duration_ms))
    return cuts


def read_frames(tools: Tools, path: Path, *, width: int, fps: float, gray: bool, src_w: int, src_h: int, start_ms: int = 0,
                duration_ms: int = 0, handle: Optional[RunHandle] = None):
    """Yield (index, frame ndarray) at ``fps`` scaled to ``width`` without loading the whole clip."""
    h = max(2, int(round(src_h * width / max(1, src_w) / 2)) * 2) if src_w else width
    cmd: list[str] = []
    if start_ms:
        cmd += ["-ss", ff.seconds(start_ms)]
    cmd += ["-i", str(path)]
    if duration_ms:
        cmd += ["-t", ff.seconds(duration_ms)]
    cmd += ["-an", "-vf", f"fps={fps},scale={width}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "gray" if gray else "rgb24", "pipe:1"]
    proc = ff.spawn(tools, cmd, handle)
    size = width * h * (1 if gray else 3)
    i = 0
    assert proc.stdout is not None
    try:
        while True:
            data = proc.stdout.read(size)
            if len(data) < size:
                break
            frame = np.frombuffer(data, dtype=np.uint8).reshape((h, width) if gray else (h, width, 3))
            yield i, frame
            i += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if handle is not None and handle.cancelled:
        raise Cancelled("Canceled.")


def motion_per_second(tools: Tools, path: Path, *, src_w: int, src_h: int, duration_ms: int = 0, fps: float = 4.0,
                      progress: Optional[Callable[[float], None]] = None, handle: Optional[RunHandle] = None) -> list[float]:
    """Mean absolute frame difference per second (0..1) on a 96 px grey copy: how much happens on screen."""
    prev = None
    per: dict[int, list[float]] = {}
    for i, f in read_frames(tools, path, width=96, fps=fps, gray=True, src_w=src_w, src_h=src_h, handle=handle):
        cur = f.astype(np.int16)
        if prev is not None:
            sec = int(i / fps)
            per.setdefault(sec, []).append(float(np.abs(cur - prev).mean() / 255.0))
        prev = cur
        if progress and duration_ms and i % 40 == 0:
            progress(min(0.99, i / fps * 1000 / duration_ms))
    if not per:
        return []
    n = max(per) + 1
    return [round(float(np.mean(per.get(s, [0.0]))), 4) for s in range(n)]


# ---------------------------------------------------------------- reframing

def _saliency(frame: np.ndarray, prev: Optional[np.ndarray]) -> tuple[float, float, float]:
    """(x, y, confidence) of where the eye goes: motion first, detail second, a pull to the centre."""
    g = frame.mean(axis=2) if frame.ndim == 3 else frame.astype(np.float32)
    h, w = g.shape
    gx = np.abs(np.diff(g, axis=1, append=g[:, -1:]))
    gy = np.abs(np.diff(g, axis=0, append=g[-1:, :]))
    detail = gx + gy
    weight = detail * 0.35
    if prev is not None:
        motion = np.abs(g - prev)
        motion[motion < 8] = 0
        weight = weight + motion * 2.0
    ys, xs = np.mgrid[0:h, 0:w]
    prior = np.exp(-(((xs - w / 2) / (w * 0.45)) ** 2 + ((ys - h / 2) / (h * 0.6)) ** 2))
    weight = weight * (0.4 + 0.6 * prior)
    total = float(weight.sum())
    if total < 1e-3:
        return 0.5, 0.5, 0.0
    cx = float((weight * xs).sum() / total) / w
    cy = float((weight * ys).sum() / total) / h
    conf = min(1.0, total / (w * h * 6.0))
    return cx, cy, conf


def focus_track(tools: Tools, path: Path, *, src_w: int, src_h: int, duration_ms: int, start_ms: int = 0, fps: float = 3.0,
                progress: Optional[Callable[[float], None]] = None, handle: Optional[RunHandle] = None,
                detector: Optional[faces.FaceDetector] = None, detector_info: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Raw focus samples: [[src_ms, x, y, weight, kind]] with kind 1 for a face and 0 for saliency (motion and detail). The
    result names the detector that ran (``yunet``, ``haar`` or ``saliency``) and why a better one did not."""
    samples: list[list[float]] = []
    prev_gray = None
    width = 416 if detector is not None and detector.name == "yunet" else 320
    seen_faces = 0
    for i, frame in read_frames(tools, path, width=width, fps=fps, gray=False, src_w=src_w, src_h=src_h, start_ms=start_ms,
                                duration_ms=duration_ms, handle=handle):
        t = start_ms + int(round(i / fps * 1000))
        h, w = frame.shape[:2]
        found = detector.detect(frame) if detector is not None else []
        if found:
            # the biggest face leads
            fx, fy, fw, fh, score = found[0]
            samples.append([t, min(1.0, max(0.0, (fx + fw / 2) / w)), min(1.0, max(0.0, (fy + fh * 0.45) / h)),
                            float(fw * fh) / (w * h) * 40 * max(0.3, min(1.0, score)), 1])
            seen_faces += 1
            prev_gray = frame.mean(axis=2)
        else:
            g = frame.mean(axis=2)
            x, y, conf = _saliency(frame, prev_gray)
            samples.append([t, x, y, conf * 0.5, 0])
            prev_gray = g
        if progress and duration_ms and i % 15 == 0:
            progress(min(0.99, i / fps * 1000 / max(1, duration_ms)))
    info = dict(detector_info or {})
    name = detector.name if detector is not None else "saliency"
    info["detector"] = name
    return {"samples": samples, "faces": detector is not None, "fps": fps, "detector": name, "face_samples": seen_faces,
            "detector_info": info}


def smooth_path(samples: list[list[float]], cuts: list[int], *, aspect_in: float, aspect_out: float, mode: str = "auto",
                sigma_s: float = 0.8, max_speed: float = 0.35) -> list[list[float]]:
    """Turn raw samples into a camera path [[src_ms, x, y]] per scene: a still camera when the subject stays put
    (``stable``), a smoothed, speed-limited pan otherwise (``track``); ``center`` ignores the samples."""
    if not samples:
        return []
    arr = np.array(samples, dtype=np.float64)
    t, x, y, w = arr[:, 0], arr[:, 1], arr[:, 2], np.maximum(arr[:, 3], 0.02)
    # how much of the width the vertical window covers: the path is clamped so the window stays inside the frame
    window = min(1.0, aspect_out / aspect_in)
    half = window / 2
    out: list[list[float]] = []
    bounds = [0] + sorted(c for c in cuts if t[0] < c < t[-1]) + [int(t[-1]) + 1]
    for a, b in zip(bounds, bounds[1:]):
        sel = (t >= a) & (t < b)
        if not sel.any():
            continue
        ts, xs, ys, ws = t[sel], x[sel], y[sel], w[sel]
        if mode == "center":
            px = np.full_like(xs, 0.5)
            py = np.full_like(ys, 0.5)
        else:
            spread = float(np.sqrt(np.average((xs - np.average(xs, weights=ws)) ** 2, weights=ws)))
            if mode == "stable" or (mode == "auto" and spread < window * 0.18):
                px = np.full_like(xs, float(np.average(xs, weights=ws)))
                py = np.full_like(ys, float(np.average(ys, weights=ws)))
            else:
                dt = np.median(np.diff(ts)) / 1000 if ts.size > 1 else 0.33
                k = max(1, int(round(sigma_s / max(dt, 1e-3) * 3)))
                kernel = np.exp(-0.5 * (np.arange(-k, k + 1) * dt / sigma_s) ** 2)
                pad = lambda v: np.concatenate([np.full(k, v[0]), v, np.full(k, v[-1])])  # noqa: E731
                px = np.convolve(pad(xs * ws), kernel, "valid") / np.convolve(pad(ws), kernel, "valid")
                py = np.convolve(pad(ys * ws), kernel, "valid") / np.convolve(pad(ws), kernel, "valid")
                # limit the pan speed (fraction of the frame per second)
                for i in range(1, px.size):
                    step = max_speed * max(1e-3, (ts[i] - ts[i - 1]) / 1000)
                    px[i] = px[i - 1] + max(-step, min(step, px[i] - px[i - 1]))
        px = np.clip(px, half, 1 - half)
        for i in range(ts.size):
            out.append([int(ts[i]), round(float(px[i]), 4), round(float(py[i]), 4)])
        # hold the position up to the cut so the next scene starts with a jump, not a pan
        if ts.size and b - ts[-1] > 50:
            out.append([int(b - 1), round(float(px[-1]), 4), round(float(py[-1]), 4)])
    return _simplify(out)


def _simplify(path: list[list[float]], tol: float = 0.004) -> list[list[float]]:
    """Drop points that a straight line between their neighbours already describes (keeps the expressions short)."""
    if len(path) < 3:
        return path
    keep = [path[0]]
    for i in range(1, len(path) - 1):
        a, b, c = keep[-1], path[i], path[i + 1]
        span = c[0] - a[0]
        if span <= 0:
            continue
        u = (b[0] - a[0]) / span
        if abs(a[1] + (c[1] - a[1]) * u - b[1]) > tol or abs(a[2] + (c[2] - a[2]) * u - b[2]) > tol * 2 or c[0] - a[0] > 4000:
            keep.append(b)
    keep.append(path[-1])
    return keep


def aspect_of(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:/x]\s*(\d+(?:\.\d+)?)\s*", text or "")
    if not m:
        raise ValueError(f"Not an aspect ratio: {text!r} (use 9:16, 1:1, 4:5, 16:9).")
    return float(m.group(1)) / float(m.group(2))


def isfinite(v: float) -> bool:
    return math.isfinite(v)
