"""A fake ComfyUI server for the cloud demo and for tests.

Speaks the small subset of the real ComfyUI HTTP API that
`prosperos_hoard.comfy_driver` and `hoard_link.ComfyClient` use:
`/system_stats`, `/object_info[/{node}]`, `/upload/image`, `/prompt`,
`/history[/{id}]`, `/view`, `/free`, `/interrupt`. Instead of running any
model it "renders" a small deterministic procedural image, a short
procedural animated WEBP for SVD and Wan, or a short synthesised song with
a real beat for ACE-Step, from the prompt/tags/lyrics + seed, with Pillow
and numpy. Same seed + same prompt -> byte-identical output, which is what
the lineage-reproduces-the-same-asset test relies on.

`/object_info` is a real ComfyUI 0.37.0 dump (962 node classes,
`comfy_object_info.json`, taken live from an install with the Flux,
Kontext, Wan and ACE-Step files already on disk), with the model file
names merged in once more so the fixture keeps working if a future dump
is taken on a machine without them -- the built-in templates validate and
queue against this fake exactly as they would against the real thing.
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont

from ..workflows.convert import validate_values

CHECKPOINTS_SDXL = ["sd_xl_base_1.0.safetensors"]
CHECKPOINTS_SD15 = ["v1-5-pruned-emaonly-fp16.safetensors"]
CHECKPOINTS_SVD = ["svd_xt.safetensors"]
CHECKPOINTS_FLUX = ["flux1-schnell-fp8.safetensors"]
CHECKPOINTS_ACE = ["ace_step_1.5_turbo_aio.safetensors"]
UNET_FILES = ["flux1-dev-kontext_fp8_scaled.safetensors", "wan2.2_ti2v_5B_fp16.safetensors"]
CLIP_FILES = ["umt5_xxl_fp8_e4m3fn_scaled.safetensors"]
DUAL_CLIP_FILES = ["clip_l.safetensors", "t5xxl_fp8_e4m3fn_scaled.safetensors"]
VAE_FILES = ["ae.safetensors", "wan2.2_vae.safetensors"]

_REAL_OBJECT_INFO_PATH = Path(__file__).parent / "comfy_object_info.json"
_real_object_info_cache: Optional[dict[str, Any]] = None


def _set_choices(object_info: dict[str, Any], class_type: str, input_name: str, choices: list[str]) -> None:
    node = object_info.get(class_type)
    if node is None:
        return
    spec = node.get("input", {})
    for bucket in ("required", "optional"):
        entry = (spec.get(bucket) or {}).get(input_name)
        if entry is not None:
            existing = entry[0] if isinstance(entry[0], list) else []
            merged = existing + [c for c in choices if c not in existing]
            entry[0] = merged


def real_object_info() -> dict[str, Any]:
    """A real ComfyUI 0.37.0 `/object_info` dump (962 node classes),
    with the checkpoint/model files of the production example
    merged in (a no-op for this dump, which already lists them). Loaded and patched once (module-level cache, ~1.7 MB): every
    caller in this process only ever reads it, so it is returned by
    reference rather than re-parsed or deep-copied per request."""
    global _real_object_info_cache
    if _real_object_info_cache is None:
        data = json.loads(_REAL_OBJECT_INFO_PATH.read_text(encoding="utf-8"))
        _set_choices(data, "CheckpointLoaderSimple", "ckpt_name",
                     CHECKPOINTS_SDXL + CHECKPOINTS_SD15 + CHECKPOINTS_FLUX + CHECKPOINTS_ACE)
        _set_choices(data, "ImageOnlyCheckpointLoader", "ckpt_name", CHECKPOINTS_SVD)
        _set_choices(data, "UNETLoader", "unet_name", UNET_FILES)
        _set_choices(data, "CLIPLoader", "clip_name", CLIP_FILES)
        _set_choices(data, "DualCLIPLoader", "clip_name1", DUAL_CLIP_FILES)
        _set_choices(data, "DualCLIPLoader", "clip_name2", DUAL_CLIP_FILES)
        _set_choices(data, "VAELoader", "vae_name", VAE_FILES)
        _real_object_info_cache = data
    return _real_object_info_cache

_PALETTE = [
    (255, 77, 141), (245, 194, 107), (108, 92, 231), (0, 184, 148),
    (9, 132, 227), (253, 121, 168), (225, 112, 85), (39, 60, 117),
]

# colour words in a prompt steer the fake picture, so "icy blue twin-tails"
# and "bright red bob" look different in the demo
_COLOUR_WORDS = {
    "platinum": (232, 228, 240), "silver": (200, 204, 216), "white": (240, 240, 245), "blonde": (240, 214, 150),
    "gold": (245, 194, 107), "golden": (245, 194, 107), "yellow": (250, 220, 90), "orange": (255, 150, 60),
    "red": (235, 64, 72), "crimson": (190, 30, 60), "auburn": (165, 72, 42), "copper": (190, 110, 60),
    "pink": (255, 120, 180), "rose": (255, 77, 141), "magenta": (220, 60, 200), "purple": (140, 80, 220),
    "violet": (150, 100, 240), "lavender": (190, 160, 240), "pastel": (240, 190, 230), "blue": (80, 150, 255),
    "icy": (150, 215, 255), "cyan": (60, 220, 240), "teal": (0, 170, 160), "green": (60, 200, 120),
    "black": (30, 26, 36), "dark": (40, 30, 56), "neon": (255, 60, 170), "sunset": (255, 120, 90),
    "night": (30, 30, 80), "dusk": (120, 70, 140), "storm": (70, 80, 120),
}
_PERSON_WORDS = ("idol", "portrait", "girl", "woman", "man", "boy", "person", "singer", "dancer", "rapper",
                 "member", "vocalist", "leader", "face", "headshot", "maknae", "figure")


def _prompt_colours(prompt: str, rng: "random.Random") -> list[tuple[int, int, int]]:
    words = [w.strip(",.;:!?()").lower() for w in prompt.split()]
    found = [_COLOUR_WORDS[w] for w in words if w in _COLOUR_WORDS]
    base = [_PALETTE[rng.randrange(len(_PALETTE))] for _ in range(3)]
    return (found + base)[:4] if found else base


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


def render_fake_image(seed: int, prompt_text: str, width: int = 512, height: int = 512, frame: int = 0,
                      reference: Optional[Image.Image] = None, denoise: float = 1.0) -> Image.Image:
    """Pure, deterministic: same args -> byte-identical pixels. A stage-lit
    procedural scene (gradient, bokeh, light beams, a silhouette when the
    prompt describes a person) in colours taken from the prompt, with a
    small "demo backend" label so nobody mistakes it for a model's output."""
    import numpy as np
    from PIL import ImageFilter

    rng = random.Random(seed)
    colours = _prompt_colours(prompt_text, rng)
    top, bottom = _mix(colours[0], (12, 8, 20), 0.55), _mix(colours[1 % len(colours)], (8, 6, 14), 0.75)
    t = np.linspace(0.0, 1.0, height)[:, None, None]
    grad = (np.array(top)[None, None, :] * (1 - t) + np.array(bottom)[None, None, :] * t)
    arr = np.repeat(grad, width, axis=1).astype(np.uint8)
    img = Image.fromarray(arr, "RGB").convert("RGBA")

    # light beams from the top
    beams = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bd = ImageDraw.Draw(beams)
    for _ in range(3):
        cx = rng.uniform(0.1, 0.9) * width
        spread = rng.uniform(0.12, 0.3) * width
        col = colours[rng.randrange(len(colours))]
        bd.polygon([(cx - 6, 0), (cx + 6, 0), (cx + spread, height), (cx - spread, height)], fill=col + (38,))
    img.alpha_composite(beams.filter(ImageFilter.GaussianBlur(max(2, width // 60))))

    # bokeh (drifts with the frame index for animations)
    bokeh = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    kd = ImageDraw.Draw(bokeh)
    for _ in range(26):
        x = rng.uniform(0, 1) * width + frame * rng.uniform(-6, 6)
        y = rng.uniform(0, 0.75) * height + frame * rng.uniform(-3, 3)
        r = rng.uniform(0.01, 0.06) * min(width, height)
        col = colours[rng.randrange(len(colours))]
        kd.ellipse([x - r, y - r, x + r, y + r], fill=_mix(col, (255, 255, 255), 0.35) + (int(rng.uniform(40, 110)),))
    img.alpha_composite(bokeh.filter(ImageFilter.GaussianBlur(max(1, width // 180))))

    low = prompt_text.lower()
    if any(w in low for w in _PERSON_WORDS) and "cover" not in low:
        fig = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fig)
        unit = min(width, height)
        cx = width * (0.5 + rng.uniform(-0.1, 0.1)) + frame * 1.5
        cy = height * 0.40
        hw, hh = unit * 0.085, unit * 0.108  # head half-width / half-height
        hair = colours[0] + (255,)
        body = (18, 13, 26, 255)
        long_hair = rng.random() < 0.6
        # hair mass behind the head (long hair falls to the shoulders)
        if long_hair:
            fd.rounded_rectangle([cx - hw * 1.45, cy - hh * 1.1, cx + hw * 1.45, cy + hh * 2.1], radius=int(hw * 1.2), fill=hair)
        # shoulders and neck
        fd.rounded_rectangle([cx - unit * 0.24, cy + hh * 1.65, cx + unit * 0.24, height + unit * 0.2],
                             radius=int(unit * 0.11), fill=body)
        fd.rectangle([cx - hw * 0.45, cy + hh * 0.6, cx + hw * 0.45, cy + hh * 1.9], fill=body)
        # head, then the hair cap and fringe over it
        fd.ellipse([cx - hw, cy - hh, cx + hw, cy + hh], fill=body)
        fd.chord([cx - hw * 1.18, cy - hh * 1.25, cx + hw * 1.18, cy + hh * 0.55], 180, 360, fill=hair)
        fd.chord([cx - hw * 1.05, cy - hh * 0.95, cx + hw * 0.55, cy - hh * 0.05], 150, 330, fill=hair)
        rim = fig.filter(ImageFilter.GaussianBlur(max(2, width // 80)))
        glow = Image.new("RGBA", (width, height), _mix(colours[-1], (255, 255, 255), 0.55) + (0,))
        glow.putalpha(rim.split()[3].point(lambda a: int(a * 0.6)))
        img.alpha_composite(glow)
        img.alpha_composite(fig)
    else:
        # abstract key visual: concentric light rings and a horizon glow
        shapes = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shapes)
        cx, cy = width * rng.uniform(0.35, 0.65), height * rng.uniform(0.35, 0.55)
        for k in range(5):
            r = min(width, height) * (0.12 + 0.09 * k)
            col = colours[k % len(colours)]
            sd.ellipse([cx - r, cy - r, cx + r, cy + r], outline=_mix(col, (255, 255, 255), 0.3) + (150 - k * 22,),
                       width=max(2, width // 160))
        sd.rectangle([0, int(height * 0.72), width, height], fill=_mix(colours[0], (0, 0, 0), 0.6) + (120,))
        img.alpha_composite(shapes.filter(ImageFilter.GaussianBlur(1)))

    out = img.convert("RGB")
    if reference is not None and denoise < 0.999:
        ref = reference.convert("RGB").resize((width, height))
        out = Image.blend(ref, out, max(0.0, min(1.0, float(denoise))))
    draw = ImageDraw.Draw(out)
    label = f"demo backend  seed {seed}  {prompt_text[:48]}"
    font = ImageFont.load_default()
    draw.rectangle([0, height - 16, width, height], fill=(0, 0, 0))
    draw.text((6, height - 14), label, fill=(200, 200, 200), font=font)
    return out


def render_fake_frames(seed: int, prompt_text: str, width: int, height: int, frames: int,
                       reference: Optional[Image.Image] = None) -> list[Image.Image]:
    return [render_fake_image(seed, prompt_text, width, height, frame=i, reference=reference, denoise=0.55) for i in range(frames)]


_NOTE_HZ = {
    "C": 261.63, "C#": 277.18, "DB": 277.18, "D": 293.66, "D#": 311.13, "EB": 311.13,
    "E": 329.63, "F": 349.23, "F#": 369.99, "GB": 369.99, "G": 392.00, "G#": 415.30,
    "AB": 415.30, "A": 440.00, "A#": 466.16, "BB": 466.16, "B": 493.88,
}


def _key_root_hz(key: str) -> float:
    letter = (key or "C").split()[0].strip().upper()
    return _NOTE_HZ.get(letter, 220.0) / 2  # a bass-register root, one octave down


def render_fake_song(seed: int, tags: str, lyrics: str, bpm: float, duration_s: float,
                     key: str = "C major", sr: int = 32000) -> bytes:
    """A deterministic synthesised track with a real, analysable rhythm: a
    kick on every beat and a filtered-noise snare on the off-beats (so a
    "half-time 140 bpm" brief reads back as such), a sustained bass/pad
    tone at the key's root, and a sparse melodic line derived from the
    lyrics' line lengths. Not music - a fixture with a real tempo and
    downbeat structure, so the app's own beat tracker
    (`studio_analyze_audio`) recovers the same BPM. Returns 16-bit mono
    WAV bytes; same seed/tags/lyrics/bpm/duration/key -> byte-identical.
    """
    import io

    import numpy as np

    rng = random.Random(seed)
    bpm = max(40.0, float(bpm or 120))
    duration_s = max(1.0, float(duration_s or 30))
    n = int(sr * duration_s)
    audio = np.zeros(n, dtype=np.float64)
    beat_s = 60.0 / bpm
    root_hz = _key_root_hz(key)

    detune = 1.0 + ((sum(map(ord, tags[:32])) % 7) - 3) * 0.0015
    t = np.arange(n) / sr
    audio += 0.10 * np.sin(2 * np.pi * root_hz * detune * t) + 0.05 * np.sin(2 * np.pi * root_hz * 2 * detune * t)

    beat_i = 0
    pos_s = 0.0
    while pos_s < duration_s:
        i0 = int(pos_s * sr)
        if beat_i % 2 == 0:
            dur = min(0.18, duration_s - pos_s)
            k = np.arange(int(sr * dur))
            env = np.exp(-k / (sr * 0.045))
            freq = 90.0 * np.exp(-k / (sr * 0.02)) + 45.0
            audio[i0:i0 + len(k)] += 0.85 * env * np.sin(2 * np.pi * np.cumsum(freq) / sr)
        else:
            dur = min(0.12, duration_s - pos_s)
            k = np.arange(int(sr * dur))
            env = np.exp(-k / (sr * 0.035))
            noise = np.array([rng.uniform(-1.0, 1.0) for _ in k])
            audio[i0:i0 + len(k)] += 0.5 * env * noise
        pos_s += beat_s
        beat_i += 1

    lines = [ln for ln in (lyrics or "").splitlines() if ln.strip() and not ln.strip().startswith("[")]
    for i, line in enumerate(lines[:24]):
        note_s = i * beat_s * 2
        if note_s >= duration_s:
            break
        step = (len(line) * (i + 1)) % 12
        freq = root_hz * 2 * (2 ** (step / 12))
        dur = min(beat_s * 1.8, duration_s - note_s)
        k = np.arange(max(1, int(sr * dur)))
        env = np.exp(-k / (sr * 0.4))
        i0 = int(note_s * sr)
        audio[i0:i0 + len(k)] += 0.12 * env * np.sin(2 * np.pi * freq * k / sr)

    peak = float(np.max(np.abs(audio))) or 1.0
    pcm = np.clip(audio / peak * 0.9, -1.0, 1.0)
    samples = (pcm * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sr)
        wav_file.writeframes(samples.tobytes())
    return buf.getvalue()


def _first(workflow: dict, class_type: str) -> Optional[dict]:
    for node in workflow.values():
        if node.get("class_type") == class_type:
            return node
    return None


def _all(workflow: dict, class_type: str) -> list[dict]:
    return [n for n in workflow.values() if n.get("class_type") == class_type]


class FakeComfyServer:
    """Owns state; `.app` is the ASGI app, `.run_in_thread()` starts it."""

    def __init__(self, storage_dir: Path):
        self.storage_dir = Path(storage_dir)
        self.output_dir = self.storage_dir / "output"
        self.input_dir = self.storage_dir / "input"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.history: dict[str, dict[str, Any]] = {}
        self.prompts_seen: list[dict[str, Any]] = []
        self.vram_free_bytes = 20_000_000_000
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None
        self.app = self._build_app()

    # ------------------------------------------------------------------
    def _object_info(self, node: Optional[str] = None) -> dict[str, Any]:
        full = real_object_info()
        if node:
            if node not in full:
                raise HTTPException(status_code=404, detail=f"unknown node class {node}")
            return {node: full[node]}
        return full

    def _find_reference(self, workflow: dict[str, Any]) -> Optional[Image.Image]:
        load = _first(workflow, "LoadImage")
        if load is None:
            return None
        ref_path = self.input_dir / Path(str(load["inputs"].get("image", ""))).name
        if not ref_path.is_file():
            return None
        with Image.open(ref_path) as im:
            return im.convert("RGB")

    def _positive_prompt_text(self, workflow: dict[str, Any], ksampler_inputs: dict[str, Any]) -> str:
        """The CLIPTextEncode the sampler's `positive` links to (works for
        SDXL/SD15/SVD/Flux/Kontext/Wan alike - they all wire a plain
        CLIPTextEncode into KSampler.positive); ACE-Step's own
        TextEncodeAceStepAudio1.5 is read directly by the audio path
        instead, since it carries tags/lyrics, not a "text" field."""
        pos = ksampler_inputs.get("positive")
        if isinstance(pos, list) and pos and pos[0] in workflow:
            node = workflow[pos[0]]
            if node.get("class_type") == "CLIPTextEncode":
                return str(node.get("inputs", {}).get("text", ""))
        texts = _all(workflow, "CLIPTextEncode")
        return str(texts[0]["inputs"].get("text", "")) if texts else ""

    def _process_prompt(self, prompt_id: str, workflow: dict[str, Any]) -> None:
        object_info = self._object_info()
        missing = sorted({n.get("class_type") for n in workflow.values() if n.get("class_type") not in object_info})
        if missing:
            raise HTTPException(status_code=400, detail={"error": f"unknown node types: {missing}"})
        # the real server validates every input before queueing (required
        # inputs incl. dynamic-combo children, combo choices, number ranges);
        # doing the same here makes a template bug fail in the cloud first
        problems = validate_values(workflow, object_info)
        if problems:
            raise HTTPException(status_code=400, detail={"error": "prompt_outputs_failed_validation",
                                                         "node_errors": problems[:20]})
        self.prompts_seen.append(workflow)

        audio_node = _first(workflow, "SaveAudioMP3") or _first(workflow, "SaveAudio")
        anim_node = _first(workflow, "SaveAnimatedWEBP")
        wan_save = _first(workflow, "SaveVideo")
        save_node = _first(workflow, "SaveImage")

        outputs: dict[str, Any] = {}
        if audio_node is not None:
            node_id = self._render_audio(prompt_id, workflow, audio_node, outputs)
        elif wan_save is not None:
            node_id = self._render_wan_video(prompt_id, workflow, wan_save, outputs)
        elif anim_node is not None:
            node_id = self._render_svd_video(prompt_id, workflow, anim_node, outputs)
        elif save_node is not None:
            node_id = self._render_image(prompt_id, workflow, save_node, outputs)
        else:
            raise HTTPException(status_code=400, detail={"error": "no recognised output node "
                                "(SaveImage/SaveAnimatedWEBP/SaveVideo/SaveAudioMP3/SaveAudio)"})
        assert node_id in outputs

        self.history[prompt_id] = {"prompt": workflow, "outputs": outputs,
                                   "status": {"status_str": "success", "completed": True, "messages": []}}

    # -- one rendering path per output kind ----------------------------
    def _render_image(self, prompt_id: str, workflow: dict[str, Any], save_node: dict, outputs: dict) -> str:
        ksampler = _first(workflow, "KSampler")
        k_inputs = (ksampler or {}).get("inputs", {})
        seed = int(k_inputs.get("seed", 0))
        denoise = float(k_inputs.get("denoise", 1.0))
        prompt_text = self._positive_prompt_text(workflow, k_inputs)
        reference = self._find_reference(workflow)
        empty_latent = _first(workflow, "EmptyLatentImage") or _first(workflow, "EmptySD3LatentImage") or {}
        if reference is not None and not empty_latent:
            width, height = reference.size
        else:
            width = int(empty_latent.get("inputs", {}).get("width", 512))
            height = int(empty_latent.get("inputs", {}).get("height", 512))
        upscale = _first(workflow, "LatentUpscale")
        if upscale is not None:
            width = int(upscale["inputs"].get("width", width))
            height = int(upscale["inputs"].get("height", height))
        img = render_fake_image(seed, prompt_text, width, height, reference=reference, denoise=denoise)
        filename = f"{prompt_id}.png"
        img.save(self.output_dir / filename)
        node_id = [k for k, v in workflow.items() if v is save_node][0]
        outputs[node_id] = {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}
        return node_id

    def _render_svd_video(self, prompt_id: str, workflow: dict[str, Any], anim_node: dict, outputs: dict) -> str:
        ksampler = _first(workflow, "KSampler")
        k_inputs = (ksampler or {}).get("inputs", {})
        seed = int(k_inputs.get("seed", 0))
        prompt_text = self._positive_prompt_text(workflow, k_inputs)
        reference = self._find_reference(workflow)
        svd = _first(workflow, "SVD_img2vid_Conditioning") or {}
        width = int(svd.get("inputs", {}).get("width", 512)) // 2
        height = int(svd.get("inputs", {}).get("height", 512)) // 2
        n_frames = int(svd.get("inputs", {}).get("video_frames", 14))
        fps = int(anim_node.get("inputs", {}).get("fps", 7))
        frames = render_fake_frames(seed, prompt_text or "animated idol portrait", width, height, n_frames, reference)
        filename = f"{prompt_id}.webp"
        path = self.output_dir / filename
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=max(1, 1000 // max(1, fps)), loop=0)
        node_id = [k for k, v in workflow.items() if v is anim_node][0]
        outputs[node_id] = {"images": [{"filename": filename, "subfolder": "", "type": "output"}], "animated": [True]}
        return node_id

    def _render_wan_video(self, prompt_id: str, workflow: dict[str, Any], save_node: dict, outputs: dict) -> str:
        """Wan 2.2 TI2V: image (or text) to video. Fake output is a short
        animated WEBP, same as SVD - `engine._import_comfy_output` already
        detects the RIFF/WEBP signature and conforms it to mp4."""
        ksampler = _first(workflow, "KSampler")
        k_inputs = (ksampler or {}).get("inputs", {})
        seed = int(k_inputs.get("seed", 0))
        prompt_text = self._positive_prompt_text(workflow, k_inputs)
        reference = self._find_reference(workflow)
        latent = _first(workflow, "Wan22ImageToVideoLatent") or {}
        width = int(latent.get("inputs", {}).get("width", 1280)) // 4
        height = int(latent.get("inputs", {}).get("height", 704)) // 4
        length = int(latent.get("inputs", {}).get("length", 121))
        create_video = _first(workflow, "CreateVideo") or {}
        fps = int(create_video.get("inputs", {}).get("fps", 24))
        # a handful of frames is enough for a fixture; sample evenly across
        # the clip's declared length so motion still reads as a real clip
        n_frames = max(2, min(24, length // max(1, fps // 6 or 1)))
        frames = render_fake_frames(seed, prompt_text or "night street scene", max(64, width), max(64, height), n_frames, reference)
        filename = f"{prompt_id}.webp"
        path = self.output_dir / filename
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=max(1, 1000 // max(1, fps)), loop=0)
        node_id = [k for k, v in workflow.items() if v is save_node][0]
        outputs[node_id] = {"images": [{"filename": filename, "subfolder": "", "type": "output"}], "animated": [True]}
        return node_id

    def _render_audio(self, prompt_id: str, workflow: dict[str, Any], save_node: dict, outputs: dict) -> str:
        """ACE-Step: a song with vocals and lyrics. Fake output is a real,
        analysable WAV (see `render_fake_song`) - a real beat so
        `studio_analyze_audio` (tempo, sections) works on it, not silence
        or noise."""
        text_node = _first(workflow, "TextEncodeAceStepAudio1.5") or {}
        t_inputs = text_node.get("inputs", {})
        ksampler = _first(workflow, "KSampler") or {}
        seed = int(t_inputs.get("seed", ksampler.get("inputs", {}).get("seed", 0)))
        tags = str(t_inputs.get("tags", ""))
        lyrics = str(t_inputs.get("lyrics", ""))
        bpm = float(t_inputs.get("bpm", 120))
        key = str(t_inputs.get("keyscale", "C major"))
        empty_audio = _first(workflow, "EmptyAceStep1.5LatentAudio") or {}
        duration_s = float(t_inputs.get("duration", empty_audio.get("inputs", {}).get("seconds", 30)))
        data = render_fake_song(seed, tags, lyrics, bpm, duration_s, key)
        filename = f"{prompt_id}.wav"
        (self.output_dir / filename).write_bytes(data)
        node_id = [k for k, v in workflow.items() if v is save_node][0]
        outputs[node_id] = {"audio": [{"filename": filename, "subfolder": "", "type": "output"}]}
        return node_id

    # ------------------------------------------------------------------
    def _build_app(self) -> FastAPI:
        app = FastAPI(title="fake-comfyui")

        @app.get("/system_stats")
        def system_stats():
            return {
                "system": {"os": "linux", "comfyui_version": "fake-0.0"},
                "devices": [{"name": "fake-gpu", "type": "cuda", "vram_total": 24_000_000_000, "vram_free": self.vram_free_bytes}],
            }

        @app.get("/object_info")
        def object_info_all():
            return self._object_info()

        @app.get("/object_info/{node}")
        def object_info_one(node: str):
            return self._object_info(node)

        @app.post("/upload/image")
        async def upload_image(image: UploadFile, overwrite: str = "true"):
            data = await image.read()
            dest = self.input_dir / Path(image.filename or "upload.png").name
            dest.write_bytes(data)
            return {"name": image.filename, "subfolder": "", "type": "input"}

        @app.post("/prompt")
        async def queue_prompt(request: Request):
            body = await request.json()
            workflow = body.get("prompt") or {}
            prompt_id = str(uuid.uuid4())
            self._process_prompt(prompt_id, workflow)
            return {"prompt_id": prompt_id, "number": len(self.history)}

        @app.get("/history/{prompt_id}")
        def history_one(prompt_id: str):
            entry = self.history.get(prompt_id)
            return {prompt_id: entry} if entry else {}

        @app.get("/history")
        def history_all():
            return self.history

        @app.get("/view")
        def view(filename: str, subfolder: str = "", type: str = "output"):
            base = (self.output_dir if type == "output" else self.input_dir).resolve()
            path = (base / subfolder / filename).resolve()
            if base not in path.parents or not path.is_file():
                raise HTTPException(status_code=404, detail="file not found")
            return FileResponse(path)

        @app.post("/free")
        async def free(request: Request):
            return JSONResponse({"ok": True})

        @app.post("/interrupt")
        def interrupt():
            return JSONResponse({"ok": True})

        return app

    # ------------------------------------------------------------------
    def run_in_thread(self, port: int = 0, host: str = "127.0.0.1") -> int:
        import socket

        import uvicorn

        if port == 0:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((host, 0))
            port = sock.getsockname()[1]
            sock.close()

        # loop="asyncio": uvicorn's default "auto" installs uvloop process-wide
        # (asyncio.set_event_loop_policy) the moment this thread starts, which
        # breaks subprocess spawning (e.g. an MCP stdio client) anywhere else
        # in the process afterwards - uvloop's policy has no legacy child
        # watcher, which is exactly what `loop.subprocess_exec` needs.
        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning", loop="asyncio")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while not getattr(self._server, "started", False) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.port = port
        return port

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
