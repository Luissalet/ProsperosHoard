"""KittenTTS (ONNX, 15-80 M parameters, CPU only, English) as a voice engine, run in its own environment.

kittentts 0.8 needs Python < 3.13 (its phonemiser, misaki) and this app runs on 3.13, so the engine lives in
``<data>/voices/engines/kittentts/venv`` built from a Python 3.10-3.12 found on the PC (PROSPERO_KITTEN_PYTHON wins)
by the engine's explicit install action, and every line is synthesised out of process by ``kitten_worker.py``.
Models: nano (15 M), micro (40 M), mini (80 M, default; PROSPERO_KITTEN_MODEL); voice_ref is a voice name or
``<size>/<voice>`` (e.g. ``nano/Luna``)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import procutil
from .voice_engines import EngineCapabilities, EngineNotInstalled, TTSEngine

WHEEL = "https://github.com/KittenML/KittenTTS/releases/download/0.8.1/kittentts-0.8.1-py3-none-any.whl"
MODELS = {"nano": "KittenML/kitten-tts-nano-0.8", "micro": "KittenML/kitten-tts-micro-0.8", "mini": "KittenML/kitten-tts-mini-0.8"}
VOICES = ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"]
WORKER = Path(__file__).with_name("kitten_worker.py")
PY_VERSIONS = ("3.12", "3.11", "3.10")


def env_dir(voices_dir: Optional[Path]) -> Path:
    return Path(voices_dir or "data/voices") / "engines" / "kittentts"


def venv_python(root: Path) -> Path:
    return root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _version_ok(exe: str) -> Optional[str]:
    try:
        out = procutil.run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = (out.stdout or "").strip()
    return version if out.returncode == 0 and version in PY_VERSIONS else None


def find_base_python() -> Optional[str]:
    """A Python 3.10-3.12 to build the environment with: PROSPERO_KITTEN_PYTHON, the py launcher, then PATH."""
    explicit = os.getenv("PROSPERO_KITTEN_PYTHON", "").strip().strip('"')
    if explicit and Path(explicit).is_file() and _version_ok(explicit):
        return explicit
    launcher = shutil.which("py")
    for version in PY_VERSIONS:
        if launcher:
            try:
                out = procutil.run([launcher, f"-{version}", "-c", "import sys; print(sys.executable)"], text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                out = None
            exe = (out.stdout or "").strip() if out is not None and out.returncode == 0 else ""
            if exe and _version_ok(exe):
                return exe
        on_path = shutil.which(f"python{version}")
        if on_path and _version_ok(on_path):
            return on_path
    return None


def _parse(stdout: str) -> dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("KITTEN_RESULT "):
            try:
                return json.loads(line[len("KITTEN_RESULT "):])
            except ValueError:
                break
    return {"ok": False, "error": "the KittenTTS worker gave no result"}


def run_worker(python: Path, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        proc = procutil.run([str(python), "-I", str(WORKER)], text=True, input=json.dumps(spec), timeout=timeout,
                            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired as exc:
        raise EngineNotInstalled("kittentts", f"KittenTTS took longer than {int(timeout)} s") from exc
    result = _parse(proc.stdout)
    if not result.get("ok"):
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise EngineNotInstalled("kittentts", result.get("error") or " | ".join(tail) or "KittenTTS failed")
    return result


def install(voices_dir: Optional[Path], progress=None) -> dict[str, Any]:
    """The explicit install action: environment with a Python 3.10-3.12, the kittentts wheel, the default model."""
    say = progress or (lambda *_: None)
    root = env_dir(voices_dir)
    base = find_base_python()
    if not base:
        raise EngineNotInstalled("kittentts", "needs Python 3.10, 3.11 or 3.12 on this PC (or PROSPERO_KITTEN_PYTHON)")
    root.mkdir(parents=True, exist_ok=True)
    py = venv_python(root)
    steps = []
    if not py.is_file():
        say(0.1, f"creating the KittenTTS environment with {base}")
        steps.append(procutil.run([base, "-m", "venv", str(root / "venv")], text=True, timeout=600))
    say(0.3, "installing kittentts 0.8.1")
    steps.append(procutil.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", WHEEL], text=True, timeout=1800))
    failed = next((s for s in steps if s.returncode != 0), None)
    if failed is not None:
        raise EngineNotInstalled("kittentts", (failed.stderr or failed.stdout or "")[-1500:] or "install failed")
    say(0.8, "downloading the default model")
    warm = run_worker(py, {"warmup": True, "model": default_model(), "cache_dir": str(root / "models")}, 900)
    return {"engine_id": "kittentts", "python": base, "venv": str(root / "venv"), "model": default_model(),
            "load_s": warm.get("load_s"), "log_tail": (steps[-1].stdout or "")[-1200:]}


def default_model() -> str:
    wanted = os.getenv("PROSPERO_KITTEN_MODEL", "mini").strip()
    return MODELS.get(wanted, wanted if "/" in wanted else MODELS["mini"])


def resolve_voice(voice_ref: Optional[str]) -> tuple[str, str]:
    """``Luna`` / ``nano/Luna`` / ``KittenML/kitten-tts-micro-0.8`` -> (model id, voice)."""
    ref = (voice_ref or "").strip()
    model, voice = default_model(), "Jasper"
    if ref.startswith("KittenML/"):
        model, ref = ref, ""
    elif "/" in ref:
        head, _, tail = ref.rpartition("/")
        if head in MODELS:
            model, ref = MODELS[head], tail
    if ref:
        match = next((v for v in VOICES if v.lower() == ref.lower()), None)
        if match is None:
            raise EngineNotInstalled("kittentts", f"unknown KittenTTS voice '{ref}'; voices: {', '.join(VOICES)}")
        voice = match
    return model, voice


class KittenTTSEngine(TTSEngine):
    """KittenTTS 0.8: tiny ONNX models that run on the CPU only (no GPU wake-up), English, eight voices."""

    id = "kittentts"
    label = "KittenTTS (CPU, ONNX)"
    pip_packages = [WHEEL]  # installed into its own environment, see install()
    capabilities = EngineCapabilities(languages=["en"], cloning=False, streaming=False, needs_gpu=False, multi_speaker=True)
    sidecar = True

    def __init__(self, voices_dir: Optional[Path] = None):
        self.root = env_dir(voices_dir)
        self.voices_dir = voices_dir

    def is_installed(self) -> bool:
        venv = self.root / "venv"
        if not venv_python(self.root).is_file():
            return False
        return any(venv.glob("Lib/site-packages/kittentts")) or any(venv.glob("lib/python3.*/site-packages/kittentts"))

    def install_hint(self) -> str:
        return "Install from the Voice screen: its own environment with Python 3.10-3.12 and the kittentts 0.8.1 wheel (CPU, ~80 MB model)"

    def status(self) -> dict[str, Any]:
        result = super().status()
        result.update({"voices": [*VOICES, *(f"{size}/{v}" for size in ("nano", "micro") for v in VOICES)],
                       "default_voice": "Jasper", "model": default_model(), "environment": str(self.root / "venv")})
        return result

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        if language and not str(language).lower().startswith("en"):
            raise EngineNotInstalled(self.id, f"KittenTTS 0.8 speaks English only, not '{language}'")
        model, voice = resolve_voice(voice_ref)
        work = self.root / "work"
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as tmp:
            out = Path(tmp) / "line.wav"
            run_worker(venv_python(self.root), {"text": text, "out": str(out), "model": model, "voice": voice,
                                                "speed": float(speed or 1.0), "cache_dir": str(self.root / "models")},
                       timeout=120 + len(text) / 10)
            if not out.is_file():
                raise EngineNotInstalled(self.id, "KittenTTS wrote no audio")
            return out.read_bytes()
