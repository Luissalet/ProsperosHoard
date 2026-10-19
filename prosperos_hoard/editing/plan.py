"""Edit plans from plain language: «quita los silencios, ponle subtítulos y hazlo vertical» becomes an editable list of
steps (timeline operations, smart commands and exports) that the person reviews before anything changes.

The local model writes the plan when one is reachable; otherwise (or when it fails) a rule-based reader understands
the common requests in Spanish and English and says which parts it did not understand."""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, Field

from . import analyze
from . import commands
from . import media as media_store
from . import projects as project_store
from . import subtitles as subtitles_mod
from .errors import LumiereError, NotFound
from .ops import OP_NAMES, PRESETS, apply_ops, parse_op
from .render.runner import EXPORTS
from .timeline import TRANSITIONS
from .util import clip, dumps, ms_to_tc, new_id, parse_time

if TYPE_CHECKING:
    from .jobs import JobCtx
    from .services import Services

log = logging.getLogger("lumiere.plan")


class Step(BaseModel):
    kind: str = Field(..., pattern="^(op|command|export)$")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    explain: str = ""


class PlanOut(BaseModel):
    steps: list[Step] = Field(default_factory=list, max_length=40)
    notes: str = ""


COMMAND_DOCS = {
    "remove_silences": "args {threshold_db?: number (default auto), min_silence_ms?: 500, margin_ms?: 150, mode?: 'cut'|'speed', speed?: 4} — jump cuts",
    "remove_fillers": "args {extra?: [words], repeats?: true, strict?: false} — removes eh/um/o sea/like... (needs a transcript)",
    "cut_words": "args {media, text?: 'exact phrase', word_ids?: [...], keep?: false} — text-based edit on the transcript",
    "split_scenes": "args {mode?: 'split'|'markers'} — cut or mark at scene changes",
    "reframe": "args {aspect: '9:16'|'1:1'|'4:5'|'16:9', mode?: 'auto'|'track'|'stable'|'center'} — new canvas, keeps the subject in frame",
    "captions": "args {enabled?: true, style?: 'clean'|'bold'|'karaoke'|'pop'|'boxed'|'minimal', props?: {position, uppercase, max_words, highlight}}",
    "beat_sync": "args {music: media id, source?: video media id, beats_per_cut?: 2, mode?: 'scenes'|'clips'} — rebuild the main track on the beat",
    "match_loudness": "args {target_lufs?: -16} — same loudness for every clip",
    "zoom_cuts": "args {scale?: 1.12, every?: 2} — punch in on every other clip so jump cuts look like camera changes",
    "speaker_cut": "args {speakers: [names or ids like 'S1'], media?, keep?: false} — remove (or keep only) what the given speakers say (needs media_analyze kinds=['speakers'])",
    "multicam_create": "args {media: [2+ media ids recorded at the same time], name?, offsets?: {media: ms}, master?, angle?} — sync them by audio and build a multicam group",
    "multicam_resync": "args {group?, offsets?: {media: start_ms}} — measure the group's sync again or set an angle's offset by hand",
    "multicam_auto": "args {group?, mode?: 'loudness'|'speakers', min_shot_ms?: 2000, hysteresis_db?: 4, wide?: angle} — cut between the cameras by who is speaking",
    "script_assemble": "args {media, script?: text, script_path?: file, take?: 'last'|'best', mode?: 'auto'|'words'|'meaning'} — rough cut of a recording read from a script: one take per segment",
    "music_add": "args {path?: audio file, media?: library audio id, start_ms?, volume_db?: -8, duck?: true, fade_in_ms?: 600, fade_out_ms?: 2500} — background music trimmed to the edit's length (music_pick ranks the tracks of a folder)",
}

OP_DOCS = """Timeline operations (times in ms or '1:23.5'; clip / track / media ids from the context):
add_media {media, track?, at?, src_in?, src_out?, length? (images), mode?: append|insert|overwrite}
add_text {text, start, length, style?: {size, color '#RRGGBB', position: top|middle|bottom, animation: none|fade|pop|slide_up|typewriter, box?: '#RRGGBBAA'}}
split {at, clip?}   trim {clip, src_in?, src_out?, length?}   move {clip, start?, track?}   delete {clips: [...], ripple?: true}
delete_range {start, end, tracks?}  (ripple: later material moves left)
cut_source {media, ranges: [[from, to], ...]} (source times)   keep_source {media, ranges}
set {clip, props: {volume_db, mute, fade_in, fade_out, audio_fade_in, audio_fade_out, transform: {x, y, scale, rotation, opacity, fit: contain|cover|fill}, crop, text, style, label, slot}}
speed {clip, speed}   transition {clip? | all_cuts: true, type, dur}   filter_add {clips? | track?, type, params}   filter_remove {clip, type?}
canvas {preset? | width, height, fps, background, length_mode?: main|longest}   marker_add {t, label, kind?}   captions {enabled, style, props}
track_add {kind: video|audio|text, name, role?: overlay|voice|music|sfx|titles}   track_set {track, props: {muted, hidden, locked, volume_db, duck}}
detach_audio {clip}   duplicate {clip}   close_gaps {track?}   keyframes {clip, prop: x|y|scale|opacity|rotation|volume_db|mask_x|mask_y|mask_w|mask_h|mask_feather|brightness (-1..1)|saturation (0..3; both animate the clip's eq effect), keys: [{t, v, ease}]}
speed_ramp {clip, preset?: speed_up|slow_down|ease_in_out|hit|clear, speed?, at? (hit: timeline time), keys?: [{t: source ms from the clip's in point, v, ease}]}
mask {clip | clips, shape?: rectangle|rounded|ellipse, x?, y?, w?, h? (fractions of the clip's picture), radius?, feather?, invert?, remove?}
add_sequence {project (prj_...: another project used as one clip), at?, track?, src_in?, src_out?}
unnest {clip} (a sequence clip: its clips come back onto this timeline)
multicam_create {angles: [{media, start?, label?}], name?, master?, angle?, at?}  (build a group from synced recordings; the sound is one continuous master)
multicam_switch {angle, at?, end?}  or  {angle, clip}  or  {cuts: [[ms, angle], ...]}  (show another camera for a range; angle = 1-based number, label or media id)
multicam_set {group?, offsets?: {angle: start_ms}, master?, name?, release?}  (fix an angle's sync by hand, change the master sound, dissolve the group)
add_overlay {media, start, length, src_in?, track?, fit?: cover, mute?: true, fade_in?, fade_out?} (b-roll over a range, on a free overlay track)
fill_slot {slot, media, src_in?, src_out?, length?, rule?: auto|keep|full} (replace the media of the clip marked set {props: {slot: 'intro'}}; slots named main* take the media's full length and the rest moves)
Transitions: """ + ", ".join(TRANSITIONS) + """
Effects (filter_add type): eq{brightness, contrast, saturation, gamma}, grayscale, sepia, vintage, warm{amount}, cool{amount}, contrast_pop{amount},
vignette{strength}, blur{radius}, sharpen{amount}, denoise{strength}, pixelate{size}, chromakey{color, similarity, blend}, hflip, vflip, lut{file},
audio_denoise{strength}, voice_enhance, highpass{hz}, lowpass{hz}, compressor{threshold_db, ratio}, pitch{semitones}, echo{delay_ms, decay}
Canvas presets: """ + ", ".join(PRESETS)

SYSTEM = """You are the edit planner of a local video editor. Turn the person's request into a short list of steps over THEIR timeline.
Answer with JSON only: {"steps": [{"kind": "op"|"command"|"export", "name": "...", "args": {...}, "explain": "one short sentence in the person's language"}], "notes": "what you could not do or assumed"}.
- Use only the ids in the context. Never invent media, clips or tracks. Times in milliseconds or '1:23.5'.
- Prefer commands for smart work (silences, fillers, captions, reframe, beat sync); ops for precise changes.
- export steps: name = preset (""" + ", ".join(EXPORTS) + """), args {start?, end?, formats?: ['16:9','9:16','1:1'] (one file per canvas in ONE export, the project is not changed; use it instead of
  a reframe step when the person wants several shapes), reframe?: 'auto'|'center'|'blur', captions_language?: 'en' (burn the subtitles translated into
  that language), captions_dual?: true (original and translation)}. Only export when asked.
- Keep the order that makes sense: cuts before captions, reframe before titles, export last.
- If something cannot be done, leave it out and say so in notes. Do not explain beyond that."""


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def context(svc: "Services", project_id: str, budget: int = 9000) -> str:
    out = project_store.outline(svc, project_id)
    p = project_store.doc(svc, project_id)
    lines = [f"Canvas {out['canvas']['width']}x{out['canvas']['height']} @ {out['canvas']['fps']} fps; duration {out['duration']} ({out['duration_ms']} ms)."]
    for t in out["tracks"]:
        lines.append(f"Track {t['id']} ({t['kind']}, {t['role']}, '{t['name']}'){' muted' if t['muted'] else ''}{' locked' if t['locked'] else ''}:")
        for c in t["clips"][:60]:
            desc = f"  clip {c['id']} {c['start']}–{c['end']} ({c['start_ms']}–{c['end_ms']} ms)"
            if "text" in c:
                desc += f" text «{c['text']}»"
            else:
                desc += f" {'sequence' if c.get('sequence') else 'media'} {c['media']} «{c['name']}» src {c['src']}"
                for k in ("speed", "speed_curve", "mask", "mute", "fit", "effects", "transition"):
                    if k in c:
                        desc += f" {k}={c[k]}"
            lines.append(desc)
        if len(t["clips"]) > 60:
            lines.append(f"  … {len(t['clips']) - 60} more clips")
    for g in p.multicams:
        lines.append(f"Multicam group {g.id} «{g.name}»: " + "; ".join(f"angle {i + 1} {a.label} = media {a.media}{' (sound only)' if a.audio_only else ''}"
                                                                   for i, a in enumerate(g.angles)) + f"; master sound = angle {g.master + 1}.")
    lines.append(f"Captions: {'on, ' + p.captions.style if p.captions.enabled else 'off'}.")
    used = p.media_ids()
    lines.append("Media on the timeline:")
    for mid in sorted(used):
        info = media_store.lookup(svc, mid)
        if not info:
            continue
        lines.append(f"  {mid} «{info['name']}» {info['kind']} {ms_to_tc(info['duration_ms'])} {info['width']}x{info['height']} "
                     f"audio={'yes' if info['has_audio'] else 'no'} analysis={','.join(info['analysis']) or 'none'}")
        tr = analyze.transcript(svc, mid)
        if tr:
            words = tr["words"]
            excerpt = []
            for i in range(0, len(words), 12):
                chunk = words[i: i + 12]
                excerpt.append(f"[{ms_to_tc(chunk[0]['t0'])}] " + " ".join(w["text"] for w in chunk))
                if sum(len(x) for x in excerpt) > 2500:
                    excerpt.append("…")
                    break
            lines.append("    transcript: " + "\n    ".join(excerpt))
    others = [m for m in media_store.list_media(svc, limit=40) if m["id"] not in used]
    if others:
        lines.append("Other media in the library: " + "; ".join(f"{m['id']} «{clip(m['name'], 40)}» {m['kind']} {ms_to_tc(m['duration_ms'])}"
                                                               for m in others[:25]))
    text = "\n".join(lines)
    return text[:budget]


def _parse(data: Any) -> PlanOut:
    if isinstance(data, list):
        data = {"steps": data}
    plan = PlanOut.model_validate(data)
    for s in plan.steps:
        if s.kind == "op" and s.name not in OP_NAMES:
            raise ValueError(f"unknown op {s.name}")
        if s.kind == "command" and s.name not in commands.COMMANDS:
            raise ValueError(f"unknown command {s.name}")
        if s.kind == "export" and s.name not in EXPORTS:
            raise ValueError(f"unknown export preset {s.name}")
    return plan


def model_plan(svc: "Services", project_id: str, instruction: str) -> tuple[PlanOut, Optional[str]]:
    from .generate import chat_json

    messages = [{"role": "system", "content": SYSTEM + "\n\n" + OP_DOCS + "\n\nCommands:\n" + "\n".join(f"{k}: {v}" for k, v in COMMAND_DOCS.items())},
                {"role": "user", "content": f"CONTEXT\n{context(svc, project_id)}\n\nREQUEST\n{instruction}"}]
    return chat_json(svc, messages, lambda data, strict: _parse(data), max_tokens=2500, effort="off")


# ---------------------------------------------------------------- rules

_NUM = r"(\d+(?:[.,]\d+)?)"
_UNIT = r"\s*(ms|milisegundos?|s|seg(?:undos?)?|seconds?|secs?|min(?:utos?)?|minutes?)?"


def _to_ms(num: str, unit: Optional[str]) -> int:
    value = float(num.replace(",", "."))
    unit = (unit or "s").lower()
    if unit.startswith("ms") or unit.startswith("mili"):
        return int(value)
    if unit.startswith("min"):
        return int(value * 60000)
    return int(value * 1000)


def rules_plan(svc: "Services", project_id: str, instruction: str) -> PlanOut:
    text = _fold(instruction)
    p = project_store.doc(svc, project_id)
    dur = p.duration
    main = p.main_track()
    main_clips = [c for c in (main.clips if main else []) if c.type == "media"]
    steps: list[Step] = []
    understood: list[str] = []

    def add(kind: str, name: str, args: dict, explain: str, key: str) -> None:
        steps.append(Step(kind=kind, name=name, args=args, explain=explain))
        understood.append(key)

    m = re.search(rf"(?:primeros?|first|inicio|principio|start)\D{{0,12}}{_NUM}{_UNIT}", text) or re.search(
        rf"{_NUM}{_UNIT}\s*(?:primeros?|iniciales|del principio|del inicio|from the start|at the start)", text)
    if m and re.search(r"recort|cort|quit|elimin|borr|trim|cut|remove", text):
        ms = _to_ms(m.group(1), m.group(2))
        add("op", "delete_range", {"start": 0, "end": min(ms, max(0, dur - 40))}, f"Quitar los primeros {ms / 1000:g} s", "start")
    m = re.search(rf"(?:ultimos?|last|final)\D{{0,12}}{_NUM}{_UNIT}", text)
    if m and re.search(r"recort|cort|quit|elimin|borr|trim|cut|remove", text):
        ms = _to_ms(m.group(1), m.group(2))
        add("op", "delete_range", {"start": max(0, dur - ms), "end": dur}, f"Quitar los últimos {ms / 1000:g} s", "end")
    if re.search(r"silenci|jump ?cut|pausas|silence|dead air", text):
        mode = "speed" if re.search(r"aceler|speed up|rapido", text) else "cut"
        add("command", "remove_silences", {"mode": mode}, "Quitar los silencios" if mode == "cut" else "Acelerar los silencios", "silences")
    if re.search(r"muletilla|filler|\behs?\b|\bums?\b|\beh+\b|coletilla|vicios del habla", text):
        add("command", "remove_fillers", {}, "Quitar las muletillas", "fillers")
    if re.search(r"escena|scene", text):
        add("command", "split_scenes", {"mode": "markers" if re.search(r"marca|marker", text) else "split"}, "Cortar en los cambios de escena", "scenes")
    # several shapes in ONE export (copies of the project; nothing is reframed in the project itself)
    formats: Optional[list[str]] = None
    if re.search(r"varios formatos|todos los formatos|multi ?formato|distintos formatos|diferentes formatos|several formats|all (?:the )?formats|"
                 r"(?:16[:x/]9|9[:x/]16|1[:x/]1|4[:x/]5)\s*(?:,|y|and|e|\+)\s*(?:16[:x/]9|9[:x/]16|1[:x/]1|4[:x/]5)", text):
        named = [a for a, pat in (("16:9", r"16[:x/]9|horizontal|youtube"), ("9:16", r"9[:x/]16|vertical|reels?|tiktok|shorts?"),
                                  ("1:1", r"1[:x/]1|cuadrad|square"), ("4:5", r"4[:x/]5")) if re.search(pat, text)]
        formats = named if len(named) >= 2 else ["16:9", "9:16", "1:1"]
    # subtitles translated into a language, burned into the export
    language: Optional[str] = None
    if re.search(r"traduc|translat|subtitul\w*\s+(?:en|al|in|to|into)\s|subtitles?\s+(?:in|into)\s|bilingu|dos idiomas|two languages", text):
        for code, (name, aliases) in subtitles_mod.LANGUAGES.items():
            if any(re.search(rf"\b{re.escape(_fold(word))}\b", text) for word in (name, *aliases)):
                language = code
                break
    aspect = None
    if formats:
        pass
    elif re.search(r"vertical|9[:x/]16|reels?|tiktok|shorts?|historia|stories", text):
        aspect = "9:16"
    elif re.search(r"cuadrad|1[:x/]1|square", text):
        aspect = "1:1"
    elif re.search(r"4[:x/]5", text):
        aspect = "4:5"
    elif re.search(r"horizontal|16[:x/]9|apaisad|landscape", text):
        aspect = "16:9"
    if aspect:
        mode = "center" if re.search(r"centr", text) else ("stable" if re.search(r"fij|estatic|stable|still", text) else "auto")
        if re.search(r"desenfoc|borros|blur|sin recortar|entero|completo", text):
            mode = "blur"
        add("command", "reframe", {"aspect": aspect, "mode": mode}, f"Formato {aspect} siguiendo al sujeto", "aspect")
    if re.search(r"subtitul|caption|rotul.*habla|texto de lo que dice", text):
        style = next((s for s in ("karaoke", "pop", "boxed", "minimal", "clean", "bold") if s in text), None)
        if not style:
            style = "boxed" if re.search(r"caja|fondo", text) else ("pop" if re.search(r"tiktok|reels?|dinamic|palabra a palabra", text) else "bold")
        add("command", "captions", {"enabled": True, "style": style}, f"Subtítulos estilo {style}", "captions")
    m = re.search(rf"(?:x|por|a)\s*{_NUM}\s*(?:de velocidad|x|veces)?", text) if re.search(r"aceler|velocidad|speed|rapido|timelapse", text) else None
    if re.search(r"camara lenta|slow ?mo|a camara lenta|ralenti", text):
        for c in main_clips:
            steps.append(Step(kind="op", name="speed", args={"clip": c.id, "speed": 0.5}, explain=f"Cámara lenta en {c.id}"))
        understood.append("speed")
    elif m and "silenci" not in text:
        factor = float(m.group(1).replace(",", "."))
        if 0.1 <= factor <= 16:
            for c in main_clips:
                steps.append(Step(kind="op", name="speed", args={"clip": c.id, "speed": factor}, explain=f"Velocidad ×{factor:g} en {c.id}"))
            understood.append("speed")
    if re.search(r"punch|zoom.{0,12}(cortes|cuts)|acerc.{0,20}cortes|zooms?\b", text) and len(main_clips) > 1:
        add("command", "zoom_cuts", {"scale": 1.12, "every": 2}, "Zoom alterno en los cortes", "zoom")
    if re.search(r"normaliz|iguala.*(volumen|audio|sonido)|mismo volumen|loudness", text):
        add("command", "match_loudness", {"target_lufs": -16}, "Igualar el volumen de los clips", "loudness")
    if re.search(r"transicion|fundid|crossfade|transition|dissolve", text) and len(main_clips) > 1:
        kind = "crossfade"
        for word, t in (("negro", "fade_black"), ("black", "fade_black"), ("blanco", "fade_white"), ("cortinilla", "wipe_left"), ("wipe", "wipe_left"),
                        ("desliz", "slide_left"), ("slide", "slide_left"), ("zoom", "zoom_in"), ("circul", "circle_open"), ("pixel", "pixelize")):
            if word in text:
                kind = t
                break
        add("op", "transition", {"all_cuts": True, "type": kind, "dur": 500}, f"Transición {kind} en cada corte", "transitions")
    effects = [("blanco y negro", "grayscale", {}), ("b/n", "grayscale", {}), ("black and white", "grayscale", {}), ("grayscale", "grayscale", {}),
               ("sepia", "sepia", {}), ("vintage", "vintage", {}), ("retro", "vintage", {}), ("calid", "warm", {"amount": 0.6}),
               ("warm", "warm", {"amount": 0.6}), ("frio", "cool", {"amount": 0.6}), ("cool", "cool", {"amount": 0.6}),
               ("contraste", "contrast_pop", {"amount": 0.6}), ("mas color", "contrast_pop", {"amount": 0.6}), ("vineta", "vignette", {"strength": 0.5}),
               ("vignette", "vignette", {"strength": 0.5}), ("ruido del audio", "audio_denoise", {"strength": 12}),
               ("reduce el ruido", "audio_denoise", {"strength": 12}), ("ruido de fondo", "audio_denoise", {"strength": 12}),
               ("mejora la voz", "voice_enhance", {}), ("voz mas clara", "voice_enhance", {}), ("enhance voice", "voice_enhance", {})]
    seen: set[str] = set()
    for word, kind, params in effects:
        if word in text and kind not in seen:
            seen.add(kind)
            add("op", "filter_add", {"type": kind, "params": params}, f"Efecto {kind}", "effect")
    if re.search(r"(quita|silencia|sin)\s+(el\s+)?(audio|sonido)|mute", text):
        for c in main_clips:
            steps.append(Step(kind="op", name="set", args={"clip": c.id, "props": {"mute": True}}, explain=f"Silenciar {c.id}"))
        understood.append("mute")
    for quoted in re.findall(r"[«\"“']([^»\"”']{2,120})[»\"”']", instruction):
        if re.search(r"titul|texto|rotul|title|text", text):
            add("op", "add_text", {"text": quoted, "start": 0, "length": 3000, "style": {"size": 96, "animation": "pop"}},
                f"Título «{quoted}» al principio", "title")
    if re.search(r"musica|music|cancion|song", text):
        library = media_store.list_media(svc, kind="audio", limit=200)
        pick = next((m for m in library if _fold(m["name"]) and _fold(m["name"]) in text), None)
        if pick and re.search(r"ritmo|beat|al compas|sincron", text):
            add("command", "beat_sync", {"music": pick["id"], "beats_per_cut": 2, "mode": "clips"}, f"Cortar al ritmo de «{pick['name']}»", "music")
        elif pick:
            add("op", "add_media", {"media": pick["id"], "at": 0, "mode": "overwrite"}, f"Música «{pick['name']}» desde el principio", "music")
            music_track = next((t for t in p.tracks if t.kind == "audio" and t.role == "music"), None)
            if music_track:
                steps.append(Step(kind="op", name="track_set", args={"track": music_track.id, "props": {"duck": True, "volume_db": -8}},
                                  explain="La música baja cuando se habla"))
    export = None
    if re.search(r"exporta|renderiz|render|descarg|saca el video|export", text) or formats or language:
        export = "final"
        for word, preset in (("gif", "gif"), ("mp3", "audio_mp3"), ("wav", "audio_wav"), ("prores", "master"), ("hevc", "hevc"), ("h265", "hevc"),
                             ("ligero", "web"), ("whatsapp", "web"), ("720", "web"), ("preview", "preview"), ("previa", "preview")):
            if word in text:
                export = preset
                break
        args: dict[str, Any] = {}
        explain = f"Exportar ({EXPORTS[export]['label']})"
        if formats and not EXPORTS[export].get("audio_only"):
            args["formats"] = formats
            if re.search(r"desenfoc|borros|blur|sin recortar|entero|completo", text):
                args["reframe"] = "blur"
            elif re.search(r"centr", text):
                args["reframe"] = "center"
            explain += f" en {', '.join(formats)} a la vez"
        if language and not EXPORTS[export].get("audio_only"):
            args["captions_language"] = language
            if re.search(r"bilingu|dual|dos idiomas|doble|both languages|two languages", text):
                args["captions_dual"] = True
            explain += f" con subtítulos traducidos ({language})"
        add("export", export, args, explain, "export")
    notes = "" if steps else "No he entendido ninguna acción concreta. Prueba con frases como «quita los silencios», «hazlo vertical», «pon subtítulos», «recorta los primeros 5 segundos» o «exporta en 720p»."
    return PlanOut(steps=steps, notes=notes)


# ---------------------------------------------------------------- store / apply

def create(svc: "Services", project_id: str, instruction: str, *, use_model: bool = True) -> dict[str, Any]:
    project_store.doc(svc, project_id)
    instruction = (instruction or "").strip()
    if not instruction:
        raise LumiereError("Write what you want done to the video.")
    source, notes, model = "rules", "", None
    plan: Optional[PlanOut] = None
    if use_model:
        try:
            plan, model = model_plan(svc, project_id, instruction)
            source = "model"
        except Exception as error:  # noqa: BLE001 - fall back to the rules on any model problem
            notes = f"Sin modelo ({clip(str(error), 160)}): plan por reglas. "
    if plan is None or not plan.steps:
        rules = rules_plan(svc, project_id, instruction)
        if plan is None or (not plan.steps and rules.steps):
            plan = rules
            source = "rules" if not plan.steps or source != "model" else source
    steps = [s.model_dump() for s in plan.steps]
    for s in steps:
        s["id"] = new_id("stp", 5)
        s["enabled"] = True
    pid = new_id("pln")
    svc.db.execute("INSERT INTO plans(id, project_id, instruction, steps, source, notes, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (pid, project_id, clip(instruction, 2000), dumps(steps), source, clip(notes + (plan.notes or ""), 2000), time.time()))
    return get(svc, pid) | {"model": model}


def get(svc: "Services", plan_id: str) -> dict[str, Any]:
    row = svc.db.one("SELECT * FROM plans WHERE id = ?", (plan_id,))
    if row is None:
        raise NotFound(f"No plan {plan_id}.")
    return {"id": row["id"], "project": row["project_id"], "instruction": row["instruction"], "steps": json.loads(row["steps"]), "source": row["source"],
            "notes": row["notes"], "state": row["state"], "created_ts": row["created_ts"], "applied_ts": row["applied_ts"]}


def list_plans(svc: "Services", project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return [get(svc, r["id"]) for r in svc.db.query("SELECT id FROM plans WHERE project_id = ? ORDER BY created_ts DESC LIMIT ?", (project_id, limit))]


def update(svc: "Services", plan_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    plan = get(svc, plan_id)
    if plan["state"] != "draft":
        raise LumiereError("Only a draft plan can be edited.")
    clean = []
    for raw in steps:
        s = Step.model_validate({k: raw[k] for k in ("kind", "name", "args", "explain") if k in raw})
        _parse({"steps": [s.model_dump()]})
        d = s.model_dump()
        d["id"] = raw.get("id") or new_id("stp", 5)
        d["enabled"] = bool(raw.get("enabled", True))
        clean.append(d)
    svc.db.execute("UPDATE plans SET steps = ? WHERE id = ?", (dumps(clean), plan_id))
    return get(svc, plan_id)


def discard(svc: "Services", plan_id: str) -> dict[str, Any]:
    get(svc, plan_id)
    svc.db.execute("UPDATE plans SET state = 'discarded' WHERE id = ? AND state = 'draft'", (plan_id,))
    return get(svc, plan_id)


def apply(svc: "Services", plan_id: str, *, wait_analysis_s: float = 0) -> dict[str, Any]:
    """Run the enabled steps on a copy of the project and save the result as one undo step; exports are queued at the end.
    Missing analyses are queued; with wait_analysis_s the plan waits for them (in the background job), otherwise it stops
    and says which jobs to wait for."""
    plan = get(svc, plan_id)
    if plan["state"] != "draft":
        raise LumiereError(f"The plan is {plan['state']}.")
    pid = plan["project"]
    p = project_store.doc(svc, pid)
    look = project_store.media_lookup(svc)
    done: list[dict[str, Any]] = []
    exports: list[dict[str, Any]] = []
    for step in plan["steps"]:
        if not step.get("enabled", True):
            continue
        kind, name, args = step["kind"], step["name"], dict(step.get("args") or {})
        if kind == "export":
            exports.append({"preset": name, **args})
            continue
        if kind == "op":
            parse_op({"op": name, **args})
            p, res = apply_ops(p, [{"op": name, **args}], look)
            done.append({"step": step["id"], "explain": step.get("explain"), "result": res[0]})
            continue
        deadline = time.time() + wait_analysis_s
        while True:
            try:
                p, summary = commands.COMMANDS[name](svc, p, **args)
                done.append({"step": step["id"], "explain": step.get("explain"), "result": summary})
                break
            except commands.NeedsAnalysis as need:
                if time.time() >= deadline:
                    return {"plan": plan_id, "applied": False, "needs": need.jobs, "message": str(need), "done_before_stop": done}
                _wait_jobs(svc, [j.get("job") for j in need.jobs if j.get("job")], deadline)
            except TypeError as error:
                raise LumiereError(f"{name}: {error}") from error
    rev = None
    if done:
        rev = project_store.save(svc, pid, p, f"Plan: {clip(plan['instruction'], 60)}", actor="plan")
    queued = [svc.start_render(pid, preset=e.pop("preset"), **e) for e in exports]
    svc.db.execute("UPDATE plans SET state = 'applied', applied_ts = ? WHERE id = ?", (time.time(), plan_id))
    return {"plan": plan_id, "applied": True, "rev": rev, "steps": done, "renders": [{"job": j["id"], "label": j["label"]} for j in queued],
            "duration": ms_to_tc(p.duration)}


def _wait_jobs(svc: "Services", job_ids: list[str], deadline: float) -> None:
    while time.time() < deadline:
        states = [svc.jobs.get(j)["state"] for j in job_ids]
        if all(s in ("done", "failed", "canceled") for s in states):
            failed = [j for j, s in zip(job_ids, states) if s != "done"]
            if failed:
                raise LumiereError("An analysis the plan needs failed: " + "; ".join(svc.jobs.get(j)["error"] for j in failed))
            return
        time.sleep(0.5)


def apply_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    ctx.progress(0.05, "plan", force=True)
    return apply(svc, ctx.params["plan"], wait_analysis_s=float(ctx.params.get("wait_s", 1800)))


def quick_time(value: Any) -> int:
    return parse_time(value)
