"""Sound for HTML animations: roles, placement by measured sync point, mastering, export with sound, sync points of imported audio."""
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import FakeLink, make_config, make_services, needs_ffmpeg, probe
from prosperos_hoard.editing import audio_sync, loudness, media as media_store, motion_sound
from prosperos_hoard.editing import projects as project_store
from prosperos_hoard.editing.agent_tools import call_tool, tool_catalog
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.foley import wavio
from prosperos_hoard.editing.html_motion import HtmlMotion, browser_executable
from prosperos_hoard.editing.main import create_app
from prosperos_hoard.editing.ops import apply_ops
from prosperos_hoard.editing.services import Services
from prosperos_hoard.editing.timeline import new_project

BROWSER = pytest.mark.skipif(importlib.util.find_spec("playwright") is None or not browser_executable(), reason="optional motion runtime and system browser not installed")
SOURCE = ("<!doctype html><meta charset='utf-8'><canvas id=c width=320 height=180></canvas>"
          "<script>window.seek=t=>{const x=c.getContext('2d');x.fillStyle=(Math.floor(t*4)%2)?'#e8e0c8':'#26323a';x.fillRect(0,0,320,180);};seek(0);</script>")
SPACED = [{"t": 0.5, "role": "tap"}, {"t": 1.1, "role": "golpe"}, {"t": 1.7, "role": "clic"}, {"t": 2.3, "role": "whoosh"}, {"t": 3.0, "role": "hit"}]
OFFSETS = {"land.light": -60, "tock": -40, "click": -11, "whoosh.short": 37, "hit": 0}


@pytest.fixture(scope="session")
def sound_home(tmp_path_factory):
    """Takes are rendered once for the whole session: a take costs a second or more of DSP."""
    return tmp_path_factory.mktemp("foley")


def ffmpeg_loudness(path: Path) -> dict:
    out = subprocess.run(["ffmpeg", "-nostats", "-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True).stderr
    tail = out[out.rfind("Summary:"):]
    return {"lufs": float(re.search(r"I:\s+(-?[\d.]+) LUFS", tail).group(1)), "tp": float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", tail).group(1))}


def decode_wav(path: Path) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", "48000", "-f", "f32le", "pipe:1"], capture_output=True).stdout
    return np.frombuffer(out, np.float32).reshape(-1, 2).astype(np.float64)


# ------------------------------------------------------------------------------------------------ the declaration
def test_roles_resolve_with_aliases_and_off_roles_say_why():
    assert motion_sound.canonical_role("tap") == "land.light"
    assert motion_sound.canonical_role("GOLPE") == "tock"
    assert motion_sound.canonical_role("whoosh") == "whoosh.short"
    assert motion_sound.canonical_role("whoosh.long") == "whoosh.long"
    assert motion_sound.canonical_role("whoosh_largo") == "whoosh.long"
    with pytest.raises(LumiereError) as off:
        motion_sound.canonical_role("suck")
    assert off.value.code == "sound_role_off"
    with pytest.raises(LumiereError) as unknown:
        motion_sound.canonical_role("explosion")
    assert unknown.value.code == "sound_role" and "land.light" in str(unknown.value)


def test_normalise_sorts_validates_and_keeps_nothing_empty():
    sound = motion_sound.normalise({"events": [{"t": 2, "role": "hit"}, {"t": 1, "role": "tap", "gain_db": -3}]}, 3)
    assert [e["role"] for e in sound["events"]] == ["land.light", "hit"] and sound["events"][0]["gain_db"] == -3
    assert sound["lufs"] == -14 and sound["true_peak_db"] == -1 and sound["kit"] == "launch"
    assert motion_sound.normalise({}) is None and motion_sound.normalise(None) is None
    with pytest.raises(LumiereError) as late:
        motion_sound.normalise({"events": [{"t": 5, "role": "tap"}]}, 3)
    assert late.value.code == "sound_event_time"
    with pytest.raises(LumiereError):
        motion_sound.normalise({"events": [{"t": 1, "role": "tap"}], "music": {}}, 3)
    with pytest.raises(LumiereError):
        motion_sound.normalise({"kit": "nope", "events": [{"t": 1, "role": "tap"}]}, 3)
    with pytest.raises(LumiereError):
        motion_sound.normalise({"events": [{"t": 1, "role": "bell", "pitch": "H9"}]}, 3)


def test_the_html_can_declare_its_sound(tmp_path):
    html = SOURCE + "<script type='application/json' id='lumiere-sound'>{\"events\": [{\"t\": 0.5, \"role\": \"clic\"}]}</script>"
    assert motion_sound.declared_in_html(html)["events"][0]["role"] == "clic"
    store = HtmlMotion(tmp_path)
    saved = store.save(html=html, width=320, height=180, fps=10, duration=1)
    assert saved["sound"]["events"] == [{"t": 0.5, "role": "click"}]
    again = store.save(composition=saved["id"], expected_revision=1, html=html.replace("#26323a", "#222222").replace(
        "id='lumiere-sound'>", "id='lumiere-sound'> "), width=320, height=180, fps=10, duration=1)
    assert again["sound"]["events"][0]["role"] == "click"
    plain = store.save(composition=saved["id"], expected_revision=2, html=SOURCE, width=320, height=180, fps=10, duration=1)
    assert plain["sound"]["events"][0]["role"] == "click", "an edit of the picture keeps the sound it had"
    cleared = store.save(composition=saved["id"], expected_revision=3, html=SOURCE, sound={}, width=320, height=180, fps=10, duration=1)
    assert "sound" not in cleared
    assert "sound" in store.restore(saved["id"], 1, 4)


def test_sound_events_tool_changes_revisions_and_refuses_stale_ones(tmp_path):
    svc = make_services(tmp_path)
    store = HtmlMotion(svc.config.data_dir)
    saved = store.save(html=SOURCE, width=320, height=180, fps=10, duration=3)
    cid = saved["id"]
    assert call_tool(svc, "motion_sound_events", {"composition": cid})["sound"] is None
    with pytest.raises(LumiereError):  # expected_revision is how a stale writer is caught
        call_tool(svc, "motion_sound_events", {"composition": cid, "action": "set", "events": [{"t": 1, "role": "tap"}]})
    done = call_tool(svc, "motion_sound_events", {"composition": cid, "action": "set", "expected_revision": 1, "events": [{"t": 1, "role": "tap"}, {"t": 2, "role": "golpe"}]})
    assert done["revision"] == 2 and done["events"] == 2
    with pytest.raises(LumiereError):
        call_tool(svc, "motion_sound_events", {"composition": cid, "action": "add", "expected_revision": 1, "events": [{"t": 1.5, "role": "clic"}]})
    added = call_tool(svc, "motion_sound_events", {"composition": cid, "action": "add", "expected_revision": 2, "events": [{"t": 1.5, "role": "clic"}], "lufs": -16})
    assert added["sound"]["lufs"] == -16 and [e["t"] for e in added["sound"]["events"]] == [1, 1.5, 2]
    removed = call_tool(svc, "motion_sound_events", {"composition": cid, "action": "remove", "expected_revision": 3, "role": "tap"})
    assert [e["role"] for e in removed["sound"]["events"]] == ["click", "tock"]
    muted = call_tool(svc, "motion_sound_events", {"composition": cid, "action": "get", "expected_revision": 4, "audio": False})
    assert muted["sound"]["audio"] is False and muted["revision"] == 5
    cleared = call_tool(svc, "motion_sound_events", {"composition": cid, "action": "clear", "expected_revision": 5})
    assert cleared["sound"] is None and cleared["events"] == 0
    assert store.get(cid)["revision"] == 6 and store.source(store.get(cid, 1)) == SOURCE
    roles = call_tool(svc, "motion_sound_roles", {})
    by_role = {r["role"]: r for r in roles["roles"]}
    assert by_role["land.light"]["offset_ms"] == -60 and by_role["tock"]["offset_ms"] == -40
    assert by_role["click"]["offset_ms"] == -11 and by_role["whoosh.short"]["offset_ms"] == 37 and by_role["whoosh.short"]["sync_point"] == "peak"
    assert by_role["suck"]["sounded"] is False
    names = {t["name"] for t in tool_catalog()}
    assert {"motion_export", "motion_sound_events", "motion_sound_roles", "motion_sound_preview", "audio_sync_points"} <= names
    svc.stop()


# ------------------------------------------------------------------------------------------------ loudness
def test_loudness_agrees_with_ffmpeg_and_the_limiter_keeps_its_ceiling(tmp_path):
    rng = np.random.default_rng(3)
    n = 48000 * 6
    t = np.arange(n) / 48000
    x = 0.05 * np.sin(2 * np.pi * 220 * t) + 0.02 * rng.standard_normal(n)
    for at in (1.0, 2.5, 4.0):
        i = int(at * 48000)
        x[i:i + 2400] += 0.3 * np.hanning(2400) * np.sin(2 * np.pi * 1800 * t[:2400])
    stereo = np.stack([x, 0.8 * x], 1)
    path = tmp_path / "probe.wav"
    wavio.write(str(path), stereo.astype(np.float32), 48000, subtype="FLOAT")
    theirs = ffmpeg_loudness(path)
    assert abs(loudness.integrated_lufs(stereo) - theirs["lufs"]) < 0.15
    assert abs(loudness.true_peak_db(stereo) - theirs["tp"]) < 0.35
    out, info = loudness.master(stereo, -14.0, -1.0)
    assert info["reached"] and abs(info["lufs"] + 14) < 0.5 and info["true_peak_db"] <= -1.0
    wavio.write(str(path), out, 48000, subtype="FLOAT")
    check = ffmpeg_loudness(path)
    assert abs(check["lufs"] + 14) < 0.6 and check["tp"] <= -0.95
    spiky = rng.standard_normal((48000, 2)) * 0.02
    spiky[1000:1010] = 1.6
    assert loudness.true_peak_db(loudness.limit(spiky, -1.0)) <= -0.9, 'the 4x look-ahead is within 0.1 dB of the 8x measure'
    assert loudness.true_peak_db(loudness.limit(spiky, -1.1)) <= -1.0, 'master() keeps that 0.1 dB margin'


def test_sparse_effects_keep_the_limiter_gentle_and_say_they_missed_the_target():
    x = np.zeros((48000 * 4, 2))
    for at in (0.5, 1.5, 2.5):
        i = int(at * 48000)
        x[i:i + 800] += 0.5 * np.hanning(800)[:, None] * np.sin(np.arange(800) * .3)[:, None]
    out, info = loudness.master(x, -14.0, -1.0)
    assert info["true_peak_db"] <= -1.0 and info["limiter_db"] <= 8.01 and info["reached"] is False


# ------------------------------------------------------------------------------------------------ placement by sync point
def test_library_renders_only_the_roles_in_use_and_keeps_them(sound_home):
    library = motion_sound.Library(sound_home)
    lock = library.ensure({"land.light", "click"})
    assert len(lock["roles"]["land.light"]["takes"]) == 3 and len(lock["roles"]["click"]["takes"]) == 2
    assert lock["roles"]["hit"]["takes"] == [] and lock["roles"]["land.light"]["align"] == "onset"
    take = lock["roles"]["click"]["takes"][0]
    assert {"onset_s", "peak_s", "end_s", "sha1", "key"} <= set(take) and take["onset_s"] <= take["peak_s"] <= take["end_s"]
    stamp = (library.out / take["file"]).stat().st_mtime_ns
    assert library.ensure({"click"})["roles"]["click"]["takes"][0]["sha1"] == take["sha1"]
    assert (library.out / take["file"]).stat().st_mtime_ns == stamp, "a cached take is not rendered again"
    assert wavio.read(str(library.out / take["file"]), always_2d=True)[0].shape[1] in (1, 2)


def test_effects_land_on_their_sync_point_within_a_millisecond(sound_home):
    sound = motion_sound.normalise({"events": SPACED}, 4)
    built = motion_sound.build_mix(motion_sound.Library(sound_home), sound, 4.0)
    for placement in built["placements"]:
        assert placement["sync_s"] == pytest.approx(placement["t"] + OFFSETS[placement["role"]] / 1000, abs=1e-6)
    measured = motion_sound.measure_sync(built["effects"], built["placements"])
    assert len([r for r in measured if r["error_ms"] is not None]) == 5
    assert max(abs(r["error_ms"]) for r in measured) < 1.5
    assert {p["sync_point"] for p in built["placements"]} == {"onset", "peak"}
    repeated = motion_sound.build_mix(motion_sound.Library(sound_home), motion_sound.normalise({"events": [{"t": .5, "role": "pop"}, {"t": 1.5, "role": "pop"}]}, 3), 3.0)
    assert repeated["placements"][0]["take"] != repeated["placements"][1]["take"], "a run of one role takes its takes in turn"


def test_music_steps_down_before_a_hit_and_effects_follow_the_bed(sound_home):
    n = 48000 * 4
    music = np.stack([0.1 * np.sin(2 * np.pi * 330 * np.arange(n) / 48000)] * 2, 1)
    sound = motion_sound.normalise({"events": [{"t": 2.5, "role": "hit"}, {"t": 1.0, "role": "pop"}], "music": {"path": "x.wav"}}, 4)
    built = motion_sound.build_mix(motion_sound.Library(sound_home), sound, 4.0, music)
    assert built["music"]["stop_down"] == [2.5]
    quiet = np.abs(built["mix"][int(2.2 * 48000):int(2.4 * 48000)]).max()
    loud = np.abs(built["mix"][int(1.5 * 48000):int(1.7 * 48000)]).max()
    assert quiet < loud * 0.5, "the bed is lower just before the hit than elsewhere"
    assert built["placements"][0]["role"] == "pop" or built["placements"][1]["role"] == "pop"


# ------------------------------------------------------------------------------------------------ sync points of any audio
def test_sync_points_of_a_sound_with_a_slow_start():
    sr = 48000
    y = np.zeros(sr * 2)
    t = np.arange(int(0.5 * sr)) / sr
    y[int(0.4 * sr):int(0.9 * sr)] = np.sin(2 * np.pi * 400 * t) * np.sin(np.pi * t / 0.5) ** 2  # swell: loudest at 0.65 s
    found = audio_sync.measure(y)
    assert 400 <= found["onset_ms"] <= 560 and 600 <= found["peak_ms"] <= 700 and 850 <= found["end_ms"] <= 920
    assert audio_sync.measure(np.zeros(1000))["silent"] is True


def test_add_media_align_puts_the_sync_point_on_the_requested_time():
    song = {"id": "m9", "name": "whoosh", "kind": "audio", "duration_ms": 3000, "width": 0, "height": 0, "fps": 0, "has_audio": True, "has_video": False,
            "sync": {"onset_ms": 100, "peak_ms": 400, "end_ms": 900}}
    look = lambda mid: {"m9": song}.get(mid)
    project, results = apply_ops(new_project(1280, 720, 30), [{"op": "add_media", "media": "m9", "at": 2000, "align": "peak"}], look)
    clip = next(c for _, c in project.all_clips())
    assert clip.start == 1600 and clip.src_in == 0 and results[0]["sync"] == {"point": "peak", "at": 2000}
    project, _ = apply_ops(new_project(1280, 720, 30), [{"op": "add_media", "media": "m9", "at": 250, "align": "peak"}], look)
    clip = next(c for _, c in project.all_clips())
    assert clip.start == 0 and clip.src_in == 150, "the part before the timeline is skipped"
    for bad, code in (({"op": "add_media", "media": "m9", "align": "peak"}, "align_needs_at"),
                      ({"op": "add_media", "media": "m9", "at": 100, "align": "end", "src_out": 600}, "sync_out_of_range")):
        with pytest.raises(LumiereError) as error:
            apply_ops(new_project(1280, 720, 30), [bad], look)
        assert error.value.code == code or {"align_needs_at": "needs `at`", "sync_out_of_range": "outside the part used"}[code] in str(error.value)
    plain = {**song, "sync": None}
    with pytest.raises(LumiereError) as error:
        apply_ops(new_project(1280, 720, 30), [{"op": "add_media", "media": "m9", "at": 100, "align": "onset"}], lambda mid: plain)
    assert error.value.code == "no_sync_points" or "no sync points" in str(error.value)


@needs_ffmpeg
def test_imported_audio_gets_sync_points_and_edit_aligns_by_them(tmp_path):
    svc = make_services(tmp_path)
    sr = 48000
    y = np.zeros(sr * 2)
    y[int(0.5 * sr):int(0.6 * sr)] = np.sin(2 * np.pi * 900 * np.arange(int(0.1 * sr)) / sr)
    source = tmp_path / "late hit.wav"
    wavio.write(str(source), y.astype(np.float32), sr, subtype="PCM_16")
    info = media_store.import_path(svc, str(source))
    mid = info["id"]
    svc.jobs.wait(info["job"]["id"], 30) if isinstance(info.get("job"), dict) else None
    points = call_tool(svc, "audio_sync_points", {"media": mid})
    assert 495 <= points["onset_ms"] <= 520 and points["end_ms"] >= 580 and "align" in points["use"]
    assert call_tool(svc, "audio_sync_points", {"path": str(source)})["onset_ms"] == points["onset_ms"]
    pid = project_store.create(svc, "sync", preset="hd720")["id"]
    result = project_store.edit(svc, pid, [{"op": "add_media", "media": mid, "at": 4000, "align": "onset"}])
    assert result["results"][0]["start"] == 4000 - points["onset_ms"] and result["results"][0]["sync"]["at"] == 4000
    job = call_tool(svc, "media_analyze", {"media": mid, "kinds": ["sync"], "force": True})
    assert job["jobs"][0]["kind"] == "sync"
    svc.stop()


# ------------------------------------------------------------------------------------------------ export with sound
def make_music(path: Path, seconds: float = 4.0):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=220:d={seconds}", "-f", "lavfi", "-i", f"sine=f=277:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=330:d={seconds}", "-filter_complex", "amix=inputs=3:normalize=0,volume=0.4", "-ar", "48000", "-ac", "2", str(path)], check=True)


def export(svc, store, saved, **kwargs):
    queued = store.queue_render(svc, saved["id"], **kwargs)
    job = svc.jobs.get(queued["job"])
    assert job["state"] == "done", job["error"]
    return job["result"]


@BROWSER
@needs_ffmpeg
def test_export_with_effects_puts_every_effect_on_its_event(tmp_path):
    svc = make_services(tmp_path)
    store = HtmlMotion(svc.config.data_dir)
    saved = store.save(html=SOURCE, name="Effects", width=320, height=180, fps=10, duration=4, sound={"events": SPACED})
    assert saved["sound"]["events"][0]["role"] == "land.light"
    result = export(svc, store, saved)
    assert result["audio"] is True and result["codec"] == "h264"
    streams = {s["codec_type"]: s for s in probe(Path(result["render_path"]))["streams"]}
    assert streams["video"]["codec_name"] == "h264" and streams["audio"]["codec_name"] == "aac" and int(streams["video"]["nb_frames"]) == 40
    assert abs(float(streams["audio"]["duration"]) - 4.0) < 0.1
    report = result["sound_report"]
    assert report["events"] == 5 and report["true_peak_db"] <= -1.0
    delivered = ffmpeg_loudness(Path(result["render_path"]))
    assert delivered["tp"] <= -0.95, "the delivered file keeps the true peak under -1 dBTP after AAC"
    decoded = decode_wav(Path(result["render_path"]))
    measured = motion_sound.measure_sync(decoded, report["placements"])
    errors = [abs(r["error_ms"]) for r in measured if r["error_ms"] is not None]
    assert len(errors) == 5 and max(errors) <= 10, measured
    svc.stop()


@BROWSER
@needs_ffmpeg
def test_export_with_music_reaches_minus_14_lufs_and_can_be_silenced(tmp_path):
    svc = make_services(tmp_path)
    store = HtmlMotion(svc.config.data_dir)
    music = tmp_path / "bed.wav"
    make_music(music)
    imported = media_store.import_path(svc, str(music))["id"]
    sound = {"events": [{"t": .5, "role": "pop"}, {"t": 1.5, "role": "tap"}, {"t": 2.5, "role": "hit"}, {"t": 3.2, "role": "whoosh"}], "music": {"media": imported}}
    saved = store.save(html=SOURCE, name="With music", width=320, height=180, fps=10, duration=4, sound=sound)
    result = export(svc, store, saved)
    report = result["sound_report"]
    assert report["reached"] and abs(report["lufs"] + 14) < 0.5 and report["music"]["stop_down"] == [2.5]
    delivered = ffmpeg_loudness(Path(result["render_path"]))
    assert abs(delivered["lufs"] + 14) < 0.7 and delivered["tp"] <= -0.95, delivered
    silent = export(svc, store, saved, audio=False)
    assert silent["audio"] is False and all(s["codec_type"] != "audio" for s in probe(Path(silent["render_path"]))["streams"])
    with TestClient(create_app(svc.config, services=svc), base_url="http://127.0.0.1") as client:
        again = client.post(f"/api/html-motion/{saved['id']}/render?audio=false").json()
        assert again["audio"] is False
        svc.jobs.wait(again["job"], 120)
        assert client.get(f"/api/html-motion/{saved['id']}").json()["revision"] == 1
    svc.stop()


@BROWSER
@needs_ffmpeg
def test_preview_gives_a_playable_mix_with_the_measured_sync(tmp_path):
    svc = make_services(tmp_path)
    saved = HtmlMotion(svc.config.data_dir).save(html=SOURCE, name="Preview", width=320, height=180, fps=10, duration=4, sound={"events": SPACED})
    with TestClient(create_app(svc.config, services=svc), base_url="http://127.0.0.1") as client:
        preview = client.post(f"/api/html-motion/{saved['id']}/sound/preview").json()
        assert preview["max_sync_error_ms"] < 1.5 and len(preview["placements"]) == 5 and preview["true_peak_db"] <= -1.0
        audio = client.get(preview["url"])
        assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
        roles = client.get("/api/html-motion/sound/roles").json()
        assert any(r["role"] == "land.light" for r in roles["roles"])
        events = client.post(f"/api/html-motion/{saved['id']}/sound", json={"composition": saved["id"], "action": "add", "expected_revision": 1,
                                                                          "events": [{"t": 3.6, "role": "bell", "pitch": "C5"}]}).json()
        assert events["revision"] == 2 and events["events"] == 6
    svc.stop()
