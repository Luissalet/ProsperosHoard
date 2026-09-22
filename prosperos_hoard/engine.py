"""The business logic behind every `/api/agent/*` endpoint (and the richer
UI endpoints that wrap the same functions). Kept separate from `api.py` so
it is directly unit-testable without spinning up FastAPI.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from PIL import Image

from . import audio as audio_mod
from . import comfy_driver
from . import design
from . import templates as design_templates
from . import timeline as timeline_mod
from . import video as video_mod
from . import voices as voices_mod
from .backend import Backend, ffmpeg_path
from .hoard_link.errors import BackendError, Unavailable
from .ids import new_id
from .jobs import WaitingForResources
from .store import NotFound, Store
from .util import now_iso

MENTION_RE = re.compile(r"@([A-Za-z0-9_\-]+)")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".webp"}


class EngineError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- prompts

def expand_mentions(store: Store, project_id: str, prompt: str) -> dict[str, Any]:
    """Replace @Name with the character's prompt fragment. Returns
    {"expanded_prompt", "negative_extra", "reference_asset_id", "matched"}."""
    characters = {c["name"].lower(): c for c in store.list_characters(project_id)}
    negatives: list[str] = []
    reference_asset_id: Optional[str] = None
    matched: list[str] = []

    def _sub(m: re.Match) -> str:
        nonlocal reference_asset_id
        name = m.group(1)
        char = characters.get(name.lower())
        if not char:
            return m.group(0)
        matched.append(char["name"])
        if char.get("negative"):
            negatives.append(char["negative"])
        if reference_asset_id is None and char.get("canonical_asset_id"):
            reference_asset_id = char["canonical_asset_id"]
        return char.get("prompt") or f"{char['name']}"

    expanded = MENTION_RE.sub(_sub, prompt)
    return {
        "expanded_prompt": expanded,
        "negative_extra": ", ".join(negatives),
        "reference_asset_id": reference_asset_id,
        "matched_characters": matched,
    }


def compose_prompt(store: Store, project_id: str, prompt: str, negative: Optional[str], style_id: Optional[str]) -> dict[str, Any]:
    expansion = expand_mentions(store, project_id, prompt)
    prefix = suffix = ""
    style_negative = ""
    defaults: dict[str, Any] = {}
    if style_id:
        style = store.get_style_preset(style_id)
        prefix = style.get("prompt_prefix") or ""
        suffix = style.get("prompt_suffix") or ""
        style_negative = style.get("negative") or ""
        defaults = style.get("defaults") or {}
    positive = f"{prefix} {expansion['expanded_prompt']} {suffix}".strip()
    negative_parts = [p for p in (negative, expansion["negative_extra"], style_negative) if p]
    final_negative = ", ".join(negative_parts)
    return {
        "positive_prompt": positive,
        "negative_prompt": final_negative,
        "reference_asset_id": expansion["reference_asset_id"],
        "matched_characters": expansion["matched_characters"],
        "style_defaults": defaults,
    }


# -------------------------------------------------------------- ComfyUI --

def _resolve_comfy_url(backend: Backend) -> str:
    res = backend.link.sync.resolve("image")
    if not res.resolved or res.provider != "comfyui":
        raise Unavailable("image", res.details.get("reasons", [res.reason]))
    return res.url


def vram_free_mb(backend: Backend) -> Optional[int]:
    from .hoard_link.gpu import gpu_free_mb

    gpus = gpu_free_mb()
    return gpus[0].free_mb if gpus else None


def check_vram_or_wait(backend: Backend, spec: dict[str, Any]) -> None:
    needed = comfy_driver.estimate_vram_mb(spec, backend.vram_estimates_mb())
    free = vram_free_mb(backend)
    if free is not None and free < needed:
        raise WaitingForResources(f"waiting for {needed} MB VRAM ({free} MB free) for a {spec.get('vram_class')} job")


def _run_comfy_workflow(backend: Backend, workflow: dict[str, Any], reference_bytes: Optional[bytes] = None,
                         reference_name: Optional[str] = None, mask_bytes: Optional[bytes] = None,
                         mask_name: Optional[str] = None, timeout_s: float = 300.0,
                         on_progress=None) -> tuple[dict[str, Any], list]:
    comfy = backend.run_async(backend.link.comfy())
    if comfy is None:
        raise Unavailable("image", ["ComfyUI not reachable"])
    if reference_bytes is not None:
        backend.run_async(comfy.upload_image(reference_bytes, reference_name or "reference.png"))
    if mask_bytes is not None:
        backend.run_async(comfy.upload_image(mask_bytes, mask_name or "mask.png"))
    client_id = str(uuid.uuid4())
    prompt_id = backend.run_async(comfy.queue(workflow, client_id))
    backend.run_async(comfy.wait(prompt_id, timeout_s=timeout_s, on_progress=on_progress))
    outputs = backend.run_async(comfy.outputs(prompt_id))
    return {"prompt_id": prompt_id}, outputs


def _download_output(backend: Backend, output) -> bytes:
    comfy = backend.run_async(backend.link.comfy())
    return backend.run_async(comfy.download(output))


def _object_info(backend: Backend) -> dict[str, Any]:
    comfy = backend.run_async(backend.link.comfy())
    if comfy is None:
        return {}
    return backend.run_async(comfy.object_info())


def _import_comfy_output(store: Store, project_id: str, data: bytes, kind: str, recipe: dict[str, Any]) -> dict[str, Any]:
    asset_id = new_id("a")
    ext = ".webp" if kind == "video" and data[:4] == b"RIFF" else (".png" if kind == "image" else ".bin")
    dest = store.path_for_asset_file(asset_id, ext)
    dest.write_bytes(data)
    width = height = None
    duration_s = None
    if kind == "image":
        with Image.open(dest) as img:
            width, height = img.size
        make_thumbnail(dest, store.path_for_thumb(asset_id))
    return store.create_asset(
        project_id=project_id, kind=kind, file_path=str(dest.relative_to(store.data_dir)),
        mime="image/png" if kind == "image" else "image/webp", width=width, height=height,
        duration_s=duration_s, thumb_path=str(store.path_for_thumb(asset_id).relative_to(store.data_dir)) if kind == "image" else None,
        source="generated", recipe=recipe, asset_id=asset_id,
    )


def generate_image(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    project_id = job["project_id"]
    template_name = params.get("template", "sdxl_txt2img")
    workflow, spec = comfy_driver.load_template(template_name)
    style_defaults = params.get("style_defaults", {})
    checkpoint = params.get("checkpoint") or style_defaults.get("checkpoint")
    values = {
        "checkpoint": checkpoint,
        "positive_prompt": params["positive_prompt"],
        "negative_prompt": params.get("negative_prompt", ""),
        "width": params.get("width") or style_defaults.get("width", 1024),
        "height": params.get("height") or style_defaults.get("height", 1024),
        "batch_size": 1,
        "seed": params.get("seed", 0),
        "steps": params.get("steps") or style_defaults.get("steps", 30),
        "cfg": params.get("cfg") or style_defaults.get("cfg", 6.5),
        "sampler": params.get("sampler") or style_defaults.get("sampler", "dpmpp_2m"),
        "scheduler": params.get("scheduler") or style_defaults.get("scheduler", "karras"),
        "denoise": params.get("strength", 1.0),
    }
    check_vram_or_wait(backend, spec)
    object_info = _object_info(backend)
    try:
        comfy_driver.validate_against_object_info(spec, values, object_info)
    except comfy_driver.ValidationError as exc:
        raise EngineError("invalid_checkpoint", str(exc)) from exc

    reference_bytes = None
    reference_name = None
    if spec.get("requires_reference"):
        ref_asset = store.get_asset(params["reference_asset_id"])
        ref_path = store.data_dir / ref_asset["file_path"]
        reference_bytes = ref_path.read_bytes()
        reference_name = f"{ref_asset['id']}{ref_path.suffix}"
        values["_ref_name"] = reference_name

    count = params.get("count", 1)
    assets = []
    for i in range(count):
        seed = values["seed"] + i
        run_values = {**values, "seed": seed}
        wf = comfy_driver.apply_params(workflow, spec, run_values)
        if spec.get("requires_reference"):
            ref_node, _, ref_input = spec["reference_node"].partition(".")
            wf[ref_node]["inputs"][ref_input] = reference_name
        progress(0.1 + 0.8 * i / count, f"rendering {i + 1}/{count}")
        _, outputs = _run_comfy_workflow(backend, wf, reference_bytes=reference_bytes, reference_name=reference_name)
        for out in outputs:
            data = _download_output(backend, out)
            recipe = {
                "operation": "generate_image", "backend": "comfyui", "template": template_name,
                "checkpoint": checkpoint, "params": run_values, "created_at": now_iso(),
            }
            assets.append(_import_comfy_output(store, project_id, data, spec.get("kind", "image"), recipe))
        reference_bytes = None  # already uploaded once
    progress(0.95, "importing outputs")
    return {"asset_ids": [a["id"] for a in assets], "assets": assets}


def edit_image(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    project_id = job["project_id"]
    operation = params["operation"]
    src_asset = store.get_asset(params["asset_id"])
    template_name = {"img2img": "sdxl_img2img", "inpaint": "sdxl_inpaint", "hires": "sdxl_hires", "vary": "sdxl_img2img"}[operation]
    workflow, spec = comfy_driver.load_template(template_name)
    check_vram_or_wait(backend, spec)

    ref_path = store.data_dir / src_asset["file_path"]
    reference_bytes = ref_path.read_bytes()
    reference_name = f"{src_asset['id']}{ref_path.suffix}"

    values = {
        "checkpoint": (src_asset.get("recipe") or {}).get("checkpoint", "sd_xl_base_1.0.safetensors"),
        "positive_prompt": params.get("prompt", (src_asset.get("recipe") or {}).get("params", {}).get("positive_prompt", "")),
        "negative_prompt": params.get("negative_prompt", ""),
        "seed": params.get("seed", int(time.time()) % 100000),
        "steps": params.get("steps", 30), "cfg": params.get("cfg", 6.5),
        "sampler": params.get("sampler", "dpmpp_2m"), "scheduler": params.get("scheduler", "karras"),
        "denoise": params.get("strength", 0.55),
        "hires_width": params.get("width", 1536), "hires_height": params.get("height", 1536),
        "hires_steps": params.get("hires_steps", 16), "hires_denoise": params.get("hires_denoise", 0.45),
    }

    mask_bytes = mask_name = None
    if operation == "inpaint":
        mask_asset = store.get_asset(params["mask_asset_id"])
        mask_path = store.data_dir / mask_asset["file_path"]
        mask_bytes = mask_path.read_bytes()
        mask_name = f"{mask_asset['id']}{mask_path.suffix}"

    count = params.get("count", 1)
    assets = []
    for i in range(count):
        seed = values["seed"] + i
        wf = comfy_driver.apply_params(workflow, spec, {**values, "seed": seed})
        ref_node, _, ref_input = spec["reference_node"].partition(".")
        wf[ref_node]["inputs"][ref_input] = reference_name
        if operation == "inpaint":
            mask_node, _, mask_input = spec["mask_node"].partition(".")
            wf[mask_node]["inputs"][mask_input] = mask_name
        progress(0.1 + 0.8 * i / count, f"editing {i + 1}/{count}")
        _, outputs = _run_comfy_workflow(
            backend, wf, reference_bytes=reference_bytes if i == 0 else None, reference_name=reference_name,
            mask_bytes=mask_bytes if i == 0 else None, mask_name=mask_name,
        )
        for out in outputs:
            data = _download_output(backend, out)
            recipe = {
                "operation": f"edit_image:{operation}", "backend": "comfyui", "template": template_name,
                "input_asset_ids": [src_asset["id"]], "params": {**values, "seed": seed}, "created_at": now_iso(),
            }
            assets.append(_import_comfy_output(store, project_id, data, "image", recipe))
    return {"asset_ids": [a["id"] for a in assets], "assets": assets}


def animate_image(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    project_id = job["project_id"]
    src_asset = store.get_asset(params["asset_id"])
    workflow, spec = comfy_driver.load_template("svd_img2vid")
    check_vram_or_wait(backend, spec)
    ref_path = store.data_dir / src_asset["file_path"]
    reference_bytes = ref_path.read_bytes()
    reference_name = f"{src_asset['id']}{ref_path.suffix}"
    with Image.open(ref_path) as im:
        width, height = im.size
    values = {
        "checkpoint": "svd_xt.safetensors",
        "width": min(width, 1024), "height": min(height, 576),
        "frames": params.get("frames", 14), "fps": params.get("fps", 7),
        "motion": params.get("motion", 127), "augmentation": 0.0,
        "seed": params.get("seed", 0), "steps": 20, "cfg": 2.5,
    }
    wf = comfy_driver.apply_params(workflow, spec, values)
    ref_node, _, ref_input = spec["reference_node"].partition(".")
    wf[ref_node]["inputs"][ref_input] = reference_name
    progress(0.2, "animating")
    _, outputs = _run_comfy_workflow(backend, wf, reference_bytes=reference_bytes, reference_name=reference_name, timeout_s=600.0)
    assets = []
    for out in outputs:
        data = _download_output(backend, out)
        webp_path = store.path_for_asset_file(new_id("a"), ".webp")
        webp_path.write_bytes(data)
        mp4_path = webp_path.with_suffix(".mp4")
        _normalise_to_mp4(webp_path, mp4_path)
        asset_id = mp4_path.stem
        with Image.open(webp_path) as im:
            n_frames = getattr(im, "n_frames", 1)
        duration_s = n_frames / max(1, values["fps"])
        recipe = {"operation": "animate", "backend": "comfyui", "template": "svd_img2vid",
                  "input_asset_ids": [src_asset["id"]], "params": values, "created_at": now_iso()}
        assets.append(store.create_asset(
            project_id=project_id, kind="video", file_path=str(mp4_path.relative_to(store.data_dir)),
            mime="video/mp4", width=values["width"], height=values["height"], duration_s=duration_s,
            source="generated", recipe=recipe, asset_id=asset_id,
        ))
    return {"asset_ids": [a["id"] for a in assets], "assets": assets}


def _normalise_to_mp4(src: Path, dest: Path) -> None:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        shutil.copyfile(src, dest.with_suffix(src.suffix))
        return
    subprocess.run([ffmpeg, "-y", "-i", str(src), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dest)],
                   capture_output=True, check=True)


# ------------------------------------------------------------------ i/o --

def make_thumbnail(src: Path, dest: Path, size: int = 512) -> None:
    with Image.open(src) as img:
        img = img.convert("RGB")
        img.thumbnail((size, size))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="WEBP", quality=85)


def import_asset(store: Store, project_id: str, source_path: Path, kind_hint: Optional[str] = None,
                  original_name: Optional[str] = None) -> dict[str, Any]:
    ext = Path(original_name or source_path).suffix.lower()
    if kind_hint:
        kind = kind_hint
    elif ext in IMAGE_EXTS:
        kind = "image"
    elif ext in AUDIO_EXTS:
        kind = "audio"
    elif ext in VIDEO_EXTS:
        kind = "video"
    elif ext in (".lrc", ".txt"):
        kind = "lyrics"
    else:
        raise EngineError("unsupported_kind", f"cannot infer asset kind from extension '{ext}'")

    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ext)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, dest)

    width = height = duration_s = None
    thumb_path = None
    mime = {
        "image": "image/png", "audio": "audio/mpeg", "video": "video/mp4", "lyrics": "text/plain",
    }.get(kind)

    if kind == "image":
        with Image.open(dest) as img:
            width, height = img.size
        thumb = store.path_for_thumb(asset_id)
        make_thumbnail(dest, thumb)
        thumb_path = str(thumb.relative_to(store.data_dir))
    elif kind == "audio":
        duration_s = audio_mod.probe_duration_s(dest)
    elif kind == "video":
        duration_s = audio_mod.probe_duration_s(dest)

    return store.create_asset(
        project_id=project_id, kind=kind, file_path=str(dest.relative_to(store.data_dir)), mime=mime,
        width=width, height=height, duration_s=duration_s, thumb_path=thumb_path, source="import",
        asset_id=asset_id,
    )


# --------------------------------------------------------------- design --

def _asset_resolver(store: Store):
    def resolve(asset_id: Optional[str]) -> Optional[Path]:
        if not asset_id:
            return None
        try:
            asset = store.get_asset(asset_id)
        except NotFound:
            return None
        return store.data_dir / asset["file_path"]
    return resolve


def render_design(store: Store, project_id: str, template: str, fields: dict[str, Any], variant: Optional[str] = None,
                   print_mode: bool = False) -> dict[str, Any]:
    layout = design_templates.get_layout(template, variant)
    img = design.render_layout(layout, fields, _asset_resolver(store))
    data = design.image_bytes(img, print_mode=print_mode)
    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ".png")
    dest.write_bytes(data)
    make_thumbnail(dest, store.path_for_thumb(asset_id))
    recipe = {"operation": "design", "template": template, "variant": variant, "fields": fields, "created_at": now_iso()}
    return store.create_asset(
        project_id=project_id, kind="image", file_path=str(dest.relative_to(store.data_dir)), mime="image/png",
        width=img.width, height=img.height, thumb_path=str(store.path_for_thumb(asset_id).relative_to(store.data_dir)),
        source="rendered", recipe=recipe, asset_id=asset_id,
    )


def contact_sheet(store: Store, image_paths: list[Path], cols: int = 3, cell: int = 320) -> bytes:
    rows = (len(image_paths) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * cell), (18, 16, 22))
    for i, path in enumerate(image_paths):
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((cell - 16, cell - 16))
            x = (i % cols) * cell + (cell - img.width) // 2
            y = (i // cols) * cell + (cell - img.height) // 2
            sheet.paste(img, (x, y))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


def photocard_set(store: Store, project_id: str, group_id: str, template_front: str = "photocard_front",
                   template_back: str = "photocard_back", image_asset_ids: Optional[dict[str, str]] = None) -> dict[str, Any]:
    group = store.get_group(group_id)
    image_asset_ids = image_asset_ids or {}
    assets = []
    for member_id in group["member_ids"]:
        char = store.get_character(member_id)
        image_id = image_asset_ids.get(member_id) or char.get("canonical_asset_id")
        if not image_id:
            candidates = store.list_assets(project_id=project_id, kind="image", limit=1)["items"]
            image_id = candidates[0]["id"] if candidates else None
        if not image_id:
            continue
        front = render_design(store, project_id, template_front, {
            "image": image_id, "member_name": char["name"], "role": char.get("role") or "",
            "accent": (char.get("palette") or ["#ff4d8d"])[0],
        })
        back = render_design(store, project_id, template_back, {
            "group_logo": group.get("logo_asset_id"), "member_name": char["name"],
            "serial": f"No. {len(assets) // 2 + 1:03d}/250", "message": char.get("bio") or "",
            "accent": (char.get("palette") or ["#ff4d8d"])[0],
        })
        assets.append(front)
        assets.append(back)
    if not assets:
        raise EngineError(
            "no_reference_images",
            "no group member has a canonical reference image or an available project image; "
            "generate or import at least one image, or pass image_asset_ids explicitly",
        )
    sheet_paths = [store.data_dir / a["file_path"] for a in assets]
    sheet_bytes = contact_sheet(store, sheet_paths, cols=4)
    sheet_id = new_id("a")
    sheet_path = store.path_for_asset_file(sheet_id, ".png")
    sheet_path.write_bytes(sheet_bytes)
    sheet_asset = store.create_asset(
        project_id=project_id, kind="image", file_path=str(sheet_path.relative_to(store.data_dir)), mime="image/png",
        source="rendered", recipe={"operation": "photocard_set", "group_id": group_id, "created_at": now_iso()},
        asset_id=sheet_id,
    )
    return {"asset_ids": [a["id"] for a in assets], "assets": assets, "contact_sheet": sheet_asset}


# ---------------------------------------------------------------- audio --

def analyze_audio(store: Store, asset_id: str) -> dict[str, Any]:
    asset = store.get_asset(asset_id)
    path = store.data_dir / asset["file_path"]
    samples = audio_mod.decode_to_mono(path)
    result = audio_mod.analyze_samples(samples)
    peaks = audio_mod.waveform_peaks(samples)
    store.update_asset(asset_id, notes=asset.get("notes"))
    store.conn.execute(
        "UPDATE assets SET waveform_json=?, duration_s=? WHERE id=?",
        (__import__("json").dumps(peaks), result["duration_s"], asset_id),
    )
    store.conn.commit()
    return result


def voice_line(store: Store, backend: Backend, project_id: str, text: str, character_id: Optional[str] = None,
               voice_override: Optional[str] = None, speed: Optional[float] = None) -> dict[str, Any]:
    voice_cfg: dict[str, Any] = {}
    if character_id:
        char = store.get_character(character_id)
        voice_cfg = dict(char.get("voice") or {})
    if voice_override:
        voice_cfg["voice_id"] = voice_override
    if speed:
        voice_cfg["speed"] = speed
    voices_dir = store.data_dir / "voices"
    wav_bytes, provider = voices_mod.synthesize(backend, voices_dir, text, voice_cfg)
    asset_id = new_id("a")
    dest = store.path_for_asset_file(asset_id, ".wav")
    dest.write_bytes(wav_bytes)
    duration_s = audio_mod.probe_duration_s(dest)
    recipe = {"operation": "voice", "provider": provider, "text": text, "voice": voice_cfg, "created_at": now_iso()}
    return store.create_asset(
        project_id=project_id, kind="audio", file_path=str(dest.relative_to(store.data_dir)), mime="audio/wav",
        duration_s=duration_s, source="generated", recipe=recipe, asset_id=asset_id,
    )


# ------------------------------------------------------------- timeline --

def timeline_auto(store: Store, project_id: str, song_asset_id: str, asset_ids: Optional[list[str]],
                   board_id: Optional[str], aspect: str, lyrics_asset_id: Optional[str],
                   options: Optional[dict[str, Any]]) -> dict[str, Any]:
    song = store.get_asset(song_asset_id)
    if not song.get("waveform") or song.get("duration_s") is None:
        analyze_audio(store, song_asset_id)
        song = store.get_asset(song_asset_id)
    analysis = analyze_audio(store, song_asset_id)  # cheap enough to redo for fresh beat/section data

    if asset_ids:
        pool = [store.get_asset(a) for a in asset_ids]
    elif board_id:
        board = store.get_board(board_id)
        pool = [store.get_asset(item["asset_id"]) for item in board["items"] if "asset_id" in item]
    else:
        pool = store.list_assets(project_id=project_id, kind="image", limit=60)["items"]
    pool = [a for a in pool if a["kind"] in ("image", "video")]
    if not pool:
        raise EngineError("empty_pool", "no image/video assets available to build a timeline from")

    lyrics_lines = None
    if lyrics_asset_id:
        lyrics_asset = store.get_asset(lyrics_asset_id)
        text = (store.data_dir / lyrics_asset["file_path"]).read_text(encoding="utf-8")
        lyrics_lines = audio_mod.parse_lrc(text)

    built = timeline_mod.build_auto_cut(
        analysis["duration_s"], analysis["beat_times"], analysis["sections"], pool,
        options=options or {}, lyrics_lines=lyrics_lines,
    )
    tl = store.create_timeline(project_id, name=f"Auto-cut {song['id']}", aspect=aspect,
                                audio_asset_id=song_asset_id, tracks=built["tracks"])
    return tl


def render_timeline_job(store: Store, backend: Backend, job: dict[str, Any], progress) -> dict[str, Any]:
    params = job["params"]
    timeline_id = params["timeline_id"]
    quality = params.get("quality", "preview")
    tl = store.get_timeline(timeline_id)

    def asset_path_for(asset_id: str) -> Path:
        return store.data_dir / store.get_asset(asset_id)["file_path"]

    work_dir = store.data_dir / "tmp" / job["id"]
    out_id = new_id("a")
    out_path = store.path_for_asset_file(out_id, ".mp4")

    def on_progress(frac: float, msg: Optional[str]) -> None:
        progress(frac, msg)

    result = video_mod.render_timeline(tl, asset_path_for, work_dir, out_path, quality=quality, progress=on_progress)
    shutil.rmtree(work_dir, ignore_errors=True)
    recipe = {"operation": "render", "timeline_id": timeline_id, "quality": quality, "created_at": now_iso()}
    asset = store.create_asset(
        project_id=tl["project_id"], kind="video", file_path=str(out_path.relative_to(store.data_dir)),
        mime="video/mp4", width=result["width"], height=result["height"], duration_s=result["duration_s"],
        source="rendered", recipe=recipe, asset_id=out_id,
    )
    return {"asset_id": asset["id"], "asset": asset}


# --------------------------------------------------------------- lineage

def get_lineage(store: Store, asset_id: str) -> dict[str, Any]:
    asset = store.get_asset(asset_id)
    return {"asset_id": asset_id, "recipe": asset.get("recipe"), "source": asset["source"]}


# ------------------------------------------------------------------ show

def show_assets(store: Store, asset_ids: list[str], size: int = 768) -> list[dict[str, Any]]:
    out = []
    for asset_id in asset_ids:
        asset = store.get_asset(asset_id)
        path = store.data_dir / asset["file_path"]
        if asset["kind"] == "image":
            with Image.open(path) as img:
                img = img.convert("RGB")
                img.thumbnail((size, size))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                out.append({"asset_id": asset_id, "kind": "image", "mime": "image/jpeg", "bytes": buf.getvalue()})
        elif asset["kind"] == "video":
            frames = _video_frames_sheet(path, asset.get("duration_s") or 1.0, size)
            out.append({"asset_id": asset_id, "kind": "video", "mime": "image/jpeg", "bytes": frames})
        elif asset["kind"] == "audio":
            wf_img = _waveform_image(asset.get("waveform") or [], size)
            buf = io.BytesIO()
            wf_img.save(buf, format="JPEG", quality=85)
            out.append({"asset_id": asset_id, "kind": "audio", "mime": "image/jpeg", "bytes": buf.getvalue()})
    return out


def _video_frames_sheet(path: Path, duration_s: float, size: int) -> bytes:
    ffmpeg = ffmpeg_path()
    frame_paths = []
    timestamps = [0.0, duration_s * 0.33, duration_s * 0.66]
    tmp_dir = path.parent / f".frames_{path.stem}"
    tmp_dir.mkdir(exist_ok=True)
    try:
        for i, ts in enumerate(timestamps):
            out = tmp_dir / f"f{i}.jpg"
            subprocess.run([ffmpeg, "-y", "-ss", f"{ts:.2f}", "-i", str(path), "-vframes", "1", str(out)],
                           capture_output=True)
            if out.is_file():
                frame_paths.append(out)
        if not frame_paths:
            img = Image.new("RGB", (size, size), (30, 28, 34))
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            return buf.getvalue()
        cell = size // len(frame_paths)
        sheet = Image.new("RGB", (cell * len(frame_paths), cell), (0, 0, 0))
        for i, fp in enumerate(frame_paths):
            with Image.open(fp) as im:
                im = im.convert("RGB")
                im.thumbnail((cell, cell))
                sheet.paste(im, (i * cell, 0))
        buf = io.BytesIO()
        sheet.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _waveform_image(peaks: list[float], width: int) -> Image.Image:
    from PIL import ImageDraw

    height = max(80, width // 4)
    img = Image.new("RGB", (width, height), (18, 16, 22))
    if not peaks:
        return img
    draw = ImageDraw.Draw(img)
    step = max(1, len(peaks) // width)
    mid = height // 2
    for x in range(min(width, len(peaks) // step)):
        v = peaks[x * step]
        h = int(v * (height // 2 - 4))
        draw.line([(x, mid - h), (x, mid + h)], fill=(255, 77, 141))
    return img
