"""Native timeline contact sheets with frame timing and portable provenance."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import tempfile
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import projects, media, ffmpeg
from .errors import LumiereError
from .render import compiler, runner
from .util import ms_to_tc, new_id

ADAPTIVE_MAX_CANDIDATES = 120
ADAPTIVE_TARGET_CANDIDATES_PER_SECOND = 4
ADAPTIVE_CHANGE_THRESHOLD = 0.035
ADAPTIVE_MIN_EVENT_SPACING_SECONDS = 0.5


def _revision(project):
    raw = json.dumps(project.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _frame_plan(project, *, mode="overview", count=12, times=None):
    """Return output-frame requests and truthful machine-readable sampling provenance."""
    if project.duration <= 0:
        raise LumiereError("The timeline is empty.")
    if mode not in {"overview", "boundaries"} or type(count) is not int or not 2 <= count <= 16:
        raise LumiereError("mode must be overview or boundaries; count must be 2–16.")
    fps = project.canvas.fps
    last = max(0, compiler.frame_of_ms(project.duration, fps) - 1)
    if times is not None:
        if not isinstance(times, list) or not 1 <= len(times) <= 16 or any(type(t) is not int or not 0 <= t < project.duration for t in times):
            raise LumiereError("times must contain 1–16 integer milliseconds inside the timeline.")
        unique_times = sorted(set(times))
        requests = {}
        for requested_ms in unique_times:
            frame = min(last, compiler.frame_of_ms(requested_ms, fps))
            requests.setdefault(frame, []).append(requested_ms)
        policy = {"strategy": "explicit_times_rounded_to_output_frames", "requested_times_ms": times,
                  "unique_requested_times_ms": unique_times, "duplicate_input_times_removed": len(times) - len(unique_times),
                  "distinct_times_merged_by_frame": len(unique_times) - len(requests),
                  "selected_output_frames": sorted(requests)}
        details = {frame: {"kind": "explicit_time", "reason": "requested timeline milliseconds rounded to this native output frame",
                           "requested_times_ms": requested} for frame, requested in requests.items()}
        return requests, False, policy, details
    if mode == "overview":
        grid_indices = {}
        for index in range(count):
            frame = round(index * last / (count - 1))
            grid_indices.setdefault(frame, []).append(index)
        frames = sorted(grid_indices)
        truncated = False
        policy = {"strategy": "uniform_output_frame_grid", "requested_frame_count": count,
                  "selected_frame_count": len(frames), "selected_output_frames": frames,
                  "selection_rule": "round(i * last_valid_output_frame / (count - 1)); i = 0..count-1",
                  "clip_starts_are_not_sampling_targets": True}
        details = {frame: {"kind": "uniform_grid_sample", "reason": "uniform position on the valid output-frame grid",
                           "grid_indices": grid_indices[frame], "grid_count": count} for frame in frames}
    else:
        track = project.main_track()
        boundaries = sorted(set(compiler.frame_of_ms(c.start, fps) for c in track.clips
                                if 0 < compiler.frame_of_ms(c.start, fps) <= last)) if track else []
        selected = boundaries[:count // 2]
        truncated = len(selected) < len(boundaries)
        details = {}
        if selected:
            frames = sorted(set(frame for boundary in selected for frame in (boundary - 1, boundary)))
            for boundary in selected:
                time_ms = round(boundary * 1000 / fps)
                for frame, relation in ((boundary - 1, "before_cut"), (boundary, "at_clip_start")):
                    details.setdefault(frame, {"kind": "main_track_cut_pair", "cut_relations": []})["cut_relations"].append(
                        {"relation": relation, "boundary_frame": boundary, "boundary_time_ms": time_ms})
            policy = {"strategy": "main_track_clip_start_pairs", "requested_frame_count": count,
                      "main_track_boundary_count": len(boundaries), "selected_boundary_frames": selected,
                      "selected_output_frames": frames, "truncated": truncated}
        else:
            frames = sorted({0, last})
            fallback_reason = "no_nonzero_main_track_clip_starts"
            details = {frame: {"kind": "fallback_endpoint", "reason": fallback_reason,
                               "endpoint": "start" if frame == 0 else "last_valid_frame"} for frame in frames}
            policy = {"strategy": "start_and_last_frame_fallback", "requested_frame_count": count,
                      "main_track_boundary_count": 0, "selected_output_frames": frames,
                      "fallback_reason": fallback_reason, "truncated": False}
    return {frame: [round(frame * 1000 / fps)] for frame in frames}, truncated, policy, details


def _adaptive_candidate_frames(last_frame: int, fps: float, clip_starts=()) -> list[int]:
    available = last_frame + 1
    desired = min(available, ADAPTIVE_MAX_CANDIDATES,
                  max(2, math.ceil(last_frame / fps * ADAPTIVE_TARGET_CANDIDATES_PER_SECOND) + 1))
    if desired <= 1:
        uniform = {0}
    else:
        uniform = {round(i * last_frame / (desired - 1)) for i in range(desired)}
    protected = {0, last_frame, *(frame for frame in clip_starts if 0 <= frame <= last_frame)}
    if len(protected) >= ADAPTIVE_MAX_CANDIDATES:
        if len(protected) <= ADAPTIVE_MAX_CANDIDATES:
            return sorted(protected)
        interior = sorted(protected - {0, last_frame})
        slots = ADAPTIVE_MAX_CANDIDATES - len({0, last_frame})
        chosen = {interior[round(i * (len(interior) - 1) / max(1, slots - 1))] for i in range(slots)}
        return sorted(chosen | {0, last_frame})
    selected = set(protected)
    while len(selected) < ADAPTIVE_MAX_CANDIDATES:
        remaining = uniform - selected
        if not remaining:
            break
        selected.add(max(remaining, key=lambda frame: (min(abs(frame - prior) for prior in selected), -frame)))
    return sorted(selected)


def _adaptive_candidate_pixels(svc, project_id: str, project, frame: int, output_path: Path) -> np.ndarray:
    """Read one low-resolution native composite into this request's private scratch path."""
    fps = project.canvas.fps
    requested_ms = min(project.duration - 1, round(frame * 1000 / fps))
    rendered = runner.render_frame(svc, project_id, requested_ms, width=64, fmt="png", output_path=output_path)
    with Image.open(rendered) as image:
        sample = image.convert("RGB")
        sample.thumbnail((48, 48), Image.Resampling.BILINEAR)
        return np.asarray(sample, dtype=np.uint8).copy()


def _snapshot_sources(svc, project, frames):
    sources = {}
    for frame in frames:
        actual_ms = float(frame * 1000 / project.canvas.fps)
        for layer in _source_layers(_layers(svc, project, frame, actual_ms)):
            media_id, source_path = layer.get("media"), layer.get("source_path")
            if not media_id or not source_path or media_id in sources:
                continue
            source = Path(source_path)
            try:
                stat = source.stat()
                sources[media_id] = {"path": str(source), "sha256": _sha(source), "bytes": stat.st_size,
                                     "mtime_ns": stat.st_mtime_ns}
            except OSError as exc:
                raise LumiereError(f"Contact sheet source is unavailable at output frame {frame} ({ms_to_tc(round(actual_ms))}): {exc}", code="contact_sheet_frame_failed") from exc
    return sources


def _source_changed(info):
    source = Path(info["path"])
    try:
        stat = source.stat()
        return stat.st_size != info["bytes"] or stat.st_mtime_ns != info["mtime_ns"] or _sha(source) != info["sha256"]
    except OSError:
        return True


def _adaptive_frame_plan(svc, project_id: str, project, *, count: int):
    """Scan bounded native composites, then select high-change and coverage frames."""
    if project.duration <= 0:
        raise LumiereError("The timeline is empty.")
    if type(count) is not int or not 2 <= count <= 16:
        raise LumiereError("count must be 2–16.")
    fps = project.canvas.fps
    last = max(0, compiler.frame_of_ms(project.duration, fps) - 1)
    track = project.main_track()
    clip_start_frames = sorted({compiler.frame_of_ms(clip.start, fps) for clip in track.clips
                                if 0 < compiler.frame_of_ms(clip.start, fps) <= last}) if track else []
    candidates = _adaptive_candidate_frames(last, fps, clip_start_frames)
    sources = _snapshot_sources(svc, project, candidates)
    records = []
    previous = None
    svc.config.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="adaptive-scan-", dir=svc.config.work_dir) as scratch_dir:
        scratch = Path(scratch_dir)
        for frame in candidates:
            pixels = _adaptive_candidate_pixels(svc, project_id, project, frame, scratch / f"{frame}.png")
            if previous is None:
                record = {"frame": frame, "time_ms": round(frame * 1000 / fps),
                          "mean_rgb_delta": None, "changed_pixel_fraction": None, "score": None}
            else:
                if pixels.shape != previous.shape:
                    image = Image.fromarray(pixels).resize((previous.shape[1], previous.shape[0]), Image.Resampling.BILINEAR)
                    pixels = np.asarray(image, dtype=np.uint8)
                delta = np.abs(pixels.astype(np.int16) - previous.astype(np.int16))
                mean_rgb = float(delta.mean() / 255.0)
                changed_fraction = float((delta.max(axis=2) >= 18).mean())
                score = 0.65 * mean_rgb + 0.35 * changed_fraction
                record = {"frame": frame, "time_ms": round(frame * 1000 / fps),
                          "mean_rgb_delta": round(mean_rgb, 6),
                          "changed_pixel_fraction": round(changed_fraction, 6), "score": round(score, 6)}
            records.append(record)
            previous = pixels

    # Consecutive above-threshold changes form one event. A hard cut is usually
    # a single sample; a gradual blend often spans several samples, so choose
    # the center of its tied/near-tied strongest changes.
    active = [i for i, row in enumerate(records) if row["score"] is not None and
              row["score"] >= ADAPTIVE_CHANGE_THRESHOLD]
    groups = []
    for index in active:
        if not groups or index != groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    events = []
    for group in groups:
        maximum = max(records[i]["score"] for i in group)
        near_best = [i for i in group if maximum - records[i]["score"] <= max(0.002, maximum * 0.02)]
        interval_midpoint = (records[group[0]]["frame"] + records[group[-1]]["frame"]) / 2
        chosen_index = min(near_best, key=lambda i: (abs(records[i]["frame"] - interval_midpoint), records[i]["frame"]))
        events.append({**records[chosen_index], "event_start_frame": records[group[0]]["frame"],
                       "event_end_frame": records[group[-1]]["frame"], "peak_score": round(maximum, 6)})

    selected = {0, last}
    event_frames = []
    min_spacing = max(1, round(fps * ADAPTIVE_MIN_EVENT_SPACING_SECONDS))
    for event in sorted(events, key=lambda row: (-row["peak_score"], row["frame"])):
        frame = event["frame"]
        if len(selected) >= count:
            break
        if frame not in selected and all(abs(frame - prior) >= min_spacing for prior in event_frames):
            selected.add(frame)
            event_frames.append(frame)

    # If the visual pass finds few changes, fill remaining slots by choosing
    # candidates farthest from the current set, preserving temporal coverage.
    while len(selected) < min(count, len(candidates)):
        remaining = [frame for frame in candidates if frame not in selected]
        if not remaining:
            break
        frame = max(remaining, key=lambda candidate: (min(abs(candidate - prior) for prior in selected), -candidate))
        selected.add(frame)

    frames = sorted(selected)
    events_by_frame = {event["frame"]: event for event in events}
    details = {}
    for frame in frames:
        event = events_by_frame.get(frame)
        if event and frame in event_frames:
            details[frame] = {"kind": "adaptive_change_sample",
                              "reason": "selected near the midpoint of this above-threshold change interval",
                              "score": event["score"], "peak_score": event["peak_score"],
                              "mean_rgb_delta": event["mean_rgb_delta"],
                              "changed_pixel_fraction": event["changed_pixel_fraction"],
                              "event_start_frame": event["event_start_frame"],
                              "event_end_frame": event["event_end_frame"]}
        elif frame == 0 or frame == last:
            details[frame] = {"kind": "adaptive_context_sample",
                              "reason": "reserved timeline endpoint for temporal context",
                              "endpoint": "start" if frame == 0 else "last_valid_frame"}
        else:
            details[frame] = {"kind": "adaptive_coverage_sample",
                              "reason": "candidate farthest in time from already selected samples"}

    fallback = None if events else "no_candidate_change_reached_threshold; selected endpoints and spread candidates"
    policy = {"strategy": "bounded_native_color_and_pixel_change",
              "requested_frame_count": count, "selected_frame_count": len(frames),
              "selected_output_frames": frames, "candidate_frame_count": len(records),
              "candidate_frame_cap": ADAPTIVE_MAX_CANDIDATES,
              "candidate_target_rate_per_second": ADAPTIVE_TARGET_CANDIDATES_PER_SECOND,
              "candidate_main_track_clip_start_frames": clip_start_frames,
              "change_threshold": ADAPTIVE_CHANGE_THRESHOLD,
              "score_formula": "0.65 * mean_absolute_RGB_delta/255 + 0.35 * fraction_of_pixels_with_any_channel_delta_at_least_18/255",
              "minimum_event_spacing_seconds": ADAPTIVE_MIN_EVENT_SPACING_SECONDS,
              "detected_change_event_count": len(events), "selected_change_frames": event_frames,
              "candidate_scores": records, "fallback_reason": fallback,
              "truncated": len(event_frames) < len(events)}
    requests = {frame: [round(frame * 1000 / fps)] for frame in frames}
    if any(_source_changed(info) for info in sources.values()):
        raise LumiereError("A source changed during adaptive sampling; create a fresh contact sheet.")
    return requests, len(event_frames) < len(events), policy, details, sources


def sample_times(project, *, mode="overview", count=12):
    """Sample real output frames, including the last valid frame, never the end."""
    requests, truncated, _, _ = _frame_plan(project, mode=mode, count=count)
    return [round(frame * 1000 / project.canvas.fps) for frame in sorted(requests)], truncated


def _layers(svc, project, frame, actual_ms, stack=()):
    out = []
    for track in reversed(project.tracks):
        if track.hidden or track.kind == "audio":
            continue
        for clip in track.clips:
            start_frame = compiler.frame_of_ms(clip.start, project.canvas.fps)
            end_frame = compiler.frame_of_ms(clip.end, project.canvas.fps)
            if not start_frame <= frame < end_frame:
                continue
            if frame == start_frame and frame == end_frame - 1:
                timeline_position = "clip_start_end"
            elif frame == start_frame:
                timeline_position = "clip_start"
            elif frame == end_frame - 1:
                timeline_position = "clip_end"
            else:
                timeline_position = "clip_interior"
            layer = {"track": track.id, "role": track.role, "clip": clip.id, "type": clip.type,
                     "timeline_position": timeline_position,
                     "clip_timing": {"clip_start_ms": clip.start, "clip_end_ms_exclusive": clip.end,
                                     "clip_start_output_frame": start_frame, "clip_end_output_frame_exclusive": end_frame,
                                     "clip_start_output_grid_time_ms": round(start_frame * 1000 / project.canvas.fps),
                                     "clip_end_output_grid_time_ms_exclusive": round(end_frame * 1000 / project.canvas.fps),
                                     "timeline_offset_from_clip_start_ms": round(actual_ms - clip.start, 3),
                                     "frames_from_clip_start": frame - start_frame,
                                     "frames_until_clip_end": end_frame - frame - 1}}
            if clip.type == "text":
                layer["text"] = clip.text
            else:
                source_ms = clip.src_at(actual_ms)
                layer.update(media=clip.media, source_time_ms=source_ms, source_time=ms_to_tc(round(source_ms)),
                             source_time_kind="timeline mapping, not decoder PTS", speed=clip.speed_at(actual_ms), reverse=clip.reverse)
                if clip.type == "sequence":
                    nested = projects.doc(svc, clip.media)
                    layer["sequence_revision"] = _revision(nested)
                    if clip.media not in stack:
                        layer["nested_layers"] = _layers(svc, nested, compiler.frame_of_ms(source_ms, nested.canvas.fps), source_ms, (*stack, clip.media))
                else:
                    info = media.lookup(svc, clip.media)
                    if info:
                        layer.update(media_name=info["name"], source_path=info["path"])
                if clip.transition_in:
                    layer["transition_in"] = clip.transition_in.model_dump(mode="json")
            out.append(layer)
    if project.captions.enabled:
        out.append({"type": "captions", "style": project.captions.style, "note": "Native burned-in captions are rendered; transcript word provenance is not expanded here"})
    return out


def _source_layers(layers):
    for layer in layers:
        yield layer
        yield from _source_layers(layer.get("nested_layers", []))


def create(svc, project_id, *, mode="overview", count=12, width=320, times=None):
    project = projects.doc(svc, project_id)
    if type(width) is not int or not 128 <= width <= 640:
        raise LumiereError("width must be 128–640 pixels.")
    revisions = {project_id: _revision(project)}
    for nested in projects.nested_ids(svc, project_id):
        revisions[nested] = _revision(projects.doc(svc, nested))
    pre_scanned_sources = {}
    if times is not None:
        requests, truncated, sampling_policy, frame_sampling = _frame_plan(project, mode="overview", count=count, times=times)
    elif mode == "adaptive":
        requests, truncated, sampling_policy, frame_sampling, pre_scanned_sources = _adaptive_frame_plan(
            svc, project_id, project, count=count)
    else:
        requests, truncated, sampling_policy, frame_sampling = _frame_plan(project, mode=mode, count=count)
    if any(_revision(projects.doc(svc, pid)) != revision for pid, revision in revisions.items()):
        raise LumiereError("The timeline changed during review; create a fresh contact sheet.")
    rate = Fraction(ffmpeg.fps_fraction(project.canvas.fps))
    columns = min(4, len(requests))
    height = max(1, min(1280, round(width * project.canvas.height / project.canvas.width)))
    gap, label_h = 8, 40
    sheet = Image.new("RGB", (columns * (width + gap) + gap, math.ceil(len(requests) / columns) * (height + label_h + gap) + gap), "#171717")
    draw = ImageDraw.Draw(sheet)
    artifact = new_id("sheet")
    folder = svc.config.renders_dir / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    outputs, frames, sources = [], [], pre_scanned_sources
    try:
        for index, (frame, requested) in enumerate(sorted(requests.items())):
            actual_ms = float(frame * 1000 / rate)
            render_ms = min(project.duration - 1, round(frame * 1000 / project.canvas.fps))
            layers = _layers(svc, project, frame, actual_ms)
            try:
                for layer in _source_layers(layers):
                    if layer.get("source_path") and layer["media"] not in sources:
                        source = Path(layer["source_path"])
                        stat = source.stat()
                        sources[layer["media"]] = {"path": str(source), "sha256": _sha(source), "bytes": stat.st_size,
                                                   "mtime_ns": stat.st_mtime_ns}
            except OSError as exc:
                raise LumiereError(f"Contact sheet source is unavailable at output frame {frame} ({ms_to_tc(round(actual_ms))}): {exc}", code="contact_sheet_frame_failed") from exc
            try:
                frame_path = folder / f"{artifact}-{index + 1}.png"
                outputs.append(frame_path)
                runner.render_frame(svc, project_id, render_ms, width=width, fmt="png", output_path=frame_path)
                frame_bytes = frame_path.read_bytes()
                with Image.open(frame_path) as image:
                    tile = image.convert("RGB")
                    tile.thumbnail((width, height))
            except Exception as exc:
                raise LumiereError(f"Contact sheet failed at output frame {frame} ({ms_to_tc(round(actual_ms))}): {exc}", code="contact_sheet_frame_failed") from exc
            x = gap + (index % columns) * (width + gap) + (width - tile.width) // 2
            y = gap + (index // columns) * (height + label_h + gap) + (height - tile.height) // 2
            sheet.paste(tile, (x, y))
            label_x, label_y = gap + (index % columns) * (width + gap), gap + (index // columns) * (height + label_h + gap) + height + 3
            draw.text((label_x, label_y), f"{ms_to_tc(round(actual_ms))}  f{frame}", fill="#eeeeee")
            names = ", ".join(layer.get("media_name") or layer.get("text") or layer.get("clip", "") for layer in layers)
            draw.text((label_x, label_y + 16), names[:max(12, width // 6)], fill="#bcbcbc")
            frames.append({"t_ms": round(actual_ms), "time": ms_to_tc(round(actual_ms)), "frame": frame,
                           "actual_time_ms": actual_ms, "actual_time_seconds": {"numerator": frame * rate.denominator, "denominator": rate.numerator},
                           "requested_times_ms": requested, "sampling": frame_sampling[frame],
                           "render_time_ms": render_ms, "layers": layers,
                           "frame_path": str(frame_path), "frame_url": "/api/frames/" + frame_path.name,
                           "frame_sha256": hashlib.sha256(frame_bytes).hexdigest(), "tile_rgb_sha256": hashlib.sha256(tile.tobytes()).hexdigest(),
                           "cell_bbox": [x, y, tile.width, tile.height]})
            if any(_revision(projects.doc(svc, pid)) != revision for pid, revision in revisions.items()):
                raise LumiereError("The timeline changed during review; create a fresh contact sheet.")
        if any(_source_changed(info) for info in sources.values()):
            raise LumiereError("A source changed during review; create a fresh contact sheet.")
        png, jpeg, receipt, page = [folder / f"{artifact}.{ext}" for ext in ("png", "jpg", "json", "html")]
        outputs.extend([png, jpeg, receipt, page])
        sheet.save(png)
        sheet.save(jpeg, quality=88)
        out = {"id": artifact, "status": "complete", "project": project_id, "project_revision": revisions[project_id], "project_revisions": revisions,
               "mode": "explicit" if times is not None else mode, "sampling_policy": sampling_policy,
               "frames": frames, "sources": sources, "truncated": truncated,
               "path": str(jpeg), "url": "/api/frames/" + jpeg.name, "png_path": str(png), "png_url": "/api/frames/" + png.name,
               "receipt_path": str(receipt), "receipt_url": "/api/frames/" + receipt.name, "html_path": str(page), "html_url": "/api/frames/" + page.name,
               "png_sha256": _sha(png), "jpeg_sha256": _sha(jpeg), "canvas": project.canvas.model_dump(mode="json"),
               "fps_fraction": str(rate), "sheet_size": list(sheet.size), "originals_modified": False,
               "limitations": ["Boundaries samples main-track clip starts, not every composite change or source scene.",
                               "Source times are timeline mappings, not decoder PTS; nested sources are attributed to their sequence.",
                               "A sheet does not prove motion/audio continuity or editor parity."]}
        rows = "".join(f"<tr><td>{row['time']}</td><td>{row['frame']}</td><td>{html.escape(', '.join(layer.get('media_name') or layer.get('text') or layer.get('clip', '') for layer in row['layers']))}</td></tr>" for row in frames)
        page.write_text("<!doctype html><html lang='en'><meta charset='utf-8'><title>Timeline contact sheet</title>"
                        "<style>body{font:16px system-ui;background:#171717;color:#eee;margin:24px}img{max-width:100%;height:auto}td,th{padding:8px;text-align:left}table{border-collapse:collapse}</style>"
                        f"<h1>Timeline contact sheet</h1><p>Project {html.escape(project_id)} · revision {revisions[project_id]}</p>"
                        f"<img alt='Timecoded timeline frames' src='data:image/png;base64,{base64.b64encode(png.read_bytes()).decode('ascii')}'>"
                        f"<table><thead><tr><th>Time</th><th>Frame</th><th>Sources / titles</th></tr></thead><tbody>{rows}</tbody></table></html>", encoding="utf-8")
        out["html_sha256"] = _sha(page)
        receipt.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        out["receipt_sha256"] = _sha(receipt)
        return out
    except Exception:
        for output in outputs:
            output.unlink(missing_ok=True)
        raise
