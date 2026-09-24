"""Identity scoring: is this image still *the same character*?

Two tiers, never mixed up in the output:

- **vision** (the family's vision model through Hoard Link): the reference
  images of the character and the candidate go to the model with a strict
  question about identity only (face, hair, silhouette, colours, signature
  outfit and props), ignoring pose, framing, lighting and background. 0-10.
- **rough** (no model): whether the character's colours (its palette, or
  the dominant colours of its references) are present in the candidate. A
  new background does not count against it; a wrong costume or a different
  creature does. Nothing subtle. The method is always reported.

Scores are cached per asset and character in the asset's `analysis`
(`analysis["identity"][character_id]`), keyed by a hash of the reference
set so a new canonical invalidates old scores.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageOps

from .store import NotFound, Store
from .util import now_iso

VisionFn = Callable[[list[bytes], str], str]

MAX_REFS = 3


def _open_rgb(path: Path, side: int = 384) -> Image.Image:
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((side, side))
        return img.copy()


def _video_still(store: Store, asset: dict[str, Any]) -> Optional[Image.Image]:
    """A video's own thumbnail stands in for it (the middle frame would need
    ffmpeg; the thumbnail is already on disk)."""
    thumb = asset.get("thumb_path")
    if not thumb:
        return None
    for base in (store.data_dir / "thumbs", store.data_dir):
        p = base / thumb
        if p.is_file():
            return _open_rgb(p)
    return None


def asset_image(store: Store, asset_id: str, side: int = 384) -> Optional[Image.Image]:
    try:
        asset = store.get_asset(asset_id)
    except NotFound:
        return None
    if asset["kind"] == "video":
        return _video_still(store, asset)
    if asset["kind"] != "image":
        return None
    path = store.data_dir / asset["file_path"]
    return _open_rgb(path, side) if path.is_file() else None


def _hex_to_rgb(value: str) -> Optional[tuple[int, int, int]]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (value or "").strip())
    if not m:
        return None
    v = m.group(1)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def derived_palette(img: Image.Image, colours: int = 5) -> list[tuple[int, int, int]]:
    """The dominant colours of the subject area (the middle of the frame)
    of a reference, for characters without a palette of their own."""
    w, h = img.size
    crop = img.crop((int(w * 0.2), int(h * 0.1), int(w * 0.8), int(h * 0.95))).convert("RGB")
    crop.thumbnail((160, 160))
    q = crop.quantize(colors=colours, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette() or []
    counts = sorted(q.getcolors() or [], reverse=True)
    out = []
    for count, idx in counts:
        if count < crop.width * crop.height * 0.03:
            continue
        out.append((pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]))
    return out[:colours]


def palette_presence(candidate: Image.Image, palette: list[tuple[int, int, int]], radius: float = 60.0) -> float:
    """0-10: how many of the character's colours show up in the candidate
    (each counts fully once it covers ~1.5 % of the frame). Presence, not
    proportion - so a new background does not count against the character,
    only its missing colours do."""
    if not palette:
        return 0.0
    arr = np.asarray(candidate.convert("RGB").resize((192, 192)), dtype=np.float32).reshape(-1, 3)
    parts = []
    for colour in palette:
        dist = np.sqrt(((arr - np.asarray(colour, dtype=np.float32)) ** 2).sum(axis=1))
        share = float((dist < radius).mean())
        parts.append(min(1.0, share / 0.015))
    return round(10.0 * sum(parts) / len(parts), 1)


def rough_score(candidate: Image.Image, references: list[Image.Image],
                palette: Optional[list[str]] = None) -> float:
    """0-10 without a model: are the character's colours there? Uses the
    character's own palette when it has one, else the dominant colours of
    its references. It catches a wrong costume or a different creature,
    nothing subtle, and is always reported as `rough`."""
    colours = [c for c in (_hex_to_rgb(p) for p in palette or []) if c]
    if not colours:
        for ref in references[:2]:
            for c in derived_palette(ref):
                if all(sum((a - b) ** 2 for a, b in zip(c, o)) > 900 for o in colours):
                    colours.append(c)
    return palette_presence(candidate, colours[:8])


def vision_prompt(name: str, look: str, n_refs: int) -> str:
    refs = "Image 1 shows" if n_refs == 1 else f"Images 1-{n_refs} show"
    return "\n".join([
        f"You check character continuity. {refs} the reference for the character {name}.",
        f"Character notes: {look[:400]}" if look else "",
        f"Image {n_refs + 1} is a candidate.",
        "Rate 0-10 how clearly the candidate shows the SAME character: face, hair, body shape, colours, signature "
        "outfit and props. Ignore pose, framing, expression, lighting and background. 10 = unmistakably the same, "
        "5 = similar but clearly drifted, 0 = someone else or no character.",
        'Answer with JSON only: {"identity": n, "why": "one short line"}',
    ])


def parse_identity(text: str) -> tuple[Optional[float], str]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    data: dict[str, Any] = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = {}
    value = data.get("identity")
    if value is None:
        m = re.search(r'"?identity"?\s*[:=]\s*(\d+(?:\.\d+)?)', text or "", re.IGNORECASE)
        value = float(m.group(1)) if m else None
    try:
        score = None if value is None else round(max(0.0, min(10.0, float(value))), 1)
    except (TypeError, ValueError):
        score = None
    why = data.get("why") if isinstance(data.get("why"), str) else re.sub(r"\s+", " ", text or "")[:160]
    return score, (why or "").strip()[:200]


def _jpeg(img: Image.Image, side: int = 640) -> bytes:
    img = img.copy()
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def reference_ids(character: dict[str, Any]) -> list[str]:
    """The images that define the character, canonical first."""
    out: list[str] = []
    for aid in [character.get("canonical_asset_id"), *(character.get("reference_asset_ids") or [])]:
        if aid and aid not in out:
            out.append(aid)
    return out[:MAX_REFS]


SCORING_VERSION = "2"  # bump when a method changes, so cached scores are redone


def refs_hash(ref_ids: list[str], palette: Optional[list[str]] = None) -> str:
    basis = "|".join(ref_ids) + "#" + ",".join(palette or []) + "#v" + SCORING_VERSION
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def score_asset(store: Store, character: dict[str, Any], asset_id: str, vision: Optional[VisionFn] = None,
                vision_name: Optional[str] = None, force: bool = False) -> dict[str, Any]:
    """Score one asset against the character (cached). Returns
    {"asset_id", "score", "method": "vision"|"rough", "why", "cached"} or
    {"asset_id", "skipped": reason}."""
    refs = reference_ids(character)
    if not refs:
        return {"asset_id": asset_id, "skipped": "the character has no canonical or reference image"}
    key = refs_hash(refs, character.get("palette"))
    asset = store.get_asset(asset_id)
    analysis = asset.get("analysis") or {}
    cached = ((analysis.get("identity") or {}).get(character["id"]) or {})
    wanted = "vision" if vision else "rough"
    if not force and cached.get("refs") == key and (cached.get("method") == wanted or cached.get("method") == "vision"):
        return {"asset_id": asset_id, "score": cached.get("score"), "method": cached.get("method"),
                "why": cached.get("why", ""), "cached": True}
    cand = asset_image(store, asset_id)
    if cand is None:
        return {"asset_id": asset_id, "skipped": "not an image or video with a thumbnail"}
    ref_imgs = [img for img in (asset_image(store, r) for r in refs if r != asset_id) if img is not None]
    if not ref_imgs:
        return {"asset_id": asset_id, "score": 10.0, "method": "self", "why": "this is the character's reference",
                "cached": False}
    score: Optional[float] = None
    method, why = "rough", "the character's colours are present or missing (no vision model)"
    if vision is not None:
        try:
            answer = vision([_jpeg(r) for r in ref_imgs] + [_jpeg(cand)],
                            vision_prompt(character["name"], character.get("prompt") or "", len(ref_imgs)))
            score, why = parse_identity(answer)
            method = "vision"
        except Exception as exc:  # noqa: BLE001 - a dead model falls back to the rough check
            why = f"vision call failed ({str(exc)[:80]}); colour signature instead"
            score = None
    if score is None:
        method = "rough"
        score = rough_score(cand, ref_imgs, character.get("palette"))
    entry = {"score": score, "method": method, "why": why, "refs": key, "at": now_iso(),
             **({"model": vision_name} if method == "vision" and vision_name else {})}
    ids = dict(analysis.get("identity") or {})
    ids[character["id"]] = entry
    store.set_asset_media(asset_id, analysis={**analysis, "identity": ids})
    return {"asset_id": asset_id, "score": score, "method": method, "why": why, "cached": False}


def cached_score(asset: dict[str, Any], character_id: str) -> Optional[dict[str, Any]]:
    entry = ((asset.get("analysis") or {}).get("identity") or {}).get(character_id)
    return entry if isinstance(entry, dict) else None
