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


def test_a_repair_with_the_current_revisions_clears_the_range_it_replaced(store, project):
    """prospero#2287: a shot repaired after an entry change stayed stale. Now the repaired range is a segment of the
    clip made with the current revisions; only what is still made with the old revision keeps the shot stale."""
    _video(store, project, "film", "testsrc", 12)
    _video(store, project, "fixa", "smptebars", 5)
    _video(store, project, "fixb", "testsrc2", 5)
    bid = continuity.create(store, project["id"], "Film")["id"]
    continuity.upsert_entry(store, bid, "lamp", {"kind": "object", "name": "Lamp", "descriptor": "brass"})
    continuity.upsert_shot(store, bid, "uno", {"start_s": 0, "end_s": 6, "description": "street"})
    continuity.upsert_shot(store, bid, "dos", {"start_s": 6, "end_s": 12, "description": "the lamp", "uses": ["lamp"]})
    continuity.accept_clip(store, bid, "uno", "film")
    continuity.accept_clip(store, bid, "dos", "film")
    assert continuity.upsert_entry(store, bid, "lamp", {"descriptor": "aged brass"})["newly_stale"] == ["dos"]

    # repair 4-8 s for the shot 6-12 s: 6-8 s now has lamp r2, 8-12 s is still the r1 material
    first = segment_repair.repair_segment(store, "film", "fixa", 4.0, 8.0, shot_id="dos", bible_id=bid, review=False)
    prov = first["provenance"]
    assert prov["up_to_date"] == [] and prov["still_stale"] == [{"shot_id": "dos", "stale_ranges_s": [[8.0, 12.0]],
                                                                 "repaired_ranges_s": [[6.0, 8.0]]}]
    view = continuity.view(continuity.load(store.data_dir, bid))
    reason = view["shots"]["dos"]["stale_reasons"][0]
    assert reason["entry_id"] == "lamp" and reason["used_revision"] == 1 and reason["current_revision"] == 2
    assert reason["ranges_s"] == [[8.0, 12.0]] and reason["repaired_ranges_s"] == [[6.0, 8.0]]
    assert view["shots"]["dos"]["clip"]["segments"][0]["entries"]["lamp"] == 2
    assert view["stale_shots"] == ["dos"] and "uno" in view["accepted_shots"]

    # repairing the rest of the shot on the repaired video brings it up to date without re-accepting anything
    second = segment_repair.repair_segment(store, first["asset_id"], "fixb", 8.0, 12.0, shot_id="dos", bible_id=bid, review=False)
    assert second["provenance"]["up_to_date"] == ["dos"] and second["provenance"]["still_stale"] == []
    bible = continuity.load(store.data_dir, bid)
    assert continuity.view(bible)["stale_shots"] == [] and bible["shots"]["dos"]["clip"]["asset_id"] == second["asset_id"]
    assert [p["repaired"] for p in continuity.clip_pieces(bible["shots"]["dos"])] == [True, True]

    # a later change of the entry makes the whole shot stale again: both repairs used r2
    continuity.upsert_entry(store, bid, "lamp", {"descriptor": "copper"})
    item = continuity.must_regenerate(continuity.load(store.data_dir, bid))[0]
    assert item["shot_id"] == "dos" and item["stale_ranges_s"] == [[6.0, 12.0]] and item["repaired_ranges_s"] == []
    # accepting a clip for the whole shot starts again from one piece with the current revisions
    continuity.accept_clip(store, bid, "dos", second["asset_id"], operation="edit")
    bible = continuity.load(store.data_dir, bid)
    assert "segments" not in bible["shots"]["dos"]["clip"] and continuity.view(bible)["stale_shots"] == []


def test_a_shot_without_repairs_keeps_its_old_stale_reasons(store, project):
    _video(store, project, "plain", "testsrc", 2)
    bid = continuity.create(store, project["id"], "Plain")["id"]
    continuity.upsert_entry(store, bid, "hero", {"kind": "character", "name": "Hero", "descriptor": "red"})
    continuity.upsert_shot(store, bid, "a", {"start_s": 0, "end_s": 2, "uses": ["hero"]})
    continuity.accept_clip(store, bid, "a", "plain")
    continuity.upsert_entry(store, bid, "hero", {"descriptor": "blue"})
    item = continuity.must_regenerate(continuity.load(store.data_dir, bid))[0]
    assert "ranges_s" not in item["reasons"][0] and "stale_ranges_s" not in item


# ---------------------------------------------------------------- prospero#2299: region, voice, regenerate

def _pcm(path, rate=16000):
    import numpy as np
    done = procutil.run([ffmpeg_path(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"])
    return np.frombuffer(done.stdout, dtype="float32")


def _rms(x, rate, a, b):
    import numpy as np
    seg = x[int(a * rate):int(b * rate)]
    return float(np.sqrt(np.mean(seg ** 2))) if seg.size else 0.0


def _audio_md5(path):
    done = procutil.run([ffmpeg_path(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "md5", "-"], text=True)
    return done.stdout.strip()


def test_a_region_repair_changes_only_the_rectangle_and_keeps_the_sound(store, project):
    import numpy as np
    src = _video(store, project, "scene", "testsrc", 4)
    _video(store, project, "patched", "testsrc", 4, vf="drawbox=x=40:y=30:w=80:h=60:color=red:t=fill", sine=880)
    # the patched take is the same moving picture with a box on it: taken from the same second, only the box differs
    res = segment_repair.repair_segment(store, "scene", "patched", 1.0, 3.0, region=[0.2, 0.2, 0.6, 0.6], replacement_in_s=1.0)
    assert res["region"]["px"] == [32, 24, 96, 72] and res["exact_outside"] and res["untouched"]["identical"]
    checks = {c["id"]: c for c in res["region_review"]["checks"]}
    assert checks["outside_preserved"]["status"] == "pass" and checks["region_changed"]["status"] == "pass"
    assert checks["region_edge"]["status"] == "pass", checks["region_edge"]
    assert store.get_asset(res["region_review"]["preview_asset_id"])["kind"] == "image"
    out = store.data_dir / store.get_asset(res["asset_id"])["file_path"]
    a = segment_repair._frame_rgb(ffmpeg_path(), store.data_dir / src["file_path"], 2.02, 160, 120)
    b = segment_repair._frame_rgb(ffmpeg_path(), out, 2.02, 160, 120)
    assert b[45, 60, 0] > 180 and b[45, 60, 1] < 90                          # red inside the rectangle
    mask = np.ones((120, 160), bool); mask[24:96, 32:128] = False
    assert np.abs(a - b).mean(axis=2)[mask].mean() < 4                      # the rest of the frame is the original
    assert _audio_md5(out) == _audio_md5(store.data_dir / src["file_path"])  # the sound is untouched
    assert any("only the region" in n for n in res["notes"])
    # the region a correction was written about
    bid = continuity.create(store, project["id"], "Scene")["id"]
    corr = continuity.add_correction(store, bid, "logo wrong", start_s=1, end_s=3, region=[0.2, 0.2, 0.6, 0.6])
    again = segment_repair.repair_segment(store, "scene", "patched", 1.0, 3.0, bible_id=bid, correction_id=corr["id"],
                                          region="correction", review=False, verify_untouched=False)
    assert again["region"]["px"] == [32, 24, 96, 72]
    assert continuity.load(store.data_dir, bid)["corrections"][0]["status"] == "applied"
    for bad in ([0.5, 0.5, 0, 0.2], [1.2, 0, 0.1, 0.1], "correction"):
        with pytest.raises(EngineError):
            segment_repair.repair_segment(store, "scene", "patched", 1.0, 3.0, region=bad, review=False)


def _speaker(seconds, calls):
    import io
    import wave
    import numpy as np

    def speak(spec, text):
        calls.append((dict(spec), text))
        n = int(16000 * seconds)
        tone = (np.sin(2 * np.pi * 300 * np.arange(n) / 16000) * 0.5 * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(tone.tobytes())
        return buf.getvalue(), "fake-tts"
    return speak


def test_a_line_in_the_bible_voice_is_spoken_in_the_repaired_range(store, project):
    _video(store, project, "talk", "testsrc", 4)
    _video(store, project, "take", "smptebars", 4, af="volume=0")
    bid = continuity.create(store, project["id"], "Talk")["id"]
    continuity.upsert_entry(store, bid, "narrador", {"kind": "voice", "name": "Narrador", "voice_id": "v1",
                                                     "settings": {"engine_id": "fake-tts", "speed": 0.9}})
    continuity.upsert_entry(store, bid, "lamp", {"kind": "object", "name": "Lamp"})
    continuity.upsert_shot(store, bid, "a", {"start_s": 0, "end_s": 4, "uses": ["lamp", "narrador"]})
    continuity.accept_clip(store, bid, "a", "talk")
    calls = []
    res = segment_repair.repair_segment(store, "talk", "take", 1.0, 3.0, shot_id="a", bible_id=bid, review=False,
                                        voice={"entry_id": "narrador", "text": "hola", "at_s": 0.5}, speak=_speaker(1.0, calls))
    assert calls == [({"engine_id": "fake-tts", "speed": 0.9, "voice_id": "v1"}, "hola")]
    v = res["voice"]
    assert v["entry_id"] == "narrador" and v["revision"] == 1 and v["engine_id"] == "fake-tts"
    assert abs(v["spoken_s"] - 1.0) < 0.05 and v["speed"] == 1.0 and v["at_s"] == 0.5 and v["cut"] is False
    pcm = _pcm(store.data_dir / store.get_asset(res["asset_id"])["file_path"])
    assert _rms(pcm, 16000, 1.05, 1.45) < 0.01          # before the line: the range's sound is the line alone
    assert _rms(pcm, 16000, 1.6, 2.4) > 0.1             # the line, placed 0.5 s into the range
    assert _rms(pcm, 16000, 0.2, 0.8) > 0.05            # outside the range the original sound stays
    last = continuity.load(store.data_dir, bid)["shots"]["a"]["repairs"][-1]
    assert last["entries"]["narrador"] == 1
    assert store.get_asset(res["asset_id"])["recipe"]["voice"]["text"] == "hola"
    long = segment_repair.repair_segment(store, "talk", "take", 1.0, 3.0, bible_id=bid, review=False, verify_untouched=False,
                                         voice={"entry_id": "narrador", "text": "una frase larga"}, speak=_speaker(4.0, []))
    assert long["voice"]["speed"] == 1.5 and long["voice"]["cut"] is True
    for voice, speak in (({"entry_id": "lamp", "text": "x"}, _speaker(1.0, [])), ({"entry_id": "narrador", "text": ""}, _speaker(1.0, [])),
                         ({"entry_id": "narrador", "text": "x"}, None)):
        with pytest.raises(EngineError):
            segment_repair.repair_segment(store, "talk", "take", 1.0, 3.0, bible_id=bid, voice=voice, speak=speak, review=False)


def test_regenerating_a_stale_shot_splices_only_its_stale_ranges(store, project):
    from prosperos_hoard import continuity_regen
    _make(store, project, "ref_lamp", ["-f", "lavfi", "-i", "color=c=0xcc8833:size=64x64", "-frames:v", "1"], "image")
    _video(store, project, "film", "testsrc", 12)
    _video(store, project, "fix", "smptebars", 5)
    bid = continuity.create(store, project["id"], "Film", identity={"style": "warm film"})["id"]
    continuity.upsert_entry(store, bid, "lamp", {"kind": "object", "name": "Lamp", "descriptor": "brass", "reference_asset_ids": ["ref_lamp"]})
    continuity.upsert_shot(store, bid, "uno", {"start_s": 0, "end_s": 6, "description": "street"})
    continuity.upsert_shot(store, bid, "dos", {"start_s": 6, "end_s": 12, "prompt": "the lamp on the desk", "camera": "slow push-in", "uses": ["lamp"]})
    for sid in ("uno", "dos"):
        continuity.accept_clip(store, bid, sid, "film")
    continuity.upsert_entry(store, bid, "lamp", {"descriptor": "aged brass"})
    first = segment_repair.repair_segment(store, "film", "fix", 4.0, 8.0, shot_id="dos", bible_id=bid, review=False)
    item = continuity_regen.plan(store, bid)["items"][0]
    assert item["shot_id"] == "dos" and item["ranges_s"] == [[8.0, 12.0]] and item["kept_ranges_s"] == [[6.0, 8.0]]
    assert "Lamp: aged brass" in item["prompt"] and "camera: slow push-in" in item["prompt"] and "warm film" in item["prompt"]
    assert item["references"] == ["ref_lamp"] and item["start_image"] == "ref_lamp" and item["seconds"] == 4.0
    assert item["entries"]["lamp"] == 2
    made = []

    def generate(it, progress):
        made.append(it["shot_id"])
        take = _video(store, project, f"take_{len(made)}", "testsrc2", it["seconds"] + 0.5)
        return {"asset_id": take["id"], "kind": "test", "jobs": ["job-1"]}
    out = continuity_regen.run_job(store, {"params": {"bible_id": bid}}, lambda *a, **k: None, generate)
    r = out["regenerate"]["results"][0]
    assert out["regenerate"]["done"] == 1 and r["ok"] and r["up_to_date"] and r["kept_ranges_s"] == [[6.0, 8.0]]
    bible = continuity.load(store.data_dir, bid)
    assert continuity.view(bible)["stale_shots"] == [] and bible["shots"]["dos"]["clip"]["asset_id"] == r["result_asset_id"]
    reg = bible["shots"]["dos"]["regenerations"][-1]
    assert reg["take_asset_id"] == "take_1" and reg["generator"]["jobs"] == ["job-1"] and reg["entries"]["lamp"] == 2
    assert bible["provenance"][-1]["operation"] == "regenerate"
    before = segment_repair.analyze_video(store.data_dir / store.get_asset(first["asset_id"])["file_path"])["hashes"]
    after = segment_repair.analyze_video(store.data_dir / store.get_asset(r["result_asset_id"])["file_path"])["hashes"]
    assert after[:200] == before[:200] and after[200:300] != before[200:300], r["repairs"]   # 0-8 s kept as it was, 8-12 s new
    # nothing stale: plan is empty; a failing take leaves the shot stale and says why
    assert continuity_regen.plan(store, bid)["items"] == []
    continuity.upsert_entry(store, bid, "lamp", {"descriptor": "copper"})

    def broken(it, progress):
        raise EngineError("take_failed", "no video model")
    bad = continuity_regen.run_job(store, {"params": {"bible_id": bid}}, lambda *a, **k: None, broken)
    assert bad["regenerate"]["failed"] == 1 and "no video model" in bad["regenerate"]["results"][0]["error"]
    assert continuity.view(continuity.load(store.data_dir, bid))["stale_shots"] == ["dos"]


def test_routes_plan_and_use_a_take_for_a_stale_shot(client, data_dir):
    from prosperos_hoard.store import Store
    c, app, _ = client
    store = Store(data_dir)
    project = store.create_project("Reel", brief="b")
    _video(store, project, "main", "testsrc", 6)
    _video(store, project, "newtake", "smptebars", 3)
    bid = c.post("/api/continuity", json={"project": project["id"], "name": "Reel"}).json()["bible_id"]
    c.put(f"/api/continuity/{bid}/entries/lamp", json={"bible_id": bid, "entry_id": "lamp", "fields": {"kind": "object", "name": "Lamp"}})
    c.put(f"/api/continuity/{bid}/shots/two", json={"bible_id": bid, "fields": {"start_s": 3, "end_s": 6, "uses": ["lamp"]}})
    c.post(f"/api/continuity/{bid}/shots/two/clip", json={"bible_id": bid, "asset_id": "main"})
    assert c.post(f"/api/continuity/{bid}/regenerate", json={"bible_id": bid}).json()["items"] == []
    c.put(f"/api/continuity/{bid}/entries/lamp", json={"bible_id": bid, "entry_id": "lamp", "fields": {"descriptor": "brass"}})
    planned = c.post("/api/agent/continuity_regenerate", json={"bible_id": bid, "action": "plan"}).json()
    assert planned["items"][0]["ranges_s"] == [[3.0, 6.0]] and "Lamp: brass" in planned["items"][0]["prompt"]
    r = c.post(f"/api/continuity/{bid}/regenerate", json={"bible_id": bid, "action": "use_clip", "shot_id": "two",
                                                         "clip_asset_id": "newtake", "wait_s": 120})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["state"] == "done", job
    got = c.get(f"/api/continuity/{bid}").json()
    assert got["stale_shots"] == [] and got["shots"]["two"]["regenerations"][-1]["take_asset_id"] == "newtake"
    bad = c.post(f"/api/continuity/{bid}/regenerate", json={"bible_id": bid, "action": "run"})
    assert bad.status_code >= 400 and "not_stale" in bad.text
