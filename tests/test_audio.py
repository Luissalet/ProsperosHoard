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


def _drum_pattern(bpm: float, duration_s: float = 24.0, sr: int = 44100, hats: bool = False) -> np.ndarray:
    """Kick on every beat, noise snare on beats 2 and 4 (the pattern that
    made a per-bin flux envelope lock onto the snare at half tempo)."""
    n = int(duration_s * sr)
    sig = np.zeros(n)
    rng = np.random.default_rng(0)
    kick_env = np.exp(-np.arange(int(sr * 0.15)) / (sr * 0.03))
    kick = np.sin(2 * np.pi * 55 * np.arange(len(kick_env)) / sr) * kick_env
    snare = rng.standard_normal(int(sr * 0.1)) * np.exp(-np.arange(int(sr * 0.1)) / (sr * 0.02))
    hat = rng.standard_normal(int(sr * 0.03)) * np.exp(-np.arange(int(sr * 0.03)) / (sr * 0.005))
    period = 60.0 / bpm
    k, t = 0, 0.0
    while t < duration_s:
        i = int(t * sr)
        sig[i:i + len(kick)] += kick[: max(0, min(len(kick), n - i))] * 0.9
        if k % 4 in (1, 3):
            sig[i:i + len(snare)] += snare[: max(0, min(len(snare), n - i))] * 0.5
        if hats:
            for sub in (0.0, period / 2):
                j = int((t + sub) * sr)
                if j < n:
                    sig[j:j + len(hat)] += hat[: max(0, min(len(hat), n - j))] * 0.15
        k += 1
        t += period
    return (sig / np.abs(sig).max() * 0.8).astype(np.float32)


@pytest.mark.parametrize("bpm,hats", [(90.0, False), (120.0, False), (140.0, False), (100.0, True)])
def test_backbeat_drums_are_not_tracked_at_half_or_double_tempo(bpm, hats):
    result = audio.analyze_samples(_drum_pattern(bpm, hats=hats))
    assert abs(result["tempo_bpm"] - bpm) <= 1.0
    period = 60.0 / bpm
    beats = np.array(result["beat_times"])
    assert len(beats) >= int(24.0 / period) - 1  # every beat, not every other one
    err = np.abs((beats / period) - np.round(beats / period)) * period
    assert err.max() <= 0.05
    assert beats[0] <= 0.05  # the first beat at t=0 is found


def test_demo_song_is_120_bpm_with_a_b_a_sections(tmp_path):
    from prosperos_hoard.devtools.demo_seed import _make_synthetic_song

    path = tmp_path / "song.wav"
    _make_synthetic_song(path)
    result = audio.analyze_samples(audio.decode_to_mono(path))
    assert abs(result["tempo_bpm"] - 120.0) <= 1.0
    labels = [s["label"] for s in result["sections"]]
    energies = [s["energy"] for s in result["sections"]]
    assert labels == ["section A", "section B", "section A"]
    assert energies == ["low", "high", "low"]
    assert abs(result["sections"][1]["start_s"] - 8.0) <= 0.1
    assert abs(result["sections"][1]["end_s"] - 24.0) <= 0.1
    assert result["sections"][0]["start_s"] == 0.0 and result["sections"][-1]["end_s"] == pytest.approx(30.0)


def test_steady_loop_is_one_section_and_silence_has_no_beats():
    steady = audio.analyze_samples(_click_track(120.0))
    assert len(steady["sections"]) == 1
    silent = audio.analyze_samples(np.zeros(44100 * 3, dtype=np.float32))
    assert silent["beat_times"] == [] and silent["tempo_bpm"] is None


def test_tempo_estimate_stays_in_range_on_awkward_envelopes():
    """The parabolic refinement used to run on whatever lag the prior
    picked, even on a slope, and extrapolate to a negative or huge BPM."""
    for seed in range(60):
        rng = np.random.default_rng(seed)
        env = np.cumsum(rng.standard_normal(int(rng.integers(300, 3000))))  # drifting, slope-heavy
        env -= env.min()
        bpm, period = audio.estimate_tempo(env, bpm_range=(50.0, 220.0))
        assert 50.0 <= bpm <= 220.0 and period > 0, (seed, bpm)
        assert bpm == pytest.approx(60.0 * audio.SAMPLE_RATE / audio.HOP / period)


def test_sound_already_playing_at_t0_is_not_an_onset():
    """Frame 0 used to be compared with silence, so a noise bed playing from
    the first sample was the loudest onset in the song and dragged the
    beat grid onto t=0. A real hit at t=0 must still be found (see the
    backbeat test above)."""
    sr = 44100
    clicks = _click_track(120.0, 10.0)
    shift = int(0.25 * sr)
    sig = np.zeros_like(clicks)
    sig[shift:] = clicks[:-shift]
    sig = (sig + 0.05 * np.random.default_rng(1).standard_normal(len(sig))).astype(np.float32)
    env = audio.onset_envelope(sig)
    assert env[0] < 0.5
    beats = audio.analyze_samples(sig)["beat_times"]
    assert abs(beats[0] - 0.25) <= 0.05


def test_frame_features_match_the_full_spectrogram():
    """The block-wise features (no whole-song spectrogram kept in memory)
    are the same numbers the full spectrogram gives."""
    sig = _drum_pattern(120.0, duration_s=4.0)
    mags = audio._stft_mags(sig)
    bands, low, power = audio._frame_features(sig)
    assert len(bands) == len(mags) == 1 + len(sig) // audio.HOP
    assert np.allclose(bands, mags @ audio._band_matrix(mags.shape[1]).T, rtol=1e-4, atol=1e-4)
    assert np.allclose(low, mags[:, :audio._band_bins(audio.SAMPLE_RATE, audio.FRAME_SIZE, 200.0)])
    lo_b = audio._band_bins(audio.SAMPLE_RATE, audio.FRAME_SIZE, 250.0)
    assert np.allclose(power[:, 0], (mags[:, :lo_b].astype(np.float64) ** 2).sum(axis=1))


@pytest.mark.parametrize("t,expected", [(59.996, "[01:00.00]"), (59.994, "[00:59.99]"), (119.999, "[02:00.00]"),
                                        (0.0, "[00:00.00]"), (-0.3, "[00:00.00]"), (3599.999, "[60:00.00]")])
def test_lrc_time_carries_instead_of_printing_sixty_seconds(t, expected):
    assert audio.to_lrc([{"time_s": t, "text": "x"}]) == f"{expected}x"
    assert audio.parse_lrc(f"{expected}x")[0]["time_s"] == pytest.approx(max(0.0, round(t, 2)), abs=0.006)
