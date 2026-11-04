"""Runs in VoxCPM2's own environment (Python 3.10-3.12, CUDA torch), never imported by Prospero.

stdin: one JSON object {text, out, model, cache_dir, gpu, reference, prompt_text, cfg, steps, seed, stream}
-> writes a mono WAV at ``out`` and prints one ``VOXCPM_RESULT {...}`` line with load_s, first_audio_s, seconds, audio_s,
sample_rate. {"warmup": true, model, cache_dir, gpu} only downloads/loads the model."""
import json
import os
import sys
import time


def main() -> int:
    spec = json.loads(sys.stdin.read() or "{}")
    if spec.get("gpu") not in (None, ""):
        os.environ["CUDA_VISIBLE_DEVICES"] = str(spec["gpu"])
    if spec.get("cache_dir"):
        os.environ.setdefault("HF_HOME", str(spec["cache_dir"]))
    started = time.time()
    import numpy as np
    import soundfile as sf
    from voxcpm import VoxCPM

    model = VoxCPM.from_pretrained(spec.get("model") or "openbmb/VoxCPM2", load_denoiser=False)
    loaded = time.time() - started
    rate = int(model.tts_model.sample_rate)
    if spec.get("warmup"):
        print("VOXCPM_RESULT " + json.dumps({"ok": True, "load_s": round(loaded, 2), "sample_rate": rate}))
        return 0
    args = {"text": spec["text"], "cfg_value": float(spec.get("cfg") or 2.0), "inference_timesteps": int(spec.get("steps") or 10)}
    if spec.get("seed") not in (None, ""):
        args["seed"] = int(spec["seed"])
    if spec.get("reference"):
        args["reference_wav_path"] = spec["reference"]
        if spec.get("prompt_text"):
            args["prompt_wav_path"] = spec["reference"]
            args["prompt_text"] = spec["prompt_text"]
    began = time.time()
    first = None
    if spec.get("stream"):
        chunks = []
        for chunk in model.generate_streaming(**args):
            if first is None:
                first = time.time() - began
            chunks.append(np.asarray(chunk, dtype="float32"))
        wav = np.concatenate(chunks) if chunks else np.zeros(1, dtype="float32")
    else:
        wav = np.asarray(model.generate(**args), dtype="float32")
    took = time.time() - began
    sf.write(spec["out"], wav.reshape(-1), rate)
    print("VOXCPM_RESULT " + json.dumps({"ok": True, "load_s": round(loaded, 2), "seconds": round(took, 2),
                                         "first_audio_s": round(first, 2) if first is not None else None,
                                         "audio_s": round(len(wav.reshape(-1)) / rate, 2), "sample_rate": rate}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # reported to Prospero as one line
        print("VOXCPM_RESULT " + json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        sys.exit(1)
