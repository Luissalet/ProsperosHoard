from __future__ import annotations

import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import voice_engines as ve
from prosperos_hoard import voice_lab
from prosperos_hoard.store import Store

HAS_FFMPEG = shutil.which("ffmpeg") is not None


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
