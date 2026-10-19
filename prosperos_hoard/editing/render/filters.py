"""Clip effects: the supported list, their parameters and the ffmpeg filters they become. Anything not in this table is
refused when it is added, so an agent cannot pass raw filter strings into the render."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

from ..errors import LumiereError

Params = dict[str, Any]

# name -> {param: (default, low, high)}; a None range means a string option checked by its own rule
SPECS: dict[str, dict[str, tuple[Any, Any, Any]]] = {
    "eq": {"brightness": (0.0, -1.0, 1.0), "contrast": (1.0, 0.0, 3.0), "saturation": (1.0, 0.0, 3.0), "gamma": (1.0, 0.1, 5.0)},
    "lut": {"file": ("", None, None)},
    "grayscale": {},
    "sepia": {},
    "vignette": {"strength": (0.5, 0.0, 1.0)},
    "blur": {"radius": (6.0, 0.5, 60.0)},
    "sharpen": {"amount": (1.0, 0.0, 3.0)},
    "denoise": {"strength": (4.0, 0.0, 20.0)},
    "hflip": {},
    "vflip": {},
    "chromakey": {"color": ("#00FF00", None, None), "similarity": (0.12, 0.01, 0.6), "blend": (0.05, 0.0, 0.5)},
    "vintage": {},
    "warm": {"amount": (0.5, 0.0, 1.0)},
    "cool": {"amount": (0.5, 0.0, 1.0)},
    "contrast_pop": {"amount": (0.5, 0.0, 1.0)},
    "pixelate": {"size": (16, 2, 128)},
    # audio
    "audio_denoise": {"strength": (12.0, 1.0, 40.0)},
    "voice_enhance": {},
    "highpass": {"hz": (80.0, 20.0, 2000.0)},
    "lowpass": {"hz": (8000.0, 500.0, 20000.0)},
    "compressor": {"threshold_db": (-18.0, -60.0, 0.0), "ratio": (3.0, 1.0, 20.0)},
    "pitch": {"semitones": (0.0, -12.0, 12.0)},
    "echo": {"delay_ms": (250.0, 20.0, 2000.0), "decay": (0.35, 0.0, 0.9)},
}

AUDIO = {"audio_denoise", "voice_enhance", "highpass", "lowpass", "compressor", "pitch", "echo"}


def _color(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) == 7 and text.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in text[1:]):
        return text.upper()
    raise LumiereError(f"Colour {value!r} must be #RRGGBB.")


def check_params(kind: str, params: Params | None) -> Params:
    spec = SPECS.get(kind)
    if spec is None:
        raise LumiereError(f"Unknown effect {kind!r}. Known: {', '.join(SPECS)}.")
    params = dict(params or {})
    unknown = set(params) - set(spec)
    if unknown:
        raise LumiereError(f"{kind} takes {', '.join(spec) or 'no parameters'}; not {', '.join(sorted(unknown))}.")
    out: Params = {}
    for name, (default, low, high) in spec.items():
        value = params.get(name, default)
        if low is None:
            if name == "color":
                value = _color(value)
            elif name == "file":
                value = str(value or "")
                if kind == "lut" and not value.lower().endswith((".cube", ".3dl")):
                    raise LumiereError("A LUT is a .cube or .3dl file.")
            out[name] = value
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise LumiereError(f"{kind}.{name} must be a number.") from error
        if not low <= number <= high:
            raise LumiereError(f"{kind}.{name} must be between {low} and {high}.")
        out[name] = int(number) if isinstance(default, int) and not isinstance(default, bool) else number
    return out


def _hex_to_ffmpeg(color: str) -> str:
    return "0x" + color.lstrip("#")


VideoBuilder = Callable[[Params, dict[str, Any]], list[str]]


EQ_KEYED = ("brightness", "saturation")


def _v_eq(p: Params, ctx: dict) -> list[str]:
    """The eq effect. ``ctx['eq_keys']`` ({prop: ffmpeg expression of the clip's time}) is set only for the eq the keyframes
    drive: those props become per-frame expressions (eval=frame); every other parameter stays as the clip has it."""
    keys = ctx.get("eq_keys") or {}
    value = {name: f"'{keys[name]}'" if name in keys else f"{p[name]:.3f}" for name in EQ_KEYED}
    text = f"eq=brightness={value['brightness']}:contrast={p['contrast']:.3f}:saturation={value['saturation']}:gamma={p['gamma']:.3f}"
    return [text + ":eval=frame" if keys else text]


def _v_lut(p: Params, ctx: dict) -> list[str]:
    name = ctx["lut_name"](p["file"])  # copied into the work folder under a plain name
    return [f"lut3d=file={name}"] if name else []


def _v_warm(p: Params, _: dict) -> list[str]:
    a = p["amount"]
    return [f"colorbalance=rs={0.12 * a:.3f}:gs={0.03 * a:.3f}:bs={-0.12 * a:.3f}:rm={0.08 * a:.3f}:bm={-0.08 * a:.3f}"]


def _v_cool(p: Params, _: dict) -> list[str]:
    a = p["amount"]
    return [f"colorbalance=rs={-0.10 * a:.3f}:bs={0.12 * a:.3f}:rm={-0.06 * a:.3f}:bm={0.08 * a:.3f}"]


def _v_pop(p: Params, _: dict) -> list[str]:
    a = p["amount"]
    return [f"eq=contrast={1 + 0.25 * a:.3f}:saturation={1 + 0.35 * a:.3f}", f"unsharp=5:5:{0.6 * a:.2f}:5:5:0"]


def _v_pixelate(p: Params, ctx: dict) -> list[str]:
    s = max(1, int(round(p["size"] * ctx.get("factor", 1.0))))  # sized in canvas pixels: a smaller output scales the block down
    return [f"scale=iw/{s}:ih/{s}:flags=neighbor", f"scale=iw*{s}:ih*{s}:flags=neighbor"]


VIDEO: dict[str, VideoBuilder] = {
    "eq": _v_eq,
    "lut": _v_lut,
    "grayscale": lambda p, c: ["hue=s=0"],
    "sepia": lambda p, c: ["colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"],
    "vignette": lambda p, c: [f"vignette=angle={0.2 + 0.6 * p['strength']:.3f}"],
    "blur": lambda p, c: [f"gblur=sigma={p['radius'] * c.get('factor', 1.0):.2f}"],  # radius in canvas pixels, like pixelate's size
    "sharpen": lambda p, c: [f"unsharp=5:5:{p['amount']:.2f}:5:5:0"],
    "denoise": lambda p, c: [f"hqdn3d={p['strength']:.1f}"],
    "hflip": lambda p, c: ["hflip"],
    "vflip": lambda p, c: ["vflip"],
    "chromakey": lambda p, c: [f"colorkey={_hex_to_ffmpeg(p['color'])}:{p['similarity']:.3f}:{p['blend']:.3f}"],
    "vintage": lambda p, c: ["curves=preset=vintage", "noise=alls=6:allf=t"],
    "warm": _v_warm,
    "cool": _v_cool,
    "contrast_pop": _v_pop,
    "pixelate": _v_pixelate,
}


def audio_chain(kind: str, p: Params) -> list[str]:
    if kind == "audio_denoise":
        return [f"afftdn=nr={p['strength']:.1f}:nf=-40"]
    if kind == "voice_enhance":
        return ["highpass=f=80", "lowpass=f=12000", "equalizer=f=3000:t=q:w=1.2:g=3",
                "acompressor=threshold=-20dB:ratio=3:attack=8:release=120:makeup=3"]
    if kind == "highpass":
        return [f"highpass=f={p['hz']:.0f}"]
    if kind == "lowpass":
        return [f"lowpass=f={p['hz']:.0f}"]
    if kind == "compressor":
        return [f"acompressor=threshold={p['threshold_db']:.1f}dB:ratio={p['ratio']:.2f}:attack=10:release=150"]
    if kind == "pitch":
        if abs(p["semitones"]) < 0.01:
            return []
        factor = 2 ** (p["semitones"] / 12)
        return [f"asetrate=48000*{factor:.6f}", "aresample=48000", *atempo_chain(1 / factor)]
    if kind == "echo":
        return [f"aecho=0.8:0.85:{p['delay_ms']:.0f}:{p['decay']:.2f}"]
    return []


def atempo_chain(factor: float) -> list[str]:
    """atempo only takes 0.5..2 per instance (100 in new builds, but 2 keeps the quality): chain them."""
    out: list[str] = []
    if abs(factor - 1) < 1e-4:
        return out
    while factor > 2.0:
        out.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        out.append("atempo=0.5")
        factor /= 0.5
    out.append(f"atempo={factor:.6f}")
    return out


def video_chain(filters: list[Any], ctx: dict[str, Any]) -> list[str]:
    """ffmpeg filters for the enabled video effects of one clip, in order. With ``ctx['eq_keys']`` (see _v_eq) the first
    enabled eq is the animated one; a clip with keys and no eq at all gets a default eq in front of its effects."""
    chain: list[str] = []
    active = [f for f in filters if f.enabled and f.type not in AUDIO]
    keys = ctx.get("eq_keys") or {}
    target = next((i for i, f in enumerate(active) if f.type == "eq"), -1)
    if keys and target < 0:
        active.insert(0, SimpleNamespace(type="eq", params={}, enabled=True))
        target = 0
    for i, f in enumerate(active):
        ctx["n"] = f"{ctx.get('uid', 'x')}{i}"
        ctx["eq_keys"] = keys if i == target else None
        chain += VIDEO[f.type](check_params(f.type, f.params), ctx)
    ctx["eq_keys"] = keys
    return chain


def audio_filters(filters: list[Any]) -> list[str]:
    chain: list[str] = []
    for f in filters:
        if f.enabled and f.type in AUDIO:
            chain += audio_chain(f.type, check_params(f.type, f.params))
    return chain
