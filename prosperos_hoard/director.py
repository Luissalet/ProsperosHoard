"""Director: an idea becomes a directed short film, shot by shot.

A director film is a narrated production (shorts.py, `state["kind"] ==
"short"`) whose spec carries a `director` section. That section holds the
direction shared by every shot (genre, lens, light, film stock, look, grade,
era) and the shot list itself; each shot has its narration line, the still
it starts from (an English description), what moves in it (`action`), its
framing, a camera move from the motion presets and optional effect/mood
presets. The pipeline is the short's (script, narration with word-timed
subtitles, music, pictures, animatic, clips, mix, timeline, report), with
these differences:

- the script stage writes the shot list (the family's language model, or the
  shots given) and keeps it in `spec.director.shots`, where it stays editable;
- one shot per narration line, keyed by the shot's id, so editing one shot
  redoes only that shot;
- every still is generated (the project's image engine, Qwen-Image when it is
  installed), `takes` per shot in one batch, and every shot is animated with
  the chosen video engine (LTX 2.3, Wan 2.2 14B or 5B, or the best installed);
- `end_frames` makes each clip end on the next shot's still (first/last frame
  model), so the cut becomes a continuous move;
- `sfx` lays a synthesised whoosh on every cut under the voice.

The motion presets, direction axes and decade looks are adapted from
OpenField (MIT, see NOTICE); the names, the Spanish labels, the formats and
the pipeline are this studio's own.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from . import engine, promo_sound, soundtrack
from . import productions as prod
from . import shorts
from . import voice_pipelines as vp
from .ids import new_id
from .util import now_iso

# ------------------------------------------------------------------ catalog

CATEGORIES = {
    "camera": {"es": "Cámara", "en": "Camera"},
    "epic": {"es": "Cámara épica", "en": "Epic camera"},
    "vfx": {"es": "Efectos", "en": "Effects"},
    "mood": {"es": "Ambiente", "en": "Mood"},
}


def _p(id_: str, cat: str, es: str, en: str, prompt: str) -> dict[str, Any]:
    return {"id": id_, "category": cat, "name": {"es": es, "en": en}, "prompt": prompt}


MOTION_PRESETS: list[dict[str, Any]] = [
    _p("free", "camera", "Libre", "Free", ""),
    _p("push-in", "camera", "Acercamiento", "Push in", "The camera pushes in slowly and steadily toward the subject."),
    _p("pull-back", "camera", "Alejamiento que revela", "Pull back reveal",
       "The camera pulls back smoothly, revealing the wider environment around the subject."),
    _p("orbit-left", "camera", "Órbita a la izquierda", "Orbit left",
       "The camera orbits slowly to the left around the subject, keeping it centered."),
    _p("orbit-right", "camera", "Órbita a la derecha", "Orbit right",
       "The camera orbits slowly to the right around the subject, keeping it centered."),
    _p("crane-up", "camera", "Grúa que sube", "Crane rise",
       "The camera cranes up and rises above the scene, tilting down to keep the subject in frame."),
    _p("truck-left", "camera", "Travelling lateral", "Side track",
       "The camera tracks sideways to the left, parallel to the subject, with foreground parallax."),
    _p("handheld", "camera", "Cámara en mano", "Handheld drift",
       "Handheld camera, subtle natural sway and breathing, documentary feel."),
    _p("static", "camera", "Plano fijo", "Locked off",
       "The camera is completely still, a locked-off frame. Only the subject and the environment move."),
    _p("rack-focus", "camera", "Cambio de foco", "Rack focus",
       "The focus racks from the foreground to the subject in the background, shallow depth of field."),
    _p("dolly-zoom", "epic", "Vértigo", "Vertigo",
       "Dolly zoom: the camera moves forward while zooming out, the background stretches away behind the subject."),
    _p("crash-zoom", "epic", "Zoom brusco", "Crash zoom", "A sudden fast crash zoom in onto the subject."),
    _p("fpv-dive", "epic", "Picado de dron", "FPV dive",
       "FPV drone shot, fast and fluid, diving down and swooping through the scene."),
    _p("whip-pan", "epic", "Barrido rápido", "Whip pan", "A fast whip pan with motion blur that lands on the subject."),
    _p("aerial-pull", "epic", "Retirada aérea", "Aerial pullback",
       "Aerial shot pulling back and rising high into the sky, the landscape opening below."),
    _p("freeze-orbit", "epic", "Órbita congelada", "Frozen orbit",
       "Time freezes; the camera orbits around the frozen moment, particles suspended in the air."),
    _p("snorricam", "epic", "Cámara al cuerpo", "Body mount",
       "Body-mounted camera locked to the subject's face while the world moves around them."),
    _p("embers", "vfx", "Tormenta de brasas", "Ember storm",
       "Glowing embers and sparks swirl through the air around the subject."),
    _p("ink-bloom", "vfx", "Tinta que florece", "Ink bloom", "Dark ink blooms and spreads through the frame like smoke in water."),
    _p("shatter", "vfx", "Cristal roto", "Glass shatter",
       "The scene cracks and shatters like glass, fragments drifting in slow motion."),
    _p("fog-roll", "vfx", "Niebla rodante", "Fog roll", "Thick fog rolls in low across the ground and slowly swallows the scene."),
    _p("neon-rain", "vfx", "Lluvia de neón", "Neon rain", "Heavy rain falls, neon reflections shimmer on every wet surface."),
    _p("disintegrate", "vfx", "Se deshace en polvo", "Dust away",
       "The subject slowly disintegrates into fine drifting dust carried by the wind."),
    _p("bloom", "vfx", "Vegetación que crece", "Overgrowth",
       "Plants and flowers grow rapidly and overtake every surface in time-lapse."),
    _p("golden", "mood", "Hora dorada", "Golden hour", "Warm low golden-hour sun, long shadows, soft haze, lens flare."),
    _p("noir", "mood", "Noche negra", "Night noir", "High-contrast night, hard single light source, deep shadows, wet streets."),
    _p("dreamlike", "mood", "Deriva onírica", "Dream drift",
       "Slow dreamlike motion, soft focus edges, floating particles, gentle light."),
    _p("storm", "mood", "Frente de tormenta", "Storm front",
       "A storm front moves in, wind whips through the scene, flashes of lightning."),
]
PRESETS = {p["id"]: p for p in MOTION_PRESETS}
MOVES = tuple(p["id"] for p in MOTION_PRESETS if p["category"] in ("camera", "epic"))
EFFECTS = tuple(p["id"] for p in MOTION_PRESETS if p["category"] in ("vfx", "mood"))


def _o(id_: str, es: str, en: str, prompt: str, sets: Optional[dict[str, str]] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": id_, "name": {"es": es, "en": en}, "prompt": prompt}
    if sets:
        out["sets"] = sets
    return out


AUTO = _o("auto", "Automático", "Auto", "")

AXES: list[dict[str, Any]] = [
    {"id": "genre", "name": {"es": "Género", "en": "Genre"}, "options": [
        AUTO,
        _o("drama", "Drama", "Drama", "intimate character drama, naturalistic performances, emotional weight in small gestures",
           {"lens": "large-format", "light": "overcast", "grade": "natural"}),
        _o("noir", "Cine negro", "Noir", "film noir, moral shadows, smoke and venetian-blind light, a city that keeps secrets",
           {"light": "chiaroscuro", "film": "bw", "look": "noir", "grade": "mono"}),
        _o("horror", "Terror", "Horror", "horror film, creeping tension, the camera lingers on what should not be there",
           {"light": "moon", "look": "horror", "grade": "steel"}),
        _o("action", "Acción", "Action", "high-octane action film, kinetic momentum, debris and speed, bodies in motion",
           {"lens": "anamorphic", "light": "hard", "grade": "teal"}),
        _o("comedy", "Comedia", "Comedy", "bright comedy, playful timing, warm and open frames, expressive faces",
           {"lens": "wide-prime", "light": "golden", "grade": "pastel"}),
        _o("epic", "Épico", "Epic", "sweeping epic, mythic scale, vast landscapes and tiny figures against them",
           {"lens": "anamorphic", "light": "golden", "look": "epic"}),
        _o("scifi", "Ciencia ficción", "Sci-fi", "science fiction, cold engineered spaces, the future made physical",
           {"light": "neon", "look": "scifi", "grade": "steel"}),
    ]},
    {"id": "lens", "name": {"es": "Cámara y lente", "en": "Camera and lens"}, "options": [
        AUTO,
        _o("large-format", "Gran formato · 50 mm f/2", "Large format · 50mm f/2",
           "shot on a large-format digital cinema camera, 50mm lens at f/2, shallow depth of field"),
        _o("anamorphic", "Anamórfica · 40 mm", "Anamorphic · 40mm",
           "anamorphic 40mm lens, oval bokeh, horizontal lens flares, 2.39:1 feel"),
        _o("wide-prime", "Angular · 24 mm", "Wide prime · 24mm", "24mm wide prime lens, deep focus, environmental framing"),
        _o("tele", "Teleobjetivo · 135 mm", "Telephoto · 135mm", "135mm telephoto lens, compressed background, creamy falloff"),
        _o("16mm", "Cámara de 16 mm", "16mm film camera", "shot on a 16mm film camera, handheld intimacy, visible grain"),
        _o("vintage", "Óptica antigua", "Vintage glass", "vintage uncoated lens, soft halation, gentle vignette"),
    ]},
    {"id": "framing", "name": {"es": "Encuadre", "en": "Framing"}, "options": [
        AUTO,
        _o("wide", "Plano general", "Wide", "wide establishing shot"),
        _o("medium", "Plano medio", "Medium", "medium shot, waist up"),
        _o("close", "Primer plano", "Close-up", "close-up on the face"),
        _o("ecu", "Plano detalle", "Extreme close-up", "extreme close-up detail"),
        _o("ots", "Sobre el hombro", "Over the shoulder", "over-the-shoulder shot"),
        _o("low", "Contrapicado", "Low angle", "low-angle shot looking up, heroic"),
        _o("high", "Picado", "High angle", "high-angle shot looking down"),
        _o("top", "Cenital", "Top down", "top-down overhead shot"),
    ]},
    {"id": "light", "name": {"es": "Luz", "en": "Light"}, "options": [
        AUTO,
        _o("golden", "Hora dorada", "Golden hour", "warm low golden-hour sun, long shadows"),
        _o("overcast", "Nublado suave", "Overcast soft", "soft overcast daylight, gentle wraparound light"),
        _o("practical", "Noche con luces prácticas", "Practical night",
           "night lit only by practical lamps and signs, pools of light"),
        _o("hard", "Mediodía duro", "Hard noon", "hard midday sun, crisp black shadows"),
        _o("neon", "Neón", "Neon", "saturated neon light, magenta and cyan spill"),
        _o("candle", "Luz de velas", "Candlelight", "flickering candlelight, warm falloff into darkness"),
        _o("moon", "Luz de luna", "Moonlight", "cold blue moonlight, silver rim light"),
        _o("chiaroscuro", "Claroscuro", "Chiaroscuro", "single hard key light, deep chiaroscuro shadows"),
    ]},
    {"id": "film", "name": {"es": "Película", "en": "Film stock"}, "options": [
        AUTO,
        _o("500t", "Tungsteno 500T", "Tungsten 500T", "tungsten-balanced 500T motion picture film, fine grain, cool shadows"),
        _o("250d", "Luz día 250D", "Daylight 250D", "daylight 250D motion picture film, natural color"),
        _o("reversal", "Diapositiva", "Slide reversal", "color reversal slide film, rich saturated color, deep blacks"),
        _o("bw", "Blanco y negro 400", "Black and white 400", "black and white 400 speed film, strong grain, silver tones"),
        _o("digital", "Digital limpio", "Clean digital", "clean digital cinema image, no grain"),
    ]},
    {"id": "look", "name": {"es": "Atmósfera", "en": "Look"}, "options": [
        AUTO,
        _o("noir", "Neo-noir", "Neo-noir", "neo-noir mood, rain-slick, menace"),
        _o("epic", "Épica", "Epic", "epic scale, vast, awe"),
        _o("horror", "Inquietud", "Dread", "quiet dread, negative space, something wrong in the frame"),
        _o("doc", "Documental", "Documentary", "observational documentary realism"),
        _o("dream", "Onírica", "Dreamlike", "dreamlike and uncanny, soft surreal atmosphere"),
        _o("scifi", "Ciencia ficción dura", "Hard sci-fi", "hard science fiction, practical-feeling machinery"),
    ]},
    {"id": "grade", "name": {"es": "Etalonaje", "en": "Grade"}, "options": [
        AUTO,
        _o("teal", "Turquesa y naranja", "Teal and orange", "teal and orange color grade"),
        _o("bleach", "Blanqueo parcial", "Bleach bypass", "bleach-bypass grade, low saturation, high contrast"),
        _o("pastel", "Pastel cálido", "Warm pastel", "warm pastel grade, lifted blacks"),
        _o("steel", "Acero frío", "Cold steel", "cold steel-blue grade"),
        _o("mono", "Monocromo", "Monochrome", "monochrome grade"),
        _o("natural", "Natural", "Natural", "natural, true-to-life color"),
    ]},
]
AXIS_BY_ID = {a["id"]: a for a in AXES}
FRAMINGS = tuple(o["id"] for o in AXIS_BY_ID["framing"]["options"] if o["id"] != "auto")

ERA_MIN, ERA_MAX = 1960, 2020
ERAS = [
    (1960, "1960s period film, dye-transfer color, rich reds and painted skies, soft contrast, fine grain"),
    (1970, "1970s film look, warm faded color, lifted blacks, soft contrast, heavy grain"),
    (1980, "1980s look, punchy saturated color, diffusion filter bloom on the highlights"),
    (1990, "1990s look, slightly green shadows, analog print texture"),
    (2000, "early 2000s look, desaturated bleach-bypass contrast, cool steel tones"),
    (2010, "2010s digital cinema look, teal shadows and warm skin tones, clean image"),
    (2020, "modern high-dynamic-range digital look, clean and crisp"),
]

#: video engine -> the generate body's template and how its length is asked for
VIDEO_ENGINES = {
    "auto": {"name": {"es": "El mejor instalado", "en": "Best installed"}},
    "ltx23": {"name": {"es": "LTX 2.3", "en": "LTX 2.3"}},
    "wan14b": {"name": {"es": "Wan 2.2 14B", "en": "Wan 2.2 14B"}},
    "wan": {"name": {"es": "Wan 2.2 5B (rápido)", "en": "Wan 2.2 5B (fast)"}},
}
MAX_SHOTS = 24
MIN_CLIP_S, MAX_CLIP_S = 2.0, 8.0
DIRECTOR_LOOK = "cinematic film still, rich detail, natural skin, no text"


class DirectorError(shorts.ShortError):
    pass


def catalog(lang: str = "es") -> dict[str, Any]:
    """Everything a person or an agent picks from: the motion presets by
    category, the direction axes with their options (a genre also sets
    other axes), the decades and the video engines."""
    def name(entry: dict[str, Any]) -> str:
        return entry["name"].get(lang) or entry["name"]["en"]

    return {
        "presets": [{"id": p["id"], "category": p["category"], "name": name(p), "prompt": p["prompt"]} for p in MOTION_PRESETS],
        "categories": {k: v.get(lang) or v["en"] for k, v in CATEGORIES.items()},
        "moves": list(MOVES), "effects": list(EFFECTS),
        "axes": [{"id": a["id"], "name": name(a),
                  "options": [{"id": o["id"], "name": name(o), "prompt": o["prompt"], **({"sets": o["sets"]} if o.get("sets") else {})}
                              for o in a["options"]]} for a in AXES],
        "eras": [{"year": y, "prompt": text} for y, text in ERAS],
        "engines": [{"id": k, "name": name(v)} for k, v in VIDEO_ENGINES.items()],
    }


# --------------------------------------------------------------- direction

def era_text(era: Optional[int]) -> str:
    if era is None:
        return ""
    year = max(ERA_MIN, min(ERA_MAX, int(era)))
    return min(ERAS, key=lambda e: abs(e[0] - year))[1]


def pick_genre(picks: dict[str, str], genre: str) -> dict[str, str]:
    """A genre fills the axes the person left on auto (its own suggestions);
    what was picked by hand stays."""
    option = next((o for o in AXIS_BY_ID["genre"]["options"] if o["id"] == genre), None)
    out = dict(picks)
    for axis, value in ((option or {}).get("sets") or {}).items():
        if out.get(axis, "auto") == "auto":
            out[axis] = value
    out["genre"] = genre
    return out


def direction(picks: dict[str, str], era: Optional[int] = None, still: bool = False) -> list[str]:
    """The direction fragments in prompt order: genre, the other axes (but
    framing, which is per shot), then the era's decade. A still leaves the
    genre's words out: they speak of performances and figures ("bodies in
    motion", "tiny figures") and make the image model add people to an empty
    establishing shot; the genre reaches the still through the axes it set
    (lens, light, film, look, grade) and the clip through its words."""
    out = []
    for axis in AXES:
        if axis["id"] == "framing" or (still and axis["id"] == "genre"):
            continue
        option = next((o for o in axis["options"] if o["id"] == picks.get(axis["id"], "auto")), None)
        if option and option["prompt"]:
            out.append(option["prompt"])
    if era is not None:
        out.append(era_text(era))
    return out


def _sentence(text: str) -> str:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return text
    text = text[:1].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _framing_text(framing: str) -> str:
    option = next((o for o in AXIS_BY_ID["framing"]["options"] if o["id"] == framing), None)
    text = (option or {}).get("prompt") or ""
    return text[:1].upper() + text[1:]


def _mentions(shot: dict[str, Any]) -> str:
    return " ".join(f"@{name}" for name in shot.get("cast") or [])


def still_prompt(shot: dict[str, Any], picks: dict[str, str], era: Optional[int] = None, look: str = "") -> str:
    """The picture a shot starts from: framing, the cast it names, what is
    seen, the shared direction and the house look. Each shot's description
    stands alone (place and characters named every time): the image model
    never sees the rest of the story."""
    parts = [_framing_text(shot.get("framing") or ""), _mentions(shot), shot.get("visual") or shot.get("text") or "",
             ", ".join(direction(picks, era, still=True)), look]
    return " ".join(_sentence(p) for p in parts if str(p).strip())


def clip_prompt(shot: dict[str, Any], picks: dict[str, str], era: Optional[int] = None) -> str:
    """The clip of a shot: the camera move, the effect/mood presets, what
    happens in it, then the shared direction."""
    move = PRESETS.get(shot.get("move") or "")
    effects = [PRESETS[e]["prompt"] for e in shot.get("effects") or [] if e in PRESETS]
    parts = [move["prompt"] if move else "", *effects, shot.get("action") or shot.get("visual") or "",
             ", ".join(direction(picks, era))]
    return " ".join(_sentence(p) for p in parts if str(p).strip())


# ------------------------------------------------------------- snapping

_FRAMING_WORDS = [
    (r"extreme|ecu|macro|detail|detalle", "ecu"),
    (r"over.?the.?shoulder|ots|hombro", "ots"),
    (r"top.?down|overhead|bird|cenital", "top"),
    (r"low|contrapicado", "low"),
    (r"high|picado", "high"),
    (r"close|primer", "close"),
    (r"medium|mid|waist|medio", "medium"),
    (r"wide|establish|long|full|general", "wide"),
]
_MOVE_WORDS = [
    (r"dolly.?zoom|vertigo|v[eé]rtigo", "dolly-zoom"),
    (r"crash", "crash-zoom"),
    (r"fpv|drone|dive|dron", "fpv-dive"),
    (r"whip|barrido", "whip-pan"),
    (r"aerial|a[eé]re", "aerial-pull"),
    (r"frozen|freeze|congel", "freeze-orbit"),
    (r"body|snorri|cuerpo", "snorricam"),
    (r"orbit.*right|arc.*right|derecha", "orbit-right"),
    (r"orbit|arc|circle|[oó]rbita", "orbit-left"),
    (r"crane|rise|boom|jib|gr[uú]a", "crane-up"),
    (r"pull|back|reveal|retreat|alej", "pull-back"),
    (r"push|dolly|\bin\b|forward|approach|acerc", "push-in"),
    (r"track|truck|side|lateral|follow|travel", "truck-left"),
    (r"hand.?held|shaky|mano", "handheld"),
    (r"rack|focus|foco", "rack-focus"),
    (r"static|still|locked|lock|tripod|none|fijo", "static"),
]


def snap_framing(value: Any, i: int = 0) -> str:
    s = str(value or "").strip()
    if s in FRAMINGS:
        return s
    for option in AXIS_BY_ID["framing"]["options"]:
        if option["id"] != "auto" and s.lower() in (option["name"]["es"].lower(), option["name"]["en"].lower()):
            return option["id"]
    for pattern, framing in _FRAMING_WORDS:
        if s and re.search(pattern, s, re.IGNORECASE):
            return framing
    return ("wide", "medium", "close")[i % 3]


def snap_move(value: Any, default: str = "push-in") -> str:
    s = str(value or "").strip()
    if s in MOVES:
        return s
    for p in MOTION_PRESETS:
        if p["id"] in MOVES and s.lower() in (p["name"]["es"].lower(), p["name"]["en"].lower()):
            return p["id"]
    for pattern, move in _MOVE_WORDS:
        if s and re.search(pattern, s, re.IGNORECASE):
            return move
    return default


# ----------------------------------------------------------------- shots

def _clean(text: Any, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def normalise_shots(shots: Any, default_move: str = "push-in", strict: bool = True) -> list[dict[str, Any]]:
    """A shot list: [{id?, text, visual?, action?, framing?, move?, effects?,
    cast?, animate?, seed?, clip_seed?}]. Ids are kept (they key the shot's
    still and clip) and given to new shots; framings and moves are snapped
    to the catalog (a slip such as "slow dolly" becomes push-in)."""
    if not isinstance(shots, list) or not shots:
        raise DirectorError("bad_shots", "the shot list must be a non-empty list")
    if len(shots) > MAX_SHOTS:
        raise DirectorError("bad_shots", f"at most {MAX_SHOTS} shots")
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for i, raw in enumerate(shots):
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict):
            raise DirectorError("bad_shots", f"shots[{i}] must be an object")
        text = _clean(raw.get("text") or raw.get("narration"), 600)
        if not text:
            if strict:
                raise DirectorError("bad_shots", f"shots[{i}] needs its narration line (text)")
            continue
        sid = str(raw.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", sid) or sid in used:
            sid = ""
        effects = raw.get("effects") or []
        if isinstance(effects, str):
            effects = [effects]
        shot = {
            "id": sid,
            "text": text,
            "visual": _clean(raw.get("visual") or raw.get("image") or raw.get("description") or text, 600),
            "action": _clean(raw.get("action") or raw.get("motion") or "", 400),
            "framing": snap_framing(raw.get("framing") or raw.get("shot") or raw.get("size"), i),
            "move": snap_move(raw.get("move") or raw.get("camera") or raw.get("movement"), default_move),
            "effects": [e for e in dict.fromkeys(str(x) for x in effects) if e in EFFECTS][:3],
            "cast": [_clean(n, 60).lstrip("@") for n in raw.get("cast") or [] if _clean(n, 60)][:4],
            "animate": raw.get("animate", True) is not False,
        }
        for key in ("seed", "clip_seed"):
            if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool):
                shot[key] = int(raw[key]) % (2**31 - 1)
        if sid:
            used.add(sid)
        out.append(shot)
    if not out:
        raise DirectorError("bad_shots", "no shot has a narration line")
    n = 1
    for shot in out:
        if not shot["id"]:
            while f"p{n}" in used:
                n += 1
            shot["id"] = f"p{n}"
            used.add(shot["id"])
    return out


def split_story(idea: str, want: int = 0) -> list[dict[str, Any]]:
    """Last resort with no language model: one shot per sentence, cycling
    wide, medium and close, with a few different moves."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|;\s*", str(idea or "")) if len(p.strip()) > 2]
    if not parts:
        raise DirectorError("bad_idea", "the idea is empty")
    if want:
        parts = parts[:max(1, want)]
    moves = ["push-in", "static", "handheld", "pull-back", "orbit-left", "crane-up"]
    return [{"text": _sentence(p[:1].upper() + p[1:]), "visual": p, "framing": ("wide", "medium", "close")[i % 3],
             "move": moves[i % len(moves)]} for i, p in enumerate(parts[:MAX_SHOTS])]


def _repair(blob: str) -> str:
    return (re.sub(r",\s*([}\]])", r"\1", re.sub(r'"(\w+):"', r'"\1":"', blob))
            .replace("“", '"').replace("”", '"'))


def _shots_in(data: Any) -> Optional[list[Any]]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("shots", "shot_list", "segments", "planos"):
            if isinstance(data.get(key), list):
                return data[key]
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
    return None


def parse_shot_reply(text: str, default_move: str = "push-in") -> dict[str, Any]:
    """The shot list inside a language model's reply: strict JSON, then
    repaired JSON (trailing commas, `"key:"` slips, curly quotes), then
    field by field over every complete {...} block, so a reply that broke
    off still gives the shots it finished. Returns {title?, description?,
    hashtags?, shots}."""
    body = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL)
    body = re.sub(r"```(?:json)?", "", body, flags=re.IGNORECASE)
    head: dict[str, Any] = {}
    candidates = []
    a, b = body.find("{"), body.rfind("}")
    if a >= 0 and b > a:
        candidates.append(body[a:b + 1])
    a, b = body.find("["), body.rfind("]")
    if a >= 0 and b > a:
        candidates.append(body[a:b + 1])
    for blob in candidates:
        for src in (blob, _repair(blob)):
            try:
                data = json.loads(src)
            except json.JSONDecodeError:
                continue
            shots = _shots_in(data)
            if shots:
                if isinstance(data, dict):
                    head = {k: data[k] for k in ("title", "description", "hashtags") if data.get(k)}
                try:
                    return {**head, "shots": normalise_shots(shots, default_move, strict=False)}
                except DirectorError:
                    continue
    blocks = re.findall(r"\{[^{}]*\}", body)

    def field(block: str, key: str) -> Optional[str]:
        m = re.search(rf'"?{key}"?\s*:\s*"([^"]*)"', block, re.IGNORECASE)
        return m.group(1) if m else None

    loose = [{k: field(blk, k) for k in ("text", "visual", "action", "framing", "move")} for blk in blocks]
    loose = [s for s in loose if s.get("text")]
    if not loose:
        raise DirectorError("shots_unreadable", "the model's reply held no shot list")
    return {"shots": normalise_shots(loose, default_move, strict=False)}


def plan_messages(idea: str, language: str, duration_s: float, director: dict[str, Any],
                  shots_wanted: int = 0) -> list[dict[str, Any]]:
    """The language model's brief: a short film told by a narrator, one shot
    per narration line."""
    lang = shorts.LANGUAGE_NAMES.get(language, language)
    words = int(duration_s * shorts.WORDS_PER_SECOND.get(language, 2.5))
    n = shots_wanted or max(3, min(12, round(duration_s / 5)))
    style = ", ".join(direction(director.get("picks") or {}, director.get("era")))
    moves = ", ".join(MOVES)
    framings = ", ".join(FRAMINGS)
    cast = director.get("cast") or []
    user = (
        f"Idea for a short film: {idea}\n"
        f"Narration language: {lang}. About {words} words in total (~{int(duration_s)} seconds spoken).\n"
        f"Exactly {n} shots, in story order, each one a single continuous camera shot of about 3 to 7 seconds.\n"
        + (f"Direction for every shot: {style}.\n" if style else "")
        + (f"Recurring characters (name them exactly like this in \"cast\" when they appear): {', '.join(cast)}.\n" if cast else "")
        + "For every shot give:\n"
        f"- \"text\": the narrator's line for this shot, in {lang} (one or two short sentences);\n"
        "- \"visual\": an English description of the first frame for an image model: subject, place, light, "
        "composition; it must stand alone, naming the place and the characters every time (the model never sees "
        "the other shots); no text, letters or logos in the picture;\n"
        "- \"action\": in English, what moves during the shot (who does what), in one sentence;\n"
        f"- \"framing\": one of {framings};\n"
        f"- \"move\": the camera move, one of {moves};\n"
        + ("- \"cast\": the recurring characters in the shot, by name (a list, may be empty).\n" if cast else "")
        + f"Also give \"title\" (at most 60 characters, in {lang}), \"description\" (one sentence, in {lang}) and "
        "\"hashtags\" (3 to 6 words without #). Vary framings and moves; open wide, close on a strong image.\n"
        "Answer with one JSON object only, exactly in this shape: {\"title\": \"...\", \"description\": \"...\", "
        "\"hashtags\": [\"...\"], \"shots\": [{\"text\": \"...\", \"visual\": \"...\", \"action\": \"...\", "
        "\"framing\": \"wide\", \"move\": \"push-in\"}]}"
    )
    system = "You are a film director and screenwriter planning a short narrated film. You answer with one JSON object and nothing else."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def plan(chat: Optional[Callable[..., str]], idea: str, language: str = "es", duration_s: float = 30,
         director: Optional[dict[str, Any]] = None, shots_wanted: int = 0) -> dict[str, Any]:
    """The shot list for an idea, without making anything: the family's
    language model writes it; with no model (or an unreadable reply) the
    idea is split one shot per sentence and `source` says so."""
    d = normalise_director(director or {})
    idea = str(idea or "").strip()
    if not idea:
        raise DirectorError("bad_idea", "describe the film's idea")
    note = None
    if chat is not None:
        try:
            reply = chat(plan_messages(idea, language, duration_s, d, shots_wanted), 2400, 0.8)
            out = parse_shot_reply(reply, d["move"])
            return {**out, "source": "llm"}
        except DirectorError as exc:
            note = exc.message
        except Exception as exc:  # noqa: BLE001 - no model resident, network...
            note = f"{type(exc).__name__}: {str(exc)[:200]}"
    shots = normalise_shots(split_story(idea, shots_wanted), d["move"])
    return {"shots": shots, "source": "split", **({"note": note} if note else {})}


# ------------------------------------------------------------------- spec

def normalise_director(d: Any) -> dict[str, Any]:
    """The `director` section: {picks: {genre, lens, light, film, look,
    grade}, era: 1960-2020 | null, move: default camera move, effects:
    [vfx/mood ids for every shot], engine: auto|ltx23|wan14b|wan, takes:
    1-4 stills per shot, end_frames: bool, sfx: bool, cast: [names],
    shots: [...]}. Unknown option ids are refused with their axis named."""
    if not isinstance(d, dict):
        raise DirectorError("bad_director", "director must be an object")
    d = deepcopy(d)
    raw_picks = d.get("picks") or {}
    if not isinstance(raw_picks, dict):
        raise DirectorError("bad_director", "director.picks must be an object")
    picks: dict[str, str] = {}
    for axis_id, value in raw_picks.items():
        axis = AXIS_BY_ID.get(axis_id)
        if axis is None or axis_id == "framing":
            raise DirectorError("bad_director", f"director.picks: unknown axis '{axis_id}' "
                                                f"({', '.join(a for a in AXIS_BY_ID if a != 'framing')})")
        if value not in {o["id"] for o in axis["options"]}:
            raise DirectorError("bad_director", f"director.picks.{axis_id}: '{value}' is not one of "
                                                f"{', '.join(o['id'] for o in axis['options'])}")
        picks[axis_id] = value
    if picks.get("genre", "auto") != "auto":
        picks = pick_genre(picks, picks["genre"])
    era = d.get("era")
    if era is not None:
        if isinstance(era, bool) or not isinstance(era, (int, float)) or not ERA_MIN <= era <= ERA_MAX:
            raise DirectorError("bad_director", f"director.era is a year between {ERA_MIN} and {ERA_MAX}")
        era = int(era)
    move = d.get("move") or "push-in"
    if move not in MOVES:
        raise DirectorError("bad_director", f"director.move is one of {', '.join(MOVES)}")
    effects = d.get("effects") or []
    if not isinstance(effects, list) or any(e not in EFFECTS for e in effects):
        raise DirectorError("bad_director", f"director.effects holds {', '.join(EFFECTS)}")
    engine_id = d.get("engine") or "auto"
    if engine_id not in VIDEO_ENGINES:
        raise DirectorError("bad_director", f"director.engine is one of {', '.join(VIDEO_ENGINES)}")
    takes = d.get("takes", 1)
    if isinstance(takes, bool) or not isinstance(takes, int) or not 1 <= takes <= 4:
        raise DirectorError("bad_director", "director.takes is 1 to 4 stills per shot")
    cast = [_clean(n, 60).lstrip("@") for n in d.get("cast") or [] if _clean(n, 60)][:8]
    out = {"picks": picks, "era": era, "move": move, "effects": list(dict.fromkeys(effects)), "engine": engine_id,
           "takes": takes, "end_frames": bool(d.get("end_frames", False)), "sfx": bool(d.get("sfx", True)), "cast": cast,
           "idea": _clean(d.get("idea"), 1200)}
    if d.get("shots"):
        out["shots"] = normalise_shots(d["shots"], move)
    return out


def is_director(state: dict[str, Any]) -> bool:
    return shorts.is_short(state) and isinstance((state.get("spec") or {}).get("director"), dict)


def create(data_dir: Path, name: str, *, idea: str = "", director: Optional[dict[str, Any]] = None,
           options: Optional[dict[str, Any]] = None, settings: Optional[dict[str, Any]] = None,
           project_id: Optional[str] = None) -> dict[str, Any]:
    """A director film: an idea (the shot list is written for you) or a shot
    list. `options` are the short's own (language, duration_s, voice, music,
    captions, timeline...); pictures are always generated."""
    d = normalise_director({**(director or {}), "idea": idea or (director or {}).get("idea") or ""})
    if not d.get("shots") and not d["idea"]:
        raise DirectorError("bad_idea", "a director film needs an idea or a shot list")
    spec = deepcopy(options or {})
    spec["topic"] = (d["idea"] or name)[:600]
    spec["director"] = d
    visuals = dict(spec.get("visuals") or {})
    visuals.update(source="generate", clips=0)
    visuals.setdefault("look", DIRECTOR_LOOK)
    spec["visuals"] = visuals
    spec.setdefault("timeline", {"aspects": ["16:9"], "qualities": ["preview"]})
    spec.setdefault("captions", {"style": "default", "max_words": 6})
    spec.pop("script", None)
    return shorts.create_short(data_dir, name, spec, settings, project_id)


# ------------------------------------------------------------------- view

def _shot_seed(spec: dict[str, Any], shot: dict[str, Any], i: int, key: str, base: int) -> int:
    if isinstance(shot.get(key), int):
        return shot[key]
    return base + 1000 * int(spec.get("seed") or 0) + 37 * i


def view(state: dict[str, Any]) -> dict[str, Any]:
    """The film shot by shot: each shot's fields with its still (and the
    other takes), its clip and its place on the narration's clock."""
    if not is_director(state):
        raise DirectorError("not_a_director_film", f"'{state.get('slug')}' is not a director film")
    d = state["spec"]["director"]
    done = state.get("done") or {}
    visuals = done.get("visuals") or {}
    items = visuals.get("items") or {}
    history = visuals.get("history") or {}
    clips = (done.get("clips") or {}).get("items") or {}
    plan_by = {p["key"]: p for p in visuals.get("plan") or []}
    shots = []
    for shot in d.get("shots") or []:
        item = items.get(shot["id"]) or {}
        takes = list(dict.fromkeys([a for t in history.get(shot["id"]) or [] for a in t.get("asset_ids") or []]))
        placed = plan_by.get(shot["id"]) or {}
        shots.append({**shot, "still": item.get("asset_id"), "takes": takes, "clip": clips.get(shot["id"]),
                      "start_s": placed.get("start_s"), "duration_s": placed.get("duration_s")})
    timelines = ((done.get("timeline") or {}).get("timelines") or {})
    return {"production": state["slug"], "name": state.get("name"), "status": state.get("status"), "stage": state.get("stage"),
            "title": (done.get("script") or {}).get("title"), "idea": d.get("idea"),
            "direction": {k: d[k] for k in ("picks", "era", "move", "effects", "engine", "takes", "end_frames", "sfx", "cast")},
            "shots": shots, "renders": {aspect: info.get("renders") for aspect, info in timelines.items()},
            "message": state.get("message")}


def script_from_shots(shots: list[dict[str, Any]], old: Optional[dict[str, Any]] = None, **extra: Any) -> dict[str, Any]:
    """The short's script mirrored from the shot list: one narration segment
    per shot (keyed by its id); title, description and hashtags kept."""
    old = old or {}
    script: dict[str, Any] = {
        "segments": [{"text": s["text"], "visual": s["visual"], "query": shorts.keywords(s["visual"]), "shot_id": s["id"]}
                     for s in shots]}
    for key in ("title", "description", "hashtags", "source"):
        value = extra.get(key) or old.get(key)
        if value:
            script[key] = value
    if isinstance(script.get("hashtags"), list):
        script["hashtags"] = [re.sub(r"[^\w]", "", str(t).lstrip("#"))[:40] for t in script["hashtags"] if str(t).strip()][:10]
    return script


# ------------------------------------------------------------------ edits

STILL_FIELDS = ("visual", "framing", "cast")
CLIP_FIELDS = ("action", "move", "effects", "animate")


def update_shots(data_dir: Path, slug: str, changes: Any, direction_patch: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Edit the film shot by shot. Each change is {id, <fields>}, {id,
    delete: true}, {after: id | null, text, ...} (a new shot) or {id, take:
    asset_id} (an earlier still of the shot back in use), and {id, redo:
    "image"|"clip"} redoes that shot with a new seed. Only what a change
    touches is redone on the next run: a new narration line (or a shot
    added, removed or moved) redoes the voice and the cut, a new picture
    description redoes that shot's still and clip, a new move or action only
    its clip. `direction_patch` changes the shared direction: everything is
    redrawn then (stills and clips)."""
    if changes is None:
        changes = []
    if not isinstance(changes, list):
        raise DirectorError("bad_changes", "changes must be a list")
    with prod.lock_for(slug):
        state = prod.load_state(data_dir, slug)
        if not is_director(state):
            raise DirectorError("not_a_director_film", f"'{slug}' is not a director film")
        if prod.is_running(state, data_dir):
            raise DirectorError("production_running", "the film is being made; wait for it to pause or finish, or cancel it")
        d = state["spec"]["director"]
        shots: list[dict[str, Any]] = deepcopy(d.get("shots") or [])
        if not shots:
            raise DirectorError("no_shots_yet", "the shot list is written by the first run; edit it after the script stage")
        done = state.setdefault("done", {})
        visuals = done.setdefault("visuals", {"complete": False, "items": {}})
        items = visuals.setdefault("items", {})
        clip_entry = done.setdefault("clips", {"complete": False, "items": {}})
        clips = clip_entry.setdefault("items", {})
        redo_voice = False
        redo_all = False
        touched: list[str] = []
        if direction_patch:
            merged = normalise_director({**{k: v for k, v in d.items() if k != "shots"}, **direction_patch, "shots": shots})
            redo_all = any(merged[k] != d.get(k) for k in ("picks", "era", "effects", "engine", "end_frames", "takes"))
            redo_voice = merged["sfx"] != d.get("sfx")
            d.update({k: v for k, v in merged.items() if k != "shots"})
        by_id = {s["id"]: s for s in shots}
        for i, change in enumerate(changes):
            if not isinstance(change, dict):
                raise DirectorError("bad_changes", f"changes[{i}] must be an object")
            sid = change.get("id")
            if sid is None and "after" in change:
                new = normalise_shots([{k: v for k, v in change.items() if k != "after"}], d["move"])[0]
                new["id"] = "p" + str(1 + max([int(m.group(1)) for s in shots if (m := re.fullmatch(r"p(\d+)", s["id"]))] or [0]))
                at = 0 if change["after"] is None else next((k + 1 for k, s in enumerate(shots) if s["id"] == change["after"]), None)
                if at is None:
                    raise DirectorError("bad_changes", f"changes[{i}].after: no shot '{change['after']}'")
                shots.insert(at, new)
                by_id[new["id"]] = new
                redo_voice = True
                touched.append(new["id"])
                continue
            shot = by_id.get(sid)
            if shot is None:
                raise DirectorError("bad_changes", f"changes[{i}]: no shot '{sid}' ({', '.join(by_id)})")
            if change.get("delete"):
                shots = [s for s in shots if s["id"] != sid]
                by_id.pop(sid)
                items.pop(sid, None)
                clips.pop(sid, None)
                redo_voice = True
                touched.append(sid)
                continue
            if "move_to" in change:
                pos = change["move_to"]
                if isinstance(pos, bool) or not isinstance(pos, int) or not 0 <= pos < len(shots):
                    raise DirectorError("bad_changes", f"changes[{i}].move_to is a position from 0 to {len(shots) - 1}")
                shots.remove(shot)
                shots.insert(pos, shot)
                redo_voice = True
            fields = {k: v for k, v in change.items() if k in ("text", *STILL_FIELDS, *CLIP_FIELDS)}
            if fields:
                fresh = normalise_shots([{**shot, **fields}], d["move"])[0]
                fresh["id"] = sid
                if fresh["text"] != shot["text"]:
                    redo_voice = True
                if any(fresh[k] != shot.get(k) for k in STILL_FIELDS):
                    items.pop(sid, None)
                    clips.pop(sid, None)
                if any(fresh[k] != shot.get(k) for k in CLIP_FIELDS):
                    clips.pop(sid, None)
                shot.clear()
                shot.update(fresh)
            take = change.get("take")
            if take:
                pool = [a for t in (visuals.get("history") or {}).get(sid) or [] for a in t.get("asset_ids") or []]
                if take not in pool:
                    raise DirectorError("bad_changes", f"changes[{i}].take: {take} is not a still of shot {sid}")
                if (items.get(sid) or {}).get("asset_id") != take:
                    items[sid] = {**(items.get(sid) or {}), "asset_id": take, "source": "generate", "kind": "image"}
                    clips.pop(sid, None)
            redo = change.get("redo")
            if redo not in (None, "image", "clip"):
                raise DirectorError("bad_changes", f"changes[{i}].redo is 'image' or 'clip'")
            if redo == "image":
                shot["seed"] = (_shot_seed(state["spec"], shot, 0, "seed", 100_000) + 7919) % (2**31 - 1)
                items.pop(sid, None)
                clips.pop(sid, None)
            if redo in ("image", "clip"):
                shot["clip_seed"] = (_shot_seed(state["spec"], shot, 0, "clip_seed", 200_000) + 104_729) % (2**31 - 1)
                clips.pop(sid, None)
            touched.append(sid)
        if redo_all:
            items.clear()
            clips.clear()
        d["shots"] = shots
        if d.get("end_frames"):
            # a clip that ends on the next still is stale once that still changes
            for k, shot in enumerate(shots[:-1]):
                link = (clip_entry.get("ends") or {}).get(shot["id"])
                nxt = (items.get(shots[k + 1]["id"]) or {}).get("asset_id")
                if link and link != nxt:
                    clips.pop(shot["id"], None)
        visuals["complete"] = False
        clip_entry["complete"] = False
        drop = ["animatic", "mix", "timeline", "report"]
        if redo_voice:
            drop = ["narration", *drop]
            if done.get("script"):
                done["script"] = script_from_shots(shots, done["script"])
        for stage in drop:
            done.pop(stage, None)
            state.get("partial", {}).pop(stage, None)
        state.get("partial", {}).pop("visuals", None)
        state.get("partial", {}).pop("clips", None)
        state.setdefault("review", {}).pop("animatic_approved", None)
        state["status"] = "queued"
        state["message"] = "shots edited: studio_director(action='run') makes what changed"
        prod.log(state, "script", "director_edit", shots=touched, voice=redo_voice, all=redo_all)
        prod.save_state(data_dir, state)
        return state


# -------------------------------------------------------------------- run

def video_body(engine_id: str, still: str, prompt: str, seconds: float, seed: int, end: Optional[str] = None) -> dict[str, Any]:
    """The generate body of a shot's clip for the chosen engine (the same
    mapping as a space's video node)."""
    seconds = max(MIN_CLIP_S, min(MAX_CLIP_S, float(seconds)))
    body: dict[str, Any] = {"reference_asset_id": still, "prompt": prompt[:4000], "seed": seed}
    if end and engine_id in ("auto", "wan14b"):
        body.update(template="auto_clip", end_asset_id=end, end_optional=True,
                    template_params={"seconds": round(seconds, 2), "lightning": True})
    elif engine_id == "ltx23":
        body.update(template="ltx23_i2v", template_params={"length": int(round(seconds * 24 / 8)) * 8 + 1})
    elif engine_id == "wan":
        body.update(template="wan22_ti2v", template_params={"length": int(round(seconds * 24)) + 1})
    elif engine_id == "wan14b":
        body.update(template="wan22_i2v_14b", template_params={"seconds": round(seconds, 2), "lightning": True})
    else:
        body.update(template="auto_clip", template_params={"seconds": round(seconds, 2), "lightning": True})
    return body


class DirectorRun(shorts.ShortRun):
    """The short's pipeline, shot by shot (see the module docstring)."""

    @property
    def director(self) -> dict[str, Any]:
        return self.spec["director"]

    def _shot(self, key: str) -> dict[str, Any]:
        return next(s for s in self.director["shots"] if s["id"] == key)

    def stage_script(self) -> Optional[str]:
        d = self.director
        title = None
        source = "given"
        if not d.get("shots"):
            chat = getattr(self.studio, "chat", None)
            out = plan(chat, d.get("idea") or self.spec.get("topic") or self.state["name"], self.spec["language"],
                       self.spec["duration_s"], d)
            if out.get("note"):
                self.log("plan_fallback", note=out["note"])
            d["shots"] = out["shots"]
            source = out["source"]
            title = out.get("title")
            extra = {k: out[k] for k in ("description", "hashtags") if out.get(k)}
        else:
            extra = {}
        old = (self.state["done"].get("script") or {})
        script = script_from_shots(d["shots"], old, source=source, **extra,
                                   title=title or old.get("title") or self.spec.get("title") or self.state["name"])
        self.state["done"]["script"] = script
        self.log("script", source=source, shots=len(d["shots"]))
        if (self.state.get("settings") or {}).get("script_review") and not (self.state.get("review") or {}).get("script_approved"):
            return "pause"
        return None

    def _plan(self) -> list[dict[str, Any]]:
        """One shot per narration line, keyed by the shot's id: from the
        line's start to the next one's (the first from 0, the last to the
        end of the voice and its tail)."""
        segments = self._segments()
        total = self.state["done"]["narration"]["duration_s"] + shorts.TAIL_S
        out = []
        for i, seg in enumerate(segments):
            a = 0.0 if i == 0 else float(seg["start_s"])
            b = float(segments[i + 1]["start_s"]) if i + 1 < len(segments) else total
            out.append({"key": seg.get("shot_id") or self.director["shots"][i]["id"], "segment": i, "index": 0,
                        "start_s": round(a, 3), "duration_s": round(max(0.5, b - a), 3)})
        return out

    def _gen_body(self, seg: dict[str, Any], shot: dict[str, Any], n: int) -> dict[str, Any]:
        d = self.director
        s = self._shot(shot["key"])
        return {"prompt": still_prompt(s, d["picks"], d.get("era"), self.spec["visuals"].get("look") or ""),
                "negative": self.spec["visuals"].get("negative") or None, "aspect": self._aspect(), "count": d.get("takes", 1),
                "seed": _shot_seed(self.spec, s, n, "seed", 100_000), "engine": self.spec.get("engine")}

    def stage_visuals(self) -> None:
        segments = self._segments()
        entry = self.state["done"].setdefault("visuals", {"complete": False, "items": {}})
        entry["plan"] = self._plan()
        items = self.items("visuals")
        keep = {p["key"] for p in entry["plan"]}
        for key in [k for k in items if k not in keep]:
            items.pop(key)
        pending = self.pending("visuals")
        for n, shot in enumerate(entry["plan"]):
            key = shot["key"]
            if key in items or key in pending:
                continue
            pending[key] = self.studio.generate(self.project_id, self._gen_body(segments[shot["segment"]], shot, n))["id"]
            self.save()
        history = entry.setdefault("history", {})

        def done(key: str, job: dict[str, Any]) -> None:
            ids = prod._asset_ids(job)
            if ids:
                items[key] = {"asset_id": ids[0], "source": "generate", "kind": "image"}
                hist = history.setdefault(key, [])
                hist.append({"asset_ids": ids, "at": now_iso()})
                del hist[:-prod.TAKES_KEPT]
                self.log("generated_shot", key=key, asset_id=ids[0], takes=len(ids))

        self.wait_jobs("visuals", done, "pictures")
        missing = [s["key"] for s in entry["plan"] if s["key"] not in items]
        if missing:
            raise DirectorError("missing_visuals", f"no picture for shot(s) {', '.join(missing)}")
        entry["complete"] = True

    def _clip_targets(self) -> list[str]:
        plan_keys = [p["key"] for p in (self.state["done"].get("visuals") or {}).get("plan") or []]
        items = self.items("visuals")
        return [k for k in plan_keys if k in items and self._shot(k).get("animate", True)]

    def stage_animatic(self) -> Optional[str]:
        out = super().stage_animatic()
        anim = self.state["done"].get("animatic") or {}
        if anim.get("plan"):
            per = {"ltx23": 4.0, "wan": 3.0}.get(self.director.get("engine"), 6.0)
            anim["plan"]["gpu_minutes"] = round(len(anim["plan"].get("clip_keys") or []) * per, 1)
        return out

    def stage_clips(self) -> None:
        targets = self._clip_targets()
        entry = self.state["done"].setdefault("clips", {"complete": False, "items": {}})
        items = self.items("clips")
        for key in [k for k in items if k not in targets]:
            items.pop(key)
        if not targets:
            entry["complete"] = True
            return
        d = self.director
        plan_by = {p["key"]: p for p in self.state["done"]["visuals"]["plan"]}
        stills = self.items("visuals")
        order = [p["key"] for p in self.state["done"]["visuals"]["plan"]]
        ends = entry.setdefault("ends", {})
        pending = self.pending("clips")
        for n, key in enumerate(targets):
            if key in items or key in pending:
                continue
            shot = self._shot(key)
            pos = order.index(key)
            end = None
            if d.get("end_frames") and pos + 1 < len(order):
                end = (stills.get(order[pos + 1]) or {}).get("asset_id")
            body = video_body(d.get("engine", "auto"), stills[key]["asset_id"], clip_prompt(shot, d["picks"], d.get("era")),
                              plan_by[key]["duration_s"] + 0.4, _shot_seed(self.spec, shot, pos, "clip_seed", 200_000), end)
            if end and body.get("end_asset_id"):
                ends[key] = end
            else:
                ends.pop(key, None)
            pending[key] = self.studio.generate(self.project_id, body)["id"]
            self.save()

        def done(key: str, job: dict[str, Any]) -> None:
            ids = prod._asset_ids(job)
            if ids:
                items[key] = ids[0]
                prod.remember_take(self.state, "clips", key, {"asset_id": ids[0]})
                self.log("clip", key=key, asset_id=ids[0])

        self.wait_jobs("clips", done, "clips")
        entry["complete"] = True

    def _sfx_voice(self, voice_path: Path) -> Optional[Path]:
        """The narration with a soft whoosh on every cut (synthesised, no
        samples); None when there is nothing to add."""
        plan_ = (self.state["done"].get("visuals") or {}).get("plan") or []
        cuts = [p["start_s"] for p in plan_[1:]]
        if not cuts:
            return None
        samples, sr = vp._wav_bytes_to_float(voice_path.read_bytes())
        duration = len(samples) / sr
        events = [{"kind": "whoosh", "t": max(0.0, t - 0.28), "dur": 0.55, "pan": (-0.3 if i % 2 else 0.3), "level": 0.8}
                  for i, t in enumerate(cuts)]
        effects = promo_sound.synthesise(events, duration, pulse=False).mean(axis=0)
        effects = vp._resample(effects.astype(np.float32), promo_sound.SR, sr)[:len(samples)]
        mixed = samples.copy()
        mixed[:len(effects)] += 0.22 * effects
        peak = float(np.max(np.abs(mixed))) or 1.0
        if peak > 0.98:
            mixed *= 0.98 / peak
        tmp = self.store.data_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        out = tmp / f"voice_sfx_{new_id('x')}.wav"
        out.write_bytes(shorts.wav_bytes_mono(mixed.astype(np.float32), sr))
        return out

    def stage_mix(self) -> None:
        if not self.director.get("sfx"):
            return super().stage_mix()
        narration = self.state["done"]["narration"]
        music = self.state["done"].get("music") or {}
        spec_music = self.spec.get("music") or {}
        voice_path = self.store.data_dir / self.store.get_asset(narration["asset_id"])["file_path"]
        music_path = self.store.data_dir / self.store.get_asset(music["asset_id"])["file_path"] if music.get("asset_id") else None
        with_sfx = self._sfx_voice(voice_path)
        tmp = self.store.data_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        out = tmp / f"mix_{new_id('x')}.m4a"
        try:
            soundtrack.mix(with_sfx or voice_path, music_path, out, narration["duration_s"],
                           spec_music.get("volume_db", soundtrack.DEFAULT_MUSIC_DB), spec_music.get("duck", "normal"))
            asset = engine.import_asset(
                self.store, self.project_id, out, kind_hint="audio", source="derived", tags=["soundtrack", "director"],
                recipe={"operation": "short_mix", "production": self.slug,
                        "input_asset_ids": [a for a in (narration["asset_id"], music.get("asset_id")) if a],
                        "effects": "whoosh on every cut" if with_sfx else None, "music_db": spec_music.get("volume_db"),
                        "duck": spec_music.get("duck"), "loudness_lufs": soundtrack.TARGET_LUFS, "created_at": now_iso()})
        except soundtrack.MixError as exc:
            raise DirectorError("mix_failed", str(exc)) from None
        finally:
            out.unlink(missing_ok=True)
            if with_sfx:
                with_sfx.unlink(missing_ok=True)
        self.store.update_asset(asset["id"], name=f"Soundtrack - {self.state['done']['script'].get('title') or self.state['name']}"[:100])
        self.state["done"]["mix"] = {"asset_id": asset["id"], "duration_s": asset.get("duration_s"), "sfx": bool(with_sfx)}
        self.log("mix", asset_id=asset["id"], music=music.get("asset_id"), sfx=bool(with_sfx))

    def build_tracks(self, aspect: str, use_clips: bool = True, audio_duration: Optional[float] = None) -> list[dict[str, Any]]:
        tracks = super().build_tracks(aspect, use_clips, audio_duration)
        if self.director.get("end_frames") and use_clips:
            # a clip that ends on the next still is cut from its first frame,
            # so the move lands where the next shot starts
            for clip in tracks[0]["clips"]:
                if clip["kind"] == "video":
                    clip["trim_start_s"] = 0.0
        return tracks
