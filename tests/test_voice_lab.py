from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard.backend import ffmpeg_path
from prosperos_hoard import voice_engines as ve
from prosperos_hoard import voice_lab
from prosperos_hoard.store import Store

HAS_FFMPEG = ffmpeg_path() is not None


def _write_wav(path: Path, samples: np.ndarray, sr: int = 44100) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def _speechlike(seconds: float = 3.0, sr: int = 44100, seed: int = 0) -> np.ndarray:
    """A crude "speech-like" signal: bursts of tone separated by near-silence,
    so the quality check's loud/quiet-decile SNR estimate has something real
    to measure (unlike a constant-amplitude test tone)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    out = np.zeros(n, dtype=np.float32)
    t = np.linspace(0, seconds, n, endpoint=False)
    envelope = (np.sin(2 * np.pi * 1.5 * t) > 0).astype(np.float32)
    tone = 0.3 * np.sin(2 * np.pi * 220 * t)
    noise = rng.normal(0, 0.001, n).astype(np.float32)
    out = (tone * envelope + noise).astype(np.float32)
    return out


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_process_sample_and_quality_check(tmp_path):
    src = tmp_path / "src.wav"
    _write_wav(src, _speechlike(3.0))
    dest = tmp_path / "out" / "sample.wav"
    voice_lab.process_sample(src, dest)
    assert dest.is_file()

    quality = voice_lab.quality_check(dest)
    assert quality["duration_s"] > 0
    assert quality["clipping_pct"] == 0.0
    assert quality["snr_db"] > voice_lab.MIN_GOOD_SNR_DB  # a clean bursty tone should read as "good"
    assert quality["warnings"] == []
    assert quality["ok"] is True


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_process_sample_output_is_44100hz(tmp_path):
    # documented as "a clean 44.1 kHz mono WAV" - ffmpeg's loudnorm filter
    # (used for the loudness-normalisation pass) has a well-known quirk of
    # emitting at 192kHz regardless of the input/requested rate unless the
    # output is explicitly resampled back down afterwards.
    src = tmp_path / "src.wav"
    _write_wav(src, _speechlike(2.0, sr=16000), sr=16000)
    dest = tmp_path / "out.wav"
    voice_lab.process_sample(src, dest)
    with wave.open(str(dest), "rb") as w:
        assert w.getframerate() == 44100


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_quality_check_flags_clipping_and_short_duration(tmp_path):
    short = tmp_path / "short.wav"
    _write_wav(short, np.ones(200, dtype=np.float32) * 0.999, sr=8000)  # ~25ms, clipped
    quality = voice_lab.quality_check(short)
    assert quality["ok"] is False
    assert any("short" in w for w in quality["warnings"])
    assert any("clipping" in w for w in quality["warnings"])
    assert quality["clipping_pct"] > voice_lab.MAX_GOOD_CLIP_PCT


def test_quality_check_unreadable_file(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(voice_lab.VoiceLabError):
        voice_lab.quality_check(bad)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_create_voice_stores_row_and_files(tmp_path):
    store = Store(tmp_path / "data")
    project = store.create_project("Test")
    src = tmp_path / "sample.wav"
    _write_wav(src, _speechlike(2.5))

    row = voice_lab.create_voice(store, "Narrator One", src, "xtts", language="en", project_id=project["id"])
    assert row["name"] == "Narrator One"
    assert row["engine_id"] == "xtts"
    assert row["cloned"] is True
    assert row["sample_path"]
    assert (store.data_dir / row["sample_path"]).is_file()
    assert row["quality"]["duration_s"] > 0

    view = voice_lab.voice_view(row)
    assert view["id"] == row["id"]
    assert view["has_sample"] is True
    assert "reference_transcript" not in view  # compact view

    # a second voice with the same name gets its own directory (no clash)
    row2 = voice_lab.create_voice(store, "Narrator One", src, "xtts")
    assert row2["sample_path"] != row["sample_path"]

    fetched = store.get_studio_voice(row["id"])
    assert fetched["name"] == "Narrator One"


def test_create_voice_requires_name(tmp_path):
    store = Store(tmp_path / "data")
    with pytest.raises(voice_lab.VoiceLabError):
        voice_lab.create_voice(store, "", tmp_path / "x.wav", "piper")


# --------------------------------------------------------- voice spec logic

class _FakeCloningEngine(ve.TTSEngine):
    id = "fake-clone"
    capabilities = ve.EngineCapabilities(cloning=True)

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        assert sample_path is not None  # cloning engines must receive the sample
        return ve.wav_bytes_mono16(np.zeros(100, dtype=np.float32), 8000)


class _FakeNonCloningEngine(ve.TTSEngine):
    id = "fake-basic"
    capabilities = ve.EngineCapabilities(cloning=False)

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        return ve.wav_bytes_mono16(np.zeros(100, dtype=np.float32), 8000)


def _store_with_voice(tmp_path, engine_id="fake-clone", with_sample=True) -> tuple[Store, dict]:
    store = Store(tmp_path / "data")
    sample_rel = None
    if with_sample:
        sample_path = store.data_dir / "voice_studio" / "voices" / "v1" / "sample.wav"
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(sample_path, np.zeros(8000, dtype=np.float32))
        sample_rel = sample_path.relative_to(store.data_dir).as_posix()
    voice = store.create_studio_voice("My Voice", engine_id, sample_path=sample_rel, language="en")
    return store, voice


def test_resolve_voice_spec_requires_engine(tmp_path):
    store = Store(tmp_path / "data")
    with pytest.raises(voice_lab.VoiceSpecError):
        voice_lab.resolve_voice_spec(store, [_FakeCloningEngine()], {})


def test_resolve_voice_spec_unknown_engine(tmp_path):
    store = Store(tmp_path / "data")
    with pytest.raises(voice_lab.VoiceSpecError):
        voice_lab.resolve_voice_spec(store, [_FakeCloningEngine()], {"engine_id": "nope"})


def test_resolve_voice_spec_cloning_needs_sample(tmp_path):
    store, _ = _store_with_voice(tmp_path, with_sample=False)
    with pytest.raises(voice_lab.VoiceSpecError):
        voice_lab.resolve_voice_spec(store, [_FakeCloningEngine()], {"engine_id": "fake-clone"})


def test_resolve_voice_spec_uses_library_voice_sample_and_engine(tmp_path):
    store, voice = _store_with_voice(tmp_path)
    resolved = voice_lab.resolve_voice_spec(store, [_FakeCloningEngine()], {"voice_id": voice["id"]})
    assert resolved["engine"].id == "fake-clone"
    assert resolved["sample_path"] is not None
    assert resolved["language"] == "en"


def test_resolve_voice_spec_preset_merging(tmp_path):
    store, voice = _store_with_voice(tmp_path)
    store.add_voice_preset(voice["id"], {"name": "calm", "speed": 0.85, "style": "soft"})
    resolved = voice_lab.resolve_voice_spec(store, [_FakeCloningEngine()], {"voice_id": voice["id"], "preset": "calm"})
    assert resolved["speed"] == 0.85 and resolved["style"] == "soft"

    # an explicit spec field overrides the preset
    resolved2 = voice_lab.resolve_voice_spec(store, [_FakeCloningEngine()],
                                             {"voice_id": voice["id"], "preset": "calm", "speed": 1.2})
    assert resolved2["speed"] == 1.2

    with pytest.raises(voice_lab.VoiceSpecError):
        voice_lab.resolve_voice_spec(store, [_FakeCloningEngine()], {"voice_id": voice["id"], "preset": "missing"})


def test_synthesize_with_spec_non_cloning_needs_no_sample(tmp_path):
    store = Store(tmp_path / "data")
    wav, engine_id = voice_lab.synthesize_with_spec(store, [_FakeNonCloningEngine()], {"engine_id": "fake-basic"},
                                                     "hello")
    assert engine_id == "fake-basic"
    assert wav[:4] == b"RIFF"


def test_synthesize_with_spec_not_installed(tmp_path):
    class NotInstalled(ve.TTSEngine):
        id = "off"

        def is_installed(self):
            return False

    store = Store(tmp_path / "data")
    with pytest.raises(voice_lab.VoiceSpecError):
        voice_lab.synthesize_with_spec(store, [NotInstalled()], {"engine_id": "off"}, "hi")


# ------------------------------------------------------------ regressions

class _RecordingEngine(ve.TTSEngine):
    id = "rec"
    capabilities = ve.EngineCapabilities(cloning=False)
    seen: list[dict] = []

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        _RecordingEngine.seen.append({"text": text, "pitch": pitch, "language": language})
        return ve.wav_bytes_mono16((0.2 * np.sin(np.linspace(0, 2000, 16000))).astype(np.float32), 16000)


class _RefTextCloner(_FakeCloningEngine):
    id = "ref-clone"
    seen: list = []

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None,
                   ref_text=None):
        _RefTextCloner.seen.append(ref_text)
        return super().synthesize(text, voice_ref, speed, pitch, style, sample_path, language)


def test_apply_lexicon_whole_words_case_insensitive():
    lex = {"Prospero": "PROS-per-oh", "Hoard": "hord", "St. Ives": "Saint Ives"}
    out = voice_lab.apply_lexicon("prospero's Hoard is not Prosperous; go to St. Ives.", lex)
    assert out == "PROS-per-oh's hord is not Prosperous; go to Saint Ives."
    assert voice_lab.apply_lexicon("nothing here", lex) == "nothing here"
    assert voice_lab.apply_lexicon("Prospero", {}) == "Prospero"


def test_normalize_lexicon_validates():
    assert voice_lab.normalize_lexicon({" Iris ": "Eye-ris"}) == {"Iris": "Eye-ris"}
    for bad in (["x"], {"": "y"}, {"x": 3}, {"x" * 101: "y"}):
        with pytest.raises(voice_lab.VoiceSpecError):
            voice_lab.normalize_lexicon(bad)


def test_lexicon_merges_voice_preset_and_request(tmp_path):
    store, voice = _store_with_voice(tmp_path, engine_id="rec", with_sample=False)
    voice_lab.set_voice_lexicon(store, voice["id"], {"Prospero": "PROS-per-oh", "Iris": "EYE-ris"})
    store.add_voice_preset(voice["id"], {"name": "show", "lexicon": {"Iris": "ee-REES"}})
    _RecordingEngine.seen = []
    voice_lab.synthesize_with_spec(store, [_RecordingEngine()], {"voice_id": voice["id"], "preset": "show",
                                                                 "lexicon": {"Hoard": "hord"}},
                                   "Prospero, Iris and the Hoard.")
    assert _RecordingEngine.seen[-1]["text"] == "PROS-per-oh, ee-REES and the hord."
    view = voice_lab.voice_view(store.get_studio_voice(voice["id"]))
    assert view["presets"] == ["show"]  # the reserved lexicon entry is not a preset
    assert view["lexicon"] == {"Prospero": "PROS-per-oh", "Iris": "EYE-ris"}
    with pytest.raises(voice_lab.VoiceSpecError):
        voice_lab.resolve_voice_spec(store, [_RecordingEngine()], {"voice_id": voice["id"], "preset": "_lexicon"})
    voice_lab.set_voice_lexicon(store, voice["id"], {})
    assert voice_lab.voice_lexicon(store.get_studio_voice(voice["id"])) == {}


def test_reference_transcript_reaches_engines_that_take_it(tmp_path):
    store, voice = _store_with_voice(tmp_path, engine_id="ref-clone")
    store.update_studio_voice(voice["id"], reference_transcript="what the sample says", )
    _RefTextCloner.seen = []
    voice_lab.synthesize_with_spec(store, [_RefTextCloner()], {"voice_id": voice["id"], "style": "calm"}, "hi")
    assert _RefTextCloner.seen == ["what the sample says"]
    # an engine without a ref_text parameter is simply not given one
    store2, voice2 = _store_with_voice(tmp_path / "b", engine_id="fake-clone")
    store2.update_studio_voice(voice2["id"], reference_transcript="x")
    voice_lab.synthesize_with_spec(store2, [_FakeCloningEngine()], {"voice_id": voice2["id"]}, "hi")


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_pitch_is_applied_and_keeps_duration(tmp_path):
    store = Store(tmp_path / "data")
    _RecordingEngine.seen = []
    plain, _ = voice_lab.synthesize_with_spec(store, [_RecordingEngine()], {"engine_id": "rec"}, "a")
    shifted, _ = voice_lab.synthesize_with_spec(store, [_RecordingEngine()], {"engine_id": "rec", "pitch": 12}, "a")
    assert _RecordingEngine.seen[-1]["pitch"] is None  # applied here, not by the engine

    def read(data):
        import io

        with wave.open(io.BytesIO(data), "rb") as wf:
            return np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2").astype(np.float32), wf.getframerate()

    a, sr_a = read(plain)
    b, sr_b = read(shifted)
    assert sr_a == sr_b == 16000
    assert b.size == pytest.approx(a.size, rel=0.05)
    peak = lambda x: np.argmax(np.abs(np.fft.rfft(x[2000:-2000])))  # noqa: E731
    assert peak(b) == pytest.approx(2 * peak(a), rel=0.05)  # +12 semitones = an octave up


def test_bad_pitch_rejected(tmp_path):
    store = Store(tmp_path / "data")
    with pytest.raises(voice_lab.VoiceSpecError) as err:
        voice_lab.validate_voice_spec(store, [_RecordingEngine()], {"engine_id": "rec", "pitch": 30})
    assert err.value.code == "bad_pitch"


def test_validate_voice_spec_checks_install_and_language(tmp_path):
    store = Store(tmp_path / "data")

    class Off(_RecordingEngine):
        id = "off"

        def is_installed(self):
            return False

    class Kokoroish(_RecordingEngine):
        id = "kok"

        def check_language(self, language):
            ve.KokoroEngine.lang_code(language)

    with pytest.raises(voice_lab.VoiceSpecError) as err:
        voice_lab.validate_voice_spec(store, [Off()], {"engine_id": "off"})
    assert err.value.code == "engine_not_installed"
    with pytest.raises(voice_lab.VoiceSpecError) as err:
        voice_lab.validate_voice_spec(store, [Kokoroish()], {"engine_id": "kok", "language": "de"})
    assert err.value.code == "unsupported_language"
    assert voice_lab.validate_voice_spec(store, [Kokoroish()], {"engine_id": "kok", "language": "es"})


def test_delete_voice_files_only_removes_the_voice_folder(tmp_path):
    store, voice = _store_with_voice(tmp_path)
    folder = store.data_dir / "voice_studio" / "voices" / "v1"
    assert voice_lab.delete_voice_files(store, voice) is True
    assert not folder.exists()
    assert (store.data_dir / "voice_studio" / "voices").is_dir()
    # a sample path pointing anywhere else is never followed
    outside = store.data_dir / "other" / "sample.wav"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")
    assert voice_lab.delete_voice_files(store, {"sample_path": "other/sample.wav"}) is False
    assert voice_lab.delete_voice_files(store, {"sample_path": "voice_studio/voices/../../other/sample.wav"}) is False
    assert outside.is_file()
