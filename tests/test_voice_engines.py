from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import voice_engines as ve
from prosperos_hoard import voices as voices_mod


def _sine_wav(seconds: float = 1.0, freq: float = 220.0, sr: int = 16000) -> bytes:
    t = np.linspace(0, seconds, int(sr * seconds), endpoint=False)
    samples = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return ve.wav_bytes_mono16(samples, sr)


def test_wav_bytes_mono16_roundtrip():
    data = _sine_wav(0.5, sr=8000)
    with wave.open(io.BytesIO(data), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 8000
        assert wf.getnframes() == 4000


def test_capabilities_to_dict():
    caps = ve.EngineCapabilities(languages=["en", "es"], cloning=True, streaming=False, needs_gpu=True,
                                 multi_speaker=False)
    assert caps.to_dict() == {"languages": ["en", "es"], "cloning": True, "streaming": False, "needs_gpu": True,
                              "multi_speaker": False}


def test_default_registries_contents():
    tts = ve.default_tts_engines()
    stt = ve.default_stt_engines()
    assert {e.id for e in tts} == {"piper", "xtts", "f5-tts", "kokoro", "chatterbox", "comfy-tts"}
    assert {e.id for e in stt} == {"faster-whisper", "whisper"}


def test_piper_engine_wraps_voices_module(tmp_path):
    eng = ve.PiperEngine(tmp_path)
    assert eng.is_installed() == voices_mod.piper_installed()
    assert eng.capabilities.cloning is False
    assert "es_ES" in eng.capabilities.languages


def test_engine_not_installed_reports_hint_and_never_imports_heavy_deps():
    # None of these packages are expected to be installed in the test
    # environment; is_installed() must be a cheap check (no import error),
    # never raise, and the class must be constructible with zero side effects.
    for cls in (ve.XTTSEngine, ve.F5TTSEngine, ve.KokoroEngine, ve.ChatterboxEngine):
        eng = cls()
        status = eng.status()
        assert status["installed"] is False
        assert status["install_hint"]
        assert "not installed" in status["reason"]
        with pytest.raises(ve.EngineNotInstalled):
            eng.synthesize("hello")


def test_comfy_tts_engine_status_only():
    eng = ve.ComfyTTSEngine(object_info={"SomeVibeVoiceNode": {}, "Other": {}})
    assert eng.is_installed() is True
    assert "VibeVoice" in eng.status()["reason"] or "SomeVibeVoiceNode" in eng.status()["reason"]
    eng_off = ve.ComfyTTSEngine(object_info={"Unrelated": {}})
    assert eng_off.is_installed() is False
    with pytest.raises(NotImplementedError):
        eng_off.synthesize("x")


def test_get_engine_and_best_installed():
    class FakeInstalled(ve.TTSEngine):
        id = "fake-installed"
        capabilities = ve.EngineCapabilities(cloning=True)

        def is_installed(self) -> bool:
            return True

        def synthesize(self, *a, **k):
            return b""

    class FakeMissing(ve.TTSEngine):
        id = "fake-missing"

        def is_installed(self) -> bool:
            return False

    engines = [FakeMissing(), FakeInstalled()]
    assert ve.get_engine(engines, "fake-installed") is engines[1]
    with pytest.raises(KeyError):
        ve.get_engine(engines, "nope")

    assert ve.best_installed_tts(engines) is engines[1]
    assert ve.best_installed_tts(engines, needs_cloning=True) is engines[1]
    assert ve.best_installed_tts([FakeMissing()]) is None
    assert ve.best_installed_tts(engines, prefer="fake-installed") is engines[1]


def test_list_engine_status_shape():
    status = ve.list_engine_status(ve.default_tts_engines(), ve.default_stt_engines())
    assert set(status) == {"tts", "stt"}
    for item in status["tts"] + status["stt"]:
        assert {"id", "label", "kind", "installed", "capabilities", "install_hint", "reason"} <= set(item)


def test_install_engine_runs_pip(monkeypatch):
    calls = []

    class FakeCompleted:
        returncode = 0
        stdout = "Successfully installed foo"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted()

    monkeypatch.setattr(ve.procutil, "run", fake_run)

    class FakeEngine:
        id = "fake"
        pip_packages = ["fake-pkg==1.0"]

        def is_installed(self):
            return True

    result = ve.install_engine(FakeEngine())
    assert result["ok"] is True
    assert result["installed"] is True
    assert calls[0][-2:] == ["fake-pkg==1.0"] or "fake-pkg==1.0" in calls[0]
    assert "-m" in calls[0] and "pip" in calls[0] and "install" in calls[0]


def test_install_engine_without_packages_raises():
    class NoPip:
        id = "nopip"
        pip_packages: list = []

    with pytest.raises(ve.EngineNotInstalled):
        ve.install_engine(NoPip())


def test_segments_to_srt_vtt_txt():
    segments = [{"start_s": 0.0, "end_s": 1.5, "text": "Hello there."},
                {"start_s": 2.0, "end_s": 3.25, "text": "General Kenobi."}]
    srt = ve.segments_to_srt(segments)
    assert srt.splitlines()[:3] == ["1", "00:00:00,000 --> 00:00:01,500", "Hello there."]
    assert "2\n00:00:02,000 --> 00:00:03,250\nGeneral Kenobi." in srt

    vtt = ve.segments_to_vtt(segments)
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nHello there.")

    txt = ve.segments_to_txt(segments)
    assert txt == "Hello there.\nGeneral Kenobi."


def test_srt_time_carries_milliseconds_into_seconds():
    # 59.9996s must round to the next second (60,000ms), not to an invalid
    # "59,1000" (1000 is not a valid millisecond field).
    srt = ve.segments_to_srt([{"start_s": 59.9996, "end_s": 60.9996, "text": "x"}])
    header = srt.splitlines()[1]
    assert header == "00:01:00,000 --> 00:01:01,000"
    assert ",1000" not in srt


def test_vtt_time_carries_seconds_into_minutes():
    # 119.9997s must round to 02:00.000, not to an invalid "01:60.000"
    # (60 is not a valid seconds field).
    vtt = ve.segments_to_vtt([{"start_s": 119.9997, "end_s": 120.9997, "text": "x"}])
    header = vtt.splitlines()[2]
    assert header == "00:02:00.000 --> 00:02:01.000"
    assert ":60." not in vtt


def test_srt_time_carries_seconds_into_minutes_at_hour_boundary():
    # 3599.9996s must round to 01:00:00,000, not "00:59:60,000" or "00:59:59,1000".
    assert ve._srt_time(3599.9996) == "01:00:00,000"


def test_transcript_segment_to_dict():
    seg = ve.TranscriptSegment(0.111, 1.999, "hi", words=[{"start_s": 0.1, "end_s": 0.2, "word": "hi"}])
    d = seg.to_dict()
    assert d["start_s"] == 0.111 and d["end_s"] == 1.999 and d["text"] == "hi" and d["words"]


# ------------------------------------------------------ real, optional tests

@pytest.mark.skipif(not voices_mod.piper_installed(), reason="piper-tts not installed")
def test_real_piper_synthesis(tmp_path):
    eng = ve.PiperEngine(tmp_path / "voices")
    assert eng.is_installed()
    wav = eng.synthesize("Testing the voice studio.", voice_ref="en_US-amy-medium")
    assert wav[:4] == b"RIFF"
    with wave.open(io.BytesIO(wav), "rb") as wf:
        assert wf.getnframes() > 0


@pytest.mark.skipif(not ve.FasterWhisperEngine().is_installed(), reason="faster-whisper not installed")
def test_real_faster_whisper_transcription(tmp_path):
    wav_path = tmp_path / "clip.wav"
    wav_path.write_bytes(_sine_wav(1.0))
    eng = ve.FasterWhisperEngine(model_size="tiny")
    result = eng.transcribe(wav_path, language="en", word_timestamps=False)
    assert "segments" in result and isinstance(result["text"], str)
