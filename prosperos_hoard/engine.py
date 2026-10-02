"""The business logic behind every `/api/agent/*` endpoint (and the richer
UI endpoints that wrap the same functions). Kept separate from `api.py` so
it is directly unit-testable without spinning up FastAPI.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import mimetypes
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image, ImageDraw, ImageOps

from . import audio as audio_mod
from . import comfy_driver
from . import design
from . import procutil
from . import templates as design_templates
from . import timeline as timeline_mod
from . import video as video_mod
from . import voices as voices_mod
from .backend import Backend, ffmpeg_path
from .hoard_link.errors import Unavailable
from .ids import new_id
from .jobs import JobCancelled, WaitingForResources
from .store import NotFound, Store
from .util import now_iso, replace_with_retry
from .workflows import convert as convert_mod

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".gif"}  # an animated GIF is a (silent) video
LYRICS_EXTS = {".lrc", ".txt"}
FONT_EXTS = {".ttf", ".otf"}
KIND_EXTS = {"image": IMAGE_EXTS, "audio": AUDIO_EXTS, "video": VIDEO_EXTS, "lyrics": LYRICS_EXTS, "font": FONT_EXTS}

MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024
MAX_LYRICS_BYTES = 512 * 1024
MAX_FONT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000

ASPECT_SIZES = {"1:1": (1024, 1024), "9:16": (768, 1344), "16:9": (1344, 768), "2:3": (832, 1216), "3:2": (1216, 832),
                "4:5": (896, 1120)}
SHOW_MAX_BYTES = 200 * 1024


class EngineError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _first(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


def _clip(text: Optional[str], n: int) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= n else text[: n - 1] + "…"


def random_seed() -> int:
    return random.SystemRandom().randrange(0, 2**31 - 1)


# ------------------------------------------------------------- compact views
# What the agent endpoints return: small, id-first, no file paths or bulky
# arrays (waveforms, analysis). The UI endpoints return the full rows.

def asset_view(asset: dict[str, Any]) -> dict[str, Any]:
    recipe = asset.get("recipe") or {}
    params = recipe.get("params") or {}
    name = _clip(asset.get("name"), 60)
    prompt = params.get("positive_prompt") or recipe.get("text")
    if prompt and name and prompt.startswith(name.rstrip("…")):
        prompt = None  # the name already is the start of the prompt
    summary = {k: v for k, v in {
        "operation": recipe.get("operation"), "template": recipe.get("template"), "seed": params.get("seed"),
        "prompt": _clip(prompt, 120), "inputs": (recipe.get("input_asset_ids") or [])[:4] or None,
    }.items() if v is not None}
    view = {
        "id": asset["id"], "kind": asset["kind"], "name": name, "source": asset["source"],
        "width": asset.get("width"), "height": asset.get("height"),
        "duration_s": round(asset["duration_s"], 2) if asset.get("duration_s") else None,
        "tags": asset.get("tags") or [], "rating": asset.get("rating", 0), "favourite": asset.get("favourite", False),
        "notes": _clip(asset.get("notes"), 200), "created_at": asset.get("created_at"),
    }
    if summary:
        view["recipe"] = summary
    return {k: v for k, v in view.items() if v not in (None, [], "")} | {"id": asset["id"]}


def job_view(job: dict[str, Any]) -> dict[str, Any]:
    outputs = job.get("outputs") or {}
    asset_ids = outputs.get("asset_ids") or ([outputs["asset_id"]] if outputs.get("asset_id") else [])
    view = {
        "id": job["id"], "type": job["type"], "state": job["state"], "progress": round(float(job.get("progress") or 0), 3),
        "message": job.get("message"), "project_id": job.get("project_id"), "asset_ids": asset_ids,
        "created_at": job.get("created_at"), "finished_at": job.get("finished_at"),
    }
    if job["state"] in ("queued", "waiting_gpu", "running"):
        view["hint"] = "poll with studio_job(job_id, wait_s=30)"
    if job["state"] == "failed":
        view["error"] = job.get("message")
    if job["state"] == "done" and job["type"] in _RESULT_JOB_TYPES:
        # jobs whose answer is data, not assets (identity scores, captions,
        # a trained adapter): the agent needs it in the job view itself
        result = {k: v for k, v in outputs.items() if k not in ("asset_ids", "asset_id")}
        if len(json.dumps(result, default=str)) > 4000:
            result = {k: v for k, v in result.items() if not isinstance(v, list)} | {"truncated": True}
        view["result"] = result
    return {k: v for k, v in view.items() if v is not None}


_RESULT_JOB_TYPES = ("character_identity", "character_caption", "train_lora", "character_sheet")


# ---------------------------------------------------------------- prompts

_WORD = re.compile(r"[\w'-]", re.UNICODE)


def _mention_aliases(characters: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """(alias, character) longest first. Every character answers to its
    full name ("Iris Volt"), the name without spaces or with _ / - ("IrisVolt",
    "Iris_Volt"), and its first word when no other character shares it."""
    aliases: dict[str, list[dict[str, Any]]] = {}

    def add(alias: str, char: dict[str, Any]) -> None:
        alias = alias.lower().strip()
        if alias and char not in aliases.setdefault(alias, []):
            aliases[alias].append(char)

    for c in characters:
        name = re.sub(r"\s+", " ", c["name"].strip())
        add(name, c)
        add(name.replace(" ", ""), c)
        add(name.replace(" ", "_"), c)
        add(name.replace(" ", "-"), c)
    firsts: dict[str, list[dict[str, Any]]] = {}
    for c in characters:
        first = c["name"].strip().split()[0].lower() if c["name"].strip() else ""
        if first:
            firsts.setdefault(first, []).append(c)
    for first, chars in firsts.items():
        if len(chars) == 1:
            add(first, chars[0])
    unique = [(a, cs[0]) for a, cs in aliases.items() if len(cs) == 1]
    return sorted(unique, key=lambda ac: -len(ac[0]))


ELEMENTS = ("character", "location", "prop")
_ELEMENT_WORD = {"character": "character", "location": "place", "prop": "object"}


def element_of(char: dict[str, Any]) -> str:
    """What a cast entry is: a character (default), a location or a prop.
    All three are @mentionable and carry a look and a reference image."""
    value = char.get("element") or "character"
    return value if value in ELEMENTS else "character"


def _scan_mentions(store: Store, project_id: str, prompt: str) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """Split a prompt into text pieces and mention hits. Returns (pieces,
    matched entries in first-mention order, unknown names); a piece is a
    str or a cast entry dict. An `@` only starts a mention at the start of
    the text or after a non-word character, so e-mail addresses are left
    alone; the longest matching name wins ("@Iris Volt" over "@Iris"), and
    the match must end at a word boundary ("@Irisa" is not "@Iris")."""
    aliases = _mention_aliases(store.list_characters(project_id))
    pieces: list[Any] = []
    matched: list[dict[str, Any]] = []
    unknown: list[str] = []
    buf: list[str] = []
    i, n = 0, len(prompt)
    while i < n:
        ch = prompt[i]
        if ch == "@" and (i == 0 or not _WORD.match(prompt[i - 1])):
            rest = prompt[i + 1:]
            low = rest.lower()
            hit = None
            for alias, char in aliases:
                if low.startswith(alias) and (len(rest) == len(alias) or not _WORD.match(rest[len(alias)])):
                    hit = (alias, char)
                    break
            if hit:
                alias, char = hit
                if buf:
                    pieces.append("".join(buf))
                    buf = []
                pieces.append(char)
                if all(m["id"] != char["id"] for m in matched):
                    matched.append(char)
                i += 1 + len(alias)
                continue
            m = re.match(r"[\w-]+", rest, re.UNICODE)
            if m:
                unknown.append(m.group(0))
        buf.append(ch)
        i += 1
    if buf:
        pieces.append("".join(buf))
    return pieces, matched, sorted(set(unknown))


def expand_mentions(store: Store, project_id: str, prompt: str) -> dict[str, Any]:
    """Replace @Name with the cast entry's prompt fragment (its look).
    Returns {"expanded_prompt", "negative_extra", "reference_asset_id",
    "matched_characters", "matched_elements", "element_references",
    "unknown_mentions"}: `matched_characters` are the characters,
    `matched_elements` the locations and props, `element_references` the
    reference images of the mentioned locations/props ({name, element,
    asset_id}), and `reference_asset_id` the first mentioned character's
    canonical image (else the first element's)."""
    pieces, matched, unknown = _scan_mentions(store, project_id, prompt)
    out = [p if isinstance(p, str) else (p.get("prompt") or p["name"]).strip() for p in pieces]
    chars = [m for m in matched if element_of(m) == "character"]
    others = [m for m in matched if element_of(m) != "character"]
    reference = next((m["canonical_asset_id"] for m in chars if m.get("canonical_asset_id")), None) \
        or next((m["canonical_asset_id"] for m in others if m.get("canonical_asset_id")), None)
    return {
        "expanded_prompt": "".join(out),
        "negative_extra": ", ".join(m["negative"] for m in matched if m.get("negative")),
        "reference_asset_id": reference,
        "matched_characters": [m["name"] for m in chars],
        "matched_elements": [m["name"] for m in others],
        "element_references": [{"name": m["name"], "element": element_of(m), "asset_id": m["canonical_asset_id"]}
                               for m in others if m.get("canonical_asset_id")],
        "unknown_mentions": unknown,
    }


def element_reference_notes(refs: list[dict[str, Any]], first_index: int) -> str:
    """'<image3>: the place Stage - keep its layout...; ...' for the
    location/prop references added after a prompt's own images."""
    notes = []
    for i, ref in enumerate(refs):
        what = ("the place {n}: keep its layout, architecture, lighting and colours" if ref["element"] == "location"
                else "the object {n}: keep its exact design, shape and colours")
        notes.append(f"<image{first_index + i}>: " + what.format(n=ref["name"]))
    return "; ".join(notes)


def add_element_references(composed: dict[str, Any], refs: list[str], engine_name: str,
                           template: Optional[str] = None, use: bool = True) -> list[dict[str, Any]]:
    """Put the mentioned places/props with an image after the call's own
    references (`refs`, image_1 first) and number them in the positive
    prompt - on Qwen-Image 2.1 only, which reads several references; the
    same rule for a real render and for the Generate screen's preview.
    Mutates `composed["positive_prompt"]`; returns the references added."""
    if not use or engine_name != "qwen21" or (template and template != "qwen21_edit"):
        return []
    added = [r for r in composed.get("element_references") or [] if r["asset_id"] not in refs]
    added = added[:max(0, 10 - len(refs))]
    if added:
        notes = element_reference_notes(added, len(refs) + 1)
        composed["positive_prompt"] = composed["positive_prompt"].rstrip(" .,") + ". " + notes
    return added


def compose_prompt(store: Store, project_id: str, prompt: str, negative: Optional[str], style_id: Optional[str]) -> dict[str, Any]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise EngineError("empty_prompt", "the prompt is empty; describe the image (mention characters as @Name)")
    if len(prompt) > 4000:
        raise EngineError("prompt_too_long", "the prompt is longer than 4000 characters")
    store.get_project(project_id)
    expansion = expand_mentions(store, project_id, prompt)
    prefix = suffix = ""
    style_negative = ""
    defaults: dict[str, Any] = {}
    style_name = None
    if style_id:
        style = _find_style(store, project_id, style_id)
        style_name = style["name"]
        prefix = style.get("prompt_prefix") or ""
        suffix = style.get("prompt_suffix") or ""
        style_negative = style.get("negative") or ""
        defaults = style.get("defaults") or {}
    positive = " ".join(p for p in (prefix.strip(), expansion["expanded_prompt"].strip()) if p)
    if suffix.strip():
        positive = positive.rstrip(" ,") + (suffix if suffix.startswith(",") else " " + suffix)
    positive = re.sub(r"\s*,(\s*,)+", ",", positive)
    negative_parts: list[str] = []
    for part in (negative, expansion["negative_extra"], style_negative):
        for piece in (part or "").split(","):
            piece = piece.strip()
            if piece and piece.lower() not in {p.lower() for p in negative_parts}:
                negative_parts.append(piece)
    return {
        "positive_prompt": re.sub(r"\s+", " ", positive).strip(),
        "negative_prompt": ", ".join(negative_parts),
        "reference_asset_id": expansion["reference_asset_id"],
        "matched_characters": expansion["matched_characters"],
        "matched_elements": expansion["matched_elements"],
        "element_references": expansion["element_references"],
        "unknown_mentions": expansion["unknown_mentions"],
        "style": style_name,
        "style_defaults": defaults,
    }


CROP_PRESETS = {"full": (0.0, 0.0, 1.0, 1.0), "left_third": (0.0, 0.0, 1 / 3, 1.0),
                "middle_third": (1 / 3, 0.0, 1 / 3, 1.0), "right_third": (2 / 3, 0.0, 1 / 3, 1.0)}


def apply_canonical_crop(store: Store, project_id: str, fields: dict[str, Any],
                         current_canonical: Optional[str] = None) -> dict[str, Any]:
    """"Cast -> Reference sheet -> mark the canonical crop": a turnaround
    sheet shows the character three times (front / three-quarter / back),
    and handing all three to Kontext as *the* reference invites a triptych
    back. `fields["canonical_crop"]` - a preset name (full, left_third,
    middle_third, right_third) or `[x, y, w, h]` as fractions of the image -
    crops `canonical_asset_id` (or the current canonical) into a new image
    asset (lineage: operation "crop", derived_from the sheet, the box) that
    becomes the canonical reference; the sheet is kept in
    reference_asset_ids. Returns the fields to store (no canonical_crop)."""
    fields = dict(fields)
    crop = fields.pop("canonical_crop", None)
    if crop is None or crop == "full":
        return fields
    if isinstance(crop, str):
        if crop not in CROP_PRESETS:
            raise EngineError("bad_crop", f"canonical_crop must be one of {', '.join(CROP_PRESETS)} or [x, y, w, h] fractions")
        box = CROP_PRESETS[crop]
    else:
        try:
            box = tuple(float(v) for v in crop)
        except (TypeError, ValueError):
            box = ()
        if len(box) != 4 or not (0 <= box[0] < 1 and 0 <= box[1] < 1 and 0 < box[2] <= 1 and 0 < box[3] <= 1
                                 and box[0] + box[2] <= 1.0001 and box[1] + box[3] <= 1.0001):
            raise EngineError("bad_crop", "canonical_crop [x, y, w, h] must be fractions of the image (0-1) inside it")
    source_id = fields.get("canonical_asset_id") or current_canonical
    if not source_id:
        raise EngineError("bad_crop", "canonical_crop needs a canonical_asset_id (the reference sheet) to crop")
    src = store.get_asset(source_id)
    if src["kind"] != "image" or src["project_id"] != project_id:
        raise EngineError("bad_crop", f"asset {source_id} is not an image of this project")
    with Image.open(store.data_dir / src["file_path"]) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        w, h = img.size
        left, top = int(round(box[0] * w)), int(round(box[1] * h))
        right, bottom = min(w, int(round((box[0] + box[2]) * w))), min(h, int(round((box[1] + box[3]) * h)))
        if right - left < 64 or bottom - top < 64:
            raise EngineError("bad_crop", "the crop is smaller than 64 px; pick a bigger area")
        cropped = img.crop((left, top, right, bottom))
    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ".png")
    cropped.save(dest)
    make_thumbnail(dest, store.path_for_thumb(asset_id))
    recipe = {"operation": "crop", "backend": "local", "derived_from": src["id"], "input_asset_ids": [src["id"]],
              "box": [round(v, 4) for v in box], "created_at": now_iso()}
    asset = store.create_asset(project_id=project_id, kind="image", file_path=_rel(store, dest), mime="image/png",
                               width=cropped.width, height=cropped.height, thumb_path=_rel(store, store.path_for_thumb(asset_id)),
                               source="generated", recipe=recipe, asset_id=asset_id,
                               name=_clip(f"canonical crop of {src.get('name') or src['id']}", 80))
    refs = list(fields.get("reference_asset_ids") or [])
    if src["id"] not in refs:
        refs.append(src["id"])
    fields["reference_asset_ids"] = refs
    fields["canonical_asset_id"] = asset["id"]
    return fields


def build_kontext_instruction(store: Store, project_id: str, prompt: str, engine: str = "flux",
                              extra_count: int = 0) -> dict[str, Any]:
    """For `consistent=true` generation ("Cast -> Reference sheet"): turn
    "@Name doing X" into an edit instruction ("the same character from the
    reference, now doing X") instead of inlining the character's full look
    description the way `compose_prompt` does for a fresh txt2img - the
    reference image already carries the look, so the instruction should
    describe only the change. Mentions are resolved the same way (longest
    name, word boundaries, unknowns kept).

    The first mentioned character with a canonical image is the reference
    (`reference_asset_id`, image_1). `engine="qwen21"` phrases it
    Qwen-Image 2.1's way (`<image1>`, `<image2>`...) and gives every other
    mentioned entry with an image - a second character, a location, a prop
    - its own numbered reference: they come back in `reference_asset_ids`
    and number after image_1 and the caller's own `extra_count` references
    (which sit right after image_1). Entries without an image, and every
    extra entry on a single-reference engine, are inlined by their look."""
    pieces, matched, unknown = _scan_mentions(store, project_id, prompt)
    chars = [m for m in matched if element_of(m) == "character"]
    primary = next((m for m in chars if m.get("canonical_asset_id")), None)
    multi = engine == "qwen21"
    numbered: dict[str, int] = {}
    refs: list[str] = []
    if multi:
        nxt = 2 + max(0, extra_count)
        for m in matched:
            if m is primary or not m.get("canonical_asset_id") or nxt > 10:
                continue
            numbered[m["id"]] = nxt
            refs.append(m["canonical_asset_id"])
            nxt += 1
    several = bool(numbered)
    out: list[str] = []
    for piece in pieces:
        if isinstance(piece, str):
            out.append(piece)
        elif primary is not None and piece["id"] == primary["id"]:
            # alone, the reference is implied ("now dancing"); with others
            # in the scene it has to be named so each one keeps its action
            out.append("the character from <image1>" if several else "")
        elif piece["id"] in numbered:
            out.append(f"the {_ELEMENT_WORD[element_of(piece)]} from <image{numbered[piece['id']]}>")
        else:
            out.append((piece.get("prompt") or piece["name"]).strip())
    scene = re.sub(r"\s+", " ", "".join(out)).strip(" ,")
    # both engines follow explicit preservation best ("keep X, change Y"):
    # name what must not drift, then the new scene
    if multi:
        # the design must not drift, the pose must: "silhouette" here used
        # to freeze every shot in the reference image's stance
        keep = ("Keep the character from <image1> exactly the same design (face, body shape, colours, props), "
                "in a new pose and action as described")
        keeps = [keep] if primary is not None else []
        for m in matched:
            if m["id"] not in numbered:
                continue
            k, word = numbered[m["id"]], _ELEMENT_WORD[element_of(m)]
            if word == "character":
                keeps.append(f"keep the character from <image{k}> exactly the same design too")
            elif word == "place":
                keeps.append(f"set it in the place from <image{k}> (same layout, architecture, lighting and colours)")
            else:
                keeps.append(f"the object from <image{k}> keeps its exact design, shape and colours")
        keep = "; ".join(keeps)
    else:
        keep = "the same character from the reference image, with exactly the same design, proportions and colours"
    instruction = (f"{keep}, now {scene}" if keep else scene) if scene else keep
    return {
        "instruction": instruction, "reference_asset_id": primary["canonical_asset_id"] if primary else None,
        "reference_asset_ids": refs, "matched_characters": [m["name"] for m in chars],
        "matched_elements": [m["name"] for m in matched if element_of(m) != "character"],
        "unknown_mentions": unknown,
    }


def _find_style(store: Store, project_id: str, style: str) -> dict[str, Any]:
    """A style preset by id or by (case-insensitive) name."""
    try:
        return store.get_style_preset(style)
    except NotFound:
        pass
    presets = store.list_style_presets(project_id)
    for p in presets:
        if p["name"].lower() == style.strip().lower():
            return p
    raise EngineError("unknown_style", f"unknown style '{style}'; available: {', '.join(p['name'] for p in presets)}")


# -------------------------------------------------------------- ComfyUI --

def check_vram_or_wait(backend: Backend, spec: dict[str, Any]) -> None:
    estimate = needed = comfy_driver.estimate_vram_mb(spec, backend.vram_estimates_mb())
    free = backend.vram_free_mb()
    total_of = getattr(backend, "vram_total_mb", None)
    total = total_of() if total_of is not None and free is not None and free < needed else None
    if total and needed > total * VRAM_CARD_SHARE:
        # a model bigger than this card ever frees (the desktop and drivers
        # keep a slice) would wait forever; ComfyUI offloads what does not
        # fit, so ask only for most of the card
        needed = int(total * VRAM_CARD_SHARE)
    if free is not None and free < needed:
        manages = getattr(backend, "comfy_manages_memory", None)
        if manages is not None and manages(estimate):
            return
        raise WaitingForResources(
            f"waiting for {needed} MB of free VRAM for a {spec.get('vram_class', 'sdxl')} job ({free} MB free now); "
            "retrying every 15 s for up to 30 min. Nothing is unloaded automatically - use Backends > Free ComfyUI "
            "memory if you want to make room."
        )


def _comfy(backend: Backend, autostart: Optional[bool] = None):
    """The ComfyUI client, or `Unavailable` with the reason. When ComfyUI is
    a local server that is off, it is started first (standalone, no Faustus
    needed) on a GPU worker thread, or here when `autostart=True` (a request
    that is about to queue a job); status and listing calls never start it."""
    wants = getattr(backend, "wants_comfy_autostart", None)
    if wants is not None and wants(autostart):
        backend.ensure_comfy()
    try:
        comfy = backend.comfy()
    except Exception as exc:  # noqa: BLE001 - resolver failures become one readable reason
        raise Unavailable("image", [f"ComfyUI could not be resolved: {exc}"]) from exc
    if comfy is None and _comfy_port_busy(backend):
        # the server is up (its port listens) but too busy to answer the
        # probe - loading a large model, or a heavy step on a shared card. A
        # GPU job waits for it instead of failing on the spot.
        deadline = time.monotonic() + COMFY_BUSY_WAIT_S
        while comfy is None and time.monotonic() < deadline and _comfy_port_busy(backend):
            time.sleep(COMFY_BUSY_POLL_S)
            try:
                comfy = backend.comfy()
            except Exception:  # noqa: BLE001
                comfy = None
    if comfy is None:
        res = backend.link.sync.resolve("image")
        raise Unavailable("image", (res.details or {}).get("reasons") or [res.reason or "ComfyUI is not reachable"])
    return comfy


# how long a GPU job waits for a ComfyUI that listens but does not answer
COMFY_BUSY_WAIT_S = 600.0
VRAM_CARD_SHARE = 0.85  # the most of a card's memory a job ever waits for (see check_vram_or_wait)
COMFY_BUSY_POLL_S = 5.0


def _comfy_port_busy(backend: Any) -> bool:
    """True on a GPU worker whose ComfyUI port accepts connections (the
    server is alive, only slow). Request threads never wait."""
    bound = getattr(backend, "_bound", None)
    if not getattr(bound, "worker", False):
        return False
    url = getattr(bound, "url", None)
    if not url:
        port = getattr(backend, "main_comfy_port", lambda: None)()
        if port is None:
            return False
        url = f"http://127.0.0.1:{port}"
    from .hoard_link.launch import _port_open

    return _port_open(url, timeout=1.0)


# How long a job may take on ComfyUI. A render on a busy or shared card
# takes far longer than on an idle one (a 5 s Wan clip: ~10 min on an idle
# 16 GB card, 40+ min when a language model spills onto the same GPU), so
# the wait is generous per output kind and PROSPERO_COMFY_TIMEOUT_S
# overrides it for every kind.
COMFY_TIMEOUT_S = {"video": 3600.0, "audio": 1800.0}
COMFY_TIMEOUT_DEFAULT_S = 1200.0


def comfy_timeout_s(kind: Optional[str]) -> float:
    raw = os.environ.get("PROSPERO_COMFY_TIMEOUT_S", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return COMFY_TIMEOUT_S.get(kind or "", COMFY_TIMEOUT_DEFAULT_S)


async def _cancel_comfy_prompt(comfy: Any, prompt_id: str) -> str:
    """Stop only this job's prompt on a (possibly shared) ComfyUI.

    A bare ``/interrupt`` stops whatever is executing, which may be another
    job's prompt while ours still waits in ComfyUI's queue. Read the queue
    first: interrupt only when our prompt is the one running (targeted by id),
    dequeue it when pending, and do nothing when it already left the queue.
    The vendored client stays untouched; its transport is reused as-is.
    """
    response = await comfy._client.get(comfy.url + "/queue", timeout=5.0)
    response.raise_for_status()
    queue = response.json() if response.content else {}

    def ids(key: str) -> set:
        items = queue.get(key) if isinstance(queue, dict) else None
        return {item[1] for item in items or [] if isinstance(item, (list, tuple)) and len(item) > 1}

    if prompt_id in ids("queue_running"):
        (await comfy._client.post(comfy.url + "/interrupt", json={"prompt_id": prompt_id},
                                  timeout=5.0)).raise_for_status()
        return "interrupted"
    if prompt_id in ids("queue_pending"):
        (await comfy._client.post(comfy.url + "/queue", json={"delete": [prompt_id]},
                                  timeout=5.0)).raise_for_status()
        return "dequeued"
    return "not_queued"


def _queue_tolerant(backend: Any, comfy: Any, workflow: dict[str, Any], client_id: str) -> str:
    """POST the prompt, riding out a ComfyUI too busy to answer in time. A
    timed-out POST may still have been accepted, so before sending it again
    look for our client_id in ComfyUI's queue - never queue the same render
    twice."""
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            return backend.run_async(comfy.queue(workflow, client_id))
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
        for _ in range(6):  # up to ~30 s to see whether it got in after all
            time.sleep(COMFY_BUSY_POLL_S)
            found = _queued_prompt_for(backend, comfy, client_id)
            if found:
                return found
    raise last if last else RuntimeError("could not queue the prompt")


def _queued_prompt_for(backend: Any, comfy: Any, client_id: str) -> Optional[str]:
    async def look() -> Optional[str]:
        resp = await comfy._client.get(comfy.url + "/queue", timeout=10.0)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        for key in ("queue_running", "queue_pending"):
            for item in data.get(key) or []:
                if (isinstance(item, (list, tuple)) and len(item) > 3 and isinstance(item[3], dict)
                        and item[3].get("client_id") == client_id):
                    return str(item[1])
        return None

    try:
        return backend.run_async(look())
    except Exception:  # noqa: BLE001 - still too busy; the caller tries again
        return None


def _run_comfy_workflow(backend: Backend, workflow: dict[str, Any], uploads: list[tuple[bytes, str]],
                         progress: Callable[..., None], timeout_s: float, output_node: Optional[str]) -> list:
    comfy = _comfy(backend)
    for data, name in uploads:
        for attempt in range(4):  # same name overwrites, so a retry is harmless
            try:
                backend.run_async(comfy.upload_image(data, name))
                break
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 3:
                    raise
                time.sleep(COMFY_BUSY_POLL_S)
    client_id = str(uuid.uuid4())
    record = getattr(progress, "record_comfy_submission", None)
    receipt = {"phase": "intent", "client_id": client_id,
               "workflow_sha256": hashlib.sha256(json.dumps(workflow, sort_keys=True).encode("utf-8")).hexdigest()}
    if record is not None:
        endpoint = urlsplit(comfy.url)
        host = endpoint.hostname or ""
        host = f"[{host}]" if ":" in host else host
        receipt["endpoint"] = urlunsplit((endpoint.scheme, host + (f":{endpoint.port}" if endpoint.port else ""),
                                          endpoint.path, "", ""))
        record(receipt)  # durable commit precedes the potentially accepted POST
    prompt_id = _queue_tolerant(backend, comfy, workflow, client_id)
    if record is not None:
        record(dict(receipt, phase="accepted", prompt_id=prompt_id))
    deadline = time.monotonic() + timeout_s
    cancelled = getattr(progress, "cancelled", lambda: False)
    while True:
        if cancelled():
            try:
                backend.run_async(_cancel_comfy_prompt(comfy, prompt_id))
            except Exception:  # pragma: no cover - best effort
                pass
            raise JobCancelled("cancelled")
        try:
            backend.run_async(comfy.wait(prompt_id, timeout_s=2.0, poll_interval_s=0.5))
            break
        except (TimeoutError, httpx.TimeoutException, httpx.TransportError) as exc:
            # TimeoutError: not finished yet. An httpx timeout or dropped
            # connection: ComfyUI is too busy to answer /history right now
            # (a model loading, a heavy step) - the render is still going,
            # so keep polling until the job's own deadline instead of failing
            if not isinstance(exc, TimeoutError) and time.monotonic() <= deadline:
                time.sleep(1.0)
            if time.monotonic() > deadline:
                # a render given up on must not stay on the card: it would
                # keep the GPU (and any language model sharing it) busy for
                # an output nobody collects
                try:
                    stopped = backend.run_async(_cancel_comfy_prompt(comfy, prompt_id))
                except Exception:  # noqa: BLE001 - best effort
                    stopped = "unknown"
                what = {"interrupted": "stopped it there", "dequeued": "took it out of ComfyUI's queue"}.get(
                    stopped, "it may still be running there - check ComfyUI's queue")
                raise EngineError("comfy_timeout", f"ComfyUI did not finish job {prompt_id} within {int(timeout_s)} s; "
                                  f"{what}. On a shared or busy GPU free it first (the header shows who holds it) "
                                  "or raise PROSPERO_COMFY_TIMEOUT_S") from None
    for attempt in range(6):
        try:
            outputs = backend.run_async(comfy.outputs(prompt_id))
            break
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == 5:
                raise
            time.sleep(COMFY_BUSY_POLL_S)
    saved = [o for o in outputs if o.type == "output"]
    if output_node:
        preferred = [o for o in saved if o.node_id == output_node]
        saved = preferred or saved
    return saved


def _download_output(backend: Backend, output) -> bytes:
    comfy = _comfy(backend)
    for attempt in range(6):
        try:
            return backend.run_async(comfy.download(output))
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == 5:
                raise
            time.sleep(COMFY_BUSY_POLL_S)
    raise RuntimeError("unreachable")


_object_info_cache_hash: dict[str, str] = {}


def object_info_cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "comfy" / "object_info.json"


def _object_info(backend: Backend, autostart: Optional[bool] = None) -> dict[str, Any]:
    """The live `/object_info`, also saved to `data/comfy/object_info.json`
    whenever it changes, so UI-format workflows can still be converted while
    ComfyUI is off (see `cached_object_info`)."""
    comfy = _comfy(backend, autostart)
    try:
        info = backend.run_async(comfy.object_info())
    except httpx.TimeoutException as exc:
        # up but too busy to answer (a long render holds it): the node list
        # does not change while it runs, so the last saved copy is as good
        # and the job just waits its turn in ComfyUI's queue
        path = object_info_cache_path(backend.data_dir)
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        raise Unavailable("image", [f"ComfyUI is not answering ({type(exc).__name__}): start it in Backends > "
                                    "Local services (or studio_service_start), or turn on autostart"]) from exc
    except httpx.TransportError as exc:
        raise Unavailable("image", [f"ComfyUI is not answering ({type(exc).__name__}): start it in Backends > "
                                    "Local services (or studio_service_start), or turn on autostart"]) from exc
    try:
        if info:
            blob = json.dumps(info, sort_keys=True)
            digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
            path = object_info_cache_path(backend.data_dir)
            if _object_info_cache_hash.get(str(path)) != digest:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(blob, encoding="utf-8")
                replace_with_retry(tmp, path)
                _object_info_cache_hash[str(path)] = digest
    except OSError:
        pass  # the cache is a convenience; never fail a job over it
    return info


def object_info_live_or_cached(backend: Backend) -> dict[str, Any]:
    """For converting a UI-format workflow: live when ComfyUI answers, else the
    last copy saved by `_object_info`."""
    try:
        return _object_info(backend)
    except Exception as live_exc:  # noqa: BLE001
        path = object_info_cache_path(backend.data_dir)
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
        raise RuntimeError(str(live_exc) or "ComfyUI is not reachable") from live_exc


def _svd_size(width: int, height: int) -> tuple[int, int]:
    if width > height * 1.2:
        return 1024, 576
    if height > width * 1.2:
        return 576, 1024
    return 768, 768


def run_template(store: Store, backend: Backend, job: dict[str, Any], progress: Callable[..., None], *,
                 template_name: str, values: dict[str, Any], operation: str, count: int = 1,
                 reference_asset_id: Optional[str] = None, reference_asset_ids: Optional[list[str]] = None,
                 mask_asset_id: Optional[str] = None, driving_asset_id: Optional[str] = None,
                 driving_start_s: float = 0.0, audio_asset_id: Optional[str] = None, audio_start_s: float = 0.0,
                 audio_seconds: Optional[float] = None,
                 extra_recipe: Optional[dict[str, Any]] = None, name: Optional[str] = None) -> dict[str, Any]:
    """Run one workflow template `count` times (seed, seed+1, ...) and import
    each output as an asset whose recipe can re-run it exactly."""
    project_id = job["project_id"]
    try:
        workflow, spec = comfy_driver.load_template(template_name, store.data_dir)
    except comfy_driver.WorkflowError as exc:
        raise EngineError("unknown_template", str(exc)) from exc
    started = time.monotonic()
    progress(0.02, "checking free VRAM")
    check_vram_or_wait(backend, spec)
    progress(0.05, "validating against ComfyUI")
    object_info = _object_info(backend)
    try:
        values = comfy_driver.validate_against_object_info(spec, values, object_info, workflow)
    except comfy_driver.ValidationError as exc:
        raise EngineError("comfy_validation", str(exc)) from exc

    uploads: list[tuple[bytes, str]] = []
    input_ids: list[str] = []
    group_names: list[str] = []
    reference_group = spec.get("reference_group")
    if reference_group:
        refs = list(reference_asset_ids or ([reference_asset_id] if reference_asset_id else []))
        refs = [r for r in refs if r]
        minimum = int(reference_group.get("min", 1))
        maximum = int(reference_group.get("max", len(reference_group.get("nodes") or []) or 10))
        if len(refs) < minimum:
            raise EngineError("reference_required",
                              f"template '{template_name}' needs at least {minimum} reference image(s) (reference_asset_ids)")
        if len(refs) > maximum:
            raise EngineError("too_many_references", f"template '{template_name}' accepts at most {maximum} reference images")
        for ref_id in refs:
            ref = store.get_asset(ref_id)
            if ref["kind"] != "image":
                raise EngineError("reference_not_image", f"reference {ref['id']} is {ref['kind']}, not an image")
            ref_path = store.data_dir / ref["file_path"]
            ref_name = f"prospero_{ref['id']}{ref_path.suffix}"
            uploads.append((ref_path.read_bytes(), ref_name))
            group_names.append(ref_name)
            input_ids.append(ref["id"])
    elif spec.get("requires_reference"):
        if not reference_asset_id:
            raise EngineError("reference_required", f"template '{template_name}' needs a reference image (reference_asset_id)")
        ref = store.get_asset(reference_asset_id)
        if ref["kind"] != "image":
            raise EngineError("reference_not_image", f"reference {ref['id']} is {ref['kind']}, not an image")
        ref_path = store.data_dir / ref["file_path"]
        ref_name = f"prospero_{ref['id']}{ref_path.suffix}"
        uploads.append((ref_path.read_bytes(), ref_name))
        input_ids.append(ref["id"])
    if spec.get("requires_mask"):
        if not mask_asset_id:
            raise EngineError("mask_required", "inpainting needs mask_asset_id (a black/white image, white = repaint)")
        mask = store.get_asset(mask_asset_id)
        mask_path = store.data_dir / mask["file_path"]
        mask_name = f"prospero_{mask['id']}_mask{mask_path.suffix}"
        uploads.append((mask_path.read_bytes(), mask_name))
        input_ids.append(mask["id"])

    driving_name: Optional[str] = None
    if spec.get("driving_video_node"):
        if not driving_asset_id:
            raise EngineError("driving_video_required", f"template '{template_name}' needs a driving video "
                                                        "(driving_asset_id: the motion to copy)")
        drive = store.get_asset(driving_asset_id)
        if drive["kind"] != "video":
            raise EngineError("driving_not_video", f"driving asset {drive['id']} is {drive['kind']}, not a video")
        fps = float(values.get("fps") or 24)
        seconds = (int(values.get("length") or 81) + 2) / fps
        clip = _driving_clip(store, drive, float(driving_start_s or 0), seconds, fps,
                             int(values.get("width") or 832), int(values.get("height") or 480))
        driving_name = f"prospero_{drive['id']}_{int(float(driving_start_s or 0) * 1000)}.mp4"
        uploads.append((clip, driving_name))
        input_ids.append(drive["id"])

    audio_name: Optional[str] = None
    if spec.get("audio_node"):
        if not audio_asset_id:
            raise EngineError("audio_required", f"template '{template_name}' needs the audio to sing or speak "
                                                "(audio_asset_id, audio_start_s, audio_seconds)")
        sound = store.get_asset(audio_asset_id)
        if sound["kind"] not in ("audio", "video"):
            raise EngineError("audio_not_audio", f"asset {sound['id']} is {sound['kind']}, not audio")
        seconds = float(audio_seconds or 0) or (int(values.get("length") or 77) * int(values.get("chunks") or 1)
                                                / float(values.get("fps") or 16))
        audio_name = f"prospero_{sound['id']}_{int(float(audio_start_s or 0) * 1000)}_{int(seconds * 1000)}.wav"
        uploads.append((_audio_clip(store, sound, float(audio_start_s or 0), seconds), audio_name))
        input_ids.append(sound["id"])

    count = max(1, min(int(count or 1), 8))
    base_seed = int(values.get("seed") if values.get("seed") is not None else random_seed())
    thash = comfy_driver.template_hash(workflow, spec)
    assets = []
    for i in range(count):
        progress.check_cancel() if hasattr(progress, "check_cancel") else None
        seed = base_seed + i
        run_values = {**values, "seed": seed}
        wf = comfy_driver.apply_params(workflow, spec, run_values)
        if run_values.get("loras"):
            # character adapters (charkit.resolve_adapters): part of the
            # recipe's params, so reuse/vary re-inject the same LoRAs
            comfy_driver.inject_loras(wf, run_values["loras"])
        if reference_group:
            comfy_driver.wire_reference_group(wf, spec, group_names)
        elif spec.get("requires_reference"):
            node, _, inp = spec["reference_node"].partition(".")
            wf[node]["inputs"][inp] = uploads[0][1]
        if spec.get("requires_mask"):
            node, _, inp = spec["mask_node"].partition(".")
            wf[node]["inputs"][inp] = next(n for _, n in uploads if n.endswith(f"_mask{Path(n).suffix}"))
        if driving_name:
            node, _, inp = spec["driving_video_node"].partition(".")
            wf[node]["inputs"][inp] = driving_name
        if audio_name:
            node, _, inp = spec["audio_node"].partition(".")
            wf[node]["inputs"][inp] = audio_name
        if spec.get("chunks"):
            comfy_driver.expand_chunks(wf, spec, int(run_values.get("chunks") or 1), seed)
        if i == 0 and object_info:
            # the whole prompt, the way ComfyUI's /prompt will check it: every
            # model file (UNet, text encoders, VAE - not only checkpoints),
            # every combo choice and number range, dynamic-combo children
            problems = convert_mod.validate_values(wf, object_info)
            if problems:
                raise EngineError("comfy_validation", "ComfyUI would reject this workflow: " + "; ".join(problems[:4]))
        progress(0.1 + 0.8 * i / count, f"rendering {i + 1}/{count} on ComfyUI")
        t0 = time.monotonic()
        outputs = _run_comfy_workflow(backend, wf, uploads if i == 0 else [], progress,
                                      timeout_s=comfy_timeout_s(spec.get("kind")),
                                      output_node=spec.get("output_node"))
        if not outputs:
            raise EngineError("no_outputs", "ComfyUI finished but saved no output; check the workflow's Save node")
        for out in outputs:
            data = _download_output(backend, out)
            recipe = {
                "operation": operation, "backend": "comfyui", "template": template_name, "template_hash": thash,
                "checkpoint": run_values.get("checkpoint"), "params": run_values, "input_asset_ids": input_ids,
                "elapsed_s": round(time.monotonic() - t0, 2), "job_id": job.get("id"), "created_at": now_iso(),
                **(extra_recipe or {}),
            }
            assets.append(_import_comfy_output(store, project_id, data, spec.get("kind", "image"), recipe, run_values, name))
    free_after(backend, spec)
    progress(0.97, "imported outputs")
    return {"asset_ids": [a["id"] for a in assets], "elapsed_s": round(time.monotonic() - started, 2)}


def free_after(backend: Any, spec: dict[str, Any]) -> bool:
    """A model that only just fits (`free_after` in its template) leaves the
    server's memory in a state where the next run of it streams at minutes
    per step. On servers that are Prospero's alone (`comfy_dedicated`), the
    next job starts from a clean card. Best effort; True when asked."""
    if not spec.get("free_after") or not getattr(backend, "comfy_dedicated", lambda: False)():
        return False
    try:
        comfy = _comfy(backend)
        backend.run_async(comfy.free(unload_models=True, free_memory=True))
        return True
    except Exception:  # noqa: BLE001 - the render is done either way
        return False


def _driving_clip(store: Store, asset: dict[str, Any], start_s: float, seconds: float, fps: float,
                  width: int, height: int) -> bytes:
    """The part of a driving video a motion-transfer render reads: from
    `start_s`, `seconds` long, resampled to the render's fps and fitted to
    its frame (cropped to fill, so the dancer keeps the framing), no audio."""
    exe = ffmpeg_path()
    if not exe:
        raise EngineError("no_ffmpeg", "ffmpeg is needed to cut the driving video")
    src = store.data_dir / asset["file_path"]
    tmp = store.data_dir / "tmp" / f"{new_id('drv')}.mp4"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"fps={fps:g},scale={width}:{height}:force_original_aspect_ratio=increase,"
          f"crop={width}:{height},setsar=1")
    procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(0.0, start_s):.3f}", "-i", str(src),
                  "-t", f"{seconds:.3f}", "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                  "-pix_fmt", "yuv420p", str(tmp)], timeout=180)
    try:
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise EngineError("driving_cut_failed", f"could not cut {seconds:.1f} s of the driving video at {start_s:.1f} s "
                                                    "(is it long enough?)")
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _audio_clip(store: Store, asset: dict[str, Any], start_s: float, seconds: float) -> bytes:
    """`seconds` of a song (or a video's sound) from `start_s`, as a 16-bit
    stereo WAV - what a lip-sync render listens to. Past the end of the
    song it is padded with silence, so a short last line still renders."""
    exe = ffmpeg_path()
    if not exe:
        raise EngineError("no_ffmpeg", "ffmpeg is needed to cut the audio")
    src = store.data_dir / asset["file_path"]
    tmp = store.data_dir / "tmp" / f"{new_id('aud')}.wav"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(0.0, start_s):.3f}", "-i", str(src),
                  "-t", f"{seconds:.3f}", "-vn", "-af", f"apad=whole_dur={seconds:.3f}", "-ac", "2", "-ar", "44100",
                  "-c:a", "pcm_s16le", str(tmp)], timeout=120)
    try:
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise EngineError("audio_cut_failed", f"could not cut {seconds:.1f} s of audio at {start_s:.1f} s")
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def _import_comfy_output(store: Store, project_id: str, data: bytes, kind: str, recipe: dict[str, Any],
                         values: dict[str, Any], name: Optional[str] = None) -> dict[str, Any]:
    asset_id = new_id("a")
    if kind == "video":
        is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        src = store.path_for_asset_file(asset_id, ".webp" if is_webp else ".mp4")
        src.write_bytes(data)
        dest = store.path_for_asset_file(asset_id, ".mp4")
        fps = float(values.get("fps") or 8)
        if is_webp:
            work = store.data_dir / "tmp" / asset_id
            try:
                video_mod.animated_webp_to_mp4(src, dest, fps, work)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            src.unlink(missing_ok=True)
        duration = audio_mod.probe_duration_s(dest)
        thumb = _video_thumbnail(dest, store.path_for_thumb(asset_id))
        with_size = _probe_video_size(dest)
        return store.create_asset(
            project_id=project_id, kind="video", file_path=_rel(store, dest), mime="video/mp4",
            width=with_size[0], height=with_size[1], duration_s=duration, thumb_path=thumb,
            source="generated", recipe=recipe, asset_id=asset_id, name=_clip(name, 80) or f"{recipe['operation']} {asset_id[-6:]}",
        )
    if kind == "audio":
        # ComfyUI's real ACE-Step returns mp3; the fake backend returns a
        # real WAV (see devtools/fake_comfy.render_fake_song) - sniff the
        # RIFF/WAVE header rather than assuming, same spirit as the webp/mp4
        # sniff above.
        is_wav = data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        dest = store.path_for_asset_file(asset_id, ".wav" if is_wav else ".mp3")
        dest.write_bytes(data)
        duration = audio_mod.probe_duration_s(dest)
        asset = store.create_asset(
            project_id=project_id, kind="audio", file_path=_rel(store, dest), mime="audio/wav" if is_wav else "audio/mpeg",
            duration_s=duration, source="generated", recipe=recipe, asset_id=asset_id,
            name=_clip(name or values.get("tags") or recipe["operation"], 80),
        )
        try:
            store.set_asset_media(asset_id, waveform=audio_mod.waveform_peaks(audio_mod.decode_to_mono(dest)))
        except audio_mod.DecodeError:
            pass
        return asset
    dest = store.path_for_asset_file(asset_id, ".png")
    dest.write_bytes(data)
    with Image.open(dest) as img:
        width, height = img.size
    make_thumbnail(dest, store.path_for_thumb(asset_id))
    return store.create_asset(
        project_id=project_id, kind="image", file_path=_rel(store, dest), mime="image/png", width=width, height=height,
        thumb_path=_rel(store, store.path_for_thumb(asset_id)), source="generated", recipe=recipe, asset_id=asset_id,
        name=_clip(name or values.get("positive_prompt") or recipe["operation"], 80),
    )


def _rel(store: Store, path: Path) -> str:
    return path.relative_to(store.data_dir).as_posix()


def _probe_video_size(path: Path) -> tuple[Optional[int], Optional[int]]:
    exe = ffmpeg_path()
    if not exe:
        return None, None
    out = procutil.run([exe, "-nostdin", "-hide_banner", "-i", str(path)], text=True, timeout=30)
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out.stderr or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _video_thumbnail(path: Path, dest: Path) -> Optional[str]:
    exe = ffmpeg_path()
    if not exe:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".jpg")
    procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", "-ss", "0.3", "-i", str(path), "-frames:v", "1", str(tmp)], timeout=60)
    if not tmp.is_file():
        procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", "-i", str(path), "-frames:v", "1", str(tmp)], timeout=60)
    if not tmp.is_file():
        return None
    make_thumbnail(tmp, dest)
    tmp.unlink(missing_ok=True)
    return dest.relative_to(dest.parent.parent).as_posix()


def extract_frames(store: Store, asset_id: str, count: int = 6, project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Stills out of a video or an animated GIF, evenly spaced, as image
    assets: a dance GIF becomes poses to hand a shot as references."""
    asset = store.get_asset(asset_id)
    if asset["kind"] != "video":
        raise EngineError("not_video", f"asset {asset_id} is {asset['kind']}; frames come out of a video or a GIF")
    if not 1 <= int(count) <= 24:
        raise EngineError("bad_parameter", "count must be between 1 and 24")
    exe = ffmpeg_path()
    if not exe:
        raise EngineError("no_ffmpeg", "ffmpeg is needed to take frames out of a video")
    src = store.data_dir / asset["file_path"]
    duration = float(asset.get("duration_s") or 0) or 1.0
    pid = project_id or asset["project_id"]
    tmp_dir = store.data_dir / "tmp" / "frames"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = []
    n = int(count)
    for i in range(n):
        t = round(duration * (i + 0.5) / n, 3)
        tmp = tmp_dir / f"{new_id('fr')}.png"
        procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(src), "-frames:v", "1", str(tmp)],
                     timeout=60)
        if not tmp.is_file():
            continue
        try:
            out.append(import_asset(store, pid, tmp, "image",
                                    original_name=f"{Path(asset.get('name') or 'video').stem} frame {i + 1}.png",
                                    recipe={"operation": "extract_frame", "backend": "local", "input_asset_ids": [asset_id],
                                            "derived_from": asset_id, "time_s": t},
                                    source="derived"))
        finally:
            tmp.unlink(missing_ok=True)
    if not out:
        raise EngineError("no_frames", "no frame could be read from that video")
    return out


_SAMPLING_KEYS = ("steps", "cfg", "sampler", "scheduler")


def _generation_values(params: dict[str, Any], template_defaults: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Explicit params win; then the template's own `defaults` (a Flux,
    Kontext or Wan template declares the sampler settings it was converted
    with - schnell at 4 steps/cfg 1, not the SDXL 30/6.5 the generic
    fallbacks below are tuned for); then a style preset's defaults (only
    for templates without their own, since a preset is tuned for SDXL);
    then the generic SDXL fallbacks."""
    t = dict(template_defaults or {})
    d = {} if t else (params.get("style_defaults") or {})
    values = {
        "checkpoint": _first(params.get("checkpoint"), d.get("checkpoint")),
        "positive_prompt": params["positive_prompt"],
        "negative_prompt": params.get("negative_prompt") or t.get("negative_prompt") or "",
        "width": _first(params.get("width"), (params.get("style_defaults") or {}).get("width"), t.get("width"), 1024),
        "height": _first(params.get("height"), (params.get("style_defaults") or {}).get("height"), t.get("height"), 1024),
        "batch_size": 1,
        "seed": params.get("seed"),
        "steps": _first(params.get("steps"), t.get("steps"), d.get("steps"), 30),
        "cfg": _first(params.get("cfg"), t.get("cfg"), d.get("cfg"), 6.5),
        "sampler": _first(params.get("sampler"), t.get("sampler"), d.get("sampler"), "dpmpp_2m"),
        "scheduler": _first(params.get("scheduler"), t.get("scheduler"), d.get("scheduler"), "karras"),
        # a template that declares its own denoise wins over the img2img
        # fallback: Qwen-Image 2.1 edit reads its references through the
        # text encoder and samples a fresh latent, so 0.6 there turns the
        # empty canvas into noise texture
        "denoise": _first(params.get("strength"), t.get("denoise"),
                          1.0 if not params.get("reference_asset_id") else 0.6),
    }
    for key, value in t.items():  # template-only knobs (guidance, shift, length, fps, ...)
        values.setdefault(key, value)
    return values


def _size_from_reference(mode: str, ref_w: int, ref_h: int) -> tuple[int, int]:
    """Output size when the caller gave none, following the reference's
    aspect: Kontext keeps it at ~1 MP (multiples of 16); Wan 2.2 TI2V 5B
    uses its native 1280x704 / 704x1280 (960x960 for near-square)."""
    ratio = (ref_w or 1) / float(ref_h or 1)
    if mode == "wan480":  # Wan 2.2 14B / Animate 2 with the 480p distillations
        if ratio > 1.2:
            return 832, 480
        if ratio < 1 / 1.2:
            return 480, 832
        return 640, 640
    if mode == "wan":
        if ratio > 1.2:
            return 1280, 704
        if ratio < 1 / 1.2:
            return 704, 1280
        return 960, 960
    area = 1024 * 1024
    width = int(round(math.sqrt(area * ratio) / 16) * 16)
    height = int(round(math.sqrt(area / ratio) / 16) * 16)
    return max(256, min(2048, width)), max(256, min(2048, height))


# Friendly per-project/per-call image engine choice (spec item 3). "auto"
# is the default everywhere a caller does not name one explicitly.
IMAGE_ENGINES = ("auto", "qwen21", "flux", "sdxl")
ENGINE_TEMPLATES = {
    "qwen21": {"txt2img": "qwen21_txt2img", "edit": "qwen21_edit"},
    "flux": {"txt2img": "flux_schnell_txt2img", "edit": "flux_kontext_edit"},
    "sdxl": {"txt2img": "sdxl_txt2img", "edit": "sdxl_img2img"},
}
TEMPLATE_TO_ENGINE = {tmpl: engine for engine, ops in ENGINE_TEMPLATES.items() for tmpl in ops.values()}
# Generic per-call overrides for template-specific knobs that are not part
# of every template's shape (resolution/custom_size, QwenImage21Cache's
# device/dtype, the separate UNet/CLIP/VAE loader filenames, Kontext's
# guidance...): anything the template's own map exposes and the core
# generation fields below do not already own.
_CORE_GENERATION_KEYS = {"checkpoint", "positive_prompt", "negative_prompt", "width", "height", "batch_size",
                         "seed", "steps", "cfg", "sampler", "scheduler", "denoise"}


def _has_model_file(object_info: dict[str, Any], class_type: str, input_name: str, needle: str) -> bool:
    try:
        entry = object_info[class_type]["input"]["required"][input_name][0]
    except (KeyError, IndexError, TypeError):
        return False
    return isinstance(entry, list) and any(needle in str(f).lower() for f in entry)


def wan14b_installed(object_info: dict[str, Any]) -> bool:
    """Wan 2.2 14B image-to-video: both experts and the 4-step LoRAs."""
    return ("WanImageToVideo" in object_info
            and _has_model_file(object_info, "UNETLoader", "unet_name", "wan2.2_i2v_high_noise")
            and _has_model_file(object_info, "UNETLoader", "unet_name", "wan2.2_i2v_low_noise")
            and _has_model_file(object_info, "LoraLoaderModelOnly", "lora_name", "lightx2v_4steps_lora_v1_high"))


def animate2_installed(object_info: dict[str, Any]) -> bool:
    """Wan Animate 2: the node class and its diffusion model."""
    return "WanAnimate2ToVideo" in object_info and _has_model_file(object_info, "UNETLoader", "unet_name", "wan_animate_2")


def animate2_unet(object_info: dict[str, Any]) -> Optional[str]:
    """The Wan Animate 2 diffusion file to load: an fp8 cast when there is
    one (it streams from system memory at full speed on a 12-16 GB card,
    where the int8 release crawls at minutes per step), else None - the
    template's own default."""
    try:
        files = object_info["UNETLoader"]["input"]["required"]["unet_name"][0]
    except (KeyError, IndexError, TypeError):
        return None
    fp8 = sorted(str(f) for f in files or [] if "wan_animate_2" in str(f).lower() and "fp8" in str(f).lower())
    distilled = [f for f in fp8 if "distill" in f.lower()]
    return (distilled or fp8 or [None])[0]


def s2v_installed(object_info: dict[str, Any]) -> bool:
    """Lip sync is possible: the S2V node class, its diffusion file and an
    audio encoder are all there."""
    if "WanSoundImageToVideo" not in (object_info or {}):
        return False
    try:
        enc = object_info["AudioEncoderLoader"]["input"]["required"]["audio_encoder_name"]
        encoders = enc[1].get("options") if enc and enc[0] == "COMBO" else enc[0]
    except (KeyError, IndexError, TypeError, AttributeError):
        encoders = []
    return (_has_model_file(object_info, "UNETLoader", "unet_name", "s2v") and bool(encoders)
            and _has_model_file(object_info, "LoraLoaderModelOnly", "lora_name", "lightx2v_4steps_lora_v1.1"))


def clip_template(object_info: dict[str, Any], motion_ref: bool = False) -> str:
    """The clip template a still gets: motion copied from a driving video
    (Wan Animate 2) when there is one and it is installed, else the 14B
    image-to-video (real motion and camera moves), else the 5B."""
    if motion_ref and animate2_installed(object_info):
        return "wan_animate2"
    if wan14b_installed(object_info):
        return "wan22_i2v_14b"
    return "wan22_ti2v"


def resolve_image_engine(object_info: dict[str, Any], requested: Optional[str] = None) -> str:
    """`auto|qwen21|flux|sdxl` (spec item 3) -> the engine this call
    actually gets. `auto` picks Qwen-Image 2.1 when its node class
    (`TextEncodeQwenImage21`) *and* a matching model file are installed,
    else Flux schnell, else SDXL - the last two always work, since they
    ship as built-in checkpoints/templates. An engine requested by name
    that turns out not to be installed falls back the same way, so a
    project already set to "qwen21" keeps rendering before the model
    finishes downloading."""
    requested = (requested or "auto").lower()
    if requested not in IMAGE_ENGINES:
        requested = "auto"
    has_qwen = "TextEncodeQwenImage21" in object_info and _has_model_file(object_info, "UNETLoader", "unet_name", "qwen")
    has_flux = _has_model_file(object_info, "CheckpointLoaderSimple", "ckpt_name", "flux")
    if requested == "sdxl":
        return "sdxl"
    if requested == "flux":
        return "flux" if has_flux else "sdxl"
    if requested in ("qwen21", "auto"):
        if has_qwen:
            return "qwen21"
        if requested == "qwen21" and not has_flux:
            return "sdxl"
        return "flux" if has_flux else "sdxl"
    return "sdxl"


def generate_image(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    template = params.get("template")
    engine_name = TEMPLATE_TO_ENGINE.get(template)
    if not template:
        is_edit = bool(params.get("reference_asset_id") or params.get("reference_asset_ids"))
        engine_name = resolve_image_engine(_object_info(backend), params.get("engine"))
        template = ENGINE_TEMPLATES[engine_name]["edit" if is_edit else "txt2img"]
    try:
        _, spec = comfy_driver.load_template(template, store.data_dir)
    except comfy_driver.WorkflowError as exc:
        raise EngineError("unknown_template", str(exc)) from exc
    values = _generation_values(params, spec.get("defaults"))
    for key in spec.get("map", {}):
        if key not in _CORE_GENERATION_KEYS and params.get(key) is not None:
            values[key] = params[key]
    if params.get("loras"):
        values["loras"] = list(params["loras"])
    if template == "wan_animate2" and not params.get("unet_name"):
        unet = animate2_unet(_object_info(backend))
        if unet:
            values["unet_name"] = unet
    if template == "qwen21_edit" and params.get("custom_size") is None and (params.get("width") or params.get("height")):
        values["custom_size"] = True
    if spec.get("chunks"):
        # one generation is `length` frames; a longer line chains extend steps
        fps, length = float(values.get("fps") or 16), int(values.get("length") or 77)
        wanted = float(params.get("audio_seconds") or 0) * fps
        values["chunks"] = max(1, min(int(spec["chunks"].get("max") or 4), math.ceil((wanted + 3) / length) if wanted else 1))
    size_mode = spec.get("size_from_reference")
    if size_mode and params.get("reference_asset_id") and not (params.get("width") and params.get("height")):
        ref = store.get_asset(params["reference_asset_id"])
        values["width"], values["height"] = _size_from_reference(size_mode, ref.get("width") or 1024, ref.get("height") or 1024)
    return run_template(
        store, backend, job, progress, template_name=template, values=values,
        operation="generate_image", count=params.get("count", 1), reference_asset_id=params.get("reference_asset_id"),
        reference_asset_ids=params.get("reference_asset_ids"),
        driving_asset_id=params.get("driving_asset_id"), driving_start_s=float(params.get("driving_start_s") or 0),
        audio_asset_id=params.get("audio_asset_id"), audio_start_s=float(params.get("audio_start_s") or 0),
        audio_seconds=float(params["audio_seconds"]) if params.get("audio_seconds") else None,
        extra_recipe={"prompt": params.get("prompt"), "style": params.get("style"),
                      "matched_characters": params.get("matched_characters") or [],
                      "image_engine": engine_name or "custom"},
        name=params.get("prompt") or params["positive_prompt"],
    )


_EDIT_TEMPLATES = {"img2img": "sdxl_img2img", "inpaint": "sdxl_inpaint", "hires": "sdxl_hires",
                   "upscale": "esrgan_upscale", "remove_background": "birefnet_remove_background"}

# The two operations that run on ComfyUI core nodes with a model file and no
# prompt: they take the source asset as reference and always make one output.
MODEL_EDIT_OPERATIONS = ("upscale", "remove_background")
UPSCALE_SCALES = (2, 4)
UPSCALE_MODEL_DEFAULT = "RealESRGAN_x4plus.safetensors"
UPSCALE_MODEL_FACTOR = 4  # the ESRGAN model enlarges 4x; ImageScaleBy brings it down to the asked factor
MAX_UPSCALE_SIDE = 8192
# operation -> (node class, combo input, ComfyUI models folder, where to get the file)
_MODEL_FILES = {
    "upscale": ("UpscaleModelLoader", "model_name", "models/upscale_models",
                "https://huggingface.co/Comfy-Org/Real-ESRGAN_repackaged"),
    "remove_background": ("LoadBackgroundRemovalModel", "bg_removal_name", "models/background_removal",
                          "https://huggingface.co/Comfy-Org/BiRefNet"),
}


def _source_size(store: Store, src: dict[str, Any]) -> Optional[tuple[int, int]]:
    if src.get("width") and src.get("height"):
        return int(src["width"]), int(src["height"])
    try:
        with Image.open(store.data_dir / src["file_path"]) as img:
            return img.size
    except (OSError, KeyError, ValueError):
        return None


def check_upscale_request(store: Store, src: dict[str, Any], scale: Any) -> int:
    """Validate an `upscale` edit before it is queued: `scale` is 2 or 4
    (default 2) and the result may not exceed 8192 px on a side. Returns
    the integer scale."""
    if scale is None:
        scale = 2
    if isinstance(scale, bool) or not isinstance(scale, int) or scale not in UPSCALE_SCALES:
        raise EngineError("bad_parameter", f"scale must be {' or '.join(str(s) for s in UPSCALE_SCALES)}, got {scale!r}")
    size = _source_size(store, src)
    if size and max(size) * scale > MAX_UPSCALE_SIDE:
        w, h = size
        raise EngineError("too_large", f"upscaling the {w}x{h} source by x{scale} would give {w * scale}x{h * scale}; "
                                       f"the maximum is {MAX_UPSCALE_SIDE} px on a side"
                                       + (" (try x2)" if scale == 4 and max(size) * 2 <= MAX_UPSCALE_SIDE else ""))
    return scale


def require_model_file(backend: Backend, operation: str, wanted: str) -> str:
    """The installed file name for `wanted`, or an actionable `model_missing`
    error naming the ComfyUI folder and what is installed. Skipped (returns
    `wanted`) when ComfyUI does not know the loader node at all: the node
    check of `run_template` then says to update ComfyUI."""
    class_type, input_name, folder, source = _MODEL_FILES[operation]
    info = _object_info(backend)
    if not info or class_type not in info:
        return wanted
    options = comfy_driver.combo_choices(info, class_type, input_name)
    found = comfy_driver.resolve_checkpoint(wanted, options, None, operation) if options else None
    if found is None:
        installed = ", ".join(options) if options else "none"
        raise EngineError("model_missing", f"model not installed: '{wanted}' is not in ComfyUI's {folder} folder; "
                                           f"download it from {source} into ComfyUI/{folder}, or choose one of the "
                                           f"installed files: {installed}")
    return found


def _edit_with_model(store: Store, backend: Backend, job: dict[str, Any], progress, src: dict[str, Any],
                     operation: str) -> dict[str, Any]:
    params = job["params"]
    label = src.get("name") or src["id"]
    if src["kind"] != "image":
        raise EngineError("not_an_image", f"asset {src['id']} is {src['kind']}; edits need an image")
    if operation == "upscale":
        scale = check_upscale_request(store, src, params.get("scale"))
        model = require_model_file(backend, operation, params.get("model") or UPSCALE_MODEL_DEFAULT)
        values = {"upscale_model": model, "scale_by": scale / UPSCALE_MODEL_FACTOR, "scale": scale,
                  "seed": params.get("seed")}
        # A cut-out keeps its transparency: the alpha variant puts the source
        # alpha back on the upscaled pixels (the plain one would bring back
        # the background colours hidden under the transparent area).
        template = UPSCALE_ALPHA_TEMPLATE if _has_transparency(store, src) else _EDIT_TEMPLATES[operation]
        return run_template(store, backend, job, progress, template_name=template, values=values,
                            operation="edit_image:upscale", count=1, reference_asset_id=src["id"],
                            extra_recipe={"derived_from": src["id"]}, name=f"upscaled x{scale}: {label}")
    model = require_model_file(backend, operation, backend.bg_removal_model())
    result = run_template(store, backend, job, progress, template_name=_EDIT_TEMPLATES[operation],
                          values={"bg_model": model, "seed": params.get("seed")},
                          operation="edit_image:remove_background", count=1, reference_asset_id=src["id"],
                          extra_recipe={"derived_from": src["id"]}, name=f"background removed: {label}")
    # A scene with no clear subject comes back fully transparent. Say so
    # instead of handing back an empty picture as if the cut-out worked.
    coverage = _opaque_share(store, result["asset_ids"][0]) if result.get("asset_ids") else None
    if coverage is not None:
        result["foreground_share"] = round(coverage, 4)
        if coverage < MIN_FOREGROUND_SHARE:
            result["warning"] = ("no_subject_found: the background-removal model kept almost nothing "
                                 f"({coverage * 100:.1f} % of the pixels); the image may have no clear subject")
    return result


MIN_FOREGROUND_SHARE = 0.01
UPSCALE_ALPHA_TEMPLATE = "esrgan_upscale_alpha"


def _has_transparency(store: Store, src: dict[str, Any]) -> bool:
    """True when the source image has at least one pixel that is not fully opaque."""
    try:
        with Image.open(store.data_dir / src["file_path"]) as img:
            if img.mode == "P":
                return "transparency" in img.info
            if "A" not in img.getbands():
                return False
            return img.getchannel("A").getextrema()[0] < 255
    except (OSError, KeyError, ValueError):
        return False


def _opaque_share(store: Store, asset_id: str) -> Optional[float]:
    """Share of pixels whose alpha is at least half opaque, or None if unreadable."""
    try:
        asset = store.get_asset(asset_id)
        with Image.open(store.data_dir / asset["file_path"]) as img:
            if "A" not in img.getbands():
                return 1.0
            alpha = img.getchannel("A")
            histogram = alpha.histogram()
            total = sum(histogram)
            return sum(histogram[128:]) / total if total else None
    except (OSError, KeyError, ValueError):
        return None


def edit_image(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    operation = params["operation"]
    src = store.get_asset(params["asset_id"])
    if operation in ("reuse", "vary"):
        return rerun_recipe(store, backend, job, progress, src, vary=operation == "vary", seed=params.get("seed"),
                            count=params.get("count", 1))
    if operation in MODEL_EDIT_OPERATIONS:
        return _edit_with_model(store, backend, job, progress, src, operation)
    template = _EDIT_TEMPLATES[operation]
    src_params = (src.get("recipe") or {}).get("params") or {}
    values = {
        "checkpoint": src_params.get("checkpoint"),
        "positive_prompt": _first(params.get("prompt"), src_params.get("positive_prompt"), ""),
        "negative_prompt": _first(params.get("negative_prompt"), src_params.get("negative_prompt"), ""),
        "seed": params.get("seed"),
        "steps": _first(params.get("steps"), src_params.get("steps"), 30),
        "cfg": _first(params.get("cfg"), src_params.get("cfg"), 6.5),
        "sampler": _first(params.get("sampler"), src_params.get("sampler"), "dpmpp_2m"),
        "scheduler": _first(params.get("scheduler"), src_params.get("scheduler"), "karras"),
        "denoise": _first(params.get("strength"), 1.0 if operation == "inpaint" else 0.55),
    }
    if operation == "hires":
        # a "hires fix": the source's own txt2img recipe is re-run at its base
        # size and upscaled in latent space with a second sampling pass, so
        # it only applies to images generated here with SDXL txt2img
        recipe = src.get("recipe") or {}
        if recipe.get("backend") != "comfyui" or recipe.get("template") != "sdxl_txt2img":
            raise EngineError("hires_needs_recipe", "upscale re-runs an SDXL txt2img recipe at a higher resolution; "
                                                    f"asset {src['id']} was not made that way (try img2img instead)")
        w, h = int(src_params.get("width") or src.get("width") or 1024), int(src_params.get("height") or src.get("height") or 1024)
        scale = 1.5
        values.update({
            "negative_prompt": src_params.get("negative_prompt", ""), "width": w, "height": h,
            "seed": src_params.get("seed") if params.get("seed") is None else params.get("seed"),
            "hires_width": _first(params.get("width"), int(round(w * scale / 64) * 64)),
            "hires_height": _first(params.get("height"), int(round(h * scale / 64) * 64)),
            "hires_steps": params.get("hires_steps") or 16, "hires_denoise": _first(params.get("strength"), 0.45),
        })
        values.pop("denoise", None)
        return run_template(store, backend, job, progress, template_name=template, values=values,
                            operation="edit_image:hires", extra_recipe={"derived_from": src["id"]},
                            name=f"upscaled: {src.get('name') or src['id']}")
    return run_template(store, backend, job, progress, template_name=template, values=values,
                        operation=f"edit_image:{operation}", count=params.get("count", 1),
                        reference_asset_id=src["id"], mask_asset_id=params.get("mask_asset_id"),
                        name=f"{operation}: {src.get('name') or src['id']}")


def rerun_recipe(store: Store, backend: Backend, job: dict[str, Any], progress, src: dict[str, Any], vary: bool,
                 seed: Optional[int] = None, count: int = 1) -> dict[str, Any]:
    """"Reuse recipe" (same seed: reproduces the asset on the same backend)
    or "vary seed" (same recipe, new seed)."""
    recipe = src.get("recipe") or {}
    if recipe.get("backend") != "comfyui" or not recipe.get("template"):
        raise EngineError("not_reproducible", f"asset {src['id']} was not generated on ComfyUI ({recipe.get('operation') or src['source']}); "
                                              "only generated images and animations can be re-run")
    values = dict(recipe.get("params") or {})
    if vary:
        values["seed"] = seed if seed is not None else random_seed()
    inputs = recipe.get("input_asset_ids") or []
    ref = inputs[0] if inputs else None
    mask = inputs[1] if len(inputs) > 1 else None
    current = None
    try:
        workflow, spec = comfy_driver.load_template(recipe["template"], store.data_dir)
        current = comfy_driver.template_hash(workflow, spec)
    except comfy_driver.WorkflowError:
        pass
    result = run_template(
        store, backend, job, progress, template_name=recipe["template"], values=values,
        operation=recipe.get("operation") or "generate_image", count=count if vary else 1,
        reference_asset_id=ref, mask_asset_id=mask,
        extra_recipe={"derived_from": src["id"], "rerun": "vary" if vary else "reuse",
                      **({k: recipe[k] for k in ("prompt", "style", "matched_characters") if k in recipe})},
        name=f"{'vary' if vary else 'reuse'}: {src.get('name') or src['id']}",
    )
    if current and recipe.get("template_hash") and current != recipe["template_hash"]:
        result["note"] = "the workflow template changed since this asset was made; the result may differ"
    return result


def animate_image(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    src = store.get_asset(params["asset_id"])
    if src["kind"] != "image":
        raise EngineError("not_an_image", f"asset {src['id']} is {src['kind']}; animate needs an image")
    width, height = _svd_size(src.get("width") or 1024, src.get("height") or 576)
    frames = int(_first(params.get("frames"), 14))
    fps = int(_first(params.get("fps"), 7))
    motion = int(_first(params.get("motion"), 127))
    if not 4 <= frames <= 50 or not 1 <= fps <= 30 or not 1 <= motion <= 255:
        raise EngineError("bad_animation_params", "frames must be 4-50, fps 1-30 and motion 1-255")
    values = {
        "checkpoint": params.get("checkpoint") or "svd_xt.safetensors", "width": width, "height": height,
        "frames": frames, "fps": fps, "motion": motion, "augmentation": 0.0,
        "seed": params.get("seed"), "steps": 20, "cfg": 2.5,
    }
    return run_template(store, backend, job, progress, template_name="svd_img2vid", values=values,
                        operation="animate", reference_asset_id=src["id"], name=f"animated: {src.get('name') or src['id']}")


def compose_song(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    """`studio_compose`: a song with vocals and lyrics via the ACE-Step 1.5
    template, the same job-queue/lineage/VRAM-wait machinery as an image or
    video generation."""
    params = job["params"]
    if not str(params.get("tags") or "").strip():
        raise EngineError("empty_tags", "tags describe the sound (genre, mood, instruments, vocal style)")
    if not str(params.get("lyrics") or "").strip():
        raise EngineError("empty_lyrics", "lyrics are required (use [Section] tags in English, e.g. [Verse], [Chorus])")
    duration = float(_first(params.get("duration"), 120))
    if not 4 <= duration <= 240:
        raise EngineError("bad_parameter", "duration must be between 4 and 240 seconds")
    bpm = int(_first(params.get("bpm"), 120))
    if not 40 <= bpm <= 220:
        raise EngineError("bad_parameter", "bpm must be between 40 and 220")
    values = {
        "checkpoint": params.get("checkpoint"), "tags": params["tags"], "lyrics": params["lyrics"],
        "bpm": bpm, "duration": duration, "timesignature": str(_first(params.get("time_signature"), 4)),
        "language": _first(params.get("language"), "en"), "key": _first(params.get("key"), "C major"),
        "seed": params.get("seed"), "steps": 8, "cfg": 1,
    }
    return run_template(
        store, backend, job, progress, template_name="ace15_song", values=values, operation="compose_song",
        count=params.get("count", 1),
        extra_recipe={"tags": params["tags"], "bpm": bpm, "key": values["key"], "language": values["language"]},
        name=params.get("name") or params["tags"][:60],
    )


# ------------------------------------------------------------------ i/o --

def make_thumbnail(src: Path, dest: Path, size: int = 512) -> None:
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (18, 14, 22))
            bg.paste(rgba, mask=rgba.split()[3])
            img = bg
        else:
            img = img.convert("RGB")
        img.thumbnail((size, size))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="WEBP", quality=85)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_import_path(backend: Backend, store: Store, raw_path: str) -> Path:
    """The only way a client-supplied path reaches the filesystem. The path
    is made absolute (relative paths are taken from `data/inbox/`),
    symlinks and `..` are resolved, and the result must be a regular file
    inside one of the allowed import folders and outside the app's own
    database/asset folders."""
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise EngineError("bad_path", "give the absolute path of a local file")
    candidate = Path(raw_path.strip().strip('"')).expanduser()
    inbox = (store.data_dir / "inbox")
    if not candidate.is_absolute():
        inbox.mkdir(parents=True, exist_ok=True)
        candidate = inbox / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise EngineError("file_not_found", f"no such file: {raw_path[:200]}") from None
    if not resolved.is_file():
        raise EngineError("not_a_file", f"{raw_path[:200]} is not a regular file")
    roots = backend.import_roots()
    if not any(_inside(resolved, r) for r in roots):
        raise EngineError(
            "outside_import_folders",
            f"{resolved} is outside the folders Prospero may import from ({', '.join(str(r) for r in roots)}). "
            "Move the file into one of them or add its folder in Settings > Import folders.",
        )
    data_root = store.data_dir.resolve()
    if _inside(resolved, data_root) and not _inside(resolved, (data_root / "inbox")):
        raise EngineError("inside_app_data", "files inside the app's own data folder cannot be imported (only data/inbox/)")
    return resolved


def _detect_kind(ext: str, kind_hint: Optional[str]) -> str:
    inferred = next((k for k, exts in KIND_EXTS.items() if ext in exts), None)
    if kind_hint:
        if kind_hint not in KIND_EXTS:
            raise EngineError("unsupported_kind", f"kind must be one of {', '.join(KIND_EXTS)}")
        if ext not in KIND_EXTS[kind_hint]:
            raise EngineError("kind_mismatch", f"a '{ext or 'no extension'}' file cannot be imported as {kind_hint}; "
                                               f"{kind_hint} files are {', '.join(sorted(KIND_EXTS[kind_hint]))}")
        return kind_hint
    if inferred is None:
        allowed = sorted(set().union(*KIND_EXTS.values()))
        raise EngineError("unsupported_kind", f"cannot import '{ext or 'no extension'}' files; supported: {', '.join(allowed)}")
    return inferred


def _validate_content(path: Path, kind: str) -> dict[str, Any]:
    """Proves the bytes are what the extension claims (so an arbitrary file
    renamed to .png or .lrc cannot be pulled into the library and served)."""
    size = path.stat().st_size
    info: dict[str, Any] = {}
    if kind == "image":
        if size > MAX_IMAGE_BYTES:
            raise EngineError("too_large", f"images are limited to {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        try:
            with Image.open(path) as img:
                if img.width * img.height > MAX_IMAGE_PIXELS:
                    raise EngineError("too_large", f"image is {img.width}x{img.height}; the limit is {MAX_IMAGE_PIXELS // 1_000_000} megapixels")
                img.verify()
            with Image.open(path) as img:
                info["format"] = (img.format or "").lower()
                img = ImageOps.exif_transpose(img)
                info["width"], info["height"] = img.size
        except EngineError:
            raise
        except Exception:
            raise EngineError("invalid_image", f"{path.name} is not a readable image") from None
    elif kind in ("audio", "video"):
        if size > MAX_MEDIA_BYTES:
            raise EngineError("too_large", "audio and video files are limited to 2 GB")
        duration = audio_mod.probe_duration_s(path)
        if not duration:
            raise EngineError(f"invalid_{kind}", f"{path.name} is not a readable {kind} file (ffmpeg could not read a duration)")
        info["duration_s"] = duration
    elif kind == "lyrics":
        if size > MAX_LYRICS_BYTES:
            raise EngineError("too_large", "lyrics files are limited to 512 KB")
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise EngineError("invalid_lyrics", f"{path.name} is not UTF-8 text") from None
        if "\x00" in text:
            raise EngineError("invalid_lyrics", f"{path.name} is not a text file")
        info["text"] = text
    elif kind == "font":
        if size > MAX_FONT_BYTES:
            raise EngineError("too_large", "fonts are limited to 20 MB")
        try:
            from PIL import ImageFont

            ImageFont.truetype(str(path), 20)
        except Exception:
            raise EngineError("invalid_font", f"{path.name} is not a TrueType/OpenType font") from None
    return info


_MIME = {"image": None, "audio": None, "video": None, "lyrics": "text/plain; charset=utf-8", "font": None}


def import_asset(store: Store, project_id: str, source_path: Path, kind_hint: Optional[str] = None,
                 original_name: Optional[str] = None, recipe: Optional[dict[str, Any]] = None,
                 tags: Optional[list[str]] = None, source: str = "import") -> dict[str, Any]:
    """Copy an already-authorised local file (see resolve_import_path) or a
    finished upload into the library. `recipe`/`tags`/`source` let a
    pipeline that fetched the file itself (stock footage, a mixed
    soundtrack) record where it came from."""
    store.get_project(project_id)
    name = Path(original_name or source_path.name).name
    ext = Path(name).suffix.lower()
    kind = _detect_kind(ext, kind_hint)
    info = _validate_content(source_path, kind)

    asset_id = new_id("a")
    stored_ext = ".lrc" if kind == "lyrics" else ext
    dest = store.path_for_asset_file(asset_id, stored_ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "lyrics":
        dest.write_text(info["text"].replace("\r\n", "\n"), encoding="utf-8")
    else:
        shutil.copyfile(source_path, dest)

    thumb_path = None
    mime = _MIME.get(kind) or mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"
    if kind == "image":
        thumb = store.path_for_thumb(asset_id)
        make_thumbnail(dest, thumb)
        thumb_path = _rel(store, thumb)
        mime = Image.MIME.get((info.get("format") or "").upper(), mime)
    elif kind == "video":
        thumb_path = _video_thumbnail(dest, store.path_for_thumb(asset_id))
        info["width"], info["height"] = _probe_video_size(dest)
    elif kind == "audio":
        mime = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}.get(ext, mime)

    asset = store.create_asset(
        project_id=project_id, kind=kind, file_path=_rel(store, dest), mime=mime,
        width=info.get("width"), height=info.get("height"), duration_s=info.get("duration_s"), thumb_path=thumb_path,
        source=source, asset_id=asset_id, name=name, recipe=recipe, tags=tags,
    )
    if kind == "audio":
        try:
            samples = audio_mod.decode_to_mono(dest)
            store.set_asset_media(asset_id, waveform=audio_mod.waveform_peaks(samples))
            asset = store.get_asset(asset_id)
        except audio_mod.DecodeError:
            pass
    return asset


def create_lyrics(store: Store, project_id: str, text: str, name: Optional[str] = None,
                  recipe: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    store.get_project(project_id)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_LYRICS_BYTES:
        raise EngineError("bad_lyrics", "lyrics must be text of at most 512 KB")
    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ".lrc")
    dest.write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return store.create_asset(project_id=project_id, kind="lyrics", file_path=_rel(store, dest),
                              mime="text/plain; charset=utf-8", source="derived", asset_id=asset_id,
                              name=(name or "Lyrics")[:120], recipe=recipe)


def save_lyrics(store: Store, asset_id: str, text: str) -> dict[str, Any]:
    asset = store.get_asset(asset_id)
    if asset["kind"] != "lyrics":
        raise EngineError("not_lyrics", f"asset {asset_id} is {asset['kind']}; only lyrics assets can be edited as text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_LYRICS_BYTES:
        raise EngineError("bad_lyrics", "lyrics must be text of at most 512 KB")
    (store.data_dir / asset["file_path"]).write_text(text.replace("\r\n", "\n"), encoding="utf-8")
    return {"asset_id": asset_id, "lines": audio_mod.parse_lrc(text), "lrc": text}


def read_lyrics(store: Store, asset_id: str) -> dict[str, Any]:
    asset = store.get_asset(asset_id)
    if asset["kind"] != "lyrics":
        raise EngineError("not_lyrics", f"asset {asset_id} is {asset['kind']}, not lyrics")
    text = (store.data_dir / asset["file_path"]).read_text(encoding="utf-8")
    sung, sections = audio_mod.lrc_sections(audio_mod.parse_lrc(text), float("inf"))
    for sec in sections:
        if sec["end_s"] == float("inf"):
            sec["end_s"] = None  # "until the end of the song" - resolved against the song's duration
    return {"asset_id": asset_id, "text": text, "lines": sung, "sections": sections}


def time_lyrics(store: Store, project_id: str, song_asset_id: str, lyrics: str, name: Optional[str] = None) -> dict[str, Any]:
    """`studio_time_lyrics`: a first-pass LRC for `lyrics` (with `[Section]`
    tags) timed to the song's bars (see `audio.time_lyrics`), saved as a
    lyrics asset whose section markers the auto-cut follows."""
    store.get_project(project_id)
    song = store.get_asset(song_asset_id)
    if song["kind"] != "audio":
        raise EngineError("not_audio", f"song_asset_id {song_asset_id} is {song['kind']}, not audio")
    if not isinstance(lyrics, str) or not lyrics.strip():
        raise EngineError("empty_lyrics", "lyrics are empty; paste them with [Verse]/[Chorus] section tags")
    analysis = analyze_audio(store, song_asset_id)
    bpm = ((song.get("recipe") or {}).get("params") or {}).get("bpm")
    try:
        timed = audio_mod.time_lyrics(lyrics, analysis, bpm=bpm)
    except ValueError as exc:
        raise EngineError("bad_lyrics", str(exc)) from None
    asset = create_lyrics(store, project_id, timed["lrc"], name or f"{song.get('name') or 'Song'} - timed lyrics",
                          recipe={"operation": "time_lyrics", "backend": "local", "input_asset_ids": [song_asset_id],
                                  "derived_from": song_asset_id, "method": "section tags + bar grid",
                                  "created_at": now_iso()})
    return {"id": asset["id"], "lines": len(timed["lines"]), "sections": timed["sections"],
            "note": "estimated from the lyrics' [Section] tags and the song's bars, not from the vocals: "
                    "re-time by ear in Audio > Lyrics timing before a final render"}


# --------------------------------------------------------------- design --

def _asset_resolver(store: Store):
    def resolve(asset_id: Optional[str]) -> Optional[Path]:
        if not asset_id or not isinstance(asset_id, str):
            return None
        try:
            asset = store.get_asset(asset_id)
        except NotFound:
            return None
        if asset["kind"] != "image":
            return None
        return store.data_dir / asset["file_path"]
    return resolve


def _check_design_fields(store: Store, template: str, fields: dict[str, Any]) -> dict[str, Any]:
    spec = design_templates.TEMPLATE_FIELDS.get(template)
    if spec is None:
        raise EngineError("unknown_template",
                          f"unknown design template '{template}'; use one of {', '.join(design_templates.TEMPLATE_FIELDS)}")
    if not isinstance(fields, dict):
        raise EngineError("bad_fields", "fields must be an object")
    clean = {}
    internal = {"holo_seed", "monogram"}
    for key, value in fields.items():
        if key not in spec and key not in internal:
            raise EngineError("unknown_field", f"template '{template}' has no field '{key}'. Fields: {design_templates.fields_hint(template)}")
        if value is None or value == "":
            continue
        ftype = spec.get(key, ("text",))[0]
        if ftype == "image":
            try:
                asset = store.get_asset(str(value))
            except NotFound:
                raise EngineError("unknown_asset", f"field '{key}': asset '{value}' does not exist") from None
            if asset["kind"] != "image":
                raise EngineError("not_an_image", f"field '{key}': asset {value} is {asset['kind']}, not an image")
        elif ftype == "colour":
            try:
                design._hex(value)
            except design.DesignError as exc:
                raise EngineError("bad_colour", f"field '{key}': {exc}") from None
        else:
            if isinstance(value, (list, tuple)):  # e.g. tracks as a list: one per line
                value = "\n".join(str(v) for v in value)
            value = str(value)[:2000]
        clean[key] = value
    missing = [f for f, (_, required, _) in spec.items() if required and f not in clean and f not in ("image", "cover_image")]
    if missing:
        raise EngineError("missing_fields", f"template '{template}' needs: {', '.join(missing)}. Fields: {design_templates.fields_hint(template)}")
    return clean


def render_design(store: Store, project_id: str, template: str, fields: dict[str, Any], variant: Optional[str] = None,
                  print_mode: bool = False) -> dict[str, Any]:
    store.get_project(project_id)
    fields = _check_design_fields(store, template, fields)
    try:
        layout = design_templates.get_layout(template, variant)
    except ValueError as exc:
        raise EngineError("bad_variant", str(exc)) from None
    started = time.monotonic()
    img = design.render_layout(layout, fields, _asset_resolver(store))
    data = design.image_bytes(img, print_mode=print_mode)
    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ".png")
    dest.write_bytes(data)
    make_thumbnail(dest, store.path_for_thumb(asset_id))
    with Image.open(dest) as out:
        width, height = out.size
    input_ids = [v for k, v in fields.items() if design_templates.TEMPLATE_FIELDS[template].get(k, ("text",))[0] == "image"]
    recipe = {"operation": "design", "template": template, "variant": variant, "fields": fields, "print": print_mode,
              "input_asset_ids": input_ids, "elapsed_s": round(time.monotonic() - started, 2), "created_at": now_iso()}
    title = fields.get("member_name") or fields.get("title") or fields.get("quote") or fields.get("group_name") or template
    return store.create_asset(
        project_id=project_id, kind="image", file_path=_rel(store, dest), mime="image/png", width=width, height=height,
        thumb_path=_rel(store, store.path_for_thumb(asset_id)), source="rendered", recipe=recipe, asset_id=asset_id,
        name=_clip(f"{template.replace('_', ' ')} - {title}", 80), tags=[template],
    )


def preview_design(store: Store, template: str, fields: dict[str, Any], variant: Optional[str] = None, max_side: int = 720) -> bytes:
    """Reduced-size JPEG of a layout for the Designer's live preview. Nothing
    is stored."""
    fields = _check_design_fields(store, template, {k: v for k, v in (fields or {}).items() if v not in (None, "")} or {})
    layout = design_templates.get_layout(template, variant)
    scale = min(1.0, max_side / max(layout["width"], layout["height"]))
    img = design.render_layout(_scale_layout(layout, scale), fields, _asset_resolver(store))
    bg = Image.new("RGB", img.size, (18, 14, 22))
    bg.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    bg.save(buf, format="JPEG", quality=86)
    return buf.getvalue()


def _scale_layout(layout: dict[str, Any], s: float) -> dict[str, Any]:
    if s >= 0.999:
        return layout
    out = json.loads(json.dumps(layout))
    out["width"] = max(1, int(layout["width"] * s))
    out["height"] = max(1, int(layout["height"] * s))
    if out.get("radius"):
        out["radius"] = max(1, int(out["radius"] * s))
    for layer in out["layers"]:
        for key in ("x", "y", "w", "h", "size", "radius", "width", "inset"):
            if isinstance(layer.get(key), (int, float)) and not isinstance(layer.get(key), bool):
                layer[key] = max(1, int(layer[key] * s)) if layer[key] else 0
        if isinstance(layer.get("shadow"), dict):
            layer["shadow"] = {**layer["shadow"], "dx": int(layer["shadow"].get("dx", 0) * s), "dy": int(layer["shadow"].get("dy", 4) * s)}
    return out


def contact_sheet(image_paths: list[Path], cols: int = 3, cell: int = 320, labels: Optional[list[str]] = None) -> Image.Image:
    cols = max(1, min(cols, len(image_paths) or 1))
    rows = (len(image_paths) + cols - 1) // cols
    label_h = 26 if labels else 0
    sheet = Image.new("RGB", (cols * cell, rows * (cell + label_h)), (18, 14, 22))
    draw = ImageDraw.Draw(sheet)
    for i, path in enumerate(image_paths):
        with Image.open(path) as img:
            img = img.convert("RGBA")
            img.thumbnail((cell - 16, cell - 16))
            x = (i % cols) * cell + (cell - img.width) // 2
            y = (i // cols) * (cell + label_h) + (cell - img.height) // 2
            flat = Image.new("RGB", img.size, (18, 14, 22))
            flat.paste(img, mask=img.split()[3])
            sheet.paste(flat, (x, y))
        if labels:
            lx = (i % cols) * cell + 8
            ly = (i // cols) * (cell + label_h) + cell - 2
            draw.text((lx, ly), labels[i][:40], fill=(235, 225, 240), font=design.get_font("space-grotesk", 16))
    return sheet


def _save_sheet(store: Store, project_id: str, sheet: Image.Image, recipe: dict[str, Any], name: str) -> dict[str, Any]:
    sheet_id = new_id("a")
    path = store.path_for_asset_file(sheet_id, ".png")
    sheet.save(path, format="PNG")
    make_thumbnail(path, store.path_for_thumb(sheet_id))
    return store.create_asset(
        project_id=project_id, kind="image", file_path=_rel(store, path), mime="image/png", width=sheet.width,
        height=sheet.height, thumb_path=_rel(store, store.path_for_thumb(sheet_id)), source="rendered", recipe=recipe,
        asset_id=sheet_id, name=name, tags=["contact_sheet"],
    )


def _best_image_for(store: Store, project_id: str, char: dict[str, Any]) -> Optional[str]:
    """The member's canonical reference, else their best-rated generated
    image that mentions them, else None."""
    if char.get("canonical_asset_id"):
        return char["canonical_asset_id"]
    for ref in char.get("reference_asset_ids") or []:
        return ref
    candidates = store.list_assets(project_id=project_id, kind="image", query=char["name"], limit=30,
                                   exclude_sources=("rendered",))["items"]
    if candidates:
        return sorted(candidates, key=lambda a: (-(a.get("rating") or 0), a["created_at"]))[0]["id"]
    return None


def photocard_set(store: Store, project_id: str, group_id: str, template_front: str = "photocard_front",
                  template_back: str = "photocard_back", image_asset_ids: Optional[dict[str, str]] = None) -> dict[str, Any]:
    group = store.get_group(group_id)
    if group["project_id"] != project_id:
        raise EngineError("wrong_project", f"group {group_id} belongs to another project")
    if not group["member_ids"]:
        raise EngineError("empty_group", f"group '{group['name']}' has no members; add member_ids with studio_cast")
    image_asset_ids = image_asset_ids or {}
    monogram = "".join(w[0] for w in group["name"].split()[:3]).upper()
    fronts, backs, skipped = [], [], []
    total = len(group["member_ids"])
    for idx, member_id in enumerate(group["member_ids"], start=1):
        char = store.get_character(member_id)
        image_id = image_asset_ids.get(member_id) or _best_image_for(store, project_id, char)
        if not image_id:
            skipped.append(char["name"])
            continue
        accent = (char.get("palette") or group.get("colours") or ["#ff4d8d"])[0]
        front_fields = {"image": image_id, "member_name": char["name"], "role": char.get("role") or "",
                        "group_name": group["name"], "accent": accent}
        back_fields = {"member_name": char["name"], "group_name": group["name"],
                       "message": (char.get("bio") or f"Thank you for loving {group['name']}.")[:160],
                       "serial": f"No. {idx:03d}/{max(total, 1):03d}", "accent": accent}
        if group.get("logo_asset_id"):
            back_fields["group_logo"] = group["logo_asset_id"]
        else:
            back_fields["monogram"] = monogram
        fronts.append(render_design(store, project_id, template_front,
                                    {k: v for k, v in front_fields.items() if k in design_templates.TEMPLATE_FIELDS.get(template_front, {})}))
        backs.append(render_design(store, project_id, template_back,
                                   {k: v for k, v in back_fields.items()
                                    if k in design_templates.TEMPLATE_FIELDS.get(template_back, {}) or k == "monogram"}))
    if not fronts:
        raise EngineError(
            "no_reference_images",
            "no group member has a canonical reference image or a generated image mentioning them; "
            "generate one per member (e.g. studio_generate_image with '@Name portrait'), set canonical_asset_id, "
            "or pass image_asset_ids {character_id: asset_id}",
        )
    ordered = [a for pair in zip(fronts, backs) for a in pair]
    sheet = contact_sheet([store.data_dir / a["file_path"] for a in ordered], cols=min(4, len(ordered)), cell=360,
                          labels=[a["name"] or "" for a in ordered])
    sheet_asset = _save_sheet(store, project_id, sheet,
                              {"operation": "photocard_set", "group_id": group_id, "input_asset_ids": [a["id"] for a in ordered],
                               "created_at": now_iso()}, f"{group['name']} photocard set")
    result = {"asset_ids": [a["id"] for a in ordered], "front_ids": [a["id"] for a in fronts],
              "back_ids": [a["id"] for a in backs], "contact_sheet_id": sheet_asset["id"]}
    if skipped:
        result["skipped_members"] = skipped
        result["note"] = f"no image for {', '.join(skipped)}; generate one and run again"
    return result


def photocard_set_looks(store: Store, project_id: str, character_id: str, cards: list[dict[str, Any]],
                        template_front: str = "photocard_front", template_back: str = "photocard_back",
                        set_name: Optional[str] = None) -> dict[str, Any]:
    """A solo artist's set: one character in several looks (each card its
    own photo, role line, back message and accent), numbered "No. 001/005",
    plus one contact sheet (front and back side by side per card) - the
    shape `photocard_set` gives a group, for a single member."""
    char = store.get_character(character_id)
    if char["project_id"] != project_id:
        raise EngineError("wrong_project", f"character {character_id} belongs to another project")
    if not isinstance(cards, list) or not 1 <= len(cards) <= 12:
        raise EngineError("bad_cards", "cards must be a list of 1-12 {image_asset_id, role, message, accent}")
    total = len(cards)
    label = set_name or char["name"]
    fronts, backs = [], []
    for idx, card in enumerate(cards, start=1):
        if not isinstance(card, dict) or not card.get("image_asset_id"):
            raise EngineError("bad_cards", f"card {idx} needs an image_asset_id")
        accent = card.get("accent") or (char.get("palette") or ["#ff4d8d"])[0]
        front_fields = {"image": card["image_asset_id"], "member_name": char["name"], "role": card.get("role") or char.get("role") or "",
                        "group_name": label, "accent": accent}
        back_fields = {"member_name": char["name"], "group_name": label, "accent": accent,
                       "message": str(card.get("message") or char.get("bio") or "")[:160],
                       "serial": card.get("serial") or f"No. {idx:03d}/{total:03d}",
                       "monogram": char["name"][:1].upper()}
        fronts.append(render_design(store, project_id, template_front,
                                    {k: v for k, v in front_fields.items() if k in design_templates.TEMPLATE_FIELDS.get(template_front, {})}))
        backs.append(render_design(store, project_id, template_back,
                                   {k: v for k, v in back_fields.items()
                                    if k in design_templates.TEMPLATE_FIELDS.get(template_back, {}) or k == "monogram"}))
    ordered = [a for pair in zip(fronts, backs) for a in pair]
    sheet = contact_sheet([store.data_dir / a["file_path"] for a in ordered], cols=min(4, len(ordered)), cell=360,
                          labels=[f"{i // 2 + 1:02d} {'front' if i % 2 == 0 else 'back'}" for i in range(len(ordered))])
    sheet_asset = _save_sheet(store, project_id, sheet,
                              {"operation": "photocard_set", "character_id": character_id,
                               "input_asset_ids": [a["id"] for a in ordered], "created_at": now_iso()},
                              f"{label} photocard set")
    return {"asset_ids": [a["id"] for a in ordered], "front_ids": [a["id"] for a in fronts],
            "back_ids": [a["id"] for a in backs], "contact_sheet_id": sheet_asset["id"]}


# ---------------------------------------------------------------- audio --

def analyze_audio(store: Store, asset_id: str, force: bool = False) -> dict[str, Any]:
    asset = store.get_asset(asset_id)
    if asset["kind"] not in ("audio", "video"):
        raise EngineError("not_audio", f"asset {asset_id} is {asset['kind']}; analysis needs an audio (or video) asset")
    if asset.get("analysis") and not force and asset["analysis"].get("version") == ANALYSIS_VERSION:
        return asset["analysis"]
    path = store.data_dir / asset["file_path"]
    try:
        samples = audio_mod.decode_to_mono(path)
    except audio_mod.DecodeError as exc:
        raise EngineError("decode_failed", f"could not decode {asset.get('name') or asset_id}: {exc}") from None
    result = audio_mod.analyze_samples(samples)
    result["version"] = ANALYSIS_VERSION
    store.set_asset_media(asset_id, waveform=audio_mod.waveform_peaks(samples), analysis=result, duration_s=result["duration_s"])
    return result


ANALYSIS_VERSION = 2


def analysis_view(asset_id: str, analysis: dict[str, Any], max_beats: int = 32) -> dict[str, Any]:
    beats = analysis.get("beat_times") or []
    downs = analysis.get("downbeats") or []
    return {
        "asset_id": asset_id, "duration_s": analysis.get("duration_s"), "tempo_bpm": analysis.get("tempo_bpm"),
        "beat_count": len(beats), "beat_times": beats[:max_beats], "beats_truncated": len(beats) > max_beats,
        "downbeats": downs[: max_beats // 4], "sections": analysis.get("sections") or [], "notes": analysis.get("notes"),
    }


def voice_line(store: Store, backend: Backend, project_id: str, text: str, character_id: Optional[str] = None,
               voice_override: Optional[str] = None, speed: Optional[float] = None) -> dict[str, Any]:
    store.get_project(project_id)
    if not isinstance(text, str) or not text.strip():
        raise EngineError("empty_text", "give the line to speak")
    if len(text) > 1500:
        raise EngineError("text_too_long", "a voice line is limited to 1500 characters; split longer text")
    voice_cfg: dict[str, Any] = {}
    char_name = None
    if character_id:
        char = store.get_character(character_id)
        char_name = char["name"]
        voice_cfg = dict(char.get("voice") or {})
    if voice_override:
        voice_cfg["voice_id"] = voice_override
        voice_cfg.setdefault("backend", "piper")
    if speed:
        if not 0.5 <= float(speed) <= 2.0:
            raise EngineError("bad_speed", "speed must be between 0.5 and 2.0")
        voice_cfg["speed"] = float(speed)
    voices_dir = store.data_dir / "voices"
    try:
        wav_bytes, provider = voices_mod.synthesize(backend, voices_dir, text.strip(), voice_cfg)
    except voices_mod.VoiceError as exc:
        raise EngineError(exc.code, str(exc)) from None
    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ".wav")
    dest.write_bytes(wav_bytes)
    duration_s = audio_mod.probe_duration_s(dest)
    recipe = {"operation": "voice", "provider": provider, "text": text, "voice": voice_cfg, "character_id": character_id,
              "created_at": now_iso()}
    asset = store.create_asset(
        project_id=project_id, kind="audio", file_path=_rel(store, dest), mime="audio/wav",
        duration_s=duration_s, source="generated", recipe=recipe, asset_id=asset_id,
        name=_clip(f"{char_name + ': ' if char_name else ''}{text.strip()}", 80), tags=["voice"],
    )
    try:
        store.set_asset_media(asset_id, waveform=audio_mod.waveform_peaks(audio_mod.decode_to_mono(dest)))
    except audio_mod.DecodeError:
        pass
    return store.get_asset(asset_id)


# ------------------------------------------------------------- timeline --

def _pool(store: Store, project_id: str, asset_ids: Optional[list[str]], board_id: Optional[str]) -> list[dict[str, Any]]:
    if asset_ids:
        pool = []
        for a in asset_ids:
            asset = store.get_asset(a)
            if asset["project_id"] != project_id:
                raise EngineError("wrong_project", f"asset {a} belongs to another project")
            pool.append(asset)
    elif board_id:
        board = store.get_board(board_id)
        pool = [store.get_asset(item["asset_id"]) for item in board["items"]]
    else:
        # generated/imported pictures and clips; rendered designs (cards,
        # covers, contact sheets) only when asked for explicitly
        pool = [a for a in store.list_assets(project_id=project_id, limit=60, exclude_sources=("rendered",))["items"]
                if a["kind"] in ("image", "video")]
    pool = [a for a in pool if a["kind"] in ("image", "video")]
    if not pool:
        raise EngineError("empty_pool", "no image or video assets to cut; generate or import some, or pass asset_ids / board_id")
    return pool


def timeline_auto(store: Store, project_id: str, song_asset_id: Optional[str], asset_ids: Optional[list[str]],
                  board_id: Optional[str], aspect: str, lyrics_asset_id: Optional[str],
                  options: Optional[dict[str, Any]]) -> dict[str, Any]:
    store.get_project(project_id)
    if not song_asset_id:
        raise EngineError("song_required", "action 'auto' needs song_asset_id (an audio asset)")
    if aspect not in timeline_mod.ASPECTS:
        raise EngineError("bad_aspect", f"aspect must be one of {', '.join(timeline_mod.ASPECTS)}")
    cut = auto_cut(store, project_id, song_asset_id, asset_ids, board_id, lyrics_asset_id, options)
    width, height = timeline_mod.ASPECTS[aspect]
    return store.create_timeline(project_id, name=f"Auto-cut - {cut['song_name']}"[:100], aspect=aspect,
                                 fps=cut["fps"], width=width, height=height, audio_asset_id=song_asset_id, tracks=cut["tracks"])


def auto_cut(store: Store, project_id: str, song_asset_id: str, asset_ids: Optional[list[str]], board_id: Optional[str],
             lyrics_asset_id: Optional[str], options: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The auto-cut itself, without storing a timeline: `{"tracks", "fps",
    "sections", "duration_s", "song_name"}`. The final cut and a
    production's animatic both come from here, so their cut points are the
    same for the same song, lyrics and options."""
    song = store.get_asset(song_asset_id)
    if song["kind"] != "audio":
        raise EngineError("not_audio", f"song_asset_id {song_asset_id} is {song['kind']}, not audio")
    analysis = analyze_audio(store, song_asset_id)
    pool = _pool(store, project_id, asset_ids, board_id)
    lyrics_lines = None
    sections = analysis["sections"]
    options = dict(options or {})
    if lyrics_asset_id:
        lyrics = read_lyrics(store, lyrics_asset_id)
        lyrics_lines = lyrics["lines"]
        if not lyrics_lines:
            raise EngineError("lyrics_untimed", "the lyrics have no [mm:ss.xx] timestamps; time them in Audio > Lyrics timing first")
        # timed [Section] markers are the song's real structure (verse,
        # chorus...), which beats energy-based segmentation for cut density
        if lyrics["sections"] and options.get("sections", "auto") != "analysis":
            sections = [dict(sec, end_s=sec["end_s"] if sec["end_s"] is not None else analysis["duration_s"])
                        for sec in lyrics["sections"]]
            if sections[0]["start_s"] > 0:
                sections.insert(0, {"label": "Intro", "kind": "intro", "energy": "low", "start_s": 0.0,
                                    "end_s": sections[0]["start_s"]})
    options.pop("sections", None)
    if options.get("section_pools") is not None:
        raw_pools = options["section_pools"]
        if not isinstance(raw_pools, dict) or len(raw_pools) > 40:
            raise EngineError("bad_options", "section_pools must be {section label or kind: [asset ids]}")
        resolved: dict[str, list[dict[str, Any]]] = {}
        for key, ids in raw_pools.items():
            if not isinstance(ids, list) or len(ids) > 200:
                raise EngineError("bad_options", f"section_pools['{key}'] must be a list of asset ids")
            resolved[str(key)] = [a for a in _pool(store, project_id, [str(i) for i in ids], None)] if ids else []
        options["section_pools"] = resolved
    if options.get("pinned_spans") is not None:
        raw_spans = options["pinned_spans"]
        if not isinstance(raw_spans, list) or len(raw_spans) > 80:
            raise EngineError("bad_options", "pinned_spans must be a list of {start_s, end_s, asset_ids}")
        spans = []
        for span in raw_spans:
            ids = span.get("asset_ids") if isinstance(span, dict) else None
            if not isinstance(ids, list) or not ids or len(ids) > 20:
                raise EngineError("bad_options", "each pinned span needs start_s, end_s and 1-20 asset_ids")
            spans.append({"start_s": span.get("start_s"), "end_s": span.get("end_s"),
                          "assets": _pool(store, project_id, [str(i) for i in ids], None),
                          "synced": bool(span.get("synced"))})
        options["pinned_spans"] = spans
    try:
        built = timeline_mod.build_auto_cut(
            analysis["duration_s"], analysis["beat_times"], sections, pool,
            options=options, lyrics_lines=lyrics_lines, downbeats=analysis.get("downbeats"),
        )
    except timeline_mod.TimelineError as exc:
        raise EngineError("bad_options", str(exc)) from None
    fps = int((options or {}).get("fps", 30))
    if fps not in (24, 25, 30):
        raise EngineError("bad_fps", "fps must be 24, 25 or 30")
    return {"tracks": built["tracks"], "fps": fps, "sections": sections, "duration_s": analysis["duration_s"],
            "song_name": song.get("name") or song["id"]}


def update_timeline(store: Store, timeline_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    tl = store.get_timeline(timeline_id)
    if not isinstance(patch, dict) or not patch:
        raise EngineError("empty_patch", "patch needs at least one of: name, aspect, fps, audio_asset_id, clip_updates, tracks, finishing")
    allowed = {"name", "aspect", "fps", "audio_asset_id", "clip_updates", "tracks", "lyrics_asset_id", "karaoke", "finishing"}
    unknown = set(patch) - allowed
    if unknown:
        raise EngineError("unknown_patch_field", f"unknown patch field(s): {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}")
    fields: dict[str, Any] = {}
    tracks = tl["tracks"]
    try:
        if "tracks" in patch:
            tracks = patch["tracks"]
        if "clip_updates" in patch:
            tracks = timeline_mod.apply_clip_updates(tracks, patch["clip_updates"])
        if "lyrics_asset_id" in patch:
            others = [t for t in tracks if t["type"] != "lyrics"]
            if patch["lyrics_asset_id"]:
                lines = read_lyrics(store, patch["lyrics_asset_id"])["lines"]
                duration = sum(c["duration_s"] for c in next(t for t in tracks if t["type"] == "visual")["clips"])
                others.append({"type": "lyrics", "clips": timeline_mod.lyrics_clips_from_lines(lines, duration, bool(patch.get("karaoke")))})
            tracks = others
        elif "karaoke" in patch:
            tracks = [dict(t, clips=[dict(c, karaoke=bool(patch["karaoke"])) for c in t["clips"]]) if t["type"] == "lyrics" else t
                      for t in tracks]

        def lookup(aid: str) -> Optional[dict[str, Any]]:
            try:
                a = store.get_asset(aid)
            except NotFound:
                return None
            return a if a["project_id"] == tl["project_id"] else None

        fields["tracks"] = timeline_mod.normalise_tracks(tracks, lookup)
    except timeline_mod.TimelineError as exc:
        raise EngineError("bad_timeline", str(exc)) from None
    if "name" in patch:
        if not str(patch["name"]).strip():
            raise EngineError("bad_name", "name cannot be empty")
        fields["name"] = str(patch["name"]).strip()[:100]
    if "aspect" in patch:
        if patch["aspect"] not in timeline_mod.ASPECTS:
            raise EngineError("bad_aspect", f"aspect must be one of {', '.join(timeline_mod.ASPECTS)}")
        fields["aspect"] = patch["aspect"]
        fields["width"], fields["height"] = timeline_mod.ASPECTS[patch["aspect"]]
    if "fps" in patch:
        if patch["fps"] not in (24, 25, 30):
            raise EngineError("bad_fps", "fps must be 24, 25 or 30")
        fields["fps"] = patch["fps"]
    if "audio_asset_id" in patch:
        if patch["audio_asset_id"]:
            a = store.get_asset(patch["audio_asset_id"])
            if a["kind"] != "audio":
                raise EngineError("not_audio", "audio_asset_id must be an audio asset")
        fields["audio_asset_id"] = patch["audio_asset_id"] or ""
    if "finishing" in patch:
        try:
            fields["finishing"] = video_mod.validate_finishing(patch["finishing"])
        except video_mod.RenderError as exc:
            raise EngineError("bad_finishing", str(exc)) from None
    return store.update_timeline(timeline_id, **fields)


def render_timeline_job(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    timeline_id = params["timeline_id"]
    quality = params.get("quality", "preview")
    tl = store.get_timeline(timeline_id)

    def asset_path_for(asset_id: str) -> Path:
        asset = store.get_asset(asset_id)
        return store.data_dir / asset["file_path"]

    work_dir = store.data_dir / "tmp" / job["id"]
    out_id = new_id("a")
    out_path = store.path_for_asset_file(out_id, ".mp4")
    started = time.monotonic()
    try:
        result = video_mod.render_timeline(tl, asset_path_for, work_dir, out_path, quality=quality, progress=progress,
                                           should_cancel=getattr(progress, "cancelled", None))
    except video_mod.RenderCancelled:
        out_path.unlink(missing_ok=True)
        raise JobCancelled("cancelled") from None
    except video_mod.RenderError as exc:
        out_path.unlink(missing_ok=True)
        raise EngineError("render_failed", str(exc)) from None
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    thumb = _video_thumbnail(out_path, store.path_for_thumb(out_id))
    recipe = {"operation": "render", "timeline_id": timeline_id, "quality": quality, "timeline_updated_at": tl["updated_at"],
              "elapsed_s": round(time.monotonic() - started, 2), "created_at": now_iso()}
    asset = store.create_asset(
        project_id=tl["project_id"], kind="video", file_path=_rel(store, out_path),
        mime="video/mp4", width=result["width"], height=result["height"], duration_s=result["duration_s"],
        thumb_path=thumb, source="rendered", recipe=recipe, asset_id=out_id,
        name=_clip(f"{tl['name']} ({quality})", 100), tags=["render", quality],
    )
    return {"asset_id": asset["id"], "asset_ids": [asset["id"]], "duration_s": result["duration_s"]}


# --------------------------------------------------------------- lineage

def get_lineage(store: Store, asset_id: str, depth: int = 3) -> dict[str, Any]:
    """The recipe of an asset plus (compactly) the recipes of its inputs, up
    to `depth` levels, so "how was this card made" answers in one call."""
    asset = store.get_asset(asset_id)
    recipe = asset.get("recipe")
    out: dict[str, Any] = {"asset_id": asset_id, "kind": asset["kind"], "source": asset["source"], "recipe": recipe}
    if recipe and recipe.get("backend") == "comfyui":
        out["reproduce"] = {"tool": "studio_edit_image", "args": {"asset_id": asset_id, "operation": "reuse"},
                            "vary": {"asset_id": asset_id, "operation": "vary"}}
    if depth > 0 and recipe:
        inputs = []
        for iid in (recipe.get("input_asset_ids") or [])[:6]:
            try:
                inp = get_lineage(store, iid, depth - 1)
            except NotFound:
                inputs.append({"asset_id": iid, "missing": True})
                continue
            r = inp.get("recipe") or {}
            inputs.append({"asset_id": iid, "source": inp["source"], "operation": r.get("operation"),
                           "template": r.get("template"), "seed": (r.get("params") or {}).get("seed")})
        if inputs:
            out["inputs"] = inputs
    return out


# ------------------------------------------------------------------ show

def _jpeg_under(img: Image.Image, max_bytes: int = SHOW_MAX_BYTES) -> bytes:
    img = img.convert("RGB")
    for quality in (85, 75, 65, 55):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    while True:
        img = img.resize((max(64, img.width * 3 // 4), max(64, img.height * 3 // 4)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        if buf.tell() <= max_bytes or img.width <= 64:
            return buf.getvalue()


def _flatten(path: Path) -> Image.Image:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGBA")
    bg = Image.new("RGB", img.size, (18, 14, 22))
    bg.paste(img, mask=img.split()[3])
    return bg


def show_assets(store: Store, asset_ids: list[str], size: int = 768) -> list[dict[str, Any]]:
    """Images for the model: up to 4 separate images, or one labelled contact
    sheet when more are asked for; a video becomes a 3-frame strip, audio a
    waveform picture. Each JPEG stays under ~200 KB."""
    size = max(128, min(int(size), 1024))
    ids = [a.strip() for a in asset_ids if a and a.strip()]
    if not ids:
        raise EngineError("no_assets", "pass one or more asset ids")
    if len(ids) > 24:
        raise EngineError("too_many", "studio_show takes at most 24 asset ids (more would be unreadable in one sheet)")
    assets = [store.get_asset(a) for a in ids]
    pictures: list[tuple[dict[str, Any], Image.Image]] = []
    for asset in assets:
        path = store.data_dir / asset["file_path"]
        if asset["kind"] == "image":
            img = _flatten(path)
        elif asset["kind"] == "video":
            img = _video_frames_sheet(path, asset.get("duration_s") or 1.0, size)
        elif asset["kind"] == "audio":
            img = _waveform_image(asset.get("waveform") or [], size, asset.get("analysis"))
        else:
            img = _text_card(asset, store, size)
        pictures.append((asset, img))
    if len(pictures) > 4:
        tmp_paths = []
        tmp_dir = store.data_dir / "tmp" / f"show_{uuid.uuid4().hex[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            for i, (_, img) in enumerate(pictures):
                p = tmp_dir / f"{i}.png"
                img.thumbnail((400, 400))
                img.save(p)
                tmp_paths.append(p)
            cols = 4 if len(pictures) > 9 else 3
            cell = max(160, min(320, (size * 2) // cols))
            sheet = contact_sheet(tmp_paths, cols=cols, cell=cell, labels=[f"{a['id'][-8:]} {a['kind']}" for a, _ in pictures])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        sheet.thumbnail((max(size, 1024), max(size, 1024)))
        return [{"asset_id": ",".join(a["id"] for a, _ in pictures), "kind": "contact_sheet", "mime": "image/jpeg",
                 "bytes": _jpeg_under(sheet), "order": [a["id"] for a, _ in pictures]}]
    out = []
    for asset, img in pictures:
        img.thumbnail((size, size))
        out.append({"asset_id": asset["id"], "kind": asset["kind"], "mime": "image/jpeg", "bytes": _jpeg_under(img)})
    return out


def _text_card(asset: dict[str, Any], store: Store, size: int) -> Image.Image:
    img = Image.new("RGB", (size, size // 2), (18, 14, 22))
    draw = ImageDraw.Draw(img)
    text = asset.get("name") or asset["id"]
    if asset["kind"] == "lyrics":
        try:
            text = (store.data_dir / asset["file_path"]).read_text(encoding="utf-8")[:400]
        except OSError:
            pass
    draw.multiline_text((16, 16), text, fill=(235, 225, 240), font=design.get_font("inter", 18))
    return img


def _video_frames_sheet(path: Path, duration_s: float, size: int) -> Image.Image:
    ffmpeg = ffmpeg_path()
    frames: list[Image.Image] = []
    timestamps = [min(duration_s * f, max(0.0, duration_s - 0.05)) for f in (0.1, 0.5, 0.9)]
    tmp_dir = path.parent.parent / "tmp" / f"frames_{path.stem}_{uuid.uuid4().hex[:6]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        for i, ts in enumerate(timestamps):
            out = tmp_dir / f"f{i}.jpg"
            if ffmpeg:
                procutil.run([ffmpeg, "-nostdin", "-y", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", str(path),
                              "-frames:v", "1", str(out)], timeout=60)
            if out.is_file():
                with Image.open(out) as im:
                    frames.append(im.convert("RGB"))
        if not frames:
            return Image.new("RGB", (size, size // 2), (30, 28, 34))
        cell_h = size // 2
        scaled = []
        for f in frames:
            f = f.copy()
            f.thumbnail((size, cell_h))
            scaled.append(f)
        width = sum(f.width for f in scaled) + 8 * (len(scaled) - 1)
        sheet = Image.new("RGB", (width, max(f.height for f in scaled)), (0, 0, 0))
        x = 0
        for f in scaled:
            sheet.paste(f, (x, 0))
            x += f.width + 8
        return sheet
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _waveform_image(peaks: list[float], width: int, analysis: Optional[dict[str, Any]] = None) -> Image.Image:
    height = max(120, width // 4)
    img = Image.new("RGB", (width, height), (18, 14, 22))
    draw = ImageDraw.Draw(img)
    duration = (analysis or {}).get("duration_s")
    if analysis and duration:
        shades = {"low": (32, 28, 44), "mid": (44, 30, 52), "high": (64, 30, 58)}
        for s in analysis.get("sections") or []:
            x0, x1 = int(s["start_s"] / duration * width), int(s["end_s"] / duration * width)
            draw.rectangle([x0, 0, x1, height], fill=shades.get(s.get("energy"), (40, 30, 50)))
            draw.text((x0 + 4, 4), f"{s['label']} ({s.get('energy')})", fill=(220, 210, 230), font=design.get_font("inter", 12))
    if peaks:
        mid = height // 2
        n = len(peaks)
        for x in range(width):
            v = peaks[min(n - 1, int(x * n / width))]
            h = int(v * (height // 2 - 8))
            draw.line([(x, mid - h), (x, mid + h)], fill=(255, 77, 141))
    if analysis and duration:
        for b in analysis.get("downbeats") or []:
            x = int(b / duration * width)
            draw.line([(x, height - 10), (x, height)], fill=(245, 194, 107))
        bpm = analysis.get("tempo_bpm")
        draw.text((width - 110, height - 26), f"{bpm} BPM" if bpm else "no beat", fill=(245, 194, 107), font=design.get_font("inter", 14))
    return img
