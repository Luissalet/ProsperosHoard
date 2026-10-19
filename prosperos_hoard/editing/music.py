"""Background music picker: analyse the audio files of a folder (tempo and beats, length, loudness, energy), compare them with
the edit (its length and how fast it cuts) and rank the tracks that fit, each with the reasons. Files stay where they are:
the analysis is cached next to the other caches (keyed by path, size and modification time), so a second look at the same
folder is instant and the media library is not filled with every candidate."""

from __future__ import annotations

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from . import ffmpeg as ff
from . import media as media_store
from . import projects as project_store
from .analysis import audio as audio_an
from .errors import LumiereError, NotFound
from .timeline import Project
from .util import atomic_write, clip, ms_to_tc

if TYPE_CHECKING:
    from .services import Services

AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
MAX_FILES = 40          # tracks analysed per call; more are listed as skipped (ask again with a narrower folder, or the cache grows call by call)
BEATS_MAX_S = 300       # the beat grid is read from the first five minutes
VERSION = 1


# ---------------------------------------------------------------- analysis of one track

def _cache_path(svc: "Services", path: Path) -> Path:
    st = path.stat()
    key = hashlib.sha1(f"{VERSION}|{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
    return svc.config.cache_dir / "music" / f"{key}.json"


def analyze_track(svc: "Services", path: Path) -> dict[str, Any]:
    """Tempo, beat grid, length, loudness and energy of one audio file (cached)."""
    cache = _cache_path(svc, path)
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    tools = svc.tools()
    info = ff.summarize(ff.probe(tools, path), path)
    if not info["has_audio"]:
        raise LumiereError(f"{path.name} has no sound.")
    grid = audio_an.beats(tools, path, max_s=BEATS_MAX_S)
    loud = audio_an.loudness(tools, path)
    _, rms = audio_an.envelope(tools, path)
    per_sec = audio_an.energy_per_second(rms)
    beats = [int(b) for b in grid.get("beats", [])]
    gaps = np.diff(beats) if len(beats) > 3 else np.zeros(0)
    cv = float(gaps.std() / gaps.mean()) if gaps.size and gaps.mean() > 0 else 1.0
    mean_db = float(np.mean(per_sec)) if per_sec.size else -60.0
    result = {
        "path": str(path), "name": path.stem, "duration_ms": int(info["duration_ms"]), "bpm": float(grid.get("bpm") or 0.0), "beat_count": len(beats),
        "beats": beats[:3000], "downbeats": [int(b) for b in grid.get("downbeats", [])[:800]],
        "first_beat_ms": beats[0] if beats else 0, "first_downbeat_ms": (grid.get("downbeats") or beats or [0])[0],
        "steadiness": round(float(np.clip(1.0 - cv * 3.0, 0.0, 1.0)), 2),
        "lufs": loud.get("integrated_lufs"), "peak_dbtp": loud.get("true_peak_dbtp"),
        "energy_db": round(mean_db, 1), "energy": round(float(np.clip((mean_db + 40.0) / 30.0, 0.0, 1.0)), 2),
        "dynamics_db": round(float(np.std(per_sec)), 1) if per_sec.size else 0.0,
    }
    try:
        atomic_write(cache, json.dumps(result))
    except OSError:
        pass
    return result


# ---------------------------------------------------------------- the edit

def edit_profile(p: Project) -> dict[str, Any]:
    """What the music has to fit: the length of the edit and how fast it cuts (cuts per minute on the main track)."""
    main = p.main_track()
    clips = sorted((c for c in (main.clips if main else []) if c.type == "media"), key=lambda c: c.start)
    dur = p.duration
    cuts = [c.start for c in clips[1:]]
    cpm = round(len(cuts) / (dur / 60000.0), 1) if dur >= 8000 and len(cuts) >= 3 else None
    return {"duration_ms": dur, "duration": ms_to_tc(dur), "cuts": len(cuts), "cut_times": cuts, "cuts_per_min": cpm,
            "target_bpm": round(target_bpm(cpm)) if cpm is not None else None}


def target_bpm(cuts_per_min: float) -> float:
    """The tempo that suits an edit cutting this often: a calm edit (a cut every ~10 s) sits near 85 bpm, a fast one (every
    2 s) near 130, and the slope is gentle because a cut every two or four bars is how most edits are built."""
    return 80.0 + min(max(cuts_per_min, 0.0), 45.0) * 1.3


def _tempo_distance(bpm: float, target: float) -> float:
    """Distance in bpm to the target, counting half and double time as a near match (a 60 bpm track still carries 120 bpm cuts)."""
    if bpm <= 0:
        return 99.0
    return min(abs(bpm - target), abs(bpm * 2 - target) + 8, abs(bpm / 2 - target) + 8)


def _sync(track: dict[str, Any], cut_times: list[int]) -> Optional[float]:
    """Share of the cuts that land within 90 ms of a beat when the track starts on its first beat at the start of the edit."""
    beats = np.array([b - track["first_beat_ms"] for b in track.get("beats", []) if b >= track["first_beat_ms"]], dtype=np.float64)
    if beats.size < 4 or len(cut_times) < 3:
        return None
    hits = 0
    for t in cut_times:
        i = int(np.searchsorted(beats, t))
        near = min(abs(beats[j] - t) for j in (i - 1, i) if 0 <= j < beats.size)
        hits += near <= 90
    return hits / len(cut_times)


def rate(track: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Score (0..1) and reasons of one analysed track for one edit. Weights: tempo against the pace 45%, length 20%, energy 15%,
    loudness 10%, cuts landing on beats 10%; a part that cannot be judged (an edit with fewer than three cuts has no pace)
    counts as neutral and is said so."""
    reasons: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    dur, tl = profile["duration_ms"], track["duration_ms"]
    cpm = profile["cuts_per_min"]
    bpm = track["bpm"]
    if cpm is None or bpm <= 0:
        tempo_s = 0.5
        if cpm is None:
            warnings.append({"code": "pace_unknown", "text": "the edit has too few cuts to judge its pace; tempo is not weighed"})
        else:
            warnings.append({"code": "no_tempo", "text": "no steady beat found in this track"})
    else:
        dist = _tempo_distance(bpm, target_bpm(cpm))
        tempo_s = float(math.exp(-0.5 * (dist / 18.0) ** 2)) * (0.6 + 0.4 * track["steadiness"])
        if tempo_s >= 0.55:
            reasons.append({"code": "tempo", "bpm": round(bpm), "cuts_per_min": cpm, "text": f"{round(bpm)} bpm suits an edit that cuts {cpm:g} times a minute"})
        else:
            warnings.append({"code": "tempo_far", "bpm": round(bpm), "cuts_per_min": cpm,
                             "text": f"{round(bpm)} bpm is far from what {cpm:g} cuts a minute call for (about {round(target_bpm(cpm))})"})
    if tl >= dur * 0.98:
        dur_s = 1.0 if tl <= dur * 4 else 0.85
        reasons.append({"code": "long_enough", "track": ms_to_tc(tl), "edit": ms_to_tc(dur), "text": f"long enough ({ms_to_tc(tl)} for {ms_to_tc(dur)}); it is trimmed and faded to length"})
    else:
        dur_s = max(0.1, tl / max(dur, 1)) * 0.7
        warnings.append({"code": "too_short", "short_ms": dur - tl, "text": f"{ms_to_tc(dur - tl)} shorter than the edit (it ends early)"})
    target_energy = min(1.0, (cpm if cpm is not None else 15.0) / 40.0) * 0.7 + 0.15
    energy_s = max(0.0, 1.0 - abs(track["energy"] - target_energy) * 1.6)
    if energy_s >= 0.6:
        reasons.append({"code": "energy", "energy": track["energy"], "text": "its energy matches the edit's pace"})
    lufs = track.get("lufs")
    loud_s = 0.6 if lufs is None else float(np.clip(1.0 - max(0.0, abs(lufs + 17.0) - 5.0) / 10.0, 0.0, 1.0))
    if lufs is not None and loud_s < 0.5:
        warnings.append({"code": "loudness", "lufs": round(lufs, 1), "text": f"very {'loud' if lufs > -17 else 'quiet'} ({lufs:.0f} LUFS); the music track volume will need adjusting"})
    sync = _sync(track, profile["cut_times"])
    sync_s = 0.5 if sync is None else sync
    if sync is not None and sync >= 0.5:
        reasons.append({"code": "sync", "share": round(sync, 2), "text": f"{round(sync * 100)}% of your cuts already fall on its beats"})
    score = 0.45 * tempo_s + 0.20 * dur_s + 0.15 * energy_s + 0.10 * loud_s + 0.10 * sync_s
    if tl < dur * 0.98:
        score *= (tl / dur) ** 0.7  # a track that runs out long before the edit ends is a poor pick however well it fits otherwise
    return {"score": round(float(score), 3), "reasons": reasons, "warnings": warnings,
            "fit": {"tempo": round(tempo_s, 2), "length": round(dur_s, 2), "energy": round(energy_s, 2), "loudness": round(loud_s, 2),
                    "sync": None if sync is None else round(sync, 2)}}


# ---------------------------------------------------------------- the folder

def list_audio(svc: "Services", folder: str, recursive: bool = False) -> list[Path]:
    root = Path(folder).expanduser()
    media_store._check_root(svc, root)
    if not root.is_dir():
        raise NotFound(f"There is no folder at {root}.")
    it = root.rglob("*") if recursive else root.iterdir()
    return sorted((f for f in it if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS), key=lambda f: f.name.lower())


def suggest(svc: "Services", project_id: str, folder: str, *, recursive: bool = False, count: int = 5) -> dict[str, Any]:
    """Rank the audio files of ``folder`` for the project: best first, each with its score, tempo, length and the reasons."""
    p = project_store.doc(svc, project_id)
    profile = edit_profile(p)
    if profile["duration_ms"] <= 0:
        raise LumiereError("The project is empty: there is no length or pace to fit music to.")
    files = list_audio(svc, folder, recursive)
    if not files:
        raise LumiereError(f"No audio files (mp3, wav, flac, m4a, ogg...) in {folder}.")
    todo, skipped = files[:MAX_FILES], [{"file": f.name, "reason": "over the limit of %d tracks per call" % MAX_FILES} for f in files[MAX_FILES:]]
    cached = sum(1 for f in todo if _cache_path(svc, f).exists())
    tracks: list[dict[str, Any]] = []

    def work(f: Path) -> tuple[Path, Any]:
        try:
            return f, analyze_track(svc, f)
        except Exception as error:  # noqa: BLE001 - one broken file must not sink the folder
            return f, error

    with ThreadPoolExecutor(max_workers=max(1, min(4, svc.config.workers + 2))) as pool:
        for f, res in pool.map(work, todo):
            if isinstance(res, Exception):
                skipped.append({"file": f.name, "reason": clip(str(res), 140)})
                continue
            fit = rate(res, profile)
            tracks.append({"path": res["path"], "name": res["name"], "duration": ms_to_tc(res["duration_ms"]), "duration_ms": res["duration_ms"],
                           "bpm": round(res["bpm"], 1), "lufs": res["lufs"], "energy": res["energy"], "start_ms": res["first_beat_ms"], **fit})
    tracks.sort(key=lambda t: -t["score"])
    for i, t in enumerate(tracks, 1):
        t["rank"] = i
    public = {k: v for k, v in profile.items() if k != "cut_times"}
    return {"project": project_id, "folder": str(Path(folder).expanduser()), "edit": public, "tracks": tracks[:count], "analysed": len(tracks),
            "from_cache": cached, "skipped": skipped}
