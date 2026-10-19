"""Several canvases from one project in one render job (16:9 and 9:16 and 1:1...).

The project is never changed: every output is rendered from a copy whose canvas and clip framing were adapted. What the
person chooses is the *reframe* for the outputs whose shape differs from the project's:

* ``auto``   — the camera path the clip already has (from a previous reframe) when there is one, else a path from the
  cached subject analysis when it exists, else the centre of the picture; the picture fills the frame (fit = cover);
* ``center`` — fill the frame and keep the centre of the picture;
* ``blur``   — the whole picture fitted inside the frame over a blurred fill, nothing is cropped.

Outputs that have the project's own shape are rendered as the project is (only scaled).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from .. import media as media_store
from ..analysis import video as video_an
from ..errors import LumiereError
from ..ops import PRESETS
from ..timeline import Project, Reframe, clone

if TYPE_CHECKING:
    from ..services import Services

MAX_FORMATS = 6
REFRAMES = ("auto", "center", "blur")
# the size an aspect ratio gets when only the ratio is given
ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350), "5:4": (1350, 1080), "4:3": (1440, 1080),
    "3:4": (1080, 1440), "21:9": (1920, 816), "2:3": (1080, 1620), "3:2": (1620, 1080),
}


@dataclass(frozen=True)
class Format:
    key: str          # used in file names: 16x9, 9x16, 1x1 or 1280x720
    label: str        # 16:9 · 1920×1080
    width: int
    height: int
    reframe: str = "auto"


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _ratio(w: int, h: int) -> str:
    g = math.gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def _one(item: Any, default_reframe: str) -> Format:
    reframe = default_reframe
    raw: Any = item
    width = height = 0
    if isinstance(item, dict):
        reframe = str(item.get("reframe") or default_reframe)
        if item.get("width") and item.get("height"):
            width, height = int(item["width"]), int(item["height"])
        raw = item.get("aspect") or item.get("preset") or item.get("format") or ""
        if not raw and not width:
            raise LumiereError("Each format needs an aspect ('9:16'), a preset ('reels') or width and height.", code="bad_format")
    if reframe not in REFRAMES:
        raise LumiereError(f"reframe must be one of {', '.join(REFRAMES)} (got {reframe!r}).", code="bad_format")
    if not width:
        text = str(raw).strip().lower()
        m = re.fullmatch(r"(\d{2,5})\s*[x×]\s*(\d{2,5})", text)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
        elif text in PRESETS:
            width, height = PRESETS[text]["width"], PRESETS[text]["height"]
        else:
            try:
                r = video_an.aspect_of(text.replace("x", ":") if re.fullmatch(r"\d+x\d+", text) else text)
            except ValueError as error:
                raise LumiereError(f"{raw!r} is not a format: use an aspect (16:9, 9:16, 1:1, 4:5), a size (1280x720) or a preset "
                                   f"({', '.join(PRESETS)}).", code="bad_format") from error
            std = next((s for a, s in ASPECT_SIZES.items() if abs(r - int(a.split(":")[0]) / int(a.split(":")[1])) < 0.005), None)
            if std:
                width, height = std
            else:
                height = 1920 if r < 1 else 1080
                width = _even(height * r)
    width, height = _even(width), _even(height)
    if not (64 <= width <= 7680 and 64 <= height <= 7680):
        raise LumiereError(f"{width}x{height} is outside 64..7680.", code="bad_format")
    ratio = _ratio(width, height)
    standard = ASPECT_SIZES.get(ratio) == (width, height)
    key = ratio.replace(":", "x") if standard else f"{width}x{height}"
    return Format(key=key, label=f"{ratio if standard else _ratio(width, height)} · {width}×{height}", width=width, height=height, reframe=reframe)


def parse_formats(raw: list[Any], default_reframe: str = "auto") -> list[Format]:
    """Normalise what a person or an assistant sends: ['16:9', '9:16', {aspect: '1:1', reframe: 'blur'}, 'reels', '1280x720']."""
    if default_reframe not in REFRAMES:
        raise LumiereError(f"reframe must be one of {', '.join(REFRAMES)} (got {default_reframe!r}).", code="bad_format")
    if not raw:
        raise LumiereError("formats is empty.", code="bad_format")
    if len(raw) > MAX_FORMATS:
        raise LumiereError(f"At most {MAX_FORMATS} formats in one export.", code="bad_format")
    out: list[Format] = []
    for item in raw:
        fmt = _one(item, default_reframe)
        if any((f.width, f.height) == (fmt.width, fmt.height) for f in out):
            raise LumiereError(f"{fmt.width}x{fmt.height} is listed twice.", code="bad_format")
        out.append(fmt)
    return out


def formats_info(raw: list[Any], reframe: str = "auto") -> list[dict[str, Any]]:
    return [{"key": f.key, "label": f.label, "width": f.width, "height": f.height, "reframe": f.reframe} for f in parse_formats(raw, reframe)]


# ---------------------------------------------------------------- the copy of the project for one output

def _same_shape(a: float, b: float) -> bool:
    return abs(a / b - 1) < 0.02


def variant_project(svc: "Services", p: Project, fmt: Format, look: Callable[[str], Optional[dict[str, Any]]]) -> tuple[Project, dict[str, Any]]:
    """A copy of ``p`` shaped for ``fmt`` plus what was done (the original is not touched)."""
    v = clone(p)
    notes: dict[str, Any] = {"format": fmt.label, "reframe": fmt.reframe, "same_shape": True, "clips": 0, "paths_reused": 0, "paths_made": 0,
                             "centered": 0, "blurred": 0}
    if _same_shape(fmt.width / fmt.height, p.canvas.width / p.canvas.height):
        return v, notes  # rendered as the project is, only scaled to the output size
    notes["same_shape"] = False
    v.canvas = p.canvas.model_copy(update={"width": fmt.width, "height": fmt.height})
    out_aspect = fmt.width / fmt.height
    main = v.main_track()
    for track in v.tracks:
        if track.kind != "video":
            continue
        for c in track.clips:
            if c.type != "media":
                continue
            tr = c.transform
            overlay = track is not main and (tr.scale != 1 or tr.x or tr.y or tr.rotation)
            info = look(c.media) or {}
            w, h = int(info.get("width") or 0), int(info.get("height") or 0)
            if overlay or not w or not h or tr.fit not in ("contain", "cover", "blur"):
                continue  # picture in picture and deliberate sizes keep what the person set
            notes["clips"] += 1
            src_aspect = (w * (1 - c.crop.left - c.crop.right)) / max(1.0, h * (1 - c.crop.top - c.crop.bottom))
            if _same_shape(src_aspect, out_aspect):
                tr.fit = "cover"
                continue
            if fmt.reframe == "blur":
                tr.fit, tr.scale, tr.x, tr.y, c.reframe = "blur", 1.0, 0.0, 0.0, None
                notes["blurred"] += 1
                continue
            tr.fit, tr.scale, tr.x, tr.y = "cover", 1.0, 0.0, 0.0
            if fmt.reframe == "auto" and c.reframe and c.reframe.path:
                notes["paths_reused"] += 1
                continue
            c.reframe = None
            tr.focus_x, tr.focus_y = 0.5, 0.5
            if fmt.reframe == "auto" and info.get("kind") == "video":
                focus = media_store.get_analysis(svc, c.media, "focus")
                samples = [s for s in (focus or {}).get("samples", []) if c.src_in - 2000 <= s[0] <= c.src_out + 2000]
                if samples:
                    scenes = [x["t"] for x in (media_store.get_analysis(svc, c.media, "scenes") or {}).get("cuts", [])]
                    path = video_an.smooth_path(samples, scenes, aspect_in=w / h, aspect_out=out_aspect, mode="auto")
                    if path:
                        c.reframe = Reframe(path=path, mode="track")
                        notes["paths_made"] += 1
                        continue
            notes["centered"] += 1
    return v, notes
