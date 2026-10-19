"""Speech to text: Funes's Hoard first, the shared transcriber here when it is not running, and the transcript in Lumiere's shape."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from prosperos_hoard.editing.analysis import speech
from prosperos_hoard.editing.errors import TranscriberUnavailable
from prosperos_hoard.editing.hoard_link.media import stt


def shared(**over):
    base = {"ok": True, "via": "funes", "language": "es", "language_probability": 0.97, "duration_s": 3.0, "model": "small", "device": "cuda",
            "text": "hola mundo", "segments": [{"start_s": 0.1, "end_s": 2.0, "text": "hola mundo", "words": [
                {"start_s": 0.1, "end_s": 0.9, "word": " hola", "p": 0.91}, {"start_s": 1.0, "end_s": 1.0, "word": "mundo", "p": 0.8},
                {"start_s": 1.5, "end_s": 1.6, "word": "  ", "p": 0.5}]}]}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def forget_engines(monkeypatch):
    monkeypatch.setattr(speech, "_local", {})


def test_funes_does_the_work_and_the_transcript_keeps_lumieres_shape(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(speech.fam_media, "transcribe", lambda path, **kw: seen.update(path=path, **kw) or shared())
    steps = []
    out = speech.transcribe(tmp_path / "a.wav", model="", language="es", initial_prompt="Lumiere", progress=lambda p, d: steps.append((p, d)))
    assert seen["language"] == "es" and seen["model"] is None and seen["initial_prompt"] == "Lumiere" and seen["local_fallback"] is False
    assert out["words"] == [{"id": "w1", "t0": 100, "t1": 900, "text": "hola", "p": 0.91},
                            {"id": "w2", "t0": 1000, "t1": 1020, "text": "mundo", "p": 0.8}]        # blank word dropped, a word keeps 20 ms at least
    assert out["segments"] == [{"t0": 100, "t1": 2000, "text": "hola mundo"}]
    assert out["language"] == "es" and out["language_p"] == 0.97 and out["model"] == "small" and out["device"] == "cuda"
    assert steps and steps[-1][0] == 0.99


def test_a_long_job_is_followed_in_short_waits_and_cancel_stops_it_in_funes(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(speech.fam_media, "transcribe",
                        lambda path, **kw: {"ok": False, "kind": "timeout", "job_id": "j1", "still_running": True, "progress": 0.2, "via": "funes"})
    answers = iter([{"ok": False, "kind": "timeout", "job_id": "j1", "progress": 0.5}, shared()])
    monkeypatch.setattr(speech.fam_media, "transcribe_status", lambda job, wait_s=0: calls.append(("status", job, wait_s)) or next(answers))
    monkeypatch.setattr(speech.fam_media, "transcribe_cancel", lambda job: calls.append(("cancel", job)) or {"ok": True})
    steps = []
    out = speech.transcribe(tmp_path / "a.wav", progress=lambda p, d: steps.append(p))
    assert [c[0] for c in calls] == ["status", "status"] and out["words"] and 0.2 in steps and 0.5 in steps
    calls.clear()
    out = speech.transcribe(tmp_path / "a.wav", cancelled=lambda: True)
    assert calls == [("cancel", "j1")] and out["words"] == []                  # the remote job stops; the app's own job then reports "cancelled"


def test_a_job_funes_ran_and_failed_is_not_repeated_here(monkeypatch, tmp_path):
    monkeypatch.setattr(speech.fam_media, "transcribe", lambda path, **kw: {"ok": False, "kind": "tool_error", "error": "no_model", "via": "funes"})
    monkeypatch.setattr(speech.stt, "Transcriber", lambda **kw: pytest.fail("must not run locally"))
    with pytest.raises(TranscriberUnavailable, match="no_model"):
        speech.transcribe(tmp_path / "a.wav")


class FakeModel:
    def transcribe(self, audio, **kw):
        seg = lambda a, b, text, nsp=0.01, words=(): SimpleNamespace(start=a, end=b, text=text, words=list(words), avg_logprob=-0.2,
                                                                       no_speech_prob=nsp, compression_ratio=1.1)
        word = lambda a, b, w: SimpleNamespace(start=a, end=b, word=w, probability=0.9)
        info = SimpleNamespace(language="es", duration=6.0, language_probability=0.9)
        return iter([seg(0.0, 1.0, " Hola a todos", words=[word(0.0, 0.4, " Hola"), word(0.5, 1.0, " a todos")]),
                     seg(2.0, 4.0, " Subtítulos por la comunidad de Amara.org"),
                     seg(4.0, 5.0, " Gracias por ver el vídeo", nsp=0.95)]), info


def test_without_funes_the_shared_transcriber_runs_here_and_drops_whispers_inventions(monkeypatch, tmp_path):
    made = {}
    real = stt.Transcriber

    def build(**kw):
        made.update(kw)
        return real(model_factory=lambda *a: FakeModel(), lease=False, **kw)

    monkeypatch.setattr(speech.fam_media, "transcribe", lambda path, **kw: {"ok": False, "kind": "hub_down", "error": "hub unreachable", "via": "funes"})
    monkeypatch.setattr(speech.stt, "Transcriber", build)
    monkeypatch.setattr(speech.stt, "available", lambda: True)
    monkeypatch.setattr(speech.stt, "cuda_available", lambda: False)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    steps = []
    out = speech.transcribe(wav, language="es", progress=lambda p, d: steps.append(d))
    assert made["size"] == "small" and made["device"] == "auto" and made["owner"] == "lumiere"        # CPU default when there is no CUDA
    assert [w["text"] for w in out["words"]] == ["Hola", "a todos"] and [s["text"] for s in out["segments"]] == ["Hola a todos"]
    assert out["language"] == "es" and out["device"] in ("cpu", "cuda") and out["model"] == "small"
    assert any("small" in d for d in steps)
    again = speech.transcribe(wav, language="es")
    assert again["words"] and len(speech._local) == 1                                                  # the model stays loaded


def test_the_gpu_model_is_the_default_when_cuda_exists(monkeypatch, tmp_path):
    made = {}
    monkeypatch.setattr(speech.fam_media, "transcribe", lambda path, **kw: {"ok": False, "kind": "app_down", "error": "x", "via": "funes"})
    monkeypatch.setattr(speech.stt, "available", lambda: True)
    monkeypatch.setattr(speech.stt, "cuda_available", lambda: True)

    class Stub:
        device_used = "cuda"

        def __init__(self, **kw):
            made.update(kw)

        def transcribe(self, path, **kw):
            return SimpleNamespace(as_dict=lambda: shared(model=made["size"]))

    monkeypatch.setattr(speech.stt, "Transcriber", Stub)
    assert speech.transcribe(tmp_path / "a.wav")["model"] == "large-v3-turbo"
    assert speech.transcribe(tmp_path / "a.wav", device="cpu")["model"] == "small"
    assert speech.transcribe(tmp_path / "a.wav", model="medium", device="cuda")["model"] == "medium"


def test_cancelling_a_local_transcription_returns_an_empty_result(monkeypatch, tmp_path):
    monkeypatch.setattr(speech.fam_media, "transcribe", lambda path, **kw: {"ok": False, "kind": "tool_missing", "error": "x", "via": "funes"})
    monkeypatch.setattr(speech.stt, "available", lambda: True)
    monkeypatch.setattr(speech.stt, "cuda_available", lambda: False)
    real = stt.Transcriber
    monkeypatch.setattr(speech.stt, "Transcriber", lambda **kw: real(model_factory=lambda *a: FakeModel(), lease=False, **kw))
    out = speech.transcribe(tmp_path / "a.wav", cancelled=lambda: True)
    assert out["words"] == [] and out["segments"] == []


def test_nothing_to_transcribe_with_says_how_to_fix_it(monkeypatch, tmp_path):
    monkeypatch.setattr(speech.fam_media, "transcribe", lambda path, **kw: {"ok": False, "kind": "hub_down", "error": "hub unreachable", "via": "funes"})
    monkeypatch.setattr(speech.stt, "available", lambda: False)
    with pytest.raises(TranscriberUnavailable, match="pip install faster-whisper"):
        speech.transcribe(tmp_path / "a.wav")
    monkeypatch.setattr(speech.stt, "available", lambda: True)
    monkeypatch.setattr(speech.stt, "cuda_available", lambda: False)

    class Boom:
        device_used = ""

        def __init__(self, **kw):
            pass

        def transcribe(self, *a, **kw):
            raise RuntimeError("cublas64_12.dll is not found")

    monkeypatch.setattr(speech.stt, "Transcriber", Boom)
    with pytest.raises(TranscriberUnavailable, match="cublas"):
        speech.transcribe(tmp_path / "a.wav")


def test_engine_status_reports_funes_and_the_local_model(monkeypatch):
    monkeypatch.setattr(speech.stt, "available", lambda: False)
    monkeypatch.setattr(speech.fam_media, "available", lambda service="media", timeout=1.0: False)
    status = speech.engine_status()
    assert status["available"] is False and "Funes" in status["reason"]
    monkeypatch.setattr(speech.fam_media, "available", lambda service="media", timeout=1.0: service == "stt")
    assert speech.engine_status()["engine"] == "funes"
    monkeypatch.setattr(speech.fam_media, "available", lambda service="media", timeout=1.0: False)
    monkeypatch.setattr(speech.stt, "available", lambda: True)
    monkeypatch.setattr(speech.stt, "cuda_available", lambda: True)
    assert speech.engine_status() == {"available": True, "engine": "faster-whisper", "funes": False, "local": True, "cuda_devices": 1, "cuda_libraries": True}


def test_a_model_already_in_the_hugging_face_cache_is_reused(monkeypatch, tmp_path):
    shared_dir, hf = tmp_path / "hoard", tmp_path / "hf"
    snap = hf / "hub" / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"x")
    monkeypatch.setenv("HF_HOME", str(hf))
    monkeypatch.setattr(speech.stt, "default_models_dir", lambda: shared_dir)
    assert speech._models_dir("small") == hf / "hub"
    assert speech._models_dir("medium") == shared_dir
