"""KittenTTS engine: voice/model resolution, status with its voices, the out-of-process worker contract (faked), the
install job going to its own environment, and the API listing it with the other engines."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import kitten_engine as ke
from prosperos_hoard import voice_engines as ve


def _fake_env(root: Path) -> None:
    py = ke.venv_python(root)
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    (root / "venv" / "Lib" / "site-packages" / "kittentts").mkdir(parents=True)


def test_voice_and_model_resolution(monkeypatch):
    monkeypatch.delenv("PROSPERO_KITTEN_MODEL", raising=False)
    assert ke.resolve_voice(None) == (ke.MODELS["mini"], "Jasper")
    assert ke.resolve_voice("luna") == (ke.MODELS["mini"], "Luna")
    assert ke.resolve_voice("nano/Kiki") == (ke.MODELS["nano"], "Kiki")
    assert ke.resolve_voice("KittenML/kitten-tts-micro-0.8") == ("KittenML/kitten-tts-micro-0.8", "Jasper")
    monkeypatch.setenv("PROSPERO_KITTEN_MODEL", "micro")
    assert ke.default_model() == ke.MODELS["micro"]
    with pytest.raises(ve.EngineNotInstalled, match="unknown KittenTTS voice"):
        ke.resolve_voice("Darth")


def test_status_and_registry(tmp_path):
    engine = ke.KittenTTSEngine(tmp_path)
    status = engine.status()
    assert status["installed"] is False and status["install_hint"] and "Jasper" in status["voices"]
    assert "nano/Luna" in status["voices"] and status["capabilities"]["needs_gpu"] is False
    _fake_env(ke.env_dir(tmp_path))
    assert engine.is_installed() is True
    ids = [e.id for e in ve.default_tts_engines(voices_dir=tmp_path)]
    assert ids[-1] == "kittentts" and "piper" in ids


def test_synthesize_runs_the_worker_out_of_process(tmp_path, monkeypatch):
    engine = ke.KittenTTSEngine(tmp_path)
    with pytest.raises(ve.EngineNotInstalled):
        engine.synthesize("hello")
    _fake_env(ke.env_dir(tmp_path))
    seen = {}

    def fake_worker(python, spec, timeout):
        seen.update(spec, python=str(python))
        Path(spec["out"]).write_bytes(ve.wav_bytes_mono16(np.zeros(2400, dtype="float32"), 24000))
        return {"ok": True}

    monkeypatch.setattr(ke, "run_worker", fake_worker)
    wav = engine.synthesize("Hello there.", voice_ref="nano/Luna", speed=1.2)
    assert wav[:4] == b"RIFF" and seen["voice"] == "Luna" and seen["model"] == ke.MODELS["nano"]
    assert seen["speed"] == 1.2 and Path(seen["python"]).stem == "python"
    with pytest.raises(ve.EngineNotInstalled, match="English only"):
        engine.synthesize("Hola", language="es")


def test_worker_result_parsing():
    assert ke._parse("noise\nKITTEN_RESULT {\"ok\": true, \"load_s\": 1.5}\n") == {"ok": True, "load_s": 1.5}
    assert ke._parse("nothing")["ok"] is False


def test_install_needs_a_compatible_python(tmp_path, monkeypatch):
    monkeypatch.setattr(ke, "find_base_python", lambda: None)
    with pytest.raises(ve.EngineNotInstalled, match="3.10, 3.11 or 3.12"):
        ke.install(tmp_path)


def test_install_job_goes_to_its_own_environment(tmp_path, monkeypatch):
    from prosperos_hoard import api

    calls = []
    monkeypatch.setattr(ke, "install", lambda voices_dir, progress=None: calls.append(voices_dir) or {"engine_id": "kittentts"})
    job = {"params": {"kind": "tts", "engine_id": "kittentts", "pip_packages": [ke.WHEEL], "sidecar": True,
                      "voices_dir": str(tmp_path)}}
    result = api._install_voice_engine_job(job, lambda *a: None)
    assert result == {"engine_id": "kittentts", "kind": "tts"} and calls == [tmp_path]


@pytest.mark.skipif(not ke.KittenTTSEngine(Path("data/voices")).is_installed(), reason="KittenTTS environment not installed")
def test_real_line_from_the_installed_environment():
    wav = ke.KittenTTSEngine(Path("data/voices")).synthesize("The render is ready.", voice_ref="nano/Bella")
    assert wav[:4] == b"RIFF" and len(wav) > 20000
