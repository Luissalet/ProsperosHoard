"""Continuity bible, segment repair, join review and consistency warnings (fixture media made with ffmpeg lavfi)."""
from __future__ import annotations

import pytest

from prosperos_hoard import continuity, procutil, segment_repair
from prosperos_hoard.backend import ffmpeg_path
from prosperos_hoard.engine import EngineError

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="ffmpeg not installed")


def _make(store, project, name, args, kind="video", duration=None):
    ext = ".png" if kind == "image" else ".mp4"
    path = store.path_for_asset_file(name, ext)
    path.parent.mkdir(parents=True, exist_ok=True)
    procutil.run([ffmpeg_path(), "-y", "-loglevel", "error", *args, str(path)], check=True)
    return store.create_asset(project_id=project["id"], kind=kind, file_path=path.relative_to(store.data_dir).as_posix(),
                              asset_id=name, name=name, duration_s=duration)


def _video(store, project, name, src, seconds, vf="", af="", sine=440, size="160x120", gop=25):
    inputs = ["-f", "lavfi", "-i", f"{src}=size={size}:rate=25:duration={seconds}",
              "-f", "lavfi", "-i", f"sine=frequency={sine}:sample_rate=48000:duration={seconds}"]
    filt = ["-vf", vf] if vf else []
    afilt = ["-af", af] if af else []
    return _make(store, project, name, [*inputs, *filt, *afilt, "-c:v", "libx264", "-g", str(gop), "-sc_threshold", "0",
                                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"], duration=seconds)


def test_bible_revision_marks_only_dependent_shots_stale(store, project):
    prop = _make(store, project, "ref_red", ["-f", "lavfi", "-i", "color=c=red:size=64x64", "-frames:v", "1"], "image")
    clip = _make(store, project, "clip_a", ["-f", "lavfi", "-i", "color=c=red:size=64x64:rate=25:duration=1", "-pix_fmt", "yuv420p"])
    bible = continuity.create(store, project["id"], "Teaser", production="teaser")
    bid = bible["id"]
    continuity.upsert_entry(store, bid, "lamp", {"kind": "object", "name": "Lamp", "descriptor": "red lamp", "reference_asset_ids": ["ref_red"]})
    continuity.upsert_entry(store, bid, "hero", {"kind": "character", "name": "Hero", "descriptor": "blue coat"})
    continuity.upsert_entry(store, bid, "narrator", {"kind": "voice", "name": "Narrator", "voice_id": "v1", "settings": {"speed": 1.0}})
    continuity.upsert_shot(store, bid, "s1", {"start_s": 0, "end_s": 5, "description": "the lamp", "uses": ["lamp"]})
    continuity.upsert_shot(store, bid, "s2", {"start_s": 5, "end_s": 10, "description": "the hero", "uses": ["hero", "narrator"]})
    continuity.upsert_shot(store, bid, "s3", {"start_s": 10, "end_s": 15, "description": "empty street"})
    for sid in ("s1", "s2", "s3"):
        continuity.accept_clip(store, bid, sid, "clip_a", references=["ref_red"] if sid == "s1" else [])
    assert continuity.view(continuity.load(store.data_dir, bid))["stale_shots"] == []

    same = continuity.upsert_entry(store, bid, "lamp", {"descriptor": "red lamp"})
    assert same["changed"] == [] and same["newly_stale"] == []  # nothing changed: no revision

    res = continuity.upsert_entry(store, bid, "lamp", {"descriptor": "green lamp"}, note="client asked for green")
    assert res["revision"] == 2 and res["newly_stale"] == ["s1"]
    assert [m["shot_id"] for m in res["must_regenerate"]] == ["s1"]
    assert res["must_regenerate"][0]["reasons"][0]["entry_id"] == "lamp"
    assert set(res["kept_accepted"]) == {"s2", "s3"}

    voice = continuity.upsert_entry(store, bid, "narrator", {"settings": {"speed": 0.9}})
    assert voice["newly_stale"] == ["s2"]

    # identity is a dependency of every shot by default
    ident = continuity.upsert_entry(store, bid, "identity", {"palette": ["#112233", "#ffcc00"]})
    assert set(continuity.view(continuity.load(store.data_dir, bid))["stale_shots"]) == {"s1", "s2", "s3"}
    assert ident["revision"] == 2

    # re-accepting a clip records the new revisions (decision provenance) and clears only that shot
    got = continuity.accept_clip(store, bid, "s1", "clip_a", operation="regenerate", references=["ref_red"])
    assert got["entries"]["lamp"] == 2 and got["entries"]["identity"] == 2 and "ref_red" in got["references"]
    assert "s1" not in continuity.view(continuity.load(store.data_dir, bid))["stale_shots"]
    hist = continuity.entry_history(continuity.load(store.data_dir, bid), "lamp")
    assert [h["revision"] for h in hist] == [1, 2] and hist[1]["changes"] == ["descriptor"]
    # a shot edit only touches that shot
    ch = continuity.upsert_shot(store, bid, "s1", {"camera": "dolly in"})
    assert ch["newly_stale"] == ["s1"] and ch["must_regenerate"][0]["reasons"][0]["why"].startswith("shot ")
    with pytest.raises(EngineError):
        continuity.upsert_entry(store, bid, "x", {"colour": "red"})


def test_repair_five_seconds_of_twenty_keeps_everything_else_identical(store, project):
    src = _video(store, project, "long", "testsrc", 20)
    rep = _video(store, project, "fix", "smptebars", 6, sine=880, size="320x240")  # other size: must be conformed
    original = segment_repair.analyze_video(store.data_dir / src["file_path"])
    assert original["frames"] == 500 and {125, 250} <= set(original["keyframes"])
    res = segment_repair.repair_segment(store, "long", "fix", 5.0, 10.0, transition="cut")
    assert res["exact_outside"] is True
    assert res["frames"] == [125, 250] and (res["width"], res["height"]) == (160, 120)
    out = store.get_asset(res["asset_id"])
    assert abs(out["duration_s"] - 20.0) < 0.06
    new = segment_repair.analyze_video(store.data_dir / out["file_path"])
    assert new["frames"] == 500
    assert new["hashes"][:125] == original["hashes"][:125]          # before the range: frame-identical
    assert new["hashes"][250:] == original["hashes"][250:]          # after the range: frame-identical
    assert new["hashes"][125:250] != original["hashes"][125:250]    # the range really changed
    assert res["untouched"]["identical"] and res["untouched"]["frames_compared"] == 375
    assert res["untouched"]["reencoded_range_s"] == [5.0, 10.0]
    info = segment_repair._probe(store.data_dir / out["file_path"])
    assert info["sample_rate"] == 48000 and info["has_audio"] and info["channels"] == 1
    # the review of the two seams ran, with a strip image each
    assert [s["t_s"] for s in res["join_review"]["seams"]] == [5.0, 10.0]
    assert all(store.get_asset(s["strip_asset_id"])["kind"] == "image" for s in res["join_review"]["seams"])
    assert {c["id"] for c in res["join_review"]["overall"]} == {"duration_preserved", "frames_preserved"}
    assert all(c["status"] == "pass" for c in res["join_review"]["overall"])
    assert store.get_asset("long")["file_path"] == src["file_path"]  # the source is untouched


def test_repair_off_keyframes_reencodes_only_the_touched_gops_and_crossfade_keeps_length(store, project):
    _video(store, project, "long2", "testsrc", 12)
    _video(store, project, "fix2", "testsrc2", 5)
    original = segment_repair.analyze_video(store.data_dir / store.get_asset("long2")["file_path"])
    res = segment_repair.repair_segment(store, "long2", "fix2", 3.4, 6.6, transition="crossfade", crossfade_s=0.4)
    assert res["exact_outside"] and res["untouched"]["identical"]
    assert res["untouched"]["reencoded_range_s"] == [3.0, 7.0]       # snapped out to the keyframes around the range
    new = segment_repair.analyze_video(store.data_dir / store.get_asset(res["asset_id"])["file_path"])
    assert new["frames"] == original["frames"] == 300
    assert new["hashes"][:75] == original["hashes"][:75] and new["hashes"][175:] == original["hashes"][175:]
    with pytest.raises(EngineError):
        segment_repair.repair_segment(store, "long2", "fix2", 3.0, 3.5, transition="crossfade", crossfade_s=0.4)
    with pytest.raises(EngineError):
        segment_repair.repair_segment(store, "long2", "fix2", 11.0, 13.0)           # outside the video
    short = _video(store, project, "tiny", "testsrc", 1)
    with pytest.raises(EngineError) as exc:
        segment_repair.repair_segment(store, "long2", short["id"], 2.0, 5.0)
    assert exc.value.code == "replacement_too_short"


def test_seam_checks_find_a_black_frame_and_an_audio_level_jump(store, project):
    # a black frame at 2.0 s (frame 50) and the audio dropping by 30 dB from 2.0 s
    vf = "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(n,50,50)'"
    af = "volume=enable='gte(t,2)':volume=0.03"
    _video(store, project, "bad", "testsrc", 4, vf=vf, af=af)
    review = segment_repair.review_join(store, "bad", [2.0], expected_duration_s=4.0)
    seam = review["seams"][0]
    by = {c["id"]: c for c in seam["checks"]}
    assert by["black_frames"]["status"] == "fail" and 50 in by["black_frames"]["actual"]
    assert by["audio_level_jump"]["status"] == "fail"
    assert not review["ok"] and review["failures"] >= 2
    assert store.get_asset(seam["strip_asset_id"])["kind"] == "image"
    # the same video without the faults passes the same checks, and a custom threshold is honoured
    _video(store, project, "good", "testsrc", 4)
    clean = segment_repair.review_join(store, "good", [2.0], expected_duration_s=4.0)
    assert clean["ok"] and clean["failures"] == 0
    assert {c["id"] for c in clean["seams"][0]["checks"]} >= {"black_frames", "frozen_frames", "colour_jump", "audio_level_jump", "audio_click"}
    with pytest.raises(EngineError):
        segment_repair.review_join(store, "good", [2.0], thresholds={"nope": 1})
    wrong = segment_repair.review_join(store, "good", [2.0], expected_duration_s=9.0, strips=False)
    assert not wrong["ok"] and wrong["overall"][0]["id"] == "duration_preserved"


def test_seam_checks_find_frozen_frames_a_colour_jump_and_a_click(store, project):
    _video(store, project, "frozen", "testsrc", 4, vf="loop=loop=7:size=1:start=50")  # frame 50 is held for 8 frames
    review = segment_repair.review_join(store, "frozen", [2.0], strips=False)
    assert {c["id"]: c["status"] for c in review["seams"][0]["checks"]}["frozen_frames"] == "warn"
    # red for 2 s, then blue: a colour jump; a 100 Hz tone cut at a non-zero phase: a click
    path = store.path_for_asset_file("jump", ".mp4")
    procutil.run([ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:size=96x96:rate=25:duration=2",
                  "-f", "lavfi", "-i", "color=c=blue:size=96x96:rate=25:duration=2", "-f", "lavfi", "-i", "sine=frequency=100:sample_rate=48000:duration=2.002",
                  "-f", "lavfi", "-i", "sine=frequency=100:sample_rate=48000:duration=2.0",
                  "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a]volume=0.8[a1];[3:a]volume=0.8[a2];[a1][a2]concat=n=2:v=0:a=1[a]",
                  "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(path.with_suffix(".mov"))], check=True)
    mov = path.with_suffix(".mov")
    store.create_asset(project_id=project["id"], kind="video", file_path=mov.relative_to(store.data_dir).as_posix(), asset_id="jump", name="jump", duration_s=4.0)
    by = {c["id"]: c for c in segment_repair.review_join(store, "jump", [2.0], strips=False)["seams"][0]["checks"]}
    assert by["colour_jump"]["status"] == "fail"
    assert by["audio_click"]["status"] in ("warn", "fail")


def test_colour_drift_is_warned_for_a_recoloured_shot_and_never_claims_identity(store, project):
    ref = _make(store, project, "ref_lamp", ["-f", "lavfi", "-i", "color=c=0xcc2222:size=64x64", "-frames:v", "1"], "image")
    same = _make(store, project, "shot_ok", ["-f", "lavfi", "-i", "color=c=0xc82424:size=96x96:rate=25:duration=1", "-pix_fmt", "yuv420p"])
    recoloured = _make(store, project, "shot_blue", ["-f", "lavfi", "-i", "color=c=0xc82424:size=96x96:rate=25:duration=1",
                                                      "-vf", "hue=h=140", "-pix_fmt", "yuv420p"])
    bid = continuity.create(store, project["id"], "Lamp")["id"]
    continuity.upsert_entry(store, bid, "lamp", {"kind": "object", "name": "Lamp", "descriptor": "red lamp", "reference_asset_ids": [ref["id"]]})
    continuity.upsert_shot(store, bid, "ok", {"start_s": 0, "end_s": 1, "prompt": "the Lamp on the table, red", "uses": ["lamp"]})
    continuity.upsert_shot(store, bid, "drift", {"start_s": 1, "end_s": 2, "prompt": "a Lamp on a shelf", "uses": ["lamp"]})
    continuity.accept_clip(store, bid, "ok", same["id"])
    continuity.accept_clip(store, bid, "drift", recoloured["id"])
    report = continuity.check_consistency(store, bid)
    kinds = {(w["shot_id"], w["check"]) for w in report["warnings"]}
    assert ("drift", "colour_drift") in kinds and ("ok", "colour_drift") not in kinds
    assert ("drift", "descriptor_colour_missing") in kinds            # the prompt dropped "red"
    assert all(w["level"] == "warning" for w in report["warnings"])
    assert report["level"] == "warnings_only" and "does not mean" in report["note"]
    measured = {m["shot_id"]: m for m in report["measured"]}
    assert measured["ok"]["delta_e"] < 8 < 18 < measured["drift"]["delta_e"]


def test_repair_records_provenance_and_links_the_correction(store, project):
    _video(store, project, "cut1", "testsrc", 8)
    _video(store, project, "take2", "smptebars", 4)
    bid = continuity.create(store, project["id"], "Film")["id"]
    continuity.upsert_entry(store, bid, "hero", {"kind": "character", "name": "Hero", "descriptor": "blue coat"})
    continuity.upsert_shot(store, bid, "a", {"start_s": 0, "end_s": 4, "description": "intro", "uses": ["hero"]})
    continuity.upsert_shot(store, bid, "b", {"start_s": 4, "end_s": 8, "description": "chase", "uses": ["hero"]})
    continuity.accept_clip(store, bid, "a", "cut1")
    continuity.accept_clip(store, bid, "b", "cut1")
    corr = continuity.add_correction(store, bid, "hero coat is wrong", shot_id="b", start_s=4, end_s=8, region=[0.2, 0.2, 0.5, 0.6])
    res = segment_repair.repair_segment(store, "cut1", "take2", shot_id="b", bible_id=bid, correction_id=corr["id"], review=False)
    assert res["range_s"] == [4.0, 8.0] and res["provenance"]["shots"] == ["b"]
    bible = continuity.load(store.data_dir, bid)
    assert bible["corrections"][0]["status"] == "applied" and bible["corrections"][0]["applied_asset_id"] == res["asset_id"]
    assert bible["shots"]["b"]["clip"]["asset_id"] == res["asset_id"] and bible["shots"]["a"]["clip"]["asset_id"] == "cut1"
    last = bible["provenance"][-1]
    assert last["operation"] == "segment_repair" and last["entries"]["hero"] == 1 and "take2" in last["references"]
    recipe = store.get_asset(res["asset_id"])["recipe"]
    assert recipe["continuity"]["bible_id"] == bid and recipe["continuity"]["entries"]["hero"] == 1


def test_routes_bible_repair_job_and_agent_endpoints(client, data_dir):
    from prosperos_hoard.store import Store
    c, app, _ = client
    store = Store(data_dir)
    project = store.create_project("Reel", brief="b")
    _video(store, project, "main", "testsrc", 6)
    _video(store, project, "patch", "smptebars", 3)
    bid = c.post("/api/continuity", json={"project": project["id"], "name": "Reel", "production": "reel"}).json()["bible_id"]
    assert c.get("/api/continuity", params={"project": project["id"]}).json()["items"][0]["bible_id"] == bid
    r = c.put(f"/api/continuity/{bid}/entries/lamp", json={"bible_id": bid, "entry_id": "lamp", "fields": {"kind": "object", "name": "Lamp"}})
    assert r.status_code == 200, r.text
    for sid, a, b in (("one", 0, 3), ("two", 3, 6)):
        c.put(f"/api/continuity/{bid}/shots/{sid}", json={"bible_id": bid, "fields": {"start_s": a, "end_s": b, "uses": ["lamp"]}})
        r = c.post(f"/api/continuity/{bid}/shots/{sid}/clip", json={"bible_id": bid, "asset_id": "main"})
        assert r.status_code == 200, r.text
    r = c.put(f"/api/continuity/{bid}/entries/lamp", json={"bible_id": bid, "entry_id": "lamp", "fields": {"descriptor": "brass"}})
    assert r.json()["newly_stale"] == ["one", "two"]
    r = c.post("/api/agent/continuity_repair_segment", json={"asset_id": "main", "replacement_asset_id": "patch", "shot_id": "two",
                                                             "bible_id": bid, "wait_s": 120})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["state"] == "done", job
    new_id = job["asset_ids"][0]
    assert store.get_asset(new_id)["tags"] == ["segment_repair"]
    review = c.post("/api/continuity/review-join", json={"asset_id": new_id, "seams_s": [3.0], "strips": False,
                                                           "expected_duration_s": 6.0}).json()
    assert review["seams"][0]["t_s"] == 3.0 and "overall" in review
    got = c.get(f"/api/continuity/{bid}").json()
    assert got["shots"]["two"]["repairs"][0]["asset_id"] == new_id
    r = c.post(f"/api/continuity/{bid}/corrections", json={"bible_id": bid, "text": "lamp too dark", "shot_id": "one"})
    assert r.json()["status"] == "open"
    assert c.post("/api/agent/continuity_check_consistency", json={"bible_id": bid}).json()["level"] == "warnings_only"
    assert c.get(f"/api/continuity/{bid}/entries/lamp/history").json()["history"][-1]["revision"] == 2
    missing = c.get("/api/continuity/cb_nope")
    assert missing.status_code >= 400 and "not_found" in missing.text

def test_a_bible_can_be_deleted_and_its_clips_stay(client, data_dir):
    from prosperos_hoard.store import Store
    c, app, _ = client
    store = Store(data_dir)
    project = store.create_project("Reel", brief="b")
    _video(store, project, "main", "testsrc", 3)
    bid = c.post("/api/continuity", json={"project": project["id"], "name": "Para borrar"}).json()["bible_id"]
    c.put(f"/api/continuity/{bid}/shots/one", json={"bible_id": bid, "fields": {"start_s": 0, "end_s": 3}})
    c.post(f"/api/continuity/{bid}/shots/one/clip", json={"bible_id": bid, "asset_id": "main"})
    out = c.delete(f"/api/continuity/{bid}").json()
    assert out["deleted"] == bid and out["removed"]["shots"] == 1 and "stay" in out["kept"]["assets"]
    assert c.get("/api/continuity", params={"project": project["id"]}).json()["items"] == []
    assert store.get_asset("main")["id"] == "main"
    assert c.get(f"/api/continuity/{bid}").status_code >= 400
    again = c.post("/api/agent/continuity_bible", json={"action": "delete", "bible_id": bid})
    assert again.status_code >= 400 and "not_found" in again.text
