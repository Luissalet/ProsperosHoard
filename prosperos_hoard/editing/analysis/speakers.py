"""Who speaks when: speaker separation (diarization) of a word-level transcript, entirely local.

Two engines:

* **embeddings** (optional, ``requirements-speakers.txt``): when speechbrain's ECAPA model or resemblyzer is installed, every
  speech segment gets a voice embedding and segments are grouped by cosine distance. Nothing is downloaded from a gated source.
* **built in** (always there): numpy only. A voice is a long-term trait, so it is measured over *turns*, never over single
  words: (1) consecutive words are grouped into segments (a pause over 300 ms or 6 s ends one; shorter in a short clip) and each
  gets the level-weighted mean of its log-mel cepstra plus the median pitch; (2) the segments are split by 2-means, and a cut
  counts as two voices only when the gap between its halves beats what chance produces in ONE voice with that many segments
  (a Gaussian null with the same spread) and each voice keeps a real share of the talk; (3) the changes of speaker are placed
  with a Viterbi pass over ~1 s pieces and then over single words, with a penalty per change, so a one-word flicker never becomes
  a turn. One real person with natural prosody stays one speaker; clearly different voices separate; voices a couple of
  semitones apart may merge (ask for the number of speakers if you know it).

Everything here is a pure function of the audio samples and the words; ``speakers.py`` does the storing.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import numpy as np

log = logging.getLogger("lumiere.speakers")

RATE = 16000
WIN = 400          # 25 ms
HOP = 160          # 10 ms
NFFT = 512
PITCH_WIN = 640    # 40 ms: two periods of the lowest voice
PITCH_NFFT = 2048
N_MEL = 26
N_CEP = 13        # c1..c13: the long-term spectral shape of a voice (averaged over seconds the vowels cancel out and the voice stays)
MAX_SPEAKERS = 8
PALETTE = ["#4FC3F7", "#FFB74D", "#81C784", "#F06292", "#BA68C8", "#4DB6AC", "#FFD54F", "#A1887F"]


# ---------------------------------------------------------------- features

def _mel_bank() -> np.ndarray:
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    pts = mel_to_hz(np.linspace(hz_to_mel(100.0), hz_to_mel(7600.0), N_MEL + 2))
    bins = np.floor((NFFT + 1) * pts / RATE).astype(int)
    bank = np.zeros((N_MEL, NFFT // 2 + 1))
    for i in range(N_MEL):
        a, b, c = bins[i], bins[i + 1], bins[i + 2]
        if b > a:
            bank[i, a:b] = (np.arange(a, b) - a) / (b - a)
        if c > b:
            bank[i, b:c] = (c - np.arange(b, c)) / (c - b)
    return bank


def _dct_matrix() -> np.ndarray:
    n = np.arange(N_MEL)
    return np.array([np.cos(np.pi * k * (2 * n + 1) / (2 * N_MEL)) for k in range(1, N_CEP + 1)])  # c1..c4 (c0 is loudness)


_BANK = _mel_bank()
_DCT = _dct_matrix()
_HAMMING = np.hamming(WIN)
_PITCH_HANN = np.hanning(PITCH_WIN)


def frame_features(audio: np.ndarray, frames: Optional[np.ndarray] = None, block: int = 1500) -> dict[str, np.ndarray]:
    """Per 10 ms frame: cepstra c1..c12, f0 in Hz (0 = unvoiced), voicing strength and level in dB. ``frames`` limits the work to the
    given frame indexes (the frames of words); pitch needs a longer window than the cepstra, centred on the same frame."""
    audio = np.asarray(audio, dtype=np.float32)
    n_frames = max(0, (audio.size - WIN) // HOP + 1)
    idx = np.arange(n_frames) if frames is None else np.asarray(frames, dtype=np.int64)
    idx = idx[(idx >= 0) & (idx < n_frames)]
    cep = np.zeros((idx.size, N_CEP), dtype=np.float32)
    f0 = np.zeros(idx.size, dtype=np.float32)
    voicing = np.zeros(idx.size, dtype=np.float32)
    level = np.full(idx.size, -120.0, dtype=np.float32)
    pre = np.concatenate([audio[:1], audio[1:] - 0.97 * audio[:-1]]) if audio.size else audio
    lo_lag, hi_lag = int(RATE / 400), int(RATE / 70)
    for s in range(0, idx.size, block):
        part = idx[s: s + block]
        starts = part * HOP
        take = starts[:, None] + np.arange(WIN)[None, :]
        raw = audio[take]
        level[s: s + part.size] = 10 * np.log10(np.mean(raw.astype(np.float64) ** 2, axis=1) + 1e-12)
        spec = np.abs(np.fft.rfft(pre[take] * _HAMMING, NFFT, axis=1)) ** 2
        logmel = np.log(spec @ _BANK.T + 1e-10)
        cep[s: s + part.size] = logmel @ _DCT.T
        centre = starts + WIN // 2
        a = centre[:, None] - PITCH_WIN // 2 + np.arange(PITCH_WIN)[None, :]
        a = np.clip(a, 0, audio.size - 1)
        seg = audio[a] * _PITCH_HANN
        seg = seg - seg.mean(axis=1, keepdims=True)
        f = np.fft.rfft(seg, PITCH_NFFT, axis=1)
        ac = np.fft.irfft(np.abs(f) ** 2, PITCH_NFFT, axis=1)[:, : hi_lag + 2]
        r0 = ac[:, 0] + 1e-9
        r = ac / r0[:, None]
        win_r = r[:, lo_lag: hi_lag + 1]
        # the first strong peak, not the highest: the second period is as high and would halve the pitch
        best = win_r.max(axis=1)
        cand = win_r >= (0.88 * best)[:, None]
        # local maxima only
        peak = np.zeros_like(cand)
        peak[:, 1:-1] = (win_r[:, 1:-1] >= win_r[:, :-2]) & (win_r[:, 1:-1] >= win_r[:, 2:])
        pick = np.argmax(cand & peak, axis=1)
        ok = (cand & peak)[np.arange(part.size), pick]
        lag = (pick + lo_lag).astype(np.float64)
        # parabolic refinement of the peak
        k = np.clip(pick, 1, win_r.shape[1] - 2)
        y0, y1, y2 = win_r[np.arange(part.size), k - 1], win_r[np.arange(part.size), k], win_r[np.arange(part.size), k + 1]
        den = y0 - 2 * y1 + y2
        shift = np.where(np.abs(den) > 1e-9, 0.5 * (y0 - y2) / np.where(den == 0, 1, den), 0.0)
        lag = lag + np.where(k == pick, np.clip(shift, -1, 1), 0.0)
        voiced = ok & (best > 0.45)
        f0[s: s + part.size] = np.where(voiced, RATE / np.maximum(lag, 1.0), 0.0)
        voicing[s: s + part.size] = best
    return {"frames": idx, "cep": cep, "f0": f0, "voicing": voicing, "level": level}


# ---------------------------------------------------------------- units: what gets a voice measurement

def frame_table(audio: np.ndarray, words: list[dict[str, Any]]) -> dict[str, Any]:
    """Frame features over the span of the words, plus the level below which a frame is not speech (breaths, room noise)."""
    a = max(0, int(min(w["t0"] for w in words) / 10))
    b = int(max(w["t1"] for w in words) / 10) + 2
    ff = frame_features(audio, np.arange(a, b))
    floor = float(np.percentile(ff["level"], 95)) - 28.0 if ff["level"].size else -100.0
    return {"ff": ff, "floor": floor, "start": int(ff["frames"][0]) if ff["frames"].size else a}


def unit_features(ft: dict[str, Any], spans: list[tuple[int, int]], min_frames: int = 12) -> tuple[np.ndarray, np.ndarray]:
    """One feature row per time span (ms): the level-weighted mean cepstrum of its voiced speech frames and the median pitch, plus
    a ``valid`` mask (spans with too little voiced speech get no measurement and take the evidence of their neighbours)."""
    ff, floor, s0 = ft["ff"], ft["floor"], ft["start"]
    level = ff["level"].astype(np.float64)
    act = level > floor
    voiced = act & (ff["f0"] > 0)
    w_voi = np.where(voiced, np.clip(level - floor + 1.0, 1.0, None), 0.0)
    w_act = np.where(act, np.clip(level - floor + 1.0, 1.0, None), 0.0)
    cep = ff["cep"].astype(np.float64)
    zero = np.zeros((1, N_CEP))
    c_voi = np.concatenate([zero, np.cumsum(cep * w_voi[:, None], axis=0)])
    c_act = np.concatenate([zero, np.cumsum(cep * w_act[:, None], axis=0)])
    s_voi = np.concatenate([[0.0], np.cumsum(w_voi)])
    s_act = np.concatenate([[0.0], np.cumsum(w_act)])
    n_voi = np.concatenate([[0], np.cumsum(voiced)])
    n_act = np.concatenate([[0], np.cumsum(act)])
    strong = voiced & (ff["voicing"] > 0.6)
    logf0 = np.where(strong, np.log(np.maximum(ff["f0"], 1.0)), np.nan)
    n = len(spans)
    feats = np.zeros((n, N_CEP + 1))
    valid = np.zeros(n, dtype=bool)
    for i, (t0, t1) in enumerate(spans):
        lo = int(np.clip(round(t0 / 10) - s0, 0, level.size))
        hi = int(np.clip(max(round(t1 / 10), round(t0 / 10) + 1) - s0, lo, level.size))
        if n_voi[hi] - n_voi[lo] >= min_frames:
            feats[i, :N_CEP] = (c_voi[hi] - c_voi[lo]) / (s_voi[hi] - s_voi[lo])
        elif n_act[hi] - n_act[lo] >= min_frames:
            feats[i, :N_CEP] = (c_act[hi] - c_act[lo]) / (s_act[hi] - s_act[lo])
        else:
            feats[i, N_CEP] = np.nan
            continue
        pitch = logf0[lo:hi]
        pitch = pitch[~np.isnan(pitch)]
        feats[i, N_CEP] = float(np.median(pitch)) if pitch.size >= 6 else np.nan
        valid[i] = True
    return feats, valid


def group_words(words: list[dict[str, Any]], *, gap_ms: int, max_ms: int, speech_ms: int = 0, within: Optional[list[tuple[int, int]]] = None) -> list[tuple[int, int]]:
    """Index ranges [a, b) of consecutive words: a pause longer than ``gap_ms`` ends a group, and so does the group reaching
    ``max_ms`` in length or ``speech_ms`` of speech. ``within`` splits only inside the given ranges."""
    out: list[tuple[int, int]] = []
    for lo, hi in within or [(0, len(words))]:
        a, speech = lo, 0
        for i in range(lo, hi):
            speech += max(0, words[i]["t1"] - words[i]["t0"])
            last = i == hi - 1
            if last or words[i + 1]["t0"] - words[i]["t1"] > gap_ms or words[i + 1]["t1"] - words[a]["t0"] > max_ms or (speech_ms and speech >= speech_ms):
                out.append((a, i + 1))
                a, speech = i + 1, 0
    return out


def span_of(words: list[dict[str, Any]], groups: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(words[a]["t0"], words[b - 1]["t1"]) for a, b in groups]


def standardise(feats: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray, float]]:
    """Centre and scale the feature columns on the valid rows (a missing pitch becomes the median pitch). Returns the matrix and
    the transform, so atoms and words are measured on the same scale as the segments."""
    f = feats.copy()
    known = ~np.isnan(f[:, N_CEP])
    fill = float(np.median(f[valid & known, N_CEP])) if (valid & known).any() else 0.0
    f[:, N_CEP] = np.where(known, f[:, N_CEP], fill)
    ref = f[valid] if valid.any() else f
    med = np.median(ref, axis=0)
    sd = ref.std(axis=0) + 1e-9
    return (f - med) / sd, (med, sd, fill)


def apply_transform(feats: np.ndarray, tr: tuple[np.ndarray, np.ndarray, float]) -> np.ndarray:
    med, sd, fill = tr
    f = feats.copy()
    f[:, N_CEP] = np.where(np.isnan(f[:, N_CEP]), fill, f[:, N_CEP])
    return (f - med) / sd


# ---------------------------------------------------------------- how many voices, and which segments are whose

SPLIT_D = 3.45       # two groups are two voices when the gap between them is this many (pooled) spreads along the line joining them...
SPLIT_D_SMALL = 4.0  # ...plus this / n for few segments: a single voice's natural variation (pitch, loudness, room) peaks around 3.3
NULL_MARGIN = 0.5    # added to the chance level of the gap
NULL_CAP = 4.6       # with a handful of segments (a short clip) chance alone reaches any gap; a real conversation still has to pass this one
FEW_SEGMENTS = 20
MIN_SPLIT = 6        # segments a group needs before it may be split again
MIN_SHARE = 0.08     # a voice that speaks less than this share of the talk, or less than MIN_TALK_MS, is a stray piece of another voice
MIN_TALK_MS = 4000
SWITCH_COST = 9.0    # log-likelihood a change of speaker must win to be believed: a one-second blip does not
PCA_DIMS = 4


def _pca(z: np.ndarray, dims: int) -> np.ndarray:
    c = z - z.mean(axis=0)
    _, _, vt = np.linalg.svd(c, full_matrices=False)
    return c @ vt[: max(1, min(dims, vt.shape[0]))].T


def _two_means(y: np.ndarray, restarts: int = 8) -> tuple[np.ndarray, float]:
    """The best split into two groups (k-means, deterministic restarts) and how clean it is: the distance between the group centres
    over the pooled spread of the groups along the line that joins them (about 2.6 for one Gaussian blob cut in half)."""
    rng = np.random.default_rng(0)
    best: Optional[tuple[float, np.ndarray, np.ndarray]] = None
    for _ in range(restarts):
        c = y[rng.choice(y.shape[0], 2, replace=False)].copy()
        a = np.zeros(y.shape[0], dtype=np.int64)
        for _ in range(40):
            a = ((y[:, None, :] - c[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
            for j in (0, 1):
                if (a == j).any():
                    c[j] = y[a == j].mean(axis=0)
        sse = float(((y - c[a]) ** 2).sum())
        if best is None or sse < best[0]:
            best = (sse, a, c.copy())
    assert best is not None
    _, a, c = best
    if min((a == 0).sum(), (a == 1).sum()) < 2:
        return a, 0.0
    axis = c[0] - c[1]
    axis = axis / (np.linalg.norm(axis) + 1e-12)
    p = y @ axis
    d = np.sqrt(2.0) * abs(p[a == 0].mean() - p[a == 1].mean()) / np.sqrt(p[a == 0].var() + p[a == 1].var() + 1e-12)
    return a, float(d)


def null_gap(z: np.ndarray, sims: int = 40, quantile: float = 0.95) -> float:
    """How big a gap 2-means finds by chance in ONE voice with this many segments and this spread of features: the same cut on
    random Gaussian data with the same covariance. With few segments chance alone produces big gaps, so the bar rises."""
    n = z.shape[0]
    _, sv, vt = np.linalg.svd(z - z.mean(axis=0), full_matrices=False)
    scale = sv / np.sqrt(max(n - 1, 1))
    rng = np.random.default_rng(7)
    out = []
    for _ in range(sims):
        r = rng.normal(size=(n, scale.size)) * scale
        out.append(_two_means(_pca(r, PCA_DIMS), restarts=3)[1])
    return float(np.quantile(out, quantile))


def chance_bar(z: np.ndarray, short_clip: bool) -> float:
    """The gap a cut of these segments must exceed to count as two voices: what chance produces in one voice, plus a margin (capped
    for a short clip, where chance could reach any gap and no conversation would ever pass)."""
    bar = null_gap(z) + NULL_MARGIN
    return min(bar, NULL_CAP) if short_clip else bar


def split_segments(z: np.ndarray, *, num_speakers: Optional[int], max_speakers: int) -> tuple[np.ndarray, list[float]]:
    """Group the segments (rows of ``z``) into voices: by repeatedly cutting the loosest group in two. Without a given number, a
    group is cut only while the cut passes the separation test; with one, until that many groups exist."""
    n = z.shape[0]
    lab = np.zeros(n, dtype=np.int64)
    seps: list[float] = []
    k = 1
    limit = max(1, min(num_speakers or max_speakers, MAX_SPEAKERS, n))
    tried: set[int] = set()
    while k < limit:
        best: Optional[tuple[float, int, np.ndarray]] = None
        for j in range(k):
            idx = np.flatnonzero(lab == j)
            if j in tried or idx.size < (2 if num_speakers else MIN_SPLIT):
                continue
            a, d = _two_means(_pca(z[idx], PCA_DIMS))
            seps.append(round(d, 2))
            need = 0.0 if num_speakers else max(SPLIT_D + SPLIT_D_SMALL / idx.size, chance_bar(z[idx], n < FEW_SEGMENTS))
            # with a given number the loosest group is cut (most scatter around its centre); without one, the clearest cut wins
            rank = float(((z[idx] - z[idx].mean(0)) ** 2).sum()) if num_speakers else d
            if (d >= need and min((a == 0).sum(), (a == 1).sum()) >= (1 if num_speakers else 3)) and (best is None or rank > best[0]):
                best = (rank, j, idx[a == 1])
            elif not num_speakers:
                tried.add(j)
        if best is None:
            break
        lab[best[2]] = k
        k += 1
    return lab, seps


def _viterbi(ll: np.ndarray, cost: float) -> np.ndarray:
    """Most likely speaker path through ``ll`` (units x speakers log-likelihoods) when every change of speaker costs ``cost``."""
    n, k = ll.shape
    score = ll[0].copy()
    back = np.zeros((n, k), dtype=np.int64)
    for i in range(1, n):
        stay = score
        jump = score.max() - cost
        arg = int(score.argmax())
        take = stay >= jump
        back[i] = np.where(take, np.arange(k), arg)
        score = np.where(take, stay, jump) + ll[i]
    path = np.zeros(n, dtype=np.int64)
    path[-1] = int(score.argmax())
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return path


def _fit(f: np.ndarray, lab: np.ndarray, ok: np.ndarray, k: int, shrink: float = 0.35) -> tuple[np.ndarray, np.ndarray]:
    """Voice centres and the shared within-voice precision matrix (shrunk towards its diagonal: few segments, many features)."""
    d = f.shape[1]
    mu = np.zeros((k, d))
    resid = []
    for j in range(k):
        sel = ok & (lab == j)
        if sel.any():
            mu[j] = f[sel].mean(axis=0)
            resid.append(f[sel] - mu[j])
        elif ok.any():
            mu[j] = f[ok].mean(axis=0)
    r = np.concatenate(resid) if resid else np.zeros((1, d))
    cov = r.T @ r / max(1, r.shape[0])
    cov = (1 - shrink) * cov + shrink * np.diag(np.diag(cov)) + 1e-6 * np.eye(d)
    return mu, np.linalg.inv(cov)


def _loglik(f: np.ndarray, ok: np.ndarray, mu: np.ndarray, prec: np.ndarray) -> np.ndarray:
    diff = f[:, None, :] - mu[None, :, :]
    ll = -0.5 * np.einsum("nkd,de,nke->nk", diff, prec, diff)
    ll[~ok] = 0.0  # no measurement: no opinion, the neighbours decide
    return ll


def refine(f: np.ndarray, ok: np.ndarray, lab: np.ndarray, k: int, cost: float, rounds: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """Viterbi re-segmentation: fit the voices on the current labels, find the best path with a cost per change, repeat."""
    ll = np.zeros((f.shape[0], k))
    for _ in range(rounds):
        mu, prec = _fit(f, lab, ok, k)
        ll = _loglik(f, ok, mu, prec)
        new = _viterbi(ll, cost)
        if (new == lab).all():
            break
        lab = new
    return lab, ll


def diarize_builtin(audio: np.ndarray, words: list[dict[str, Any]], *, num_speakers: Optional[int], max_speakers: int) -> dict[str, Any]:
    """Built-in engine: cluster speech *turns* (3-6 s stretches), not words; then place the changes of speaker with a Viterbi pass
    over ~1 s pieces, then over single words near the changes. See the module docstring."""
    n = len(words)
    ft = frame_table(audio, words)
    talk_ms = sum(max(0, w["t1"] - w["t0"]) for w in words)
    # turns are up to 6 s; in a short clip they are cut shorter so that there are enough of them to tell voices apart
    segs = group_words(words, gap_ms=300, max_ms=int(min(6000, max(1200, talk_ms / 24))))
    atoms = group_words(words, gap_ms=300, max_ms=2500, speech_ms=1000, within=segs)
    f_seg, ok_seg = unit_features(ft, span_of(words, segs))
    f_atom, ok_atom = unit_features(ft, span_of(words, atoms))
    f_word, ok_word = unit_features(ft, span_of(words, [(i, i + 1) for i in range(n)]), min_frames=5)
    if ok_seg.sum() < 2 or ok_atom.sum() < 2:
        return {"labels": np.zeros(n, dtype=np.int64), "k": 1, "confidence": 0.0, "heights": []}
    z_seg, tr = standardise(f_seg, ok_seg)
    z_atom = apply_transform(f_atom, tr)
    z_word = apply_transform(f_word, tr)
    # 1) which voices: split the valid segments
    valid_idx = np.flatnonzero(ok_seg)
    lab_v, seps = split_segments(z_seg[valid_idx], num_speakers=num_speakers, max_speakers=max_speakers)
    k = int(lab_v.max()) + 1
    lab_seg = np.zeros(len(segs), dtype=np.int64)
    lab_seg[valid_idx] = lab_v
    for i in np.flatnonzero(~ok_seg):  # a segment without a measurement follows the nearest measured one
        lab_seg[i] = lab_seg[valid_idx[np.argmin(np.abs(valid_idx - i))]]
    lab_atom = np.zeros(len(atoms), dtype=np.int64)
    seg_of_word = np.zeros(n, dtype=np.int64)
    for s, (a, b) in enumerate(segs):
        seg_of_word[a:b] = s
    for i, (a, _b) in enumerate(atoms):
        lab_atom[i] = lab_seg[seg_of_word[a]]
    dur = np.array([max(0, w["t1"] - w["t0"]) for w in words], dtype=np.float64)
    atom_of_word = np.zeros(n, dtype=np.int64)
    for i, (a, b) in enumerate(atoms):
        atom_of_word[a:b] = i
    while True:
        # 2) when they change: one-second pieces, then words
        if k > 1:
            lab_atom, _ = refine(z_atom, ok_atom, lab_atom, k, SWITCH_COST)
        lab_word = lab_atom[atom_of_word]
        if k > 1:
            lab_word, _ = refine(z_word, ok_word, lab_word, k, SWITCH_COST * 0.8)
        talk = np.array([dur[lab_word == j].sum() for j in range(k)])
        if num_speakers or k == 1:
            break
        least = max(MIN_SHARE * talk.sum(), min(MIN_TALK_MS, 0.2 * talk.sum()))  # in a short clip a few seconds are a fair share
        weak = [j for j in range(k) if talk[j] < least]
        if not weak:
            break
        # a "voice" with almost no talk is a stray piece of another one: fold it into the nearest voice and look again
        drop = min(weak, key=lambda j: talk[j])
        mu, _ = _fit(z_atom, lab_atom, ok_atom, k)
        near = min((j for j in range(k) if j != drop), key=lambda j: float(np.linalg.norm(mu[j] - mu[drop])))
        lab_atom = np.where(lab_atom == drop, near, lab_atom)
        lab_atom = np.where(lab_atom > drop, lab_atom - 1, lab_atom)
        k -= 1
    ll = np.zeros((len(atoms), max(k, 1)))
    conf = 0.0
    if k > 1:
        mu, prec = _fit(z_atom, lab_atom, ok_atom, k)
        ll = _loglik(z_atom, ok_atom, mu, prec)
        two = np.sort(ll[ok_atom], axis=1)
        margin = two[:, -1] - two[:, -2]
        conf = float(np.mean(margin / (margin + 4.0)))
    return {"labels": lab_word, "k": k, "confidence": round(conf, 3), "heights": seps}


# ---------------------------------------------------------------- clustering

def agglomerate(points: np.ndarray, sizes: np.ndarray) -> tuple[list[tuple[int, int, float]], int]:
    """Average-linkage clustering of weighted points: the merges [(a, b, height), ...] in order (ids >= len(points) are earlier
    merges) and the number of points."""
    m = points.shape[0]
    d = np.sqrt(((points[:, None, :] - points[None, :, :]) ** 2).sum(axis=2))
    np.fill_diagonal(d, np.inf)
    size = {i: float(sizes[i]) for i in range(m)}
    alive = np.ones(m, dtype=bool)
    merges: list[tuple[int, int, float]] = []
    nxt = m
    node = np.arange(m)  # row -> cluster id
    for _ in range(m - 1):
        masked = np.where(alive[:, None] & alive[None, :], d, np.inf)
        flat = int(np.argmin(masked))
        i, j = divmod(flat, m)
        h = float(masked[i, j])
        merges.append((int(node[i]), int(node[j]), h))
        si, sj = size[int(node[i])], size[int(node[j])]
        new = (si * d[i] + sj * d[j]) / (si + sj)
        d[i, :] = new
        d[:, i] = new
        d[i, i] = np.inf
        alive[j] = False
        size[nxt] = si + sj
        node[i] = nxt
        nxt += 1
    return merges, m


def cut_tree(merges: list[tuple[int, int, float]], m: int, k: int) -> np.ndarray:
    """Cluster index (0..k-1) of every original point when the tree is cut into ``k`` clusters."""
    parent = list(range(m + len(merges)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for step, (a, b, _) in enumerate(merges[: max(0, m - k)]):
        parent[find(a)] = m + step
        parent[find(b)] = m + step
    roots: dict[int, int] = {}
    out = np.zeros(m, dtype=np.int64)
    for i in range(m):
        out[i] = roots.setdefault(find(i), len(roots))
    return out


# ---------------------------------------------------------------- optional embedding engines

_EMBEDDER: Any = False


def embedder_status() -> dict[str, Any]:
    """Which voice-embedding library is installed: speechbrain (ECAPA), resemblyzer or none."""
    for name in ("speechbrain", "resemblyzer"):
        try:
            __import__(name)
            return {"available": True, "engine": name}
        except Exception:  # noqa: BLE001 - not installed, or its dependencies (torch) are broken
            continue
    return {"available": False, "engine": None}


def _load_embedder() -> Optional[Callable[[np.ndarray], np.ndarray]]:
    global _EMBEDDER
    if _EMBEDDER is not False:
        return _EMBEDDER
    _EMBEDDER = None
    try:
        from speechbrain.inference.speaker import EncoderClassifier  # type: ignore
        import torch  # type: ignore

        model = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", run_opts={"device": "cpu"})

        def embed(wav: np.ndarray) -> np.ndarray:
            with torch.no_grad():
                e = model.encode_batch(torch.from_numpy(wav.astype(np.float32))[None, :])
            return e.squeeze().cpu().numpy()

        _EMBEDDER = embed
        return _EMBEDDER
    except Exception as error:  # noqa: BLE001
        log.info("speechbrain is not usable (%s)", error)
    try:
        from resemblyzer import VoiceEncoder  # type: ignore

        enc = VoiceEncoder(device="cpu")
        _EMBEDDER = lambda wav: enc.embed_utterance(wav.astype(np.float32))  # noqa: E731
    except Exception as error:  # noqa: BLE001
        log.info("resemblyzer is not usable (%s)", error)
    return _EMBEDDER


def speech_chunks(words: list[dict[str, Any]], gap_ms: int = 350, max_ms: int = 8000) -> list[tuple[int, int]]:
    """Index ranges [a, b) of words that form one chunk of continuous speech (pause or length limit ends it)."""
    out: list[tuple[int, int]] = []
    a = 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i]["t0"] - words[i - 1]["t1"] > gap_ms or words[i]["t1"] - words[a]["t0"] > max_ms:
            out.append((a, i))
            a = i
    return out


def _embedding_labels(audio: np.ndarray, words: list[dict[str, Any]], embed: Callable[[np.ndarray], np.ndarray], num_speakers: Optional[int],
                      max_speakers: int) -> Optional[dict[str, Any]]:
    chunks = speech_chunks(words)
    vecs, owners = [], []
    for a, b in chunks:
        t0, t1 = words[a]["t0"], words[b - 1]["t1"]
        if t1 - t0 < 400:
            continue
        wav = audio[int(t0 * RATE / 1000): int(t1 * RATE / 1000)]
        if wav.size < RATE // 3:
            continue
        v = np.asarray(embed(wav), dtype=np.float64).ravel()
        vecs.append(v / (np.linalg.norm(v) + 1e-9))
        owners.append((a, b))
    if len(vecs) < 2:
        return None
    emb = np.array(vecs)
    merges, m = agglomerate(emb, np.array([b - a for a, b in owners], dtype=np.float64))
    if num_speakers:
        k = max(1, min(num_speakers, m, MAX_SPEAKERS))
    else:
        # cosine distance of unit vectors: sqrt(2 * (1 - cos)); different voices sit above 0.9 (cosine below ~0.6)
        k = 1
        for kk in range(2, min(max_speakers, MAX_SPEAKERS, m) + 1):
            h = merges[m - kk][2] if m - kk < len(merges) else 0.0
            if h >= 1.0:
                k = kk
    lab = cut_tree(merges, m, k)
    labels = np.zeros(len(words), dtype=np.int64)
    centroids = [emb[lab == j].mean(axis=0) for j in range(k)]
    for (a, b), lb in zip(owners, lab):
        labels[a:b] = lb
    # words of chunks too short to embed: nearest embedded chunk
    covered = np.zeros(len(words), dtype=bool)
    for a, b in owners:
        covered[a:b] = True
    for i in np.flatnonzero(~covered):
        j = int(np.argmin([min(abs(i - a), abs(i - (b - 1))) for a, b in owners]))
        labels[i] = lab[j]
    sims = [float(np.max([np.dot(e, c) / (np.linalg.norm(c) + 1e-9) for c in centroids])) for e in emb]
    return {"labels": labels, "k": k, "confidence": round(float(np.mean(sims)), 3), "heights": []}


# ---------------------------------------------------------------- entry point

def diarize(audio: np.ndarray, words: list[dict[str, Any]], *, num_speakers: Optional[int] = None, max_speakers: int = 6,
            engine: str = "auto") -> dict[str, Any]:
    """Speaker index per word, ordered by first appearance. ``engine``: auto or embeddings (voice embeddings when a library is
    installed, else built in), builtin (always the built-in engine)."""
    if not words:
        return {"labels": [], "k": 0, "method": "none", "confidence": 0.0}
    if num_speakers is not None and num_speakers < 1:
        raise ValueError("num_speakers must be at least 1.")
    method = "builtin"
    res: Optional[dict[str, Any]] = None
    if engine in ("auto", "embeddings"):
        embed = _load_embedder()
        if embed is not None:
            try:
                res = _embedding_labels(audio, words, embed, num_speakers, max_speakers)
                method = "embeddings"
            except Exception as error:  # noqa: BLE001 - fall back rather than fail the job
                log.warning("embedding diarization failed (%s); using the built-in engine", error)
                res = None
    if res is None:
        method = "builtin"
        res = diarize_builtin(audio, words, num_speakers=num_speakers, max_speakers=max_speakers)
    labels = np.asarray(res["labels"], dtype=np.int64)
    order: dict[int, int] = {}
    for lab in labels:
        order.setdefault(int(lab), len(order))
    mapped = [order[int(lab)] for lab in labels]
    return {"labels": mapped, "k": len(order), "method": method, "confidence": res["confidence"], "heights": res.get("heights", [])}

