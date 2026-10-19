"""Synthetic voices and cameras for the speaker and multicam tests: harmonic stacks shaped by formant-like resonances (each
voice has its own pitch and formants, every word its own vowel colour), written as WAV and muxed into coloured videos."""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 16000

VOICES = {
    "A": {"f0": 118.0, "formants": (680.0, 1180.0, 2500.0)},   # low, "male-ish"
    "B": {"f0": 212.0, "formants": (540.0, 1900.0, 3050.0)},   # high, "female-ish"
    "C": {"f0": 158.0, "formants": (820.0, 1500.0, 2200.0)},   # in between
}


def voice_word(voice: dict, dur_s: float, rng: np.random.Generator, sr: int = SR) -> np.ndarray:
    """One word: a voiced burst with vibrato, a per-word vowel colour (formants +-12 %) and intonation (pitch +-6 %)."""
    n = int(dur_s * sr)
    t = np.arange(n) / sr
    f0 = voice["f0"] * rng.uniform(0.94, 1.06)
    vib = 1 + 0.015 * np.sin(2 * np.pi * rng.uniform(4.5, 6.0) * t + rng.uniform(0, 6.28)) + rng.uniform(-0.03, 0.03) * (t / max(dur_s, 1e-3))
    phase = np.cumsum(2 * np.pi * f0 * vib / sr)
    formants = [f * rng.uniform(0.88, 1.12) for f in voice["formants"]]
    out = np.zeros(n)
    h = 1
    while f0 * h < 4800:
        freq = f0 * h
        amp = sum(np.exp(-0.5 * ((freq - f) / (0.18 * f + 60)) ** 2) * g for f, g in zip(formants, (1.0, 0.7, 0.4)))
        out += (amp / h ** 0.6) * np.sin(h * phase)
        h += 1
    env = np.minimum(1, t / 0.02) * np.minimum(1, (dur_s - t) / 0.04) * (0.85 + 0.15 * np.sin(2 * np.pi * 3.5 * t))
    out *= np.clip(env, 0, 1)
    return out / (np.abs(out).max() + 1e-9)


def conversation(turns: list[tuple[str, int]], seed: int = 1, gap: tuple[float, float] = (0.05, 0.35)) -> tuple[np.ndarray, list[dict]]:
    """Alternating turns [(speaker, words)] -> (audio 16 kHz, words [{t0, t1, text, speaker}] with the true speaker)."""
    rng = np.random.default_rng(seed)
    chunks: list[np.ndarray] = []
    words: list[dict] = []
    t = 0.3
    chunks.append(np.zeros(int(t * SR)))
    for who, n in turns:
        for _ in range(n):
            dur = float(rng.uniform(0.22, 0.55))
            w = voice_word(VOICES[who], dur, rng) * 0.4
            words.append({"t0": int(round(t * 1000)), "t1": int(round((t + dur) * 1000)), "text": f"w{len(words) + 1}", "speaker": who})
            chunks.append(w)
            g = float(rng.uniform(*gap))
            chunks.append(np.zeros(int(g * SR)))
            t += dur + g
    chunks.append(np.zeros(int(0.4 * SR)))
    audio = np.concatenate(chunks)
    audio += rng.normal(0, 0.0015, audio.size)
    return audio.astype(np.float32), words


def natural_talk(turns: list[tuple[str, float]], seed: int = 1, drift: float = 0.05, loud: float = 0.25, couple: float = 0.3) -> tuple[np.ndarray, list[dict]]:
    """Longer, messier speech than ``conversation``: turns [(speaker, seconds)], short words (0.12-0.55 s) with almost no pause
    between them (as a transcriber times them), a pitch that wanders slowly (about ``drift`` either way) and loudness that wanders too, the way one real
    person's delivery varies over minutes. Returns (audio, words [{t0, t1, text, speaker}])."""
    rng = np.random.default_rng(seed)
    chunks: list[np.ndarray] = [np.zeros(int(0.3 * SR))]
    words: list[dict] = []
    t = 0.3
    pitch_walk = loud_walk = 0.0  # slow random walks (a bell-shaped spread of values, like a real delivery), not a regular wave
    for who, seconds in turns:
        end = t + seconds
        while t < end:
            dur = float(rng.choice([0.12, 0.16, 0.2, 0.28, 0.35, 0.45, 0.55], p=[0.1, 0.15, 0.2, 0.25, 0.15, 0.1, 0.05]))
            keep = float(np.exp(-(dur + 0.1) / 20.0))
            pitch_walk = keep * pitch_walk + float(np.sqrt(1 - keep * keep)) * rng.normal()
            loud_walk = keep * loud_walk + float(np.sqrt(1 - keep * keep)) * rng.normal()
            wander = 1 + drift * float(np.clip(pitch_walk, -2.5, 2.5)) / 1.5
            voice = {"f0": VOICES[who]["f0"] * wander, "formants": tuple(f * (1 + couple * (wander - 1)) for f in VOICES[who]["formants"])}
            gain = 0.4 * float(np.clip(1 + loud * loud_walk, 0.4, 1.6)) * rng.uniform(0.8, 1.2)
            chunks.append(voice_word(voice, dur, rng) * gain)
            words.append({"t0": int(round(t * 1000)), "t1": int(round((t + dur) * 1000)), "text": f"w{len(words) + 1}", "speaker": who})
            g = float(rng.choice([0.0, 0.0, 0.01, 0.03, 0.08, 0.4], p=[0.3, 0.2, 0.2, 0.15, 0.1, 0.05]))
            chunks.append(np.zeros(int(g * SR)))
            t += dur + g
    chunks.append(np.zeros(int(0.4 * SR)))
    audio = np.concatenate(chunks)
    audio += rng.normal(0, 0.002, audio.size)
    return audio.astype(np.float32), words


def write_wav(path: Path, audio: np.ndarray, sr: int = SR) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def video_with_audio(path: Path, wav: Path, color: str, seconds: float, size: str = "320x180", fps: int = 25) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r={fps}:d={seconds:.3f}",
                    "-i", str(wav), "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True)


def two_person_event(total_s: float = 26.0, seed: int = 3) -> tuple[np.ndarray, np.ndarray, list[tuple[float, float, str]]]:
    """Two people talking in turns: (voice of A, voice of B, [(start_s, end_s, 'A'|'B')]) at 16 kHz on one shared event clock."""
    rng = np.random.default_rng(seed)
    plan = [(0.6, 5.8, "A"), (6.4, 11.6, "B"), (12.2, 15.8, "A"), (16.4, 20.0, "B"), (20.6, 24.0, "A")]
    tracks = {"A": np.zeros(int(total_s * SR)), "B": np.zeros(int(total_s * SR))}
    for t0, t1, who in plan:
        t = t0
        while t < t1 - 0.3:
            dur = float(rng.uniform(0.25, 0.5))
            w = voice_word(VOICES[who], min(dur, t1 - t), rng) * 0.5
            i = int(t * SR)
            tracks[who][i: i + w.size] += w
            t += dur + float(rng.uniform(0.05, 0.2))
    return tracks["A"], tracks["B"], plan


def camera_audio(own: np.ndarray, other: np.ndarray, *, own_gain: float = 1.0, other_gain: float = 0.12, noise: float = 0.004, seed: int = 0) -> np.ndarray:
    """What one camera's microphone hears: its own person loud, the other one faint, plus its own noise floor."""
    rng = np.random.default_rng(seed)
    return (own_gain * own + other_gain * other + rng.normal(0, noise, own.size)).astype(np.float32)
