"""Brand scan of a web page: logo, three main colours, fonts, headline, value proposition and key phrases, kept as a project's brand kit.

A real browser (Playwright, the Chromium it ships or the Edge/Chrome of the PC) opens the page; `collect_page` is the only part that
touches it and returns plain data, so everything after it (`build_kit`, the colour choice, the font choice, the phrases) is pure and
tested without a browser. Fonts are only downloaded when their licence is known to allow it (the open families of Google Fonts);
for any other family the closest open font that ships with Prospero is used instead and the kit says so.
"""
from __future__ import annotations

import colorsys
import hashlib
import io
import json
import math
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urljoin, urlparse

from PIL import Image, ImageDraw, ImageFont

from . import engine

FONT_DIR = Path(__file__).resolve().parent / "fonts"
BROWSER_ARGS = ["--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb"]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
MAX_FONT_BYTES = 3 * 1024 * 1024
GENERIC_FAMILIES = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-sans-serif", "ui-serif", "ui-monospace",
                    "ui-rounded", "-apple-system", "blinkmacsystemfont", "emoji", "math", "fangsong", "inherit", "initial"}
SYSTEM_FAMILIES = {"arial", "helvetica", "helvetica neue", "segoe ui", "roboto", "tahoma", "verdana", "times new roman", "times", "georgia",
                   "courier new", "courier", "trebuchet ms", "calibri", "cambria", "consolas", "menlo", "monaco", "sf pro text", "sf pro display",
                   "san francisco", "noto sans", "liberation sans", "dejavu sans", "ubuntu", "cantarell", "oxygen", "fira sans", "droid sans"}
# open families (SIL OFL / Apache) a site commonly serves from its own host or from Google Fonts
OPEN_FAMILIES = {"inter", "roboto", "open sans", "lato", "montserrat", "poppins", "raleway", "source sans pro", "source sans 3", "nunito",
                 "work sans", "dm sans", "space grotesk", "playfair display", "merriweather", "lora", "oswald", "bebas neue", "ibm plex sans",
                 "ibm plex serif", "ibm plex mono", "fira sans", "fira code", "jetbrains mono", "manrope", "rubik", "karla", "mulish",
                 "barlow", "quicksand", "cabin", "pt sans", "pt serif", "source serif 4", "source code pro", "roboto mono", "roboto slab",
                 "noto sans", "noto serif", "libre baskerville", "crimson text", "dm serif display", "outfit", "sora", "plus jakarta sans"}
GOOGLE_FONT_HOSTS = {"fonts.gstatic.com"}
_FACE = re.compile(r"@font-face\s*\{([^}]*)\}", re.S | re.I)


class PromoBrandError(engine.EngineError):
    pass


# ------------------------------------------------------------------ the browser

def _browser_executable() -> Optional[str]:
    try:
        from .editing.html_motion import browser_executable
        return browser_executable()
    except Exception:  # noqa: BLE001 - the editing module is optional here
        return None


@contextmanager
def open_browser() -> Iterator[Any]:
    """A headless Chromium: the one Playwright ships, else the Edge/Chrome of the PC (``LUMIERE_BROWSER_EXECUTABLE`` wins)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise PromoBrandError("no_browser", "Playwright is not installed; install it and run 'python -m playwright install chromium'") from None
    with sync_playwright() as runtime:
        browser = None
        try:
            configured = os.environ.get("LUMIERE_BROWSER_EXECUTABLE", "").strip()
            attempts: list[Optional[str]] = [configured] if configured else [None, _browser_executable()]
            last: Optional[Exception] = None
            for exe in attempts:
                if exe is None and configured:
                    continue
                try:
                    kwargs: dict[str, Any] = {"headless": True, "args": BROWSER_ARGS}
                    if exe:
                        kwargs["executable_path"] = exe
                    browser = runtime.chromium.launch(**kwargs)
                    break
                except Exception as exc:  # noqa: BLE001
                    last = exc
            if browser is None:
                raise PromoBrandError("no_browser", f"no browser could be started ({str(last)[:200]}); run 'python -m playwright install chromium'")
            yield browser
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass


# ------------------------------------------------------------------ colours

def parse_color(value: Any) -> Optional[tuple[int, int, int, float]]:
    s = str(value or "").strip().lower()
    if not s or s in ("transparent", "none", "inherit", "currentcolor"):
        return (0, 0, 0, 0.0) if s == "transparent" else None
    m = re.match(r"#([0-9a-f]{3,8})$", s)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
        if len(h) == 8:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16) / 255)
        return None
    m = re.match(r"rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$", s)
    if m:
        a = m.group(4)
        alpha = 1.0 if a is None else (float(a[:-1]) / 100 if a.endswith("%") else float(a))
        return (round(float(m.group(1))), round(float(m.group(2))), round(float(m.group(3))), alpha)
    m = re.match(r"color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*/\s*([\d.]+))?\)$", s)
    if m:
        return (round(float(m.group(1)) * 255), round(float(m.group(2)) * 255), round(float(m.group(3)) * 255), float(m.group(4) or 1))
    return None


def to_hex(rgb: tuple[int, ...]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb[:3])


def from_hex(value: str) -> tuple[int, int, int]:
    parsed = parse_color(value)
    if not parsed:
        raise PromoBrandError("bad_colour", f"not a colour: {value!r}")
    return parsed[:3]


def hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def from_hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    return (round(r * 255), round(g * 255), round(b * 255))


def luminance(rgb: tuple[int, int, int]) -> float:
    def lin(c: int) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2])


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def is_chromatic(rgb: tuple[int, int, int]) -> bool:
    h, s, v = hsv(rgb)
    return s >= 0.22 and 0.12 <= v <= 0.98


def pixel_bins(png: bytes, side: int = 220) -> list[tuple[tuple[int, int, int], float]]:
    """The colours of a screenshot, grouped in 16 levels per channel: [(centre rgb, share of the pixels)] from the most to the least."""
    with Image.open(io.BytesIO(png)) as image:
        small = image.convert("RGB")
        small.thumbnail((side, side * 4), Image.Resampling.BOX)
    counts: dict[tuple[int, int, int], int] = {}
    data = small.tobytes()
    total = len(data) // 3
    for i in range(0, len(data), 3):
        key = (data[i] >> 4, data[i + 1] >> 4, data[i + 2] >> 4)
        counts[key] = counts.get(key, 0) + 1
    out = [(((k[0] << 4) + 8, (k[1] << 4) + 8, (k[2] << 4) + 8), n / total) for k, n in counts.items()]
    out.sort(key=lambda item: -item[1])
    return out


class _Clusters:
    """Colours weighed by where they were found; nearby ones count as one."""

    def __init__(self, radius: float = 34.0):
        self.items: list[dict[str, Any]] = []
        self.radius = radius

    def add(self, rgb: tuple[int, int, int], weight: float, source: str) -> None:
        for item in self.items:
            if distance(item["rgb"], rgb) <= self.radius:
                total = item["w"] + weight
                item["rgb"] = tuple(round((a * item["w"] + b * weight) / total) for a, b in zip(item["rgb"], rgb))
                item["w"] = total
                if source not in item["sources"]:
                    item["sources"].append(source)
                return
        self.items.append({"rgb": rgb, "w": weight, "sources": [source]})

    def ranked(self) -> list[dict[str, Any]]:
        return sorted(self.items, key=lambda i: -i["w"])


VAR_WEIGHTS = (("primary", 4.0), ("brand", 4.0), ("accent", 3.5), ("secondary", 3.0), ("highlight", 3.0), ("cta", 3.0), ("main", 2.5),
               ("theme", 2.5), ("link", 2.0))


def choose_palette(raw: dict[str, Any]) -> dict[str, Any]:
    """Primary, secondary and accent colours, the background and the text colour of the page, from the pixels of its screenshots, the
    colours of its buttons, links, headings and header, its CSS variables and its theme-color."""
    chroma, neutral = _Clusters(), _Clusters(radius=24.0)
    for png in raw.get("screenshots") or []:
        for rgb, share in pixel_bins(png)[:60]:
            if is_chromatic(rgb):
                chroma.add(rgb, share * 40 * (1 + 2 * hsv(rgb)[1]), "pixels")
            else:
                neutral.add(rgb, share * 10, "pixels")
    for row in raw.get("element_colors") or []:
        role = row.get("role")
        for key, weight in (("bg", {"button": 6.0, "header": 3.0}.get(role, 0.0)), ("color", {"link": 2.5, "heading": 2.0}.get(role, 0.0))):
            parsed = parse_color(row.get(key))
            if not parsed or parsed[3] < 0.6 or weight <= 0:
                continue
            rgb = parsed[:3]
            (chroma if is_chromatic(rgb) else neutral).add(rgb, weight, role)
    for name, value in (raw.get("css_vars") or {}).items():
        parsed = parse_color(value)
        if not parsed or parsed[3] < 0.6 or not is_chromatic(parsed[:3]):
            continue
        weight = next((w for key, w in VAR_WEIGHTS if key in name.lower()), 0.8)
        chroma.add(parsed[:3], weight, "css " + name)
    theme = parse_color(raw.get("theme_color"))
    if theme and is_chromatic(theme[:3]):
        chroma.add(theme[:3], 5.0, "theme-color")
    for item in chroma.items:                                # a near-black navy is an ink, not a brand colour to build shots on
        if hsv(item["rgb"])[2] < 0.3:
            item["w"] *= 0.3
    ranked = chroma.ranked()
    chosen: list[dict[str, Any]] = []
    for item in ranked:
        if all(distance(item["rgb"], c["rgb"]) >= 75 for c in chosen):
            chosen.append(item)
        if len(chosen) == 3:
            break
    roles = ["primary", "secondary", "accent"]
    colours: list[dict[str, Any]] = [{"hex": to_hex(c["rgb"]), "role": roles[i], "weight": round(c["w"], 2), "derived": False,
                                      "source": ", ".join(c["sources"][:3])} for i, c in enumerate(chosen)]
    if not chosen:                                           # a black-and-white site: its ink and its tint are the brand
        base = next((n["rgb"] for n in neutral.ranked() if luminance(n["rgb"]) < 0.2), (20, 20, 20))
        chosen = [{"rgb": base, "w": 0, "sources": ["ink"]}]
        colours = [{"hex": to_hex(base), "role": "primary", "weight": 0, "derived": True, "source": "the ink of the page"}]
    h, s, v = hsv(chosen[0]["rgb"])
    while len(colours) < 3:
        n = len(colours)
        rgb = from_hsv(h, max(s, 0.55) * 0.9, min(1.0, max(v, 0.5) + 0.25)) if n == 2 and s > 0.1 else (
            mix(chosen[0]["rgb"], (255, 255, 255), 0.35) if v < 0.5 else mix(chosen[0]["rgb"], (0, 0, 0), 0.45))
        if n == 2 and s > 0.1:
            rgb = from_hsv(h + 0.09, 0.85, 0.95)
        colours.append({"hex": to_hex(rgb), "role": roles[n], "weight": 0, "derived": True, "source": "derived from the primary colour"})
    background = next((n["rgb"] for n in neutral.ranked()), (255, 255, 255))
    body = next((parse_color(r.get("color")) for r in raw.get("element_colors") or [] if r.get("role") == "body" and parse_color(r.get("color"))), None)
    page_bg = next((parse_color(r.get("bg")) for r in raw.get("element_colors") or [] if r.get("role") in ("body", "html")
                    and parse_color(r.get("bg")) and parse_color(r.get("bg"))[3] > 0.9), None)  # type: ignore[index]
    if page_bg:
        background = page_bg[:3]
    text = body[:3] if body and contrast(body[:3], background) >= 4.5 else ((20, 20, 20) if luminance(background) > 0.4 else (245, 245, 245))
    prim = from_hex(colours[0]["hex"])
    dark_pool = [c for c in (text, background, prim) if luminance(c) < 0.35]
    dark = min(dark_pool, key=luminance) if dark_pool else (24, 24, 27)
    if luminance(dark) > 0.05:
        dark = mix(dark, (0, 0, 0), 0.65)
    light_pool = [c for c in (text, background) if luminance(c) > 0.6]
    light = max(light_pool, key=luminance) if light_pool else (250, 250, 250)
    if luminance(light) < 0.75:
        light = mix(light, (255, 255, 255), 0.8)
    return {"colors": colours, "palette": {"background": to_hex(background), "text": to_hex(text), "primary": colours[0]["hex"],
                                          "secondary": colours[1]["hex"], "accent": colours[2]["hex"], "dark": to_hex(dark), "light": to_hex(light)}}


# ------------------------------------------------------------------ fonts

def split_stack(stack: str) -> list[str]:
    families, current, quote = [], "", ""
    for ch in str(stack or ""):
        if quote:
            if ch == quote:
                quote = ""
            else:
                current += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            families.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        families.append(current.strip())
    return [f for f in families if f]


def named_family(stack: str) -> Optional[str]:
    """The first family of a font-family stack that is a real typeface (not a generic keyword)."""
    for family in split_stack(stack):
        if family.lower() not in GENERIC_FAMILIES:
            return family
    return None


def stack_kind(stack: str) -> str:
    lowered = [f.lower() for f in split_stack(stack)]
    for generic, kind in (("monospace", "mono"), ("ui-monospace", "mono"), ("cursive", "script"), ("serif", "serif")):
        if generic in lowered:
            return kind
    return "sans"


def substitute_for(family: Optional[str], stack: str = "") -> dict[str, Any]:
    """The open font that ships with Prospero and looks most like a family whose own file cannot be used."""
    name = (family or "").lower()
    kind = stack_kind(stack)
    table = [
        (r"playfair|georgia|times|garamond|baskerville|didot|bodoni|merriweather|lora|cambria|serif|palatino|charter|caslon", "PlayfairDisplay"),
        (r"bebas|impact|oswald|anton|condensed|league gothic|fjalla|barlow condensed", "BebasNeue"),
        (r"grotesk|grotesque|neue|geometric|poppins|montserrat|gotham|futura|avenir|circular|proxima|sora|outfit|manrope|jakarta|dm sans", "SpaceGrotesk"),
        (r"caveat|script|hand|brush|marker", "Caveat"),
    ]
    pick = next((folder for pattern, folder in table if re.search(pattern, name)), None)
    if pick is None and kind == "serif":
        pick = "PlayfairDisplay"
    if pick is None and kind == "script":
        pick = "Caveat"
    pick = pick or "Inter"
    files = {"Inter": ("Inter/Inter.ttf", True), "PlayfairDisplay": ("PlayfairDisplay/PlayfairDisplay.ttf", True),
             "SpaceGrotesk": ("SpaceGrotesk/SpaceGrotesk.ttf", True), "BebasNeue": ("BebasNeue/BebasNeue-Regular.ttf", False),
             "Caveat": ("Caveat/Caveat.ttf", True)}
    relative, variable = files[pick]
    return {"family": pick if pick != "PlayfairDisplay" else "Playfair Display", "file": str(FONT_DIR / relative), "variable": variable,
            "licence": "SIL Open Font License (ships with Prospero)"}


def parse_font_faces(css: str, base_url: str) -> list[dict[str, Any]]:
    """The @font-face rules of a stylesheet: family, weight range, style, unicode-range and the urls with their formats."""
    faces = []
    for block in _FACE.findall(css or ""):
        fam = re.search(r"font-family\s*:\s*([^;]+)", block, re.I)
        if not fam:
            continue
        weight = re.search(r"font-weight\s*:\s*([^;]+)", block, re.I)
        lo, hi = 400, 400
        if weight:
            nums = [int(n) for n in re.findall(r"\d+", weight.group(1))]
            if nums:
                lo, hi = nums[0], nums[-1]
            elif "bold" in weight.group(1).lower():
                lo = hi = 700
        style = re.search(r"font-style\s*:\s*([^;]+)", block, re.I)
        urange = re.search(r"unicode-range\s*:\s*([^;]+)", block, re.I)
        sources = []
        for _q, url, fmt in re.findall(r"url\(\s*(['\"]?)(.*?)\1\s*\)(?:\s*format\(\s*['\"]?([\w-]+)['\"]?\s*\))?", block, re.I):
            if url.startswith("data:"):
                continue
            sources.append({"url": urljoin(base_url, url.strip()), "format": (fmt or Path(urlparse(url).path).suffix.lstrip(".")).lower()})
        if sources:
            faces.append({"family": fam.group(1).strip().strip("'\""), "lo": lo, "hi": hi, "italic": bool(style and "italic" in style.group(1).lower()),
                          "unicode_range": urange.group(1).strip() if urange else "", "sources": sources})
    return faces


FORMAT_RANK = {"woff2": 0, "woff": 1, "truetype": 2, "ttf": 2, "opentype": 3, "otf": 3}


def best_face(faces: list[dict[str, Any]], family: str, weight: int) -> Optional[dict[str, Any]]:
    """The face of a family closest to a weight, upright, covering Latin, in the lightest format."""
    own = [f for f in faces if f["family"].lower() == family.lower()]
    if not own:
        return None

    def cost(face: dict[str, Any]) -> tuple[float, int, int]:
        gap = 0 if face["lo"] <= weight <= face["hi"] else min(abs(face["lo"] - weight), abs(face["hi"] - weight))
        latin = 0 if (not face["unicode_range"] or "U+0000-00FF" in face["unicode_range"].upper()) else 1
        return (latin * 1000 + (500 if face["italic"] else 0) + gap, min(FORMAT_RANK.get(s["format"], 9) for s in face["sources"]), 0)

    face = min(own, key=cost)
    source = min(face["sources"], key=lambda s: FORMAT_RANK.get(s["format"], 9))
    return {**face, "source": source}


_WEIGHT_WORDS = re.compile(r"\b(bold|regular|light|italic|semibold|medium|black|heavy|thin|extralight|extrabold|webfont|web|roman|book|variable)\b")


def norm_family(name: str) -> str:
    """A family name as comparable text: 'SourceSansProBold' -> 'source sans pro' (per-weight family names are common)."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name or "")).replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", _WEIGHT_WORDS.sub(" ", spaced.lower())).strip()


def licence_allows(family: str, url: str) -> tuple[bool, str]:
    host = urlparse(url).hostname or ""
    if host in GOOGLE_FONT_HOSTS:
        return True, "served by Google Fonts (open licence)"
    if norm_family(family) in OPEN_FAMILIES:
        return True, f"{family} is an open font (SIL OFL / Apache)"
    return False, f"the licence of {family} is not known to allow reuse"


def pick_fonts(raw: dict[str, Any]) -> dict[str, Any]:
    """Display and body fonts of the kit: the site's own when its licence allows the download (the file was fetched by collect_page),
    else the closest open font that ships with Prospero."""
    computed = {r.get("role"): r for r in raw.get("fonts_computed") or []}
    downloaded = raw.get("font_files") or {}
    out: dict[str, Any] = {"site_families": []}
    for role, source_roles in (("display", ("h1", "h2", "button")), ("body", ("p", "body", "a"))):
        row = next((computed[r] for r in source_roles if r in computed and named_family(computed[r].get("family", ""))), None)
        stack = (row or {}).get("family", "") or (computed.get("body") or {}).get("family", "")
        family = named_family(stack)
        if family and family not in out["site_families"]:
            out["site_families"].append(family)
        weight = int(float((row or {}).get("weight") or (700 if role == "display" else 400)))
        got = downloaded.get(family) if family else None
        if family and isinstance(got, dict) and got.get("bytes"):
            out[role] = {"family": family, "file": "", "variable": True, "weight": weight, "source": "site", "licence": got["licence"],
                         "site_family": family}               # scan() writes the file and fills in its path
        else:
            sub = substitute_for(family, stack)
            note = (f"{family or 'the system font'} is not reused: " + (downloaded.get("_why", {}).get(family or "", "") or
                    ("it is a system font" if (family or "").lower() in SYSTEM_FAMILIES or not family else "its file could not be fetched")))
            out[role] = {**sub, "weight": 700 if role == "display" else 400, "source": "substitute", "site_family": family, "note": note}
        if out[role].get("family") == "Bebas Neue" or out[role].get("file", "").endswith("Regular.ttf"):
            out[role]["weight"] = 400
    return out


# ------------------------------------------------------------------ words

_NOISE = re.compile(r"cookie|privacy|terms|copyright|©|subscribe|newsletter|sign in|log in|login|all rights|javascript|skip to|menu", re.I)


_TAIL_LINK = re.compile(r"[\s.|>»-]*(learn more|read more|more info|find out more|saber más|leer más|más información|ver más)[\s.»>]*$", re.I)


def clean_text(text: str, limit: int) -> str:
    text = _TAIL_LINK.sub("", re.sub(r"\s+", " ", str(text or "")).strip())
    if len(text) <= limit:
        return text
    sentence = re.match(r"(.{20,%d}?[.!?…])(\s|$)" % limit, text)
    if sentence:
        return sentence.group(1)
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:- ")
    return cut + "…"


def pick_phrases(texts: list[dict[str, Any]], headline: str, value_prop: str, count: int = 5) -> list[str]:
    """3-5 key phrases of the page: headings first, then short paragraphs, in the order of the page, never the headline or the proposition."""
    seen = {re.sub(r"\W+", "", headline.lower()), re.sub(r"\W+", "", value_prop.lower())}
    scored = []
    for position, row in enumerate(texts):
        text = clean_text(row.get("text", ""), 90)
        key = re.sub(r"\W+", "", text.lower())
        words = len(text.split())
        if (not text or key in seen or _NOISE.search(text) or words < 3 or len(text) < 14 or text.startswith((">>>", "$", "#", "//"))
                or text.endswith(("…", "..."))):                  # a sentence cut in the middle reads as a mistake on screen
            continue
        seen.add(key)
        base = {"h1": 1.0, "h2": 3.0, "h3": 2.2, "blockquote": 2.0, "p": 1.2}.get(row.get("tag"), 1.0)
        fit = 1.0 if 20 <= len(text) <= 80 else 0.6
        scored.append((base * fit - position * 0.03, position, text))
    best = sorted(scored, reverse=True)[:count]
    return [text for _s, _p, text in sorted(best, key=lambda item: item[1])]


def strip_site_suffix(title: str, site: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    for sep in (" | ", " - ", " – ", " — ", " · ", " :: "):
        if sep in title:
            head, tail = title.rsplit(sep, 1) if site and site.lower() in title.rsplit(sep, 1)[1].lower() else (title, "")
            if tail:
                return head.strip()
    return title


def build_kit(raw: dict[str, Any], url: str) -> dict[str, Any]:
    host = (urlparse(raw.get("final_url") or url).hostname or "site").removeprefix("www.")
    site = (raw.get("site_name") or "").strip() or host
    h1 = clean_text(raw.get("h1") or "", 110)
    title = strip_site_suffix(raw.get("og_title") or raw.get("title") or "", site)
    description = clean_text(raw.get("description") or "", 170)
    # a heading of one or two words (a slide's title, 'Welcome') says little: the page's own description is the better opening line
    if len(h1.split()) >= 3:
        headline, value_prop = h1, description
    elif description and len(description) <= 95:
        headline, value_prop = description, ""
    else:
        headline, value_prop = clean_text(h1 or title or site, 110), description
    texts = list(raw.get("texts") or [])
    if not value_prop:
        value_prop = next((clean_text(t["text"], 170) for t in texts if t.get("tag") == "p" and 40 <= len(t.get("text", "")) <= 220
                           and not _NOISE.search(t["text"]) and t["text"][:60] != headline[:60]), "")
    phrases = pick_phrases(texts, headline, value_prop)
    colours = choose_palette(raw)
    lang = (raw.get("lang") or "").lower()[:2]
    kit = {"url": url, "final_url": raw.get("final_url") or url, "host": host, "site_name": site, "title": raw.get("title") or "",
           "headline": headline, "value_prop": value_prop, "phrases": phrases, "language": lang or "", **colours,
           "fonts": pick_fonts(raw), "scanned_at": time.time()}
    return kit


# ------------------------------------------------------------------ the page

SCAN_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.05; };
  const txt = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const meta = n => { const m = document.querySelector(`meta[name="${n}"],meta[property="${n}"]`); return m ? (m.content || '').trim() : ''; };
  const out = { lang: document.documentElement.lang || '', title: document.title || '', description: meta('description') || meta('og:description'),
    site_name: meta('og:site_name') || meta('application-name'), og_title: meta('og:title'), theme_color: meta('theme-color') };
  const skip = 'nav,footer,[role=navigation],[class*=cookie],[id*=cookie],[class*=consent],[id*=consent],[class*=banner],script,style,noscript';
  const h1 = [...document.querySelectorAll('h1')].filter(vis).map(txt).find(t => t.length >= 3);
  out.h1 = h1 || '';
  const seen = new Set(), texts = [];
  for (const el of document.querySelectorAll('h1,h2,h3,p,blockquote')) {
    if (texts.length >= 60) break;
    if (el.closest(skip) || !vis(el)) continue;
    const t = txt(el); if (t.length < 12 || t.length > 400 || seen.has(t)) continue;
    seen.add(t); texts.push({ tag: el.tagName.toLowerCase(), text: t });
  }
  out.texts = texts;
  const colours = []; const add = (role, el) => { if (!el) return; const cs = getComputedStyle(el); colours.push({ role, color: cs.color, bg: cs.backgroundColor }); };
  add('body', document.body); add('html', document.documentElement);
  [...document.querySelectorAll('header,[role=banner],nav')].filter(vis).slice(0, 3).forEach(e => add('header', e));
  [...document.querySelectorAll('button,a[class*=btn],a[class*=button],[role=button],input[type=submit]')].filter(vis).slice(0, 14).forEach(e => add('button', e));
  [...document.querySelectorAll('a')].filter(vis).slice(0, 30).forEach(e => add('link', e));
  [...document.querySelectorAll('h1,h2')].filter(vis).slice(0, 6).forEach(e => add('heading', e));
  out.element_colors = colours;
  const vars = {}; const root = getComputedStyle(document.documentElement);
  for (const sheet of document.styleSheets) { let rules; try { rules = sheet.cssRules; } catch (e) { continue; }
    for (const rule of rules || []) { if (!rule.style || !/(:root|^html|^body)/.test(rule.selectorText || '')) continue;
      for (const name of rule.style) if (name.startsWith('--') && Object.keys(vars).length < 160) vars[name] = root.getPropertyValue(name).trim(); } }
  out.css_vars = vars;
  const fonts = []; const fam = (role, el) => { if (!el) return; const cs = getComputedStyle(el); fonts.push({ role, family: cs.fontFamily, weight: cs.fontWeight, size: cs.fontSize }); };
  fam('body', document.body); fam('h1', document.querySelector('h1')); fam('h2', document.querySelector('h2'));
  fam('p', document.querySelector('main p, article p, p')); fam('button', document.querySelector('button,a[class*=btn],[role=button]'));
  fam('a', document.querySelector('a'));
  out.fonts_computed = fonts;
  out.stylesheets = [...document.querySelectorAll('link[rel~=stylesheet]')].map(l => l.href).filter(Boolean).slice(0, 12);
  out.inline_css = [...document.querySelectorAll('style')].map(s => s.textContent || '').join('\n').slice(0, 400000);
  const cands = [];
  const origin = location.origin;
  for (const el of [...document.querySelectorAll('img,svg')].slice(0, 300)) {
    if (!vis(el)) continue; const r = el.getBoundingClientRect(); if (r.top > 520 || r.width < 20 || r.height < 12 || r.width > 700 || r.height > 260) continue;
    const a = el.closest('a'); const cn = typeof el.className === 'string' ? el.className : (el.className && el.className.baseVal) || '';
    const hay = ((el.id || '') + ' ' + cn + ' ' + (el.getAttribute('alt') || '') + ' ' + (el.getAttribute('src') || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
      (a ? (a.className + ' ' + a.getAttribute('href') + ' ' + (a.getAttribute('aria-label') || '')) : '')).toLowerCase();
    let s = 0;
    if (/logo|brand|wordmark|logotype/.test(hay)) s += 6;
    if (el.closest('header,[role=banner],nav,[class*=header],[class*=navbar],#header')) s += 3;
    if (a) { const h = a.getAttribute('href') || ''; if (h === '/' || h === origin || h === origin + '/' || h === './' || h === '#') s += 3; }
    if (r.top < 200) s += 2;
    if (r.width / Math.max(1, r.height) > 8) s -= 3;
    cands.push({ s, el, r });
  }
  cands.sort((a, b) => b.s - a.s);
  out.logo = null;
  if (cands.length && cands[0].s >= 5) { const c = cands[0]; c.el.setAttribute('data-promo-logo', '1');
    out.logo = { tag: c.el.tagName.toLowerCase(), src: c.el.currentSrc || c.el.getAttribute('src') || '', score: c.s, w: c.r.width, h: c.r.height,
      natural_w: c.el.naturalWidth || 0 }; }
  out.icons = [...document.querySelectorAll('link[rel~=icon],link[rel=apple-touch-icon],link[rel=apple-touch-icon-precomposed],link[rel=mask-icon]')]
    .map(l => ({ href: l.href, sizes: l.getAttribute('sizes') || '', rel: l.rel })).filter(i => i.href);
  return out;
}"""

HIDE_OVERLAYS_JS = """() => { for (const el of document.querySelectorAll('[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],[id*=gdpr i],[class*=gdpr i]'))
  { const p = getComputedStyle(el).position; if (p === 'fixed' || p === 'sticky' || p === 'absolute') el.remove(); } }"""


def _fetch(context: Any, url: str, limit: int = 6 * 1024 * 1024) -> Optional[tuple[bytes, str]]:
    try:
        response = context.request.get(url, timeout=15000)
        if not response.ok:
            return None
        body = response.body()
        return (body, response.headers.get("content-type", "")) if len(body) <= limit else None
    except Exception:  # noqa: BLE001
        return None


def _svg_to_png(browser: Any, markup: bytes, width: int = 640) -> Optional[bytes]:
    page = browser.new_page(viewport={"width": width, "height": width}, device_scale_factor=2)
    try:
        page.set_content("<body style='margin:0;background:transparent'><img id=i style='width:%dpx;display:block' src='data:image/svg+xml;base64,%s'>"
                         % (width, __import__("base64").b64encode(markup).decode()))
        page.wait_for_function("document.getElementById('i').complete")
        return page.locator("#i").screenshot(omit_background=True)
    except Exception:  # noqa: BLE001
        return None
    finally:
        page.close()


def collect_page(url: str, *, timeout_s: float = 35.0) -> dict[str, Any]:
    """Opens the page in a browser and returns what the kit is made from; no decisions are made here."""
    with open_browser() as browser:
        context = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, locale="es-ES", user_agent=USER_AGENT)
        try:
            page = context.new_page()
            try:
                page.goto(url, wait_until="load", timeout=timeout_s * 1000)
            except Exception as exc:  # noqa: BLE001 - a slow tracker must not lose the page
                if "Timeout" not in type(exc).__name__ and "Timeout" not in str(exc):
                    raise PromoBrandError("page_unreachable", f"could not open {url}: {str(exc)[:200]}") from None
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:  # noqa: BLE001
                pass
            page.evaluate(HIDE_OVERLAYS_JS)
            raw: dict[str, Any] = page.evaluate(SCAN_JS)
            raw["final_url"] = page.url
            shots = [page.screenshot(type="png")]
            page.evaluate("() => window.scrollTo(0, innerHeight)")
            page.wait_for_timeout(350)
            shots.append(page.screenshot(type="png"))
            raw["screenshots"] = shots
            css_text = raw.get("inline_css", "")
            faces = parse_font_faces(css_text, raw["final_url"])
            for sheet in raw.get("stylesheets") or []:
                got = _fetch(context, sheet, 600 * 1024)
                if got:
                    faces += parse_font_faces(got[0].decode("utf-8", "replace"), sheet)
            raw["font_files"] = _download_fonts(context, raw, faces)
            raw["logo_png"], raw["logo_from"] = _logo_bytes(browser, context, page, raw)
            return raw
        finally:
            context.close()


def _download_fonts(context: Any, raw: dict[str, Any], faces: list[dict[str, Any]]) -> dict[str, Any]:
    """Fetches the files of the display and body families when their licence allows reusing them."""
    out: dict[str, Any] = {"_why": {}}
    wanted: dict[str, int] = {}
    for row in raw.get("fonts_computed") or []:
        family = named_family(row.get("family", ""))
        if family:
            wanted.setdefault(family, int(float(row.get("weight") or 400)) if row.get("role") in ("h1", "h2", "button") else 400)
    for family, weight in list(wanted.items())[:3]:
        face = best_face(faces, family, weight)
        if not face:
            out["_why"][family] = "it declares no downloadable @font-face (a system or installed font)"
            continue
        ok, why = licence_allows(family, face["source"]["url"])
        if not ok:
            out["_why"][family] = why
            continue
        got = _fetch(context, face["source"]["url"], MAX_FONT_BYTES)
        if not got:
            out["_why"][family] = "the font file could not be fetched"
            continue
        out[family] = {"bytes": got[0], "ext": face["source"]["format"] if face["source"]["format"] in ("woff2", "woff", "ttf", "otf") else "woff2",
                       "licence": why, "url": face["source"]["url"], "weight": weight}
    return out


def _logo_bytes(browser: Any, context: Any, page: Any, raw: dict[str, Any]) -> tuple[Optional[bytes], str]:
    logo = raw.get("logo")
    if logo:
        src = logo.get("src") or ""
        if logo["tag"] == "img" and src and not src.startswith("data:"):
            got = _fetch(context, src)
            if got:
                body, ctype = got
                if "svg" in ctype or src.lower().split("?")[0].endswith(".svg"):
                    png = _svg_to_png(browser, body)
                    if png:
                        return png, "header image (svg)"
                elif ctype.startswith("image/") and logo.get("natural_w", 0) >= logo["w"]:
                    return body, "header image"
        try:
            return page.locator("[data-promo-logo='1']").first.screenshot(omit_background=True), f"header {logo['tag']}"
        except Exception:  # noqa: BLE001
            pass
    best, best_size = None, 0
    for icon in raw.get("icons") or []:
        numbers = [int(n) for n in re.findall(r"\d+", icon.get("sizes", ""))]
        size = max(numbers) if numbers else (180 if "apple" in icon.get("rel", "") else 32)
        if size > best_size:
            best, best_size = icon, size
    if best:
        got = _fetch(context, best["href"])
        if got:
            if "svg" in got[1] or best["href"].lower().split("?")[0].endswith(".svg"):
                png = _svg_to_png(browser, got[0], 480)
                if png:
                    return png, "site icon (svg)"
            else:
                return got[0], "site icon"
    return None, ""


# ------------------------------------------------------------------ logo

def prepare_logo(data: bytes) -> Optional[dict[str, Any]]:
    """The logo as a trimmed RGBA PNG of at most 900 px, with its average luminance (to choose a background it reads on)."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = source.convert("RGBA")
    except Exception:  # noqa: BLE001
        return None
    if min(image.size) < 8:
        return None
    alpha = image.getchannel("A")
    if alpha.getextrema()[0] == 255:                      # opaque: a flat background is knocked out
        corners = [image.getpixel(p)[:3] for p in ((0, 0), (image.width - 1, 0), (0, image.height - 1), (image.width - 1, image.height - 1))]
        if all(distance(corners[0], c) < 18 for c in corners):
            base = corners[0]
            px = image.load()
            for y in range(image.height):
                for x in range(image.width):
                    d = distance(px[x, y][:3], base)
                    if d < 40:
                        px[x, y] = (px[x, y][0], px[x, y][1], px[x, y][2], int(max(0, min(255, (d - 14) * 255 / 26))))
    box = image.getchannel("A").point(lambda v: 255 if v > 12 else 0).getbbox()
    if not box:
        return None
    image = image.crop(box)
    scale = 900 / max(image.size)
    if scale < 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    pixels = [p for p in image.getdata() if p[3] > 128]
    if not pixels:
        return None
    lum = sum(luminance(p[:3]) for p in pixels) / len(pixels)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return {"png": out.getvalue(), "width": image.width, "height": image.height, "luminance": round(lum, 3)}


# ------------------------------------------------------------------ storage

def kits_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "promo" / "kits"


def kit_id_for(url: str) -> str:
    host = (urlparse(url).hostname or "site").removeprefix("www.")
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:40] + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:6]


def kit_folder(data_dir: Path, kit_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{3,60}", kit_id or ""):
        raise PromoBrandError("bad_kit", "that is not a brand kit id")
    return kits_dir(data_dir) / kit_id


def load_kit(data_dir: Path, kit_id: str) -> dict[str, Any]:
    path = kit_folder(data_dir, kit_id) / "kit.json"
    if not path.is_file():
        raise engine.EngineError("kit_not_found", f"there is no brand kit {kit_id!r}; scan the site first (brand_scan)")
    return json.loads(path.read_text(encoding="utf-8"))


def list_kits(data_dir: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(kits_dir(data_dir).glob("*/kit.json"), key=lambda p: -p.stat().st_mtime):
        try:
            kit = json.loads(path.read_text(encoding="utf-8"))
            out.append({k: kit.get(k) for k in ("id", "url", "host", "site_name", "headline", "colors", "scanned_at")})
        except (OSError, ValueError):
            continue
    return out


def brand_board(kit: dict[str, Any], logo_png: Optional[bytes]) -> bytes:
    """One picture of the kit: the logo, the three colours with their codes, the fonts in use and the headline."""
    width, height = 1600, 900
    palette = kit["palette"]
    bg, ink = from_hex(palette["light"]), from_hex(palette["dark"])
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)

    def font(key: str, size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(kit["fonts"][key]["file"], size)
        except Exception:  # noqa: BLE001
            return ImageFont.truetype(str(FONT_DIR / "Inter" / "Inter.ttf"), size)

    draw.text((80, 70), kit["site_name"], font=font("display", 64), fill=ink)
    draw.text((80, 160), clean_text(kit["headline"], 80), font=font("body", 30), fill=ink)
    if logo_png:
        with Image.open(io.BytesIO(logo_png)) as logo:
            logo = logo.convert("RGBA")
            logo.thumbnail((520, 260), Image.Resampling.LANCZOS)
            plate = ink if luminance(bg) > 0.4 and kit.get("logo", {}).get("luminance", 0.3) > 0.7 else bg
            draw.rectangle((width - 640, 60, width - 80, 340), fill=plate)
            image.paste(logo, (width - 640 + (560 - logo.width) // 2, 60 + (280 - logo.height) // 2), logo)
    for i, c in enumerate(kit["colors"]):
        x = 80 + i * 500
        rgb = from_hex(c["hex"])
        draw.rectangle((x, 400, x + 460, 640), fill=rgb)
        draw.text((x + 20, 660), c["role"].capitalize(), font=font("body", 28), fill=ink)
        draw.text((x + 20, 702), c["hex"].upper() + ("  (derived)" if c.get("derived") else ""), font=font("body", 26), fill=ink)
    f = kit["fonts"]
    draw.text((80, 800), f"{f['display']['family']} / {f['body']['family']}", font=font("display", 36), fill=ink)
    draw.text((80, 850), "From the site" if f["display"]["source"] == "site" else "Open substitute for " + str(f["display"].get("site_family") or "the site's font"),
              font=font("body", 22), fill=ink)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def scan(data_dir: Path, url: str, *, collector: Any = None) -> dict[str, Any]:
    """Scans a page and keeps the kit on disk (kit.json, logo.png, brand-board.png, the site's fonts when reusable)."""
    url = normalise_url(url)
    raw = (collector or collect_page)(url)
    kit = build_kit(raw, url)
    kit_id = kit_id_for(url)
    folder = kit_folder(data_dir, kit_id)
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    kit["id"] = kit_id
    logo = prepare_logo(raw["logo_png"]) if raw.get("logo_png") else None
    if logo:
        (folder / "logo.png").write_bytes(logo["png"])
        kit["logo"] = {"file": "logo.png", "source": raw.get("logo_from") or "page", "width": logo["width"], "height": logo["height"],
                       "luminance": logo["luminance"]}
    else:
        kit["logo"] = None
    for family, got in (raw.get("font_files") or {}).items():
        if family == "_why" or not isinstance(got, dict):
            continue
        name = re.sub(r"[^A-Za-z0-9]+", "", family) + "." + got["ext"]
        (folder / "fonts").mkdir(exist_ok=True)
        (folder / "fonts" / name).write_bytes(got["bytes"])
        for role in ("display", "body"):
            if kit["fonts"].get(role, {}).get("site_family") == family and kit["fonts"][role]["source"] == "site":
                kit["fonts"][role]["file"] = str(folder / "fonts" / name)
    kit["files"] = {"folder": str(folder)}
    (folder / "brand-board.png").write_bytes(brand_board(kit, logo["png"] if logo else None))
    if raw.get("screenshots"):
        (folder / "capture.png").write_bytes(raw["screenshots"][0])
    (folder / "kit.json").write_text(json.dumps(kit, ensure_ascii=False, indent=2), encoding="utf-8")
    return kit


def normalise_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise PromoBrandError("url_required", "give the address of the web page (url)")
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PromoBrandError("bad_url", "only http and https addresses can be scanned")
    return url


def import_kit_assets(store: Any, project_id: str, kit: dict[str, Any]) -> dict[str, Any]:
    """Puts the kit in the project's library: the logo and the brand board as image assets tagged brand, with the kit in the recipe."""
    folder = Path(kit["files"]["folder"])
    summary = {k: kit.get(k) for k in ("id", "url", "host", "site_name", "headline", "colors", "palette")}
    ids: dict[str, str] = {}
    for key, name, tags in (("logo", "logo.png", ["brand", "logo"]), ("board", "brand-board.png", ["brand", "brand-board"])):
        path = folder / name
        if path.is_file():
            asset = engine.import_asset(store, project_id, path, "image", original_name=f"{kit['host']} {name}", tags=tags, source="rendered",
                                        recipe={"operation": "brand_scan", "kit_id": kit["id"], "input_asset_ids": [], "kit": summary})
            ids[key] = asset["id"]
    return ids
