"""Speakers of a media: separation of its transcript by voice, renaming them and fixing who said what.

The labels live on the transcript itself (``word["speaker"] = "S1"``, ``transcript["speakers"] = {"S1": {name, color}}``) so
text editing, captions and the text view read them from the place they already read words. A transcript without these
fields is a single speaker; nothing else changes for it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from . import analyze
from . import media as media_store
from .analysis import speakers as engine
from .analysis import speech
from .errors import LumiereError, NotFound

if TYPE_CHECKING:
    from .jobs import JobCtx
    from .services import Services

log = logging.getLogger("lumiere.speakers")


def status() -> dict[str, Any]:
    """What separates voices on this machine: an embedding library when installed, else the built-in engine (always there)."""
    emb = engine.embedder_status()
    if emb["available"]:
        return {"available": True, "engine": f"embeddings ({emb['engine']})", "fallback": "builtin",
                "note": "Voice embeddings are used when the model loads; the built-in engine takes over otherwise."}
    return {"available": True, "engine": "builtin", "embeddings": False,
            "note": "Built-in engine (pitch and spectral shape): reliable for clearly different voices. For similar voices install "
                    "requirements-speakers.txt (speechbrain or resemblyzer) to use voice embeddings."}


# ---------------------------------------------------------------- reading

def _transcript(svc: "Services", media_id: str) -> dict[str, Any]:
    t = analyze.transcript(svc, media_id)
    if t is None:
        raise LumiereError("This media has no transcript yet: run media_analyze with kinds=['transcript'] (or ['speakers'], which transcribes first).",
                           code="no_transcript")
    return t


def _speaker_table(t: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return t.get("speakers") or {}


def name_of(t: dict[str, Any], speaker: Optional[str]) -> str:
    if not speaker:
        return ""
    return _speaker_table(t).get(speaker, {}).get("name") or speaker


def color_of(t: dict[str, Any], speaker: Optional[str]) -> str:
    if not speaker:
        return ""
    return _speaker_table(t).get(speaker, {}).get("color") or ""


def resolve(t: dict[str, Any], ref: str) -> str:
    """The speaker id for an id ('S2') or a name ('Ana', case-insensitive)."""
    table = _speaker_table(t)
    if ref in table:
        return ref
    low = str(ref).strip().lower()
    for sid, info in table.items():
        if str(info.get("name", "")).lower() == low:
            return sid
    raise NotFound(f"No speaker {ref!r} in this transcript. Speakers: " + ", ".join(f"{k} ({v.get('name')})" for k, v in table.items()) + ".")


def turns(t: dict[str, Any]) -> list[dict[str, Any]]:
    """Consecutive words of one speaker as turns [{speaker, name, color, t0, t1, words, first_word, text}]."""
    out: list[dict[str, Any]] = []
    for w in t.get("words", []):
        sp = w.get("speaker")
        if not sp:
            continue
        if out and out[-1]["speaker"] == sp:
            out[-1]["t1"] = w["t1"]
            out[-1]["words"] += 1
            if len(out[-1]["text"]) < 90:
                out[-1]["text"] += " " + w["text"]
            continue
        out.append({"speaker": sp, "name": name_of(t, sp), "color": color_of(t, sp), "t0": w["t0"], "t1": w["t1"], "words": 1,
                    "first_word": w["id"], "text": w["text"]})
    return out


def summary(svc: "Services", media_id: str) -> dict[str, Any]:
    t = _transcript(svc, media_id)
    table = _speaker_table(t)
    stats: dict[str, dict[str, Any]] = {sid: {"id": sid, "name": info.get("name") or sid, "color": info.get("color", ""), "words": 0, "talk_ms": 0}
                                        for sid, info in table.items()}
    for w in t.get("words", []):
        sp = w.get("speaker")
        if sp in stats:
            stats[sp]["words"] += 1
            stats[sp]["talk_ms"] += max(0, w["t1"] - w["t0"])
    meta = media_store.get_analysis(svc, media_id, "speakers") or {}
    tl = turns(t)
    return {"media": media_id, "diarized": bool(table), "speakers": list(stats.values()), "turns": len(tl),
            "turns_list": tl[:200], "method": (t.get("diarization") or meta).get("method"), "confidence": (t.get("diarization") or meta).get("confidence"),
            "status": status()}


# ---------------------------------------------------------------- diarization

def _tag_segments(t: dict[str, Any]) -> None:
    """The sentence segments of the transcript get the speaker that says most of their words."""
    words = t.get("words", [])
    for seg in t.get("segments", []):
        votes: dict[str, int] = {}
        for w in words:
            mid = (w["t0"] + w["t1"]) / 2
            if seg["t0"] <= mid < seg["t1"] and w.get("speaker"):
                votes[w["speaker"]] = votes.get(w["speaker"], 0) + 1
        if votes:
            seg["speaker"] = max(votes, key=lambda k: votes[k])
        else:
            seg.pop("speaker", None)


def _store(svc: "Services", media_id: str, t: dict[str, Any], params: Optional[dict[str, Any]] = None) -> None:
    _tag_segments(t)
    media_store.put_analysis(svc, media_id, "transcript", t, {"edited": True})
    meta = {"method": (t.get("diarization") or {}).get("method"), "speakers": len(_speaker_table(t)), "confidence": (t.get("diarization") or {}).get("confidence"),
            **(params or {})}
    media_store.put_analysis(svc, media_id, "speakers", meta, params or {})


def diarize(svc: "Services", media_id: str, *, num_speakers: Optional[int] = None, max_speakers: int = 6, engine_name: str = "auto",
            handle: Any = None) -> dict[str, Any]:
    """Label every word of the transcript with its speaker (S1, S2... in order of first appearance)."""
    t = _transcript(svc, media_id)
    words = t.get("words", [])
    if not words:
        raise LumiereError("The transcript is empty: there is nothing to separate by speaker.", code="no_speech")
    if engine_name == "embeddings" and not engine.embedder_status()["available"]:
        raise LumiereError("No voice-embedding library is installed: pip install -r requirements-speakers.txt (or use engine='auto'/'builtin').", code="no_embeddings")
    wav = media_store.speech_wav(svc, media_id, handle=handle)
    audio = speech._load_audio(wav)
    res = engine.diarize(audio, words, num_speakers=num_speakers, max_speakers=max_speakers, engine=engine_name)
    palette = engine.PALETTE
    t["speakers"] = {f"S{i + 1}": {"name": f"S{i + 1}", "color": palette[i % len(palette)]} for i in range(res["k"])}
    for w, label in zip(words, res["labels"]):
        w["speaker"] = f"S{int(label) + 1}"
    t["diarization"] = {"method": res["method"], "speakers": res["k"], "confidence": res["confidence"], "requested": num_speakers}
    _store(svc, media_id, t, {"requested": num_speakers, "max_speakers": max_speakers})
    svc.emit("lumiere.media.speakers", {"id": media_id, "speakers": res["k"], "method": res["method"]})
    return summary(svc, media_id)


def diarize_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    mid = ctx.params["media"]
    if analyze.transcript(svc, mid) is None:
        ctx.progress(0.01, "transcript", force=True)
        analyze.transcribe_job(svc, ctx)
    ctx.progress(0.7, "speakers", force=True)
    res = diarize(svc, mid, num_speakers=ctx.params.get("num_speakers"), max_speakers=int(ctx.params.get("max_speakers") or 6),
                  engine_name=ctx.params.get("engine") or "auto", handle=ctx.handle())
    ctx.check()
    return {"speakers": len(res["speakers"]), "method": res["method"], "words": sum(s["words"] for s in res["speakers"])}


# ---------------------------------------------------------------- editing

def _next_id(t: dict[str, Any]) -> str:
    table = _speaker_table(t)
    n = 1
    while f"S{n}" in table:
        n += 1
    return f"S{n}"


def rename(svc: "Services", media_id: str, names: dict[str, str]) -> dict[str, Any]:
    """Give speakers real names: {"S1": "Ana", "S2": "Luis"} (a name or an id as the key). Two speakers cannot share a name."""
    t = _transcript(svc, media_id)
    if not _speaker_table(t):
        raise LumiereError("This transcript has no speakers yet: run media_analyze with kinds=['speakers'].", code="no_speakers")
    resolved = {resolve(t, k): str(v).strip()[:40] for k, v in names.items()}
    for sid, name in resolved.items():
        if not name:
            raise LumiereError("A speaker name cannot be empty.")
    final = {sid: (resolved.get(sid) or info.get("name") or sid) for sid, info in t["speakers"].items()}
    lowered = [n.lower() for n in final.values()]
    if len(set(lowered)) != len(lowered):
        raise LumiereError("Two speakers would have the same name; use speakers_edit with action='merge' to join them.", code="duplicate_name")
    for sid, name in final.items():
        t["speakers"][sid]["name"] = name
    _store(svc, media_id, t)
    return summary(svc, media_id)


def assign(svc: "Services", media_id: str, speaker: str, *, word_ids: Optional[list[str]] = None, from_ms: Optional[int] = None,
           to_ms: Optional[int] = None) -> dict[str, Any]:
    """Say who spoke some words: by word ids or by a source time range. ``speaker`` is an id, a name, or a new name (which creates the speaker)."""
    t = _transcript(svc, media_id)
    words = t.get("words", [])
    if word_ids:
        wanted = set(word_ids)
        unknown = wanted - {w["id"] for w in words}
        if unknown:
            raise NotFound(f"Unknown word ids: {', '.join(sorted(unknown)[:10])}.")
        picked = [w for w in words if w["id"] in wanted]
    elif from_ms is not None and to_ms is not None:
        picked = [w for w in words if from_ms <= (w["t0"] + w["t1"]) / 2 < to_ms]
    else:
        raise LumiereError("Give word_ids or from_ms and to_ms.")
    if not picked:
        raise LumiereError("No words in that selection.")
    table = t.setdefault("speakers", {})
    try:
        sid = resolve(t, speaker)
    except NotFound:
        sid = _next_id(t)
        table[sid] = {"name": str(speaker).strip()[:40] or sid, "color": engine.PALETTE[len(table) % len(engine.PALETTE)]}
    for w in picked:
        w["speaker"] = sid
    _drop_empty(t)
    t.setdefault("diarization", {"method": "manual", "speakers": len(table), "confidence": None})
    _store(svc, media_id, t)
    return {**summary(svc, media_id), "changed": len(picked), "speaker": sid}


def _drop_empty(t: dict[str, Any]) -> None:
    """Speakers without words disappear from the table (after merges and reassignments)."""
    used = {w.get("speaker") for w in t.get("words", [])}
    t["speakers"] = {sid: info for sid, info in _speaker_table(t).items() if sid in used}


def merge(svc: "Services", media_id: str, source: str, into: str) -> dict[str, Any]:
    t = _transcript(svc, media_id)
    a, b = resolve(t, source), resolve(t, into)
    if a == b:
        raise LumiereError("Choose two different speakers to merge.")
    changed = 0
    for w in t["words"]:
        if w.get("speaker") == a:
            w["speaker"] = b
            changed += 1
    _drop_empty(t)
    _store(svc, media_id, t)
    return {**summary(svc, media_id), "changed": changed}


def clear(svc: "Services", media_id: str) -> dict[str, Any]:
    """Forget the speaker separation (the transcript goes back to a single voice)."""
    t = _transcript(svc, media_id)
    for w in t.get("words", []):
        w.pop("speaker", None)
    for seg in t.get("segments", []):
        seg.pop("speaker", None)
    t.pop("speakers", None)
    t.pop("diarization", None)
    media_store.put_analysis(svc, media_id, "transcript", t, {"edited": True})
    svc.db.execute("DELETE FROM analysis WHERE media_id = ? AND kind = 'speakers'", (media_id,))
    return {"media": media_id, "diarized": False}


def words_of(t: dict[str, Any], speakers: list[str]) -> list[dict[str, Any]]:
    """The words of the given speakers (ids or names)."""
    ids = {resolve(t, s) for s in speakers}
    return [w for w in t.get("words", []) if w.get("speaker") in ids]
