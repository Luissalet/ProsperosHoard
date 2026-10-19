"""Multicam analysis on sound levels (10 ms values per recording, as the library keeps them): finding the offset between
recordings of the same event, and choosing which camera to show from who is loudest on their own microphone.

Pure functions over numpy arrays; ``multicam.py`` connects them to the library and the timeline.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np

HOP_MS = 10  # one level per 10 ms (analysis.audio.ENV_HZ = 100)


# ---------------------------------------------------------------- sync

def sync_feature(levels_db: np.ndarray, floor_percentile: float = 5.0, smooth_s: float = 1.5) -> np.ndarray:
    """What two microphones have in common is *when* things happen, not how loud: the level in dB above the recording's own noise
    floor (so gains and noise differ freely), minus its slow average (so only the changes remain)."""
    x = np.asarray(levels_db, dtype=np.float64)
    if x.size == 0:
        return x
    x = np.clip(x - np.percentile(x, floor_percentile), 0, 60)
    w = max(3, int(smooth_s * 1000 / HOP_MS))
    kernel = np.ones(w) / w
    pad = np.pad(x, (w // 2, w - 1 - w // 2), mode="edge")
    slow = np.convolve(pad, kernel, mode="valid")
    return x - slow


def _normalised_xcorr(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Normalised cross-correlation for every lag k: sum_t a[t+k] * b[t], divided by the energy of the overlapping parts.
    Returns (lags, r, overlap length) where lag k > 0 means b starts k samples after a."""
    n = int(2 ** np.ceil(np.log2(a.size + b.size)))
    c = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)
    lags = np.arange(-(b.size - 1), a.size)
    raw = np.concatenate([c[n - (b.size - 1):], c[: a.size]])
    ea = np.concatenate([[0.0], np.cumsum(a * a)])
    eb = np.concatenate([[0.0], np.cumsum(b * b)])
    t_lo = np.maximum(0, -lags)
    t_hi = np.minimum(b.size, a.size - lags)
    n_ov = t_hi - t_lo
    e_b = eb[np.clip(t_hi, 0, b.size)] - eb[np.clip(t_lo, 0, b.size)]
    e_a = ea[np.clip(t_hi + lags, 0, a.size)] - ea[np.clip(t_lo + lags, 0, a.size)]
    r = raw / np.sqrt(np.maximum(e_a * e_b, 1e-9))
    r[n_ov < 1] = 0
    return lags, r, n_ov


def pair_offset(a_db: np.ndarray, b_db: np.ndarray, *, min_overlap_s: float = 8.0) -> dict[str, Any]:
    """Offset of recording b against recording a: ``offset_ms`` > 0 means b started that long after a (b's time 0 is a's time
    ``offset_ms``). ``confidence`` (0..1) combines how well the levels agree (``correlation``) and how much better the best lag is
    than any other (``sharpness``). Sub-frame: the peak is refined by a parabola through its neighbours."""
    fa, fb = sync_feature(a_db), sync_feature(b_db)
    if fa.size < 50 or fb.size < 50 or fa.std() < 1e-6 or fb.std() < 1e-6:
        return {"offset_ms": 0.0, "confidence": 0.0, "correlation": 0.0, "sharpness": 0.0, "reason": "no sound to compare"}
    lags, r, n_ov = _normalised_xcorr(fa, fb)
    need = int(min(min_overlap_s * 1000 / HOP_MS, 0.5 * min(fa.size, fb.size)))
    r = np.where(n_ov >= need, r, -1.0)
    k = int(np.argmax(r))
    r1 = float(r[k])
    if 0 < k < r.size - 1:
        y0, y1, y2 = r[k - 1], r[k], r[k + 1]
        den = y0 - 2 * y1 + y2
        delta = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
        delta = float(np.clip(delta, -1, 1))
    else:
        delta = 0.0
    far = np.abs(lags - lags[k]) > 30  # 300 ms
    r2 = float(r[far].max()) if far.any() else 0.0
    sharp = max(0.0, 1.0 - max(0.0, r2) / r1) if r1 > 1e-6 else 0.0
    conf = float(np.clip(sharp * min(1.0, r1 / 0.25), 0, 1))
    return {"offset_ms": round((float(lags[k]) + delta) * HOP_MS, 2), "confidence": round(conf, 3), "correlation": round(r1, 3), "sharpness": round(sharp, 3)}


def sync_group(levels: list[np.ndarray], *, reference: int = 0, min_confidence: float = 0.35) -> list[dict[str, Any]]:
    """Group-time start of every recording (ms, the earliest at 0) from their level tracks. Recordings are linked along the most
    confident pairs (a recording that matches the reference badly can still match another one well), starting at ``reference``."""
    n = len(levels)
    pair: dict[tuple[int, int], dict[str, Any]] = {}
    for i in range(n):
        for j in range(n):
            if i < j:
                res = pair_offset(levels[i], levels[j])
                pair[(i, j)] = res
                pair[(j, i)] = {**res, "offset_ms": -res["offset_ms"]}
    start = {reference: 0.0}
    info: dict[int, dict[str, Any]] = {reference: {"confidence": 1.0, "correlation": 1.0, "via": None}}
    while len(start) < n:
        best: Optional[tuple[float, int, int]] = None
        for j in start:
            for i in range(n):
                if i not in start and (best is None or pair[(j, i)]["confidence"] > best[0]):
                    best = (pair[(j, i)]["confidence"], j, i)
        assert best is not None
        _, j, i = best
        start[i] = start[j] + pair[(j, i)]["offset_ms"]
        info[i] = {"confidence": pair[(j, i)]["confidence"], "correlation": pair[(j, i)]["correlation"], "via": j}
    low = min(start.values())
    return [{"start_ms": round(start[i] - low, 2), "confidence": info[i]["confidence"], "correlation": info[i]["correlation"], "via": info[i]["via"],
             "reliable": info[i]["confidence"] >= min_confidence} for i in range(n)]


# ---------------------------------------------------------------- who speaks

def level_matrix(levels: list[np.ndarray], starts_ms: list[float], times_ms: np.ndarray, *, window_ms: int = 100) -> np.ndarray:
    """dB above each recording's own noise floor, averaged over ``window_ms`` from every group time in ``times_ms``: one row per
    recording (-inf when the recording does not cover that moment)."""
    out = np.full((len(levels), times_ms.size), -np.inf)
    w = max(1, window_ms // HOP_MS)
    for i, (lv, s) in enumerate(zip(levels, starts_ms)):
        if lv.size == 0:
            continue
        floor = float(np.percentile(lv, 10))
        power = 10 ** (lv.astype(np.float64) / 10)
        cs = np.concatenate([[0.0], np.cumsum(power)])
        idx = np.round((times_ms - s) / HOP_MS).astype(np.int64)
        ok = (idx >= 0) & (idx + w <= lv.size)
        a = np.clip(idx, 0, lv.size)
        b = np.clip(idx + w, 0, lv.size)
        mean = (cs[b] - cs[a]) / np.maximum(1, b - a)
        db = 10 * np.log10(mean + 1e-12) - floor
        out[i] = np.where(ok, db, -np.inf)
    return out


def choose_shots(rel: np.ndarray, hop_ms: int, *, start_angle: Optional[int] = None, min_shot_ms: int = 2000, hysteresis_db: float = 4.0,
                 dwell_ms: int = 400, speech_db: float = 8.0, lead_ms: int = 150, wide: Optional[int] = None, wide_after_ms: int = 2500) -> list[tuple[int, int]]:
    """Shots [(start_ms, angle)] following the loudest recording relative to its own noise floor.

    A switch needs the new recording to lead the current one by ``hysteresis_db`` for ``dwell_ms`` (no flicker on interruptions),
    starts ``lead_ms`` before the speech so the picture is there when the voice is; when nobody is above ``speech_db`` the shot
    holds (or goes to the ``wide`` angle after ``wide_after_ms`` of silence); shots shorter than ``min_shot_ms`` are absorbed."""
    n_angles, steps = rel.shape
    if steps == 0:
        return []
    r = np.where(np.isfinite(rel), rel, -100.0)
    active = np.argmax(r, axis=0)
    loud = r[active, np.arange(steps)] >= speech_db
    cur = start_angle
    if cur is None:
        first = np.flatnonzero(loud)
        cur = int(active[first[0]]) if first.size else int(active[0])
    shots: list[list[int]] = [[0, cur]]
    pending_from: Optional[int] = None
    pending_angle = cur
    quiet_since: Optional[int] = None
    dwell = max(1, dwell_ms // hop_ms)
    for k in range(steps):
        t = k * hop_ms
        if not loud[k]:
            pending_from = None
            quiet_since = k if quiet_since is None else quiet_since
            if wide is not None and cur != wide and (k - quiet_since) * hop_ms >= wide_after_ms:
                cur = wide
                shots.append([t, cur])
            continue
        quiet_since = None
        best = int(active[k])
        if best != cur and r[best, k] - r[cur, k] >= hysteresis_db:
            if pending_from is None or pending_angle != best:
                pending_from, pending_angle = k, best
            if k - pending_from + 1 >= dwell:
                cut = max(shots[-1][0] + 1, pending_from * hop_ms - lead_ms)
                cur = best
                shots.append([cut, cur])
                pending_from = None
        else:
            pending_from = None
    return _absorb_short(shots, steps * hop_ms, min_shot_ms)


def _absorb_short(shots: list[list[int]], total_ms: int, min_ms: int) -> list[tuple[int, int]]:
    """Merge shots shorter than ``min_ms`` into the previous shot (the first one into the next); neighbours of the same angle join."""
    shots = [list(s) for s in shots]
    changed = True
    while changed and len(shots) > 1:
        changed = False
        ends = [s[0] for s in shots[1:]] + [total_ms]
        for i, (s, e) in enumerate(zip(shots, ends)):
            if e - s[0] < min_ms:
                if i == 0:
                    shots[1][0] = shots[0][0]
                    del shots[0]
                else:
                    del shots[i]
                changed = True
                break
        out: list[list[int]] = []
        for s in shots:
            if out and out[-1][1] == s[1]:
                continue
            out.append(s)
        if len(out) != len(shots):
            changed = True
        shots = out
    return [(s[0], s[1]) for s in shots]


def shots_from_turns(turns: list[tuple[int, int, int]], total_ms: int, *, start_angle: int, min_shot_ms: int = 2000, lead_ms: int = 150) -> list[tuple[int, int]]:
    """Shots from speaker turns [(t0, t1, angle)] (the angle each speaker is filmed by): the angle holds through pauses."""
    shots: list[list[int]] = [[0, start_angle]]
    for t0, _t1, angle in sorted(turns):
        if angle != shots[-1][1]:
            shots.append([max(shots[-1][0] + 1, t0 - lead_ms), angle])
    return _absorb_short(shots, total_ms, min_shot_ms)
