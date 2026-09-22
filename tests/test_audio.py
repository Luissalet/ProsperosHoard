import numpy as np
import pytest

from prosperos_hoard import audio


def _click_track(bpm: float, duration_s: float = 20.0, sr: int = 44100) -> np.ndarray:
    n = int(duration_s * sr)
    sig = np.zeros(n, dtype=np.float32)
    period = 60.0 / bpm
    click_len = int(sr * 0.02)
    click = (np.sin(2 * np.pi * 1000 * np.arange(click_len) / sr) * np.exp(-np.arange(click_len) / (sr * 0.005))).astype(np.float32)
    t = 0.0
    while t < duration_s:
        i = int(t * sr)
        end = min(n, i + click_len)
        sig[i:end] += click[: end - i]
        t += period
    return sig


@pytest.mark.parametrize("bpm", [90.0, 120.0, 140.0])
def test_tempo_and_beats_on_synthetic_click_track(bpm):
    sig = _click_track(bpm)
    result = audio.analyze_samples(sig)
    assert abs(result["tempo_bpm"] - bpm) <= 1.0

    period = 60.0 / bpm
    max_err_ms = 0.0
    for b in result["beat_times"]:
        nearest_k = round(b / period)
        expected = nearest_k * period
        max_err_ms = max(max_err_ms, abs(b - expected) * 1000)
    assert max_err_ms <= 50.0


def test_section_count_on_synthetic_low_high_low_song():
    sr = 44100
    duration = 24.0
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    # low energy - high energy - low energy, each 8s, at 120 BPM
    beat_hz = 2.0
    click = np.zeros_like(t)
    period = 1 / beat_hz
    for i in range(int(duration / period)):
        idx = int(i * period * sr)
        if idx < len(click):
            click[idx : idx + 200] = 1.0
    amp = np.ones_like(t) * 0.2
    amp[(t >= 8) & (t < 16)] = 1.0
    signal = (click * amp).astype(np.float32)
    result = audio.analyze_samples(signal)
    energies = [s["energy"] for s in result["sections"]]
    assert "high" in energies
    assert "low" in energies


def test_lrc_round_trip():
    text = "[00:01.20]hello world\n[00:03.50]second line\n"
    lines = audio.parse_lrc(text)
    assert lines[0]["time_s"] == pytest.approx(1.2)
    assert lines[1]["text"] == "second line"
    exported = audio.to_lrc(lines)
    assert "[00:01.20]hello world" in exported


def test_waveform_peaks_length():
    sig = np.random.default_rng(0).uniform(-1, 1, 44100).astype(np.float32)
    peaks = audio.waveform_peaks(sig, buckets=200)
    assert len(peaks) == 200
    assert all(0.0 <= p <= 1.0 for p in peaks)
