"""Treatment-first productions: before anything renders, a director's treatment - logline, three candidate structures
and the pick, a benchmark (what to learn from, what to avoid), a shot list (framing, camera, duration, why) and a
beat sheet / cue map - drafted by the family's language model (or by rules when none answers), edited by the
person, and approved ("Approve and render"). A production that has a treatment runs a `treatment` stage first that
pauses until it is approved. `storyboard_sheet` tiles 6-9 key stills (the shots' own stills when they exist, else
placeholder frames drawn from the shot text) into one contact sheet with Pillow.

The method (treatment first, structures, benchmark, shot list with reasons, cue map) follows DIRECTOR.md of
lemomo-ai/lemo-opuscar (MIT). Pure logic: no FastAPI.
"""

from __future__ import annotations

import json
import math
import re
import textwrap
from typing import Any, Callable, Optional

from PIL import Image, ImageDraw

from . import design
from .engine import EngineError
from .util import now_iso

Chat = Callable[[list[dict[str, Any]], int, float], str]
FRAMINGS = ("extreme wide", "wide", "full", "medium", "medium close-up", "close-up", "extreme close-up", "insert", "over the shoulder")
SHOT_LIMIT = 40
SHEET_MIN, SHEET_MAX = 6, 9


class TreatmentError(EngineError):
    pass


def _clip(text: Any, n: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def duration_of(state: dict[str, Any]) -> float:
    spec = state.get("spec") or {}
    song = spec.get("song") or {}
    for value in (spec.get("duration_s"), (spec.get("options") or {}).get("duration_s"), song.get("duration")):
        try:
            if value and float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 60.0


def context(state: dict[str, Any]) -> str:
    """What the director knows: title, brief, lead, world look, sound and the planned shots."""
    spec = state.get("spec") or {}
    lead = spec.get("lead") or {}
    song = spec.get("song") or {}
    lines = [f"Title: {_clip(spec.get('title') or state.get('name'), 120)}",
             f"Kind: {state.get('kind') or 'music_video'}, about {duration_of(state):.0f} s"]
    for label, value in (("Brief", spec.get("brief") or spec.get("concept") or spec.get("topic")),
                         ("Lead", " - ".join(x for x in (_clip(lead.get("name"), 60), _clip(lead.get("look"), 300)) if x)),
                         ("World look", _clip((spec.get("world") or {}).get("look"), 300)),
                         ("Song", _clip(song.get("tags"), 200)), ("Lyrics", _clip(song.get("lyrics"), 900)),
                         ("Script", _clip(spec.get("script"), 900))):
        if value:
            lines.append(f"{label}: {value}")
    shots = spec.get("shots") or []
    if shots:
        lines.append("Planned shots:")
        for shot in shots[:SHOT_LIMIT]:
            tags = [shot.get("section") or "", "lead" if shot.get("lead") else "", "sings" if shot.get("sing") else "",
                    shot.get("motion") or ""]
            lines.append(f"- {shot.get('key')}: {_clip(shot.get('prompt') or shot.get('grammar'), 220)}"
                         + (f" [{', '.join(t for t in tags if t)}]" if any(tags) else ""))
    return "\n".join(lines)


SYSTEM = ("You are a film director writing a treatment before anything is shot. Work in this order: a one-sentence "
          "logline; three different candidate structures for the piece and the one you pick with the reason; a benchmark "
          "(what to learn from strong work of this kind, what to avoid); a shot list where every shot has framing, camera "
          "move, duration in seconds and why it is there; a beat sheet / cue map of moments in seconds. Keep the planned "
          "shot keys when there are planned shots. Answer with one JSON object and nothing else.")
SHAPE = ('{"logline": str, "structures": [{"name": str, "summary": str, "beats": [str]}] (exactly 3), '
         '"pick": {"index": 0-2, "why": str}, "benchmark": {"learn": [str], "avoid": [str]}, '
         '"shot_list": [{"key": str, "framing": str, "camera": str, "duration_s": number, "why": str}], '
         '"beat_sheet": [{"t_s": number, "section": str, "cue": str}]}')


def messages(state: dict[str, Any], language: str = "en", notes: Optional[str] = None) -> list[dict[str, Any]]:
    user = (f"{context(state)}\n\n" + (f"Notes from the person: {_clip(notes, 800)}\n\n" if notes else "")
            + f"Write the treatment in language '{language}'. JSON shape: {SHAPE}")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _json_object(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?|```$", "", str(text or "").strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise TreatmentError("bad_treatment", "the model's answer had no JSON object")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        raise TreatmentError("bad_treatment", f"the model's JSON did not parse: {exc}") from None
    if not isinstance(data, dict):
        raise TreatmentError("bad_treatment", "the treatment must be an object")
    return data


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, out)) if math.isfinite(out) else default


def _strings(value: Any, limit: int, n: int = 280) -> list[str]:
    items = value if isinstance(value, list) else ([value] if value else [])
    return [_clip(v, n) for v in items if str(v or "").strip()][:limit]


def normalise(data: Any, state: dict[str, Any]) -> dict[str, Any]:
    """A treatment from the model or from the person's edits, with every field in its shape and bounds."""
    if not isinstance(data, dict):
        raise TreatmentError("bad_treatment", "the treatment must be an object")
    total = duration_of(state)
    structures = []
    for s in (data.get("structures") or [])[:3]:
        if isinstance(s, dict) and (s.get("name") or s.get("summary")):
            structures.append({"name": _clip(s.get("name") or f"Structure {len(structures) + 1}", 80),
                               "summary": _clip(s.get("summary"), 600), "beats": _strings(s.get("beats"), 12, 200)})
    pick = data.get("pick") if isinstance(data.get("pick"), dict) else {}
    index = int(_num(pick.get("index"), 0, 0, max(0, len(structures) - 1)))
    bench = data.get("benchmark") if isinstance(data.get("benchmark"), dict) else {}
    shots = []
    for i, s in enumerate((data.get("shot_list") or [])[:SHOT_LIMIT]):
        if not isinstance(s, dict):
            continue
        key = re.sub(r"[^A-Za-z0-9_-]", "", str(s.get("key") or i + 1))[:24] or str(i + 1)
        shots.append({"key": key, "framing": _clip(s.get("framing") or "medium", 60), "camera": _clip(s.get("camera") or "static", 80),
                      "duration_s": round(_num(s.get("duration_s"), 3.0, 0.3, 120), 2), "why": _clip(s.get("why"), 300),
                      **({"text": _clip(s.get("text"), 300)} if s.get("text") else {})})
    beats = []
    for b in (data.get("beat_sheet") or [])[:60]:
        if isinstance(b, dict) and str(b.get("cue") or "").strip():
            beats.append({"t_s": round(_num(b.get("t_s"), 0, 0, max(total, 1) * 2), 2), "section": _clip(b.get("section"), 40),
                          "cue": _clip(b.get("cue"), 200)})
    beats.sort(key=lambda b: b["t_s"])
    return {"logline": _clip(data.get("logline"), 300), "structures": structures,
            "pick": {"index": index, "why": _clip(pick.get("why"), 400)},
            "benchmark": {"learn": _strings(bench.get("learn"), 8), "avoid": _strings(bench.get("avoid"), 8)},
            "shot_list": shots, "beat_sheet": beats}


_FRAMING_WORDS = (("extreme close", "extreme close-up"), ("close-up", "close-up"), ("closeup", "close-up"),
                  ("close up", "close-up"), ("portrait", "medium close-up"), ("medium", "medium"), ("full body", "full"),
                  ("full-length", "full"), ("establishing", "extreme wide"), ("wide", "wide"), ("landscape", "wide"))


def rules_draft(state: dict[str, Any]) -> dict[str, Any]:
    """A treatment without a language model: the planned shots become the shot list (framing read from the prompt,
    camera from the motion), sections become the beat sheet, and three stock structures frame the pick."""
    spec = state.get("spec") or {}
    title = _clip(spec.get("title") or state.get("name") or "the piece", 80)
    lead = _clip((spec.get("lead") or {}).get("name"), 60)
    brief = _clip(spec.get("brief") or spec.get("concept") or spec.get("topic") or (spec.get("world") or {}).get("look"), 160)
    total = duration_of(state)
    planned = (spec.get("shots") or [])[:SHOT_LIMIT] or [{"key": str(i + 1), "prompt": p} for i, p in enumerate(
        ("establishing image of the world", "the lead enters", "detail that tells the story", "the turn",
         "the peak moment, closest on the lead", "closing image"))]
    each = total / max(1, len(planned))
    shots, beats, t, last_section = [], [], 0.0, None
    for i, shot in enumerate(planned):
        prompt = str(shot.get("prompt") or shot.get("grammar") or "").lower()
        framing = next((f for word, f in _FRAMING_WORDS if word in prompt), ("wide", "medium", "close-up")[i % 3])
        span = (shot.get("start_s"), shot.get("end_s"))
        dur = float(span[1]) - float(span[0]) if None not in span else each
        camera = "locked off" if shot.get("motion") == "still" else ("push in" if framing.endswith("close-up") else "slow dolly")
        section = shot.get("section") or ""
        why = f"{section + ': ' if section else ''}{_clip(shot.get('prompt') or shot.get('grammar'), 140)}"
        shots.append({"key": str(shot.get("key") or i + 1), "framing": framing, "camera": camera, "duration_s": round(dur, 2), "why": why})
        if section and section != last_section or i == 0:
            beats.append({"t_s": round(t, 2), "section": section or "open", "cue": f"shot {shots[-1]['key']}: {framing}, {camera}"})
            last_section = section
        t += dur
    beats.append({"t_s": round(min(t, total), 2), "section": "end", "cue": "last image holds, music resolves"})
    who = lead or "the lead"
    return normalise({
        "logline": f"{title}: {who}" + (f" - {brief}" if brief else "") + ".",
        "structures": [
            {"name": "Linear journey", "summary": f"{who} moves from the opening situation through an escalation to a release.",
             "beats": ["set the world", "first change", "escalation", "peak", "release"]},
            {"name": "Bookends", "summary": "Open and close on the same image so the change in between reads at a glance.",
             "beats": ["opening image", "departure", "middle turns", "return to the opening image, changed"]},
            {"name": "Contrast montage", "summary": "Two worlds or moods alternate and converge at the peak.",
             "beats": ["world A", "world B", "faster alternation", "both in one frame", "aftermath"]},
        ],
        "pick": {"index": 0, "why": "It follows the planned shot order, so nothing has to be re-planned."},
        "benchmark": {"learn": ["Open on the strongest image within the first two seconds",
                                "Change framing at every cut and cut on the beat at section changes",
                                "Save the closest framing of the lead for the emotional peak"],
                      "avoid": ["Two shots in a row with the same framing", "Every shot the same length (slideshow pacing)",
                                "Text over busy backgrounds"]},
        "shot_list": shots, "beat_sheet": beats}, state)


def draft(chat: Optional[Chat], state: dict[str, Any], language: str = "en", notes: Optional[str] = None) -> dict[str, Any]:
    """The model's treatment, or the rule-based one (with the reason) when no model answers or its answer is unusable."""
    reason = "no language model is configured"
    if chat is not None:
        try:
            data = normalise(_json_object(chat(messages(state, language, notes), 3500, 0.6)), state)
            if data["shot_list"] and data["structures"]:
                return {**data, "source": "model", "status": "draft", "drafted_at": now_iso(), "language": language}
            reason = "the model's treatment had no shots or structures"
        except EngineError as exc:
            reason = exc.message
        except Exception as exc:  # noqa: BLE001 - an unreachable model falls back to the rule draft, with the reason
            reason = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    return {**rules_draft(state), "source": "rules", "status": "draft", "drafted_at": now_iso(), "language": language,
            "note": f"Drafted by rules: {_clip(reason, 200)}"}


def set_treatment(state: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Save the person's edits. An edit after approval makes it a draft again (and the stage pending)."""
    old = state.get("treatment") or {}
    new = normalise(data, state)
    new.update({k: old[k] for k in ("source", "drafted_at", "language", "note", "storyboard") if k in old})
    new.update({"status": "draft", "edited_at": now_iso()})
    if old.get("status") == "approved":
        (state.get("done") or {}).pop("treatment", None)
        (state.get("review") or {}).pop("treatment_approved", None)
    state["treatment"] = new
    return new


def approve(state: dict[str, Any]) -> dict[str, Any]:
    t = state.get("treatment")
    if not t:
        raise TreatmentError("no_treatment", "draft a treatment first (production_treatment_draft)")
    if not t.get("shot_list"):
        raise TreatmentError("bad_treatment", "the treatment needs at least one shot before it can be approved")
    t["status"], t["approved_at"] = "approved", now_iso()
    state.setdefault("done", {})["treatment"] = {"approved_at": t["approved_at"], "shots": len(t["shot_list"])}
    state.setdefault("review", {})["treatment_approved"] = True
    return t


def key_shots(t: dict[str, Any], limit: int = SHEET_MAX) -> list[dict[str, Any]]:
    """6-9 shots for the storyboard: all of them when there are few, else evenly spread from first to last."""
    shots = t.get("shot_list") or []
    if len(shots) <= limit:
        return list(shots)
    step = (len(shots) - 1) / (limit - 1)
    return [shots[round(i * step)] for i in range(limit)]


def placeholder_frame(shot: dict[str, Any], size: tuple[int, int]) -> Image.Image:
    """A frame drawn from the shot's text: number, framing, camera and the reason, on a dark card."""
    w, h = size
    im = Image.new("RGB", size, (24, 22, 30))
    draw = ImageDraw.Draw(im)
    for y in range(h):  # a soft vertical gradient so the cards read as frames, not blank boxes
        shade = int(18 + 22 * y / max(1, h))
        draw.line([(0, y), (w, y)], fill=(shade + 6, shade, shade + 14))
    big = design.get_font("space-grotesk", max(28, h // 4))
    small = design.get_font("space-grotesk", max(13, h // 16))
    draw.text((w * 0.06, h * 0.06), f"#{shot.get('key')}", fill=(236, 200, 120), font=big)
    draw.text((w * 0.06, h * 0.42), f"{shot.get('framing')} · {shot.get('camera')}", fill=(230, 226, 240), font=small)
    y = h * 0.55
    for line in textwrap.wrap(str(shot.get("why") or ""), width=max(18, w // max(7, h // 22)))[:4]:
        draw.text((w * 0.06, y), line, fill=(170, 165, 185), font=small)
        y += small.size * 1.25 if hasattr(small, "size") else 18
    draw.rectangle([0, 0, w - 1, h - 1], outline=(70, 64, 86))
    return im


def sheet_image(t: dict[str, Any], stills: dict[str, Optional[Image.Image]], title: str,
                cell: tuple[int, int] = (480, 270)) -> Image.Image:
    """The storyboard: key shots in a 3-column grid, each a still or a placeholder, labelled under the frame."""
    shots = key_shots(t)
    cols = 3 if len(shots) > 4 else max(1, len(shots))
    rows = max(1, math.ceil(len(shots) / cols))
    cw, ch = cell
    label_h, pad, head = 46, 12, 54
    sheet = Image.new("RGB", (cols * (cw + pad) + pad, head + rows * (ch + label_h + pad) + pad), (14, 13, 18))
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, 14), _clip(f"{title} - storyboard", 90), fill=(240, 236, 248), font=design.get_font("space-grotesk", 26))
    label = design.get_font("space-grotesk", 15)
    for i, shot in enumerate(shots):
        x = pad + (i % cols) * (cw + pad)
        y = head + (i // cols) * (ch + label_h + pad)
        still = stills.get(str(shot.get("key")))
        if still is not None:
            frame = still.convert("RGB")
            scale = max(cw / frame.width, ch / frame.height)
            frame = frame.resize((max(cw, round(frame.width * scale)), max(ch, round(frame.height * scale))))
            left, top = (frame.width - cw) // 2, (frame.height - ch) // 2
            sheet.paste(frame.crop((left, top, left + cw, top + ch)), (x, y))
        else:
            sheet.paste(placeholder_frame(shot, cell), (x, y))
        draw.text((x, y + ch + 6), _clip(f"#{shot.get('key')} · {shot.get('framing')} · {shot.get('camera')} · "
                                          f"{shot.get('duration_s')} s", 60), fill=(225, 220, 235), font=label)
        draw.text((x, y + ch + 25), _clip(shot.get("why"), 62), fill=(150, 145, 165), font=label)
    return sheet
