"""Background music picker: analysis of synthetic click tracks at known tempos and lengths, ranking against fast and slow edits,
and the command that puts the chosen track under the edit (checked on the rendered sound)."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from conftest import make_services, needs_ffmpeg
from prosperos_hoard.editing import commands, music
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.agent_tools import call_tool
from prosperos_hoard.editing.analysis import audio as audio_an
from prosperos_hoard.editing.errors import LumiereError, Refused


def make_clicks(path: Path, bpm: float, seconds: float) -> None:
    gap = 60.0 / bpm
    expr = f"if(lt(mod(t,{gap:.5f}),0.02),sin(2*PI*1000*t)*0.9,0)"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"aevalsrc='{expr}':s=48000:d={seconds}",
                    "-c:a", "pcm_s16le", str(path)], check=True)


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    folder = tmp_path_factory.mktemp("music")
    if not (subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode == 0):
        pytest.skip("ffmpeg is not installed")
    make_clicks(folder / "slow_90bpm_45s.wav", 90, 45)
    make_clicks(folder / "mid_120bpm_45s.wav", 120, 45)
    make_clicks(folder / "fast_140bpm_45s.wav", 140, 45)
    make_clicks(folder / "mid_120bpm_8s.wav", 120, 8)
    (folder / "notes.txt").write_text("not audio")
    (folder / "broken.mp3").write_bytes(b"this is not an mp3 at all" * 40)
    return folder


def edit(svc, media_id, clip_ms, count, src_step=2000):
    """A project of ``count`` clips of ``clip_ms`` taken from the 12 s talk: its pace is count-1 cuts over count*clip_ms."""
    pid = store.create(svc, "Montaje", preset="hd720")["id"]
    ops = []
    for i in range(count):
        a = (i * src_step) % (12000 - clip_ms)
        ops.append({"op": "add_media", "media": media_id, "src_in": a, "src_out": a + clip_ms})
    store.edit(svc, pid, ops)
    return pid


@pytest.fixture
def lab(tmp_path, media_dir, library):
    svc = make_services(tmp_path)
    svc.start()
    talk = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
    yield svc, talk, library
    svc.stop()


# ---------------------------------------------------------------- analysis and ranking

@needs_ffmpeg
def test_tracks_are_analysed_for_tempo_length_and_energy(lab):
    svc, _, folder = lab
    for name, bpm, secs in (("slow_90bpm_45s.wav", 90, 45), ("mid_120bpm_45s.wav", 120, 45), ("fast_140bpm_45s.wav", 140, 45)):
        t = music.analyze_track(svc, folder / name)
        assert abs(t["bpm"] - bpm) < 4 and abs(t["duration_ms"] - secs * 1000) < 60, (name, t["bpm"])
        assert t["steadiness"] > 0.8 and t["beat_count"] > secs * bpm / 60 * 0.8
        assert 0 <= t["energy"] <= 1 and t["lufs"] is not None and t["first_beat_ms"] < 700
    assert music._cache_path(svc, folder / "mid_120bpm_45s.wav").exists()


@needs_ffmpeg
def test_a_fast_edit_gets_the_fast_track_and_a_slow_edit_the_slow_one(lab):
    svc, talk, folder = lab
    fast = edit(svc, talk, 2000, 20)      # 19 cuts in 40 s = 28.5 cuts a minute -> about 117 bpm
    res = music.suggest(svc, fast, str(folder), count=10)
    assert res["edit"]["cuts_per_min"] == 28.5 and res["edit"]["target_bpm"] == 117 and res["edit"]["duration_ms"] == 40000
    names = [t["name"] for t in res["tracks"]]
    by_name = {t["name"]: t for t in res["tracks"]}
    assert names[0] == "mid_120bpm_45s" and names[-1] == "mid_120bpm_8s"  # the right tempo, but it ends 32 s early
    assert by_name["mid_120bpm_45s"]["fit"]["tempo"] > by_name["fast_140bpm_45s"]["fit"]["tempo"] > by_name["slow_90bpm_45s"]["fit"]["tempo"]
    best = res["tracks"][0]
    assert best["rank"] == 1 and best["score"] > res["tracks"][1]["score"] > 0 and any(r["code"] == "tempo" for r in best["reasons"])
    assert any(r["code"] == "long_enough" for r in best["reasons"])
    short = next(t for t in res["tracks"] if t["name"] == "mid_120bpm_8s")
    assert any(w["code"] == "too_short" and w["short_ms"] > 30000 for w in short["warnings"])
    assert {s["file"] for s in res["skipped"]} == {"broken.mp3"}  # a file that is not audio is reported, the folder still works
    assert res["analysed"] == 4 and res["from_cache"] == 0
    slow = edit(svc, talk, 10000, 4)      # 3 cuts in 40 s = 4.5 a minute -> about 86 bpm
    res = music.suggest(svc, slow, str(folder), count=3)
    assert res["edit"]["cuts_per_min"] == 4.5 and res["tracks"][0]["name"] == "slow_90bpm_45s"
    assert any(w["code"] == "tempo_far" for w in next(t for t in music.suggest(svc, slow, str(folder), count=10)["tracks"] if t["name"] == "fast_140bpm_45s")["warnings"])
    assert music.suggest(svc, slow, str(folder))["from_cache"] == 4  # the second look at a folder is instant


def test_ranking_logic_on_plain_numbers():
    base = {"duration_ms": 60000, "bpm": 120.0, "steadiness": 0.95, "energy": 0.55, "lufs": -16.0, "beats": [i * 500 for i in range(100)], "first_beat_ms": 0}
    profile = {"duration_ms": 30000, "cuts_per_min": 24.0, "cut_times": [i * 2500 for i in range(1, 12)]}
    good = music.rate(base, profile)
    assert good["score"] > 0.8 and good["fit"]["sync"] == 1.0 and any(r["code"] == "sync" for r in good["reasons"])
    half = music.rate({**base, "bpm": 60.0}, profile)          # half time still carries the cuts
    wrong = music.rate({**base, "bpm": 175.0, "energy": 0.95}, profile)
    assert half["fit"]["tempo"] > wrong["fit"]["tempo"] + 0.3 and good["score"] > half["score"] > wrong["score"]
    shorty = music.rate({**base, "duration_ms": 12000}, profile)
    assert shorty["fit"]["length"] < 0.4 and shorty["warnings"][0]["code"] == "too_short"
    unknown = music.rate(base, {**profile, "cuts_per_min": None, "cut_times": []})
    assert unknown["fit"]["tempo"] == 0.5 and unknown["warnings"][0]["code"] == "pace_unknown"
    assert music.target_bpm(0) == 80 and music.target_bpm(100) == music.target_bpm(45)


@needs_ffmpeg
def test_folder_and_project_problems_are_explained(lab, tmp_path):
    svc, talk, folder = lab
    empty = store.create(svc, "Vacío", preset="hd720")["id"]
    with pytest.raises(LumiereError, match="project is empty"):
        music.suggest(svc, empty, str(folder))
    pid = edit(svc, talk, 2000, 5)
    with pytest.raises(Exception, match="no folder"):
        music.suggest(svc, pid, str(tmp_path / "nope"))
    (tmp_path / "silent").mkdir()
    with pytest.raises(LumiereError, match="No audio files"):
        music.suggest(svc, pid, str(tmp_path / "silent"))


@needs_ffmpeg
def test_the_folder_must_be_inside_the_allowed_roots(tmp_path, media_dir, library):
    svc = make_services(tmp_path / "app", file_roots=(media_dir,))
    svc.start()
    try:
        talk = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
        pid = edit(svc, talk, 2000, 5)
        with pytest.raises(Refused):
            music.suggest(svc, pid, str(library))
    finally:
        svc.stop()


# ---------------------------------------------------------------- putting it under the edit

def _peak(svc, path: Path, t0: float, t1: float) -> float:
    tools = svc.tools()
    pcm = np.concatenate(list(audio_an.stream_pcm(tools, path)))
    seg = pcm[int(t0 * audio_an.RATE): int(t1 * audio_an.RATE)]
    return float(np.abs(seg).max() / 32768.0) if seg.size else 0.0


@needs_ffmpeg
def test_music_add_trims_fades_ducks_and_is_heard_in_the_render(lab):
    svc, talk, folder = lab
    pid = edit(svc, talk, 2000, 20)
    store.edit(svc, pid, [{"op": "track_set", "track": next(t.id for t in store.doc(svc, pid).tracks if t.role == "music"), "props": {"duck": False}}])
    out = commands.run(svc, pid, "music_add", {"path": str(folder / "mid_120bpm_45s.wav")})
    s = out["summary"]
    p = store.doc(svc, pid)
    track = next(t for t in p.tracks if t.role == "music")
    c = track.clips[0]
    assert (c.start, c.end) == (0, 40000) and c.src_out - c.src_in == 40000 and c.src_in < 700  # starts on its first beat, as long as the edit
    assert c.audio_fade_in == 600 and c.audio_fade_out == 2500
    assert track.duck is True and track.volume_db == -8 and s["ducking"] and s["ends_early_ms"] == 0 and abs(s["bpm"] - 120) < 4
    # the track is now in the library (nothing copied), and adding another replaces it instead of piling up
    assert media_store.get(svc, c.media)["path"].endswith("mid_120bpm_45s.wav")
    commands.run(svc, pid, "music_add", {"path": str(folder / "slow_90bpm_45s.wav"), "volume_db": -12, "duck": False})
    track = next(t for t in store.doc(svc, pid).tracks if t.role == "music")
    assert len(track.clips) == 1 and track.volume_db == -12 and track.duck is False and track.clips[0].media != c.media
    # a shorter track ends early and says so
    commands.run(svc, pid, "music_add", {"path": str(folder / "mid_120bpm_8s.wav")})
    assert commands.run(svc, pid, "music_add", {"path": str(folder / "mid_120bpm_8s.wav")})["summary"]["ends_early_ms"] > 30000
    # the sound: the talk is silent at 3-5 s of the source, so the clicks are the only thing heard around 3.2-3.8 s of the edit (1.2-1.8 s of a render of 2-6 s)
    commands.run(svc, pid, "music_add", {"path": str(folder / "mid_120bpm_45s.wav")})
    job = svc.start_render(pid, preset="web", start=2000, end=6000)
    res = svc.jobs.get(job["id"])
    assert res["state"] == "done", res["error"]
    out_path = Path(res["result"]["path"])
    assert _peak(svc, out_path, 1.2, 1.8) > 0.05
    store.undo(svc, pid)
    store.undo(svc, pid)
    store.undo(svc, pid)
    store.undo(svc, pid)
    store.undo(svc, pid)
    job = svc.start_render(pid, preset="web", start=2000, end=6000)
    bare = svc.jobs.get(job["id"])
    assert bare["state"] == "done" and _peak(svc, Path(bare["result"]["path"]), 1.2, 1.8) < 0.01


@needs_ffmpeg
def test_music_add_rejects_what_cannot_be_music(lab, media_dir):
    svc, talk, folder = lab
    pid = edit(svc, talk, 2000, 5)
    with pytest.raises(LumiereError, match="Give the audio file"):
        commands.run(svc, pid, "music_add", {})
    pic = media_store.import_path(svc, str(media_dir / "pic.png"))["id"]
    with pytest.raises(LumiereError, match="no sound"):
        commands.run(svc, pid, "music_add", {"media": pic})
    empty = store.create(svc, "Vacío", preset="hd720")["id"]
    with pytest.raises(LumiereError, match="project is empty"):
        commands.run(svc, empty, "music_add", {"path": str(folder / "mid_120bpm_45s.wav")})


@needs_ffmpeg
def test_the_tool_ranks_and_adds_the_best_in_one_call(lab):
    svc, talk, folder = lab
    pid = edit(svc, talk, 2000, 20)
    out = call_tool(svc, "music_pick", {"project": pid, "folder": str(folder), "count": 2})
    assert [t["rank"] for t in out["tracks"]] == [1, 2] and "added" not in out
    assert not [c for t in store.doc(svc, pid).tracks if t.role == "music" for c in t.clips]
    out = call_tool(svc, "music_pick", {"project": pid, "folder": str(folder), "add": "best", "volume_db": -10})
    assert out["added"]["name"] == out["tracks"][0]["name"] and out["added"]["volume_db"] == -10
    assert store.history(svc, pid)["items"][0]["label"] == "Música de fondo"
