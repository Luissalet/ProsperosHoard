import json

import pytest

from conftest import FakeLink, fake_transcript, make_services, needs_ffmpeg
from prosperos_hoard.editing import commands
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import plan as plan_mod
from prosperos_hoard.editing import projects as store

pytestmark = needs_ffmpeg


@pytest.fixture
def talk(services, media_dir):
    return media_store.import_path(services, str(media_dir / "talk.mp4"))["id"]


@pytest.fixture
def proj(services, talk):
    return store.create(services, "Charla", preset="hd720", media=[talk])["id"]


def test_remove_silences_cut_and_speed(services, talk, proj):
    res = commands.run(services, proj, "remove_silences", {}, preview=True)
    assert res["preview"] and res["summary"]["cuts"] == 2
    assert store.doc(services, proj).duration == 12000
    res = commands.run(services, proj, "remove_silences", {})
    d = store.doc(services, proj).duration
    assert 8000 < d < 9700 and res["summary"]["saved_ms"] == 12000 - d
    store.undo(services, proj)
    commands.run(services, proj, "remove_silences", {"mode": "speed", "speed": 4})
    p = store.doc(services, proj)
    assert any(c.speed == 4 for c in p.main_track().clips)
    assert 9000 < p.duration < 11000


def test_fillers_need_a_transcript_then_cut(services, talk, proj):
    with pytest.raises(commands.NeedsAnalysis) as need:
        commands.run(services, proj, "remove_fillers", {})
    job = need.value.jobs[0]
    assert job["kind"] == "transcript"
    fake_transcript(services, talk, [(100, 400, "Hola"), (500, 800, "eh,"), (900, 1200, "esto"), (1250, 1500, "es"), (1550, 1900, "una"),
                                     (1950, 2400, "prueba"), (2500, 2700, "o"), (2750, 3000, "sea")])
    res = commands.run(services, proj, "remove_fillers", {})
    assert res["summary"]["removed"] == 2
    words = commands.timeline_transcript(services, store.doc(services, proj))["words"]
    assert [w["text"] for w in words] == ["Hola", "esto", "es", "una", "prueba"]


def test_text_cut_phrase_and_keep(services, talk, proj):
    fake_transcript(services, talk, [(100, 400, "Uno"), (500, 800, "dos"), (900, 1200, "tres."), (5200, 5600, "Cuatro"), (5700, 6100, "cinco.")])
    commands.run(services, proj, "cut_words", {"media": talk, "text": "dos tres"})
    texts = [w["text"] for w in commands.timeline_transcript(services, store.doc(services, proj))["words"]]
    assert texts == ["Uno", "Cuatro", "cinco."]
    store.undo(services, proj)
    commands.run(services, proj, "cut_words", {"media": talk, "word_ids": ["w4", "w5"], "keep": True})
    p = store.doc(services, proj)
    assert p.duration < 1500
    with pytest.raises(Exception):
        commands.run(services, proj, "cut_words", {"media": talk, "text": "no existe"})


def test_reframe_to_vertical_uses_the_focus_track(services, talk, proj):
    media_store.put_analysis(services, talk, "focus", {"samples": [[t, 0.75, 0.5, 1.0, 1] for t in range(0, 12000, 333)], "faces": True})
    media_store.put_analysis(services, talk, "scenes", {"cuts": []})
    res = commands.run(services, proj, "reframe", {"aspect": "9:16"})
    p = store.doc(services, proj)
    c = p.main_track().clips[0]
    assert (p.canvas.width, p.canvas.height) == (1080, 1920)
    assert c.transform.fit == "cover" and c.reframe and abs(c.reframe.path[0][1] - 0.75) < 0.01
    assert res["summary"]["detector"] == "faces+saliency"


def test_reframe_without_focus_queues_the_analysis(services, talk, proj):
    res = None
    with pytest.raises(commands.NeedsAnalysis):
        res = commands.run(services, proj, "reframe", {"aspect": "1:1"})
    assert res is None
    assert media_store.get_analysis(services, talk, "focus") is not None  # inline jobs ran it
    res = commands.run(services, proj, "reframe", {"aspect": "1:1"})
    assert res["summary"]["canvas"] == "1080x1080"


def test_beat_sync_builds_cuts_on_the_beat(services, media_dir, talk, proj):
    clicks = media_store.import_path(services, str(media_dir / "clicks.wav"))["id"]
    with pytest.raises(commands.NeedsAnalysis):
        commands.run(services, proj, "beat_sync", {"music": clicks})
    res = commands.run(services, proj, "beat_sync", {"music": clicks, "beats_per_cut": 4, "mode": "clips"})
    p = store.doc(services, proj)
    lengths = [c.duration for c in sorted(p.main_track().clips, key=lambda c: c.start)]
    assert res["summary"]["cuts"] >= 4 and all(abs(x - 2000) < 80 for x in lengths)
    music = next(t for t in p.tracks if t.kind == "audio" and t.role == "music")
    assert music.clips and music.clips[0].media == clicks


def test_highlights_and_short(services, media_dir, talk):
    res = commands.highlights(services, talk, count=2, length_ms=3000, min_gap_ms=3000)
    assert len(res["highlights"]) == 2 and "sound" in res["signals"]
    hl = res["highlights"][0]
    out = commands.short_from_range(services, talk, hl["start_ms"], hl["end_ms"], name="Corto")
    p = store.doc(services, out["project"])
    assert (p.canvas.width, p.canvas.height) == (1080, 1920) and p.captions.enabled


def test_match_loudness_sets_gain(services, talk, proj):
    with pytest.raises(commands.NeedsAnalysis):
        commands.run(services, proj, "match_loudness", {"target_lufs": -16})
    res = commands.run(services, proj, "match_loudness", {"target_lufs": -16})
    assert res["summary"]["clips"] == 1
    assert store.doc(services, proj).main_track().clips[0].volume_db != 0


# ---------------------------------------------------------------- plans

def test_rules_plan_understands_spanish(services, talk, proj):
    plan = plan_mod.create(services, proj, "Recorta los primeros 2 segundos, quita los silencios, ponle subtítulos estilo karaoke, "
                                          "hazlo vertical, en blanco y negro y expórtalo en 720", use_model=False)
    names = [(s["kind"], s["name"]) for s in plan["steps"]]
    assert ("op", "delete_range") in names and ("command", "remove_silences") in names and ("command", "captions") in names
    assert ("command", "reframe") in names and ("op", "filter_add") in names and ("export", "web") in names
    assert plan["source"] == "rules"
    cap = next(s for s in plan["steps"] if s["name"] == "captions")
    assert cap["args"]["style"] == "karaoke"


def test_plan_apply_waits_for_analyses_and_queues_export(services, talk, proj):
    fake_transcript(services, talk, [(100, 400, "Hola"), (500, 800, "mundo.")])
    plan = plan_mod.create(services, proj, "quita los silencios y pon subtítulos, luego exporta", use_model=False)
    res = plan_mod.apply(services, plan["id"], wait_analysis_s=5)
    assert res["applied"] and res["renders"]
    p = store.doc(services, proj)
    assert p.captions.enabled and p.duration < 12000
    h = store.history(services, proj)
    assert h["items"][0]["label"].startswith("Plan:")
    job = services.jobs.get(res["renders"][0]["job"])
    assert job["state"] == "done", job["error"]


def test_model_plan_is_validated_and_repaired(tmp_path, media_dir):
    answers = iter([
        "Here: {\"steps\": [{\"kind\": \"op\", \"name\": \"explode\", \"args\": {}}]}",
        json.dumps({"steps": [{"kind": "op", "name": "delete_range", "args": {"start": 0, "end": 1000}, "explain": "Quitar el primer segundo"},
                              {"kind": "export", "name": "final", "args": {}, "explain": "Exportar"}], "notes": ""}),
    ])
    link = FakeLink(responder=lambda messages: next(answers))
    svc = make_services(tmp_path, link=link)
    svc.start()
    try:
        talk = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
        pid = store.create(svc, "x", preset="hd720", media=[talk])["id"]
        plan = plan_mod.create(svc, pid, "quita el primer segundo y exporta")
        assert plan["source"] == "model" and [s["name"] for s in plan["steps"]] == ["delete_range", "final"]
        assert "CONTEXT" in link.calls[0][1]["content"] and talk in link.calls[0][1]["content"]
        edited = plan["steps"][:1]
        plan = plan_mod.update(svc, plan["id"], edited)
        res = plan_mod.apply(svc, plan["id"])
        assert res["applied"] and not res["renders"] and store.doc(svc, pid).duration == 11000
    finally:
        svc.stop()


def test_model_failure_falls_back_to_rules(services, talk, proj):
    plan = plan_mod.create(services, proj, "quita los silencios")
    assert plan["source"] == "rules" and plan["steps"][0]["name"] == "remove_silences" and "Sin modelo" in plan["notes"]
    empty = plan_mod.create(services, proj, "hazlo bonito", use_model=False)
    assert not empty["steps"] and empty["notes"]


def test_script_assemble_words_and_meaning(tmp_path, media_dir):
    from prosperos_hoard.editing import media as ms

    answer = "1: 1-1\n2: 3, 1\n3: -\nNOTE: parte 3 no está"
    link = FakeLink(responder=lambda messages: answer)
    svc = make_services(tmp_path, link=link)
    svc.start()
    try:
        talk = ms.import_path(svc, str(media_dir / "talk.mp4"))["id"]
        pid = store.create(svc, "Guion", preset="hd720", media=[talk])["id"]
        words = [(200, 500, "uno"), (600, 900, "dos"), (1000, 1300, "tres"), (4000, 4300, "uno"), (4400, 4700, "dos"), (4800, 5100, "tres"),
                 (6000, 6300, "cuatro"), (6400, 6700, "cinco"), (6800, 7100, "seis")]
        fake_transcript(svc, talk, words)
        t = ms.get_analysis(svc, talk, "transcript")
        t["segments"] = [{"t0": 0, "t1": 150, "text": "vamos allá"}, {"t0": 200, "t1": 1300, "text": "uno dos tres"},
                         {"t0": 3000, "t1": 3500, "text": "otra vez"}, {"t0": 4000, "t1": 7100, "text": "uno dos tres cuatro cinco seis"}]
        ms.put_analysis(svc, talk, "transcript", t)
        script = "## A\nUno dos tres.\n\n## B\nCuatro cinco seis.\n\n## C\nSiete ocho nueve."
        res = commands.run(svc, pid, "script_assemble", {"media": talk, "script": script, "mode": "words"})
        s = res["summary"]
        assert s["mode"] == "words" and s["segments"] == 2 and [m["segment"] for m in s["missing"]] == [3]
        clips = sorted(store.doc(svc, pid).main_track().clips, key=lambda c: c.start)
        assert clips[0].src_in >= 3800  # the second (last) take of A
        assert [m.label for m in store.doc(svc, pid).markers] == ["A", "B"]
        with pytest.raises(Exception, match="word for word"):
            commands.run(svc, pid, "script_assemble", {"media": talk, "script": "## X\nnada que ver con esto aquí.", "mode": "words"})
        res = commands.run(svc, pid, "script_assemble", {"media": talk, "script": script, "mode": "meaning"})
        assert res["summary"]["mode"] == "meaning" and res["summary"]["notes"] == "parte 3 no está"
        clips = sorted(store.doc(svc, pid).main_track().clips, key=lambda c: c.start)
        assert clips[0].src_in >= 150 and clips[0].src_out <= 3000 and len(link.calls) == 1
        commands.run(svc, pid, "script_assemble", {"media": talk, "script": script, "mode": "meaning"}, preview=True)
        assert len(link.calls) == 1  # cached
    finally:
        svc.stop()
