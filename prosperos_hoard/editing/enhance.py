"""Video enhancement on a spare GPU for saved clips: noise reduction, upscaling, sharpening, frame-rate interpolation and HDR.

The effects are ffmpeg filters. The GPU ones run on Vulkan (``libplacebo``, ``nlmeans_vulkan``) and the result is encoded with NVENC, both on the one
GPU named by ``LUMIERE_ENHANCE_GPU`` (default 2: the spare card on this PC; the Vulkan and NVENC index). Without Vulkan or NVENC the same effects
run on the CPU with plainer filters and x264. The result is a derived media file next to the source's cache (``<name> (mejorado)``); a clip can be
pointed at it, like a stabilized copy. The source is never touched.

The effect names and the filter chain follow the live enhancement of Baird's Hoard so the two apps describe the same capabilities.
"""

from __future__ import annotations

import copy
import hashlib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import ffmpeg as ff
from . import media as media_store
from . import projects as project_store
from .errors import LumiereError

if TYPE_CHECKING:
    from .jobs import JobCtx
    from .services import Services

DENOISE = ("off", "light", "medium", "strong")
INTERPOLATE = ("off", "blend", "mci")
UPSCALE_FACTORS = (0.0, 1.5, 2.0, 3.0)
MAX_WIDTH = 3840
HANDLE_MS = 500
EFFECT_KEYS = ("denoise", "upscale", "sharpen", "interpolate", "hdr")
OFF: dict[str, Any] = {"denoise": "off", "upscale": 0.0, "sharpen": 0.0, "interpolate": "off", "hdr": False}
DEFAULTS: dict[str, Any] = {**OFF, "interpolate_fps": 0}


def default_gpu() -> int:
    try:
        return min(max(int(os.environ.get("LUMIERE_ENHANCE_GPU", "2")), 0), 7)
    except ValueError:
        return 2


def normalize(raw: Optional[dict[str, Any]], gpu: Optional[int] = None) -> dict[str, Any]:
    """The effects in ``raw`` checked and completed with the defaults; a wrong value raises :class:`LumiereError`."""
    data = copy.deepcopy(DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            raise LumiereError(f"Unknown enhancement setting: {key}", code="bad_enhance")
        if value is not None:
            data[key] = value
    if data["denoise"] not in DENOISE:
        raise LumiereError(f"denoise must be one of {', '.join(DENOISE)}.", code="bad_enhance")
    if data["interpolate"] not in INTERPOLATE:
        raise LumiereError(f"interpolate must be one of {', '.join(INTERPOLATE)}.", code="bad_enhance")
    try:
        factor = float(data["upscale"])
        data["sharpen"] = round(min(max(float(data["sharpen"]), 0.0), 1.0), 2)
        data["interpolate_fps"] = int(min(max(int(data["interpolate_fps"]), 0), 120))
    except (TypeError, ValueError) as exc:
        raise LumiereError("upscale, sharpen and interpolate_fps must be numbers.", code="bad_enhance") from exc
    if factor not in UPSCALE_FACTORS:
        raise LumiereError("upscale must be 0 (off), 1.5, 2 or 3.", code="bad_enhance")
    data["upscale"] = factor
    data["hdr"] = data["hdr"] is True
    data["gpu"] = default_gpu() if gpu is None else int(gpu)
    return data


def effects_on(fx: dict[str, Any]) -> list[str]:
    on = []
    if fx.get("denoise", "off") != "off":
        on.append("denoise")
    if float(fx.get("upscale") or 0) > 0:
        on.append("upscale")
    if float(fx.get("sharpen") or 0) > 0:
        on.append("sharpen")
    if fx.get("interpolate", "off") != "off":
        on.append("interpolate")
    if fx.get("hdr"):
        on.append("hdr")
    return on


def only(fx: dict[str, Any], effect: str) -> dict[str, Any]:
    out = copy.deepcopy(fx)
    for key in EFFECT_KEYS:
        if key != effect:
            out[key] = OFF[key]
    return out


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


SHARPEN_SHADER = """//!HOOK OUTPUT
//!BIND HOOKED
//!DESC sharpen
vec4 hook() {{
    vec4 c = HOOKED_texOff(vec2(0.0, 0.0));
    vec4 n = HOOKED_texOff(vec2(0.0, -1.0));
    vec4 s = HOOKED_texOff(vec2(0.0, 1.0));
    vec4 w = HOOKED_texOff(vec2(-1.0, 0.0));
    vec4 e = HOOKED_texOff(vec2(1.0, 0.0));
    return c + {amount:.3f} * (4.0 * c - n - s - w - e);
}}
"""


class Chain:
    def __init__(self) -> None:
        self.hw_args: list[str] = []
        self.vf = ""
        self.out_w = 0
        self.out_h = 0
        self.out_fps = 0.0
        self.pix_fmt = "yuv420p"
        self.applied: list[str] = []
        self.skipped: dict[str, str] = {}
        self.gpu_filters = False
        self.shader: Optional[str] = None
        self.hdr = False

    def describe(self) -> dict[str, Any]:
        return {"filters": self.vf, "width": self.out_w, "height": self.out_h, "fps": round(self.out_fps, 2), "applied": list(self.applied),
                "skipped": dict(self.skipped), "gpu_filters": self.gpu_filters, "hdr": self.hdr}


def build_chain(fx: dict[str, Any], width: int, height: int, fps: float, caps: dict[str, Any]) -> Chain:
    """The ffmpeg video filter chain for ``fx`` on a ``width`` x ``height`` picture at ``fps`` (what is left out is in ``skipped``)."""
    ch = Chain()
    fps = fps or 30.0
    gpu = int(fx.get("gpu", 2))
    filters: dict[str, bool] = caps.get("filters", {})
    vulkan = bool(caps.get("vulkan"))
    pre: list[str] = []          # CPU, first (light noise reduction)
    gpu_denoise: list[str] = []  # GPU, first hop
    post: list[str] = []         # CPU frame-rate interpolation on the small picture
    on_gpu: list[str] = []       # GPU, second hop: upscale, sharpen, colour
    cpu_place: list[str] = []    # the same work on the CPU when Vulkan is missing
    out_w, out_h = width, height

    level = fx.get("denoise", "off")
    if level != "off":
        if vulkan and filters.get("nlmeans_vulkan") and level in ("medium", "strong"):
            gpu_denoise.append("nlmeans_vulkan=s=2:p=5:r=7" if level == "medium" else "nlmeans_vulkan=s=4:p=7:r=11")
        elif filters.get("hqdn3d"):
            pre.append({"light": "hqdn3d=2:1.5:3:2.5", "medium": "hqdn3d=4:3:6:4.5", "strong": "hqdn3d=7:6:9:7"}[level])
        else:
            ch.skipped["denoise"] = "ffmpeg has no denoise filter"
            level = "off"
        if level != "off":
            ch.applied.append("denoise")

    factor = float(fx.get("upscale") or 0)
    strength = float(fx.get("sharpen") or 0)
    hdr = bool(fx.get("hdr"))
    place: list[str] = []
    if factor > 0:
        scale = min(factor, MAX_WIDTH / width)
        if scale > 1.01:
            out_w, out_h = _even(width * scale), _even(height * scale)
            place += [f"w={out_w}", f"h={out_h}", "upscaler=ewa_lanczos"]
            ch.applied.append("upscale")
        else:
            ch.skipped["upscale"] = f"The picture is already {width} px wide (limit {MAX_WIDTH})."
    if strength > 0:
        ch.shader = f"enhance-sharpen-{int(round(strength * 100)):03d}.glsl"
        place.append(f"custom_shader_path={ch.shader}")
        ch.applied.append("sharpen")
    if hdr:
        place += ["colorspace=bt2020nc", "color_primaries=bt2020", "color_trc=smpte2084", "range=tv", "inverse_tonemapping=1", "format=p010le"]
        ch.applied.append("hdr")
        ch.pix_fmt = "p010le"
        ch.hdr = True
    if place:
        if vulkan and filters.get("libplacebo"):
            on_gpu.append("libplacebo=" + ":".join(place))
        else:
            if "upscale" in ch.applied:
                cpu_place.append(f"scale={out_w}:{out_h}:flags=lanczos")
            if strength > 0:
                cpu_place.append(f"unsharp=5:5:{strength * 1.5:.2f}:5:5:0")
            if hdr:
                ch.skipped["hdr"] = "HDR needs Vulkan (libplacebo)."
                ch.applied.remove("hdr")
                ch.hdr = False
                ch.pix_fmt = "yuv420p"

    mode = fx.get("interpolate", "off")
    target = float(fx.get("interpolate_fps") or 0) or min(120.0, fps * 2)
    if mode != "off":
        if target <= fps + 0.5:
            ch.skipped["interpolate"] = f"The target ({target:g} fps) does not exceed the source's {fps:g} fps."
        elif mode == "blend" and filters.get("framerate"):
            post.append(f"framerate=fps={target:g}:scene=0")
            ch.applied.append("interpolate")
            fps = target
        elif mode == "mci" and filters.get("minterpolate"):
            post.append(f"minterpolate=fps={target:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
            ch.applied.append("interpolate")
            fps = target
        else:
            ch.skipped["interpolate"] = "ffmpeg has no filter for that."

    def hop(filters_: list[str], fmt: str) -> list[str]:
        return ["format=yuv420p", "hwupload", *filters_, "hwdownload", f"format={fmt}"]

    parts = list(pre)
    if gpu_denoise or on_gpu:
        ch.gpu_filters = True
        ch.hw_args = ["-init_hw_device", f"vulkan=vk:{gpu}", "-filter_hw_device", "vk"]
    if post:
        if gpu_denoise:
            parts += hop(gpu_denoise, "yuv420p")
        parts += post
        if on_gpu:
            parts += hop(on_gpu, ch.pix_fmt)
    elif gpu_denoise or on_gpu:
        parts += hop(gpu_denoise + on_gpu, ch.pix_fmt)
    parts += cpu_place
    if ch.pix_fmt == "yuv420p" and not (parts and parts[-1].startswith("format=")):
        parts.append("format=yuv420p")
    ch.vf = ",".join(parts)
    ch.out_w, ch.out_h, ch.out_fps = out_w, out_h, fps
    return ch


def ensure_shader(work: Path, chain: Chain) -> None:
    if chain.shader:
        amount = int(chain.shader.split("-")[-1].split(".")[0]) / 100.0 * 0.5
        text = SHARPEN_SHADER.format(amount=amount)
        path = work / chain.shader
        work.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


def encoder_args(caps: dict[str, Any], gpu: int, width: int, height: int, fps: float, *, hdr: bool, benchmark: bool = False) -> tuple[list[str], str]:
    """``(arguments, encoder name)`` for the output: NVENC on ``gpu`` when it works, x264/x265 otherwise."""
    kbps = int(min(60000, max(1500, width * height * fps * 0.09 / 1000)))
    gop = max(1, int(round(fps)) * 2)
    if hdr:
        if caps.get("nvenc_hevc"):
            return (["-c:v", "hevc_nvenc", "-gpu", str(gpu), "-preset", "p5", "-profile:v", "main10", "-rc", "vbr", "-b:v", f"{kbps}k",
                     "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k", "-g", str(gop), "-tag:v", "hvc1",
                     "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc"], "hevc_nvenc")
        return (["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
                 "-x265-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc", "-g", str(gop)], "libx265")
    if caps.get("nvenc"):
        return (["-c:v", "h264_nvenc", "-gpu", str(gpu), "-preset", "p3" if benchmark else "p5", "-rc", "vbr", "-b:v", f"{kbps}k",
                 "-maxrate", f"{int(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k", "-g", str(gop), "-profile:v", "high", "-pix_fmt", "yuv420p"], "h264_nvenc")
    return (["-c:v", "libx264", "-preset", "ultrafast" if benchmark else "medium", "-crf", "18", "-g", str(gop), "-pix_fmt", "yuv420p"], "libx264")


# ---------------------------------------------------------------- the machine

_CAPS: dict[tuple[str, int], dict[str, Any]] = {}


def capabilities(svc: "Services", gpu: int, refresh: bool = False) -> dict[str, Any]:
    """Which filters and encoders exist and whether Vulkan and NVENC work on ``gpu`` (probed once, on that GPU only)."""
    tools = svc.tools()
    key = (tools.ffmpeg, gpu)
    if key in _CAPS and not refresh:
        return copy.deepcopy(_CAPS[key])
    out: dict[str, Any] = {"gpu": gpu, "vulkan": False, "nvenc": False, "nvenc_hevc": False,
                           "filters": {n: tools.has_filter(n) for n in ("libplacebo", "nlmeans_vulkan", "hqdn3d", "framerate", "minterpolate", "unsharp")}}
    tiny = ["-f", "lavfi", "-i", "color=c=black:s=640x360:r=10:d=0.5", "-loglevel", "error"]

    def works(args: list[str]) -> bool:
        try:
            ff.capture(tools, [*args, "-f", "null", "-"], timeout=60)
            return True
        except LumiereError:
            return False

    if out["filters"]["libplacebo"] or out["filters"]["nlmeans_vulkan"]:
        out["vulkan"] = works(["-init_hw_device", f"vulkan=vk:{gpu}", "-filter_hw_device", "vk", *tiny, "-vf", "format=yuv420p,hwupload,hwdownload,format=yuv420p"])
    for name, enc in (("nvenc", "h264_nvenc"), ("nvenc_hevc", "hevc_nvenc")):
        if enc in tools.encoders:
            out[name] = works([*tiny, "-c:v", enc, "-gpu", str(gpu)])
    _CAPS[key] = out
    return copy.deepcopy(out)


def info(svc: "Services") -> dict[str, Any]:
    gpu = default_gpu()
    return {"gpu": gpu, "capabilities": capabilities(svc, gpu), "defaults": normalize({}, gpu),
            "effects": {"denoise": list(DENOISE), "upscale": list(UPSCALE_FACTORS), "sharpen": "0..1", "interpolate": list(INTERPOLATE),
                        "hdr": "saved files, HEVC Main10 PQ BT.2020"}}


# ---------------------------------------------------------------- running it

def _run_chain(svc: "Services", src: Path, dst: Path, fx: dict[str, Any], summary: dict[str, Any], *, start_ms: int = 0, length_ms: int = 0,
               progress=None, handle=None) -> dict[str, Any]:
    tools = svc.tools()
    caps = capabilities(svc, int(fx["gpu"]))
    chain = build_chain(fx, int(summary["width"]), int(summary["height"]), float(summary.get("fps") or 30.0), caps)
    work = svc.config.work_dir / "enhance"
    work.mkdir(parents=True, exist_ok=True)
    ensure_shader(work, chain)
    enc, name = encoder_args(caps, int(fx["gpu"]), chain.out_w, chain.out_h, chain.out_fps, hdr=chain.hdr)
    span = ["-ss", ff.seconds(start_ms), "-t", ff.seconds(length_ms)] if length_ms else []
    args = [*chain.hw_args, *span, "-i", str(src), "-map", "0:v:0", "-map", "0:a?"]
    if chain.vf:
        args += ["-vf", chain.vf]
    args += [*enc, "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(dst)]
    started = time.time()
    ff.run(tools, args, duration_ms=length_ms or int(summary.get("duration_ms") or 0), progress=progress, handle=handle, cwd=work)
    return {"encoder": name, "seconds": round(time.time() - started, 2), **chain.describe()}


def _key(media: str, a: int, b: int, fx: dict[str, Any]) -> str:
    sig = "|".join(f"{k}={fx[k]}" for k in sorted(fx))
    return hashlib.sha1(f"{media}:{a}:{b}:{sig}".encode()).hexdigest()[:10]


def _derive(svc: "Services", ctx: "JobCtx", media_id: str, fx: dict[str, Any], a: int, b: int) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Enhance ``a..b`` ms of a media file (all of it when ``b`` is 0) into the media's cache and import it; returns (media, result, file)."""
    info_ = media_store.get(svc, media_id)
    if info_["kind"] != "video":
        raise LumiereError("Only videos can be enhanced.", code="not_video")
    if not effects_on(fx):
        raise LumiereError("Choose at least one effect: denoise, upscale, sharpen, interpolate or hdr.", code="no_effect")
    cdir = media_store.cache_dir(svc, media_id)
    out = cdir / f"enh_{_key(media_id, a, b, fx)}.mp4"
    result: dict[str, Any] = {}
    if not out.exists():
        tmp = cdir / f"enh_{out.stem}.tmp.mp4"
        ctx.progress(0.02, "enhancing", force=True)
        summary = {"width": info_["width"], "height": info_["height"], "fps": info_["fps"], "duration_ms": info_["duration_ms"]}
        result = _run_chain(svc, Path(info_["path"]), tmp, fx, summary, start_ms=a, length_ms=(b - a) if b else 0,
                            progress=lambda x: ctx.progress(0.02 + 0.96 * x, "enhancing"), handle=ctx.handle())
        os.replace(tmp, out)
    derived = media_store.import_path(svc, str(out), origin="derived", name=f"{info_['name']} (mejorado)")
    return derived, result, str(out)


def media_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    fx = normalize(ctx.params.get("effects"), ctx.params.get("gpu"))
    derived, result, path = _derive(svc, ctx, ctx.params["media"], fx, 0, 0)
    return {"media": derived["id"], "file": path, **result}


def clip_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    pid, clip_id = ctx.params["project"], ctx.params["clip"]
    fx = normalize(ctx.params.get("effects"), ctx.params.get("gpu"))
    p = project_store.doc(svc, pid)
    _, c = p.find(clip_id)
    if c.type != "media":
        raise LumiereError("Only video clips can be enhanced.", code="not_video")
    info_ = media_store.get(svc, c.media)
    a = max(0, c.src_in - HANDLE_MS)
    b = min(info_["duration_ms"] or c.src_out + HANDLE_MS, c.src_out + HANDLE_MS)
    derived, result, path = _derive(svc, ctx, c.media, fx, a, b)
    project_store.edit(svc, pid, [{"op": "replace_media", "clip": clip_id, "media": derived["id"]},
                                  {"op": "trim", "clip": clip_id, "src_in": c.src_in - a, "src_out": c.src_out - a, "ripple": False}],
                       label="Mejorar vídeo", actor="tool")
    return {"clip": clip_id, "media": derived["id"], "file": path, **result}


# ---------------------------------------------------------------- cost

def benchmark(svc: "Services", media_id: Optional[str], effects: Optional[dict[str, Any]], frames: int = 150, gpu: Optional[int] = None) -> dict[str, Any]:
    """Milliseconds per frame of the baseline (decode and encode) and of each effect, over a sample.

    Every run is timed over a third of the frames and over all of them: the slope is the steady cost per frame, what is left is the start-up
    (Vulkan, shader compilation, the encoder). ``added_ms`` is the cost over the baseline. The sample is the named media, or a synthetic noisy
    720p video when none is named."""
    fx = normalize(effects, gpu)
    if not effects_on(fx):
        fx = normalize({**effects_defaults_for_benchmark(), **(effects or {})}, gpu)
    tools = svc.tools()
    caps = capabilities(svc, int(fx["gpu"]))
    work = svc.config.work_dir / "enhance"
    work.mkdir(parents=True, exist_ok=True)
    temp: Optional[Path] = None
    if media_id:
        row = media_store.get(svc, media_id)
        if row["kind"] != "video":
            raise LumiereError("Only videos can be measured.", code="not_video")
        sample = Path(row["path"])
        summary = {"width": row["width"], "height": row["height"], "fps": row["fps"], "duration_ms": row["duration_ms"]}
    else:
        temp = sample = work / f"bench-{int(time.time() * 1000)}.mp4"
        ff.capture(tools, ["-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={max(4, frames // 30 + 1)},noise=alls=14:allf=t",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(sample)], timeout=120)
        summary = {"width": 1280, "height": 720, "fps": 30.0, "duration_ms": max(4, frames // 30 + 1) * 1000}
    fps = float(summary["fps"] or 30.0)
    frames = min(frames, max(30, int(fps * summary["duration_ms"] / 1000) - 2))
    short = max(10, frames // 3)

    def timed(chain: Chain, enc: list[str], count: int) -> float:
        args = [*chain.hw_args, "-t", f"{count / fps:.4f}", "-i", str(sample), "-map", "0:v:0"]
        if chain.vf:
            args += ["-vf", chain.vf]
        args += [*enc, "-f", "null", "-"]
        best = float("inf")
        for _ in range(2):  # the faster of two runs: the other is whatever else the machine was doing
            t0 = time.perf_counter()
            ff.run(tools, args, cwd=work)
            best = min(best, time.perf_counter() - t0)
        return best

    def run(label: str, effect_fx: dict[str, Any]) -> dict[str, Any]:
        chain = build_chain(effect_fx, int(summary["width"]), int(summary["height"]), fps, caps)
        ensure_shader(work, chain)
        enc, name = encoder_args(caps, int(fx["gpu"]), chain.out_w, chain.out_h, chain.out_fps, hdr=chain.hdr, benchmark=True)
        try:
            t_short, t_long = timed(chain, enc, short), timed(chain, enc, frames)
        except LumiereError as exc:
            return {"effect": label, "ok": False, "ms_per_frame": None, "startup_s": None, "applied": chain.applied, "skipped": chain.skipped,
                    "encoder": name, "error": str(exc)[-200:]}
        per = (t_long - t_short) * 1000 / (frames - short)
        if per <= 0:
            per = None  # the longer run was not slower: too noisy to say
        return {"effect": label, "ok": per is not None, "ms_per_frame": round(per, 2) if per is not None else None,
                "startup_s": round(max(0.0, t_short - short * per / 1000), 2) if per is not None else None,
                "applied": chain.applied, "skipped": chain.skipped, "encoder": name, "error": ""}

    try:
        base_fx = {**fx, **OFF}
        baseline = run("baseline", base_fx)
        rows = []
        for effect in effects_on(fx):
            row = run(effect, only(fx, effect))
            row["added_ms"] = round(row["ms_per_frame"] - (baseline["ms_per_frame"] or 0.0), 2) if row["ok"] else None
            rows.append(row)
        combined = run("all", fx) if len(effects_on(fx)) > 1 else (dict(rows[0], effect="all") if rows else None)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    budget = 1000.0 / fps
    return {"frames": frames, "input": {"width": summary["width"], "height": summary["height"], "fps": fps}, "frame_budget_ms": round(budget, 2),
            "baseline": baseline, "effects": rows, "all": combined, "gpu": int(fx["gpu"]),
            "realtime": bool(combined and combined["ok"] and combined["ms_per_frame"] <= budget)}


def effects_defaults_for_benchmark() -> dict[str, Any]:
    return {"denoise": "medium", "upscale": 2.0, "sharpen": 0.5, "interpolate": "blend"}


def benchmark_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    ctx.progress(0.05, "measuring", force=True)
    return benchmark(svc, ctx.params.get("media"), ctx.params.get("effects"), int(ctx.params.get("frames") or 150), ctx.params.get("gpu"))
