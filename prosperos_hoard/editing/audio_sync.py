"""Sync points of a sound: where it is first heard (onset), where it is loudest (peak) and where it dies out (end).

A sound effect, a hit or a spoken word is placed on the timeline by its sync point, not by the start of its file: a
whoosh whose peak is 130 ms into the file has to start 130 ms before the picture event. The points come from the same
measure the foley kits use (bank.times: onset = first 1 ms envelope within 30 dB of the maximum, peak = loudest 5 ms,
end = last 5 ms within 50 dB of the maximum) over the first two minutes of the file."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

from . import media as media_store
from . import motion_sound
from .errors import LumiereError, NotFound
from .foley.forge import bank

if TYPE_CHECKING:
    from .services import Services

ANALYSE_S = 120.0
POINTS = ("onset", "peak", "end")


def measure(samples: np.ndarray, sr: int = motion_sound.SR) -> dict[str, Any]:
    """Sync points in ms of mono (or stereo) samples."""
    y = np.asarray(samples, np.float64)
    y = y.mean(axis=1) if y.ndim == 2 else y
    if y.size < sr // 200 or float(np.abs(y).max()) < 1e-6:
        return {"onset_ms": 0, "peak_ms": 0, "end_ms": 0, "analysed_ms": int(round(y.size * 1000 / sr)), "silent": True}
    found = bank.times(y, sr)
    return {"onset_ms": int(round(found["onset_s"] * 1000)), "peak_ms": int(round(found["peak_s"] * 1000)),
            "end_ms": int(round(found["end_s"] * 1000)), "analysed_ms": int(round(found["dur_s"] * 1000)), "silent": False}


def measure_file(svc: "Services", path: Path, handle: Any = None) -> dict[str, Any]:
    samples = motion_sound.decode(svc.tools(), path, 0.0, ANALYSE_S, handle)
    return measure(samples)


def compute(svc: "Services", media_id: str, handle: Any = None) -> dict[str, Any]:
    """Measure and store the sync points of a library media that has sound."""
    info = media_store.get(svc, media_id)
    if not info["has_audio"]:
        raise LumiereError(f"{info['name']} has no sound: it has no sync points.", code="no_audio")
    path = Path(info["path"])
    if not path.exists():
        raise NotFound(f"The file is missing: {path}")
    result = measure_file(svc, path, handle)
    media_store.put_analysis(svc, media_id, "sync", result, {"window_s": ANALYSE_S})
    return result


def ensure(svc: "Services", media_id: str) -> dict[str, Any]:
    cached = media_store.get_analysis(svc, media_id, "sync")
    return cached if cached is not None else compute(svc, media_id)


def points(svc: "Services", media: Optional[str] = None, path: Optional[str] = None, force: bool = False) -> dict[str, Any]:
    """The sync points of a library media or of a file, with how to use them."""
    if bool(media) == bool(path):
        raise LumiereError("Give either media (a library id) or path.", code="bad_args")
    if media:
        info = media_store.get(svc, media)
        result = compute(svc, media) if force else ensure(svc, media)
        name = info["name"]
        duration = info["duration_ms"]
    else:
        file = Path(path).expanduser()
        media_store._check_root(svc, file)
        if not file.is_file():
            raise NotFound(f"No file {file}.")
        result = measure_file(svc, file)
        name, duration = file.name, None
    return {"media": media, "name": name, "duration_ms": duration, **result,
            "use": "timeline_edit add_media with align: 'onset' | 'peak' | 'end' and at: the picture event's time in ms puts that point on the event "
                   "(for example a whoosh with align 'peak'), instead of its first sample."}
