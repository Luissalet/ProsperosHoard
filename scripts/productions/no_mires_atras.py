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
        --app-url http://127.0.0.1:8815 --quality final --lrc-path ~/no_mires_atras.lrc

    # re-run a single step once its inputs exist (e.g. after picking a
    # different canonical reference, or after reviewing the output)
    .venv/bin/python scripts/productions/no_mires_atras.py --backend fake --only stills

Steps: 1 project, 2 character
(FAROL reference sheet -> canonical), 3 song (studio_compose, 2 seeds),
4 stills (12 shots, Kontext with the canonical reference), 5 clips (Wan
2.2 TI2V from the best still of shots 1,2,3,5,7,10,12), 6 photocards (5
idol looks + backs + a contact sheet), 7 album art (cover, tracklist
back, teaser poster, lyric card), 8 timeline (auto-cut, sodium-night
grade, grain, vignette, glitch on the downbeats; preview then final), 9
REPORT.md.

--quality draft trims the scope (fewer seeds/variants/clips/looks/aspects)
so a full run finishes quickly; --quality final uses the full
numbers. Both produce every asset kind, which is what "done" requires.

Idempotency: step 1 finds the project by name through a real MCP call
(studio_projects). For everything else, this script keeps its own
checkpoint (`state.json` next to this file's other output) recording the
ids each step produced - the MCP surface has no generic "what have I
already made for this production" query, so this file is the script's own
bookkeeping, not a side channel into Prospero. Delete state.json (or pass
--only <step>) to force a step to redo; every other already-done step is
skipped on the next run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
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
MEDIA_DIR = OUT_DIR / "review"  # contact sheets etc. saved to look at directly

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

IDOL_LOOKS = [
    {"role": "Bouquet", "message": "gracias por venir",
     "prompt": f"{FAROL_LOOK}, glossy idol photocard studio shoot, pastel pink backdrop, holding a "
               "bouquet of wilted roses, soft beauty lighting, magazine grade"},
    {"role": "Knit sweater", "message": "noche tranquila",
     "prompt": f"{FAROL_LOOK}, glossy idol photocard studio shoot, sitting on a stool, cream knit "
               "sweater, the candle flame glowing softly inside the lantern head, soft beauty lighting"},
    {"role": "Photobooth", "prompt": f"{FAROL_LOOK}, glossy idol photocard, peace sign, photo-booth "
                                     "strip style, soft flash lighting, magazine grade", "message": "click"},
    {"role": "Rainy window", "message": "no mires atras",
     "prompt": f"{FAROL_LOOK}, glossy idol photocard, rainy window backdrop, fairy lights, soft beauty lighting"},
    {"role": "Sorry note", "message": "perdon",
     "prompt": f"{FAROL_LOOK}, glossy idol photocard, holding a hand-written \"perdon\" note, soft "
               "beauty lighting, magazine grade"},
]

SHOTS = [
    {"n": 1, "prompt": "empty Spanish-style street at 2:15 a.m., fog, a far sodium lamp flickering, a "
                       "tall figure standing under it, wet asphalt, no readable signs or brands"},
    {"n": 2, "prompt": "over-the-shoulder point of view looking back down an empty wet street, the "
                       "figure now under a closer sodium lamp"},
    {"n": 3, "prompt": "extreme close-up of the lantern head, painted crooked smile, candle flame "
                       "inside, raindrops on the paper"},
    {"n": 4, "prompt": "a phone in a trembling hand, screen glow on a wet face, rainy night street "
                       "behind, screen left blank"},
    {"n": 5, "prompt": "bus shelter at night, in the glass reflection the figure stands beside the "
                       "viewer; beside the viewer in reality, nothing"},
    {"n": 6, "prompt": "stairwell seen from below, long paper fingers curling over the rail two floors "
                       "up, dim stairwell light"},
    {"n": 7, "prompt": "empty laundromat at 3 a.m., machines spinning, the figure seated among empty "
                       "plastic chairs"},
    {"n": 8, "prompt": "elevator mirror interior, amber lantern glow reflected behind a shoulder"},
    {"n": 9, "prompt": "close-up of a wet doormat, footprints that are not human leading to a front door"},
    {"n": 10, "prompt": "from inside a dark flat looking out through a window, the figure on the street "
                        "below, lantern lit, looking up"},
    {"n": 11, "prompt": "quick chorus insert: flickering sodium lamps, the crooked smile, long paper "
                        "fingers, the candle flame, very short fragmented framing"},
    {"n": 12, "prompt": "dark bedroom just after the light switch is turned off, the room slowly "
                        "turning sodium orange"},
]
CLIP_MOTION = {
    1: "light rain falling, the far lamp flickering",
    2: "light rain falling, a subtle slow push-in",
    3: "the candle flame flickering gently inside the lantern",
    5: "rain streaking down the glass, a slow push-in",
    7: "a washing machine spinning, faint fluorescent flicker",
    10: "rain on the window glass, the street lamp flickering",
    12: "the room slowly brightening into sodium orange, a very slow push-in",
}

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
              "clip_shots": [1, 3, 7], "idol_looks": IDOL_LOOKS[:2],
              "timeline_aspects": ["9:16"], "render_qualities": ["preview"]},
    "final": {"ref_seeds": 4, "song_seeds": 2, "still_variants": 3, "posts": True,
              "clip_shots": [1, 2, 3, 5, 7, 10, 12], "idol_looks": IDOL_LOOKS,
              "timeline_aspects": ["9:16", "16:9"], "render_qualities": ["preview", "final"]},
}


class ProductionError(RuntimeError):
    pass


# ------------------------------------------------------------------- state

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
    save_state(state)


def is_done(state: dict[str, Any], step: int) -> bool:
    return str(step) in state["done"]


# --------------------------------------------------------------- MCP glue

async def call(session: Any, tool: str, args: dict[str, Any]) -> Any:
    """Call an MCP tool and return its parsed JSON payload (raises
    ProductionError with the tool's own message on an MCP-level error)."""
    args = {k: v for k, v in args.items() if v is not None}
    result = await session.call_tool(tool, args)
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "unknown error"
        raise ProductionError(f"{tool} failed: {text}")
    for block in result.content:
        if type(block).__name__ == "TextContent":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return block.text
    return None


async def call_images(session: Any, tool: str, args: dict[str, Any]) -> tuple[Any, list[bytes]]:
    """Like `call`, but also returns the raw bytes of any ImageContent
    blocks in the result (used only for studio_show, to save a contact
    sheet next to the report to look at directly)."""
    args = {k: v for k, v in args.items() if v is not None}
    result = await session.call_tool(tool, args)
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "unknown error"
        raise ProductionError(f"{tool} failed: {text}")
    payload, images = None, []
    for block in result.content:
        if type(block).__name__ == "TextContent":
            if payload is None:
                try:
                    payload = json.loads(block.text)
                except json.JSONDecodeError:
                    payload = block.text
        elif type(block).__name__ == "ImageContent":
            images.append(base64.b64decode(block.data))
    return payload, images


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


def naive_lrc(lyrics: str, duration_s: float) -> str:
    """A [mm:ss.xx]-timed lyric file, sung lines only (section tags like
    [Chorus] are structure, not on-screen captions), evenly spaced across
    the song. Good enough to prove the timeline's karaoke captions render;
    for a real run, re-align this by ear against the
    analysed sections instead of trusting this even spacing."""
    lines = [ln.strip() for ln in lyrics.splitlines() if ln.strip() and not ln.strip().startswith("[")]
    if not lines:
        return ""
    margin = min(2.0, duration_s * 0.05)
    span = max(1.0, duration_s - 2 * margin)
    step = span / len(lines)
    out = []
    for i, line in enumerate(lines):
        t = margin + i * step
        out.append(f"[{int(t // 60):02d}:{t % 60:05.2f}]{line}")
    return "\n".join(out) + "\n"


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
    created = await call(session, "studio_create_project", {"name": PROJECT_NAME, "brief": brief})
    print(f"  created project {created['id']}")
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
        "template": "flux_schnell_txt2img", "aspect": "16:9", "count": seeds, "seed": 1001, "wait_s": 240,
    })
    job = await wait_job(session, gen["job"])
    ref_ids = job["asset_ids"]
    # Scoring hook: the fake backend has no real image to judge, so the first
    # seed is the canonical pick; for a real run this is a provisional pick -
    # look with studio_show and, if a different seed
    # reads better, call studio_cast(action="update", fields={"canonical_asset_id": ...})
    # and re-run `--only stills` (and clips/photocards/album if already made).
    canonical = ref_ids[0]
    await call(session, "studio_cast", {
        "project": pid, "action": "update", "id": existing["id"],
        "fields": {"canonical_asset_id": canonical},
    })
    print(f"  reference sheet: {ref_ids} -> canonical {canonical}"
          f"{' (PROVISIONAL - confirm by eye on a real run)' if args.backend == 'real' else ''}")
    mark_done(state, 2, {"character_id": existing["id"], "reference_asset_ids": ref_ids, "canonical_asset_id": canonical})


async def step_song(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    count = QUALITY[args.quality]["song_seeds"]
    res = await call(session, "studio_compose", {
        "project": pid, "tags": SONG_TAGS, "lyrics": SONG_LYRICS, "count": count, "seed": 2001,
        "wait_s": 240, **SONG_PARAMS,
    })
    job = await wait_job(session, res["job"], timeout_s=900)
    song_ids = job["asset_ids"]
    print(f"  composed {len(song_ids)} song take(s): {song_ids}")
    mark_done(state, 3, {"song_asset_ids": song_ids, "song_asset_id": song_ids[0]})


async def step_stills(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    cfg = QUALITY[args.quality]
    variants, want_posts = cfg["still_variants"], cfg["posts"]
    stills: dict[str, dict[str, Any]] = {}
    for shot in SHOTS:
        n = shot["n"]
        prompt = f"@{CHARACTER_NAME} {shot['prompt']}"
        gen = await call(session, "studio_generate_image", {
            "project": pid, "prompt": prompt, "consistent": True, "width": 1344, "height": 768,
            "count": variants, "seed": 3000 + n * 10, "wait_s": 240,
        })
        job = await wait_job(session, gen["job"])
        entry = {"aspect_16_9": job["asset_ids"], "best": job["asset_ids"][0]}
        if want_posts:
            post = await call(session, "studio_generate_image", {
                "project": pid, "prompt": prompt, "consistent": True, "aspect": "4:5",
                "count": 1, "seed": 3000 + n * 10 + 1, "wait_s": 240,
            })
            post_job = await wait_job(session, post["job"])
            entry["aspect_4_5"] = post_job["asset_ids"]
        stills[str(n)] = entry
        print(f"  shot {n}: {entry['aspect_16_9']}" + (f" + {entry.get('aspect_4_5')}" if want_posts else ""))
    mark_done(state, 4, {"stills": stills})


async def step_clips(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    stills = state["done"]["4"]["stills"]
    clips: dict[str, str] = {}
    for n in QUALITY[args.quality]["clip_shots"]:
        best = stills[str(n)]["best"]
        gen = await call(session, "studio_generate_image", {
            "project": pid, "template": "wan22_ti2v", "reference_asset_id": best,
            "prompt": CLIP_MOTION.get(n, "subtle motion, rain, flicker"), "wait_s": 280,
        })
        job = await wait_job(session, gen["job"], timeout_s=1200)
        clips[str(n)] = job["asset_ids"][0]
        print(f"  shot {n} clip: {clips[str(n)]}")
    mark_done(state, 5, {"clips": clips})


async def step_photocards(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    looks = QUALITY[args.quality]["idol_looks"]
    fronts, backs = [], []
    for i, look in enumerate(looks, start=1):
        gen = await call(session, "studio_generate_image", {
            "project": pid, "prompt": f"@{CHARACTER_NAME} {look['prompt']}", "consistent": True,
            "aspect": "2:3", "count": 1, "seed": 4000 + i, "wait_s": 240,
        })
        job = await wait_job(session, gen["job"])
        photo_id = job["asset_ids"][0]
        front = await call(session, "studio_design", {
            "project": pid, "template": "photocard_front", "image_asset_id": photo_id,
            "fields": {"member_name": CHARACTER_NAME, "role": look["role"], "group_name": PROJECT_NAME, "accent": ACCENT},
        })
        back = await call(session, "studio_design", {
            "project": pid, "template": "photocard_back",
            "fields": {"member_name": CHARACTER_NAME, "group_name": PROJECT_NAME, "message": look.get("message", ""),
                       "serial": f"FAROL-{i:02d}", "accent": ACCENT},
        })
        fronts.append(front["id"])
        backs.append(back["id"])
        print(f"  photocard {i} ({look['role']}): front {front['id']}, back {back['id']}")

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    sheet_paths = []
    _payload, images = await call_images(session, "studio_show", {"asset_ids": fronts, "size": 768})
    for i, raw in enumerate(images):
        path = MEDIA_DIR / f"photocards_contact_sheet_{i}.jpg"
        path.write_bytes(raw)
        sheet_paths.append(str(path.relative_to(REPO_ROOT)))
    print(f"  contact sheet saved to {sheet_paths}")
    mark_done(state, 6, {"front_ids": fronts, "back_ids": backs, "contact_sheet_paths": sheet_paths})


async def step_album(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    pid = state["done"]["1"]["project_id"]
    stills = state["done"]["4"]["stills"]
    cover_image = stills["3"]["best"]        # lantern close-up: the strongest single graphic
    poster_image = stills["1"]["best"]       # the establishing shot
    lyric_image = stills["11"]["best"]       # chorus insert

    cover = await call(session, "studio_design", {
        "project": pid, "template": "album_cover", "image_asset_id": cover_image, "variant": "center_title",
        "fields": {"title": f"{CHARACTER_NAME} / {PROJECT_NAME}", "accent": ACCENT},
    })
    tracklist = await call(session, "studio_design", {
        "project": pid, "template": "tracklist_back", "image_asset_id": cover["id"],
        "fields": {"group_name": CHARACTER_NAME, "tracks": [PROJECT_NAME], "accent": ACCENT},
    })
    poster = await call(session, "studio_design", {
        "project": pid, "template": "teaser_poster", "image_asset_id": poster_image,
        "fields": {"title": CHARACTER_NAME, "tagline": PROJECT_NAME, "accent": ACCENT},
    })
    lyric_card = await call(session, "studio_design", {
        "project": pid, "template": "lyric_card", "image_asset_id": lyric_image,
        "fields": {"quote": "No mires atrás, no mires atrás,\nla luz que te sigue no es de la ciudad",
                   "attribution": f"{CHARACTER_NAME} - {PROJECT_NAME}", "accent": ACCENT_RED},
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

    lyrics_asset_id = None
    lrc_text = naive_lrc(SONG_LYRICS, SONG_PARAMS["duration"])
    if args.backend == "fake":
        inbox = Path(state["_appdata"]) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        lrc_path = inbox / "no_mires_atras.lrc"
        lrc_path.write_text(lrc_text, encoding="utf-8")
        imported = await call(session, "studio_import", {"project": pid, "path": lrc_path.name, "kind": "lyrics"})
        lyrics_asset_id = imported["id"]
    elif args.lrc_path:
        imported = await call(session, "studio_import", {"project": pid, "path": args.lrc_path, "kind": "lyrics"})
        lyrics_asset_id = imported["id"]
    else:
        print("  no --lrc-path given for a real run: timeline will have no karaoke captions "
              "(time the lyrics in Audio > Lyrics in the app, or pass --lrc-path)")

    timelines: dict[str, Any] = {}
    finishing = {"color_grade": "sodium_night", "grain": 0.3, "vignette": True, "glitch_on_downbeats": True,
                "lyric_style": "horror"}
    for aspect in QUALITY[args.quality]["timeline_aspects"]:
        built = await call(session, "studio_timeline", {
            "project": pid, "action": "auto", "song_asset_id": song_id, "asset_ids": asset_ids, "aspect": aspect,
            "lyrics_asset_id": lyrics_asset_id, "options": {"karaoke": True, "fps": 24},
        })
        tid = built["id"]
        await call(session, "studio_timeline", {
            "project": pid, "action": "update", "timeline_id": tid, "patch": {"finishing": finishing},
        })
        renders = {}
        for quality in QUALITY[args.quality]["render_qualities"]:
            rendered = await call(session, "studio_render", {"timeline_id": tid, "quality": quality, "wait_s": 280})
            job = await wait_job(session, rendered["job"], timeout_s=900)
            renders[quality] = job["asset_ids"][0]
            print(f"  timeline {aspect} render ({quality}): {renders[quality]}")
        timelines[aspect] = {"timeline_id": tid, "renders": renders}
    mark_done(state, 8, {"lyrics_asset_id": lyrics_asset_id, "timelines": timelines, "finishing": finishing})


async def step_report(session: Any, state: dict[str, Any], args: argparse.Namespace) -> None:
    done = state["done"]
    lines = [f"# {PROJECT_NAME} - production report", "",
             f"Backend: **{args.backend}** - quality: **{args.quality}**",
             "", "Every id below is a Prospero asset id; `studio_lineage(asset_id)` gives its full recipe.", ""]

    lines += ["## Project & character", "",
              f"- Project: `{done['1']['project_id']}`",
              f"- Character: `{done['2']['character_id']}`",
              f"- Reference sheet seeds: {done['2']['reference_asset_ids']}",
              f"- Canonical reference: `{done['2']['canonical_asset_id']}`" +
              (" **(provisional pick on the fake backend's first seed - review before the real run's cards go out)**"
               if args.backend == "real" else ""), ""]

    lines += ["## Song", "", f"- Takes: {done['3']['song_asset_ids']}",
              f"- Used for the timeline: `{done['3']['song_asset_id']}`",
              f"- Tags: `{SONG_TAGS}`",
              f"- bpm {SONG_PARAMS['bpm']}, key {SONG_PARAMS['key']}, language {SONG_PARAMS['language']}, "
              f"time signature {SONG_PARAMS['time_signature']}, duration {SONG_PARAMS['duration']}s", ""]

    lines += ["## Stills (12 shots)", "", "| Shot | 16:9 variants | best | 4:5 post |", "| --- | --- | --- | --- |"]
    for n, entry in sorted(done["4"]["stills"].items(), key=lambda kv: int(kv[0])):
        lines.append(f"| {n} | {entry['aspect_16_9']} | `{entry['best']}` | {entry.get('aspect_4_5', '-')} |")
    lines.append("")

    if "5" in done:
        lines += ["## Clips (Wan 2.2 TI2V)", "", "| Shot | clip asset |", "| --- | --- |"]
        for n, cid in sorted(done["5"]["clips"].items(), key=lambda kv: int(kv[0])):
            lines.append(f"| {n} | `{cid}` |")
        lines.append("")

    if "6" in done:
        lines += ["## Photocards", "", f"- Fronts: {done['6']['front_ids']}", f"- Backs: {done['6']['back_ids']}",
                  "- Contact sheet: rendered with studio_show and saved as a plain image file next to "
                  f"this report (not a Prospero library asset - studio_photocard_set expects a group "
                  f"of distinct members, and FAROL is one character in several looks): "
                  f"{done['6']['contact_sheet_paths']}", ""]

    if "7" in done:
        a = done["7"]
        lines += ["## Album art", "", f"- Cover: `{a['cover_id']}`", f"- Tracklist back: `{a['tracklist_back_id']}`",
                  f"- Teaser poster: `{a['teaser_poster_id']}`", f"- Lyric card: `{a['lyric_card_id']}`", ""]

    if "8" in done:
        t = done["8"]
        finishing_str = ", ".join(f"{k}={v}" for k, v in t["finishing"].items())
        lines += ["## Timeline", "", f"- Lyrics asset: `{t['lyrics_asset_id']}`" if t["lyrics_asset_id"]
                  else "- Lyrics: none (no LRC was available for this run - see the note above)",
                  f"- Finishing: {finishing_str}", ""]
        for aspect, info in t["timelines"].items():
            lines.append(f"- {aspect}: timeline `{info['timeline_id']}`, renders {info['renders']}")
        lines.append("")

    lines += ["## What to review", "",
              "- The canonical FAROL reference (first seed, auto-picked) - swap it with "
              "`studio_cast` if another seed reads better, then re-run with `--only stills`.",
              "- The naive, evenly-spaced LRC timing (`naive_lrc` in this script) - re-align it by "
              "ear against the analysed sections (`studio_analyze_audio`) for real karaoke timing.",
              "- On the real backend: VRAM per step (Flux/Kontext ~13 GB, Wan 5B ~12 GB at "
              "1280x704, ACE-Step 1.5 turbo ~8 GB) on a card with 16 "
              "GB - `--cuda-device` picks which GPU ComfyUI starts on; only one workflow class runs "
              "at a time per the job queue's GPU lane.",
              "- The `final` render quality (1080p, slower) versus the `preview` used for early looks.", ""]

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote {REPORT_PATH.relative_to(REPO_ROOT)}")
    mark_done(state, 9, {"report_path": str(REPORT_PATH.relative_to(REPO_ROOT))})


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
                    for dep in range(1, n):
                        if dep in (1,) and not is_done(state, dep) and n != 1:
                            raise ProductionError(f"step {n} needs step {dep} ({STEP_NAMES[dep]}) to run first")
                    await STEP_FUNCS[n](session, state, args)
        return 0
    finally:
        if fake_app:
            fake_app.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["fake", "real"], default="fake")
    parser.add_argument("--quality", choices=["draft", "final"], default="draft")
    parser.add_argument("--app-url", default=None, help="real backend only; default PROSPERO_URL or 127.0.0.1:8815")
    parser.add_argument("--lrc-path", default=None, help="real backend only; a timed .lrc file to import for karaoke")
    parser.add_argument("--only", default=None, help="run just one step (name or number 1-9)")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
