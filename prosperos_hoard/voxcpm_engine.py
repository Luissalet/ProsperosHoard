"""VoxCPM2 (Apache-2.0, 30 languages, 48 kHz, voice design and cloning, streaming) as a voice engine, run in its own environment.

voxcpm needs Python 3.10-3.12 and a CUDA torch (>= 2.5), so the engine lives in ``<data>/voices/engines/voxcpm/venv`` built by the
engine's explicit install action (PROSPERO_VOXCPM_PYTHON wins; the CUDA 12.8 torch wheels, which also cover the RTX 50 cards) and
every line is synthesised out of process by ``voxcpm_worker.py`` on the GPU named by PROSPERO_VOXCPM_GPU (default 2, an idle
Aorus card; the text model of the Sparks is never touched). voice_ref: empty = the model's own voice, ``(a calm older man)``-style
text in parentheses = a designed voice, a path to a WAV = a reference clip (the clip given as ``sample_path`` wins)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import procutil
from .kitten_engine import PY_VERSIONS, _version_ok, venv_python
from .voice_engines import EngineCapabilities, EngineNotInstalled, TTSEngine

MODEL = "openbmb/VoxCPM2"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
WORKER = Path(__file__).with_name("voxcpm_worker.py")
LANGUAGES = ["es", "en", "fr", "de", "it", "pt", "nl", "pl", "ru", "tr", "ar", "hi", "ja", "ko", "zh", "vi", "th", "id", "sv", "da",
             "fi", "no", "el", "he", "cs", "hu", "ro", "uk", "ca", "ms"]


def env_dir(voices_dir: Optional[Path]) -> Path:
    return Path(voices_dir or "data/voices") / "engines" / "voxcpm"


def find_base_python() -> Optional[str]:
    """PROSPERO_VOXCPM_PYTHON, else the first Python 3.10-3.12 the py launcher or PATH knows."""
    import shutil
    explicit = os.getenv("PROSPERO_VOXCPM_PYTHON", "").strip().strip('"')
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


def gpu_index() -> str:
    return os.getenv("PROSPERO_VOXCPM_GPU", "2").strip()


def _parse(stdout: str) -> dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        if line.startswith("VOXCPM_RESULT "):
            try:
                return json.loads(line[len("VOXCPM_RESULT "):])
            except ValueError:
                break
    return {"ok": False, "error": "the VoxCPM worker gave no result"}


def run_worker(python: Path, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        proc = procutil.run([str(python), "-I", str(WORKER)], text=True, input=json.dumps(spec), timeout=timeout,
                            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired as exc:
        raise EngineNotInstalled("voxcpm", f"VoxCPM2 took longer than {int(timeout)} s") from exc
    result = _parse(proc.stdout)
    if not result.get("ok"):
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise EngineNotInstalled("voxcpm", result.get("error") or " | ".join(tail) or "VoxCPM2 failed")
    return result


def install(voices_dir: Optional[Path], progress=None) -> dict[str, Any]:
    """The explicit install action: environment with a Python 3.10-3.12, CUDA torch, voxcpm, then the model (~8 GB)."""
    say = progress or (lambda *_: None)
    root = env_dir(voices_dir)
    base = find_base_python()
    if not base:
        raise EngineNotInstalled("voxcpm", "needs Python 3.10, 3.11 or 3.12 on this PC (or PROSPERO_VOXCPM_PYTHON)")
    root.mkdir(parents=True, exist_ok=True)
    py = venv_python(root)
    pip = [str(py), "-m", "pip", "install", "--disable-pip-version-check"]
    steps = []
    if not py.is_file():
        say(0.05, f"creating the VoxCPM2 environment with {base}")
        steps.append(procutil.run([base, "-m", "venv", str(root / "venv")], text=True, timeout=600))
    if steps[-1:] and steps[-1].returncode != 0:
        raise EngineNotInstalled("voxcpm", (steps[-1].stderr or steps[-1].stdout or "")[-1500:] or "venv failed")
    say(0.15, "installing CUDA torch")
    steps.append(procutil.run([*pip, "torch", "torchaudio", "--index-url", TORCH_INDEX], text=True, timeout=3600))
    if steps[-1].returncode == 0:
        say(0.5, "installing voxcpm")
        steps.append(procutil.run([*pip, "voxcpm", "soundfile", "numpy"], text=True, timeout=3600))
    failed = next((s for s in steps if s.returncode != 0), None)
    if failed is not None:
        raise EngineNotInstalled("voxcpm", (failed.stderr or failed.stdout or "")[-1500:] or "install failed")
    say(0.8, "downloading the VoxCPM2 model")
    warm = run_worker(py, {"warmup": True, "model": MODEL, "cache_dir": str(root / "models"), "gpu": gpu_index()}, 3600)
    return {"engine_id": "voxcpm", "python": base, "venv": str(root / "venv"), "model": MODEL, "gpu": gpu_index(),
            "load_s": warm.get("load_s"), "sample_rate": warm.get("sample_rate"), "log_tail": (steps[-1].stdout or "")[-1200:]}


class VoxCPMEngine(TTSEngine):
    """VoxCPM2: multilingual (30 languages), 48 kHz, voice design from a description, cloning from a clip, chunked streaming."""

    id = "voxcpm"
    label = "VoxCPM2 (GPU, 30 languages, cloning)"
    pip_packages = ["voxcpm"]  # installed into its own environment, see install()
    capabilities = EngineCapabilities(languages=LANGUAGES, cloning=True, streaming=True, needs_gpu=True, multi_speaker=False,
                                      reference_optional=True)
    sidecar = True

    def __init__(self, voices_dir: Optional[Path] = None):
        self.root = env_dir(voices_dir)
        self.voices_dir = voices_dir
        self.last: dict[str, Any] = {}

    def is_installed(self) -> bool:
        venv = self.root / "venv"
        if not venv_python(self.root).is_file():
            return False
        return any(venv.glob("Lib/site-packages/voxcpm")) or any(venv.glob("lib/python3.*/site-packages/voxcpm"))

    def install_hint(self) -> str:
        return ("Install from the Voice screen: its own environment with Python 3.10-3.12, CUDA torch and voxcpm, "
                "plus the ~8 GB VoxCPM2 model; it runs on GPU " + gpu_index())

    def status(self) -> dict[str, Any]:
        result = super().status()
        result.update({"model": MODEL, "gpu": gpu_index(), "environment": str(self.root / "venv"),
                       "voice_ref": "empty, '(voice description)' or a path to a reference WAV", "last_run": self.last})
        return result

    def synthesize(self, text: str, voice_ref: Optional[str] = None, speed: Optional[float] = None,
                   pitch: Optional[float] = None, style: Optional[str] = None,
                   sample_path: Optional[Path] = None, language: Optional[str] = None,
                   prompt_text: Optional[str] = None, stream: bool = False, seed: Optional[int] = None) -> bytes:
        if not self.is_installed():
            raise EngineNotInstalled(self.id, self.install_hint())
        ref = (voice_ref or "").strip()
        reference = str(sample_path) if sample_path else (ref if ref and not ref.startswith("(") and Path(ref).is_file() else "")
        design = ref if ref.startswith("(") else (f"({style.strip()})" if style and style.strip() and not reference else "")
        if ref and not reference and not design:
            raise EngineNotInstalled(self.id, f"voice_ref '{ref}' is neither a reference WAV nor a '(description)'")
        line = f"{design}{text}" if design else text
        work = self.root / "work"
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as tmp:
            out = Path(tmp) / "line.wav"
            result = run_worker(venv_python(self.root), {
                "text": line, "out": str(out), "model": MODEL, "cache_dir": str(self.root / "models"), "gpu": gpu_index(),
                "reference": reference, "prompt_text": prompt_text or "", "stream": bool(stream), "seed": seed},
                timeout=300 + len(text) / 4)
            if not out.is_file():
                raise EngineNotInstalled(self.id, "VoxCPM2 wrote no audio")
            self.last = {k: result.get(k) for k in ("load_s", "seconds", "first_audio_s", "audio_s", "sample_rate")}
            return out.read_bytes()
