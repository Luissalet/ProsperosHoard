"""Deterministic layout renderer (Pillow only, no browser).

A layout is a JSON spec: `{"width", "height", "layers": [...]}`. Layer
types: `rect`, `image`, `text`, `holo` (procedural iridescent foil),
`grain`, `vignette` (radial edge darkening), `frame`, `badge`. Every layer may reference a named `field` whose
value is filled in at render time from the caller's `fields` dict, so one
layout can be reused for many photocards/covers.

`qr` layers are recognised but not rendered (documented boundary - no QR
library is in the dependency set); a layout containing one renders a
plain placeholder box instead of failing. `image` and `holo` layers take a
`blend` of normal/screen/overlay/multiply; `text` layers take font, size,
colour, letter_spacing (em), line_height, stroke, shadow, alignment and a
box they shrink to fit.
"""

from __future__ import annotations

import hashlib
import io
import math
import random
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONTS_DIR = Path(__file__).parent / "fonts"

_FONT_FILES = {
    "inter": FONTS_DIR / "Inter" / "Inter.ttf",
    "bebas-neue": FONTS_DIR / "BebasNeue" / "BebasNeue-Regular.ttf",
    "playfair-display": FONTS_DIR / "PlayfairDisplay" / "PlayfairDisplay.ttf",
    "space-grotesk": FONTS_DIR / "SpaceGrotesk" / "SpaceGrotesk.ttf",
    "caveat": FONTS_DIR / "Caveat" / "Caveat.ttf",
    "special-elite": FONTS_DIR / "SpecialElite" / "SpecialElite-Regular.ttf",
}

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def available_fonts() -> list[str]:
    return sorted(_FONT_FILES.keys())


def get_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key in _font_cache:
        return _font_cache[key]
    path = _FONT_FILES.get(name, _FONT_FILES["inter"])
    font = ImageFont.truetype(str(path), size=size)
    _font_cache[key] = font
    return font


def _resolve(value: Any, fields: dict[str, Any]) -> Any:
    """`{"field": "member_name"}` -> fields["member_name"]; anything else passes through."""
    if isinstance(value, dict) and "field" in value:
        return fields.get(value["field"], value.get("default"))
    return value


class DesignError(ValueError):
    pass


def _hex(colour: Any) -> tuple[int, int, int, int]:
    """'#rgb', '#rrggbb' or '#rrggbbaa' -> RGBA. Anything else raises a
    DesignError naming the bad value."""
    text = str(colour or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) == 6:
        text += "ff"
    try:
        if len(text) != 8:
            raise ValueError
        r, g, b, a = (int(text[i : i + 2], 16) for i in (0, 2, 4, 6))
    except ValueError:
        raise DesignError(f"invalid colour {str(colour)[:20]!r}: use a hex colour like '#ff4d8d'") from None
    return (r, g, b, a)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int, spacing: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if _text_width(draw, trial, font, spacing) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, spacing: float = 0.0) -> float:
    if not text:
        return 0.0
    bbox = draw.textbbox((0, 0), text, font=font)
    return (bbox[2] - bbox[0]) + spacing * max(0, len(text) - 1)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_name: str, box: tuple[int, int], max_size: int, min_size: int = 10,
              line_height: float = 1.2, spacing_em: float = 0.0) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink-to-fit: the largest size (>= min_size) whose wrapped text fits
    `box`. If even `min_size` overflows, the lines that fit are kept and the
    last one ends with an ellipsis (never text spilling out of its box)."""
    width, height = box
    for size in range(max_size, min_size - 1, -2):
        font = get_font(font_name, size)
        lines = _wrap(draw, text, font, width, spacing_em * size)
        lh = _line_px(font) * line_height
        widest = max((_text_width(draw, ln, font, spacing_em * size) for ln in lines), default=0)
        if lh * len(lines) <= height + 1 and widest <= width:
            return font, lines
    font = get_font(font_name, min_size)
    lines = _wrap(draw, text, font, width, spacing_em * min_size)
    lh = _line_px(font) * line_height
    keep = max(1, int(height // lh))
    if len(lines) > keep:
        lines = lines[:keep]
        last = lines[-1]
        while last and _text_width(draw, last + "...", font, spacing_em * min_size) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "..."
    return font, lines


def _line_px(font: ImageFont.FreeTypeFont) -> float:
    ascent, descent = font.getmetrics()
    return float(ascent + descent)


def _draw_holo(size: tuple[int, int], seed: int, angle: float = 35.0) -> Image.Image:
    """Procedural iridescent foil: a rainbow gradient along `angle`, a
    second slower wave for the oil-slick shimmer, and seeded noise. Numpy,
    deterministic for a given seed."""
    import numpy as np

    width, height = max(1, size[0]), max(1, size[1])
    rng = np.random.default_rng(seed)
    hue_shift = float(rng.uniform(0, 1))
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    a = math.radians(angle)
    t = (xx * math.cos(a) + yy * math.sin(a)) / float(width + height)
    wave = 0.08 * np.sin((xx * 0.9 - yy * 1.3) / max(width, height) * 12.0 + hue_shift * 6.28)
    hue = (hue_shift + t * 1.6 + wave) % 1.0
    s_, v_ = 0.55, 0.97
    h6 = hue * 6.0
    i = np.floor(h6).astype(int) % 6
    f = h6 - np.floor(h6)
    p = v_ * (1 - s_)
    q = v_ * (1 - s_ * f)
    tt = v_ * (1 - s_ * (1 - f))
    r = np.choose(i, [v_ + 0 * f, q, p + 0 * f, p + 0 * f, tt, v_ + 0 * f])
    g = np.choose(i, [tt, v_ + 0 * f, v_ + 0 * f, q, p + 0 * f, p + 0 * f])
    b = np.choose(i, [p + 0 * f, p + 0 * f, tt, v_ + 0 * f, v_ + 0 * f, q])
    rgb = np.stack([r, g, b], axis=-1) * 255.0
    noise = rng.normal(0, 10.0, size=(height, width, 1))
    rgb = np.clip(rgb + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(rgb, "RGB")


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(h / 360, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


def _blend_onto(canvas: Image.Image, layer_rgba: Image.Image, pos: tuple[int, int], mode: str) -> None:
    """Composite `layer_rgba` at `pos` with `normal`, `screen`, `overlay` or
    `multiply`, respecting the layer's alpha."""
    if mode not in ("screen", "overlay", "multiply"):
        canvas.alpha_composite(layer_rgba, pos)
        return
    from PIL import ImageChops

    x, y = pos
    box = (x, y, x + layer_rgba.width, y + layer_rgba.height)
    base = canvas.crop(box)
    base_rgb = base.convert("RGB")
    top_rgb = layer_rgba.convert("RGB")
    if mode == "screen":
        mixed = ImageChops.screen(base_rgb, top_rgb)
    elif mode == "multiply":
        mixed = ImageChops.multiply(base_rgb, top_rgb)
    else:
        mixed = ImageChops.overlay(base_rgb, top_rgb)
    mixed = mixed.convert("RGBA")
    mixed.putalpha(base.split()[3])
    out = Image.composite(mixed, base, layer_rgba.split()[3])
    canvas.paste(out, box)


def render_layout(layout: dict[str, Any], fields: dict[str, Any], asset_resolver) -> Image.Image:
    """`asset_resolver(asset_id) -> Path | None` loads an image layer's source."""
    width, height = layout["width"], layout["height"]
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    for layer in layout.get("layers", []):
        ltype = layer["type"]
        if ltype == "rect":
            _layer_rect(canvas, layer, fields)
        elif ltype == "image":
            _layer_image(canvas, layer, fields, asset_resolver)
        elif ltype == "text":
            _layer_text(canvas, layer, fields)
        elif ltype == "holo":
            _layer_holo(canvas, layer, fields)
        elif ltype == "grain":
            _layer_grain(canvas, layer)
        elif ltype == "vignette":
            _layer_vignette(canvas, layer)
        elif ltype == "frame":
            _layer_frame(canvas, layer, fields)
        elif ltype == "badge":
            _layer_badge(canvas, layer, fields)
        elif ltype == "qr":
            _layer_qr_placeholder(canvas, layer)
        # unknown layer types are skipped, not fatal

    if layout.get("radius"):
        canvas = _round_corners(canvas, layout["radius"])
    return canvas


def _box(layer: dict, fields: dict[str, Any]) -> tuple[int, int, int, int]:
    x = int(_resolve(layer.get("x", 0), fields))
    y = int(_resolve(layer.get("y", 0), fields))
    w = int(_resolve(layer.get("w", 100), fields))
    h = int(_resolve(layer.get("h", 100), fields))
    return x, y, w, h


def _layer_rect(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    x, y, w, h = _box(layer, fields)
    radius = int(_resolve(layer.get("radius", 0), fields))
    gradient = layer.get("gradient")
    if gradient:
        _layer_gradient(canvas, (x, y, w, h), [_hex(_resolve(c, fields)) for c in gradient.get("colours", [])],
                        gradient.get("direction", "vertical"), radius)
        return
    fill = _hex(_resolve(layer.get("fill", "#000000ff"), fields))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if radius:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill)
    else:
        draw.rectangle([x, y, x + w, y + h], fill=fill)
    canvas.alpha_composite(overlay)


def _layer_gradient(canvas: Image.Image, box: tuple[int, int, int, int], colours: list[tuple[int, int, int, int]],
                    direction: str, radius: int) -> None:
    import numpy as np

    x, y, w, h = box
    if len(colours) < 2 or w <= 0 or h <= 0:
        return
    steps = np.linspace(0.0, 1.0, h if direction == "vertical" else w)
    stops = np.linspace(0.0, 1.0, len(colours))
    cols = np.array(colours, dtype=np.float32)
    ramp = np.stack([np.interp(steps, stops, cols[:, k]) for k in range(4)], axis=-1)
    if direction == "vertical":
        arr = np.repeat(ramp[:, None, :], w, axis=1)
    else:
        arr = np.repeat(ramp[None, :, :], h, axis=0)
    grad = Image.fromarray(arr.astype("uint8"), "RGBA")
    if radius:
        grad = _round_corners(grad, radius)
    canvas.alpha_composite(grad, (x, y))


def subject_centering(src: Image.Image, target: tuple[int, int],
                      default: tuple[float, float] = (0.5, 0.35)) -> tuple[float, float]:
    """`ImageOps.fit` centering that keeps a studio subject in frame.

    A photo shot on a plain backdrop (a photocard, a portrait) is cropped
    to the card's aspect; a fixed focal point cuts the head off a tall
    subject that fills the frame. The backdrop colour is read from the
    left and right edges, the subject is every clearly different pixel,
    and the crop keeps the subject's top (with a little headroom) or, for
    a sideways crop, centres on it. Busy backgrounds with no clear subject
    fall back to `default`."""
    import numpy as np

    tw, th = target
    if src.width <= 0 or src.height <= 0 or tw <= 0 or th <= 0:
        return default
    sw = 64
    sh = max(8, round(sw * src.height / src.width))
    arr = np.asarray(src.convert("RGB").resize((sw, sh), Image.BILINEAR), dtype=np.float32)
    edges = np.concatenate([arr[:, :2].reshape(-1, 3), arr[:, -2:].reshape(-1, 3)])
    backdrop = np.median(edges, axis=0)
    if float(np.mean(np.linalg.norm(edges - backdrop, axis=1))) > 30:  # the edges are not a plain backdrop
        return default
    mask = np.linalg.norm(arr - backdrop, axis=2) > 48
    rows = np.where(mask.sum(axis=1) >= max(2, sw // 32))[0]
    cols = np.where(mask.sum(axis=0) >= max(2, sh // 32))[0]
    if rows.size == 0 or cols.size == 0:
        return default
    cx, cy = default
    src_ratio, dst_ratio = src.width / src.height, tw / th
    if src_ratio < dst_ratio:  # taller than the card: the crop removes rows
        scaled_h = src.height * tw / src.width
        excess = scaled_h - th
        if excess > 1:
            top = rows.min() / sh * scaled_h
            bottom = (rows.max() + 1) / sh * scaled_h
            headroom = 0.04 * th
            if bottom - top + 2 * headroom <= th:  # the whole subject fits: centre on it
                y0 = (top + bottom) / 2 - th / 2
            else:  # a tall subject: keep its top (the head), lose the feet
                y0 = top - headroom
            cy = min(max(y0, 0.0), excess) / excess
    elif src_ratio > dst_ratio:  # wider than the card: the crop removes columns
        scaled_w = src.width * th / src.height
        excess = scaled_w - tw
        if excess > 1:
            centre = (cols.min() + cols.max() + 1) / 2 / sw * scaled_w
            cx = min(max(centre - tw / 2, 0.0), excess) / excess
    return (min(1.0, max(0.0, cx)), min(1.0, max(0.0, cy)))


def _layer_image(canvas: Image.Image, layer: dict, fields: dict[str, Any], asset_resolver) -> None:
    x, y, w, h = _box(layer, fields)
    asset_id = _resolve(layer.get("asset"), fields)
    path = asset_resolver(asset_id) if asset_id else None
    if path is None or not Path(path).is_file():
        if layer.get("placeholder", True) is False:
            return
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([x, y, x + w, y + h], fill=(40, 40, 46, 255))
        canvas.alpha_composite(overlay)
        return
    with Image.open(path) as opened:
        src = opened.convert("RGBA")
    fit = layer.get("fit", "cover")
    if fit == "cover":
        focal = _resolve(layer.get("focal", [0.5, 0.4]), fields) or [0.5, 0.4]
        if focal == "subject":
            fallback = layer.get("focal_fallback", [0.5, 0.35])
            centering = subject_centering(src, (w, h), (float(fallback[0]), float(fallback[1])))
        else:
            try:
                centering = (min(1.0, max(0.0, float(focal[0]))), min(1.0, max(0.0, float(focal[1]))))
            except (TypeError, ValueError, IndexError):
                centering = (0.5, 0.4)
        src = ImageOps.fit(src, (w, h), method=Image.LANCZOS, centering=centering)
    else:
        src = ImageOps.contain(src, (w, h), method=Image.LANCZOS)
        pad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        pad.alpha_composite(src, ((w - src.width) // 2, (h - src.height) // 2))
        src = pad
    blur = float(layer.get("blur", 0) or 0)
    if blur > 0:
        src = src.filter(ImageFilter.GaussianBlur(blur))
    radius = int(_resolve(layer.get("radius", 0), fields))
    if radius:
        src = _round_corners(src, radius)
    opacity = float(_resolve(layer.get("opacity", 1.0), fields))
    if opacity < 1.0:
        alpha = src.split()[3].point(lambda p: int(p * opacity))
        src.putalpha(alpha)
    _blend_onto(canvas, src, (x, y), layer.get("blend", "normal"))


def _layer_text(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    x, y, w, h = _box(layer, fields)
    text = str(_resolve(layer.get("text", ""), fields) or "")
    if layer.get("uppercase"):
        text = text.upper()
    if not text.strip():
        return
    font_name = layer.get("font", "inter")
    colour = _hex(_resolve(layer.get("colour", "#ffffffff"), fields))
    align = layer.get("align", "left")
    max_size = int(layer.get("size", 48))
    line_height = float(layer.get("line_height", 1.2))
    spacing_em = float(layer.get("letter_spacing", 0.0))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if layer.get("columns"):
        _draw_columns(canvas, overlay, draw, layer, text, font_name, colour, (x, y, w, h), max_size, line_height, spacing_em)
        return
    if layer.get("auto_fit", True):
        font, lines = _fit_text(draw, text, font_name, (w, h), max_size, line_height=line_height, spacing_em=spacing_em)
    else:
        font = get_font(font_name, max_size)
        lines = text.split("\n")
    spacing = spacing_em * font.size
    lh = _line_px(font) * line_height
    total = lh * len(lines)
    valign = layer.get("valign", "top")
    if valign == "middle":
        cursor_y = y + max(0.0, (h - total) / 2)
    elif valign == "bottom":
        cursor_y = y + max(0.0, h - total)
    else:
        cursor_y = float(y)
    stroke = layer.get("stroke")
    shadow = layer.get("shadow")
    for line in lines:
        line_w = _text_width(draw, line, font, spacing)
        if align == "center":
            line_x = x + (w - line_w) / 2
        elif align == "right":
            line_x = x + w - line_w
        else:
            line_x = float(x)
        kwargs: dict[str, Any] = {}
        if stroke:
            kwargs["stroke_width"] = int(stroke.get("width", 2))
            kwargs["stroke_fill"] = _hex(stroke.get("colour", "#000000ff"))
        if shadow:
            sdx, sdy = int(shadow.get("dx", 0)), int(shadow.get("dy", 4))
            _draw_line(draw, (line_x + sdx, cursor_y + sdy), line, font, _hex(shadow.get("colour", "#00000099")), spacing, {})
        _draw_line(draw, (line_x, cursor_y), line, font, colour, spacing, kwargs)
        cursor_y += lh
    if shadow and int(shadow.get("blur", 0)) > 0:
        # blur only makes sense on a separate shadow pass; approximate with a
        # light blur of the whole text layer underneath the sharp text
        blurred = overlay.filter(ImageFilter.GaussianBlur(int(shadow["blur"])))
        canvas.alpha_composite(blurred)
    canvas.alpha_composite(overlay)


def _draw_columns(canvas: Image.Image, overlay: Image.Image, draw: ImageDraw.ImageDraw, layer: dict, text: str,
                  font_name: str, colour: tuple[int, int, int, int], box: tuple[int, int, int, int], max_size: int,
                  line_height: float, spacing_em: float) -> None:
    """A tracklist-style table: each line splits on a tab or 2+ spaces into
    number / title / time; the first column sits at x, the middle one at
    x + `columns.indent` (fraction of the width), the last is right-aligned.
    One size for every row, the largest at which all rows fit the box."""
    import re as _re

    x, y, w, h = box
    rows = [[c for c in _re.split(r"\t+|\s{2,}", ln.strip()) if c] for ln in text.split("\n") if ln.strip()]
    if not rows:
        return
    indent = float((layer.get("columns") or {}).get("indent", 0.1)) * w if isinstance(layer.get("columns"), dict) else 0.1 * w
    muted = _hex(layer.get("muted_colour", "#ffffff99"))
    size = max_size
    while size > 10:
        font = get_font(font_name, size)
        sp = spacing_em * size
        widest = max(indent + sum(_text_width(draw, c, font, sp) for c in r[1:]) + size for r in rows)
        if widest <= w and _line_px(font) * line_height * len(rows) <= h:
            break
        size -= 2
    font = get_font(font_name, size)
    sp = spacing_em * size
    lh = _line_px(font) * line_height
    cy = float(y)
    for r in rows:
        if len(r) == 1:
            _draw_line(draw, (x, cy), r[0], font, colour, sp, {})
        else:
            _draw_line(draw, (x, cy), r[0], font, muted, sp, {})
            if len(r) >= 3:
                last = r[-1]
                _draw_line(draw, (x + w - _text_width(draw, last, font, sp), cy), last, font, muted, sp, {})
                middle = " ".join(r[1:-1])
            else:
                middle = r[1]
            _draw_line(draw, (x + indent, cy), middle, font, colour, sp, {})
        cy += lh
    canvas.alpha_composite(overlay)


def _draw_line(draw: ImageDraw.ImageDraw, xy: tuple[float, float], line: str, font: ImageFont.FreeTypeFont,
               fill: tuple[int, int, int, int], spacing: float, kwargs: dict[str, Any]) -> None:
    if not spacing:
        draw.text(xy, line, font=font, fill=fill, **kwargs)
        return
    cx, cy = xy
    for ch in line:
        draw.text((cx, cy), ch, font=font, fill=fill, **kwargs)
        cx += draw.textlength(ch, font=font) + spacing


def _layer_holo(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    x, y, w, h = _box(layer, fields)
    seed = int(_resolve(layer.get("seed", 0), fields))
    holo = _draw_holo((w, h), seed, angle=float(layer.get("angle", 35.0))).convert("RGBA")
    opacity = float(layer.get("opacity", 0.35))
    alpha = holo.split()[3].point(lambda p: int(255 * opacity))
    holo.putalpha(alpha)
    _blend_onto(canvas, holo, (x, y), layer.get("blend", "screen"))


def _layer_grain(canvas: Image.Image, layer: dict) -> None:
    """Seeded monochrome film grain (deterministic, unlike effect_noise)."""
    import numpy as np

    amount = float(layer.get("amount", 8))
    rng = np.random.default_rng(int(layer.get("seed", 1234)))
    noise = np.clip(128 + rng.normal(0, amount * 6, size=(canvas.height, canvas.width)), 0, 255).astype("uint8")
    grey = Image.fromarray(noise, "L")
    alpha = Image.new("L", canvas.size, int(min(60, 4 + amount * 2)))
    grain = Image.merge("RGBA", (grey, grey, grey, alpha))
    _blend_onto(canvas, grain, (0, 0), "overlay")


def _layer_vignette(canvas: Image.Image, layer: dict) -> None:
    """Radial darkening towards the edges and corners (`strength` 0-1: the
    corner opacity; `radius` 0-1: where the falloff starts, as a fraction of
    the half-diagonal) - the lens/print look a horror cover wants."""
    import numpy as np

    strength = max(0.0, min(1.0, float(layer.get("strength", 0.55))))
    start = max(0.0, min(0.95, float(layer.get("radius", 0.45))))
    w, h = canvas.size
    small_w, small_h = max(2, w // 8), max(2, h // 8)
    yy, xx = np.mgrid[0:small_h, 0:small_w].astype(np.float32)
    dx = (xx - (small_w - 1) / 2) / ((small_w - 1) / 2)
    dy = (yy - (small_h - 1) / 2) / ((small_h - 1) / 2)
    dist = np.sqrt(dx * dx + dy * dy) / math.sqrt(2)
    t = np.clip((dist - start) / (1.0 - start), 0.0, 1.0)
    alpha = (t * t * (3 - 2 * t) * strength * 255).astype("uint8")
    mask = Image.fromarray(alpha, "L").resize((w, h), Image.BILINEAR)
    shade = Image.new("RGBA", (w, h), _hex(layer.get("colour", "#000000ff")))
    shade.putalpha(mask)
    canvas.alpha_composite(shade)


def _layer_frame(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    width = int(layer.get("width", 6))
    inset = int(layer.get("inset", 0))
    radius = int(layer.get("radius", 0))
    colour = _hex(_resolve(layer.get("colour", "#ffffffff"), fields))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    box = [inset, inset, canvas.width - 1 - inset, canvas.height - 1 - inset]
    if radius:
        draw.rounded_rectangle(box, radius=radius, outline=colour, width=width)
    else:
        draw.rectangle(box, outline=colour, width=width)
    canvas.alpha_composite(overlay)


def _layer_badge(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    x, y, w, h = _box(layer, fields)
    text = str(_resolve(layer.get("text", ""), fields) or "")
    if not text.strip():
        return
    fill = _hex(_resolve(layer.get("fill", "#000000cc"), fields))
    colour = _hex(_resolve(layer.get("colour", "#ffffffff"), fields))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=fill)
    size = int(layer.get("size", h * 0.5))
    font = get_font(layer.get("font", "inter"), size)
    while size > 10 and draw.textbbox((0, 0), text, font=font)[2] > w - h * 0.6:
        size -= 2
        font = get_font(layer.get("font", "inter"), size)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x + (w - tw) / 2, y + (h - th) / 2 - bbox[1]), text, font=font, fill=colour)
    canvas.alpha_composite(overlay)


def _layer_qr_placeholder(canvas: Image.Image, layer: dict) -> None:
    x, y, w, h = layer.get("x", 0), layer.get("y", 0), layer.get("w", 80), layer.get("h", 80)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([x, y, x + w, y + h], outline=(255, 255, 255, 200), width=2)
    draw.line([x, y, x + w, y + h], fill=(255, 255, 255, 120))
    draw.line([x + w, y, x, y + h], fill=(255, 255, 255, 120))
    canvas.alpha_composite(overlay)


def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.width, img.height], radius=radius, fill=255)
    out = img.copy()
    out.putalpha(mask if img.mode != "RGBA" else Image.composite(img.split()[3], Image.new("L", img.size, 0), mask))
    return out


BLEED_MM = 3.0
PRINT_DPI = 300


def bleed_px(dpi: int = PRINT_DPI, mm: float = BLEED_MM) -> int:
    return int(round(mm / 25.4 * dpi))


def add_bleed(img: Image.Image, px: int) -> Image.Image:
    """Extend every edge by `px` by mirroring the outermost pixels (what a
    print shop expects: artwork continues past the trim line). Transparent
    rounded corners are flattened first, since cards are die-cut."""
    import numpy as np

    rgba = img.convert("RGBA")
    flat = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    flat.alpha_composite(rgba)
    arr = np.asarray(flat)
    padded = np.pad(arr, ((px, px), (px, px), (0, 0)), mode="symmetric")
    return Image.fromarray(padded, "RGBA")


def image_bytes(img: Image.Image, print_mode: bool = False) -> bytes:
    """PNG bytes. `print_mode` adds a 3 mm bleed on every side and 300 dpi
    metadata (the design's pixel size is the trim size)."""
    buf = io.BytesIO()
    if print_mode:
        img = add_bleed(img, bleed_px())
    dpi = (PRINT_DPI, PRINT_DPI) if print_mode else (96, 96)
    img.convert("RGBA").save(buf, format="PNG", dpi=dpi, optimize=False)
    return buf.getvalue()


def pixel_hash(img: Image.Image) -> str:
    return hashlib.sha256(img.convert("RGBA").tobytes()).hexdigest()
