"""Speech to text with word timestamps (faster-whisper, local), filler words and repeated words."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

from ..errors import TranscriberUnavailable
from ..hoard_link import fam_media
from ..hoard_link import proc as hlproc
from ..hoard_link.media import stt

log = logging.getLogger("lumiere.speech")

FILLERS = {
    "es": ["eh", "ehh", "em", "emm", "mm", "mmm", "este", "esto", "o sea", "pues", "bueno", "vale", "en plan", "tipo", "digamos",
           "a ver", "osea", "ah", "uh", "um", "ehm"],
    "en": ["um", "uh", "uhm", "umm", "er", "erm", "ah", "hmm", "mm", "like", "you know", "i mean", "sort of", "kind of", "basically",
           "actually", "literally", "so yeah"],
}
# words that only count as fillers when they stand alone between pauses (they are also real words)
SOFT = {"este", "esto", "pues", "bueno", "vale", "tipo", "like", "so", "actually", "basically", "literally", "en plan", "digamos",
        "a ver", "sort of", "kind of", "i mean", "you know", "so yeah"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w\s']", "", text).strip()


LOCAL_KINDS = frozenset({"hub_down", "app_down", "app_missing", "tool_missing"})   # Funes cannot be reached: transcribe here
SLICE_S = 15.0                                                                       # how long one wait on Funes blocks
_local: dict[tuple[str, str], Any] = {}
_local_lock = threading.Lock()


def engine_status() -> dict[str, Any]:
    """What can transcribe: Funes's Hoard through the hub (one Whisper for the family) or faster-whisper in this environment."""
    local = stt.available()
    funes = fam_media.available("stt")
    if not (local or funes):
        return {"available": False, "engine": None, "funes": False,
                "reason": "No speech engine: Funes's Hoard is not running and faster-whisper is not installed."}
    cuda = stt.cuda_available() if local else False
    return {"available": True, "engine": "funes" if funes else "faster-whisper", "funes": funes, "local": local,
            "cuda_devices": 1 if cuda else 0, "cuda_libraries": cuda or not sys.platform.startswith("win")}


def _models_dir(size: str) -> Path:
    """The shared whisper folder, unless this machine already has the model in the Hugging Face cache faster-whisper used before."""
    shared = stt.default_models_dir()
    if stt.model_present(shared, size):
        return shared
    hf = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"
    return hf if stt.model_present(hf, size) else shared


def _as_lumiere(done: dict[str, Any], note: str = "") -> dict[str, Any]:
    """A shared transcript (seconds, `words` inside `segments`) as Lumiere's: words [{id, t0, t1, text, p}] and segments [{t0, t1, text}] in ms."""
    words: list[dict[str, Any]] = []
    segs: list[dict[str, Any]] = []
    for seg in done.get("segments") or []:
        segs.append({"t0": int(round(float(seg["start_s"]) * 1000)), "t1": int(round(float(seg["end_s"]) * 1000)), "text": str(seg["text"]).strip()})
        for w in seg.get("words") or []:
            text = str(w.get("word") or "").strip()
            if not text:
                continue
            start, end = float(w["start_s"]), float(w["end_s"])
            words.append({"id": f"w{len(words) + 1}", "t0": int(round(start * 1000)), "t1": int(round(max(end, start + 0.02) * 1000)), "text": text,
                          "p": round(float(w.get("p") or 0), 3)})
    out = {"language": done.get("language") or "", "language_p": round(float(done.get("language_probability") or 0), 3),
           "model": done.get("model") or "", "device": done.get("device") or "", "words": words, "segments": segs}
    if note or done.get("note"):
        out["note"] = note or done["note"]
    return out


def _flag(cancelled: Callable[[], bool]) -> Any:
    class _Flag:
        def is_set(self) -> bool:
            return bool(cancelled())
    return _Flag()


def _via_funes(path: Path, model: str, language: str, initial_prompt: str, progress: Optional[Callable[[float, str], None]],
               cancelled: Callable[[], bool]) -> dict[str, Any]:
    """Funes's `transcribe_file`, followed in short waits so a cancelled job stops the remote one too."""
    def on_progress(fraction: float) -> None:
        if progress:
            progress(min(0.98, fraction), "transcribing (Funes's Hoard)")

    res = fam_media.transcribe(str(path), language=language or "auto", model=model or None, word_timestamps=True,
                               initial_prompt=initial_prompt, timeout_s=SLICE_S, local_fallback=False, progress=on_progress)
    while not res.get("ok") and res.get("kind") == "timeout" and res.get("job_id"):
        if cancelled():
            fam_media.transcribe_cancel(res["job_id"])
            return {"language": language, "language_p": 0, "model": model, "device": "", "words": [], "segments": []}
        if progress and res.get("progress") is not None:
            try:
                on_progress(float(res["progress"]))
            except (TypeError, ValueError):
                pass
        res = fam_media.transcribe_status(res["job_id"], wait_s=SLICE_S)
    return res


def transcribe(path: Path, *, model: str = "", language: str = "", device: str = "auto", gpu: Optional[int] = None,
               progress: Optional[Callable[[float, str], None]] = None, duration_ms: int = 0, cancelled: Callable[[], bool] = lambda: False,
               initial_prompt: str = "") -> dict[str, Any]:
    """Words [{id, t0, t1, text, p}], segments [{t0, t1, text}] and the language. ``path`` is a 16 kHz mono 16-bit WAV
    (media.speech_wav). Funes's Hoard transcribes it when it runs (one model for the family); otherwise the shared transcriber does it
    here: on the GPU when the hub lends it (large-v3-turbo) and on the CPU (small) when it does not or CUDA fails. Whisper's inventions
    over silence are filtered out."""
    res = _via_funes(path, model, language, initial_prompt, progress, cancelled)
    if res.get("ok"):
        if progress:
            progress(0.99, "transcribed (Funes's Hoard)")
        return _as_lumiere(res)
    if "words" in res and not res.get("error"):         # cancelled while Funes was working
        return res
    if res.get("kind") not in LOCAL_KINDS:
        raise TranscriberUnavailable(f"Funes's Hoard could not transcribe it: {res.get('error') or 'unknown error'}")
    if not stt.available():
        raise TranscriberUnavailable("Funes's Hoard is not running and faster-whisper is not installed here. Install it with: pip install faster-whisper")
    size = model or ("large-v3-turbo" if device != "cpu" and stt.cuda_available() else "small")
    with _local_lock:
        engine = _local.get((size, device))
        if engine is None:
            engine = _local[(size, device)] = stt.Transcriber(models_dir=_models_dir(size), size=size, device=device, owner="lumiere")
    if progress:
        progress(0.02, f"{size} (faster-whisper)")
    try:
        done = engine.transcribe(str(path), language=language or None, word_timestamps=True, initial_prompt=initial_prompt, cancel=_flag(cancelled),
                                 progress=(lambda f: progress(min(0.98, f), f"{size} on {engine.device_used or device}")) if progress else None)
    except hlproc.Cancelled:
        return {"language": language, "language_p": 0, "model": size, "device": engine.device_used, "words": [], "segments": []}
    except Exception as error:  # noqa: BLE001 - a model that cannot load or run: say why
        raise TranscriberUnavailable(f"Could not run the speech model: {error}") from error
    return _as_lumiere(done.as_dict())


def _load_audio(path: Path):
    """16 kHz mono float32 samples. A 16-bit mono WAV at 16 kHz (what the app prepares) is read directly, so the
    transcriber never decodes media itself (its decoder depends on a media library whose API changes between versions)."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise TranscriberUnavailable("The speech audio must be 16 kHz mono 16-bit WAV.")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def find_fillers(words: list[dict[str, Any]], language: str = "es", extra: Optional[list[str]] = None, repeats: bool = True,
                 strict: bool = False) -> list[dict[str, Any]]:
    """Word runs to remove: [{ids, t0, t1, text, reason}]. Soft fillers ('bueno', 'like') only count when isolated by pauses
    unless strict=True; immediate repeats ('que que') are reported too."""
    vocab = [norm(x) for x in (FILLERS.get(language[:2], []) + FILLERS["en"] * (language[:2] != "en") + (extra or []))]
    multi = sorted({v for v in vocab if " " in v}, key=lambda v: -len(v.split()))
    single = {v for v in vocab if " " not in v}
    soft = {norm(x) for x in SOFT}
    out: list[dict[str, Any]] = []
    i = 0
    n = len(words)
    while i < n:
        matched = 0
        for phrase in multi:
            parts = phrase.split()
            if i + len(parts) <= n and [norm(words[i + k]["text"]) for k in range(len(parts))] == parts:
                matched = len(parts)
                text = phrase
                break
        if not matched and norm(words[i]["text"]) in single:
            matched, text = 1, norm(words[i]["text"])
        if matched:
            run = words[i: i + matched]
            before = words[i - 1]["t1"] if i > 0 else -10_000
            after = words[i + matched]["t0"] if i + matched < n else 10 ** 9
            isolated = run[0]["t0"] - before >= 250 or after - run[-1]["t1"] >= 250
            if text not in soft or strict or isolated:
                out.append({"ids": [w["id"] for w in run], "t0": run[0]["t0"], "t1": run[-1]["t1"], "text": " ".join(w["text"] for w in run),
                            "reason": "filler"})
            i += matched
            continue
        if repeats and i + 1 < n and norm(words[i]["text"]) and norm(words[i]["text"]) == norm(words[i + 1]["text"]) \
                and words[i + 1]["t0"] - words[i]["t1"] < 600 and len(norm(words[i]["text"])) > 1:
            out.append({"ids": [words[i]["id"]], "t0": words[i]["t0"], "t1": words[i]["t1"], "text": words[i]["text"], "reason": "repeat"})
        i += 1
    return out


def cut_ranges_for_words(words: list[dict[str, Any]], ids: set[str]) -> list[list[int]]:
    """Source ranges that remove the given words with half of the pauses around them, so the words that stay keep a
    natural rhythm (consecutive words become one range)."""
    out: list[list[int]] = []
    n = len(words)
    i = 0
    while i < n:
        if words[i]["id"] not in ids:
            i += 1
            continue
        j = i
        while j + 1 < n and words[j + 1]["id"] in ids:
            j += 1
        prev_end = words[i - 1]["t1"] if i > 0 else None
        next_start = words[j + 1]["t0"] if j + 1 < n else None
        a = words[i]["t0"] if prev_end is None else max(prev_end, words[i]["t0"] - min(120, (words[i]["t0"] - prev_end) // 2))
        b = words[j]["t1"] if next_start is None else min(next_start, words[j]["t1"] + max(0, (next_start - words[j]["t1"]) // 2))
        if prev_end is not None and next_start is not None:
            # also close up the gap so the cut does not leave two pauses back to back
            a = max(prev_end + 40, min(a, words[i]["t0"]))
        if b > a:
            out.append([int(a), int(b)])
        i = j + 1
    return out
