"""Runs a render: audio masters, picture chunks in parallel, the sound pass, loudness, the final mux and a quality check.
Also single frames (what the timeline looks like at a time) and the fast lossless cut."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .. import ffmpeg as ff
from .. import media as media_store
from .. import projects as project_store
from ..analysis import audio as audio_an
from .. import subtitles as subtitles_mod
from ..errors import LumiereError
from ..ops import delete_range
from ..timeline import Project, clone, validate
from ..util import clip, dumps, ms_to_tc, new_id, safe_filename
from . import compiler as C
from . import formats as F
from . import sequences
from .ass import build_ass, build_srt, build_vtt

if TYPE_CHECKING:
    from ..jobs import JobCtx
    from ..services import Services

log = logging.getLogger("lumiere.render")

EXPORTS: dict[str, dict[str, Any]] = {
    "final": {"label": "MP4 H.264 · alta calidad", "container": "mp4", "codec": "h264", "quality": "high", "abr": "192k", "lufs": -14.0},
    "hevc": {"label": "MP4 H.265 · menos peso", "container": "mp4", "codec": "hevc", "quality": "high", "abr": "192k", "lufs": -14.0},
    "web": {"label": "MP4 720p · ligero para compartir", "container": "mp4", "codec": "h264", "quality": "medium", "abr": "128k", "lufs": -14.0,
            "max_short": 720},
    "master": {"label": "MOV ProRes 422 HQ · para otro editor", "container": "mov", "codec": "prores", "quality": "high", "abr": None, "lufs": None},
    "preview": {"label": "Vista previa rápida (540p, proxies)", "container": "mp4", "codec": "h264", "quality": "draft", "abr": "128k", "lufs": None,
                "max_short": 540, "proxies": True},
    "gif": {"label": "GIF animado (480 px)", "container": "gif", "codec": "h264", "quality": "draft", "abr": None, "lufs": None, "max_long": 480,
            "gif_fps": 15},
    "audio_mp3": {"label": "Solo audio MP3 320k", "container": "mp3", "audio_only": True, "abr": "320k", "lufs": -14.0},
    "audio_wav": {"label": "Solo audio WAV", "container": "wav", "audio_only": True, "abr": None, "lufs": None},
}
LOUDNESS_TARGETS = {"social": -14.0, "youtube": -14.0, "podcast": -16.0, "broadcast": -23.0}


def _strip_media_ext(name: str) -> str:
    name = (name or "").strip()
    stem, dot, ext = name.rpartition(".")
    return stem if dot and stem and ext.lower() in {"mp4", "mov", "mkv", "webm", "gif", "mp3", "wav", "m4a", "m4v", "avi"} else name


def export_presets() -> list[dict[str, Any]]:
    return [{"id": k, **v} for k, v in EXPORTS.items()]


def _video_args(svc: "Services", codec: str, quality: str, fps: float, width: int = 1920, height: int = 1080) -> tuple[list[str], str]:
    tools = svc.tools()
    gop = str(max(1, int(round(fps * 2))))
    # constant quality with a ceiling: phone footage is noisy and pure CQ balloons (a 1080x1920 talk came out at 16 Mbit/s)
    pixels = width * height * fps
    cap_mbps = {"high": 0.17, "medium": 0.085, "draft": 0.05}[quality] * pixels / 1e6
    cap_mbps = max(1.5, min(cap_mbps, 80.0))
    rate = ["-maxrate", f"{cap_mbps:.1f}M", "-bufsize", f"{cap_mbps * 2:.1f}M"]
    if codec == "prores":
        return ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"], "prores_ks"
    enc = tools.video_encoder(svc.config.encoder, "hevc" if codec == "hevc" else "h264")
    if "nvenc" in enc:
        cq = {"high": "20", "medium": "24", "draft": "30"}[quality]
        preset = {"high": "p5", "medium": "p4", "draft": "p1"}[quality]
        args = ["-c:v", enc, "-preset", preset, "-tune", "hq", "-rc", "vbr", "-cq", cq, "-b:v", "0", *rate, "-g", gop, "-bf", "2", "-pix_fmt", "yuv420p"]
        if enc == "h264_nvenc":
            args += ["-profile:v", "high"]
        else:
            args += ["-tag:v", "hvc1"]
        return args, enc
    if enc == "libx265":
        crf = {"high": "21", "medium": "26", "draft": "32"}[quality]
        return ["-c:v", "libx265", "-preset", "fast" if quality != "draft" else "ultrafast", "-crf", crf, *rate, "-g", gop, "-pix_fmt", "yuv420p",
                "-tag:v", "hvc1", "-x265-params", "log-level=error"], enc
    crf = {"high": "18", "medium": "23", "draft": "30"}[quality]
    preset = {"high": "medium", "medium": "veryfast", "draft": "ultrafast"}[quality]
    return ["-c:v", "libx264", "-preset", preset, "-crf", crf, *rate, "-g", gop, "-bf", "2", "-pix_fmt", "yuv420p", "-profile:v", "high"], enc


def _out_size(W: int, H: int, spec: dict[str, Any]) -> tuple[int, int]:
    short = spec.get("max_short")
    long_ = spec.get("max_long")
    k = 1.0
    if short and min(W, H) > short:
        k = short / min(W, H)
    if long_ and max(W, H) * k > long_:
        k = long_ / max(W, H)
    return max(2, int(round(W * k / 2)) * 2), max(2, int(round(H * k / 2)) * 2)


def _script_flag(tools: ff.Tools, path: str) -> list[str]:
    """ffmpeg 7+ reads option values from files with -/option; older builds use -filter_complex_script."""
    try:
        major = int(tools.version.split(".")[0].lstrip("n"))
    except (ValueError, IndexError):
        major = 6
    return ["-/filter_complex", path] if major >= 7 else ["-filter_complex_script", path]


class RenderContext:
    def __init__(self, svc: "Services", project: Project, work: Path, *, use_proxies: bool = False, stack: tuple[str, ...] = ()):
        self.svc = svc
        self.p = project
        self.work = work
        self.use_proxies = use_proxies
        self.stack = stack  # projects being rendered around this one (nested sequences refuse loops)
        self._luts: dict[str, str] = {}
        self._media: dict[str, C.MediaRef] = {}
        self._seq: dict[str, sequences.SeqMaster] = {}
        self._words: dict[str, Optional[list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        self._seq_lock = threading.Lock()

    def media(self, mid: str) -> C.MediaRef:
        if mid not in self._media:
            if mid.startswith("prj_"):
                m = self.sequence(mid)
                self._media[mid] = C.MediaRef(id=mid, path=str(m.video), kind="video", width=m.width, height=m.height, has_audio=True,
                                              has_video=True, duration_ms=m.duration_ms, proxy=None, fps=m.fps)
                return self._media[mid]
            info = media_store.get(self.svc, mid)
            self._media[mid] = C.MediaRef(id=mid, path=info["path"], kind=info["kind"], width=info["width"], height=info["height"],
                                          has_audio=info["has_audio"], has_video=info["has_video"], duration_ms=info["duration_ms"],
                                          proxy=media_store.proxy_path(self.svc, mid), fps=float(info.get("fps") or 0))
        return self._media[mid]

    def sequence(self, project_id: str, ctx: Optional["JobCtx"] = None) -> sequences.SeqMaster:
        with self._seq_lock:
            if project_id not in self._seq:
                self._seq[project_id] = sequences.ensure(self.svc, project_id, stack=self.stack, ctx=ctx)
            return self._seq[project_id]

    def prepare(self, ctx: Optional["JobCtx"] = None) -> int:
        """Make the intermediates of every nested sequence before the parallel chunks need them."""
        ids = sorted(self.p.sequence_ids())
        for i, sid in enumerate(ids):
            if ctx is not None:
                ctx.check()
                ctx.progress(0.01, f"nested sequence {i + 1}/{len(ids)}", force=True)
            self.sequence(sid, ctx)
        return len(ids)

    def master_name(self, mid: str, stream: int) -> str:
        """The FLAC a sound comes from, relative to the cache folder (where the sound pass runs)."""
        if mid.startswith("prj_"):
            return self.sequence(mid).audio_rel
        return f"{mid}/a{stream}.flac"

    def lut_name(self, file: str) -> str:
        with self._lock:
            if file not in self._luts:
                src = Path(file)
                if not src.is_file():
                    raise LumiereError(f"The LUT file is missing: {file}")
                name = f"lut_{len(self._luts) + 1}{src.suffix.lower()}"
                shutil.copyfile(src, self.work / name)
                self._luts[file] = name
            return self._luts[file]

    def words_for(self, mid: str) -> Optional[list[dict[str, Any]]]:
        """Words of a media with their speaker's name and colour added when the transcript is speaker-separated."""
        if mid not in self._words:
            t = media_store.get_analysis(self.svc, mid, "transcript")
            words = t.get("words") if t else None
            table = (t or {}).get("speakers") or {}
            if words and table:
                words = [dict(w, speaker_name=(table.get(w.get("speaker")) or {}).get("name", ""), speaker_color=(table.get(w.get("speaker")) or {}).get("color", ""))
                         for w in words]
            self._words[mid] = words
        return self._words[mid]


def trimmed(project: Project, start: Optional[int], end: Optional[int]) -> Project:
    """The part [start, end) of the timeline as its own project (for exporting a range)."""
    p = clone(project)
    for t in p.tracks:
        t.locked = False
    total = p.duration
    if end is not None and end < total:
        delete_range(p, end, max(total, p.content_end) + 1, None, True)
    if start:
        delete_range(p, 0, start, None, True)
    for t, o in zip(p.tracks, project.tracks):
        t.locked = o.locked
    return p


@dataclass
class Target:
    """One file a render job writes: the project shaped for one canvas (the project itself for a plain export)."""

    project: Project
    key: str = ""             # 16x9, 9x16... empty for a single plain export
    label: str = ""
    width: int = 0            # output size
    height: int = 0
    notes: Optional[dict[str, Any]] = None


def render_event(info: dict[str, Any], job_id: str) -> dict[str, Any]:
    """The family event that says a render is ready: where it is and how it checked out."""
    qc = info.get("qc") or {}
    event = {"id": info["id"], "project": info["project"], "preset": info["preset"], "path": info["path"], "duration_ms": info["duration_ms"],
             "width": info["width"], "height": info["height"], "bytes": info["bytes"], "seconds": info["seconds"], "ok": qc.get("ok", True),
             "problems": (qc.get("problems") or [])[:3], "job": job_id}
    if qc.get("integrated_lufs") is not None:
        event["lufs"] = qc["integrated_lufs"]
    if info.get("variant"):
        event["variant"] = info["variant"]
    return event


def _targets(svc: "Services", p: Project, spec: dict[str, Any], params: dict[str, Any], audio_only: bool, cue_lines: Optional[list]) -> list[Target]:
    """What to render: the project once, or one adapted copy per requested canvas."""
    def shown(project: Project) -> Project:
        if cue_lines is not None and not project.captions.enabled:
            project = project.model_copy(update={"captions": project.captions.model_copy(update={"enabled": True})})
        return project

    raw = params.get("formats")
    if not raw:
        W, H = _out_size(p.canvas.width, p.canvas.height, spec)
        return [Target(shown(p), width=W, height=H)]
    if audio_only or spec["container"] in ("mp3", "wav"):
        raise LumiereError("Several formats are canvases of a picture: choose a video preset (not audio only).", code="bad_format")
    look = project_store.media_lookup(svc)
    out = []
    for fmt in F.parse_formats(raw, params.get("reframe") or "auto"):
        v, notes = F.variant_project(svc, p, fmt, look)
        W, H = _out_size(fmt.width, fmt.height, spec)
        out.append(Target(shown(v), fmt.key, fmt.label, W, H, notes))
    return out


def render_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    """One export: the sound masters, the sound pass and the loudness once, then the picture and the mux for every output
    (one for a plain export, one per canvas with ``formats``). The project is never changed: outputs are made from copies."""
    params = ctx.params
    pid = params["project"]
    spec = dict(EXPORTS[params.get("preset", "final")])
    if params.get("lufs", "default") != "default":
        spec["lufs"] = params.get("lufs")
    p0 = project_store.doc(svc, pid)
    look = project_store.media_lookup(svc)
    errors = [i for i in validate(p0, look) if i["level"] == "error"]
    if errors:
        raise LumiereError("The timeline has errors: " + "; ".join(i["message"] for i in errors[:5]), code="invalid_timeline")
    p = trimmed(p0, params.get("start"), params.get("end")) if params.get("start") is not None or params.get("end") is not None else p0
    total = p.duration
    if total < 40:
        raise LumiereError("The timeline is empty.", code="empty_timeline")
    audio_only = bool(spec.get("audio_only"))
    # translated captions: the translation is made first when it is missing or out of date (the model is needed then)
    lang = params.get("captions_language") or ""
    cue_lines = None
    if lang:
        if audio_only:
            raise LumiereError("Burned captions need a video preset.", code="bad_format")
        if not subtitles_mod.is_fresh(svc, pid, lang, p0):
            ctx.progress(0.01, "traduciendo subtítulos", force=True)
            subtitles_mod.translate(svc, pid, lang, progress=lambda x, d: ctx.progress(0.01 + 0.1 * x, d, force=True), check=ctx.check)
        cue_lines = subtitles_mod.shift_cues(subtitles_mod.translated_cues(svc, pid, lang, dual=bool(params.get("captions_dual")), p=p0),
                                             int(params.get("start") or 0), total)
    targets = _targets(svc, p, spec, params, audio_only, cue_lines)
    multi = bool(params.get("formats"))
    tools = svc.tools()
    rid0 = params.get("render_id") or new_id("rnd")
    work = svc.config.work_dir / rid0
    work.mkdir(parents=True, exist_ok=True)
    rc = RenderContext(svc, p, work, use_proxies=bool(spec.get("proxies")))
    fps = p.canvas.fps
    fps_expr = ff.fps_fraction(fps)
    chosen = _strip_media_ext(params.get("filename") or "")
    name = safe_filename(chosen or project_store.summary(svc, svc.db.one("SELECT * FROM projects WHERE id = ?", (pid,)))["name"])
    ext = {"mp4": ".mp4", "mov": ".mov", "gif": ".gif", "mp3": ".mp3", "wav": ".wav"}[spec["container"]]
    out_dir = Path(params["folder"]) if params.get("folder") else svc.config.renders_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    # a name the person chose is used as given; automatic names say which version they are (-preview, -web...)
    tag = "" if chosen or spec["container"] != "mp4" or params.get("preset") == "final" else "-" + params.get("preset", "")
    t_start = time.time()
    outputs: list[dict[str, Any]] = []
    try:
        # 0. nested sequences (each one rendered once, then read like a media)
        rc.prepare(ctx)
        # 1. sound: masters, the sound pass and the loudness, once for every output
        ag = C.audio_graph(p, total, rc.media, rc.master_name)
        for i, (mid, stream) in enumerate(ag.sources):
            ctx.check()
            ctx.progress(0.12 + 0.04 * i / max(1, len(ag.sources)), f"audio {i + 1}/{len(ag.sources)}", force=True)
            if not mid.startswith("prj_"):
                media_store.audio_master(svc, mid, stream, handle=ctx.handle())
        ctx.check()
        ctx.progress(0.16, "sound", force=True)
        (work / "audio_graph.txt").write_text(ag.graph, encoding="utf-8")
        audio_wav = work / "audio.wav"
        ff.run(tools, _script_flag(tools, str(work / "audio_graph.txt")) + ["-map", "[aout]", "-c:a", "pcm_s16le", "-ar", "48000", str(audio_wav)],
               duration_ms=total, progress=lambda x: ctx.progress(0.16 + 0.05 * x, "sound"), handle=ctx.handle(), cwd=svc.config.cache_dir)
        af: list[str] = []
        loud_info: dict[str, Any] = {}
        target = spec.get("lufs")
        if target is not None and ag.clips:
            ctx.progress(0.21, "loudness", force=True)
            m = audio_an.loudnorm_measure(tools, audio_wav, float(target), handle=ctx.handle())
            if m and m["input_i"] > -70:  # NaN / -inf (nothing audible to measure) fail this test
                af = ["-af", f"{ff.loudnorm_filter(m, float(target))},aresample=48000"]
                loud_info = {"measured_lufs": float(m["input_i"]), "target_lufs": target}
        # 2. every output: picture and mux
        span = 0.77
        for n, tg in enumerate(targets):
            lo = 0.22 + span * n / len(targets)
            hi = 0.22 + span * (n + 1) / len(targets)
            rid = rid0 if n == 0 else new_id("rnd")
            suffix = f"-{tg.key}" if tg.key else ""
            out_path = out_dir / f"{name}{tag}{suffix}{ext}"
            if out_path.exists() and not params.get("overwrite"):
                stem = out_path.stem
                k = 2
                while out_path.exists():
                    out_path = out_dir / f"{stem} ({k}){ext}"
                    k += 1
            twork = work / tg.key if multi else work
            twork.mkdir(parents=True, exist_ok=True)
            prefix = f"{tg.label} ({n + 1}/{len(targets)}) · " if multi else ""
            info = _render_target(svc, ctx, params, spec, tg, rid, out_path, twork, audio_wav, af, loud_info, total, audio_only, lo, hi, prefix, cue_lines,
                                  RenderContext(svc, tg.project, twork, use_proxies=bool(spec.get("proxies"))), tools, fps, fps_expr, ext, t_start)
            outputs.append(info)
            svc.emit("lumiere.render.done", svc.job_events.done_data(info, render_event(info, ctx.id), ctx.id, pid))
        first = outputs[0]
        if not multi:
            return first
        return {**first, "outputs": outputs, "formats": [o["variant"] for o in outputs], "seconds": round(time.time() - t_start, 1),
                "ok": all(o["qc"].get("ok", True) for o in outputs)}
    finally:
        if not params.get("keep_work"):
            shutil.rmtree(work, ignore_errors=True)


def _render_target(svc: "Services", ctx: "JobCtx", params: dict[str, Any], spec: dict[str, Any], tg: Target, rid: str, out_path: Path, work: Path,
                   audio_wav: Path, af: list[str], loud_info: dict[str, Any], total: int, audio_only: bool, lo: float, hi: float, prefix: str,
                   cue_lines: Optional[list], rc: RenderContext, tools: ff.Tools, fps: float, fps_expr: str, ext: str, t_start: float) -> dict[str, Any]:
    """The picture chunks (in parallel), the mux and the quality check of one output."""
    pid = params["project"]
    p = tg.project
    W, H = tg.width, tg.height
    at = lambda x: lo + (hi - lo) * x  # noqa: E731  progress inside this output
    encoder = None
    chunk_files: list[str] = []
    ctx.check()
    if not audio_only:
        rc.prepare(ctx)  # nested sequences: made once (cached), read like media by every output
        ass, counts = build_ass(p, rc.words_for, cue_lines)
        ass_name = None
        if counts["captions"] or counts["titles"]:
            (work / "subs.ass").write_text(ass, encoding="utf-8")
            ass_name = "subs.ass"
        out = C.Output(W, H, fps, fps_expr, use_proxies=rc.use_proxies, factor=W / p.canvas.width)
        hwdec = svc.hwdec_enabled() and not rc.use_proxies
        cx = C.ChainCtx(out, rc.lut_name, lambda c: None, hwdec=hwdec)
        plan = C.plan_chunks(p, total, fps)
        vargs, encoder = _video_args(svc, spec.get("codec", "h264"), spec.get("quality", "high"), fps, W, H)
        frames_total = sum(b - a for a, b in plan)
        done_frames = [0]
        progress_lock = threading.Lock()
        chunk_ext = ".mov" if spec.get("codec") == "prores" else ".mp4"

        def run_chunk(i: int, f0: int, f1: int) -> str:
            ctx.check()
            g = C.chunk_graph(p, i, f0, f1, out, rc.media, cx, ass_name)
            gfile = f"graph_{i:04d}.txt"
            (work / gfile).write_text(g.graph, encoding="utf-8")
            fname = f"chunk_{i:04d}{chunk_ext}"
            args = [a for inp in g.inputs for a in inp] + _script_flag(tools, gfile) + [
                "-map", f"[{g.out_label}]", "-frames:v", str(f1 - f0), "-r", fps_expr, *vargs, "-an"]
            if chunk_ext == ".mp4":
                args += ["-video_track_timescale", "90000"]
            args.append(fname)
            last = [0.0]

            def prog(x: float) -> None:
                with progress_lock:
                    delta = x - last[0]
                    last[0] = x
                    done_frames[0] += delta * (f1 - f0)
                    ctx.progress(at(0.9 * done_frames[0] / frames_total), f"{prefix}picture {int(done_frames[0])}/{frames_total} frames")

            ff.run(tools, args, duration_ms=int((f1 - f0) * 1000 / fps), progress=prog, handle=ctx.handle(), cwd=work)
            return fname

        workers = 1 if len(plan) == 1 else svc.config.render_workers
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_chunk, i, a, b): i for i, (a, b) in enumerate(plan)}
            results: dict[int, str] = {}
            try:
                for fut in as_completed(futures):
                    results[futures[fut]] = fut.result()
            except BaseException:
                ctx.cancelled = True
                for h in ctx.handles:
                    h.cancel()
                raise
        chunk_files = [results[i] for i in range(len(plan))]
        (work / "chunks.txt").write_text("".join(f"file '{f}'\n" for f in chunk_files), encoding="utf-8")
    # mux
    ctx.check()
    ctx.progress(at(0.92), f"{prefix}mux", force=True)
    tmp_out = work / f"out{ext}"
    if audio_only:
        acodec = ["-c:a", "libmp3lame", "-b:a", spec["abr"]] if spec["container"] == "mp3" else ["-c:a", "pcm_s16le"]
        ff.run(tools, ["-i", str(audio_wav), *af, *acodec, str(tmp_out)], duration_ms=total, handle=ctx.handle())
    elif spec["container"] == "gif":
        mute = work / f"video{'.mp4'}"
        ff.run(tools, ["-f", "concat", "-safe", "0", "-i", "chunks.txt", "-c", "copy", str(mute)], cwd=work, handle=ctx.handle())
        gfps = spec.get("gif_fps", 15)
        ff.run(tools, ["-i", str(mute), "-vf", f"fps={gfps},split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4",
                       "-loop", "0", str(tmp_out)], handle=ctx.handle())
    else:
        acodec = ["-c:a", "pcm_s16le"] if spec["container"] == "mov" else ["-c:a", "aac", "-b:a", spec.get("abr") or "192k"]
        extra = ["-movflags", "+faststart"] if spec["container"] == "mp4" else []
        ff.run(tools, ["-f", "concat", "-safe", "0", "-i", "chunks.txt", "-i", str(audio_wav), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                       *af, *acodec, "-t", ff.seconds(total), *extra, str(tmp_out)], duration_ms=total, cwd=work, handle=ctx.handle(),
               progress=lambda x: ctx.progress(at(0.92 + 0.07 * x), f"{prefix}mux"))
    shutil.move(str(tmp_out), out_path)
    # check
    qc = quality_check(svc, out_path, total, W if not audio_only else 0, H if not audio_only else 0, audio_only=audio_only,
                       gif=spec["container"] == "gif")
    qc.update(loud_info)
    info = {"id": rid, "project": pid, "path": str(out_path), "bytes": out_path.stat().st_size, "duration_ms": total,
            "duration": ms_to_tc(total), "width": W if not audio_only else 0, "height": H if not audio_only else 0, "preset": params.get("preset", "final"),
            "encoder": None if audio_only else encoder, "seconds": round(time.time() - t_start, 1), "qc": qc,
            "url": f"/api/renders/{rid}/file"}
    if tg.key:
        info.update({"variant": tg.key, "format": tg.label, "reframe": (tg.notes or {}).get("reframe"), "framing": tg.notes})
    svc.db.execute("INSERT INTO renders(id, project_id, job_id, preset, mode, path, bytes, duration_ms, width, height, qc, created_ts, variant) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (rid, pid, ctx.id, info["preset"], "final", str(out_path), info["bytes"], total, info["width"], info["height"], dumps(qc), time.time(),
                    tg.key))
    lang = params.get("captions_language") or ""
    if params.get("subtitles") and p.captions.enabled and not lang:
        srt = out_path.with_suffix(".srt")
        srt.write_text(build_srt(p, rc.words_for), encoding="utf-8")
        info["subtitles"] = str(srt)
    if params.get("subtitles") and lang and cue_lines is not None:
        code = subtitles_mod.resolve_language(lang)[0]
        srt = out_path.with_suffix(f".{code}.srt")
        srt.write_text(build_srt(p, rc.words_for, cue_lines), encoding="utf-8")
        info["subtitles"] = str(srt)
    return info


def quality_check(svc: "Services", path: Path, expect_ms: int, W: int, H: int, *, audio_only: bool = False, gif: bool = False) -> dict[str, Any]:
    tools = svc.tools()
    problems: list[str] = []
    info = ff.summarize(ff.probe(tools, path), path)
    got = info["duration_ms"]
    if abs(got - expect_ms) > max(120, expect_ms * 0.01) and not gif:
        problems.append(f"Duration {ms_to_tc(got)} instead of {ms_to_tc(expect_ms)}.")
    if not audio_only and (info["width"], info["height"]) != (W, H):
        problems.append(f"Size {info['width']}x{info['height']} instead of {W}x{H}.")
    if not gif and not info["has_audio"]:
        problems.append("No audio track.")
    loud: dict[str, Any] = {}
    if info["has_audio"]:
        try:
            loud = audio_an.loudness(tools, path)
            if loud.get("integrated_lufs") is None or loud["integrated_lufs"] < -60:
                problems.append("The sound is silent.")
            if (loud.get("true_peak_dbtp") or -99) > 0.5:
                problems.append(f"The sound clips (true peak {loud['true_peak_dbtp']} dBTP).")
        except Exception as error:  # noqa: BLE001
            loud = {"error": str(error)}
    return {"ok": not problems, "problems": problems, "duration_ms": got, "width": info["width"], "height": info["height"],
            "video_codec": info["video_codec"], "audio_codec": info["audio_codec"], **{k: v for k, v in loud.items() if v is not None}}


# ---------------------------------------------------------------- frames

def render_frame(svc: "Services", project_id: str, t_ms: int, *, width: int = 0, fmt: str = "jpg",
                 output_path: Path | None = None) -> Path:
    """What the timeline shows at ``t_ms`` (exactly as the render would draw it), as an image."""
    p = project_store.doc(svc, project_id)
    if p.duration <= 0:
        raise LumiereError("The timeline is empty.")
    t_ms = max(0, min(int(t_ms), p.duration - 1))
    rid = new_id("frm")
    work = svc.config.work_dir / rid
    work.mkdir(parents=True, exist_ok=True)
    try:
        rc = RenderContext(svc, p, work, use_proxies=False, stack=(project_id,))
        rc.prepare()
        W, H = p.canvas.width, p.canvas.height
        if width and width < W:
            H = max(2, int(round(H * width / W / 2)) * 2)
            W = max(2, int(round(width / 2)) * 2)
        fps = p.canvas.fps
        out = C.Output(W, H, fps, ff.fps_fraction(fps), factor=W / p.canvas.width)
        cx = C.ChainCtx(out, rc.lut_name, lambda c: None)
        ass, counts = build_ass(p, rc.words_for)
        ass_name = None
        if counts["captions"] or counts["titles"]:
            (work / "subs.ass").write_text(ass, encoding="utf-8")
            ass_name = "subs.ass"
        f0 = C.frame_of_ms(t_ms, fps)
        g = C.chunk_graph(p, 0, f0, f0 + 1, out, rc.media, cx, ass_name)
        (work / "graph.txt").write_text(g.graph, encoding="utf-8")
        if output_path is None:
            frames_dir = svc.config.renders_dir / "frames"
            dest = frames_dir / f"{project_id}-{t_ms}-{W}.{fmt}"
        else:
            dest = Path(output_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tools = svc.tools()
        args = [a for inp in g.inputs for a in inp] + _script_flag(tools, "graph.txt") + ["-map", f"[{g.out_label}]", "-frames:v", "1"]
        if fmt == "jpg":
            args += ["-q:v", "3"]
        args.append(str(dest))
        ff.run(tools, args, cwd=work, timeout=120)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- lossless cut

def copy_cut_check(p: Project) -> list[str]:
    """Why a timeline cannot be exported without re-encoding (empty list = it can)."""
    reasons = []
    used = [c for t in p.tracks for c in t.clips]
    media_ids = {c.media for c in used if c.media}
    main = p.main_track()
    if len(media_ids) != 1:
        reasons.append("it must use exactly one source file")
    for t in p.tracks:
        if t.clips and t is not main:
            reasons.append(f"track {t.name or t.id} has clips (only the main track can)")
    for c in used:
        if c.type == "text":
            reasons.append("it has titles")
            break
        if c.type == "sequence":
            reasons.append(f"clip {c.id} is a nested sequence")
            break
        if c.speed != 1 or c.speed_keys or c.reverse or c.filters or c.transition_in or c.keyframes or c.reframe or c.fade_in or c.fade_out or c.mask:
            reasons.append(f"clip {c.id} has speed, effects, masks, fades or transitions")
            break
        if c.transform.fit not in ("contain", "cover") or c.transform.scale != 1 or c.transform.x or c.transform.y:
            reasons.append(f"clip {c.id} is moved or scaled")
            break
    if p.captions.enabled:
        reasons.append("burned captions are on")
    return reasons


def copy_cut_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    """Cut and join without re-encoding: every cut snaps back to the keyframe before it (the result can start up to a few
    frames early per cut). Seconds instead of minutes for long gameplay."""
    pid = ctx.params["project"]
    p = project_store.doc(svc, pid)
    reasons = copy_cut_check(p)
    if reasons:
        raise LumiereError("This timeline needs a normal render: " + "; ".join(reasons) + ".", code="copy_not_possible")
    main = p.main_track()
    clips = sorted(main.clips, key=lambda c: c.start)
    info = media_store.get(svc, clips[0].media)
    tools = svc.tools()
    kfs = ff.keyframes(tools, Path(info["path"]))
    rid = new_id("rnd")
    work = svc.config.work_dir / rid
    work.mkdir(parents=True, exist_ok=True)
    try:
        parts = []
        snapped = []
        for i, c in enumerate(clips):
            ctx.check()
            start = max((k for k in kfs if k <= c.src_in), default=0)
            snapped.append({"clip": c.id, "asked_ms": c.src_in, "starts_ms": start})
            part = f"part_{i:04d}{Path(info['path']).suffix.lower() if Path(info['path']).suffix.lower() in ('.mp4', '.mov', '.mkv') else '.mkv'}"
            ff.run(tools, ["-ss", ff.seconds(start), "-i", info["path"], "-to", ff.seconds(c.src_out - start), "-map", "0:v:0", "-map", "0:a?",
                           "-c", "copy", "-avoid_negative_ts", "make_zero", part], cwd=work, handle=ctx.handle())
            parts.append(part)
            ctx.progress(0.9 * (i + 1) / len(clips), f"{i + 1}/{len(clips)}")
        (work / "parts.txt").write_text("".join(f"file '{f}'\n" for f in parts), encoding="utf-8")
        name = safe_filename(_strip_media_ext(ctx.params.get("filename") or "") or "corte")
        out_dir = Path(ctx.params["folder"]) if ctx.params.get("folder") else svc.config.renders_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(parts[0]).suffix
        out_path = out_dir / f"{name}-copia{ext}"
        n = 2
        while out_path.exists():
            out_path = out_dir / f"{name}-copia ({n}){ext}"
            n += 1
        ff.run(tools, ["-f", "concat", "-safe", "0", "-i", "parts.txt", "-c", "copy", "-map", "0", str(out_path)], cwd=work, handle=ctx.handle())
        probe = ff.summarize(ff.probe(tools, out_path), out_path)
        svc.db.execute("INSERT INTO renders(id, project_id, job_id, preset, mode, path, bytes, duration_ms, width, height, qc, created_ts) "
                       "VALUES (?, ?, ?, 'copy', 'copy', ?, ?, ?, ?, ?, ?, ?)",
                       (rid, pid, ctx.id, str(out_path), out_path.stat().st_size, probe["duration_ms"], probe["width"], probe["height"],
                        dumps({"ok": True, "snapped": snapped}), time.time()))
        return {"id": rid, "path": str(out_path), "duration_ms": probe["duration_ms"], "snapped": snapped, "url": f"/api/renders/{rid}/file",
                "note": "Cuts start at the keyframe before each in point."}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def renders_list(svc: "Services", project_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM renders" + (" WHERE project_id = ?" if project_id else "") + " ORDER BY created_ts DESC LIMIT ?"
    args = (project_id, limit) if project_id else (limit,)
    out = []
    for r in svc.db.query(sql, args):
        out.append({"id": r["id"], "project": r["project_id"], "preset": r["preset"], "mode": r["mode"], "path": r["path"], "bytes": r["bytes"],
                    "duration_ms": r["duration_ms"], "width": r["width"], "height": r["height"], "qc": json.loads(r["qc"] or "{}"),
                    "exists": Path(r["path"]).exists(), "created_ts": r["created_ts"], "url": f"/api/renders/{r['id']}/file",
                    "variant": r["variant"] or None, "job": r["job_id"]})
    return out


def subtitles_export(svc: "Services", project_id: str, fmt: str, language: str = "", dual: bool = False) -> tuple[str, str]:
    """The captions as srt, vtt or ass. With ``language``: the stored translation of that language (``dual``: the original
    line and its translation under it); the timing of every cue is the original's."""
    p = project_store.doc(svc, project_id)
    rc = RenderContext(svc, p, svc.config.work_dir)
    cues = subtitles_mod.translated_cues(svc, project_id, language, dual=dual, p=p) if language else None
    if fmt == "srt":
        return build_srt(p, rc.words_for, cues), "application/x-subrip"
    if fmt == "vtt":
        return build_vtt(p, rc.words_for, cues), "text/vtt"
    if fmt == "ass":
        cap = p.captions.model_copy()
        p.captions = cap.model_copy(update={"enabled": True})
        return build_ass(p, rc.words_for, cues)[0], "text/plain"
    raise LumiereError("Subtitles are srt, vtt or ass.")


def edl_export(svc: "Services", project_id: str) -> str:
    """CMX 3600 EDL of the main track (for another editor)."""
    p = project_store.doc(svc, project_id)
    main = p.main_track()
    fps = p.canvas.fps
    lines = [f"TITLE: {clip(project_store.summary(svc, svc.db.one('SELECT * FROM projects WHERE id = ?', (project_id,)))['name'], 60)}",
             "FCM: NON-DROP FRAME", ""]
    for i, c in enumerate(sorted(main.clips if main else [], key=lambda c: c.start), start=1):
        if c.type != "media":
            continue  # titles and nested sequences have no source file
        info = media_store.get(svc, c.media)
        tc = lambda ms: ms_to_tc(ms, fps)  # noqa: E731
        lines.append(f"{i:03d}  AX       AA/V  C        {tc(c.src_in)} {tc(c.src_out)} {tc(c.start)} {tc(c.end)}")
        lines.append(f"* FROM CLIP NAME: {Path(info['path']).name}")
        if c.speed != 1 or c.has_ramp:
            mean = (c.src_out - c.src_in) / max(1, c.duration) if c.has_ramp else c.speed  # a curve is listed at its mean speed
            lines.append(f"M2   AX       {mean * fps:05.1f}                {tc(c.src_in)}")
        lines.append("")
    return "\n".join(lines)
