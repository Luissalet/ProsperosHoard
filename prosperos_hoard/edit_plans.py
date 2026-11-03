"""Edit plans: plan -> run -> verify.

A declarative edit (clips with trims and transitions, captions, image overlays, a music bed with ducking and a
loudness target) is compiled into the exact ffmpeg commands that will make it, and the commands are saved as a
plan in ``data/edit_plans/<plan_id>/plan.json`` together with a fingerprint of every input (size, modification
time and a hash of the first MiB). Running a plan refuses when an input changed since it was planned, so what
runs is what was reviewed. ``verify`` checks a rendered file against a platform's delivery spec (duration,
resolution, frame rate, audio layout, EBU R128 loudness through ffmpeg's ``ebur128``) and ``contact_sheet``
tiles every Nth frame into one PNG.

The per-clip, join and mux commands come from ``video.py`` (the same builders the timeline renderer uses);
only the audio mix and the image overlays are composed here. Pure logic: no FastAPI. The pattern (plan with
fingerprints, run, verify against platform specs, contact sheet) follows kajisho5/ffmpeg-skill (MIT).
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import procutil
from . import video as video_mod
from .backend import ffmpeg_path
from .engine import EngineError, _rel, _video_thumbnail, make_thumbnail
from .hoard_link.media.ffmpeg import FFmpeg, FFmpegError
from .ids import new_id
from .store import AssetInUse, NotFound, Store
from .util import now_iso, write_text_atomic

FORMAT = "prospero.edit_plan/1"
HEAD_BYTES = 1 << 20  # the fingerprint hashes the first MiB: cheap on long clips, still catches a re-render

# Delivery specs. Durations and sizes follow each platform's upload help as published in 2026; loudness is the
# -14 LUFS integrated / -1 dBTP true peak the platforms normalise to. Edit here when a platform changes its rules.
PLATFORMS: dict[str, dict[str, Any]] = {
    "shorts": {"label": "YouTube Shorts", "width": 1080, "height": 1920, "fps": (24, 60), "max_s": 180.0,
               "lufs": -14.0, "true_peak": -1.0},
    "reels": {"label": "Instagram Reels", "width": 1080, "height": 1920, "fps": (23, 60), "max_s": 180.0,
              "lufs": -14.0, "true_peak": -1.0},
    "tiktok": {"label": "TikTok", "width": 1080, "height": 1920, "fps": (24, 60), "max_s": 600.0,
               "lufs": -14.0, "true_peak": -1.0},
    "youtube": {"label": "YouTube", "width": 1920, "height": 1080, "fps": (24, 60), "max_s": 43200.0,
                "lufs": -14.0, "true_peak": -1.0},
}
XFADE_TYPES = tuple(sorted(video_mod.XFADE_NATIVE))
TRANSITIONS = ("cut", *video_mod.TRANSITION_MAP, *XFADE_TYPES)
QUALITY = {"final": ("medium", 19, "192k"), "preview": ("veryfast", 26, "128k")}


def plans_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "edit_plans"


def plan_dir(data_dir: Path, plan_id: str) -> Path:
    if not plan_id.startswith("ep_") or not plan_id.replace("_", "").isalnum():
        raise EngineError("bad_plan", f"'{plan_id}' is not an edit plan id")
    return plans_dir(data_dir) / plan_id


def fingerprint(path: Path) -> dict[str, Any]:
    """size + modification time (ns) + sha256 of the first MiB."""
    st = path.stat()
    with path.open("rb") as fh:
        head = hashlib.sha256(fh.read(HEAD_BYTES)).hexdigest()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "head_sha256": head}


def _num(value: Any, where: str, lo: float, hi: float, default: Optional[float] = None) -> float:
    if value is None or value == "":
        if default is None:
            raise EngineError("bad_edit", f"{where} is required")
        return float(default)
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise EngineError("bad_edit", f"{where} must be a number") from None
    if not math.isfinite(out) or not lo <= out <= hi:
        raise EngineError("bad_edit", f"{where} must be between {lo:g} and {hi:g}")
    return out


def _until_end(value: Any, total: float) -> Any:
    """An end time past the edit's end means 'until the end' (the page's default overlay lasts 5 s on any edit)."""
    try:
        return total if math.isfinite(float(value)) and float(value) > total else value
    except (TypeError, ValueError):
        return value


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _probe(path: Path) -> dict[str, Any]:
    try:
        return FFmpeg().summarize(FFmpeg().probe(path))
    except FFmpegError as exc:
        raise EngineError("bad_media", f"{path.name}: {exc}") from None


def normalise(store: Store, spec: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Check an edit and fill its defaults. Returns (edit, inputs by asset id with path, kind and probe)."""
    if not isinstance(spec, dict):
        raise EngineError("bad_edit", "the edit must be an object with clips")
    inputs: dict[str, dict[str, Any]] = {}

    def use(asset_id: Any, kinds: tuple[str, ...], where: str) -> dict[str, Any]:
        aid = str(asset_id or "")
        if not aid:
            raise EngineError("bad_edit", f"{where} needs an asset_id")
        if aid not in inputs:
            asset = store.get_asset(aid)
            path = store.data_dir / asset["file_path"]
            if not path.is_file():
                raise EngineError("missing_file", f"asset {aid} has no file on disk")
            info = _probe(path) if asset["kind"] in ("video", "audio") else {"kind": "image", "has_audio": False}
            inputs[aid] = {"asset": asset, "path": path, "kind": asset["kind"], "probe": info}
        if inputs[aid]["kind"] not in kinds:
            raise EngineError("bad_edit", f"{where}: asset {aid} is {inputs[aid]['kind']}, expected {' or '.join(kinds)}")
        return inputs[aid]

    platform = spec.get("platform") or None
    if platform is not None and platform not in PLATFORMS:
        raise EngineError("bad_edit", f"platform must be one of {', '.join(PLATFORMS)}")
    target = PLATFORMS.get(platform or "", {})
    width = _even(_num(spec.get("width"), "width", 64, 4096, target.get("width", 1080)))
    height = _even(_num(spec.get("height"), "height", 64, 4096, target.get("height", 1920)))
    fps_in = _num(spec.get("fps"), "fps", 1, 120, 30)
    if abs(fps_in - round(fps_in)) > 1e-6:  # int() would quietly render 29.97 as 29 fps
        raise EngineError("bad_edit", f"fps must be a whole number (24, 25, 30, 50, 60), not {fps_in:g}")
    fps = int(round(fps_in))
    framing = spec.get("framing") or "fill"
    if framing not in video_mod.FRAMINGS:
        raise EngineError("bad_edit", f"framing must be one of {', '.join(video_mod.FRAMINGS)}")
    quality = spec.get("quality") or "final"
    if quality not in QUALITY:
        raise EngineError("bad_edit", "quality is 'final' or 'preview'")

    raw_clips = spec.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise EngineError("bad_edit", "the edit needs at least one clip")
    if len(raw_clips) > 80:
        raise EngineError("bad_edit", "at most 80 clips per edit")
    clips: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_clips):
        where = f"clips[{i}]"
        if not isinstance(raw, dict):
            raise EngineError("bad_edit", f"{where} must be an object")
        src = use(raw.get("asset_id"), ("video", "image"), where)
        clip: dict[str, Any] = {"asset_id": src["asset"]["id"], "kind": src["kind"]}
        if src["kind"] == "video":
            length = float(src["probe"].get("duration_s") or src["asset"].get("duration_s") or 0)
            clip["in_s"] = round(_num(raw.get("in_s"), f"{where}.in_s", 0, max(0.0, length - 0.04), 0), 3)
            clip["out_s"] = round(_num(raw.get("out_s"), f"{where}.out_s", clip["in_s"] + 0.04, length + 0.001, length), 3)
            clip["duration_s"] = round(clip["out_s"] - clip["in_s"], 3)
            clip["source_s"] = round(length, 3)
            clip["has_audio"] = bool(src["probe"].get("has_audio"))
            clip["audio"] = bool(raw.get("audio", True)) and clip["has_audio"]
            clip["volume_db"] = _num(raw.get("volume_db"), f"{where}.volume_db", -60, 12, 0)
        else:
            clip["duration_s"] = round(_num(raw.get("duration_s"), f"{where}.duration_s", 0.1, 600, 3), 3)
            clip["audio"] = False
        trans = raw.get("transition") or {}
        if isinstance(trans, str):
            trans = {"type": trans}
        kind = trans.get("type") or "cut"
        if kind not in TRANSITIONS:
            raise EngineError("bad_edit", f"{where}.transition must be one of {', '.join(TRANSITIONS)}")
        clip["transition"] = {"type": kind, "duration_s": _num(trans.get("duration_s"), f"{where}.transition.duration_s",
                                                               0.05, 3, 0.4) if kind != "cut" else 0.0}
        if i == 0:
            clip["transition"] = {"type": "cut", "duration_s": 0.0}
        elif clip["transition"]["duration_s"] > clip["duration_s"]:
            # the incoming clip has to last through its transition: a longer one swallows the clip and the next xfade
            raise EngineError("bad_edit", f"{where}.transition.duration_s ({clip['transition']['duration_s']:g} s) is longer "
                                          f"than the clip it enters ({clip['duration_s']:g} s)")
        clips.append(clip)
    total = round(sum(c["duration_s"] for c in clips), 3)

    captions = []
    for i, raw in enumerate(spec.get("captions") or []):
        where = f"captions[{i}]"
        text = str((raw or {}).get("text") or "").strip()[:300]
        if not text:
            continue
        start = _num(raw.get("start_s"), f"{where}.start_s", 0, total, 0)
        end = _num(_until_end(raw.get("end_s"), total), f"{where}.end_s", start + 0.1, total + 0.001, min(total, start + 2.5))
        captions.append({"text": text, "start_s": round(start, 3), "end_s": round(end, 3)})
    caption_style = spec.get("caption_style") or "default"
    if caption_style not in video_mod.LYRIC_STYLES:
        raise EngineError("bad_edit", f"caption_style must be one of {', '.join(video_mod.LYRIC_STYLES)}")
    overlays = []
    for i, raw in enumerate(spec.get("overlays") or []):
        where = f"overlays[{i}]"
        src = use((raw or {}).get("asset_id"), ("image",), where)
        start = _num(raw.get("start_s"), f"{where}.start_s", 0, total, 0)
        overlays.append({"asset_id": src["asset"]["id"], "start_s": round(start, 3),
                         "end_s": round(_num(_until_end(raw.get("end_s"), total), f"{where}.end_s", start + 0.04, total + 0.001,
                                             total), 3),
                         "x": _num(raw.get("x"), f"{where}.x", 0, 1, 0.95), "y": _num(raw.get("y"), f"{where}.y", 0, 1, 0.05),
                         "scale": _num(raw.get("scale"), f"{where}.scale", 0.02, 1, 0.2),
                         "opacity": _num(raw.get("opacity"), f"{where}.opacity", 0, 1, 1)})
    music = None
    if spec.get("music"):
        raw = spec["music"] if isinstance(spec["music"], dict) else {"asset_id": spec["music"]}
        src = use(raw.get("asset_id"), ("audio", "video"), "music")
        if not src["probe"].get("has_audio"):
            raise EngineError("bad_edit", f"music: asset {src['asset']['id']} has no audio stream")
        music = {"asset_id": src["asset"]["id"], "volume_db": _num(raw.get("volume_db"), "music.volume_db", -60, 12, -8),
                 "duck": bool(raw.get("duck", True)), "start_s": _num(raw.get("start_s"), "music.start_s", 0, 36000, 0),
                 "fade_out_s": _num(raw.get("fade_out_s"), "music.fade_out_s", 0, 10, 1.5),
                 "source_s": float(src["probe"].get("duration_s") or 0)}
    loud = spec.get("loudness") or {}
    loudness = {"target_lufs": _num(loud.get("target_lufs"), "loudness.target_lufs", -36, -5, target.get("lufs", -14.0)),
                "true_peak": _num(loud.get("true_peak"), "loudness.true_peak", -9, 0, target.get("true_peak", -1.0))}
    edit = {"platform": platform, "width": width, "height": height, "fps": fps, "framing": framing, "quality": quality,
            "clips": clips, "captions": captions, "caption_style": caption_style, "overlays": overlays, "music": music,
            "loudness": loudness, "duration_s": total}
    return edit, inputs


FF = "@ffmpeg@"
OUT = "@out@"


def _in(asset_id: str) -> str:
    return f"@in_{asset_id}@"


def compile_steps(edit: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The ffmpeg steps for an edit, run in the plan folder. Input files appear as ``@in_<asset id>@``, ffmpeg as
    ``@ffmpeg@`` and the result as ``@out@``; everything else is a file name inside the plan folder. Returns
    (steps, text files the steps read - concat list, captions)."""
    W, H, fps, clips = edit["width"], edit["height"], edit["fps"], edit["clips"]
    total = edit["duration_s"]
    files: dict[str, str] = {}
    steps: list[dict[str, Any]] = []
    transitions = [{"type": c["transition"]["type"], "duration_s": c["transition"]["duration_s"]} for c in clips]
    all_cuts = all(t["type"] == "cut" for t in transitions)
    starts, acc = [], 0.0
    for c in clips:  # the frame grid of render_timeline: every clip starts on a frame, overlaps add whole frames
        starts.append(int(round(acc * fps)))
        acc += c["duration_s"]
    starts.append(int(round(acc * fps)))
    frames = [max(1, starts[i + 1] - starts[i]) for i in range(len(clips))]
    overlap = [max(1, int(round(video_mod.transition_duration(t, fps) * fps))) for t in transitions]
    grid_durations = [n / fps for n in frames]
    grid_transitions = [dict(t, duration_s=overlap[i] / fps) if t["type"] != "cut" else t for i, t in enumerate(transitions)]
    names = []
    for i, c in enumerate(clips):
        n = frames[i] + (overlap[i + 1] + 1 if not all_cuts and i + 1 < len(clips) else 0)
        name = f"clip_{i:03d}.mp4"
        if c["kind"] == "video":
            argv = video_mod.build_video_clip_cmd(FF, Path(_in(c["asset_id"])), Path(name), W, H, fps, n / fps, c["in_s"],
                                                  framing=edit["framing"], source_duration_s=c["source_s"], source_fit="hold")
        else:
            argv = video_mod.build_image_clip_cmd(FF, Path(_in(c["asset_id"])), Path(name), W, H, fps, n / fps,
                                                  framing=edit["framing"])
        steps.append({"name": f"clip {i + 1}", "argv": [str(a) for a in argv], "output": name})
        names.append(Path(name))
    if len(names) == 1:
        joined = names[0].name
    elif all_cuts:
        files["concat_list.txt"] = video_mod.concat_list_text(names)
        steps.append({"name": "join", "argv": [str(a) for a in video_mod.build_concat_cmd(FF, Path("concat_list.txt"), Path("joined.mp4"))],
                      "output": "joined.mp4"})
        joined = "joined.mp4"
    else:
        argv = video_mod.build_xfade_cmd(FF, names, grid_durations, grid_transitions, Path("joined.mp4"), fps=fps)
        steps.append({"name": "join with transitions", "argv": [str(a) for a in argv], "output": "joined.mp4"})
        joined = "joined.mp4"
    picture = joined
    if edit["overlays"]:
        argv = [FF, "-y", "-nostdin", "-loglevel", "error", "-i", joined]
        graph, base = [], "0:v"
        for k, ov in enumerate(edit["overlays"], start=1):
            argv += ["-loop", "1", "-t", f"{total + 1:.3f}", "-i", _in(ov["asset_id"])]
            graph.append(f"[{k}:v]format=rgba,scale={_even(W * ov['scale'])}:-2,colorchannelmixer=aa={ov['opacity']:.3f}[o{k}]")
            graph.append(f"[{base}][o{k}]overlay=x='(W-w)*{ov['x']:.4f}':y='(H-h)*{ov['y']:.4f}'"
                         f":enable='between(t,{ov['start_s']:.3f},{ov['end_s']:.3f})':eof_action=pass[b{k}]")
            base = f"b{k}"
        argv += ["-filter_complex", ";".join(graph), "-map", f"[{base}]", "-t", f"{total:.3f}", "-r", str(fps),
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16", "-pix_fmt", "yuv420p", "picture.mp4"]
        steps.append({"name": "overlays", "argv": argv, "output": "picture.mp4"})
        picture = "picture.mp4"
    steps.append(_audio_step(edit, starts, frames, overlap, all_cuts))
    ass = None
    if edit["captions"]:
        ass = "captions.ass"
        files[ass] = video_mod.build_ass(W, H, edit["captions"], style=edit["caption_style"])
    preset, crf, bitrate = QUALITY[edit["quality"]]
    argv = video_mod.build_mux_cmd(FF, Path(picture), Path("mix.wav"), ass, Path(OUT), preset, crf, bitrate)
    steps.append({"name": "encode with audio" + (" and captions" if ass else ""), "argv": [str(a) for a in argv],
                  "output": OUT, "fonts": bool(ass)})
    return steps, files


def _audio_step(edit: dict[str, Any], starts: list[int], frames: list[int], overlap: list[int], all_cuts: bool) -> dict[str, Any]:
    """The soundtrack as mix.wav: each clip's own sound at its place on the cut (faded through its transitions),
    the music bed under it (ducked by the sound when asked), then loudnorm to the target."""
    fps, total, clips = edit["fps"], edit["duration_s"], edit["clips"]
    argv: list[str] = [FF, "-y", "-nostdin", "-loglevel", "error"]
    graph: list[str] = []
    voices: list[str] = []
    k = 0
    norm = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    for i, c in enumerate(clips):
        if not c.get("audio"):
            continue
        tail = overlap[i + 1] / fps if not all_cuts and i + 1 < len(clips) else 0.0
        dur = frames[i] / fps + tail
        argv += ["-ss", f"{c['in_s']:.3f}", "-t", f"{dur:.3f}", "-i", _in(c["asset_id"])]
        chain = f"[{k}:a]{norm},atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,volume={c['volume_db']:.2f}dB"
        if c["transition"]["type"] != "cut":
            chain += f",afade=t=in:d={overlap[i] / fps:.3f}"
        if tail:
            chain += f",afade=t=out:st={max(0.0, dur - tail):.3f}:d={tail:.3f}"
        ms = int(round(starts[i] / fps * 1000))
        graph.append(f"{chain},adelay={ms}|{ms}[c{k}]")
        voices.append(f"c{k}")
        k += 1
    pad = f"apad,atrim=0:{total:.3f}"
    if len(voices) > 1:
        graph.append("".join(f"[{v}]" for v in voices) + f"amix=inputs={len(voices)}:duration=longest:normalize=0,{pad}[voice]")
    elif voices:
        graph.append(f"[{voices[0]}]{pad}[voice]")
    else:
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{total:.3f}[voice]")
    mix = "voice"
    music = edit.get("music")
    if music:
        argv += ["-stream_loop", "-1", "-ss", f"{music['start_s']:.3f}", "-i", _in(music["asset_id"])]
        chain = f"[{k}:a]{norm},atrim=0:{total:.3f},asetpts=PTS-STARTPTS,volume={music['volume_db']:.2f}dB"
        if music["fade_out_s"] > 0:
            chain += f",afade=t=out:st={max(0.0, total - music['fade_out_s']):.3f}:d={music['fade_out_s']:.3f}"
        graph.append(chain + "[mus]")
        if music["duck"] and voices:
            graph.append("[voice]asplit=2[vk][vm]")
            graph.append("[mus][vk]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=350[md]")
            graph.append("[vm][md]amix=inputs=2:duration=first:normalize=0[mix]")
        else:
            graph.append("[voice][mus]amix=inputs=2:duration=first:normalize=0[mix]")
        mix = "mix"
    loud = edit["loudness"]
    if voices or music:
        graph.append(f"[{mix}]loudnorm=I={loud['target_lufs']:.1f}:TP={loud['true_peak']:.1f}:LRA=11,"
                     "aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo[aout]")
    else:
        graph.append(f"[{mix}]aformat=sample_fmts=s16:channel_layouts=stereo[aout]")
    argv += ["-filter_complex", ";".join(graph), "-map", "[aout]", "-t", f"{total:.3f}", "-c:a", "pcm_s16le", "mix.wav"]
    return {"name": "audio mix" + (" with music" if music else "") + (" and loudness" if voices or music else ""),
            "argv": argv, "output": "mix.wav"}


# ------------------------------------------------------------------ plans

def _save(data_dir: Path, plan: dict[str, Any]) -> None:
    plan["updated_at"] = now_iso()
    write_text_atomic(plan_dir(data_dir, plan["id"]) / "plan.json", json.dumps(plan, ensure_ascii=False, indent=1))


def load(data_dir: Path, plan_id: str) -> dict[str, Any]:
    path = plan_dir(data_dir, plan_id) / "plan.json"
    if not path.is_file():
        raise EngineError("not_found", f"edit plan {plan_id} not found")
    return json.loads(path.read_text(encoding="utf-8"))


def create(store: Store, project_id: str, spec: dict[str, Any], name: Optional[str] = None) -> dict[str, Any]:
    """Validate the edit, compile its ffmpeg steps and save them as a plan with the fingerprints of its inputs."""
    store.get_project(project_id)
    edit, inputs = normalise(store, spec)
    steps, files = compile_steps(edit)
    plan_id = new_id("ep")
    folder = plan_dir(store.data_dir, plan_id)
    folder.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        (folder / fname).write_text(text, encoding="utf-8")
    plan = {"format": FORMAT, "id": plan_id, "project_id": project_id, "created_at": now_iso(),
            "name": (name or spec.get("name") or f"Edit {len(edit['clips'])} clips")[:120], "status": "planned",
            "spec": spec, "edit": edit, "steps": steps, "files": sorted(files), "runs": [],
            "inputs": [{"asset_id": aid, "kind": info["kind"], "name": info["asset"].get("name") or aid,
                        "fingerprint": fingerprint(info["path"])} for aid, info in inputs.items()]}
    _save(store.data_dir, plan)
    return plan


def list_plans(data_dir: Path, project_id: Optional[str] = None, limit: int = 30) -> list[dict[str, Any]]:
    out = []
    for path in sorted(plans_dir(data_dir).glob("ep_*/plan.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if project_id and plan.get("project_id") != project_id:
            continue
        out.append(view(plan))
        if len(out) >= limit:
            break
    return out


ACTIVE_STATES = ("queued", "waiting_gpu", "running")


def active_run(store: Store, plan_id: str) -> Optional[str]:
    """The id of a queued or running `edit_plan_run` job of this plan, if any."""
    rows = store.conn.execute("SELECT id, params_json FROM jobs WHERE type='edit_plan_run' AND state IN (?, ?, ?)",
                              ACTIVE_STATES).fetchall()
    for row in rows:
        try:
            params = json.loads(row["params_json"] or "{}")
        except ValueError:
            continue
        if params.get("plan_id") == plan_id:
            return row["id"]
    return None


def output_assets(store: Store, plan: dict[str, Any]) -> list[str]:
    """The assets the plan's runs made: every rendered video whose recipe names the plan, and the contact sheets made of
    those videos. Never one of the plan's inputs."""
    inputs = {i["asset_id"] for i in plan.get("inputs") or []}
    renders: list[str] = []
    for row in store.conn.execute("SELECT id, recipe_json FROM assets WHERE source='rendered' AND recipe_json LIKE ?",
                                  (f"%{plan['id']}%",)).fetchall():
        recipe = json.loads(row["recipe_json"] or "{}")
        if recipe.get("operation") == "edit_plan" and recipe.get("plan_id") == plan["id"]:
            renders.append(row["id"])
    renders = [a for a in dict.fromkeys(renders) if a not in inputs]
    sheets: list[str] = []
    for aid in renders:
        for row in store.conn.execute("SELECT id, recipe_json FROM assets WHERE source='rendered' AND recipe_json LIKE ?",
                                      (f"%{aid}%",)).fetchall():
            recipe = json.loads(row["recipe_json"] or "{}")
            if recipe.get("operation") == "contact_sheet" and aid in (recipe.get("input_asset_ids") or []):
                sheets.append(row["id"])
    return [a for a in dict.fromkeys(renders + sheets) if a not in inputs]


def delete(store: Store, plan_id: str, outputs: bool = True) -> dict[str, Any]:
    """Delete a plan: its folder under data/edit_plans (plan.json, captions, concat list and any leftover intermediate)
    and, with `outputs`, move the videos and contact sheets its runs made to the trash (restorable). The plan's input
    assets are never touched. Refuses while a run of the plan is queued or rendering."""
    plan = load(store.data_dir, plan_id)
    job = active_run(store, plan_id)
    if job:
        raise EngineError("plan_running", f"edit plan {plan_id} is rendering (job {job}): cancel the job or wait for it to "
                                          "finish before deleting the plan")
    root = plans_dir(store.data_dir).resolve()
    folder = plan_dir(store.data_dir, plan_id)
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise EngineError("bad_plan", f"edit plan {plan_id} is not a folder of the plans directory")
    trashed: list[str] = []
    kept: list[dict[str, Any]] = []
    if outputs:
        for aid in output_assets(store, plan):
            try:
                store.trash_asset(aid)
                trashed.append(aid)
            except AssetInUse as exc:
                kept.append({"asset_id": aid, "error": "asset_in_use", "message": str(exc)})
            except NotFound:
                continue  # already deleted
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    shutil.rmtree(folder)
    out: dict[str, Any] = {"deleted": plan_id, "name": plan.get("name"), "project_id": plan.get("project_id"),
                           "files_removed": files, "outputs_trashed": trashed}
    if kept:
        out["outputs_kept"] = kept
    if trashed:
        out["note"] = "the rendered videos and contact sheets are in the trash: restore them with studio_trash(action='restore')"
    return out


def _shown(arg: str, names: dict[str, str]) -> str:
    for aid, label in names.items():
        arg = arg.replace(_in(aid), f"<{label}>")
    arg = arg.replace(FF, "ffmpeg").replace(OUT, "<result>.mp4")
    return f'"{arg}"' if (" " in arg or ";" in arg) else arg


def view(plan: dict[str, Any], full: bool = False) -> dict[str, Any]:
    """Ids first, no file paths. `full` adds the commands as text (inputs shown by name) for the person to read."""
    edit = plan["edit"]
    out = {"plan_id": plan["id"], "name": plan.get("name"), "project_id": plan.get("project_id"), "status": plan.get("status"),
           "platform": edit.get("platform"), "width": edit["width"], "height": edit["height"], "fps": edit["fps"],
           "duration_s": edit["duration_s"], "clips": len(edit["clips"]), "captions": len(edit["captions"]),
           "overlays": len(edit["overlays"]), "music": bool(edit.get("music")), "loudness": edit["loudness"],
           "steps": [s["name"] for s in plan["steps"]], "inputs": [{k: i[k] for k in ("asset_id", "kind", "name")} for i in plan["inputs"]],
           "created_at": plan.get("created_at")}
    runs = plan.get("runs") or []
    if runs:
        last = runs[-1]
        out["last_run"] = {k: last.get(k) for k in ("at", "asset_id", "elapsed_s", "verify_ok") if last.get(k) is not None}
    if plan.get("last_verify"):
        out["last_verify"] = plan["last_verify"]
    if full:
        names = {i["asset_id"]: i["name"] for i in plan["inputs"]}
        out["commands"] = [" ".join(_shown(a, names) for a in s["argv"]) for s in plan["steps"]]
        out["edit"] = edit
    return out


def check_inputs(store: Store, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The inputs that are not what the plan saw: deleted, or a different size, time or first MiB."""
    changed = []
    for item in plan["inputs"]:
        try:
            asset = store.get_asset(item["asset_id"])
            now = fingerprint(store.data_dir / asset["file_path"])
        except Exception:  # noqa: BLE001 - a deleted asset or file is a change too
            changed.append({"asset_id": item["asset_id"], "name": item["name"], "what": ["missing"]})
            continue
        diff = [k for k in ("size", "mtime_ns", "head_sha256") if now.get(k) != item["fingerprint"].get(k)]
        if diff:
            changed.append({"asset_id": item["asset_id"], "name": item["name"], "what": diff})
    return changed


def run_job(store: Store, job: dict[str, Any], progress: Callable[..., None]) -> dict[str, Any]:
    """Run a saved plan (an `edit_plan_run` job on the cpu lane): refuse when an input changed, run every step in the
    plan folder, register the result as a video asset and verify it against the plan's platform."""
    plan_id = job["params"]["plan_id"]
    plan = load(store.data_dir, plan_id)
    changed = check_inputs(store, plan)
    if changed:
        listed = ", ".join(f"{c['name']} ({'/'.join(c['what'])})" for c in changed[:6])
        raise EngineError("inputs_changed", f"the plan's inputs changed since it was planned: {listed}. Plan again to "
                                            "review the new commands.")
    exe = ffmpeg_path()
    if not exe:
        raise EngineError("no_ffmpeg", "ffmpeg is needed to run an edit plan")
    folder = plan_dir(store.data_dir, plan_id)
    paths = {item["asset_id"]: str((store.data_dir / store.get_asset(item["asset_id"])["file_path"]).resolve())
             for item in plan["inputs"]}
    out_id = new_id("a")
    out_path = store.path_for_asset_file(out_id, ".mp4").resolve()
    started = time.monotonic()
    n = len(plan["steps"])
    try:
        for i, step in enumerate(plan["steps"]):
            progress(0.02 + 0.9 * i / n, step["name"])
            if step.get("fonts"):
                (folder / "fonts").mkdir(exist_ok=True)
                for ttf in video_mod.FONTS_DIR.glob("*/*.ttf"):
                    shutil.copyfile(ttf, folder / "fonts" / ttf.name)
            argv = []
            for arg in step["argv"]:
                arg = arg.replace(FF, exe).replace(OUT, str(out_path))
                for aid, path in paths.items():
                    arg = arg.replace(_in(aid), path)
                argv.append(arg)
            done = procutil.run(argv, text=True, cwd=str(folder), timeout=7200)
            if done.returncode != 0:
                tail = " ".join((done.stderr or "").strip().splitlines()[-3:])[:500]
                raise EngineError("edit_step_failed", f"{step['name']}: {tail or f'ffmpeg exit {done.returncode}'}")
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    finally:
        for leftover in [*folder.glob("clip_*.mp4"), folder / "joined.mp4", folder / "picture.mp4", folder / "mix.wav"]:
            leftover.unlink(missing_ok=True)
        shutil.rmtree(folder / "fonts", ignore_errors=True)
    progress(0.93, "verifying")
    edit = plan["edit"]
    info = _probe(out_path)
    try:
        thumb = _video_thumbnail(out_path, store.path_for_thumb(out_id))
    except Exception:  # noqa: BLE001 - a missing thumbnail never fails the render
        thumb = None
    recipe = {"operation": "edit_plan", "plan_id": plan_id, "platform": edit.get("platform"),
              "input_asset_ids": [i["asset_id"] for i in plan["inputs"]][:50],
              "elapsed_s": round(time.monotonic() - started, 2), "created_at": now_iso()}
    asset = store.create_asset(project_id=plan["project_id"], kind="video", file_path=_rel(store, out_path), mime="video/mp4",
                               width=info["width"], height=info["height"], duration_s=info["duration_s"], thumb_path=thumb,
                               source="rendered", recipe=recipe, asset_id=out_id, name=plan["name"][:100],
                               tags=["edit_plan"] + ([edit["platform"]] if edit.get("platform") else []))
    report = verify(store, asset["id"], edit.get("platform") or _platform_for(edit), sheet=True)
    plan = load(store.data_dir, plan_id)
    plan["status"] = "rendered"
    plan["last_verify"] = report
    plan.setdefault("runs", []).append({"at": now_iso(), "asset_id": asset["id"], "job_id": job.get("id"),
                                        "elapsed_s": recipe["elapsed_s"], "verify_ok": report["ok"]})
    _save(store.data_dir, plan)
    progress(1.0, "done")
    ids = [asset["id"]] + ([report["contact_sheet_asset_id"]] if report.get("contact_sheet_asset_id") else [])
    return {"asset_id": asset["id"], "asset_ids": ids, "plan_id": plan_id, "verify_ok": report["ok"],
            "failed_checks": [c["id"] for c in report["checks"] if c["status"] == "fail"]}


def _platform_for(edit: dict[str, Any]) -> str:
    return "youtube" if edit["width"] > edit["height"] else "shorts"


# ------------------------------------------------------------------ verify

def _check(checks: list, cid: str, label: str, status: str, expected: Any, actual: Any) -> None:
    checks.append({"id": cid, "label": label, "status": status, "expected": expected, "actual": actual})


def check_media(info: dict[str, Any], loud: Optional[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    """A probed file (and its EBU R128 measurement) against a platform's delivery spec: pass / warn / fail per check."""
    spec = PLATFORMS[platform]
    checks: list[dict[str, Any]] = []
    w, h, tw, th = info.get("width") or 0, info.get("height") or 0, spec["width"], spec["height"]
    if not info.get("has_video"):
        _check(checks, "video", "Video stream", "fail", "present", "none")
    else:
        same_shape = h and abs(w / h - tw / th) < 0.01
        status = "pass" if (w, h) == (tw, th) else ("warn" if same_shape and min(w, h) >= 720 else "fail")
        _check(checks, "resolution", "Resolution", status, f"{tw}x{th}", f"{w}x{h}")
        fps, (lo, hi) = float(info.get("fps") or 0), spec["fps"]
        # the NTSC rates (23.976, 29.97, 59.94) are the whole rates x 1000/1001 and every platform takes them
        _check(checks, "fps", "Frame rate", "pass" if lo * 1000 / 1001 - 0.01 <= fps <= hi + 0.01 else "fail", f"{lo}-{hi}",
               round(fps, 3))
        codec = info.get("video_codec")
        _check(checks, "video_codec", "Video codec", "pass" if codec in ("h264", "hevc") else "warn", "h264", codec)
    dur = float(info.get("duration_s") or 0)
    _check(checks, "duration", "Duration", "pass" if 0 < dur <= spec["max_s"] else "fail", f"<= {spec['max_s']:g} s", round(dur, 2))
    if not info.get("has_audio"):
        _check(checks, "audio", "Audio stream", "fail", "stereo AAC", "none")
        return checks
    ch = int(info.get("channels") or 0)
    _check(checks, "audio_layout", "Audio channels", "pass" if ch == 2 else "warn", "2 (stereo)", ch)
    sr = int(info.get("sample_rate") or 0)
    _check(checks, "sample_rate", "Sample rate", "pass" if sr in (44100, 48000) else "warn", "48000", sr)
    _check(checks, "audio_codec", "Audio codec", "pass" if info.get("audio_codec") == "aac" else "warn", "aac",
           info.get("audio_codec"))
    lufs = (loud or {}).get("integrated_lufs")
    if lufs is None:
        _check(checks, "loudness", "Integrated loudness", "warn", f"{spec['lufs']:g} LUFS", "silent or not measured")
    else:
        off = abs(lufs - spec["lufs"])
        _check(checks, "loudness", "Integrated loudness", "pass" if off <= 1.5 else ("warn" if off <= 3 else "fail"),
               f"{spec['lufs']:g} LUFS", round(lufs, 1))
    tp = (loud or {}).get("true_peak_dbfs")
    if tp is not None:
        _check(checks, "true_peak", "True peak", "pass" if tp <= spec["true_peak"] + 0.5 else
               ("warn" if tp <= 0 else "fail"), f"<= {spec['true_peak']:g} dBTP", round(tp, 1))
    return checks


def verify(store: Store, asset_id: str, platform: str = "shorts", sheet: bool = True, every_n: Optional[int] = None,
           count: int = 12) -> dict[str, Any]:
    """Probe + EBU R128 (ffmpeg ebur128) of a video asset checked against `platform`, plus a contact sheet."""
    if platform not in PLATFORMS:
        raise EngineError("bad_platform", f"platform must be one of {', '.join(PLATFORMS)}")
    asset = store.get_asset(asset_id)
    if asset["kind"] != "video":
        raise EngineError("not_video", f"asset {asset_id} is {asset['kind']}; verify checks a rendered video")
    path = store.data_dir / asset["file_path"]
    info = _probe(path)
    loud = None
    if info.get("has_audio"):
        try:
            loud = FFmpeg().loudness(path)
        except FFmpegError:
            loud = None
    checks = check_media(info, loud, platform)
    report = {"asset_id": asset_id, "platform": platform, "label": PLATFORMS[platform]["label"],
              "ok": not any(c["status"] == "fail" for c in checks),
              "warnings": sum(1 for c in checks if c["status"] == "warn"), "checks": checks,
              "media": {k: info.get(k) for k in ("width", "height", "fps", "duration_s", "video_codec", "audio_codec",
                                                 "channels", "sample_rate")} | {k: (loud or {}).get(k) for k in
                                                                                ("integrated_lufs", "lra", "true_peak_dbfs")},
              "verified_at": now_iso()}
    if sheet:
        try:
            report["contact_sheet_asset_id"] = contact_sheet(store, asset_id, every_n=every_n, count=count)["asset_id"]
        except EngineError as exc:
            report["contact_sheet_error"] = exc.message
    return report


def contact_sheet(store: Store, asset_id: str, every_n: Optional[int] = None, count: int = 12, columns: int = 4,
                  width: int = 320) -> dict[str, Any]:
    """Every Nth frame of a video tiled into one PNG (ffmpeg select + tile), saved as an image asset in the
    video's project. Without `every_n`, N spreads `count` frames over the whole clip."""
    asset = store.get_asset(asset_id)
    if asset["kind"] != "video":
        raise EngineError("not_video", f"asset {asset_id} is {asset['kind']}; a contact sheet needs a video")
    exe = ffmpeg_path()
    if not exe:
        raise EngineError("no_ffmpeg", "ffmpeg is needed for a contact sheet")
    path = store.data_dir / asset["file_path"]
    info = _probe(path)
    count = int(max(1, min(64, count)))
    columns = int(max(1, min(12, columns)))
    width = int(max(64, min(960, width)))
    total = max(1, int(round(float(info.get("duration_s") or 0) * float(info.get("fps") or 0))))
    every = int(every_n) if every_n else max(1, math.ceil(total / count))
    shown = min(64, max(1, math.ceil(total / every)))
    rows = max(1, math.ceil(shown / columns))
    sheet_id = new_id("a")
    out = store.path_for_asset_file(sheet_id, ".png")
    vf = f"select='not(mod(n,{every}))',scale={width}:-2,tile={columns}x{rows}:padding=4:margin=4:color=0x111111"
    done = procutil.run([exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(path), "-vf", vf,
                         "-frames:v", "1", "-fps_mode", "vfr", str(out)], text=True, timeout=600)
    if done.returncode != 0 or not out.is_file():
        done = procutil.run([exe, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", str(path), "-vf", vf,
                             "-frames:v", "1", "-vsync", "vfr", str(out)], text=True, timeout=600)  # ffmpeg before 5.1
    if done.returncode != 0 or not out.is_file():
        raise EngineError("contact_sheet_failed", " ".join((done.stderr or "").strip().splitlines()[-2:])[:400])
    from PIL import Image
    with Image.open(out) as im:
        size = im.size
    make_thumbnail(out, store.path_for_thumb(sheet_id))
    store.create_asset(project_id=asset["project_id"], kind="image", file_path=_rel(store, out), mime="image/png",
                       width=size[0], height=size[1], thumb_path=_rel(store, store.path_for_thumb(sheet_id)),
                       source="rendered", asset_id=sheet_id, name=f"Contact sheet - {asset.get('name') or asset_id}"[:100],
                       tags=["contact_sheet"], recipe={"operation": "contact_sheet", "input_asset_ids": [asset_id],
                                                       "every_n": every, "columns": columns, "created_at": now_iso()})
    return {"asset_id": sheet_id, "source_asset_id": asset_id, "every_n": every, "frames": shown, "columns": columns,
            "rows": rows, "width": size[0], "height": size[1]}
