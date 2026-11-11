"""Promo from a web page: brand scan -> script -> frames built in code -> checks on a 4 fps contact sheet -> fixes -> ffmpeg render -> sound.

The page is scanned once into a brand kit (`promo_brand`), the script is planned (`promo_compose`) and every frame is drawn by a browser
from an HTML timeline. Before the long render the composition is checked: the text boxes are measured in the DOM at each shot, the
contrast is computed, a 4 fps contact sheet is captured and compared with the palette (colours that are not the brand's), with
itself (a shot that does not change, a jump between shots) and anything wrong is repaired by rules and checked again (twice at most).
Then each format is rendered frame by frame into ffmpeg (yuv420p, 60 fps by default), the sound written in code is mastered to about -14 LUFS
and mixed in, and the picture and the contact sheet of the real file are saved as assets of the project.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from . import engine
from . import promo_brand as brand
from . import promo_sound as sound
from .backend import ffmpeg_path, ffprobe_path
from .jobs import JobCancelled
from .promo_compose import (FORMATS, LEAVE_S, MIN_HOLD_S, MIN_PACE, SETTLE_S, VIBES, PromoError, build_html, check_custom, check_request, hold_of,
                            look_of, plan_script, retime, shot_colors, shot_windows)
from .store import Store

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FROZEN_DIFF = 0.1           # mean grey levels between two 4 fps frames of a settled shot: below this the shot is a still
JUMP_DIFF = 34.0            # mean grey levels between two consecutive frames at a cut: above this the picture jumps
PALETTE_DISTANCE = 42.0     # colours this far (RGB) from every palette tint and blend are off brand
PALETTE_SHARE = 0.04        # ...when they cover this much of a frame
FRAME_FORMAT = os.environ.get("PROSPERO_PROMO_FRAME", "png")
MAX_FIX_ROUNDS = 2


# ------------------------------------------------------------------ measuring

def _gray(img: Image.Image, width: int = 160) -> np.ndarray:
    small = img.convert("L")
    small = small.resize((width, max(1, round(img.height * width / img.width))), Image.Resampling.BOX)
    return np.asarray(small, dtype=np.float32)


def _mean_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a - b)))


def _css_rgb(value: str) -> Optional[tuple[int, int, int]]:
    parsed = brand.parse_color(value)
    return parsed[:3] if parsed else None


def _issue(kind: str, shot: str, t: float, detail: str, severity: str = "error", **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "shot": shot, "t": round(t, 3), "detail": detail, "severity": severity, **extra}


def dom_issues(measures: list[tuple[float, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Text that leaves the frame or the safe margin, is cut, overlaps other text, is too small or has too little contrast, from the
    boxes the page reports (`window.measure()`) at moments when the text has settled."""
    found: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(kind: str, shot: str, item_id: str, t: float, detail: str) -> None:
        key = (kind, shot, item_id)
        if key in found:
            found[key]["count"] += 1
        else:
            found[key] = {**_issue(kind, shot, t, detail), "count": 1, "item": item_id}

    for t, report in measures:
        W, H = report["W"], report["H"]
        items = [i for i in report["items"] if i.get("box")]
        for item in items:
            box, shot = item["box"], item["shot"]
            text = item["text"][:40]
            if item["role"] == "mono":
                continue
            if box["x0"] < 0.04 * W or box["x1"] > 0.96 * W or box["y0"] < 0.04 * H or box["y1"] > 0.96 * H:
                add("text_out", shot, item["id"], t, f"'{text}' goes outside the frame or its safe margin ({box['x0']:.0f},{box['y0']:.0f}-{box['x1']:.0f},{box['y1']:.0f} in {W}x{H})")
            if item["sw"] > item["cw"] + 2:
                add("text_cut", shot, item["id"], t, f"'{text}' is wider than its box ({item['sw']} > {item['cw']} px)")
            if item["role"] != "idx" and item["fs"] < 0.02 * max(W, H):
                add("text_small", shot, item["id"], t, f"'{text}' is {item['fs']:.0f} px tall in a {W}x{H} frame: hard to read")
            ink, bg = _css_rgb(item["color"]), _css_rgb(item["bg"])
            if ink and bg:
                need = 3.0 if item["fs"] >= 0.025 * max(W, H) else 4.5
                ratio = brand.contrast(ink, bg)
                if ratio < need:
                    add("contrast", shot, item["id"], t, f"'{text}' has contrast {ratio:.1f}:1, needs {need:g}:1")
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if a["shot"] != b["shot"] or "mono" in (a["role"], b["role"]):
                    continue
                ax, ay = min(a["box"]["x1"], b["box"]["x1"]) - max(a["box"]["x0"], b["box"]["x0"]), min(a["box"]["y1"], b["box"]["y1"]) - max(a["box"]["y0"], b["box"]["y0"])
                if ax > 0 and ay > 0:
                    area = lambda r: max(1.0, (r["x1"] - r["x0"]) * (r["y1"] - r["y0"]))  # noqa: E731
                    if ax * ay / min(area(a["box"]), area(b["box"])) > 0.08:
                        add("overlap", a["shot"], a["id"] + "+" + b["id"], t, f"'{a['text'][:24]}' overlaps '{b['text'][:24]}'")
    return list(found.values())


def allowed_colors(palette: dict[str, str], extra: Optional[list[tuple[int, int, int]]] = None) -> np.ndarray:
    base = [brand.from_hex(v) for v in palette.values()] + [(0, 0, 0), (255, 255, 255)] + list(extra or [])
    points = list(base)
    for i, a in enumerate(base):
        for b in base[i + 1:]:
            for k in range(1, 8):
                points.append(brand.mix(a, b, k / 8))
    return np.asarray(points, dtype=np.float32)


def palette_issues(frames: dict[float, Image.Image], script: dict[str, Any], allowed: np.ndarray) -> list[dict[str, Any]]:
    """Colours that cover a good part of a frame and are not the brand's (nor a tint or blend of them)."""
    found: dict[str, dict[str, Any]] = {}
    starts = [(s["start"], s["key"]) for s in script["shots"]]
    for t, img in sorted(frames.items()):
        shot = [k for s, k in starts if s <= t + 1e-6][-1]
        for rgb, share in brand.pixel_bins(_to_bytes(img.resize((240, round(img.height * 240 / img.width)), Image.Resampling.BOX)), side=240)[:40]:
            if share < PALETTE_SHARE:
                break
            nearest = float(np.min(np.linalg.norm(allowed - np.asarray(rgb, dtype=np.float32), axis=1)))
            if nearest > PALETTE_DISTANCE:
                hexed = brand.to_hex(rgb)
                key = next((k for k, v in found.items() if brand.distance(brand.from_hex(k), rgb) < 36), hexed)
                row = found.setdefault(key, {**_issue("palette", shot, t, f"{hexed} covers {share * 100:.0f}% of the frame and is not a brand colour "
                                                         f"(nearest tint is {nearest:.0f} away)"), "count": 0, "colour": hexed})
                row["count"] += 1
    return list(found.values())


def _to_bytes(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def motion_issues(frames: dict[float, Image.Image], script: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Shots that do not change while they are on screen (a slide): the largest change between consecutive 4 fps frames of the settled stretch."""
    out, measured = [], {}
    gray = {t: _gray(img) for t, img in frames.items()}
    for shot, first, last in shot_windows(script):
        ts = sorted(t for t in gray if first - 1e-6 <= t <= last + 1e-6)
        if len(ts) < 2:
            continue
        biggest = max(_mean_diff(gray[a], gray[b]) for a, b in zip(ts, ts[1:]))
        measured[shot["key"]] = round(biggest, 3)
        if biggest < FROZEN_DIFF:
            out.append(_issue("frozen", shot["key"], ts[0], f"the picture changes only {biggest:.3f} grey levels between frames: it reads as a slide", "warn"))
    return out, measured


def hold_issues(script: dict[str, Any]) -> list[dict[str, Any]]:
    """On the timeline: a shot whose text is not complete at least MIN_HOLD_S before the cut that ends it (the cut is the end of the shot)."""
    out = []
    for shot in script["shots"]:
        hold = hold_of(shot)
        if hold < MIN_HOLD_S - 0.01:
            out.append(_issue("short_hold", shot["key"], shot["start"] + shot["dur"] - MIN_HOLD_S,
                              f"the text is complete only {max(hold, 0):.2f} s before the cut (it needs {MIN_HOLD_S:g} s)", "error", hold_s=hold))
    return out


def late_text_issues(late: dict[str, list[tuple[float, str, float]]]) -> list[dict[str, Any]]:
    """On the frames: at the last moment the text must already be complete (MIN_HOLD_S before the cut), words still on their way in."""
    out = []
    for shot, rows in late.items():
        if rows:
            t, item, rev = max(rows, key=lambda r: r[2])
            out.append(_issue("late_text", shot, t, f"at {t:.2f} s, {MIN_HOLD_S:g} s before the cut, '{item[:30]}' is still appearing ({rev * 100:.0f}% to go)", "error"))
    return out


def jump_issues(boundary: dict[str, float], limit: float = JUMP_DIFF) -> list[dict[str, Any]]:
    """A cut that changes the picture by more than `limit` grey levels from one frame to the next (a style of quick cuts allows more)."""
    return [_issue("jump", key, 0.0, f"the picture jumps {diff:.0f} grey levels between two frames at the cut", "error")
            for key, diff in boundary.items() if diff > limit]


def logo_colors(path: Path) -> list[tuple[int, int, int]]:
    """The colours that cover at least 2% of a logo: they are the brand's even when the three chosen colours are not them."""
    try:
        with Image.open(path) as img:
            rgba = img.convert("RGBA")
            rgba.thumbnail((96, 96))
        px = [(p[0], p[1], p[2]) for p in rgba.getdata() if p[3] > 200]
    except Exception:  # noqa: BLE001
        return []
    if not px:
        return []
    counts: dict[tuple[int, int, int], int] = {}
    for r, g, b in px:
        key = (r >> 4, g >> 4, b >> 4)
        counts[key] = counts.get(key, 0) + 1
    return [((k[0] << 4) + 8, (k[1] << 4) + 8, (k[2] << 4) + 8) for k, n in counts.items() if n / len(px) >= 0.02]


# ------------------------------------------------------------------ the browser pass

def _page(browser: Any, html_path: Path, width: int, height: int) -> tuple[Any, list[str]]:
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(html_path.resolve().as_uri(), wait_until="load")
    try:
        page.wait_for_function("window.__ready === true", timeout=20000)
    except Exception:  # noqa: BLE001
        raise PromoError("page_not_ready", "the composition did not finish loading its fonts") from None
    if errors:
        raise PromoError("page_script", "the composition's script failed: " + "; ".join(errors[:2]))
    return page, errors


def sample_times(duration: float, rate: float = 4.0) -> list[float]:
    return [round(k / rate, 4) for k in range(int(duration * rate + 1e-9))]


def review_format(html_path: Path, script: dict[str, Any], kit: dict[str, Any], fmt: str, fps: int,
                  cancelled: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
    """Captures the 4 fps contact frames of one format, measures the text in the DOM and runs every check."""
    width, height = FORMATS[fmt]
    windows = shot_windows(script)
    frames: dict[float, Image.Image] = {}
    measures: list[tuple[float, dict[str, Any]]] = []
    boundary: dict[str, float] = {}
    late: dict[str, list[tuple[float, str, float]]] = {}
    with brand.open_browser() as browser:
        page, errors = _page(browser, html_path, width, height)
        try:
            for t in sample_times(script["length_s"]):
                if cancelled and cancelled():
                    raise JobCancelled()
                page.evaluate("t => window.seek(t)", t)
                frames[t] = Image.open(io.BytesIO(page.screenshot(type="jpeg", quality=85))).convert("RGB")
                if any(first - 1e-6 <= t <= last + 1e-6 for _s, first, last in windows):
                    measures.append((t, page.evaluate("() => window.measure()")))
            for shot in script["shots"]:
                # the latest moment at which the text of the shot has to be complete and still on screen
                t_late = max(shot["start"], shot["start"] + shot["dur"] - MIN_HOLD_S)
                page.evaluate("t => window.seek(t)", t_late)
                report = page.evaluate("() => window.measure()")
                late[shot["key"]] = [(t_late, i["text"], i["rev"]) for i in report["items"] if i["shot"] == shot["key"] and i.get("rev", 0) > 0.01]
            for shot in script["shots"][1:]:
                grays = []
                for k in range(-1, 7):
                    page.evaluate("t => window.seek(t)", max(0.0, shot["start"] + k / fps))
                    grays.append(_gray(Image.open(io.BytesIO(page.screenshot(type="jpeg", quality=70))).convert("RGB")))
                boundary[shot["key"]] = max(_mean_diff(a, b) for a, b in zip(grays, grays[1:]))
            if errors:
                raise PromoError("page_script", "the composition's script failed: " + "; ".join(errors[:2]))
        finally:
            page.close()
    extra = logo_colors(Path(kit["files"]["folder"]) / kit["logo"]["file"]) if kit.get("logo") else []
    motion, motion_values = motion_issues(frames, script)
    issues = (dom_issues(measures) + palette_issues(frames, script, allowed_colors(kit["palette"], extra)) + motion + jump_issues(boundary, look_of(script["vibe"])["jump"])
              + hold_issues(script) + late_text_issues(late))
    try:
        from .editing.motion_lint import slideshow_lint
        lint = slideshow_lint(html_path.read_text(encoding="utf-8"))
        if lint["verdict"] == "reject":
            issues.append(_issue("slideshow", "-", 0.0, lint["reprompt"], "warn"))
    except ImportError:
        pass
    return {"format": fmt, "frames": frames, "issues": issues, "motion": motion_values, "holds": {s["key"]: hold_of(s) for s in script["shots"]}, "jumps": {k: round(v, 2) for k, v in boundary.items()},
            "samples": len(frames), "text_checks": len(measures)}


def apply_fixes(script: dict[str, Any], issues: list[dict[str, Any]]) -> list[str]:
    """Repairs by rules what the checks found, in the script itself; returns what was changed."""
    shots = {s["key"]: s for s in script["shots"]}
    done: list[str] = []
    for issue in issues:
        shot, kind = shots.get(issue["shot"]), issue["kind"]
        if kind in ("text_out", "text_cut", "overlap") and shot:
            before = shot["fit"]
            shot["fit"] = round(max(0.45, shot["fit"] * 0.82), 3)
            if shot["kind"] in ("point", "value") and len(shot["text"]) > 48 and before > 0.8:
                shot["text"] = brand.clean_text(shot["text"], int(len(shot["text"]) * 0.78))
            done.append(f"{shot['key']}: smaller text ({before:.2f} -> {shot['fit']:.2f}) for {kind}")
        elif kind == "text_small" and shot and shot["fit"] < 1.4:
            shot["fit"] = round(shot["fit"] * 1.2, 3)
            done.append(f"{shot['key']}: larger text for {kind}")
        elif kind == "contrast" and shot:
            shot["bg_role"] = "dark" if shot["bg_role"] != "dark" else "light"
            done.append(f"{shot['key']}: background changed to {shot['bg_role']} for contrast")
        elif kind == "palette":
            if script.get("deco", True):
                script["deco"] = False
                done.append("decorative shape removed (it brought a colour out of the palette)")
        elif kind == "frozen" and shot:
            shot["drift"] = round(min(4.0, shot["drift"] * 2.0), 2)
            if not script.get("deco", True):
                script["deco"] = True
            done.append(f"{shot['key']}: stronger drift so the shot does not read as a slide")
        elif kind in ("short_hold", "late_text") and shot:
            before = shot.get("pace", 1.0)
            shot["pace"] = round(max(MIN_PACE, before * 0.8), 4)
            retime(shot, script["vibe"])
            done.append(f"{shot['key']}: faster reveal ({before:.2f} -> {shot['pace']:.2f}) so the text is complete {MIN_HOLD_S:g} s before the cut")
        elif kind == "jump":
            script["wipe_s"] = round(min(1.1, float(script.get("wipe_s", 0.5)) + 0.2), 2)
            done.append(f"wipe between shots lengthened to {script['wipe_s']} s")
    return list(dict.fromkeys(done))


# ------------------------------------------------------------------ contact sheet

def _label_font(size: int = 13) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(brand.FONT_DIR / "Inter" / "Inter.ttf"), size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()  # type: ignore[return-value]


def make_sheet(items: list[tuple[float, Image.Image]], *, thumb_w: int = 192, columns: Optional[int] = None,
               marks: Optional[dict[float, str]] = None, title: str = "") -> Image.Image:
    """Frames in a grid, each with its time; a frame with a problem has a red (error) or amber (warning) border."""
    if not items:
        raise PromoError("no_frames", "there are no frames to put on the sheet")
    marks = marks or {}
    first = items[0][1]
    thumb_h = max(1, round(first.height * thumb_w / first.width))
    portrait = first.height > first.width
    cols = columns or (min(14, max(1, 1920 // (thumb_w + 6))) if portrait else min(10, max(1, 1920 // (thumb_w + 6))))
    cols = max(1, min(cols, len(items)))
    rows = math.ceil(len(items) / cols)
    pad, label_h, head = 6, 20, 30 if title else 0
    sheet = Image.new("RGB", (cols * (thumb_w + pad) + pad, head + rows * (thumb_h + label_h + pad) + pad), (22, 22, 26))
    draw = ImageDraw.Draw(sheet)
    font = _label_font()
    if title:
        draw.text((pad + 2, 6), title, font=_label_font(15), fill=(235, 235, 240))
    for n, (t, img) in enumerate(items):
        x = pad + (n % cols) * (thumb_w + pad)
        y = head + pad + (n // cols) * (thumb_h + label_h + pad)
        sheet.paste(img.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS), (x, y))
        level = marks.get(round(t, 4))
        if level:
            colour = (220, 60, 60) if level == "error" else (230, 160, 30)
            draw.rectangle((x - 2, y - 2, x + thumb_w + 1, y + thumb_h + 1), outline=colour, width=3)
        draw.text((x + 2, y + thumb_h + 2), f"{t:.2f} s", font=font, fill=(225, 225, 230) if not level else (255, 190, 120))
    return sheet


def sheet_from_video(video: Path, *, fps: float = 4.0, thumb_w: int = 192, columns: Optional[int] = None, max_frames: int = 400,
                     title: str = "", marks: Optional[dict[float, str]] = None) -> tuple[Image.Image, dict[str, Any]]:
    """The contact sheet of a video file: a frame every 1/fps s (fewer if there would be more than `max_frames`)."""
    exe = ffmpeg_path()
    if not exe:
        raise PromoError("no_ffmpeg", "ffmpeg was not found")
    if not video.is_file():
        raise PromoError("no_video", "the video file is missing")
    probe = _probe(video)
    duration = float(probe.get("duration") or 0)
    rate = float(fps)
    if duration * rate > max_frames:
        rate = max_frames / max(duration, 0.1)
    with tempfile.TemporaryDirectory(prefix="promo-sheet-") as tmp:
        proc = subprocess.run([exe, "-hide_banner", "-v", "error", "-i", str(video), "-an", "-vf", f"fps={rate:.6f},scale={thumb_w * 2}:-2",
                               "-frames:v", str(max_frames + 1), str(Path(tmp) / "f%05d.png")], capture_output=True, text=True, timeout=600,
                              creationflags=_NO_WINDOW)
        files = sorted(Path(tmp).glob("f*.png"))
        if proc.returncode != 0 or not files:
            raise PromoError("sheet_failed", "frames could not be read from the video: " + proc.stderr[-300:])
        items = []
        for i, f in enumerate(files):
            with Image.open(f) as im:
                items.append((i / rate, im.convert("RGB")))
        sheet = make_sheet(items, thumb_w=thumb_w, columns=columns, title=title, marks=marks)
    return sheet, {"frames": len(items), "fps": round(rate, 3), "duration_s": round(duration, 3), "marked": len(marks or {})}


# ------------------------------------------------------------------ rendering

def _probe(path: Path) -> dict[str, Any]:
    exe = ffprobe_path()
    if not exe:
        raise PromoError("no_ffprobe", "ffprobe was not found")
    proc = subprocess.run([exe, "-v", "error", "-count_packets", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True,
                          text=True, timeout=120, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise PromoError("probe_failed", "ffprobe could not read the file: " + proc.stderr[-200:])
    data = json.loads(proc.stdout)
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    num, _, den = str(video.get("avg_frame_rate", "0/1")).partition("/")
    rate = float(num) / float(den or 1) if float(den or 1) else 0.0
    return {"width": video.get("width"), "height": video.get("height"), "fps": round(rate, 3), "pix_fmt": video.get("pix_fmt"),
            "codec": video.get("codec_name"), "frames": int(video.get("nb_read_packets") or 0), "duration": float(data["format"].get("duration") or 0),
            "audio": {"codec": audio["codec_name"], "sample_rate": int(audio.get("sample_rate") or 0), "channels": audio.get("channels")} if audio else None}


def render_video(html_path: Path, out_path: Path, fmt: str, fps: int, duration: float, on_frame: Callable[[int], None],
                 cancelled: Optional[Callable[[], bool]] = None) -> int:
    """Draws every frame of one format in a browser and feeds it to ffmpeg: H.264, yuv420p, BT.709, no sound yet."""
    width, height = FORMATS[fmt]
    exe = ffmpeg_path()
    if not exe:
        raise PromoError("no_ffmpeg", "ffmpeg was not found")
    count = int(round(duration * fps))
    log = out_path.with_suffix(".log")
    codec = "png" if FRAME_FORMAT == "png" else "mjpeg"
    with brand.open_browser() as browser:
        page, errors = _page(browser, html_path, width, height)
        process = None
        try:
            with log.open("wb") as stderr:
                process = subprocess.Popen([exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(fps), "-c:v", codec,
                                            "-i", "pipe:0", "-an", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264",
                                            "-preset", "medium", "-crf", "17", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                                            "-r", str(fps), "-movflags", "+faststart", str(out_path)],
                                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr, creationflags=_NO_WINDOW)
                for i in range(count):
                    if cancelled and cancelled():
                        raise JobCancelled()
                    page.evaluate("t => window.seek(t)", i / fps)
                    shot = page.screenshot(type="png") if codec == "png" else page.screenshot(type="jpeg", quality=95)
                    try:
                        process.stdin.write(shot)
                    except OSError:
                        raise PromoError("encode_failed", "ffmpeg stopped while encoding: " + log.read_text(errors="replace")[-300:]) from None
                    on_frame(i + 1)
                if errors:
                    raise PromoError("page_script", "the composition's script failed: " + "; ".join(errors[:2]))
                process.stdin.close()
                if process.wait(timeout=300) != 0:
                    raise PromoError("encode_failed", "ffmpeg could not encode the video: " + log.read_text(errors="replace")[-300:])
        finally:
            page.close()
            if process is not None and process.poll() is None:
                process.kill()
    log.unlink(missing_ok=True)
    return count


# ------------------------------------------------------------------ the job

def _chat_or_none(chat: Optional[Callable[..., str]]) -> Optional[Callable[..., str]]:
    return chat


def prepare(store: Store, params: dict[str, Any], progress: Callable[..., Any]) -> dict[str, Any]:
    """The brand kit of a request: a saved one (kit_id) or a fresh scan of the url."""
    data_dir = store.data_dir
    if params.get("kit_id"):
        return brand.load_kit(data_dir, params["kit_id"])
    if not params.get("url"):
        raise PromoError("url_required", "give the address of the web page (url) or a saved brand kit (kit_id)")
    return brand.scan(data_dir, params["url"])


def review_script(kit: dict[str, Any], script: dict[str, Any], formats: list[str], fps: int, work: Path,
                  cancelled: Optional[Callable[[], bool]] = None, on_round: Optional[Callable[[int], None]] = None) -> dict[str, Any]:
    """Builds the page of every format, checks it (text, contrast, palette, motion, jumps, the hold of every text before its cut) and repairs
    by rules what failed, checking again, twice at most. The script is changed in place by the repairs."""
    rounds: list[dict[str, Any]] = []
    fixes_done: list[str] = []
    html_paths: dict[str, Path] = {}
    issues: list[dict[str, Any]] = []
    frames: dict[str, dict[float, Image.Image]] = {}
    for attempt in range(MAX_FIX_ROUNDS + 1):
        if on_round:
            on_round(attempt)
        issues = []
        reviews = {}
        for fmt in formats:
            html_paths[fmt] = build_html(kit, script, *FORMATS[fmt], fps, work / fmt.replace(":", "x"))
            reviews[fmt] = review_format(html_paths[fmt], script, kit, fmt, fps, cancelled)
            issues += [{**i, "format": fmt} for i in reviews[fmt]["issues"]]
        frames = {fmt: r["frames"] for fmt, r in reviews.items()}
        rounds.append({"round": attempt + 1, "issues": [{k: v for k, v in i.items()} for i in issues[:30]],
                       "motion": {f: r["motion"] for f, r in reviews.items()}, "jumps": {f: r["jumps"] for f, r in reviews.items()},
                       "holds": {f: r["holds"] for f, r in reviews.items()}})
        if not issues or attempt == MAX_FIX_ROUNDS:
            break
        changed = apply_fixes(script, issues)
        rounds[-1]["fixes"] = changed
        fixes_done += changed
        if not changed:
            break
    return {"rounds": rounds, "fixes": fixes_done, "html": html_paths, "issues": issues, "frames": frames}


def render_job(store: Store, job: dict[str, Any], progress: Any, chat: Optional[Callable[..., str]] = None) -> dict[str, Any]:
    """The ``promo_render`` job: scan, script, checks and fixes, render, sound, import."""
    p = job["params"]
    project_id = p["project_id"]
    length, formats, vibe, fps = check_request(p.get("length_s"), p.get("formats"), p.get("vibe"), p.get("fps"))
    cancelled = getattr(progress, "cancelled", None)
    data_dir = Path(store.data_dir)
    music_path = None
    if p.get("music_asset_id"):
        music = store.get_asset(p["music_asset_id"])
        if music["kind"] != "audio":
            raise PromoError("bad_music", "music_asset_id must be an audio asset of the project")
        music_path = data_dir / music["file_path"]
    progress(0.02, "scanning the web page" if not p.get("kit_id") else "loading the brand kit")
    kit = prepare(store, p, progress)
    kit_assets = {}
    if not p.get("skip_kit_assets"):
        kit_assets = brand.import_kit_assets(store, project_id, kit)
    progress(0.1, "writing the script")
    script = plan_script(kit, length, vibe, p.get("language") or "auto", chat, p.get("custom"))
    work = data_dir / "tmp" / "promo" / (str(job.get("id") or "job"))
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        review = review_script(kit, script, formats, fps, work, cancelled,
                               lambda attempt: progress(0.14 + 0.06 * attempt, "checking the composition" + (f" (round {attempt + 1})" if attempt else "")))
        rounds, fixes_done, html_paths, last_issues = review["rounds"], review["fixes"], review["html"], review["issues"]
        remaining = last_issues
        total = int(round(length * fps)) * len(formats)
        done = {"n": 0}
        lock = threading.Lock()
        paths = {fmt: work / f"video-{fmt.replace(':', 'x')}.mp4" for fmt in formats}

        def tick(_n: int) -> None:
            with lock:
                done["n"] += 1
                if done["n"] % 15 == 0:
                    progress(0.24 + 0.56 * done["n"] / total, f"frame {done['n']} of {total}")

        progress(0.24, "rendering the frames")
        with ThreadPoolExecutor(max_workers=len(formats)) as pool:
            futures = [pool.submit(render_video, html_paths[f], paths[f], f, fps, length, tick, cancelled) for f in formats]
            for future in futures:
                future.result()
        progress(0.82, "writing the sound")
        events = sound.events_for(script["shots"], script["bpm"], length, look_of(vibe)["sound"])
        audio = sound.synthesise(events, length, pulse=music_path is None)
        exe = ffmpeg_path()
        if music_path:
            audio = sound.mix_with_music(audio, sound.decode_music(exe, music_path, length))
        raw_wav, master = work / "mix.wav", work / "master.wav"
        sound.write_wav(raw_wav, audio)
        sound.master_for_delivery(exe, raw_wav, master)
        loud = sound.measure(exe, master)
        results: dict[str, Any] = {}
        asset_ids: dict[str, str] = {}
        sheet_ids: dict[str, str] = {}
        progress(0.88, "mixing the sound into the video")
        for n, fmt in enumerate(formats):
            final = work / f"final-{fmt.replace(':', 'x')}.mp4"
            sound.mux(exe, paths[fmt], master, final)
            info = _probe(final)
            expected = int(round(length * fps))
            info["ok"] = bool((info["width"], info["height"]) == FORMATS[fmt] and info["pix_fmt"] == "yuv420p" and abs(info["fps"] - fps) < 0.01
                              and info["frames"] == expected and info["audio"] and info["audio"]["codec"] == "aac")
            info["loudness"] = sound.measure(exe, final)
            results[fmt] = info
            if not info["ok"]:
                raise PromoError("verify_failed", f"the {fmt} video does not match what was asked: {info}")
            progress(0.9 + 0.05 * n / len(formats), "contact sheet of the real video")
            marks = {}
            for issue in remaining:
                if issue.get("format") == fmt:
                    key = round(round(issue["t"] * 4) / 4, 4)
                    if marks.get(key) != "error":
                        marks[key] = issue["severity"]
            sheet, sheet_info = sheet_from_video(final, fps=4, thumb_w=192 if fmt == "16:9" else 108, title=f"{kit['host']} - {fmt} - 4 fps",
                                                 marks=marks)
            sheet_path = work / f"sheet-{fmt.replace(':', 'x')}.png"
            sheet.save(sheet_path)
            recipe = {"operation": "promo_from_url", "url": kit["url"], "kit_id": kit["id"], "vibe": vibe, "length_s": length, "format": fmt, "fps": fps,
                      "input_asset_ids": [p["music_asset_id"]] if p.get("music_asset_id") else [], "script": script, "fixes": fixes_done,
                      "sound": {"bpm": script["bpm"], "profile": look_of(vibe)["sound"], "events": len(events), "music": bool(music_path), "lufs": results[fmt]["loudness"]["lufs"]}}
            name = f"{kit['host']} promo {fmt.replace(':', 'x')}"
            asset = engine.import_asset(store, project_id, final, "video", original_name=f"{name}.mp4", recipe=recipe, tags=["promo", fmt, vibe], source="rendered")
            asset_ids[fmt] = asset["id"]
            image = engine.import_asset(store, project_id, sheet_path, "image", original_name=f"{name} contact sheet.png", source="rendered",
                                        recipe={"operation": "contact_sheet", "input_asset_ids": [asset["id"]], "fps": 4, **sheet_info}, tags=["promo", "contact-sheet", fmt])
            sheet_ids[fmt] = image["id"]
        progress(0.99, "done")
        ids = list(asset_ids.values()) + list(sheet_ids.values())
        return {"asset_id": ids[0], "asset_ids": ids, "videos": asset_ids, "contact_sheets": sheet_ids, "kit": {k: kit.get(k) for k in ("id", "host", "site_name", "colors", "palette", "headline")},
                "kit_assets": kit_assets, "script": script, "written_by": script["written_by"], "checks": {"rounds": len(rounds), "fixes": fixes_done,
                "remaining": [{k: i[k] for k in ("kind", "shot", "t", "detail", "severity", "format")} for i in remaining[:40]], "log": rounds,
                "min_hold_s": min(hold_of(s) for s in script["shots"]), "required_hold_s": MIN_HOLD_S},
                "files": results, "loudness": loud, "fps": fps, "length_s": length}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ for the API

def scan_for_project(store: Store, url: str, project_id: Optional[str] = None) -> dict[str, Any]:
    """The `brand_scan` operation: scan, keep the kit, and with a project put the logo and the brand board in its library."""
    if project_id:
        store.get_project(project_id)
    kit = brand.scan(Path(store.data_dir), url)
    out = {k: kit.get(k) for k in ("id", "url", "final_url", "host", "site_name", "headline", "value_prop", "phrases", "language", "colors", "palette",
                                   "logo", "fonts")}
    if project_id:
        out["assets"] = brand.import_kit_assets(store, project_id, kit)
    return out


def contact_sheet_for_asset(store: Store, asset_id: str, *, fps: float = 4.0, columns: Optional[int] = None, thumb_w: Optional[int] = None) -> dict[str, Any]:
    """The `contact_sheet` operation: a sheet of a project's video asset (a frame every 1/fps s) saved as an image asset beside it."""
    asset = store.get_asset(asset_id)
    if asset["kind"] != "video":
        raise PromoError("not_video", "contact_sheet needs a video asset")
    try:
        rate = float(fps)
    except (TypeError, ValueError):
        raise PromoError("bad_fps", "fps must be a number") from None
    if not 0.1 <= rate <= 30:
        raise PromoError("bad_fps", "fps is between 0.1 and 30 frames per second")
    portrait = (asset.get("height") or 0) > (asset.get("width") or 0)
    sheet, info = sheet_from_video(Path(store.data_dir) / asset["file_path"], fps=rate, columns=columns, thumb_w=thumb_w or (108 if portrait else 192),
                                   title=f"{asset.get('name') or asset_id} - {rate:g} fps")
    with tempfile.TemporaryDirectory(prefix="promo-sheet-") as tmp:
        path = Path(tmp) / "sheet.png"
        sheet.save(path)
        image = engine.import_asset(store, asset["project_id"], path, "image", original_name=f"{Path(asset.get('name') or 'video').stem} contact sheet.png",
                                    source="rendered", recipe={"operation": "contact_sheet", "input_asset_ids": [asset_id], **info}, tags=["contact-sheet"])
    return {"asset_id": image["id"], "width": sheet.width, "height": sheet.height, **info}
