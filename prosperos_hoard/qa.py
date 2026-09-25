"""The local QA director: checks a production's outputs stage by stage and,
when asked to, regenerates the ones that fail.

**Model-free checks** (numpy/Pillow/ffmpeg, always on):

- flat image (luminance spread) or noise (pixel-to-pixel differences as
  large as the spread itself, the "Qwen edit came out as texture" failure);
- black or coloured bands along an edge (ffmpeg 8's planar-RGB round trip
  blacked out the right-most 8 columns of a 1080-wide frame and the grade
  tinted them red): a strip that differs from its neighbour on almost
  every row/column;
- exposure jumps between consecutive frames of a clip (mean luminance);
- motion vs the shot's `motion` flag (mean frame-difference energy): a
  figure asked to stay still that walks, or a clip that froze;
- head cropping on photocard photos: the subject (colour distance from the
  side-border background plus edges - a saliency proxy, no face detector)
  reaching the top edge;
- lyric coverage of an aligned/imported LRC (share of the written lines it
  contains) and durations against the plan (song, clips, renders).

**Model checks** through Hoard Link's vision capability (the family's
`Link.chat` with an image): 0-10 scores against the bible (the lead's look
and palette), the shot prompt, and the reference (the lead's canonical
reference for stills, the source still for clips), with a one-line reason.
Without a vision model every item says `"no vision model"` and the pass
goes on - QA never blocks a production.

**Policy**: a threshold per check (overridable per stage in
`settings.qa.thresholds`) and a retry cap (`settings.qa.max_retries`,
default 2). A failing still, clip or photocard photo is regenerated with a
new seed plus a targeted fix for a known failure (noise -> denoise 1;
exposure jump -> another sampler and a lower cfg; walking when it should
stand still -> the stillness negative; a cropped head -> headroom in the
prompt). Every retry and every give-up is written to the production's
lineage and REPORT.md.
"""

from __future__ import annotations

import difflib
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
from PIL import Image, ImageOps

from . import audio as audio_mod
from . import procutil
from . import productions as prod
from .backend import ffmpeg_path
from .store import NotFound, Store
from .util import now_iso

VisionFn = Callable[[list[bytes], str], str]

DEFAULT_THRESHOLDS: dict[str, float] = {
    "flat_std": 6.0,            # luminance standard deviation (0-255) below which a picture is flat
    "noise_ratio": 0.62,        # neighbour-difference spread / picture spread above which it is noise
    "band_delta": 38.0,         # edge strip vs its neighbour, mean |difference| per channel (0-255)
    "band_rows": 0.9,           # share of rows/columns that must show the difference
    "exposure_jump": 30.0,      # mean-luminance change between consecutive frames (0-255)
    "still_motion_max": 10.0,   # frame-difference energy above which a "still" shot moves too much
    "move_motion_min": 0.4,     # frame-difference energy below which a "move" shot is frozen
    "headroom_min": 0.02,       # share of the height above the subject on a photocard photo
    "lyric_coverage": 0.85,     # share of written lines an aligned LRC must contain
    "duration_tolerance": 0.06, # relative duration difference allowed against the plan
    "model_min": 6.0,           # lowest acceptable vision score (0-10)
}
RETRYABLE_STAGES = ("frames", "clips", "photocards")
CHECKED_STAGES = ("character", "song", "frames", "lyrics", "animatic", "clips", "photocards", "timeline")
MAX_MODEL_ITEMS = 80


class QAError(prod.ProductionError):
    pass


# ------------------------------------------------------------- measurements

def _gray(img: Image.Image, max_side: int = 384) -> np.ndarray:
    img = ImageOps.exif_transpose(img).convert("L")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    return np.asarray(img, dtype=np.float32)


def image_stats(img: Image.Image) -> dict[str, float]:
    """`std`: luminance spread; `noise_ratio`: spread of horizontal and
    vertical neighbour differences over sqrt(2) x the spread - about 1 for
    white noise, far lower for a picture with shapes and gradients."""
    g = _gray(img)
    std = float(g.std())
    if std < 1e-6:
        return {"std": 0.0, "noise_ratio": 0.0, "mean": float(g.mean())}
    dx = np.diff(g, axis=1)
    dy = np.diff(g, axis=0)
    diff_std = float(np.sqrt((dx.var() + dy.var()) / 2.0))
    return {"std": round(std, 2), "noise_ratio": round(diff_std / (np.sqrt(2.0) * std), 3), "mean": round(float(g.mean()), 2)}


def edge_bands(img: Image.Image, width: int = 8, delta: float = 38.0, share: float = 0.9) -> list[dict[str, Any]]:
    """Strips along an edge that differ from the strip next to them on
    almost every row (or column): the ffmpeg gbrp band, a black border, a
    red stripe. A natural edge differs only here and there."""
    a = np.asarray(ImageOps.exif_transpose(img).convert("RGB"), dtype=np.float32)
    h, w = a.shape[:2]
    if w < 4 * width or h < 4 * width:
        return []
    strips = {
        "right": (a[:, w - width:], a[:, w - 2 * width:w - width], 1),
        "left": (a[:, :width], a[:, width:2 * width], 1),
        "bottom": (a[h - width:], a[h - 2 * width:h - width], 0),
        "top": (a[:width], a[width:2 * width], 0),
    }
    found = []
    for side, (edge, inner, axis) in strips.items():
        # per row (vertical strips) or per column (horizontal ones): mean colour of each strip
        e = edge.mean(axis=axis)
        i = inner.mean(axis=axis)
        d = np.abs(e - i).max(axis=1)
        frac = float((d > delta / 2).mean())
        med = float(np.median(d))
        if med > delta and frac >= share:
            colour = [int(round(v)) for v in edge.reshape(-1, 3).mean(axis=0)]
            found.append({"side": side, "delta": round(med, 1), "rows": round(frac, 3), "rgb": colour})
    return found


def headroom(img: Image.Image) -> dict[str, Any]:
    """Where the subject starts from the top, as a share of the height.
    Background colour = the median of the side borders (a full-length
    portrait keeps its sides clear); saliency = colour distance from it
    plus local edges; the subject's top = the first row whose central band
    is mostly salient."""
    rgb = ImageOps.exif_transpose(img).convert("RGB")
    rgb.thumbnail((256, 256))
    a = np.asarray(rgb, dtype=np.float32)
    h, w = a.shape[:2]
    side = max(2, w // 12)
    border = np.concatenate([a[:, :side].reshape(-1, 3), a[:, w - side:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    dist = np.sqrt(((a - bg) ** 2).sum(axis=2))
    g = a.mean(axis=2)
    edges = np.zeros_like(g)
    edges[:, 1:] += np.abs(np.diff(g, axis=1))
    edges[1:, :] += np.abs(np.diff(g, axis=0))
    border_dist = np.sqrt(((border - bg) ** 2).sum(axis=1))
    cut = max(28.0, float(np.percentile(border_dist, 95)) * 1.5)
    mask = (dist > cut) | (edges > 60)
    centre = mask[:, w // 5: w - w // 5]
    rows = centre.mean(axis=1)
    subject_rows = np.nonzero(rows > 0.08)[0]
    if subject_rows.size == 0:
        return {"subject": False, "headroom": None}
    top = int(subject_rows[0])
    return {"subject": True, "headroom": round(top / h, 3), "top_row_share": round(float(rows[0]), 3)}


def _ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise QAError("ffmpeg_missing", "ffmpeg not found (install ffmpeg or the imageio-ffmpeg wheel)")
    return exe


def video_gray_frames(path: Path, width: int = 160, height: int = 90, max_frames: int = 400) -> np.ndarray:
    """Every frame of a clip, tiny and grey: (n, height, width) float32."""
    cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-i", str(path), "-vf", f"scale={width}:{height},format=gray",
           "-frames:v", str(max_frames), "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    proc = procutil.run(cmd, timeout=180)
    if proc.returncode != 0:
        raise QAError("decode_failed", f"could not read {path.name}: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = data.size // (width * height)
    return data[: n * width * height].reshape(n, height, width).astype(np.float32)


def video_frame(path: Path, at_s: float) -> Optional[Image.Image]:
    cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-ss", f"{max(0.0, at_s):.3f}", "-i", str(path), "-frames:v", "1",
           "-f", "image2pipe", "-vcodec", "png", "-"]
    proc = procutil.run(cmd, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def clip_motion(frames: np.ndarray) -> dict[str, Any]:
    """Mean luminance per frame, the largest jump between consecutive
    frames (and where), and the mean frame-difference energy."""
    if len(frames) < 2:
        return {"frames": int(len(frames)), "max_jump": 0.0, "jump_at": None, "energy": 0.0}
    means = frames.reshape(len(frames), -1).mean(axis=1)
    jumps = np.abs(np.diff(means))
    energy = float(np.abs(np.diff(frames, axis=0)).mean())
    i = int(jumps.argmax())
    return {"frames": int(len(frames)), "max_jump": round(float(jumps[i]), 2), "jump_at": i + 1,
            "energy": round(energy, 3), "mean_luma": round(float(means.mean()), 1)}


_NORM = re.compile(r"[^\w\s]", re.UNICODE)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _NORM.sub(" ", text.lower())).strip()


def lyric_coverage(written: str, lrc_lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Share of the written lyric lines (section tags and ad-libs in
    parentheses left out) that an aligned LRC contains."""
    wanted = []
    for raw in written.splitlines():
        line = raw.strip()
        if not line or re.fullmatch(r"\[[^\]]*\]", line) or re.fullmatch(r"\(.*\)", line):
            continue
        wanted.append(_norm(line))
    got = [_norm(ln.get("text") or "") for ln in lrc_lines if (ln.get("text") or "").strip()]
    if not wanted:
        return {"lines": 0, "matched": 0, "coverage": None}
    matched, missing = 0, []
    for w in wanted:
        best = max((difflib.SequenceMatcher(None, w, g).ratio() for g in got), default=0.0)
        if best >= 0.75:
            matched += 1
        else:
            missing.append(w[:60])
    return {"lines": len(wanted), "matched": matched, "coverage": round(matched / len(wanted), 3), "missing": missing[:8]}


# ------------------------------------------------------------ model checks

def model_prompt(lead: dict[str, Any], shot_prompt: str, lead_in_frame: bool, has_reference: bool, kind: str) -> str:
    palette = ", ".join(lead.get("palette") or []) or "not given"
    parts = [
        "You are a strict continuity supervisor for a music video. Image 1 is a production "
        f"{'video frame' if kind == 'clip' else 'still'}.",
        ("Image 2 is the reference." if has_reference else ""),
        f"Bible - lead character {lead.get('name')}: {lead.get('look')}. Palette: {palette}.",
        f"What was asked for this shot: {shot_prompt}.",
        ("The lead should be in this shot." if lead_in_frame else "The lead should NOT be in this shot; judge the bible "
                                                                  "score on the world's look and palette instead."),
        "Score each 0-10: bible (colour palette, silhouette, key props consistent with the bible), prompt (does it show "
        "what was asked), reference (is it the same character/scene as image 2; null when there is no image 2).",
        'Answer with JSON only: {"bible": n, "prompt": n, "reference": n or null, "reason": "one short line"}',
    ]
    return "\n".join(p for p in parts if p)


def parse_scores(text: str) -> dict[str, Any]:
    """The model's JSON answer, tolerating prose or code fences around it."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    data: dict[str, Any] = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            data = {}
    out: dict[str, Any] = {}
    for key in ("bible", "prompt", "reference"):
        value = data.get(key)
        if value is None:
            m = re.search(rf'"?{key}"?\s*[:=]\s*(\d+(?:\.\d+)?)', text or "", re.IGNORECASE)
            value = float(m.group(1)) if m else None
        try:
            out[key] = None if value is None else max(0.0, min(10.0, float(value)))
        except (TypeError, ValueError):
            out[key] = None
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    out["reason"] = (reason or re.sub(r"\s+", " ", text or "")[:160]).strip()[:200]
    return out


def _jpeg(img: Image.Image, side: int = 768) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


# ------------------------------------------------------------------ context

class QA:
    """One QA pass over a production's state."""

    def __init__(self, store: Store, state: dict[str, Any], vision: Optional[VisionFn] = None,
                 vision_name: Optional[str] = None):
        self.store = store
        self.state = state
        self.spec = state.get("spec") or {}
        self.vision = vision
        self.vision_name = vision_name or ("vision model" if vision else "no vision model")
        self.model_calls = 0
        settings = (state.get("settings") or {}).get("qa") or {}
        self.user_thresholds = settings.get("thresholds") or {}

    def thresholds(self, stage: str) -> dict[str, float]:
        out = dict(DEFAULT_THRESHOLDS)
        flat = {k: v for k, v in self.user_thresholds.items() if not isinstance(v, dict)}
        out.update({k: float(v) for k, v in flat.items() if k in DEFAULT_THRESHOLDS})
        per_stage = self.user_thresholds.get(stage)
        if isinstance(per_stage, dict):
            out.update({k: float(v) for k, v in per_stage.items() if k in DEFAULT_THRESHOLDS})
        return out

    def path(self, asset_id: str) -> Path:
        return self.store.data_dir / self.store.get_asset(asset_id)["file_path"]

    def shot(self, key: str) -> dict[str, Any]:
        base, _ = prod.split_key(key)
        return next((s for s in self.spec.get("shots") or [] if s["key"] == base), {})

    # -- model
    def model_check(self, image: Image.Image, reference: Optional[str], shot_prompt: str, lead_in_frame: bool,
                    kind: str) -> dict[str, Any]:
        if self.vision is None:
            return {"skipped": "no vision model"}
        if self.model_calls >= MAX_MODEL_ITEMS:
            return {"skipped": f"model checks capped at {MAX_MODEL_ITEMS} per pass"}
        images = [_jpeg(image)]
        if reference:
            try:
                with Image.open(self.path(reference)) as ref:
                    images.append(_jpeg(ref))
            except (NotFound, OSError):
                reference = None
        prompt = model_prompt(self.spec.get("lead") or {}, shot_prompt, lead_in_frame, bool(reference), kind)
        self.model_calls += 1
        try:
            answer = self.vision(images, prompt)
        except Exception as exc:  # noqa: BLE001 - a model failure is a skipped check, never a failed production
            return {"skipped": f"vision call failed: {str(exc)[:160]}"}
        scores = parse_scores(answer)
        if not reference:
            scores["reference"] = None
        return scores

    # -- item checks
    def check_still(self, stage: str, key: str, asset_id: str, *, shot_prompt: str, lead_in_frame: bool,
                    reference: Optional[str], photocard: bool = False) -> dict[str, Any]:
        th = self.thresholds(stage)
        item = _item(stage, key, asset_id)
        try:
            with Image.open(self.path(asset_id)) as img:
                img.load()
                image = img.convert("RGB")
        except (NotFound, OSError) as exc:
            item.update(verdict="fail", reasons=[f"unreadable: {exc}"], codes=["missing"])
            return item
        stats = image_stats(image)
        item["checks"]["image"] = stats
        if stats["std"] < th["flat_std"]:
            _fail(item, "flat", f"flat picture (spread {stats['std']} < {th['flat_std']})")
        elif stats["noise_ratio"] > th["noise_ratio"]:
            _fail(item, "noisy", f"looks like noise (neighbour/spread {stats['noise_ratio']} > {th['noise_ratio']})")
        bands = edge_bands(image, delta=th["band_delta"], share=th["band_rows"])
        if bands:
            item["checks"]["bands"] = bands
            _fail(item, "edge_band", f"{bands[0]['side']} edge band, rgb {bands[0]['rgb']}")
        if photocard:
            room = headroom(image)
            item["checks"]["headroom"] = room
            if room.get("subject") and room["headroom"] is not None and room["headroom"] < th["headroom_min"]:
                _fail(item, "head_cropped", f"the subject touches the top edge (headroom {room['headroom']:.0%})")
        self._apply_model(item, image, reference, shot_prompt, lead_in_frame, "still", th)
        return _finish(item)

    def check_clip(self, key: str, asset_id: str) -> dict[str, Any]:
        th = self.thresholds("clips")
        item = _item("clips", key, asset_id)
        shot = self.shot(key)
        try:
            path = self.path(asset_id)
            frames = video_gray_frames(path)
        except (NotFound, QAError) as exc:
            item.update(verdict="fail", reasons=[str(exc)], codes=["missing"])
            return item
        motion = clip_motion(frames)
        item["checks"]["motion"] = motion
        fps = 24.0
        if motion["max_jump"] > th["exposure_jump"]:
            _fail(item, "exposure_jump", f"exposure jump of {motion['max_jump']} at frame {motion['jump_at']} "
                                         f"(~{motion['jump_at'] / fps:.1f} s)")
        wanted = shot.get("motion", "move")
        if wanted == "still" and motion["energy"] > th["still_motion_max"]:
            _fail(item, "moves_when_still", f"moves too much for a still shot (energy {motion['energy']} > {th['still_motion_max']})")
        if wanted == "move" and motion["energy"] < th["move_motion_min"]:
            _fail(item, "still_when_moving", f"barely moves (energy {motion['energy']} < {th['move_motion_min']})")
        asset = self.store.get_asset(asset_id)
        duration = asset.get("duration_s") or audio_mod.probe_duration_s(path)
        if duration:
            item["checks"]["duration_s"] = round(float(duration), 2)
        middle = video_frame(path, (duration or 2.0) / 2)
        if middle is not None:
            bands = edge_bands(middle, delta=th["band_delta"], share=th["band_rows"])
            if bands:
                item["checks"]["bands"] = bands
                _fail(item, "edge_band", f"{bands[0]['side']} edge band, rgb {bands[0]['rgb']}")
            source = ((asset.get("recipe") or {}).get("input_asset_ids") or [None])[0]
            self._apply_model(item, middle, source, f"{shot.get('prompt', '')} - motion: {shot.get('motion_prompt', '')}",
                              bool(shot.get("lead")), "clip", th)
        return _finish(item)

    def check_render(self, stage: str, key: str, asset_id: str, expected_s: Optional[float]) -> dict[str, Any]:
        th = self.thresholds(stage)
        item = _item(stage, key, asset_id)
        try:
            path = self.path(asset_id)
        except NotFound as exc:
            item.update(verdict="fail", reasons=[str(exc)], codes=["missing"])
            return item
        duration = audio_mod.probe_duration_s(path)
        item["checks"]["duration_s"] = duration
        _duration(item, duration, expected_s, th)
        for frac in (0.25, 0.75):
            frame = video_frame(path, (duration or 1.0) * frac)
            if frame is None:
                continue
            bands = edge_bands(frame, delta=th["band_delta"], share=th["band_rows"])
            if bands:
                item["checks"]["bands"] = bands
                _fail(item, "edge_band", f"{bands[0]['side']} edge band at {frac:.0%}, rgb {bands[0]['rgb']}")
                break
        return _finish(item)

    def _apply_model(self, item: dict[str, Any], image: Image.Image, reference: Optional[str], shot_prompt: str,
                     lead_in_frame: bool, kind: str, th: dict[str, float]) -> None:
        model = self.model_check(image, reference, shot_prompt, lead_in_frame, kind)
        item["model"] = model
        if model.get("skipped"):
            return
        scores = [v for k, v in model.items() if k in ("bible", "prompt", "reference") and isinstance(v, (int, float))]
        if scores:
            item["score"] = round(min(scores), 1)
            if item["score"] < th["model_min"]:
                low = [k for k in ("bible", "prompt", "reference") if isinstance(model.get(k), (int, float)) and model[k] < th["model_min"]]
                _fail(item, "low_score", f"scored {item['score']}/10 on {', '.join(low)}: {model.get('reason', '')}")

    # -- stages
    def stage_items(self, stage: str, keys: Optional[set[str]] = None) -> list[dict[str, Any]]:
        done = self.state.get("done") or {}
        lead = self.spec.get("lead") or {}
        canonical = (done.get("character") or {}).get("canonical_asset_id")
        wanted = (lambda k: True) if not keys else (lambda k: k in keys or prod.split_key(k)[0] in keys)
        items: list[dict[str, Any]] = []
        if stage == "character" and canonical:
            items.append(self.check_still("character", "canonical", canonical, shot_prompt=f"{lead.get('name')} reference",
                                          lead_in_frame=True, reference=None))
        elif stage == "frames":
            for key, entry in ((done.get("frames") or {}).get("items") or {}).items():
                if not wanted(key) or not entry.get("best"):
                    continue
                shot = self.shot(key)
                items.append(self.check_still("frames", key, entry["best"], shot_prompt=shot.get("prompt", ""),
                                              lead_in_frame=bool(shot.get("lead")),
                                              reference=canonical if shot.get("lead") else None))
        elif stage == "clips":
            for key, asset_id in ((done.get("clips") or {}).get("items") or {}).items():
                if wanted(key) and asset_id:
                    items.append(self.check_clip(key, asset_id))
        elif stage == "photocards":
            looks = (self.spec.get("photocards") or {}).get("looks") or []
            for key, asset_id in ((done.get("photocards") or {}).get("items") or {}).items():
                if not wanted(key) or not asset_id:
                    continue
                look = looks[int(key) - 1] if key.isdigit() and 0 < int(key) <= len(looks) else {}
                items.append(self.check_still("photocards", key, asset_id, shot_prompt=look.get("prompt", ""),
                                              lead_in_frame=True, reference=canonical, photocard=look.get("framing") is not False))
        elif stage == "song":
            items += self.check_song()
        elif stage == "lyrics":
            items += self.check_lyrics()
        elif stage == "animatic":
            song_s = self.song_duration()
            for aspect, asset_id in ((done.get("animatic") or {}).get("renders") or {}).items():
                items.append(self.check_render("animatic", aspect, asset_id, song_s))
        elif stage == "timeline":
            song_s = self.song_duration()
            for aspect, info in ((done.get("timeline") or {}).get("timelines") or {}).items():
                for quality, asset_id in (info.get("renders") or {}).items():
                    items.append(self.check_render("timeline", f"{aspect} {quality}", asset_id, song_s))
        return items

    def song_duration(self) -> Optional[float]:
        song_id = (self.state.get("done", {}).get("song") or {}).get("song_asset_id")
        if not song_id:
            return None
        try:
            return self.store.get_asset(song_id).get("duration_s")
        except NotFound:
            return None

    def check_song(self) -> list[dict[str, Any]]:
        song_id = (self.state.get("done", {}).get("song") or {}).get("song_asset_id")
        if not song_id:
            return []
        item = _item("song", "song", song_id)
        planned = (self.spec.get("song") or {}).get("duration")
        actual = self.song_duration()
        item["checks"]["duration_s"] = actual
        if (self.spec.get("song") or {}).get("asset_id"):
            planned = None  # a reused song has no plan to compare with
        _duration(item, actual, float(planned) if planned else None, self.thresholds("song"))
        return [_finish(item)]

    def check_lyrics(self) -> list[dict[str, Any]]:
        entry = self.state.get("done", {}).get("lyrics") or {}
        asset_id = entry.get("lyrics_asset_id")
        if not asset_id:
            return []
        item = _item("lyrics", "lyrics", asset_id)
        if entry.get("source") != "imported":
            item.update(verdict="skip", reasons=["estimated timing (no vocal alignment to measure)"])
            return [item]
        from . import engine

        try:
            lyrics = engine.read_lyrics(self.store, asset_id)
        except Exception as exc:  # noqa: BLE001
            item.update(verdict="fail", reasons=[f"unreadable lyrics: {exc}"], codes=["missing"])
            return [item]
        cov = lyric_coverage(str((self.spec.get("song") or {}).get("lyrics") or ""), lyrics["lines"])
        item["checks"]["coverage"] = cov
        th = self.thresholds("lyrics")
        if cov["coverage"] is not None and cov["coverage"] < th["lyric_coverage"]:
            _fail(item, "lyric_coverage", f"only {cov['matched']}/{cov['lines']} written lines are aligned")
        return [_finish(item)]


def _item(stage: str, key: str, asset_id: Optional[str]) -> dict[str, Any]:
    return {"stage": stage, "key": key, "asset_id": asset_id, "verdict": "pass", "score": None, "reasons": [], "codes": [],
            "checks": {}, "model": None}


def _fail(item: dict[str, Any], code: str, reason: str) -> None:
    item["codes"].append(code)
    item["reasons"].append(reason)


def _finish(item: dict[str, Any]) -> dict[str, Any]:
    if item["codes"]:
        item["verdict"] = "fail"
    return item


def _duration(item: dict[str, Any], actual: Optional[float], expected: Optional[float], th: dict[str, float]) -> None:
    if not actual or not expected:
        return
    diff = abs(float(actual) - float(expected))
    if diff > max(1.0, th["duration_tolerance"] * float(expected)):
        _fail(item, "duration", f"{float(actual):.1f} s instead of the planned {float(expected):.1f} s")


def scorecard(slug: str, stage: str, items: list[dict[str, Any]], vision_name: str, dry_run: bool) -> dict[str, Any]:
    return {"production": slug, "stage": stage, "at": now_iso(), "dry_run": dry_run, "vision": vision_name,
            "passed": sum(1 for i in items if i["verdict"] == "pass"),
            "failed": sum(1 for i in items if i["verdict"] == "fail"),
            "skipped": sum(1 for i in items if i["verdict"] == "skip"), "items": items}


def compact_scorecard(card: dict[str, Any], limit: int = 40) -> dict[str, Any]:
    """Failures first, one line each; passes as a count (an agent's view)."""
    items = sorted(card.get("items") or [], key=lambda i: {"fail": 0, "skip": 1, "pass": 2}[i["verdict"]])
    out = []
    for it in items[:limit]:
        row = {"stage": it["stage"], "key": it["key"], "asset_id": it.get("asset_id"), "verdict": it["verdict"]}
        if it.get("score") is not None:
            row["score"] = it["score"]
        if it.get("reasons"):
            row["why"] = "; ".join(it["reasons"])[:300]
        model = it.get("model") or {}
        if model.get("reason") and it["verdict"] != "fail":
            row["model"] = model["reason"][:160]
        elif model.get("skipped") and it["verdict"] == "fail":
            row["model"] = model["skipped"]
        out.append(row)
    return {k: v for k, v in card.items() if k != "items"} | {"items": out, "items_total": len(card.get("items") or [])}


# ----------------------------------------------------------------- fixes

def fix_for(stage: str, codes: list[str], shot: dict[str, Any], attempt: int) -> dict[str, Any]:
    """The change a retry makes: always a new seed, plus a targeted fix for
    a known failure (the lessons of the real run in docs/examples)."""
    fix: dict[str, Any] = {}
    if stage == "frames":
        fix["seed"] = int(shot.get("seed", 3000)) + 7919 * attempt
        if "noisy" in codes and shot.get("lead"):
            fix["strength"] = 1.0  # an edit sampled from an empty latent below denoise 1 renders as texture
        if "noisy" in codes and not shot.get("lead"):
            fix["steps"] = int(shot.get("steps") or 20) + 10
    elif stage == "clips":
        fix["clip_seed"] = int(shot.get("clip_seed", 5000)) + 7919 * attempt
        if "exposure_jump" in codes:
            fix["clip_sampler"] = "euler" if shot.get("clip_sampler") != "euler" else "dpmpp_2m"
            fix["clip_cfg"] = max(3.0, float(shot.get("clip_cfg") or 5.0) - 1.0)
        if "moves_when_still" in codes:
            fix["clip_negative"] = prod.STILL_NEGATIVE + ", camera shake, fast motion"
            if "stays perfectly still" not in str(shot.get("motion_prompt") or ""):
                fix["motion_prompt"] = f"{shot.get('motion_prompt') or 'subtle motion'}; the figure stays perfectly still"
        if "still_when_moving" in codes:
            fix["clip_negative"] = None
            fix["motion_prompt"] = f"{shot.get('motion_prompt') or 'motion'}, clearly visible motion"
    elif stage == "photocards":
        fix["seed"] = int(shot.get("seed", 4000)) + 7919 * attempt
        if "head_cropped" in codes:
            fix["framing"] = None  # use the headroom framing again
    return fix


def _apply(target: dict[str, Any], fix: dict[str, Any]) -> None:
    for k, v in fix.items():
        if v is None:
            target.pop(k, None)
        else:
            target[k] = v


def _retry_targets(run: "prod.Run", stage: str, failures: list[dict[str, Any]], attempt: int) -> list[str]:
    """Apply the fix of each failure to the spec and drop the failed item so
    the stage makes it again. Returns the keys retried."""
    done = run.state["done"].get(stage) or {}
    items = done.get("items") or {}
    retried = []
    for failure in failures:
        key = failure["key"]
        if stage == "photocards":
            looks = (run.spec.get("photocards") or {}).get("looks") or []
            if not key.isdigit() or not 0 < int(key) <= len(looks):
                continue
            target = looks[int(key) - 1]
            fix = fix_for(stage, failure["codes"], target, attempt)
        else:
            base, _ = prod.split_key(key)
            target = next((s for s in run.spec.get("shots") or [] if s["key"] == base), None)
            if target is None:
                continue
            fix = fix_for(stage, failure["codes"], target, attempt)
        _apply(target, fix)
        items.pop(key, None)
        if stage == "frames":
            # a new still means new clips from it
            for ck in [k for k in ((run.state["done"].get("clips") or {}).get("items") or {}) if prod.split_key(k)[0] == key]:
                run.state["done"]["clips"]["items"].pop(ck, None)
        done["complete"] = False
        run.state.setdefault("done", {})[stage] = done
        prod.log(run.state, stage, "qa_retry", key=key, attempt=attempt, reason="; ".join(failure["reasons"])[:300],
                 fix={k: v for k, v in fix.items() if k not in ("clip_negative",)} | ({"clip_negative": "stillness"} if fix.get("clip_negative") else {}))
        retried.append(key)
    return retried


def run_stage_with_policy(run: "prod.Run", stage: str, qa: QA, dry_run: bool = False,
                          keys: Optional[set[str]] = None) -> dict[str, Any]:
    """Check a stage; unless `dry_run`, regenerate what fails (up to the
    retry cap) through the stage's own function. Returns the scorecard of
    the final state."""
    settings = (run.state.get("settings") or {}).get("qa") or {}
    max_retries = int(settings.get("max_retries", 2))
    qa.state = run.state
    items = qa.stage_items(stage, keys)
    if dry_run or stage not in RETRYABLE_STAGES:
        return scorecard(run.slug, stage, items, qa.vision_name, dry_run)
    final = {i["key"]: i for i in items}
    failures = [i for i in items if i["verdict"] == "fail" and "missing" not in i["codes"]]
    if stage == "frames":
        failures = _swap_variants(run, qa, failures, final)
    attempt = 0
    while failures and attempt < max_retries:
        attempt += 1
        retried = _retry_targets(run, stage, failures, attempt)
        if not retried:
            break
        run.save()
        previous_stage = run.stage
        run.stage = stage
        getattr(run, f"stage_{stage}")()
        run.stage = previous_stage
        run.save()
        qa.state = run.state
        rechecked = qa.stage_items(stage, set(retried))
        for item in rechecked:
            item["retries"] = attempt
            final[item["key"]] = item
        failures = [i for i in rechecked if i["verdict"] == "fail" and "missing" not in i["codes"]]
    for failure in failures:
        prod.log(run.state, stage, "qa_gave_up", key=failure["key"], reason="; ".join(failure["reasons"])[:300],
                 retries=attempt)
    run.save()
    return scorecard(run.slug, stage, list(final.values()), qa.vision_name, dry_run)


def _swap_variants(run: "prod.Run", qa: QA, failures: list[dict[str, Any]], final: dict[str, Any]) -> list[dict[str, Any]]:
    """Before regenerating a failed still, try its other variants: the first
    one that passes becomes the shot's best (no GPU time)."""
    frames = (run.state["done"].get("frames") or {}).get("items") or {}
    canonical = (run.state["done"].get("character") or {}).get("canonical_asset_id")
    remaining = []
    for failure in failures:
        entry = frames.get(failure["key"]) or {}
        shot = qa.shot(failure["key"])
        swapped = None
        for variant in entry.get("variants") or []:
            if variant == entry.get("best"):
                continue
            item = qa.check_still("frames", failure["key"], variant, shot_prompt=shot.get("prompt", ""),
                                  lead_in_frame=bool(shot.get("lead")), reference=canonical if shot.get("lead") else None)
            if item["verdict"] == "pass":
                swapped = item
                break
        if swapped:
            entry["best"] = swapped["asset_id"]
            clips = (run.state["done"].get("clips") or {}).get("items") or {}
            for ck in [k for k in clips if prod.split_key(k)[0] == failure["key"]]:
                clips.pop(ck, None)  # made from the old still
            swapped["swapped_from"] = failure["asset_id"]
            final[failure["key"]] = swapped
            prod.log(run.state, "frames", "qa_swap_variant", key=failure["key"], asset_id=swapped["asset_id"],
                     reason="; ".join(failure["reasons"])[:300])
        else:
            remaining.append(failure)
    return remaining


def record(state: dict[str, Any], card: dict[str, Any]) -> None:
    qa_state = state.setdefault("qa", {})
    qa_state["last"] = card
    history = qa_state.setdefault("history", [])
    history.append({k: card[k] for k in ("stage", "at", "passed", "failed", "skipped", "dry_run")})
    del history[:-20]


def merge_cards(slug: str, cards: list[dict[str, Any]], vision_name: str, dry_run: bool) -> dict[str, Any]:
    items = [i for c in cards for i in c["items"]]
    stage = cards[0]["stage"] if len(cards) == 1 else "all"
    return scorecard(slug, stage, items, vision_name, dry_run)


def inline_hook(run: "prod.Run", stage: str, vision: Optional[VisionFn], vision_name: Optional[str] = None) -> None:
    """Called by the pipeline after each stage when `settings.qa.enabled`."""
    if stage not in CHECKED_STAGES or run.state.get("kind") == "short":
        return
    qa = QA(run.store, run.state, vision, vision_name)
    card = run_stage_with_policy(run, stage, qa, dry_run=False)
    record(run.state, card)
    run.save()


def run_qa(store: Store, studio: prod.Studio, slug: str, stage: str = "all", dry_run: bool = True,
           keys: Optional[list[str]] = None, vision: Optional[VisionFn] = None, vision_name: Optional[str] = None,
           progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """`studio_qa_run`: a QA pass over one stage or all of them. Without
    `dry_run`, failing stills, clips and photocard photos are regenerated
    (up to the retry cap) and whatever depends on them is invalidated, so
    the next run of the production rebuilds it. Returns the scorecard and
    whether the production needs to run again."""
    if stage != "all" and stage not in CHECKED_STAGES:
        raise QAError("bad_stage", f"stage must be 'all' or one of {', '.join(CHECKED_STAGES)}")
    progress = progress or (lambda *_a, **_k: None)
    raw = prod.load_state(store.data_dir, slug)
    if raw.get("kind") == "short":
        raise QAError("not_for_shorts", "the QA director checks music videos; watch a short's render with studio_show")
    if prod.is_legacy(raw):
        return _run_legacy(store, slug, raw, stage, dry_run, keys, vision, vision_name)
    run = prod.Run(store, studio, slug, progress)
    if run.state.get("status") == "running":
        raise QAError("production_running", "the production is running; QA runs inline with settings.qa.enabled, "
                                            "or after it pauses or finishes")
    qa = QA(store, run.state, vision, vision_name)
    stages = [s for s in CHECKED_STAGES if stage in ("all", s)]
    key_set = {str(k) for k in keys} if keys else None
    cards = []
    changed = False
    for i, st in enumerate(stages):
        if prod.stage_status(run.state, st) == "pending" and st not in ("frames", "clips", "photocards"):
            continue
        progress(i / max(1, len(stages)), f"checking {st}")
        before = json.dumps((run.state["done"].get(st) or {}).get("items") or {}, sort_keys=True)
        cards.append(run_stage_with_policy(run, st, qa, dry_run=dry_run, keys=key_set))
        if not dry_run and json.dumps((run.state["done"].get(st) or {}).get("items") or {}, sort_keys=True) != before:
            changed = True
            _invalidate_after(run.state, st)
    card = merge_cards(slug, cards, qa.vision_name, dry_run) if cards else scorecard(slug, stage, [], qa.vision_name, dry_run)
    record(run.state, card)
    if changed:
        run.state["status"] = "queued"
        run.state["message"] = "QA regenerated outputs; the production runs again to rebuild what depends on them"
    run.save()
    if prod.stage_status(run.state, "report") == "done" or changed:
        prod.write_report(store, run.state)
    return {"scorecard": card, "requeue": changed}


def _invalidate_after(state: dict[str, Any], stage: str) -> None:
    order = list(prod.stages_for(state))
    if stage not in order:
        return
    for later in order[order.index(stage) + 1:]:
        if later in ("clips", "photocards", "frames"):
            if stage == "frames" and later == "clips" and "clips" in state["done"]:
                state["done"]["clips"]["complete"] = False
            continue
        if later in ("song", "lyrics", "character"):
            continue
        state["done"].pop(later, None)
    state.get("partial", {}).pop("timeline", None)


def _run_legacy(store: Store, slug: str, raw: dict[str, Any], stage: str, dry_run: bool, keys: Optional[list[str]],
                vision: Optional[VisionFn], vision_name: Optional[str]) -> dict[str, Any]:
    """A production made by the production script: checked read-only (its
    outputs are the script's to redo), the scorecard saved in its state."""
    if not dry_run:
        raise QAError("legacy_production", "a production made by the production script is checked with dry_run=true; "
                                           "to regenerate outputs, run it from a recipe (studio_recipe_export, studio_recipe_run)")
    view = prod.state_from_legacy(store, raw)
    qa = QA(store, view, vision, vision_name)
    key_set = {str(k) for k in keys} if keys else None
    cards = [scorecard(slug, st, qa.stage_items(st, key_set), qa.vision_name, True)
             for st in CHECKED_STAGES if stage in ("all", st)]
    card = merge_cards(slug, cards, qa.vision_name, True) if cards else scorecard(slug, stage, [], qa.vision_name, True)
    with prod.lock_for(slug):
        current = prod.load_state(store.data_dir, slug)
        record(current, card)
        prod.save_state(store.data_dir, current)
    return {"scorecard": card, "requeue": False}
