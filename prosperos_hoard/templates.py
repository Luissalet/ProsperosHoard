"""Named design layouts. Each function returns a layout spec for
`design.render_layout()`. Fields referenced by `{"field": "..."}` are
supplied by the caller (`studio_design` / the Designer screen);
`TEMPLATE_FIELDS` is the contract (shown in the UI, the MCP docstring and
the error for a wrong field).
"""

from __future__ import annotations

from typing import Any

# field -> (type, required, description). Types: text, image (asset id), colour.
TEMPLATE_FIELDS: dict[str, dict[str, tuple[str, bool, str]]] = {
    "photocard_front": {
        "image": ("image", True, "member photo"),
        "member_name": ("text", True, "name printed on the card"),
        "role": ("text", False, "position, e.g. 'Main Vocal'"),
        "group_name": ("text", False, "small group name above the member name"),
        "accent": ("colour", False, "frame and name colour"),
    },
    "photocard_back": {
        "member_name": ("text", True, "name"),
        "group_name": ("text", False, "used for the monogram when there is no logo"),
        "group_logo": ("image", False, "logo image"),
        "message": ("text", False, "handwritten-style message"),
        "serial": ("text", False, "e.g. 'No. 007/250'"),
        "accent": ("colour", False, "gradient and frame colour"),
    },
    "album_cover": {
        "cover_image": ("image", True, "cover art"),
        "title": ("text", True, "album or group title"),
        "subtitle": ("text", False, "e.g. '1st Mini Album'"),
        "artist": ("text", False, "artist line (night variant: small, top left)"),
        "accent": ("colour", False, "subtitle colour"),
    },
    "teaser_poster": {
        "image": ("image", True, "key visual"),
        "title": ("text", True, "headline"),
        "tagline": ("text", False, "second line"),
        "date": ("text", False, "release date line"),
        "accent": ("colour", False, "tagline colour"),
    },
    "lyric_card": {
        "image": ("image", False, "background image"),
        "quote": ("text", True, "the lyric"),
        "attribution": ("text", False, "song / artist line"),
        "accent": ("colour", False, "attribution colour"),
    },
    "tracklist_back": {
        "cover_image": ("image", False, "small cover (night variant: blurred full-bleed backdrop)"),
        "group_name": ("text", True, "group name"),
        "title": ("text", False, "release title under the name"),
        "tracks": ("text", True, "one track per line"),
        "credits": ("text", False, "small print at the bottom"),
        "accent": ("colour", False, "title colour"),
    },
    "thumbnail": {
        "image": ("image", True, "background"),
        "title": ("text", True, "video title"),
        "accent": ("colour", False, "title colour"),
    },
}

# The first variant of each list is the default. "night" is the horror /
# thriller look: bone-white condensed titles with a faded-red print
# misregistration, sodium accents, typewriter small print, vignette and
# heavy grain.
VARIANTS = {
    "album_cover": ["center_title", "bottom_band", "corner_minimal", "night"],
    "teaser_poster": ["classic", "night"],
    "lyric_card": ["classic", "night"],
    "tracklist_back": ["classic", "night"],
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

_ACCENT = {"field": "accent", "default": "#ff4d8dff"}
_GOLD = {"field": "accent", "default": "#f5c26bff"}
# night variants
_SODIUM = {"field": "accent", "default": "#f28c28ff"}
_BONE = "#ede6daff"
_FOG = "#8a9099ff"
_SMILE_RED = "#a33a2ecc"


def photocard_front() -> dict[str, Any]:
    w, h = 1100, 1700
    photo_h = int(h * 0.80)
    return {
        "width": w, "height": h, "radius": 48,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#120d18ff"},
            {"type": "image", "x": 0, "y": 0, "w": w, "h": photo_h, "asset": {"field": "image"}, "fit": "cover",
             "focal": "subject", "focal_fallback": [0.5, 0.35]},
            {"type": "holo", "x": 0, "y": 0, "w": w, "h": photo_h, "seed": {"field": "holo_seed", "default": 7}, "opacity": 0.16, "blend": "screen"},
            {"type": "rect", "x": 0, "y": photo_h - 260, "w": w, "h": 262,
             "gradient": {"colours": ["#120d1800", "#120d18ff"], "direction": "vertical"}},
            {"type": "rect", "x": 0, "y": photo_h, "w": w, "h": h - photo_h, "fill": "#120d18ff"},
            {"type": "text", "x": 72, "y": photo_h - 70, "w": w - 144, "h": 50, "text": {"field": "group_name", "default": ""},
             "font": "space-grotesk", "size": 30, "colour": "#ffffffb3", "letter_spacing": 0.25, "uppercase": True},
            {"type": "text", "x": 72, "y": photo_h - 10, "w": w - 144, "h": 150, "text": {"field": "member_name"},
             "font": "playfair-display", "size": 104, "colour": _ACCENT, "valign": "middle"},
            {"type": "badge", "x": 72, "y": h - 170, "w": 360, "h": 68, "text": {"field": "role", "default": ""},
             "font": "space-grotesk", "size": 28, "fill": "#ffffff1f", "colour": "#ffffffff"},
            {"type": "grain", "amount": 4},
            {"type": "frame", "width": 6, "inset": 22, "radius": 30, "colour": _ACCENT},
        ],
    }


def photocard_back() -> dict[str, Any]:
    w, h = 1100, 1700
    return {
        "width": w, "height": h, "radius": 48,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h,
             "gradient": {"colours": [_ACCENT, "#1a1022ff", "#0b0710ff"], "direction": "vertical"}},
            {"type": "holo", "x": 0, "y": int(h * 0.60), "w": w, "h": 14, "seed": 3, "opacity": 0.9, "blend": "screen"},
            {"type": "rect", "x": w // 2 - 170, "y": 210, "w": 340, "h": 340, "radius": 170, "fill": "#ffffff14"},
            {"type": "text", "x": w // 2 - 170, "y": 210, "w": 340, "h": 340, "text": {"field": "monogram", "default": ""},
             "font": "bebas-neue", "size": 170, "colour": "#ffffffee", "align": "center", "valign": "middle"},
            {"type": "image", "x": w // 2 - 150, "y": 230, "w": 300, "h": 300, "asset": {"field": "group_logo"},
             "fit": "contain", "placeholder": False},
            {"type": "text", "x": 80, "y": 640, "w": w - 160, "h": 60, "text": {"field": "group_name", "default": ""},
             "font": "space-grotesk", "size": 34, "colour": "#ffffffb3", "align": "center", "letter_spacing": 0.3, "uppercase": True},
            {"type": "text", "x": 80, "y": 710, "w": w - 160, "h": 170, "text": {"field": "member_name"},
             "font": "playfair-display", "size": 110, "colour": "#ffffffff", "align": "center", "valign": "middle"},
            {"type": "text", "x": 120, "y": 1080, "w": w - 240, "h": 300, "text": {"field": "message", "default": ""},
             "font": "caveat", "size": 64, "colour": "#f3ecffff", "align": "center", "valign": "middle"},
            {"type": "badge", "x": w // 2 - 170, "y": h - 190, "w": 340, "h": 66, "text": {"field": "serial", "default": ""},
             "font": "space-grotesk", "size": 28, "fill": "#ffffff1f", "colour": "#ffffffff"},
            {"type": "grain", "amount": 4},
            {"type": "frame", "width": 6, "inset": 22, "radius": 30, "colour": "#ffffff66"},
        ],
    }


def album_cover(variant: str = "center_title") -> dict[str, Any]:
    w = h = 3000
    base_layers: list[dict[str, Any]] = [
        {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "cover_image"}, "fit": "cover"},
    ]
    if variant == "bottom_band":
        extra = [
            {"type": "rect", "x": 0, "y": int(h * 0.70), "w": w, "h": int(h * 0.30),
             "gradient": {"colours": ["#00000000", "#000000d9"], "direction": "vertical"}},
            {"type": "text", "x": 150, "y": int(h * 0.76), "w": w - 300, "h": 380, "text": {"field": "title"},
             "font": "bebas-neue", "size": 360, "colour": "#ffffffff", "letter_spacing": 0.04, "valign": "bottom"},
            {"type": "text", "x": 150, "y": int(h * 0.895), "w": w - 300, "h": 150, "text": {"field": "subtitle", "default": ""},
             "font": "space-grotesk", "size": 90, "colour": _GOLD, "letter_spacing": 0.2, "uppercase": True},
        ]
    elif variant == "corner_minimal":
        extra = [
            {"type": "text", "x": 140, "y": 140, "w": w - 280, "h": 200, "text": {"field": "title"},
             "font": "space-grotesk", "size": 150, "colour": "#ffffffff", "letter_spacing": 0.12, "uppercase": True},
            {"type": "text", "x": 140, "y": h - 260, "w": w - 280, "h": 120, "text": {"field": "subtitle", "default": ""},
             "font": "inter", "size": 70, "colour": _GOLD, "align": "right", "letter_spacing": 0.1},
        ]
    elif variant == "night":
        extra = [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": 900, "gradient": {"colours": ["#000000b3", "#00000000"]}},
            {"type": "rect", "x": 0, "y": 1400, "w": w, "h": h - 1400, "gradient": {"colours": ["#00000000", "#000000f0"]}},
            {"type": "vignette", "strength": 0.6, "radius": 0.4},
            {"type": "text", "x": 170, "y": 170, "w": 1500, "h": 120, "text": {"field": "artist", "default": ""},
             "font": "space-grotesk", "size": 84, "colour": _SODIUM, "letter_spacing": 0.55, "uppercase": True},
            {"type": "text", "x": 1600, "y": 180, "w": w - 1770, "h": 110, "text": {"field": "subtitle", "default": ""},
             "font": "special-elite", "size": 66, "colour": _FOG, "align": "right", "uppercase": True, "letter_spacing": 0.06},
            {"type": "text", "x": 150, "y": 1450, "w": w - 300, "h": 1290, "text": {"field": "title"},
             "font": "bebas-neue", "size": 700, "colour": _BONE, "valign": "bottom", "line_height": 0.86, "uppercase": True,
             "shadow": {"dx": 16, "dy": 0, "colour": _SMILE_RED}},
            {"type": "rect", "x": 170, "y": h - 200, "w": 380, "h": 14, "fill": _SODIUM},
        ]
        return {"width": w, "height": h, "layers": base_layers + extra + [{"type": "grain", "amount": 7}]}
    else:  # center_title
        extra = [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#0000004d"},
            {"type": "text", "x": 240, "y": int(h * 0.36), "w": w - 480, "h": 560, "text": {"field": "title"},
             "font": "playfair-display", "size": 300, "colour": "#ffffffff", "align": "center", "valign": "middle",
             "shadow": {"dx": 0, "dy": 10, "colour": "#00000088"}},
            {"type": "text", "x": 240, "y": int(h * 0.56), "w": w - 480, "h": 160, "text": {"field": "subtitle", "default": ""},
             "font": "space-grotesk", "size": 90, "colour": _GOLD, "align": "center", "letter_spacing": 0.25, "uppercase": True},
        ]
    return {"width": w, "height": h, "layers": base_layers + extra + [{"type": "grain", "amount": 4}]}


def teaser_poster(variant: str = "classic") -> dict[str, Any]:
    w, h = 1600, 2400
    if variant == "night":
        return {
            "width": w, "height": h,
            "layers": [
                {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover", "focal": [0.5, 0.4]},
                {"type": "rect", "x": 0, "y": 0, "w": w, "h": 700, "gradient": {"colours": ["#000000cc", "#00000000"]}},
                {"type": "rect", "x": 0, "y": 1150, "w": w, "h": h - 1150, "gradient": {"colours": ["#00000000", "#000000f2"]}},
                {"type": "vignette", "strength": 0.65, "radius": 0.35},
                {"type": "text", "x": 110, "y": 150, "w": w - 220, "h": 90, "text": {"field": "tagline", "default": ""},
                 "font": "special-elite", "size": 58, "colour": _SODIUM, "align": "center", "letter_spacing": 0.16, "uppercase": True},
                {"type": "text", "x": 80, "y": 1500, "w": w - 160, "h": 640, "text": {"field": "title"},
                 "font": "bebas-neue", "size": 560, "colour": _BONE, "align": "center", "valign": "bottom", "line_height": 0.88,
                 "uppercase": True, "shadow": {"dx": 9, "dy": 0, "colour": _SMILE_RED}},
                {"type": "text", "x": 110, "y": 2180, "w": w - 220, "h": 70, "text": {"field": "date", "default": ""},
                 "font": "space-grotesk", "size": 40, "colour": _FOG, "align": "center", "letter_spacing": 0.45, "uppercase": True},
                {"type": "frame", "width": 3, "inset": 44, "colour": "#ede6da40"},
                {"type": "grain", "amount": 8},
            ],
        }
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover"},
            {"type": "rect", "x": 0, "y": int(h * 0.55), "w": w, "h": int(h * 0.45),
             "gradient": {"colours": ["#00000000", "#000000e6"], "direction": "vertical"}},
            {"type": "text", "x": 110, "y": int(h * 0.70), "w": w - 220, "h": 360, "text": {"field": "title"},
             "font": "bebas-neue", "size": 300, "colour": "#ffffffff", "letter_spacing": 0.03, "valign": "bottom"},
            {"type": "text", "x": 110, "y": int(h * 0.855), "w": w - 220, "h": 150, "text": {"field": "tagline", "default": ""},
             "font": "caveat", "size": 110, "colour": _ACCENT},
            {"type": "text", "x": 110, "y": int(h * 0.93), "w": w - 220, "h": 80, "text": {"field": "date", "default": ""},
             "font": "space-grotesk", "size": 46, "colour": "#ffffffcc", "letter_spacing": 0.3, "uppercase": True},
            {"type": "grain", "amount": 5},
        ],
    }


def lyric_card(variant: str = "classic") -> dict[str, Any]:
    w = h = 1080
    if variant == "night":
        return {
            "width": w, "height": h,
            "layers": [
                {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#0b0c10ff"},
                {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover", "placeholder": False},
                {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#000000a6"},
                {"type": "vignette", "strength": 0.6, "radius": 0.35},
                {"type": "text", "x": 90, "y": 130, "w": w - 180, "h": 680, "text": {"field": "quote"},
                 "font": "bebas-neue", "size": 140, "colour": _BONE, "valign": "middle", "line_height": 0.95, "uppercase": True,
                 "shadow": {"dx": 5, "dy": 0, "colour": _SMILE_RED}},
                {"type": "rect", "x": 92, "y": 862, "w": 120, "h": 6, "fill": _SODIUM},
                {"type": "text", "x": 90, "y": 892, "w": w - 180, "h": 60, "text": {"field": "attribution", "default": ""},
                 "font": "special-elite", "size": 34, "colour": _SODIUM, "letter_spacing": 0.1, "uppercase": True},
                {"type": "grain", "amount": 7},
            ],
        }
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "gradient": {"colours": ["#2a1236ff", "#0b0710ff"]}},
            {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover", "placeholder": False},
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#00000080"},
            {"type": "text", "x": 110, "y": 300, "w": w - 220, "h": 400, "text": {"field": "quote"},
             "font": "playfair-display", "size": 76, "colour": "#ffffffff", "align": "center", "valign": "middle",
             "line_height": 1.25},
            {"type": "text", "x": 110, "y": 740, "w": w - 220, "h": 70, "text": {"field": "attribution", "default": ""},
             "font": "space-grotesk", "size": 32, "colour": _GOLD, "align": "center", "letter_spacing": 0.2, "uppercase": True},
        ],
    }


def tracklist_back(variant: str = "classic") -> dict[str, Any]:
    w = h = 3000
    if variant == "night":
        return {
            "width": w, "height": h,
            "layers": [
                {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#0b0c10ff"},
                {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "cover_image"}, "fit": "cover",
                 "blur": 22, "opacity": 0.6, "placeholder": False},
                {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "fill": "#000000b0"},
                {"type": "vignette", "strength": 0.6, "radius": 0.35},
                {"type": "text", "x": 220, "y": 250, "w": w - 440, "h": 340, "text": {"field": "group_name"},
                 "font": "bebas-neue", "size": 320, "colour": _BONE, "letter_spacing": 0.04, "uppercase": True,
                 "shadow": {"dx": 10, "dy": 0, "colour": _SMILE_RED}},
                {"type": "text", "x": 224, "y": 610, "w": w - 440, "h": 130, "text": {"field": "title", "default": ""},
                 "font": "special-elite", "size": 92, "colour": _SODIUM, "letter_spacing": 0.08, "uppercase": True},
                {"type": "rect", "x": 224, "y": 800, "w": w - 448, "h": 4, "fill": "#ede6da40"},
                {"type": "text", "x": 224, "y": 920, "w": w - 448, "h": 1450, "text": {"field": "tracks"},
                 "font": "space-grotesk", "size": 112, "colour": _BONE, "line_height": 1.7, "letter_spacing": 0.04,
                 "columns": {"indent": 0.09}, "muted_colour": _FOG},
                {"type": "text", "x": 224, "y": 2480, "w": w - 448, "h": 320, "text": {"field": "credits", "default": ""},
                 "font": "special-elite", "size": 54, "colour": _FOG, "line_height": 1.45},
                {"type": "grain", "amount": 6},
            ],
        }
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": w, "h": h, "gradient": {"colours": ["#1a1022ff", "#0b0710ff"]}},
            {"type": "image", "x": w // 2 - 300, "y": 180, "w": 600, "h": 600, "asset": {"field": "cover_image"},
             "fit": "cover", "radius": 28, "placeholder": False},
            {"type": "text", "x": 200, "y": 860, "w": w - 400, "h": 260, "text": {"field": "group_name"},
             "font": "bebas-neue", "size": 220, "colour": _GOLD, "align": "center", "letter_spacing": 0.08},
            {"type": "text", "x": 420, "y": 1220, "w": w - 840, "h": 1500, "text": {"field": "tracks"},
             "font": "space-grotesk", "size": 96, "colour": "#e8e2f0ff", "align": "left", "line_height": 1.5},
            {"type": "grain", "amount": 4},
        ],
    }


def thumbnail() -> dict[str, Any]:
    w, h = 1280, 720
    return {
        "width": w, "height": h,
        "layers": [
            {"type": "image", "x": 0, "y": 0, "w": w, "h": h, "asset": {"field": "image"}, "fit": "cover"},
            {"type": "rect", "x": 0, "y": h - 260, "w": w, "h": 260, "gradient": {"colours": ["#00000000", "#000000d9"]}},
            {"type": "text", "x": 56, "y": h - 200, "w": w - 112, "h": 160, "text": {"field": "title"},
             "font": "bebas-neue", "size": 120, "colour": _ACCENT, "valign": "bottom", "letter_spacing": 0.03},
        ],
    }


_BUILDERS = {
    "photocard_front": photocard_front,
    "photocard_back": photocard_back,
    "teaser_poster": teaser_poster,
    "lyric_card": lyric_card,
    "tracklist_back": tracklist_back,
    "thumbnail": thumbnail,
}


def describe_templates() -> list[dict[str, Any]]:
    out = []
    for name, fields in TEMPLATE_FIELDS.items():
        w, h = TEMPLATE_DIMENSIONS[name]
        out.append({
            "template": name, "width": w, "height": h, "variants": VARIANTS.get(name, []),
            "fields": [{"name": f, "type": t, "required": r, "description": d} for f, (t, r, d) in fields.items()],
        })
    return out


def fields_hint(template: str) -> str:
    fields = TEMPLATE_FIELDS.get(template, {})
    return ", ".join(f"{f} ({t}{', required' if r else ''})" for f, (t, r, _) in fields.items())


def get_layout(template: str, variant: str | None = None) -> dict[str, Any]:
    if template == "album_cover":
        if variant and variant not in VARIANTS["album_cover"]:
            raise ValueError(f"unknown album_cover variant '{variant}'; use one of {', '.join(VARIANTS['album_cover'])}")
        return album_cover(variant or "center_title")
    builder = _BUILDERS.get(template)
    if builder is None:
        raise ValueError(f"unknown design template '{template}'; use one of {', '.join(TEMPLATE_FIELDS)}")
    if template in VARIANTS:
        if variant and variant not in VARIANTS[template]:
            raise ValueError(f"unknown {template} variant '{variant}'; use one of {', '.join(VARIANTS[template])}")
        return builder(variant or VARIANTS[template][0])
    if variant:
        raise ValueError(f"template '{template}' has no variants")
    return builder()
