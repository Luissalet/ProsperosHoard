"""Speaker separation: the built-in engine on synthetic voices, the service glue (jobs, rename, reassign), the text view,
captions and smart edits by speaker."""

from __future__ import annotations

import itertools

import pytest

from prosperos_hoard.editing import analyze, commands, speakers
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.analysis import speakers as engine
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.render import ass, runner

from conftest import make_services, needs_ffmpeg
from synth import SR, conversation, natural_talk, video_with_audio, write_wav


def accuracy(labels: list[int], truth: list[str]) -> float:
    """Share of words labelled right under the best mapping of cluster numbers to true speakers."""
    names = sorted(set(truth))
    n_clusters = max(labels) + 1
    best = 0.0
    for perm in itertools.permutations(names, min(len(names), n_clusters)):
        mapping = dict(enumerate(perm))
        best = max(best, sum(mapping.get(l) == t for l, t in zip(labels, truth)) / len(truth))
    return best


TURNS = [("A", 5), ("B", 4), ("A", 3), ("B", 6), ("A", 4), ("B", 3)]


@pytest.fixture(scope="module")
def talk():
    return conversation(TURNS, seed=5)


# ------------------------------------------------------------------ engine

def test_two_voices_are_separated_automatically(talk):
    audio, words = talk
    res = engine.diarize(audio, words)
    assert res["k"] == 2 and res["method"] == "builtin"
    assert accuracy(res["labels"], [w["speaker"] for w in words]) > 0.9


def test_number_of_speakers_is_respected():
    audio, words = conversation([("A", 4), ("B", 4), ("C", 4), ("A", 3), ("C", 3), ("B", 3)], seed=8)
    truth = [w["speaker"] for w in words]
    three = engine.diarize(audio, words, num_speakers=3)
    assert three["k"] == 3 and accuracy(three["labels"], truth) > 0.9
    assert engine.diarize(audio, words)["k"] == 3
    assert engine.diarize(audio, words, num_speakers=2)["k"] == 2
    one = engine.diarize(audio, words, num_speakers=1)
    assert one["k"] == 1 and set(one["labels"]) == {0}


def test_one_voice_stays_one_speaker():
    audio, words = conversation([("B", 14)], seed=2)
    assert engine.diarize(audio, words)["k"] == 1


# Real speech is messier than the clean synthetic turns above: short words timed back to back, a pitch and a loudness that
# wander over minutes, turns of 3-15 s. A transcript of ONE person used to come out as two speakers (the second one made of
# stray single words), so these checks use that kind of material.

@pytest.mark.parametrize("who,seed", [("A", 2), ("B", 4), ("C", 1)])
def test_a_long_natural_single_voice_stays_one_speaker(who, seed):
    audio, words = natural_talk([(who, 150)], seed=seed)
    assert len(words) > 400
    res = engine.diarize(audio, words)
    assert res["k"] == 1 and set(res["labels"]) == {0} and res["confidence"] == 0.0


def test_single_short_words_do_not_become_a_speaker():
    # a monologue cut into one-word turns: no speaker may be made of a few isolated words
    audio, words = natural_talk([("A", 90)], seed=7)
    res = engine.diarize(audio, words)
    assert res["k"] == 1
    forced = engine.diarize(audio, words, num_speakers=2)
    changes = sum(1 for a, b in zip(forced["labels"], forced["labels"][1:]) if a != b)
    assert changes <= 12  # even when two are demanded, the changes are smoothed into a few long stretches, not 100+ flickers


@pytest.mark.parametrize("pair,turns,seed", [(("A", "B"), [3, 9, 5, 14, 4, 7, 12, 6], 2), (("B", "C"), [5, 12, 3, 9, 14, 6, 8, 4], 3), (("A", "C"), [8, 4, 11, 6, 15, 3, 9, 5], 6)])
def test_natural_two_voices_with_long_turns(pair, turns, seed):
    audio, words = natural_talk([(pair[i % 2], s) for i, s in enumerate(turns)], seed=seed)
    truth = [w["speaker"] for w in words]
    res = engine.diarize(audio, words)
    assert res["k"] == 2 and accuracy(list(res["labels"]), truth) > 0.9
    assert accuracy(list(engine.diarize(audio, words, num_speakers=2)["labels"]), truth) > 0.9


def test_natural_three_voices():
    audio, words = natural_talk([("ABC"[i % 3], s) for i, s in enumerate([5, 9, 7, 12, 4, 8, 14, 6, 10, 3, 9, 11])], seed=4)
    truth = [w["speaker"] for w in words]
    res = engine.diarize(audio, words)
    assert res["k"] == 3 and accuracy(list(res["labels"]), truth) > 0.9
    assert accuracy(list(engine.diarize(audio, words, num_speakers=3)["labels"]), truth) > 0.9


def test_labels_follow_the_order_of_first_appearance(talk):
    audio, words = talk
    res = engine.diarize(audio, words)
    assert res["labels"][0] == 0 and 1 in res["labels"]


def test_no_words_and_bad_count(talk):
    audio, words = talk
    assert engine.diarize(audio, [])["k"] == 0
    with pytest.raises(ValueError):
        engine.diarize(audio, words, num_speakers=0)


def test_status_line_says_what_is_in_use():
    s = speakers.status()
    assert s["available"] and s["engine"] and s["note"]


# ------------------------------------------------------------------ the library and the text view

@pytest.fixture(scope="module")
def svc(tmp_path_factory, talk):
    s = make_services(tmp_path_factory.mktemp("lib"))
    s.start()
    audio, words = talk
    d = tmp_path_factory.mktemp("wav")
    write_wav(d / "charla.wav", audio)
    video_with_audio(d / "charla.mp4", d / "charla.wav", "gray", audio.size / SR)  # a video, so it lands on the main track
    s.mid = media_store.import_path(s, str(d / "charla.mp4"))["id"]
    s.truth = words
    yield s
    s.stop()


def _put_transcript(svc):
    """The words the speech model would have written (the true ones), without speakers."""
    media_store.put_analysis(svc, svc.mid, "transcript",
                             {"language": "es", "model": "fake", "device": "cpu", "segments": [],
                              "words": [{"id": f"w{i + 1}", "t0": w["t0"], "t1": w["t1"], "text": w["text"], "p": 0.9} for i, w in enumerate(svc.truth)]})
    svc.db.execute("DELETE FROM analysis WHERE media_id = ? AND kind = 'speakers'", (svc.mid,))


def _project(svc, name="Charla"):
    pid = store.create(svc, name, preset="hd720")["id"]
    store.edit(svc, pid, [{"op": "add_clip", "media": svc.mid}])
    return pid


def _words_for(svc, pid):
    """The words as the renderer hands them to the captions (speaker names and colours included)."""
    return runner.RenderContext(svc, store.doc(svc, pid), svc.config.work_dir).words_for


@needs_ffmpeg
def test_diarize_job_labels_words_and_segments(svc):
    _put_transcript(svc)
    jobs = analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2)
    assert svc.jobs.get(jobs[0]["job"])["state"] == "done"
    t = analyze.transcript(svc, svc.mid)
    assert sorted(t["speakers"]) == ["S1", "S2"] and all(t["speakers"][s]["color"].startswith("#") for s in t["speakers"])
    labels = [int(w["speaker"][1:]) - 1 for w in t["words"]]
    assert accuracy(labels, [w["speaker"] for w in svc.truth]) > 0.9
    s = speakers.summary(svc, svc.mid)
    assert s["diarized"] and len(s["speakers"]) == 2 and s["turns"] >= 4 and sum(x["words"] for x in s["speakers"]) == len(svc.truth)
    # a cached result is not computed twice, a different count is
    assert analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2)[0]["state"] == "cached"
    again = analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=3)
    assert again[0]["state"] != "cached" and len(speakers.summary(svc, svc.mid)["speakers"]) == 3


@needs_ffmpeg
def test_rename_assign_merge_and_clear(svc):
    _put_transcript(svc)
    analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2)
    first = speakers.rename(svc, svc.mid, {"S1": "Ana", "S2": "Luis"})
    assert [s["name"] for s in first["speakers"]] == ["Ana", "Luis"]
    speakers.rename(svc, svc.mid, {"luis": "Luis M."})  # by current name, any case
    assert speakers.name_of(analyze.transcript(svc, svc.mid), "S2") == "Luis M."
    with pytest.raises(LumiereError):
        speakers.rename(svc, svc.mid, {"S1": "luis m."})
    t = analyze.transcript(svc, svc.mid)
    ids = [w["id"] for w in t["words"][:3]]
    res = speakers.assign(svc, svc.mid, "Nuria", word_ids=ids)        # a new name makes a new speaker
    assert res["changed"] == 3 and len(res["speakers"]) == 3
    assert all(w["speaker"] == res["speaker"] for w in analyze.transcript(svc, svc.mid)["words"][:3])
    res = speakers.merge(svc, svc.mid, "Luis M.", "Ana")
    assert {s["name"] for s in res["speakers"]} == {"Ana", "Nuria"}
    res = speakers.assign(svc, svc.mid, "Ana", from_ms=0, to_ms=100000)
    assert len(res["speakers"]) == 1 and res["speakers"][0]["name"] == "Ana"
    with pytest.raises(LumiereError):
        speakers.assign(svc, svc.mid, "Ana")
    with pytest.raises(Exception):
        speakers.assign(svc, svc.mid, "Ana", word_ids=["w9999"])
    speakers.clear(svc, svc.mid)
    t = analyze.transcript(svc, svc.mid)
    assert "speakers" not in t and all("speaker" not in w for w in t["words"])
    with pytest.raises(LumiereError):
        speakers.rename(svc, svc.mid, {"S1": "X"})


@needs_ffmpeg
def test_timeline_transcript_shows_speakers_and_old_transcripts_do_not(svc):
    _put_transcript(svc)
    pid = _project(svc)
    plain = commands.timeline_transcript(svc, store.doc(svc, pid))
    assert len(plain["words"]) == len(svc.truth) and plain["speakers"] == [] and all("speaker" not in w for w in plain["words"])
    analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2)
    speakers.rename(svc, svc.mid, {"S1": "Ana", "S2": "Luis"})
    tt = commands.timeline_transcript(svc, store.doc(svc, pid))
    assert {s["name"] for s in tt["speakers"]} == {"Ana", "Luis"}
    assert all(w["speaker"] in ("S1", "S2") and w["speaker_name"] in ("Ana", "Luis") and w["speaker_color"].startswith("#") for w in tt["words"])
    assert [w["text"] for w in tt["words"]] == [w["text"] for w in plain["words"]]


@needs_ffmpeg
def test_captions_prefix_and_colour_by_speaker(svc):
    _put_transcript(svc)
    analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2)
    speakers.rename(svc, svc.mid, {"S1": "Ana", "S2": "Luis"})
    pid = _project(svc)
    words_for = _words_for(svc, pid)
    store.edit(svc, pid, [{"op": "captions", "enabled": True, "style": "clean"}])
    off = ass.caption_cues(store.doc(svc, pid), words_for)
    assert not any(c[2].startswith(("Ana:", "Luis:")) for c in off) and not any(c[3] for c in off)
    store.edit(svc, pid, [{"op": "captions", "props": {"speaker_labels": "both"}}])
    p = store.doc(svc, pid)
    cues = ass.caption_cues(p, words_for)
    assert any(c[2].startswith("Ana: ") for c in cues) and any(c[2].startswith("Luis: ") for c in cues)
    assert {c[3] for c in cues} == {s["color"] for s in speakers.summary(svc, svc.mid)["speakers"]}
    srt, _ = runner.subtitles_export(svc, pid, "srt")
    assert '<font color="#' in srt and "Ana: " in srt
    doc, _ = runner.subtitles_export(svc, pid, "ass")
    assert "\\1c&H" in doc and "Ana: " in doc
    store.edit(svc, pid, [{"op": "captions", "props": {"speaker_labels": "color"}}])
    colour_only = runner.subtitles_export(svc, pid, "srt")[0]
    assert '<font color="#' in colour_only and "Ana: " not in colour_only
    for _a, _b, text, _c in cues:  # a line never mixes two speakers
        assert not ("Ana: " in text[1:] or "Luis: " in text[1:])


@needs_ffmpeg
def test_cut_and_keep_one_speakers_parts(svc):
    _put_transcript(svc)
    analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2)
    speakers.rename(svc, svc.mid, {"S1": "Ana", "S2": "Luis"})
    t = analyze.transcript(svc, svc.mid)
    ana = [w for w in t["words"] if w["speaker"] == "S1"]
    luis = [w for w in t["words"] if w["speaker"] == "S2"]
    pid = _project(svc)
    before = store.doc(svc, pid).duration
    res = commands.run(svc, pid, "speaker_cut", {"speakers": ["Luis"]})
    assert res["summary"]["words"] == len(luis) and res["summary"]["mode"] == "cut"
    left = [w["speaker"] for w in commands.timeline_transcript(svc, store.doc(svc, pid))["words"]]
    assert set(left) == {"S1"} and len(left) == len(ana)
    assert store.doc(svc, pid).duration < before - 2000
    store.undo(svc, pid)
    assert store.doc(svc, pid).duration == before
    commands.run(svc, pid, "speaker_cut", {"speakers": ["S1"], "keep": True})
    left = [w["speaker"] for w in commands.timeline_transcript(svc, store.doc(svc, pid))["words"]]
    assert set(left) == {"S1"}
    with pytest.raises(Exception):
        commands.run(svc, pid, "speaker_cut", {"speakers": ["Nadie"]})
    with pytest.raises(Exception):
        commands.run(svc, pid, "speaker_cut", {"speakers": []})


@needs_ffmpeg
def test_cut_by_speaker_needs_the_separation_first(svc):
    _put_transcript(svc)
    pid = _project(svc)
    with pytest.raises(Exception):
        commands.run(svc, pid, "speaker_cut", {"speakers": ["S1"]})


@needs_ffmpeg
def test_transcribe_can_separate_speakers_in_one_go(svc):
    svc.db.execute("DELETE FROM analysis WHERE media_id = ? AND kind IN ('transcript', 'speakers')", (svc.mid,))
    original = svc.transcriber
    svc.transcriber = lambda path, **kw: {"language": "es", "language_p": 1.0, "model": "fake", "device": "cpu", "segments": [],
                                          "words": [{"id": f"w{i + 1}", "t0": w["t0"], "t1": w["t1"], "text": w["text"], "p": 0.9} for i, w in enumerate(svc.truth)]}
    try:
        jobs = analyze.schedule(svc, svc.mid, ["transcript"], speakers=True, num_speakers=2)
        assert svc.jobs.get(jobs[0]["job"])["state"] == "done"
    finally:
        svc.transcriber = original
    t = analyze.transcript(svc, svc.mid)
    assert len(t["speakers"]) == 2 and all(w.get("speaker") for w in t["words"])


@needs_ffmpeg
def test_speakers_are_reachable_through_the_tools(svc):
    from prosperos_hoard.editing import agent_tools

    call = lambda tool, **args: agent_tools.call_tool(svc, tool, args)  # noqa: E731
    _put_transcript(svc)
    with pytest.raises(LumiereError):
        call("speakers_edit", media=svc.mid, action="rename", names={"S1": "Ana"})
    jobs = call("media_analyze", media=svc.mid, kinds=["speakers"], num_speakers=2)["jobs"]
    assert jobs[0]["kind"] == "speakers"
    got = call("speakers_get", media=svc.mid)
    assert got["diarized"] and len(got["speakers"]) == 2 and got["status"]["engine"]
    call("speakers_edit", media=svc.mid, action="rename", names={"S1": "Ana", "S2": "Luis"})
    pid = _project(svc)
    words = call("timeline_transcript", project=pid)["words"]
    assert {w["speaker"] for w in words} == {"Ana", "Luis"}
    res = call("edit_command", project=pid, command="speaker_cut", args={"speakers": ["Luis"]})
    assert res["summary"]["mode"] == "cut"
    assert {w["speaker"] for w in call("timeline_transcript", project=pid)["words"]} == {"Ana"}
    call("speakers_edit", media=svc.mid, action="assign", speaker="Luis", from_ms=0, to_ms=1000)
    call("speakers_edit", media=svc.mid, action="merge", source="Luis", into="Ana")
    call("speakers_edit", media=svc.mid, action="clear")
    assert not call("timeline_transcript", project=pid)["speakers"]
    again = call("speakers_edit", media=svc.mid, action="diarize", num_speakers=2)
    assert again["jobs"][0]["kind"] == "speakers" and call("speakers_get", media=svc.mid)["diarized"]


def _pixels_near(path, hex_color: str, tolerance: int = 40) -> int:
    from PIL import Image
    import numpy as np

    rgb = np.asarray(Image.open(path).convert("RGB")).astype(int)
    want = np.array([int(hex_color[i:i + 2], 16) for i in (1, 3, 5)])
    return int((np.abs(rgb - want).max(axis=2) < tolerance).sum())


@needs_ffmpeg
def test_burned_captions_are_painted_in_the_speakers_colour(svc):
    _put_transcript(svc)
    analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2)
    table = analyze.transcript(svc, svc.mid)["speakers"]
    first_a = next(w for w in svc.truth if w["speaker"] == "A")
    first_b = next(w for w in svc.truth if w["speaker"] == "B")
    # the engine numbers speakers by first appearance, so A (who talks first) is S1
    pid = _project(svc)
    store.edit(svc, pid, [{"op": "captions", "enabled": True, "style": "bold", "props": {"speaker_labels": "color", "max_words": 2}}])
    for word, sid in ((first_a, "S1"), (first_b, "S2")):
        png = runner.render_frame(svc, pid, word["t0"] + 80, width=640)
        other = "S2" if sid == "S1" else "S1"
        mine, theirs = table[sid]["color"], table[other]["color"]
        assert _pixels_near(png, mine) > 40 and _pixels_near(png, theirs) < 5, (sid, _pixels_near(png, mine), _pixels_near(png, theirs))
    store.edit(svc, pid, [{"op": "captions", "props": {"speaker_labels": "off"}}])
    png = runner.render_frame(svc, pid, first_b["t0"] + 80, width=640)
    assert _pixels_near(png, table["S2"]["color"]) < 5


@needs_ffmpeg
def test_engine_can_be_chosen_and_embeddings_need_their_library(svc):
    _put_transcript(svc)
    job = analyze.schedule(svc, svc.mid, ["speakers"], num_speakers=2, engine="builtin")[0]["job"]
    assert svc.jobs.get(job)["state"] == "done" and speakers.summary(svc, svc.mid)["method"] == "builtin"
    if not engine.embedder_status()["available"]:
        job = analyze.schedule(svc, svc.mid, ["speakers"], force=True, engine="embeddings")[0]["job"]
        failed = svc.jobs.get(job)
        assert failed["state"] == "failed" and "requirements-speakers.txt" in failed["error"]
        assert speakers.summary(svc, svc.mid)["diarized"]  # the earlier result is untouched by the failed run
