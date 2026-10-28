"""A working starting page for an animation built from a style card and its treatment.

The page is plain HTML (text in the DOM, ``window.seek(t)`` a pure function of time, the cues embedded in
``lumiere-cues``). It is a scaffold, not the finished film: the agent edits the source from here. It does carry what the
checks need: every shot's caption is real DOM text, and every cue is a single sharp change of the picture on its frame."""
from __future__ import annotations

import html
import json
import re
from typing import Optional

from .. import style_cards
from . import motion_direction as direction
from .errors import LumiereError, NotFound

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")


def _rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luma(value: str) -> float:
    r, g, b = _rgb(value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _chroma(value: str) -> int:
    rgb = _rgb(value)
    return max(rgb) - min(rgb)


def palette_of(card: dict) -> dict:
    """Ground, ink and accent for the scaffold, taken from the card's swatches (the card decides light or dark)."""
    raw = list((card.get("meta") or {}).get("swatches") or []) + [c.get("hex") if isinstance(c, dict) else c for c in card.get("palette") or []]
    swatches = []
    for item in raw:
        if isinstance(item, str) and _HEX.match(item.strip()) and item.strip().upper() not in swatches:
            swatches.append(item.strip().upper())
    if len(swatches) < 2:
        swatches = ["#15171A", "#F2EFE6", "#E8553A"]
    # A card lists its ground first ("neutral ground: ...", "dark void: ..."); the ink is the swatch furthest from it in lightness.
    ground = swatches[0]
    ink = max(swatches[1:], key=lambda c: abs(_luma(c) - _luma(ground)))
    if abs(_luma(ground) - _luma(ink)) < 90:
        ink = "#15171A" if _luma(ground) > 128 else "#F2EFE6"
    rest = [s for s in swatches if s not in (ground, ink)] or ["#E8553A"]
    accent = max(rest, key=_chroma)
    return {"ground": ground, "ink": ink, "accent": accent, "light": _luma(ground) > 128}


def _font(card: dict) -> str:
    text = str((card.get("sections") or {}).get("type", {}).get("text", "")).lower()
    if "mono" in text and "serif" not in text:
        return "'Cascadia Mono', Consolas, 'Courier New', monospace"
    if re.search(r"serif|editorial|georgia|garamond", text):
        return "Georgia, 'Times New Roman', serif"
    return "'Segoe UI', system-ui, Arial, sans-serif"


def caption_fits(length_s: float, fps: float) -> int:
    """How many letters a caption may have to stay on screen long enough in a shot of this length (the readcheck rule)."""
    return max(0, int((length_s - 0.3 - 1.0 / fps - direction.READ_PAD_S) * direction.READ_LETTERS_PER_S))


def caption_of(shot: dict, room: int = 60) -> str:
    explicit = re.sub(r"\s+", " ", (shot.get("text") or "").strip())
    if explicit:
        return explicit  # the author's own words are never cut; motion_readcheck says if they stay too short
    text = re.sub(r"\s+", " ", (shot.get("what") or "").strip())
    room = min(60, room)
    if len(text) > room:
        text = text[:room].rsplit(" ", 1)[0].rstrip(",;:- ") if " " in text[:room] else text[:room]
        if len(text) < 3:
            text = ""
    return text


_PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><title>{title}</title>
<!-- Style card: {card_name}. {attribution} -->
<style>
html,body{{margin:0;background:{ground};}}
#stage{{position:relative;width:{width}px;height:{height}px;overflow:hidden;background:{ground};color:{ink};font-family:{font};}}
#flash{{position:absolute;inset:0;background:{accent};opacity:0;}}
#rule{{position:absolute;left:8%;top:{rule_top}px;width:84%;height:{rule_h}px;background:{accent};transform-origin:left center;}}
#title{{position:absolute;left:8%;right:8%;top:{title_top}px;font-size:{title_size}px;line-height:1.12;font-weight:700;letter-spacing:-.01em;}}
#bar{{position:absolute;left:8%;bottom:{bar_bottom}px;width:84%;height:{bar_h}px;background:{ink};opacity:.18;}}
#fill{{position:absolute;left:8%;bottom:{bar_bottom}px;width:84%;height:{bar_h}px;background:{ink};transform-origin:left center;}}
</style></head><body>
<div id="stage"><div id="flash"></div><div id="rule"></div><div id="title"></div><div id="bar"></div><div id="fill"></div></div>
<script id="lumiere-cues" type="application/json">{cues}</script>
<script>
const DURATION={duration}, SHOTS={shots}, CUES=JSON.parse(document.getElementById('lumiere-cues').textContent);
const $=id=>document.getElementById(id), clamp=x=>Math.min(1,Math.max(0,x)), out=x=>1-Math.pow(1-clamp(x),3);
window.seek=t=>{{
  let i=0; for(let k=0;k<SHOTS.length;k++) if(t>=SHOTS[k].t-1e-6) i=k;
  const next=i+1<SHOTS.length?SHOTS[i+1].t:DURATION, local=t-SHOTS[i].t;
  const fade=i+1<SHOTS.length?clamp((next-t)/.2):1;
  const title=$('title'); if(title.textContent!==SHOTS[i].text) title.textContent=SHOTS[i].text;
  title.style.opacity=(out(local/.3)*fade).toFixed(4);
  title.style.transform='translateY('+((1-out(local/.3))*{rise}).toFixed(2)+'px)';
  let kick=0; for(const c of CUES){{const d=t-c.t; if(d>=-1e-6) kick=Math.max(kick,Math.exp(-Math.max(0,d)*8));}}
  $('flash').style.opacity=(.22*kick).toFixed(4);
  $('rule').style.transform='scaleX('+(.12+.88*kick).toFixed(4)+')';
  $('fill').style.transform='scaleX('+clamp(t/DURATION).toFixed(4)+')';
}};
seek(0);
</script></body></html>
"""


def build_html(card: dict, treatment: dict, cues: list[dict], *, name: str, width: int, height: int, duration: float, fps: float = 30.0) -> str:
    pal = palette_of(card)
    starts = [s["t"] for s in treatment["shots"]] + [duration]
    shots = [{"t": s["t"], "text": caption_of(s, caption_fits(starts[i + 1] - s["t"], fps))} for i, s in enumerate(treatment["shots"])]
    safe = lambda value: json.dumps(value, ensure_ascii=False).replace("</", "<\\/")
    return _PAGE.format(
        lang="es" if card.get("lang") == "es" else "en", title=html.escape(name), card_name=html.escape(str(card.get("name", ""))).replace("--", "- -"),
        attribution=html.escape(str(card.get("attribution", ""))).replace("--", "- -"), width=width, height=height, ground=pal["ground"], ink=pal["ink"],
        accent=pal["accent"], font=_font(card), rule_top=round(height * .16), rule_h=max(4, round(height * .012)), title_top=round(height * .26),
        title_size=max(18, round(height * .075)), bar_bottom=round(height * .1), bar_h=max(3, round(height * .006)), rise=round(height * .04),
        duration=repr(float(duration)), shots=safe(shots), cues=safe([{"id": c["id"], "t": c["t"]} for c in cues]))


def create_from_style(store, *, card_ref: str, name: Optional[str], width: int, height: int, fps: float, duration: float,
                      treatment: Optional[dict], draft: bool, caption: Optional[str], project_id: Optional[str]) -> dict:
    card = style_cards.find(card_ref)
    if card is None:
        raise NotFound(f"No style card {card_ref!r}: search with motion_styles.")
    if treatment is None:
        if not draft:
            raise LumiereError("Write the treatment before the page: three structures (and why one), the shots one by one with why each is there, "
                               "and the cue map. motion_treatment action=draft gives a starting point from the card; pass draft_treatment=true to accept it as is.",
                               code="treatment_required")
        treatment = direction.draft_treatment(card, duration, caption)
    treatment = {**treatment, "card": treatment.get("card") or f"{card['source']}/{card['slug']}"}
    verdict = direction.check_treatment(treatment, duration)
    if not verdict["ok"]:
        raise LumiereError("The treatment is not usable: " + " ".join(verdict["problems"]), code="treatment")
    parsed = direction.Treatment.model_validate(treatment)
    cues = direction._clean_cues([direction.Cue(t=i.t, visual=i.visual, sound=i.sound) for i in parsed.cue_map], duration, "launch")
    events = direction.sound_events_from_cues(cues)
    page = build_html(card, parsed.model_dump(exclude_none=True), cues, name=name or card["name"], width=width, height=height, duration=duration, fps=fps)
    manifest = store.save(html=page, name=name or f"{card['name']}", width=width, height=height, fps=fps, duration=duration,
                          project_id=project_id, sound={"kit": "launch", "events": events} if events else {})
    folder = store.folder(manifest["id"])
    store._write(folder / direction.TREATMENT_FILE, {"rev": 1, **parsed.model_dump(exclude_none=True)})
    store._write(folder / direction.CUES_FILE, {"rev": 1, "cues": cues})
    return {**manifest, "card": f"{card['source']}/{card['slug']}", "license": card.get("license"), "attribution": card.get("attribution"),
            "treatment_check": verdict, "cues": cues, "events": len(events), "draft_treatment": bool(parsed.draft),
            "next": ["motion_storyboard / html_motion_storyboard: look at it", "motion_cuecheck: every cue has its visual hit",
                     "motion_readcheck: every text stays long enough", "motion_export: render with sound"]}
