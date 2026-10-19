"""Primitive timeline operations. Pure functions over a Project: no files, no database.

Every operation is a JSON object ``{"op": "<name>", ...}``. ``apply_ops`` runs a list of them on a copy of the project
and returns the new project plus one result per operation; if any operation fails nothing changes. The same vocabulary
is used by the editor UI, the MCP tools and the edit plans written by the model.
"""

from __future__ import annotations

from typing import Any, Callable, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .errors import LumiereError, NotFound
from .timeline import (EQ_KEY_PROPS, MASK_PROPS, MIN_CLIP_MS, Angle, Canvas, Captions, CaptionStyle, Clip, Crop, Ease, Filter, FilterType, Keyframe, KeyProp,
                       Marker, Mask, MaskShape, Multicam, Project, SpeedKey, TextStyle, Track, TrackKind, Transform, Transition, TransitionType,
                       clone, eq_key_error, slice_keyframes, snap)
from .util import new_id, parse_time

MediaLookup = Callable[[str], Optional[dict[str, Any]]]
DocLookup = Callable[[str], Optional[Project]]
RAMP_PRESETS = ("speed_up", "slow_down", "ease_in_out", "hit", "clear")

PRESETS: dict[str, dict[str, Any]] = {
    "reels": {"width": 1080, "height": 1920, "fps": 30, "label": "Reels / Shorts / TikTok 9:16"},
    "shorts": {"width": 1080, "height": 1920, "fps": 30, "label": "YouTube Shorts 9:16"},
    "tiktok": {"width": 1080, "height": 1920, "fps": 30, "label": "TikTok 9:16"},
    "youtube": {"width": 1920, "height": 1080, "fps": 30, "label": "YouTube 1080p 16:9"},
    "youtube_60": {"width": 1920, "height": 1080, "fps": 60, "label": "YouTube 1080p60 (gameplay)"},
    "youtube_4k": {"width": 3840, "height": 2160, "fps": 30, "label": "YouTube 4K"},
    "square": {"width": 1080, "height": 1080, "fps": 30, "label": "Cuadrado 1:1"},
    "portrait_4_5": {"width": 1080, "height": 1350, "fps": 30, "label": "Vertical 4:5 (feed)"},
    "cinema": {"width": 1920, "height": 804, "fps": 24, "label": "Cine 2.39:1"},
    "hd720": {"width": 1280, "height": 720, "fps": 30, "label": "720p"},
}


class OpBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


Time = Union[int, float, str]


def _t(value: Time | None) -> Optional[int]:
    if value is None:
        return None
    try:
        return parse_time(value) if isinstance(value, str) else int(round(value))
    except ValueError as error:
        raise LumiereError(str(error)) from error


class AddMedia(OpBase):
    op: Literal["add_media"]
    media: str
    track: Optional[str] = None
    at: Optional[Time] = Field(None, description="Timeline ms; omitted = end of the track.")
    src_in: Optional[Time] = None
    src_out: Optional[Time] = None
    length: Optional[Time] = Field(None, description="Images: how long they stay (default 4000 ms).")
    mode: Literal["insert", "overwrite", "append"] = "append"
    label: str = ""
    fit: Optional[Literal["contain", "cover", "fill", "none", "blur"]] = None


class AddText(OpBase):
    op: Literal["add_text"]
    text: str = Field(..., min_length=1, max_length=4000)
    start: Time = 0
    length: Time = 3000
    track: Optional[str] = None
    style: dict[str, Any] = Field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0


class Split(OpBase):
    op: Literal["split"]
    at: Time
    clip: Optional[str] = Field(None, description="Omitted = every clip under 'at' on unlocked tracks.")


class Trim(OpBase):
    op: Literal["trim"]
    clip: str
    src_in: Optional[Time] = None
    src_out: Optional[Time] = None
    length: Optional[Time] = Field(None, description="Text clips / images: new length.")
    ripple: bool = True


class Move(OpBase):
    op: Literal["move"]
    clip: str
    start: Optional[Time] = None
    track: Optional[str] = None


class Delete(OpBase):
    op: Literal["delete"]
    clips: list[str] = Field(..., min_length=1, max_length=2000)
    ripple: bool = True


class DeleteRange(OpBase):
    op: Literal["delete_range"]
    start: Time
    end: Time
    tracks: Optional[list[str]] = Field(None, description="Omitted = every unlocked track.")
    ripple: bool = True


class CutSource(OpBase):
    op: Literal["cut_source"]
    media: str
    ranges: list[list[Time]] = Field(..., min_length=1, max_length=20000, description="[[from_ms, to_ms], ...] in source time.")
    scope: Literal["linked", "all"] = Field("linked", description="linked: tracks holding this media plus text tracks; all: every unlocked track.")
    ripple: bool = True
    min_gap: int = Field(0, ge=0, le=5000, description="Ranges closer than this are merged.")


class KeepSource(OpBase):
    op: Literal["keep_source"]
    media: str
    ranges: list[list[Time]] = Field(..., min_length=1, max_length=20000)
    scope: Literal["linked", "all"] = "linked"


class SetClip(OpBase):
    op: Literal["set"]
    clip: str
    props: dict[str, Any]
    ripple: bool = True


class Speed(OpBase):
    op: Literal["speed"]
    clip: str
    speed: float = Field(..., ge=0.1, le=16)
    ripple: bool = True


class TransitionOp(OpBase):
    op: Literal["transition"]
    clip: Optional[str] = Field(None, description="The clip that receives the transition from the previous one.")
    track: Optional[str] = Field(None, description="With all_cuts: the track.")
    all_cuts: bool = False
    type: Optional[TransitionType] = Field("crossfade", description="null removes it.")
    dur: int = Field(500, ge=40, le=5000)


class FilterAdd(OpBase):
    op: Literal["filter_add"]
    clips: Optional[list[str]] = None
    track: Optional[str] = None
    type: FilterType
    params: dict[str, Any] = Field(default_factory=dict)


class FilterRemove(OpBase):
    op: Literal["filter_remove"]
    clip: str
    type: Optional[FilterType] = None
    index: Optional[int] = None


class TrackAdd(OpBase):
    op: Literal["track_add"]
    kind: TrackKind
    name: str = ""
    role: Optional[Literal["main", "overlay", "voice", "music", "sfx", "titles"]] = None
    index: Optional[int] = None
    id: Optional[str] = None


class TrackSet(OpBase):
    op: Literal["track_set"]
    track: str
    props: dict[str, Any]


class TrackDelete(OpBase):
    op: Literal["track_delete"]
    track: str


class CanvasOp(OpBase):
    op: Literal["canvas"]
    preset: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    background: Optional[str] = None
    fit: Optional[Literal["contain", "cover", "fill", "blur"]] = Field(None, description="Also set this fit on every video clip (blur: whole frame over a blurred fill).")
    length_mode: Optional[Literal["main", "longest"]] = Field(None, description="main: the video ends with the main track; longest: with the last clip.")


class MarkerAdd(OpBase):
    op: Literal["marker_add"]
    t: Time
    label: str = ""
    color: str = "#F5B700"
    kind: Literal["note", "beat", "scene", "highlight", "chapter"] = "note"


class MarkerDelete(OpBase):
    op: Literal["marker_delete"]
    id: Optional[str] = None
    kind: Optional[Literal["note", "beat", "scene", "highlight", "chapter"]] = None


class CaptionsOp(OpBase):
    op: Literal["captions"]
    enabled: Optional[bool] = None
    style: Optional[CaptionStyle] = None
    props: dict[str, Any] = Field(default_factory=dict)


class Duplicate(OpBase):
    op: Literal["duplicate"]
    clip: str
    at: Optional[Time] = None


class DetachAudio(OpBase):
    op: Literal["detach_audio"]
    clip: str
    track: Optional[str] = None


class CloseGaps(OpBase):
    op: Literal["close_gaps"]
    track: Optional[str] = None


class ReplaceMedia(OpBase):
    op: Literal["replace_media"]
    clip: str
    media: str


class Sequence(OpBase):
    op: Literal["sequence"]
    items: list[dict[str, Any]] = Field(..., min_length=1, max_length=2000, description="[{media, src_in?, src_out?, length?}] appended in order.")
    track: Optional[str] = None


class KeyframesOp(OpBase):
    op: Literal["keyframes"]
    clip: str
    prop: KeyProp
    keys: list[Keyframe] = Field(default_factory=list, max_length=500, description="Empty list removes the animation.")


class Slip(OpBase):
    op: Literal["slip"]
    clip: str
    delta: Time = Field(..., description="Show later (+) or earlier (-) source material; the clip stays where it is.")


class Roll(OpBase):
    op: Literal["roll"]
    clip: str = Field(..., description="The clip after the cut: the cut between it and the previous clip moves.")
    delta: Time = Field(..., description="Move the cut later (+) or earlier (-); both clips change, nothing else moves.")


class InsertClips(OpBase):
    op: Literal["insert_clips"]
    clips: list[dict[str, Any]] = Field(..., min_length=1, max_length=500, description="Clip objects (as in project_get full), e.g. copied ones.")
    at: Time
    track: Optional[str] = None
    mode: Literal["insert", "overwrite"] = "overwrite"


class Notes(OpBase):
    op: Literal["notes"]
    text: str = Field("", max_length=8000)


class RampKey(OpBase):
    t: Time = Field(..., description="Source ms from the clip's in point (relative=true) or of the media.")
    v: float = Field(..., ge=0.1, le=16)
    ease: Ease = "linear"


class SpeedRamp(OpBase):
    op: Literal["speed_ramp"]
    clip: str
    keys: Optional[list[RampKey]] = Field(None, max_length=64, description="Speed curve [{t, v, ease}]; t in source ms (see relative).")
    preset: Optional[Literal["speed_up", "slow_down", "ease_in_out", "hit", "clear"]] = Field(
        None, description="speed_up / slow_down: 1x to speed over the clip; ease_in_out: up to speed and back; hit: slow motion around 'at'; clear.")
    speed: Optional[float] = Field(None, ge=0.1, le=16, description="Target speed of the preset (speed_up 2, slow_down 0.5, ease_in_out 2, hit 0.25).")
    at: Optional[Time] = Field(None, description="hit: timeline time of the moment to slow down (default the clip's middle).")
    hold: Optional[Time] = Field(None, description="hit: source ms played at the slow speed (default 600).")
    ramp: Optional[Time] = Field(None, description="hit: source ms to go down and back up (default 400).")
    relative: bool = Field(True, description="keys: t counts from the clip's in point (false: media time).")
    ripple: bool = True


class AddSequence(OpBase):
    op: Literal["add_sequence"]
    project: str = Field(..., description="The project to use as a clip (prj_...); it cannot contain this project.")
    track: Optional[str] = None
    at: Optional[Time] = Field(None, description="Timeline ms; omitted = end of the track.")
    src_in: Optional[Time] = Field(None, description="From this time of the nested timeline.")
    src_out: Optional[Time] = None
    mode: Literal["insert", "overwrite", "append"] = "append"
    label: str = ""


class Unnest(OpBase):
    op: Literal["unnest"]
    clip: str = Field(..., description="A sequence clip: its nested clips come back onto this timeline in its place.")


class MaskOp(OpBase):
    op: Literal["mask"]
    clip: Optional[str] = None
    clips: Optional[list[str]] = Field(None, max_length=500)
    shape: Optional[MaskShape] = None
    x: Optional[float] = Field(None, ge=-1, le=2, description="Centre as a fraction of the clip's picture (0.5 = middle).")
    y: Optional[float] = Field(None, ge=-1, le=2)
    w: Optional[float] = Field(None, gt=0, le=4, description="Size as a fraction of the clip's picture.")
    h: Optional[float] = Field(None, gt=0, le=4)
    radius: Optional[float] = Field(None, ge=0, le=0.5)
    feather: Optional[float] = Field(None, ge=0, le=0.5, description="Soft edge, fraction of the picture's smaller side.")
    invert: Optional[bool] = None
    enabled: Optional[bool] = None
    remove: bool = Field(False, description="Remove the mask and its keyframes.")


class MulticamCreate(OpBase):
    op: Literal["multicam_create"]
    angles: list[dict[str, Any]] = Field(..., min_length=2, max_length=12, description="[{media, start?, label?}]: start = group time (ms) at which that media's own time 0 happened (multicam_sync finds it).")
    name: str = Field("Multicámara", max_length=80)
    master: Optional[Union[str, int]] = Field(None, description="Media id or label whose sound is heard (an external microphone is an angle with no picture). Default: the first angle with sound.")
    angle: Optional[Union[str, int]] = Field(None, description="Angle shown first: media id, label or 1-based number. Default: the first with picture.")
    at: Optional[Time] = Field(None, description="Timeline ms where the group starts; omitted = end of the main track.")
    from_ms: Optional[Time] = Field(None, description="Group time to start from; default: where every angle has material.")
    to_ms: Optional[Time] = None
    id: Optional[str] = None


class MulticamSwitch(OpBase):
    op: Literal["multicam_switch"]
    angle: Optional[Union[str, int]] = Field(None, description="Angle to show: media id, label or 1-based number.")
    at: Optional[Time] = Field(None, description="Timeline ms where the new angle starts (the playhead).")
    end: Optional[Time] = Field(None, description="Timeline ms where it ends; omitted = the end of the shot that contains 'at'.")
    clip: Optional[str] = Field(None, description="A clip of the group (its whole shot is switched when 'at' is omitted).")
    group: Optional[str] = Field(None, description="Group id; omitted = the group of 'clip', or the only one.")
    cuts: Optional[list[list[Any]]] = Field(None, description="Many switches at once: [[timeline_ms, angle], ...]; each angle holds until the next cut (the last until the end of the group).")


class MulticamSet(OpBase):
    op: Literal["multicam_set"]
    group: Optional[str] = None
    name: Optional[str] = Field(None, max_length=80)
    offsets: Optional[dict[str, Time]] = Field(None, description="{media id or label: start_ms}: put an angle's sync right by hand; clips follow.")
    master: Optional[Union[str, int]] = Field(None, description="Media id or label: change whose sound is heard.")
    release: bool = Field(False, description="Dissolve the group: its clips become ordinary clips.")


class AddOverlay(OpBase):
    op: Literal["add_overlay"]
    media: str
    start: Time = Field(..., description="Timeline ms where the overlay (b-roll) starts.")
    length: Time = Field(..., description="How long it covers; a shorter media covers less.")
    src_in: Optional[Time] = Field(None, description="Where in the media it starts (default 0).")
    track: Optional[str] = Field(None, description="A video track; omitted = the first overlay track that is free there, or a new 'B-roll' track on top.")
    fit: Literal["contain", "cover", "fill", "none", "blur"] = "cover"
    mute: bool = True
    fade_in: int = Field(0, ge=0, le=5000)
    fade_out: int = Field(0, ge=0, le=5000)


class FillSlot(OpBase):
    op: Literal["fill_slot"]
    slot: str = Field(..., min_length=1, max_length=40, description="The slot name on a clip (set it with set {props: {slot: 'intro'}}).")
    media: str
    src_in: Optional[Time] = None
    src_out: Optional[Time] = None
    length: Optional[Time] = Field(None, description="Images, or a shorter stay for a slot that keeps its length.")
    rule: Literal["auto", "keep", "full"] = Field("auto", description="full: the clip takes the media's whole length and what follows moves; keep: the slot's length "
                                                  "(shorter when the media is); auto: full for slots named main*, keep for the rest.")


Op = Union[AddMedia, AddText, Split, Trim, Move, Delete, DeleteRange, CutSource, KeepSource, SetClip, Speed, TransitionOp, FilterAdd,
           FilterRemove, TrackAdd, TrackSet, TrackDelete, CanvasOp, MarkerAdd, MarkerDelete, CaptionsOp, Duplicate, DetachAudio, CloseGaps,
           ReplaceMedia, Sequence, KeyframesOp, Slip, Roll, InsertClips, Notes, SpeedRamp, AddSequence, Unnest, MaskOp, MulticamCreate,
           MulticamSwitch, MulticamSet, AddOverlay, FillSlot]
_ADAPTER = TypeAdapter(Op)
OP_MODELS: dict[str, type[BaseModel]] = {m.model_fields["op"].annotation.__args__[0]: m for m in Op.__args__}  # type: ignore[union-attr]
OP_NAMES = sorted(OP_MODELS)

# names assistants reach for first; each maps to exactly one operation
OP_ALIASES = {
    "add_title": "add_text", "title": "add_text", "text": "add_text", "add_titles": "add_text", "text_add": "add_text", "add_label": "add_text",
    "add_clip": "add_media", "add_video": "add_media", "add_audio": "add_media", "add_image": "add_media", "add_music": "add_media",
    "remove": "delete", "remove_clip": "delete", "delete_clip": "delete", "delete_clips": "delete", "ripple_delete": "delete",
    "split_clip": "split", "razor": "split", "move_clip": "move", "trim_clip": "trim",
    "marker": "marker_add", "add_marker": "marker_add", "chapter": "marker_add", "add_transition": "transition", "add_filter": "filter_add",
    "effect": "filter_add", "add_effect": "filter_add", "set_clip": "set", "update": "set", "set_speed": "speed", "add_track": "track_add",
    "insert": "insert_clips", "subtitles": "captions", "set_canvas": "canvas", "resize": "canvas", "close_gap": "close_gaps",
    "ramp": "speed_ramp", "speed_curve": "speed_ramp", "time_remap": "speed_ramp", "add_project": "add_sequence", "add_nested": "add_sequence",
    "nested": "add_sequence", "un_nest": "unnest", "flatten": "unnest", "add_mask": "mask", "set_mask": "mask", "shape_mask": "mask",
}
# argument names assistants guess; renamed only when the operation has the target field and not the guessed one
_ARG_ALIASES = {
    "duration": "length", "duration_ms": "length", "len": "length", "len_ms": "length", "length_ms": "length",
    "start_ms": "start", "at_ms": "at", "clip_id": "clip", "media_id": "media", "track_id": "track", "clip_ids": "clips",
    "content": "text", "title": "text",
}


def _type_hint(annotation: Any) -> str:
    args = getattr(annotation, "__args__", None) or ()
    literals = [a for a in args if isinstance(a, str)]
    if getattr(annotation, "__origin__", None) is Literal or (literals and len(literals) == len(args)):
        return "|".join(literals)
    for a in args:
        inner = _type_hint(a)
        if "|" in inner:
            return inner
    return ""


def op_signature(name: str) -> str:
    """One line per operation, generated from its model so it never drifts: add_text {text, start?, length?, ...}."""
    model = OP_MODELS[name]
    parts = []
    for field, info in model.model_fields.items():
        if field == "op":
            continue
        hint = _type_hint(info.annotation)
        parts.append(f"{field}{'' if info.is_required() else '?'}{': ' + hint if hint else ''}")
    return f"{name} {{{', '.join(parts)}}}"


def op_reference(names: Optional[list[str]] = None) -> str:
    return "\n".join(op_signature(n) for n in (names or OP_NAMES))


def _normalise(raw: dict[str, Any]) -> dict[str, Any]:
    name = OP_ALIASES.get(str(raw["op"]).strip().lower(), str(raw["op"]).strip().lower())
    out = dict(raw, op=name)
    model = OP_MODELS.get(name)
    if model is None:
        return out
    fields = model.model_fields
    for guess, target in _ARG_ALIASES.items():
        if guess in out and guess not in fields and target in fields and target not in out:
            out[target] = out.pop(guess)
    if "at" in out and "at" not in fields and "start" in fields and "start" not in out:
        out["start"] = out.pop("at")
    return out


def parse_op(raw: dict[str, Any]) -> BaseModel:
    if not isinstance(raw, dict) or "op" not in raw:
        raise LumiereError("Each operation is an object with an 'op' field. Operations:\n" + op_reference(), code="bad_op")
    raw = _normalise(raw)
    model = OP_MODELS.get(raw["op"])
    if model is None:
        import difflib

        close = difflib.get_close_matches(raw["op"], OP_NAMES, n=3, cutoff=0.4)
        hint = ("Closest: " + "; ".join(op_signature(n) for n in close) + "\n") if close else ""
        raise LumiereError(f"Unknown operation {raw['op']!r}. {hint}All operations:\n{op_reference()}", code="unknown_op")
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        issues = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in error.errors())
        raise LumiereError(f"{raw['op']}: {issues}. Expected: {op_signature(raw['op'])}", code="bad_op") from error


# ------------------------------------------------------------------ helpers

class Ctx:
    def __init__(self, project: Project, media: MediaLookup, project_id: Optional[str] = None, docs: Optional[DocLookup] = None):
        self.p = project
        self.media_lookup = media
        self.project_id = project_id  # the project being edited (refuses nesting it inside itself)
        self.docs = docs  # reads another project's timeline (un-nest)

    def media(self, media_id: str) -> dict[str, Any]:
        info = self.media_lookup(media_id)
        if info is None:
            if str(media_id).startswith("prj_"):
                raise NotFound(f"No project {media_id} to use as a sequence.")
            raise NotFound(f"No media {media_id}. Import it first (media_import).")
        return info

    def check_nesting(self, project_id: str) -> dict[str, Any]:
        """The nested project's info; refuses a project that is this one or contains it (a sequence cannot hold itself)."""
        info = self.media(project_id)
        if info.get("kind") != "sequence":
            raise LumiereError(f"{project_id} is not a project.")
        if info.get("cycle") or (self.project_id and (project_id == self.project_id or self.project_id in (info.get("contains") or []))):
            raise LumiereError(f"Project {info.get('name') or project_id} contains this project: nesting it here would make a loop.", code="sequence_cycle")
        return info

    def fps(self) -> float:
        return self.p.canvas.fps

    def snap(self, ms: float) -> int:
        return snap(ms, self.fps())


def _unlocked(track: Track) -> None:
    if track.locked:
        raise LumiereError(f"Track {track.name or track.id} is locked.", code="track_locked")


def _track_end(track: Track) -> int:
    return max((c.end for c in track.clips), default=0)


def _shift(track: Track, after: int, delta: int, exclude: set[str] = frozenset()) -> None:
    """Move every clip of ``track`` that starts at or after ``after`` by ``delta`` ms."""
    if not delta:
        return
    for c in track.clips:
        if c.id not in exclude and c.start >= after:
            c.start = max(0, c.start + delta)


def _default_track(ctx: Ctx, info: dict[str, Any]) -> Track:
    p = ctx.p
    if info.get("kind") == "audio":
        for role in ("music", "voice", "sfx"):
            for t in p.tracks:
                if t.kind == "audio" and t.role == role and not t.locked:
                    return t
        t = Track(kind="audio", name="Audio", role="music")
        p.tracks.append(t)
        return t
    main = p.main_track()
    if main is None:
        main = Track(kind="video", name="Principal", role="main")
        p.tracks.insert(0, main)
    return main


def _media_span(ctx: Ctx, info: dict[str, Any], src_in: Optional[int], src_out: Optional[int], length: Optional[int]) -> tuple[int, int]:
    if info.get("kind") == "image":
        span = length if length is not None else (src_out - (src_in or 0) if src_out is not None else 4000)
        if span < MIN_CLIP_MS:
            raise LumiereError("An image needs at least 40 ms on the timeline.")
        return 0, int(span)
    dur = int(info.get("duration_ms") or 0)
    a = 0 if src_in is None else src_in
    b = dur if src_out is None else src_out
    if dur:
        b = min(b, dur)
    if a < 0 or b - a < MIN_CLIP_MS:
        raise LumiereError(f"Empty or too short source range [{a}, {b}] for a {dur} ms media.")
    return a, b


def _clear_range(track: Track, start: int, end: int, keep: set[str] = frozenset()) -> None:
    """Remove [start, end) from a track without moving anything (overwrite / non-ripple delete)."""
    out: list[Clip] = []
    for c in track.clips:
        if c.id in keep or c.end <= start or c.start >= end:
            out.append(c)
            continue
        if c.start < start:
            left = _cut_clip(c, c.start, start)
            if left:
                out.append(left)
        if c.end > end:
            right = _cut_clip(c, end, c.end, new_id_for_part=c.start < start)
            if right:
                out.append(right)
    track.clips = out


def _cut_clip(c: Clip, t0: int, t1: int, new_id_for_part: bool = False) -> Optional[Clip]:
    """The part of clip ``c`` shown between timeline t0 and t1 (a copy)."""
    t0, t1 = max(t0, c.start), min(t1, c.end)
    if t1 - t0 < MIN_CLIP_MS // 2:
        return None
    part = c.model_copy(deep=True)
    if new_id_for_part:
        part.id = new_id("clp")
    part.start = t0
    if c.type == "text":
        part.length = t1 - t0
    else:
        a, b = c.src_at(t0), c.src_at(t1)
        lo, hi = (b, a) if c.reverse else (a, b)
        part.src_in, part.src_out = int(round(lo)), int(round(hi))
        if part.src_out - part.src_in < 1:
            return None
    if t0 > c.start:
        part.transition_in = None
        part.fade_in = 0
        part.audio_fade_in = 0
    if t1 < c.end:
        part.fade_out = 0
        part.audio_fade_out = 0
    # Property keys are clip-local: both halves get a cut key with the interpolated value and keep interior keys.
    # speed_keys stay absolute in source time (deep copy); the trimmed src_in/src_out already scopes them.
    if c.keyframes and (t0 > c.start or t1 < c.end):
        part.keyframes = slice_keyframes(c.keyframes, t0 - c.start, t1 - c.start)
    return part


def _merge(ranges: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted((min(a, b), max(a, b)) for a, b in ranges if b != a):
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def delete_range(p: Project, start: int, end: int, track_ids: Optional[list[str]], ripple: bool) -> int:
    if end <= start:
        return 0
    tracks = [t for t in p.tracks if (track_ids is None or t.id in track_ids) and not t.locked]
    span = end - start
    for t in tracks:
        _clear_range(t, start, end)
        if ripple:
            _shift(t, end, -span)
    if ripple and (track_ids is None or (p.main_track() and p.main_track().id in track_ids)):
        kept = []
        for m in p.markers:
            if start <= m.t < end:
                continue
            if m.t >= end:
                m.t -= span
            kept.append(m)
        p.markers = kept
    return span


def _scope_tracks(p: Project, media_id: str, scope: str) -> list[str]:
    if scope == "all":
        return [t.id for t in p.tracks if not t.locked]
    ids = [t.id for t in p.tracks if not t.locked and any(c.media == media_id for c in t.clips)]
    groups = {c.multicam for _, c in p.all_clips() if c.multicam and c.media == media_id}
    if groups:  # a multicam group moves as one: its picture and its master sound are cut together
        ids += [t.id for t in p.tracks if not t.locked and t.id not in ids and any(c.multicam in groups for c in t.clips)]
    ids += [t.id for t in p.tracks if t.kind == "text" and not t.locked and t.id not in ids]
    return ids


def source_to_timeline(p: Project, media_id: str, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Timeline ranges where the given source ranges of a media are shown (every clip of that media)."""
    out: list[tuple[int, int]] = []
    for _, c in p.all_clips():
        if c.media != media_id or c.type == "text":
            continue
        for a, b in ranges:
            lo, hi = max(a, c.src_in), min(b, c.src_out)
            if hi - lo <= 0:
                continue
            t0, t1 = sorted((c.timeline_at(lo), c.timeline_at(hi)))
            out.append((int(round(t0)), int(round(t1))))
    return _merge(out)


# ------------------------------------------------------------------ operations

def _add_media(ctx: Ctx, o: AddMedia) -> dict:
    p = ctx.p
    info = ctx.media(o.media)
    track = p.track(o.track) if o.track else _default_track(ctx, info)
    _unlocked(track)
    if track.kind == "text":
        raise LumiereError("Media goes on a video or audio track, not on a text track.")
    if track.kind == "video" and not (info.get("has_video") or info.get("kind") == "image"):
        raise LumiereError(f"{info.get('name')} has no picture: put it on an audio track.")
    if info.get("kind") == "sequence":
        ctx.check_nesting(o.media)
    a, b = _media_span(ctx, info, _t(o.src_in), _t(o.src_out), _t(o.length))
    clip = Clip(type="sequence" if info.get("kind") == "sequence" else "media", media=o.media, src_in=a, src_out=b,
                label=o.label or str(info.get("name") or "")[:120])
    if o.fit:
        clip.transform.fit = o.fit
    elif track.kind == "video":
        cw, ch = p.canvas.width, p.canvas.height
        mw, mh = int(info.get("width") or cw), int(info.get("height") or ch)
        # Same orientation: fill the frame; a horizontal video on a vertical canvas: keep it whole (reframe crops it on request).
        clip.transform.fit = "cover" if (mw >= mh) == (cw >= ch) and abs(mw / max(1, mh) - cw / ch) < 0.15 else "contain"
    at = _t(o.at)
    if at is None or o.mode == "append":
        clip.start = _track_end(track) if at is None else ctx.snap(at)
        if at is not None:
            _clear_range(track, clip.start, clip.end)
    elif o.mode == "insert":
        clip.start = ctx.snap(at)
        hit = next((c for c in track.clips if c.start < clip.start < c.end), None)
        if hit:
            _split_clip(track, hit, clip.start)
        _shift(track, clip.start, clip.duration)
    else:
        clip.start = ctx.snap(at)
        _clear_range(track, clip.start, clip.end)
    track.clips.append(clip)
    return {"clip": clip.id, "track": track.id, "start": clip.start, "end": clip.end}


def _add_text(ctx: Ctx, o: AddText) -> dict:
    p = ctx.p
    track = p.track(o.track) if o.track else next((t for t in p.tracks if t.kind == "text" and not t.locked), None)
    if track is None:
        track = Track(kind="text", name="Textos", role="titles")
        p.tracks.append(track)
    if track.kind != "text":
        raise LumiereError("Text goes on a text track.")
    _unlocked(track)
    given = dict(o.style)
    if "size" not in given:  # a title reads the same on a phone at 1080x1920 and on a 4K frame
        given["size"] = max(48, min(600, round(min(p.canvas.width, p.canvas.height) * 0.09)))
    style = TextStyle(**given)
    clip = Clip(type="text", text=o.text, start=ctx.snap(_t(o.start) or 0), length=max(MIN_CLIP_MS, _t(o.length) or 3000), style=style,
                transform=Transform(x=o.x, y=o.y))
    _clear_range(track, clip.start, clip.end)
    track.clips.append(clip)
    return {"clip": clip.id, "track": track.id, "start": clip.start, "end": clip.end}


def _split_clip(track: Track, c: Clip, at: int) -> Optional[str]:
    if not (c.start + MIN_CLIP_MS // 2 < at < c.end - MIN_CLIP_MS // 2):
        return None
    left = _cut_clip(c, c.start, at)
    right = _cut_clip(c, at, c.end, new_id_for_part=True)
    if not left or not right:
        return None
    if c.has_ramp:
        left, right = _ramp_split(c, left, right, at)
    idx = track.clips.index(c)
    track.clips[idx:idx + 1] = [left, right]
    return right.id


def _ramp_split(c: Clip, left: Clip, right: Clip, at: int) -> tuple[Clip, Clip]:
    """Sources are whole ms, so on a speed curve the rounded cut can make the halves 1 ms longer or shorter than asked:
    nudge the cut by a few ms until the left half ends at ``at`` and the right one where the clip ended."""
    base = int(round(c.src_at(at)))
    for d in (0, 1, -1, 2, -2, 3, -3, 4, -4):
        s = base + d
        if c.reverse:
            l2, r2 = left.model_copy(update={"src_in": s}), right.model_copy(update={"src_out": s})
        else:
            l2, r2 = left.model_copy(update={"src_out": s}), right.model_copy(update={"src_in": s})
        if l2.end == at and r2.end == c.end:
            return l2, r2
    right.start = left.end
    return left, right


def _split(ctx: Ctx, o: Split) -> dict:
    at = ctx.snap(_t(o.at))
    created: list[str] = []
    if o.clip:
        track, c = ctx.p.find(o.clip)
        _unlocked(track)
        new = _split_clip(track, c, at)
        if not new:
            raise LumiereError(f"{at} ms is not inside clip {o.clip} (or too close to an edge).")
        created.append(new)
    else:
        for track in ctx.p.tracks:
            if track.locked:
                continue
            for c in list(track.clips):
                new = _split_clip(track, c, at)
                if new:
                    created.append(new)
    return {"at": at, "new_clips": created}


def _trim(ctx: Ctx, o: Trim) -> dict:
    track, c = ctx.p.find(o.clip)
    _unlocked(track)
    old_end = c.end
    if c.type == "text" or (c.media and ctx.media(c.media).get("kind") == "image"):
        length = _t(o.length)
        if length is None:
            raise LumiereError("Text and image clips are trimmed with 'length'.")
        if c.type == "text":
            c.length = max(MIN_CLIP_MS, length)
        else:
            c.speed_keys = []
            c.src_out = c.src_in + int(max(MIN_CLIP_MS, length) * c.speed)
    else:
        info = ctx.media(c.media)
        dur = int(info.get("duration_ms") or 0)
        new_in = c.src_in if o.src_in is None else max(0, _t(o.src_in))
        new_out = c.src_out if o.src_out is None else _t(o.src_out)
        if dur:
            new_out = min(new_out, dur)
        if new_out - new_in < MIN_CLIP_MS:
            raise LumiereError("That trim leaves the clip empty.")
        c.src_in, c.src_out = new_in, new_out
    delta = c.end - old_end
    if o.ripple and delta:
        _shift(track, old_end, delta, exclude={c.id})
    elif delta > 0:
        _clear_range(track, old_end, c.end, keep={c.id})
    return {"clip": c.id, "start": c.start, "end": c.end, "src_in": c.src_in, "src_out": c.src_out}


def _move(ctx: Ctx, o: Move) -> dict:
    p = ctx.p
    track, c = p.find(o.clip)
    _unlocked(track)
    dest = p.track(o.track) if o.track else track
    _unlocked(dest)
    if (dest.kind == "text") != (c.type == "text"):
        raise LumiereError("Text clips stay on text tracks and media clips on video or audio tracks.")
    if dest.kind == "video" and c.media and not (ctx.media(c.media).get("has_video") or ctx.media(c.media).get("kind") == "image"):
        raise LumiereError("That media has no picture: it cannot go on a video track.")
    track.clips.remove(c)
    if o.start is not None:
        c.start = ctx.snap(max(0, _t(o.start)))
    c.transition_in = None if dest is not track else c.transition_in
    _clear_range(dest, c.start, c.end)
    dest.clips.append(c)
    return {"clip": c.id, "track": dest.id, "start": c.start, "end": c.end}


def _delete(ctx: Ctx, o: Delete) -> dict:
    removed = []
    for clip_id in o.clips:
        track, c = ctx.p.find(clip_id)
        _unlocked(track)
        track.clips.remove(c)
        removed.append(clip_id)
        if not o.ripple:
            continue
        following = sorted((x for x in track.clips if x.start >= c.start), key=lambda x: x.start)
        if not following:
            continue
        first = following[0]
        if first.start > c.end:
            continue  # a gap after the clip stays a gap
        if first.transition_in is not None and first.start < c.end:
            first.transition_in = None  # its transition was with the deleted clip
        prev_end = max((x.end for x in track.clips if x.start < c.start), default=c.start)
        target = max(c.start if prev_end <= c.start else prev_end, 0)
        _shift(track, first.start, target - first.start)
    return {"removed": removed}


def _delete_range(ctx: Ctx, o: DeleteRange) -> dict:
    a, b = ctx.snap(_t(o.start)), ctx.snap(_t(o.end))
    if b <= a:
        raise LumiereError("The range is empty (end must be after start).")
    for tid in o.tracks or []:
        ctx.p.track(tid)
    removed = delete_range(ctx.p, a, b, o.tracks, o.ripple)
    return {"removed_ms": removed, "duration": ctx.p.duration}


def _source_ranges(o_ranges: list[list[Time]]) -> list[tuple[int, int]]:
    out = []
    for r in o_ranges:
        if len(r) != 2:
            raise LumiereError("Each range is [from, to].")
        a, b = _t(r[0]), _t(r[1])
        if b > a:
            out.append((a, b))
    return out


def _cut_source(ctx: Ctx, o: CutSource) -> dict:
    ctx.media(o.media)
    ranges = _merge(_source_ranges(o.ranges), gap=o.min_gap)
    timeline = source_to_timeline(ctx.p, o.media, ranges)
    tracks = _scope_tracks(ctx.p, o.media, o.scope)
    total = 0
    for a, b in reversed(timeline):  # from the end, so earlier ranges keep their positions
        total += delete_range(ctx.p, a, b, tracks, o.ripple)
    return {"cuts": len(timeline), "removed_ms": total, "duration": ctx.p.duration}


def _keep_source(ctx: Ctx, o: KeepSource) -> dict:
    ctx.media(o.media)
    keep = _merge(_source_ranges(o.ranges))
    spans = [(c.src_in, c.src_out) for _, c in ctx.p.all_clips() if c.media == o.media]
    cut: list[tuple[int, int]] = []
    for lo, hi in spans:
        cursor = lo
        for a, b in keep:
            if b <= cursor or a >= hi:
                continue
            if a > cursor:
                cut.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < hi:
            cut.append((cursor, hi))
    if not cut:
        return {"cuts": 0, "removed_ms": 0, "duration": ctx.p.duration}
    return _cut_source(ctx, CutSource(op="cut_source", media=o.media, ranges=[list(r) for r in cut], scope=o.scope))


_SETTABLE = {"label", "mute", "volume_db", "fade_in", "fade_out", "audio_fade_in", "audio_fade_out", "transform", "crop", "text", "style", "color",
             "speed", "reverse", "audio_stream", "keyframes", "filters", "reframe", "transition_in", "start", "src_in", "src_out", "length",
             "speed_keys", "mask", "slot"}
_MERGED = {"transform": Transform, "crop": Crop, "style": TextStyle, "mask": Mask}


def _set(ctx: Ctx, o: SetClip) -> dict:
    track, c = ctx.p.find(o.clip)
    _unlocked(track)
    unknown = set(o.props) - _SETTABLE
    if unknown:
        raise LumiereError(f"Not settable: {', '.join(sorted(unknown))}. Settable: {', '.join(sorted(_SETTABLE))}.")
    old_end = c.end
    if o.props.get("slot"):
        taken = next((x for _, x in ctx.p.all_clips() if x.slot == o.props["slot"] and x.id != c.id), None)
        if taken is not None:
            raise LumiereError(f"The slot {o.props['slot']!r} is already on clip {taken.id}; a slot names one clip.")
    data = c.model_dump()
    for key, value in o.props.items():
        if key in _MERGED and isinstance(value, dict):
            base = data.get(key) or _MERGED[key]().model_dump()
            data[key] = {**base, **value}
        elif key in ("start", "src_in", "src_out", "length") and value is not None:
            data[key] = _t(value)
        else:
            data[key] = value
    try:
        new = Clip.model_validate(data)
    except ValidationError as error:
        issues = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'clip'}: {e['msg']}" for e in error.errors())
        raise LumiereError(issues) from error
    track.clips[track.clips.index(c)] = new
    delta = new.end - old_end
    if delta and o.ripple and "start" not in o.props:
        _shift(track, old_end, delta, exclude={new.id})
    return {"clip": new.id, "start": new.start, "end": new.end}


def _speed(ctx: Ctx, o: Speed) -> dict:
    """A constant speed; it replaces a speed curve if the clip had one."""
    return _set(ctx, SetClip(op="set", clip=o.clip, props={"speed": o.speed, "speed_keys": []}, ripple=o.ripple))


def _transition(ctx: Ctx, o: TransitionOp) -> dict:
    p = ctx.p
    targets: list[tuple[Track, Clip]] = []
    if o.all_cuts:
        track = p.track(o.track) if o.track else p.main_track()
        if track is None:
            raise LumiereError("No video track.")
        track.clips.sort(key=lambda c: c.start)
        targets = [(track, c) for c in track.clips[1:]]
    elif o.clip:
        targets = [p.find(o.clip)]
    else:
        raise LumiereError("Give 'clip' or all_cuts=true.")
    done = []
    for track, c in targets:
        _unlocked(track)
        track.clips.sort(key=lambda x: x.start)
        idx = track.clips.index(c)
        prev = track.clips[idx - 1] if idx > 0 else None
        had = c.transition_in.dur if c.transition_in else 0
        if o.type is None:
            if had and prev and prev.end > c.start:
                c.transition_in = None
                _shift(track, c.start, had)
            else:
                c.transition_in = None
            done.append(c.id)
            continue
        dur = o.dur
        if prev is None:
            # first clip: a fade from black instead of a transition
            c.fade_in = dur
            done.append(c.id)
            continue
        dur = min(dur, prev.duration - MIN_CLIP_MS, c.duration - MIN_CLIP_MS)
        if dur < MIN_CLIP_MS:
            continue
        gap = c.start - prev.end
        if had:
            # already overlapping: adjust to the new length
            _shift(track, c.start, had - dur)
        elif gap <= 1000 / p.canvas.fps + 1:
            _shift(track, c.start, -(dur + max(0, gap)))
        else:
            continue  # a real gap: no transition across it
        c.transition_in = Transition(type=o.type, dur=dur)
        done.append(c.id)
    return {"clips": done}


def _filter_add(ctx: Ctx, o: FilterAdd) -> dict:
    p = ctx.p
    clips: list[Clip] = []
    if o.clips:
        clips = [p.find(cid)[1] for cid in o.clips]
    elif o.track:
        clips = [c for c in p.track(o.track).clips if c.type != "text"]
    else:
        main = p.main_track()
        clips = [c for c in (main.clips if main else []) if c.type != "text"]
    from .render.filters import check_params  # lazy: render imports ops

    params = check_params(o.type, o.params)
    for c in clips:
        c.filters = [f for f in c.filters if f.type != o.type] + [Filter(type=o.type, params=params)]
    return {"clips": [c.id for c in clips], "filter": o.type}


def _filter_remove(ctx: Ctx, o: FilterRemove) -> dict:
    _, c = ctx.p.find(o.clip)
    before = len(c.filters)
    if o.index is not None:
        if not 0 <= o.index < before:
            raise LumiereError("No filter at that index.")
        c.filters.pop(o.index)
    elif o.type:
        c.filters = [f for f in c.filters if f.type != o.type]
    else:
        c.filters = []
    return {"clip": c.id, "removed": before - len(c.filters)}


def _track_add(ctx: Ctx, o: TrackAdd) -> dict:
    p = ctx.p
    if len(p.tracks) >= 24:
        raise LumiereError("24 tracks at most.")
    role = o.role or {"video": "overlay", "audio": "sfx", "text": "titles"}[o.kind]
    if role == "main" and p.main_track() and p.main_track().role == "main":
        raise LumiereError("There is already a main track.")
    t = Track(kind=o.kind, name=o.name or {"video": "Vídeo", "audio": "Audio", "text": "Textos"}[o.kind], role=role)
    if o.id:
        t.id = o.id
    p.tracks.insert(o.index if o.index is not None else len(p.tracks), t)
    return {"track": t.id}


def _track_set(ctx: Ctx, o: TrackSet) -> dict:
    t = ctx.p.track(o.track)
    allowed = {"name", "muted", "hidden", "locked", "volume_db", "role", "duck"}
    unknown = set(o.props) - allowed
    if unknown:
        raise LumiereError(f"Not settable on a track: {', '.join(sorted(unknown))}.")
    data = {**t.model_dump(), **o.props}
    new = Track.model_validate(data)
    ctx.p.tracks[ctx.p.tracks.index(t)] = new
    return {"track": new.id}


def _track_delete(ctx: Ctx, o: TrackDelete) -> dict:
    t = ctx.p.track(o.track)
    ctx.p.tracks.remove(t)
    return {"track": t.id, "removed_clips": len(t.clips)}


def _canvas(ctx: Ctx, o: CanvasOp) -> dict:
    data = ctx.p.canvas.model_dump()
    if o.preset:
        preset = PRESETS.get(o.preset)
        if not preset:
            raise LumiereError(f"Unknown preset {o.preset!r}. Known: {', '.join(PRESETS)}.")
        data.update({k: preset[k] for k in ("width", "height", "fps")})
    for key in ("width", "height", "fps", "background"):
        value = getattr(o, key)
        if value is not None:
            data[key] = value
    ctx.p.canvas = Canvas.model_validate(data)
    if o.length_mode:
        ctx.p.length_mode = o.length_mode
    if o.fit:
        for t in ctx.p.tracks:
            if t.kind == "video":
                for c in t.clips:
                    c.transform.fit = o.fit
    return {"canvas": ctx.p.canvas.model_dump(), "length_mode": ctx.p.length_mode}


def _marker_add(ctx: Ctx, o: MarkerAdd) -> dict:
    m = Marker(t=ctx.snap(_t(o.t)), label=o.label, color=o.color, kind=o.kind)
    ctx.p.markers.append(m)
    ctx.p.markers.sort(key=lambda m: m.t)
    return {"marker": m.id, "t": m.t}


def _marker_delete(ctx: Ctx, o: MarkerDelete) -> dict:
    before = len(ctx.p.markers)
    ctx.p.markers = [m for m in ctx.p.markers if not ((o.id and m.id == o.id) or (o.kind and m.kind == o.kind))]
    return {"removed": before - len(ctx.p.markers)}


def _captions(ctx: Ctx, o: CaptionsOp) -> dict:
    data = {**ctx.p.captions.model_dump(), **o.props}
    if o.enabled is not None:
        data["enabled"] = o.enabled
    if o.style is not None:
        data["style"] = o.style
    ctx.p.captions = Captions.model_validate(data)
    return {"captions": ctx.p.captions.model_dump()}


def _duplicate(ctx: Ctx, o: Duplicate) -> dict:
    track, c = ctx.p.find(o.clip)
    _unlocked(track)
    copy_ = c.model_copy(deep=True)
    copy_.id = new_id("clp")
    copy_.transition_in = None
    at = _t(o.at)
    copy_.start = c.end if at is None else ctx.snap(at)
    _shift(track, copy_.start, copy_.duration)
    track.clips.append(copy_)
    return {"clip": copy_.id, "start": copy_.start, "end": copy_.end}


def _detach_audio(ctx: Ctx, o: DetachAudio) -> dict:
    p = ctx.p
    track, c = p.find(o.clip)
    if c.type == "text" or track.kind != "video":
        raise LumiereError("Only media clips on a video track have audio to detach.")
    if not ctx.media(c.media).get("has_audio"):
        raise LumiereError("That media has no sound.")
    dest = p.track(o.track) if o.track else next((t for t in p.tracks if t.kind == "audio" and t.role == "voice" and not t.locked), None)
    if dest is None:
        dest = Track(kind="audio", name="Voz", role="voice")
        p.tracks.append(dest)
    audio = Clip(type=c.type, media=c.media, src_in=c.src_in, src_out=c.src_out, start=c.start, speed=c.speed, reverse=c.reverse,
                 speed_keys=[k.model_copy() for k in c.speed_keys], volume_db=c.volume_db,
                 audio_fade_in=c.audio_fade_in, audio_fade_out=c.audio_fade_out, audio_stream=c.audio_stream, label=c.label,
                 filters=[f for f in c.filters if f.type in _AUDIO_FILTER_TYPES])
    _clear_range(dest, audio.start, audio.end)
    dest.clips.append(audio)
    c.mute = True
    return {"clip": audio.id, "track": dest.id}


_AUDIO_FILTER_TYPES = {"audio_denoise", "voice_enhance", "highpass", "lowpass", "compressor", "pitch", "echo"}


def _close_gaps(ctx: Ctx, o: CloseGaps) -> dict:
    tracks = [ctx.p.track(o.track)] if o.track else [t for t in ctx.p.tracks if not t.locked]
    closed = 0
    for t in tracks:
        t.clips.sort(key=lambda c: c.start)
        cursor = 0
        for c in t.clips:
            target = cursor - (c.transition_in.dur if c.transition_in and cursor else 0)
            if c.start > target:
                closed += c.start - target
                c.start = max(0, target)
            cursor = max(cursor, c.end)
    return {"closed_ms": closed}


def _replace_media(ctx: Ctx, o: ReplaceMedia) -> dict:
    track, c = ctx.p.find(o.clip)
    info = ctx.media(o.media)
    if c.type == "text":
        raise LumiereError("Only media clips can change their media.")
    if info.get("kind") == "sequence":
        ctx.check_nesting(o.media)
    if c.multicam:
        raise LumiereError("This clip is an angle of a multicam group: change it with multicam_switch.")
    span = c.src_out - c.src_in
    dur = int(info.get("duration_ms") or 0)
    c.media = o.media
    c.type = "sequence" if info.get("kind") == "sequence" else "media"
    if info.get("kind") == "image":
        c.speed_keys = []
    if info.get("kind") == "image":
        c.src_in, c.src_out = 0, span
    else:
        c.src_in = min(c.src_in, max(0, dur - span)) if dur else c.src_in
        c.src_out = min(c.src_in + span, dur) if dur else c.src_in + span
    c.reframe = None
    c.label = str(info.get("name") or c.label)[:120]
    return {"clip": c.id, "src_in": c.src_in, "src_out": c.src_out}


def _sequence(ctx: Ctx, o: Sequence) -> dict:
    created = []
    for item in o.items:
        r = _add_media(ctx, AddMedia(op="add_media", media=item["media"], track=o.track, src_in=item.get("src_in"), src_out=item.get("src_out"),
                                     length=item.get("length"), label=item.get("label", "")))
        created.append(r["clip"])
        if not o.track:
            o.track = r["track"]
    return {"clips": created, "track": o.track}


def _keyframes(ctx: Ctx, o: KeyframesOp) -> dict:
    track, c = ctx.p.find(o.clip)
    _unlocked(track)
    # Keep ease domains on segments whose endpoints are unchanged; drop only affected ones
    # (MCP/UI may omit domain fields — restore from the previous clip keys when compatible).
    from .timeline import reconcile_key_domains
    keys = reconcile_key_domains(c.keyframes.get(o.prop), list(o.keys))
    if keys and o.prop in EQ_KEY_PROPS:
        if c.type == "text" or track.kind != "video":
            raise LumiereError(f"{o.prop} keys colour the picture of media clips on video tracks.")
        problem = eq_key_error(o.prop, keys)
        if problem:
            raise LumiereError(problem)
    if keys and o.prop in MASK_PROPS and c.mask is None:
        if c.type == "text" or track.kind != "video":
            raise LumiereError("Masks shape the picture of media clips on video tracks.")
        c.mask = Mask()  # animating a mask that is not there yet starts from the default ellipse
    if keys:
        c.keyframes[o.prop] = keys
    else:
        c.keyframes.pop(o.prop, None)
    return {"clip": c.id, "prop": o.prop, "keys": len(keys)}


def _slip(ctx: Ctx, o: Slip) -> dict:
    track, c = ctx.p.find(o.clip)
    _unlocked(track)
    if c.type == "text":
        raise LumiereError("Only media clips can slip.")
    info = ctx.media(c.media)
    if info.get("kind") == "image":
        raise LumiereError("A picture has nothing to slip.")
    dur = int(info.get("duration_ms") or 0)
    rate = (c.src_out - c.src_in) / max(1, c.duration)  # mean speed (a curve slides along with the content it shapes)
    delta = int(round(_t(o.delta) * rate))
    delta = max(-c.src_in, delta)
    if dur:
        delta = min(dur - c.src_out, delta)
    c.src_in += delta
    c.src_out += delta
    if c.speed_keys:
        c.speed_keys = [SpeedKey(t=max(0, k.t + delta), v=k.v, ease=k.ease) for k in c.speed_keys]
    return {"clip": c.id, "src_in": c.src_in, "src_out": c.src_out, "applied_ms": delta}


def _roll(ctx: Ctx, o: Roll) -> dict:
    track, right = ctx.p.find(o.clip)
    _unlocked(track)
    track.clips.sort(key=lambda c: c.start)
    idx = track.clips.index(right)
    if idx == 0:
        raise LumiereError("That clip has no clip before it on its track.")
    left = track.clips[idx - 1]
    if left.end != right.start and not right.transition_in:
        raise LumiereError("Roll needs two clips that touch.")
    delta = ctx.snap(_t(o.delta))
    lo = -(left.duration - MIN_CLIP_MS)
    hi = right.duration - MIN_CLIP_MS
    if left.type != "text" and (ctx.media(left.media).get("kind") != "image"):
        dur = int(ctx.media(left.media).get("duration_ms") or 0)
        if dur:
            room = (left.timeline_at(dur) - left.end) if left.has_ramp and not left.reverse else (dur - left.src_out) / left.speed
            hi = min(hi, int(room))
    if right.type != "text" and (ctx.media(right.media).get("kind") != "image"):
        room = (right.start - right.timeline_at(0)) if right.has_ramp and not right.reverse else right.src_in / right.speed
        lo = max(lo, -int(room))
    delta = max(lo, min(hi, delta))
    if left.type == "text":
        left.length += delta
    elif left.has_ramp and not left.reverse:
        left.src_out = int(round(left.src_at(left.end + delta)))  # through the speed curve
    else:
        left.src_out += int(round(delta * left.speed))
    if right.type == "text":
        right.length -= delta
    elif ctx.media(right.media).get("kind") == "image":
        right.src_out -= int(round(delta * right.speed))
    elif right.has_ramp and not right.reverse:
        right.src_in = int(round(right.src_at(right.start + delta)))
    else:
        right.src_in += int(round(delta * right.speed))
    right.start += delta
    return {"cut_at": right.start, "applied_ms": delta}


def _insert_clips(ctx: Ctx, o: InsertClips) -> dict:
    p = ctx.p
    clips = []
    for raw in o.clips:
        data = {k: v for k, v in raw.items() if k != "id"}
        try:
            c = Clip.model_validate(data)
        except ValidationError as error:
            raise LumiereError(f"Not a clip: {error.errors()[0]['msg']}") from error
        if c.media:
            info = ctx.media(c.media)
            if c.type == "sequence" or info.get("kind") == "sequence":
                ctx.check_nesting(c.media)
                c.type = "sequence"
        clips.append(c)
    first = min(c.start for c in clips)
    at = ctx.snap(_t(o.at))
    kind = "text" if clips[0].type == "text" else None
    if o.track:
        track = p.track(o.track)
    elif kind == "text":
        track = next((t for t in p.tracks if t.kind == "text" and not t.locked), None) or p.main_track()
    else:
        info = ctx.media(clips[0].media)
        track = _default_track(ctx, info)
    _unlocked(track)
    span = max(c.end for c in clips) - first
    if o.mode == "insert":
        hit = next((c for c in track.clips if c.start < at < c.end), None)
        if hit:
            _split_clip(track, hit, at)
        _shift(track, at, span)
    else:
        _clear_range(track, at, at + span)
    created = []
    for c in clips:
        if (track.kind == "text") != (c.type == "text"):
            raise LumiereError("Text clips go on text tracks and media clips on video or audio tracks.")
        c.start = at + (c.start - first)
        c.transition_in = None if c.start == at else c.transition_in
        track.clips.append(c)
        created.append(c.id)
    return {"clips": created, "track": track.id}


def _notes(ctx: Ctx, o: Notes) -> dict:
    ctx.p.notes = o.text
    return {"ok": True}


# ------------------------------------------------------------------ speed curves

def _distinct(keys: list[SpeedKey]) -> list[SpeedKey]:
    """Sorted keys with strictly increasing times (presets clamped at 0 can collide)."""
    out: list[SpeedKey] = []
    for k in sorted(keys, key=lambda k: k.t):
        if out and k.t <= out[-1].t:
            k = SpeedKey(t=out[-1].t + 1, v=k.v, ease=k.ease)
        out.append(k)
    return out


def ramp_preset(c: Clip, preset: str, speed: Optional[float] = None, at: Optional[int] = None, hold: Optional[int] = None,
                ramp: Optional[int] = None) -> list[SpeedKey]:
    """Speed keys for a named curve over the clip's source span (absolute source times)."""
    lo, hi = c.src_in, c.src_out
    span = hi - lo
    base = 1.0 if c.has_ramp else c.speed
    if preset == "clear":
        return []
    if preset in ("speed_up", "slow_down"):
        target = speed or (2.0 if preset == "speed_up" else 0.5)
        return [SpeedKey(t=lo, v=base, ease="ease_in_out"), SpeedKey(t=hi, v=target)]
    if preset == "ease_in_out":
        target = speed or 2.0
        return [SpeedKey(t=lo, v=base, ease="ease_in_out"), SpeedKey(t=int(lo + span * 0.3), v=target),
                SpeedKey(t=int(lo + span * 0.7), v=target, ease="ease_in_out"), SpeedKey(t=hi, v=base)]
    if preset == "hit":
        target = speed or 0.25
        centre = c.src_at(at) if at is not None else (lo + hi) / 2
        hold_ms = hold if hold is not None else min(600, span / 3)
        ramp_ms = ramp if ramp is not None else min(400, span / 4)
        a = centre - hold_ms / 2
        b = centre + hold_ms / 2
        return _distinct([SpeedKey(t=max(0, int(a - ramp_ms)), v=base, ease="ease_in_out"), SpeedKey(t=max(0, int(a)), v=target),
                          SpeedKey(t=max(0, int(b)), v=target, ease="ease_in_out"), SpeedKey(t=max(0, int(b + ramp_ms)), v=base)])
    raise LumiereError(f"Unknown speed curve {preset!r}. Known: {', '.join(RAMP_PRESETS)}.")


def _speed_ramp(ctx: Ctx, o: SpeedRamp) -> dict:
    track, c = ctx.p.find(o.clip)
    _unlocked(track)
    if c.type == "text" or ctx.media(c.media).get("kind") == "image":
        raise LumiereError("Speed curves are for video and audio clips (not titles or pictures).")
    if o.keys is None and o.preset is None:
        raise LumiereError("Give keys [{t, v, ease}] or a preset (" + ", ".join(RAMP_PRESETS) + ").")
    if o.keys is not None:
        offset = c.src_in if o.relative else 0
        keys = [SpeedKey(t=max(0, (_t(k.t) or 0) + offset), v=k.v, ease=k.ease) for k in o.keys]
        times = [k.t for k in keys]
        if len(set(times)) != len(times):
            raise LumiereError("Two speed keys at the same time.")
        keys.sort(key=lambda k: k.t)
    else:
        keys = ramp_preset(c, o.preset, o.speed, _t(o.at), _t(o.hold), _t(o.ramp))
    old_end = c.end
    data = c.model_dump()
    data["speed_keys"] = [k.model_dump() for k in keys]
    if not keys and o.preset == "clear":
        data["speed"] = 1.0 if c.has_ramp else c.speed
    try:
        new = Clip.model_validate(data)
    except ValidationError as error:
        raise LumiereError(f"That curve leaves the clip unusable: {error.errors()[0]['msg']}") from error
    track.clips[track.clips.index(c)] = new
    delta = new.end - old_end
    if o.ripple and delta:
        _shift(track, old_end, delta, exclude={new.id})
    elif delta > 0:
        _clear_range(track, old_end, new.end, keep={new.id})
    return {"clip": new.id, "start": new.start, "end": new.end, "duration": new.duration,
            "keys": [{"t": k.t - new.src_in, "v": k.v, "ease": k.ease} for k in new.speed_keys]}


# ------------------------------------------------------------------ masks

def _mask(ctx: Ctx, o: MaskOp) -> dict:
    ids = o.clips or ([o.clip] if o.clip else [])
    if not ids:
        raise LumiereError("Give 'clip' or 'clips'.")
    patch = {k: getattr(o, k) for k in ("shape", "x", "y", "w", "h", "radius", "feather", "invert", "enabled") if getattr(o, k) is not None}
    last: Optional[dict] = None
    for cid in ids:
        track, c = ctx.p.find(cid)
        _unlocked(track)
        if c.type == "text" or track.kind != "video":
            raise LumiereError("Masks shape the picture of media clips on video tracks.")
        if o.remove:
            c.mask = None
            c.keyframes = {k: v for k, v in c.keyframes.items() if k not in MASK_PROPS}
            continue
        base = c.mask.model_dump() if c.mask else Mask().model_dump()
        try:
            c.mask = Mask.model_validate({**base, **patch})
        except ValidationError as error:
            raise LumiereError(f"mask: {error.errors()[0]['msg']}") from error
        last = c.mask.model_dump()
    return {"clips": ids, "mask": last}


# ------------------------------------------------------------------ nested sequences

def _add_sequence(ctx: Ctx, o: AddSequence) -> dict:
    ctx.check_nesting(o.project)
    return _add_media(ctx, AddMedia(op="add_media", media=o.project, track=o.track, at=o.at, src_in=o.src_in, src_out=o.src_out, mode=o.mode,
                                    label=o.label))


def _outer_changes(c: Clip) -> list[str]:
    """What a sequence clip does to the whole nested picture / sound (lost when its clips come back one by one)."""
    dropped = []
    if c.filters:
        dropped.append("effects")
    if c.mask:
        dropped.append("mask")
    if c.keyframes:
        dropped.append("keyframes")
    if c.transform != Transform(fit=c.transform.fit) or c.crop != Crop():
        dropped.append("position / scale / crop")
    if c.fade_in or c.fade_out or c.audio_fade_in or c.audio_fade_out:
        dropped.append("fades")
    if abs(c.volume_db) > 0.01 or c.mute:
        dropped.append("volume")
    return dropped


def _unnest(ctx: Ctx, o: Unnest) -> dict:
    """Put the clips of a nested project back on this timeline in place of its sequence clip."""
    p = ctx.p
    track, c = p.find(o.clip)
    _unlocked(track)
    if c.type != "sequence":
        raise LumiereError(f"Clip {c.id} is not a nested sequence.")
    if track.kind != "video":
        raise LumiereError("Un-nesting works on sequences on video tracks.")
    if c.has_ramp or abs(c.speed - 1) > 1e-6 or c.reverse:
        raise LumiereError("Set the sequence back to speed 1 (no curve, not reversed) before un-nesting: its clips cannot keep that change.")
    nested = ctx.docs(c.media) if ctx.docs else None
    if nested is None:
        raise NotFound(f"No project {c.media} to un-nest.")
    inner = clone(nested)
    for t in inner.tracks:
        t.locked = False
    end_all = max(inner.content_end, inner.duration) + 1
    if c.src_out < end_all:
        delete_range(inner, c.src_out, end_all, None, True)
    if c.src_in > 0:
        delete_range(inner, 0, c.src_in, None, True)
    span0, span1 = c.start, c.end
    track.clips.remove(c)
    used: set[str] = {track.id}

    def free(t: Track) -> bool:
        return not t.locked and t.id not in used and not any(x.start < span1 and x.end > span0 for x in t.clips)

    def place(nt: Track, after: Optional[Track]) -> Track:
        """The parent track for one nested track: the next track of its kind (above ``after``) that is empty over the
        span, or a new one, so nothing already on this timeline is overwritten."""
        start_at = p.tracks.index(after) + 1 if after is not None else 0
        for t in p.tracks[start_at:]:
            if t.kind == nt.kind and free(t):
                return t
        if len(p.tracks) >= 24:
            raise LumiereError("24 tracks at most: no room for the nested tracks.")
        role = {"video": "overlay", "text": "titles"}.get(nt.kind) or (nt.role if nt.role in ("voice", "music", "sfx") else "sfx")
        new = Track(kind=nt.kind, name=nt.name or {"video": "Vídeo", "audio": "Audio", "text": "Textos"}[nt.kind], role=role, duck=nt.duck)
        same = [i for i, t in enumerate(p.tracks) if t.kind == nt.kind]
        p.tracks.insert((p.tracks.index(after) + 1) if after is not None else ((same[-1] + 1) if same else len(p.tracks)), new)
        return new

    created: list[str] = []
    last: dict[str, Optional[Track]] = {"video": None, "audio": None, "text": None}
    for nt in inner.tracks:
        if not nt.clips or (nt.hidden and nt.kind != "audio"):
            continue
        dest = track if nt.kind == "video" and last["video"] is None else place(nt, last[nt.kind])
        used.add(dest.id)
        last[nt.kind] = dest
        for x in sorted(nt.clips, key=lambda x: x.start):
            if span0 + x.start >= span1:
                continue
            copy_ = x.model_copy(deep=True)
            copy_.id = new_id("clp")
            copy_.start = span0 + x.start
            if nt.muted:
                copy_.mute = True
            if abs(nt.volume_db) > 0.01:
                copy_.volume_db = max(-60.0, min(24.0, copy_.volume_db + nt.volume_db))
            if dest is track and copy_.start == span0 and c.transition_in:
                copy_.transition_in = c.transition_in  # the transition into the sequence now leads into its first clip
            dest.clips.append(copy_)
            created.append(copy_.id)
    return {"clips": created, "tracks": sorted(used), "dropped": _outer_changes(c), "from": c.media}


def nest_plan(p: Project, clip_ids: list[str]) -> tuple[Project, dict[str, Any]]:
    """The project that will hold the selected clips (times from the start of the selection, same canvas) and where the
    sequence clip that replaces them goes: the lowest video track of the selection (or its lowest audio track)."""
    if not clip_ids:
        raise LumiereError("Select the clips to nest.")
    found = [p.find(cid) for cid in dict.fromkeys(clip_ids)]
    for t, _ in found:
        _unlocked(t)
    start = min(c.start for _, c in found)
    end = max(c.end for _, c in found)
    order = {t.id: i for i, t in enumerate(p.tracks)}
    involved = sorted({t.id: t for t, _ in found}.values(), key=lambda t: order[t.id])
    dest = next((t for t in involved if t.kind == "video"), None) or next((t for t in involved if t.kind == "audio"), None)
    if dest is None:
        raise LumiereError("Select at least one video or audio clip (titles alone cannot be a sequence).")
    chosen = {c.id for _, c in found}
    first_on_dest = min((c for t, c in found if t is dest), key=lambda c: c.start)
    lead = first_on_dest.transition_in.dur if first_on_dest.transition_in and first_on_dest.start == start else 0
    for c in dest.clips:
        if c.id in chosen or c.end <= start or c.start >= end:
            continue
        if c.start < start and c.end - start <= lead + 1:
            continue  # the clip before, overlapping by the transition into the selection
        if c.start >= start and c.transition_in and end - c.start <= c.transition_in.dur + 1:
            continue  # the clip after, overlapping by its transition
        raise LumiereError(f"Clip {c.id} on track {dest.name or dest.id} is partly inside the selected span: select it too "
                           "(the sequence takes the whole span).")
    nested = Project(canvas=p.canvas.model_copy(), tracks=[], length_mode="longest")
    for t in involved:
        nt = Track(kind=t.kind, name=t.name, role=t.role, duck=t.duck)
        for c in sorted((c for tt, c in found if tt is t), key=lambda c: c.start):
            copy_ = c.model_copy(deep=True)
            copy_.start = c.start - start
            if copy_.transition_in and not any(o.id in chosen and o.end > c.start and o.start < c.start for o in t.clips if o.id != c.id):
                copy_.transition_in = None  # its transition was with a clip that stays outside
            nt.clips.append(copy_)
        nested.tracks.append(nt)
    keep_transition = first_on_dest.transition_in if lead else None
    if keep_transition and not any(o.id not in chosen and o.end > first_on_dest.start and o.start < first_on_dest.start for o in dest.clips):
        keep_transition = None
    return nested, {"start": start, "end": end, "length": end - start, "track": dest.id, "transition_in": keep_transition}


# ------------------------------------------------------------------ multicam

def _angle_ref(g: Multicam, ref: Any, *, video: bool = False) -> int:
    """Index of an angle given as a media id, a label (any case) or a 1-based number."""
    idx: Optional[int] = None
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        n = int(ref) - 1
        idx = n if 0 <= n < len(g.angles) else None
    else:
        low = str(ref).strip().lower()
        for i, a in enumerate(g.angles):
            if a.media == ref or a.label.lower() == low:
                idx = i
                break
    if idx is None:
        raise LumiereError(f"No angle {ref!r} in {g.name}. Angles: " + ", ".join(f"{i + 1} {a.label} ({a.media})" for i, a in enumerate(g.angles)) + ".")
    if video and g.angles[idx].audio_only:
        raise LumiereError(f"{g.angles[idx].label} has no picture: it can be the sound but not the angle shown.")
    return idx


def _group_for(p: Project, group: Optional[str], clip: Optional[str]) -> Multicam:
    if group:
        return p.multicam(group)
    if clip:
        gid = p.find(clip)[1].multicam
        if not gid:
            raise LumiereError(f"Clip {clip} is not part of a multicam group.")
        return p.multicam(gid)
    if len(p.multicams) == 1:
        return p.multicams[0]
    if not p.multicams:
        raise LumiereError("This project has no multicam group: create one with multicam_create.")
    raise LumiereError("Several multicam groups: say which with 'group' (" + ", ".join(g.id for g in p.multicams) + ").")


def _group_track(p: Project, g: Multicam) -> Track:
    for t in p.tracks:
        if t.kind == "video" and any(c.multicam == g.id for c in t.clips):
            return t
    raise LumiereError(f"{g.name} has no picture on the timeline any more.")


def _angle_label(i: int, audio_only: bool) -> str:
    return f"Micrófono {i + 1}" if audio_only else f"Cámara {chr(65 + i) if i < 26 else i + 1}"


def _multicam_create(ctx: Ctx, o: MulticamCreate) -> dict:
    p = ctx.p
    angles: list[Angle] = []
    for i, raw in enumerate(o.angles):
        if "media" not in raw:
            raise LumiereError("Each angle needs a media id: {media, start?, label?}.")
        info = ctx.media(str(raw["media"]))
        if info.get("kind") == "image":
            raise LumiereError(f"{info.get('name')} is a picture: angles are recordings.")
        audio_only = not info.get("has_video")
        if audio_only and not info.get("has_audio"):
            raise LumiereError(f"{info.get('name')} has neither picture nor sound.")
        conf = raw.get("confidence")
        angles.append(Angle(media=str(raw["media"]), start=int(round(_t(raw.get("start")) or 0)), audio_only=audio_only,
                            label=str(raw.get("label") or _angle_label(i, audio_only))[:60], confidence=None if conf is None else float(conf)))
    g = Multicam(name=o.name, angles=angles)
    if o.id:
        g.id = o.id
    sound = [i for i, a in enumerate(angles) if ctx.media(a.media).get("has_audio")]
    if o.master is not None:
        g.master = _angle_ref(g, o.master)
    elif sound:
        g.master = next((i for i in sound if angles[i].audio_only), sound[0])
    if not ctx.media(angles[g.master].media).get("has_audio"):
        raise LumiereError(f"{angles[g.master].label} has no sound to use as the master.")
    first = _angle_ref(g, o.angle, video=True) if o.angle is not None else next((i for i, a in enumerate(angles) if not a.audio_only), None)
    if first is None:
        raise LumiereError("A multicam group needs at least one angle with picture.")
    lo = max(a.start for a in angles)
    hi = min(a.start + int(ctx.media(a.media).get("duration_ms") or 0) for a in angles)
    if o.from_ms is not None:
        lo = max(lo, _t(o.from_ms))
    if o.to_ms is not None:
        hi = min(hi, _t(o.to_ms))
    if hi - lo < 200:
        raise LumiereError(f"The recordings barely overlap once synced ({max(0, hi - lo)} ms): check their offsets with multicam_sync.")
    track = p.main_track()
    if track is None:
        track = Track(kind="video", name="Principal", role="main")
        p.tracks.insert(0, track)
    _unlocked(track)
    a0 = angles[first]
    start = _track_end(track) if o.at is None else ctx.snap(_t(o.at))
    clip = Clip(media=a0.media, src_in=lo - a0.start, src_out=hi - a0.start, start=start, mute=True, multicam=g.id, label=a0.label)
    info0 = ctx.media(a0.media)
    same_orientation = ((info0.get("width") or 16) >= (info0.get("height") or 9)) == (p.canvas.width >= p.canvas.height)
    clip.transform.fit = "cover" if same_orientation else "contain"
    if o.at is not None:
        _clear_range(track, clip.start, clip.end)
    track.clips.append(clip)
    master = angles[g.master]
    sound_track = Track(kind="audio", name=f"Audio · {o.name}"[:60], role="voice")
    p.tracks.append(sound_track)
    sound_clip = Clip(media=master.media, src_in=lo - master.start, src_out=hi - master.start, start=clip.start, multicam=g.id, label=master.label)
    sound_track.clips.append(sound_clip)
    p.multicams.append(g)
    return {"group": g.id, "clip": clip.id, "audio_clip": sound_clip.id, "track": track.id, "audio_track": sound_track.id, "start": clip.start,
            "end": clip.end, "group_range": [lo, hi], "angles": [a.label for a in angles]}


def _mc_repoint(ctx: Ctx, g: Multicam, c: Clip, new: int) -> bool:
    """Show angle ``new`` in clip ``c`` (same timeline span, same moment of the event). False when it already does."""
    old = g.angle_index(c.media)
    if old == new:
        return False
    if c.reverse:
        raise LumiereError("Reversed clips cannot change angle.")
    a, b = g.angles[new], g.angles[old]
    gt0, gt1 = c.src_in + b.start, c.src_out + b.start
    lo, hi = gt0 - a.start, gt1 - a.start
    dur = int(ctx.media(a.media).get("duration_ms") or 0)
    if lo < -MIN_CLIP_MS or (dur and hi > dur + MIN_CLIP_MS):
        raise LumiereError(f"{a.label} has no picture between group time {gt0} and {gt1} ms (it covers {a.start}–{a.start + dur} ms).")
    c.media, c.src_in, c.src_out = a.media, max(0, lo), min(hi, dur) if dur else hi
    c.label = a.label
    c.reframe = None
    return True


def _mc_merge(track: Track, gid: str) -> None:
    """Join neighbouring shots of the same angle that continue each other (a switch to the angle already shown leaves no seam)."""
    track.clips.sort(key=lambda c: c.start)
    out: list[Clip] = []
    for c in track.clips:
        prev = out[-1] if out else None
        if (prev and c.multicam == gid and prev.multicam == gid and prev.media == c.media and abs(prev.end - c.start) <= 1
                and abs(prev.src_out - c.src_in) <= 2 and prev.speed == c.speed and not c.reverse and not prev.reverse
                and not c.transition_in and not prev.fade_out and not c.fade_in and prev.transform == c.transform
                and prev.filters == c.filters and not prev.keyframes and not c.keyframes):
            prev.src_out = c.src_out
            continue
        out.append(c)
    track.clips = out


def _mc_set_range(ctx: Ctx, g: Multicam, t0: int, t1: int, angle: int) -> int:
    track = _group_track(ctx.p, g)
    _unlocked(track)
    mine = [c for c in track.clips if c.multicam == g.id]
    lo, hi = min(c.start for c in mine), max(c.end for c in mine)
    t0, t1 = max(t0, lo), min(t1, hi)
    if t1 - t0 < MIN_CLIP_MS // 2:
        return 0
    for at in (t0, t1):
        hit = next((c for c in track.clips if c.multicam == g.id and c.start < at < c.end), None)
        if hit:
            _split_clip(track, hit, at)
    changed = 0
    tol = MIN_CLIP_MS // 2
    for c in sorted(track.clips, key=lambda c: c.start):
        if c.multicam == g.id and c.start >= t0 - tol and c.end <= t1 + tol:
            changed += _mc_repoint(ctx, g, c, angle)
    _mc_merge(track, g.id)
    return changed


def _multicam_switch(ctx: Ctx, o: MulticamSwitch) -> dict:
    p = ctx.p
    g = _group_for(p, o.group, o.clip)
    if o.cuts:
        pts = sorted(((ctx.snap(_t(c[0])), _angle_ref(g, c[1], video=True)) for c in o.cuts if len(c) == 2), key=lambda x: x[0])
        if not pts:
            raise LumiereError("cuts is [[timeline_ms, angle], ...].")
        track = _group_track(p, g)
        end = max(c.end for c in track.clips if c.multicam == g.id)
        changed = 0
        for (t, ai), nxt in zip(pts, [x[0] for x in pts[1:]] + [end]):
            changed += _mc_set_range(ctx, g, t, nxt, ai)
        return {"group": g.id, "switches": len(pts), "changed_shots": changed}
    if o.angle is None:
        raise LumiereError("Say which angle: angle (media id, label or 1-based number).")
    ai = _angle_ref(g, o.angle, video=True)
    track = _group_track(p, g)
    if o.at is not None:
        t0 = ctx.snap(_t(o.at))
        shot = next((c for c in track.clips if c.multicam == g.id and c.start <= t0 < c.end), None)
        if shot is None:
            raise LumiereError(f"{t0} ms is outside the group's picture.")
        t1 = ctx.snap(_t(o.end)) if o.end is not None else shot.end
    elif o.clip:
        _, shot = p.find(o.clip)
        t0, t1 = shot.start, shot.end if o.end is None else ctx.snap(_t(o.end))
    else:
        raise LumiereError("Say where: 'at' (timeline ms, usually the playhead) or 'clip'.")
    changed = _mc_set_range(ctx, g, t0, t1, ai)
    return {"group": g.id, "angle": g.angles[ai].label, "from": t0, "to": t1, "changed_shots": changed}


def _multicam_set(ctx: Ctx, o: MulticamSet) -> dict:
    p = ctx.p
    g = _group_for(p, o.group, None)
    if o.release:
        for _, c in p.all_clips():
            if c.multicam == g.id:
                c.multicam = None
        p.multicams.remove(g)
        return {"group": g.id, "released": True}
    out: dict[str, Any] = {"group": g.id}
    if o.name is not None:
        g.name = o.name
    for key, value in (o.offsets or {}).items():
        i = _angle_ref(g, key)
        new = int(round(_t(value)))
        delta = new - g.angles[i].start
        if not delta:
            continue
        for _, c in p.all_clips():
            if c.multicam == g.id and c.media == g.angles[i].media:
                dur = int(ctx.media(c.media).get("duration_ms") or 0)
                if c.src_in - delta < 0 or (dur and c.src_out - delta > dur + MIN_CLIP_MS):
                    raise LumiereError(f"Moving {g.angles[i].label} by {-delta} ms leaves a clip outside its recording ({dur} ms long).")
        for _, c in p.all_clips():
            if c.multicam == g.id and c.media == g.angles[i].media:
                c.src_in -= delta
                c.src_out -= delta
        g.angles[i].start = new
        g.angles[i].confidence = None
        out.setdefault("moved", {})[g.angles[i].label] = new
    if o.master is not None:
        i = _angle_ref(g, o.master)
        if not ctx.media(g.angles[i].media).get("has_audio"):
            raise LumiereError(f"{g.angles[i].label} has no sound.")
        if i != g.master:
            old = g.angles[g.master]
            new_master = g.angles[i]
            dur = int(ctx.media(new_master.media).get("duration_ms") or 0)
            for t, c in p.all_clips():
                if c.multicam == g.id and t.kind == "audio" and c.media == old.media:
                    lo, hi = c.src_in + old.start - new_master.start, c.src_out + old.start - new_master.start
                    if lo < -MIN_CLIP_MS or (dur and hi > dur + MIN_CLIP_MS):
                        raise LumiereError(f"{new_master.label} has no sound for the whole stretch (it covers {new_master.start}–{new_master.start + dur} ms).")
                    c.media, c.src_in, c.src_out, c.label = new_master.media, max(0, lo), min(hi, dur) if dur else hi, new_master.label
            g.master = i
        out["master"] = g.angles[i].label
    return out


def _prune_multicams(p: Project) -> None:
    """A group whose clips are all gone leaves nothing behind."""
    alive = {c.multicam for _, c in p.all_clips() if c.multicam}
    p.multicams = [g for g in p.multicams if g.id in alive]


def _overlay_track(ctx: Ctx, wanted: Optional[str], start: int, end: int) -> Track:
    """The video track an overlay goes on: the one asked for, else the first non-main video track with nothing in [start, end),
    else a new 'B-roll' track on top of the others."""
    p = ctx.p
    if wanted:
        t = p.track(wanted)
        if t.kind != "video":
            raise LumiereError("An overlay goes on a video track.")
        _unlocked(t)
        return t
    main = p.main_track()
    for t in p.tracks:
        if t.kind == "video" and t is not main and t.role != "main" and not t.locked and not any(c.start < end and c.end > start for c in t.clips):
            return t
    if len(p.tracks) >= 24:
        raise LumiereError("24 tracks at most.")
    t = Track(kind="video", name="B-roll", role="overlay")
    p.tracks.append(t)
    return t


def _add_overlay(ctx: Ctx, o: AddOverlay) -> dict:
    info = ctx.media(o.media)
    if not (info.get("has_video") or info.get("kind") == "image"):
        raise LumiereError(f"{info.get('name')} has no picture: it cannot be an overlay.")
    start = ctx.snap(max(0, _t(o.start)))
    length = int(_t(o.length) or 0)
    if length < MIN_CLIP_MS:
        raise LumiereError("An overlay needs a length of at least 40 ms.")
    if info.get("kind") == "image":
        a, b = 0, length
    else:
        a = max(0, _t(o.src_in) or 0)
        dur = int(info.get("duration_ms") or 0)
        b = a + (min(length, dur - a) if dur else length)
        if b - a < MIN_CLIP_MS:
            raise LumiereError(f"Nothing left of {info.get('name')} after {a} ms for an overlay.")
    track = _overlay_track(ctx, o.track, start, start + (b - a))
    clip = Clip(media=o.media, start=start, src_in=a, src_out=b, mute=o.mute, label=str(info.get("name") or "")[:120], fade_in=o.fade_in, fade_out=o.fade_out)
    clip.transform.fit = o.fit
    _clear_range(track, clip.start, clip.end)
    track.clips.append(clip)
    return {"clip": clip.id, "track": track.id, "start": clip.start, "end": clip.end, "short_by": max(0, length - (b - a))}


def _slot_clip(p: Project, slot: str) -> tuple[Track, Clip]:
    hits = sorted(((t, c) for t, c in p.all_clips() if c.slot == slot), key=lambda tc: tc[1].start)
    if not hits:
        known = sorted({c.slot for _, c in p.all_clips() if c.slot})
        raise NotFound(f"No clip has the slot {slot!r}. " + (f"Slots here: {', '.join(known)}." if known else "This project has no slots (mark a clip with set {props: {slot: 'name'}})."))
    return hits[0]


def _fill_slot(ctx: Ctx, o: FillSlot) -> dict:
    """Replace the media of the clip that carries a slot, keeping its look (effects, transform, fades, speed). The new clip takes the
    slot's length (``keep``: shorter if the media is) or the media's whole length (``full``, the rule for slots named main*); on the
    main track, and whenever the length changes under ``full``, the clips and markers that follow move with it, and background
    music that ran to the end of the edit is stretched to the new end when its file is long enough."""
    p = ctx.p
    track, c = _slot_clip(p, o.slot)
    _unlocked(track)
    if c.type != "media":
        raise LumiereError(f"The slot {o.slot!r} is a title: change its text with set {{props: {{text: ...}}}}.")
    info = ctx.media(o.media)
    if track.kind == "video" and not (info.get("has_video") or info.get("kind") == "image"):
        raise LumiereError(f"{info.get('name')} has no picture: the slot {o.slot!r} is on a video track.")
    if track.kind == "audio" and not info.get("has_audio"):
        raise LumiereError(f"{info.get('name')} has no sound: the slot {o.slot!r} is on an audio track.")
    full = o.rule == "full" or (o.rule == "auto" and o.slot.lower().startswith("main"))
    old_end, old_total = c.end, p.duration
    want = _t(o.length)
    if info.get("kind") == "image":
        span = int(round((want if want is not None else (c.duration if not full else 4000)) * c.speed))
        a, b = 0, span
    else:
        dur = int(info.get("duration_ms") or 0)
        a = max(0, _t(o.src_in) or 0)
        if o.src_out is not None:
            b = _t(o.src_out)
        elif full and want is None:
            b = dur
        else:
            b = a + int(round((want if want is not None else c.duration) * c.speed))
        if dur:
            b = min(b, dur)
        if b - a < MIN_CLIP_MS:
            raise LumiereError(f"Empty or too short source range [{a}, {b}] for the {dur} ms media {info.get('name')}.")
    c.media, c.src_in, c.src_out, c.reframe, c.audio_stream = o.media, a, b, None, 0
    c.label = str(info.get("name") or c.label)[:120]
    delta = c.end - old_end
    if delta and (full or track.role == "main"):
        for t in p.tracks:
            _shift(t, old_end, delta, exclude={c.id})
        for m in p.markers:
            if m.t >= old_end:
                m.t = max(0, m.t + delta)
        _stretch_music(ctx, old_total)
    return {"clip": c.id, "slot": o.slot, "media": o.media, "start": c.start, "end": c.end, "delta_ms": delta, "rule": "full" if full else "keep"}


def _stretch_music(ctx: Ctx, old_total: int) -> None:
    """After the main track changed length: music that ran to the old end follows the new end (never past its file)."""
    p = ctx.p
    main = p.main_track()
    new_total = max((x.end for x in main.clips), default=0) if main else 0
    for t in p.tracks:
        if t.kind != "audio" or t.role != "music":
            continue
        for m in t.clips:
            if m.type != "media" or m.end < old_total - 60 or m.end >= new_total:
                continue
            dur = int((ctx.media_lookup(m.media) or {}).get("duration_ms") or 0)
            m.src_out = min(dur, m.src_out + int(round((new_total - m.end) * m.speed))) if dur else m.src_out


HANDLERS: dict[str, Callable[[Ctx, Any], dict]] = {
    "add_media": _add_media, "add_text": _add_text, "split": _split, "trim": _trim, "move": _move, "delete": _delete,
    "delete_range": _delete_range, "cut_source": _cut_source, "keep_source": _keep_source, "set": _set, "speed": _speed,
    "transition": _transition, "filter_add": _filter_add, "filter_remove": _filter_remove, "track_add": _track_add, "track_set": _track_set,
    "track_delete": _track_delete, "canvas": _canvas, "marker_add": _marker_add, "marker_delete": _marker_delete, "captions": _captions,
    "duplicate": _duplicate, "detach_audio": _detach_audio, "close_gaps": _close_gaps, "replace_media": _replace_media,
    "sequence": _sequence, "keyframes": _keyframes, "slip": _slip, "roll": _roll, "insert_clips": _insert_clips, "notes": _notes,
    "speed_ramp": _speed_ramp, "add_sequence": _add_sequence, "unnest": _unnest, "mask": _mask,
    "multicam_create": _multicam_create, "multicam_switch": _multicam_switch, "multicam_set": _multicam_set,
    "add_overlay": _add_overlay, "fill_slot": _fill_slot,
}


def apply_ops(project: Project, ops: list[dict[str, Any]], media: MediaLookup, *, project_id: Optional[str] = None,
              docs: Optional[DocLookup] = None) -> tuple[Project, list[dict[str, Any]]]:
    """Apply ``ops`` in order to a copy of ``project``. All or nothing: the first failure raises and the original is untouched.
    ``project_id`` (the project being edited) lets nesting refuse loops; ``docs`` reads nested projects for un-nesting."""
    work = clone(project)
    ctx = Ctx(work, media, project_id, docs)
    results: list[dict[str, Any]] = []
    for index, raw in enumerate(ops):
        op = parse_op(raw)
        try:
            result = HANDLERS[op.op](ctx, op)
        except (LumiereError, NotFound) as error:
            prefix = f"Step {index + 1} ({op.op}): " if len(ops) > 1 else ""
            raise type(error)(prefix + str(error)) from error
        work.sort()
        results.append({"op": op.op, **result})
    _prune_multicams(work)
    try:
        final = Project.model_validate(work.dump())
    except ValidationError as error:
        issues = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()[:5])
        raise LumiereError(f"The result is not a valid project: {issues}") from error
    return final, results
