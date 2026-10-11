"""Word-level retake of a voice line: only the picked words change."""
from __future__ import annotations

import numpy as np
import pytest

from prosperos_hoard import voice_engines as ve
from prosperos_hoard import voice_retake as vr

SR = 16000


def _line() -> tuple[np.ndarray, list[dict]]:
    """Three 'words' (tones at 200/300/400 Hz) separated by 0.2 s pauses."""
    parts, words, t = [], [], 0.0
    for i, f in enumerate((200, 300, 400)):
        sil = np.zeros(int(0.2 * SR), dtype=np.float32)
        tone = (0.3 * np.sin(2 * np.pi * f * np.arange(int(0.4 * SR)) / SR)).astype(np.float32)
        parts += [sil, tone]
        t += 0.2
        words.append({"start_s": round(t, 2), "end_s": round(t + 0.4, 2), "word": f"w{i}"})
        t += 0.4
    parts.append(np.zeros(int(0.2 * SR), dtype=np.float32))
    return np.concatenate(parts), words


def _patch(seconds=0.6, sr=22050, lead=0.3) -> bytes:
    """A replacement with its own leading/trailing silence, at another sample rate."""
    tone = (0.5 * np.sin(2 * np.pi * 900 * np.arange(int(seconds * sr)) / sr)).astype(np.float32)
    sil = np.zeros(int(lead * sr), dtype=np.float32)
    return ve.wav_bytes_mono16(np.concatenate([sil, tone, sil]), sr)


def test_word_cut_uses_middle_of_pauses():
    _, words = _line()
    t0, t1 = vr.word_cut(words, 1, 1, 2.0)
    assert t0 == pytest.approx(0.7) and t1 == pytest.approx(1.3)
    t0, t1 = vr.word_cut(words, 0, 0, 2.0)
    assert t0 == pytest.approx(0.15) and t1 == pytest.approx(0.7)
    t0, t1 = vr.word_cut(words, 2, 2, 2.0)
    assert t1 == pytest.approx(1.85)


def test_word_cut_rejects_bad_ranges():
    _, words = _line()
    for a, b in ((2, 1), (-1, 0), (0, 3)):
        with pytest.raises(vr.RetakeError) as exc:
            vr.word_cut(words, a, b, 2.0)
        assert exc.value.code == "bad_word_range"
    with pytest.raises(vr.RetakeError):
        vr.word_cut([], 0, 0, 2.0)


def test_trim_silence_keeps_small_margin():
    x, _ = vr.decode_wav(_patch(sr=SR))
    y = vr.trim_silence(x, SR)
    assert len(y) == pytest.approx(int(0.6 * SR) + 2 * int(vr.KEEP_PAD_S * SR), abs=2)
    with pytest.raises(vr.RetakeError):
        vr.trim_silence(np.zeros((100, 1), dtype=np.float32), SR)


def test_retake_keeps_everything_outside_the_cut_bit_for_bit():
    line, words = _line()
    original = ve.wav_bytes_mono16(line, SR)
    calls = []

    def synth(text):
        calls.append(text)
        return _patch()

    out = vr.retake_words(original, words, 1, 1, "nuevo", synth)
    assert calls == ["nuevo"]  # only the new words are synthesized
    x, _ = vr.decode_wav(original)
    y, rate = vr.decode_wav(out["wav"])
    assert rate == SR
    rep = out["report"]
    cf = int(vr.CROSSFADE_S * SR)
    i0 = int(round(rep["cut_start_s"] * SR))
    i1 = int(round(rep["cut_end_s"] * SR))
    # before the cut: identical samples
    assert np.array_equal(y[: i0 - cf], x[: i0 - cf])
    # after the cut: identical samples, shifted by the length change
    tail = len(x) - i1 - cf
    assert np.array_equal(y[-tail:], x[-tail:])
    assert rep["shift_after_cut_s"] == pytest.approx(rep["duration_after_s"] - rep["duration_before_s"], abs=1e-3)
    assert rep["replaced"] == "w1" and rep["new_text"] == "nuevo"
    # the replacement sits in the cut (900 Hz tone dominates the middle)
    mid = y[i0 + cf: i0 + cf + int(0.3 * SR), 0]
    spec = np.abs(np.fft.rfft(mid))
    assert np.fft.rfftfreq(len(mid), 1 / SR)[spec.argmax()] == pytest.approx(900, abs=10)


def test_retake_shifts_later_words_and_estimates_new_ones():
    line, words = _line()
    out = vr.retake_words(ve.wav_bytes_mono16(line, SR), words, 1, 1, "dos palabras", lambda t: _patch())
    w = out["words"]
    assert [x["word"] for x in w] == ["w0", "dos", "palabras", "w2"]
    assert w[0] == words[0]
    assert w[1]["estimated"] and w[2]["estimated"]
    shift = out["report"]["shift_after_cut_s"]
    assert w[3]["start_s"] == pytest.approx(words[2]["start_s"] + shift, abs=0.011)


def test_retake_stereo_line_and_empty_text():
    line, words = _line()
    stereo = np.stack([line, line * 0.5], axis=1)
    orig = vr.encode_wav(stereo, SR)
    out = vr.retake_words(orig, words, 0, 1, "hola", lambda t: _patch())
    y, _ = vr.decode_wav(out["wav"])
    assert y.shape[1] == 2
    with pytest.raises(vr.RetakeError) as exc:
        vr.retake_words(orig, words, 0, 0, "  ", lambda t: _patch())
    assert exc.value.code == "empty_text"


def test_flatten_words_from_transcript():
    tr = {"segments": [{"words": [{"start_s": 0, "end_s": 0.5, "word": " hola", "p": 0.9}]},
                       {"words": [{"start_s": 0.6, "end_s": 1.0, "word": "mundo"}]}]}
    assert [w["word"] for w in vr.flatten_words(tr)] == ["hola", "mundo"]


# ------------------------------------------------------------------- API


class FakeTTS(ve.TTSEngine):
    id = "fake-tts"
    label = "Fake"
    capabilities = ve.EngineCapabilities(languages=["es"], cloning=False)

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        return _patch()


class FakeSTT(ve.STTEngine):
    id = "fake-stt"
    label = "Fake"
    capabilities = ve.EngineCapabilities()

    def is_installed(self):
        return True

    def transcribe(self, path, language=None, word_timestamps=True):
        _, words = _line()
        return {"language": "es", "text": "w0 w1 w2", "segments": [{"start_s": 0, "end_s": 2, "text": "w0 w1 w2",
                                                                    "words": words}]}


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [FakeTTS()])
    monkeypatch.setattr("prosperos_hoard.api.ve.default_stt_engines", lambda: [FakeSTT()])


def _line_asset(c):
    project = c.post("/api/projects", json={"name": "Retake"}).json()
    # a spoken line saved as an asset by the normal speak route, then its file replaced by the 3-word line
    asset = c.post("/api/voice/speak", json={"text": "w0 w1 w2", "voice": {"engine_id": "fake-tts"},
                                             "project": project["id"]}).json()
    return project, asset


def test_api_retake_words_saves_derived_asset(client, fakes):
    c, app, _allowed = client
    project, asset = _line_asset(c)
    store = app.state.store if hasattr(app.state, "store") else None
    line, _ = _line()
    if store is not None:
        (store.data_dir / store.get_asset(asset["id"])["file_path"]).write_bytes(ve.wav_bytes_mono16(line, SR))
    resp = c.post("/api/voice/retake_words", json={"asset_id": asset["id"], "first_word": 1, "last_word": 1,
                                                   "text": "nuevo"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["engine_id"] == "fake-tts"  # the line's own voice, from its recipe
    assert body["words_from"].startswith("transcribed:")
    assert body["retake"]["replaced"] == "w1"
    new = body["asset"]
    assert new["id"] != asset["id"] and new["kind"] == "audio"
    # a second retake on the result reuses the stored word timings instead of transcribing again
    resp2 = c.post("/api/agent/voice_retake_words", json={"asset_id": new["id"], "first_word": 0, "last_word": 0,
                                                          "text": "otro"})
    assert resp2.status_code == 200, resp2.text
    assert "otro" in resp2.text


def test_api_retake_words_errors(client, fakes):
    c, app, _allowed = client
    _, asset = _line_asset(c)
    resp = c.post("/api/voice/retake_words", json={"asset_id": asset["id"], "first_word": 5, "last_word": 6,
                                                   "text": "x", "words": [{"start_s": 0, "end_s": 1, "word": "a"}]})
    assert resp.status_code in (400, 422)
    assert "bad_word_range" in resp.text


def test_snap_moves_cut_into_the_gap_between_run_together_words():
    tone = (0.3 * np.sin(2 * np.pi * 250 * np.arange(SR) / SR)).astype(np.float32)
    tone[int(0.50 * SR):int(0.53 * SR)] = 0.0  # the real pause is at 0.50-0.53 s
    x = tone[:, None]
    i = vr.snap_to_quiet(x, SR, 0.44)  # the transcriber put the boundary at 0.44 s
    assert 0.50 * SR <= i <= 0.53 * SR
    assert vr.snap_to_quiet(x, SR, 0.0) == 0  # the line's start stays the start

def test_snap_keeps_the_timing_when_there_is_no_real_pause():
    t = np.arange(SR) / SR
    tone = (0.3 * np.sin(2 * np.pi * 250 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    x = tone[:, None]  # words run together: dips in loudness, no silence
    assert vr.snap_to_quiet(x, SR, 0.44) == int(round(0.44 * SR))


def _glued_line() -> np.ndarray:
    """Two words run together: loud vowel, a shallow (not silent) dip at 0.38 s, loud again."""
    t = np.arange(int(1.0 * SR)) / SR
    env = np.full_like(t, 0.8)
    env[(t > 0.34) & (t < 0.42)] = 0.55  # the a->i transition: quieter, never silent
    return (0.4 * env * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_valley_moves_a_glued_end_cut_out_to_the_dip():
    x = _glued_line()[:, None]
    i = vr.valley_after_glued_boundary(x, SR, 0.30, +1)  # transcriber ended the word at 0.30 s
    assert i is not None and 0.34 * SR <= i <= 0.42 * SR
    # looking the other way finds nothing: the dip is not before 0.30
    assert vr.valley_after_glued_boundary(x, SR, 0.30, -1) is None


def test_valley_ignores_steady_loudness():
    t = np.arange(SR) / SR
    x = (0.3 * np.sin(2 * np.pi * 250 * t)).astype(np.float32)[:, None]
    assert vr.valley_after_glued_boundary(x, SR, 0.44, +1) is None


def test_retake_of_a_glued_first_word_cuts_at_the_dip():
    line = _glued_line()
    words = [{"start_s": 0.0, "end_s": 0.30, "word": "Mañana"}, {"start_s": 0.30, "end_s": 0.76, "word": "iremos"}]
    out = vr.retake_words(ve.wav_bytes_mono16(line, SR), words, 0, 0, "Hoy", lambda _t: _patch(0.2))
    rep = out["report"]
    assert rep["cut_by"] == ["timing", "valley"]
    assert 0.34 <= rep["cut_end_s"] <= 0.42
    # a word followed by a real gap in the timings keeps the old behaviour
    words_gap = [{"start_s": 0.0, "end_s": 0.30, "word": "a"}, {"start_s": 0.36, "end_s": 0.76, "word": "b"}]
    out2 = vr.retake_words(ve.wav_bytes_mono16(line, SR), words_gap, 0, 0, "Hoy", lambda _t: _patch(0.2))
    assert out2["report"]["cut_by"][1] == "timing"
