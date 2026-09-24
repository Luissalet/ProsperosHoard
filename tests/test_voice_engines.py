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


# ------------------------------------------------------------ regressions

@pytest.mark.parametrize("value,code", [
    ("es", "es"), ("ES", "es"), ("es-ES", "es"), ("es_MX", "es"), ("Spanish", "es"), ("español", "es"),
    ("Castellano", "es"), ("English", "en"), ("en-GB", "en"), ("zh-cn", "zh"), ("Chinese", "zh"),
    ("japonés", "ja"), ("pt-BR", "pt"),
])
def test_normalize_language(value, code):
    assert ve.normalize_language(value) == code


def test_normalize_language_auto_and_errors():
    assert ve.normalize_language(None) is None
    assert ve.normalize_language("") is None
    assert ve.normalize_language("auto", allow_auto=True) is None
    with pytest.raises(ve.UnsupportedLanguage):
        ve.normalize_language("auto")
    with pytest.raises(ve.UnsupportedLanguage):
        ve.normalize_language("Klingon")
    assert ve.language_name("es") == "Spanish"


def test_xtts_language_codes():
    assert ve.XTTSEngine.engine_language("zh") == "zh-cn"
    assert ve.XTTSEngine.engine_language("zh-cn") == "zh-cn"
    assert ve.XTTSEngine.engine_language("Spanish") == "es"
    assert ve.XTTSEngine.engine_language(None) == "en"
    with pytest.raises(ve.UnsupportedLanguage):
        ve.XTTSEngine.engine_language("sv")


@pytest.mark.parametrize("language,code", [(None, "a"), ("en", "a"), ("en-GB", "b"), ("es", "e"), ("Spanish", "e"),
                                           ("fr", "f"), ("hi", "h"), ("it", "i"), ("ja", "j"), ("pt", "p"),
                                           ("zh", "z"), ("e", "e")])
def test_kokoro_lang_codes(language, code):
    assert ve.KokoroEngine.lang_code(language) == code


def test_kokoro_rejects_unsupported_language():
    with pytest.raises(ve.UnsupportedLanguage):
        ve.KokoroEngine().check_language("de")


def test_kokoro_pipeline_is_cached_and_unloadable(monkeypatch):
    import sys
    import types

    built = []

    class KPipeline:
        def __init__(self, lang_code):
            built.append(lang_code)

        def __call__(self, text, voice, speed):
            yield None, None, np.zeros(240, dtype=np.float32) + (0.1 if voice == "ef_dora" else 0.0)

    monkeypatch.setitem(sys.modules, "kokoro", types.SimpleNamespace(KPipeline=KPipeline))
    monkeypatch.setattr(ve.KokoroEngine, "is_installed", lambda self: True)
    ve.unload_models()
    eng = ve.KokoroEngine()
    for _ in range(3):
        eng.synthesize("Hola.", language="es")
    eng.synthesize("Hello.", language="en")
    assert built == ["e", "a"]  # one pipeline per language, not one per sentence
    assert "kokoro:e" in ve.loaded_models()
    result = ve.unload_models()
    assert result["unloaded"] == 2 and ve.loaded_models() == []
    eng.synthesize("Hola.", language="es")
    assert built == ["e", "a", "e"]
    ve.unload_models()


def test_f5_gets_reference_text_and_is_cached(monkeypatch, tmp_path):
    import sys
    import types

    built, calls = [], []

    class F5TTS:
        def __init__(self):
            built.append(1)

        def infer(self, ref_file, ref_text, gen_text, speed):
            calls.append(ref_text)
            return np.zeros(100, dtype=np.float32), 24000, None

    monkeypatch.setitem(sys.modules, "f5_tts", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "f5_tts.api", types.SimpleNamespace(F5TTS=F5TTS))
    monkeypatch.setattr(ve.F5TTSEngine, "is_installed", lambda self: True)
    ve.unload_models()
    eng = ve.F5TTSEngine()
    eng.synthesize("one", sample_path=tmp_path / "s.wav", style="calm", ref_text="the sample says this")
    eng.synthesize("two", sample_path=tmp_path / "s.wav", style="calm")
    assert built == [1]
    assert calls == ["the sample says this", ""]  # never the style
    ve.unload_models()


def test_piper_rejects_path_like_voice_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(ve.PiperEngine, "is_installed", lambda self: True)
    for bad in ("../../etc/passwd", "a/b", "..\\x", ".hidden", ""):
        with pytest.raises(voices_mod.VoiceError):
            voices_mod.validate_voice_id(bad)
    with pytest.raises(voices_mod.VoiceError):
        ve.PiperEngine(tmp_path).synthesize("hi", voice_ref="../outside")
    assert voices_mod.validate_voice_id("es_ES-davefx-medium") == "es_ES-davefx-medium"


def _mock_client(monkeypatch, handler):
    import httpx

    real = httpx.Client
    monkeypatch.setattr(voices_mod.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_download_voice_rejects_truncated_file(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, headers={"content-length": "10"}, stream=httpx.ByteStream(b"abc"))

    _mock_client(monkeypatch, handler)
    with pytest.raises(voices_mod.VoiceError) as err:
        voices_mod.download_voice(tmp_path, "en_US-amy-medium")
    assert err.value.code == "voice_download_failed"
    assert list(tmp_path.iterdir()) == []  # no model, no leftover .part


def test_download_voice_wraps_errors_and_cleans_up(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(500)

    _mock_client(monkeypatch, handler)
    with pytest.raises(voices_mod.VoiceError):
        voices_mod.download_voice(tmp_path, "en_US-amy-medium")
    assert list(tmp_path.iterdir()) == []


def test_download_voice_writes_both_files(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(200, content=b"model-bytes")

    _mock_client(monkeypatch, handler)
    path = voices_mod.download_voice(tmp_path, "en_US-amy-medium")
    assert path.read_bytes() == b"model-bytes"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["en_US-amy-medium.onnx", "en_US-amy-medium.onnx.json"]
