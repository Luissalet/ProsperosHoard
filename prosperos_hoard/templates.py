"""Named design layouts. Each function returns a layout spec for
`design.render_layout()`. Fields referenced by `{"field": "..."}` are
supplied by the caller (`studio_design` / `POST /api/design/render`).

Field contracts (documented here, also in docs/API.md):
  photocard_front:  image, member_name, role, accent
  photocard_back:   group_logo (asset), member_name, serial, message, accent
  album_cover:      cover_image, title, subtitle, accent  (variant chooses layout)
  teaser_poster:    image, title, tagline, accent
  lyric_card:       image, quote, attribution, accent
  tracklist_back:   cover_image, group_name, tracks (str, one per line), accent
  thumbnail:        image, title, accent
"""

from __future__ import annotations

from typing import Any


def photocard_front() -> dict[str, Any]:
    w, h = 1100, 1700
    return {
        "width": w, "height": h, "radius": 48,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#141018ff"},
            {"type": "image", "x": 0, "y": 0, "w": w, "h": int(h * 0.82), "asset": {"field": "image"}, "fit": "cover"},
            {"type": "holo", "x": 0, "y": 0, "w": w, "h": int(h * 0.82), "seed": {"field": "holo_seed", "default": 7}, "opacity": 0.22},
            {"type": "rect", "x": 0, "y": int(h * 0.78), "w": w, "h": int(h * 0.22), "fill": "#0b0710e6"},
            {"type": "text", "x": 60, "y": int(h * 0.82), "w": w - 120, "h": 100, "text": {"field": "member_name"},
             "font": "playfair-display", "size": 72, "colour": {"field": "accent", "default": "#ff4d8dff"}, "align": "left"},
            {"type": "badge", "x": 60, "y": int(h * 0.90), "w": 260, "h": 60, "text": {"field": "role", "default": ""},
             "font": "space-grotesk", "size": 26, "fill": "#ffffff22", "colour": "#ffffffff"},
            {"type": "grain", "amount": 6},
            {"type": "frame", "width": 4, "colour": {"field": "accent", "default": "#ff4d8dff"}},
        ],
    }


def photocard_back() -> dict[str, Any]:
    w, h = 1100, 1700
    return {
        "width": w, "height": h, "radius": 48,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#0b0710ff"},
            {"type": "image", "x": w // 2 - 90, "y": 90, "w": 180, "h": 180, "asset": {"field": "group_logo"}, "fit": "contain"},
            {"type": "text", "x": 60, "y": 340, "w": w - 120, "h": 140, "text": {"field": "member_name"},
             "font": "playfair-display", "size": 64, "colour": {"field": "accent", "default": "#ff4d8dff"}, "align": "center"},
            {"type": "text", "x": 60, "y": h - 260, "w": w - 120, "h": 200, "text": {"field": "message", "default": ""},
             "font": "caveat", "size": 46, "colour": "#e8e2f0ff", "align": "center", "valign": "middle"},
            {"type": "badge", "x": w // 2 - 140, "y": h - 90, "w": 280, "h": 56, "text": {"field": "serial"},
             "font": "space-grotesk", "size": 26, "fill": "#ffffff1a", "colour": "#ffffffff"},
            {"type": "grain", "amount": 6},
            {"type": "frame", "width": 4, "colour": {"field": "accent", "default": "#ff4d8dff"}},
        ],
    }


def album_cover(variant: str = "center_title") -> dict[str, Any]:
    w = h = 3000
    base_layers = [
        {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "cover_image"}, "fit": "cover"},
    ]
    if variant == "bottom_band":
        extra = [
            {"type": "rect", "x": 0, "y": int(h * 0.78), "w": w, "h": int(h * 0.22), "fill": "#000000b3"},
            {"type": "text", "x": 120, "y": int(h * 0.82), "w": w - 240, "h": 260, "text": {"field": "title"},
             "font": "bebas-neue", "size": 220, "colour": "#ffffffff", "align": "left"},
            {"type": "text", "x": 120, "y": int(h * 0.92), "w": w - 240, "h": 130, "text": {"field": "subtitle", "default": ""},
             "font": "space-grotesk", "size": 64, "colour": {"field": "accent", "default": "#f5c26bff"}, "align": "left"},
        ]
    elif variant == "corner_minimal":
        extra = [
            {"type": "text", "x": 100, "y": h - 420, "w": w - 200, "h": 220, "text": {"field": "title"},
             "font": "space-grotesk", "size": 130, "colour": "#ffffffff", "align": "left"},
            {"type": "text", "x": 100, "y": h - 200, "w": w - 200, "h": 120, "text": {"field": "subtitle", "default": ""},
             "font": "inter", "size": 54, "colour": {"field": "accent", "default": "#f5c26bff"}, "align": "left"},
        ]
    else:  # center_title
        extra = [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#00000066"},
            {"type": "text", "x": 200, "y": int(h * 0.42), "w": w - 400, "h": 400, "text": {"field": "title"},
             "font": "playfair-display", "size": 200, "colour": "#ffffffff", "align": "center", "valign": "middle"},
            {"type": "text", "x": 200, "y": int(h * 0.60), "w": w - 400, "h": 140, "text": {"field": "subtitle", "default": ""},
             "font": "space-grotesk", "size": 70, "colour": {"field": "accent", "default": "#f5c26bff"}, "align": "center"},
        ]
    return {"width": w, "height": h, "layers": base_layers + extra + [{"type": "grain", "amount": 5}]}


def teaser_poster() -> dict[str, Any]:
    w, h = 1600, 2400
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover"},
            {"type": "rect", "x": 0, "y": int(h * 0.65), "w": w, "h": int(h * 0.35), "fill": "#00000099"},
            {"type": "text", "x": 90, "y": int(h * 0.70), "w": w - 180, "h": 320, "text": {"field": "title"},
             "font": "bebas-neue", "size": 180, "colour": "#ffffffff", "align": "left"},
            {"type": "text", "x": 90, "y": int(h * 0.86), "w": w - 180, "h": 160, "text": {"field": "tagline", "default": ""},
             "font": "caveat", "size": 70, "colour": {"field": "accent", "default": "#ff4d8dff"}, "align": "left"},
            {"type": "grain", "amount": 6},
        ],
    }


def lyric_card() -> dict[str, Any]:
    w = h = 1080
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover"},
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#00000073"},
            {"type": "text", "x": 100, "y": 340, "w": w - 200, "h": 320, "text": {"field": "quote"},
             "font": "playfair-display", "size": 64, "colour": "#ffffffff", "align": "center", "valign": "middle"},
            {"type": "text", "x": 100, "y": 700, "w": w - 200, "h": 80, "text": {"field": "attribution", "default": ""},
             "font": "space-grotesk", "size": 32, "colour": {"field": "accent", "default": "#f5c26bff"}, "align": "center"},
        ],
    }


def tracklist_back() -> dict[str, Any]:
    w = h = 3000
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#0b0710ff"},
            {"type": "image", "x": w // 2 - 260, "y": 140, "w": 520, "h": 520, "asset": {"field": "cover_image"}, "fit": "cover", "radius": 24},
            {"type": "text", "x": 200, "y": 760, "w": w - 400, "h": 200, "text": {"field": "group_name"},
             "font": "bebas-neue", "size": 130, "colour": {"field": "accent", "default": "#f5c26bff"}, "align": "center"},
            {"type": "text", "x": 300, "y": 1000, "w": w - 600, "h": 1600, "text": {"field": "tracks"},
             "font": "space-grotesk", "size": 60, "colour": "#e8e2f0ff", "align": "left", "auto_fit": True},
            {"type": "grain", "amount": 5},
        ],
    }


def thumbnail() -> dict[str, Any]:
    w, h = 1280, 720
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover"},
            {"type": "rect", "x": 0, "y": h - 160, "w": w, "h": 160, "fill": "#000000b3"},
            {"type": "text", "x": 48, "y": h - 140, "w": w - 96, "h": 120, "text": {"field": "title"},
             "font": "space-grotesk", "size": 60, "colour": {"field": "accent", "default": "#ff4d8dff"}, "align": "left", "valign": "middle"},
        ],
    }


TEMPLATE_DIMENSIONS = {
    "photocard_front": (1100, 1700),
    "photocard_back": (1100, 1700),
    "album_cover": (3000, 3000),
    "teaser_poster": (1600, 2400),
    "lyric_card": (1080, 1080),
    "tracklist_back": (3000, 3000),
    "thumbnail": (1280, 720),
}


def get_layout(template: str, variant: str | None = None) -> dict[str, Any]:
    if template == "photocard_front":
        return photocard_front()
    if template == "photocard_back":
        return photocard_back()
    if template == "album_cover":
        return album_cover(variant or "center_title")
    if template == "teaser_poster":
        return teaser_poster()
    if template == "lyric_card":
        return lyric_card()
    if template == "tracklist_back":
        return tracklist_back()
    if template == "thumbnail":
        return thumbnail()
    raise ValueError(f"unknown design template '{template}'")
