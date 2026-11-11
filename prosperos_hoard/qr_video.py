"""Frame-exact QR films and exhaustive verification of the encoded video."""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from . import backend, engine, motion_graphics, procutil
from .ids import new_id
from .store import Store
from .util import write_text_atomic

SIZES = {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (720, 720)}
MOTIONS = ("still", "pulse", "orbit")


def dependencies():
    try:
        import qrcode
        import zxingcpp
    except ImportError as exc:
        raise engine.EngineError("qr_dependencies", "Install the project's qrcode and zxing-cpp dependencies") from exc
    return qrcode, zxingcpp


def plan(scenes: list[dict], aspect: str = "9:16", fps: int = 24, motion: str = "pulse") -> dict:
    qrcode, _ = dependencies()
    if aspect not in SIZES or motion not in MOTIONS or isinstance(fps, bool) or fps not in range(1, 61):
        raise engine.EngineError("qr_settings", "Choose aspect 9:16, 16:9 or 1:1, fps 1..60 and motion still, pulse or orbit")
    if not scenes:
        raise engine.EngineError("qr_scenes", "Add at least one scene with a payload and duration_s")
    width, height = SIZES[aspect]
    result, end = [], 0
    for scene in scenes:
        payload = scene.get("payload")
        duration = scene.get("duration_s", 3)
        if not isinstance(payload, str) or not payload.strip():
            raise engine.EngineError("qr_payload", "Every scene needs a non-empty text or URL payload")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise engine.EngineError("qr_duration", "Scene durations must be finite positive seconds")
        frames = max(1, math.floor(duration * fps + 0.5))
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=4, box_size=1)
        try:
            qr.add_data(payload.encode("utf-8"), optimize=0)
            qr.make(fit=True)
        except (qrcode.exceptions.DataOverflowError, ValueError) as exc:
            raise engine.EngineError("qr_payload", "This payload exceeds QR capacity at error correction H") from exc
        modules = len(qr.get_matrix())
        box = int(min(width, height) * .68) // modules
        if box < 4:
            raise engine.EngineError("qr_density", "Shorten this payload so each QR module has at least four pixels")
        caption = scene.get("caption", "")
        if not isinstance(caption, str):
            raise engine.EngineError("qr_caption", "Scene captions must be text")
        result.append({"payload": payload, "caption": caption, "frames": frames, "start_frame": end,
                       "end_frame": end + frames, "duration_s": frames / fps, "modules": modules, "box": box})
        end += frames
    return {"scenes": result, "aspect": aspect, "fps": fps, "motion": motion,
            "width": width, "height": height, "frames": end, "duration_s": end / fps}


def frame(spec: dict, scene: dict, local_frame: int) -> Image.Image:
    qrcode, _ = dependencies()
    width, height = spec["width"], spec["height"]
    phase = local_frame / max(1, scene["frames"] - 1)
    wave = (math.sin(phase * math.tau) + 1) / 2 if spec["motion"] != "still" else 0
    image = Image.new("RGB", (width, height), (29, 20, 26))
    draw = ImageDraw.Draw(image)
    # Colour and position move while integer modules and the four-module quiet zone stay intact.
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, border=4, box_size=scene["box"])
    qr.add_data(scene["payload"].encode("utf-8"), optimize=0)
    qr.make(fit=True)
    ink = (int(32 + wave * 25), 16, int(31 + wave * 20))
    code = qr.make_image(fill_color=ink, back_color="white").convert("RGB")
    x, y = (width - code.width) // 2, (height - code.height) // 2 - round(min(width, height) * .04)
    if spec["motion"] == "orbit":
        x += round(math.sin(phase * math.tau) * min(width, height) * .025)
        y += round(math.cos(phase * math.tau) * min(width, height) * .025)
    image.paste(code, (x, y))
    draw.line((width * .13, height - 24, width * (.13 + .74 * phase), height - 24), fill=(230, 93, 182), width=3)
    if scene["caption"]:
        font = motion_graphics.load_font("inter", int(min(width, height) * .031))
        lines, current = [], ""
        for word in scene["caption"].split():
            if draw.textlength(word, font=font) > width * .84:
                raise engine.EngineError("qr_caption", "Shorten the caption's longest word")
            candidate = (current + " " + word).strip()
            if draw.textlength(candidate, font=font) > width * .84:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
        line_height = int(min(width, height) * .042)
        top = y + code.height + int(min(width, height) * .05)
        if top + len(lines) * line_height > height - 42:
            raise engine.EngineError("qr_caption", "Shorten this caption to fit below the QR")
        for index, line in enumerate(lines):
            draw.text((width / 2, top + index * line_height), line, font=font, anchor="mt", fill=(236, 225, 232))
    return image


def report_path(store: Store, job_id: str) -> Path:
    job = store.get_job(job_id)
    if job["type"] not in ("qr_video", "qr_verify"):
        raise engine.EngineError("qr_report", "This job is not a QR render or verification")
    return store.data_dir / "qr-reports" / f"{job['id']}.json"


def read_report(store: Store, job_id: str, offset: int = 0, limit: int = 50) -> dict:
    path = report_path(store, job_id)
    if not path.is_file():
        raise engine.EngineError("qr_report_pending", "The QR verification report is not ready")
    report = json.loads(path.read_text(encoding="utf-8"))
    rows = report.pop("frames")
    offset, limit = max(0, offset), max(0, min(200, limit))
    return {**report, "frames": rows[offset:offset + limit], "total": len(rows), "offset": offset,
            "next_offset": offset + limit if limit and offset + limit < len(rows) else None}


def _ffmpeg(args: list[str], cwd: Path) -> None:
    exe = backend.ffmpeg_path()
    if not exe:
        raise engine.EngineError("no_ffmpeg", "QR video needs ffmpeg")
    proc = procutil.run([exe, "-nostdin", "-y", "-loglevel", "error", *args], cwd=cwd, timeout=3600)
    if proc.returncode:
        raise engine.EngineError("qr_video_failed", proc.stderr.decode("utf-8", errors="replace")[-600:])


def verify(path: Path, work: Path, progress, spec: dict | None = None) -> dict:
    """Decode every frame after video compression, including all scene boundaries."""
    _, decoder = dependencies()
    decoded = work / "decoded"
    decoded.mkdir()
    progress(.6, "Extracting every encoded frame")
    _ffmpeg(["-i", str(path), "-map", "0:v:0", "-vsync", "0", "decoded/%08d.png"], work)
    paths = sorted(decoded.glob("*.png"))
    rows, payloads, failed, mismatch = [], [], 0, 0
    scene_index = 0
    for index, png in enumerate(paths):
        progress(.65 + .33 * index / max(1, len(paths)), f"Decoding frame {index + 1}/{len(paths)}")
        with Image.open(png) as im:
            found = decoder.read_barcode(np.asarray(im.convert("RGB")), formats=decoder.BarcodeFormat.QRCode,
                                         text_mode=decoder.TextMode.Plain)
        payload = found.bytes.decode("utf-8", errors="replace") if found and found.valid else None
        if payload is None:
            failed += 1
        elif payload not in payloads:
            payloads.append(payload)
        expected = None
        if spec and index < spec["frames"]:
            while index >= spec["scenes"][scene_index]["end_frame"]:
                scene_index += 1
            expected = spec["scenes"][scene_index]["payload"]
        matches = payload is not None and (not spec or payload == expected)
        if payload is not None and not matches:
            mismatch += 1
        rows.append({"frame": index, "payload_index": payloads.index(payload) if payload is not None else None,
                     "matches": matches, "sha256": hashlib.sha256(png.read_bytes()).hexdigest()})
    expected_count = spec["frames"] if spec else len(paths)
    with path.open("rb") as video:
        video_hash = hashlib.file_digest(video, "sha256").hexdigest()
    return {"format": "prospero.qr-verification/1", "verified": bool(paths) and not failed and not mismatch and len(paths) == expected_count,
            "checked_frames": len(paths), "expected_frames": expected_count, "undecodable_frames": failed,
            "mismatched_frames": mismatch, "payloads": payloads, "fps": spec["fps"] if spec else None,
            "video_sha256": video_hash, "frames": rows}


def render_job(store: Store, job: dict[str, Any], progress) -> dict:
    params = job["params"]
    spec = plan(params["scenes"], params.get("aspect", "9:16"), params.get("fps", 24), params.get("motion", "pulse"))
    asset_id = new_id("a")
    out = store.path_for_asset_file(asset_id, ".mp4")
    thumb = store.path_for_thumb(asset_id)
    try:
        with tempfile.TemporaryDirectory(prefix="qr-", dir=store.data_dir) as temp:
            work = Path(temp)
            index = 0
            for scene in spec["scenes"]:
                for local in range(scene["frames"]):
                    progress(.5 * index / spec["frames"], f"Drawing frame {index + 1}/{spec['frames']}")
                    frame(spec, scene, local).save(work / f"{index:08d}.png")
                    index += 1
            _ffmpeg(["-framerate", str(spec["fps"]), "-i", "%08d.png", "-frames:v", str(spec["frames"]),
                     "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], work)
            report = verify(out, work, progress, spec)
            report.update(asset_id=asset_id, scenes=spec["scenes"])
            write_text_atomic(report_path(store, job["id"]), json.dumps(report, ensure_ascii=False))
            if not report["verified"]:
                raise engine.EngineError("qr_verification_failed", "The encoded video failed frame-by-frame QR verification. Read the job's QR report")
            progress(.99, "Saving verified QR film")
            with Image.open(work / "00000000.png") as first:
                first.thumbnail((512, 512))
                first.save(thumb, "WEBP")
        summary = {key: report[key] for key in ("verified", "checked_frames", "expected_frames", "undecodable_frames", "mismatched_frames", "video_sha256")}
        store.create_asset(project_id=params["project"], kind="video", file_path=engine._rel(store, out),
                           mime="video/mp4", width=spec["width"], height=spec["height"], duration_s=spec["duration_s"],
                           thumb_path=engine._rel(store, thumb), asset_id=asset_id, name=params.get("name") or "QR film",
                           source="generated", tags=["qr"], recipe={"operation": "qr_video", "spec": spec,
                           "verification": summary, "report_job_id": job["id"]})
        return {"asset_id": asset_id, "asset_ids": [asset_id], "report_job_id": job["id"], **summary}
    except BaseException:
        out.unlink(missing_ok=True)
        thumb.unlink(missing_ok=True)
        raise


def verify_job(store: Store, job: dict[str, Any], progress) -> dict:
    asset = store.get_asset(job["params"]["asset_id"])
    recipe = asset.get("recipe") or {}
    spec = recipe.get("spec") if recipe.get("operation") == "qr_video" else None
    with tempfile.TemporaryDirectory(prefix="qr-check-", dir=store.data_dir) as temp:
        report = verify(store.data_dir / asset["file_path"], Path(temp), progress, spec)
    report["asset_id"] = asset["id"]
    write_text_atomic(report_path(store, job["id"]), json.dumps(report, ensure_ascii=False))
    return {"asset_id": asset["id"], "report_job_id": job["id"], **{key: value for key, value in report.items()
            if key not in ("frames", "payloads", "format", "asset_id")}}
