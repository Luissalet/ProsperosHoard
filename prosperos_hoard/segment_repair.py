"""Segment repair and join review.

``repair_segment`` replaces a time range of a video with another clip without redoing the rest:

* the range is snapped to frames; the replacement is conformed to the source's resolution, frame rate, pixel format and
  audio layout (sample rate, channels);
* the frames before the nearest keyframe at or before the range, and after the nearest keyframe at or after it, are
  stream-copied, so they stay bit-identical. Only the GOPs that touch the range are re-encoded (``reencoded_range_s``
  says which). When copying is not possible (not H.264 in MP4, or the copied parts do not decode to the same frames),
  everything is re-encoded and the result says ``exact_outside=false``;
* the seams are a hard cut (with an optional few-millisecond audio declick) or a crossfade placed inside the repaired
  range, so the total duration and the frame count do not change;
* the audio is rebuilt from decoded samples around the repaired range and encoded once.

``review_join`` checks each seam of a video: black frames, frozen frames, colour jump (mean colour and histogram
difference across the cut), audio level jump and click, and that the duration is the expected one, against thresholds
that the caller can change, and saves a thumbnail strip around each seam for a person to look at. Every check is
a heuristic: a warning asks for a look, a pass does not approve the edit.
"""

from __future__ import annotations

import math
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from . import continuity as continuity_mod
from . import procutil
from .backend import ffmpeg_path
from .engine import EngineError, _rel, _video_thumbnail, make_thumbnail
from .hoard_link.media.ffmpeg import FFmpeg, FFmpegError
from .ids import new_id
from .store import Store
from .util import now_iso

TRANSITIONS = ("cut", "crossfade")
FRAMINGS = ("fit", "fill")
THRESHOLDS: dict[str, float] = {
    "black_luma": 12.0,            # mean luma (0-255) under which a frame counts as black ...
    "black_context_luma": 30.0,    # ... when the surrounding frames' median luma is above this
    "frozen_frames": 4,            # this many identical consecutive frames touching the seam are a freeze
    "frozen_diff": 0.4,            # mean absolute pixel difference (0-255) under which two frames are identical
    "colour_warn": 12.0,           # largest per-channel change of the mean colour across the cut (0-255)
    "colour_fail": 40.0,
    "hist_warn": 0.25,             # half the L1 distance between the colour histograms (0 equal .. 1 disjoint)
    "hist_fail": 0.6,
    "level_warn_db": 6.0,          # RMS change across the cut over 150 ms on each side
    "level_fail_db": 12.0,
    "level_floor_db": -70.0,       # both sides quieter than this: not measured
    "click_ratio": 5.0,            # largest sample-to-sample step at the seam over the 99.9th percentile elsewhere
    "click_min": 0.08,             # ... and at least this fraction of full scale
    "click_fail": 0.4,
    "duration_tol_s": 0.06,        # AAC adds a little padding; more than this is a changed duration
}
WINDOW_FRAMES = 12
THUMB = 6                          # frames in a seam strip: three before the cut, three after


def _exe() -> str:
    exe = ffmpeg_path()
    if not exe:
        raise EngineError("no_ffmpeg", "ffmpeg is needed for segment repair and join review")
    return exe


def _run(argv: list[str], what: str, timeout: int = 3600) -> procutil.subprocess.CompletedProcess:
    done = procutil.run(argv, text=True, timeout=timeout)
    if done.returncode != 0:
        tail = " ".join((done.stderr or "").strip().splitlines()[-3:])[:500]
        raise EngineError("repair_step_failed", f"{what}: {tail or f'ffmpeg exit {done.returncode}'}")
    return done


def _probe(path: Path) -> dict[str, Any]:
    try:
        ff = FFmpeg()
        raw = ff.probe(path)
        info = ff.summarize(raw)
    except FFmpegError as exc:
        raise EngineError("bad_media", f"{path.name}: {exc}") from None
    video = next((s for s in raw.get("streams", []) if s.get("codec_type") == "video"), {})
    rate = video.get("r_frame_rate") or video.get("avg_frame_rate") or ""
    if not re.fullmatch(r"\d+/\d+", rate) or rate.endswith("/0"):
        rate = f"{info.get('fps') or 25}"
    info["fps_str"] = rate
    info["container"] = ((raw.get("format") or {}).get("format_name") or "")
    return info


def _fps_value(info: dict[str, Any]) -> float:
    a, _, b = str(info["fps_str"]).partition("/")
    return float(a) / float(b or 1)


# ------------------------------------------------------------------ frame analysis

def analyze_video(path: Path) -> dict[str, Any]:
    """One decode pass: the md5 of every decoded frame (presentation order), its presentation time and whether it is a keyframe."""
    exe = _exe()
    done = procutil.run([exe, "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:v:0", "-an", "-vf", "showinfo",
                         "-f", "framemd5", "-"], text=True, timeout=3600)
    if done.returncode != 0:
        raise EngineError("bad_media", f"could not decode {path.name}")
    hashes = [ln.rsplit(",", 1)[-1].strip() for ln in done.stdout.splitlines() if ln and not ln.startswith("#")]
    times, keys = [], []
    for m in re.finditer(r"n:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:(\S+).*?iskey:(\d)", done.stderr or ""):
        times.append(m.group(3))
        keys.append(m.group(4) == "1")
    if len(times) != len(hashes):  # showinfo format differs: no keyframe data, copy path is not used
        times, keys = [], []
    return {"frames": len(hashes), "hashes": hashes, "pts_time": times,
            "keyframes": [i for i, k in enumerate(keys) if k]}


def frame_hashes(path: Path) -> list[str]:
    return analyze_video(path)["hashes"]


# ------------------------------------------------------------------ repair

def _scale_chain(info: dict[str, Any], framing: str) -> str:
    w, h = int(info["width"]), int(info["height"])
    if framing == "fill":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"


def _mid_filter(src: dict[str, Any], fps: float, ka: int, kb: int, sf: int, ef: int, d_in: int, d_out: int,
                rep_start: int, framing: str) -> str:
    """Filter graph of the re-encoded part [ka, kb): old frames, the replacement over [sf, ef), old frames."""
    lf = ef - sf
    a_end = sf + d_in
    b_start = ef - d_out
    pix = src.get("pix_fmt") or "yuv420p"
    f = src["fps_str"]
    g = [f"[0:v]setpts=PTS-STARTPTS,setsar=1,format={pix},split=2[s0][s1]"]
    names = []
    if a_end > ka:
        g.append(f"[s0]trim=start_frame={ka}:end_frame={a_end},setpts=PTS-STARTPTS,fps={f}[A]")
        names.append("A")
    else:
        g.append("[s0]nullsink")
    g.append(f"[1:v]{_scale_chain(src, framing)},setsar=1,fps={f},format={pix},"
             f"trim=start_frame={rep_start}:end_frame={rep_start + lf},setpts=PTS-STARTPTS[N]")
    names.append("N")
    if kb > b_start:
        g.append(f"[s1]trim=start_frame={b_start}:end_frame={kb},setpts=PTS-STARTPTS,fps={f}[B]")
        names.append("B")
    else:
        g.append("[s1]nullsink")
    lens = {"A": max(0, a_end - ka), "N": lf, "B": max(0, kb - b_start)}
    if d_in == 0 and d_out == 0:
        g.append("".join(f"[{n}]" for n in names) + f"concat=n={len(names)}:v=1:a=0[mid]")
        return ";".join(g)
    cur, cur_len = names[0], lens[names[0]]
    for nxt, d in (("N", d_in), ("B", d_out)):
        if nxt == names[0] or nxt not in names:
            continue
        if nxt == "N" and "A" not in names:
            continue
        if d:
            g.append(f"[{cur}][{nxt}]xfade=transition=fade:duration={d / fps:.6f}:offset={(cur_len - d) / fps:.6f}[x_{nxt}]")
            cur, cur_len = f"x_{nxt}", cur_len + lens[nxt] - d
        else:
            g.append(f"[{cur}][{nxt}]concat=n=2:v=1:a=0[c_{nxt}]")
            cur, cur_len = f"c_{nxt}", cur_len + lens[nxt]
    g.append(f"[{cur}]trim=end_frame={kb - ka},setpts=PTS-STARTPTS[mid]")
    return ";".join(g)


def _audio_filter(src: dict[str, Any], fps: float, total_frames: int, sf: int, ef: int, d_in: int, d_out: int,
                  rep_in_s: float, rep_has_audio: bool, declick_ms: float) -> str:
    sr = int(src.get("sample_rate") or 48000)
    ch = int(src.get("channels") or 2)
    layout = "mono" if ch == 1 else "stereo"
    fmt = f"aresample={sr},aformat=sample_fmts=fltp:channel_layouts={layout}"
    total = total_frames / fps
    lf = ef - sf
    ll = lf / fps
    g = [f"[0:a]{fmt},asplit=2[sa][sb]"]
    pieces = []
    t_a = (sf + d_in) / fps
    t_b = (ef - d_out) / fps
    dk = max(0.0, declick_ms) / 1000.0
    cut = d_in == 0 and d_out == 0
    if sf > 0:
        fade = f",afade=t=out:st={max(0.0, t_a - dk):.6f}:d={dk:.6f}" if cut and dk else ""
        g.append(f"[sa]atrim=0:{t_a:.6f},asetpts=PTS-STARTPTS{fade}[aA]")
        pieces.append("aA")
    else:
        g.append("[sa]anullsink")
    if rep_has_audio:
        nfade = ""
        if cut and dk:
            if sf > 0:
                nfade += f",afade=t=in:st=0:d={dk:.6f}"
            if ef < total_frames:
                nfade += f",afade=t=out:st={max(0.0, ll - dk):.6f}:d={dk:.6f}"
        g.append(f"[1:a]{fmt},atrim={rep_in_s:.6f}:{rep_in_s + ll:.6f},asetpts=PTS-STARTPTS,apad=whole_dur={ll:.6f},"
                 f"atrim=0:{ll:.6f}{nfade}[aN]")
    else:
        g.append(f"anullsrc=r={sr}:cl={layout},atrim=0:{ll:.6f},asetpts=PTS-STARTPTS[aN]")
    pieces.append("aN")
    if ef < total_frames:
        fade = f",afade=t=in:st=0:d={dk:.6f}" if cut and dk else ""
        g.append(f"[sb]atrim={t_b:.6f},asetpts=PTS-STARTPTS{fade}[aB]")
        pieces.append("aB")
    else:
        g.append("[sb]anullsink")
    if cut:
        g.append("".join(f"[{p}]" for p in pieces) + f"concat=n={len(pieces)}:v=0:a=1[mix]")
    else:
        cur = pieces[0]
        for nxt, d in (("aN", d_in), ("aB", d_out)):
            if nxt not in pieces or nxt == pieces[0]:
                continue
            if d:
                g.append(f"[{cur}][{nxt}]acrossfade=d={d / fps:.6f}:c1=tri:c2=tri[x_{nxt}]")
            else:
                g.append(f"[{cur}][{nxt}]concat=n=2:v=0:a=1[x_{nxt}]")
            cur = f"x_{nxt}"
        g.append(f"[{cur}]anull[mix]")
    g.append(f"[mix]apad=whole_dur={total:.6f},atrim=0:{total:.6f},asetpts=PTS-STARTPTS[aout]")
    return ";".join(g)


def _stitch(exe: str, work: Path, src_path: Path, rep_path: Path, src: dict[str, Any], fps: float, ka: int, kb: int,
            kf_times: dict[int, str], n: int, sf: int, ef: int, d_in: int, d_out: int, rep_start: int, framing: str,
            enc: list[str]) -> Path:
    """Video-only file: stream-copied head [0, ka), re-encoded middle [ka, kb), stream-copied tail [kb, n)."""
    parts = []
    base = [exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if ka > 0:
        head = work / "head.mp4"
        _run([*base, "-i", str(src_path), "-map", "0:v:0", "-an", "-c:v", "copy", "-frames:v", str(ka), str(head)], "copy head")
        parts.append(head)
    mid = work / "mid.mp4"
    graph = _mid_filter(src, fps, ka, kb, sf, ef, d_in, d_out, rep_start, framing)
    _run([*base, "-i", str(src_path), "-i", str(rep_path), "-filter_complex", graph, "-map", "[mid]", "-an", *enc,
          "-frames:v", str(kb - ka), str(mid)], "encode repaired part")
    parts.append(mid)
    if kb < n:
        tail = work / "tail.mp4"
        seek = float(kf_times[kb]) + 0.1 / fps
        _run([*base, "-ss", f"{seek:.6f}", "-i", str(src_path), "-map", "0:v:0", "-an", "-c:v", "copy", str(tail)], "copy tail")
        parts.append(tail)
    out = work / "video.mp4"
    if len(parts) == 1:
        shutil.copyfile(parts[0], out)
    else:
        listing = work / "parts.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        _run([*base, "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)], "join parts")
    return out


def repair_segment(store: Store, asset_id: str, replacement_asset_id: str, start_s: Optional[float] = None,
                   end_s: Optional[float] = None, *, shot_id: Optional[str] = None, bible_id: Optional[str] = None,
                   transition: str = "cut", crossfade_s: float = 0.3, declick_ms: float = 5.0,
                   replacement_in_s: float = 0.0, framing: str = "fit", pad: bool = False,
                   entries: Optional[list[str]] = None, references: Optional[list[str]] = None,
                   correction_id: Optional[str] = None, verify_untouched: bool = True, review: bool = True,
                   thresholds: Optional[dict[str, float]] = None, name: Optional[str] = None,
                   progress: Optional[Callable[..., None]] = None) -> dict[str, Any]:
    """Replace [start_s, end_s) of a video asset with another clip and review the seams. Creates a new video asset;
    the source is untouched. Without a range, the shot's own range in the bible is used."""
    progress = progress or (lambda *a, **k: None)
    exe = _exe()
    if transition not in TRANSITIONS:
        raise EngineError("bad_repair", f"transition must be one of {', '.join(TRANSITIONS)}")
    if framing not in FRAMINGS:
        raise EngineError("bad_repair", f"framing must be one of {', '.join(FRAMINGS)}")
    bible = continuity_mod.load(store.data_dir, bible_id) if bible_id else None
    if shot_id and not bible:
        raise EngineError("bad_repair", "shot_id needs a bible_id")
    if bible and shot_id:
        shot = bible["shots"].get(shot_id)
        if shot is None:
            raise EngineError("not_found", f"shot {shot_id} not found in the bible")
        start_s = shot.get("start_s") if start_s is None else start_s
        end_s = shot.get("end_s") if end_s is None else end_s
    if start_s is None or end_s is None:
        raise EngineError("bad_repair", "give start_s and end_s, or a shot with a range")
    asset = store.get_asset(asset_id)
    rep_asset = store.get_asset(replacement_asset_id)
    if asset["kind"] != "video" or rep_asset["kind"] != "video":
        raise EngineError("bad_repair", "the video and its replacement must both be video assets")
    src_path = (store.data_dir / asset["file_path"]).resolve()
    rep_path = (store.data_dir / rep_asset["file_path"]).resolve()
    src, rep = _probe(src_path), _probe(rep_path)
    fps = _fps_value(src)
    progress(0.03, "analysing the video")
    ana = analyze_video(src_path)
    n = ana["frames"]
    sf, ef = int(round(float(start_s) * fps)), int(round(float(end_s) * fps))
    if not 0 <= sf < ef <= n:
        raise EngineError("bad_repair", f"the range {start_s}-{end_s} s is outside the video ({n / fps:.3f} s, {n} frames)")
    lf = ef - sf
    ll = lf / fps
    rep_in_s = float(replacement_in_s or 0)
    if rep_in_s < 0:
        raise EngineError("bad_repair", "replacement_in_s cannot be negative")
    if (float(rep.get("duration_s") or 0) - rep_in_s) < ll - 0.5 / fps and not pad:
        raise EngineError("replacement_too_short", f"the replacement has {float(rep.get('duration_s') or 0) - rep_in_s:.2f} s "
                          f"from replacement_in_s; the range needs {ll:.2f} s (pad=true holds the last frame)")
    d = 0
    if transition == "crossfade":
        d = max(1, int(round(float(crossfade_s) * fps)))
        if 2 * d > lf:
            raise EngineError("bad_repair", f"the range ({ll:.2f} s) is too short for crossfades of {d / fps:.2f} s on both sides")
    d_in = d if sf > 0 else 0
    d_out = d if ef < n else 0
    rep_start = int(round(rep_in_s * fps))
    rep_for_video = rep_path
    work = Path(tempfile.mkdtemp(prefix="repair_", dir=str(store.data_dir)))
    try:
        if pad:  # hold the replacement's last frame and extend its audio with silence
            padded = work / "padded.mp4"
            extra = max(0.0, rep_in_s + ll - float(rep.get("duration_s") or 0)) + 1.0
            _run([exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(rep_path), "-vf",
                  f"tpad=stop_mode=clone:stop_duration={extra:.3f}", "-af", f"apad=pad_dur={extra:.3f}", "-c:v", "libx264",
                  "-crf", "16", "-c:a", "aac", str(padded)], "pad replacement")
            rep_for_video = padded
        enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", src.get("pix_fmt") or "yuv420p",
               "-r", src["fps_str"], "-fps_mode", "cfr"]
        copyable = (src.get("video_codec") == "h264" and "mp4" in str(src.get("container")) and bool(ana["keyframes"])
                    and len(ana["pts_time"]) == n)
        kfs = ana["keyframes"] or [0]
        ka = max([k for k in kfs if k <= sf] or [0]) if copyable else 0
        kb = min([k for k in kfs if k >= ef] or [n]) if copyable else n
        kf_times = {i: t for i, t in zip(range(n), ana["pts_time"])} if copyable else {}
        total_s = n / fps
        has_audio = bool(src.get("has_audio"))
        rep_has_audio = bool(rep.get("has_audio"))
        notes = []
        if has_audio and not rep_has_audio:
            notes.append("the replacement has no audio: silence was used for the repaired range")
        if sf != float(start_s) * fps or ef != float(end_s) * fps:
            notes.append(f"the range was snapped to frames {sf}-{ef} ({sf / fps:.4f}-{ef / fps:.4f} s)")
        exact, fallback = copyable, None
        video = None
        for attempt in (0, 1):
            ka_, kb_ = (ka, kb) if attempt == 0 else (0, n)
            progress(0.15 + 0.4 * attempt, "re-encoding the repaired part" if ka_ or kb_ < n else "re-encoding the video")
            tmp = work / f"try{attempt}"
            tmp.mkdir()
            video = _stitch(exe, tmp, src_path, rep_for_video, src, fps, ka_, kb_, kf_times, n, sf, ef, d_in, d_out,
                            rep_start, framing, enc)
            if attempt == 0 and not (ka_ == 0 and kb_ == n):
                check = analyze_video(video)
                same = (check["frames"] == n and check["hashes"][:ka_] == ana["hashes"][:ka_]
                        and check["hashes"][kb_:] == ana["hashes"][kb_:])
                if same:
                    break
                fallback = ("copied parts did not decode to the same frames" if check["frames"] == n else
                            f"copy path gave {check['frames']} frames instead of {n}")
                exact = False
                continue
            ka, kb = ka_, kb_
            break
        else:  # pragma: no cover - the loop always breaks on attempt 1
            raise EngineError("repair_step_failed", "could not stitch the repaired video")
        if fallback:
            ka, kb = 0, n
            notes.append(f"all frames were re-encoded: {fallback}")
        elif not copyable:
            exact = False
            notes.append("the source is not H.264 in MP4 (or has no keyframe data): all frames were re-encoded")
        out_id = new_id("a")
        out_path = store.path_for_asset_file(out_id, ".mp4").resolve()
        base = [exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
        progress(0.6, "rebuilding the audio")
        if has_audio:
            graph = _audio_filter(src, fps, n, sf, ef, d_in, d_out, rep_in_s, rep_has_audio, declick_ms)
            audio = work / "audio.m4a"
            _run([*base, "-i", str(src_path), "-i", str(rep_for_video), "-filter_complex", graph,
                  "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", str(audio)], "rebuild audio")
            _run([*base, "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                  "-movflags", "+faststart", str(out_path)], "mux")
        else:
            _run([*base, "-i", str(video), "-map", "0:v:0", "-c", "copy", "-movflags", "+faststart", str(out_path)], "mux")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        if "out_path" in locals():
            out_path.unlink(missing_ok=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    progress(0.8, "checking the result")
    info = _probe(out_path)
    result_ana = analyze_video(out_path) if verify_untouched else None
    untouched = None
    if result_ana is not None:
        outside = [i for i in range(n) if i < ka or i >= kb]
        mism = [i for i in outside if i >= result_ana["frames"] or result_ana["hashes"][i] != ana["hashes"][i]]
        untouched = {"frames_total": n, "frames_result": result_ana["frames"], "frames_compared": len(outside),
                     "frames_identical": len(outside) - len(mism), "identical": not mism and result_ana["frames"] == n,
                     "first_mismatch": mism[0] if mism else None,
                     "reencoded_frames": [ka, kb], "reencoded_range_s": [round(ka / fps, 4), round(kb / fps, 4)]}
    try:
        thumb = _video_thumbnail(out_path, store.path_for_thumb(out_id))
    except Exception:  # noqa: BLE001
        thumb = None
    refs = list(dict.fromkeys([replacement_asset_id, *(references or [])]))
    stamped: dict[str, int] = {}
    if bible:
        shots = [shot_id] if shot_id else [sid for sid, s in bible["shots"].items()
                                           if s.get("start_s") is not None and s["start_s"] < ef / fps and sf / fps < s["end_s"]]
        for sid in shots:
            stamped.update(continuity_mod._stamp(bible, bible["shots"][sid], entries))
    recipe = {"operation": "segment_repair", "source_asset_id": asset_id, "replacement_asset_id": replacement_asset_id,
              "range_s": [round(sf / fps, 4), round(ef / fps, 4)], "frames": [sf, ef], "transition": transition,
              "crossfade_s": round(d / fps, 4) if d else 0, "framing": framing, "exact_outside": exact,
              "reencoded_range_s": [round(ka / fps, 4), round(kb / fps, 4)],
              "continuity": ({"bible_id": bible_id, "bible_revision": bible["revision"], "shot_id": shot_id,
                              "entries": stamped, "references": refs, "correction_id": correction_id} if bible else None),
              "input_asset_ids": [asset_id, replacement_asset_id], "created_at": now_iso()}
    new = store.create_asset(project_id=asset["project_id"], kind="video", file_path=_rel(store, out_path), mime="video/mp4",
                             width=info["width"], height=info["height"], duration_s=info["duration_s"], thumb_path=thumb,
                             source="rendered", recipe=recipe, asset_id=out_id,
                             name=(name or f"{asset.get('name') or asset_id} (repaired {sf / fps:.1f}-{ef / fps:.1f} s)")[:100],
                             tags=["segment_repair"])
    prov = None
    if bible:
        prov = continuity_mod.record_repair(store, bible_id, out_id, asset_id, sf / fps, ef / fps, shot_id, refs, entries,
                                            correction_id)
    out: dict[str, Any] = {"asset_id": out_id, "source_asset_id": asset_id, "range_s": recipe["range_s"], "frames": [sf, ef],
                           "transition": transition, "exact_outside": exact, "untouched": untouched,
                           "duration_s": info["duration_s"], "source_duration_s": round(n / fps, 4),
                           "width": info["width"], "height": info["height"], "fps": round(fps, 3),
                           "conformed": {"width": src["width"], "height": src["height"], "fps": round(fps, 3),
                                         "pix_fmt": src.get("pix_fmt"), "sample_rate": src.get("sample_rate"),
                                         "channels": src.get("channels")},
                           "notes": notes, "provenance": prov}
    if review:
        progress(0.9, "reviewing the seams")
        seams = [t for t, real in ((sf / fps, sf > 0), (ef / fps, ef < n)) if real]
        out["join_review"] = review_join(store, out_id, seams, thresholds, expected_duration_s=n / fps, expected_frames=n)
    progress(1.0, "done")
    return out


def run_job(store: Store, job: dict[str, Any], progress: Callable[..., None]) -> dict[str, Any]:
    p = job["params"]
    result = repair_segment(store, p["asset_id"], p["replacement_asset_id"], p.get("start_s"), p.get("end_s"),
                            shot_id=p.get("shot_id"), bible_id=p.get("bible_id"), transition=p.get("transition") or "cut",
                            crossfade_s=p.get("crossfade_s", 0.3), declick_ms=p.get("declick_ms", 5.0),
                            replacement_in_s=p.get("replacement_in_s", 0.0), framing=p.get("framing") or "fit",
                            pad=bool(p.get("pad")), entries=p.get("entries"), references=p.get("references"),
                            correction_id=p.get("correction_id"), verify_untouched=p.get("verify_untouched", True),
                            review=p.get("review", True), thresholds=p.get("thresholds"), progress=progress)
    ids = [result["asset_id"], *[s["strip_asset_id"] for s in (result.get("join_review") or {}).get("seams", [])
                                  if s.get("strip_asset_id")]]
    return {"asset_id": result["asset_id"], "asset_ids": ids, "repair": result}


# ------------------------------------------------------------------ join review

def _decode_window(exe: str, path: Path, first: int, count: int, fps: float, size: tuple[int, int] = (48, 27)) -> np.ndarray:
    t0 = first / fps
    done = procutil.run([exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{t0:.6f}", "-i", str(path), "-an",
                         "-vf", f"scale={size[0]}:{size[1]}:flags=area,format=rgb24", "-frames:v", str(count), "-f", "rawvideo", "-"],
                        timeout=300)
    data = np.frombuffer(done.stdout or b"", dtype=np.uint8)
    per = size[0] * size[1] * 3
    k = data.size // per
    return data[: k * per].reshape(k, size[1], size[0], 3)


def _decode_audio(exe: str, path: Path, t0: float, dur: float, sr: int) -> np.ndarray:
    done = procutil.run([exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, t0):.6f}", "-t", f"{dur:.6f}",
                         "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"], timeout=300)
    return np.frombuffer(done.stdout or b"", dtype="<i2").astype(np.float64) / 32768.0


def _st(value: float, warn: float, fail: Optional[float] = None) -> str:
    return "pass" if value < warn else ("fail" if fail is not None and value >= fail else "warn")


def _rms_db(x: np.ndarray) -> float:
    if x.size == 0:
        return -120.0
    return 20 * math.log10(max(float(np.sqrt(np.mean(x * x))), 1e-6))


def _seam_checks(exe: str, path: Path, info: dict[str, Any], seam_frame: int, nframes: int, th: dict[str, float]) -> list[dict[str, Any]]:
    fps = _fps_value(info)
    checks: list[dict[str, Any]] = []
    first = max(0, seam_frame - WINDOW_FRAMES)
    frames = _decode_window(exe, path, first, min(nframes, seam_frame + WINDOW_FRAMES) - first, fps)
    k = seam_frame - first  # index of the first frame after the cut inside the window
    if frames.shape[0] > 1 and 0 < k < frames.shape[0]:
        f = frames.astype(np.float64)
        luma = (f[..., 0] * 0.299 + f[..., 1] * 0.587 + f[..., 2] * 0.114).mean(axis=(1, 2))
        median = float(np.median(luma))
        black = [first + i for i, v in enumerate(luma) if v < th["black_luma"]] if median >= th["black_context_luma"] else []
        checks.append({"id": "black_frames", "label": "Black frames", "status": "fail" if black else "pass",
                       "expected": f"no frame under luma {th['black_luma']:g}", "actual": black or "none",
                       "threshold": th["black_luma"]})
        diffs = np.abs(np.diff(f, axis=0)).mean(axis=(1, 2, 3))  # diffs[i] = frame i+1 vs i
        same = diffs < th["frozen_diff"]
        run, best = 0, (0, 0)
        for i, s in enumerate(list(same) + [False]):
            if s:
                run += 1
            else:
                if run and run > best[0] and (i - run) <= k + 1 and i >= k - 1:  # touches the seam
                    best = (run, i - run)
                run = 0
        still = float(np.mean(same)) if same.size else 0.0
        frozen = best[0] + 1 >= th["frozen_frames"] and still < 0.6
        checks.append({"id": "frozen_frames", "label": "Frozen frames", "status": "warn" if frozen else "pass",
                       "expected": f"under {int(th['frozen_frames'])} identical frames at the cut",
                       "actual": f"{best[0] + 1} identical frames from frame {first + best[1]}" if best[0] else "none",
                       "threshold": th["frozen_frames"]})
        pre, post = f[max(0, k - 3):k], f[k:k + 3]
        mean_delta = float(np.abs(pre.mean(axis=(0, 1, 2)) - post.mean(axis=(0, 1, 2))).max())

        def hist(a: np.ndarray) -> np.ndarray:
            idx = (a.reshape(-1, 3) // 32).astype(int).clip(0, 7)
            h = np.bincount(idx[:, 0] * 64 + idx[:, 1] * 8 + idx[:, 2], minlength=512).astype(np.float64)
            return h / h.sum()

        hist_d = float(0.5 * np.abs(hist(pre) - hist(post)).sum())
        status = max((_st(mean_delta, th["colour_warn"], th["colour_fail"]), _st(hist_d, th["hist_warn"], th["hist_fail"])),
                     key={"pass": 0, "warn": 1, "fail": 2}.get)
        checks.append({"id": "colour_jump", "label": "Colour jump across the cut", "status": status,
                       "expected": f"mean change < {th['colour_warn']:g}, histogram < {th['hist_warn']:g}",
                       "actual": {"mean_delta": round(mean_delta, 1), "hist_distance": round(hist_d, 3)},
                       "threshold": {"mean": th["colour_warn"], "hist": th["hist_warn"]}})
    if info.get("has_audio"):
        sr = min(int(info.get("sample_rate") or 48000), 48000)
        ts = seam_frame / fps
        x = _decode_audio(exe, path, ts - 0.6, 1.2, sr)
        c = int(round(min(ts, 0.6) * sr))  # sample index of the cut inside the window
        w = int(0.15 * sr)
        before, after = x[max(0, c - w - int(0.01 * sr)):max(0, c - int(0.01 * sr))], x[c + int(0.01 * sr):c + int(0.01 * sr) + w]
        if before.size > 10 and after.size > 10:
            a, b = _rms_db(before), _rms_db(after)
            jump = abs(b - a)
            measured = max(a, b) > th["level_floor_db"]
            checks.append({"id": "audio_level_jump", "label": "Audio level jump", "status": _st(jump, th["level_warn_db"], th["level_fail_db"]) if measured else "pass",
                           "expected": f"< {th['level_warn_db']:g} dB", "actual": f"{jump:.1f} dB ({a:.1f} to {b:.1f} dBFS)" if measured else "silence",
                           "threshold": th["level_warn_db"]})
            steps = np.abs(np.diff(x))
            near = steps[max(0, c - int(0.004 * sr)):c + int(0.004 * sr)]
            rest = np.concatenate([steps[:max(0, c - int(0.02 * sr))], steps[c + int(0.02 * sr):]])
            peak = float(near.max()) if near.size else 0.0
            ref = float(np.percentile(rest, 99.9)) if rest.size else 0.0
            ratio = peak / ref if ref > 1e-6 else (math.inf if peak > th["click_min"] else 0.0)
            click = peak >= th["click_min"] and ratio >= th["click_ratio"]
            checks.append({"id": "audio_click", "label": "Audio click", "status": ("fail" if peak >= th["click_fail"] else "warn") if click else "pass",
                           "expected": f"step < {th['click_ratio']:g}x the rest and < {th['click_min']:g} of full scale",
                           "actual": {"step": round(peak, 3), "ratio": None if math.isinf(ratio) else round(ratio, 1)},
                           "threshold": {"ratio": th["click_ratio"], "min": th["click_min"]}})
    return checks


def _strip(store: Store, asset: dict[str, Any], path: Path, info: dict[str, Any], seam_frame: int, nframes: int, label: str) -> Optional[str]:
    """THUMB consecutive frames around the cut tiled into one PNG: three before it, three after it."""
    exe = _exe()
    fps = _fps_value(info)
    first = max(0, min(seam_frame - THUMB // 2, nframes - THUMB))
    sid = new_id("a")
    out = store.path_for_asset_file(sid, ".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    done = procutil.run([exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{first / fps:.6f}", "-i", str(path),
                         "-an", "-vf", f"scale=176:-2,tile={THUMB}x1:padding=4:margin=4:color=0x111111", "-frames:v", "1", str(out)],
                        text=True, timeout=300)
    if done.returncode != 0 or not out.is_file():
        return None
    from PIL import Image
    with Image.open(out) as im:
        size = im.size
    make_thumbnail(out, store.path_for_thumb(sid))
    store.create_asset(project_id=asset["project_id"], kind="image", file_path=_rel(store, out), mime="image/png",
                       width=size[0], height=size[1], thumb_path=_rel(store, store.path_for_thumb(sid)), source="rendered",
                       asset_id=sid, name=f"Seam strip {label} - {asset.get('name') or asset['id']}"[:100], tags=["seam_strip"],
                       recipe={"operation": "seam_strip", "input_asset_ids": [asset["id"]], "seam_frame": seam_frame,
                               "first_frame": first, "cut_after_tile": seam_frame - first, "created_at": now_iso()})
    return sid


def review_join(store: Store, asset_id: str, seams_s: list[float], thresholds: Optional[dict[str, float]] = None,
                expected_duration_s: Optional[float] = None, expected_frames: Optional[int] = None,
                strips: bool = True) -> dict[str, Any]:
    """Automatic checks at each seam (time in seconds, snapped to a frame) plus a thumbnail strip for a person to look at."""
    exe = _exe()
    asset = store.get_asset(asset_id)
    if asset["kind"] != "video":
        raise EngineError("not_video", f"asset {asset_id} is {asset['kind']}; a join review needs a video")
    th = dict(THRESHOLDS)
    for key, value in (thresholds or {}).items():
        if key not in th:
            raise EngineError("bad_threshold", f"unknown threshold '{key}'; known: {', '.join(th)}")
        th[key] = float(value)
    path = store.data_dir / asset["file_path"]
    info = _probe(path)
    fps = _fps_value(info)
    nframes = analyze_video(path)["frames"] if expected_frames is not None else max(1, int(round(float(info["duration_s"]) * fps)))
    seams = []
    for t in seams_s[:24]:
        frame = int(round(float(t) * fps))
        if not 0 < frame < nframes:
            raise EngineError("bad_repair", f"seam at {t} s is not inside the video")
        checks = _seam_checks(exe, path, info, frame, nframes, th)
        status = max((c["status"] for c in checks), key={"pass": 0, "warn": 1, "fail": 2}.get, default="pass")
        row = {"t_s": round(frame / fps, 4), "frame": frame, "status": status, "checks": checks}
        if strips:
            row["strip_asset_id"] = _strip(store, asset, path, info, frame, nframes, f"{frame / fps:.2f}s")
            row["strip_note"] = f"{THUMB} consecutive frames; the cut falls between tiles {THUMB // 2} and {THUMB // 2 + 1}"
        seams.append(row)
    overall: list[dict[str, Any]] = []
    if expected_duration_s is not None:
        dur = float(info["duration_s"])
        off = abs(dur - float(expected_duration_s))
        overall.append({"id": "duration_preserved", "label": "Duration preserved", "status": "pass" if off <= th["duration_tol_s"] + 1 / fps else "fail",
                        "expected": f"{float(expected_duration_s):.3f} s", "actual": f"{dur:.3f} s", "threshold": th["duration_tol_s"]})
    if expected_frames is not None:
        overall.append({"id": "frames_preserved", "label": "Frame count preserved", "status": "pass" if nframes == int(expected_frames) else "fail",
                        "expected": int(expected_frames), "actual": nframes, "threshold": 0})
    rank = {"pass": 0, "warn": 1, "fail": 2}
    worst = max([s["status"] for s in seams] + [c["status"] for c in overall] or ["pass"], key=rank.get)
    return {"asset_id": asset_id, "ok": worst != "fail", "status": worst,
            "warnings": sum(1 for s in seams for c in s["checks"] if c["status"] == "warn")
                        + sum(1 for c in overall if c["status"] == "warn"),
            "failures": sum(1 for s in seams for c in s["checks"] if c["status"] == "fail") + sum(1 for c in overall if c["status"] == "fail"),
            "seams": seams, "overall": overall, "thresholds": th, "reviewed_at": now_iso(),
            "note": "Automatic heuristics. A pass does not approve the edit: look at the strip."}
