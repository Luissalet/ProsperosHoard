"""Deterministic layout renderer (Pillow only, no browser).

A layout is a JSON spec: `{"width", "height", "layers": [...]}`. Layer
types: `rect`, `image`, `text`, `holo` (procedural iridescent foil),
`grain`, `frame`, `badge`. Every layer may reference a named `field` whose
value is filled in at render time from the caller's `fields` dict, so one
layout can be reused for many photocards/covers.

`qr` layers are recognised but not rendered (documented boundary - no QR
library is in the dependency set); a layout containing one renders a
plain placeholder box instead of failing.
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


def _hex(colour: str) -> tuple[int, int, int, int]:
    colour = colour.lstrip("#")
    if len(colour) == 6:
        colour += "ff"
    r, g, b, a = (int(colour[i : i + 2], 16) for i in (0, 2, 4, 6))
    return (r, g, b, a)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font_name: str, box: tuple[int, int], max_size: int, min_size: int = 10) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink-to-fit: largest font size (>= min_size) whose wrapped text fits `box`."""
    width, height = box
    for size in range(max_size, min_size - 1, -2):
        font = get_font(font_name, size)
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            bbox = draw.textbbox((0, 0), trial, font=font)
            if bbox[2] - bbox[0] <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        line_height = font.getbbox("Ay")[3] - font.getbbox("Ay")[1]
        total_height = line_height * len(lines) * 1.25
        widest = max((draw.textbbox((0, 0), ln, font=font)[2] for ln in lines), default=0)
        if total_height <= height and widest <= width:
            return font, lines
    return get_font(font_name, min_size), [text]


def _draw_holo(size: tuple[int, int], seed: int, angle: float = 35.0) -> Image.Image:
    width, height = size
    rng = random.Random(seed)
    base = Image.new("RGB", (width, height))
    px = base.load()
    hue_shift = rng.uniform(0, 360)
    for y in range(height):
        for x in range(width):
            t = ((x * math.cos(math.radians(angle)) + y * math.sin(math.radians(angle))) / (width + height)) % 1.0
            hue = (hue_shift + t * 360) % 360
            px[x, y] = _hsv_to_rgb(hue, 0.6, 0.95)
    noise = Image.effect_noise((width, height), 24).convert("L")
    holo = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.12)
    return holo


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(h / 360, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


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
    fill = _hex(_resolve(layer.get("fill", "#000000ff"), fields))
    radius = int(_resolve(layer.get("radius", 0), fields))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if radius:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill)
    else:
        draw.rectangle([x, y, x + w, y + h], fill=fill)
    canvas.alpha_composite(overlay)


def _layer_image(canvas: Image.Image, layer: dict, fields: dict[str, Any], asset_resolver) -> None:
    x, y, w, h = _box(layer, fields)
    asset_id = _resolve(layer.get("asset"), fields)
    path = asset_resolver(asset_id) if asset_id else None
    if path is None or not Path(path).is_file():
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([x, y, x + w, y + h], fill=(40, 40, 46, 255))
        canvas.alpha_composite(overlay)
        return
    src = Image.open(path).convert("RGBA")
    fit = layer.get("fit", "cover")
    if fit == "cover":
        src = ImageOps.fit(src, (w, h), method=Image.LANCZOS)
    else:
        src = ImageOps.contain(src, (w, h), method=Image.LANCZOS)
        pad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        pad.alpha_composite(src, ((w - src.width) // 2, (h - src.height) // 2))
        src = pad
    radius = int(_resolve(layer.get("radius", 0), fields))
    if radius:
        src = _round_corners(src, radius)
    opacity = float(_resolve(layer.get("opacity", 1.0), fields))
    if opacity < 1.0:
        alpha = src.split()[3].point(lambda p: int(p * opacity))
        src.putalpha(alpha)
    canvas.alpha_composite(src, (x, y))


def _layer_text(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    x, y, w, h = _box(layer, fields)
    text = str(_resolve(layer.get("text", ""), fields))
    if not text:
        return
    font_name = layer.get("font", "inter")
    colour = _hex(_resolve(layer.get("colour", "#ffffffff"), fields))
    align = layer.get("align", "left")
    max_size = int(layer.get("size", 48))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if layer.get("auto_fit", True):
        font, lines = _fit_text(draw, text, font_name, (w, h), max_size)
    else:
        font = get_font(font_name, max_size)
        lines = text.split("\n")
    line_height = (font.getbbox("Ay")[3] - font.getbbox("Ay")[1]) * 1.25
    total = line_height * len(lines)
    cursor_y = y + max(0, (h - total) / 2) if layer.get("valign") == "middle" else y
    stroke = layer.get("stroke")
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        if align == "center":
            line_x = x + (w - line_w) / 2
        elif align == "right":
            line_x = x + w - line_w
        else:
            line_x = x
        kwargs: dict[str, Any] = {}
        if stroke:
            kwargs["stroke_width"] = int(stroke.get("width", 2))
            kwargs["stroke_fill"] = _hex(stroke.get("colour", "#000000ff"))
        draw.text((line_x, cursor_y), line, font=font, fill=colour, **kwargs)
        cursor_y += line_height
    canvas.alpha_composite(overlay)


def _layer_holo(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    x, y, w, h = _box(layer, fields)
    seed = int(_resolve(layer.get("seed", 0), fields))
    holo = _draw_holo((w, h), seed, angle=float(layer.get("angle", 35.0))).convert("RGBA")
    opacity = float(layer.get("opacity", 0.35))
    alpha = holo.split()[3].point(lambda p: int(255 * opacity))
    holo.putalpha(alpha)
    canvas.alpha_composite(holo, (x, y))


def _layer_grain(canvas: Image.Image, layer: dict) -> None:
    amount = float(layer.get("amount", 8))
    noise = Image.effect_noise(canvas.size, int(amount * 8)).convert("L")
    noise_rgba = Image.merge("RGBA", (noise, noise, noise, noise.point(lambda p: int(p * 0.08))))
    canvas.alpha_composite(noise_rgba)


def _layer_frame(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    width = int(layer.get("width", 6))
    colour = _hex(_resolve(layer.get("colour", "#ffffffff"), fields))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle([0, 0, canvas.width - 1, canvas.height - 1], outline=colour, width=width)
    canvas.alpha_composite(overlay)


def _layer_badge(canvas: Image.Image, layer: dict, fields: dict[str, Any]) -> None:
    x, y, w, h = _box(layer, fields)
    text = str(_resolve(layer.get("text", ""), fields))
    fill = _hex(_resolve(layer.get("fill", "#000000cc"), fields))
    colour = _hex(_resolve(layer.get("colour", "#ffffffff"), fields))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=fill)
    font = get_font(layer.get("font", "inter"), int(layer.get("size", h * 0.5)))
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


def image_bytes(img: Image.Image, print_mode: bool = False) -> bytes:
    buf = io.BytesIO()
    dpi = (300, 300) if print_mode else (96, 96)
    img.convert("RGBA").save(buf, format="PNG", dpi=dpi)
    return buf.getvalue()


def pixel_hash(img: Image.Image) -> str:
    return hashlib.sha256(img.convert("RGBA").tobytes()).hexdigest()
