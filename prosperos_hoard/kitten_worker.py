"""Runs in KittenTTS's own environment (Python 3.10-3.12), never imported by Prospero.

stdin: one JSON object {text, out, model, voice, speed, cache_dir} -> writes a 24 kHz WAV at ``out`` and prints one
``KITTEN_RESULT {...}`` line. {"warmup": true, model, cache_dir} only downloads/loads the model."""
import json
import sys
import time


def main() -> int:
    spec = json.loads(sys.stdin.read() or "{}")
    started = time.time()
    from kittentts import KittenTTS

    model = KittenTTS(spec.get("model") or "KittenML/kitten-tts-mini-0.8", cache_dir=spec.get("cache_dir"))
    loaded = time.time() - started
    if spec.get("warmup"):
        print("KITTEN_RESULT " + json.dumps({"ok": True, "load_s": round(loaded, 2)}))
        return 0
    model.generate_to_file(spec["text"], spec["out"], voice=spec.get("voice") or "Jasper", speed=float(spec.get("speed") or 1.0))
    print("KITTEN_RESULT " + json.dumps({"ok": True, "load_s": round(loaded, 2), "seconds": round(time.time() - started, 2)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # reported to Prospero as one line
        print("KITTEN_RESULT " + json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        sys.exit(1)
