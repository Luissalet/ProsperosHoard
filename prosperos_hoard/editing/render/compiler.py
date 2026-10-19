"""Timeline -> ffmpeg. Pure functions: they build argument lists and filtergraph text, never run anything.

Picture: the timeline is cut into chunks (about 8 s, on clip boundaries when possible, never inside a fade or a
transition). Each chunk is one ffmpeg run whose inputs are only the clips visible in it, each opened with an input
seek, so a two-hour timeline with hundreds of cuts never opens hundreds of decoders at once. Chunks are encoded with
identical settings and joined without re-encoding.

Sound: one pass over the whole timeline from per-media FLAC masters (``amovie`` inside the filter script, so the
command line stays short), lanes concatenated with silence, tracks mixed, music ducked under speech, limited.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..timeline import EQ_KEY_PROPS, MASK_PROPS, Clip, Keyframe, Mask, Project, Track
from . import filters as fx

XFADE = {
    "crossfade": "fade", "dissolve": "dissolve", "fade_black": "fadeblack", "fade_white": "fadewhite",
    "slide_left": "slideleft", "slide_right": "slideright", "slide_up": "slideup", "slide_down": "slidedown",
    "wipe_left": "wipeleft", "wipe_right": "wiperight", "wipe_up": "wipeup", "wipe_down": "wipedown",
    "circle_open": "circleopen", "circle_close": "circleclose", "zoom_in": "zoomin", "pixelize": "pixelize",
    "radial": "radial", "smooth_left": "smoothleft", "smooth_right": "smoothright", "blur": "hblur",
}


@dataclass
class MediaRef:
    id: str
    path: str
    kind: str
    width: int
    height: int
    has_audio: bool
    has_video: bool
    duration_ms: int
    proxy: Optional[str] = None
    fps: float = 0.0  # frames per second of the source picture (0 = unknown: 30 is assumed where a grid is needed)


@dataclass
class Output:
    width: int
    height: int
    fps: float
    fps_expr: str
    use_proxies: bool = False
    factor: float = 1.0  # output pixels per canvas pixel (previews render smaller)
    alpha: bool = False  # transparent where nothing is drawn (a nested sequence that only holds overlays)


@dataclass
class Piece:
    track: Track
    clip: Clip
    t0: float
    t1: float
    other: Optional[Clip] = None  # transition piece: the incoming clip (``clip`` is the outgoing one)
    transition: Optional[str] = None


@dataclass
class ChunkGraph:
    index: int
    f0: int
    f1: int
    inputs: list[list[str]] = field(default_factory=list)
    graph: str = ""
    out_label: str = "vout"


def ms_of_frame(f: int, fps: float) -> float:
    return f * 1000.0 / fps


def frame_of_ms(ms: float, fps: float) -> int:
    return int(round(ms * fps / 1000.0))


def _num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


# ---------------------------------------------------------------- expressions

def _fmt(value: float, digits: int) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def piecewise(points: list[tuple[float, float]], var: str = "t", digits: int = 4) -> str:
    """Linear interpolation through (time_s, value) points as a flat ffmpeg expression (no nesting):
    v0 + sum(slope_i * clip(var - t_i, 0, dt_i))."""
    pts = sorted(points)
    if not pts:
        return "0"
    if len(pts) == 1:
        return _fmt(pts[0][1], digits)
    terms = [_fmt(pts[0][1], digits)]
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        dt = tb - ta
        if dt <= 1e-9 or abs(vb - va) < 1e-9:
            continue
        slope = (vb - va) / dt
        terms.append(f"{_fmt(slope, digits)}*clip({var}-{_fmt(ta, digits)},0,{_fmt(dt, digits)})")
    return "+".join(terms).replace("+-", "-")


_EASE_STEPS = 6


def _canonical_grid(span: float, v0: float, v1: float, ease: str) -> list[tuple[float, float]]:
    """Full-domain 6-step (domain_ms, value) vertices for one eased segment."""
    from ..timeline import ease_value

    span = float(span)
    out: list[tuple[float, float]] = []
    for j in range(0, _EASE_STEPS + 1):
        u = j / float(_EASE_STEPS)
        out.append((span * u, v0 + (v1 - v0) * ease_value(ease, u)))
    return out


def _polyline_at(pts: list[tuple[float, float]], x: float) -> float:
    """Piecewise-linear value of ``pts`` (x ascending) at abscissa ``x``."""
    if not pts:
        return 0.0
    if x <= pts[0][0]:
        return pts[0][1]
    for (xa, va), (xb, vb) in zip(pts, pts[1:]):
        if x <= xb:
            dt = xb - xa
            if dt <= 1e-12:
                return vb
            return va + (vb - va) * ((x - xa) / dt)
    return pts[-1][1]


def _render_endpoint_v(k: Keyframe, prev: Keyframe | None) -> float:
    """Endpoint value on the sampled render polyline (not necessarily analytical ``k.v``)."""
    from ..timeline import _segment_domain

    if k.ease_span is not None and k.ease_into is not None and k.ease_v0 is not None and k.ease_v1 is not None:
        grid = _canonical_grid(float(k.ease_span), float(k.ease_v0), float(k.ease_v1), k.ease or "linear")
        return _polyline_at(grid, float(k.ease_into))
    if prev is not None and prev.ease not in (None, "linear", "hold") and (
        prev.ease_span is not None and prev.ease_into is not None
        and prev.ease_v0 is not None and prev.ease_v1 is not None
    ):
        span, into0, v0, v1 = _segment_domain(prev, k)
        grid = _canonical_grid(float(span), float(v0), float(v1), prev.ease)
        return _polyline_at(grid, float(into0) + (k.t - prev.t))
    return float(k.v)


def _ease_points(keys: list[Keyframe]) -> list[tuple[float, float]]:
    """Keyframes as (seconds, value) points; eased segments get intermediate points, hold segments a step.

    Samples use a fixed 6-step grid on the full ease domain, clipped to the visible segment. Endpoints
    after a mid-span cut use that same polyline (not analytical ``k.v``), so arbitrary cuts keep the
    pre-split render curve exactly.
    """
    from ..timeline import _segment_domain

    out: list[tuple[float, float]] = []
    keys = sorted(keys, key=lambda k: k.t)
    for i, k in enumerate(keys):
        prev = keys[i - 1] if i else None
        out.append((k.t / 1000, _render_endpoint_v(k, prev)))
        if i + 1 >= len(keys):
            break
        n = keys[i + 1]
        if k.ease == "hold":
            out.append(((n.t - 1) / 1000, _render_endpoint_v(k, prev)))
        elif k.ease != "linear":
            span, into, v0, v1 = _segment_domain(k, n)
            origin = k.t - into
            grid = _canonical_grid(float(span), float(v0), float(v1), k.ease)
            for j in range(1, _EASE_STEPS):
                full_t = origin + span * (j / float(_EASE_STEPS))
                if full_t <= k.t or full_t >= n.t:
                    continue
                out.append((full_t / 1000, grid[j][1]))
    return out


def keyframe_expr(keys: list[Keyframe], shift_s: float, var: str = "t") -> str:
    """Expression of the animated value where the clip-local time is ``var + shift_s``."""
    pts = [(t - shift_s, v) for t, v in _ease_points(keys)]
    return piecewise(pts, var)


# ---------------------------------------------------------------- chunk planning

def video_tracks(project: Project) -> list[Track]:
    return [t for t in project.tracks if t.kind == "video" and not t.hidden]


def forbidden_spans(project: Project) -> list[tuple[float, float]]:
    """Timeline spans a chunk boundary must not cut: fades and transitions (their filters need the whole span)."""
    spans: list[tuple[float, float]] = []
    for t in video_tracks(project):
        for c in t.clips:
            if c.transition_in:
                spans.append((c.start, c.start + c.transition_in.dur))
            elif c.fade_in:
                spans.append((c.start, c.start + c.fade_in))
            if c.fade_out:
                spans.append((c.end - c.fade_out, c.end))
            if c.reverse:
                spans.append((c.start, c.end))
    return spans


def plan_chunks(project: Project, total_ms: int, fps: float, target_ms: int = 8000, min_ms: int = 3000, max_ms: int = 16000) -> list[tuple[int, int]]:
    total_f = max(1, frame_of_ms(total_ms, fps))
    spans = [(frame_of_ms(a, fps), frame_of_ms(b, fps)) for a, b in forbidden_spans(project)]

    def blocked(f: int) -> Optional[int]:
        for a, b in spans:
            if a < f < b:
                return b
        return None

    candidates = sorted({frame_of_ms(x, fps) for t in video_tracks(project) for c in t.clips for x in (c.start, c.end)})
    tf, mn, mx = frame_of_ms(target_ms, fps), frame_of_ms(min_ms, fps), frame_of_ms(max_ms, fps)
    chunks: list[tuple[int, int]] = []
    cur = 0
    while cur < total_f:
        if total_f - cur <= mx:
            chunks.append((cur, total_f))
            break
        ideal = cur + tf
        options = [c for c in candidates if cur + mn <= c <= cur + mx and blocked(c) is None]
        if options:
            nxt = min(options, key=lambda c: abs(c - ideal))
        else:
            nxt = ideal
            while True:
                end = blocked(nxt)
                if end is None:
                    break
                nxt = end
        nxt = min(max(nxt, cur + 1), total_f)
        chunks.append((cur, nxt))
        cur = nxt
    return chunks


def grid_ceil(ms: float, fps: float) -> float:
    """The first output frame time at or after ``ms`` (clip edges are whole ms, frames are not: 33 ms -> 33.33 ms at 30 fps)."""
    return math.ceil(ms * fps / 1000 - 1e-6) * 1000 / fps


def pieces_in(project: Project, A: float, B: float, fps: Optional[float] = None) -> list[Piece]:
    """What each visible video track shows between A and B (ms), bottom track first. With ``fps`` the pieces start and
    end on output frames: a clip is in the frames whose time is inside [start, end), so every piece is whole frames and
    sits on the same grid as the chunk."""
    snap = (lambda ms: grid_ceil(ms, fps)) if fps else (lambda ms: ms)
    out: list[Piece] = []
    for t in video_tracks(project):
        clips = sorted((c for c in t.clips if c.type != "text"), key=lambda c: c.start)
        for i, c in enumerate(clips):
            nxt = clips[i + 1] if i + 1 < len(clips) else None
            start = c.start + (c.transition_in.dur if c.transition_in and i > 0 else 0)
            end = c.end
            if nxt is not None and nxt.transition_in and nxt.start < c.end:
                end = nxt.start
                ta, tb = nxt.start, nxt.start + nxt.transition_in.dur
                ga, gb = snap(max(A, ta)), snap(min(B, tb))
                if ta < B and tb > A and gb - ga > 0.5:
                    out.append(Piece(t, c, ga, gb, other=nxt, transition=nxt.transition_in.type))
            a, b = snap(max(A, start)), snap(min(B, end))
            if b - a > 0.5:
                out.append(Piece(t, c, a, b))
    order = {t.id: i for i, t in enumerate(project.tracks)}
    out.sort(key=lambda p: (order[p.track.id], p.t0))
    return out


# ---------------------------------------------------------------- one clip -> filter chain

@dataclass
class SourceWindow:
    """How one piece reads its source. Forward, input timestamps stay absolute: a frame at source second x arrives at
    T = x - ss (``ss`` is exactly the -ss value), so nothing depends on which frame the seek keeps first. Reversed, the
    frames are cut on timestamps at a point of the source frame grid before ``reverse``, so the last frame kept (the
    first one shown) is known; after reversing they are one source frame apart."""

    ss: float  # seconds, as passed to -ss
    length: float  # seconds read
    frame: float  # source frame duration, ms
    trim_end: float = 0.0  # reversed: input seconds; frames at or after it are dropped
    last: float = 0.0  # reversed: source ms of the last frame kept


def _source_frame_ms(media: MediaRef) -> float:
    return 1000.0 / media.fps if media.fps and media.fps > 0 else 1000.0 / 30


def source_window(clip: Clip, media: MediaRef, t0: float, t1: float, fps: float) -> SourceWindow:
    """The source span a piece [t0, t1) may show, with margins: the frame shown at an output time is the last one the
    timeline reaches before the middle of that output frame, which can be a couple of source frames before t0."""
    frame = _source_frame_ms(media)
    vmax = max(v for _, _, v in clip.segments())
    half = 500.0 / fps
    a, b = clip.src_at(t0 - half), clip.src_at(t1 + half)
    margin = 3 * frame + half * vmax
    lo, hi = max(0.0, min(a, b) - margin), max(a, b) + margin
    ss = round(lo / 1000, 6)
    if not clip.reverse:
        return SourceWindow(ss, hi / 1000 - ss, frame)
    top = math.ceil(hi / frame - 1e-6) * frame  # a grid point at or above the span
    if media.duration_ms:
        top = min(top, math.floor(media.duration_ms / frame + 1e-6) * frame)  # no frame exists past the end
    return SourceWindow(ss, (top + frame) / 1000 - ss, frame, trim_end=(top - frame / 2) / 1000 - ss, last=top - frame)


def _input_args(clip: Clip, media: MediaRef, t0: float, t1: float, out: Output, hwdec: bool) -> tuple[list[str], float]:
    """-ss/-t/-i for the source span shown between t0 and t1; returns (args, seconds of output)."""
    dur_s = (t1 - t0) / 1000
    path = media.proxy if out.use_proxies and media.proxy else media.path
    if media.kind == "image":
        return ["-loop", "1", "-framerate", out.fps_expr, "-t", _num(dur_s + 0.5), "-i", path], dur_s
    w = source_window(clip, media, t0, t1, out.fps)
    args: list[str] = []
    if hwdec and not out.use_proxies:
        args += ["-hwaccel", "auto"]
    args += ["-ss", f"{w.ss:.6f}", "-t", f"{w.length:.6f}", "-i", path]
    return args, dur_s


def time_map(clip: Clip, w: SourceWindow, t0: float) -> str:
    """setpts expression: output seconds from the piece start as a function of T, the input seconds (absolute from ss
    forward; from the first frame shown when reversed). Piecewise linear on the clip's constant-speed steps (one step at
    a constant speed), the same steps the sound and the timing helpers use."""
    if clip.reverse:
        def src(T: float) -> float:
            return w.last + w.frame - 1000 * T  # a reversed frame is reached at its end
        top = (w.last + w.frame) / 1000 - w.ss + 1
    else:
        def src(T: float) -> float:
            return (w.ss + T) * 1000
        top = w.length + 1
    marks = {0.0, top}
    for e in {x for seg in clip.segments() for x in seg[:2]}:
        T = (w.last + w.frame - e) / 1000 if clip.reverse else e / 1000 - w.ss
        if 0 < T < top:
            marks.add(T)
    return piecewise([(T, (clip.timeline_at(src(T)) - t0) / 1000) for T in marks], "T", digits=7)


def time_chain(clip: Clip, media: MediaRef, t0: float, t1: float, out: Output) -> list[str]:
    """Source frames -> the piece's output frames, frame-exact: each output frame (at piece time n / fps) shows the last
    source frame the timeline reaches before n / fps + half a frame. The timestamps are mapped to timeline time (in a
    microsecond time base), ``fps`` keeps for every slot the last frame that rounds to it and repeats the previous one
    for empty slots, then the slots of the piece are kept."""
    n = max(1, int(round((t1 - t0) * out.fps / 1000)))
    if media.kind == "image":
        return ["setpts=PTS-STARTPTS", f"fps={out.fps_expr}", f"trim=duration={_num((t1 - t0) / 1000)}", "setpts=PTS-STARTPTS"]
    w = source_window(clip, media, t0, t1, out.fps)
    chain: list[str] = []
    if clip.reverse:
        chain += [f"trim=end={w.trim_end:.6f}", "reverse", "setpts=PTS-STARTPTS"]
    chain += ["settb=AVTB", f"setpts='({time_map(clip, w, t0)})/TB'", f"fps={out.fps_expr}",
              "trim=start=0", f"trim=end_frame={n}", "setpts=PTS-STARTPTS"]
    return chain


def ramp_expr(clip: Clip, t0: float, t1: float, fps: float, media: Optional[MediaRef] = None) -> str:
    """The time map of a piece (kept for callers that only want the expression)."""
    m = media or MediaRef("", "", "video", 0, 0, False, True, 0)
    return time_map(clip, source_window(clip, m, t0, t1, fps), t0)


def _num6(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def ramp_audio(clip: Clip, label: str, rate: int = 48000) -> list[str]:
    """[label] (the clip's whole source span, reversed if the clip is) -> [label c]: each constant-speed step through its
    own pitch-keeping atempo chain, padded / cut to the exact sample count the timeline gives it, then joined."""
    origin = clip.src_out if clip.reverse else clip.src_in
    acc = 0.0
    parts: list[tuple[float, float, float, int]] = []
    prev_samples = 0
    for s0, s1, v in clip.steps():
        acc += abs(s1 - s0) / v
        samples = int(round(acc * rate / 1000))
        if samples > prev_samples:
            parts.append((abs(s0 - origin) / 1000, abs(s1 - origin) / 1000, v, samples - prev_samples))
        prev_samples = samples
    if not parts:
        return [f"[{label}]anull[{label}c]"]
    lines = [f"[{label}]asplit={len(parts)}" + "".join(f"[{label}_{i}]" for i in range(len(parts))) if len(parts) > 1 else f"[{label}]anull[{label}_0]"]
    for i, (u0, u1, v, count) in enumerate(parts):
        tempo = ",".join(fx.atempo_chain(v)) or "anull"
        lines.append(f"[{label}_{i}]atrim=start={_num6(u0)}:end={_num6(u1)},asetpts=PTS-STARTPTS,{tempo},apad,atrim=end_sample={count},"
                     f"asetpts=PTS-STARTPTS[{label}s{i}]")
    joined = "".join(f"[{label}s{i}]" for i in range(len(parts)))
    lines.append(f"{joined}concat=n={len(parts)}:v=0:a=1[{label}c]" if len(parts) > 1 else f"{joined}anull[{label}c]")
    return lines


def _mask_value(clip: Clip, attr: str, shift: float) -> str:
    keys = clip.keyframes.get("mask_" + attr)
    if keys:
        return f"({keyframe_expr(keys, shift, var='T')})"
    return _num(getattr(clip.mask, attr))


def mask_alpha_expr(clip: Clip, shift: float) -> str:
    """0..1 coverage of the clip's mask at pixel (X, Y) of a W x H picture, from a signed distance to the shape (negative
    inside) and a linear soft edge of 'feather' width centred on the outline. Animated values are functions of T."""
    m: Mask = clip.mask  # type: ignore[assignment]
    x, y, w, h, feather = (_mask_value(clip, a, shift) for a in ("x", "y", "w", "h", "feather"))
    setup = f"st(0,{x}*W);st(1,{y}*H);st(2,max(0.5,{w}*W/2));st(3,max(0.5,{h}*H/2));st(4,max(1,{feather}*min(W,H)));"
    if m.shape == "ellipse":
        dist = "(hypot((X-ld(0))/ld(2),(Y-ld(1))/ld(3))-1)*min(ld(2),ld(3))"
    else:
        r = f"{_num(m.radius)}*2*min(ld(2),ld(3))" if m.shape == "rounded" else "0"
        setup += f"st(5,{r});st(6,abs(X-ld(0))-ld(2)+ld(5));st(7,abs(Y-ld(1))-ld(3)+ld(5));"
        dist = "(hypot(max(ld(6),0),max(ld(7),0))+min(max(ld(6),ld(7)),0)-ld(5))"
    cover = f"clip(0.5-{dist}/ld(4),0,1)"
    return setup + (f"1-{cover}" if m.invert else cover)


def mask_graph(clip: Clip, w: int, h: int, shift: float, dur_s: float, out: Output, p: str) -> list[str]:
    """[p0] -> [p1]: the picture with its alpha multiplied by the mask. A still mask is drawn once and repeated; an
    animated one (mask keyframes) is drawn per frame, on one grey plane only."""
    animated = any(clip.keyframes.get(k) for k in MASK_PROPS)
    expr = mask_alpha_expr(clip, shift)
    source = f"color=c=white:s={w}x{h}:r={out.fps_expr}:d={_num(dur_s + 1 if animated else 0.1)},format=gray,geq=lum='255*({expr})'"
    if not animated:
        source += ",loop=loop=-1:size=1:start=0"
    return [source + f"[{p}m]",
            f"[{p}0]split[{p}a][{p}b]",
            f"[{p}b]alphaextract[{p}ab]",
            f"[{p}ab][{p}m]blend=all_mode=multiply:shortest=1[{p}am]",
            f"[{p}a][{p}am]alphamerge[{p}1]"]


def _fit_chain(clip: Clip, media: MediaRef, out: Output, box_scale: float) -> tuple[list[str], int, int, Optional[tuple[int, int]]]:
    """Crop + scale for the fit mode; returns (filters, width, height, cover size or None). With cover the chain ends
    in a crop whose x / y are the placeholders {CX} / {CY}."""
    W, H = out.width, out.height
    cr = clip.crop
    iw = max(2.0, media.width * (1 - cr.left - cr.right)) if media.width else W
    ih = max(2.0, media.height * (1 - cr.top - cr.bottom)) if media.height else H
    chain: list[str] = []
    if cr.left or cr.right or cr.top or cr.bottom:
        chain.append(f"crop=iw*{_num(1 - cr.left - cr.right)}:ih*{_num(1 - cr.top - cr.bottom)}:iw*{_num(cr.left)}:ih*{_num(cr.top)}")
    bw = max(2, int(round(W * box_scale / 2)) * 2)
    bh = max(2, int(round(H * box_scale / 2)) * 2)
    fit = clip.transform.fit
    if fit == "fill":
        chain.append(f"scale={bw}:{bh}")
        return chain, bw, bh, None
    if fit == "none":
        w = max(2, int(round(iw * box_scale * out.factor / 2)) * 2)
        h = max(2, int(round(ih * box_scale * out.factor / 2)) * 2)
        chain.append(f"scale={w}:{h}")
        return chain, w, h, None
    ratio = iw / ih
    if fit == "contain":
        if bw / bh > ratio:
            h = bh
            w = max(2, int(round(bh * ratio / 2)) * 2)
        else:
            w = bw
            h = max(2, int(round(bw / ratio / 2)) * 2)
        chain.append(f"scale={w}:{h}")
        return chain, w, h, None
    if bw / bh > ratio:
        sw = bw
        sh = max(bh, int(math.ceil(bw / ratio / 2)) * 2)
    else:
        sh = bh
        sw = max(bw, int(math.ceil(bh * ratio / 2)) * 2)
    chain.append(f"scale={sw}:{sh}")
    chain.append(f"crop={bw}:{bh}:{{CX}}:{{CY}}")
    return chain, bw, bh, (sw, sh)


def _focus_exprs(clip: Clip, t0: float, sw: int, sh: int, bw: int, bh: int) -> tuple[str, str]:
    """crop x / y for a cover clip: the focus point, or the reframe path, as functions of the piece-local time t."""
    max_x, max_y = max(0, sw - bw), max(0, sh - bh)
    tr = clip.transform
    if clip.reframe and clip.reframe.path and max_x + max_y > 0:
        src0 = clip.src_at(t0)
        pts_x: list[tuple[float, float]] = []
        pts_y: list[tuple[float, float]] = []
        for p in clip.reframe.path:
            if len(p) < 2:
                continue
            src, fx_ = p[0], p[1]
            fy_ = p[2] if len(p) > 2 else tr.focus_y
            if clip.has_ramp:
                local = (clip.timeline_at(src) - t0) / 1000
            else:
                local = ((src0 - src) if clip.reverse else (src - src0)) / clip.speed / 1000
            pts_x.append((local, fx_))
            pts_y.append((local, fy_))
        pts_x = _window(pts_x)
        pts_y = _window(pts_y)
        fx_expr = piecewise(pts_x) if pts_x else _num(tr.focus_x)
        fy_expr = piecewise(pts_y) if pts_y else _num(tr.focus_y)
        x = f"clip(({fx_expr})*{sw}-{bw / 2},0,{max_x})" if max_x else "0"
        y = f"clip(({fy_expr})*{sh}-{bh / 2},0,{max_y})" if max_y else "0"
        return x, y
    x = _num(min(max_x, max(0.0, tr.focus_x * sw - bw / 2)))
    y = _num(min(max_y, max(0.0, tr.focus_y * sh - bh / 2)))
    return x, y


def _window(points: list[tuple[float, float]], limit: int = 48) -> list[tuple[float, float]]:
    """Keep the points around the piece (t in [-1, piece length + 1]) and thin them to at most ``limit``."""
    pts = sorted(points)
    if not pts:
        return []
    inside = [p for p in pts if p[0] >= 0]
    before = [p for p in pts if p[0] < 0]
    keep = ([before[-1]] if before else []) + inside
    if len(keep) > limit:
        step = len(keep) / limit
        keep = [keep[int(i * step)] for i in range(limit)] + [keep[-1]]
    return keep


def eq_key_exprs(clip: Clip, shift: float) -> dict[str, str]:
    """Per-frame expressions of the clip's brightness / saturation keyframes (``t`` is the piece's time in seconds; ``shift`` is
    the clip-local time at its start, as for the other keyed props), clamped to the eq range. Empty when the clip has no such keys.

    Where they apply: the keys animate the clip's first enabled eq effect (its static contrast, gamma and any prop without keys
    stay as set); a clip with keys and no eq effect at all gets a default one. If every eq on the clip is disabled the keys do
    nothing, so switching the effect off really switches the grade off."""
    eqs = [f for f in clip.filters if f.type == "eq"]
    if eqs and not any(f.enabled for f in eqs):
        return {}
    out: dict[str, str] = {}
    for prop, (lo, hi) in EQ_KEY_PROPS.items():
        keys = clip.keyframes.get(prop)
        if keys:
            out[prop] = f"clip({keyframe_expr(keys, shift, var='t')},{_num(lo)},{_num(hi)})"
    return out


@dataclass
class ChainCtx:
    out: Output
    lut_name: Callable[[str], str]
    stab_trf: Callable[[Clip], Optional[str]]
    hwdec: bool = False


def clip_chain(idx: int, piece_clip: Clip, media: MediaRef, t0: float, t1: float, cx: ChainCtx, label: str, *,
               fades: bool = True) -> tuple[list[str], str, int, int]:
    """Input args and the filter chain [idx:v] -> [label] for clip between timeline t0..t1 (piece-local time from 0).
    Returns (input_args, chain_text, width, height)."""
    out = cx.out
    clip = piece_clip
    in_args, dur_s = _input_args(clip, media, t0, t1, out, cx.hwdec)
    chain: list[str] = time_chain(clip, media, t0, t1, out)
    scale_keys = clip.keyframes.get("scale")
    box_scale = clip.transform.scale if not scale_keys else 1.0
    blur_fill = clip.transform.fit == "blur"
    head = ""
    if blur_fill:
        # the whole frame, fitted, over a blurred and darkened copy that fills the box (vertical from horizontal, and the reverse)
        bg_clip = clip.model_copy(deep=True)
        bg_clip.transform.fit = "cover"
        bg_clip.reframe = None
        bchain, bw, bh, bcover = _fit_chain(bg_clip, media, out, box_scale)
        bx, by = _focus_exprs(bg_clip, t0, bcover[0], bcover[1], bw, bh) if bcover else ("0", "0")
        bchain = [f.replace("{CX}", f"'{bx}'").replace("{CY}", f"'{by}'") for f in bchain]
        fg_clip = clip.model_copy(deep=True)
        fg_clip.transform.fit = "contain"
        fchain, fw, fh, _ = _fit_chain(fg_clip, media, out, box_scale)
        sigma = max(8, int(min(bw, bh) / 30))
        head = (f"[{idx}:v]" + ",".join(chain) + f",split[{label}a][{label}b];"
                f"[{label}a]" + ",".join(bchain) + f",gblur=sigma={sigma},eq=brightness=-0.06:saturation=0.9[{label}bg];"
                f"[{label}b]" + ",".join(fchain) + f"[{label}fg];"
                f"[{label}bg][{label}fg]overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2")
        chain = []
        w, h = bw, bh
    else:
        fchain, w, h, cover = _fit_chain(clip, media, out, box_scale)
        if cover:
            x, y = _focus_exprs(clip, t0, cover[0], cover[1], w, h)
            fchain = [f.replace("{CX}", f"'{x}'").replace("{CY}", f"'{y}'") for f in fchain]
        chain += fchain
    shift = (t0 - clip.start) / 1000  # clip-local time at the start of the piece
    if scale_keys:
        z = keyframe_expr(scale_keys, shift, var="(on/" + _num(out.fps) + ")")
        chain.append(f"zoompan=z='max(1,{z})':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps={out.fps_expr}")
    others = [f for f in clip.filters if f.enabled and f.type not in fx.AUDIO]
    eq_keys = eq_key_exprs(clip, shift)
    if others or eq_keys:
        chain += fx.video_chain(others, {"lut_name": cx.lut_name, "uid": label, "factor": out.factor, "eq_keys": eq_keys})
    chain.append("format=yuva420p")
    if clip.mask and clip.mask.enabled:
        # the mask multiplies the clip's alpha: close the chain here, branch, and carry on from the masked picture
        lines = mask_graph(clip, w, h, shift, dur_s, out, f"{label}k")
        body = (head + ("," + ",".join(chain) if chain else "")) if head else f"[{idx}:v]" + ",".join(chain)
        head = body + f"[{label}k0];" + ";".join(lines) + f";[{label}k1]null"
        chain = []
    rot_keys = clip.keyframes.get("rotation")
    if rot_keys or abs(clip.transform.rotation) > 1e-3:
        angle = f"({keyframe_expr(rot_keys, shift)})*PI/180" if rot_keys else _num(clip.transform.rotation * math.pi / 180)
        if rot_keys:
            side = int(math.ceil(math.hypot(w, h) / 2)) * 2
            chain.append(f"rotate=a='{angle}':ow={side}:oh={side}:c=none")
            w = h = side
        else:
            a = clip.transform.rotation * math.pi / 180
            rw = int(math.ceil((abs(w * math.cos(a)) + abs(h * math.sin(a))) / 2)) * 2
            rh = int(math.ceil((abs(w * math.sin(a)) + abs(h * math.cos(a))) / 2)) * 2
            chain.append(f"rotate=a={angle}:ow={rw}:oh={rh}:c=none")
            w, h = rw, rh
    op_keys = clip.keyframes.get("opacity")
    if op_keys:
        expr = keyframe_expr(op_keys, shift, var="T")
        chain.append(f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*clip({expr},0,1)'")
    elif clip.transform.opacity < 0.999:
        chain.append(f"colorchannelmixer=aa={_num(clip.transform.opacity)}")
    if fades:
        piece_len = (t1 - t0) / 1000
        if clip.fade_in and not clip.transition_in and abs(t0 - clip.start) < 1:
            chain.append(f"fade=t=in:st=0:d={_num(min(clip.fade_in / 1000, piece_len))}:alpha=1")
        elif clip.fade_in and not clip.transition_in and 0 < t0 - clip.start < clip.fade_in:
            # a piece that starts inside the fade (a single frame of the timeline): the fade filter cannot start before its input
            chain.append(_alpha_ramp(f"(T+{_num((t0 - clip.start) / 1000)})/{_num(clip.fade_in / 1000)}"))
        if clip.fade_out and abs(t1 - clip.end) < 1:
            d = min(clip.fade_out / 1000, piece_len)
            chain.append(f"fade=t=out:st={_num(piece_len - d)}:d={_num(d)}:alpha=1")
        elif clip.fade_out and t1 < clip.end - 1 and t1 > clip.end - clip.fade_out:
            chain.append(_alpha_ramp(f"({_num((clip.end - t0) / 1000)}-T)/{_num(clip.fade_out / 1000)}"))
    if head:
        text = head + ("," + ",".join(chain) if chain else "") + f"[{label}]"
    else:
        text = f"[{idx}:v]" + ",".join(chain) + f"[{label}]"
    return in_args, text, w, h


def _alpha_ramp(expr: str) -> str:
    """Multiply the picture's opacity by clip(expr, 0, 1); T is the frame's time in seconds."""
    return f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='p(X,Y)*clip({expr},0,1)'"


def _position(clip: Clip, out: Output, A: float) -> tuple[str, str]:
    """overlay x / y: centred plus the transform offset (animated when keyframed). t is chunk-local."""
    W, H = out.width, out.height
    base_shift = (A - clip.start) / 1000  # clip-local time at chunk t=0
    kx, ky = clip.keyframes.get("x"), clip.keyframes.get("y")
    x = f"(main_w-overlay_w)/2+({keyframe_expr(kx, base_shift)})*{W}" if kx else f"(main_w-overlay_w)/2+{_num(clip.transform.x * W)}"
    y = f"(main_h-overlay_h)/2+({keyframe_expr(ky, base_shift)})*{H}" if ky else f"(main_h-overlay_h)/2+{_num(clip.transform.y * H)}"
    return x, y


def chunk_graph(project: Project, index: int, f0: int, f1: int, out: Output, media: Callable[[str], MediaRef], cx: ChainCtx,
                ass_file: Optional[str]) -> ChunkGraph:
    """The ffmpeg inputs and filter script for frames [f0, f1) of the timeline."""
    A, B = ms_of_frame(f0, out.fps), ms_of_frame(f1, out.fps)
    dur = (B - A) / 1000
    g = ChunkGraph(index, f0, f1)
    base = "black@0" if out.alpha else project.canvas.background.replace('#', '0x')
    lines = [f"color=c={base}:s={out.width}x{out.height}:r={out.fps_expr}:d={_num(dur + 1)},format={'yuva420p' if out.alpha else 'yuv420p'}[b0]"]
    cur = "b0"
    n = 0
    for p in pieces_in(project, A, B, out.fps):
        m = media(p.clip.media)
        # where the piece starts in the chunk, as a whole number of frames (a rounded decimal could land after the frame)
        at = f"{int(round((p.t0 - A) * out.fps / 1000))}/({out.fps_expr})"
        if p.other is None:
            args, text, w, h = clip_chain(len(g.inputs), p.clip, m, p.t0, p.t1, cx, f"c{n}")
            g.inputs.append(args)
            lines.append(text)
            x, y = _position(p.clip, out, A)
            lines.append(f"[c{n}]setpts=PTS-STARTPTS+({at})/TB[d{n}]")
            lines.append(f"[{cur}][d{n}]overlay=x='{x}':y='{y}':eof_action=pass:repeatlast=0:format=auto[b{n + 1}]")
        else:
            # transition: both clips on their own transparent canvas, xfade, then onto the picture
            sides = []
            for k, side in enumerate((p.clip, p.other)):
                sm = media(side.media)
                args, text, w, h = clip_chain(len(g.inputs), side, sm, p.t0, p.t1, cx, f"c{n}s{k}", fades=False)
                g.inputs.append(args)
                lines.append(text)
                x, y = _position(side, out, p.t0)
                lines.append(f"color=c=black@0:s={out.width}x{out.height}:r={out.fps_expr}:d={_num((p.t1 - p.t0) / 1000 + 0.5)},format=yuva420p[k{n}s{k}]")
                lines.append(f"[k{n}s{k}][c{n}s{k}]overlay=x='{x}':y='{y}':eof_action=pass:repeatlast=0:format=auto,format=yuva420p[e{n}s{k}]")
                sides.append(f"e{n}s{k}")
            tdur = (p.t1 - p.t0) / 1000
            # A piece is the whole transition in an export; a single frame of the timeline starts part-way in: a negative offset
            # starts the filter at that progress (its duration is always the transition's own).
            whole = p.other.transition_in.dur / 1000 if p.other.transition_in else tdur
            into = (p.t0 - p.other.start) / 1000
            lines.append(f"[{sides[0]}][{sides[1]}]xfade=transition={XFADE.get(p.transition or 'crossfade', 'fade')}:duration={_num(max(0.04, whole - 0.001))}:offset={_num(-into)},"
                         f"trim=duration={_num(tdur)},setpts=PTS-STARTPTS+({at})/TB[d{n}]")
            lines.append(f"[{cur}][d{n}]overlay=eof_action=pass:repeatlast=0:format=auto[b{n + 1}]")
        cur = f"b{n + 1}"
        n += 1
    tail = [f"trim=duration={_num(dur)}"]
    if ass_file:
        tail += [f"setpts=PTS-STARTPTS+{_num(A / 1000)}/TB", f"ass={ass_file}", "setpts=PTS-STARTPTS"]
    tail.append("format=yuva420p" if out.alpha else "format=yuv420p")
    lines.append(f"[{cur}]" + ",".join(tail) + f"[{g.out_label}]")
    g.graph = ";\n".join(lines) + "\n"
    return g


# ---------------------------------------------------------------- audio

@dataclass
class AudioGraph:
    graph: str
    clips: int
    sources: list[tuple[str, int]]  # (media id, stream) masters the graph reads


def _lanes(clips: list[Clip]) -> list[list[Clip]]:
    lanes: list[list[Clip]] = []
    for c in sorted(clips, key=lambda c: c.start):
        for lane in lanes:
            if lane[-1].end <= c.start + 1:
                lane.append(c)
                break
        else:
            lanes.append([c])
    return lanes


def _transition_fades(track: Track) -> dict[str, tuple[int, int]]:
    """Audio crossfades at transitions: clip id -> (extra fade-in ms, extra fade-out ms)."""
    out: dict[str, tuple[int, int]] = {}
    clips = sorted(track.clips, key=lambda c: c.start)
    for i, c in enumerate(clips[1:], start=1):
        if c.transition_in and clips[i - 1].end > c.start:
            d = clips[i - 1].end - c.start
            a, b = out.get(c.id, (0, 0)), out.get(clips[i - 1].id, (0, 0))
            out[c.id] = (max(a[0], d), a[1])
            out[clips[i - 1].id] = (b[0], max(b[1], d))
    return out


def audio_graph(project: Project, total_ms: int, media: Callable[[str], MediaRef], master_name: Callable[[str, int], str]) -> AudioGraph:
    """Filter script producing [aout] (48 kHz stereo, exactly total_ms long). master_name(media, stream) is the FLAC
    path relative to the folder ffmpeg runs in."""
    lines: list[str] = []
    track_labels: list[tuple[Track, str]] = []
    sources: set[tuple[str, int]] = set()
    total_s = total_ms / 1000
    n = 0
    count = 0
    fmt = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    for t in project.tracks:
        if t.muted or t.kind == "text":
            continue
        fades = _transition_fades(t)
        clips = []
        for c in t.clips:
            if c.type == "text" or c.mute or c.start >= total_ms:
                continue
            m = media(c.media)
            if not m.has_audio or m.kind == "image":
                continue
            clips.append(c)
        if not clips:
            continue
        lane_labels = []
        for li, lane in enumerate(_lanes(clips)):
            parts: list[str] = []
            cursor = 0
            for c in lane:
                gap = c.start - cursor
                if gap > 0:
                    lines.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={_num(gap / 1000)}[g{n}]")
                    parts.append(f"g{n}")
                    n += 1
                lo, hi = (c.src_in, c.src_out)
                sources.add((c.media, c.audio_stream))
                src = master_name(c.media, c.audio_stream)
                chain = [f"amovie={src}:seek_point={_num(lo / 1000)}", f"atrim=start={_num(lo / 1000)}:end={_num(hi / 1000)}", "asetpts=PTS-STARTPTS"]
                if c.reverse:
                    chain.append("areverse")
                if c.has_ramp:
                    # one atempo per constant-speed step, each cut to its exact length: the sound follows the picture's steps
                    lines.append(",".join(chain + [fmt]) + f"[r{n}]")
                    lines += ramp_audio(c, f"r{n}")
                    chain = [f"[r{n}c]anull"]
                else:
                    chain += fx.atempo_chain(c.speed)
                chain += fx.audio_filters(c.filters)
                vol_keys = c.keyframes.get("volume_db")
                if vol_keys:
                    expr = keyframe_expr(vol_keys, 0.0)
                    chain.append(f"volume=volume='pow(10,({expr})/20)':eval=frame")
                elif abs(c.volume_db) > 0.01:
                    chain.append(f"volume={_num(c.volume_db)}dB")
                length = c.duration / 1000
                extra_in, extra_out = fades.get(c.id, (0, 0))
                fin, fout = max(c.audio_fade_in, extra_in), max(c.audio_fade_out, extra_out)
                if fin:
                    chain.append(f"afade=t=in:st=0:d={_num(min(fin / 1000, length))}:curve=qsin")
                if fout:
                    d = min(fout / 1000, length)
                    chain.append(f"afade=t=out:st={_num(length - d)}:d={_num(d)}:curve=qsin")
                chain += [fmt, "apad", f"atrim=duration={_num(length)}", "asetpts=PTS-STARTPTS"]
                lines.append(",".join(chain) + f"[s{n}]")
                parts.append(f"s{n}")
                n += 1
                count += 1
                cursor = c.end
            label = f"l{len(track_labels)}_{li}"
            if len(parts) == 1:
                lines.append(f"[{parts[0]}]anull[{label}]")
            else:
                lines.append("".join(f"[{p}]" for p in parts) + f"concat=n={len(parts)}:v=0:a=1[{label}]")
            lane_labels.append(label)
        tl = f"t{len(track_labels)}"
        mix = "".join(f"[{x}]" for x in lane_labels)
        if len(lane_labels) > 1:
            lines.append(f"{mix}amix=inputs={len(lane_labels)}:normalize=0:dropout_transition=0,{fmt},volume={_num(t.volume_db)}dB[{tl}]")
        else:
            lines.append(f"{mix}{fmt},volume={_num(t.volume_db)}dB[{tl}]")
        track_labels.append((t, tl))
    if not track_labels:
        lines.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={_num(total_s)}[aout]")
        return AudioGraph(";\n".join(lines) + "\n", 0, [])
    keys = [lbl for t, lbl in track_labels if not t.duck and (t.role in ("main", "voice") or t.kind == "video")]
    ducked = [lbl for t, lbl in track_labels if t.duck]
    final: list[str] = [lbl for t, lbl in track_labels if not t.duck]
    if ducked and keys:
        # the speech bus is the key: split it so it is heard and drives the compressor
        if len(keys) > 1:
            lines.append("".join(f"[{k}]" for k in keys) + f"amix=inputs={len(keys)}:normalize=0:dropout_transition=0[keymix]")
        else:
            lines.append(f"[{keys[0]}]anull[keymix]")
        lines.append(f"[keymix]asplit={len(ducked) + 1}" + "".join(f"[key{i}]" for i in range(len(ducked) + 1)))
        final = [lbl for t, lbl in track_labels if not t.duck and lbl not in keys] + ["key0"]
        for i, lbl in enumerate(ducked, start=1):
            lines.append(f"[{lbl}][key{i}]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=450:makeup=1:level_sc=1[dk{i}]")
            final.append(f"dk{i}")
    else:
        final = [lbl for _, lbl in track_labels]
    joined = "".join(f"[{x}]" for x in final)
    pre = f"{joined}amix=inputs={len(final)}:normalize=0:dropout_transition=0," if len(final) > 1 else f"{joined}"
    lines.append(f"{pre}{fmt},alimiter=limit=0.97:level=0,apad,atrim=duration={_num(total_s)},asetpts=PTS-STARTPTS[aout]")
    return AudioGraph(";\n".join(lines) + "\n", count, sorted(sources))
