"""Rendered ease polylines must stay identical after a mid-span split (#124)."""
from __future__ import annotations

from prosperos_hoard.editing.render.compiler import _ease_points, piecewise
from prosperos_hoard.editing.timeline import Keyframe, keyframe_value, slice_keyframes


def _piecewise_at(pts: list[tuple[float, float]], t_s: float) -> float:
    # Evaluate the same clip()/sum form without ffmpeg: reuse Python piecewise semantics.
    v = pts[0][1]
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        dt = tb - ta
        if dt <= 1e-9 or abs(vb - va) < 1e-9:
            continue
        x = t_s - ta
        if x <= 0:
            continue
        if x >= dt:
            v += vb - va
        else:
            v += (vb - va) * (x / dt)
    return v


def test_split_keeps_sampled_polyline_at_sparks_repro_times():
    original = [Keyframe(t=0, v=0, ease="ease_in"), Keyframe(t=1000, v=1)]
    right = [
        Keyframe(t=0, v=0.25, ease="ease_in", ease_span=1000, ease_into=500, ease_v0=0, ease_v1=1),
        Keyframe(t=500, v=1),
    ]
    assert abs(keyframe_value(original, 537) - keyframe_value(right, 37)) < 1e-12

    o_pts = _ease_points(original)
    r_pts = _ease_points(right)
    assert _piecewise_at(o_pts, 0.537) == _piecewise_at(r_pts, 0.037)
    assert _piecewise_at(o_pts, 0.750) == _piecewise_at(r_pts, 0.250)
    assert _piecewise_at(o_pts, 0.537) == 0.2931666666666667


def test_arbitrary_cuts_match_original_polyline_on_mesh():
    original = [Keyframe(t=0, v=0, ease="ease_in"), Keyframe(t=1000, v=1)]
    o_pts = _ease_points(original)
    for cut in (123, 250, 400, 500, 700):
        left = slice_keyframes({"opacity": original}, 0, cut)["opacity"]
        right = slice_keyframes({"opacity": original}, cut, 1000)["opacity"]
        lp, rp = _ease_points(left), _ease_points(right)
        for t in range(0, 1001, 5):
            want = _piecewise_at(o_pts, t / 1000)
            got = (
                _piecewise_at(lp, t / 1000)
                if t <= cut
                else _piecewise_at(rp, (t - cut) / 1000)
            )
            assert abs(want - got) < 1e-12, f"cut={cut} t={t}: {want} vs {got}"


def test_repeated_cut_on_right_half_stays_exact():
    original = [Keyframe(t=0, v=0, ease="ease_in"), Keyframe(t=1000, v=1)]
    o_pts = _ease_points(original)
    mid = slice_keyframes({"opacity": original}, 400, 1000)["opacity"]
    again = slice_keyframes({"opacity": mid}, 100, 600)["opacity"]  # global 500..1000
    a_pts = _ease_points(again)
    for t in range(500, 1001, 7):
        want = _piecewise_at(o_pts, t / 1000)
        got = _piecewise_at(a_pts, (t - 500) / 1000)
        assert abs(want - got) < 1e-12, f"t={t}: {want} vs {got}"
