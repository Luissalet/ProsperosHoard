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
    for i in range(buckets):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        window = samples[a:b]  # no whole-song abs() copy: peak = max(max, -min)
        peaks.append(round(float(max(window.max(), -window.min())) if window.size else 0.0, 4))
    top = max(peaks) or 1.0
    return [round(min(1.0, p / top), 4) for p in peaks]


# ----------------------------------------------------------------------
# Onset envelope + tempo + beats
# ----------------------------------------------------------------------

def _padded_slice(samples: np.ndarray, a: int, z: int) -> np.ndarray:
    """`samples[a:z]` where an index before the start or past the end reads
    silence."""
    n = len(samples)
    parts = []
    if a < 0:
        parts.append(np.zeros(-a, dtype=np.float32))
        a = 0
    parts.append(samples[a:min(n, z)])
    if z > max(n, a):
        parts.append(np.zeros(z - max(n, a), dtype=np.float32))
    return np.concatenate(parts).astype(np.float32, copy=False)


def _frame_blocks(samples: np.ndarray, frame_size: int = FRAME_SIZE, hop: int = HOP, block: int = 256):
    """Yields (first frame index, float32 |rfft| of up to `block` frames).
    Centred frames (frame i is centred on sample i*hop), so an onset at t=0
    is visible and frame times need no latency correction. Built block by
    block from the samples themselves: a 20-minute song never needs a padded
    float64 copy or the whole magnitude spectrogram in memory."""
    samples = np.asarray(samples, dtype=np.float32)
    half = frame_size // 2
    n_frames = 1 + max(0, len(samples) + 2 * half - frame_size) // hop
    window = np.hanning(frame_size).astype(np.float32)
    for b0 in range(0, n_frames, block):
        b1 = min(n_frames, b0 + block)
        chunk = _padded_slice(samples, b0 * hop - half, (b1 - 1) * hop - half + frame_size)
        frames = np.lib.stride_tricks.sliding_window_view(chunk, frame_size)[::hop]
        yield b0, np.abs(np.fft.rfft(frames * window, axis=1)).astype(np.float32)


def _stft_mags(samples: np.ndarray, frame_size: int = FRAME_SIZE, hop: int = HOP) -> np.ndarray:
    """The whole magnitude spectrogram (frames x bins, float32)."""
    return np.concatenate([m for _, m in _frame_blocks(samples, frame_size, hop)])


def _frame_features(samples: np.ndarray, sr: int = SAMPLE_RATE) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """What the analysis needs per frame, without keeping the spectrogram:
    (40 log-spaced band magnitudes, the magnitudes below 200 Hz, and the
    power below 250 Hz / 250-2000 Hz / above 2000 Hz)."""
    low_hi = _band_bins(sr, FRAME_SIZE, 200.0)
    lo_b, mid_b = _band_bins(sr, FRAME_SIZE, 250.0), _band_bins(sr, FRAME_SIZE, 2000.0)
    bands, low, power = [], [], []
    for _, m in _frame_blocks(samples):
        bands.append(m @ _band_matrix(m.shape[1]).T)
        low.append(m[:, :low_hi].copy())
        pw = m.astype(np.float64) ** 2  # one block at a time
        power.append(np.stack([pw[:, :lo_b].sum(axis=1), pw[:, lo_b:mid_b].sum(axis=1), pw[:, mid_b:].sum(axis=1)], axis=1))
    return np.concatenate(bands), np.concatenate(low), np.concatenate(power)


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


_ONSET_HEAD_FRAMES = 8  # ~93 ms at 44.1 kHz / hop 512


def _flux(mags: np.ndarray, lo_bin: int = 0, hi_bin: Optional[int] = None) -> np.ndarray:
    if lo_bin == 0 and hi_bin is None:
        return _band_flux(mags @ _band_matrix(mags.shape[1]).T)
    return _band_flux(mags[:, lo_bin:hi_bin])


def _band_flux(bands: np.ndarray) -> np.ndarray:
    """Positive log-magnitude change per frame, summed over the columns of
    `bands` (frames x bands), detrended and scaled to 0..1."""
    band = np.log1p(100.0 * bands)
    # Frame 0 has no real predecessor. Comparing it with silence made any
    # sound already playing at t=0 (a noise bed, a held chord) a huge fake
    # onset there, which could pull the whole beat grid onto t=0; comparing
    # it with itself hid a real downbeat at 0. So it is compared with the
    # quietest of the next ~90 ms per band: a transient (kick, click) decays
    # and still counts, a held sound does not. (A tone that starts at full
    # level is a real click in the file and still reads as one.)
    head = band[1:1 + _ONSET_HEAD_FRAMES]
    first_prev = head.min(axis=0, keepdims=True) if len(head) else np.zeros_like(band[:1])
    prev = np.vstack([first_prev, band[:-1]])
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
        # only a real local maximum of the autocorrelation is refined (the
        # prior can pick a lag on a slope, where the parabola extrapolates
        # to a negative or huge period), and never by more than half a lag
        if y1 >= y0 and y1 >= y2 and denom < 0:
            best += float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))
    bpm = min(max(60.0 * fps / best, bpm_range[0]), bpm_range[1])
    return float(bpm), float(60.0 * fps / bpm)


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
    bands, low_bands, frame_power = _frame_features(samples, sr)
    env = _band_flux(bands)
    low_env = _band_flux(low_bands)
    fps = sr / HOP
    peak = max(float(samples.max()), -float(samples.min())) if samples.size else 0.0
    if peak < 1e-4 or env.max() == 0:
        return {"duration_s": duration_s, "tempo_bpm": None, "beat_times": [], "downbeats": [],
                "sections": [{"label": "section A", "start_s": 0.0, "end_s": duration_s, "energy": "low"}],
                "notes": "silent audio: no beats found"}
    bpm, period = estimate_tempo(env, sr)
    beat_times = track_beats(env, period, sr)
    beat_frames = [int(round(t * fps)) for t in beat_times]
    phase = _downbeat_phase(low_env, beat_frames)
    downbeats = beat_times[phase::4]
    sections = _detect_sections(samples, frame_power, sr, duration_s, downbeats)
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


def _detect_sections(samples: np.ndarray, frame_power: np.ndarray, sr: int, duration_s: float,
                     downbeats: list[float], min_bars: int = 2, novelty_db: float = 4.0) -> list[dict[str, Any]]:
    """Bars (from downbeats) -> per-bar loudness + three band levels in dB ->
    boundaries where the two bars before and after differ by more than
    `novelty_db` -> segments; segments that sound alike share a letter
    (A/B/A...). Energy is relative to the song's own loudness range.
    `frame_power`: per frame, the power of the three bands (see
    `_frame_features`)."""
    bounds = [0.0] + [t for t in downbeats if 0.25 < t < duration_s - 0.25] + [duration_s]
    if len(bounds) < 4:
        return [{"label": "section A", "start_s": 0.0, "end_s": duration_s, "energy": "mid"}]
    fps = sr / HOP
    feats = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        fa, fb = int(a * fps), max(int(a * fps) + 1, int(b * fps))
        seg = frame_power[fa:fb]
        sa, sb = int(a * sr), max(int(a * sr) + 1, int(b * sr))
        rms = float(np.sqrt(np.mean(samples[sa:sb].astype(np.float64) ** 2))) if sb > sa else 0.0
        bands = [float(v) for v in seg.mean(axis=0)]
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


def lrc_time(t: float) -> str:
    """`[mm:ss.xx]` for `t` seconds, rounded to the centisecond *before*
    splitting, so 59.996 s carries into the next minute (`[01:00.00]`)
    instead of printing an invalid `[00:60.00]`. Negative times clamp to 0."""
    total_cs = int(round(max(0.0, float(t)) * 100))
    minutes, cs = divmod(total_cs, 6000)
    return f"[{minutes:02d}:{cs // 100:02d}.{cs % 100:02d}]"


def to_lrc(lines: list[dict[str, Any]]) -> str:
    return "\n".join(f"{lrc_time(line['time_s'])}{line['text']}" for line in lines)


# ----------------------------------------------------------------------
# Lyric timing from the song structure
# ----------------------------------------------------------------------
#
# ACE-Step (and most songwriting) lyrics carry their own structure as
# `[Section]` tags. Before anyone taps the lines in by ear, that structure
# plus the song's bar grid already gives a musically plausible first pass:
# every line starts on a bar, rap verses move a line per bar, hooks and
# spoken intros/bridges breathe over two, and each section gets the share
# of the song its lines need. A section marker is written into the LRC as a
# timed tag line (`[01:01.71][Chorus]`) - the same thing tapping a
# `[Chorus]` line in Audio > Lyrics timing produces - so one file carries both the
# captions and the structure the auto-cut uses.

_SECTION_TAG = re.compile(r"^\[([^\]\d:][^\]]*)\]$")
_SECTION_KINDS = (
    ("pre", ("pre-chorus", "pre chorus", "prechorus", "pre-hook", "pre hook")),
    ("chorus", ("chorus", "hook", "estribillo", "coro")),
    ("intro", ("intro",)),
    ("outro", ("outro", "coda", "final")),
    ("bridge", ("bridge", "puente", "breakdown", "interlude", "break")),
    ("verse", ("verse", "verso", "estrofa", "rap")),
)
# bars one sung line takes, by section kind (a 4/4 bar at the song's tempo)
_BARS_PER_LINE = {"verse": 1, "pre": 2, "chorus": 2, "intro": 2, "bridge": 2, "outro": 2, "other": 2}
_ENERGY = {"chorus": "high", "verse": "mid", "pre": "mid", "intro": "low", "bridge": "low", "outro": "low", "other": "mid"}
# bars of music before the first line of an intro / after the last line of an outro
_LEAD_IN_BARS, _TAIL_BARS = 2, 2


def section_kind(label: str) -> str:
    low = label.strip().lower()
    for kind, words in _SECTION_KINDS:
        if any(low.startswith(w) for w in words):
            return kind
    return "other"


def section_energy(label: str) -> str:
    return _ENERGY[section_kind(label)]


def parse_structured_lyrics(text: str) -> list[dict[str, Any]]:
    """`[Section]` tags + lines -> [{"label", "lines": [...]}]. Lines before
    any tag form an untitled "Verse"; empty sections are dropped; existing
    `[mm:ss.xx]` stamps are stripped."""
    sections: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for raw in (text or "").splitlines():
        line = re.sub(r"^(\[\d+:\d+(?:\.\d+)?\])+", "", raw.strip()).strip()
        if not line:
            continue
        m = _SECTION_TAG.match(line)
        if m:
            current = {"label": m.group(1).strip(), "lines": []}
            sections.append(current)
            continue
        if current is None:
            current = {"label": "Verse", "lines": []}
            sections.append(current)
        current["lines"].append(line)
    return [s for s in sections if s["lines"]]


def _bar_grid(analysis: dict[str, Any], duration_s: float, bpm: Optional[float]) -> list[float]:
    downs = [float(d) for d in (analysis.get("downbeats") or []) if 0 <= float(d) < duration_s]
    if len(downs) >= 8:
        return downs
    tempo = float(bpm or analysis.get("tempo_bpm") or 120.0)
    bar = 4 * 60.0 / max(40.0, min(240.0, tempo))
    start = float((analysis.get("beat_times") or [0.0])[0])
    out, t = [], start
    while t < duration_s - 0.25:
        out.append(round(t, 3))
        t += bar
    return out or [0.0]


def _allocate(weights: list[float], total: int, minimums: list[int]) -> list[int]:
    """Integer shares of `total` proportional to `weights` (largest
    remainder), each at least its minimum while the total allows it."""
    if total <= 0:
        return [0] * len(weights)
    wsum = sum(weights) or 1.0
    raw = [total * w / wsum for w in weights]
    out = [max(m, int(r)) for r, m in zip(raw, minimums)]
    while sum(out) > total and any(o > 1 for o in out):
        i = max(range(len(out)), key=lambda k: out[k] - raw[k])
        out[i] -= 1
    order = sorted(range(len(out)), key=lambda k: raw[k] - out[k], reverse=True)
    k = 0
    while sum(out) < total and order:
        out[order[k % len(order)]] += 1
        k += 1
    return out


def _time_at_bar(bars: list[float], pos: float, bar_len: float) -> float:
    i = int(pos)
    if i >= len(bars) - 1:
        return bars[-1] + (pos - (len(bars) - 1)) * bar_len
    return bars[i] + (pos - i) * (bars[i + 1] - bars[i])


def time_lyrics(lyrics: str, analysis: dict[str, Any], bpm: Optional[float] = None) -> dict[str, Any]:
    """First-pass karaoke timing from the lyrics' own `[Section]` structure
    and the song's bar grid (downbeats from the analysis, else the tempo).

    Each section gets bars in proportion to what its lines need (one bar
    per rap-verse line, two per hook/pre-chorus/intro/bridge/outro line,
    plus a short instrumental lead-in and tail), scaled to the bars the song
    actually has; when the analysis found real section boundaries, each
    lyric section start snaps to the nearest one within two bars. Every
    line then starts on a bar. It is an estimate of where a line is sung,
    not vocal detection: re-time it by ear in Audio > Lyrics timing.

    Returns {"lrc", "lines": [{time_s, text, section}], "sections":
    [{label, kind, energy, start_s, end_s}]}."""
    duration = float(analysis.get("duration_s") or 0.0)
    if duration <= 0:
        raise ValueError("the song analysis has no duration")
    parsed = parse_structured_lyrics(lyrics)
    if not parsed:
        raise ValueError("no lyric lines to time")
    bars = _bar_grid(analysis, duration, bpm)
    n_bars = len(bars)
    kinds = [section_kind(s["label"]) for s in parsed]
    need = [len(s["lines"]) * _BARS_PER_LINE[k] for s, k in zip(parsed, kinds)]
    if kinds[0] == "intro":
        need[0] += _LEAD_IN_BARS
    if kinds[-1] == "outro":
        need[-1] += _TAIL_BARS
    shares = _allocate([float(x) for x in need], n_bars, [1] * len(parsed))
    starts, acc = [], 0
    for share in shares:
        starts.append(acc)
        acc += share

    # snap lyric section starts to the analysis' own boundaries (if any)
    found = [float(s["start_s"]) for s in (analysis.get("sections") or [])[1:]]
    bar_len = (bars[-1] - bars[0]) / max(1, n_bars - 1) if n_bars > 1 else 2.0
    for i in range(1, len(starts)):
        t = bars[min(starts[i], n_bars - 1)]
        near = [b for b in found if abs(b - t) <= 2 * bar_len + 0.05]
        if near:
            target = min(near, key=lambda b: abs(b - t))
            idx = min(range(n_bars), key=lambda j: abs(bars[j] - target))
            if starts[i - 1] < idx < (starts[i + 1] if i + 1 < len(starts) else n_bars):
                starts[i] = idx
    ends = starts[1:] + [n_bars]
    beats = [float(b) for b in (analysis.get("beat_times") or []) if 0 <= float(b) < duration]

    lines: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    lrc: list[str] = []
    for sec, kind, b0, b1 in zip(parsed, kinds, starts, ends):
        start_s = bars[min(b0, n_bars - 1)] if b0 > 0 else 0.0
        end_s = bars[b1] if b1 < n_bars else duration
        sections.append({"label": sec["label"], "kind": kind, "energy": _ENERGY[kind],
                         "start_s": round(start_s, 3), "end_s": round(end_s, 3)})
        lrc.append(to_lrc([{"time_s": start_s, "text": f"[{sec['label']}]"}]))
        first = b0 + (_LEAD_IN_BARS if kind == "intro" and b1 - b0 > len(sec["lines"]) + _LEAD_IN_BARS else 0)
        span = max(1, (b1 - (_TAIL_BARS if kind == "outro" and b1 - first > len(sec["lines"]) + _TAIL_BARS else 0)) - first)
        # more music than words: lines keep their natural pace from the
        # section start and the rest of the section is instrumental
        span = min(span, len(sec["lines"]) * _BARS_PER_LINE[kind])
        for j, text in enumerate(sec["lines"]):
            # a fractional bar position when the section has fewer bars than
            # lines (a crowded verse), snapped to the nearest beat so every
            # line still lands on the grid and no two share a start
            pos = first + j * span / len(sec["lines"])
            t = _time_at_bar(bars, pos, bar_len)
            if beats:
                t = min(beats, key=lambda b: abs(b - t))
            t = round(max(start_s, t, (lines[-1]["time_s"] + 0.25) if lines else 0.0), 3)
            lines.append({"time_s": t, "text": text, "section": sec["label"]})
            lrc.append(to_lrc([{"time_s": t, "text": text}]))
    return {"lrc": "\n".join(lrc) + "\n", "lines": lines, "sections": sections}


def lrc_sections(lines: list[dict[str, Any]], duration_s: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split parsed LRC lines into (sung lines, sections): a line whose whole
    text is a `[Section]` tag marks where that section starts. Sections end
    where the next begins (the last one at `duration_s`)."""
    sung, marks = [], []
    for line in lines:
        m = _SECTION_TAG.match(line["text"].strip())
        if m:
            marks.append({"label": m.group(1).strip(), "start_s": float(line["time_s"])})
        else:
            sung.append(line)
    sections = []
    for i, mark in enumerate(marks):
        end = marks[i + 1]["start_s"] if i + 1 < len(marks) else duration_s
        if end > mark["start_s"]:
            sections.append({"label": mark["label"], "kind": section_kind(mark["label"]),
                             "energy": section_energy(mark["label"]), "start_s": round(mark["start_s"], 3),
                             "end_s": round(end, 3)})
    return sung, sections
