"""A fake ComfyUI server for the cloud demo and for tests.

Speaks the small subset of the real ComfyUI HTTP API that
`prosperos_hoard.comfy_driver` and `hoard_link.ComfyClient` use:
`/system_stats`, `/object_info[/{node}]`, `/upload/image`, `/prompt`,
`/history[/{id}]`, `/view`, `/free`, `/interrupt`. Instead of running any
model it "renders" a small deterministic procedural image (or a short
procedural animated WEBP for SVD) from the prompt text + seed, with
Pillow. Same seed + same prompt -> byte-identical output, which is what
the lineage-reproduces-the-same-asset test relies on.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageFont

CHECKPOINTS_SDXL = ["sd_xl_base_1.0.safetensors"]
CHECKPOINTS_SD15 = ["v1-5-pruned-emaonly-fp16.safetensors"]
CHECKPOINTS_SVD = ["svd_xt.safetensors"]
SAMPLERS = ["euler", "euler_ancestral", "heun", "dpm_2", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_sde", "ddim", "uni_pc"]
SCHEDULERS = ["normal", "karras", "exponential", "sgm_uniform", "simple", "ddim_uniform"]

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
        full = {
            "CheckpointLoaderSimple": {
                "input": {"required": {"ckpt_name": [CHECKPOINTS_SDXL + CHECKPOINTS_SD15]}}
            },
            "ImageOnlyCheckpointLoader": {
                "input": {"required": {"ckpt_name": [CHECKPOINTS_SVD]}}
            },
            "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {}]}}},
            "KSampler": {"input": {"required": {
                "seed": ["INT", {}], "steps": ["INT", {}], "cfg": ["FLOAT", {}],
                "sampler_name": [SAMPLERS], "scheduler": [SCHEDULERS], "denoise": ["FLOAT", {}],
            }}},
            "EmptyLatentImage": {"input": {"required": {
                "width": ["INT", {}], "height": ["INT", {}], "batch_size": ["INT", {}],
            }}},
            "LoadImage": {"input": {"required": {"image": [[]]}}},
            "SaveImage": {"input": {"required": {"images": ["IMAGE", {}]}}},
            "SaveAnimatedWEBP": {"input": {"required": {"images": ["IMAGE", {}], "fps": ["INT", {}]}}},
            "VAEDecode": {"input": {"required": {}}},
            "VAEEncode": {"input": {"required": {}}},
            "VAEEncodeForInpaint": {"input": {"required": {}}},
            "ImageToMask": {"input": {"required": {}}},
            "LatentUpscale": {"input": {"required": {}}},
            "SVD_img2vid_Conditioning": {"input": {"required": {}}},
        }
        if node:
            if node not in full:
                raise HTTPException(status_code=404, detail=f"unknown node class {node}")
            return {node: full[node]}
        return full

    def _process_prompt(self, prompt_id: str, workflow: dict[str, Any]) -> None:
        missing = [n.get("class_type") for n in workflow.values() if n.get("class_type") not in self._object_info()]
        if missing:
            raise HTTPException(status_code=400, detail={"error": f"unknown node types: {missing}"})
        ksampler = _first(workflow, "KSampler")
        k_inputs = (ksampler or {}).get("inputs", {})
        seed = int(k_inputs.get("seed", 0))
        denoise = float(k_inputs.get("denoise", 1.0))
        # the positive prompt is the CLIPTextEncode the sampler's `positive` links to
        prompt_text = ""
        pos = k_inputs.get("positive")
        if isinstance(pos, list) and pos and pos[0] in workflow:
            prompt_text = str(workflow[pos[0]].get("inputs", {}).get("text", ""))
        elif _all(workflow, "CLIPTextEncode"):
            prompt_text = str(_all(workflow, "CLIPTextEncode")[0]["inputs"].get("text", ""))
        reference = None
        load = _first(workflow, "LoadImage")
        if load is not None:
            ref_path = self.input_dir / Path(str(load["inputs"].get("image", ""))).name
            if ref_path.is_file():
                with Image.open(ref_path) as im:
                    reference = im.convert("RGB")
        self.prompts_seen.append(workflow)

        outputs: dict[str, Any] = {}
        anim_node = _first(workflow, "SaveAnimatedWEBP")
        if anim_node is not None:
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
        else:
            save_node = _first(workflow, "SaveImage")
            empty_latent = _first(workflow, "EmptyLatentImage") or {}
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
            node_id = [k for k, v in workflow.items() if v is save_node][0] if save_node else "9"
            outputs[node_id] = {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}

        self.history[prompt_id] = {"prompt": workflow, "outputs": outputs,
                                   "status": {"status_str": "success", "completed": True, "messages": []}}

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

        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning")
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
