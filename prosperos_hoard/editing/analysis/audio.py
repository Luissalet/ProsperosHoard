"""Sound analysis from a 16 kHz mono stream: the envelope (peaks for the waveform, RMS for silence detection),
silences, loudness and the beat grid of music."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import numpy as np

from .. import ffmpeg as ff
from ..ffmpeg import Cancelled, RunHandle, Tools

RATE = 16000
HOP = 160  # 10 ms
ENV_HZ = RATE // HOP  # 100 values per second


def stream_pcm(tools: Tools, path: Path, *, stream: int = 0, rate: int = RATE, handle: Optional[RunHandle] = None,
               block_s: float = 30.0) -> Iterator[np.ndarray]:
    """Mono int16 blocks of ~block_s seconds, read from ffmpeg without holding the whole file in memory."""
    proc = ff.spawn(tools, ["-i", str(path), "-map", f"0:a:{stream}", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le",
                            "pipe:1"], handle)
    size = int(rate * block_s) * 2
    assert proc.stdout is not None
    try:
        while True:
            data = proc.stdout.read(size)
            if not data:
                break
            if len(data) % 2:
                data = data[:-1]
            yield np.frombuffer(data, dtype=np.int16)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if handle is not None and handle.cancelled:
        raise Cancelled("Canceled.")


def envelope(tools: Tools, path: Path, *, stream: int = 0, duration_ms: int = 0, progress: Optional[Callable[[float], None]] = None,
             handle: Optional[RunHandle] = None) -> tuple[np.ndarray, np.ndarray]:
    """(peaks uint8 0..255, rms dBFS float32), one value per 10 ms."""
    peaks: list[np.ndarray] = []
    rms: list[np.ndarray] = []
    carry = np.zeros(0, dtype=np.int16)
    done = 0
    for block in stream_pcm(tools, path, stream=stream, handle=handle):
        data = np.concatenate([carry, block]) if carry.size else block
        n = data.size // HOP
        carry = data[n * HOP:]
        if n == 0:
            continue
        frames = data[: n * HOP].astype(np.float32).reshape(n, HOP) / 32768.0
        peaks.append(np.minimum(255, np.abs(frames).max(axis=1) * 255).astype(np.uint8))
        r = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        rms.append((20 * np.log10(r)).astype(np.float32))
        done += n * HOP
        if progress and duration_ms:
            progress(min(0.99, done / RATE * 1000 / duration_ms))
    if not peaks:
        return np.zeros(0, dtype=np.uint8), np.zeros(0, dtype=np.float32)
    return np.concatenate(peaks), np.concatenate(rms)


def noise_floor(rms: np.ndarray) -> float:
    if rms.size == 0:
        return -60.0
    return float(np.percentile(rms, 10))


def auto_threshold(rms: np.ndarray) -> float:
    """Between the noise floor and the speech level: robust for quiet rooms and noisy gameplay alike."""
    if rms.size == 0:
        return -40.0
    floor = float(np.percentile(rms, 10))
    speech = float(np.percentile(rms, 80))
    if speech - floor < 6:  # nearly constant sound (music, game audio): only real drops count
        return max(-60.0, floor - 3)
    return max(-55.0, min(-20.0, floor + (speech - floor) * 0.35))


def silences(rms: np.ndarray, *, threshold_db: Optional[float] = None, min_silence_ms: int = 500, margin_ms: int = 150,
             min_sound_ms: int = 120) -> tuple[list[list[int]], float]:
    """Ranges [from_ms, to_ms] that are silent for at least ``min_silence_ms``, shrunk by ``margin_ms`` on each side so the
    words around keep their breath; sounds shorter than ``min_sound_ms`` count as silence."""
    if rms.size == 0:
        return [], threshold_db if threshold_db is not None else -40.0
    thr = auto_threshold(rms) if threshold_db is None else threshold_db
    loud = rms > thr
    # sound blips shorter than min_sound are not speech
    min_sound = max(1, min_sound_ms // 10)
    idx = np.flatnonzero(np.diff(np.concatenate([[0], loud.astype(np.int8), [0]])))
    starts, ends = idx[0::2], idx[1::2]
    for a, b in zip(starts, ends):
        if b - a < min_sound:
            loud[a:b] = False
    quiet = ~loud
    idx = np.flatnonzero(np.diff(np.concatenate([[0], quiet.astype(np.int8), [0]])))
    out: list[list[int]] = []
    min_len = max(1, min_silence_ms // 10)
    total_ms = rms.size * 10
    for a, b in zip(idx[0::2], idx[1::2]):
        if b - a < min_len:
            continue
        a_ms, b_ms = int(a) * 10, int(b) * 10
        lo = a_ms + (margin_ms if a_ms > 0 else 0)
        hi = b_ms - (margin_ms if b_ms < total_ms else 0)
        if hi - lo >= 80:
            out.append([lo, hi])
    return out, thr


def loudness(tools: Tools, path: Path, *, stream: int = 0, handle: Optional[RunHandle] = None) -> dict[str, Any]:
    """EBU R128 integrated loudness, range and true peak of one audio stream (the shared ``ebur128`` reading)."""
    return ff.loudness(tools, path, stream=stream, handle=handle)


def loudnorm_measure(tools: Tools, path: Path, target: float, tp: float = -1.5, lra: float = 11.0, handle: Optional[RunHandle] = None) -> dict[str, Any]:
    """First pass of two-pass loudnorm for a final render (``{}`` when there is nothing to measure)."""
    return ff.loudnorm_measure(tools, path, target, tp, lra, handle)


# ---------------------------------------------------------------- beats

def onset_envelope(pcm: np.ndarray, rate: int = RATE, hop: int = 256, n_fft: int = 1024) -> tuple[np.ndarray, float]:
    """Spectral flux (log-magnitude, positive differences), one value per hop; returns (envelope, frames per second)."""
    x = pcm.astype(np.float32) / 32768.0
    if x.size < n_fft:
        return np.zeros(0, dtype=np.float32), rate / hop
    frames = 1 + (x.size - n_fft) // hop
    window = np.hanning(n_fft).astype(np.float32)
    out = np.zeros(frames, dtype=np.float32)
    prev = None
    step = 4096
    for s in range(0, frames, step):
        idx = np.arange(s, min(frames, s + step))[:, None] * hop + np.arange(n_fft)[None, :]
        spec = np.log1p(np.abs(np.fft.rfft(x[idx] * window, axis=1)) * 10)
        if prev is None:
            diff = np.vstack([np.zeros((1, spec.shape[1]), dtype=spec.dtype), np.diff(spec, axis=0)])
        else:
            diff = np.diff(np.vstack([prev[None, :], spec]), axis=0)
        out[s: s + spec.shape[0]] = np.maximum(diff, 0).sum(axis=1)
        prev = spec[-1]
    out -= out.mean()
    out /= out.std() + 1e-6
    return out, rate / hop


def tempo(env: np.ndarray, fps: float, lo: float = 60, hi: float = 180) -> float:
    if env.size < fps * 4:
        return 0.0
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[e.size - 1:]
    lags = np.arange(ac.size)
    bpm = np.where(lags > 0, 60.0 * fps / np.maximum(lags, 1), 0)
    mask = (bpm >= lo) & (bpm <= hi)
    if not mask.any():
        return 0.0
    # prefer tempos near 120 (log-gaussian weighting), the usual octave-error guard
    weight = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1) / 120.0) / 0.9) ** 2)
    score = np.where(mask, ac * weight, -np.inf)
    lag = int(np.argmax(score))
    return float(60.0 * fps / lag)


def beat_track(env: np.ndarray, fps: float, bpm: float, tightness: float = 100.0) -> np.ndarray:
    """Dynamic-programming beat tracker (Ellis 2007): beat frame indices that follow the onsets at a steady period."""
    if env.size == 0 or bpm <= 0:
        return np.zeros(0, dtype=int)
    period = 60.0 * fps / bpm
    n = env.size
    score = env.astype(np.float64).copy()
    back = np.full(n, -1, dtype=int)
    lo, hi = int(round(period * 0.5)), int(round(period * 2))
    offsets = np.arange(-hi, -lo + 1)
    penalty = -tightness * np.log(-offsets / period) ** 2
    for i in range(n):
        idx = i + offsets
        ok = idx >= 0
        if not ok.any():
            continue
        cand = score[idx[ok]] + penalty[ok]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            score[i] = env[i] + cand[j]
            back[i] = idx[ok][j]
    # start from the best score in the last period
    tail = max(0, n - int(period))
    i = tail + int(np.argmax(score[tail:]))
    beats = []
    while i >= 0:
        beats.append(i)
        i = back[i]
    return np.array(beats[::-1], dtype=int)


def beats(tools: Tools, path: Path, *, stream: int = 0, handle: Optional[RunHandle] = None, max_s: float = 1800) -> dict[str, Any]:
    chunks = []
    total = 0
    for block in stream_pcm(tools, path, stream=stream, rate=RATE, handle=handle):
        chunks.append(block)
        total += block.size
        if total > max_s * RATE:
            break
    if not chunks:
        return {"bpm": 0, "beats": [], "downbeats": []}
    pcm = np.concatenate(chunks)
    env, fps = onset_envelope(pcm)
    bpm = tempo(env, fps)
    idx = beat_track(env, fps, bpm)
    times = [int(round(i / fps * 1000)) for i in idx]
    # downbeats: the phase (of 4) whose beats carry the most onset energy
    downbeats: list[int] = []
    if len(idx) >= 8:
        strength = [float(np.mean(env[idx[p::4]])) for p in range(4)]
        phase = int(np.argmax(strength))
        downbeats = times[phase::4]
    return {"bpm": round(bpm, 2), "beats": times, "downbeats": downbeats}


def energy_per_second(rms: np.ndarray) -> np.ndarray:
    """Mean loudness per second (dB) for highlight scoring."""
    if rms.size == 0:
        return np.zeros(0, dtype=np.float32)
    n = rms.size // ENV_HZ
    if n == 0:
        return np.array([float(rms.mean())], dtype=np.float32)
    lin = 10 ** (rms[: n * ENV_HZ] / 20)
    sec = lin.reshape(n, ENV_HZ)
    return (20 * np.log10(np.sqrt((sec ** 2).mean(axis=1)) + 1e-9)).astype(np.float32)


def db_to_text(db: float) -> str:
    return "-inf" if not math.isfinite(db) else f"{db:.1f} dB"
