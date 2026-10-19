"""Several canvases from one project in one render job."""

import json
import subprocess
from pathlib import Path

import pytest

from conftest import needs_ffmpeg, probe
from prosperos_hoard.editing import agent_tools
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.analysis import audio as audio_an
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.render import formats as F

from conftest import LOOK


@pytest.fixture
def lib(services, media_dir):
    ids = {}
    for name in ("talk.mp4", "vert.mp4", "clicks.wav"):
        ids[name.split(".")[0]] = media_store.import_path(services, str(media_dir / name))["id"]
    return ids


def sizes(path):
    v = next(s for s in probe(Path(path))["streams"] if s["codec_type"] == "video")
    return v["width"], v["height"]


def duration_ms(path):
    return round(float(probe(Path(path))["format"]["duration"]) * 1000)


def frame(path, t_s, dest):
    from PIL import Image

    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t_s), "-i", str(path), "-frames:v", "1", str(dest)], check=True)
    return Image.open(dest).convert("RGB")


def run(services, pid, **kw):
    job = services.jobs.get(services.start_render(pid, **kw)["id"])
    assert job["state"] == "done", job["error"]
    return job


# ---------------------------------------------------------------- what is asked for

def test_formats_are_parsed_from_what_assistants_send():
    got = F.parse_formats(["16:9", "9:16", "1:1"])
    assert [(f.key, f.width, f.height) for f in got] == [("16x9", 1920, 1080), ("9x16", 1080, 1920), ("1x1", 1080, 1080)]
    got = F.parse_formats(["reels", {"aspect": "4:5", "reframe": "blur"}, "1280x720", {"width": 800, "height": 600}], "center")
    assert [(f.key, f.width, f.height, f.reframe) for f in got] == [
        ("9x16", 1080, 1920, "center"), ("4x5", 1080, 1350, "blur"), ("1280x720", 1280, 720, "center"), ("800x600", 800, 600, "center")]
    assert F.parse_formats(["21:9"])[0].width == 1920 and F.parse_formats(["2:1"])[0].height == 1080
    for bad in ([], ["16:9", "1920x1080"], ["potato"], [{"reframe": "blur"}], ["16:9"] * 7, [{"aspect": "9:16", "reframe": "wobble"}], ["10x10"]):
        with pytest.raises(LumiereError) as err:
            F.parse_formats(bad)
        assert err.value.code == "bad_format"
    with pytest.raises(LumiereError):
        F.parse_formats(["16:9"], "wobble")


def test_variant_project_adapts_a_copy_and_never_the_original(services):
    from prosperos_hoard.editing.timeline import Reframe

    doc = store.create(services, "V", preset="hd720")
    from prosperos_hoard.editing.ops import apply_ops
    from prosperos_hoard.editing.timeline import load

    p = load(store.doc(services, doc["id"]).dump())
    p, _ = apply_ops(p, [{"op": "add_media", "media": "m1"}, {"op": "add_media", "media": "m2", "track": p.tracks[0].id}], LOOK)
    before = json.dumps(p.dump(), sort_keys=True)
    horizontal, vertical = p.tracks[0].clips
    nine = F.parse_formats(["9:16"])[0]
    v, notes = F.variant_project(services, p, nine, LOOK)
    assert json.dumps(p.dump(), sort_keys=True) == before                      # the project is untouched
    assert (v.canvas.width, v.canvas.height) == (1080, 1920) and (p.canvas.width, p.canvas.height) == (1280, 720)
    h, vv = v.tracks[0].clips
    assert h.transform.fit == "cover" and h.reframe is None and h.transform.focus_x == 0.5     # centre: nothing to follow yet
    assert vv.transform.fit == "cover" and notes["clips"] == 2 and notes["centered"] == 1
    # an existing camera path is reused
    horizontal.reframe = Reframe(path=[[0, 0.2, 0.5], [6000, 0.8, 0.5]])
    v, notes = F.variant_project(services, p, nine, LOOK)
    assert v.tracks[0].clips[0].reframe.path == [[0, 0.2, 0.5], [6000, 0.8, 0.5]] and notes["paths_reused"] == 1
    # centre drops it, blur keeps the whole picture
    v, _ = F.variant_project(services, p, F.parse_formats(["9:16"], "center")[0], LOOK)
    assert v.tracks[0].clips[0].reframe is None
    v, notes = F.variant_project(services, p, F.parse_formats(["1:1"], "blur")[0], LOOK)
    assert notes["blurred"] == 2 and [c.transform.fit for c in v.tracks[0].clips] == ["blur", "blur"]
    # the project's own shape is rendered as it is
    now = json.dumps(p.dump(), sort_keys=True)
    same, notes = F.variant_project(services, p, F.parse_formats(["16:9"])[0], LOOK)
    assert notes["same_shape"] and json.dumps(same.dump(), sort_keys=True) == now
    # a picture in picture that was placed by hand is left alone
    p, _ = apply_ops(p, [{"op": "track_add", "kind": "video", "name": "PiP"}], LOOK)
    pip_track = p.tracks[-1] if p.tracks[-1].name == "PiP" else next(t for t in p.tracks if t.name == "PiP")
    p, _ = apply_ops(p, [{"op": "add_media", "media": "m2", "track": pip_track.id}], LOOK)
    pip = next(t for t in p.tracks if t.name == "PiP").clips[0]
    pip.transform.scale, pip.transform.x = 0.4, 0.3
    v, _ = F.variant_project(services, p, nine, LOOK)
    kept = next(t for t in v.tracks if t.name == "PiP").clips[0]
    assert (kept.transform.scale, kept.transform.x, kept.transform.fit) == (0.4, 0.3, pip.transform.fit)


# ---------------------------------------------------------------- the job

@needs_ffmpeg
def test_one_job_makes_three_files_with_their_own_sizes_and_checks(services, lib, monkeypatch):
    pid = store.create(services, "Multi", preset="hd720", media=[lib["talk"]])["id"]
    store.edit(services, pid, [{"op": "add_text", "text": "Hola", "start": 200, "length": 900}])
    snapshot = store.history(services, pid)
    rev, doc = store.view(services, pid)["rev"], json.dumps(store.doc(services, pid).dump(), sort_keys=True)
    calls = {"master": 0, "loud": 0}
    real_master, real_loud = media_store.audio_master, audio_an.loudnorm_measure
    monkeypatch.setattr(media_store, "audio_master", lambda *a, **k: calls.__setitem__("master", calls["master"] + 1) or real_master(*a, **k))
    monkeypatch.setattr(audio_an, "loudnorm_measure", lambda *a, **k: calls.__setitem__("loud", calls["loud"] + 1) or real_loud(*a, **k))
    job = run(services, pid, preset="final", end=1500, formats=["16:9", "9:16", "1:1"], filename="clip")
    out = job["result"]
    assert [o["variant"] for o in out["outputs"]] == ["16x9", "9x16", "1x1"] and out["formats"] == ["16x9", "9x16", "1x1"]
    paths = [Path(o["path"]) for o in out["outputs"]]
    assert [p.name for p in paths] == ["clip-16x9.mp4", "clip-9x16.mp4", "clip-1x1.mp4"] and all(p.exists() for p in paths)
    assert [sizes(p) for p in paths] == [(1920, 1080), (1080, 1920), (1080, 1080)]
    for o, p in zip(out["outputs"], paths):
        assert abs(duration_ms(p) - 1500) <= 70 and abs(o["duration_ms"] - 1500) == 0
        assert o["qc"]["ok"], o["qc"]                                                # a quality check of its own
        assert (o["width"], o["height"]) == sizes(p) and o["qc"]["width"] == o["width"] and "integrated_lufs" in o["qc"]
    assert out["ok"] and out["path"] == out["outputs"][0]["path"]               # the first output answers like a plain export
    # the sound was made once for the three
    assert calls == {"master": 1, "loud": 1}
    # renders are listed one by one, each with its variant, and each file can be fetched
    listed = services.db.query("SELECT * FROM renders WHERE project_id = ? ORDER BY variant", (pid,))
    assert sorted(r["variant"] for r in listed) == ["16x9", "1x1", "9x16"] and len({r["id"] for r in listed}) == 3
    from prosperos_hoard.editing.render import runner

    assert sorted(r["variant"] for r in runner.renders_list(services, pid)) == ["16x9", "1x1", "9x16"]
    # the project did not change at all
    assert store.view(services, pid)["rev"] == rev and json.dumps(store.doc(services, pid).dump(), sort_keys=True) == doc
    assert store.history(services, pid) == snapshot
    assert store.doc(services, pid).canvas.width == 1280
    # every output fires its own event
    done = [d for t, d in services._emit.events if t == "lumiere.render.done"]
    assert sorted(d["variant"] for d in done) == ["16x9", "1x1", "9x16"] and all(d["path"] and d["duration_ms"] == 1500 and d["ok"] for d in done)


@needs_ffmpeg
def test_each_output_has_its_own_framing_and_the_same_shape_one_matches_a_plain_export(services, lib, tmp_path):
    pid = store.create(services, "Marcos", preset="hd720", media=[lib["talk"]])["id"]
    plain = run(services, pid, preset="preview", end=1200, filename="plain")["result"]
    centre = run(services, pid, preset="preview", end=1200, formats=["16:9", "9:16"], filename="c")["result"]["outputs"]
    blur = run(services, pid, preset="preview", end=1200, formats=[{"aspect": "9:16", "reframe": "blur"}], filename="b")["result"]["outputs"]
    assert [sizes(o["path"]) for o in centre] == [(960, 540), (540, 960)] and sizes(blur[0]["path"]) == (540, 960)
    assert centre[1]["framing"]["centered"] == 1 and blur[0]["framing"]["blurred"] == 1 and blur[0]["reframe"] == "blur"
    from PIL import ImageChops, ImageStat

    d = tmp_path / "f"
    d.mkdir()
    # the project's own shape is rendered exactly as a plain export would
    same = ImageChops.difference(frame(plain["path"], 0.5, d / "a.png"), frame(centre[0]["path"], 0.5, d / "b.png"))
    assert sum(ImageStat.Stat(same).mean) / 3 < 0.5
    # the whole picture in the blurred version shows the picture's own left and right edges, the cropped one does not
    c_img, b_img = frame(centre[1]["path"], 0.5, d / "c.png"), frame(blur[0]["path"], 0.5, d / "d.png")
    assert sum(ImageStat.Stat(ImageChops.difference(c_img, b_img)).mean) / 3 > 8
    top = b_img.crop((0, 0, b_img.size[0], int(b_img.size[1] * 0.12)))
    assert sum(ImageStat.Stat(top).mean) / 3 > 12          # a blurred fill, not black bars


@needs_ffmpeg
def test_reframe_paths_the_clip_already_has_are_used_per_output(services, lib, tmp_path):
    pid = store.create(services, "Camino", preset="hd720", media=[lib["talk"]])["id"]
    clip_id = store.doc(services, pid).main_track().clips[0].id
    left = run(services, pid, preset="preview", end=1000, formats=["9:16"], filename="centre")["result"]["outputs"][0]
    store.edit(services, pid, [{"op": "set", "clip": clip_id, "props": {"reframe": {"path": [[0, 0.1, 0.5], [12000, 0.1, 0.5]]}}}])
    path_one = run(services, pid, preset="preview", end=1000, formats=["9:16"], filename="path")["result"]["outputs"][0]
    assert path_one["framing"]["paths_reused"] == 1
    forced = run(services, pid, preset="preview", end=1000, formats=["9:16"], reframe="center", filename="forced")["result"]["outputs"][0]
    from PIL import ImageChops, ImageStat

    d = tmp_path / "f"
    d.mkdir()
    a, b, c = (frame(o["path"], 0.4, d / f"{i}.png") for i, o in enumerate((left, path_one, forced)))
    diff = lambda x, y: sum(ImageStat.Stat(ImageChops.difference(x, y)).mean) / 3  # noqa: E731
    assert diff(a, b) > 8 and diff(a, c) < 0.5          # the path moves the crop to the left; 'center' ignores it
    assert store.doc(services, pid).main_track().clips[0].reframe is not None      # and the project keeps its path


@needs_ffmpeg
def test_formats_reject_what_cannot_work(services, lib):
    pid = store.create(services, "Mal", preset="hd720", media=[lib["talk"]])["id"]
    for kw in ({"preset": "audio_mp3", "formats": ["16:9"]}, {"formats": ["potato"]}, {"formats": ["16:9", "1920x1080"]},
               {"mode": "copy", "formats": ["16:9"]}, {"reframe": "wobble", "formats": ["16:9"]}):
        with pytest.raises(LumiereError):
            services.start_render(pid, **kw)
    assert services.db.one("SELECT COUNT(*) c FROM jobs WHERE kind = 'render'")["c"] == 0


@needs_ffmpeg
def test_tool_and_http_route_start_a_multi_format_render(client, media_dir):
    svc = client.svc
    mid = client.post("/api/media/import", json={"path": str(media_dir / "talk.mp4")}).json()["id"]
    pid = client.post("/api/projects", json={"name": "Web", "preset": "hd720", "media": [mid]}).json()["id"]
    res = agent_tools.call_tool(svc, "render_start", {"project": pid, "preset": "preview", "end": 1000, "formats": ["9:16", {"aspect": "1:1", "reframe": "blur"}]})
    assert [o["key"] for o in res["outputs"]] == ["9x16", "1x1"] and res["outputs"][1]["reframe"] == "blur"
    done = client.get(f"/api/jobs/{res['job']}").json()
    assert done["state"] == "done" and "2 formatos" in done["label"] and done["params"]["formats"][0]["width"] == 1080
    job = client.post(f"/api/projects/{pid}/render", json={"preset": "preview", "end": 1000, "formats": ["4:5"], "reframe": "center"}).json()
    assert client.get(f"/api/jobs/{job['id']}").json()["result"]["outputs"][0]["variant"] == "4x5"
    renders = client.get("/api/renders", params={"project": pid}).json()["renders"]
    assert {r["variant"] for r in renders} == {"9x16", "1x1", "4x5"} and all(client.get(r["url"]).status_code == 200 for r in renders)
    presets = client.get("/api/presets").json()
    assert {"id": "9:16", "width": 1080, "height": 1920} in presets["multi_formats"] and presets["reframe_modes"] == ["auto", "center", "blur"]


@needs_ffmpeg
def test_progress_names_each_output(services, lib, monkeypatch):
    from prosperos_hoard.editing.jobs import JobCtx

    pid = store.create(services, "Progreso", preset="hd720", media=[lib["talk"]])["id"]
    seen = []
    real = JobCtx.progress
    monkeypatch.setattr(JobCtx, "progress", lambda self, value, detail=None, force=False: (seen.append((value, detail)), real(self, value, detail, force))[1])
    run(services, pid, preset="preview", end=1200, formats=["16:9", "9:16"])
    details = [d for _, d in seen if d]
    assert any(d.startswith("16:9 · 1920×1080 (1/2) · picture") for d in details) and any(d.startswith("9:16 · 1080×1920 (2/2) · picture") for d in details), details
    values = [v for v, _ in seen]
    assert values == sorted(values) or max(values) <= 1      # progress never leaves 0..1


def test_plans_understand_several_formats_and_translated_subtitles(services):
    from prosperos_hoard.editing import plan as plan_mod

    pid = store.create(services, "Plan", preset="hd720")["id"]
    steps = plan_mod.create(services, pid, "Exporta en 16:9 y 9:16 y 1:1 con los subtítulos traducidos al inglés, bilingües", use_model=False)["steps"]
    assert [(s["kind"], s["name"]) for s in steps] == [("command", "captions"), ("export", "final")]     # no reframe step: the project stays as it is
    assert steps[1]["args"] == {"formats": ["16:9", "9:16", "1:1"], "captions_language": "en", "captions_dual": True}
    steps = plan_mod.create(services, pid, "Quiero todos los formatos, sin recortar", use_model=False)["steps"]
    assert steps[-1]["args"] == {"formats": ["16:9", "9:16", "1:1"], "reframe": "blur"}
    steps = plan_mod.create(services, pid, "hazlo vertical y exporta", use_model=False)["steps"]
    assert [s["name"] for s in steps] == ["reframe", "final"] and steps[1]["args"] == {}
    assert "formats?:" in plan_mod.SYSTEM and "captions_language?" in plan_mod.SYSTEM
