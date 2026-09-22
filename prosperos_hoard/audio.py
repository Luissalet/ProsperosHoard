"""Audio import, analysis (tempo/beats/sections), lyrics (LRC), and voices.

Beat tracking does not depend on `librosa` (kept out of the pinned deps to
avoid its heavier native-wheel chain on Windows cp313): a spectral-flux
onset envelope + autocorrelation tempo + a small dynamic-programming beat
tracker, all numpy/scipy. Tested against synthetic click tracks at known
BPM in `tests/test_audio.py`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import numpy as np

SAMPLE_RATE = 44100
FRAME_SIZE = 2048
HOP = 512


class DecodeError(RuntimeError):
    pass


def _ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise DecodeError("ffmpeg not found and imageio-ffmpeg unavailable") from exc


def decode_to_mono(path: Path, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any ffmpeg-readable audio file to mono float32 PCM at `sr`."""
    exe = _ffmpeg_exe()
    cmd = [exe, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise DecodeError(proc.stderr.decode("utf-8", "replace")[:500])
    return np.frombuffer(proc.stdout, dtype=np.float32)


def probe_duration_s(path: Path) -> Optional[float]:
    exe = _ffmpeg_exe()
    ffprobe = exe.replace("ffmpeg", "ffprobe")
    if Path(ffprobe).name == Path(exe).name:
        # imageio-ffmpeg only ships ffmpeg; fall back to decoding length.
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def waveform_peaks(samples: np.ndarray, buckets: int = 800) -> list[float]:
    if len(samples) == 0:
        return [0.0] * buckets
    chunk = max(1, len(samples) // buckets)
    peaks = []
    for i in range(0, len(samples), chunk):
        window = samples[i : i + chunk]
        if len(window):
            peaks.append(float(np.max(np.abs(window))))
        if len(peaks) >= buckets:
            break
    while len(peaks) < buckets:
        peaks.append(0.0)
    return peaks


# ----------------------------------------------------------------------
# Onset envelope + tempo + beats
# ----------------------------------------------------------------------

def onset_envelope(samples: np.ndarray, sr: int = SAMPLE_RATE, frame_size: int = FRAME_SIZE, hop: int = HOP) -> np.ndarray:
    if len(samples) < frame_size:
        samples = np.pad(samples, (0, frame_size - len(samples)))
    n_frames = 1 + (len(samples) - frame_size) // hop
    window = np.hanning(frame_size)
    mags = np.empty((n_frames, frame_size // 2 + 1), dtype=np.float64)
    for i in range(n_frames):
        seg = samples[i * hop : i * hop + frame_size] * window
        spec = np.fft.rfft(seg)
        mags[i] = np.abs(spec)
    log_mag = np.log1p(mags)
    flux = np.diff(log_mag, axis=0, prepend=log_mag[:1])
    flux = np.clip(flux, 0, None).sum(axis=1)
    flux = flux - flux.min()
    if flux.max() > 0:
        flux = flux / flux.max()
    return flux


def estimate_tempo(env: np.ndarray, sr: int = SAMPLE_RATE, hop: int = HOP,
                    bpm_range: tuple[float, float] = (60.0, 200.0)) -> tuple[float, int]:
    """Returns (bpm, period_in_frames) from autocorrelation of the onset envelope."""
    fps = sr / hop
    min_lag = max(1, int(fps * 60.0 / bpm_range[1]))
    max_lag = min(len(env) - 1, int(fps * 60.0 / bpm_range[0]))
    if max_lag <= min_lag:
        return 120.0, max(1, int(fps * 0.5))
    env_z = env - env.mean()
    autocorr = np.correlate(env_z, env_z, mode="full")[len(env_z) - 1 :]
    window = autocorr[min_lag : max_lag + 1]
    best = int(np.argmax(window)) + min_lag
    bpm = 60.0 * fps / best
    return float(bpm), best


def track_beats(env: np.ndarray, period_frames: int, sr: int = SAMPLE_RATE, hop: int = HOP) -> list[float]:
    """A compact dynamic-programming beat tracker (Ellis-style): pick the
    onset peak sequence closest to a steady `period_frames` interval that
    maximises cumulative onset strength."""
    n = len(env)
    if n == 0:
        return []
    tightness = 100.0
    lo = max(1, int(period_frames * 0.5))
    hi = max(lo + 1, int(period_frames * 2.0))
    cum = np.copy(env)
    backlink = np.full(n, -1, dtype=int)
    for i in range(1, n):
        window_lo = max(0, i - hi)
        window_hi = max(0, i - lo)
        if window_hi <= window_lo:
            continue
        candidates = np.arange(window_lo, window_hi)
        deltas = i - candidates
        penalty = tightness * (np.log(deltas / period_frames) ** 2)
        scores = cum[candidates] - penalty
        best_idx = int(np.argmax(scores))
        best_score = scores[best_idx] + env[i]
        if best_score > cum[i]:
            cum[i] = best_score
            backlink[i] = candidates[best_idx]

    beats: list[int] = []
    i = int(np.argmax(cum[-max(1, hi) :]) + max(0, n - hi))
    while i >= 0:
        beats.append(i)
        i = backlink[i]
    beats.reverse()
    fps = sr / hop
    return [round(b / fps, 4) for b in beats]


def analyze_samples(samples: np.ndarray, sr: int = SAMPLE_RATE) -> dict[str, Any]:
    env = onset_envelope(samples, sr)
    bpm, period = estimate_tempo(env, sr)
    beat_times = track_beats(env, period, sr)
    downbeats = beat_times[0::4]
    sections = _detect_sections(env, beat_times, sr)
    duration_s = round(len(samples) / sr, 3)
    return {
        "duration_s": duration_s,
        "tempo_bpm": round(bpm, 1),
        "beat_times": beat_times,
        "downbeats": downbeats,
        "onset_envelope_preview": [round(float(v), 3) for v in env[:: max(1, len(env) // 200)]][:200],
        "sections": sections,
    }


def analyze_file(path: Path) -> dict[str, Any]:
    samples = decode_to_mono(path)
    return analyze_samples(samples)


def _detect_sections(env: np.ndarray, beat_times: list[float], sr: int, hop: int = HOP, bars_per_window: int = 2) -> list[dict[str, Any]]:
    if len(beat_times) < 8:
        return [{"label": "section A", "start_s": 0.0, "end_s": beat_times[-1] if beat_times else 0.0, "energy": "mid"}]
    beats_per_window = bars_per_window * 4
    fps = sr / hop
    window_energy = []
    window_bounds = []
    for i in range(0, len(beat_times) - beats_per_window, beats_per_window):
        t0, t1 = beat_times[i], beat_times[min(i + beats_per_window, len(beat_times) - 1)]
        f0, f1 = int(t0 * fps), max(int(t0 * fps) + 1, int(t1 * fps))
        seg = env[f0:f1]
        window_energy.append(float(seg.mean()) if len(seg) else 0.0)
        window_bounds.append((t0, t1))
    if not window_energy:
        return [{"label": "section A", "start_s": 0.0, "end_s": beat_times[-1], "energy": "mid"}]

    energies = np.array(window_energy)
    lo, hi = np.percentile(energies, [33, 66])

    def level(e: float) -> str:
        if e <= lo:
            return "low"
        if e >= hi:
            return "high"
        return "mid"

    labels = [level(e) for e in energies]
    sections: list[dict[str, Any]] = []
    letter_for_level: dict[str, str] = {}
    next_letter = ord("A")
    cur_level = labels[0]
    cur_start = window_bounds[0][0]
    for idx in range(1, len(labels) + 1):
        changed = idx == len(labels) or labels[idx] != cur_level
        if changed:
            end = window_bounds[idx - 1][1]
            if cur_level not in letter_for_level:
                letter_for_level[cur_level] = chr(next_letter)
                next_letter += 1
            sections.append({
                "label": f"section {letter_for_level[cur_level]}",
                "start_s": round(cur_start, 3),
                "end_s": round(end, 3),
                "energy": cur_level,
            })
            if idx < len(labels):
                cur_level = labels[idx]
                cur_start = window_bounds[idx][0]
    return sections


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
