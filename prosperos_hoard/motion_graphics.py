"""Deterministic motion graphics: titles, animated lyrics, lower thirds and end
credits drawn in code (Pillow + numpy, no browser, no GPU, no ComfyUI) and piped
frame by frame to ffmpeg.

A **graphic** is a JSON spec in the spirit of a clip spec::

    {"grammar": "kinetic_lyrics" | "title_card" | "lower_third" | "outro_card",
     "mode": "clip" | "overlay",          # opaque shot, or alpha overlay over the cut
     "duration": 4.0,                     # seconds (a shot's span); frame-exact
     "seed": 7,                           # same spec + seed -> identical frames
     "style": "<style card id or name>",  # resolved by the caller into `look`
     "look": {palette, fonts, case, tracking, align, motion, transition, background, fx},
     "data": {...},                       # the grammar's texts (see below)
     "cues": [{"at": 1.2, "kind": "hit" | "flash"}],
     "safe": {"top": 0.09, "bottom": 0.21, "side": 0.07},
     "suppress_captions": true}

All times inside a spec (word onsets, cues) are **spec time**: seconds from the
start of the graphic's window. ``window_start_s`` (song time of spec time 0) is
only bookkeeping for the cut. Same spec + seed + size + fps -> byte-identical
frames (no wall clock, no global randomness; the grain and every jitter come
from ``random.Random`` / ``numpy.random.RandomState`` seeded from the spec).

Grammars and their ``data``:

* ``kinetic_lyrics``: ``lines`` (``[{text, start_s, end_s, words?: [{text, start_s,
  end_s}]}]``; without ``words`` they are spread over the line by syllable weight,
  the same weights the karaoke captions use), ``layout`` ``line`` (default) or
  ``word`` (one huge word at a time), ``snap_to_beats`` + ``beats`` (word onsets
  within 0.1 s of a beat move onto it), ``upcoming`` (show the words still to
  come dimmed).
* ``title_card``: ``title``, ``subtitle``, ``kicker``.
* ``lower_third``: ``name``, ``caption``.
* ``outro_card``: ``title``, ``subtitle``, ``credits`` (``[{role, name}]``) or ``lines``.

Boundaries: text is laid out with FreeType's basic engine (kerning yes, complex
scripts no), the bundled fonts only, and the whole thing is CPU: a 1080x1920 frame
takes roughly 20-60 ms. Alpha overlays are written as QuickTime Animation (lossless RLE with alpha, PNG-in-MOV
when the ffmpeg in use lacks it), which every ffmpeg can overlay; they are working files,
not delivery files.
"""

from __future__ import annotations

import hashlib
import io
import math
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import design, procutil
from .backend import ffmpeg_path
from .hoard_link import proc as hlproc

GRAMMARS = ("kinetic_lyrics", "title_card", "lower_third", "outro_card")
MODES = ("clip", "overlay")
DEFAULT_MODE = {"kinetic_lyrics": "clip", "title_card": "clip", "lower_third": "overlay", "outro_card": "clip"}
TRANSITIONS = ("fade", "slide_up", "wipe", "scale_pop", "glitch", "flicker", "mask_reveal")
EASINGS = ("linear", "out_cubic", "in_cubic", "in_out_cubic", "out_quart", "out_expo", "in_out_expo", "out_back", "in_out_sine")
CASES = ("upper", "none")
ALIGNS = ("center", "left")
BACKGROUNDS = ("solid", "gradient", "paper", "scanlines", "grid")
SHADOWS = ("none", "soft", "hard")
CUE_KINDS = ("hit", "flash")
LAYOUTS = ("line", "word")
MAX_LINES = 80
MAX_DURATION_S = 600.0
SNAP_S = 0.10
KARAOKE_MAX_FILL_S = 3.6  # the same cap the karaoke captions use (video.KARAOKE_MAX_FILL_S)

# the order of a palette: background, ink, accent, second accent, muted
PALETTE_ROLES = ("background", "ink", "accent", "accent2", "muted")


class GraphicError(ValueError):
    """A graphic spec or render that cannot be used; `code` says why."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class GraphicCancelled(GraphicError):
    def __init__(self) -> None:
        super().__init__("cancelled", "render cancelled")


# --------------------------------------------------------------------- look

DEFAULT_LOOK: dict[str, Any] = {
    "palette": ["#0b0b12", "#f4f1ea", "#ff3d81", "#3dd6ff", "#8a8aa0"],
    "fonts": {"display": "bebas-neue", "body": "inter"},
    "weight": {"display": 700, "body": 500},
    "case": "upper",
    "tracking": 0.0,
    "align": "center",
    "motion": {"easing": "out_cubic", "bezier": None, "stagger_s": 0.07, "stepped_fps": 0, "pop": 1.0},
    "transition": "fade",
    "background": {"kind": "gradient", "grain": 0.0},
    "fx": {"glow": 0.0, "shadow": "soft", "shadow_px": 0.006, "rgb_split": 0.0, "jitter_deg": 0.0, "scanlines": 0.0,
           "rule": True},
}

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def parse_hex(colour: Any) -> tuple[int, int, int]:
    text = str(colour or "").strip()
    if not _HEX.match(text):
        raise GraphicError("bad_graphic", f"'{text[:20]}' is not a #hex colour (like #ff3d81)")
    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def clean_palette(value: Any, *, minimum: int = 2) -> list[str]:
    if not isinstance(value, (list, tuple)) or not minimum <= len(value) <= 8:
        raise GraphicError("bad_graphic", f"palette must be a list of {minimum}-8 #hex colours (background, ink, accent, "
                                          "second accent, muted)")
    return ["#%02x%02x%02x" % parse_hex(c) for c in value]


def _num(value: Any, where: str, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise GraphicError("bad_graphic", f"{where} must be a number") from None
    if not (lo <= number <= hi) or number != number:
        raise GraphicError("bad_graphic", f"{where} must be between {lo:g} and {hi:g}")
    return number


def _choice(value: Any, where: str, options: tuple[str, ...]) -> str:
    if value not in options:
        raise GraphicError("bad_graphic", f"{where} must be one of {', '.join(options)}")
    return value


def clean_look(look: Any) -> dict[str, Any]:
    """A partial look (what a style card or a spec overrides), validated. Only
    the keys given are returned."""
    if look in (None, {}):
        return {}
    if not isinstance(look, dict):
        raise GraphicError("bad_graphic", "look must be an object")
    allowed = {"palette", "fonts", "weight", "case", "tracking", "align", "motion", "transition", "background", "fx"}
    unknown = set(look) - allowed
    if unknown:
        raise GraphicError("bad_graphic", f"unknown look field(s): {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    if look.get("palette") is not None:
        out["palette"] = clean_palette(look["palette"])
    if look.get("fonts") is not None:
        fonts = look["fonts"]
        if not isinstance(fonts, dict) or set(fonts) - {"display", "body"}:
            raise GraphicError("bad_graphic", "look.fonts is {display, body} with bundled font names")
        known = design.available_fonts()
        for role, name in fonts.items():
            if name not in known:
                raise GraphicError("bad_graphic", f"look.fonts.{role}: '{name}' is not a bundled font ({', '.join(known)})")
        out["fonts"] = dict(fonts)
    if look.get("weight") is not None:
        w = look["weight"]
        if not isinstance(w, dict) or set(w) - {"display", "body"}:
            raise GraphicError("bad_graphic", "look.weight is {display, body} with weights 100-900")
        out["weight"] = {k: int(_num(v, f"look.weight.{k}", 100, 900)) for k, v in w.items()}
    if look.get("case") is not None:
        out["case"] = _choice(look["case"], "look.case", CASES)
    if look.get("tracking") is not None:
        out["tracking"] = round(_num(look["tracking"], "look.tracking", -0.1, 0.6), 4)
    if look.get("align") is not None:
        out["align"] = _choice(look["align"], "look.align", ALIGNS)
    if look.get("transition") is not None:
        out["transition"] = _choice(look["transition"], "look.transition", TRANSITIONS)
    if look.get("motion") is not None:
        out["motion"] = clean_motion(look["motion"])
    if look.get("background") is not None:
        bg = look["background"]
        if not isinstance(bg, dict) or set(bg) - {"kind", "grain"}:
            raise GraphicError("bad_graphic", "look.background is {kind, grain}")
        clean: dict[str, Any] = {}
        if bg.get("kind") is not None:
            clean["kind"] = _choice(bg["kind"], "look.background.kind", BACKGROUNDS)
        if bg.get("grain") is not None:
            clean["grain"] = round(_num(bg["grain"], "look.background.grain", 0, 1), 3)
        out["background"] = clean
    if look.get("fx") is not None:
        fx = look["fx"]
        keys = {"glow", "shadow", "shadow_px", "rgb_split", "jitter_deg", "scanlines", "rule"}
        if not isinstance(fx, dict) or set(fx) - keys:
            raise GraphicError("bad_graphic", f"look.fx takes {', '.join(sorted(keys))}")
        clean = {}
        for key in ("glow", "rgb_split", "scanlines"):
            if fx.get(key) is not None:
                clean[key] = round(_num(fx[key], f"look.fx.{key}", 0, 1), 3)
        if fx.get("shadow") is not None:
            clean["shadow"] = _choice(fx["shadow"], "look.fx.shadow", SHADOWS)
        if fx.get("shadow_px") is not None:
            clean["shadow_px"] = round(_num(fx["shadow_px"], "look.fx.shadow_px", 0, 0.05), 4)
        if fx.get("jitter_deg") is not None:
            clean["jitter_deg"] = round(_num(fx["jitter_deg"], "look.fx.jitter_deg", 0, 8), 2)
        if fx.get("rule") is not None:
            clean["rule"] = bool(fx["rule"])
        out["fx"] = clean
    return out


def clean_motion(motion: Any) -> dict[str, Any]:
    """`{easing, bezier: [x1, y1, x2, y2], stagger_s, stepped_fps, pop}`; a bezier wins over the named easing."""
    if motion in (None, {}):
        return {}
    if not isinstance(motion, dict) or set(motion) - {"easing", "bezier", "stagger_s", "stepped_fps", "pop"}:
        raise GraphicError("bad_graphic", "motion is {easing, bezier, stagger_s, stepped_fps, pop}")
    out: dict[str, Any] = {}
    if motion.get("easing") is not None:
        out["easing"] = _choice(motion["easing"], "motion.easing", EASINGS)
    if motion.get("bezier") is not None:
        b = motion["bezier"]
        if not isinstance(b, (list, tuple)) or len(b) != 4:
            raise GraphicError("bad_graphic", "motion.bezier is [x1, y1, x2, y2] (a CSS cubic-bezier)")
        x1, y1, x2, y2 = (_num(v, "motion.bezier", -2, 3) for v in b)
        if not (0 <= x1 <= 1 and 0 <= x2 <= 1):
            raise GraphicError("bad_graphic", "motion.bezier x values must be between 0 and 1")
        out["bezier"] = [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]
    if motion.get("stagger_s") is not None:
        out["stagger_s"] = round(_num(motion["stagger_s"], "motion.stagger_s", 0, 0.5), 3)
    if motion.get("stepped_fps") is not None:
        out["stepped_fps"] = int(_num(motion["stepped_fps"], "motion.stepped_fps", 0, 60))
    if motion.get("pop") is not None:
        out["pop"] = round(_num(motion["pop"], "motion.pop", 0, 2), 3)
    return out


def merge_look(*layers: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The default look with each (already cleaned) layer laid over it, deeply."""
    out = _deep(DEFAULT_LOOK)
    for layer in layers:
        for key, value in (layer or {}).items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key].update(value)
            else:
                out[key] = _deep(value)
    pal = out["palette"]
    while len(pal) < 5:  # a short palette keeps its colours; the missing roles come from the default
        pal.append(DEFAULT_LOOK["palette"][len(pal)])
    return out


def _deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep(v) for v in value]
    return value


def look_from_card(card: Optional[dict[str, Any]]) -> dict[str, Any]:
    """A style card (db.style_presets row, parsed) as a partial look: its palette, motion,
    signature transition and typography. A card without graphic fields gives {}."""
    if not card:
        return {}
    raw: dict[str, Any] = {}
    if card.get("palette"):
        raw["palette"] = card["palette"]
    if card.get("motion"):
        raw["motion"] = card["motion"]
    if card.get("signature_transition"):
        raw["transition"] = card["signature_transition"]
    typo = card.get("typography") or {}
    for key in ("fonts", "weight", "case", "tracking", "align", "background", "fx"):
        if typo.get(key) is not None:
            raw[key] = typo[key]
    try:
        return clean_look(raw)
    except GraphicError:
        return {}  # a hand-edited card never breaks a render: fall back to what is valid


def _luma(rgb: tuple[int, int, int]) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def _saturation(rgb: tuple[int, int, int]) -> float:
    hi, lo = max(rgb), min(rgb)
    return 0.0 if hi == 0 else (hi - lo) / hi


def look_from_palette(colours: Any) -> dict[str, Any]:
    """A partial look from a character's palette (a list of #hex colours): the darkest colour is the
    background, the lightest the ink, the two most saturated of the rest are the accents. Fewer than
    two usable colours gives {} (the defaults stay)."""
    rgb: list[tuple[int, int, int]] = []
    for c in colours if isinstance(colours, (list, tuple)) else []:
        try:
            rgb.append(parse_hex(c))
        except GraphicError:
            continue
    rgb = list(dict.fromkeys(rgb))
    if len(rgb) < 2:
        return {}
    by_luma = sorted(rgb, key=_luma)
    bg = by_luma[0]
    # the ink is the lightest colour when it is light enough to read on the background; else the default ink
    ink = by_luma[-1] if _luma(by_luma[-1]) >= 0.7 and by_luma[-1] != bg else parse_hex(DEFAULT_LOOK["palette"][1])
    rest = sorted((c for c in rgb if c not in (bg, ink)), key=lambda c: (-_saturation(c), _luma(c)))
    accent = rest[0] if rest else parse_hex(DEFAULT_LOOK["palette"][2])
    accent2 = rest[1] if len(rest) > 1 else accent
    muted = _mix(bg, ink, 0.45)
    return {"palette": ["#%02x%02x%02x" % c for c in (bg, ink, accent, accent2, muted)]}


def clean_card_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate the graphic fields of a style card (technique, palette, motion, signature_transition,
    quality, pitfalls, typography); returns only the keys given, cleaned."""
    out: dict[str, Any] = {}
    if fields.get("technique") is not None:
        out["technique"] = str(fields["technique"]).strip()[:600]
    if fields.get("palette") is not None:
        out["palette"] = clean_palette(fields["palette"]) if fields["palette"] else []
    if fields.get("motion") is not None:
        out["motion"] = clean_motion(fields["motion"])
    if fields.get("signature_transition") not in (None, ""):
        out["signature_transition"] = _choice(fields["signature_transition"], "signature_transition", TRANSITIONS)
    elif "signature_transition" in fields:
        out["signature_transition"] = None
    if fields.get("quality") is not None:
        out["quality"] = int(_num(fields["quality"], "quality", 0, 3))
    if fields.get("pitfalls") is not None:
        out["pitfalls"] = str(fields["pitfalls"]).strip()[:800]
    if fields.get("typography") is not None:
        typo = fields["typography"]
        if not isinstance(typo, dict):
            raise GraphicError("bad_graphic", "typography is an object (fonts, weight, case, tracking, align, background, fx)")
        out["typography"] = clean_look({k: v for k, v in typo.items()})
        if set(typo) - {"fonts", "weight", "case", "tracking", "align", "background", "fx"}:
            raise GraphicError("bad_graphic", "typography takes fonts, weight, case, tracking, align, background, fx")
    return out


# ------------------------------------------------------------------ easing

def _bezier_easing(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    def coord(t: float, a: float, b: float) -> float:
        return 3 * a * (1 - t) ** 2 * t + 3 * b * (1 - t) * t ** 2 + t ** 3

    def deriv(t: float, a: float, b: float) -> float:
        return 3 * a * (1 - t) ** 2 + 6 * (b - a) * (1 - t) * t + 3 * (1 - b) * t ** 2

    def ease(x: float) -> float:
        if x <= 0:
            return 0.0
        if x >= 1:
            return 1.0
        t = x
        for _ in range(8):  # Newton
            err = coord(t, x1, x2) - x
            if abs(err) < 1e-6:
                return coord(t, y1, y2)
            d = deriv(t, x1, x2)
            if abs(d) < 1e-6:
                break
            t -= err / d
        lo, hi = 0.0, 1.0  # bisection when Newton stalls
        t = x
        for _ in range(40):
            err = coord(t, x1, x2) - x
            if abs(err) < 1e-7:
                break
            if err > 0:
                hi = t
            else:
                lo = t
            t = (lo + hi) / 2
        return coord(t, y1, y2)

    return ease


def _named_easing(name: str) -> Callable[[float], float]:
    c1 = 1.70158
    table: dict[str, Callable[[float], float]] = {
        "linear": lambda x: x,
        "out_cubic": lambda x: 1 - (1 - x) ** 3,
        "in_cubic": lambda x: x ** 3,
        "in_out_cubic": lambda x: 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2,
        "out_quart": lambda x: 1 - (1 - x) ** 4,
        "out_expo": lambda x: 1.0 if x >= 1 else 1 - 2 ** (-10 * x),
        "in_out_expo": lambda x: 0.0 if x <= 0 else 1.0 if x >= 1 else (2 ** (20 * x - 10) / 2 if x < 0.5 else (2 - 2 ** (-20 * x + 10)) / 2),
        "out_back": lambda x: 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2,
        "in_out_sine": lambda x: -(math.cos(math.pi * x) - 1) / 2,
    }
    return table[name]


def make_easing(motion: dict[str, Any]) -> Callable[[float], float]:
    """The look's primary curve, clamped to progress 0-1 on input."""
    bez = motion.get("bezier")
    base = _bezier_easing(*bez) if bez else _named_easing(motion.get("easing") or "out_cubic")
    return lambda x: base(min(1.0, max(0.0, x)))


def pop_easing(amount: float) -> Callable[[float], float]:
    """Overshoot curve for words and cards that pop in; amount 0 = no overshoot."""
    c1 = 1.70158 * max(0.0, amount)
    return lambda x: 0.0 if x <= 0 else 1.0 if x >= 1 else 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def _clamp01(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else x


# ---------------------------------------------------------- spec validation

def _text(value: Any, where: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GraphicError("bad_graphic", f"{where} must be text")
    return re.sub(r"[ \t]+", " ", value.replace("\r", "")).strip()[:limit]


def _clean_words(words: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(words, list) or len(words) > 60:
        raise GraphicError("bad_graphic", f"{where}.words must be a list of at most 60 {{text, start_s, end_s}}")
    out = []
    for i, w in enumerate(words):
        if not isinstance(w, dict) or not str(w.get("text", "")).strip():
            raise GraphicError("bad_graphic", f"{where}.words[{i}] needs text")
        start = _num(w.get("start_s"), f"{where}.words[{i}].start_s", 0, MAX_DURATION_S)
        end = _num(w.get("end_s", start), f"{where}.words[{i}].end_s", 0, MAX_DURATION_S)
        out.append({"text": str(w["text"]).strip()[:60], "start_s": round(start, 3), "end_s": round(max(start, end), 3)})
    return sorted(out, key=lambda w: w["start_s"])


def _clean_data(grammar: str, data: Any) -> dict[str, Any]:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise GraphicError("bad_graphic", "data must be an object")
    out: dict[str, Any] = {}
    if grammar == "kinetic_lyrics":
        lines = data.get("lines")
        if lines is not None:
            if not isinstance(lines, list) or len(lines) > MAX_LINES:
                raise GraphicError("bad_graphic", f"data.lines must be a list of at most {MAX_LINES} lines")
            clean = []
            for i, line in enumerate(lines):
                if not isinstance(line, dict):
                    raise GraphicError("bad_graphic", f"data.lines[{i}] must be {{text, start_s, end_s}}")
                text = _text(line.get("text"), f"data.lines[{i}].text", 300)
                if not text:
                    continue
                start = _num(line.get("start_s"), f"data.lines[{i}].start_s", 0, MAX_DURATION_S)
                end = _num(line.get("end_s", start + 2.0), f"data.lines[{i}].end_s", 0, MAX_DURATION_S)
                if end <= start:
                    raise GraphicError("bad_graphic", f"data.lines[{i}]: end_s must be after start_s")
                entry: dict[str, Any] = {"text": text, "start_s": round(start, 3), "end_s": round(end, 3)}
                if line.get("words"):
                    entry["words"] = _clean_words(line["words"], f"data.lines[{i}]")
                clean.append(entry)
            out["lines"] = sorted(clean, key=lambda ln: ln["start_s"])
        out["layout"] = _choice(data.get("layout") or "line", "data.layout", LAYOUTS)
        if data.get("snap_to_beats"):
            out["snap_to_beats"] = True
        if data.get("beats") is not None:
            beats = data["beats"]
            if not isinstance(beats, list) or len(beats) > 4000:
                raise GraphicError("bad_graphic", "data.beats must be a list of seconds")
            out["beats"] = [round(_num(b, "data.beats[]", 0, MAX_DURATION_S), 3) for b in beats]
        if data.get("upcoming"):
            out["upcoming"] = True
        if data.get("from_song") is not None or data.get("from_song") is False:
            out["from_song"] = bool(data["from_song"])
    elif grammar == "title_card":
        out["title"] = _text(data.get("title"), "data.title", 120)
        out["subtitle"] = _text(data.get("subtitle"), "data.subtitle", 160)
        out["kicker"] = _text(data.get("kicker"), "data.kicker", 80)
        if not out["title"]:
            raise GraphicError("bad_graphic", "a title card needs data.title")
    elif grammar == "lower_third":
        out["name"] = _text(data.get("name"), "data.name", 80)
        out["caption"] = _text(data.get("caption"), "data.caption", 120)
        if not out["name"]:
            raise GraphicError("bad_graphic", "a lower third needs data.name")
    elif grammar == "outro_card":
        out["title"] = _text(data.get("title"), "data.title", 120)
        out["subtitle"] = _text(data.get("subtitle"), "data.subtitle", 160)
        credits = data.get("credits")
        if credits is not None:
            if not isinstance(credits, list) or len(credits) > 24:
                raise GraphicError("bad_graphic", "data.credits must be a list of at most 24 {role, name}")
            out["credits"] = [{"role": _text(c.get("role"), "data.credits[].role", 60), "name": _text(c.get("name"), "data.credits[].name", 80)}
                              for c in credits if isinstance(c, dict) and (c.get("name") or c.get("role"))]
        lines = data.get("lines")
        if lines is not None:
            if not isinstance(lines, list) or len(lines) > 12:
                raise GraphicError("bad_graphic", "data.lines must be a list of at most 12 texts")
            out["lines"] = [_text(x, "data.lines[]", 100) for x in lines if _text(x, "data.lines[]", 100)]
        if not (out["title"] or out.get("credits") or out.get("lines")):
            raise GraphicError("bad_graphic", "an outro card needs a data.title, data.credits or data.lines")
    return out


def normalise_graphic(spec: Any, *, need_duration: bool = False) -> dict[str, Any]:
    """Validate a graphic spec and return a clean copy. `need_duration` demands a `duration`
    (a standalone render); inside a production the shot's span supplies it."""
    if not isinstance(spec, dict):
        raise GraphicError("bad_graphic", "a graphic is an object: {grammar, data, ...}")
    allowed = {"grammar", "mode", "duration", "seed", "style", "look", "data", "cues", "safe", "suppress_captions",
               "window_start_s", "in_s", "out_s"}
    unknown = set(spec) - allowed
    if unknown:
        raise GraphicError("bad_graphic", f"unknown graphic field(s): {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}")
    grammar = spec.get("grammar")
    if grammar not in GRAMMARS:
        raise GraphicError("bad_graphic", f"grammar must be one of {', '.join(GRAMMARS)}")
    out: dict[str, Any] = {"grammar": grammar}
    mode = spec.get("mode") or DEFAULT_MODE[grammar]
    out["mode"] = _choice(mode, "mode", MODES)
    if grammar == "lower_third" and out["mode"] != "overlay":
        raise GraphicError("bad_graphic", "a lower third is an overlay (mode 'overlay'): it sits over the cut")
    if spec.get("duration") is not None:
        out["duration"] = round(_num(spec["duration"], "duration", 0.5, MAX_DURATION_S), 4)
    elif need_duration:
        raise GraphicError("bad_graphic", "a standalone graphic needs a duration in seconds")
    out["seed"] = int(_num(spec.get("seed", 0), "seed", 0, 2**31 - 2))
    if spec.get("style") not in (None, ""):
        out["style"] = _text(spec["style"], "style", 80)
    if spec.get("look") not in (None, {}):
        out["look"] = clean_look(spec["look"])
    out["data"] = _clean_data(grammar, spec.get("data"))
    cues = spec.get("cues") or []
    if not isinstance(cues, list) or len(cues) > 400:
        raise GraphicError("bad_graphic", "cues must be a list of at most 400 {at, kind}")
    clean_cues = []
    for i, cue in enumerate(cues):
        if not isinstance(cue, dict):
            raise GraphicError("bad_graphic", f"cues[{i}] must be {{at, kind}}")
        clean_cues.append({"at": round(_num(cue.get("at"), f"cues[{i}].at", 0, MAX_DURATION_S), 3),
                           "kind": _choice(cue.get("kind") or "hit", f"cues[{i}].kind", CUE_KINDS)})
    if clean_cues:
        out["cues"] = sorted(clean_cues, key=lambda c: c["at"])
    if spec.get("safe") not in (None, {}):
        safe = spec["safe"]
        if not isinstance(safe, dict) or set(safe) - {"top", "bottom", "side"}:
            raise GraphicError("bad_graphic", "safe is {top, bottom, side} as fractions of the frame (0-0.4)")
        out["safe"] = {k: round(_num(v, f"safe.{k}", 0, 0.4), 4) for k, v in safe.items()}
    if spec.get("suppress_captions") is not None:
        out["suppress_captions"] = bool(spec["suppress_captions"])
    if spec.get("window_start_s") is not None:
        out["window_start_s"] = round(_num(spec["window_start_s"], "window_start_s", 0, 36000), 4)
    for key in ("in_s", "out_s"):
        if spec.get(key) is not None:
            out[key] = round(_num(spec[key], key, 0.1, 3.0), 3)
    return out


def captions_suppressed(spec: dict[str, Any]) -> bool:
    """Whether the burned-in lyric captions step aside while this graphic is on screen."""
    if spec.get("suppress_captions") is not None:
        return bool(spec["suppress_captions"])
    return spec.get("grammar") == "kinetic_lyrics"


# ------------------------------------------------------------- word timing

def _word_weight(word: str) -> int:
    return max(1, len(re.findall(r"[aeiouáéíóúüy]+", word, flags=re.IGNORECASE)))


def spread_words(text: str, start: float, end: float) -> list[dict[str, Any]]:
    """The words of a sung line over [start, end] by syllable weight: the same fill window and
    weights the karaoke captions use (a line lights up within KARAOKE_MAX_FILL_S at most)."""
    words = text.split()
    if not words or end <= start:
        return []
    fill = min(0.92 * (end - start), KARAOKE_MAX_FILL_S)
    weights = [_word_weight(w) for w in words]
    total = sum(weights)
    out, t = [], start
    for w, wt in zip(words, weights):
        dur = fill * wt / total
        out.append({"text": w, "start_s": round(t, 3), "end_s": round(t + dur, 3)})
        t += dur
    return out


def timed_lines(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The kinetic lyrics' lines, each with every word timed: given word times win, else spread by
    syllable weight; with `snap_to_beats` an onset within SNAP_S of a beat moves onto it (never
    out of order)."""
    beats = sorted(data.get("beats") or []) if data.get("snap_to_beats") else []
    out = []
    for line in data.get("lines") or []:
        words = [dict(w) for w in (line.get("words") or spread_words(line["text"], line["start_s"], line["end_s"]))]
        if beats:
            prev = -1.0
            for w in words:
                near = min(beats, key=lambda b: abs(b - w["start_s"]))
                if abs(near - w["start_s"]) <= SNAP_S and near > prev:
                    shift = near - w["start_s"]
                    w["start_s"] = round(near, 3)
                    w["end_s"] = round(max(w["end_s"] + shift, near), 3)
                prev = w["start_s"]
        out.append({"text": line["text"], "start_s": line["start_s"], "end_s": line["end_s"], "words": words})
    return out


# -------------------------------------------------------------------- fonts

_font_cache: dict[tuple[str, int, int], ImageFont.FreeTypeFont] = {}


def load_font(name: str, size: int, weight: int = 0) -> ImageFont.FreeTypeFont:
    """A bundled font at `size` px, with FreeType's basic layout (same result on every machine)
    and, for a variable font, the nearest weight it offers."""
    size = max(6, int(size))
    key = (name, size, int(weight))
    if key in _font_cache:
        return _font_cache[key]
    path = design._FONT_FILES.get(name) or design._FONT_FILES["inter"]
    font = ImageFont.truetype(str(path), size=size, layout_engine=ImageFont.Layout.BASIC)
    if weight:
        try:
            axes = font.get_variation_axes()
            values = []
            for axis in axes:
                label = axis["name"].decode() if isinstance(axis["name"], bytes) else str(axis["name"])
                if label == "Weight":
                    values.append(min(axis["maximum"], max(axis["minimum"], weight)))
                else:
                    values.append(axis["default"])
            font.set_variation_by_axes(values)
        except (OSError, AttributeError, KeyError):
            pass  # a static font has no weight to set
    _font_cache[key] = font
    return font


def _advance(font: ImageFont.FreeTypeFont, text: str, tracking_px: float) -> float:
    return font.getlength(text) + tracking_px * max(0, len(text) - 1)


# ------------------------------------------------------------------ sprites

@dataclass
class Sprite:
    img: Image.Image            # RGBA, padded; its centre is the centre of the text's ink box
    ink_w: float
    ink_h: float
    text: str = ""
    size: int = 0


def _rgba(rgb: tuple[int, int, int], alpha: float = 1.0) -> tuple[int, int, int, int]:
    return rgb[0], rgb[1], rgb[2], int(round(255 * alpha))


def text_sprite(text: str, font: ImageFont.FreeTypeFont, fill: tuple[int, int, int], *, tracking_px: float = 0.0,
                shadow: str = "none", shadow_px: float = 0.0, shadow_rgb: tuple[int, int, int] = (0, 0, 0),
                glow: float = 0.0, glow_rgb: Optional[tuple[int, int, int]] = None, angle: float = 0.0) -> Sprite:
    """Text on a transparent, symmetrically padded RGBA image (so its centre is the ink box's
    centre): optional hard or soft shadow, glow, and a baked-in rotation."""
    ascent, descent = font.getmetrics()
    width = _advance(font, text, tracking_px)
    ink_h = float(ascent + descent)
    size = font.size
    blur = max(2.0, size * 0.06) if shadow == "soft" else 0.0
    glow_r = max(3.0, size * 0.16) if glow > 0 else 0.0
    off = shadow_px if shadow in ("soft", "hard") else 0.0
    pad = int(math.ceil(max(blur * 2.5 + off, glow_r * 2.2, 4))) + 2
    w, h = int(math.ceil(width)) + pad * 2, int(math.ceil(ink_h)) + pad * 2

    def draw_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], colour: tuple[int, ...]) -> None:
        if tracking_px:
            x = xy[0]
            for ch in text:
                draw.text((x, xy[1]), ch, font=font, fill=colour)
                x += font.getlength(ch) + tracking_px
        else:
            draw.text(xy, text, font=font, fill=colour)

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if glow > 0:
        mask = Image.new("L", (w, h), 0)
        draw_text(ImageDraw.Draw(mask), (pad, pad), (255,))
        halo = mask.filter(ImageFilter.GaussianBlur(glow_r))
        halo = halo.point([min(255, int(v * (0.6 + 1.6 * glow))) for v in range(256)])
        tint = Image.new("RGBA", (w, h), _rgba(glow_rgb or fill))
        tint.putalpha(halo)
        layer = Image.alpha_composite(layer, tint)
    if shadow in ("soft", "hard"):
        mask = Image.new("L", (w, h), 0)
        draw_text(ImageDraw.Draw(mask), (pad + off, pad + off), (255,))
        if shadow == "soft":
            mask = mask.filter(ImageFilter.GaussianBlur(blur)).point([min(255, int(v * 0.75)) for v in range(256)])
        sh = Image.new("RGBA", (w, h), _rgba(shadow_rgb))
        sh.putalpha(mask)
        layer = Image.alpha_composite(layer, sh)
    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw_text(ImageDraw.Draw(body), (pad, pad), _rgba(fill))
    layer = Image.alpha_composite(layer, body)
    if angle:
        layer = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
    return Sprite(layer, width, ink_h, text, size)


def _tinted(sprite: Sprite, rgb: tuple[int, int, int], alpha: float) -> Image.Image:
    """The sprite's silhouette in one colour (a ghost for the glitch / RGB split)."""
    flat = Image.new("RGBA", sprite.img.size, _rgba(rgb))
    a = sprite.img.getchannel("A").point([int(v * alpha) for v in range(256)])
    flat.putalpha(a)
    return flat


# ------------------------------------------------------------- compositing

@dataclass
class Pose:
    """How a layer is drawn at one instant."""
    opacity: float = 1.0
    dx: float = 0.0
    dy: float = 0.0
    scale: float = 1.0
    clip: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)  # x0, y0, x1, y1 as fractions of the sprite
    glitch: float = 0.0


def _opacity(img: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 0.999:
        return img
    lut = [int(round(v * opacity)) for v in range(256)]
    out = img.copy()
    out.putalpha(img.getchannel("A").point(lut))
    return out


def _glitch_image(spr: Sprite, base: Image.Image, amount: float, rng: random.Random, accent: tuple[int, int, int],
                  accent2: tuple[int, int, int], split: float) -> Image.Image:
    """Horizontal slices shifted at random plus red/blue ghosts, strength `amount` (0-1)."""
    w, h = base.size
    strips = 7
    canvas_w = w + int(w * 0.10) + 4
    out = Image.new("RGBA", (canvas_w, h), (0, 0, 0, 0))
    pad = (canvas_w - w) // 2
    ghost = max(2, int(spr.size * 0.07 * (0.4 + split) * amount))
    for rgb, direction in ((accent, -1), (accent2, 1)):
        g = _tinted(Sprite(base, spr.ink_w, spr.ink_h), rgb, 0.6)
        out.alpha_composite(g, (pad + direction * ghost, 0))
    for i in range(strips):
        y0, y1 = i * h // strips, (i + 1) * h // strips
        shift = int(rng.uniform(-1, 1) * amount * w * 0.05)
        out.alpha_composite(base.crop((0, y0, w, y1)), (pad + shift, y0))
    return out


def blit(canvas: Image.Image, spr: Sprite, cx: float, cy: float, pose: Optional[Pose] = None, *,
         group: Optional[tuple[float, float, float]] = None, frame_seed: int = 0,
         accents: Optional[tuple[tuple[int, int, int], tuple[int, int, int], float]] = None) -> None:
    """Draw a sprite centred at (cx, cy) with an animation pose; `group` = (gx, gy, s) scales the
    position about a group centre (the whole block punching on a beat)."""
    pose = pose or Pose()
    if pose.opacity <= 0.004:
        return
    img = spr.img
    centre = (cx, cy)
    if pose.clip != (0.0, 0.0, 1.0, 1.0):
        w, h = img.size
        box = (int(round(w * pose.clip[0])), int(round(h * pose.clip[1])), int(round(w * pose.clip[2])), int(round(h * pose.clip[3])))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        img = img.crop(box)
        # the cropped part stays where it was on the whole sprite
        centre = (cx - w / 2 + box[0] + img.width / 2, cy - h / 2 + box[1] + img.height / 2)
    if pose.glitch > 0.01 and accents:
        img = _glitch_image(spr, img, pose.glitch, random.Random(frame_seed), accents[0], accents[1], accents[2])
    scale = pose.scale
    ccx, ccy = centre[0] + pose.dx, centre[1] + pose.dy
    if group:
        ccx = group[0] + (ccx - group[0]) * group[2]
        ccy = group[1] + (ccy - group[1]) * group[2]
        scale *= group[2]
    if abs(scale - 1.0) > 1e-3:
        nw, nh = max(1, int(round(img.width * scale))), max(1, int(round(img.height * scale)))
        img = img.resize((nw, nh), Image.LANCZOS if scale < 1 else Image.BICUBIC)
    img = _opacity(img, pose.opacity)
    px, py = int(round(ccx - img.width / 2)), int(round(ccy - img.height / 2))
    cw, ch = canvas.size
    sx0, sy0 = max(0, -px), max(0, -py)
    sx1, sy1 = min(img.width, cw - px), min(img.height, ch - py)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    if (sx0, sy0, sx1, sy1) != (0, 0, img.width, img.height):
        img = img.crop((sx0, sy0, sx1, sy1))
    canvas.alpha_composite(img, (px + sx0, py + sy0))


# --------------------------------------------------------------- transitions

def _flicker_on(p: float, seed: int, step: int) -> float:
    """Neon sign: on/off stutter that settles to on as p reaches 1."""
    if p >= 0.999:
        return 1.0
    rng = random.Random(seed * 7919 + step)
    return 1.0 if rng.random() < 0.15 + 0.85 * p * p else 0.18


def enter_pose(name: str, p: float, ease: Callable[[float], float], pop: Callable[[float], float], width: int, height: int,
               seed: int, step: int) -> Pose:
    """Entrance of a layer at progress p (0 hidden, 1 settled)."""
    if p >= 1.0:
        return Pose()
    e = ease(p)
    if p <= 0:
        return Pose(opacity=0.0)
    if name == "slide_up":
        return Pose(opacity=min(1.0, e * 1.8), dy=(1 - e) * 0.055 * height)
    if name == "wipe":
        return Pose(clip=(0.0, 0.0, e, 1.0))
    if name == "scale_pop":
        return Pose(opacity=min(1.0, p * 4), scale=0.72 + 0.28 * pop(p))
    if name == "glitch":
        return Pose(opacity=0.0 if random.Random(seed * 31 + step).random() < 0.18 * (1 - p) else 1.0, glitch=(1 - p) ** 1.5)
    if name == "flicker":
        return Pose(opacity=_flicker_on(p, seed, step))
    if name == "mask_reveal":
        return Pose(clip=(0.0, 0.0, 1.0, e))
    return Pose(opacity=e)  # fade


def exit_pose(name: str, q: float, ease: Callable[[float], float], width: int, height: int, seed: int, step: int) -> Pose:
    """Exit of a layer at progress q (0 still there, 1 gone)."""
    if q <= 0:
        return Pose()
    if q >= 1:
        return Pose(opacity=0.0)
    e = ease(q)
    if name == "slide_up":
        return Pose(opacity=1 - e, dy=-e * 0.04 * height)
    if name == "wipe":
        return Pose(clip=(e, 0.0, 1.0, 1.0))
    if name == "scale_pop":
        return Pose(opacity=1 - e, scale=1 + 0.08 * e)
    if name == "glitch":
        return Pose(opacity=0.0 if random.Random(seed * 37 + step).random() < 0.25 * q else 1.0, glitch=q ** 1.2)
    if name == "flicker":
        return Pose(opacity=_flicker_on(1 - q, seed + 5, step))
    if name == "mask_reveal":
        return Pose(clip=(0.0, e, 1.0, 1.0))
    return Pose(opacity=1 - e)


def combine(a: Pose, b: Pose) -> Pose:
    clip = (max(a.clip[0], b.clip[0]), max(a.clip[1], b.clip[1]), min(a.clip[2], b.clip[2]), min(a.clip[3], b.clip[3]))
    return Pose(a.opacity * b.opacity, a.dx + b.dx, a.dy + b.dy, a.scale * b.scale, clip, max(a.glitch, b.glitch))


# ------------------------------------------------------------- the renderer

def default_safe(width: int, height: int) -> dict[str, float]:
    if height > width * 1.2:
        return {"top": 0.09, "bottom": 0.21, "side": 0.07}
    if width > height * 1.2:
        return {"top": 0.08, "bottom": 0.17, "side": 0.06}
    return {"top": 0.08, "bottom": 0.18, "side": 0.07}


def subtitle_band_top(width: int, height: int, lyric_style: str = "default") -> float:
    """Where the burned-in lyric captions start, as a fraction of the frame height from the top:
    the same sizes and margins `video.build_ass` uses (two caption lines above the bottom
    margin). Text of a graphic below this line collides with the captions."""
    short = min(width, height)
    portrait = height > width
    if lyric_style == "horror":
        fs, margin = max(28, short // 13), int(height * (0.2 if portrait else 0.09))
    elif lyric_style == "bold":
        fs, margin = max(30, short // 10), int(height * (0.26 if portrait else 0.12))
    elif lyric_style in ("pulse", "typewriter", "handwritten", "cinema"):
        fs = max(24, {"pulse": short // 8, "typewriter": short // 13, "handwritten": short // 8, "cinema": short // 15}[lyric_style])
        margin = int(height * {"pulse": 0.24, "typewriter": 0.18, "handwritten": 0.2, "cinema": 0.1}[lyric_style]
                     * (1.0 if portrait else 0.6))
    elif lyric_style == "pop":
        return 0.46 if portrait else 0.40  # one huge word in the middle
    else:
        fs, margin = max(28, height // 24), 80
    return max(0.0, 1.0 - (margin + 2.1 * fs) / height)


@dataclass
class Box:
    """A text box at rest, in pixels, and when it is on screen (spec time)."""
    id: str
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    t0: float
    t1: float
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text[:80], "x0": round(self.x0, 1), "y0": round(self.y0, 1), "x1": round(self.x1, 1),
                "y1": round(self.y1, 1), "t0": round(self.t0, 3), "t1": round(self.t1, 3), "truncated": self.truncated}


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


class Renderer:
    """Draws frames of one graphic at one size. Build it once and call `frame(t)`; everything
    that can be prepared (fonts, text sprites, the background) is prepared here."""

    def __init__(self, spec: dict[str, Any], width: int, height: int, fps: int = 30):
        self.spec = normalise_graphic(spec)
        if width < 64 or height < 64 or width > 4096 or height > 4096:
            raise GraphicError("bad_graphic", "the frame must be between 64 and 4096 px on each side")
        self.width, self.height, self.fps = int(width), int(height), int(fps)
        self.mode = self.spec["mode"]
        self.seed = self.spec["seed"]
        self.duration = float(self.spec.get("duration") or 4.0)
        self.look = merge_look(self.spec.get("look"))
        self.pal = [parse_hex(c) for c in self.look["palette"]]
        self.bg_rgb, self.ink, self.accent, self.accent2, self.muted = self.pal[:5]
        self.m = float(min(self.width, self.height))
        safe = {**default_safe(self.width, self.height), **(self.spec.get("safe") or {})}
        self.safe = safe
        self.x0, self.x1 = self.width * safe["side"], self.width * (1 - safe["side"])
        self.y0, self.y1 = self.height * safe["top"], self.height * (1 - safe["bottom"])
        motion = self.look["motion"]
        self.ease = make_easing(motion)
        self.pop = pop_easing(motion.get("pop", 1.0))
        self.stagger = float(motion.get("stagger_s", 0.07))
        self.stepped = int(motion.get("stepped_fps") or 0)
        self.transition = self.look["transition"]
        self.rng_seed = self.seed
        self.in_s = float(self.spec.get("in_s") or 0.8)
        self.out_s = float(self.spec.get("out_s") or 0.6)
        self._bg: Optional[Image.Image] = None
        self.cues = self.spec.get("cues") or []
        self.scene = _SCENES[self.spec["grammar"]](self)

    # -- helpers the scenes use
    def font(self, role: str, size: float) -> ImageFont.FreeTypeFont:
        return load_font(self.look["fonts"][role], int(round(size)), self.look["weight"].get(role, 0))

    def display_case(self, text: str) -> str:
        return text.upper() if self.look["case"] == "upper" else text

    def tracking_px(self, size: float, extra_em: float = 0.0) -> float:
        return (self.look["tracking"] + extra_em) * size

    def sprite(self, text: str, role: str, size: float, rgb: tuple[int, int, int], *, extra_tracking_em: float = 0.0,
               angle: float = 0.0, glow: Optional[float] = None, shadow: Optional[str] = None) -> Sprite:
        fx = self.look["fx"]
        sh = shadow if shadow is not None else fx["shadow"]
        if self.mode == "clip" and shadow is None and sh == "soft":
            sh = "soft"
        return text_sprite(text, self.font(role, size), rgb, tracking_px=self.tracking_px(size, extra_tracking_em), shadow=sh,
                           shadow_px=fx["shadow_px"] * self.m, shadow_rgb=_mix(self.bg_rgb, (0, 0, 0), 0.65),
                           glow=fx["glow"] if glow is None else glow, glow_rgb=self.accent, angle=angle)

    def anim_time(self, t: float) -> float:
        """Animation clock: stepped for stop-motion looks."""
        return math.floor(t * self.stepped + 1e-6) / self.stepped if self.stepped else t

    def step_of(self, t: float) -> int:
        return int(math.floor(t * self.fps + 1e-6))

    def punch(self, t: float) -> float:
        """Scale of the whole text block from the `hit` cues (an instant attack, a quick decay)."""
        s = 1.0
        for cue in self.cues:
            if cue["kind"] == "hit" and t >= cue["at"]:
                s += 0.05 * math.exp(-(t - cue["at"]) / 0.09)
        return s

    def flash(self, t: float) -> float:
        a = 0.0
        for cue in self.cues:
            if cue["kind"] == "flash" and t >= cue["at"]:
                a = max(a, 0.22 * math.exp(-(t - cue["at"]) / 0.10))
        return a

    # -- background
    def background(self) -> Image.Image:
        if self._bg is None:
            self._bg = _make_background(self)
        return self._bg

    def frame(self, t: float) -> Image.Image:
        """The RGBA frame at spec time t (opaque in clip mode, transparent where nothing is drawn
        in overlay mode)."""
        W, H = self.width, self.height
        if self.mode == "clip":
            canvas = self.background().copy()
            fl = self.flash(t)
            if fl > 0.004:
                canvas.alpha_composite(Image.new("RGBA", canvas.size, _rgba(self.accent, fl)))
        else:
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        self.scene.draw(canvas, t)
        if self.mode == "clip":
            _finish_clip_frame(self, canvas, t)
        return canvas

    def boxes(self) -> list[Box]:
        return self.scene.boxes()

    def hero_time(self) -> float:
        return float(min(self.duration, max(0.0, self.scene.hero_time())))

    @property
    def max_scale(self) -> float:
        return 1.0 + (0.06 if any(c["kind"] == "hit" for c in self.cues) else 0.0) + (0.04 if self.spec["grammar"] == "kinetic_lyrics" else 0.0)


def _make_background(r: Renderer) -> Image.Image:
    W, H = r.width, r.height
    kind = r.look["background"]["kind"]
    bg = r.bg_rgb
    rng = np.random.RandomState(r.seed + 1)
    if kind == "solid":
        arr = np.empty((H, W, 3), dtype=np.float32)
        arr[:] = bg
    elif kind == "paper":
        # warm paper: low-frequency mottling plus fibres
        small = rng.rand(max(2, H // 48), max(2, W // 48)).astype(np.float32)
        mott = np.asarray(Image.fromarray((small * 255).astype(np.uint8)).resize((W, H), Image.BICUBIC), dtype=np.float32) / 255.0
        fibre = rng.rand(H // 2 + 1, W // 2 + 1).astype(np.float32)
        fibre = np.repeat(np.repeat(fibre, 2, axis=0), 2, axis=1)[:H, :W]
        arr = np.empty((H, W, 3), dtype=np.float32)
        arr[:] = bg
        arr *= (0.955 + 0.06 * mott + 0.025 * fibre)[..., None]
    else:  # gradient, scanlines, grid: a soft vertical gradient with a faint vignette
        top = np.array(_mix(bg, r.accent, 0.10), dtype=np.float32)
        bottom = np.array(_mix(bg, (0, 0, 0), 0.25), dtype=np.float32)
        ramp = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
        arr = top[None, None, :] * (1 - ramp) + bottom[None, None, :] * ramp
        arr = np.broadcast_to(arr, (H, W, 3)).copy()
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d = np.sqrt(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H / 2) / (H * 0.75)) ** 2)
        arr *= (1.0 - 0.22 * np.clip(d, 0, 1) ** 2)[..., None]
        if kind == "grid":
            arr = np.empty((H, W, 3), dtype=np.float32)
            arr[:] = bg
            step = max(24, int(r.m / 12))
            line = np.array(_mix(bg, r.muted, 0.22), dtype=np.float32)
            for x in range(int(r.x0) % step, W, step):
                arr[:, x:x + 1, :] = line
            for y in range(int(r.y0) % step, H, step):
                arr[y:y + 1, :, :] = line
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    return img


def _finish_clip_frame(r: Renderer, canvas: Image.Image, t: float) -> None:
    """Scan lines and grain over a clip-mode frame (after the text)."""
    fx = r.look["fx"]
    grain = r.look["background"].get("grain", 0.0)
    scan = fx.get("scanlines", 0.0) or (0.0 if r.look["background"]["kind"] != "scanlines" else 0.35)
    if scan <= 0 and grain <= 0:
        return
    arr = np.asarray(canvas, dtype=np.int16).copy()
    H, W = arr.shape[:2]
    if scan > 0:
        rows = np.arange(H)
        dark = ((rows // 2) % 2 == 0).astype(np.float32)
        arr[..., :3] = (arr[..., :3] * (1 - 0.30 * scan * dark)[:, None, None]).astype(np.int16)
        # a slow tracking bar rolling up the frame
        centre = (1.0 - (t * 0.22 + (r.seed % 97) / 97.0) % 1.0) * H
        band = np.exp(-(((rows - centre) / (H * 0.035)) ** 2)).astype(np.float32)
        arr[..., :3] = np.clip(arr[..., :3] + (band * 26 * scan)[:, None, None], 0, 255).astype(np.int16)
    if grain > 0:
        rng = np.random.RandomState((r.seed * 100003 + r.step_of(t)) % (2**31 - 1))
        noise = rng.randint(-int(10 + 34 * grain), int(10 + 34 * grain) + 1, size=(H // 2 + 1, W // 2 + 1)).astype(np.int16)
        noise = np.repeat(np.repeat(noise, 2, axis=0), 2, axis=1)[:H, :W]
        arr[..., :3] = np.clip(arr[..., :3] + (noise * 0.5 * (0.3 + grain))[..., None].astype(np.int16), 0, 255)
    canvas.paste(Image.fromarray(arr.astype(np.uint8), "RGBA"))


# ------------------------------------------------------------------- scenes

def _wrap_fit(r: Renderer, text: str, role: str, max_size: float, min_size: float, width: float, height: float,
              max_lines: int, line_gap: float = 1.08, extra_tracking_em: float = 0.0) -> tuple[float, list[str], bool]:
    """The largest size in [min_size, max_size] at which `text` wraps into at most `max_lines`
    lines of at most `width` that together stay within `height`. Returns (size, lines, truncated);
    when even min_size overflows the last line ends with an ellipsis."""
    words = text.split()
    size = max_size
    while size >= min_size - 1e-6:
        font = r.font(role, size)
        tp = r.tracking_px(size, extra_tracking_em)
        lines: list[str] = []
        cur = ""
        for w in words:
            trial = f"{cur} {w}".strip()
            if _advance(font, trial, tp) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        asc, desc = font.getmetrics()
        fits_w = all(_advance(font, ln, tp) <= width for ln in lines)
        if len(lines) <= max_lines and fits_w and len(lines) * (asc + desc) * line_gap <= height + 1:
            return size, lines, False
        size -= max(2.0, size * 0.04)
    size = min_size
    font = r.font(role, size)
    tp = r.tracking_px(size, extra_tracking_em)
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if _advance(font, trial, tp) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    asc, desc = font.getmetrics()
    keep = max(1, min(max_lines, int(height // ((asc + desc) * line_gap))))
    truncated = len(lines) > keep or any(_advance(font, ln, tp) > width for ln in lines)
    lines = lines[:keep]
    for i, ln in enumerate(lines):
        while _advance(font, ln, tp) > width and len(ln) > 1:
            ln = ln[:-1].rstrip()
            lines[i] = ln + "…"
    if truncated and not lines[-1].endswith("…"):
        lines[-1] = lines[-1].rstrip(".,;: ") + "…"
    return size, lines, truncated


class _Scene:
    def __init__(self, r: Renderer):
        self.r = r
        self._boxes: list[Box] = []

    def boxes(self) -> list[Box]:
        return self._boxes

    def hero_time(self) -> float:
        return self.r.duration * 0.5

    def draw(self, canvas: Image.Image, t: float) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # a layer's pose from its entrance (starting at `delay`) and the block's exit
    def layer_pose(self, t: float, delay: float, *, enter_s: Optional[float] = None, exit_start: Optional[float] = None,
                   exit_s: Optional[float] = None, name: Optional[str] = None) -> Pose:
        r = self.r
        name = name or r.transition
        ta = r.anim_time(t)
        step = r.step_of(t)
        enter_s = enter_s or r.in_s
        pose = enter_pose(name, _clamp01((ta - delay) / enter_s), r.ease, r.pop, r.width, r.height, r.seed, step)
        if exit_start is not None and exit_s:
            pose = combine(pose, exit_pose(name, _clamp01((ta - exit_start) / exit_s), r.ease, r.width, r.height, r.seed, step))
        return pose


class TitleScene(_Scene):
    def __init__(self, r: Renderer):
        super().__init__(r)
        d = r.spec["data"]
        align = r.look["align"]
        m = r.m
        box_w = r.x1 - r.x0
        box_h = r.y1 - r.y0
        title = r.display_case(d["title"])
        t_size, t_lines, t_trunc = _wrap_fit(r, title, "display", m * 0.20, m * 0.07, box_w, box_h * 0.62, 3,
                                             extra_tracking_em=0.0)
        self.elems: list[dict[str, Any]] = []
        gap = t_size * 0.32
        heights = []
        if d.get("kicker"):
            k_size = max(m * 0.03, min(m * 0.042, t_size * 0.3))
            spr = r.sprite(r.display_case(d["kicker"]), "body", k_size, r.accent, extra_tracking_em=0.22, shadow="none", glow=0.0)
            self.elems.append({"id": "kicker", "spr": spr, "delay": 0.0, "text": d["kicker"]})
            heights.append(spr.ink_h)
        for i, ln in enumerate(t_lines):
            spr = r.sprite(ln, "display", t_size, r.ink)
            self.elems.append({"id": f"title{i}", "spr": spr, "delay": 0.12 + i * r.stagger * 2, "text": ln, "trunc": t_trunc})
            heights.append(spr.ink_h * 0.84)
        rule_h = max(3, int(m * 0.006))
        if d.get("subtitle"):
            s_size = max(m * 0.032, min(m * 0.055, t_size * 0.34))
            s_max_w = box_w
            _, s_lines, s_trunc = _wrap_fit(r, d["subtitle"], "body", s_size, s_size * 0.7, s_max_w, box_h * 0.25, 2, extra_tracking_em=0.04)
            sub_font_size = s_size
            if r.look["fx"].get("rule", True):
                rule_w = min(box_w * 0.34, t_size * 2.2)
                self.elems.append({"id": "rule", "rule": (rule_w, rule_h), "delay": 0.12 + len(t_lines) * r.stagger * 2 + 0.15, "text": ""})
                heights.append(rule_h)
            for i, ln in enumerate(s_lines):
                spr = r.sprite(ln, "body", sub_font_size, _mix(r.ink, r.muted, 0.25), extra_tracking_em=0.04, shadow="none", glow=0.0)
                self.elems.append({"id": f"sub{i}", "spr": spr, "delay": 0.12 + len(t_lines) * r.stagger * 2 + 0.3 + i * r.stagger,
                                   "text": ln, "trunc": s_trunc})
                heights.append(spr.ink_h)
        elif r.look["fx"].get("rule", True):
            rule_w = min(box_w * 0.34, t_size * 2.2)
            self.elems.append({"id": "rule", "rule": (rule_w, rule_h), "delay": 0.12 + len(t_lines) * r.stagger * 2 + 0.15, "text": ""})
            heights.append(rule_h)
        total = sum(heights) + gap * (len(heights) - 1)
        # optical centre sits a little above the geometric one
        cy = r.y0 + (r.y1 - r.y0) * 0.47
        y = cy - total / 2
        for el, h in zip(self.elems, heights):
            el["cy"] = y + h / 2
            y += h + gap * (0.55 if el["id"].startswith("title") else 1.0)
        # re-centre: titles were packed tighter than the generic gap
        used = (self.elems[-1]["cy"] + heights[-1] / 2) - (self.elems[0]["cy"] - heights[0] / 2)
        shift = cy - ((self.elems[0]["cy"] - heights[0] / 2) + used / 2)
        for el in self.elems:
            el["cy"] += shift
            if "spr" in el:
                w = el["spr"].ink_w
                el["cx"] = (r.x0 + w / 2) if align == "left" else (r.width / 2)
                el["w"], el["h"] = w, el["spr"].ink_h
            else:
                rw, rh = el["rule"]
                el["cx"] = (r.x0 + rw / 2) if align == "left" else (r.width / 2)
                el["w"], el["h"] = rw, rh
        for el in self.elems:
            if el["text"]:
                self._boxes.append(Box(el["id"], el["text"], el["cx"] - el["w"] / 2, el["cy"] - el["h"] / 2, el["cx"] + el["w"] / 2,
                                       el["cy"] + el["h"] / 2, 0.0, r.duration, bool(el.get("trunc"))))
        self.exit_s = min(r.out_s, max(0.2, r.duration * 0.25))
        self.in_s = min(r.in_s, max(0.25, r.duration * 0.3))

    def hero_time(self) -> float:
        return min(self.r.duration * 0.55, max(0.0, self.r.duration - self.exit_s - 0.1))

    def draw(self, canvas: Image.Image, t: float) -> None:
        r = self.r
        exit_start = r.duration - self.exit_s
        accents = (r.accent, r.accent2, r.look["fx"].get("rgb_split", 0.0) + 0.3)
        group = (r.width / 2, r.height / 2, r.punch(t))
        for el in self.elems:
            pose = self.layer_pose(t, el["delay"], enter_s=self.in_s, exit_start=exit_start, exit_s=self.exit_s)
            if "rule" in el:
                rw, rh = el["rule"]
                p = _clamp01((r.anim_time(t) - el["delay"]) / 0.45)
                width = max(0.0, rw * r.ease(p))
                if width < 1 or pose.opacity <= 0.004:
                    continue
                spr = Sprite(Image.new("RGBA", (int(width), int(rh)), _rgba(r.accent)), width, rh)
                x = el["cx"] - rw / 2 + width / 2 if r.look["align"] == "left" else el["cx"]
                blit(canvas, spr, x, el["cy"], Pose(opacity=pose.opacity, dy=pose.dy), group=group)
            else:
                blit(canvas, el["spr"], el["cx"], el["cy"], pose, group=group, frame_seed=r.seed * 1000 + r.step_of(t), accents=accents)


class LowerThirdScene(_Scene):
    def __init__(self, r: Renderer):
        super().__init__(r)
        d = r.spec["data"]
        m = r.m
        n_size = m * 0.058
        c_size = m * 0.034
        max_w = (r.x1 - r.x0) * (0.62 if r.width > r.height else 0.86)
        name = r.display_case(d["name"])
        n_size, n_lines, n_trunc = _wrap_fit(r, name, "display", n_size, n_size * 0.6, max_w, m * 0.12, 1)
        self.name_spr = r.sprite(n_lines[0], "display", n_size, r.ink, shadow="none", glow=0.0)
        self.cap_spr = None
        c_trunc = False
        if d.get("caption"):
            c_size, c_lines, c_trunc = _wrap_fit(r, d["caption"], "body", c_size, c_size * 0.7, max_w, m * 0.08, 1, extra_tracking_em=0.04)
            self.cap_spr = r.sprite(c_lines[0], "body", c_size, _mix(r.ink, r.muted, 0.3), extra_tracking_em=0.04, shadow="none", glow=0.0)
        pad = m * 0.022
        stripe = max(4, int(m * 0.008))
        text_w = max(self.name_spr.ink_w, self.cap_spr.ink_w if self.cap_spr else 0)
        text_h = self.name_spr.ink_h * 0.95 + (self.cap_spr.ink_h + m * 0.006 if self.cap_spr else 0)
        self.panel_w = int(text_w + pad * 2.4)
        self.panel_h = int(text_h + pad * 1.6)
        self.stripe = stripe
        self.pad = pad
        left = r.x0
        bottom = r.y1
        self.cx_stripe = left + stripe / 2
        self.cy = bottom - self.panel_h / 2
        self.panel_cx = left + stripe + self.panel_w / 2
        self.text_left = left + stripe + pad * 1.2
        self.name_cy = self.cy - self.panel_h / 2 + pad * 0.8 + self.name_spr.ink_h * 0.95 / 2
        self.cap_cy = self.name_cy + self.name_spr.ink_h * 0.95 / 2 + m * 0.006 + (self.cap_spr.ink_h / 2 if self.cap_spr else 0)
        panel = Image.new("RGBA", (self.panel_w, self.panel_h), _rgba(r.bg_rgb, 0.82))
        self.panel_spr = Sprite(panel, self.panel_w, self.panel_h)
        self.stripe_spr = Sprite(Image.new("RGBA", (stripe, self.panel_h), _rgba(r.accent)), stripe, self.panel_h)
        self._boxes.append(Box("name", d["name"], self.text_left, self.name_cy - self.name_spr.ink_h * 0.475,
                               self.text_left + self.name_spr.ink_w, self.name_cy + self.name_spr.ink_h * 0.475, 0.0, r.duration, n_trunc))
        if self.cap_spr:
            self._boxes.append(Box("caption", d["caption"], self.text_left, self.cap_cy - self.cap_spr.ink_h / 2,
                                   self.text_left + self.cap_spr.ink_w, self.cap_cy + self.cap_spr.ink_h / 2, 0.0, r.duration, c_trunc))
        self.exit_s = min(0.45, max(0.2, r.duration * 0.2))

    def hero_time(self) -> float:
        return min(self.r.duration * 0.5, max(0.0, self.r.duration - self.exit_s - 0.1))

    def draw(self, canvas: Image.Image, t: float) -> None:
        r = self.r
        ta = r.anim_time(t)
        ease = r.ease
        out_q = _clamp01((ta - (r.duration - self.exit_s)) / self.exit_s)
        stripe_p = ease(_clamp01(ta / 0.28)) * (1 - ease(_clamp01(out_q / 0.6)))
        panel_p = ease(_clamp01((ta - 0.10) / 0.42)) * (1 - ease(_clamp01((out_q - 0.1) / 0.9)))
        h = self.panel_h * stripe_p
        if h >= 1:
            spr = Sprite(self.stripe_spr.img.resize((self.stripe, max(1, int(h)))), self.stripe, h)
            blit(canvas, spr, self.cx_stripe, self.cy)
        if panel_p > 0.004:
            self._panel(canvas, panel_p)
        text_in = _clamp01((ta - 0.22) / 0.42)
        name = r.transition if r.transition in ("fade", "slide_up", "flicker", "glitch") else "slide_up"
        for spr, cy in ((self.name_spr, self.name_cy), (self.cap_spr, self.cap_cy)):
            if spr is None:
                continue
            tp = enter_pose(name, text_in, ease, r.pop, r.width, r.height, r.seed, r.step_of(t))
            ep = exit_pose("fade", _clamp01(out_q / 0.7), ease, r.width, r.height, r.seed, r.step_of(t))
            blit(canvas, spr, self.text_left + spr.ink_w / 2, cy, combine(tp, ep), frame_seed=r.seed * 1000 + r.step_of(t),
                 accents=(r.accent, r.accent2, 0.4))

    def _panel(self, canvas: Image.Image, p: float) -> None:
        w = max(1, int(self.panel_w * p))
        img = self.panel_spr.img.crop((0, 0, w, self.panel_h))
        spr = Sprite(img, w, self.panel_h)
        blit(canvas, spr, self.cx_stripe + self.stripe / 2 + w / 2, self.cy)


class OutroScene(_Scene):
    def __init__(self, r: Renderer):
        super().__init__(r)
        d = r.spec["data"]
        m = r.m
        align = r.look["align"]
        box_w = r.x1 - r.x0
        box_h = r.y1 - r.y0
        rows: list[dict[str, Any]] = []
        credits = d.get("credits") or []
        lines = d.get("lines") or []
        # budget: title takes up to 38% of the height, the rest is for the credits
        title_h = box_h * (0.34 if (credits or lines) else 0.6)
        if d.get("title"):
            t_size, t_lines, t_trunc = _wrap_fit(r, r.display_case(d["title"]), "display", m * 0.15, m * 0.06, box_w, title_h, 2)
            for i, ln in enumerate(t_lines):
                rows.append({"id": f"title{i}", "spr": r.sprite(ln, "display", t_size, r.ink), "gap": 0.0, "text": ln, "trunc": t_trunc})
            if d.get("subtitle"):
                s_size = min(m * 0.045, t_size * 0.32)
                _, s_lines, s_trunc = _wrap_fit(r, d["subtitle"], "body", s_size, s_size * 0.7, box_w, m * 0.1, 2, extra_tracking_em=0.05)
                for i, ln in enumerate(s_lines):
                    rows.append({"id": f"sub{i}", "spr": r.sprite(ln, "body", s_size, _mix(r.ink, r.muted, 0.3), extra_tracking_em=0.05,
                                                                  shadow="none", glow=0.0),
                                 "gap": s_size * (0.9 if i == 0 else 0.15), "text": ln, "trunc": s_trunc})
        n_rows = len(credits) * 2 + len(lines)
        remaining = box_h - sum(x["spr"].ink_h for x in rows) - 0.12 * m
        base = min(m * 0.04, remaining / max(1, n_rows * 1.6)) if n_rows else 0
        base = max(base, m * 0.018) if n_rows else 0
        for i, c in enumerate(credits):
            if c.get("role"):
                rows.append({"id": f"role{i}", "spr": r.sprite(r.display_case(c["role"]), "body", base * 0.62, r.accent,
                                                               extra_tracking_em=0.2, shadow="none", glow=0.0),
                             "gap": base * (1.1 if i else 1.6), "text": c["role"]})
            if c.get("name"):
                rows.append({"id": f"name{i}", "spr": r.sprite(c["name"], "body", base, r.ink, shadow="none", glow=0.0),
                             "gap": base * (0.15 if c.get("role") else 0.9), "text": c["name"]})
        for i, ln in enumerate(lines):
            rows.append({"id": f"line{i}", "spr": r.sprite(ln, "body", base, r.ink, shadow="none", glow=0.0), "gap": base * 0.7, "text": ln})
        total = sum(x["spr"].ink_h + x["gap"] for x in rows)
        y = r.y0 + max(0.0, (box_h - total) * 0.45)
        for k, row in enumerate(rows):
            y += row["gap"]
            spr = row["spr"]
            row["cy"] = y + spr.ink_h / 2
            y += spr.ink_h
            row["cx"] = (r.x0 + spr.ink_w / 2) if align == "left" else r.width / 2
            row["delay"] = 0.1 + k * max(0.12, r.stagger * 2.2)
            self._boxes.append(Box(row["id"], row["text"], row["cx"] - spr.ink_w / 2, row["cy"] - spr.ink_h / 2, row["cx"] + spr.ink_w / 2,
                                   row["cy"] + spr.ink_h / 2, 0.0, r.duration, bool(row.get("trunc"))))
        self.rows = rows
        self.exit_s = min(max(r.out_s, 0.8), max(0.3, r.duration * 0.3))
        self.in_s = min(r.in_s, 0.7)

    def hero_time(self) -> float:
        return min(self.r.duration * 0.6, max(0.0, self.r.duration - self.exit_s - 0.1))

    def draw(self, canvas: Image.Image, t: float) -> None:
        r = self.r
        group = (r.width / 2, r.height / 2, r.punch(t))
        accents = (r.accent, r.accent2, 0.3)
        for row in self.rows:
            pose = self.layer_pose(t, row["delay"], enter_s=self.in_s)
            blit(canvas, row["spr"], row["cx"], row["cy"], pose, group=group, frame_seed=r.seed * 1000 + r.step_of(t), accents=accents)
        # the card ends by fading to its own background (overlay mode: to transparent)
        q = _clamp01((t - (r.duration - self.exit_s)) / self.exit_s)
        if q > 0:
            fade = r.ease(q)
            if r.mode == "clip":
                canvas.alpha_composite(Image.new("RGBA", canvas.size, _rgba(r.bg_rgb, fade)))
            else:
                a = canvas.getchannel("A").point([int(v * (1 - fade)) for v in range(256)])
                canvas.putalpha(a)


class KineticScene(_Scene):
    def __init__(self, r: Renderer):
        super().__init__(r)
        d = r.spec["data"]
        m = r.m
        self.layout = d.get("layout", "line")
        self.upcoming = bool(d.get("upcoming"))
        self.lines = timed_lines(d)
        box_w = r.x1 - r.x0
        box_h = r.y1 - r.y0
        self.rows: list[dict[str, Any]] = []
        jitter = r.look["fx"].get("jitter_deg", 0.0)
        for li, line in enumerate(self.lines):
            nxt = self.lines[li + 1]["start_s"] if li + 1 < len(self.lines) else r.duration + 10
            last_end = max(line["end_s"], line["words"][-1]["end_s"] if line["words"] else line["end_s"])
            exit_start = min(last_end + 0.30, nxt)
            record = {"line": line, "words": [], "exit_start": exit_start, "exit_s": 0.20, "boxes": []}
            if not line["words"]:
                self.rows.append(record)
                continue
            texts = [r.display_case(w["text"]) for w in line["words"]]
            if self.layout == "word":
                for wi, (w, txt) in enumerate(zip(line["words"], texts)):
                    size, lns, trunc = _wrap_fit(r, txt, "display", m * 0.24, m * 0.08, box_w, box_h * 0.5, 1)
                    angle = self._angle(jitter, li, wi)
                    ink = r.sprite(lns[0], "display", size, r.ink, angle=angle)
                    acc = r.sprite(lns[0], "display", size, r.accent, angle=angle)
                    off = line["words"][wi + 1]["start_s"] if wi + 1 < len(line["words"]) else exit_start
                    record["words"].append({"ink": ink, "acc": acc, "t0": w["start_s"], "t1": w["end_s"], "cx": r.width / 2,
                                            "cy": r.y0 + box_h * 0.48, "off": min(max(off, w["end_s"] + 0.1), exit_start + 0.01),
                                            "text": lns[0]})
                    self._boxes.append(Box(f"l{li}w{wi}", lns[0], r.width / 2 - ink.ink_w / 2, r.y0 + box_h * 0.48 - ink.ink_h / 2,
                                           r.width / 2 + ink.ink_w / 2, r.y0 + box_h * 0.48 + ink.ink_h / 2, w["start_s"],
                                           min(record["words"][-1]["off"] + 0.2, r.duration), trunc))
            else:
                self._layout_line(record, texts, li, box_w, box_h, jitter)
            self.rows.append(record)

    def _angle(self, jitter: float, li: int, wi: int) -> float:
        if not jitter:
            return 0.0
        return round(random.Random(f"{self.r.seed}:{li}:{wi}").uniform(-jitter, jitter), 2)

    def _layout_line(self, record: dict[str, Any], texts: list[str], li: int, box_w: float, box_h: float, jitter: float) -> None:
        r = self.r
        m = r.m
        full = " ".join(texts)
        size, lns, trunc = _wrap_fit(r, full, "display", m * 0.13, m * 0.05, box_w, box_h * 0.7, 3, line_gap=1.0)
        font = r.font("display", size)
        tp = r.tracking_px(size)
        space = font.getlength(" ") + tp
        asc, desc = font.getmetrics()
        line_h = (asc + desc) * 1.0
        # the wrapped lines say how many words each row holds
        words = list(zip(record["line"]["words"], texts))
        rows: list[list[tuple[dict[str, Any], str]]] = []
        cursor = 0
        for ln in lns:
            count = len(ln.split())
            rows.append(words[cursor:cursor + count])
            cursor += count
        if cursor < len(words):  # an ellipsis ate words: the rest never shows
            record["truncated"] = True
        rows = [row for row in rows if row] or [words]
        if trunc:
            record["truncated"] = True
        block_h = line_h * len(rows)
        cy0 = r.y0 + (r.y1 - r.y0) * 0.5 - block_h / 2
        align = r.look["align"]
        xs0, ys0, xs1, ys1 = 1e9, 1e9, -1e9, -1e9
        for ri, row in enumerate(rows):
            widths = [_advance(font, txt, tp) for _, txt in row]
            row_w = sum(widths) + space * (len(row) - 1)
            x = (r.x0 if align == "left" else r.width / 2 - row_w / 2)
            cy = cy0 + ri * line_h + line_h / 2
            for (w, txt), wd in zip(row, widths):
                angle = self._angle(jitter, li, len(record["words"]))
                ink = r.sprite(txt, "display", size, r.ink, angle=angle)
                acc = r.sprite(txt, "display", size, r.accent, angle=angle)
                dim = r.sprite(txt, "display", size, _mix(r.bg_rgb, r.muted, 0.55), angle=angle, shadow="none", glow=0.0) if self.upcoming else None
                record["words"].append({"ink": ink, "acc": acc, "dim": dim, "t0": w["start_s"], "t1": w["end_s"], "cx": x + wd / 2, "cy": cy,
                                        "off": record["exit_start"], "text": txt})
                xs0, ys0, xs1, ys1 = min(xs0, x), min(ys0, cy - line_h / 2), max(xs1, x + wd), max(ys1, cy + line_h / 2)
                x += wd + space
        self._boxes.append(Box(f"l{li}", full, xs0, ys0 + line_h * 0.08, xs1, ys1 - line_h * 0.08, record["words"][0]["t0"],
                               min(r.duration, record["exit_start"] + record["exit_s"]), bool(record.get("truncated"))))

    def hero_time(self) -> float:
        # the moment the first line is fully sung, else a third of the way in
        for row in self.rows:
            if row["words"]:
                return min(self.r.duration, row["words"][-1]["t0"] + 0.55)
        return self.r.duration * 0.35

    def draw(self, canvas: Image.Image, t: float) -> None:
        r = self.r
        ta = r.anim_time(t)
        step = r.step_of(t)
        group = (r.width / 2, r.height * 0.5, r.punch(t))
        pop = r.pop
        for row in self.rows:
            if not row["words"]:
                continue
            first = row["words"][0]["t0"]
            if t < first - 0.02 or t > row["exit_start"] + row["exit_s"] + 0.05:
                continue
            q = _clamp01((ta - row["exit_start"]) / row["exit_s"])
            line_exit = Pose(opacity=1 - r.ease(q), dy=-r.ease(q) * 0.045 * r.height) if self.layout == "line" else Pose(opacity=1 - r.ease(q))
            for w in row["words"]:
                p = (ta - w["t0"]) / 0.24
                if self.layout == "word" and ta > w["off"]:
                    continue
                if p <= 0:
                    if w.get("dim") is not None and self.layout == "line":
                        blit(canvas, w["dim"], w["cx"], w["cy"], combine(Pose(opacity=0.5), line_exit), group=group)
                    continue
                pp = _clamp01(p)
                base = enter_pose(r.transition if r.transition != "wipe" else "scale_pop", pp, r.ease, pop, r.width, r.height, r.seed, step) \
                    if r.transition in ("glitch", "flicker", "slide_up", "fade", "wipe", "scale_pop", "mask_reveal") else Pose()
                if r.transition in ("wipe", "mask_reveal"):
                    base = Pose(opacity=min(1.0, pp * 5), dy=(1 - r.ease(pp)) * 0.03 * r.height, scale=0.62 + 0.38 * pop(pp))
                elif r.transition in ("fade", "slide_up", "scale_pop"):
                    base = Pose(opacity=min(1.0, pp * 5), dy=(1 - r.ease(pp)) * 0.03 * r.height, scale=0.6 + 0.4 * pop(pp))
                pose = combine(base, line_exit)
                # the word being sung is lit in the accent colour, then settles to ink
                if ta < w["t0"] + 0.06:
                    hl = _clamp01((ta - w["t0"]) / 0.06)
                elif ta <= w["t1"]:
                    hl = 1.0
                else:
                    hl = _clamp01(1 - (ta - w["t1"]) / 0.18)
                accents = (r.accent, r.accent2, r.look["fx"].get("rgb_split", 0.0) + 0.3)
                fs = r.seed * 1000 + step
                if hl < 0.999:
                    blit(canvas, w["ink"], w["cx"], w["cy"], pose, group=group, frame_seed=fs, accents=accents)
                if hl > 0.004:
                    hp = Pose(pose.opacity * hl, pose.dx, pose.dy, pose.scale, pose.clip, pose.glitch)
                    blit(canvas, w["acc"], w["cx"], w["cy"], hp, group=group, frame_seed=fs, accents=accents)


_SCENES: dict[str, Callable[[Renderer], _Scene]] = {
    "title_card": TitleScene, "lower_third": LowerThirdScene, "outro_card": OutroScene, "kinetic_lyrics": KineticScene,
}


# ------------------------------------------------------------------ checks

def check_geometry(spec: dict[str, Any], width: int, height: int, *, band_top: Optional[float] = None,
                   captions: Optional[list[tuple[float, float]]] = None, fps: int = 30) -> list[dict[str, Any]]:
    """Problems with where a graphic's text sits, from its exact text boxes (no OCR):

    * ``text_clipped``: the box (grown by the largest scale the animation reaches) leaves the frame;
    * ``outside_safe``: it leaves the safe area (platform buttons, rounded screens);
    * ``text_in_subtitle_band``: it reaches below ``band_top`` (a fraction of the height) while
      burned-in captions are on screen (``captions``: their [start, end] in spec time; the
      graphic's own ``suppress_captions`` clears them);
    * ``text_truncated``: the text did not fit even at the smallest size and was cut with an ellipsis.
    """
    r = Renderer(spec, width, height, fps)
    problems: list[dict[str, Any]] = []
    grow = r.max_scale
    suppressed = captions_suppressed(r.spec)
    tol = 1.5
    for box in r.boxes():
        cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
        hw, hh = (box.x1 - box.x0) / 2 * grow, (box.y1 - box.y0) / 2 * grow
        x0, x1, y0, y1 = cx - hw, cx + hw, cy - hh, cy + hh
        base = {"box": box.id, "text": box.text[:60], "t0": round(box.t0, 2), "t1": round(box.t1, 2)}
        if x0 < -tol or y0 < -tol or x1 > width + tol or y1 > height + tol:
            problems.append({**base, "code": "text_clipped",
                             "detail": f"leaves the {width}x{height} frame (box {x0:.0f},{y0:.0f}-{x1:.0f},{y1:.0f})"})
        elif x0 < r.x0 - tol or x1 > r.x1 + tol or y0 < r.y0 - tol or y1 > r.y1 + tol:
            problems.append({**base, "code": "outside_safe", "detail": f"leaves the safe area (box {x0:.0f},{y0:.0f}-{x1:.0f},{y1:.0f})"})
        if box.truncated:
            problems.append({**base, "code": "text_truncated", "detail": "did not fit at the smallest size and was cut"})
        if band_top is not None and not suppressed and y1 > band_top * height + tol:
            overlapping = [c for c in (captions or []) if c[0] < box.t1 and c[1] > box.t0]
            if captions is None or overlapping:
                problems.append({**base, "code": "text_in_subtitle_band",
                                 "detail": f"reaches y={y1:.0f}, below the captions' top at y={band_top * height:.0f}"})
    return problems


def frame_hash(img: Image.Image) -> str:
    return hashlib.sha256(img.tobytes()).hexdigest()[:16]


def determinism_check(spec: dict[str, Any], width: int, height: int, fps: int = 30, times: Optional[list[float]] = None) -> dict[str, Any]:
    """Render a few frames twice, with two separate renderers (a cache or a leftover state that
    changes a frame would show), and compare pixels."""
    clean = normalise_graphic(spec)
    dur = float(clean.get("duration") or 4.0)
    times = times or [round(dur * f, 3) for f in (0.2, 0.5, 0.85)]
    a, b = Renderer(clean, width, height, fps), Renderer(clean, width, height, fps)
    frames = []
    for t in times:
        ha, hb = frame_hash(a.frame(t)), frame_hash(b.frame(t))
        frames.append({"t": t, "hash": ha, "same": ha == hb})
    return {"deterministic": all(f["same"] for f in frames), "frames": frames}


# --------------------------------------------------------------- rendering

def frame_count(duration: float, fps: int) -> int:
    return max(1, int(round(float(duration) * fps)))


def still_png(spec: dict[str, Any], width: int, height: int, at_s: Optional[float] = None, fps: int = 30,
              backdrop: bool = True) -> bytes:
    """One frame as a PNG. An overlay is shown over a mid-grey checker-free backdrop when `backdrop`
    (so it can be judged by eye); otherwise it keeps its alpha."""
    r = Renderer(spec, width, height, fps)
    t = r.hero_time() if at_s is None else float(at_s)
    img = r.frame(t)
    if backdrop and r.mode == "overlay":
        bg = Image.new("RGBA", img.size, (38, 40, 48, 255))
        yy = np.linspace(0, 1, img.height, dtype=np.float32)[:, None]
        shade = (np.broadcast_to(34 + 38 * yy, (img.height, img.width))).astype(np.uint8)
        bg = Image.merge("RGBA", (Image.fromarray(shade), Image.fromarray(shade), Image.fromarray(np.clip(shade + 8, 0, 255)),
                                  Image.new("L", img.size, 255)))
        bg.alpha_composite(img)
        img = bg
    buf = io.BytesIO()
    img.convert("RGB" if backdrop or r.mode == "clip" else "RGBA").save(buf, format="PNG")
    return buf.getvalue()


_ALPHA_CODEC: Optional[list[str]] = None


def alpha_codec_args(ffmpeg: str) -> list[str]:
    """The encoder arguments for a video with alpha. QuickTime Animation (`qtrle`, RLE, lossless)
    is the choice: every ffmpeg build has it (the imageio-ffmpeg one too), it encodes about as fast
    as it is fed, mostly-empty frames collapse to almost nothing and `overlay` reads it natively.
    ProRes 4444 is 5-15x slower to write and 2x bigger for text on transparency; VP9 with alpha
    is small but ffmpeg's own VP9 decoder drops the alpha. Without qtrle, PNG-in-MOV is used."""
    global _ALPHA_CODEC
    if _ALPHA_CODEC is None:
        listing = procutil.run([ffmpeg, "-hide_banner", "-encoders"], text=True, timeout=20)
        text = (listing.stdout or "") if listing.returncode == 0 else ""
        if re.search(r"\bqtrle\b", text):
            _ALPHA_CODEC = ["-c:v", "qtrle", "-pix_fmt", "argb"]
        elif re.search(r"(?m)^\s*\S+\s+png\b", text):
            _ALPHA_CODEC = ["-c:v", "png", "-compression_level", "1", "-pix_fmt", "rgba"]
        else:
            raise GraphicError("no_alpha_codec", "this ffmpeg has neither the QuickTime Animation nor the PNG encoder, so it "
                                                 "cannot write a video with transparency")
    return list(_ALPHA_CODEC)


# the clip encoder matches the clips the montage makes from stills and videos (same x264 preset and
# pixel format), so the concat demuxer can join them without re-encoding
CLIP_CODEC_ARGS = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-pix_fmt", "yuv420p"]


def render_video(spec: dict[str, Any], out_path: Path, *, width: int, height: int, fps: int = 30, frames: Optional[int] = None,
                 start_frame: int = 0, alpha: Optional[bool] = None, codec_args: Optional[list[str]] = None,
                 progress: Optional[Callable[[float], None]] = None, should_cancel: Optional[Callable[[], bool]] = None,
                 silent_frames: int = 0) -> dict[str, Any]:
    """Render a graphic to a video file by piping raw frames to ffmpeg.

    `frames` frames are drawn from spec frame `start_frame` on (a montage clip that is one cut of a
    longer window starts later in it); the default is the whole duration. `alpha` (default: the
    graphic is an overlay) writes a `.mov` with transparency; otherwise H.264 yuv420p `.mp4`.
    `silent_frames` leading frames are fully transparent without being drawn (an overlay that
    begins late). Returns {"path", "frames", "fps", "width", "height", "duration_s"}.
    """
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise GraphicError("no_ffmpeg", "ffmpeg not found (install ffmpeg or the imageio-ffmpeg wheel)")
    if width % 2 or height % 2:
        raise GraphicError("bad_graphic", "the frame size must be even (H.264 and ProRes need it)")
    r = Renderer(spec, width, height, fps)
    if alpha is None:
        alpha = r.mode == "overlay"
    total = frames if frames is not None else frame_count(r.duration, fps) - start_frame
    total = max(1, int(total))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix_in = "rgba" if alpha else "rgb24"
    enc = alpha_codec_args(ffmpeg) if alpha else (codec_args or CLIP_CODEC_ARGS)
    cmd = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", pix_in, "-s", f"{width}x{height}",
           "-r", str(fps), "-i", "-", "-an", *enc]
    if not alpha:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-frames:v", str(total + silent_frames), str(out_path)]
    blank = bytes(width * height * 4) if alpha else None
    with tempfile.TemporaryFile(mode="w+b") as err:
        proc = procutil.popen(cmd, stdin=procutil.subprocess.PIPE, stdout=procutil.subprocess.DEVNULL, stderr=err)
        assert proc.stdin is not None
        try:
            for i in range(silent_frames):
                proc.stdin.write(blank or b"")
            for i in range(total):
                if should_cancel and should_cancel():
                    hlproc.kill_tree(proc, grace_s=1.0)
                    proc.wait()
                    out_path.unlink(missing_ok=True)
                    raise GraphicCancelled()
                t = (start_frame + i) / fps
                img = r.frame(t)
                proc.stdin.write(img.tobytes() if alpha else img.convert("RGB").tobytes())
                if progress and (i % 8 == 0 or i == total - 1):
                    progress((i + 1) / total)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # ffmpeg died; its stderr says why, below
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        code = proc.wait()
        if code != 0:
            err.seek(0)
            out_path.unlink(missing_ok=True)
            raise GraphicError("render_failed", f"ffmpeg exited {code}: {err.read().decode('utf-8', 'replace')[-600:]}")
    return {"path": str(out_path), "frames": total + silent_frames, "fps": fps, "width": width, "height": height,
            "duration_s": round((total + silent_frames) / fps, 3), "alpha": bool(alpha)}


def render_overlay_track(clips: list[dict[str, Any]], out_path: Path, *, width: int, height: int, fps: int,
                         progress: Optional[Callable[[float], None]] = None,
                         should_cancel: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """One alpha video holding every overlay of a timeline: transparent where nothing is on
    screen, each graphic drawn at its own time (`clips`: [{start_s, end_s, graphic}], graphics may
    overlap and are composed in list order). It runs from time 0 to the end of the last overlay;
    the montage lets the cut play on after it."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise GraphicError("no_ffmpeg", "ffmpeg not found (install ffmpeg or the imageio-ffmpeg wheel)")
    if width % 2 or height % 2:
        raise GraphicError("bad_graphic", "the frame size must be even")
    if not clips:
        raise GraphicError("bad_graphic", "no overlays to render")
    items = []
    for c in clips:
        start, end = float(c["start_s"]), float(c["end_s"])
        spec = normalise_graphic(c["graphic"])
        spec["duration"] = round(end - start, 4)
        spec["mode"] = "overlay"
        items.append({"first": int(round(start * fps)), "last": int(round(end * fps)), "renderer": Renderer(spec, width, height, fps)})
    total = max(i["last"] for i in items)
    enc = alpha_codec_args(ffmpeg)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-r", str(fps),
           "-i", "-", "-an", *enc, "-frames:v", str(total), str(out_path)]
    blank = bytes(width * height * 4)
    with tempfile.TemporaryFile(mode="w+b") as err:
        proc = procutil.popen(cmd, stdin=procutil.subprocess.PIPE, stdout=procutil.subprocess.DEVNULL, stderr=err)
        assert proc.stdin is not None
        try:
            for f in range(total):
                if should_cancel and should_cancel():
                    hlproc.kill_tree(proc, grace_s=1.0)
                    proc.wait()
                    out_path.unlink(missing_ok=True)
                    raise GraphicCancelled()
                active = [i for i in items if i["first"] <= f < i["last"]]
                if not active:
                    proc.stdin.write(blank)
                else:
                    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                    for i in active:
                        canvas.alpha_composite(i["renderer"].frame((f - i["first"]) / fps))
                    proc.stdin.write(canvas.tobytes())
                if progress and (f % 8 == 0 or f == total - 1):
                    progress((f + 1) / total)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        code = proc.wait()
        if code != 0:
            err.seek(0)
            out_path.unlink(missing_ok=True)
            raise GraphicError("render_failed", f"ffmpeg exited {code}: {err.read().decode('utf-8', 'replace')[-600:]}")
    return {"path": str(out_path), "frames": total, "fps": fps, "width": width, "height": height, "duration_s": round(total / fps, 3)}
