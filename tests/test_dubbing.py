from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prosperos_hoard.backend import ffmpeg_path
from prosperos_hoard import dubbing as db
from prosperos_hoard import voice_engines as ve
from prosperos_hoard.hoard_link.errors import Unavailable
from prosperos_hoard.jobs import Progress
from prosperos_hoard.store import Store

HAS_FFMPEG = ffmpeg_path() is not None


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


def _probe_sample_rate(path: Path) -> int:
    # `ffmpeg -i` rather than ffprobe: the bundled imageio-ffmpeg build ships no ffprobe
    import re

    from prosperos_hoard import procutil

    proc = procutil.run([ffmpeg_path(), "-hide_banner", "-nostdin", "-i", str(path)], text=True, timeout=30)
    m = re.search(r"Audio: [^\n]*?(\d+) Hz", proc.stderr or "")
    assert m, proc.stderr
    return int(m.group(1))


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_mix_dub_audio_normalises_mismatched_sample_rates(tmp_path):
    """The original track and each TTS engine's raw output can each be a
    different sample rate (e.g. 44100 vs 24000); left to ffmpeg's default
    filtergraph negotiation this does not fail but can silently resample
    the whole mix to an unrelated, much higher rate. The mixed output must
    land on the fixed, predictable MIX_SAMPLE_RATE instead."""
    from prosperos_hoard import procutil
    from prosperos_hoard.backend import ffmpeg_path

    original = tmp_path / "original.wav"
    clip = tmp_path / "clip.wav"
    proc = procutil.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=frequency=300:duration=2", "-ar", "44100", "-ac", "1", str(original)], timeout=30)
    assert proc.returncode == 0
    proc = procutil.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=frequency=500:duration=2", "-ar", "24000", "-ac", "1", str(clip)], timeout=30)
    assert proc.returncode == 0

    out = tmp_path / "mixed.wav"
    db.mix_dub_audio(original, [clip], [{"start_s": 0.0, "end_s": 2.0}], out)
    assert out.is_file()
    assert _probe_sample_rate(out) == db.MIX_SAMPLE_RATE


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


@pytest.mark.parametrize("reply,expected", [
    ('"Hola mundo"', "Hola mundo"),
    ("'Hola mundo'", "Hola mundo"),
    ("“Hola mundo”", "Hola mundo"),  # curly double quotes
    ('Dijo "hola" ayer', 'Dijo "hola" ayer'),  # an inner quote is not a wrapping pair - kept as-is
])
def test_translate_segments_strips_wrapping_quotes_despite_prompt(reply, expected):
    # the system prompt asks for "no quotes" but a real model sometimes
    # wraps its reply anyway; a quote that is only part of the dialogue
    # itself (not wrapping the whole line) must be left alone.
    out = db.translate_segments(lambda m: reply, [{"text": "source"}], "Spanish")
    assert out == [expected]


def test_translate_segments_collapses_embedded_newlines():
    # a garbled multi-line reply must not leave a blank line embedded in
    # the translated text, which would break the SRT block format.
    out = db.translate_segments(lambda m: "Hola\n\nmundo", [{"text": "source"}], "Spanish")
    assert out == ["Hola mundo"]


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


# ------------------------------------------------------------ regressions

def _video(path: Path, seconds: float) -> None:
    from prosperos_hoard import procutil

    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i",
           f"testsrc=duration={seconds}:size=160x90:rate=10", "-f", "lavfi", "-i",
           f"sine=frequency=300:duration={seconds}", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", str(path)]
    proc = procutil.run(cmd, timeout=60)
    assert proc.returncode == 0, proc.stderr


def _tone_wav(path: Path, seconds: float, sr: int = 44100, lead_silence_s: float = 0.0,
              tail_silence_s: float = 0.0) -> None:
    n = int(seconds * sr)
    tone = (0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / sr)).astype(np.float32)
    samples = np.concatenate([np.zeros(int(lead_silence_s * sr), np.float32), tone,
                              np.zeros(int(tail_silence_s * sr), np.float32)])
    path.write_bytes(ve.wav_bytes_mono16(samples, sr))


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    import wave

    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    return data, sr


def test_duck_gain_envelope():
    gain = db.duck_gain(0, 1000, [(400, 600)], 0.1, ramp=100)
    assert gain[0] == 1.0 and gain[299] == 1.0
    assert gain[450] == pytest.approx(0.1)
    assert 0.1 < gain[350] < 1.0  # ramping down
    assert 0.1 < gain[650] < 1.0  # ramping back up
    assert gain[800] == 1.0
    # a chunk that starts mid-span sees the same envelope
    tail = db.duck_gain(500, 500, [(400, 600)], 0.1, ramp=100)
    assert np.allclose(tail, gain[500:])


def test_trim_silence_drops_padding():
    sr = 1000
    samples = np.concatenate([np.zeros(300), np.ones(100) * 0.5, np.zeros(400)]).astype(np.float32)
    out = db.trim_silence(samples, sr, keep_s=0.0)
    assert out.size == 100
    assert db.trim_silence(np.zeros(50, np.float32), sr).size == 0


@pytest.mark.parametrize("requested,applied", [(0.1667, 0.9), (0.95, 0.95), (1.0, 1.0), (1.8, 1.8), (9.0, 4.0)])
def test_applied_fit_factor_never_slows_a_short_line_much(requested, applied):
    assert db.applied_fit_factor(requested) == pytest.approx(applied)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_fit_short_line_is_padded_not_slowed(tmp_path):
    # a 0.5 s line (plus the engine's own silence padding) in a 3 s slot:
    # never stretched to fill it, just trimmed, kept at its pace and padded
    src = tmp_path / "si.wav"
    _tone_wav(src, 0.5, lead_silence_s=0.4, tail_silence_s=0.6)
    dest = tmp_path / "fit.wav"
    info = db.fit_audio_to_duration(src, 3.0, dest)
    assert info["source_duration_s"] == pytest.approx(0.56, abs=0.05)  # silence trimmed (plus a small margin)
    assert info["applied_factor"] >= db.MIN_FIT_FACTOR
    samples, sr = _read_wav(dest)
    assert sr == db.MIX_SAMPLE_RATE
    assert samples.size == pytest.approx(3.0 * sr, abs=2)
    loud = np.flatnonzero(np.abs(samples) > 0.01)
    assert loud[0] < 0.05 * sr  # the line starts on the segment's start, not after the engine's silence
    assert loud[-1] < 0.75 * sr  # ...and is not stretched over the whole slot


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_fit_long_line_is_sped_up_past_two_x(tmp_path):
    src = tmp_path / "long.wav"
    _tone_wav(src, 3.0)
    dest = tmp_path / "fit.wav"
    info = db.fit_audio_to_duration(src, 1.0, dest)
    assert info["applied_factor"] == pytest.approx(3.0, rel=0.02)
    samples, sr = _read_wav(dest)
    assert samples.size == pytest.approx(sr, abs=2)
    assert np.abs(samples[: int(0.95 * sr)]).max() > 0.1  # filled with speech, not truncated silence


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_mix_many_segments_keeps_length_and_places_clips(tmp_path):
    # hundreds of segments: one ffmpeg input per clip used to hit command
    # line / file descriptor limits; the numpy mix has no such limit
    sr = db.MIX_SAMPLE_RATE
    original = tmp_path / "original.wav"
    original.write_bytes(ve.wav_bytes_mono16(np.full(sr * 30, 0.2, np.float32), sr))
    segments, clips = [], []
    for i in range(300):
        start = i * 0.1
        clip = tmp_path / f"c{i:03d}.wav"
        clip.write_bytes(ve.wav_bytes_mono16(np.full(int(0.05 * sr), 0.5, np.float32), sr))
        segments.append({"start_s": start, "end_s": start + 0.05})
        clips.append(clip)
    out = tmp_path / "mixed.wav"
    db.mix_dub_audio(original, clips, segments, out)
    samples, out_sr = _read_wav(out)
    assert out_sr == sr
    assert samples.size == pytest.approx(sr * 30, rel=0.001)


def test_mix_numpy_core_ducks_and_adds(tmp_path, monkeypatch):
    # the mix itself, without loudnorm: the final ffmpeg pass is replaced
    # by one that captures the raw float mix it would have encoded
    sr = 1000
    original = tmp_path / "bg.wav"
    original.write_bytes(ve.wav_bytes_mono16(np.full(sr * 3, 0.5, np.float32), sr))
    clip = tmp_path / "clip.wav"
    clip.write_bytes(ve.wav_bytes_mono16(np.full(sr, 0.25, np.float32), sr))
    captured = {}

    def fake_stream(path, sr_, chunk):
        data, _ = _read_wav(path)
        for i in range(0, data.size, chunk):
            yield data[i:i + chunk]

    class Done:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        raw = Path(kw["cwd"]) / cmd[cmd.index("-i") + 1]
        captured["mix"] = np.frombuffer(raw.read_bytes(), dtype="<f4").copy()
        (Path(kw["cwd"]) / cmd[-1]).write_bytes(b"x")
        return Done()

    monkeypatch.setattr(db, "_stream_decode", fake_stream)
    monkeypatch.setattr(db.procutil, "run", fake_run)
    monkeypatch.setattr(db, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(db, "MIX_CHUNK_S", 0.7)  # the clip straddles chunk boundaries
    db.mix_dub_audio(original, [clip], [{"start_s": 1.0, "end_s": 2.0}], tmp_path / "out.wav", duck_db=-20.0,
                     sample_rate=sr)
    mix = captured["mix"]
    assert mix.size == 3 * sr
    assert mix[100] == pytest.approx(0.5, abs=1e-3)  # untouched before the line
    assert mix[1500] == pytest.approx(0.5 * 0.1 + 0.25, abs=2e-3)  # ducked by 20 dB plus the dub line
    assert mix[2900] == pytest.approx(0.5, abs=1e-3)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_mux_keeps_full_video_length_with_early_subtitles(tmp_path):
    from prosperos_hoard import audio as audio_mod

    video = tmp_path / "six.mp4"
    _video(video, 6)
    audio = tmp_path / "dub.wav"
    audio.write_bytes(ve.wav_bytes_mono16(np.zeros(44100 * 6, np.float32), 44100))
    srt = tmp_path / "subs.srt"
    srt.write_text(ve.segments_to_srt([{"start_s": 0.0, "end_s": 1.0, "text": "hola"}]), encoding="utf-8")
    out = tmp_path / "out.mp4"
    db.mux_video(video, audio, out, srt)
    assert audio_mod.probe_duration_s(out) == pytest.approx(6.0, abs=0.3)


class RecordingTTS(FakeTTS):
    calls: list[dict[str, Any]] = []

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        RecordingTTS.calls.append({"text": text, "language": language, "voice_ref": voice_ref, "id": self.id})
        return super().synthesize(text, voice_ref, speed, pitch, style, sample_path, language)


@pytest.mark.parametrize("value,code", [("Spanish", "es"), ("español", "es"), ("es-ES", "es"), ("fr", "fr"),
                                        ("zh-cn", "zh"), ("Japanese", "ja")])
def test_target_language_code(value, code):
    assert db.target_language_code(value) == code


def test_target_language_code_rejects_nonsense():
    with pytest.raises(db.DubbingError) as err:
        db.target_language_code("Klingonese")
    assert err.value.code == "bad_language"


def test_voice_spec_for_language_keeps_explicit_language():
    assert db.voice_spec_for_language({"engine_id": "x"}, "es")["language"] == "es"
    assert db.voice_spec_for_language({"engine_id": "x", "language": "pt"}, "es")["language"] == "pt"


def test_translation_max_tokens_scales_with_length():
    assert db.translation_max_tokens("hola") == 200
    assert db.translation_max_tokens("x" * 900) > 1000
    assert db.translation_max_tokens("x" * 100000) == db.MAX_SEGMENT_TEXT_TOKENS


def test_translate_segments_reports_each_segment_and_can_cancel():
    seen = []

    def on_segment(i, total):
        seen.append((i, total))
        if i == 1:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError):
        db.translate_segments(lambda m: "x", [{"text": "a"}, {"text": "b"}, {"text": "c"}], "Spanish",
                              on_segment=on_segment)
    assert seen == [(0, 3), (1, 3)]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_target_language_reaches_tts_and_prompt(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    video_path = tmp_path / "clip.mp4"
    _tiny_video(video_path)
    RecordingTTS.calls = []
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [RecordingTTS()])
    prompts = []
    tokens = []

    class Sync(FakeSync):
        def chat(self, messages, max_tokens=None, temperature=None):
            prompts.append(messages[0]["content"])
            tokens.append(max_tokens)
            return super().chat(messages, max_tokens, temperature)

    backend = FakeBackend()
    backend.link.sync = Sync(_fake_chat({"hello": "hola", "kenobi": "kenobi"}))
    job = store.create_job("dub", "cpu", {"video_path": str(video_path), "target_language": "español",
                                          "voice": {"engine_id": "fake-tts"}})
    outputs = db.dub_job(store, backend, job, Progress(store, job["id"]))
    assert outputs["target_language"] == "es"
    assert {c["language"] for c in RecordingTTS.calls} == {"es"}
    assert all("Translate into Spanish" in p for p in prompts)
    assert all(t >= 200 for t in tokens)


def test_separate_background_falls_back_when_demucs_cannot_start(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(db, "demucs_installed", lambda: True)

    def boom(cmd, **kw):
        assert cmd[:3] == [sys.executable, "-m", "demucs"]
        raise FileNotFoundError("no demucs")

    monkeypatch.setattr(db.procutil, "run", boom)
    assert db.separate_background(tmp_path / "original.wav", tmp_path / "background") is None


def test_separate_background_reuses_existing_stems(tmp_path, monkeypatch):
    stem = tmp_path / "background" / "htdemucs" / "original" / "no_vocals.wav"
    stem.parent.mkdir(parents=True)
    stem.write_bytes(b"RIFF....")
    monkeypatch.setattr(db, "demucs_installed", lambda: True)
    monkeypatch.setattr(db.procutil, "run", lambda *a, **k: pytest.fail("demucs must not run again"))
    got = db.separate_background(tmp_path / "original.wav", tmp_path / "background")
    assert got and got["no_vocals"] == stem


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_resynthesize_segment_keeps_default_voice_updates_job_and_registers_asset(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    project = store.create_project("Dub fix")
    video_path = tmp_path / "clip.mp4"
    _tiny_video(video_path)

    class OtherTTS(RecordingTTS):
        id = "other-tts"

    RecordingTTS.calls = []
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [RecordingTTS(), OtherTTS()])
    backend = FakeBackend(_fake_chat({"hello": "hola", "kenobi": "kenobi"}))
    job = store.create_job("dub", "cpu", {"video_path": str(video_path), "target_language": "fr",
                                          "voice": {"engine_id": "fake-tts"}, "project_id": project["id"]})
    outputs = db.dub_job(store, backend, job, Progress(store, job["id"]))
    store.update_job(job["id"], state="done", outputs=outputs)
    work_dir = tmp_path / "data" / outputs["work_dir"]

    result = db.resynthesize_segment(store, backend, work_dir, 1, new_text="salut",
                                     voice_spec={"engine_id": "other-tts"}, job_id=job["id"])
    assert result["asset_id"] and result["asset_id"] != outputs["asset_ids"][0]
    assert store.get_asset(result["asset_id"])["kind"] == "video"
    assert RecordingTTS.calls[-1] == {"text": "salut", "language": "fr", "voice_ref": None, "id": "other-tts"}

    manifest = db.load_manifest(work_dir)
    assert manifest["voice"]["engine_id"] == "fake-tts"  # the dub's default voice is untouched
    assert manifest["segments"][1]["voice"] == {"engine_id": "other-tts"}
    assert manifest["segments"][1]["engine_id"] == "other-tts"
    assert not list(work_dir.glob("*.tmp"))

    updated = store.get_job(job["id"])["outputs"]
    assert updated["segments"][1]["translated_text"] == "salut"
    assert updated["asset_ids"][0] == result["asset_id"]

    # re-synthesizing another segment keeps using the default voice
    db.resynthesize_segment(store, backend, work_dir, 0, new_text="bonjour", remix=False)
    assert db.load_manifest(work_dir)["segments"][0]["engine_id"] == "fake-tts"


def test_dub_segments_view_pages_and_clips():
    rows = [{"index": i, "start_s": i, "end_s": i + 1, "source_text": "s" * 500, "translated_text": f"t{i}",
             "fit": {}, "engine_id": "x"} for i in range(75)]
    page = db.dub_segments_view({"segments": rows}, 0, 50)
    assert page["total"] == 75 and len(page["items"]) == 50 and page["next_offset"] == 50
    assert set(page["items"][0]) == {"index", "start_s", "end_s", "source_text", "translated_text"}
    assert len(page["items"][0]["source_text"]) <= 300
    last = db.dub_segments_view({"segments": rows}, 50, 50)
    assert len(last["items"]) == 25 and last["next_offset"] is None
    view = db.dub_outputs_view({"segments": rows, "final_video": "voice_studio/dub/x/dubbed.mp4"})
    assert "final_video" not in view and view["segments"]["total"] == 75 and "hint" in view
