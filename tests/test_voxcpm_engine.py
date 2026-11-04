"""VoxCPM2 engine: status, voice design / reference resolution, the out-of-process worker contract (faked), the install
job routing. A real line runs only when the environment is installed."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import voice_engines as ve
from prosperos_hoard import voxcpm_engine as vx


def _fake_env(root: Path) -> None:
    (root / "venv" / "Scripts").mkdir(parents=True)
    (root / "venv" / "Scripts" / "python.exe").write_bytes(b"")
    (root / "venv" / "Lib" / "site-packages" / "voxcpm").mkdir(parents=True)


def test_status_and_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("PROSPERO_VOXCPM_GPU", raising=False)
    engine = vx.VoxCPMEngine(tmp_path)
    status = engine.status()
    assert status["installed"] is False and status["install_hint"] and status["gpu"] == "2"
    caps = status["capabilities"]
    assert caps["cloning"] is True and caps["streaming"] is True and caps["needs_gpu"] is True and "es" in caps["languages"]
    _fake_env(vx.env_dir(tmp_path))
    assert engine.is_installed() is True
    assert "voxcpm" in [e.id for e in ve.default_tts_engines(voices_dir=tmp_path)]


def test_synthesize_passes_design_reference_and_gpu(tmp_path, monkeypatch):
    engine = vx.VoxCPMEngine(tmp_path)
    with pytest.raises(ve.EngineNotInstalled):
        engine.synthesize("hola")
    _fake_env(vx.env_dir(tmp_path))
    monkeypatch.setenv("PROSPERO_VOXCPM_GPU", "3")
    seen = {}

    def fake_worker(python, spec, timeout):
        seen.update(spec)
        Path(spec["out"]).write_bytes(ve.wav_bytes_mono16(np.zeros(4800, dtype="float32"), 48000))
        return {"ok": True, "seconds": 1.5, "first_audio_s": 0.4, "audio_s": 0.1, "sample_rate": 48000}

    monkeypatch.setattr(vx, "run_worker", fake_worker)
    assert engine.synthesize("Hola, Luis.", voice_ref="(una mujer serena)")[:4] == b"RIFF"
    assert seen["text"] == "(una mujer serena)Hola, Luis." and seen["gpu"] == "3" and seen["reference"] == ""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    engine.synthesize("Hola", sample_path=ref, prompt_text="transcript", stream=True)
    assert seen["reference"] == str(ref) and seen["prompt_text"] == "transcript" and seen["stream"] is True
    assert engine.status()["last_run"]["first_audio_s"] == 0.4
    engine.synthesize("Hola", style="voz grave")
    assert seen["text"] == "(voz grave)Hola"
    with pytest.raises(ve.EngineNotInstalled, match="neither a reference WAV"):
        engine.synthesize("Hola", voice_ref="nowhere.wav")


def test_worker_result_parsing():
    assert vx._parse('noise\nVOXCPM_RESULT {"ok": true, "load_s": 2.0}\n') == {"ok": True, "load_s": 2.0}
    assert vx._parse("nothing")["ok"] is False


def test_install_needs_a_compatible_python(tmp_path, monkeypatch):
    monkeypatch.setattr(vx, "find_base_python", lambda: None)
    with pytest.raises(ve.EngineNotInstalled, match="3.10, 3.11 or 3.12"):
        vx.install(tmp_path)


def test_install_job_goes_to_its_own_environment(tmp_path, monkeypatch):
    from prosperos_hoard import api

    calls = []
    monkeypatch.setattr(vx, "install", lambda voices_dir, progress=None: calls.append(voices_dir) or {"engine_id": "voxcpm"})
    job = {"params": {"kind": "tts", "engine_id": "voxcpm", "pip_packages": ["voxcpm"], "sidecar": True, "voices_dir": str(tmp_path)}}
    assert api._install_voice_engine_job(job, lambda *a: None) == {"engine_id": "voxcpm", "kind": "tts"} and calls == [tmp_path]
