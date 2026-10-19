from pathlib import Path

import pytest

from conftest import fake_transcript, nb_frames, needs_ffmpeg, probe
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.render import runner

pytestmark = needs_ffmpeg


@pytest.fixture
def lib(services, media_dir):
    ids = {}
    for name in ("talk.mp4", "vert.mp4", "pic.png", "clicks.wav", "scenes.mp4"):
        m = media_store.import_path(services, str(media_dir / name))
        ids[name.split(".")[0]] = m["id"]
    return ids


def test_import_prepares_proxy_sprite_and_waveform(services, lib):
    talk = media_store.get(services, lib["talk"])
    assert talk["kind"] == "video" and talk["duration_ms"] == 12000 and talk["proxy"] == "ready"
    assert talk["urls"]["sprite"] and talk["urls"]["waveform"]
    wave = (services.config.cache_dir / lib["talk"] / "wave.bin").read_bytes()
    assert 1150 <= len(wave) <= 1210
    assert media_store.get(services, lib["pic"])["kind"] == "image"
    assert media_store.get(services, lib["clicks"])["kind"] == "audio"
    again = media_store.import_path(services, talk["path"])
    assert again["existing"] and again["id"] == lib["talk"]


def _render(services, pid, **kw):
    job = services.start_render(pid, **kw)
    job = services.jobs.get(job["id"])
    assert job["state"] == "done", job["error"]
    return job["result"]


def test_final_render_is_frame_exact_with_sound_and_loudness(services, lib):
    p = store.create(services, "Prueba", preset="hd720", media=[lib["talk"], lib["vert"], lib["pic"]])
    pid = p["id"]
    main = store.doc(services, pid).main_track()
    second = sorted(main.clips, key=lambda c: c.start)[1]
    store.edit(services, pid, [{"op": "transition", "clip": second.id, "type": "crossfade", "dur": 500},
                               {"op": "add_text", "text": "Hola", "start": 500, "length": 2000},
                               {"op": "add_media", "media": lib["clicks"], "at": 0, "src_out": 8000}])
    doc = store.doc(services, pid)
    res = _render(services, pid, preset="final")
    out = Path(res["path"])
    assert out.exists() and res["qc"]["ok"], res["qc"]
    assert abs(res["qc"]["duration_ms"] - doc.duration) <= 70
    assert nb_frames(out) == round(doc.duration * 30 / 1000)
    info = probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1280, 720)
    assert abs(res["qc"]["integrated_lufs"] - (-14)) < 1.5
    assert services.db.one("SELECT COUNT(*) c FROM renders")["c"] == 1


def test_range_export_preview_gif_and_audio(services, lib):
    pid = store.create(services, "Rango", preset="hd720", media=[lib["talk"]])["id"]
    res = _render(services, pid, preset="preview", start=2000, end=6000)
    assert abs(res["duration_ms"] - 4000) < 50 and res["height"] == 540
    gif = _render(services, pid, preset="gif", start=0, end=1500)
    assert Path(gif["path"]).suffix == ".gif"
    mp3 = _render(services, pid, preset="audio_mp3")
    assert Path(mp3["path"]).suffix == ".mp3" and res["qc"]["ok"]


def test_vertical_reframe_render_and_frame_snapshot(services, lib):
    pid = store.create(services, "Vertical", preset="reels", media=[lib["talk"]])["id"]
    clip = store.doc(services, pid).main_track().clips[0]
    store.edit(services, pid, [{"op": "set", "clip": clip.id, "props": {"transform": {"fit": "cover"},
                                                                         "reframe": {"path": [[0, 0.2, 0.5], [6000, 0.8, 0.5], [12000, 0.5, 0.5]]}}}])
    frame = runner.render_frame(services, pid, 3000, width=360)
    from PIL import Image

    img = Image.open(frame)
    assert img.size == (360, 640)
    from prosperos_hoard.editing import agent_tools

    shown = agent_tools.call_tool(services, "frame_snapshot", {"project": pid, "t": "0:01", "width": 320})
    assert shown["_image"]["mime"] == "image/jpeg" and len(shown["_image"]["data"]) > 1000
    assert [layer["layer"] for layer in shown["shows"]] == ["main"]
    store.edit(services, pid, [{"op": "add_title", "text": "Hola", "at": 0, "duration": 2000}])
    shown = agent_tools.call_tool(services, "frame_snapshot", {"project": pid, "t": 1000, "width": 320, "show": False})
    assert [(layer["layer"], layer.get("text")) for layer in shown["shows"]] == [("text", "Hola"), ("main", None)]
    assert "_image" not in agent_tools.call_tool(services, "frame_snapshot", {"project": pid, "t": 1000, "width": 320, "show": False})
    res = _render(services, pid, preset="web", end=3000)
    assert (res["width"], res["height"]) == (720, 1280)


def test_captions_burn_and_subtitle_files(services, lib):
    pid = store.create(services, "Subs", preset="hd720", media=[lib["talk"]])["id"]
    fake_transcript(services, lib["talk"], [(100, 400, "Hola"), (450, 800, "qué"), (850, 1300, "tal."), (5200, 5600, "Vuelvo"), (5700, 6100, "ahora")])
    store.edit(services, pid, [{"op": "captions", "enabled": True, "style": "pop"}, {"op": "delete_range", "start": 3000, "end": 5000}])
    srt, _ = runner.subtitles_export(services, pid, "srt")
    assert "Hola qué tal." in srt and "00:00:03,200" in srt  # «Vuelvo» moved 2 s earlier with the cut
    vtt, _ = runner.subtitles_export(services, pid, "vtt")
    assert vtt.startswith("WEBVTT")
    res = _render(services, pid, preset="preview", end=2000, filename="Subtitulado.mp4")
    assert res["qc"]["ok"] and Path(res["path"]).name == "Subtitulado.mp4"  # the chosen name, without a preset tag


def test_copy_cut_lossless(services, lib):
    pid = store.create(services, "Copia", preset="hd720", media=[lib["talk"]])["id"]
    store.edit(services, pid, [{"op": "delete_range", "start": 2000, "end": 4000}])
    job = services.start_render(pid, mode="copy")
    job = services.jobs.get(job["id"])
    assert job["state"] == "done", job["error"]
    assert abs(job["result"]["duration_ms"] - 10000) < 1200
    store.edit(services, pid, [{"op": "add_text", "text": "x", "start": 0, "length": 500}])
    job = services.jobs.get(services.start_render(pid, mode="copy")["id"])
    assert job["state"] == "failed" and "titles" in job["error"]


def test_effects_keyframes_and_pip_render(services, lib):
    pid = store.create(services, "Efectos", preset="hd720", media=[lib["talk"]])["id"]
    r = store.edit(services, pid, [{"op": "track_add", "kind": "video", "name": "PiP"}])
    track = r["results"][0]["track"]
    r = store.edit(services, pid, [{"op": "add_media", "media": lib["vert"], "track": track, "at": 1000, "src_out": 3000}])
    cid = r["results"][0]["clip"]
    main_clip = store.doc(services, pid).main_track().clips[0]
    store.edit(services, pid, [
        {"op": "set", "clip": cid, "props": {"transform": {"scale": 0.35, "x": 0.3, "rotation": 8}, "fade_in": 300, "fade_out": 300, "volume_db": -6}},
        {"op": "keyframes", "clip": cid, "prop": "y", "keys": [{"t": 0, "v": -0.2}, {"t": 2000, "v": 0.2, "ease": "ease_in_out"}]},
        {"op": "keyframes", "clip": main_clip.id, "prop": "scale", "keys": [{"t": 0, "v": 1.0}, {"t": 4000, "v": 1.3}]},
        {"op": "filter_add", "clips": [main_clip.id], "type": "warm", "params": {"amount": 0.7}},
        {"op": "filter_add", "clips": [main_clip.id], "type": "voice_enhance"},
        {"op": "speed", "clip": main_clip.id, "speed": 1.5},
    ])
    res = _render(services, pid, preset="preview")
    assert res["qc"]["ok"], res["qc"]
    assert abs(res["duration_ms"] - 8000) < 70


def test_render_refuses_an_invalid_timeline(services, lib):
    pid = store.create(services, "Mal", preset="hd720", media=[lib["talk"], lib["vert"]])["id"]
    p = store.doc(services, pid)
    clips = sorted(p.main_track().clips, key=lambda c: c.start)
    clips[1].start = clips[0].start + 1000
    store.save(services, pid, p, "romper")
    job = services.jobs.get(services.start_render(pid)["id"])
    assert job["state"] == "failed" and "overlaps" in job["error"]


def test_undo_redo_and_history(services, lib):
    pid = store.create(services, "Historia", preset="hd720", media=[lib["talk"]])["id"]
    store.edit(services, pid, [{"op": "split", "at": 5000}])
    store.edit(services, pid, [{"op": "delete_range", "start": 0, "end": 1000}])
    assert store.doc(services, pid).duration == 11000
    store.undo(services, pid)
    assert store.doc(services, pid).duration == 12000
    store.redo(services, pid)
    assert store.doc(services, pid).duration == 11000
    h = store.history(services, pid)
    assert h["items"][0]["current"] and len(h["items"]) == 4
    store.restore(services, pid, 2)
    assert store.doc(services, pid).duration == 12000


def test_blur_fill_vertical_and_zoom_cuts(services, lib):
    from prosperos_hoard.editing import commands

    pid = store.create(services, "Blur", preset="reels", media=[lib["talk"]])["id"]
    store.edit(services, pid, [{"op": "split", "at": 3000}, {"op": "split", "at": 6000}])
    commands.run(services, pid, "reframe", {"aspect": "9:16", "mode": "blur"})
    commands.run(services, pid, "zoom_cuts", {"scale": 1.2})
    p = store.doc(services, pid)
    clips = sorted(p.main_track().clips, key=lambda c: c.start)
    assert all(c.transform.fit == "blur" for c in clips) and [c.transform.scale for c in clips] == [1.0, 1.2, 1.0]
    frame = runner.render_frame(services, pid, 1000, width=270)
    from PIL import Image

    img = Image.open(frame).convert("RGB")
    assert img.size == (270, 480)
    top = img.getpixel((135, 20))
    assert sum(top) > 30  # the blurred fill, not black bars
    res = _render(services, pid, preset="preview", end=4000)
    assert res["qc"]["ok"]


def test_project_get_for_assistants_is_small_and_puts_titles_first(services, lib):
    from prosperos_hoard.editing import agent_tools

    pid = store.create(services, "Muchos", preset="hd720", media=[lib["talk"]])["id"]
    store.edit(services, pid, [{"op": "split", "at": t} for t in range(1000, 12000, 1000)]
               + [{"op": "add_text", "text": "Título", "start": 0, "length": 2000, "style": {"position": "top"}}])
    out = agent_tools.call_tool(services, "project_get", {"project": pid, "max_clips": 5})
    assert out["tracks"][0]["kind"] == "text" and out["tracks"][0]["clips"][0]["style"]["position"] == "top"
    main = next(t for t in out["tracks"] if t["role"] == "main")
    assert main["clip_count"] == 12 and len(main["clips"]) == 5 and "7 more clips" in main["more"]
    window = agent_tools.call_tool(services, "project_get", {"project": pid, "track": "video", "start": "0:03", "end": "0:05"})
    assert [c["start_ms"] for c in window["tracks"][0]["clips"]] == [3000, 4000]
