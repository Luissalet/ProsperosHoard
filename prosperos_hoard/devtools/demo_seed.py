"""Seeds `--demo` mode: an original (invented) idol group, generated
photocards via the fake ComfyUI backend, an album cover, a synthetic
30s song with a real 120 BPM beat, and an auto-cut preview render.

No real people or franchises are referenced anywhere in this module.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import numpy as np

from .. import engine
from ..backend import Backend
from ..store import Store

CHARACTERS = [
    {"name": "Iris Volt", "role": "Leader / Main Vocal",
     "prompt": "a confident young pop idol with short platinum hair and sharp eyeliner, stage lighting",
     "negative": "extra limbs, blurry face", "palette": ["#ff4d8d", "#1c1024"]},
    {"name": "Mika Frost", "role": "Lead Dancer",
     "prompt": "an energetic idol with icy blue twin-tails and athletic build, dynamic pose",
     "negative": "extra limbs, blurry face", "palette": ["#6cc0ff", "#0b1a24"]},
    {"name": "Suri Nova", "role": "Rapper",
     "prompt": "a cool idol with an undercut and gold hoop earrings, streetwear stage outfit",
     "negative": "extra limbs, blurry face", "palette": ["#f5c26b", "#241a0b"]},
    {"name": "Lena Echo", "role": "Vocalist",
     "prompt": "a soft-spoken idol with long wavy auburn hair, pastel stage outfit",
     "negative": "extra limbs, blurry face", "palette": ["#c98bf0", "#1a0b24"]},
    {"name": "Tessa Rune", "role": "Visual / Maknae",
     "prompt": "the youngest idol with bright red bob haircut and playful expression",
     "negative": "extra limbs, blurry face", "palette": ["#ff6b6b", "#240b0f"]},
]

GROUP_NAME = "Neon Static"
GROUP_CONCEPT = "A five-member synth-pop idol unit built around a shared 'neon storm' visual identity."


def _no_progress(*_a: Any, **_k: Any) -> None:
    return None


def _make_synthetic_song(path: Path, bpm: float = 120.0, duration_s: float = 30.0, sr: int = 44100) -> None:
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    beat_period = 60.0 / bpm
    signal = np.zeros(n, dtype=np.float64)

    # four-on-the-floor kick
    kick_env = np.exp(-np.arange(int(sr * 0.15)) / (sr * 0.03))
    kick_tone = np.sin(2 * np.pi * 55 * np.arange(len(kick_env)) / sr)
    kick = kick_tone * kick_env
    beat_times = np.arange(0, duration_s, beat_period)
    for bt in beat_times:
        i = int(bt * sr)
        end = min(n, i + len(kick))
        signal[i:end] += kick[: end - i] * 0.9

    # backbeat snare (beats 2 and 4 of each bar)
    snare_env = np.exp(-np.arange(int(sr * 0.1)) / (sr * 0.02))
    rng = np.random.default_rng(42)
    snare_noise = rng.standard_normal(len(snare_env))
    snare = snare_noise * snare_env
    for bar_start in np.arange(0, duration_s, beat_period * 4):
        for beat_idx in (1, 3):
            bt = bar_start + beat_idx * beat_period
            if bt >= duration_s:
                continue
            i = int(bt * sr)
            end = min(n, i + len(snare))
            signal[i:end] += snare[: end - i] * 0.5

    # simple chord pad (Am - F - C - G, two bars each)
    chords = [220.00, 174.61, 261.63, 196.00]  # A3, F3, C4, G3 roots
    bar_s = beat_period * 4
    for idx, root in enumerate(chords):
        start = idx * 2 * bar_s
        end = min(duration_s, start + 2 * bar_s)
        if start >= duration_s:
            break
        seg_t = t[(t >= start) & (t < end)]
        if len(seg_t) == 0:
            continue
        pad = 0.12 * (np.sin(2 * np.pi * root * seg_t) + 0.5 * np.sin(2 * np.pi * root * 1.5 * seg_t))
        i0 = int(start * sr)
        signal[i0 : i0 + len(pad)] += pad

    signal = signal / max(1e-6, np.max(np.abs(signal))) * 0.85
    pcm16 = (signal * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sr)
        wav_file.writeframes(pcm16.tobytes())


def seed_demo_data(store: Store, backend: Backend) -> dict[str, Any]:
    project = store.create_project(
        GROUP_NAME, brief="Demo project for Prospero's Hoard: an original idol group, generated with the fake ComfyUI backend."
    )
    project_id = project["id"]

    character_ids: list[str] = []
    image_pool: list[str] = []
    for spec in CHARACTERS:
        char = store.create_character(
            project_id, spec["name"], role=spec["role"], prompt=spec["prompt"], negative=spec["negative"],
            palette=spec["palette"],
        )
        character_ids.append(char["id"])

        job = {"id": f"seed-{char['id']}", "project_id": project_id, "params": {
            "positive_prompt": spec["prompt"], "negative_prompt": spec["negative"],
            "width": 1024, "height": 1024, "seed": abs(hash(spec["name"])) % 100000,
            "steps": 20, "cfg": 6.0, "sampler": "dpmpp_2m", "scheduler": "karras", "count": 1,
            "template": "sdxl_txt2img", "checkpoint": "sd_xl_base_1.0.safetensors",
        }}
        result = engine.generate_image(store, backend, job, _no_progress)
        asset_id = result["asset_ids"][0]
        store.update_character(char["id"], canonical_asset_id=asset_id)
        image_pool.append(asset_id)

    group = store.create_group(project_id, GROUP_NAME, concept=GROUP_CONCEPT, member_ids=character_ids, colours=["#ff4d8d", "#f5c26b"])

    cover_job = {"id": "seed-cover", "project_id": project_id, "params": {
        "positive_prompt": f"album cover for the idol group {GROUP_NAME}, neon storm aesthetic, five members silhouette",
        "negative_prompt": "text, watermark", "width": 1024, "height": 1024, "seed": 4242, "steps": 22,
        "cfg": 6.5, "sampler": "dpmpp_2m", "scheduler": "karras", "count": 1, "template": "sdxl_txt2img",
        "checkpoint": "sd_xl_base_1.0.safetensors",
    }}
    cover_result = engine.generate_image(store, backend, cover_job, _no_progress)
    cover_asset_id = cover_result["asset_ids"][0]

    engine.render_design(store, project_id, "album_cover", {
        "cover_image": cover_asset_id, "title": GROUP_NAME.upper(), "subtitle": "Debut Single", "accent": "#ff4d8d",
    }, variant="bottom_band")

    engine.photocard_set(store, project_id, group["id"])

    song_path = store.assets_dir / "seed_song.wav"
    _make_synthetic_song(song_path)
    song_asset = engine.import_asset(store, project_id, song_path, kind_hint="audio", original_name="neon_static_demo.wav")
    engine.analyze_audio(store, song_asset["id"])

    timeline = engine.timeline_auto(
        store, project_id, song_asset["id"], asset_ids=image_pool, board_id=None, aspect="9:16",
        lyrics_asset_id=None, options={"flash_on_strong_downbeats": True, "ken_burns_variety": True},
    )

    render_result = None
    from ..backend import ffmpeg_path

    if ffmpeg_path():
        render_job = {"id": "seed-render", "params": {"timeline_id": timeline["id"], "quality": "preview"}}
        render_result = engine.render_timeline_job(store, backend, render_job, _no_progress)

    return {
        "project_id": project_id, "group_id": group["id"], "character_ids": character_ids,
        "song_asset_id": song_asset["id"], "timeline_id": timeline["id"],
        "render_asset_id": render_result["asset_id"] if render_result else None,
    }
