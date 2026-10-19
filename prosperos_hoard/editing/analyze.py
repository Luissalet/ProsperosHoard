"""Analysis jobs per media: scenes, beats, loudness, motion, the reframing focus track and transcription.
Results are cached in the analysis table; a tool that needs one either reads it or says which job to run."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from . import media as media_store
from .analysis import audio as audio_an
from .analysis import faces
from .analysis import video as video_an
from .errors import LumiereError, NotFound
from .util import clip

if TYPE_CHECKING:
    from .jobs import JobCtx
    from .services import Services

log = logging.getLogger("lumiere.analyze")

KINDS = ("scenes", "beats", "loudness", "motion", "focus", "transcript", "speakers")
LABELS = {"scenes": "Escenas", "beats": "Ritmo", "loudness": "Sonoridad", "motion": "Movimiento", "focus": "Encuadre", "transcript": "Transcripción", "speakers": "Hablantes"}


def schedule(svc: "Services", media_id: str, kinds: list[str], *, force: bool = False, language: str = "", model: str = "",
             num_speakers: Optional[int] = None, speakers: bool = False, engine: str = "") -> list[dict[str, Any]]:
    info = media_store.get(svc, media_id)
    jobs = []
    for kind in kinds:
        if kind not in KINDS:
            raise LumiereError(f"Unknown analysis {kind!r}. Known: {', '.join(KINDS)}.")
        if kind in ("beats", "loudness", "transcript", "speakers") and not info["has_audio"]:
            raise LumiereError(f"{info['name']} has no sound: {kind} needs audio.")
        if kind in ("scenes", "motion", "focus") and not info["has_video"]:
            raise LumiereError(f"{info['name']} has no picture: {kind} needs video.")
        cached = media_store.get_analysis(svc, media_id, kind)
        if kind == "speakers" and cached is not None and num_speakers and cached.get("requested") != num_speakers:
            force = True  # a different number of speakers asked for: separate again
        if not force and cached is not None:
            jobs.append({"kind": kind, "state": "cached"})
            continue
        params: dict[str, Any] = {"media": media_id, "kind": kind}
        if kind == "speakers":
            params.update({"num_speakers": num_speakers, "language": language, "model": model, "engine": engine or "auto"})
            job = svc.jobs.submit("diarize", params, label=f"Hablantes · {clip(info['name'], 60)}", media_id=media_id)
        elif kind == "transcript":
            params.update({"language": language, "model": model, "speakers": speakers, "num_speakers": num_speakers, "engine": engine or "auto"})
            job = svc.jobs.submit("transcribe", params, label=f"Transcribir {clip(info['name'], 60)}", media_id=media_id)
        else:
            job = svc.jobs.submit("analyze", params, label=f"{LABELS[kind]} · {clip(info['name'], 60)}", media_id=media_id)
        jobs.append({"kind": kind, "state": job["state"], "job": job["id"]})
    return jobs


def analyze_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    mid, kind = ctx.params["media"], ctx.params["kind"]
    info = media_store.get(svc, mid)
    tools = svc.tools()
    src = Path(info["path"])
    if not src.exists():
        raise NotFound(f"The file is missing: {src}")
    fast = Path(media_store.proxy_path(svc, mid) or src)  # picture analysis reads the small proxy when there is one
    dur = info["duration_ms"]
    prog = lambda p: ctx.progress(p, LABELS[kind])  # noqa: E731
    if kind == "scenes":
        thr = float(ctx.params.get("threshold", 27.0))
        cuts = video_an.scenes(tools, fast, threshold=thr, duration_ms=dur, src_w=info["width"], src_h=info["height"], fps=info["fps"],
                               progress=prog, handle=ctx.handle())
        result: dict[str, Any] = {"cuts": cuts, "threshold": thr}
    elif kind == "beats":
        result = audio_an.beats(tools, src, handle=ctx.handle())
    elif kind == "loudness":
        result = audio_an.loudness(tools, src, handle=ctx.handle())
    elif kind == "motion":
        result = {"per_second": video_an.motion_per_second(tools, fast, src_w=info["width"], src_h=info["height"], duration_ms=dur, progress=prog,
                                                           handle=ctx.handle())}
    elif kind == "focus":
        prefer = svc.db.get_setting("face_detector", "auto") or "auto"
        detector, detector_info = faces.pick(svc.config.models_dir, prefer=prefer, fetch=svc.model_fetch)
        result = video_an.focus_track(tools, fast, src_w=info["width"], src_h=info["height"], duration_ms=dur, progress=prog, handle=ctx.handle(),
                                      detector=detector, detector_info=detector_info)
    else:
        raise LumiereError(f"Unknown analysis {kind}.")
    media_store.put_analysis(svc, mid, kind, result, ctx.params)
    svc.emit("lumiere.media.analyzed", {"id": mid, "kind": kind})
    return {"kind": kind, **summarize(kind, result)}


def transcribe_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    mid = ctx.params["media"]
    info = media_store.get(svc, mid)
    ctx.progress(0.01, "audio", force=True)
    wav = media_store.speech_wav(svc, mid, int(ctx.params.get("stream", 0)), handle=ctx.handle())
    model = ctx.params.get("model") or svc.db.get_setting("whisper_model", "") or ""
    language = ctx.params.get("language") or svc.db.get_setting("transcript_language", "") or ""
    device = svc.db.get_setting("whisper_device", "auto") or "auto"
    result = svc.transcriber(wav, model=model, language=language, device=device, duration_ms=info["duration_ms"],
                             progress=lambda p, d: ctx.progress(p, d), cancelled=lambda: ctx.cancelled)
    ctx.check()
    media_store.put_analysis(svc, mid, "transcript", result, {"model": result["model"], "language": result["language"]})
    svc.emit("lumiere.media.transcribed", {"id": mid, "name": clip(info["name"], 80), "words": len(result["words"]), "language": result["language"],
                                           "duration_ms": info["duration_ms"], "model": result.get("model"), "job": ctx.id})
    out = {"words": len(result["words"]), "language": result["language"], "model": result["model"], "device": result["device"]}
    if ctx.params.get("speakers") and result["words"]:
        from . import speakers as speakers_mod

        found = speakers_mod.diarize(svc, mid, num_speakers=ctx.params.get("num_speakers"), engine_name=ctx.params.get("engine") or "auto", handle=ctx.handle())
        out["speakers"] = len(found["speakers"])
    return out


def summarize(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    if kind == "scenes":
        return {"cuts": len(result.get("cuts", []))}
    if kind == "beats":
        return {"bpm": result.get("bpm"), "beats": len(result.get("beats", []))}
    if kind == "loudness":
        return {k: result.get(k) for k in ("integrated_lufs", "range_lu", "true_peak_dbtp")}
    if kind == "motion":
        ps = result.get("per_second", [])
        return {"seconds": len(ps), "mean": round(sum(ps) / len(ps), 4) if ps else 0}
    if kind == "focus":
        return {"samples": len(result.get("samples", [])), "faces": result.get("faces"), "detector": result.get("detector"),
                "face_samples": result.get("face_samples")}
    if kind == "transcript":
        return {"words": len(result.get("words", [])), "language": result.get("language"), "speakers": len(result.get("speakers") or {}) or None}
    if kind == "speakers":
        return {"speakers": result.get("speakers"), "method": result.get("method")}
    return {}


def silences_for(svc: "Services", media_id: str, *, threshold_db: Optional[float] = None, min_silence_ms: int = 500,
                 margin_ms: int = 150) -> dict[str, Any]:
    levels = media_store.rms(svc, media_id)
    if levels is None:
        info = media_store.get(svc, media_id)
        if not info["has_audio"]:
            raise LumiereError(f"{info['name']} has no sound.")
        raise LumiereError(f"{info['name']} is still being prepared (waveform); try again when its job ends.", code="not_ready")
    ranges, thr = audio_an.silences(levels, threshold_db=threshold_db, min_silence_ms=min_silence_ms, margin_ms=margin_ms)
    total = sum(b - a for a, b in ranges)
    return {"media": media_id, "threshold_db": round(thr, 1), "ranges": ranges, "count": len(ranges), "silent_ms": total}


def transcript(svc: "Services", media_id: str) -> Optional[dict[str, Any]]:
    return media_store.get_analysis(svc, media_id, "transcript")


def update_words(svc: "Services", media_id: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Correct the text of transcript words (captions use the corrected text): [{id, text}]."""
    t = transcript(svc, media_id)
    if t is None:
        raise LumiereError("This media has no transcript yet.", code="no_transcript")
    by_id = {w["id"]: w for w in t["words"]}
    changed = 0
    for ch in changes:
        w = by_id.get(str(ch.get("id")))
        if w is None:
            raise NotFound(f"No word {ch.get('id')} in the transcript.")
        text = str(ch.get("text", "")).strip()
        if not text:
            raise LumiereError("A word cannot be empty; cut it from the timeline instead.")
        if w["text"] != text:
            w["text"] = text[:80]
            changed += 1
    media_store.put_analysis(svc, media_id, "transcript", t, {"edited": True})
    return {"media": media_id, "changed": changed}
