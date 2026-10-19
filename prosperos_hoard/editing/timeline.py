"""The project document: canvas, tracks, clips, markers and captions. Times are integer milliseconds.

A media clip shows ``[src_in, src_out)`` of its media from ``start`` on the timeline; its length on the timeline is
``(src_out - src_in) / speed``, or, with a speed curve (``speed_keys``), the integral of ``1 / speed`` over the source
span. A media clip on a video track carries its own audio (muted with ``mute``); audio tracks hold sound only. A
sequence clip is a media clip whose ``media`` is another project (``prj_...``): ``src_in`` / ``src_out`` are times of
that project's timeline. Text clips have ``start`` and ``length``. Clips on one track never overlap, except a clip
with ``transition_in`` which overlaps the previous one by at most the transition length.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .errors import LumiereError, NotFound
from .util import new_id

SCHEMA = "lumiere/1"
MIN_CLIP_MS = 40
MAX_TRACKS = 24
MAX_CLIPS = 4000

TrackKind = Literal["video", "audio", "text"]
FitMode = Literal["contain", "cover", "fill", "none", "blur"]
TransitionType = Literal["crossfade", "dissolve", "fade_black", "fade_white", "slide_left", "slide_right", "slide_up", "slide_down",
                         "wipe_left", "wipe_right", "wipe_up", "wipe_down", "circle_open", "circle_close", "zoom_in", "pixelize",
                         "radial", "smooth_left", "smooth_right", "blur"]
FilterType = Literal["eq", "lut", "grayscale", "sepia", "vignette", "blur", "sharpen", "denoise", "hflip", "vflip",
                     "chromakey", "vintage", "warm", "cool", "contrast_pop", "pixelate",
                     "audio_denoise", "voice_enhance", "highpass", "lowpass", "compressor", "pitch", "echo"]
CaptionStyle = Literal["clean", "bold", "karaoke", "pop", "boxed", "minimal"]
Ease = Literal["linear", "hold", "ease_in", "ease_out", "ease_in_out"]
KeyProp = Literal["x", "y", "scale", "opacity", "rotation", "volume_db", "mask_x", "mask_y", "mask_w", "mask_h", "mask_feather",
                  "brightness", "saturation"]
MaskShape = Literal["rectangle", "rounded", "ellipse"]
MASK_PROPS = {"mask_x": "x", "mask_y": "y", "mask_w": "w", "mask_h": "h", "mask_feather": "feather"}
# Colour props animate the clip's eq effect; a key outside the eq range is refused (the same limits as render/filters.py SPECS["eq"]).
EQ_KEY_PROPS = {"brightness": (-1.0, 1.0), "saturation": (0.0, 3.0)}
RAMP_STEP_MS = 100  # source ms per constant-speed step while the speed changes (picture and sound share the steps)
RAMP_MAX_STEPS = 24  # steps between two keys at most
MAX_SPEED_KEYS = 64

TRANSITIONS = TransitionType.__args__  # type: ignore[attr-defined]
FILTERS = FilterType.__args__  # type: ignore[attr-defined]
AUDIO_FILTERS = {"audio_denoise", "voice_enhance", "highpass", "lowpass", "compressor", "pitch", "echo"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=False)


class Canvas(Strict):
    width: int = Field(1920, ge=64, le=7680)
    height: int = Field(1080, ge=64, le=7680)
    fps: float = Field(30.0, ge=1, le=240)
    background: str = Field("#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    sample_rate: int = Field(48000, ge=8000, le=192000)

    @field_validator("width", "height")
    @classmethod
    def _even(cls, v: int) -> int:
        return v - (v % 2)


class Transform(Strict):
    x: float = Field(0.0, ge=-2, le=2, description="Horizontal offset as a fraction of the canvas width (0 = centred).")
    y: float = Field(0.0, ge=-2, le=2)
    scale: float = Field(1.0, gt=0, le=20)
    rotation: float = Field(0.0, ge=-360, le=360)
    opacity: float = Field(1.0, ge=0, le=1)
    fit: FitMode = "contain"
    focus_x: float = Field(0.5, ge=0, le=1, description="With fit=cover: which part of the frame stays visible (0 left, 1 right).")
    focus_y: float = Field(0.5, ge=0, le=1)


class Crop(Strict):
    left: float = Field(0.0, ge=0, le=0.45)
    top: float = Field(0.0, ge=0, le=0.45)
    right: float = Field(0.0, ge=0, le=0.45)
    bottom: float = Field(0.0, ge=0, le=0.45)


class Filter(Strict):
    type: FilterType
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class Transition(Strict):
    type: TransitionType = "crossfade"
    dur: int = Field(500, ge=40, le=5000)


class Keyframe(Strict):
    t: int = Field(..., ge=0, description="ms from the clip's start on the timeline.")
    v: float
    ease: Ease = "linear"
    # When a split cuts inside an eased span, the visible keys keep their local times/values
    # but evaluation continues the original ease over [ease_v0→ease_v1] of length ease_span,
    # with this key sitting ease_into ms into that domain (see keyframe_value / slice_keyframes).
    ease_span: Optional[int] = Field(None, ge=1, description="Original eased segment length (ms).")
    ease_into: Optional[int] = Field(None, ge=0, description="Ms from the original segment start to this key.")
    ease_v0: Optional[float] = Field(None, description="Value at the original segment start.")
    ease_v1: Optional[float] = Field(None, description="Value at the original segment end.")


def eq_key_error(prop: str, keys: list[Keyframe]) -> Optional[str]:
    """Why the keys of a colour prop are refused (a value outside the eq range), or None."""
    if prop not in EQ_KEY_PROPS:
        return None
    lo, hi = EQ_KEY_PROPS[prop]
    bad = next((k for k in keys if not lo <= k.v <= hi), None)
    return None if bad is None else f"Keyframe {prop} must be between {lo:g} and {hi:g}, not {bad.v:g} (at {bad.t} ms)."


class SpeedKey(Strict):
    t: int = Field(..., ge=0, description="Source time (ms of the media) where the clip plays at speed v.")
    v: float = Field(..., ge=0.1, le=16, description="Speed at that point (1 = normal, 0.25 = slow motion, 4 = fast).")
    ease: Ease = Field("linear", description="How the speed goes from this key to the next.")


class Mask(Strict):
    """A shape that keeps (or, inverted, removes) part of a clip's picture. Coordinates are fractions of the clip's own
    picture (after fit and crop, before position / rotation), so the mask travels with a picture-in-picture."""

    shape: MaskShape = "ellipse"
    x: float = Field(0.5, ge=-1, le=2, description="Centre, as a fraction of the clip's picture width (0.5 = middle).")
    y: float = Field(0.5, ge=-1, le=2)
    w: float = Field(0.8, gt=0, le=4, description="Width as a fraction of the clip's picture width.")
    h: float = Field(0.8, gt=0, le=4)
    radius: float = Field(0.2, ge=0, le=0.5, description="rounded: corner radius as a fraction of the shape's smaller side.")
    feather: float = Field(0.0, ge=0, le=0.5, description="Soft edge width as a fraction of the picture's smaller side.")
    invert: bool = Field(False, description="Keep the outside instead of the inside.")
    enabled: bool = True


class Reframe(Strict):
    """A focus path computed by the auto-reframe tool: [[src_ms, focus_x, focus_y], ...]."""

    path: list[list[float]] = Field(default_factory=list, max_length=20000)
    mode: Literal["track", "stable", "center"] = "track"


class TextStyle(Strict):
    font: str = Field("Arial", max_length=80)
    size: int = Field(72, ge=8, le=600, description="Pixels at the canvas size.")
    color: str = Field("#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    outline: str = Field("#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    outline_width: float = Field(4, ge=0, le=40)
    box: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$", description="Background box colour (#RRGGBB or #RRGGBBAA).")
    bold: bool = True
    italic: bool = False
    align: Literal["left", "center", "right"] = "center"
    position: Literal["top", "middle", "bottom"] = "middle"
    margin: int = Field(80, ge=0, le=2000)
    animation: Literal["none", "fade", "pop", "slide_up", "typewriter"] = "fade"
    shadow: float = Field(2, ge=0, le=20)


class Clip(Strict):
    id: str = Field(default_factory=lambda: new_id("clp"))
    type: Literal["media", "text", "sequence"] = "media"
    start: int = Field(0, ge=0)
    # media and sequence clips (a sequence's media is a project id)
    media: Optional[str] = None
    src_in: int = Field(0, ge=0)
    src_out: int = Field(0, ge=0)
    speed: float = Field(1.0, ge=0.1, le=16)
    reverse: bool = False
    speed_keys: list[SpeedKey] = Field(default_factory=list, max_length=MAX_SPEED_KEYS,
                                       description="Speed curve over source time; when set it replaces 'speed'.")
    audio_stream: int = Field(0, ge=0, le=16)
    # text clips
    length: int = Field(0, ge=0, description="Length of a text clip (ms).")
    text: str = Field("", max_length=4000)
    style: Optional[TextStyle] = None
    # shared
    label: str = Field("", max_length=120)
    mute: bool = False
    volume_db: float = Field(0.0, ge=-60, le=24)
    fade_in: int = Field(0, ge=0, le=60000, description="Video opacity fade-in (ms).")
    fade_out: int = Field(0, ge=0, le=60000)
    audio_fade_in: int = Field(0, ge=0, le=60000)
    audio_fade_out: int = Field(0, ge=0, le=60000)
    transform: Transform = Field(default_factory=Transform)
    crop: Crop = Field(default_factory=Crop)
    filters: list[Filter] = Field(default_factory=list, max_length=24)
    transition_in: Optional[Transition] = None
    keyframes: dict[KeyProp, list[Keyframe]] = Field(default_factory=dict)
    reframe: Optional[Reframe] = None
    mask: Optional[Mask] = None
    color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    slot: Optional[str] = Field(None, min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$",
                                description="Template slot name (intro, main, outro...): the clip a project made from this template fills with other media.")
    multicam: Optional[str] = Field(None, description="Id of the multicam group this clip belongs to (its picture is an angle, or it is the group's master sound).")
    _seg_cache: Any = PrivateAttr(default=None)

    @property
    def is_sequence(self) -> bool:
        return self.type == "sequence"

    @property
    def has_ramp(self) -> bool:
        return bool(self.speed_keys) and self.type != "text"

    def segments(self) -> list[tuple[float, float, float]]:
        """Constant-speed steps (src_a, src_b, speed) covering [src_in, src_out] in source order. Without a curve this is
        one step at ``speed``. Render (picture and sound), preview and every timing helper use the same steps, so they
        agree to the frame."""
        if not self.has_ramp:
            return [(float(self.src_in), float(self.src_out), self.speed)]
        key = (self.src_in, self.src_out, tuple((k.t, k.v, k.ease) for k in self.speed_keys))
        if self._seg_cache is None or self._seg_cache[0] != key:
            self._seg_cache = (key, ramp_segments(self.src_in, self.src_out, self.speed_keys))
        return self._seg_cache[1]

    def steps(self) -> list[tuple[float, float, float]]:
        """The constant-speed steps in playback order as (source where the step starts, source where it ends, speed)."""
        segs = self.segments()
        return [(b, a, v) for a, b, v in reversed(segs)] if self.reverse else list(segs)

    @property
    def duration(self) -> int:
        if self.type == "text":
            return self.length
        if not self.has_ramp:
            return int(round((self.src_out - self.src_in) / self.speed))
        return int(round(sum((b - a) / v for a, b, v in self.segments())))

    @property
    def end(self) -> int:
        return self.start + self.duration

    def src_at(self, t: float) -> float:
        """Source time (ms) shown at timeline time ``t`` (outside the clip it goes on at the speed of the nearest edge)."""
        if not self.has_ramp:
            offset = (t - self.start) * self.speed
            return (self.src_out - offset) if self.reverse else (self.src_in + offset)
        sign = -1.0 if self.reverse else 1.0
        steps = self.steps()
        local = t - self.start
        if local <= 0:
            return steps[0][0] + sign * local * steps[0][2]
        acc = 0.0
        for s0, s1, v in steps:
            d = abs(s1 - s0) / v
            if local <= acc + d:
                return s0 + sign * (local - acc) * v
            acc += d
        return steps[-1][1] + sign * (local - acc) * steps[-1][2]

    def timeline_at(self, src: float) -> float:
        """Timeline time (ms, unrounded) at which source time ``src`` is shown."""
        if not self.has_ramp:
            return self.start + ((self.src_out - src) if self.reverse else (src - self.src_in)) / self.speed
        sign = -1.0 if self.reverse else 1.0
        steps = self.steps()
        q = sign * (src - steps[0][0])  # source progress along playback order
        if q <= 0:
            return self.start + q / steps[0][2]
        acc_q = acc_t = 0.0
        for s0, s1, v in steps:
            length = abs(s1 - s0)
            if q <= acc_q + length:
                return self.start + acc_t + (q - acc_q) / v
            acc_q += length
            acc_t += length / v
        return self.start + acc_t + (q - acc_q) / steps[-1][2]

    def speed_at(self, t: float) -> float:
        """Playback speed at timeline time ``t``."""
        if not self.has_ramp:
            return self.speed
        return speed_value(self.speed_keys, self.src_at(t))

    @model_validator(mode="after")
    def _check(self) -> "Clip":
        if self.type in ("media", "sequence"):
            if not self.media:
                raise ValueError("A media clip needs a media id." if self.type == "media" else "A sequence clip needs a project id (media).")
            if self.type == "sequence" and not self.media.startswith("prj_"):
                raise ValueError(f"Sequence clip {self.id} must point at a project (prj_...), not {self.media}.")
            if self.has_ramp:
                if self.src_out - self.src_in < 1 or self.duration < MIN_CLIP_MS // 2:
                    raise ValueError(f"Clip {self.id} is too short (src_in {self.src_in}, src_out {self.src_out}).")
            elif self.src_out - self.src_in < MIN_CLIP_MS * self.speed * 0.5:
                raise ValueError(f"Clip {self.id} is too short (src_in {self.src_in}, src_out {self.src_out}).")
        else:
            if self.length < MIN_CLIP_MS:
                raise ValueError(f"Text clip {self.id} needs a length of at least {MIN_CLIP_MS} ms.")
        for prop in EQ_KEY_PROPS:
            problem = eq_key_error(prop, self.keyframes.get(prop, []))
            if problem:
                raise ValueError(problem)
        return self


def ease_value(kind: str, u: float) -> float:
    """0..1 progress for an easing kind (the same curves as keyframes)."""
    if kind == "hold":
        return 0.0
    if kind == "ease_in":
        return u * u
    if kind == "ease_out":
        return 1 - (1 - u) ** 2
    if kind == "ease_in_out":
        return 3 * u * u - 2 * u * u * u
    return u


def _segment_domain(a: Keyframe, b: Keyframe) -> tuple[int, int, float, float]:
    """Full ease domain (span, into_at_a, v0, v1) for the segment from ``a`` to ``b``."""
    if a.ease_span is not None and a.ease_into is not None and a.ease_v0 is not None and a.ease_v1 is not None:
        return int(a.ease_span), int(a.ease_into), float(a.ease_v0), float(a.ease_v1)
    return max(1, b.t - a.t), 0, float(a.v), float(b.v)


def _domain_fields(k: Keyframe) -> Optional[dict[str, Any]]:
    if k.ease_span is None or k.ease_into is None or k.ease_v0 is None or k.ease_v1 is None:
        return None
    return {"ease_span": int(k.ease_span), "ease_into": int(k.ease_into),
            "ease_v0": float(k.ease_v0), "ease_v1": float(k.ease_v1)}


def reconcile_key_domains(prev: Optional[list[Keyframe]], incoming: list[Keyframe]) -> list[Keyframe]:
    """Keep ease domains only on segments whose endpoints (t, v, ease→next) still match ``prev``.

    Editing the last key of a multipoint split curve must leave earlier domains intact so
    mid-span values (e.g. original t=537) stay exact. MCP/UI edits that omit domain fields
    still restore a previous domain when the segment endpoints are unchanged.
    """
    prev_s = sorted(prev or [], key=lambda k: k.t)
    new_s = sorted(incoming, key=lambda k: k.t)
    out: list[Keyframe] = []
    for i, k in enumerate(new_s):
        keep: Optional[dict[str, Any]] = None
        if i + 1 < len(new_s):
            nxt = new_s[i + 1]
            for j, a in enumerate(prev_s):
                if j + 1 >= len(prev_s):
                    break
                b = prev_s[j + 1]
                if (a.t == k.t and a.v == k.v and a.ease == k.ease
                        and b.t == nxt.t and b.v == nxt.v):
                    keep = _domain_fields(a)
                    break
        if keep:
            out.append(Keyframe(t=k.t, v=k.v, ease=k.ease, **keep))
        else:
            out.append(Keyframe(t=k.t, v=k.v, ease=k.ease))
    return out


def keyframe_value(keys: list[Keyframe], local_ms: float) -> float:
    """Animated property value at clip-local time ``local_ms`` (ms), with the key's ease to the next."""
    if not keys:
        return 0.0
    keys = sorted(keys, key=lambda k: k.t)
    if local_ms <= keys[0].t:
        return keys[0].v
    for a, b in zip(keys, keys[1:]):
        if a.t <= local_ms < b.t:
            if a.ease == "hold":
                return a.v
            span, into, v0, v1 = _segment_domain(a, b)
            u = (into + (local_ms - a.t)) / float(span)
            return v0 + (v1 - v0) * ease_value(a.ease, u)
    return keys[-1].v


def slice_keyframes(keyframes: dict[str, list[Keyframe]], lo: int, hi: int) -> dict[str, list[Keyframe]]:
    """Keys for a subclip covering original clip-local [lo, hi]; times rebased to 0.

    Preserves the evaluated curve on both halves for linear/hold/ease_in/out/in_out.
    Hold keeps a hold key. Eased segments keep the original ease with an explicit domain
    (ease_span / ease_into / ease_v0 / ease_v1) so a mid-span cut does not restart the curve
    (e.g. ease_in 0→1 over 1000 ms cut at 500 keeps t=750 → 0.5625 on the right at local 250,
    and arbitrary times like 537 match exactly before and after the cut).
    """
    if not keyframes or hi <= lo:
        return {}
    out: dict[str, list[Keyframe]] = {}
    for prop, keys in keyframes.items():
        if not keys:
            continue
        ordered = sorted(keys, key=lambda k: k.t)
        rebuilt: list[Keyframe] = []

        def push(k: Keyframe) -> None:
            if rebuilt and rebuilt[-1].t == k.t:
                rebuilt[-1] = k
            else:
                rebuilt.append(k)

        if len(ordered) == 1 or lo >= ordered[-1].t or hi <= ordered[0].t:
            # One key, or the window is entirely before/after the animated span:
            # keyframe_value is constant — keep a single local key (never drop the curve).
            push(Keyframe(t=0, v=keyframe_value(ordered, float(lo)), ease="linear"))
            out[prop] = rebuilt
            continue

        for a, b in zip(ordered, ordered[1:]):
            if b.t <= lo or a.t >= hi:
                continue
            seg_lo, seg_hi = max(a.t, lo), min(b.t, hi)
            if seg_hi <= seg_lo:
                continue
            if a.ease == "hold":
                push(Keyframe(t=seg_lo - lo, v=a.v, ease="hold"))
                if seg_hi >= b.t:
                    push(Keyframe(t=b.t - lo, v=b.v, ease="linear"))
                else:
                    push(Keyframe(t=seg_hi - lo, v=a.v, ease="linear"))
                continue

            span, into0, v0, v1 = _segment_domain(a, b)
            into_lo = into0 + (seg_lo - a.t)
            v_lo = v0 + (v1 - v0) * ease_value(a.ease, into_lo / float(span))
            v_hi = v0 + (v1 - v0) * ease_value(a.ease, (into0 + (seg_hi - a.t)) / float(span))
            eased = a.ease in ("ease_in", "ease_out", "ease_in_out")
            cut_inside = eased and (into_lo > 0 or seg_hi < a.t + (span - into0))
            if cut_inside or (eased and seg_hi < b.t):
                push(Keyframe(
                    t=seg_lo - lo, v=v_lo, ease=a.ease,
                    ease_span=span, ease_into=into_lo, ease_v0=v0, ease_v1=v1,
                ))
            else:
                push(Keyframe(t=seg_lo - lo, v=v_lo, ease=a.ease if eased else "linear"))
            push(Keyframe(t=seg_hi - lo, v=v_hi, ease="linear"))

        if rebuilt:
            last = rebuilt[-1]
            rebuilt[-1] = Keyframe(t=last.t, v=last.v, ease="linear")
            out[prop] = rebuilt
    return out


def speed_value(keys: list[SpeedKey], src: float) -> float:
    """The speed curve at source time ``src``: the first / last key's speed outside them, eased in between."""
    if not keys:
        return 1.0
    keys = sorted(keys, key=lambda k: k.t)
    if src <= keys[0].t:
        return keys[0].v
    for a, b in zip(keys, keys[1:]):
        if a.t <= src < b.t:
            u = (src - a.t) / max(1e-9, b.t - a.t)
            return a.v + (b.v - a.v) * ease_value(a.ease, u)
    return keys[-1].v


def _inverse_integral(keys: list[SpeedKey], a: float, b: float) -> float:
    """Timeline ms spent on source [a, b]: the integral of 1 / speed (Simpson; the curve is smooth inside one step)."""
    if b <= a:
        return 0.0
    n = 8
    h = (b - a) / n
    total = 1 / speed_value(keys, a) + 1 / speed_value(keys, b)
    for i in range(1, n):
        total += (4 if i % 2 else 2) / speed_value(keys, a + i * h)
    return total * h / 3


def ramp_segments(src_in: int, src_out: int, keys: list[SpeedKey]) -> list[tuple[float, float, float]]:
    """Constant-speed steps (a, b, speed) covering [src_in, src_out]. Steps are anchored on the keys (absolute source
    times), not on the clip, so both halves of a split clip keep exactly the steps they had; each step's speed is the one
    that spends the exact integral of 1 / speed on it, so the clip lasts what the curve says."""
    keys = sorted(keys, key=lambda k: k.t)
    lo, hi = float(src_in), float(src_out)
    cuts = {lo, hi}
    for k in keys:
        if lo < k.t < hi:
            cuts.add(float(k.t))
    for a, b in zip(keys, keys[1:]):
        if b.t <= lo or a.t >= hi or abs(a.v - b.v) < 1e-6 or a.ease == "hold":
            continue
        n = max(1, min(RAMP_MAX_STEPS, int(math.ceil((b.t - a.t) / RAMP_STEP_MS))))
        for j in range(1, n):
            x = a.t + (b.t - a.t) * j / n
            if lo < x < hi:
                cuts.add(x)
    edges = sorted(cuts)
    out: list[tuple[float, float, float]] = []
    for a, b in zip(edges, edges[1:]):
        if b - a < 1e-6:
            continue
        out.append((a, b, max(0.1, min(16.0, (b - a) / _inverse_integral(keys, a, b)))))
    return out or [(lo, hi, keys[0].v)]


class Track(Strict):
    id: str = Field(default_factory=lambda: new_id("trk"))
    kind: TrackKind = "video"
    name: str = Field("", max_length=60)
    muted: bool = False
    hidden: bool = False
    locked: bool = False
    volume_db: float = Field(0.0, ge=-60, le=24)
    role: Literal["main", "overlay", "voice", "music", "sfx", "titles"] = "overlay"
    duck: bool = Field(False, description="Audio tracks: lower this track while there is speech on the main / voice tracks.")
    clips: list[Clip] = Field(default_factory=list)


class Marker(Strict):
    id: str = Field(default_factory=lambda: new_id("mk"))
    t: int = Field(..., ge=0)
    label: str = Field("", max_length=120)
    color: str = Field("#F5B700", pattern=r"^#[0-9a-fA-F]{6}$")
    kind: Literal["note", "beat", "scene", "highlight", "chapter"] = "note"


class Captions(Strict):
    enabled: bool = False
    style: CaptionStyle = "bold"
    font: str = Field("Arial", max_length=80)
    size: int = Field(0, ge=0, le=400, description="0 = automatic from the canvas height.")
    color: str = Field("#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    highlight: str = Field("#FFD400", pattern=r"^#[0-9a-fA-F]{6}$")
    outline: str = Field("#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    position: Literal["top", "middle", "bottom", "lower_third"] = "lower_third"
    max_words: int = Field(4, ge=1, le=16)
    max_chars: int = Field(32, ge=8, le=120)
    uppercase: bool = False
    tracks: list[str] = Field(default_factory=list, description="Track ids whose speech is captioned; empty = the main track.")
    speaker_labels: Literal["off", "color", "prefix", "both"] = Field(
        "off", description="With a speaker-separated transcript: colour each speaker's words, start each speaker's lines with the name, or both.")


class Angle(Strict):
    """One recording of a multicam group. ``start`` is where the media's own time 0 falls on the group's shared clock (ms), so
    the media shows ``group_time - start`` at ``group_time``. Audio-only angles (an external microphone) can be the master sound
    but never the picture."""

    media: str
    label: str = Field("", max_length=60)
    start: int = Field(0, description="Group time (ms) at which this media's time 0 happened.")
    audio_only: bool = False
    confidence: Optional[float] = Field(None, ge=0, le=1, description="How sure the audio sync was (null = set by hand).")


class Multicam(Strict):
    """Recordings of one event made at the same time. The timeline shows them as ordinary clips of the angle media on the main track
    (``Clip.multicam`` points here) over one continuous master sound, so cuts, text edits and undo need nothing special."""

    id: str = Field(default_factory=lambda: new_id("mc"))
    name: str = Field("Multicámara", max_length=80)
    angles: list[Angle] = Field(..., min_length=2, max_length=12)
    master: int = Field(0, ge=0, description="Index of the angle whose sound is heard.")

    def angle_index(self, media: str) -> int:
        for i, a in enumerate(self.angles):
            if a.media == media:
                return i
        raise NotFound(f"Media {media} is not an angle of {self.name}.")

    @model_validator(mode="after")
    def _check(self) -> "Multicam":
        if len({a.media for a in self.angles}) != len(self.angles):
            raise ValueError("Each angle needs a different media.")
        if self.master >= len(self.angles):
            raise ValueError("master is not one of the angles.")
        return self


class Project(Strict):
    schema_: str = Field(SCHEMA, alias="schema")
    canvas: Canvas = Field(default_factory=Canvas)
    tracks: list[Track] = Field(default_factory=list, max_length=MAX_TRACKS)
    markers: list[Marker] = Field(default_factory=list, max_length=5000)
    captions: Captions = Field(default_factory=Captions)
    notes: str = Field("", max_length=8000)
    length_mode: Literal["main", "longest"] = Field("main", description="main: the video ends with the main track (music or titles past it are cut); longest: with the last clip of any track.")
    multicams: list[Multicam] = Field(default_factory=list, max_length=20)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ------------------------------------------------------------ lookup
    def track(self, track_id: str) -> Track:
        for t in self.tracks:
            if t.id == track_id:
                return t
        raise NotFound(f"No track {track_id} in this project.")

    def find(self, clip_id: str) -> tuple[Track, Clip]:
        for t in self.tracks:
            for c in t.clips:
                if c.id == clip_id:
                    return t, c
        raise NotFound(f"No clip {clip_id} in this project.")

    def multicam(self, group_id: str) -> Multicam:
        for g in self.multicams:
            if g.id == group_id:
                return g
        raise NotFound(f"No multicam group {group_id} in this project.")

    def with_multicam_sound(self, track_ids: set[str]) -> set[str]:
        """``track_ids`` plus the tracks holding the master sound of every multicam group shown on them: the picture of a
        group is muted angle clips, so its words (captions, text view) come from that sound."""
        groups = {c.multicam for t in self.tracks if t.id in track_ids for c in t.clips if c.multicam}
        if not groups:
            return track_ids
        extra = {t.id for t in self.tracks if t.kind == "audio" and any(c.multicam in groups for c in t.clips)}
        return track_ids | extra

    def main_track(self) -> Optional[Track]:
        for t in self.tracks:
            if t.kind == "video" and t.role == "main":
                return t
        return next((t for t in self.tracks if t.kind == "video"), None)

    @property
    def duration(self) -> int:
        main = self.main_track()
        if self.length_mode == "main" and main is not None and main.clips:
            return max(c.end for c in main.clips)
        return max((c.end for t in self.tracks for c in t.clips), default=0)

    @property
    def content_end(self) -> int:
        return max((c.end for t in self.tracks for c in t.clips), default=0)

    def all_clips(self):
        for t in self.tracks:
            for c in t.clips:
                yield t, c

    def media_ids(self) -> set[str]:
        return {c.media for _, c in self.all_clips() if c.media and c.type == "media"}

    def sequence_ids(self) -> set[str]:
        """Projects nested in this one as sequence clips (directly)."""
        return {c.media for _, c in self.all_clips() if c.media and c.type == "sequence"}

    def sort(self) -> None:
        for t in self.tracks:
            t.clips.sort(key=lambda c: (c.start, c.id))

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=False)


def new_project(width: int = 1920, height: int = 1080, fps: float = 30.0) -> Project:
    return Project(canvas=Canvas(width=width, height=height, fps=fps), tracks=[
        Track(kind="video", name="Principal", role="main"),
        Track(kind="text", name="Textos", role="titles"),
        Track(kind="audio", name="Música", role="music", duck=True),
    ])


def load(doc: dict[str, Any]) -> Project:
    return Project.model_validate(doc)


def validate(project: Project, media_lookup=None) -> list[dict[str, Any]]:
    """Problems that would make the render wrong: overlaps, clips past the end of their media, unknown media.
    Returns a list of {level, code, message, clip?, track?} (empty when everything is fine)."""
    issues: list[dict[str, Any]] = []
    ids: set[str] = set()
    total = 0
    for t in project.tracks:
        prev: Optional[Clip] = None
        for c in sorted(t.clips, key=lambda c: c.start):
            total += 1
            if c.id in ids:
                issues.append({"level": "error", "code": "duplicate_id", "message": f"Duplicated clip id {c.id}.", "clip": c.id})
            ids.add(c.id)
            if t.kind == "text" and c.type != "text":
                issues.append({"level": "error", "code": "wrong_track", "message": f"Clip {c.id} is media on a text track.", "clip": c.id})
            if t.kind != "text" and c.type == "text":
                issues.append({"level": "error", "code": "wrong_track", "message": f"Text clip {c.id} is on a {t.kind} track.", "clip": c.id})
            if prev is not None and c.start < prev.end:
                overlap = prev.end - c.start
                allowed = c.transition_in.dur if c.transition_in else 0
                if overlap > allowed + 1:
                    issues.append({"level": "error", "code": "overlap", "track": t.id, "clip": c.id,
                                   "message": f"Clip {c.id} overlaps {prev.id} by {overlap} ms on track {t.name or t.id}."})
            if c.type in ("media", "sequence") and media_lookup is not None:
                info = media_lookup(c.media)
                if info is None:
                    what = "project" if c.type == "sequence" else "media"
                    issues.append({"level": "error", "code": "missing_media", "clip": c.id, "message": f"Clip {c.id} uses unknown {what} {c.media}."})
                else:
                    if info.get("missing"):
                        issues.append({"level": "error", "code": "file_missing", "clip": c.id, "message": f"The file of {info.get('name')} is missing: {info.get('path')}."})
                    dur = int(info.get("duration_ms") or 0)
                    if info.get("kind") != "image" and dur and c.src_out > dur + 50:
                        issues.append({"level": "warning", "code": "past_end", "clip": c.id,
                                       "message": f"Clip {c.id} asks for {c.src_out} ms of a {dur} ms media; the rest will be frozen or silent."})
                    if t.kind == "audio" and not info.get("has_audio"):
                        issues.append({"level": "warning", "code": "no_audio", "clip": c.id, "message": f"Clip {c.id} is on an audio track but its media has no sound."})
                    if c.reverse and c.src_out - c.src_in > 60000:
                        issues.append({"level": "error", "code": "reverse_too_long", "clip": c.id, "message": "Reverse playback is limited to 60 s of source per clip."})
                    if c.type == "sequence" and info.get("cycle"):
                        issues.append({"level": "error", "code": "sequence_cycle", "clip": c.id,
                                       "message": f"Sequence clip {c.id} nests a project that contains this one."})
            prev = c if prev is None or c.end > prev.end else prev
    groups = {g.id for g in project.multicams}
    for t in project.tracks:
        for c in t.clips:
            if c.multicam and c.multicam not in groups:
                issues.append({"level": "warning", "code": "unknown_multicam", "clip": c.id, "message": f"Clip {c.id} points at multicam group {c.multicam}, which does not exist."})
    if total > MAX_CLIPS:
        issues.append({"level": "error", "code": "too_many_clips", "message": f"{total} clips: the limit is {MAX_CLIPS}."})
    return issues


def clone(project: Project) -> Project:
    return Project.model_validate(copy.deepcopy(project.dump()))


def snap(ms: float, fps: float) -> int:
    """Round to the frame grid of the canvas."""
    frame = 1000.0 / fps
    return int(round(round(ms / frame) * frame))


def frames(ms: int, fps: float) -> int:
    return int(math.floor(ms * fps / 1000 + 1e-6))


def require(cond: bool, message: str, code: str | None = None) -> None:
    if not cond:
        raise LumiereError(message, code=code)
