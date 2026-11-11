"""Sound for a promo, written in code with numpy: a pulse at the tempo of the shots, a whoosh on every movement and a hit on the logo.

Everything is synthesised (no samples). `events_for(shots, ...)` turns the shot list of a promo into the list of sound events,
`synthesise(events, duration_s)` renders them to a stereo float array, `write_wav` saves it, and `master_wav` / `mux` use ffmpeg
(loudnorm, two passes, linear) to bring the whole mix to about -14 LUFS and put it into the video. Music of the user's own can be
mixed under the effects.
"""
from __future__ import annotations

import json
import re
import subprocess
import wave
from pathlib import Path
from typing import Any, Optional

import numpy as np

SR = 48000
TARGET_LUFS = -14.0
TRUE_PEAK_DB = -1.5
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class SoundError(RuntimeError):
    pass


# ------------------------------------------------------------------ events

PROFILES = ("pulse", "soft", "drive")
CHORDS = [(220.0, "min"), (174.61, "maj"), (261.63, "maj"), (196.0, "maj")]       # A minor, F, C, G: one chord per shot
BASS_ROOTS = [55.0, 43.65, 65.41, 49.0]
RHYTHMIC = ("beat", "clap", "hat", "bass", "tick", "pad")                            # the kinds a track of the user's own replaces


def events_for(shots: list[dict[str, Any]], bpm: float, duration_s: float, profile: str = "pulse") -> list[dict[str, Any]]:
    """The sound events of a promo. Profile "pulse": a beat on every pulse of the tempo (the first of each shot louder), a whoosh where each
    shot's wipe arrives and where its second line comes up, and a hit where the logo lands. "soft" (editorial): a slow chord under each shot,
    quiet ticks on the beat, a breath where the pages dissolve. "drive" (energetic): four on the floor, a clap on 2 and 4, hats between,
    a bass note per shot and short risers into every cut."""
    if profile == "soft":
        return _soft_events(shots, bpm, duration_s)
    if profile == "drive":
        return _drive_events(shots, bpm, duration_s)
    events: list[dict[str, Any]] = []
    period = 60.0 / max(40.0, float(bpm))
    first = shots[0]["start"] if shots else 0.0
    t, n = first, 0
    downbeats = {round(s["start"], 3) for s in shots}
    while t < duration_s - 0.05:
        strong = any(abs(t - d) < period * 0.25 for d in downbeats)
        events.append({"t": round(t, 4), "kind": "beat", "level": 1.0 if strong else 0.55})
        n += 1
        t = first + n * period
    for i, shot in enumerate(shots):
        start = float(shot["start"])
        side = 1 if i % 2 == 0 else -1
        if i > 0:
            events.append({"t": round(max(0.0, start - 0.14), 4), "kind": "whoosh", "dur": 0.55, "pan": side * 0.6, "level": 0.9})
        if shot.get("sub"):
            events.append({"t": round(start + 0.5, 4), "kind": "whoosh", "dur": 0.35, "pan": -side * 0.4, "level": 0.5})
        if shot.get("kind") == "logo":
            events.append({"t": round(start + 0.42, 4), "kind": "hit", "level": 1.0})
    events.sort(key=lambda e: (e["t"], e["kind"]))
    return events


def _soft_events(shots: list[dict[str, Any]], bpm: float, duration_s: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    period = 60.0 / max(40.0, float(bpm))
    first = shots[0]["start"] if shots else 0.0
    downbeats = {round(s["start"], 3) for s in shots}
    n = 0
    t = first
    while t < duration_s - 0.05:
        strong = any(abs(t - d) < period * 0.25 for d in downbeats)
        events.append({"t": round(t, 4), "kind": "tick", "level": 0.9 if strong else 0.4 if n % 2 == 0 else 0.0})
        n += 1
        t = first + n * period
    events = [e for e in events if e["level"] > 0]
    for i, shot in enumerate(shots):
        start = float(shot["start"])
        root, quality = CHORDS[i % len(CHORDS)]
        third = 1.189 if quality == "min" else 1.26
        events.append({"t": round(start, 4), "kind": "pad", "freqs": [root, root * third, root * 1.498, root * 2.0], "dur": float(shot["dur"]) + 0.9,
                       "level": 0.8})
        if i > 0:
            events.append({"t": round(max(0.0, start - 0.1), 4), "kind": "whoosh", "dur": 0.9, "pan": 0.3 if i % 2 else -0.3, "level": 0.32})
        if shot.get("kind") == "logo":
            events.append({"t": round(start + 0.5, 4), "kind": "hit", "level": 0.55})
    events.sort(key=lambda e: (e["t"], e["kind"]))
    return events


def _drive_events(shots: list[dict[str, Any]], bpm: float, duration_s: float) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    period = 60.0 / max(40.0, float(bpm))
    first = shots[0]["start"] if shots else 0.0
    downbeats = {round(s["start"], 3) for s in shots}
    n = 0
    t = first
    while t < duration_s - 0.05:
        strong = any(abs(t - d) < period * 0.25 for d in downbeats)
        events.append({"t": round(t, 4), "kind": "beat", "level": 1.0 if strong else 0.8})
        if n % 4 in (1, 3):
            events.append({"t": round(t, 4), "kind": "clap", "level": 0.8})
        if t + period / 2 < duration_s - 0.05:
            events.append({"t": round(t + period / 2, 4), "kind": "hat", "level": 0.55 if n % 2 == 0 else 0.4})
        n += 1
        t = first + n * period
    for i, shot in enumerate(shots):
        start = float(shot["start"])
        end = start + float(shot["dur"])
        root = BASS_ROOTS[i % len(BASS_ROOTS)]
        b = start
        k = 0
        while b < end - 0.05 and b < duration_s - 0.05:
            if k % 4 in (0, 2):
                events.append({"t": round(b + (period / 2 if k % 4 == 2 else 0.0), 4), "kind": "bass", "freq": root, "dur": period * 0.9, "level": 0.9})
            b += period
            k += 1
        if i > 0:
            events.append({"t": round(max(0.0, start - 0.3), 4), "kind": "whoosh", "dur": 0.34, "pan": 0.5 if i % 2 else -0.5, "level": 0.8})
        if shot.get("kind") == "logo":
            events.append({"t": round(start + 0.3, 4), "kind": "hit", "level": 1.0})
    events.sort(key=lambda e: (e["t"], e["kind"]))
    return events


# ------------------------------------------------------------------ voices

def _tick(level: float) -> np.ndarray:
    n = int(0.09 * SR)
    t = np.arange(n) / SR
    body = (np.sin(2 * np.pi * 1760.0 * t) + 0.4 * np.sin(2 * np.pi * 2640.0 * t)) * np.exp(-t * 70.0)
    return (body * level * 0.35).astype(np.float32)


def _pad(freqs: list[float], dur: float, level: float) -> np.ndarray:
    """A soft chord: a few slightly detuned sines per note, a slow attack and a long release."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for j, f in enumerate(freqs):
        for detune in (-0.0035, 0.0, 0.0035):
            out += np.sin(2 * np.pi * f * (1 + detune) * t + j) * (0.5 if f > 400 else 1.0)
    attack, release = min(0.7, dur * 0.4), min(0.9, dur * 0.4)
    env = np.minimum(1.0, t / max(attack, 1e-3)) * np.minimum(1.0, (dur - t) / max(release, 1e-3))
    return (out / (len(freqs) * 3.0) * env ** 2 * level * 0.9).astype(np.float32)


def _clap(level: float) -> np.ndarray:
    n = int(0.22 * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(21)
    noise = np.diff(rng.standard_normal(n + 1))                       # a first difference: the highs of white noise
    env = np.exp(-t * 28.0) + 0.7 * np.exp(-np.clip(t - 0.012, 0, None) * 60.0) * (t > 0.012) + 0.5 * np.exp(-np.clip(t - 0.026, 0, None) * 60.0) * (t > 0.026)
    return (noise * env * level * 0.22).astype(np.float32)


def _hat(level: float) -> np.ndarray:
    n = int(0.07 * SR)
    t = np.arange(n) / SR
    noise = np.diff(np.random.default_rng(5).standard_normal(n + 2), n=2)
    return (noise * np.exp(-t * 110.0) * level * 0.16).astype(np.float32)


def _bass(freq: float, dur: float, level: float) -> np.ndarray:
    n = int(max(0.12, dur) * SR)
    t = np.arange(n) / SR
    body = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(2 * np.pi * freq * 2 * t)
    env = np.minimum(1.0, t / 0.008) * np.exp(-t * 4.5)
    return (body * env * level * 0.5).astype(np.float32)


def _kick(level: float) -> np.ndarray:
    n = int(0.3 * SR)
    t = np.arange(n) / SR
    freq = 42.0 + 95.0 * np.exp(-t * 30.0)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 13.0)
    click = np.random.default_rng(7).standard_normal(n) * np.exp(-t * 700.0) * 0.12
    return ((body + click) * level).astype(np.float32)


def _noise_band(n: int, f0: float, f1: float, seed: int) -> np.ndarray:
    """White noise filtered by a band whose centre sweeps (on a log scale) from f0 to f1 over its length: block FFT, overlap-add."""
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n + 2048).astype(np.float64)
    block, hop = 1024, 512
    window = np.hanning(block)
    out = np.zeros_like(noise)
    freqs = np.fft.rfftfreq(block, 1.0 / SR)
    for pos in range(0, n + 1024, hop):
        if pos + block > noise.size:
            break
        frac = min(1.0, pos / max(1, n))
        centre = f0 * (f1 / f0) ** frac
        width = centre * 0.9
        mask = np.exp(-0.5 * ((freqs - centre) / width) ** 2)
        spec = np.fft.rfft(noise[pos:pos + block] * window) * mask
        out[pos:pos + block] += np.fft.irfft(spec, block) * window
    out = out[:n]
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak).astype(np.float32)


def _whoosh(dur: float, level: float, seed: int) -> np.ndarray:
    n = int(dur * SR)
    body = _noise_band(n, 280.0, 5200.0, seed)
    x = np.linspace(0.0, 1.0, n)
    env = np.sin(np.pi * np.clip(x ** 0.8, 0, 1)) ** 2          # swells, peaks a little after the middle, falls
    return (body * env * level * 0.8).astype(np.float32)


def _hit(level: float) -> np.ndarray:
    n = int(1.6 * SR)
    t = np.arange(n) / SR
    freq = 38.0 + 40.0 * np.exp(-t * 9.0)
    boom = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 3.4)
    burst = _noise_band(n, 3000.0, 400.0, 11) * np.exp(-t * 16.0) * 0.55
    ring = sum(np.sin(2 * np.pi * f * t) * a for f, a in ((392.0, 0.10), (392.0 * 2.76, 0.06), (392.0 * 5.4, 0.035))) * np.exp(-t * 6.0)
    return ((boom * 0.9 + burst + ring) * level).astype(np.float32)


def _place(mix: np.ndarray, voice: np.ndarray, at_s: float, pan: float = 0.0) -> None:
    start = int(round(at_s * SR))
    if start >= mix.shape[1] or start + voice.size <= 0:
        return
    lo = max(0, -start)
    hi = min(voice.size, mix.shape[1] - start)
    seg = voice[lo:hi]
    left = float(np.sqrt(0.5 * (1 - pan)))
    right = float(np.sqrt(0.5 * (1 + pan)))
    mix[0, start + lo:start + hi] += seg * left
    mix[1, start + lo:start + hi] += seg * right


def synthesise(events: list[dict[str, Any]], duration_s: float, *, pulse: bool = True, seed: int = 3) -> np.ndarray:
    """A stereo float array (2, samples) at 48 kHz with every event placed; peaks at 0.89 before mastering."""
    total = int(round((duration_s + 0.25) * SR))
    mix = np.zeros((2, total), dtype=np.float64)
    for i, e in enumerate(events):
        kind, level = e["kind"], float(e.get("level", 1.0))
        if kind == "beat" and pulse:
            _place(mix, _kick(0.42 * level), e["t"])
        elif kind == "whoosh":
            _place(mix, _whoosh(float(e.get("dur", 0.5)), 0.55 * level, seed + i), e["t"], float(e.get("pan", 0.0)))
        elif kind == "hit":
            _place(mix, _hit(0.7 * level), e["t"])
        elif kind == "tick" and pulse:
            _place(mix, _tick(level), e["t"])
        elif kind == "pad" and pulse:
            _place(mix, _pad(list(e["freqs"]), float(e["dur"]), level), e["t"])
        elif kind == "clap" and pulse:
            _place(mix, _clap(level), e["t"])
        elif kind == "hat" and pulse:
            _place(mix, _hat(level), e["t"], 0.2 if i % 2 else -0.2)
        elif kind == "bass" and pulse:
            _place(mix, _bass(float(e["freq"]), float(e.get("dur", 0.3)), level), e["t"])
    fade = int(0.35 * SR)
    end = int(round(duration_s * SR))
    if end < total:
        mix[:, end:] = 0.0
    if end > fade:
        mix[:, end - fade:end] *= np.linspace(1.0, 0.0, fade)
    mix = np.tanh(mix * 1.15) / np.tanh(1.15)               # a soft ceiling instead of a hard clip
    peak = float(np.max(np.abs(mix))) or 1.0
    return (mix / peak * 0.89).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray) -> Path:
    data = np.clip(audio.T, -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return path


# ------------------------------------------------------------------ music of the user's own

def decode_music(ffmpeg: str, source: Path, duration_s: float) -> np.ndarray:
    """The user's track as stereo 48 kHz float, looped or cut to the length, with a fade out; set to a steady level under the effects."""
    proc = subprocess.run([ffmpeg, "-v", "error", "-i", str(source), "-vn", "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
                          capture_output=True, timeout=300, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not proc.stdout:
        raise SoundError("the music file could not be read: " + proc.stderr.decode("utf-8", "replace")[-300:])
    raw = np.frombuffer(proc.stdout, dtype="<f4").reshape(-1, 2).T.astype(np.float64)
    need = int(round(duration_s * SR))
    if raw.shape[1] < need:
        reps = int(np.ceil(need / max(1, raw.shape[1])))
        raw = np.tile(raw, (1, reps))
    raw = raw[:, :need]
    rms = float(np.sqrt(np.mean(raw ** 2))) or 1.0
    raw = raw * (0.09 / rms)
    fade = min(need, int(1.2 * SR))
    raw[:, need - fade:] *= np.linspace(1.0, 0.0, fade)
    return raw.astype(np.float32)


def mix_with_music(effects: np.ndarray, music: np.ndarray) -> np.ndarray:
    n = max(effects.shape[1], music.shape[1])
    out = np.zeros((2, n), dtype=np.float64)
    out[:, :music.shape[1]] += music
    out[:, :effects.shape[1]] += effects * 0.9
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak * 0.89).astype(np.float32)


# ------------------------------------------------------------------ mastering with ffmpeg

def _json_block(text: str) -> dict[str, Any]:
    found = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.S)
    if not found:
        raise SoundError("loudnorm gave no measurement")
    return json.loads(found[-1])


def master_wav(ffmpeg: str, source: Path, destination: Path, lufs: float = TARGET_LUFS, true_peak: float = TRUE_PEAK_DB) -> dict[str, Any]:
    """Two-pass loudnorm (measure, then apply linearly) to about `lufs` integrated with a true-peak ceiling."""
    base = f"loudnorm=I={lufs}:TP={true_peak}:LRA=11"
    first = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(source), "-af", base + ":print_format=json", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=300, creationflags=_NO_WINDOW)
    try:
        m = _json_block(first.stderr)
        measured = (f"{base}:measured_I={m['input_i']}:measured_LRA={m['input_lra']}:measured_TP={m['input_tp']}"
                    f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    except (SoundError, KeyError, ValueError):
        measured = base
    second = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-y", "-i", str(source), "-af", measured, "-ar", str(SR), "-ac", "2",
                             "-c:a", "pcm_s16le", str(destination)], capture_output=True, text=True, timeout=300, creationflags=_NO_WINDOW)
    if second.returncode != 0:
        raise SoundError("mastering failed: " + second.stderr[-300:])
    got = measure(ffmpeg, destination)
    mode = "linear"
    if got["lufs"] is not None and abs(got["lufs"] - lufs) > 0.7:
        # sparse effects: the peaks stop the linear gain short of the target; the dynamic mode rides the gain to get closer
        trial = destination.with_name(destination.stem + ".dyn.wav")
        again = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-y", "-i", str(destination), "-af", f"loudnorm=I={lufs}:TP={true_peak}:LRA=7",
                                "-ar", str(SR), "-ac", "2", "-c:a", "pcm_s16le", str(trial)], capture_output=True, text=True, timeout=300,
                               creationflags=_NO_WINDOW)
        if again.returncode == 0:
            other = measure(ffmpeg, trial)
            if other["lufs"] is not None and abs(other["lufs"] - lufs) < abs(got["lufs"] - lufs) and (other["true_peak_db"] or 0) <= -1.0:
                trial.replace(destination)
                got, mode = other, "dynamic"
        trial.unlink(missing_ok=True)
    return {"target_lufs": lufs, "true_peak_db": true_peak, "lufs": got["lufs"], "mode": mode}


def master_for_delivery(ffmpeg: str, source: Path, destination: Path, lufs: float = TARGET_LUFS, ceiling: float = -1.0) -> dict[str, Any]:
    """`master_wav`, then the check that matters: the master is encoded as the AAC that goes into the video and measured again. AAC overshoots a
    dense, loud mix (drums) by a dB or more above the wav's true peak, so when the encoded peak is above `ceiling` the master is made again
    with a lower true-peak target (three tries at most)."""
    ceil_now = TRUE_PEAK_DB
    info: dict[str, Any] = {}
    for _ in range(3):
        info = master_wav(ffmpeg, source, destination, lufs, ceil_now)
        probe = destination.with_name(destination.stem + ".aac-check.m4a")
        subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(destination), "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), str(probe)],
                       capture_output=True, text=True, timeout=300, creationflags=_NO_WINDOW)
        got = measure(ffmpeg, probe) if probe.exists() else {"lufs": None, "true_peak_db": None}
        probe.unlink(missing_ok=True)
        info["aac_true_peak_db"] = got["true_peak_db"]
        if got["true_peak_db"] is None or got["true_peak_db"] <= ceiling:
            break
        ceil_now = round(ceil_now - (got["true_peak_db"] - ceiling) - 0.3, 2)
    info["tp_target"] = ceil_now
    return info


def measure(ffmpeg: str, path: Path) -> dict[str, Optional[float]]:
    """Integrated loudness (LUFS) and true peak (dBTP) of the audio of a file, from ffmpeg's ebur128."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=300, creationflags=_NO_WINDOW)
    tail = proc.stderr.split("Summary:")[-1]
    lufs = re.search(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", tail)
    peak = re.search(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", tail)
    return {"lufs": float(lufs.group(1)) if lufs else None, "true_peak_db": float(peak.group(1)) if peak else None}


def mux(ffmpeg: str, video: Path, wav: Path, destination: Path) -> None:
    """The picture is copied; the sound goes in as AAC 192k at 48 kHz."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", str(video), "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-shortest", "-movflags", "+faststart", str(destination)],
                          capture_output=True, text=True, timeout=600, creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise SoundError("could not put the sound into the video: " + proc.stderr[-300:])
