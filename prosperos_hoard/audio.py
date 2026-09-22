"""Audio import, analysis (tempo/beats/sections), lyrics (LRC), and voices.

Beat tracking does not depend on `librosa` (kept out of the pinned deps to
avoid its heavier native-wheel chain on Windows cp313): a spectral-flux
onset envelope + autocorrelation tempo + a small dynamic-programming beat
tracker, all numpy/scipy. Tested against synthetic click tracks at known
BPM in `tests/test_audio.py`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import procutil

SAMPLE_RATE = 44100
FRAME_SIZE = 2048
HOP = 512


class DecodeError(RuntimeError):
    pass


def _ffmpeg_exe() -> str:
    from .backend import ffmpeg_path

    exe = ffmpeg_path()
    if not exe:
        raise DecodeError("ffmpeg not found: install ffmpeg or the imageio-ffmpeg wheel")
    return exe


def decode_to_mono(path: Path, sr: int = SAMPLE_RATE, max_duration_s: float = 1200.0) -> np.ndarray:
    """Decode any ffmpeg-readable audio file to mono float32 PCM at `sr`
    (at most `max_duration_s`, 20 minutes, to bound memory)."""
    exe = _ffmpeg_exe()
    cmd = [exe, "-nostdin", "-v", "error", "-i", str(path), "-t", f"{max_duration_s:.0f}",
           "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"]
    proc = procutil.run(cmd, timeout=180)
    if proc.returncode != 0:
        raise DecodeError(proc.stderr.decode("utf-8", "replace")[:500])
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        raise DecodeError(f"{Path(path).name} contains no decodable audio")
    return samples


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration_s(path: Path) -> Optional[float]:
    """Container duration in seconds, via ffprobe when it sits next to
    ffmpeg, else by parsing `ffmpeg -i` (the imageio-ffmpeg binary ships
    no ffprobe). None when the file is not a readable media file."""
    from .backend import ffprobe_path

    probe = ffprobe_path()
    try:
        if probe:
            out = procutil.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                               text=True, timeout=30)
            value = out.stdout.strip()
            if out.returncode == 0 and value and value != "N/A":
                return round(float(value), 3)
        out = procutil.run([_ffmpeg_exe(), "-nostdin", "-hide_banner", "-i", str(path)], text=True, timeout=30)
        m = _DURATION_RE.search(out.stderr or "")
        if m:
            h, mnt, sec = m.groups()
            return round(int(h) * 3600 + int(mnt) * 60 + float(sec), 3)
    except (OSError, ValueError, DecodeError, procutil.subprocess.SubprocessError):
        return None
    return None


def waveform_peaks(samples: np.ndarray, buckets: int = 800) -> list[float]:
    if len(samples) == 0:
        return [0.0] * buckets
    edges = np.linspace(0, len(samples), buckets + 1).astype(int)
    peaks = []
    absval = np.abs(samples)
    for i in range(buckets):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        window = absval[a:b]
        peaks.append(round(float(window.max()) if window.size else 0.0, 4))
    top = max(peaks) or 1.0
    return [round(min(1.0, p / top), 4) for p in peaks]


# ----------------------------------------------------------------------
# Onset envelope + tempo + beats
# ----------------------------------------------------------------------

def _stft_mags(samples: np.ndarray, frame_size: int = FRAME_SIZE, hop: int = HOP) -> np.ndarray:
    """Centred frames (frame i is centred on sample i*hop), so an onset at
    t=0 is visible and frame times need no latency correction."""
    padded = np.pad(samples.astype(np.float64), (frame_size // 2, frame_size // 2))
    if len(padded) < frame_size:
        padded = np.pad(padded, (0, frame_size - len(padded)))
    n_frames = 1 + (len(padded) - frame_size) // hop
    window = np.hanning(frame_size)
    mags = np.empty((n_frames, frame_size // 2 + 1), dtype=np.float32)
    block = 256
    for b0 in range(0, n_frames, block):
        idx = np.arange(b0, min(n_frames, b0 + block))
        frames = np.stack([padded[i * hop: i * hop + frame_size] for i in idx]) * window
        mags[b0:b0 + len(idx)] = np.abs(np.fft.rfft(frames, axis=1))
    return mags


_BAND_CACHE: dict[tuple[int, int], np.ndarray] = {}


def _band_matrix(n_bins: int, n_bands: int = 40, sr: int = SAMPLE_RATE, fmin: float = 30.0) -> np.ndarray:
    """Log-spaced triangular filter bank (bins -> bands). Summing flux per
    band instead of per FFT bin keeps a kick drum (a handful of low bins)
    as loud in the onset envelope as a broadband snare or hi-hat."""
    key = (n_bins, n_bands)
    if key in _BAND_CACHE:
        return _BAND_CACHE[key]
    freqs = np.linspace(0, sr / 2, n_bins)
    edges = np.geomspace(fmin, sr / 2, n_bands + 2)
    fb = np.zeros((n_bands, n_bins), dtype=np.float32)
    for b in range(n_bands):
        lo, mid, hi = edges[b], edges[b + 1], edges[b + 2]
        up = (freqs - lo) / max(1e-9, mid - lo)
        down = (hi - freqs) / max(1e-9, hi - mid)
        fb[b] = np.clip(np.minimum(up, down), 0, None)
        if fb[b].sum() == 0:
            fb[b, int(np.argmin(np.abs(freqs - mid)))] = 1.0
        fb[b] /= fb[b].sum()
    _BAND_CACHE[key] = fb
    return fb


def _flux(mags: np.ndarray, lo_bin: int = 0, hi_bin: Optional[int] = None) -> np.ndarray:
    if lo_bin == 0 and hi_bin is None:
        bands = mags @ _band_matrix(mags.shape[1]).T
    else:
        bands = mags[:, lo_bin:hi_bin]
    band = np.log1p(100.0 * bands)
    prev = np.vstack([np.zeros((1, band.shape[1]), dtype=band.dtype), band[:-1]])
    flux = np.clip(band - prev, 0, None).sum(axis=1)
    # remove the slowly varying part so sustained pads do not look like onsets
    k = 16
    if len(flux) > k:
        kernel = np.ones(k) / k
        flux = np.clip(flux - np.convolve(flux, kernel, mode="same"), 0, None)
    if flux.max() > 0:
        flux = flux / flux.max()
    return flux.astype(np.float64)


def onset_envelope(samples: np.ndarray, sr: int = SAMPLE_RATE, frame_size: int = FRAME_SIZE, hop: int = HOP) -> np.ndarray:
    return _flux(_stft_mags(samples, frame_size, hop))


def estimate_tempo(env: np.ndarray, sr: int = SAMPLE_RATE, hop: int = HOP,
                    bpm_range: tuple[float, float] = (50.0, 220.0), prior_bpm: float = 120.0) -> tuple[float, float]:
    """(bpm, period_in_frames). Autocorrelation of the onset envelope,
    weighted by a log-normal prior around `prior_bpm` (one octave wide) so
    the tracker prefers the felt beat over half/double time; the peak is
    refined with parabolic interpolation."""
    fps = sr / hop
    min_lag = max(1, int(fps * 60.0 / bpm_range[1]))
    max_lag = min(len(env) - 2, int(fps * 60.0 / bpm_range[0]) + 1)
    if max_lag <= min_lag + 2:
        return 120.0, fps * 0.5
    env_z = env - env.mean()
    n = len(env_z)
    spec = np.fft.rfft(env_z, n=2 * n)
    autocorr = np.fft.irfft(spec * np.conj(spec))[:n]
    if autocorr[0] <= 0:
        return 120.0, fps * 0.5
    autocorr = autocorr / autocorr[0]
    lags = np.arange(min_lag, max_lag + 1)
    bpms = 60.0 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / prior_bpm) / 1.0) ** 2)
    score = autocorr[lags] * prior
    best_i = int(np.argmax(score))
    best = float(lags[best_i])
    if 0 < best_i < len(lags) - 1:
        y0, y1, y2 = autocorr[lags[best_i] - 1], autocorr[lags[best_i]], autocorr[lags[best_i] + 1]
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            best += 0.5 * (y0 - y2) / denom
    return float(60.0 * fps / best), best


def track_beats(env: np.ndarray, period_frames: float, sr: int = SAMPLE_RATE, hop: int = HOP,
                tightness: float = 100.0) -> list[float]:
    """Dynamic-programming beat tracker (Ellis 2007): the beat sequence that
    maximises onset strength while keeping inter-beat intervals close to
    `period_frames`. Beats are then extended to the first and last strong
    onsets the DP left out."""
    n = len(env)
    if n == 0 or period_frames <= 0:
        return []
    fps = sr / hop
    local = env / (env.std() + 1e-9)
    lo = max(1, int(round(period_frames * 0.5)))
    hi = max(lo + 1, int(round(period_frames * 2.0)))
    cum = np.copy(local)
    backlink = np.full(n, -1, dtype=int)
    offsets = np.arange(lo, hi + 1)
    penalty = tightness * (np.log(offsets / period_frames) ** 2)
    for i in range(lo, n):
        prev = i - offsets
        valid = prev >= 0
        if not valid.any():
            continue
        scores = np.where(valid, cum[np.clip(prev, 0, None)] - penalty, -np.inf)
        j = int(np.argmax(scores))
        if scores[j] > 0:
            cum[i] = local[i] + scores[j]
            backlink[i] = prev[j]

    # end on the best-scoring frame within the last period
    tail = max(1, int(round(period_frames)))
    i = int(np.argmax(cum[n - tail:])) + n - tail
    beats: list[int] = []
    while i >= 0:
        beats.append(i)
        i = backlink[i]
    beats.reverse()

    # extend backwards/forwards on the grid where the DP stopped early
    threshold = 0.1 * float(np.median(local[beats])) if beats else 0.0
    search = max(1, int(round(period_frames * 0.1)))

    def snap(center: float) -> Optional[int]:
        a, b = int(max(0, center - search)), int(min(n - 1, center + search))
        if a > b:
            return None
        k = a + int(np.argmax(local[a:b + 1]))
        return k if local[k] >= threshold else None

    while beats and beats[0] - period_frames > -search:
        k = snap(beats[0] - period_frames)
        if k is None or k >= beats[0]:
            break
        beats.insert(0, k)
    while beats and beats[-1] + period_frames < n + search:
        k = snap(beats[-1] + period_frames)
        if k is None or k <= beats[-1]:
            break
        beats.append(k)
    return [round(b / fps, 4) for b in beats]


def _downbeat_phase(low_env: np.ndarray, beat_frames: list[int]) -> int:
    """Which of the first four beats starts a bar: the phase whose beats
    carry the most low-frequency onset energy (kick drums, bass notes).
    A guess, labelled as such in the API."""
    if len(beat_frames) < 8:
        return 0
    strengths = [float(np.mean(low_env[beat_frames[p::4]])) for p in range(4)]
    best = int(np.argmax(strengths))
    # a flat profile (every beat the same) keeps the first beat as the downbeat
    if strengths[best] < 1.1 * min(strengths):
        return 0
    return best


def _band_bins(sr: int, frame_size: int, hz: float) -> int:
    return int(round(hz * frame_size / sr))


def analyze_samples(samples: np.ndarray, sr: int = SAMPLE_RATE) -> dict[str, Any]:
    samples = np.asarray(samples, dtype=np.float32)
    duration_s = round(len(samples) / sr, 3)
    mags = _stft_mags(samples)
    env = _flux(mags)
    low_env = _flux(mags, 0, _band_bins(sr, FRAME_SIZE, 200.0))
    fps = sr / HOP
    if float(np.abs(samples).max(initial=0.0)) < 1e-4 or env.max() == 0:
        return {"duration_s": duration_s, "tempo_bpm": None, "beat_times": [], "downbeats": [],
                "sections": [{"label": "section A", "start_s": 0.0, "end_s": duration_s, "energy": "low"}],
                "notes": "silent audio: no beats found"}
    bpm, period = estimate_tempo(env, sr)
    beat_times = track_beats(env, period, sr)
    beat_frames = [int(round(t * fps)) for t in beat_times]
    phase = _downbeat_phase(low_env, beat_frames)
    downbeats = beat_times[phase::4]
    sections = _detect_sections(samples, mags, sr, duration_s, downbeats)
    if len(beat_times) > 3:
        # least-squares slope over the whole beat grid: sub-frame precise
        slope = float(np.polyfit(np.arange(len(beat_times)), np.array(beat_times), 1)[0])
        if slope > 0:
            bpm = 60.0 / slope
    return {
        "duration_s": duration_s,
        "tempo_bpm": round(bpm, 1),
        "beat_times": beat_times,
        "downbeats": downbeats,
        "sections": sections,
        "notes": "downbeats and section labels are estimates (energy/timbre changes), not verse/chorus detection",
    }


def analyze_file(path: Path) -> dict[str, Any]:
    samples = decode_to_mono(path)
    return analyze_samples(samples)


def _detect_sections(samples: np.ndarray, mags: np.ndarray, sr: int, duration_s: float,
                     downbeats: list[float], min_bars: int = 2, novelty_db: float = 4.0) -> list[dict[str, Any]]:
    """Bars (from downbeats) -> per-bar loudness + three band levels in dB ->
    boundaries where the two bars before and after differ by more than
    `novelty_db` -> segments; segments that sound alike share a letter
    (A/B/A...). Energy is relative to the song's own loudness range."""
    bounds = [0.0] + [t for t in downbeats if 0.25 < t < duration_s - 0.25] + [duration_s]
    if len(bounds) < 4:
        return [{"label": "section A", "start_s": 0.0, "end_s": duration_s, "energy": "mid"}]
    fps = sr / HOP
    lo_b, mid_b = _band_bins(sr, FRAME_SIZE, 250.0), _band_bins(sr, FRAME_SIZE, 2000.0)
    power = mags.astype(np.float64) ** 2
    feats = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        fa, fb = int(a * fps), max(int(a * fps) + 1, int(b * fps))
        seg = power[fa:fb]
        sa, sb = int(a * sr), max(int(a * sr) + 1, int(b * sr))
        rms = float(np.sqrt(np.mean(samples[sa:sb].astype(np.float64) ** 2))) if sb > sa else 0.0
        bands = [seg[:, :lo_b].sum(axis=1).mean(), seg[:, lo_b:mid_b].sum(axis=1).mean(), seg[:, mid_b:].sum(axis=1).mean()]
        feats.append([20 * np.log10(rms + 1e-6)] + [10 * np.log10(x + 1e-9) for x in bands])
    f = np.array(feats)
    n_bars = len(f)

    w = min_bars
    novelty = np.zeros(n_bars + 1)
    for i in range(1, n_bars):
        before = f[max(0, i - w):i].mean(axis=0)
        after = f[i:i + w].mean(axis=0)
        diff = np.abs(after - before)
        # loudness drives the boundary; the band balance (timbre) adds to
        # it, but a chord change alone (bands shift, loudness does not)
        # should not start a new section
        novelty[i] = float(diff[0] + 0.25 * diff[1:].mean())
    # boundaries = local novelty peaks above the threshold, strongest first,
    # at least `min_bars` apart
    candidates = [i for i in range(1, n_bars)
                  if novelty[i] >= novelty_db and novelty[i] >= novelty[i - 1] and novelty[i] >= novelty[i + 1]]
    chosen: list[int] = []
    for i in sorted(candidates, key=lambda j: -novelty[j]):
        if i >= min_bars and n_bars - i >= min_bars and all(abs(i - c) >= min_bars for c in chosen):
            chosen.append(i)
    cuts = [0] + sorted(chosen)
    segs = []
    for k, c in enumerate(cuts):
        end = cuts[k + 1] if k + 1 < len(cuts) else n_bars
        segs.append((c, end, f[c:end].mean(axis=0)))
    # merge a too-short trailing segment into the previous one
    if len(segs) > 1 and segs[-1][1] - segs[-1][0] < min_bars:
        c0, _, _ = segs[-2]
        segs = segs[:-2] + [(c0, n_bars, f[c0:n_bars].mean(axis=0))]

    letters: list[np.ndarray] = []
    labels = []
    for _, _, feat in segs:
        for li, ref in enumerate(letters):
            d = np.abs(feat - ref)
            if float(d[0] + 0.25 * d[1:].mean()) < novelty_db * 0.75:
                labels.append(li)
                break
        else:
            letters.append(feat)
            labels.append(len(letters) - 1)

    levels = np.array([feat[0] for _, _, feat in segs])
    spread = float(levels.max() - levels.min())

    def energy(level: float) -> str:
        if spread < 3.0:
            return "mid"
        x = (level - levels.min()) / spread
        return "low" if x < 0.34 else ("high" if x > 0.66 else "mid")

    return [
        {"label": f"section {chr(ord('A') + min(labels[k], 25))}", "start_s": round(bounds[c], 3),
         "end_s": round(bounds[e], 3), "energy": energy(float(feat[0]))}
        for k, (c, e, feat) in enumerate(segs)
    ]


# ----------------------------------------------------------------------
# LRC
# ----------------------------------------------------------------------

_LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")


def parse_lrc(text: str) -> list[dict[str, Any]]:
    lines = []
    for raw in text.splitlines():
        m = _LRC_TAG.match(raw.strip())
        if not m:
            continue
        minutes, seconds, content = m.groups()
        t = int(minutes) * 60 + float(seconds)
        lines.append({"time_s": round(t, 3), "text": content.strip()})
    lines.sort(key=lambda x: x["time_s"])
    return lines


def to_lrc(lines: list[dict[str, Any]]) -> str:
    out = []
    for line in lines:
        t = line["time_s"]
        minutes = int(t // 60)
        seconds = t - minutes * 60
        out.append(f"[{minutes:02d}:{seconds:05.2f}]{line['text']}")
    return "\n".join(out)
