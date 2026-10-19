from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from conftest import needs_ffmpeg
from prosperos_hoard.editing import contact_sheet as sheets, media, projects
from prosperos_hoard.editing.agent_tools import call_tool, tool_catalog
from prosperos_hoard.editing.errors import LumiereError


def _timeline(svc, directory):
    scene = media.import_path(svc, str(directory / "scenes.mp4"))
    sound = media.import_path(svc, str(directory / "clicks.wav"))
    pid = projects.create(svc, "Three-cut contact sheet", width=320, height=180, fps=25)["id"]
    projects.edit(svc, pid, [{"op": "add_media", "media": scene["id"], "src_in": a, "src_out": b}
                            for a, b in ((500, 1500), (2500, 3500), (4500, 5500), (500, 1500))])
    projects.edit(svc, pid, [{"op": "add_media", "media": sound["id"], "src_in": 0, "src_out": 4000},
                            {"op": "add_text", "text": "Review title", "start": 1000, "length": 1500,
                             "style": {"position": "bottom", "size": 35}}])
    return pid, scene


def test_boundary_pairs_and_end_frame_are_native_grid_samples():
    p = SimpleNamespace(duration=9000, canvas=SimpleNamespace(fps=25),
        main_track=lambda: SimpleNamespace(clips=[SimpleNamespace(start=t) for t in (0, 2000, 4000, 6000)]))
    assert sheets.sample_times(p, mode="boundaries", count=4) == ([1960, 2000, 3960, 4000], True)
    assert sheets.sample_times(p, mode="boundaries", count=3) == ([1960, 2000], True)
    assert sheets.sample_times(p, count=2) == ([0, 8960], False)


def test_sampling_policy_distinguishes_exact_and_rounded_cut_boundaries():
    p = SimpleNamespace(duration=4000, canvas=SimpleNamespace(fps=25),
        main_track=lambda: SimpleNamespace(clips=[SimpleNamespace(start=t) for t in (0, 2000, 2021)]))
    requests, _, policy, details = sheets._frame_plan(p, mode="boundaries", count=6)
    assert policy["strategy"] == "main_track_clip_start_pairs"
    # 2000 ms is exactly frame 50; 2021 ms rounds to frame 51 (2040 ms).
    assert policy["selected_boundary_frames"] == [50, 51]
    assert details[49]["cut_relations"] == [{"relation": "before_cut", "boundary_frame": 50, "boundary_time_ms": 2000}]
    assert details[50]["cut_relations"] == [
        {"relation": "at_clip_start", "boundary_frame": 50, "boundary_time_ms": 2000},
        {"relation": "before_cut", "boundary_frame": 51, "boundary_time_ms": 2040},
    ]
    assert details[51]["cut_relations"][0]["boundary_time_ms"] == 2040


def test_short_timeline_groups_all_original_uniform_grid_indices():
    # An 80 ms, 25 fps timeline has only two valid output frames for four grid points.
    p = SimpleNamespace(duration=80, canvas=SimpleNamespace(fps=25), main_track=lambda: None)
    requests, _, policy, details = sheets._frame_plan(p, mode="overview", count=4)
    assert sorted(requests) == [0, 1]
    assert policy["requested_frame_count"] == 4
    assert policy["selected_frame_count"] == 2
    assert details[0]["grid_indices"] == [0, 1]
    assert details[1]["grid_indices"] == [2, 3]
    assert details[0]["grid_count"] == details[1]["grid_count"] == 4


def test_boundary_mode_reports_endpoint_fallback_reason():
    p = SimpleNamespace(duration=1000, canvas=SimpleNamespace(fps=25),
        main_track=lambda: SimpleNamespace(clips=[SimpleNamespace(start=0)]))
    requests, truncated, policy, details = sheets._frame_plan(p, mode="boundaries", count=4)
    assert sorted(requests) == [0, 24] and not truncated
    assert policy["strategy"] == "start_and_last_frame_fallback"
    assert policy["fallback_reason"] == "no_nonzero_main_track_clip_starts"
    assert {row["reason"] for row in details.values()} == {policy["fallback_reason"]}


def test_adaptive_candidate_grid_is_capped_and_short_flat_timeline_falls_back(tmp_path, monkeypatch):
    assert len(sheets._adaptive_candidate_frames(10_000, 25)) == 120
    crowded = sheets._adaptive_candidate_frames(10_000, 25, range(10_001))
    assert len(crowded) == 120 and crowded[0] == 0 and crowded[-1] == 10_000
    assert sheets._adaptive_candidate_frames(1, 25) == [0, 1]
    project = SimpleNamespace(duration=80, canvas=SimpleNamespace(width=100, height=100, fps=25),
                              main_track=lambda: None, tracks=[], captions=SimpleNamespace(enabled=False))
    renders = tmp_path / "renders"
    svc = SimpleNamespace(config=SimpleNamespace(renders_dir=renders, work_dir=tmp_path / "work"))

    def render(_svc, project_id, time_ms, *, width, fmt, output_path=None):
        path = Path(output_path) if output_path is not None else renders / "frames" / f"{project_id}-{time_ms}-64.{fmt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), "#354a62").save(path)
        return path

    monkeypatch.setattr(sheets.runner, "render_frame", render)
    requests, truncated, policy, details, sources = sheets._adaptive_frame_plan(svc, "short", project, count=8)
    assert sorted(requests) == [0, 1] and not truncated
    assert policy["requested_frame_count"] == 8 and policy["selected_frame_count"] == 2
    assert policy["candidate_frame_count"] == 2
    assert policy["fallback_reason"].startswith("no_candidate_change_reached_threshold")
    assert sources == {}
    assert all(details[frame]["kind"] == "adaptive_context_sample" for frame in requests)
    assert not list((renders / "frames").glob("short-*.png"))  # candidate renders are temporary
    assert not list((tmp_path / "work").glob("adaptive-scan-*"))


@needs_ffmpeg
def test_concurrent_adaptive_sheets_use_private_prepass_paths(services, media_dir):
    source = media.import_path(services, str(media_dir / "scenes.mp4"))
    pid = projects.create(services, "Concurrent short review", width=320, height=180, fps=25)["id"]
    projects.edit(services, pid, [{"op": "add_media", "media": source["id"], "src_in": 0, "src_out": 400}])

    cached_files = [services.config.renders_dir / "frames" / f"{pid}-0-{width}.png" for width in (64, 128)]
    original_cached_bytes = []
    for cached, color in zip(cached_files, ("magenta", "cyan")):
        cached.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), color).save(cached)
        original_cached_bytes.append(cached.read_bytes())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(sheets.create, services, pid, mode="adaptive", count=4, width=128)
                   for _ in range(2)]
        results = [future.result(timeout=120) for future in futures]

    assert [cached.read_bytes() for cached in cached_files] == original_cached_bytes
    assert results[0]["id"] != results[1]["id"]
    result_frame_paths = [{row["frame_path"] for row in result["frames"]} for result in results]
    assert result_frame_paths[0].isdisjoint(result_frame_paths[1])
    for result in results:
        assert result["status"] == "complete"
        assert result["mode"] == "adaptive"
        assert Path(result["png_path"]).is_file()
        assert Path(result["path"]).is_file()
        receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
        assert receipt["id"] == result["id"]
        assert receipt["sampling_policy"]["strategy"] == "bounded_native_color_and_pixel_change"
        for row in result["frames"]:
            with Image.open(row["frame_path"]) as frame:
                assert frame.width == 128
    assert not list(services.config.work_dir.glob("adaptive-scan-*"))


def test_revision_change_never_publishes_mixed_sheet(tmp_path, monkeypatch):
    revision = [1]
    p = SimpleNamespace(duration=1000, canvas=SimpleNamespace(width=100, height=100, fps=25), tracks=[],
                        captions=SimpleNamespace(enabled=False), model_dump=lambda **kw: {"revision": revision[0]})
    monkeypatch.setattr(sheets.projects, "doc", lambda *args: p)
    monkeypatch.setattr(sheets.projects, "nested_ids", lambda *args: set())
    def render(*args, **kwargs):
        path = Path(kwargs.get("output_path") or (tmp_path / "frame.png"))
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (100, 100), "red").save(path)
        revision[0] += 1
        return path
    monkeypatch.setattr(sheets.runner, "render_frame", render)
    svc = SimpleNamespace(config=SimpleNamespace(renders_dir=tmp_path / "renders"))
    with pytest.raises(LumiereError, match="changed"):
        sheets.create(svc, "project", times=[100], width=128)
    assert not list((tmp_path / "renders" / "frames").glob("sheet*"))


@needs_ffmpeg
def test_native_three_cuts_pixels_provenance_export_readback_and_sources(services, media_dir, tmp_path):
    pid, scene = _timeline(services, media_dir)
    original = hashlib.sha256((media_dir / "scenes.mp4").read_bytes()).hexdigest()
    before = projects.doc(services, pid).model_dump(mode="json")
    result = call_tool(services, "project_contact_sheet", {"project": pid, "mode": "boundaries", "count": 6, "width": 160})
    assert result["status"] == "complete" and result["_image"]["mime"] == "image/jpeg"
    assert [row["frame"] for row in result["frames"]] == [24, 25, 49, 50, 74, 75]
    assert [row["t_ms"] for row in result["frames"]] == [960, 1000, 1960, 2000, 2960, 3000]
    assert not result["truncated"]
    assert result["sampling_policy"]["strategy"] == "main_track_clip_start_pairs"
    main = projects.doc(services, pid).main_track().clips
    assert [next(layer["clip"] for layer in row["layers"] if layer["role"] == "main") for row in result["frames"]] == [main[i].id for i in (0, 1, 1, 2, 2, 3)]
    for row in result["frames"]:
        layer = next(layer for layer in row["layers"] if layer["role"] == "main")
        assert layer["timeline_position"] == ("clip_end" if row["frame"] in {24, 49, 74} else "clip_start")
        assert layer["clip_timing"]["clip_start_output_frame"] in {0, 25, 50, 75}
        expected_source = 500 + row["t_ms"] if row["frame"] < 25 else 2500 + row["t_ms"] - 1000 if row["frame"] < 50 else 4500 + row["t_ms"] - 2000 if row["frame"] < 75 else 500 + row["t_ms"] - 3000
        assert layer["source_time_ms"] == expected_source
    assert any(layer["type"] == "text" and layer["text"] == "Review title" for layer in result["frames"][1]["layers"])
    with Image.open(result["png_path"]) as png, Image.open(result["path"]) as jpeg:
        colors = [(255, 0, 0), (0, 128, 0), (0, 128, 0), (0, 0, 255), (0, 0, 255), (255, 0, 0)]
        for row, color in zip(result["frames"], colors):
            x, y, width, height = row["cell_bbox"]
            crop = png.crop((x, y, x + width, y + height)).convert("RGB")
            assert hashlib.sha256(crop.tobytes()).hexdigest() == row["tile_rgb_sha256"]
            with Image.open(row["frame_path"]) as native:
                assert crop.tobytes() == native.convert("RGB").tobytes()
            for picture in (png, jpeg):
                actual = picture.getpixel((x + 10, y + 10))
                assert max(abs(a - b) for a, b in zip(actual, color)) <= 5
    # Independent full timeline export, then decoder readback at each recorded frame.
    job = services.jobs.get(services.start_render(pid, preset="final", lufs=None)["id"])
    assert job["state"] == "done", job["error"]
    export_path = Path(job["result"]["path"])
    for row in result["frames"]:
        output = subprocess.run(["ffmpeg", "-v", "error", "-i", str(export_path), "-vf", f"select=eq(n\\,{row['frame']}),scale=160:90", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        decoded = np.frombuffer(output, np.uint8).reshape(90, 160, 3)
        with Image.open(row["frame_path"]) as frame:
            delta = np.abs(decoded.astype(int) - np.asarray(frame.convert("RGB")).astype(int))
            assert delta.mean() < 3  # Full export codec/resampling differs; labelled content agrees.
    assert projects.doc(services, pid).model_dump(mode="json") == before
    assert hashlib.sha256((media_dir / "scenes.mp4").read_bytes()).hexdigest() == original
    assert result["sources"][scene["id"]]["sha256"] == original
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["frames"] == result["frames"] and receipt["png_sha256"] == result["png_sha256"]
    assert "data:image/png;base64," in Path(result["html_path"]).read_text(encoding="utf-8")
    again = sheets.create(services, pid, mode="boundaries", count=6, width=160)
    assert again["png_sha256"] == result["png_sha256"] and again["jpeg_sha256"] == result["jpeg_sha256"]
    output = os.environ.get("LUMIERE_CONTACT_SHEET_EVIDENCE")
    if output:
        destination = Path(output); destination.mkdir(parents=True, exist_ok=True)
        (destination / "three-cut-proof.json").write_text(json.dumps({"result": receipt, "full_export_path": str(export_path), "full_export_sha256": hashlib.sha256(export_path.read_bytes()).hexdigest(), "source_sha256": original, "repeat_png_sha256": again["png_sha256"]}, indent=2), encoding="utf-8")


@needs_ffmpeg
def test_api_and_agent_catalog_are_usable_and_validate_times(client, media_dir):
    pid, _ = _timeline(client.svc, media_dir)
    entry = next(item for item in tool_catalog() if item["name"] == "project_contact_sheet")
    assert entry["inputSchema"]["properties"]["mode"]["enum"] == ["overview", "boundaries", "adaptive"]
    assert "uniform grid" in entry["description"] and "120 low-resolution native composites" in entry["description"]
    response = client.post(f"/api/projects/{pid}/contact-sheet", json={"times": [1, 10, 10, 500], "width": 160})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "_image" not in body and len(body["frames"]) == 2
    assert body["frames"][0]["frame"] == 0 and body["frames"][0]["requested_times_ms"] == [1, 10]
    assert body["sampling_policy"]["strategy"] == "explicit_times_rounded_to_output_frames"
    assert body["sampling_policy"]["duplicate_input_times_removed"] == 1
    assert body["sampling_policy"]["distinct_times_merged_by_frame"] == 1
    adaptive = client.post(f"/api/projects/{pid}/contact-sheet", json={"mode": "adaptive", "count": 4, "width": 128})
    assert adaptive.status_code == 200, adaptive.text
    assert adaptive.json()["sampling_policy"]["strategy"] == "bounded_native_color_and_pixel_change"
    for field in ("url", "png_url", "receipt_url", "html_url"):
        assert client.get(body[field]).status_code == 200
    agent = client.post("/api/agent/call", headers={"Authorization": f"Bearer {client.svc.token}"}, json={"name": "project_contact_sheet", "arguments": {"project": pid, "times": [500], "width": 160, "show": False}, "reason": "Look at the montage"})
    assert agent.status_code == 200 and "_image" not in agent.json()
    assert client.post(f"/api/projects/{pid}/contact-sheet", json={"times": [-1]}).status_code == 400
    assert client.post(f"/api/projects/{pid}/contact-sheet", json={"times": [4000]}).status_code == 400


@needs_ffmpeg
def test_adaptive_native_sheet_detects_flat_color_cuts_and_preserves_source(services, media_dir, monkeypatch):
    pid, source = _timeline(services, media_dir)
    original_source_hash = hashlib.sha256((media_dir / "scenes.mp4").read_bytes()).hexdigest()
    project_before = projects.doc(services, pid).model_dump(mode="json")
    calls = []
    native_render = sheets.runner.render_frame

    def observed_render(*args, **kwargs):
        calls.append((args[2], kwargs.get("width")))
        return native_render(*args, **kwargs)

    monkeypatch.setattr(sheets.runner, "render_frame", observed_render)
    result = call_tool(services, "project_contact_sheet", {
        "project": pid, "mode": "adaptive", "count": 8, "width": 128, "show": False})
    policy = result["sampling_policy"]
    frames = [row["frame"] for row in result["frames"]]
    assert result["mode"] == "adaptive" and result["status"] == "complete"
    assert policy["strategy"] == "bounded_native_color_and_pixel_change"
    assert policy["candidate_frame_count"] <= 120
    assert len(calls) == policy["candidate_frame_count"] + len(frames)
    assert {25, 50, 75} <= set(policy["selected_change_frames"])
    assert len(frames) <= 8 and len(frames) == len(set(frames))
    for row in result["frames"]:
        with Image.open(row["frame_path"]) as frame:
            assert hashlib.sha256(frame.convert("RGB").tobytes()).hexdigest() == row["tile_rgb_sha256"]
        assert row["sampling"]["kind"] in {"adaptive_change_sample", "adaptive_context_sample", "adaptive_coverage_sample"}
    assert projects.doc(services, pid).model_dump(mode="json") == project_before
    assert hashlib.sha256((media_dir / "scenes.mp4").read_bytes()).hexdigest() == original_source_hash
    assert result["sources"][source["id"]]["sha256"] == original_source_hash
    assert len(list((services.config.renders_dir / "frames").glob(f"{pid}-*-64.png"))) == 0
    evidence = os.environ.get("LUMIERE_ADAPTIVE_EVIDENCE")
    if evidence:
        destination = Path(evidence)
        destination.mkdir(parents=True, exist_ok=True)
        artifacts = {}
        for label, path in (("sheet.png", result["png_path"]), ("sheet.jpg", result["path"])):
            target = destination / label
            shutil.copy2(path, target)
            artifacts[label] = {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        for row in result["frames"]:
            target = destination / f"frame-{row['frame']:04d}.png"
            shutil.copy2(row["frame_path"], target)
            artifacts[target.name] = {"path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        (destination / "adaptive-proof.json").write_text(json.dumps({
            "sampling_policy": policy, "selected_frames": frames, "source_sha256": original_source_hash,
            "project_unchanged": projects.doc(services, pid).model_dump(mode="json") == project_before,
            "source_unchanged": hashlib.sha256((media_dir / "scenes.mp4").read_bytes()).hexdigest() == original_source_hash,
            "renderer_calls": len(calls), "artifacts": artifacts,
        }, indent=2), encoding="utf-8")


@needs_ffmpeg
def test_adaptive_native_scan_samples_inside_a_crossfade(services, media_dir):
    scene = media.import_path(services, str(media_dir / "scenes.mp4"))
    pid = projects.create(services, "Adaptive dissolve", width=320, height=180, fps=25)["id"]
    projects.edit(services, pid, [
        {"op": "add_media", "media": scene["id"], "src_in": 0, "src_out": 1000},
        {"op": "add_media", "media": scene["id"], "src_in": 2000, "src_out": 3000},
    ])
    second = projects.doc(services, pid).main_track().clips[1]
    projects.edit(services, pid, [{"op": "transition", "clip": second.id, "type": "crossfade", "dur": 500}])
    project = projects.doc(services, pid)
    transition_clip = project.find(second.id)[1]
    transition_start, transition_end = transition_clip.start, transition_clip.start + transition_clip.transition_in.dur
    result = sheets.create(services, pid, mode="adaptive", count=4, width=128)
    selected = result["sampling_policy"]["selected_change_frames"]
    assert any(transition_start <= round(frame * 1000 / project.canvas.fps) <= transition_end
               for frame in selected), (transition_start, transition_end, selected, result["sampling_policy"]["candidate_scores"])


@needs_ffmpeg
def test_adaptive_native_scan_handles_textured_motion_without_duplicate_samples(services, media_dir):
    source_path = media_dir / "talk.mp4"
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source = media.import_path(services, str(source_path))
    pid = projects.create(services, "Adaptive textured movement", width=320, height=180, fps=25)["id"]
    projects.edit(services, pid, [{"op": "add_media", "media": source["id"], "src_in": 1000, "src_out": 3000}])
    result = sheets.create(services, pid, mode="adaptive", count=6, width=128)
    frames = [row["frame"] for row in result["frames"]]
    policy = result["sampling_policy"]
    assert 2 <= len(frames) <= 6 and len(frames) == len(set(frames))
    assert policy["candidate_frame_count"] <= 120
    assert policy["detected_change_event_count"] >= 1
    assert any((row["score"] or 0) >= policy["change_threshold"] for row in policy["candidate_scores"])
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    assert result["sources"][source["id"]]["sha256"] == source_hash


@needs_ffmpeg
def test_overview_is_uniform_grid_and_layers_label_clip_position(services, media_dir):
    pid, _ = _timeline(services, media_dir)
    project = projects.doc(services, pid)
    main = project.main_track().clips
    result = sheets.create(services, pid, mode="overview", count=4, width=128)
    assert [row["frame"] for row in result["frames"]] == [0, 33, 66, 99]
    policy = result["sampling_policy"]
    assert policy["strategy"] == "uniform_output_frame_grid"
    assert policy["clip_starts_are_not_sampling_targets"] is True
    assert all(row["sampling"]["kind"] == "uniform_grid_sample" for row in result["frames"])
    positions = []
    for row in result["frames"]:
        layer = next(layer for layer in row["layers"] if layer["role"] == "main")
        positions.append(layer["timeline_position"])
        timing = layer["clip_timing"]
        assert timing["clip_start_output_frame"] == project.find(layer["clip"])[1].start // 40
        assert timing["frames_from_clip_start"] == row["frame"] - timing["clip_start_output_frame"]
    assert positions == ["clip_start", "clip_interior", "clip_interior", "clip_end"]
    assert [next(layer["clip"] for layer in row["layers"] if layer["role"] == "main") for row in result["frames"]] == [main[0].id, main[1].id, main[2].id, main[3].id]


@needs_ffmpeg
def test_external_source_change_fails_and_publishes_no_sheet(services, media_dir, tmp_path, monkeypatch):
    source = tmp_path / "local-scenes.mp4"
    source.write_bytes((media_dir / "scenes.mp4").read_bytes())
    info = media.import_path(services, str(source))
    pid = projects.create(services, "Concurrent source change", media=[info["id"]], width=320, height=180, fps=25)["id"]
    real_render = sheets.runner.render_frame
    def render(*args, **kwargs):
        path = real_render(*args, **kwargs)
        stat = source.stat()
        modified = bytearray(source.read_bytes()); modified[-1] ^= 1
        source.write_bytes(modified)
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return path
    monkeypatch.setattr(sheets.runner, "render_frame", render)
    with pytest.raises(LumiereError, match="source changed"):
        sheets.create(services, pid, times=[500], width=160)
    assert not list((services.config.renders_dir / "frames").glob("sheet*"))


@needs_ffmpeg
def test_adaptive_rejects_source_change_during_candidate_scan(services, media_dir, tmp_path, monkeypatch):
    source = tmp_path / "adaptive-source.mp4"
    source.write_bytes((media_dir / "scenes.mp4").read_bytes())
    info = media.import_path(services, str(source))
    pid = projects.create(services, "Adaptive source race", media=[info["id"]], width=320, height=180, fps=25)["id"]
    original_render = sheets.runner.render_frame
    mutated = False

    def render(*args, **kwargs):
        nonlocal mutated
        path = original_render(*args, **kwargs)
        if not mutated:
            stat = source.stat()
            data = bytearray(source.read_bytes())
            data[-1] ^= 1
            source.write_bytes(data)
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            mutated = True
        return path

    monkeypatch.setattr(sheets.runner, "render_frame", render)
    with pytest.raises(LumiereError, match="source changed during adaptive sampling"):
        sheets.create(services, pid, mode="adaptive", count=4, width=128)
    frames = services.config.renders_dir / "frames"
    assert not list(frames.glob("sheet*"))
    assert not list(frames.glob(f"{pid}-*-64.png"))


@needs_ffmpeg
def test_counter_frames_confirm_speed_reverse_and_end_sampling(services, tmp_path):
    from test_frame_accuracy import _counter, _index, expected, W, H
    source = tmp_path / "frame-counter.mp4"
    _counter(source, "25", 4)
    mid = media.import_path(services, str(source))["id"]
    pid = projects.create(services, "Counter timing", width=W, height=H, fps=25)["id"]
    projects.edit(services, pid, [{"op": "add_media", "media": mid, "src_in": 1000, "src_out": 2000}])
    first = projects.doc(services, pid).main_track().clips[0]
    projects.edit(services, pid, [{"op": "speed", "clip": first.id, "speed": 2},
                                {"op": "add_media", "media": mid, "src_in": 3000, "src_out": 3500}])
    second = projects.doc(services, pid).main_track().clips[1]
    projects.edit(services, pid, [{"op": "set", "clip": second.id, "props": {"reverse": True}}])
    project = projects.doc(services, pid)
    result = sheets.create(services, pid, width=W, times=[0, 200, 480, 520, 720, 999])
    for row in result["frames"]:
        accepted, shown = expected(project.main_track().clips, row["frame"], 25, 25)
        assert shown
        with Image.open(row["frame_path"]) as image:
            assert _index(np.asarray(image.convert("L")).astype(int)) in accepted
        layer = next(layer for layer in row["layers"] if layer["role"] == "main")
        clip = project.find(layer["clip"])[1]
        assert layer["source_time_ms"] == clip.src_at(row["actual_time_ms"])
    overview = sheets.create(services, pid, width=W, count=2)
    assert [row["frame"] for row in overview["frames"]] == [0, 24]
    assert overview["frames"][-1]["t_ms"] == 960
    with Image.open(overview["frames"][-1]["frame_path"]) as image:
        assert _index(np.asarray(image.convert("L")).astype(int)) in expected(project.main_track().clips, 24, 25, 25)[0]


@needs_ffmpeg
def test_render_failure_identifies_frame_and_publishes_no_receipt(services, media_dir, monkeypatch):
    pid, _ = _timeline(services, media_dir)
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic native renderer failure")
    monkeypatch.setattr(sheets.runner, "render_frame", fail)
    with pytest.raises(LumiereError, match=r"output frame 12 \(0:00.480\).*renderer failure"):
        sheets.create(services, pid, times=[500], width=160)
    assert not list((services.config.renders_dir / "frames").glob("sheet*"))
