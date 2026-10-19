"""Every output frame shows the source frame the timing rule says, in exports cut into many chunks and in single exact
frames, for source and canvas rates that differ, in points that are not on the source frame grid, at several speeds,
reversed and on speed curves.

The rule: the output frame at time t shows the last source frame (in playback order) that the timeline reaches before
t + half an output frame. Forward a frame is reached at its own time; reversed, at its end (the next frame's time).
"""

import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from conftest import needs_ffmpeg
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.render import compiler as C
from prosperos_hoard.editing.render import runner

pytestmark = needs_ffmpeg

W, H = 192, 64
# (source, timeline length, speed, reverse) on the main track; in points off the source grid
LAYOUT = [((517, 2517), 1.0, False), ((1033, 2533), 0.5, False), ((2950, 5950), 1.5, False), ((3011, 8011), 2.5, False),
          ((4567, 6067), 1.0, True), ((1234, 4234), 1.5, True)]


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _counter(path: Path, rate: str, seconds: int) -> None:
    """Three blocks whose grey level spells the frame index in base 8 (up to 511 frames), with a tone."""
    expr = "if(lt(X,64),mod(N,8)*32+16,if(lt(X,128),mod(floor(N/8),8)*32+16,floor(N/64)*32+16))"
    _ff("-f", "lavfi", "-i", f"color=s={W}x{H}:r={rate}:d={seconds},geq=lum='{expr}':cb=128:cr=128", "-f", "lavfi", "-i",
        f"sine=f=440:r=48000:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "6", "-g", "12", "-c:a", "aac",
        "-shortest", str(path))


def _index(frame: np.ndarray) -> int:
    h, w = frame.shape
    d = [int(round((frame[h // 4: 3 * h // 4, x - w // 12: x + w // 12].mean() - 16) / 32)) for x in (w // 6, w // 2, 5 * w // 6)]
    return d[0] + 8 * d[1] + 64 * d[2]


def _frames(path: Path) -> list[int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
    return [_index(f) for f in np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(int)]


def expected(clips, f: int, out_fps: float, src_fps: float) -> tuple[set[int], bool]:
    """Source frames acceptable for output frame f ({one}, or two on an exact tie) and whether a clip shows at all."""
    t = f * 1000 / out_fps
    clip = next((c for c in clips if c.start <= t + 1e-6 and t < c.end), None)
    if clip is None:
        return set(), False
    frame = 1000 / src_fps
    limit = t + 500 / out_fps
    guess = int(clip.src_at(t) / frame)
    best, tie = None, set()
    for k in range(max(0, guess - 12), guess + 13):
        reach = clip.timeline_at((k + clip.reverse) * frame)
        if reach < limit:
            if best is None or (k < best if clip.reverse else k > best):
                best = k
        if abs(reach - limit) < 0.05:
            tie.add(k)
    return ({best} | tie) if tie else {best}, True


def _project(services, mid: str, out_fps: float, ramp: bool) -> str:
    pid = store.create(services, "Precisión", width=W, height=H, fps=out_fps)["id"]
    ops = [{"op": "add_media", "media": mid, "src_in": a, "src_out": b} for (a, b), _, _ in LAYOUT]
    if ramp:
        ops.append({"op": "add_media", "media": mid, "src_in": 6011, "src_out": 9011})
    store.edit(services, pid, ops)
    clips = sorted(store.doc(services, pid).main_track().clips, key=lambda c: c.start)
    ops = []
    for c, (_, speed, rev) in zip(clips, LAYOUT):
        if speed != 1:
            ops.append({"op": "speed", "clip": c.id, "speed": speed})
        if rev:
            ops.append({"op": "set", "clip": c.id, "props": {"reverse": True}})
    if ramp:
        ops.append({"op": "speed_ramp", "clip": clips[-1].id, "preset": "hit", "speed": 0.3})
    store.edit(services, pid, ops)
    return pid


@pytest.mark.parametrize("src_rate,out_fps", [("25", 30.0), ("30", 24.0), ("30", 29.97), ("30000/1001", 30.0)])
def test_exports_and_exact_frames_follow_the_frame_rule(services, tmp_path, monkeypatch, src_rate, out_fps):
    src = tmp_path / "counter.mp4"
    _counter(src, src_rate, 16)
    src_fps = float(Fraction(src_rate))
    mid = media_store.import_path(services, str(src))["id"]
    pid = _project(services, mid, out_fps, ramp=src_rate == "25")
    p = store.doc(services, pid)
    clips = sorted(p.main_track().clips, key=lambda c: c.start)
    # small chunks, so many joins fall inside clips at times that are not whole milliseconds
    real_plan = C.plan_chunks
    plans = []

    def small(project, total, fps, **_):
        plan = real_plan(project, total, fps, target_ms=700, min_ms=300, max_ms=1100)
        plans.append(plan)
        return plan

    monkeypatch.setattr(C, "plan_chunks", small)
    job = services.jobs.get(services.start_render(pid, preset="final", lufs=None)["id"])
    assert job["state"] == "done", job["error"]
    assert len(plans[0]) >= 8
    got = _frames(Path(job["result"]["path"]))
    assert len(got) == C.frame_of_ms(p.duration, out_fps)
    wrong = []
    for f, g in enumerate(got):
        want, shown = expected(clips, f, out_fps, src_fps)
        if shown and g not in want:
            wrong.append((f, g, sorted(want)))
    joins = {a for a, _ in plans[0]}
    assert not wrong, (f"{len(wrong)} of {len(got)} frames off; at chunk joins: {[w for w in wrong if w[0] in joins]}", wrong[:12])
    # the exact frame is the export's frame: first and last frame of every clip, a frame in the middle, and chunk joins
    picks = sorted({C.frame_of_ms(c.start, out_fps) + 1 for c in clips} | {C.frame_of_ms(c.end, out_fps) - 1 for c in clips}
                   | {C.frame_of_ms((c.start + c.end) / 2, out_fps) for c in clips} | set(list(joins)[1:5]))
    from PIL import Image

    for f in picks:
        if f >= len(got):
            continue
        still = runner.render_frame(services, pid, int(np.ceil(f * 1000 / out_fps)), fmt="png")
        exact = _index(np.asarray(Image.open(still).convert("L")).astype(int))
        assert exact == got[f], (f, exact, got[f])
