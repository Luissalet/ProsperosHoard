"""Loudness for generated soundtracks: BS.1770-4 integrated loudness, true peak and a look-ahead true-peak limiter.

Everything works on float arrays (n, 2) at 48 kHz. The K-weighting filters are the ones in the foley forge
(forge.polish.K_SOS); the gating is the standard one (absolute -70 LUFS, relative -10 LU)."""
from __future__ import annotations

import math

import numpy as np
from scipy import signal
from scipy.ndimage import minimum_filter1d, uniform_filter1d

from .foley.forge.polish import K_SOS

SR = 48000
SILENT_LUFS = -120.0


def stereo(x) -> np.ndarray:
    """(n, 2) float64 from a mono (n,) or (n, ch) array; a mono signal plays as two identical channels"""
    x = np.asarray(x, np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return np.ascontiguousarray(x[:, :2])


def db(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-12))


def amp(decibels: float) -> float:
    return 10.0 ** (float(decibels) / 20.0)


def block_powers(x: np.ndarray, sr: int = SR) -> np.ndarray:
    """Mean K-weighted power of every 400 ms block (75 % overlap). Shorter signals are zero-padded to one block, as a meter shows them."""
    k = signal.sosfilt(K_SOS, stereo(x), axis=0)
    power = (k * k).sum(axis=1)
    block, hop = int(round(0.4 * sr)), int(round(0.1 * sr))
    if power.size < block:
        power = np.pad(power, (0, block - power.size))
    csum = np.concatenate([[0.0], np.cumsum(power)])
    starts = np.arange(0, power.size - block + 1, hop)
    return (csum[starts + block] - csum[starts]) / block


def integrated_lufs(x, sr: int = SR) -> float:
    z = block_powers(x, sr)
    loud = -0.691 + 10.0 * np.log10(z + 1e-24)
    gated = z[loud > -70.0]
    if not gated.size:
        return SILENT_LUFS
    threshold = -0.691 + 10.0 * math.log10(float(gated.mean())) - 10.0
    kept = z[loud > threshold]
    if not kept.size:
        return SILENT_LUFS
    return float(-0.691 + 10.0 * math.log10(float(kept.mean())))


def true_peak_db(x, oversample: int = 8) -> float:
    """Largest inter-sample peak in dBTP (band-limited interpolation, stricter than a 4x meter)"""
    x = stereo(x)
    if not x.size:
        return -120.0
    sample = float(np.abs(x).max())
    up = signal.resample_poly(x, oversample, 1, axis=0)
    return db(max(sample, float(np.abs(up).max())))


def limit(x, ceiling_db: float = -1.0, lookahead_s: float = 0.004, release_s: float = 0.08, sr: int = SR) -> np.ndarray:
    """Look-ahead limiter on the 4x oversampled peak: the result never goes past ceiling_db (true peak)."""
    x = stereo(x)
    ceiling = amp(ceiling_db)
    up = signal.resample_poly(x, 4, 1, axis=0)
    peak = np.abs(up).max(axis=1)[: x.shape[0] * 4].reshape(-1, 4).max(axis=1)
    peak = np.maximum(peak, np.abs(x).max(axis=1))
    wanted = np.minimum(1.0, ceiling / np.maximum(peak, 1e-12))
    if wanted.min() >= 1.0:
        return x
    ahead = max(2, int(lookahead_s * sr))
    held = minimum_filter1d(wanted, size=2 * ahead + 1, mode="nearest")
    gain = np.minimum(uniform_filter1d(held, size=ahead + 1, mode="nearest"), wanted)
    reduction = 1.0 - gain
    decay = math.exp(-1.0 / max(1.0, release_s * sr))
    active = np.nonzero(reduction > 1e-9)[0]
    if active.size:
        # release: the reduction fades out exponentially after its last peak instead of snapping back
        reach = int(release_s * sr * 6)
        lo, hi = int(max(0, active[0])), int(min(reduction.size, active[-1] + reach))
        seg = reduction[lo:hi].tolist()
        carry = 0.0
        for i, r in enumerate(seg):
            carry = r if r > carry * decay else carry * decay
            seg[i] = carry
        reduction[lo:hi] = seg
    return x * (1.0 - reduction)[:, None]


def master(x, target_lufs: float = -14.0, ceiling_db: float = -1.0, sr: int = SR, max_reduction_db: float = 8.0, steps: int = 8):
    """Gain to target_lufs, limited under ceiling_db true peak. Returns (float32 (n, 2), info).

    A signal that cannot reach the target without the limiter working harder than max_reduction_db (a few sparse
    transients, whose loudest peak sets the level) keeps the largest gain that stays under that and reports
    reached=False with the loudness it got."""
    x = stereo(x)
    start = integrated_lufs(x, sr)
    if start <= -70.0:
        return x.astype(np.float32), {"lufs": start, "true_peak_db": true_peak_db(x), "gain_db": 0.0, "limiter_db": 0.0,
                                      "target_lufs": target_lufs, "ceiling_db": ceiling_db, "reached": False, "silent": True}
    peak = db(np.abs(x).max())

    def attempt(gain_db: float):
        boosted = x * amp(gain_db)
        out = limit(boosted, ceiling_db - 0.1, sr=sr)
        return out, integrated_lufs(out, sr), max(0.0, db(np.abs(boosted).max()) - db(np.abs(out).max()))

    wanted = target_lufs - start
    clean = ceiling_db - 0.1 - peak                      # the gain at which the peak just touches the ceiling: no limiting
    if wanted <= clean:
        gain, (out, loud, reduction) = wanted, attempt(wanted)
    else:
        out, loud, reduction = attempt(wanted)
        gain = wanted
        if reduction > max_reduction_db:
            lo, hi = clean, wanted
            gain, (out, loud, reduction) = lo, attempt(lo)
            for _ in range(steps):
                mid = (lo + hi) / 2.0
                candidate = attempt(mid)
                if candidate[2] <= max_reduction_db:
                    lo, gain, (out, loud, reduction) = mid, mid, candidate
                else:
                    hi = mid
        else:
            for _ in range(4):                           # the limiter costs some loudness: nudge the gain until the target is met
                if abs(loud - target_lufs) <= 0.1:
                    break
                trial = gain + (target_lufs - loud)
                candidate = attempt(trial)
                if candidate[2] > max_reduction_db:      # more gain would make the limiter work harder than allowed: stop here
                    break
                gain, (out, loud, reduction) = trial, candidate
    tp = true_peak_db(out)
    if tp > ceiling_db:
        out = out * amp(ceiling_db - tp - 0.01)
        loud, tp = integrated_lufs(out, sr), true_peak_db(out)
    return out.astype(np.float32), {"lufs": round(loud, 2), "true_peak_db": round(tp, 2), "gain_db": round(gain, 2),
                                    "limiter_db": round(reduction, 2), "target_lufs": target_lufs, "ceiling_db": ceiling_db,
                                    "reached": abs(loud - target_lufs) <= 0.5, "silent": False}
