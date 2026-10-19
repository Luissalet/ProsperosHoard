"""Awkward sources from phones and screen recorders: rotation flags and variable frame rate."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import _ff, needs_ffmpeg
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.render import runner

pytestmark = needs_ffmpeg


def _red_corner(img: Image.Image) -> str:
    """Which quadrant holds the red block."""
    a = np.asarray(img.convert("RGB")).astype(int)
    h, w = a.shape[:2]
    quads = {"tl": a[: h // 2, : w // 2], "tr": a[: h // 2, w // 2:], "bl": a[h // 2:, : w // 2], "br": a[h // 2:, w // 2:]}
    return max(quads, key=lambda k: (quads[k][..., 0] - quads[k][..., 2]).mean())


def _decoded_first_frame(path: Path, tmp: Path) -> Image.Image:
    out = tmp / f"{path.stem}-ref.png"
    _ff("-i", str(path), "-frames:v", "1", str(out))  # ffmpeg applies the rotation flag itself
    return Image.open(out)


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_phone_footage_renders_upright(services, tmp_path, rotation):
    base = tmp_path / "base.mp4"
    _ff("-f", "lavfi", "-i", "color=c=blue:s=320x176:r=25:d=2,drawbox=x=0:y=0:w=160:h=88:c=red:t=fill", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-preset", "ultrafast", str(base))
    src = tmp_path / f"rot{rotation}.mp4"
    _ff("-display_rotation", str(rotation), "-i", str(base), "-c", "copy", str(src))
    ref = _decoded_first_frame(src, tmp_path)
    m = media_store.import_path(services, str(src))
    assert (m["width"], m["height"]) == ref.size
    pid = store.create(services, "Rot", media=[m["id"]], width=ref.size[0], height=ref.size[1], fps=25)["id"]
    frame = Image.open(runner.render_frame(services, pid, 200, width=ref.size[0]))
    assert frame.size == ref.size
    assert _red_corner(frame) == _red_corner(ref)
    proxy = media_store.proxy_path(services, m["id"])
    assert proxy
    proxy_frame = _decoded_first_frame(Path(proxy), tmp_path)
    assert _red_corner(proxy_frame) == _red_corner(ref)


def _first_flash_s(path: Path, fps: int = 30) -> float:
    """Time of the first bright frame (the renders are constant frame rate, so frame index / fps is the time)."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=16:9,format=gray", "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 16 * 9)
    bright = np.nonzero(frames.mean(axis=1) > 128)[0]
    assert len(bright), "no flash found"
    return float(bright[0] / fps)


def _first_beep_s(path: Path) -> float:
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.abs(np.frombuffer(pcm, dtype=np.int16).astype(float))
    return float(np.argmax(a > 8000) / 8000)


def test_variable_frame_rate_screen_recording_stays_in_sync(services, tmp_path):
    # a flash and a beep together at 3.0 s; frames are dropped unevenly, as screen recorders do when nothing moves
    cfr = tmp_path / "cfr.mp4"
    _ff("-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=6,drawbox=c=white:t=fill:enable='between(t,3,3.2)'",
        "-f", "lavfi", "-i", "aevalsrc='if(between(t,3,3.1),0.8*sin(2*PI*1000*t),0)':s=48000:d=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cfr))
    vfr = tmp_path / "vfr.mp4"
    _ff("-i", str(cfr), "-vf", "select='not(mod(n,4))+between(t,0.9,1.4)+between(t,2.9,3.3)'", "-fps_mode", "vfr",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", str(vfr))
    m = media_store.import_path(services, str(vfr))
    pid = store.create(services, "VFR", media=[m["id"]], width=320, height=180, fps=30)["id"]
    job = services.start_render(pid, preset="final")
    job = services.jobs.get(job["id"])
    assert job["state"] == "done", job["error"]
    out = Path(job["result"]["path"])
    flash, beep = _first_flash_s(out), _first_beep_s(out)
    assert abs(flash - 3.0) <= 0.05 and abs(beep - 3.0) <= 0.05
    assert abs(flash - beep) <= 1 / 30 + 0.01
