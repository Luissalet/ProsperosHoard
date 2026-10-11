"""Video dubbing: extract audio, transcribe it with timestamps, translate
each segment with the local LLM (through Hoard Link, never edited - see
`hoard_link/VENDORED.txt`), synthesize each in the target voice, time-fit it
back onto the original segment, mix it under (or over) the original track,
and mux a new video with target-language subtitles.

Every stage's files are kept under the job's work directory so one segment
can be inspected or re-synthesized without re-running the rest (see
`resynthesize_segment`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from . import audio as audio_mod
from . import dub_plan
from . import procutil
from . import voice_engines as ve
from . import voice_lab
from .backend import Backend, ffmpeg_path
from .hoard_link.media import ffmpeg as ffmpeg_shared
from .ids import new_id
from .store import Store
from .util import now_iso

ChatFn = Callable[[list[dict[str, Any]]], str]

MIN_ATEMPO, MAX_ATEMPO = 0.5, 2.0
MAX_OVERALL_FACTOR = 4.0  # two chained atempo stages cover 0.25x-4x
DUCK_DB = -18.0
MIX_SAMPLE_RATE = 44100  # matches extract_audio's default; every mix input is normalised to this


class DubbingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _ffmpeg() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise DubbingError("ffmpeg_missing", "ffmpeg not found: install ffmpeg or the imageio-ffmpeg wheel")
    return exe


# ------------------------------------------------------------ time-fit math

atempo_chain = ffmpeg_shared.atempo_chain   # source / target duration -> a chain of atempo= filters (ffmpeg takes 0.5-2.0 each)


def compute_time_fit_factor(source_duration_s: float, target_duration_s: float) -> float:
    if source_duration_s <= 0 or target_duration_s <= 0:
        return 1.0
    return source_duration_s / target_duration_s


def fit_audio_to_duration(src_wav: Path, target_duration_s: float, dest_wav: Path) -> dict[str, Any]:
    """Speed the clip up/down to approximately `target_duration_s` with
    `atempo`, then pad with silence or trim to hit it exactly (a translated
    line rarely lands on the original's duration to the millisecond)."""
    source_duration_s = audio_mod.probe_duration_s(src_wav) or 0.0
    factor = compute_time_fit_factor(source_duration_s, target_duration_s)
    clamped = max(1.0 / MAX_OVERALL_FACTOR, min(MAX_OVERALL_FACTOR, factor))
    chain = atempo_chain(clamped)
    filters = ",".join(chain) if chain else "anull"
    dest_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(src_wav), "-af",
           f"{filters},apad,atrim=0:{target_duration_s:.6f}", str(dest_wav)]
    proc = procutil.run(cmd, timeout=120)
    if proc.returncode != 0 or not dest_wav.is_file():
        raise DubbingError("time_fit_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])
    return {"source_duration_s": round(source_duration_s, 3), "target_duration_s": round(target_duration_s, 3),
            "requested_factor": round(factor, 4), "applied_factor": round(clamped, 4), "clamped": clamped != factor}


# --------------------------------------------------------------- audio I/O

def extract_audio(video_path: Path, out_wav: Path, sr: int = 44100) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(video_path), "-vn", "-ac", "1", "-ar",
           str(sr), str(out_wav)]
    proc = procutil.run(cmd, timeout=600)
    if proc.returncode != 0 or not out_wav.is_file():
        raise DubbingError("extract_failed", (proc.stderr or b"").decode("utf-8", "replace")[:500])


def demucs_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("demucs") is not None


def separate_background(audio_path: Path, out_dir: Path) -> Optional[dict[str, Path]]:
    """Runs `demucs` (2-stem: vocals/no_vocals) when it is installed;
    returns None otherwise so the caller falls back to ducking the
    original track instead of replacing it."""
    if not demucs_installed():
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["demucs", "--two-stems", "vocals", "-o", str(out_dir), str(audio_path)]
    proc = procutil.run(cmd, timeout=1800)
    if proc.returncode != 0:
        return None
    stem = audio_path.stem
    candidates = list(out_dir.glob(f"*/{stem}/no_vocals.wav"))
    if not candidates:
        return None
    folder = candidates[0].parent
    return {"vocals": folder / "vocals.wav", "no_vocals": folder / "no_vocals.wav"}


def build_dub_mix_filter(segments: list[dict[str, Any]], duck_db: float = DUCK_DB,
                         sample_rate: int = MIX_SAMPLE_RATE) -> str:
    """The `filter_complex` string that ducks the original track under each
    dubbed segment and mixes in the delayed dub clips. `segments`:
    [{"start_s", "end_s"}] in the original timeline; input `0:a` is the
    original (or separated background) track, inputs `1:a`..`N:a` are each
    segment's time-fitted dub clip, in the same order - each TTS engine
    (and the video's own audio track) can emit a different sample rate, so
    every branch is normalised to `sample_rate`/mono before it reaches
    `amix`: left to ffmpeg's own format negotiation, mismatched inputs do
    not fail outright but can silently resample the whole mix up to an
    unrelated, much higher rate (observed: two branches at 44100/24000
    negotiating to 192000). Pure string building - unit-tested without
    running ffmpeg."""
    fmt = f"aformat=sample_rates={sample_rate}:channel_layouts=mono"
    if not segments:
        return f"[0:a]{fmt}[mixed]"
    enables = "+".join(f"between(t\\,{s['start_s']:.3f}\\,{s['end_s']:.3f})" for s in segments)
    parts = [f"[0:a]{fmt},volume=enable='{enables}':volume={10 ** (duck_db / 20):.6f}[bg]"]
    delayed_labels = []
    for i, seg in enumerate(segments):
        delay_ms = max(0, int(round(seg["start_s"] * 1000)))
        parts.append(f"[{i + 1}:a]{fmt},adelay={delay_ms}|{delay_ms}[d{i}]")
        delayed_labels.append(f"[d{i}]")
    mix_inputs = "[bg]" + "".join(delayed_labels)
    parts.append(f"{mix_inputs}amix=inputs={len(segments) + 1}:normalize=0[mixed]")
    return ";".join(parts)


def mix_dub_audio(original_audio: Path, dub_clip_paths: list[Path], segments: list[dict[str, Any]],
                  out_path: Path, duck_db: float = DUCK_DB, sample_rate: int = MIX_SAMPLE_RATE) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # loudnorm has to be part of the same filtergraph as the mix (ffmpeg
    # refuses to chain a simple `-af` onto a stream that came out of
    # `-filter_complex`), so it is appended to the [mixed] label here.
    filter_complex = (build_dub_mix_filter(segments, duck_db, sample_rate)
                      + ";[mixed]loudnorm=I=-18:TP=-1.5:LRA=9[out]")
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(original_audio)]
    for p in dub_clip_paths:
        cmd += ["-i", str(p)]
    # loudnorm's single-pass true-peak limiting emits at 192kHz regardless
    # of the graph's own aformat upstream of it; force the output back down
    # explicitly so the mixed track is actually `sample_rate` (see
    # build_dub_mix_filter's docstring for the same issue on the inputs).
    cmd += ["-filter_complex", filter_complex, "-map", "[out]", "-ar", str(sample_rate), str(out_path)]
    proc = procutil.run(cmd, timeout=1200)
    if proc.returncode != 0 or not out_path.is_file():
        raise DubbingError("mix_failed", (proc.stderr or b"").decode("utf-8", "replace")[:800])


def mux_video(video_path: Path, dub_audio_path: Path, out_path: Path, srt_path: Optional[Path] = None) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-nostdin", "-loglevel", "error", "-i", str(video_path), "-i", str(dub_audio_path)]
    maps = ["-map", "0:v:0", "-map", "1:a:0"]
    if srt_path and srt_path.is_file():
        cmd += ["-i", str(srt_path)]
        maps += ["-map", "2:s:0", "-c:s", "mov_text"]
    cmd += maps + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out_path)]
    proc = procutil.run(cmd, timeout=1800)
    if proc.returncode != 0 or not out_path.is_file():
        raise DubbingError("mux_failed", (proc.stderr or b"").decode("utf-8", "replace")[:800])


# --------------------------------------------------------------- translation

def build_translation_messages(text: str, target_lang: str, source_lang: Optional[str],
                               glossary: Optional[dict[str, str]]) -> list[dict[str, Any]]:
    glossary_line = ""
    if glossary:
        pairs = "; ".join(f"{k} -> {v}" for k, v in list(glossary.items())[:50])
        glossary_line = f" Use this glossary for names/terms: {pairs}."
    source_line = f" The source language is {source_lang}." if source_lang else ""
    system = (
        f"You translate video dialogue for dubbing. Translate into {target_lang}.{source_line}{glossary_line} "
        "Keep the translation about the same length (in spoken duration) as the source line so it can be "
        "timed back onto the original clip - prefer a shorter, natural phrasing over a longer literal one. "
        "Reply with only the translated line, no quotes, no notes, no explanation."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


# A garbled reply despite the prompt's "no quotes, no notes": one plain line, no wrapping quotes.
_clean_translated_line = dub_plan.clean_line


def _same_line(a: str, b: str) -> bool:
    norm = lambda t: " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in (t or "")).split())
    return norm(a) == norm(b)


def _lang_root(code: Optional[str]) -> str:
    return (dub_plan.normalize_lang(code) if code else "") or ""


def translate_segments(chat_fn: ChatFn, segments: list[dict[str, Any]], target_lang: str,
                       source_lang: Optional[str] = None, glossary: Optional[dict[str, str]] = None,
                       report: Optional[list[str]] = None) -> list[str]:
    """One LLM call per segment (through Hoard Link's `chat`, injected as
    `chat_fn` so this stays testable with a fake). Raises whatever `chat_fn`
    raises - typically `hoard_link.errors.Unavailable` when no local LLM is
    reachable, which the job handler surfaces as a clear failure.

    An empty reply or one that just repeats the source line (a reasoning model
    that spent its budget thinking, or one that ignored the target language) is
    retried once with a stricter instruction; if it is still not a translation
    the source line is kept and ``report`` gets ``"untranslated"`` for that
    segment (``"ok"`` / ``"retried"`` otherwise), so the job can say so instead
    of voicing the wrong language in silence."""
    out = []
    differs = _lang_root(target_lang) != _lang_root(source_lang) if source_lang else True
    for seg in segments:
        messages = build_translation_messages(seg["text"], target_lang, source_lang, glossary)
        translated = _clean_translated_line(chat_fn(messages) or "")
        status = "ok"
        if not translated or (differs and _same_line(translated, seg["text"])):
            retry = messages + [{"role": "user", "content": (
                f"That was not a translation. Write this line in {target_lang} now. "
                f"Reply with the {target_lang} line only:\n{seg['text']}")}]
            translated = _clean_translated_line(chat_fn(retry) or "")
            status = "retried"
            if not translated or (differs and _same_line(translated, seg["text"])):
                translated, status = "", "untranslated"
        if report is not None:
            report.append(status)
        out.append(translated or seg["text"])
    return out


# ------------------------------------------------------------------- job --

def _work_dir(store: Store, key: str) -> Path:
    return store.data_dir / "voice_studio" / "dub" / key


def _content_key(job: dict[str, Any]) -> str:
    import hashlib

    params = job["params"]
    h = hashlib.sha256()
    h.update(repr(sorted(params.items())).encode("utf-8", "replace"))
    return h.hexdigest()[:24]


def _rel(store: Store, path: Path) -> str:
    return path.relative_to(store.data_dir).as_posix()


def dub_job(store: Store, backend: Backend, job: dict[str, Any], progress: Any) -> dict[str, Any]:
    params = job["params"]
    video_path = Path(params["video_path"])
    target_lang = params["target_language"]
    source_lang = params.get("source_language")
    glossary = params.get("glossary") or {}
    voice_spec = params["voice"]
    project_id = params.get("project_id")
    title = params.get("title") or video_path.stem

    if not video_path.is_file():
        raise DubbingError("video_not_found", f"video not found: {video_path}")

    work_dir = _work_dir(store, _content_key(job))
    work_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = work_dir / "manifest.json"

    progress(0.02, "extracting audio")
    original_audio = work_dir / "original.wav"
    if not original_audio.is_file():
        extract_audio(video_path, original_audio)

    progress(0.08, "transcribing")
    stt_engines = ve.default_stt_engines()
    stt_engine = ve.best_installed_stt(stt_engines, prefer=params.get("stt_engine_id"))
    if stt_engine is None:
        raise DubbingError("stt_not_installed", "no speech-to-text engine is installed; "
                                                "install faster-whisper (pip install faster-whisper)")
    transcript = stt_engine.transcribe(original_audio, language=source_lang)
    segments = [{"index": i, "start_s": s["start_s"], "end_s": s["end_s"], "source_text": s["text"]}
                for i, s in enumerate(transcript["segments"])]
    if not segments:
        raise DubbingError("no_speech", "no speech was detected in this video's audio track")
    detected_source_lang = source_lang or transcript.get("language")

    progress(0.2, f"translating {len(segments)} segment(s) to {target_lang}")

    def chat_fn(messages: list[dict[str, Any]]) -> str:
        # room for models that reason before answering: 200 tokens could be spent thinking and leave no line
        result = backend.link.sync.chat(messages=messages, max_tokens=800, temperature=0.3)
        return result.text

    statuses: list[str] = []
    translations = translate_segments(chat_fn, [{"text": s["source_text"]} for s in segments], target_lang,
                                      detected_source_lang, glossary, report=statuses)
    for seg, tr, status in zip(segments, translations, statuses or ["ok"] * len(segments)):
        seg["translated_text"] = tr
        seg["translation"] = status  # ok | retried | untranslated (the source line was kept)

    options = {"plan": params.get("plan", True) is not False, "qc": params.get("qc", True) is not False,
               "qc_threshold": params.get("qc_threshold"), "stt_engine_id": stt_engine.id}
    if options["plan"]:
        progress(0.26, "planning line timing")
        apply_plan(segments, target_lang, chat_fn)

    tts_engines = ve.default_tts_engines(voices_dir=store.data_dir / "voices")
    seg_dir = work_dir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    qc_engine = stt_engine if options["qc"] else None
    for i, seg in enumerate(segments):
        progress(0.3 + 0.5 * (i / len(segments)), f"voicing segment {i + 1}/{len(segments)}")
        _synthesize_and_fit_segment(store, tts_engines, voice_spec, seg, seg_dir, borrow_gap=options["plan"])
        if options["qc"]:
            run_qc(qc_engine, seg, target_lang, options["qc_threshold"])
    summary = dub_plan.summarize(segments)
    summary["calibrated_cps"] = _calibration(segments)

    _write_manifest(manifest_path, {
        "title": title, "target_language": target_lang, "source_language": detected_source_lang,
        "glossary": glossary, "voice": voice_spec, "video_path": str(video_path), "segments": segments,
        "options": options, "summary": summary, "created_at": now_iso(),
    })

    progress(0.82, "mixing audio")
    outputs = _assemble_dub(store, backend, work_dir, video_path, segments, project_id, title, progress)
    outputs["work_dir"] = _rel(store, work_dir)
    outputs["manifest"] = _rel(store, manifest_path)
    outputs["segments"] = [{k: v for k, v in s.items() if k != "fitted_path"} for s in segments]
    outputs["summary"] = summary
    return outputs


# ------------------------------------------------------- plan and quality

def apply_plan(segments: list[dict[str, Any]], target_lang: str, chat_fn: Optional[ChatFn]) -> None:
    """Estimate each translated line's spoken length against its slot, ask the
    model to shorten the ones that cannot possibly fit (no model: they stay and
    are marked `shorten: "no_llm"`), and record the result in `seg["plan"]`.
    A shortened line replaces `translated_text`; the original is kept in
    `plan.shortened_from`."""
    plan = dub_plan.shorten_lines(chat_fn, dub_plan.plan_segments(segments, target_lang), target_lang)
    for seg, item in zip(segments, plan):
        seg["translated_text"] = item["text"]
        seg["plan"] = _plan_record(item)


def _plan_record(item: dict[str, Any]) -> dict[str, Any]:
    rec = {k: item[k] for k in ("est_s", "slot_s", "own_s", "gap_s", "factor", "class", "beyond_hard", "cps",
                                "char_budget")}
    if item.get("shortened_from"):
        rec["shortened_from"] = item["shortened_from"]
    if item.get("shorten"):
        rec["shorten"] = item["shorten"]["status"]
        rec["shorten_attempts"] = len(item["shorten"]["attempts"])
    return rec


def _calibration(segments: list[dict[str, Any]], skip: Optional[int] = None) -> Optional[float]:
    """Characters per second the voice really produced in this job (before any
    time-fit), from the segments synthesized so far."""
    return dub_plan.calibrate((s["translated_text"], s["raw_duration_s"]) for s in segments
                              if s.get("raw_duration_s") and s["index"] != skip)


def run_qc(stt_engine: Optional[Any], seg: dict[str, Any], target_lang: str, threshold: Optional[float]) -> None:
    """Transcribe the segment's finished clip and compare it with the text that
    was meant to be said. No transcriber, or one that fails, marks the segment
    `skipped` - quality control never fails a dub."""
    code = dub_plan.normalize_lang(target_lang)
    if stt_engine is None:
        seg["qc"] = {"status": "skipped", "reason": "no_transcriber", "flagged": False}
        return
    try:
        heard = stt_engine.transcribe(Path(seg["fitted_path"]), language=code, word_timestamps=False)
        hyp = (heard.get("text") or " ".join(str(x.get("text", "")) for x in heard.get("segments") or [])).strip()
    except Exception as exc:  # noqa: BLE001 - a transcriber that cannot run must not sink the job
        seg["qc"] = {"status": "skipped", "reason": f"transcribe_failed: {str(exc)[:160]}", "flagged": False}
        return
    seg["qc"] = dub_plan.qc_line(seg["translated_text"], hyp, code or target_lang, threshold)


def agent_result(outputs: dict[str, Any]) -> dict[str, Any]:
    """A finished dub job's answer for an agent: the per-segment table with the
    plan and quality-check fields, compact (no file paths, no model attempt logs)."""
    rows = []
    for s in outputs.get("segments") or []:
        row: dict[str, Any] = {k: s[k] for k in ("index", "start_s", "end_s", "source_text", "translated_text", "translation") if k in s}
        plan = s.get("plan")
        if plan:
            row["plan"] = {k: plan[k] for k in ("est_s", "slot_s", "class", "shortened_from", "shorten") if k in plan}
        qc = s.get("qc")
        if qc:
            row["qc"] = {k: qc[k] for k in ("status", "reason", "wer", "cer", "hyp", "flagged") if k in qc}
        if s.get("fit"):
            row["speedup"] = s["fit"].get("applied_factor")
        rows.append(row)
    return {"title": outputs.get("title"), "summary": outputs.get("summary"), "segments": rows}


def _mix_windows(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Where each dubbed clip plays: from the line's start for as long as it was
    fitted (it may run on into the silence before the next line)."""
    out = []
    for s in segments:
        fitted = float((s.get("fit") or {}).get("target_duration_s") or 0.0)
        out.append({"start_s": s["start_s"], "end_s": max(s["end_s"], s["start_s"] + fitted)})
    return out


def _synthesize_and_fit_segment(store: Store, tts_engines: list[Any], voice_spec: dict[str, Any],
                                seg: dict[str, Any], seg_dir: Path, borrow_gap: bool = False) -> None:
    raw_wav = seg_dir / f"seg{seg['index']:04d}.raw.wav"
    fitted_wav = seg_dir / f"seg{seg['index']:04d}.fit.wav"
    wav_bytes, engine_id = voice_lab.synthesize_with_spec(store, tts_engines, voice_spec, seg["translated_text"])
    raw_wav.write_bytes(wav_bytes)
    own_duration = max(0.05, seg["end_s"] - seg["start_s"])
    raw_duration = audio_mod.probe_duration_s(raw_wav) or 0.0
    target_duration = own_duration
    if borrow_gap and raw_duration > own_duration:
        # a line that runs long takes the silence before the next one (as the plan
        # allowed) before anything is sped up
        target_duration = min(raw_duration, own_duration + float((seg.get("plan") or {}).get("gap_s") or 0.0))
    fit_info = fit_audio_to_duration(raw_wav, target_duration, fitted_wav)
    fit_info["borrowed_gap_s"] = round(target_duration - own_duration, 3)
    seg["raw_duration_s"] = round(raw_duration, 3)
    seg["engine_id"] = engine_id
    seg["fitted_path"] = fitted_wav
    seg["fit"] = fit_info


def _assemble_dub(store: Store, backend: Backend, work_dir: Path, video_path: Path, segments: list[dict[str, Any]],
                  project_id: Optional[str], title: str, progress: Any) -> dict[str, Any]:
    original_audio = work_dir / "original.wav"
    background_dir = work_dir / "background"
    separated = separate_background(original_audio, background_dir)
    bg_audio = separated["no_vocals"] if separated else original_audio

    mixed_audio = work_dir / "dub_mix.wav"
    fitted_paths = [Path(s["fitted_path"]) for s in segments]
    mix_dub_audio(bg_audio, fitted_paths, _mix_windows(segments), mixed_audio)

    srt_path = work_dir / "subtitles.srt"
    srt_path.write_text(
        ve.segments_to_srt([{"start_s": s["start_s"], "end_s": s["end_s"], "text": s["translated_text"]} for s in segments]),
        encoding="utf-8",
    )

    progress(0.95, "muxing final video")
    final_video = work_dir / "dubbed.mp4"
    mux_video(video_path, mixed_audio, final_video, srt_path)

    outputs: dict[str, Any] = {
        "title": title, "final_video": _rel(store, final_video), "subtitles": _rel(store, srt_path),
        "background_separated": separated is not None,
    }
    if project_id:
        from . import engine as engine_mod

        asset_id = new_id("a")
        dest = store.path_for_asset_file(asset_id, ".mp4")
        dest.write_bytes(final_video.read_bytes())
        duration_s = audio_mod.probe_duration_s(dest)
        asset = store.create_asset(
            project_id=project_id, kind="video", file_path=engine_mod._rel(store, dest), mime="video/mp4",
            duration_s=duration_s, source="generated",
            recipe={"operation": "dub", "title": title, "segments": len(segments), "created_at": now_iso()},
            asset_id=asset_id, name=title[:80], tags=["dub"],
        )
        outputs["asset_ids"] = [asset["id"]]
    return outputs


def _write_manifest(path: Path, data: dict[str, Any]) -> None:
    clean = dict(data)
    clean["segments"] = [{k: v for k, v in s.items() if k != "fitted_path"} for s in data["segments"]]
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def load_manifest(work_dir: Path) -> dict[str, Any]:
    path = work_dir / "manifest.json"
    if not path.is_file():
        raise DubbingError("no_manifest", f"no dub manifest at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resynthesize_segment(
    store: Store, backend: Backend, work_dir: Path, segment_index: int, new_text: Optional[str] = None,
    voice_spec: Optional[dict[str, Any]] = None, remix: bool = True,
) -> dict[str, Any]:
    """Re-translate-free re-synthesis of one segment (optionally with edited
    text and/or a different voice), then optionally rebuild the mixed audio
    and re-mux the video - without re-running transcription or translation
    for the other segments."""
    manifest = load_manifest(work_dir)
    segments = manifest["segments"]
    if not 0 <= segment_index < len(segments):
        raise DubbingError("bad_segment", f"segment index must be between 0 and {len(segments) - 1}")
    seg_dir = work_dir / "segments"
    # fitted_path is not persisted in manifest.json (a Path is only valid on
    # this machine); every segment's file lives at this deterministic name
    # from the original run, so it is rebuilt here before mixing.
    for s in segments:
        s["fitted_path"] = seg_dir / f"seg{s['index']:04d}.fit.wav"

    seg = segments[segment_index]
    options = manifest.get("options") or {}
    target_lang = manifest.get("target_language") or ""
    plan_on = bool(options.get("plan", "plan" in seg))
    if new_text is not None:
        seg["translated_text"] = new_text
        seg.pop("raw_duration_s", None)  # measured on the old text: no use for calibration
    spec = voice_spec or manifest["voice"]

    tts_engines = ve.default_tts_engines(voices_dir=store.data_dir / "voices")
    _synthesize_and_fit_segment(store, tts_engines, spec, seg, seg_dir, borrow_gap=plan_on)
    if plan_on:
        # re-estimated against the pace the voice really showed on the other lines
        item = dub_plan.plan_segments(segments, target_lang, _calibration(segments, skip=segment_index))[segment_index]
        previous = seg.get("plan") or {}
        seg["plan"] = _plan_record(item)
        if new_text is None:  # the text is still the shortened one: keep how it got there
            seg["plan"].update({k: previous[k] for k in ("shortened_from", "shorten", "shorten_attempts") if k in previous})
    if options.get("qc", True) is not False:
        stt_engine = ve.best_installed_stt(ve.default_stt_engines(), prefer=options.get("stt_engine_id"))
        run_qc(stt_engine, seg, target_lang, options.get("qc_threshold"))
    else:
        seg.pop("qc", None)
    if voice_spec is not None:
        manifest["voice"] = voice_spec  # only the manifest's own default changes; per-segment engine_id is recorded above
    manifest["summary"] = {**dub_plan.summarize(segments), "calibrated_cps": _calibration(segments)}
    _write_manifest(work_dir / "manifest.json", manifest)

    result: dict[str, Any] = {"segment": {k: v for k, v in seg.items() if k != "fitted_path"},
                              "summary": manifest["summary"]}
    if remix:
        video_path = Path(manifest["video_path"])
        outputs = _assemble_dub(store, backend, work_dir, video_path, segments, None, manifest["title"],
                                lambda *a, **k: None)
        result["final_video"] = outputs["final_video"]
    return result
