"""Run AuK speech editing for one job and write the candidates. Standalone: it runs inside AuK's own environment
(its torch, its repository), never imports Prospero, and is started by ``auk_retake.run_worker``.

    <auk venv python> auk_worker.py job.json

job.json: {"repo", "ckpt", "qwen", "audio", "instruction", "gen_seconds", "seeds": [1, 2, 3], "nfe": 4,
           "cfg": 2.0, "cpu_offload": true, "out_dir", "result"}
The result file gets {"ok", "load_s", "peak_vram_gib", "candidates": [{"seed", "file", "gen_s", "sample_rate"}]}
or {"ok": false, "error"}.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path


def main(job_path: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    result_path = Path(job["result"])
    try:
        sys.path.insert(0, str(Path(job["repo"]) / "src"))
        import soundfile as sf
        import torch
        import torchaudio

        def _load(path, *args, **kwargs):  # recent torchaudio loads through torchcodec, which needs FFmpeg DLLs on Windows
            data, rate = sf.read(str(path), dtype="float32", always_2d=True)
            return torch.from_numpy(data.T.copy()), rate

        torchaudio.load = _load
        from auk.infer.infer_auk import AukInfer

        ckpt = Path(job["ckpt"])
        t0 = time.time()
        engine = AukInfer(config_path=str(ckpt.parent / "config.yaml"), ckpt_path=str(ckpt), device="cuda",
                          dtype="bf16", qwen_path=job.get("qwen") or None, cpu_offload=bool(job.get("cpu_offload", True)))
        load_s = time.time() - t0
        out_dir = Path(job["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        messages = [{"role": "user", "content": [{"type": "text", "text": job["instruction"]},
                                                 {"type": "audio", "audio": job["audio"]}]}]
        candidates = []
        for seed in job.get("seeds") or [1]:
            t1 = time.time()
            audio, sr = engine.generate(messages, audio=job["audio"], gen_seconds=float(job["gen_seconds"]),
                                        nfe=int(job.get("nfe", 4)), cfg_strength=float(job.get("cfg", 2.0)), seed=int(seed))
            data = audio.to(torch.float32).detach().cpu().numpy()
            if data.ndim == 2:
                data = data.T
            out = out_dir / f"auk-seed{int(seed)}.wav"
            sf.write(str(out), data, int(sr), subtype="PCM_16")
            candidates.append({"seed": int(seed), "file": str(out), "gen_s": round(time.time() - t1, 2), "sample_rate": int(sr)})
        peak = torch.cuda.max_memory_allocated() / 2 ** 30 if torch.cuda.is_available() else 0.0
        result = {"ok": True, "load_s": round(load_s, 2), "peak_vram_gib": round(peak, 2), "candidates": candidates}
    except Exception as exc:  # noqa: BLE001 - the parent reads the reason from the result file
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-4000:]}
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
