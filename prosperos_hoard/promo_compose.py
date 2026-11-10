"""The script of a promo and its composition: shots planned from the brand kit, each frame built in code as an HTML page with a timeline.

`plan_script` writes the shots (the local writing model when there is one, a deterministic template made of the phrases of the site
when there is not), snapped to a beat grid; `build_html` turns the script, the kit and a canvas into a page whose `window.seek(t)` draws any
instant exactly (the same convention as the HTML compositions of the editing studio) and whose `window.measure()` reports the text boxes
for the checks. Nothing here needs a browser.
"""
from __future__ import annotations

import html as htmllib
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from . import engine
from .promo_brand import clean_text, contrast, from_hex, mix, to_hex, luminance

VIBES: dict[str, dict[str, Any]] = {
    "keynote": {"label": "Keynote", "shot_s": 4.0, "bpm": 96, "scale": 1.0, "wipe_s": 0.55, "hook_bg": "dark",
                "about": "Big type, calm pace, dark opening."},
    "launch": {"label": "Lanzamiento", "shot_s": 2.8, "bpm": 124, "scale": 1.06, "wipe_s": 0.4, "hook_bg": "primary",
               "about": "Fast shots, strong colour, more beats."},
    "minimal": {"label": "Minimal", "shot_s": 5.0, "bpm": 80, "scale": 0.92, "wipe_s": 0.7, "hook_bg": "light",
                "about": "Light background, slow, few words."},
}
FORMATS: dict[str, tuple[int, int]] = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
CTA = {"es": "Descúbrelo en", "en": "Discover it at"}
MIN_LENGTH_S, MAX_LENGTH_S = 6.0, 120.0
SETTLE_S = 0.95            # a shot's text has arrived this long after the shot starts
LEAVE_S = 0.45            # and starts to leave this long before the next one covers it


class PromoError(engine.EngineError):
    pass


def check_request(length_s: Any, formats: Any, vibe: Any, fps: Any) -> tuple[float, list[str], str, int]:
    try:
        length = float(length_s if length_s is not None else 30)
    except (TypeError, ValueError):
        raise PromoError("bad_length", "length_s must be a number of seconds") from None
    if not MIN_LENGTH_S <= length <= MAX_LENGTH_S:
        raise PromoError("bad_length", f"length_s must be between {MIN_LENGTH_S:g} and {MAX_LENGTH_S:g} seconds")
    wanted = list(formats) if formats else ["16:9", "9:16"]
    bad = [f for f in wanted if f not in FORMATS]
    if bad:
        raise PromoError("bad_format", f"formats are {', '.join(FORMATS)}; not {', '.join(map(str, bad))}")
    chosen = list(dict.fromkeys(wanted))
    style = (vibe or "keynote")
    if style not in VIBES:
        raise PromoError("bad_vibe", f"vibe is one of {', '.join(VIBES)}")
    try:
        rate = int(fps if fps is not None else 60)
    except (TypeError, ValueError):
        raise PromoError("bad_fps", "fps must be a whole number") from None
    if rate not in (24, 25, 30, 50, 60):
        raise PromoError("bad_fps", "fps is 24, 25, 30, 50 or 60")
    return length, chosen, style, rate


# ------------------------------------------------------------------ colours of a shot

def shot_colors(palette: dict[str, str], role: str, forced_ink: Optional[str] = None) -> dict[str, str]:
    """Background, ink and mark colour of a shot, all from the palette; the ink is the dark or the light one with the better contrast."""
    base = from_hex(palette.get(role) or palette["dark"])
    dark, light = from_hex(palette["dark"]), from_hex(palette["light"])
    ink = from_hex(forced_ink) if forced_ink else (dark if contrast(dark, base) >= contrast(light, base) else light)
    if contrast(ink, base) < 4.5:                              # a mid colour neither ink reads on: pushed towards the side the ink is not
        target = (255, 255, 255) if luminance(ink) < 0.4 else (0, 0, 0)
        for step in range(1, 10):
            moved = mix(base, target, step / 10)
            if contrast(ink, moved) >= 4.5:
                base = moved
                break
    marks = [from_hex(palette[k]) for k in ("secondary", "accent", "primary") if palette.get(k)]
    marks = [m for m in marks if contrast(m, base) >= 3.0 and m != base]
    mark = max(marks, key=lambda m: contrast(m, base)) if marks else ink
    return {"bg": to_hex(base), "ink": to_hex(ink), "mark": to_hex(mark)}


# ------------------------------------------------------------------ the script

def resolve_language(kit: dict[str, Any], language: Optional[str]) -> str:
    lang = (language or "auto").lower()[:2]
    if lang in CTA:
        return lang
    return kit.get("language") if kit.get("language") in CTA else "en"


def choose_beats(shot_s: float, bpm: float) -> int:
    return min((2, 4, 6, 8), key=lambda b: abs(b * 60.0 / shot_s - bpm))


def template_texts(kit: dict[str, Any], n_points: int, language: str) -> dict[str, Any]:
    phrases = [clean_text(p, 80) for p in kit.get("phrases") or []]
    value = clean_text(kit.get("value_prop") or (phrases.pop() if len(phrases) > n_points else kit.get("headline", "")), 120)
    tagline = clean_text(kit.get("headline", ""), 60)
    return {"hook": {"text": clean_text(kit.get("headline") or kit["site_name"], 90), "sub": kit["site_name"]},
            "points": phrases[:n_points], "value": value,
            "logo": {"text": kit["site_name"], "sub": tagline if tagline != clean_text(kit.get("headline", ""), 90) or len(tagline) < 60 else ""},
            "cta": {"text": kit["host"], "sub": CTA[language]}}


def _model_texts(chat: Callable[..., str], kit: dict[str, Any], n_points: int, vibe: str, language: str) -> dict[str, Any]:
    lang_name = {"es": "Spanish (Spain)", "en": "English"}[language]
    system = ("You write the on-screen text of a short promo video for a web site. Reply with one JSON object and nothing else: "
              '{"hook": {"text": "...", "sub": "..."}, "points": ["..."], "value": "...", "cta": "..."}. '
              "hook.text is a punchy opening line (max 60 characters), hook.sub a short line under it (max 70). points has exactly "
              f"{n_points} claims, each at most 55 characters, concrete, taken from the site's own phrases (no invented numbers or promises). "
              "value is the value proposition in one sentence (max 110 characters). cta is a short call to action (max 28 characters). "
              f"Language: {lang_name}. No emojis, no hashtags, no marketing clichés. Tone: {VIBES[vibe]['about']}")
    user = json.dumps({"site": kit["site_name"], "host": kit["host"], "headline": kit.get("headline"), "value_proposition": kit.get("value_prop"),
                       "phrases": kit.get("phrases")}, ensure_ascii=False)
    reply = chat([{"role": "system", "content": system}, {"role": "user", "content": user}], 900, 0.5)
    match = re.search(r"\{.*\}", str(reply), re.S)
    if not match:
        raise ValueError("no JSON in the reply")
    data = json.loads(match.group(0))
    out: dict[str, Any] = {}
    hook = data.get("hook") or {}
    if isinstance(hook.get("text"), str) and 3 <= len(hook["text"].strip()) <= 80:
        out["hook"] = {"text": hook["text"].strip(), "sub": clean_text(str(hook.get("sub") or kit["site_name"]), 80)}
    points = [clean_text(str(p), 70) for p in data.get("points") or [] if isinstance(p, str) and 6 <= len(p.strip()) <= 90]
    if len(points) >= n_points:
        out["points"] = points[:n_points]
    if isinstance(data.get("value"), str) and 10 <= len(data["value"].strip()) <= 140:
        out["value"] = data["value"].strip()
    if isinstance(data.get("cta"), str) and 3 <= len(data["cta"].strip()) <= 36:
        out["cta"] = data["cta"].strip()
    return out


def plan_script(kit: dict[str, Any], length_s: float, vibe: str = "keynote", language: str = "auto",
                chat: Optional[Callable[..., str]] = None) -> dict[str, Any]:
    """The shots of the promo. Shots are all the same length and that length is a whole number of beats, so the tempo of the pulse
    and the cuts agree."""
    style = VIBES[vibe]
    lang = resolve_language(kit, language)
    n = max(4, min(12, round(length_s / style["shot_s"])))
    phrases = [p for p in kit.get("phrases") or [] if p]
    n = max(4, min(n, 4 + len(phrases)))
    n_points = n - 4
    texts = template_texts(kit, n_points, lang)
    written_by = "template"
    if chat is not None:
        try:
            got = _model_texts(chat, kit, n_points, vibe, lang)
            for key in ("hook", "points", "value"):
                if key in got:
                    texts[key] = got[key]
            if "cta" in got:
                texts["cta"]["sub"] = got["cta"]
            written_by = "model" if got else "template"
        except Exception:  # noqa: BLE001 - no model, a stalled one, bad JSON: the template stands
            written_by = "template"
    texts["points"] = (texts["points"] + [kit["site_name"]] * n_points)[:n_points]
    dur = length_s / n
    beats = choose_beats(dur, style["bpm"])
    logo = kit.get("logo") or {}
    logo_bg = "dark" if (logo.get("luminance") or 0) > 0.5 else "light"
    cycle = ["light", "secondary", "dark", "accent", "light", "primary"]
    kinds = ["hook"] + ["point"] * n_points + ["value", "logo", "cta"]
    shots = []
    pi = 0
    for i, kind in enumerate(kinds):
        if kind == "hook":
            text, sub, bg = texts["hook"]["text"], texts["hook"]["sub"], style["hook_bg"]
        elif kind == "point":
            text, sub, bg = texts["points"][pi], "", cycle[pi % len(cycle)]
            pi += 1
        elif kind == "value":
            text, sub, bg = texts["value"], "", "primary"
        elif kind == "logo":
            text, sub, bg = texts["logo"]["text"], texts["logo"]["sub"], logo_bg
        else:
            text, sub, bg = texts["cta"]["text"], texts["cta"]["sub"], "primary" if kinds[i - 1] != "value" else "dark"
        shots.append({"key": f"s{i + 1}", "kind": kind, "text": text, "sub": sub, "start": round(i * dur, 4), "dur": round(dur, 4),
                      "bg_role": bg, "fit": 1.0, "layout": "center" if kind in ("logo", "cta") else "left", "drift": 1.0,
                      "index": f"{pi:02d}" if kind == "point" else ""})
    return {"vibe": vibe, "language": lang, "length_s": round(length_s, 3), "shots": shots, "beats_per_shot": beats,
            "bpm": round(beats * 60.0 / dur, 2), "written_by": written_by, "wipe_s": style["wipe_s"], "deco": True}


# ------------------------------------------------------------------ the page

BASE_SIZES = {"hook": (136, 120), "point": (120, 106), "value": (96, 88), "logo": (84, 84), "cta": (128, 104)}
SUB_SIZE = (54, 50)


def font_size(shot: dict[str, Any], portrait: bool, vibe: str) -> float:
    base = BASE_SIZES[shot["kind"]][1 if portrait else 0]
    length = len(shot["text"])
    shrink = min(1.0, max(0.6, (26.0 / max(1, length)) ** 0.3)) if length > 26 else 1.0
    return round(base * VIBES[vibe]["scale"] * shrink * float(shot.get("fit", 1.0)), 2)


def _words(text: str) -> str:
    parts = []
    for word in str(text).split():
        parts.append(f'<span class="w"><span class="wi">{htmllib.escape(word)}</span></span>')
    return " ".join(parts)


def _font_face(name: str, font: dict[str, Any], relative: str) -> str:
    ext = Path(relative).suffix.lstrip(".").lower()
    fmt = {"woff2": "woff2", "woff": "woff", "ttf": "truetype", "otf": "opentype"}.get(ext, "truetype")
    weight = "100 900" if font.get("variable") else str(font.get("weight", 400))
    return (f'@font-face{{font-family:"{name}";src:url("{relative}") format("{fmt}");font-weight:{weight};font-style:normal;font-display:block}}')


PAGE_JS = r"""
(function () {
  const DATA = JSON.parse(document.getElementById('promo-data').textContent);
  const E = { out: t => 1 - Math.pow(1 - t, 4), expo: t => t >= 1 ? 1 : 1 - Math.pow(2, -10 * t),
              inout: t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2,
              back: t => { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); } };
  const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
  const prog = (t, a, d) => clamp((t - a) / d);
  const root = document.documentElement;
  let W = innerWidth, H = innerHeight;
  const portrait = H > W;
  const U = W / (portrait ? 1080 : 1920);
  root.style.setProperty('--u', U + 'px');
  const stage = document.getElementById('stage');
  const rail = document.querySelector('#rail i');
  const shots = [...document.querySelectorAll('.shot')].map((el, i) => ({
    el, i, d: DATA.shots[i], bg: el.querySelector('.bg'), copy: el.querySelector('.copy'),
    words: [...el.querySelectorAll('.t .wi')], subs: [...el.querySelectorAll('.sub .wi')], rule: el.querySelector('.rule'),
    idx: el.querySelector('.idx'), logo: el.querySelector('.logo'), ring: el.querySelector('.ring'), deco: el.querySelector('.deco') }));
  const WIPE = DATA.wipe_s, TOTAL = DATA.length_s;
  function wipePath(i, p) {
    const r = (1 - p) * 100, side = portrait ? (i % 2 ? 'v' : 'v2') : ['h', 'v', 'h2'][i % 3];
    if (side === 'h') return `inset(0 ${r}% 0 0)`;
    if (side === 'h2') return `inset(0 0 0 ${r}%)`;
    if (side === 'v') return `inset(${r}% 0 0 0)`;
    return `inset(0 0 ${r}% 0)`;
  }
  window.seek = function (t) {
    const last = shots.length - 1;
    for (const s of shots) {
      const d = s.d, end = d.start + d.dur;
      const show = (s.i === 0 ? t < end + WIPE : (t >= d.start && t < end + WIPE)) || (s.i === last && t >= d.start);
      s.el.style.display = show ? 'block' : 'none';
      if (!show) continue;
      s.el.style.zIndex = s.i + 1;
      const wp = s.i === 0 ? 1 : E.inout(prog(t, d.start, WIPE));
      s.bg.style.clipPath = wp >= 1 ? 'none' : wipePath(s.i, wp);
      const lt = t - d.start - (s.i === 0 ? -0.2 : 0.14);
      const leave = s.i === last ? 0 : E.inout(prog(t, end - 0.35, 0.35 + WIPE));
      const drift = (t - d.start) / d.dur;
      s.copy.style.transform = `translate(0, ${(-leave * 70 - drift * 14 * d.drift) * U}px)`;
      s.words.forEach((w, k) => { const p = E.expo(prog(lt, 0.04 + 0.07 * k, 0.75)); w.style.transform = `translateY(${(1 - p) * 118}%) skewY(${(1 - p) * 4}deg)`; });
      const sw = s.words.length;
      s.subs.forEach((w, k) => { const p = E.expo(prog(lt, 0.45 + 0.05 * k + 0.04 * sw * 0.4, 0.7)); w.style.transform = `translateY(${(1 - p) * 118}%)`; });
      if (s.rule) s.rule.style.transform = `scaleX(${E.out(prog(lt, 0.25, 0.8))})`;
      if (s.idx) s.idx.style.transform = `translate(${(1 - E.out(prog(lt, 0, 0.6))) * -60 * U}px, 0)`;
      if (s.logo) { const p = E.back(prog(lt, 0.18, 0.7)); s.logo.style.transform = `scale(${0.55 + 0.45 * p})`; s.logo.style.clipPath = `inset(0 ${(1 - E.out(prog(lt, 0.18, 0.5))) * 100}% 0 0)`; }
      if (s.ring) { const p = E.out(prog(lt, 0.4, 1.0)); s.ring.style.transform = `translate(-50%, -50%) scale(${0.3 + 0.85 * p})`; s.ring.style.borderWidth = ((1 - p) * 20 + 3) * U + 'px'; }
      if (s.deco) s.deco.style.transform = `translate(${(-drift * 120 * d.drift) * U}px, ${(drift * 90 * d.drift) * U}px) scale(${1 + 0.18 * drift * d.drift})`;
      if (t >= d.start && t < end + 0.001) rail.style.background = d.mark;
    }
    rail.style.width = clamp(t / TOTAL) * 100 + '%';
  };
  window.measure = function () {
    const items = [];
    for (const el of document.querySelectorAll('[data-fit]')) {
      const sh = el.closest('.shot');
      if (!sh || sh.style.display === 'none') continue;
      const range = document.createRange(); range.selectNodeContents(el);
      const rects = [...range.getClientRects()].filter(r => r.width > 1 && r.height > 1);
      const box = rects.length ? { x0: Math.min(...rects.map(r => r.left)), y0: Math.min(...rects.map(r => r.top)),
        x1: Math.max(...rects.map(r => r.right)), y1: Math.max(...rects.map(r => r.bottom)) } : null;
      const cs = getComputedStyle(el);
      items.push({ id: el.dataset.fit, shot: sh.dataset.key, role: el.dataset.role, text: (el.textContent || '').trim().slice(0, 90), box,
        sw: el.scrollWidth, cw: el.clientWidth, fs: parseFloat(cs.fontSize), color: cs.color, bg: sh.dataset.bg });
    }
    return { W: innerWidth, H: innerHeight, items };
  };
  const loads = ['display', 'body'].map(k => document.fonts.load(`${k === 'display' ? DATA.fonts.display_weight : DATA.fonts.body_weight} 80px "Promo${k === 'display' ? 'Display' : 'Body'}"`));
  Promise.all(loads).then(() => document.fonts.ready).then(() => { window.seek(0); window.__ready = true; }, () => { window.seek(0); window.__ready = true; });
  window.seek(0);
})();
"""


def build_html(kit: dict[str, Any], script: dict[str, Any], width: int, height: int, fps: int, folder: Path) -> Path:
    """Writes `composition.html` (and the logo and the fonts it uses) in `folder`; returns its path."""
    folder.mkdir(parents=True, exist_ok=True)
    portrait = height > width
    vibe = script["vibe"]
    palette = kit["palette"]
    fonts_dir = folder / "fonts"
    fonts_dir.mkdir(exist_ok=True)
    faces, families = [], {}
    for role, css_name in (("display", "PromoDisplay"), ("body", "PromoBody")):
        font = kit["fonts"][role]
        source = Path(font["file"])
        target = fonts_dir / f"{role}{source.suffix.lower()}"
        shutil.copyfile(source, target)
        faces.append(_font_face(css_name, font, f"fonts/{target.name}"))
        families[role] = font
    logo_html = ""
    if kit.get("logo"):
        shutil.copyfile(Path(kit["files"]["folder"]) / kit["logo"]["file"], folder / "logo.png")
    shots_html, data_shots = [], []
    for shot in script["shots"]:
        colours = shot_colors(palette, shot.get("bg_role", "dark"), shot.get("ink_force"))
        size = font_size(shot, portrait, vibe)
        sub_size = round(SUB_SIZE[1 if portrait else 0] * VIBES[vibe]["scale"] * min(1.0, float(shot.get("fit", 1.0)) + 0.1), 2)
        centered = shot["layout"] == "center"
        key = shot["key"]
        parts = []
        if shot["kind"] == "point" and shot.get("index"):
            parts.append(f'<div class="idx" data-fit="{key}-idx" data-role="idx" style="font-size:calc(var(--u)*{round(size * 0.42, 1)})">{htmllib.escape(shot["index"])}</div>')
        if shot["kind"] == "logo":
            if kit.get("logo"):
                parts.append('<div class="logowrap"><div class="ring"></div><img class="logo" src="logo.png" alt=""></div>')
            else:
                letter = htmllib.escape((kit["site_name"] or "?")[0].upper())
                parts.append(f'<div class="logowrap"><div class="ring"></div><div class="logo mono" data-fit="{key}-mono" data-role="mono">{letter}</div></div>')
        if shot["kind"] == "cta" and shot["sub"]:
            parts.append(f'<div class="sub pre" data-fit="{key}-sub" data-role="sub" style="font-size:calc(var(--u)*{sub_size})">{_words(shot["sub"])}</div>')
        parts.append(f'<div class="t" data-fit="{key}-t" data-role="title" style="font-size:calc(var(--u)*{size})">{_words(shot["text"])}</div>')
        if shot["kind"] != "cta" and shot["sub"]:
            parts.append(f'<div class="sub" data-fit="{key}-sub" data-role="sub" style="font-size:calc(var(--u)*{sub_size})">{_words(shot["sub"])}</div>')
        parts.append('<div class="rule"></div>')
        deco = '<div class="deco"></div>' if script.get("deco", True) else ""
        shots_html.append(
            f'<section class="shot{" c" if centered else ""} k-{shot["kind"]}" data-key="{key}" data-bg="{colours["bg"]}" '
            f'style="--bg:{colours["bg"]};--ink:{colours["ink"]};--mark:{colours["mark"]}"><div class="bg"></div>{deco}'
            f'<div class="copy">{"".join(parts)}</div></section>')
        data_shots.append({"key": key, "start": shot["start"], "dur": shot["dur"], "kind": shot["kind"], "mark": colours["mark"],
                           "drift": float(shot.get("drift", 1.0))})
    data = {"shots": data_shots, "length_s": script["length_s"], "wipe_s": float(script.get("wipe_s", 0.5)), "fps": fps,
            "fonts": {"display_weight": int(families["display"].get("weight", 700)), "body_weight": int(families["body"].get("weight", 400))}}
    width_copy = "84%" if portrait else "70%"
    css = f"""
{chr(10).join(faces)}
html,body{{margin:0;padding:0;width:100vw;height:100vh;overflow:hidden;background:#000}}
#stage{{position:absolute;inset:0;overflow:hidden;font-family:"PromoBody","PromoDisplay",sans-serif;font-synthesis:none}}
.shot{{position:absolute;inset:0;display:none;overflow:hidden;color:var(--ink)}}
.bg{{position:absolute;inset:0;background:var(--bg)}}
.copy{{position:absolute;left:8%;top:0;height:100%;width:{width_copy};display:flex;flex-direction:column;justify-content:center;
  gap:calc(var(--u)*26);will-change:transform}}
.shot.c .copy{{left:8%;width:84%;align-items:center;text-align:center}}
.t{{font-family:"PromoDisplay",sans-serif;font-weight:{int(families["display"].get("weight", 700))};line-height:1.06;letter-spacing:-0.012em;color:var(--ink);
  overflow-wrap:break-word;hyphens:manual}}
.sub{{font-family:"PromoBody",sans-serif;font-weight:{int(families["body"].get("weight", 400))};line-height:1.25;color:var(--ink);overflow-wrap:break-word}}
.sub.pre{{margin-bottom:calc(var(--u)*-6)}}
.idx{{font-family:"PromoDisplay",sans-serif;font-weight:{int(families["display"].get("weight", 700))};color:var(--mark);letter-spacing:.08em}}
.w{{display:inline-block;overflow:hidden;vertical-align:top;padding-bottom:.16em;margin-bottom:-.16em}}
.wi{{display:inline-block;will-change:transform}}
.rule{{height:calc(var(--u)*10);width:calc(var(--u)*220);background:var(--mark);transform-origin:left center;border-radius:calc(var(--u)*5)}}
.shot.c .rule{{transform-origin:center}}
.logowrap{{position:relative;display:flex;justify-content:center;align-items:center;width:100%;height:calc(var(--u)*{300 if not portrait else 260})}}
.logo{{max-width:{56 if not portrait else 78}%;max-height:100%;object-fit:contain;display:block;will-change:transform}}
.logo.mono{{display:flex;align-items:center;justify-content:center;width:calc(var(--u)*240);height:calc(var(--u)*240);border-radius:50%;background:var(--mark);
  color:var(--bg);font-family:"PromoDisplay",sans-serif;font-weight:{int(families["display"].get("weight", 700))};font-size:calc(var(--u)*150)}}
.ring{{position:absolute;left:50%;top:50%;width:calc(var(--u)*260);height:calc(var(--u)*260);border-radius:50%;border:solid var(--mark);
  box-sizing:border-box;transform:translate(-50%,-50%) scale(.3)}}
.deco{{position:absolute;right:calc(var(--u)*-230);{"bottom" if portrait else "top"}:calc(var(--u)*-230);width:calc(var(--u)*560);height:calc(var(--u)*560);
  border:calc(var(--u)*16) solid var(--mark);border-radius:50%;box-sizing:border-box}}
#rail{{position:absolute;left:0;right:0;bottom:0;height:calc(var(--u)*10);z-index:99}}
#rail i{{display:block;height:100%;width:0;background:#fff}}
"""
    page = (f'<!doctype html>\n<html lang="{script["language"]}"><head><meta charset="utf-8"><meta name="viewport" content="width={width},height={height}">'
            f'<title>{htmllib.escape(kit["site_name"])} promo</title><style>{css}</style></head><body>'
            f'<div id="stage">{"".join(shots_html)}<div id="rail"><i></i></div></div>'
            f'<script id="promo-data" type="application/json">{json.dumps(data, ensure_ascii=False)}</script><script>{PAGE_JS}</script></body></html>')
    path = folder / "composition.html"
    path.write_text(page, encoding="utf-8")
    return path


def shot_windows(script: dict[str, Any]) -> list[tuple[dict[str, Any], float, float]]:
    """(shot, first, last) of the moments in which its text has settled: the checks look at the text only then."""
    out = []
    for shot in script["shots"]:
        last_shot = shot is script["shots"][-1]
        first = shot["start"] + SETTLE_S
        last = shot["start"] + shot["dur"] - (0.05 if last_shot else LEAVE_S)
        out.append((shot, first, last))
    return out
