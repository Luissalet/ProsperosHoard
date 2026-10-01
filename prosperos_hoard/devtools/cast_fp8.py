"""Cast a Wan diffusion model's transformer-block linear weights to float8_e4m3fn
(the same plain cast as the community fp8 Wan files), keeping norms, biases,
embeddings and heads in their original precision. Streams tensor by tensor.

Wan Animate 2 ships in bf16 (33 GB) and int8: the int8 file crawls on a card
it does not fit, the fp8 cast streams at full speed. Run with ComfyUI's Python
(it has torch and safetensors):

    python -m prosperos_hoard.devtools.cast_fp8 SRC.safetensors DST.safetensors
"""
from __future__ import annotations

import sys
import time


def cast(src: str, dst: str) -> tuple[int, int]:
    import torch  # noqa: PLC0415 - only ComfyUI's environment has it
    from safetensors import safe_open  # noqa: PLC0415
    from safetensors.torch import save_file  # noqa: PLC0415

    out = {}
    started = time.time()
    cast_n = kept = 0
    with safe_open(src, framework="pt") as f:
        meta = f.metadata() or {}
        keys = list(f.keys())
        for i, k in enumerate(keys):
            t = f.get_tensor(k)
            if (k.endswith(".weight") and t.ndim == 2 and ".blocks." in f".{k}" and "norm" not in k
                    and t.numel() >= 1 << 20):
                out[k] = t.to(torch.float8_e4m3fn)
                cast_n += 1
            else:
                out[k] = t
                kept += 1
            if i % 200 == 0:
                print(f"{i}/{len(keys)} {time.time() - started:.0f}s", flush=True)
    meta = {k: v for k, v in meta.items() if "quant" not in k.lower()}
    save_file(out, dst, metadata=meta)
    print(f"done: {cast_n} cast to fp8, {kept} kept, {time.time() - started:.0f}s", flush=True)
    return cast_n, kept


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    cast(sys.argv[1], sys.argv[2])
