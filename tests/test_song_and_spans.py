"""Changing a production's song (library asset, another take, recompose),
shots placed on an exact stretch of the song (pinned spans in the cut),
the timed lyric line for the editor, the Windows-safe state write and the
recovery of a production left "running" by a run that died."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from prosperos_hoard import productions as prod
from prosperos_hoard import timeline as tl
from prosperos_hoard import util
from test_productions import tiny_spec, wait_job


# ------------------------------------------------------- atomic writes

def test_replace_with_retry_survives_a_transient_lock(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a.json"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "Access is denied")  # what WinError 5 maps to
        return real(a, b)

    monkeypatch.setattr(util.os, "replace", flaky)
    util.replace_with_retry(src, dst, delay=0.001)
    assert dst.read_text(encoding="utf-8") == "new" and calls["n"] == 3 and not src.exists()


def test_replace_with_retry_gives_up_and_cleans_the_temp(tmp_path, monkeypatch):
    src, dst = tmp_path / "b.tmp", tmp_path / "b.json"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(util.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(PermissionError):
        util.replace_with_retry(src, dst, attempts=3, delay=0.001)
    assert not src.exists()


def test_other_os_errors_are_not_retried(tmp_path, monkeypatch):
    calls = {"n": 0}

    def missing(a, b):
        calls["n"] += 1
        raise FileNotFoundError(2, "nope")

    monkeypatch.setattr(util.os, "replace", missing)
    with pytest.raises(FileNotFoundError):
        util.replace_with_retry(tmp_path / "c", tmp_path / "d", delay=0.001)
    assert calls["n"] == 1


# ------------------------------------------------- stale "running" state

def test_a_dead_run_does_not_lock_the_production(data_dir):
    state = prod.create_production(data_dir, "Stale", prod.normalise_spec(tiny_spec()), {"animatic": False})
    state.update(status="running", job_id="job_gone")
    prod.save_state(data_dir, state)
    prod.set_job_probe(lambda job_id: None)  # the job no longer exists
    try:
        listed = {p["slug"]: p for p in prod.list_productions(data_dir)}
        assert listed[state["slug"]]["status"] == "failed"
        out = prod.set_song_lyrics(data_dir, state["slug"], "[Verse]\nnew words")
        assert out["status"] == "queued"
        # a live job keeps it locked
        state = prod.load_state(data_dir, state["slug"])
        state.update(status="running", job_id="job_live")
        prod.save_state(data_dir, state)
        prod.set_job_probe(lambda job_id: "running")
        with pytest.raises(prod.ProductionError, match="running"):
            prod.set_song_lyrics(data_dir, state["slug"], "x")
    finally:
        prod.set_job_probe(None)


# ------------------------------------------------------------ spans

def _flat_cut(spans, pool_ids=("p1", "p2", "p3"), duration=20.0):
    beats = [i * 0.5 for i in range(int(duration / 0.5))]
    sections = [{"label": "Verse", "kind": "verse", "energy": "mid", "start_s": 0.0, "end_s": duration}]
    pool = [{"id": i, "kind": "image"} for i in pool_ids]
    return tl.build_auto_cut(duration, beats, sections, pool, {"pinned_spans": spans, "ken_burns_variety": False})


def test_pinned_span_plays_exactly_its_asset_from_start_to_end():
    out = _flat_cut([{"start_s": 6.3, "end_s": 11.1, "assets": [{"id": "SHOT", "kind": "video", "duration_s": 5}]}])
    clips = out["tracks"][0]["clips"]
    pinned = [c for c in clips if c["asset_id"] == "SHOT"]
    assert len(pinned) == 1 and pinned[0]["start_s"] == 6.3
    assert abs(pinned[0]["start_s"] + pinned[0]["duration_s"] - 11.1) < 1e-6
    assert all(c["asset_id"] != "SHOT" for c in clips if c["start_s"] < 6.3 or c["start_s"] >= 11.1)
    assert abs(sum(c["duration_s"] for c in clips) - 20.0) < 1e-6
    assert all(c["duration_s"] >= tl.MIN_CLIP_S - 1e-6 for c in clips)


def test_overlapping_spans_keep_the_first():
    out = _flat_cut([{"start_s": 2, "end_s": 6, "assets": [{"id": "A"}]}, {"start_s": 5, "end_s": 9, "assets": [{"id": "B"}]}])
    ids = [c["asset_id"] for c in out["tracks"][0]["clips"]]
    assert "A" in ids and "B" not in ids


def test_shot_span_is_validated_and_freed(data_dir):
    spec = prod.normalise_spec(tiny_spec())
    state = prod.create_production(data_dir, "Spans", spec, {"animatic": False})
    slug = state["slug"]
    prod.update_shots(data_dir, slug, [{"key": "1", "span": {"start_s": 1.0, "end_s": 3.5}}])
    shot = prod.load_state(data_dir, slug)["spec"]["shots"][0]
    assert (shot["start_s"], shot["end_s"]) == (1.0, 3.5)
    with pytest.raises(prod.ProductionError, match="overlaps"):
        prod.update_shots(data_dir, slug, [{"key": "2", "span": {"start_s": 3.0, "end_s": 5.0}}])
    with pytest.raises(prod.ProductionError, match="0.5 s"):
        prod.update_shots(data_dir, slug, [{"key": "2", "span": {"start_s": 3.0, "end_s": 3.2}}])
    out = prod.update_shots(data_dir, slug, [{"insert": {"after": "2", "prompt": "a new one", "span": {"start_s": 4, "end_s": 6}}}])
    new = next(s for s in prod.load_state(data_dir, slug)["spec"]["shots"] if s["key"] == out["changed"][0])
    assert (new["start_s"], new["end_s"]) == (4.0, 6.0)
    prod.update_shots(data_dir, slug, [{"key": "1", "span": None}])
    assert "start_s" not in prod.load_state(data_dir, slug)["spec"]["shots"][0]


def test_pins_leave_the_free_pools():
    spec = {"shots": [{"key": "1", "start_s": 2.0, "end_s": 4.0}, {"key": "2"}]}
    spans = prod.pinned_spans(spec, lambda k: [f"still_{k}"])
    options: dict = {}
    pool, pools = prod.apply_pins(options, ["still_1", "still_2"], {"chorus": ["still_1", "still_2"]}, spans)
    assert pool == ["still_2"] and pools == {"chorus": ["still_2"]}
    assert options["pinned_spans"] == [{"start_s": 2.0, "end_s": 4.0, "asset_ids": ["still_1"]}]


# ---------------------------------------------------- the song, end to end

def test_change_song_take_library_and_compose_and_place_a_shot(client):
    c, app, _ = client
    store = app.state.store
    song = dict(tiny_spec()["song"], count=2)
    r = c.post("/api/productions", json={"name": "Swap", "spec": tiny_spec(song=song),
                                         "settings": {"song_review": True, "animatic_autocontinue": True}})
    assert r.status_code == 200, r.text
    slug = r.json()["production"]["slug"]
    assert wait_job(c, r.json()["job"]["id"])["state"] == "done"
    takes = c.get(f"/api/productions/{slug}").json()["partial"]["song"]["song_asset_ids"]

    # another take, timed right away
    r = c.put(f"/api/productions/{slug}/song", json={"take": 2})
    assert r.status_code == 200, r.text
    state = c.get(f"/api/productions/{slug}").json()
    assert state["done"]["song"]["song_asset_id"] == takes[1] and state["spec"]["song"]["take"] == 2
    timing = state["timing"]
    assert timing["timed"] is True and timing["lines"] and timing["song_asset_id"] == takes[1]
    first = timing["lines"][0]
    assert first["end_s"] > first["time_s"] and first["section"]

    # a song of another project, from the library: copied in, its own words come with it
    other = store.create_project("Elsewhere")
    lib = store.create_asset(project_id=other["id"], kind="audio",
                             file_path=store.get_asset(takes[0])["file_path"], source="generated",
                             recipe={"params": {"lyrics": "[Chorus]\nbrand new words\nsing it twice"}},
                             duration_s=8.0)
    r = c.put(f"/api/productions/{slug}/song", json={"asset_id": lib["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["lyrics_source"] == "song"
    state = c.get(f"/api/productions/{slug}").json()
    assert state["spec"]["song"]["asset_id"] == lib["id"] and "brand new words" in state["spec"]["song"]["lyrics"]
    assert state["done"]["song"]["song_asset_id"] != lib["id"]  # a copy in the production's project
    assert [l["text"] for l in state["timing"]["lines"]][:1] == ["brand new words"]
    assert c.put(f"/api/productions/{slug}/song", json={"take": 1, "asset_id": lib["id"]}).status_code == 400

    # place shot 2 on the first line through the agent tool, then read the timing
    line = state["timing"]["lines"][0]
    r = c.post(f"/api/agent/studio_production_shots?production={slug}",
               json={"changes": [{"key": "2", "span": {"start_s": line["time_s"], "end_s": line["end_s"]}}], "run": False})
    assert r.status_code == 200, r.text
    t = c.get(f"/api/agent/studio_production_timing?production={slug}").json()
    assert t["spans"]["2"] == {"start_s": round(line["time_s"], 3), "end_s": round(line["end_s"], 3)}

    # recompose through the agent tool: the song is made again on the next run
    r = c.post(f"/api/agent/studio_production_song?production={slug}",
               json={"compose": {"tags": "bright funk", "bpm": 110, "count": 1}})
    assert r.status_code == 200, r.text
    state = c.get(f"/api/productions/{slug}").json()
    assert "song" not in state["done"] and state["spec"]["song"]["tags"] == "bright funk"
    assert "asset_id" not in state["spec"]["song"]
    assert c.put(f"/api/productions/{slug}/song", json={}).status_code == 400


def test_a_pinned_shot_lands_on_its_stretch_in_the_animatic(client):
    c, app, _ = client
    spec = tiny_spec()
    spec["timeline"] = dict(spec["timeline"], storyboard={})
    spec["shots"][1]["start_s"], spec["shots"][1]["end_s"] = 2.0, 5.0
    r = c.post("/api/productions", json={"name": "Pinned", "spec": spec, "settings": {"animatic": True}})
    assert r.status_code == 200, r.text
    slug = r.json()["production"]["slug"]
    assert wait_job(c, r.json()["job"]["id"])["state"] == "done"
    plan = c.get(f"/api/productions/{slug}/animatic").json()
    on_2 = [cut for cut in plan["cuts"] if cut["shot"].startswith("2")]
    assert on_2 and abs(on_2[0]["start_s"] - 2.0) < 1e-3
    assert abs(sum(cut["duration_s"] for cut in on_2) - 3.0) < 1e-3
    assert all(2.0 - 1e-3 <= cut["start_s"] < 5.0 for cut in on_2)
    timing = c.get(f"/api/productions/{slug}").json()["timing"]
    assert timing["spans"]["2"] == {"start_s": 2.0, "end_s": 5.0}
    bad = tiny_spec()
    bad["shots"][0].update(start_s=1, end_s=4)
    bad["shots"][1].update(start_s=3, end_s=6)
    assert c.post("/api/productions", json={"name": "Bad", "spec": bad}).status_code == 400
