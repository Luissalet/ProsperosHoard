#!/usr/bin/env python3
"""Produces "NO MIRES ATRÁS" by FAROL end to end, driving Prospero's Hoard
**only through its MCP adapter** (spawns `mcp_server.py` over stdio, the
same path Faustus uses) - never by importing `engine`/`store` to mutate
data directly.

Usage (from the repo root):

    # against the bundled fake ComfyUI, entirely local and fast
    .venv/bin/python scripts/productions/no_mires_atras.py --backend fake --quality draft

    # against a real, already-running Prospero + ComfyUI
    .venv/bin/python scripts/productions/no_mires_atras.py --backend real \\
        --app-url http://127.0.0.1:8815 --quality final

    # pin the image engine instead of letting "auto" reach for Qwen-Image
    # 2.1 first (see engine.resolve_image_engine)
    .venv/bin/python scripts/productions/no_mires_atras.py --backend fake --engine flux

    # re-run a single step once its inputs exist (e.g. after picking a
    # different canonical reference, or after reviewing the output)
    .venv/bin/python scripts/productions/no_mires_atras.py --backend fake --only stills

Steps: 1 project (sets the
--engine choice as the project's own image_engine), 2 character (FAROL
reference sheet -> front-view crop as the canonical), 3 song
(studio_compose, 2 seeds), 4 stills (12 shots: an edit template with the
canonical reference when FAROL is in frame, a fresh txt2img in the same
night look when not - both through --engine, auto reaching for Qwen-Image
2.1 first), 5 clips (Wan 2.2 TI2V from the best still of shots 1,2,3,5,7,
10,12), 6 photocards (a solo set: 5 idol looks, fronts, backs, contact
sheet), 7 album art (night variants: cover, tracklist back, teaser poster,
lyric card), 8 timeline (lyrics timed to the song's bars, a storyboard per
section, a shot per sung line, sodium-night grade, grain, vignette,
flash + glitch on the chorus downbeats, horror karaoke; 9:16 and 16:9,
preview then final), 9 REPORT.md.

--quality draft trims the scope (fewer seeds/variants/clips/looks, preview renders only)
so a full run finishes quickly; --quality final uses the full
numbers. Both produce every asset kind, so a draft run still exercises the whole pipeline.

Idempotency: step 1 finds the project by name through a real MCP call
(studio_projects). For everything else, this script keeps its own
checkpoint (`state.json` next to REPORT.md) recording the ids each step
produced - and, inside the long steps, each still / clip / card / render
as it finishes, so an interrupted GPU run resumes where it stopped. The
MCP surface has no generic "what have I already made for this
production" query, so this file is the script's own bookkeeping, not a
side channel into Prospero. `--only <step>` redoes one step from scratch;
delete state.json to start over.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "data" / "productions" / "no_mires_atras"
STATE_PATH = OUT_DIR / "state.json"
REPORT_PATH = OUT_DIR / "REPORT.md"

PROJECT_NAME = "NO MIRES ATRÁS"
CHARACTER_NAME = "FAROL"
ACCENT = "#F28C28"       # sodium orange, from the bible's palette
ACCENT_RED = "#A33A2E"   # faded smile red

STEP_NAMES = {
    1: "project", 2: "character", 3: "song", 4: "stills", 5: "clips",
    6: "photocards", 7: "album", 8: "timeline", 9: "report",
}
STEP_NUMBERS = {name: n for n, name in STEP_NAMES.items()}


# --------------------------------------------------------------- the brief

FAROL_LOOK = (
    "FAROL, a very tall and thin urban night creature about 2.3 meters tall, made of wet black paper "
    "and wire, its head an old oval paper street lantern (washi, amber) with rips, a candle flame "
    "visible inside, a crooked smile painted in faded red on the paper, long fingers like bent umbrella "
    "ribs, a torn too-long dark raincoat, never shown mid-stride, always still, always a little closer; "
    "palette sodium orange, wet asphalt dark grey, fog grey, faded smile red; quiet dread, cinematic, "
    "35mm, shallow depth of field, rain, fog, practical light only"
)
FAROL_NEGATIVE = "cartoon, cute, bright daylight, sunny, comic style, text, watermark, extra limbs, deformed hands"

REFERENCE_SHEET_PROMPT = (
    f"{FAROL_LOOK}, character turnaround reference sheet, three full-body poses side by side on a "
    "neutral grey seamless studio backdrop: front view, three-quarter view, back view, identical "
    "design and proportions in every pose, even studio lighting, reference photography"
)

# The idol cards are the fun contrast: a glossy studio shoot. The canonical
# reference already carries FAROL's look, so these describe only the shoot
# (repeating the night-street description here would drag rain and fog
# into a pastel studio). Roles parody an idol line-up; the accents are the
# set's pastel versions; the backs are creepy-sweet notes in Spanish.
IDOL_LOOKS = [
    {"role": "Visual", "message": "gracias por venir", "accent": "#F4A7C0",
     "prompt": "posing for a glossy idol photocard, bright studio, pastel pink seamless backdrop, holding a bouquet "
               "of wilted roses, soft beauty lighting, magazine retouching, the candle flame glowing warmly inside the lantern head"},
    {"role": "Main Rapper", "message": "abrígate, fuera hace frío", "accent": "#EADBC8",
     "prompt": "sitting on a wooden stool in a cream knit sweater for a glossy idol photocard, warm cream backdrop, "
               "the candle flame glowing softly inside the lantern head, soft beauty lighting, magazine retouching"},
    {"role": "Center", "message": "¡clic!", "accent": "#B8D8F0",
     "prompt": "making a peace sign with one long paper finger, photo-booth strip style, four small frames, bright "
               "flash, baby blue curtain backdrop, glossy idol photocard"},
    {"role": "Lead Vocal", "message": "te veo desde aquí", "accent": "#F6D98B",
     "prompt": "standing by a rainy window strung with warm fairy lights, glossy idol photocard, soft bokeh, soft "
               "beauty lighting, gentle smile painted on the lantern"},
    {"role": "Maknae", "message": "perdón por seguirte", "accent": "#C9B8E8",
     "prompt": "holding a small hand-written note that says \"sorry\" with both paper hands, lilac backdrop, glossy "
               "idol photocard, soft beauty lighting, magazine retouching"},
]

# The night look every still shares (Kontext keeps the character, this keeps the world).
NIGHT_LOOK = ("cinematic 35mm film still, night, sodium-vapour street lamps, wet asphalt, fog, light rain, shallow depth "
              "of field, practical light only, deep shadows, subtle film grain, no text, no readable signs, no logos")

# `farol`: whether the character is in frame. Shots without FAROL (the
# phone, the doormat, the dark bedroom) are generated fresh with Flux
# schnell in the same night look: handing Kontext FAROL's reference for a
# shot it must not appear in would put the creature in every frame.
SHOTS = [
    {"n": 1, "farol": True, "prompt": "standing perfectly still under a far flickering sodium lamp at the end of an empty "
                                      "Spanish-style street at 2:15 a.m., seen small in the distance, fog"},
    {"n": 2, "farol": True, "prompt": "seen over the shoulder of someone looking back down an empty wet street, standing "
                                      "under a closer sodium lamp, still, a little closer than before"},
    {"n": 3, "farol": True, "prompt": "extreme close-up of the paper lantern head: the crooked painted smile, the candle "
                                      "flame inside, raindrops beading on the wet paper"},
    {"n": 4, "farol": False, "prompt": "close-up of a phone held in a trembling hand at night, the screen's cold glow "
                                       "on a wet frightened face, rainy street blurred behind, the screen itself blank"},
    {"n": 5, "farol": True, "prompt": "reflected in the fogged glass of a night bus shelter, standing right beside the "
                                      "viewer's reflection, while the real bench beside the viewer is empty"},
    {"n": 6, "farol": True, "prompt": "only its long paper fingers curling over a stairwell rail two floors up, seen "
                                      "from below, dim stairwell light"},
    {"n": 7, "farol": True, "prompt": "seated perfectly still among empty plastic chairs in a laundromat at 3 a.m., "
                                      "washing machines spinning, flickering fluorescent tubes"},
    {"n": 8, "farol": True, "prompt": "an amber lantern glow in an elevator mirror, just behind the viewer's shoulder, "
                                      "the lantern head barely visible"},
    {"n": 9, "farol": False, "prompt": "close-up of a wet doormat outside a flat door at night, a trail of wet "
                                       "footprints that are not human, long and thin like bent umbrella ribs, leading to the door"},
    {"n": 10, "farol": True, "prompt": "seen from inside a dark flat through a rain-streaked window: standing on the "
                                       "empty street below, lantern lit, looking up at the window"},
    {"n": 11, "farol": True, "prompt": "a quick fragmented insert: flickering sodium lamps, the crooked painted smile, "
                                       "long paper fingers and the candle flame, extreme close framing"},
    {"n": 12, "farol": False, "prompt": "a dark bedroom a second after the light switch was turned off, a hand still on "
                                        "the switch, the room slowly filling with a sodium-orange glow from the window"},
]
CLIP_MOTION = {
    1: "light rain falling, the far lamp flickering, fog drifting; the tall figure under the lamp stands "
       "perfectly still and does not walk",
    2: "light rain falling, a subtle slow push-in; the figure under the lamp stays perfectly still",
    3: "the candle flame flickering gently inside the lantern, raindrops sliding down the paper",
    5: "rain streaking down the glass, a slow push-in; the reflected figure stays perfectly still",
    7: "a washing machine spinning, faint fluorescent flicker; the seated figure stays perfectly still",
    10: "rain on the window glass, the street lamp flickering; the figure on the street stays perfectly "
        "still, looking up",
    12: "the room slowly brightening into sodium orange, a very slow push-in",
}
# FAROL is "never shown mid-stride - always still, always a little closer".
# Wan's stock negative prompt pushes *away* from stillness (it lists
# "static" and "motionless frame"), so the shots where FAROL is visible
# get the stock negative without those terms, plus walking.
CLIP_STILL_FIGURE = {1, 2, 5, 7, 10}
CLIP_NEGATIVE_STILL = ("色调艳丽，过曝，细节模糊不清，字幕，风格，作品，画作，画面，整体发灰，最差质量，低质量，"
                       "JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，"
                       "形态畸形的肢体，手指融合，杂乱的背景，三条腿，背景人很多，倒着走，走路，迈步，"
                       "walking, stepping, striding, moving figure, turning around")

SONG_TAGS = ("dark trap, horror rap, eerie music box melody, detuned piano, heavy 808, half-time 140 "
             "bpm, whispered ad-libs, male rap vocals, spanish, minor key, cinematic, tape hiss, rain ambience")
SONG_PARAMS = {"bpm": 140, "key": "F# minor", "language": "es", "time_signature": 4, "duration": 120.0}
SONG_LYRICS = """[Intro]
(shh…)
Cuenta las farolas… una… dos…
Si parpadea la tercera… no fui yo.

[Verse 1]
Son las dos y cuarto, calle mojada,
el asfalto brilla como una mirada,
llevo los cascos pero no suena nada,
solo un clic de mechero a mi espalda.
Cambio de acera, cambio de paso,
la sombra se estira más larga que el caso,
cuento farolas pa' no pensar,
y hay una encendida que no debería estar.
Luz de naranja, papel mojado,
una sonrisa pintada de lado,
no tiene prisa, no tiene cara,
solo una vela que nunca se apaga.

[Pre-Chorus]
Si te giras, no está.
Si no miras… se acerca un poco más.

[Chorus]
No mires atrás, no mires atrás,
la luz que te sigue no es de la ciudad,
cada farola, un paso más,
cuando se apaga… ya está detrás.
No mires atrás, no mires atrás,
tu sombra esta noche tiene a quién esperar,
cuenta hasta tres y echa a andar,
que el farol no duerme… y tú tampoco más.

[Verse 2]
En la parada, cristal empañado,
mi reflejo tiene a alguien al lado,
el bus no viene, la app no carga,
y el mensaje dice: "¿por qué no te paras?"
Subo escaleras de tres en tres,
dedos de alambre en la baranda otra vez,
el ascensor me mira en el espejo
y la vela tiembla detrás de mi reflejo.
Llego a mi puerta, charco en el felpudo,
huellas mojadas de un paso desnudo,
cierro con llave, dos vueltas, tres,
y abajo en la calle hay una luz que me ve.

[Bridge]
Apaga la luz… apaga la luz…
si no ves nada, no existe… ¿verdad?
Apaga la luz…
(clic)
…y el cuarto se vuelve naranja.

[Chorus]
No mires atrás, no mires atrás,
la luz que te sigue no es de la ciudad,
cada farola, un paso más,
cuando se apaga… ya está detrás.

[Outro]
Una… dos… tres…
(ya está.)
"""

# --quality knobs: (spec numbers under "final"; a fast, cheap subset under "draft")
QUALITY = {
    "draft": {"ref_seeds": 2, "song_seeds": 1, "still_variants": 1, "posts": False,
              "clip_shots": [1, 3, 12], "idol_looks": IDOL_LOOKS[:3],
              "timeline_aspects": ["9:16", "16:9"], "render_qualities": ["preview"]},
    "final": {"ref_seeds": 4, "song_seeds": 2, "still_variants": 3, "posts": True,
              "clip_shots": [1, 2, 3, 5, 7, 10, 12], "idol_looks": IDOL_LOOKS,
              "timeline_aspects": ["9:16", "16:9"], "render_qualities": ["preview", "final"]},
}


class ProductionError(RuntimeError):
    pass


# ------------------------------------------------------------------- state

def set_out_dir(path: Path) -> None:
    """`--out-dir`: keep separate runs (a draft and a final) side by side."""
    global OUT_DIR, STATE_PATH, REPORT_PATH
    OUT_DIR = path.resolve()
    STATE_PATH = OUT_DIR / "state.json"
    REPORT_PATH = OUT_DIR / "REPORT.md"


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"done": {}}


def save_state(state: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def mark_done(state: dict[str, Any], step: int, data: dict[str, Any]) -> None:
    state["done"][str(step)] = data
    state.get("partial", {}).pop(str(step), None)
    save_state(state)


def is_done(state: dict[str, Any], step: int) -> bool:
    return str(step) in state["done"]


def partial(state: dict[str, Any], step: int) -> dict[str, Any]:
    """Per-item progress inside a long step (a still, a clip, a card, a
    render), checkpointed as each item finishes: a real GPU run that stops
    at shot 7 resumes at shot 7, not shot 1. Cleared when the step
    completes, or when `--only` forces the step to redo."""
    return state.setdefault("partial", {}).setdefault(str(step), {})


def save_partial(state: dict[str, Any], step: int, key: str, value: Any) -> None:
    partial(state, step)[key] = value
    save_state(state)


# --------------------------------------------------------------- MCP glue

async def call(session: Any, tool: str, args: dict[str, Any]) -> Any:
    """Call an MCP tool and return its parsed JSON payload (raises
    ProductionError with the tool's own message on an MCP-level error)."""
    args = {k: v for k, v in args.items() if v is not None}
    result = await session.call_tool(tool, args)
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "unknown error"
        raise ProductionError(f"{tool} failed: {text}")
    # the production never asks for pictures (include_image stays false), and
    # a text-only local model must never receive one it did not ask for
    if any(type(block).__name__ == "ImageContent" for block in result.content):
        raise ProductionError(f"{tool} returned an image without include_image=true")
    for block in result.content:
        if type(block).__name__ == "TextContent":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


async def wait_job(session: Any, job: dict[str, Any], timeout_s: float = 600.0) -> dict[str, Any]:
    """Polls studio_job in 60 s chunks until the job leaves queued/waiting_gpu/
    running, or `timeout_s` is used up. A job already returned as wait_s>0
    on the original call may already be done - this only polls if not."""
    deadline = time.monotonic() + timeout_s
    while job.get("state") in ("queued", "waiting_gpu", "running") and time.monotonic() < deadline:
        job = await call(session, "studio_job", {"job_id": job["id"], "wait_s": 60})
    if job.get("state") not in ("done",):
        raise ProductionError(f"job {job.get('id')} ended in state {job.get('state')!r}: {job.get('message')}")
    return job


def shot_asset(stills: dict[str, Any], clips: dict[str, str], n: int, prefer_clip: bool = True) -> str:
    """The asset that shows shot `n` in the edit: its Wan clip when one was
    made (and wanted), else its best still."""
    if prefer_clip and str(n) in clips:
        return clips[str(n)]
    return stills[str(n)]["best"]


# The storyboard: which shots play under which part of the song, in the
# order the lyrics tell the walk home. `c` = the clip when there is one.
# With cut_on_lyrics the edit changes shot on every sung line, so each
# verse list reads line by line (e.g. Verse 2: bus shelter, its reflection,
# the phone, the message, the stairs, the fingers, the elevator...).
STORYBOARD = {
    "Intro": [(1, "c"), (2, ""), (1, "")],
    "Verse 1": [(1, ""), (1, "c"), (4, ""), (2, ""), (1, ""), (2, "c"), (11, ""), (2, ""), (3, ""), (3, "c"), (1, ""), (3, "c")],
    "Pre-Chorus": [(5, "c"), (2, "")],
    "Chorus": [(11, ""), (3, "c"), (1, ""), (11, ""), (10, ""), (3, ""), (6, ""), (11, ""), (8, ""), (3, "c"), (7, ""), (11, "")],
    "Verse 2": [(5, ""), (5, "c"), (4, ""), (4, ""), (6, ""), (6, ""), (8, ""), (3, ""), (9, ""), (9, ""), (10, ""), (10, "c")],
    "Bridge": [(12, ""), (7, "c"), (12, ""), (3, ""), (12, "c")],
    "Outro": [(1, ""), (3, "c")],
}


def storyboard_pools(stills: dict[str, Any], clips: dict[str, str]) -> dict[str, list[str]]:
    return {section: [shot_asset(stills, clips, n, prefer_clip=(flag == "c")) for n, flag in shots]
            for section, shots in STORYBOARD.items()}


# ------------------------------------------------------------------ steps

async def step_project(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    found = await call(session, "studio_projects", {"query": PROJECT_NAME, "limit": 5})
    existing = next((p for p in found.get("items", []) if p["name"] == PROJECT_NAME), None)
    if existing:
        print(f"  found existing project {existing['id']}")
        mark_done(state, 1, {"project_id": existing["id"]})
        return
    brief = ("A single by FAROL: dark trap horror-rap, a night creature that is always a little closer, "
             "sodium-lit empty streets, idol-style photocards as the fun contrast.")
    created = await call(session, "studio_create_project",
                        {"name": PROJECT_NAME, "brief": brief, "image_engine": args.engine})
    print(f"  created project {created['id']} (image_engine={created.get('image_engine')})")
    mark_done(state, 1, {"project_id": created["id"]})


async def step_character(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    cast = await call(session, "studio_cast", {"project": pid, "action": "list"})
    existing = next((c for c in cast.get("characters", cast.get("items", [])) if c["name"] == CHARACTER_NAME), None)
    if not existing:
        existing = await call(session, "studio_cast", {
            "project": pid, "action": "create", "kind": "character", "name": CHARACTER_NAME,
            "fields": {"role": "lead", "prompt": FAROL_LOOK, "negative": FAROL_NEGATIVE,
                       "palette": [ACCENT, "#1B1D22", "#8A9099", ACCENT_RED],
                       "bio": "An urban night creature that is always a little closer."},
        })
        print(f"  created character {existing['id']}")
    else:
        print(f"  found existing character {existing['id']}")

    seeds = QUALITY[args.quality]["ref_seeds"]
    gen = await call(session, "studio_generate_image", {
        "project": pid, "prompt": REFERENCE_SHEET_PROMPT, "negative": FAROL_NEGATIVE,
        "engine": args.engine, "aspect": "16:9", "count": seeds, "seed": 1001, "wait_s": 240,
    })
    job = await wait_job(session, gen["job"])
    ref_ids = job["asset_ids"]
    # Scoring hook: the fake backend has no real image to judge, so the first
    # seed is the canonical pick; for a real run this is a provisional pick -
    # look with studio_show and, if another seed or pose reads
    # better, call studio_cast(action="update", fields={"canonical_asset_id":
    # <sheet>, "canonical_crop": "left_third"|"middle_third"|"right_third"})
    # and re-runs `--only stills` (then clips/photocards/album/timeline).
    sheet = ref_ids[0]
    # The sheet shows front / three-quarter / back left to right; Kontext
    # follows one pose far better than a triptych, so the canonical is the
    # front view, cropped out of the sheet (a new asset with lineage).
    updated = await call(session, "studio_cast", {
        "project": pid, "action": "update", "id": existing["id"],
        "fields": {"canonical_asset_id": sheet, "canonical_crop": "left_third"},
    })
    canonical = updated["canonical_asset_id"]
    print(f"  reference sheet: {ref_ids} -> sheet {sheet}, canonical (front view crop) {canonical}"
          f"{' (PROVISIONAL - confirm by eye on a real run)' if args.backend == 'real' else ''}")
    mark_done(state, 2, {"character_id": existing["id"], "reference_asset_ids": ref_ids, "sheet_asset_id": sheet,
                         "canonical_asset_id": canonical, "canonical_crop": "left_third"})


async def step_song(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    count = QUALITY[args.quality]["song_seeds"]
    res = await call(session, "studio_compose", {
        "project": pid, "tags": SONG_TAGS, "lyrics": SONG_LYRICS, "count": count, "seed": 2001,
        "wait_s": 240, **SONG_PARAMS,
    })
    job = await wait_job(session, res["job"], timeout_s=900)
    song_ids = job["asset_ids"]
    durations = {a["id"]: a.get("duration_s") for a in job.get("assets") or []}
    print(f"  composed {len(song_ids)} song take(s): {song_ids}")
    mark_done(state, 3, {"song_asset_ids": song_ids, "song_asset_id": song_ids[0],
                         "duration_s": durations.get(song_ids[0]) or SONG_PARAMS["duration"]})


async def _generate(session: Any, args: dict[str, Any], timeout_s: float = 600.0) -> list[str]:
    gen = await call(session, "studio_generate_image", {**args, "wait_s": 240})
    return (await wait_job(session, gen["job"], timeout_s=timeout_s))["asset_ids"]


async def step_stills(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    cfg = QUALITY[args.quality]
    variants, want_posts = cfg["still_variants"], cfg["posts"]
    stills: dict[str, dict[str, Any]] = dict(partial(state, 4))
    for shot in SHOTS:
        n = shot["n"]
        if str(n) in stills:
            print(f"  shot {n}: {stills[str(n)]['aspect_16_9']} (already made)")
            continue
        if shot["farol"]:
            base = {"project": pid, "prompt": f"@{CHARACTER_NAME} {shot['prompt']}, {NIGHT_LOOK}",
                    "consistent": True, "engine": args.engine}
        else:
            base = {"project": pid, "prompt": f"{shot['prompt']}, {NIGHT_LOOK}", "negative": FAROL_NEGATIVE,
                    "engine": args.engine}
        wide = await _generate(session, {**base, "width": 1344, "height": 768, "count": variants, "seed": 3000 + n * 10})
        entry: dict[str, Any] = {"aspect_16_9": wide, "best": wide[0],
                                 "route": f"{args.engine} edit (FAROL reference)" if shot["farol"] else f"{args.engine} txt2img (no FAROL)"}
        if want_posts:
            entry["aspect_4_5"] = await _generate(session, {**base, "aspect": "4:5", "count": 1, "seed": 3000 + n * 10 + 1})
        stills[str(n)] = entry
        save_partial(state, 4, str(n), entry)
        print(f"  shot {n}: {entry['aspect_16_9']}" + (f" + {entry.get('aspect_4_5')}" if want_posts else ""))
    mark_done(state, 4, {"stills": stills})


async def step_clips(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    stills = state["done"]["4"]["stills"]
    clips: dict[str, str] = dict(partial(state, 5))
    for n in QUALITY[args.quality]["clip_shots"]:
        if str(n) in clips:
            print(f"  shot {n} clip: {clips[str(n)]} (already made)")
            continue
        best = stills[str(n)]["best"]
        # template defaults: 1280x704 (follows the still's aspect), 121 frames
        # at 24 fps = 5 s, 20 steps, cfg 5, shift 8, uni_pc
        gen: dict[str, Any] = {"project": pid, "template": "wan22_ti2v", "reference_asset_id": best,
                               "prompt": CLIP_MOTION.get(n, "subtle motion, rain, flicker"), "seed": 5000 + n}
        if n in CLIP_STILL_FIGURE:
            gen["negative"] = CLIP_NEGATIVE_STILL
        ids = await _generate(session, gen, timeout_s=1800)
        clips[str(n)] = ids[0]
        save_partial(state, 5, str(n), ids[0])
        print(f"  shot {n} clip: {clips[str(n)]}")
    mark_done(state, 5, {"clips": clips})


async def step_photocards(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    char_id = state["done"]["2"]["character_id"]
    looks = QUALITY[args.quality]["idol_looks"]
    cards, photos = [], []
    done_photos = partial(state, 6)
    for i, look in enumerate(looks, start=1):
        if str(i) in done_photos:
            ids = [done_photos[str(i)]]
        else:
            ids = await _generate(session, {"project": pid, "prompt": f"@{CHARACTER_NAME} {look['prompt']}",
                                            "consistent": True, "aspect": "2:3", "count": 1, "seed": 4000 + i})
            save_partial(state, 6, str(i), ids[0])
        photos.append(ids[0])
        cards.append({"image_asset_id": ids[0], "role": look["role"], "message": look["message"], "accent": look["accent"]})
        print(f"  look {i} ({look['role']}): photo {ids[0]}")
    result = await call(session, "studio_photocard_set", {
        "project": pid, "character_id": char_id, "cards": cards, "set_name": PROJECT_NAME,
    })
    print(f"  set: fronts {result['front_ids']}, backs {result['back_ids']}, contact sheet {result['contact_sheet_id']}")
    mark_done(state, 6, {"photo_ids": photos, "front_ids": result["front_ids"], "back_ids": result["back_ids"],
                         "contact_sheet_id": result["contact_sheet_id"]})


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


async def step_album(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    stills = state["done"]["4"]["stills"]
    duration = state["done"]["3"].get("duration_s") or SONG_PARAMS["duration"]
    cover_image = stills["3"]["best"]        # lantern close-up: the strongest single graphic
    poster_image = stills["1"]["best"]       # the establishing shot: small, far, under the lamp
    lyric_image = stills["11"]["best"]       # chorus insert

    cover = await call(session, "studio_design", {
        "project": pid, "template": "album_cover", "image_asset_id": cover_image, "variant": "night",
        "fields": {"title": PROJECT_NAME, "artist": CHARACTER_NAME, "subtitle": "single", "accent": ACCENT},
    })
    tracklist = await call(session, "studio_design", {
        "project": pid, "template": "tracklist_back", "image_asset_id": cover_image, "variant": "night",
        "fields": {"group_name": CHARACTER_NAME, "title": PROJECT_NAME,
                   "tracks": [f"01  {PROJECT_NAME}  {_mmss(duration)}"],
                   "credits": "Letra y música: FAROL\nHecho de noche, bajo farolas de sodio, con Prospero's Hoard.",
                   "accent": ACCENT},
    })
    poster = await call(session, "studio_design", {
        "project": pid, "template": "teaser_poster", "image_asset_id": poster_image, "variant": "night",
        "fields": {"title": CHARACTER_NAME, "tagline": "No mires atrás", "date": "Siempre un poco más cerca", "accent": ACCENT},
    })
    lyric_card = await call(session, "studio_design", {
        "project": pid, "template": "lyric_card", "image_asset_id": lyric_image, "variant": "night",
        "fields": {"quote": "No mires atrás,\nno mires atrás,\nla luz que te sigue\nno es de la ciudad",
                   "attribution": f"{CHARACTER_NAME} — {PROJECT_NAME}", "accent": ACCENT},
    })
    print(f"  cover {cover['id']}, tracklist back {tracklist['id']}, poster {poster['id']}, lyric card {lyric_card['id']}")
    mark_done(state, 7, {"cover_id": cover["id"], "tracklist_back_id": tracklist["id"],
                         "teaser_poster_id": poster["id"], "lyric_card_id": lyric_card["id"]})


async def step_timeline(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    song_id = state["done"]["3"]["song_asset_id"]
    stills = state["done"]["4"]["stills"]
    clips = state["done"]["5"]["clips"]
    asset_ids = [s["best"] for s in stills.values()] + list(clips.values())

    sections: list[dict[str, Any]] = []
    if args.lrc_path:
        # your own timing (re-timed by ear) wins over the estimate
        imported = await call(session, "studio_import", {"project": pid, "path": args.lrc_path, "kind": "lyrics"})
        lyrics_asset_id, lyrics_source = imported["id"], f"imported from {Path(args.lrc_path).name}"
    else:
        timed = await call(session, "studio_time_lyrics", {"project": pid, "song_asset_id": song_id, "lyrics": SONG_LYRICS})
        lyrics_asset_id, sections = timed["id"], timed["sections"]
        lyrics_source = "studio_time_lyrics (section tags + the song's bars - an estimate, re-time by ear)"
        print(f"  timed {timed['lines']} lines in {len(sections)} sections -> lyrics {lyrics_asset_id}")

    options = {
        "karaoke": True, "fps": 24, "cut_on_lyrics": True, "flash_on_strong_downbeats": True,
        # quiet dread in the intro/bridge/outro (two-bar holds), a shot a bar
        # in the verses (or a line, whichever comes first), half a bar in the chorus
        "beats_low": 8, "beats_mid": 4, "beats_high": 2,
        "section_pools": storyboard_pools(stills, clips),
    }
    timelines: dict[str, Any] = dict(partial(state, 8).get("timelines", {}))
    finishing = {"color_grade": "sodium_night", "grain": 0.3, "vignette": True, "glitch_on_downbeats": True,
                 "lyric_style": "horror"}
    for aspect in QUALITY[args.quality]["timeline_aspects"]:
        if aspect in timelines:
            print(f"  timeline {aspect}: {timelines[aspect]['renders']} (already rendered)")
            continue
        built = await call(session, "studio_timeline", {
            "project": pid, "action": "auto", "song_asset_id": song_id, "asset_ids": asset_ids, "aspect": aspect,
            "lyrics_asset_id": lyrics_asset_id, "options": options,
        })
        tid = built["id"]
        await call(session, "studio_timeline", {
            "project": pid, "action": "update", "timeline_id": tid, "patch": {"finishing": finishing},
        })
        renders = {}
        for quality in QUALITY[args.quality]["render_qualities"]:
            rendered = await call(session, "studio_render", {"timeline_id": tid, "quality": quality, "wait_s": 280})
            job = await wait_job(session, rendered["job"], timeout_s=1800)
            renders[quality] = job["asset_ids"][0]
            print(f"  timeline {aspect} render ({quality}): {renders[quality]}")
        timelines[aspect] = {"timeline_id": tid, "clips": built.get("clips_total"), "renders": renders}
        save_partial(state, 8, "timelines", timelines)
    mark_done(state, 8, {"lyrics_asset_id": lyrics_asset_id, "lyrics_source": lyrics_source, "sections": sections,
                         "timelines": timelines, "finishing": finishing})


async def step_report(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    done = state["done"]
    timings = state.get("timings", {})
    lines = [f"# {PROJECT_NAME} - production report", "",
             f"Backend: **{args.backend}** - quality: **{args.quality}**",
             "", "Every id below is a Prospero asset id; `studio_lineage(asset_id)` gives its full recipe "
             "(template, every parameter, seed, inputs).", ""]
    if args.backend == "fake":
        lines += ["> Made against the bundled demo ComfyUI: pictures, clips and the song are procedural "
                  "placeholders. Everything Prospero itself decides - design layouts, typography, grade, grain, "
                  "glitch, cut rhythm, storyboard, karaoke timing - is real and is what a GPU run reuses unchanged.", ""]

    if timings:
        lines += ["## Timings", "", "| Step | seconds |", "| --- | --- |"]
        for n in sorted(timings, key=int):
            lines.append(f"| {n} {STEP_NAMES[int(n)]} | {timings[n]} |")
        lines.append("")

    d2 = done["2"]
    lines += ["## Project & character", "",
              f"- Project: `{done['1']['project_id']}`",
              f"- Character FAROL: `{d2['character_id']}`",
              f"- Reference sheet seeds: {d2['reference_asset_ids']} (sheet used: `{d2.get('sheet_asset_id', '-')}`)",
              f"- Canonical reference: `{d2['canonical_asset_id']}` - the front view cropped out of the sheet "
              f"({d2.get('canonical_crop', 'full')})" +
              (" **(provisional: first seed - confirm before the cards go out)**" if args.backend == "real" else ""), ""]

    lines += ["## Song", "", f"- Takes: {done['3']['song_asset_ids']}",
              f"- Used for the timeline: `{done['3']['song_asset_id']}` ({_mmss(done['3'].get('duration_s') or 0)})",
              f"- Tags: `{SONG_TAGS}`",
              f"- bpm {SONG_PARAMS['bpm']}, key {SONG_PARAMS['key']}, language {SONG_PARAMS['language']}, "
              f"time signature {SONG_PARAMS['time_signature']}, duration {SONG_PARAMS['duration']}s", ""]

    lines += ["## Stills (12 shots)", "", "| Shot | route | 16:9 variants | best | 4:5 post |", "| --- | --- | --- | --- | --- |"]
    for n, entry in sorted(done["4"]["stills"].items(), key=lambda kv: int(kv[0])):
        lines.append(f"| {n} | {entry.get('route', 'kontext')} | {entry['aspect_16_9']} | `{entry['best']}` | "
                     f"{entry.get('aspect_4_5', '-')} |")
    lines += ["", "Shots 4, 9 and 12 do not show FAROL, so they are generated fresh (Flux schnell, same night look) "
              "instead of edited from FAROL's reference - a Kontext edit keeps the character in frame.", ""]

    if "5" in done:
        lines += ["## Clips (Wan 2.2 TI2V, 5 s from the best still)", "", "| Shot | clip asset | motion |", "| --- | --- | --- |"]
        for n, cid in sorted(done["5"]["clips"].items(), key=lambda kv: int(kv[0])):
            lines.append(f"| {n} | `{cid}` | {CLIP_MOTION.get(int(n), '')} |")
        lines.append("")

    if "6" in done:
        d6 = done["6"]
        lines += ["## Photocards (solo set, one card per look)", "",
                  f"- Photos (Kontext, 2:3): {d6.get('photo_ids', [])}",
                  f"- Fronts: {d6['front_ids']}", f"- Backs: {d6['back_ids']}",
                  f"- Contact sheet: `{d6.get('contact_sheet_id', '-')}`", ""]

    if "7" in done:
        a = done["7"]
        lines += ["## Album art (night variants)", "", f"- Cover 3000x3000: `{a['cover_id']}`",
                  f"- Tracklist back: `{a['tracklist_back_id']}`", f"- Teaser poster: `{a['teaser_poster_id']}`",
                  f"- Lyric card (hook): `{a['lyric_card_id']}`", ""]

    if "8" in done:
        t = done["8"]
        finishing_str = ", ".join(f"{k}={v}" for k, v in t["finishing"].items())
        lines += ["## Timeline", "", f"- Lyrics: `{t['lyrics_asset_id']}` - {t.get('lyrics_source', '')}",
                  f"- Finishing: {finishing_str}",
                  "- Cut: a new shot on every sung line and every section start; two-bar holds in the intro, "
                  "bridge and outro, half-bar cuts with a white flash + RGB glitch on the chorus's strong downbeats; "
                  "shots follow the storyboard (`STORYBOARD` in this script) section by section", ""]
        if t.get("sections"):
            lines += ["| Section | energy | from | to |", "| --- | --- | --- | --- |"]
            for sec in t["sections"]:
                lines.append(f"| {sec['label']} | {sec['energy']} | {sec['start_s']:.2f} s | {sec['end_s']:.2f} s |")
            lines.append("")
        for aspect, info in t["timelines"].items():
            lines.append(f"- {aspect}: timeline `{info['timeline_id']}` ({info.get('clips') or '?'} clips), renders {info['renders']}")
        lines.append("")

    lines += ["## What to review", "",
              "- The canonical FAROL reference: first sheet seed, front third. If another seed or pose reads "
              "better: `studio_cast` update with `canonical_asset_id` + `canonical_crop`, then `--only stills` "
              "and the steps after it.",
              "- The best variant of each still (the first one is used): swap `best` in state.json, then "
              "`--only clips`, `--only album`, `--only timeline`.",
              "- Karaoke timing: estimated from the lyrics' [Section] tags and the song's bars, not from the "
              "vocals. Re-time by ear in Audio > Lyrics timing (tap Space per line, [Section] lines included), export "
              "the LRC and run `--only timeline --lrc-path <file>`.",
              "- VRAM per step (Flux/Kontext ~13 GB, Wan 5B ~12 GB at 1280x704, ACE-Step 1.5 turbo ~8 GB): run "
              "ComfyUI on a card with 16 GB or more (`--cuda-device` picks it on a multi-GPU machine).",
              "- The `final` render (1080p) after approving the `preview`.", ""]

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {_shown(REPORT_PATH)}")
    mark_done(state, 9, {"report_path": _shown(REPORT_PATH)})


STEP_FUNCS = {
    1: step_project, 2: step_character, 3: step_song, 4: step_stills, 5: step_clips,
    6: step_photocards, 7: step_album, 8: step_timeline, 9: step_report,
}


# ---------------------------------------------------------------- backend

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeAppHandle:
    """Runs the real FastAPI app + the bundled fake ComfyUI in this process
    (uvicorn on a background thread), exactly like the test suite's
    `running_app` fixture - infrastructure the script stands up, not a
    shortcut around the MCP adapter: every actual production step below
    still only ever talks to this app through `mcp_server.py` over stdio."""

    def __init__(self, data_dir: Path):
        import uvicorn

        from prosperos_hoard.api import create_app
        from prosperos_hoard.devtools.fake_comfy import FakeComfyServer

        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.fake_comfy = FakeComfyServer(data_dir / "fake_comfy")
        comfy_port = self.fake_comfy.run_in_thread()
        # Always rewritten: the fake ComfyUI's port is different on every
        # process (it is bound fresh each run), so a stale backend.json left
        # over from a previous invocation would point at a server that no
        # longer exists.
        backend_json = data_dir / "backend.json"
        backend_json.write_text(json.dumps({"comfy": {"url": f"http://127.0.0.1:{comfy_port}"}}), encoding="utf-8")
        self.port = _free_port()
        self.app = create_app(data_dir, static_dir=None, port=self.port)
        # loop="asyncio": uvicorn's default "auto" installs uvloop process-wide
        # (asyncio.set_event_loop_policy), which breaks this script's own main
        # thread afterwards - uvloop's policy has no legacy child watcher, and
        # that is exactly what spawning mcp_server.py over stdio needs.
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not getattr(self.server, "started", False) and time.monotonic() < deadline:
            time.sleep(0.05)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.app.state.queue.stop()
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.fake_comfy.stop()


async def run(args: argparse.Namespace) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    state = load_state()
    fake_app: Optional[FakeAppHandle] = None
    try:
        if args.backend == "fake":
            fake_app = FakeAppHandle(OUT_DIR / "appdata")
            app_url = fake_app.url
            state["_appdata"] = str(fake_app.data_dir)
        else:
            app_url = args.app_url or os.environ.get("PROSPERO_URL", "http://127.0.0.1:8815")
            try:
                import urllib.request
                with urllib.request.urlopen(f"{app_url}/api/health", timeout=5) as resp:
                    json.loads(resp.read())
            except Exception as exc:
                print(f"error: Prospero's Hoard is not reachable at {app_url} ({exc}). "
                      f"Start it first (see the README's 'Run locally'), then retry.")
                return 1

        params = StdioServerParameters(
            command=sys.executable, args=[str(REPO_ROOT / "prosperos_hoard" / "mcp_server.py")],
            env={**os.environ, "PROSPERO_URL": app_url},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                if args.only:
                    steps = [STEP_NUMBERS.get(args.only, int(args.only) if args.only.isdigit() else -1)]
                    if steps[0] not in STEP_FUNCS:
                        print(f"error: --only must be one of {', '.join(STEP_NAMES.values())} or 1-9")
                        return 1
                else:
                    steps = list(range(1, 10))

                for n in steps:
                    if not args.only and is_done(state, n):
                        print(f"[{n}/9] {STEP_NAMES[n]}: already done, skipping (use --only to force)")
                        continue
                    print(f"[{n}/9] {STEP_NAMES[n]}...")
                    if args.only:
                        state.get("partial", {}).pop(str(n), None)  # --only redoes the step from scratch
                    for dep in range(1, n):
                        if dep in (1,) and not is_done(state, dep) and n != 1:
                            raise ProductionError(f"step {n} needs step {dep} ({STEP_NAMES[dep]}) to run first")
                    started = time.monotonic()
                    await STEP_FUNCS[n](session, state, args)
                    state.setdefault("timings", {})[str(n)] = round(time.monotonic() - started, 1)
                    save_state(state)
        return 0
    finally:
        if fake_app:
            fake_app.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["fake", "real"], default="fake")
    parser.add_argument("--quality", choices=["draft", "final"], default="draft")
    parser.add_argument("--engine", choices=["auto", "qwen21", "flux"], default="auto",
                        help="image engine for the reference sheet, stills, photocards and cover: "
                             "auto (default) uses Qwen-Image 2.1 when it is installed, else Flux schnell; "
                             "set on the project once at creation (step 1) so a resumed run stays consistent")
    parser.add_argument("--app-url", default=None, help="real backend only; default PROSPERO_URL or 127.0.0.1:8815")
    parser.add_argument("--lrc-path", default=None,
                        help="a timed .lrc (re-timed by ear in Audio > Lyrics timing) to use instead of the bar-grid estimate; "
                             "a path the app may import (home folder or a folder allowed in Settings)")
    parser.add_argument("--only", default=None, help="run just one step (name or number 1-9)")
    parser.add_argument("--out-dir", default=None,
                        help="where state.json, REPORT.md (and the fake backend's data) go; default data/productions/no_mires_atras")
    args = parser.parse_args()
    if args.out_dir:
        set_out_dir(Path(args.out_dir))
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
