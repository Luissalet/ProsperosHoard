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

_PALETTE = [
    (255, 77, 141), (245, 194, 107), (108, 92, 231), (0, 184, 148),
    (9, 132, 227), (253, 121, 168), (225, 112, 85), (39, 60, 117),
]


def _seeded_gradient(width: int, height: int, seed: int) -> Image.Image:
    rng = random.Random(seed)
    c1 = _PALETTE[rng.randrange(len(_PALETTE))]
    c2 = _PALETTE[rng.randrange(len(_PALETTE))]
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        for x in range(width):
            px[x, y] = (r, g, b)
    return img


def render_fake_image(seed: int, prompt_text: str, width: int = 512, height: int = 512, frame: int = 0) -> Image.Image:
    """Pure, deterministic: same args -> byte-identical pixels."""
    img = _seeded_gradient(width, height, seed + frame * 7919)
    draw = ImageDraw.Draw(img)
    rng = random.Random(seed + frame * 7919)
    for _ in range(6):
        cx, cy = rng.randrange(width), rng.randrange(height)
        r = rng.randrange(min(width, height) // 8, min(width, height) // 3)
        colour = _PALETTE[rng.randrange(len(_PALETTE))]
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=colour, width=4)
    label = f"seed {seed} | {prompt_text[:60]}"
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    draw.rectangle([0, height - 22, width, height], fill=(0, 0, 0))
    draw.text((6, height - 18), label, fill=(255, 255, 255), font=font)
    return img


def render_fake_frames(seed: int, prompt_text: str, width: int, height: int, frames: int) -> list[Image.Image]:
    return [render_fake_image(seed, prompt_text, width, height, frame=i) for i in range(frames)]


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
                "sampler_name": [["euler", "euler_a", "dpmpp_2m"]],
                "scheduler": [["normal", "karras"]], "denoise": ["FLOAT", {}],
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
        ksampler = _first(workflow, "KSampler")
        seed = int((ksampler or {}).get("inputs", {}).get("seed", 0))
        clip_nodes = _all(workflow, "CLIPTextEncode")
        prompt_text = clip_nodes[0]["inputs"].get("text", "") if clip_nodes else ""

        outputs: dict[str, Any] = {}
        anim_node = _first(workflow, "SaveAnimatedWEBP")
        if anim_node is not None:
            svd = _first(workflow, "SVD_img2vid_Conditioning") or {}
            width = int(svd.get("inputs", {}).get("width", 512))
            height = int(svd.get("inputs", {}).get("height", 512))
            n_frames = int(svd.get("inputs", {}).get("video_frames", 14))
            frames = render_fake_frames(seed, prompt_text, width, height, n_frames)
            filename = f"{prompt_id}.webp"
            path = self.output_dir / filename
            frames[0].save(path, save_all=True, append_images=frames[1:], duration=1000 // 7, loop=0)
            node_id = [k for k, v in workflow.items() if v is anim_node][0]
            outputs[node_id] = {"videos": [{"filename": filename, "subfolder": "", "type": "output"}]}
        else:
            save_node = _first(workflow, "SaveImage")
            empty_latent = _first(workflow, "EmptyLatentImage") or {}
            width = int(empty_latent.get("inputs", {}).get("width", 512))
            height = int(empty_latent.get("inputs", {}).get("height", 512))
            img = render_fake_image(seed, prompt_text, width, height)
            filename = f"{prompt_id}.png"
            img.save(self.output_dir / filename)
            node_id = [k for k, v in workflow.items() if v is save_node][0] if save_node else "9"
            outputs[node_id] = {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}

        self.history[prompt_id] = {"prompt": workflow, "outputs": outputs, "status": {"completed": True}}

    # ------------------------------------------------------------------
    def _build_app(self) -> FastAPI:
        app = FastAPI(title="fake-comfyui")

        @app.get("/system_stats")
        def system_stats():
            return {
                "system": {"os": "linux", "comfyui_version": "fake-0.0"},
                "devices": [{"name": "fake-gpu", "type": "cuda", "vram_total": 24_000_000_000, "vram_free": 20_000_000_000}],
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
            dest = self.input_dir / image.filename
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
            base = self.output_dir if type == "output" else self.input_dir
            path = base / subfolder / filename if subfolder else base / filename
            if not path.is_file():
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
