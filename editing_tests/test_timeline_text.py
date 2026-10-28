"""The timeline as text with exact replacements, dry runs with a revision, and cut proposals that expire."""
import pytest

from conftest import fake_transcript, needs_ffmpeg
from prosperos_hoard.editing import commands, cut_proposals, timeline_text
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.agent_tools import call_tool
from prosperos_hoard.editing.errors import Conflict, LumiereError, NotFound

pytestmark = needs_ffmpeg

WORDS = [(100, 400, "Hola"), (500, 800, "eh,"), (900, 1200, "esto"), (1250, 1500, "es"), (1550, 1900, "una"), (1950, 2400, "prueba."),
         (4400, 4700, "Luego"), (4800, 5100, "ehm"), (5200, 5500, "sigo"), (5550, 5800, "que"), (5850, 6100, "que"), (6150, 6500, "todo")]


@pytest.fixture
def talk(services, media_dir):
    return media_store.import_path(services, str(media_dir / "talk.mp4"))["id"]


@pytest.fixture
def proj(services, talk):
    pid = store.create(services, "Charla", preset="hd720", media=[talk])["id"]
    store.edit(services, pid, [{"op": "split", "at": 4000}, {"op": "add_text", "text": "Título", "start": 500, "length": 2000}], label="setup")
    return pid


def line_of(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


def clip_ids(svc, pid):
    p = store.doc(svc, pid)
    return [(t.kind, c.id) for t in p.tracks for c in sorted(t.clips, key=lambda c: c.start)]


# ------------------------------------------------------------------------------------------------ the text
def test_the_timeline_text_is_stable_and_carries_its_revision(services, proj, talk):
    a = timeline_text.render(services, proj)
    b = timeline_text.render(services, proj)
    assert a["text"] == b["text"] and a["revision"] == store._row(services, proj)["rev"] and a["format"] == timeline_text.FORMAT
    text = a["text"]
    assert f"revision: {a['revision']}" in text and "timeline: " in text and "canvas: 1280x720 30fps" in text
    for _kind, cid in clip_ids(services, proj):
        assert text.count(cid + " ") == 1, "one line per clip"
    first = line_of(text, f"media {talk}")
    assert "| start=0 |" in first and "| src=0-4000 |" in first and "| speed=1 |" in first and "| volume=0 |" in first and 'label="talk"' in first
    assert 'text="Título"' in text and "| length=2000 |" in text
    only = timeline_text.render(services, proj, track=store.doc(services, proj).tracks[0].id)
    assert 'text="Título"' not in only["text"] and only["clips"] == a["clips"]
    assert timeline_text.render(services, proj, max_chars=1000)["truncated"] is True


def test_text_survives_a_round_trip_into_no_operations(services, proj):
    text = timeline_text.render(services, proj)["text"]
    assert timeline_text.plan(timeline_text.parse(text), timeline_text.parse(text)) == []
    again = timeline_text.apply(services, proj, text=text, expected_revision=store._row(services, proj)["rev"])
    assert again["applied"] is False and again["no_change"] is True


# ------------------------------------------------------------------------------------------------ replacements
def test_exact_replacements_change_the_timeline_and_prove_it(services, proj, talk):
    rev = store._row(services, proj)["rev"]
    text = timeline_text.render(services, proj)["text"]
    main = [cid for kind, cid in clip_ids(services, proj) if kind == "video"]
    title = next(cid for kind, cid in clip_ids(services, proj) if kind == "text")
    old = line_of(text, title).split("   #")[0].strip()
    new = old.replace('text="Título"', 'text="Otro título"').replace("| length=2000 |", "| length=1500 |").replace('label=""', 'label="intro"')
    done = timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": old, "new": new}])
    assert done["applied"] is True and done["revision"] == rev + 1 and done["changes"]["clips_changed"][title]["length"] == [2000, 1500]
    clip = store.doc(services, proj).find(title)[1]
    assert (clip.text, clip.length, clip.label) == ("Otro título", 1500, "intro")
    # volume and fades on a media clip, and the sentence from the brief: the text I wrote is what is there
    text2 = timeline_text.render(services, proj)["text"]
    mline = line_of(text2, main[0]).split("   #")[0].strip()
    done2 = timeline_text.apply(services, proj, expected_revision=done["revision"], label="Más suave", replacements=[
        {"old": mline, "new": mline.replace("volume=0", "volume=-6").replace("fade=0/0", "fade=300/500").replace("mute=no", "mute=yes")}])
    c = store.doc(services, proj).find(main[0])[1]
    assert (c.volume_db, c.fade_in, c.fade_out, c.mute) == (-6, 300, 500, True) and done2["revision"] == rev + 2
    store.undo(services, proj)
    assert store.doc(services, proj).find(main[0])[1].volume_db == 0, "one edit, one undo step"


def test_every_replacement_must_match_once_and_nothing_changes_otherwise(services, proj):
    rev = store._row(services, proj)["rev"]
    before = store.doc(services, proj).dump()
    text = timeline_text.render(services, proj)["text"]
    with pytest.raises(LumiereError, match="not found"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": "volume=99", "new": "volume=0"}])
    with pytest.raises(LumiereError, match=r"appears \d+ times"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": "volume=0", "new": "volume=-3"}])
    with pytest.raises(LumiereError, match="same"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": "mute=no", "new": "mute=no"}])
    title = next(cid for kind, cid in clip_ids(services, proj) if kind == "text")
    line = line_of(text, title).split("   #")[0].strip()
    # the first replacement is fine and the second does not match: the first is not applied either
    with pytest.raises(LumiereError, match="Replacement 2"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": line, "new": line.replace('label=""', 'label="x"')},
                                                                                  {"old": "no such text", "new": "x"}])
    assert store.doc(services, proj).dump() == before and store._row(services, proj)["rev"] == rev
    # a later replacement sees the earlier one (sequential, like an editor)
    done = timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": line, "new": line.replace('label=""', 'label="x"')},
                                                                                      {"old": 'label="x"', "new": 'label="y"'}])
    assert store.doc(services, proj).find(title)[1].label == "y" and done["applied"]


def test_a_stale_revision_is_a_conflict_and_a_dry_run_saves_nothing(services, proj):
    rev = store._row(services, proj)["rev"]
    text = timeline_text.render(services, proj)["text"]
    title = next(cid for kind, cid in clip_ids(services, proj) if kind == "text")
    line = line_of(text, title).split("   #")[0].strip()
    edit = [{"old": line, "new": line.replace("| start=500 |", "| start=700 |")}]
    dry = timeline_text.apply(services, proj, expected_revision=rev, replacements=edit, dry_run=True)
    assert dry["dry_run"] and dry["applied"] is False and dry["would_be_revision"] == rev + 1 and dry["changes"]["clips_changed"][title]["start"] == [500, 700]
    assert dry["ops"] == [{"op": "set", "clip": title, "props": {"start": 700}, "ripple": False}]
    assert store._row(services, proj)["rev"] == rev and store.doc(services, proj).find(title)[1].start == 500
    store.edit(services, proj, [{"op": "notes", "text": "someone else"}])
    with pytest.raises(Conflict, match="Read it again"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=edit)


def test_deleting_a_line_deletes_the_clip_and_the_header_is_read_only(services, proj):
    rev = store._row(services, proj)["rev"]
    text = timeline_text.render(services, proj)["text"]
    title = next(cid for kind, cid in clip_ids(services, proj) if kind == "text")
    full = next(line for line in text.splitlines() if title in line)
    done = timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": full + "\n", "new": ""}])
    assert done["changes"]["clips_removed"] == [title] and title not in [c for _, c in clip_ids(services, proj)]
    rev = store._row(services, proj)["rev"]
    with pytest.raises(LumiereError, match="read-only"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": f"revision: {rev}", "new": "revision: 99"}])
    with pytest.raises(LumiereError, match="read-only"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": "duration: ", "new": "duration: 1"}])
    media_clip = next(cid for kind, cid in clip_ids(services, proj) if kind == "video")
    line = line_of(timeline_text.render(services, proj)["text"], media_clip).split("   #")[0].strip()
    with pytest.raises(LumiereError, match="derived"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": line, "new": line.replace("end=", "end=1", 1)}])
    with pytest.raises(LumiereError, match="unknown field"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": line, "new": line + " | colour=red"}])
    with pytest.raises(LumiereError, match="does not exist"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": line, "new": line + "\n  clp_new text | start=0 | length=1000 | text=\"x\""}])
    with pytest.raises(LumiereError, match="valid value"):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": line.split(" | ")[1], "new": "start=abc"}])


def test_what_cannot_be_applied_exactly_is_refused_and_saves_nothing(services, proj):
    rev = store._row(services, proj)["rev"]
    media_clip = next(cid for kind, cid in clip_ids(services, proj) if kind == "video")
    line = line_of(timeline_text.render(services, proj)["text"], media_clip).split("   #")[0].strip()
    src = next(f for f in line.split(" | ") if f.startswith("src="))
    with pytest.raises(LumiereError):
        timeline_text.apply(services, proj, expected_revision=rev, replacements=[{"old": line, "new": line.replace(src, "src=0-999999999")}])
    assert store._row(services, proj)["rev"] == rev


# ------------------------------------------------------------------------------------------------ check and dry runs
def test_check_reports_instead_of_raising_and_edit_has_a_dry_run(services, proj):
    rev = store._row(services, proj)["rev"]
    title = next(cid for kind, cid in clip_ids(services, proj) if kind == "text")
    line = line_of(timeline_text.render(services, proj)["text"], title).split("   #")[0].strip()
    good = call_tool(services, "timeline_check", {"project": proj, "expected_revision": rev, "replacements": [{"old": line, "new": line.replace("| start=500 |", "| start=900 |")}]})
    assert good["ok"] and good["applied"] is False and good["changes"]["clips_changed"][title]["start"] == [500, 900] and store._row(services, proj)["rev"] == rev
    bad = call_tool(services, "timeline_check", {"project": proj, "replacements": [{"old": "nothing like this", "new": "x"}]})
    assert bad["ok"] is False and "not found" in bad["error"]
    stale = call_tool(services, "timeline_check", {"project": proj, "expected_revision": rev - 1, "replacements": [{"old": line, "new": line + " "}]})
    assert stale["ok"] is False and stale["code"] == "version_conflict"
    ops = call_tool(services, "timeline_check", {"project": proj, "ops": [{"op": "delete", "clips": [title]}], "expected_revision": rev})
    assert ops["ok"] and ops["dry_run"] and ops["changes"]["clips_removed"] == [title] and store._row(services, proj)["rev"] == rev
    assert call_tool(services, "timeline_check", {"project": proj, "ops": [{"op": "delete", "clips": ["clp_nope"]}]})["ok"] is False
    assert call_tool(services, "timeline_check", {"project": proj})["ok"] is False
    # timeline_edit: dry_run and expected_revision
    dry = call_tool(services, "timeline_edit", {"project": proj, "ops": [{"op": "delete", "clips": [title]}], "dry_run": True, "expected_revision": rev})
    assert dry["dry_run"] and dry["would_be_rev"] == rev + 1 and dry["rev"] == rev and store._row(services, proj)["rev"] == rev
    with pytest.raises(Conflict):
        call_tool(services, "timeline_edit", {"project": proj, "ops": [{"op": "delete", "clips": [title]}], "expected_revision": rev + 5})
    with pytest.raises(LumiereError, match="same thing"):
        call_tool(services, "timeline_edit", {"project": proj, "ops": [{"op": "notes", "text": "x"}], "base_rev": rev, "expected_revision": rev + 1})
    real = call_tool(services, "timeline_edit", {"project": proj, "ops": [{"op": "delete", "clips": [title]}], "expected_revision": rev})
    assert real["rev"] == rev + 1
    with pytest.raises(Conflict):
        call_tool(services, "timeline_history", {"project": proj, "action": "undo", "expected_revision": rev})
    assert call_tool(services, "timeline_history", {"project": proj, "action": "undo", "expected_revision": rev + 1})["undone"]


# ------------------------------------------------------------------------------------------------ cut proposals
def test_proposals_find_fillers_and_pauses_with_confidence_and_reason(services, proj, talk):
    with pytest.raises(commands.NeedsAnalysis):
        cut_proposals.create(services, proj)
    fake_transcript(services, talk, WORDS)
    made = cut_proposals.create(services, proj)
    kinds = {i["id"]: i for i in made["items"]}
    assert made["fillers"] == 3 and made["pauses"] == 1 and made["status"] == "open"
    eh, ehm, rep = kinds["f1"], kinds["f2"], kinds["f3"]
    assert (eh["text"], eh["words"]) == ("eh,", ["w2"]) and eh["confidence"] >= 0.7 and "filler word" in eh["reason"]
    assert ehm["words"] == ["w8"] and rep["text"] == "que" and "repeat" in rep["reason"] and rep["confidence"] < eh["confidence"]
    pause = kinds["p1"]
    assert pause["words"] == ["w6", "w7"] and pause["gap_ms"] == 2000 and 0.55 <= pause["confidence"] <= 0.99 and "silence" in pause["reason"]
    assert "sentence" in pause["reason"], "the pause follows the full stop of «prueba.»"
    assert all("cut" not in i for i in made["items"]), "the model sees word ids and reasons, not source times"
    assert store.doc(services, proj).duration == 12000, "making proposals changes nothing"
    assert [i["id"] for i in cut_proposals.view(services, cut_proposals.load(services, proj), min_confidence=0.8)["items"]] == ["f1", "f2"]
    assert cut_proposals.listing(services, proj)["proposals"][0]["proposal"] == made["proposal"]


def test_accepting_in_bulk_is_one_edit_and_one_undo(services, proj, talk):
    fake_transcript(services, talk, WORDS)
    made = cut_proposals.create(services, proj)
    rev = store._row(services, proj)["rev"]
    before = store.doc(services, proj).duration
    dry = cut_proposals.accept(services, proj, made["proposal"], dry_run=True, expected_revision=rev)
    assert dry["dry_run"] and dry["applied"] is False and store._row(services, proj)["rev"] == rev and store.doc(services, proj).duration == before
    done = cut_proposals.accept(services, proj, made["proposal"], min_confidence=0.7, kind="filler", expected_revision=rev)
    assert sorted(done["accepted"]) == ["f1", "f2"] and done["revision"] == rev + 1 and done["removed_ms"] > 0 and done["counts"]["accepted"] == 2 and done["counts"]["open"] == 2
    words = [w["text"] for w in commands.timeline_transcript(services, store.doc(services, proj))["words"]]
    assert "eh," not in words and "ehm" not in words and words.count("que") == 2
    rest = cut_proposals.accept(services, proj, made["proposal"])
    assert sorted(rest["accepted"]) == ["f3", "p1"] and rest["revision"] == rev + 2, "the rest still holds: the transcript did not change"
    words = [w["text"] for w in commands.timeline_transcript(services, store.doc(services, proj))["words"]]
    assert words.count("que") == 1
    store.undo(services, proj)
    assert [w["text"] for w in commands.timeline_transcript(services, store.doc(services, proj))["words"]].count("que") == 2
    with pytest.raises(LumiereError, match="No open proposal"):
        cut_proposals.accept(services, proj, made["proposal"], ids=["f1"])
    with pytest.raises(NotFound):
        cut_proposals.accept(services, proj, made["proposal"], ids=["f99"])
    with pytest.raises(Conflict):
        cut_proposals.accept(services, proj, made["proposal"], ids=["f3"], expected_revision=rev)


def test_proposals_expire_when_the_transcript_changes(services, proj, talk):
    fake_transcript(services, talk, WORDS)
    made = cut_proposals.create(services, proj)
    assert cut_proposals.load(services, proj)["status"] == "open"
    from prosperos_hoard.editing import analyze
    analyze.update_words(services, talk, [{"id": "w2", "text": "oye"}])
    gone = cut_proposals.view(services, cut_proposals.load(services, proj))
    assert gone["status"] == "expired" and "Make them again" in gone["expired"]
    with pytest.raises(LumiereError) as refused:
        cut_proposals.accept(services, proj, made["proposal"])
    assert refused.value.code == "proposals_expired"
    fresh = cut_proposals.create(services, proj)
    assert fresh["status"] == "open" and fresh["proposal"] != made["proposal"] and fresh["fillers"] == 2
    rejected = cut_proposals.reject(services, proj, fresh["proposal"], ids=["f1"])
    assert rejected["rejected"] == ["f1"] and rejected["counts"]["rejected"] == 1
    assert sorted(cut_proposals.accept(services, proj, fresh["proposal"])["accepted"]) == ["f2", "p1"]


def test_tools_over_the_agent_contract(services, proj, talk):
    fake_transcript(services, talk, WORDS)
    text = call_tool(services, "timeline_text", {"project": proj})
    assert text["revision"] >= 1 and text["text"].startswith("# ")
    made = call_tool(services, "cut_proposals", {"project": proj, "action": "create"})
    assert made["fillers"] == 3
    got = call_tool(services, "cut_proposals", {"project": proj})
    assert got["proposal"] == made["proposal"] and got["counts"]["open"] == 4
    low = call_tool(services, "cut_proposals", {"project": proj, "action": "reject", "max_confidence": 0.7})
    assert "f3" in low["rejected"] or low["rejected"] == []
    accepted = call_tool(services, "cut_proposals", {"project": proj, "action": "accept", "expected_revision": text["revision"], "min_confidence": 0.5})
    assert accepted["applied"] and accepted["accepted"]
    with pytest.raises(LumiereError):
        call_tool(services, "cut_proposals", {"project": proj, "action": "reject"})


def test_the_screen_routes(client, media_dir):
    svc = client.svc
    talk = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
    pid = store.create(svc, "Charla", preset="hd720", media=[talk])["id"]
    fake_transcript(svc, talk, WORDS)
    shown = client.get(f"/api/projects/{pid}/text").json()
    rev = shown["revision"]
    assert "timeline: " in shown["text"]
    edited = shown["text"].replace("volume=0", "volume=-4", 1)
    checked = client.post(f"/api/projects/{pid}/text/check", json={"text": edited, "expected_revision": rev}).json()
    assert checked["ok"] and checked["applied"] is False and store._row(svc, pid)["rev"] == rev
    applied = client.post(f"/api/projects/{pid}/text/apply", json={"text": edited, "expected_revision": rev}).json()
    assert applied["applied"] and applied["revision"] == rev + 1
    assert client.post(f"/api/projects/{pid}/text/apply", json={"text": edited, "expected_revision": rev}).status_code == 409
    assert client.post(f"/api/projects/{pid}/text/apply", json={"replacements": [{"old": "zzz", "new": "y"}], "expected_revision": rev + 1}).status_code == 400
    made = client.post(f"/api/projects/{pid}/cut-proposals", json={}).json()
    assert made["fillers"] == 3 and client.get(f"/api/projects/{pid}/cut-proposals").json()["proposal"] == made["proposal"]
    assert client.get(f"/api/projects/{pid}/cut-proposals", params={"proposal": "all"}).json()["proposals"]
    done = client.post(f"/api/projects/{pid}/cut-proposals/accept", json={"ids": ["f1"], "expected_revision": rev + 1}).json()
    assert done["accepted"] == ["f1"] and done["revision"] == rev + 2
    rej = client.post(f"/api/projects/{pid}/cut-proposals/reject", json={"ids": ["f2"]}).json()
    assert rej["rejected"] == ["f2"]
