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

def events_for(shots: list[dict[str, Any]], bpm: float, duration_s: float) -> list[dict[str, Any]]:
    """The sound events of a promo: a beat on every pulse of the tempo (the first of each shot louder), a whoosh where each shot's
    wipe arrives and where its second line comes up, and a hit where the logo lands."""
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


# ------------------------------------------------------------------ voices

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
