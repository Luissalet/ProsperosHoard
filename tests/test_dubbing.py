from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prosperos_hoard import dubbing as db
from prosperos_hoard import voice_engines as ve
from prosperos_hoard.hoard_link.errors import Unavailable
from prosperos_hoard.jobs import Progress
from prosperos_hoard.store import Store

HAS_FFMPEG = shutil.which("ffmpeg") is not None


class FakeTTS(ve.TTSEngine):
    id = "fake-tts"
    label = "Fake"
    capabilities = ve.EngineCapabilities(languages=["es"], cloning=False)

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        n = max(1, len(text)) * 400
        samples = (np.sin(np.linspace(0, 40, n)) * 0.2).astype("float32")
        return ve.wav_bytes_mono16(samples, 16000)


class FakeSTT(ve.STTEngine):
    id = "fake-stt"
    label = "Fake"
    capabilities = ve.EngineCapabilities()

    def is_installed(self):
        return True

    def transcribe(self, path, language=None, word_timestamps=True):
        return {
            "language": "en", "text": "hello there. general kenobi.",
            "segments": [
                {"start_s": 0.0, "end_s": 1.5, "text": "hello there", "words": []},
                {"start_s": 2.0, "end_s": 3.5, "text": "general kenobi", "words": []},
            ],
        }


def _fake_chat(reply_map: dict[str, str]):
    def chat_fn(messages: list[dict[str, Any]]) -> str:
        user_text = messages[1]["content"].lower()
        for key, reply in reply_map.items():
            if key in user_text:
                return reply
        return "??"
    return chat_fn


class FakeSync:
    def __init__(self, chat_fn=None, raise_unavailable=False):
        self._chat_fn = chat_fn
        self._raise = raise_unavailable

    def chat(self, messages, max_tokens=None, temperature=None):
        if self._raise:
            raise Unavailable("llm", ["no local LLM reachable"])

        class R:
            pass

        r = R()
        r.text = self._chat_fn(messages)
        return r


class FakeLink:
    def __init__(self, sync):
        self.sync = sync


class FakeBackend:
    def __init__(self, chat_fn=None, raise_unavailable=False):
        self.link = FakeLink(FakeSync(chat_fn, raise_unavailable))


def _tiny_video(path: Path) -> None:
    from prosperos_hoard import procutil
    from prosperos_hoard.backend import ffmpeg_path

    exe = ffmpeg_path()
    cmd = [exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=160x90:rate=10",
           "-f", "lavfi", "-i", "sine=frequency=300:duration=4", "-shortest", "-c:v", "libx264", "-pix_fmt",
           "yuv420p", "-c:a", "aac", str(path)]
    proc = procutil.run(cmd, timeout=60)
    assert proc.returncode == 0, proc.stderr


# --------------------------------------------------------------- time-fit math

@pytest.mark.parametrize("factor", [0.5, 0.9, 1.0, 1.3, 2.5, 3.9, 6.0, 0.2, 0.1])
def test_atempo_chain_product_matches_clamped_factor(factor):
    chain = db.atempo_chain(factor)
    product = 1.0
    for stage in chain:
        value = float(stage.split("=")[1])
        assert db.MIN_ATEMPO <= value <= db.MAX_ATEMPO
        product *= value
    clamped = max(1.0 / db.MAX_OVERALL_FACTOR, min(db.MAX_OVERALL_FACTOR, factor))
    assert product == pytest.approx(clamped, rel=1e-3)


def test_atempo_chain_identity():
    assert db.atempo_chain(1.0) == []


def test_compute_time_fit_factor():
    assert db.compute_time_fit_factor(4.0, 2.0) == 2.0
    assert db.compute_time_fit_factor(0.0, 2.0) == 1.0
    assert db.compute_time_fit_factor(2.0, 0.0) == 1.0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_fit_audio_to_duration_hits_target(tmp_path):
    from prosperos_hoard import audio as audio_mod

    src = tmp_path / "src.wav"
    from prosperos_hoard import procutil
    from prosperos_hoard.backend import ffmpeg_path

    proc = procutil.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                        "-ar", "16000", str(src)], timeout=30)
    assert proc.returncode == 0
    dest = tmp_path / "fit.wav"
    info = db.fit_audio_to_duration(src, 1.0, dest)
    assert dest.is_file()
    got = audio_mod.probe_duration_s(dest)
    assert got == pytest.approx(1.0, abs=0.05)
    assert info["applied_factor"] == pytest.approx(2.0, rel=1e-2)


# ------------------------------------------------------------ mix filter build

def test_build_dub_mix_filter_shape():
    segments = [{"start_s": 1.0, "end_s": 2.0}, {"start_s": 3.0, "end_s": 4.5}]
    filt = db.build_dub_mix_filter(segments)
    assert "[0:a]volume=enable=" in filt
    assert "[1:a]adelay=1000|1000[d0]" in filt
    assert "[2:a]adelay=3000|3000[d1]" in filt
    assert filt.endswith("amix=inputs=3:normalize=0[mixed]")


def test_build_dub_mix_filter_no_segments():
    assert db.build_dub_mix_filter([]) == "[0:a]anull[mixed]"


# ------------------------------------------------------------------ translation

def test_build_translation_messages_includes_glossary_and_language():
    msgs = db.build_translation_messages("Hello", "French", "en", {"Bob": "Roberto"})
    system = msgs[0]["content"]
    assert "French" in system
    assert "Bob -> Roberto" in system
    assert msgs[1]["content"] == "Hello"


def test_translate_segments_uses_chat_fn_per_segment():
    chat_fn = _fake_chat({"hello": "hola", "kenobi": "kenobi es"})
    out = db.translate_segments(chat_fn, [{"text": "hello there"}, {"text": "general kenobi"}], "Spanish")
    assert out == ["hola", "kenobi es"]


def test_translate_segments_falls_back_to_source_on_empty_reply():
    out = db.translate_segments(lambda m: "", [{"text": "keep me"}], "Spanish")
    assert out == ["keep me"]


def test_translate_segments_propagates_unavailable():
    def chat_fn(messages):
        raise Unavailable("llm", ["no local LLM reachable"])

    with pytest.raises(Unavailable):
        db.translate_segments(chat_fn, [{"text": "hi"}], "Spanish")


# ------------------------------------------------------------------------ job

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_job_end_to_end_with_fakes(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    video_path = tmp_path / "clip.mp4"
    _tiny_video(video_path)

    monkeypatch.setattr(ve, "default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])
    backend = FakeBackend(_fake_chat({"hello": "hola ahi", "kenobi": "general kenobi en espanol"}))

    job = store.create_job("dub", "cpu", {
        "video_path": str(video_path), "target_language": "Spanish", "voice": {"engine_id": "fake-tts"},
        "title": "Test dub",
    })
    outputs = db.dub_job(store, backend, job, Progress(store, job["id"]))

    assert outputs["title"] == "Test dub"
    final_video = tmp_path / "data" / outputs["final_video"]
    assert final_video.is_file() and final_video.stat().st_size > 0
    subs = (tmp_path / "data" / outputs["subtitles"]).read_text(encoding="utf-8")
    assert "hola ahi" in subs
    assert len(outputs["segments"]) == 2
    assert outputs["segments"][0]["translated_text"] == "hola ahi"
    assert outputs["segments"][0]["source_text"] == "hello there"
    assert "fit" in outputs["segments"][0]

    manifest_path = tmp_path / "data" / outputs["manifest"]
    assert manifest_path.is_file()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_job_saves_project_asset(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    project = store.create_project("Dub Project")
    video_path = tmp_path / "clip.mp4"
    _tiny_video(video_path)
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])
    backend = FakeBackend(_fake_chat({"hello": "hola", "kenobi": "kenobi"}))

    job = store.create_job("dub", "cpu", {
        "video_path": str(video_path), "target_language": "Spanish", "voice": {"engine_id": "fake-tts"},
        "project_id": project["id"],
    })
    outputs = db.dub_job(store, backend, job, Progress(store, job["id"]))
    assert outputs["asset_ids"]
    asset = store.get_asset(outputs["asset_ids"][0])
    assert asset["kind"] == "video"
    assert asset["recipe"]["operation"] == "dub"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_job_fails_clearly_without_llm(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    video_path = tmp_path / "clip.mp4"
    _tiny_video(video_path)
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])
    backend = FakeBackend(raise_unavailable=True)

    job = store.create_job("dub", "cpu", {
        "video_path": str(video_path), "target_language": "Spanish", "voice": {"engine_id": "fake-tts"},
    })
    with pytest.raises(Unavailable, match="llm"):
        db.dub_job(store, backend, job, Progress(store, job["id"]))
    # the extraction + transcription stages already ran and left artefacts
    work_dirs = list((tmp_path / "data" / "voice_studio" / "dub").glob("*/original.wav"))
    assert work_dirs


def test_dub_job_requires_stt_engine(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [])
    job = store.create_job("dub", "cpu", {"video_path": "/does/not/exist.mp4", "target_language": "fr",
                                          "voice": {"engine_id": "x"}})
    with pytest.raises(db.DubbingError):
        db.dub_job(store, FakeBackend(), job, Progress(store, job["id"]))


def test_dub_job_missing_video_raises(tmp_path):
    store = Store(tmp_path / "data")
    job = store.create_job("dub", "cpu", {"video_path": "/does/not/exist.mp4", "target_language": "fr",
                                          "voice": {"engine_id": "x"}})
    with pytest.raises(db.DubbingError):
        db.dub_job(store, FakeBackend(), job, Progress(store, job["id"]))


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_resynthesize_segment_updates_text_and_remixes(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    video_path = tmp_path / "clip.mp4"
    _tiny_video(video_path)
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])
    backend = FakeBackend(_fake_chat({"hello": "hola", "kenobi": "kenobi"}))

    job = store.create_job("dub", "cpu", {"video_path": str(video_path), "target_language": "Spanish",
                                          "voice": {"engine_id": "fake-tts"}})
    outputs = db.dub_job(store, backend, job, Progress(store, job["id"]))
    work_dir = tmp_path / "data" / outputs["work_dir"]

    result = db.resynthesize_segment(store, backend, work_dir, 0, new_text="saludos")
    assert result["segment"]["translated_text"] == "saludos"
    assert "final_video" in result
    manifest = db.load_manifest(work_dir)
    assert manifest["segments"][0]["translated_text"] == "saludos"
    assert manifest["segments"][1]["translated_text"] == "kenobi"  # untouched


def test_resynthesize_segment_bad_index(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "manifest.json").write_text(
        '{"title": "t", "voice": {}, "video_path": "x", "segments": [{"index": 0, "start_s": 0, "end_s": 1, '
        '"source_text": "a", "translated_text": "a"}]}',
        encoding="utf-8",
    )
    store = Store(tmp_path / "data")
    with pytest.raises(db.DubbingError):
        db.resynthesize_segment(store, FakeBackend(), work_dir, 5)


def test_demucs_installed_false_when_not_present():
    assert db.demucs_installed() is False
    assert db.separate_background(Path("/nonexistent.wav"), Path("/tmp/does-not-matter")) is None
