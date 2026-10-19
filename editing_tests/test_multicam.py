"""Multicam: sync by sound, the group on the timeline, switching angles, automatic switching by who speaks, renders and text edits.

Two synthetic cameras film the same two-person conversation. Camera A (red picture) has the first speaker loud on its own
microphone, camera B (blue picture) the second one; B started recording 1.37 s after A.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import fake_transcript, make_services, needs_ffmpeg
from synth import SR, camera_audio, two_person_event, video_with_audio, write_wav
from prosperos_hoard.editing import commands, multicam
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.render import runner

pytestmark = needs_ffmpeg

OFFSET_MS = 1370
PLAN = [(0.6, 5.8, "A"), (6.4, 11.6, "B"), (12.2, 15.8, "A"), (16.4, 20.0, "B"), (20.6, 24.0, "A")]


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    d = tmp_path_factory.mktemp("cams")
    a, b, plan = two_person_event()
    assert plan == PLAN
    cam_a = camera_audio(a, b, own_gain=1.0, other_gain=0.12, noise=0.003, seed=1)
    cam_b = camera_audio(b, a, own_gain=0.8, other_gain=0.1, noise=0.006, seed=2)
    start_b = int(OFFSET_MS / 1000 * SR)
    seg_a = cam_a[: 24 * SR]
    seg_b = cam_b[start_b: start_b + 24 * SR]
    write_wav(d / "a.wav", seg_a)
    write_wav(d / "b.wav", seg_b)
    video_with_audio(d / "camA.mp4", d / "a.wav", "red", 24.0)
    video_with_audio(d / "camB.mp4", d / "b.wav", "blue", 24.0)
    return d


@pytest.fixture(scope="module")
def svc(tmp_path_factory, rig):
    s = make_services(tmp_path_factory.mktemp("lib"))
    s.start()
    s.cam_a = media_store.import_path(s, str(rig / "camA.mp4"))["id"]
    s.cam_b = media_store.import_path(s, str(rig / "camB.mp4"))["id"]
    yield s
    s.stop()


def _group_project(svc, name="Dos cámaras"):
    pid = store.create(svc, name, preset="hd720")["id"]
    res = commands.run(svc, pid, "multicam_create", {"media": [svc.cam_a, svc.cam_b], "name": "Charla"})
    return pid, res


def _shots(svc, pid):
    p = store.doc(svc, pid)
    g = p.multicams[0]
    return [(c.start, c.end, g.angle_index(c.media) + 1) for c in sorted(p.main_track().clips, key=lambda c: c.start) if c.multicam]


def _angle_at(shots, t):
    return next(a for s, e, a in shots if s <= t < e)


def _dominant(path: Path) -> str:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    r, g, b = (float(np.asarray(img)[..., i].mean()) for i in range(3))
    if r > 120 and r > 1.8 * b:
        return "red"
    if b > 120 and b > 1.8 * r:
        return "blue"
    return f"other({r:.0f},{g:.0f},{b:.0f})"


# ------------------------------------------------------------------ sync

def test_sync_finds_the_known_offset_within_a_frame(svc):
    res = multicam.sync(svc, [svc.cam_a, svc.cam_b])
    a, b = res["angles"]
    assert a["start_ms"] == 0
    assert abs(b["start_ms"] - OFFSET_MS) < 15, b            # well inside one 25 fps frame (40 ms)
    assert b["confidence"] > 0.35 and b["reliable"] and res["notes"] == []
    flipped = multicam.sync(svc, [svc.cam_b, svc.cam_a])      # the other camera as the reference: same spacing, other sign
    fa, fb = flipped["angles"]
    assert fa["start_ms"] == pytest.approx(OFFSET_MS, abs=15) and fb["start_ms"] == 0


def test_manual_offsets_override_the_measurement(svc):
    res = multicam.sync(svc, [svc.cam_a, svc.cam_b], offsets={svc.cam_b: 2000})
    assert res["angles"][1]["start_ms"] == 2000 and res["angles"][1]["confidence"] is None and res["angles"][1]["manual"]
    with pytest.raises(Exception):
        multicam.sync(svc, [svc.cam_a, svc.cam_b], offsets={"med_nope": 1})
    with pytest.raises(LumiereError):
        multicam.sync(svc, [svc.cam_a])


# ------------------------------------------------------------------ the group on the timeline

def test_group_is_plain_clips_over_one_master_sound(svc):
    pid, res = _group_project(svc)
    assert res["summary"]["weakest_confidence"] > 0.35
    p = store.doc(svc, pid)
    g = p.multicams[0]
    assert [a.media for a in g.angles] == [svc.cam_a, svc.cam_b] and abs(g.angles[1].start - OFFSET_MS) < 15
    main = p.main_track().clips
    assert len(main) == 1 and main[0].multicam == g.id and main[0].mute and main[0].media == svc.cam_a
    audio = [t for t in p.tracks if t.kind == "audio" and t.clips][0]
    assert len(audio.clips) == 1 and audio.clips[0].media == svc.cam_a and not audio.clips[0].mute and audio.role == "voice"
    # the group covers where both cameras have material: from B's start to the end of A
    assert main[0].start == 0 and abs(main[0].duration - (24000 - g.angles[1].start)) < 2
    assert audio.clips[0].duration == main[0].duration
    out = store.outline(svc, pid)
    assert out["multicams"][0]["angles"][1]["label"] == "Cámara B" and any(c.get("multicam") for t in out["tracks"] for c in t["clips"])
    assert multicam.view(p)["groups"][0]["shot_count"] == 1


def test_switch_angle_at_the_playhead_for_a_range_and_undo(svc):
    pid, _ = _group_project(svc)
    p0 = store.doc(svc, pid)
    store.edit(svc, pid, [{"op": "multicam_switch", "at": 4000, "angle": 2}])           # until the end of the shot
    shots = _shots(svc, pid)
    assert [(s, a) for s, _e, a in shots] == [(0, 1), (4000, 2)] and shots[-1][1] == p0.main_track().clips[0].end
    store.edit(svc, pid, [{"op": "multicam_switch", "at": 8000, "end": 9000, "angle": "Cámara A"}])   # a range, by label
    shots = _shots(svc, pid)
    assert [(s, e, a) for s, e, a in shots][1:] == [(4000, 8000, 2), (8000, 9000, 1), (9000, shots[-1][1], 2)]
    # the same moment of the event: group time = source time + angle start
    p = store.doc(svc, pid)
    g = p.multicams[0]
    for c in p.main_track().clips:
        a = g.angles[g.angle_index(c.media)]
        assert abs((c.src_in + a.start) - (c.start + g.angles[1].start)) <= 2
    # switching to the angle already shown leaves no seam; the sound is one untouched clip
    store.edit(svc, pid, [{"op": "multicam_switch", "at": 8000, "end": 9000, "angle": "2"}])
    assert [a for _s, _e, a in _shots(svc, pid)] == [1, 2]
    sound = [c for t in store.doc(svc, pid).tracks if t.kind == "audio" for c in t.clips]
    assert len(sound) == 1 and sound[0].src_in == p0.tracks[-1].clips[0].src_in
    with pytest.raises(LumiereError) as e:
        store.edit(svc, pid, [{"op": "multicam_switch", "at": 1000, "angle": "Cámara Z"}])
    assert "Angles:" in str(e.value)
    store.undo(svc, pid)
    store.undo(svc, pid)
    store.undo(svc, pid)
    assert [(s, a) for s, _e, a in _shots(svc, pid)] == [(0, 1)]


def test_switch_many_at_once_and_by_clip(svc):
    pid, _ = _group_project(svc)
    store.edit(svc, pid, [{"op": "multicam_switch", "cuts": [[0, 1], [3000, 2], [6000, 1], [9000, "Cámara B"]]}])
    assert [(s, a) for s, _e, a in _shots(svc, pid)] == [(0, 1), (3000, 2), (6000, 1), (9000, 2)]
    clip = [c for c in store.doc(svc, pid).main_track().clips if c.start == 3000][0]
    store.edit(svc, pid, [{"op": "multicam_switch", "clip": clip.id, "angle": 1}])
    assert [(s, a) for s, _e, a in _shots(svc, pid)] == [(0, 1), (9000, 2)]


def test_offsets_by_hand_move_the_clips_and_release_dissolves(svc):
    pid, _ = _group_project(svc)
    store.edit(svc, pid, [{"op": "multicam_switch", "at": 5000, "angle": 2}])
    p = store.doc(svc, pid)
    before = [c for c in p.main_track().clips if c.start == 5000][0]
    start_b = p.multicams[0].angles[1].start
    store.edit(svc, pid, [{"op": "multicam_set", "offsets": {"Cámara B": start_b + 100}}])
    p = store.doc(svc, pid)
    after = [c for c in p.main_track().clips if c.start == 5000][0]
    assert after.src_in == before.src_in - 100 and p.multicams[0].angles[1].start == start_b + 100 and p.multicams[0].angles[1].confidence is None
    store.edit(svc, pid, [{"op": "multicam_set", "release": True}])
    p = store.doc(svc, pid)
    assert not p.multicams and all(c.multicam is None for _, c in p.all_clips())


def test_resync_command_measures_again(svc):
    pid, _ = _group_project(svc)
    store.edit(svc, pid, [{"op": "multicam_set", "offsets": {"Cámara B": 5000}}])
    res = commands.run(svc, pid, "multicam_resync", {})
    g = store.doc(svc, pid).multicams[0]
    assert abs(g.angles[1].start - OFFSET_MS) < 15 and res["summary"]["moved"]


def test_ops_guard_the_group(svc):
    pid, _ = _group_project(svc)
    clip = store.doc(svc, pid).main_track().clips[0]
    with pytest.raises(LumiereError):
        store.edit(svc, pid, [{"op": "replace_media", "clip": clip.id, "media": svc.cam_b}])
    with pytest.raises(LumiereError):
        store.edit(svc, pid, [{"op": "multicam_create", "angles": [{"media": svc.cam_a}]}])
    # deleting everything leaves no orphan group
    store.edit(svc, pid, [{"op": "delete", "clips": [c.id for _, c in store.doc(svc, pid).all_clips()]}])
    assert store.doc(svc, pid).multicams == []


# ------------------------------------------------------------------ automatic switching

def test_auto_switching_follows_who_talks(svc):
    pid, _ = _group_project(svc)
    res = commands.run(svc, pid, "multicam_auto", {})
    shots = _shots(svc, pid)
    # one shot per turn: A, B, A, B, A (timeline time = event time - 1.37 s)
    assert [a for _s, _e, a in shots] == [1, 2, 1, 2, 1], shots
    for t, expect in ((2000, 1), (8000, 2), (12500, 1), (17000, 2), (21000, 1)):
        assert _angle_at(shots, t) == expect
    # cuts land just before the new voice (lead 150 ms), not in the middle of a sentence
    for (s, _e, _a), turn_start in zip(shots[1:], (6400, 12200, 16400, 20600)):
        assert turn_start - 1370 - 400 <= s <= turn_start - 1370 + 100, (s, turn_start)
    assert min(e - s for s, e, _a in shots) >= 2000 and res["summary"]["shots"] == 5
    # a long minimum shot length keeps the picture still; hysteresis keeps interruptions from flickering
    commands.run(svc, pid, "multicam_auto", {"min_shot_ms": 6000})
    assert all(e - s >= 4500 for s, e, _a in _shots(svc, pid)[:-1])
    store.undo(svc, pid)
    commands.run(svc, pid, "multicam_auto", {"hysteresis_db": 60})
    assert len(_shots(svc, pid)) == 1


def test_auto_switching_can_use_the_speakers_of_the_transcript(svc):
    pid, _ = _group_project(svc)
    words = []
    for t0, t1, who in PLAN:
        t = int(t0 * 1000)
        while t < t1 * 1000 - 400:
            words.append((t, t + 300, f"p{len(words)}"))
            t += 380
    fake_transcript(svc, svc.cam_a, words)
    t = media_store.get_analysis(svc, svc.cam_a, "transcript")
    t["speakers"] = {"S1": {"name": "Ana", "color": "#4FC3F7"}, "S2": {"name": "Luis", "color": "#FFB74D"}}
    spans = [(int(a * 1000), int(b * 1000), "S1" if w == "A" else "S2") for a, b, w in PLAN]
    for w in t["words"]:
        w["speaker"] = next(s for a, b, s in spans if a <= w["t0"] < b)
    media_store.put_analysis(svc, svc.cam_a, "transcript", t)
    res = commands.run(svc, pid, "multicam_auto", {"mode": "speakers"})
    assert res["summary"]["speaker_angles"] == {"Ana": "Cámara A", "Luis": "Cámara B"}
    assert [a for _s, _e, a in _shots(svc, pid)] == [1, 2, 1, 2, 1]
    # the mapping can be given instead of measured: Ana filmed by camera B
    commands.run(svc, pid, "multicam_auto", {"mode": "speakers", "speaker_map": {"Ana": "Cámara B", "Luis": "Cámara A"}})
    assert [a for _s, _e, a in _shots(svc, pid)] == [2, 1, 2, 1, 2]
    with pytest.raises(LumiereError):
        commands.run(svc, pid, "multicam_auto", {"mode": "nonsense"})


# ------------------------------------------------------------------ render

def _decode(path: Path) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True, check=True)
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _level_db(x: np.ndarray, hop: int = 1600) -> np.ndarray:
    n = x.size // hop
    return 10 * np.log10(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-10)


def test_render_shows_the_right_angle_and_the_sound_never_breaks(svc):
    pid, _ = _group_project(svc)
    commands.run(svc, pid, "multicam_auto", {})
    shots = _shots(svc, pid)
    expected = {1: "red", 2: "blue"}
    for t in (2000, 8000, 12500, 17000, 21000):
        frame = runner.render_frame(svc, pid, t, width=160)
        assert _dominant(frame) == expected[_angle_at(shots, t)], (t, _dominant(frame))
    job = svc.start_render(pid, preset="preview")
    res = svc.jobs.get(job["id"])
    assert res["state"] == "done", res["error"]
    out = Path(res["result"]["path"])
    # frames of the exported file too
    for t in (2.0, 8.0, 17.0):
        png = out.parent / f"mc-{t}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1", str(png)], check=True)
        assert _dominant(png) == expected[_angle_at(shots, int(t * 1000))]
    # the sound is camera A's recording from the group start on, with no gap at any cut
    heard = _decode(out)
    p = store.doc(svc, pid)
    a_audio = _decode(Path(media_store.get(svc, svc.cam_a)["path"]))[int(1.37 * SR): int(1.37 * SR) + heard.size]
    n = min(heard.size, a_audio.size)
    la, lh = _level_db(a_audio[:n]), _level_db(heard[:n])
    assert abs(n - p.duration * 16) < 1600 * 3
    assert np.mean(np.abs((lh - np.median(lh)) - (la - np.median(la)))) < 4.0
    for s, _e, _a in shots[1:]:  # around every cut the exported sound keeps its level
        i = int(s / 100)
        assert lh[max(0, i - 1): i + 2].max() > np.median(lh) - 25


def test_text_cut_moves_picture_and_sound_together_and_captions_read_the_master(svc):
    pid, _ = _group_project(svc)
    commands.run(svc, pid, "multicam_auto", {})
    fake_transcript(svc, svc.cam_a, [(1500, 1900, "uno"), (2000, 2400, "dos"), (2500, 2900, "tres"), (9000, 9400, "cuatro"), (9500, 9900, "cinco.")])
    tt = commands.timeline_transcript(svc, store.doc(svc, pid))
    assert [w["text"] for w in tt["words"]] == ["uno", "dos", "tres", "cuatro", "cinco."]
    before = store.doc(svc, pid).duration
    commands.run(svc, pid, "cut_words", {"media": svc.cam_a, "text": "dos tres"})
    p = store.doc(svc, pid)
    audio = [t for t in p.tracks if t.kind == "audio" and t.clips][0]
    picture = p.main_track()
    assert p.duration < before - 400
    assert audio.clips[-1].end == picture.clips[-1].end and sum(c.duration for c in audio.clips) == sum(c.duration for c in picture.clips)
    assert [w["text"] for w in commands.timeline_transcript(svc, p)["words"]] == ["uno", "cuatro", "cinco."]
    from prosperos_hoard.editing.render import ass

    store.edit(svc, pid, [{"op": "captions", "enabled": True, "style": "clean"}])
    cues = ass.caption_cues(store.doc(svc, pid), lambda mid: (media_store.get_analysis(svc, mid, "transcript") or {}).get("words"))
    assert " ".join(c[2] for c in cues) == "uno cuatro cinco."
    store.undo(svc, pid)
    store.undo(svc, pid)
    assert store.doc(svc, pid).duration == before


# ------------------------------------------------------------------ MCP tools

def test_every_multicam_step_is_reachable_through_the_tools(svc):
    from prosperos_hoard.editing import agent_tools

    call = lambda tool, **args: agent_tools.call_tool(svc, tool, args)  # noqa: E731
    sync = call("multicam_sync", media=[svc.cam_a, svc.cam_b])
    assert abs(sync["angles"][1]["start_ms"] - OFFSET_MS) < 15
    pid = call("project_create", name="Entrevista", preset="hd720")["id"]
    made = call("multicam_create", project=pid, media=[svc.cam_a, svc.cam_b], name="Charla")
    assert made["summary"]["weakest_confidence"] > 0.35
    got = call("multicam_get", project=pid)
    assert got["groups"][0]["shot_count"] == 1 and [a["label"] for a in got["groups"][0]["angles"]] == ["Cámara A", "Cámara B"]
    call("multicam_switch", project=pid, at="0:04", angle="Cámara B")
    assert call("multicam_get", project=pid)["groups"][0]["shot_count"] == 2
    auto = call("multicam_auto", project=pid)
    assert auto["summary"]["shots"] == 5
    out = call("multicam_get", project=pid)["groups"][0]
    assert [s["angle"] for s in out["shots"]] == [1, 2, 1, 2, 1]
    call("timeline_history", project=pid, action="undo")
    assert call("multicam_get", project=pid)["groups"][0]["shot_count"] == 2
    with pytest.raises(Exception):
        call("multicam_switch", project=pid, at=1000, angle="Cámara Z")


def test_http_routes_serve_the_multicam_ui(svc):
    from fastapi.testclient import TestClient

    from prosperos_hoard.editing.main import create_app

    with TestClient(create_app(svc.config, svc), base_url="http://127.0.0.1") as c:
        r = c.post("/api/multicam/sync", json={"media": [svc.cam_a, svc.cam_b]})
        assert r.status_code == 200 and abs(r.json()["angles"][1]["start_ms"] - OFFSET_MS) < 15
        pid = c.post("/api/projects", json={"name": "HTTP", "preset": "hd720"}).json()["id"]
        made = c.post(f"/api/projects/{pid}/command", json={"command": "multicam_create", "args": {"media": [svc.cam_a, svc.cam_b]}}).json()
        assert made["done"] and made["summary"]["weakest_confidence"] > 0.35
        view = c.get(f"/api/projects/{pid}/multicam").json()["groups"][0]
        assert view["shot_count"] == 1
        # the thumbnails are server-made JPEGs of the same moment of the event: A is red, B is blue
        for angle, colour in ((1, "red"), (2, "blue")):
            img = c.get(f"/api/projects/{pid}/multicam/frame", params={"angle": angle, "t": 3000, "width": 96})
            assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
            path = svc.config.work_dir / f"thumb{angle}.jpg"
            path.write_bytes(img.content)
            assert _dominant(path) == colour
        assert c.get(f"/api/media/{svc.cam_b}/frame", params={"t": 1000}).status_code == 200
        cut = c.post(f"/api/projects/{pid}/edit", json={"ops": [{"op": "multicam_switch", "at": 5000, "angle": 2}]})
        assert cut.status_code == 200
        assert c.get(f"/api/projects/{pid}/multicam").json()["groups"][0]["shot_count"] == 2
        assert c.get(f"/api/projects/{pid}/multicam/frame", params={"angle": "Cámara Z"}).status_code == 400
        assert c.get(f"/api/media/{svc.cam_a}/speakers").json()["diarized"] is False
