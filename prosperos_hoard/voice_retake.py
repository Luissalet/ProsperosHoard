"""Redo some words of a voice line without regenerating the rest.

A voice line's word timings (from ``voice_transcribe``: ``[{start_s, end_s,
word}]``) say where the words to redo sit. Only the replacement text is
synthesized; the new stretch is trimmed of its own leading and trailing
silence and spliced into the original between the pauses that surround the
picked words, with a short equal-power crossfade at each join. Everything
outside the cut keeps the original samples bit for bit.
"""
from __future__ import annotations

import io
import wave
from typing import Any, Optional

import numpy as np

CROSSFADE_S = 0.025
EDGE_PAD_S = 0.05
SILENCE_RATIO = 0.03
KEEP_PAD_S = 0.02
SNAP_WINDOW_S = 0.12
VALLEY_WINDOW_S = 0.10
VALLEY_LOUD = 0.4
VALLEY_PROMINENCE = 0.08
GLUED_GAP_S = 0.03


class RetakeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (float32 samples shaped (frames, channels) in [-1, 1], sample rate)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as fh:
            channels, width, rate = fh.getnchannels(), fh.getsampwidth(), fh.getframerate()
            raw = fh.readframes(fh.getnframes())
    except (wave.Error, EOFError) as exc:
        raise RetakeError("bad_wav", f"not a PCM WAV file: {exc}") from None
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float32) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise RetakeError("bad_wav", f"unsupported sample width {width}")
    return x.reshape(-1, channels), rate


def encode_wav(samples: np.ndarray, rate: int) -> bytes:
    """(frames, channels) float32 -> 16-bit PCM WAV bytes (the inverse of decode_wav for 16-bit
    input, so untouched samples of a 16-bit line come back identical)."""
    pcm = np.clip(np.round(samples * 32768.0), -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(int(samples.shape[1]))
        fh.setsampwidth(2)
        fh.setframerate(int(rate))
        fh.writeframes(pcm.tobytes())
    return buf.getvalue()


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(x) == 0:
        return x
    n = max(1, int(round(len(x) * dst / src)))
    pos = np.linspace(0, len(x) - 1, n)
    return np.stack([np.interp(pos, np.arange(len(x)), x[:, c]) for c in range(x.shape[1])], axis=1).astype(np.float32)


def _match_channels(x: np.ndarray, channels: int) -> np.ndarray:
    if x.shape[1] == channels:
        return x
    mono = x.mean(axis=1, keepdims=True)
    return np.repeat(mono, channels, axis=1)


def trim_silence(x: np.ndarray, rate: int, ratio: float = SILENCE_RATIO, keep_s: float = KEEP_PAD_S) -> np.ndarray:
    """Drop the leading and trailing quiet part (below ``ratio`` of the peak), keeping ``keep_s`` of margin."""
    if len(x) == 0:
        return x
    level = np.abs(x).max(axis=1)
    peak = float(level.max())
    if peak <= 0:
        raise RetakeError("silent_retake", "the synthesized replacement is silent")
    loud = np.flatnonzero(level >= peak * ratio)
    keep = int(keep_s * rate)
    return x[max(0, loud[0] - keep): min(len(x), loud[-1] + 1 + keep)]


def word_cut(words: list[dict[str, Any]], first: int, last: int, duration_s: float) -> tuple[float, float]:
    """Where to cut for words[first..last] (inclusive): halfway into the pause before and after them."""
    if not words:
        raise RetakeError("no_words", "the line has no word timings; transcribe it with word timestamps first")
    if not (0 <= first <= last < len(words)):
        raise RetakeError("bad_word_range", f"pick words between 0 and {len(words) - 1} (first <= last)")
    w0, w1 = words[first], words[last]
    s, e = float(w0["start_s"]), float(w1["end_s"])
    t0 = (float(words[first - 1]["end_s"]) + s) / 2 if first > 0 else max(0.0, s - EDGE_PAD_S)
    t1 = (e + float(words[last + 1]["start_s"])) / 2 if last + 1 < len(words) else min(duration_s, e + EDGE_PAD_S)
    t0, t1 = max(0.0, min(t0, s)), min(duration_s, max(t1, e))
    if t1 <= t0:
        raise RetakeError("bad_word_range", "those words have no duration in the timings")
    return t0, t1


def _join(a: np.ndarray, b: np.ndarray, n: int) -> np.ndarray:
    """a then b, overlapping n frames with an equal-power crossfade."""
    n = min(n, len(a), len(b))
    if n <= 0:
        return np.concatenate([a, b])
    t = np.linspace(0.0, np.pi / 2, n, dtype=np.float32)[:, None]
    mid = a[-n:] * np.cos(t) + b[:n] * np.sin(t)
    return np.concatenate([a[:-n], mid, b[n:]])


def snap_to_quiet(x: np.ndarray, rate: int, t: float, window_s: float = SNAP_WINDOW_S) -> int:
    """The silent 10 ms frame nearest to ``t`` within ``window_s`` (as a sample index), or ``t`` itself
    when there is no real silence there. Only true pauses count (every sample under SILENCE_RATIO of
    the line's peak): the dips inside a word are not pauses, and cutting there leaves half a word."""
    hop = max(1, int(0.01 * rate))
    centre = int(round(t * rate))
    lo, hi = max(0, centre - int(window_s * rate)), min(len(x), centre + int(window_s * rate))
    if hi - lo < 2 * hop or centre <= 0 or centre >= len(x):
        return max(0, min(centre, len(x)))
    level = np.abs(x).max(axis=1)
    floor = float(level.max()) * SILENCE_RATIO
    window = level[lo:hi]
    n = (len(window) - hop) // hop + 1
    silent = np.flatnonzero(np.array([float(window[k * hop:k * hop + hop].max()) <= floor for k in range(n)]))
    if len(silent) == 0:
        return centre
    starts = lo + silent * hop + hop // 2
    return int(starts[np.argmin(np.abs(starts - centre))])


def valley_after_glued_boundary(x: np.ndarray, rate: int, t: float, direction: int,
                                window_s: float = VALLEY_WINDOW_S,
                                prominence: float = VALLEY_PROMINENCE) -> Optional[int]:
    """For two words run together with no pause, the transcriber tends to put the boundary early, inside
    the end of the first word (``Mañana|iremos`` ends ``Mañana`` at 0.30 s while its last vowel runs
    to about 0.38 s), so a cut there leaves half a syllable behind or eats one. When the timing falls
    on a loud spot (at least ``VALLEY_LOUD`` of the line's loudest 20 ms frame), look from ``t`` in
    ``direction`` (+1 later, -1 earlier) for the nearest clear dip of the 20 ms loudness envelope: a
    local minimum at least ``prominence`` under the louder frames on both sides within 50 ms. Returns its
    sample index, or None when there is no such dip and the timing should stay."""
    hop = max(1, int(0.01 * rate))
    win = 2 * hop
    mono = x.mean(axis=1) if x.ndim == 2 else x
    n = (len(mono) - win) // hop + 1
    if n < 5:
        return None
    env = np.sqrt(np.array([float(np.mean(mono[k * hop:k * hop + win] ** 2)) for k in range(n)]))
    peak = float(env.max())
    if peak <= 0:
        return None
    env = env / peak
    centre = int(round(t * rate / hop - 1))  # frame k is centred on (k + 1) * hop
    if not (0 <= centre < n) or env[centre] < VALLEY_LOUD:
        return None  # the timing already falls somewhere quiet: nothing is being cut in half
    reach = int(round(window_s * rate / hop))
    lo, hi = (centre, centre + reach) if direction > 0 else (centre - reach, centre)
    lo, hi = max(1, lo), min(n - 2, hi)
    side = 5
    best = None
    for k in range(lo, hi + 1):
        if env[k] > env[k - 1] or env[k] > env[k + 1]:
            continue
        left = float(env[max(0, k - side):k].max())
        right = float(env[k + 1:k + 1 + side].max())
        if min(left, right) - float(env[k]) < prominence:
            continue
        if best is None or abs(k - centre) < abs(best - centre):
            best = k
    if best is None:
        return None
    # plateau: take the middle of equal minima
    end = best
    while end + 1 <= hi and env[end + 1] == env[best]:
        end += 1
    return int(((best + end) / 2 + 1) * hop)


def splice(original: bytes, replacement: bytes, t0: float, t1: float,
           crossfade_s: float = CROSSFADE_S, snap: bool = True,
           glued_start: bool = False, glued_end: bool = False) -> tuple[bytes, dict[str, Any]]:
    """Replace original[t0:t1] with the trimmed replacement. Returns (wav bytes, report). With ``snap``
    each cut moves to a real pause near it (within the line). ``glued_start``/``glued_end`` say the
    word on that side runs into its neighbour with no pause in the timings: when there is no real pause
    either and the timing falls mid-sound, that cut moves later to the nearest clear loudness dip,
    where the two words actually meet."""
    x, rate = decode_wav(original)
    y, yrate = decode_wav(replacement)
    y = _match_channels(_resample(trim_silence(y, yrate), yrate, rate), x.shape[1])
    how = ["timing", "timing"]
    if snap:
        i0, i1 = snap_to_quiet(x, rate, t0), snap_to_quiet(x, rate, t1)
        for side, (t, i, glued, direction) in enumerate(((t0, i0, glued_start, 1), (t1, i1, glued_end, 1))):
            if i != int(round(t * rate)):
                how[side] = "pause"
            elif glued:
                v = valley_after_glued_boundary(x, rate, t, direction)
                if v is not None:
                    how[side] = "valley"
                    if side == 0:
                        i0 = v
                    else:
                        i1 = v
    else:
        i0, i1 = int(round(t0 * rate)), int(round(t1 * rate))
    i0, i1 = max(0, min(i0, len(x))), max(0, min(i1, len(x)))
    if i1 <= i0:
        raise RetakeError("bad_cut", "the cut is empty")
    cf = int(crossfade_s * rate)
    # silence margins so the crossfades run over pause, not over the new word's onset or tail
    pad = np.zeros((cf, x.shape[1]), dtype=np.float32)
    patch = np.concatenate([pad, y, pad])
    out = _join(_join(x[:i0], patch, cf), x[i1:], cf)
    new_dur = len(out) / rate
    report = {"cut_start_s": round(i0 / rate, 3), "cut_end_s": round(i1 / rate, 3),
              "timing_cut_s": [round(t0, 3), round(t1, 3)], "snapped": bool(snap), "cut_by": how,
              "replacement_s": round(len(y) / rate, 3), "duration_before_s": round(len(x) / rate, 3),
              "duration_after_s": round(new_dur, 3), "shift_after_cut_s": round(new_dur - len(x) / rate, 3),
              "sample_rate": rate, "untouched_before_s": round(max(0, i0 - cf) / rate, 3)}
    return encode_wav(out, rate), report


def shifted_words(words: list[dict[str, Any]], first: int, last: int, new_text: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Word timings after the retake: words before the cut stay, the new words share the replacement's span
    (approximate until the line is transcribed again), words after the cut move by the length change."""
    shift = float(report["shift_after_cut_s"])
    start = float(report["cut_start_s"]) + CROSSFADE_S
    span = float(report["replacement_s"])
    toks = new_text.split() or [new_text]
    step = span / len(toks)
    new = [{"start_s": round(start + i * step, 2), "end_s": round(start + (i + 1) * step, 2), "word": t, "estimated": True}
           for i, t in enumerate(toks)]
    after = [{**w, "start_s": round(float(w["start_s"]) + shift, 2), "end_s": round(float(w["end_s"]) + shift, 2)}
             for w in words[last + 1:]]
    return list(words[:first]) + new + after


def flatten_words(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """The word list of a ``voice_transcribe`` result (``segments[].words``)."""
    out: list[dict[str, Any]] = []
    for seg in transcript.get("segments") or []:
        for w in seg.get("words") or []:
            out.append({"start_s": float(w["start_s"]), "end_s": float(w["end_s"]), "word": str(w.get("word", "")).strip()})
    return out


def retake_words(original: bytes, words: list[dict[str, Any]], first: int, last: int, new_text: str,
                 synthesize, duration_s: Optional[float] = None) -> dict[str, Any]:
    """Synthesize only ``new_text`` (``synthesize(text) -> wav bytes``) and splice it over words[first..last]."""
    if not new_text.strip():
        raise RetakeError("empty_text", "give the replacement words")
    if duration_s is None:
        x, rate = decode_wav(original)
        duration_s = len(x) / rate
    t0, t1 = word_cut(words, first, last, duration_s)
    glued_start = first > 0 and float(words[first]["start_s"]) - float(words[first - 1]["end_s"]) < GLUED_GAP_S
    glued_end = last + 1 < len(words) and float(words[last + 1]["start_s"]) - float(words[last]["end_s"]) < GLUED_GAP_S
    wav, report = splice(original, synthesize(new_text.strip()), t0, t1,
                         glued_start=glued_start, glued_end=glued_end)
    replaced = " ".join(str(w.get("word", "")).strip() for w in words[first:last + 1])
    return {"wav": wav, "report": {**report, "replaced": replaced, "new_text": new_text.strip(),
                                   "first_word": first, "last_word": last},
            "words": shifted_words(words, first, last, new_text.strip(), report)}
