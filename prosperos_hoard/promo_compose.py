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

LOOK_DEFAULT: dict[str, Any] = {"reveal": "mask", "transition": "wipe", "deco": "ring", "speed": 1.0, "pulse": 0.0, "slam": 0.0, "upper": False,
                                "idx": 0.42, "leading": 1.06, "left": 8.0, "width": (70.0, 84.0), "sound": "pulse", "jump": 34.0}
VIBES: dict[str, dict[str, Any]] = {
    "keynote": {"label": "Keynote", "shot_s": 4.0, "bpm": 96, "scale": 1.0, "wipe_s": 0.55, "hook_bg": "dark",
                "about": "Big type, calm pace, dark opening."},
    "launch": {"label": "Lanzamiento", "shot_s": 2.8, "bpm": 124, "scale": 1.06, "wipe_s": 0.4, "hook_bg": "primary",
               "about": "Fast shots, strong colour, more beats."},
    "minimal": {"label": "Minimal", "shot_s": 5.0, "bpm": 80, "scale": 0.92, "wipe_s": 0.7, "hook_bg": "light",
                "about": "Light background, slow, few words."},
    "editorial": {"label": "Editorial", "shot_s": 4.8, "bpm": 72, "scale": 0.86, "wipe_s": 0.9, "hook_bg": "light",
                  "about": "Typographic, lots of space, light pages that dissolve into each other, soft chords and ticks.",
                  "cycle": ["light", "light", "dark", "light", "secondary", "light"], "value_bg": "dark", "cta_bg": "light",
                  "look": {"reveal": "fade", "transition": "fade", "deco": "hair", "speed": 1.35, "idx": 0.78, "leading": 1.14, "left": 13.0,
                           "width": (56.0, 76.0), "sound": "soft"}},
    "energetic": {"label": "Energético", "shot_s": 2.5, "bpm": 140, "scale": 1.1, "wipe_s": 0.28, "hook_bg": "primary",
                  "about": "Quick cuts, capitals, words that slam in on the beat, bars that move, four-on-the-floor drive.",
                  "cycle": ["dark", "accent", "light", "secondary", "dark", "primary"], "value_bg": "dark", "cta_bg": "accent", "fragments": True,
                  "look": {"reveal": "pop", "transition": "wipe", "deco": "bars", "speed": 0.55, "pulse": 0.07, "slam": 0.12, "upper": True,
                           "idx": 0.5, "leading": 1.0, "sound": "drive", "jump": 72.0}},
}
FORMATS: dict[str, tuple[int, int]] = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
CTA = {"es": "Descúbrelo en", "en": "Discover it at"}
MIN_LENGTH_S, MAX_LENGTH_S = 6.0, 120.0
SETTLE_S = 0.95            # fallback: a shot's text has arrived this long after the shot starts (the real figure is shot["tm"]["settle"])
LEAVE_S = 0.45            # and starts to leave this long before the next one covers it
MIN_HOLD_S = 1.5          # every text is complete and readable at least this long before the cut that ends its shot
MIN_PACE = 0.35           # the reveal can be sped up to this fraction of its normal duration to make room for the hold
MAX_OWN_LINE = 140


def look_of(vibe: str) -> dict[str, Any]:
    return {**LOOK_DEFAULT, **VIBES[vibe].get("look", {})}


def retime(shot: dict[str, Any], vibe: str) -> dict[str, Any]:
    """The timeline of a shot's text, the one source the page and the checks share: when each word starts and how long it takes, and
    `settle`, the second (after the shot starts) at which all its text is complete. `shot["pace"]` shortens the reveal to make room."""
    k = max(MIN_PACE * 0.5, float(shot.get("pace", 1.0))) * look_of(vibe)["speed"]
    n = max(1, len(str(shot["text"]).split()))
    m = len(str(shot.get("sub") or "").split())
    t0, tw = 0.04 * k, 0.75 * k
    ts = min(0.07, 0.5 / max(1, n - 1)) * k
    title_end = t0 + ts * (n - 1) + tw
    s0 = (0.45 + min(0.2, 0.016 * n)) * k
    sst = min(0.05, 0.4 / max(1, m - 1)) * k
    sw = 0.7 * k
    end = max(title_end, (s0 + sst * (m - 1) + sw) if m else 0.0)
    if shot.get("kind") == "logo":
        end = max(end, 0.9 * k)
    # Outgoing text finishes its dissolve 0.05 s after the cut. Give the
    # new text a short gap without spending half a second of a short shot.
    lead = -0.2 if shot.get("lead_in") else 0.14
    shot["tm"] = {"lead": lead, "t0": round(t0, 4), "ts": round(ts, 4), "tw": round(tw, 4), "s0": round(s0, 4), "sst": round(sst, 4), "sw": round(sw, 4),
                  "settle": round(max(0.0, lead + end), 4)}
    return shot["tm"]


def fit_hold(shot: dict[str, Any], vibe: str) -> bool:
    """Speeds the reveal of a shot up (never below MIN_PACE) until its text has settled MIN_HOLD_S before the cut; False if even that is not enough."""
    shot["pace"] = 1.0
    if retime(shot, vibe)["settle"] <= shot["dur"] - MIN_HOLD_S:
        return True
    lo, hi = MIN_PACE, 1.0
    shot["pace"] = lo
    if retime(shot, vibe)["settle"] > shot["dur"] - MIN_HOLD_S:
        return False
    for _ in range(14):
        mid = (lo + hi) / 2
        shot["pace"] = mid
        if retime(shot, vibe)["settle"] <= shot["dur"] - MIN_HOLD_S:
            lo = mid
        else:
            hi = mid
    shot["pace"] = round(lo, 4)
    retime(shot, vibe)
    return True


def hold_of(shot: dict[str, Any]) -> float:
    """Seconds the shot's text is complete on screen before the cut."""
    return round(shot["dur"] - float((shot.get("tm") or {}).get("settle", SETTLE_S)), 3)


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


def own_line(value: Any, limit: int = MAX_OWN_LINE) -> str:
    """A line of the user's own: whitespace collapsed and cut at a word if it is longer than the limit (never an ellipsis: it is their text)."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:- ") if " " in text[:limit] else text[:limit]


def check_custom(custom: Any) -> dict[str, Any]:
    """The user's own script, validated: `headlines` (the lines of the middle shots, or a multi-line `script`), `hook`, `hook_sub`, `value`, `cta`
    (the small line of the last shot) and `cta_text` (its big line, the address by default). Empty things are dropped."""
    if not custom:
        return {}
    if not isinstance(custom, dict):
        raise PromoError("bad_script", "the own script must be an object")
    out: dict[str, Any] = {}
    lines = custom.get("headlines")
    if lines is None and custom.get("script"):
        lines = str(custom["script"]).splitlines()
    if lines is not None:
        if not isinstance(lines, (list, tuple)):
            raise PromoError("bad_script", "headlines must be a list of lines")
        cleaned = [own_line(x) for x in lines if own_line(x)]
        if len(cleaned) > 10:
            raise PromoError("bad_script", "at most 10 headlines")
        if cleaned:
            out["headlines"] = cleaned
    for key in ("hook", "hook_sub", "value", "cta", "cta_text"):
        if custom.get(key) is not None:
            if not isinstance(custom[key], str):
                raise PromoError("bad_script", f"{key} must be text")
            text = own_line(custom[key], 80 if key in ("cta", "cta_text", "hook_sub") else MAX_OWN_LINE)
            if text:
                out[key] = text
    return out


def fragments(kit: dict[str, Any], taken: list[str]) -> list[str]:
    """Short clauses of the site's own value proposition (the headline is the hook already): more lines for a style with quick cuts when the page
    has few phrases. The whole proposition still closes the video as its own shot."""
    seen = {p.lower() for p in taken} | {str(kit.get("headline") or "").lower()}
    out: list[str] = []
    for piece in re.split(r"[.;:!?\u2014\u2013]+|,\s+| and ", str(kit.get("value_prop") or "")):
        text = clean_text(piece.strip(" -"), 80)
        words = len(text.split())
        if 2 <= words <= 11 and text.lower() not in seen and text != kit.get("value_prop"):
            seen.add(text.lower())
            out.append(text[:1].upper() + text[1:])
    return out[:4]


def plan_script(kit: dict[str, Any], length_s: float, vibe: str = "keynote", language: str = "auto",
                chat: Optional[Callable[..., str]] = None, custom: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The shots of the promo. Shots are all the same length and that length is a whole number of beats, so the tempo of the pulse
    and the cuts agree. Every shot's text is timed so that it is complete MIN_HOLD_S before the cut: the reveal is sped up when needed and,
    when even that is not enough, there are fewer and longer shots."""
    style = VIBES[vibe]
    lang = resolve_language(kit, language)
    own = check_custom(custom)
    n = max(4, min(12, round(length_s / style["shot_s"])))
    if own.get("headlines"):
        n = max(4, min(12, 4 + len(own["headlines"])))          # every own line gets its shot (fewer only if the length leaves no room to read them)
    else:
        phrases = [p for p in kit.get("phrases") or [] if p]
        if style.get("fragments"):
            phrases += fragments(kit, phrases)
            kit = {**kit, "phrases": phrases}
        n = max(4, min(n, 4 + len(phrases)))
    n_max = n
    n_points = n - 4
    texts = template_texts(kit, n_points, lang)
    written_by = "template"
    if chat is not None:
        try:
            got = _model_texts(chat, kit, n_points, vibe, lang)
            for key in ("hook", "points", "value"):
                if key in got and not (key == "points" and own.get("headlines")):
                    texts[key] = got[key]
            if "cta" in got:
                texts["cta"]["sub"] = got["cta"]
            written_by = "model" if got else "template"
        except Exception:  # noqa: BLE001 - no model, a stalled one, bad JSON: the template stands
            written_by = "template"
    if own.get("headlines"):
        texts["points"] = list(own["headlines"])
    if own.get("hook"):
        texts["hook"] = {"text": own["hook"], "sub": own.get("hook_sub", texts["hook"]["sub"])}
    elif own.get("hook_sub"):
        texts["hook"]["sub"] = own["hook_sub"]
    if own.get("value"):
        texts["value"] = own["value"]
    if own.get("cta"):
        texts["cta"]["sub"] = own["cta"]
    if own.get("cta_text"):
        texts["cta"]["text"] = own["cta_text"]
    if own:
        written_by = "custom" if written_by == "template" else written_by + "+custom"
    texts["points"] = (texts["points"] + [kit["site_name"]] * 12)
    logo = kit.get("logo") or {}
    logo_bg = "dark" if (logo.get("luminance") or 0) > 0.5 else "light"
    cycle = style.get("cycle") or ["light", "secondary", "dark", "accent", "light", "primary"]
    result: dict[str, Any] = {}
    while True:
        n_points = n - 4
        dur = length_s / n
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
                text, sub, bg = texts["value"], "", style.get("value_bg", "primary")
            elif kind == "logo":
                text, sub, bg = texts["logo"]["text"], texts["logo"]["sub"], logo_bg
            else:
                text, sub = texts["cta"]["text"], texts["cta"]["sub"]
                bg = style.get("cta_bg") or ("primary" if kinds[i - 1] != "value" else "dark")
            shots.append({"key": f"s{i + 1}", "kind": kind, "text": text, "sub": sub, "start": round(i * dur, 4), "dur": round(dur, 4),
                          "bg_role": bg, "fit": 1.0, "layout": "center" if kind in ("logo", "cta") else "left", "drift": 1.0,
                          "index": f"{pi:02d}" if kind == "point" else "", "lead_in": i == 0})
        feasible = all([fit_hold(s, vibe) for s in shots])
        result = {"shots": shots, "n": n}
        if feasible or n <= 4:
            break
        n -= 1                                   # not enough room to read everything: fewer, longer shots
    shots = result["shots"]
    dur = length_s / len(shots)
    beats = choose_beats(dur, style["bpm"])
    return {"vibe": vibe, "language": lang, "length_s": round(length_s, 3), "shots": shots, "beats_per_shot": beats,
            "bpm": round(beats * 60.0 / dur, 2), "written_by": written_by, "wipe_s": style["wipe_s"], "deco": True, "min_hold_s": MIN_HOLD_S,
            "shots_planned": n_max, "own_headlines_dropped": max(0, len(own.get("headlines", [])) - (len(shots) - 4)) if own.get("headlines") else 0}


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
  const WIPE = DATA.wipe_s, TOTAL = DATA.length_s, LOOK = DATA.look;
  function wipePath(i, p) {
    const r = (1 - p) * 100, side = portrait ? (i % 2 ? 'v' : 'v2') : ['h', 'v', 'h2'][i % 3];
    if (side === 'h') return `inset(0 ${r}% 0 0)`;
    if (side === 'h2') return `inset(0 0 0 ${r}%)`;
    if (side === 'v') return `inset(${r}% 0 0 0)`;
    return `inset(0 0 ${r}% 0)`;
  }
  // one word of a line: p is its progress 0..1 (the checks read it back from window.measure)
  function setWord(w, p, k) {
    w._p = p;
    if (LOOK.reveal === 'fade') { w.style.transform = `translateY(${(1 - p) * 46}%)`; w.style.opacity = p; }
    else if (LOOK.reveal === 'pop') { w.style.transform = `translate(${(1 - p) * (k % 2 ? -34 : 34)}%, ${(1 - p) * 118}%) skewY(${(1 - p) * -7}deg)`; }
    else { w.style.transform = `translateY(${(1 - p) * 118}%) skewY(${(1 - p) * 4}deg)`; }
  }
  window.seek = function (t) {
    const last = shots.length - 1;
    const phase = (t * DATA.bpm / 60) % 1, pulse = LOOK.pulse * Math.exp(-phase * 7);
    for (const s of shots) {
      const d = s.d, tm = d.tm, end = d.start + d.dur;
      const show = (s.i === 0 ? t < end + WIPE : (t >= d.start && t < end + WIPE)) || (s.i === last && t >= d.start);
      s.el.style.display = show ? 'block' : 'none';
      if (!show) continue;
      s.el.style.zIndex = s.i + 1;
      const wp = s.i === 0 ? 1 : E.inout(prog(t, d.start, WIPE));
      // the whole shot is revealed by the transition (not only its background), so its text never shows on the shot it is covering
      if (LOOK.transition === 'fade') { s.el.style.clipPath = 'none'; s.el.style.opacity = wp >= 1 ? 1 : wp; }
      else { s.el.style.opacity = 1; s.el.style.clipPath = wp >= 1 ? 'none' : wipePath(s.i, wp); }
      const lt = t - d.start - tm.lead;
      const leave = s.i === last ? 0 : E.inout(prog(t, end - 0.35, 0.35 + WIPE));
      const drift = (t - d.start) / d.dur;
      const slam = 1 + LOOK.slam * (1 - E.out(prog(lt, 0, 0.3)));
      s.copy.style.transform = `translate(0, ${(-leave * 70 - drift * 14 * d.drift) * U}px) scale(${slam})`;
      s.copy.style.opacity = (LOOK.transition === 'fade' && s.i !== last) ? 1 - E.inout(prog(t, end - 0.45, 0.5)) : 1;   // in a dissolve the old text goes before the new arrives
      s.words.forEach((w, k) => setWord(w, E.expo(prog(lt, tm.t0 + tm.ts * k, tm.tw)), k));
      s.subs.forEach((w, k) => setWord(w, E.expo(prog(lt, tm.s0 + tm.sst * k, tm.sw)), k));
      if (s.rule) s.rule.style.transform = `scaleX(${E.out(prog(lt, 0.25 * tm.tw / 0.75, 0.8 * tm.tw / 0.75))})`;
      if (s.idx) s.idx.style.transform = `translate(${(1 - E.out(prog(lt, 0, 0.6))) * -60 * U}px, 0)`;
      if (s.logo) { const p = E.back(prog(lt, 0.18, 0.7)); s.logo.style.transform = `scale(${0.55 + 0.45 * p})`; s.logo.style.clipPath = `inset(0 ${(1 - E.out(prog(lt, 0.18, 0.5))) * 100}% 0 0)`; }
      if (s.ring) { const p = E.out(prog(lt, 0.4, 1.0)); s.ring.style.transform = `translate(-50%, -50%) scale(${0.3 + 0.85 * p})`; s.ring.style.borderWidth = ((1 - p) * 20 + 3) * U + 'px'; }
      if (s.deco) {
        if (LOOK.deco === 'hair') s.deco.style.transform = `scaleX(${0.08 + 0.92 * E.out(prog(lt, 0, 1.6))})`;
        else if (LOOK.deco === 'bars') s.deco.style.transform = `translate(${(-drift * 520 * d.drift) * U}px, 0) scale(${1 + pulse})`;
        else s.deco.style.transform = `translate(${(-drift * 120 * d.drift) * U}px, ${(drift * 90 * d.drift) * U}px) scale(${1 + 0.18 * drift * d.drift + pulse})`;
      }
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
        sw: el.scrollWidth, cw: el.clientWidth, fs: parseFloat(cs.fontSize), color: cs.color, bg: sh.dataset.bg,
        rev: (() => { const ws = [...el.querySelectorAll('.wi')]; return ws.length ? 1 - Math.min(...ws.map(w => w._p === undefined ? 1 : w._p)) : 0; })() });
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
    look = look_of(vibe)
    for shot in script["shots"]:
        colours = shot_colors(palette, shot.get("bg_role", "dark"), shot.get("ink_force"))
        size = font_size(shot, portrait, vibe)
        sub_size = round(SUB_SIZE[1 if portrait else 0] * VIBES[vibe]["scale"] * min(1.0, float(shot.get("fit", 1.0)) + 0.1), 2)
        centered = shot["layout"] == "center"
        key = shot["key"]
        parts = []
        if shot["kind"] == "point" and shot.get("index"):
            parts.append(f'<div class="idx" data-fit="{key}-idx" data-role="idx" style="font-size:calc(var(--u)*{round(size * look["idx"], 1)})">{htmllib.escape(shot["index"])}</div>')
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
                           "drift": float(shot.get("drift", 1.0)), "tm": shot.get("tm") or retime(shot, vibe)})
    data = {"shots": data_shots, "length_s": script["length_s"], "wipe_s": float(script.get("wipe_s", 0.5)), "fps": fps, "bpm": float(script.get("bpm", 96)),
            "look": {k: look[k] for k in ("reveal", "transition", "deco", "pulse", "slam")},
            "fonts": {"display_weight": int(families["display"].get("weight", 700)), "body_weight": int(families["body"].get("weight", 400))}}
    width_copy = f"{look['width'][1 if portrait else 0]:g}%"
    copy_left = f"{look['left']:g}%"
    upper = "text-transform:uppercase;" if look["upper"] else ""
    deco_css = {
        "ring": f""".deco{{position:absolute;right:calc(var(--u)*-230);{"bottom" if portrait else "top"}:calc(var(--u)*-230);width:calc(var(--u)*560);height:calc(var(--u)*560);
  border:calc(var(--u)*16) solid var(--mark);border-radius:50%;box-sizing:border-box}}""",
        "hair": f""".deco{{position:absolute;left:{copy_left};right:{copy_left};top:{"13%" if portrait else "12%"};height:calc(var(--u)*3);background:var(--mark);
  transform-origin:left center;opacity:.9}}""",
        "bars": f""".deco{{position:absolute;left:-10%;width:240%;bottom:{"22%" if portrait else "14%"};height:calc(var(--u)*{26 if portrait else 34});background:var(--mark);
  transform:skewY(-6deg);transform-origin:left center}}
.deco::after{{content:"";position:absolute;left:12%;width:60%;top:calc(var(--u)*{64 if portrait else 78});height:calc(var(--u)*{12 if portrait else 16});background:var(--mark)}}""",
    }[look["deco"]]
    css = f"""
{chr(10).join(faces)}
html,body{{margin:0;padding:0;width:100vw;height:100vh;overflow:hidden;background:#000}}
#stage{{position:absolute;inset:0;overflow:hidden;font-family:"PromoBody","PromoDisplay",sans-serif;font-synthesis:none}}
.shot{{position:absolute;inset:0;display:none;overflow:hidden;color:var(--ink)}}
.bg{{position:absolute;inset:0;background:var(--bg)}}
.copy{{position:absolute;left:{copy_left};top:0;height:100%;width:{width_copy};display:flex;flex-direction:column;justify-content:center;
  gap:calc(var(--u)*26);will-change:transform}}
.shot.c .copy{{left:8%;width:84%;align-items:center;text-align:center}}
.t{{font-family:"PromoDisplay",sans-serif;font-weight:{int(families["display"].get("weight", 700))};line-height:{look["leading"]};letter-spacing:-0.012em;color:var(--ink);{upper}
  overflow-wrap:break-word;hyphens:manual}}
.sub{{font-family:"PromoBody",sans-serif;font-weight:{int(families["body"].get("weight", 400))};line-height:1.25;color:var(--ink);overflow-wrap:break-word}}
.sub.pre{{margin-bottom:calc(var(--u)*-6)}}
.idx{{font-family:"PromoDisplay",sans-serif;font-weight:{int(families["display"].get("weight", 700))};color:var(--mark);letter-spacing:.08em}}
.w{{display:inline-block;overflow:{"visible" if look["reveal"] == "fade" else "hidden"};vertical-align:top;padding-bottom:.16em;margin-bottom:-.16em}}
.wi{{display:inline-block;will-change:transform}}
.rule{{height:calc(var(--u)*10);width:calc(var(--u)*220);background:var(--mark);transform-origin:left center;border-radius:calc(var(--u)*5)}}
.shot.c .rule{{transform-origin:center}}
.logowrap{{position:relative;display:flex;justify-content:center;align-items:center;width:100%;height:calc(var(--u)*{300 if not portrait else 260})}}
.logo{{max-width:{56 if not portrait else 78}%;max-height:100%;object-fit:contain;display:block;will-change:transform}}
.logo.mono{{display:flex;align-items:center;justify-content:center;width:calc(var(--u)*240);height:calc(var(--u)*240);border-radius:50%;background:var(--mark);
  color:var(--bg);font-family:"PromoDisplay",sans-serif;font-weight:{int(families["display"].get("weight", 700))};font-size:calc(var(--u)*150)}}
.ring{{position:absolute;left:50%;top:50%;width:calc(var(--u)*260);height:calc(var(--u)*260);border-radius:50%;border:solid var(--mark);
  box-sizing:border-box;transform:translate(-50%,-50%) scale(.3)}}
{deco_css}
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
        first = shot["start"] + float((shot.get("tm") or {}).get("settle", SETTLE_S)) + 0.02
        last = shot["start"] + shot["dur"] - (0.05 if last_shot else LEAVE_S)
        out.append((shot, first, last))
    return out
