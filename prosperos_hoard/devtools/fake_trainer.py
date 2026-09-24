"""A fake LoRA trainer for tests and demos: emits tqdm-like progress lines
on stdout, sleeps a configurable delay per step, then writes a tiny but
structurally valid `.safetensors` file so `trainers.find_output` and any
downstream code that opens the file (header length + JSON header) works
against real output shapes.

Run as `python -m prosperos_hoard.devtools.fake_trainer --out DIR --name
NAME --steps N [--delay SECONDS]`.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path


def write_fake_safetensors(path: Path) -> None:
    """A minimal valid safetensors file: an 8-byte little-endian header
    length, a JSON header describing one small tensor, then that tensor's
    raw bytes (4 bytes: one float32)."""
    header = {
        "__metadata__": {"format": "pt", "fake": "prospero"},
        "lora_unet_fake.lora_down.weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]},
    }
    header_bytes = json.dumps(header).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(struct.pack("<Q", len(header_bytes)))
        f.write(header_bytes)
        f.write(b"\x00\x00\x00\x00")  # one float32 payload byte


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake LoRA trainer for tests/demos")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--name", required=True, help="output file stem")
    parser.add_argument("--steps", type=int, required=True, help="total training steps")
    parser.add_argument("--delay", type=float, default=0.01, help="seconds to sleep per step")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    steps = max(1, args.steps)

    for i in range(1, steps + 1):
        pct = int(100 * i / steps)
        loss = max(0.01, 1.0 - i / steps)
        print(f"{pct:3d}%|{'#' * (pct // 10):10s}| {i}/{steps} [00:00<00:00, 1.0it/s, loss={loss:.3f}]", flush=True)
        if args.delay:
            time.sleep(args.delay)

    write_fake_safetensors(out_dir / f"{args.name}.safetensors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
