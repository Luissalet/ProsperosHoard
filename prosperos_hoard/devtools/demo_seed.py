"""Seeds `--demo` mode: an original (invented) idol group, generated
photocards via the fake ComfyUI backend, an album cover, a synthetic
30s song with a real 120 BPM beat, and an auto-cut preview render.

No real people or franchises are referenced anywhere in this module.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .. import engine
from ..backend import Backend
from ..store import Store
from ..util import now_iso

CHARACTERS = [
    {"name": "Iris Volt", "role": "Leader / Main Vocal",
     "prompt": "a confident young pop idol with short platinum hair and sharp eyeliner, stage lighting",
     "negative": "extra limbs, blurry face", "palette": ["#ff4d8d", "#1c1024"],
     "bio": "Writes the hooks. Believes every song needs one line you can shout.", "voice": "en_US-amy-medium"},
    {"name": "Mika Frost", "role": "Lead Dancer",
     "prompt": "an energetic idol with icy blue twin-tails and athletic build, dynamic pose",
     "negative": "extra limbs, blurry face", "palette": ["#6cc0ff", "#0b1a24"],
     "bio": "Choreographs the bridge. Counts everything in eights.", "voice": "en_US-lessac-medium"},
    {"name": "Suri Nova", "role": "Rapper",
     "prompt": "a cool idol with an undercut and gold hoop earrings, streetwear stage outfit",
     "negative": "extra limbs, blurry face", "palette": ["#f5c26b", "#241a0b"],
     "bio": "Raps the second verse in two languages.", "voice": "es_ES-davefx-medium"},
    {"name": "Lena Echo", "role": "Vocalist",
     "prompt": "a soft-spoken idol with long wavy auburn hair, pastel stage outfit",
     "negative": "extra limbs, blurry face", "palette": ["#c98bf0", "#1a0b24"],
     "bio": "Carries the ballad parts. Collects cassette tapes.", "voice": "en_GB-alba-medium"},
    {"name": "Tessa Rune", "role": "Visual / Maknae",
     "prompt": "the youngest idol with bright red bob haircut and playful expression",
     "negative": "extra limbs, blurry face", "palette": ["#ff6b6b", "#240b0f"],
     "bio": "The youngest. Draws the fan-club doodles.", "voice": "es_ES-sharvard-medium"},
]

GROUP_NAME = "Neon Static"
GROUP_CONCEPT = "A five-member synth-pop idol unit built around a shared 'neon storm' visual identity."


def _no_progress(*_a: Any, **_k: Any) -> None:
    return None


def _make_synthetic_song(path: Path, bpm: float = 120.0, duration_s: float = 30.0, sr: int = 44100) -> None:
    """A 30 s, 120 BPM, A/B/A-shaped instrumental made with numpy only:
    a quiet intro (pad + soft kick, bars 1-4), a loud middle (kick, snare on
    2 and 4, eighth-note hats, bass and chords, bars 5-12) and a quiet
    outro. It gives the beat tracker and the section detector something
    real to find, and the auto-cut something to change density on."""
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    beat = 60.0 / bpm
    bar = beat * 4
    rng = np.random.default_rng(42)
    signal = np.zeros(n, dtype=np.float64)

    def add(sample: np.ndarray, at_s: float, gain: float) -> None:
        i = int(at_s * sr)
        if i >= n:
            return
        end = min(n, i + len(sample))
        signal[i:end] += sample[: end - i] * gain

    kick_env = np.exp(-np.arange(int(sr * 0.18)) / (sr * 0.035))
    kick_f = 45 + 70 * np.exp(-np.arange(len(kick_env)) / (sr * 0.02))
    kick = np.sin(2 * np.pi * np.cumsum(kick_f) / sr) * kick_env
    snare = rng.standard_normal(int(sr * 0.12)) * np.exp(-np.arange(int(sr * 0.12)) / (sr * 0.025))
    hat = rng.standard_normal(int(sr * 0.03)) * np.exp(-np.arange(int(sr * 0.03)) / (sr * 0.006))
    hat = np.diff(hat, prepend=0.0)  # crude high-pass

    loud = lambda s: 4 * bar <= s < 12 * bar  # noqa: E731 - bars 5-12
    k = 0
    tb = 0.0
    while tb < duration_s:
        if loud(tb):
            add(kick, tb, 0.9)
            if k % 4 in (1, 3):
                add(snare, tb, 0.55)
            add(hat, tb, 0.18)
            add(hat, tb + beat / 2, 0.12)
        else:
            add(kick, tb, 0.35)
        k += 1
        tb += beat

    chords = [(220.00, 261.63, 329.63), (174.61, 220.00, 261.63), (261.63, 329.63, 392.00), (196.00, 246.94, 293.66)]
    for b in range(int(np.ceil(duration_s / bar))):
        root = chords[(b // 2) % 4]
        start, end = b * bar, min(duration_s, (b + 1) * bar)
        mask = (t >= start) & (t < end)
        seg_t = t[mask] - start
        level = 0.10 if loud(start) else 0.05
        pad = sum(np.sin(2 * np.pi * f * seg_t) for f in root) / 3
        pad *= np.minimum(1.0, seg_t / 0.05) * level
        if loud(start):
            bass_f = root[0] / 4
            bass = 0.25 * np.sign(np.sin(2 * np.pi * bass_f * seg_t)) * np.exp(-((seg_t % beat) / (beat * 0.6)))
            pad = pad + bass * 0.5
        signal[mask] += pad

    signal = signal / max(1e-6, np.max(np.abs(signal))) * 0.85
    pcm16 = (signal * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sr)
        wav_file.writeframes(pcm16.tobytes())


def _stable_seed(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 1_000_000


def _run_generation(store: Store, backend: Backend, project_id: str, prompt: str, negative: str, seed: int,
                    width: int = 1024, height: int = 1024, style: str | None = None) -> str:
    composed = engine.compose_prompt(store, project_id, prompt, negative, style)
    # created as "running" so the live job workers never pick the seed jobs up
    job = store.create_job("generate_image", "gpu", {}, project_id=project_id, state="running")
    params = {
        "prompt": prompt, "positive_prompt": composed["positive_prompt"], "negative_prompt": composed["negative_prompt"],
        "width": width, "height": height, "seed": seed, "steps": 28, "cfg": 6.5, "sampler": "dpmpp_2m",
        "scheduler": "karras", "count": 1, "template": "sdxl_txt2img", "checkpoint": "sd_xl_base_1.0.safetensors",
        "style": composed["style"], "style_defaults": composed["style_defaults"],
        "matched_characters": composed["matched_characters"],
    }
    store.conn.execute("UPDATE jobs SET params_json=? WHERE id=?", (__import__("json").dumps(params), job["id"]))
    store.conn.commit()
    job = {**job, "params": params}
    try:
        result = engine.generate_image(store, backend, job, _no_progress)
    except Exception as exc:
        # never leave a "running" job behind: nothing else would ever end it
        store.update_job(job["id"], state="failed", message=f"demo seed failed: {str(exc)[:300]}",
                         started_at=job["created_at"], finished_at=now_iso())
        raise
    store.update_job(job["id"], state="done", progress=1.0, outputs=result, message="demo seed",
                     started_at=job["created_at"], finished_at=job["created_at"])
    return result["asset_ids"][0]


LYRICS = """[00:00.00]Neon Static - Afterglow (demo lyrics)
[00:04.00]Static on the skyline
[00:08.00]We turn the noise into light
[00:12.00]Five hearts on one frequency
[00:16.00]Hold on, we're burning bright
[00:20.00]Say it louder, make it shine
[00:24.00]Afterglow, you and I
"""


def seed_demo_data(store: Store, backend: Backend) -> dict[str, Any]:
    project = store.create_project(
        GROUP_NAME, brief="Debut single campaign for an original five-member synth-pop group: member photocards, "
                          "album cover and a vertical music-video cut. Demo data rendered by the procedural demo backend.",
    )
    project_id = project["id"]
    styles = {p["name"]: p["id"] for p in store.list_style_presets(project_id)}

    character_ids: list[str] = []
    image_pool: list[str] = []
    for spec in CHARACTERS:
        char = store.create_character(
            project_id, spec["name"], role=spec["role"], prompt=spec["prompt"], negative=spec["negative"],
            palette=spec["palette"], bio=spec.get("bio"),
            voice={"backend": "piper", "voice_id": spec.get("voice", "en_US-amy-medium"), "speed": 1.0},
        )
        character_ids.append(char["id"])
        asset_id = _run_generation(store, backend, project_id, f"@{spec['name']} studio portrait, looking at camera",
                                   "", _stable_seed(spec["name"]), 896, 1152, styles.get("Studio portrait"))
        store.update_character(char["id"], canonical_asset_id=asset_id)
        store.update_asset(asset_id, rating=5, favourite=True, tags=["reference", "portrait"])
        image_pool.append(asset_id)
        stage = _run_generation(store, backend, project_id, f"@{spec['name']} performing on a neon stage, dynamic idol pose",
                                "", _stable_seed(spec["name"] + "stage"), 1024, 1024, styles.get("Neon night city"))
        store.update_asset(stage, rating=4, tags=["stage"])
        image_pool.append(stage)

    group = store.create_group(project_id, GROUP_NAME, concept=GROUP_CONCEPT, member_ids=character_ids,
                               colours=["#ff4d8d", "#f5c26b"])

    cover_asset_id = _run_generation(store, backend, project_id,
                                     f"album cover key visual for {GROUP_NAME}, neon storm over a city skyline",
                                     "text, watermark", 4242, 1024, 1024, styles.get("Album art minimal"))
    store.update_asset(cover_asset_id, tags=["cover-art"], rating=4)
    cover = engine.render_design(store, project_id, "album_cover", {
        "cover_image": cover_asset_id, "title": GROUP_NAME.upper(), "subtitle": "Debut single - Afterglow", "accent": "#f5c26b",
    }, variant="bottom_band")
    store.update_project(project_id, cover_asset_id=cover["id"])

    engine.photocard_set(store, project_id, group["id"])

    board = store.create_board(project_id, "MV storyboard", "storyboard")
    store.update_board_items(board["id"], [{"asset_id": a, "note": ""} for a in image_pool])

    song_path = store.data_dir / "inbox" / "neon_static_afterglow_demo.wav"
    song_path.parent.mkdir(parents=True, exist_ok=True)
    _make_synthetic_song(song_path)
    song_asset = engine.import_asset(store, project_id, song_path, kind_hint="audio", original_name="Afterglow (demo song).wav")
    song_path.unlink(missing_ok=True)
    engine.analyze_audio(store, song_asset["id"])
    lyrics = engine.create_lyrics(store, project_id, LYRICS, "Afterglow lyrics")

    timeline = engine.timeline_auto(
        store, project_id, song_asset["id"], asset_ids=image_pool, board_id=None, aspect="9:16",
        lyrics_asset_id=lyrics["id"], options={"flash_on_strong_downbeats": True, "ken_burns_variety": True, "karaoke": True},
    )

    render_asset_id = None
    from ..backend import ffmpeg_path

    if ffmpeg_path():
        job = store.create_job("render_timeline", "cpu", {"timeline_id": timeline["id"], "quality": "preview"},
                               project_id=project_id, state="running")
        result = engine.render_timeline_job(store, backend, job, _no_progress)
        store.update_job(job["id"], state="done", progress=1.0, outputs=result, message="demo seed",
                         started_at=job["created_at"], finished_at=job["created_at"])
        render_asset_id = result["asset_id"]

    return {
        "project_id": project_id, "group_id": group["id"], "character_ids": character_ids,
        "song_asset_id": song_asset["id"], "lyrics_asset_id": lyrics["id"], "timeline_id": timeline["id"],
        "render_asset_id": render_asset_id,
    }
