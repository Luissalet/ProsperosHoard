"""Anti-slideshow lint for HTML motion compositions.

A motion piece where three or more sections only fade in and out (opacity / autoAlpha with nothing moving) reads as a
slideshow, not as motion. The lint reads the composition's own animation code - GSAP tweens (to / from / fromTo / set)
on a selector, CSS @keyframes and opacity transitions - and counts the targets whose only animated property is opacity.
Three or more: verdict ``reject`` with a re-prompt the author (or the assistant) uses to rewrite the scene with real
movement. Static and deterministic: no browser, so it runs on every save. The rule follows bangtutorial/bang-motion (MIT).
"""
from __future__ import annotations

import re
from typing import Any

THRESHOLD = 3
OPACITY_KEYS = {"opacity", "autoalpha"}
MOTION_KEYS = {"x", "y", "z", "xpercent", "ypercent", "scale", "scalex", "scaley", "rotation", "rotate", "rotatex", "rotatey",
               "skew", "skewx", "skewy", "left", "top", "right", "bottom", "width", "height", "clippath", "motionpath",
               "drawsvg", "strokedashoffset", "transform", "translate", "backgroundposition", "filter", "morphsvg", "text",
               "attr", "fontsize", "letterspacing", "borderradius"}
IGNORED_KEYS = {"duration", "delay", "ease", "stagger", "repeat", "yoyo", "repeatdelay", "onstart", "oncomplete",
                "onupdate", "immediaterender", "overwrite", "paused", "id", "transformorigin", "display", "visibility",
                "zindex", "pointerevents", "color", "backgroundcolor", "background"}
_TWEEN = re.compile(r"\.(to|from|fromTo|set)\(\s*([\"'`])(.+?)\2\s*,\s*(\{[^{}]*\})(?:\s*,\s*(\{[^{}]*\}))?", re.S)
_KEY = re.compile(r"([A-Za-z_$][\w$]*)\s*:")
_KEYFRAMES = re.compile(r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)[^{}]*\}", re.S)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _keys(obj: str) -> set[str]:
    return {k.lower() for k in _KEY.findall(obj or "")}


def _mark(targets: dict[str, dict[str, Any]], selector: str, keys: set[str]) -> None:
    animated = keys - IGNORED_KEYS
    if not animated:
        return
    t = targets.setdefault(selector, {"opacity": False, "motion": False, "tweens": 0})
    t["tweens"] += 1
    t["opacity"] |= bool(animated & OPACITY_KEYS)
    t["motion"] |= bool(animated - OPACITY_KEYS)  # anything else that changes over time is movement


def slideshow_lint(html: str, threshold: int = THRESHOLD) -> dict[str, Any]:
    """Targets animated only through opacity; `reject` with a re-prompt at `threshold` or more."""
    targets: dict[str, dict[str, Any]] = {}
    for _method, _q, selector, first, second in _TWEEN.findall(html or ""):
        _mark(targets, selector.strip(), _keys(first) | _keys(second))
    frames: dict[str, set[str]] = {}
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html or "", re.S | re.I))  # CSS only: script braces are not rules
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for name, body in _KEYFRAMES.findall(css):
        props = {p.strip().lower() for block in re.findall(r"\{([^{}]*)\}", body) for p in re.findall(r"([\w-]+)\s*:", block)}
        frames[name] = {"opacity"} if props <= {"opacity"} else (props | {"transform"})
    stripped = _KEYFRAMES.sub("", css)
    for selector, body in _RULE.findall(stripped):
        selector = " ".join(selector.split())[-120:]
        decl = body.lower()
        for name, props in frames.items():
            if re.search(rf"animation(-name)?\s*:[^;]*\b{re.escape(name.lower())}\b", decl):
                _mark(targets, selector, {p.replace("-", "") for p in props})
        trans = re.search(r"transition\s*:\s*([^;]+)", decl)
        if trans:
            props = {p.strip().split()[0].replace("-", "") for p in trans.group(1).split(",") if p.strip()}
            if props and props <= {"opacity"}:
                _mark(targets, selector, {"opacity"})
    toggled = sorted(s for s, t in targets.items() if t["opacity"] and not t["motion"])
    moving = sorted(s for s, t in targets.items() if t["motion"])
    reject = len(toggled) >= threshold
    out: dict[str, Any] = {"verdict": "reject" if reject else ("warn" if toggled and not moving else "pass"),
                           "opacity_only": toggled[:24], "opacity_only_count": len(toggled), "moving_count": len(moving),
                           "threshold": threshold}
    if reject:
        out["reprompt"] = (f"Rewrite this as motion, not a slideshow: {len(toggled)} sections ({', '.join(toggled[:6])}) only fade "
                           "in and out. Give every section its own movement - x/y travel, a scale push-in, a clip-path or mask "
                           "wipe, staggered entrances of its elements - carry at least one element across each section change "
                           "instead of cross-fading whole sections, and keep opacity only as a helper of a move.")
    return out
