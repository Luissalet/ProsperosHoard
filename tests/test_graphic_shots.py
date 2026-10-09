"""Graphic shots in a production: spec, editing, the cut, the animatic and the final render."""
from __future__ import annotations

import pytest

from prosperos_hoard import graphic_shots, productions as prod, timeline as tl

from test_productions import tiny_spec, wait_job
from test_shot_editor import _paused

TITLE = {"grammar": "title_card", "data": {"title": "Noche de lámparas", "subtitle": "WISP"}}
KINETIC = {"grammar": "kinetic_lyrics", "data": {"layout": "line"}}
LOWER = {"grammar": "lower_third", "data": {"name": "WISP", "caption": "voz y polillas"}}


def graphic_spec(**over):
    spec = tiny_spec()
    spec["shots"] += [
        {"key": "3", "kind": "graphic", "graphic": TITLE, "start_s": 0, "end_s": 2},
        {"key": "4", "kind": "graphic", "graphic": KINETIC, "start_s": 2.0, "end_s": 6.0},
        {"key": "5", "kind": "graphic", "graphic": LOWER, "start_s": 3.0, "end_s": 5.0},
    ]
    spec.update(over)
    return spec


# ---------------------------------------------------------------------- spec

def test_a_graphic_shot_needs_a_span_a_valid_graphic_and_loses_its_generation_fields():
    spec = prod.normalise_spec(graphic_spec())
    title = next(s for s in spec["shots"] if s["key"] == "3")
    assert title["kind"] == "graphic" and title["clips"] == [] and title["variants"] == 1 and title["lead"] is False
    assert title["prompt"] == "Title card: Noche de lámparas"
    assert title["graphic"]["mode"] == "clip" and "duration" not in title["graphic"]
    assert next(s for s in spec["shots"] if s["key"] == "5")["graphic"]["mode"] == "overlay"
    bad = graphic_spec()
    del bad["shots"][2]["start_s"], bad["shots"][2]["end_s"]
    with pytest.raises(prod.ProductionError, match="needs start_s and end_s"):
        prod.normalise_spec(bad)
    bad = graphic_spec()
    bad["shots"][2]["graphic"] = {"grammar": "spinning_logo"}
    with pytest.raises(prod.ProductionError, match="grammar"):
        prod.normalise_spec(bad)
    plain = tiny_spec()
    plain["shots"][0]["graphic"] = TITLE
    assert "graphic" not in prod.normalise_spec(plain)["shots"][0]


def test_an_overlay_may_share_a_stretch_but_two_clip_shots_may_not():
    prod.normalise_spec(graphic_spec())  # shot 5 (overlay, 3-5 s) sits over shot 4 (2-6 s)
    bad = graphic_spec()
    bad["shots"][3]["end_s"] = 3.5
    bad["shots"].append({"key": "6", "kind": "graphic", "graphic": TITLE, "start_s": 3.0, "end_s": 4.0})
    with pytest.raises(prod.ProductionError, match="overlaps"):
        prod.normalise_spec(bad)


def test_the_production_default_look_is_validated():
    spec = prod.normalise_spec(graphic_spec(graphics={"style": "Neón nocturno", "look": {"transition": "glitch"}}))
    assert spec["graphics"] == {"style": "Neón nocturno", "look": {"transition": "glitch"}}
    with pytest.raises(prod.ProductionError, match="transition"):
        prod.normalise_spec(graphic_spec(graphics={"look": {"transition": "teleport"}}))


# --------------------------------------------------------------------- looks

def test_the_look_comes_from_the_shot_then_its_card_then_the_production_then_the_lead(store, project):
    state = {"project_id": project["id"], "spec": prod.normalise_spec(graphic_spec())}
    plain = graphic_shots.resolve_look(store, state, {"grammar": "title_card"})
    assert plain["palette"][0] == "#1b1d22" and plain["palette"][2] == "#f28c28"  # the lead's palette
    state["spec"]["graphics"] = {"style": "VHS terror"}
    assert graphic_shots.resolve_look(store, state, {"grammar": "title_card"})["transition"] == "glitch"
    card = graphic_shots.resolve_look(store, state, {"grammar": "title_card", "style": "papel recortado"})
    assert card["transition"] == "slide_up" and card["motion"]["stepped_fps"] == 12
    own = graphic_shots.resolve_look(store, state, {"grammar": "title_card", "style": "papel recortado", "look": {"transition": "fade"}})
    assert own["transition"] == "fade" and own["palette"] == card["palette"]
    state["spec"]["shots"][2]["graphic"]["style"] = "No existe"
    assert graphic_shots.unknown_styles(store, state) == ["No existe"]


# ------------------------------------------------------------------- editing

def test_shots_can_be_made_graphic_changed_and_inserted(data_dir):
    slug = _paused(data_dir)
    prod.update_shots(data_dir, slug, [{"insert": {"after": "1", "kind": "graphic", "graphic": TITLE, "span": {"start_s": 0, "end_s": 2}}}])
    st = prod.load_state(data_dir, slug)
    new = st["spec"]["shots"][1]
    assert new["kind"] == "graphic" and new["clips"] == [] and (new["start_s"], new["end_s"]) == (0, 2)
    assert st["done"]["frames"]["complete"] is False and st["status"] == "queued"
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    prod.update_shots(data_dir, slug, [{"key": new["key"], "graphic": {"data": {"subtitle": "otra vez"}, "look": {"transition": "wipe"}}}])
    st = prod.load_state(data_dir, slug)
    shot = st["spec"]["shots"][1]
    assert shot["graphic"]["data"] == {"title": "Noche de lámparas", "subtitle": "otra vez", "kicker": ""}
    assert shot["graphic"]["look"]["transition"] == "wipe"
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    # a partial look is laid over the old one; an empty look clears the overrides
    prod.update_shots(data_dir, slug, [{"key": new["key"], "graphic": {"look": {"motion": {"easing": "out_cubic"}}}}])
    look = next(s for s in prod.load_state(data_dir, slug)["spec"]["shots"] if s["key"] == new["key"])["graphic"]["look"]
    assert look["transition"] == "wipe" and look["motion"]["easing"] == "out_cubic"
    st = prod.load_state(data_dir, slug)
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    prod.update_shots(data_dir, slug, [{"key": new["key"], "graphic": {"look": {}}}])
    assert not next(s for s in prod.load_state(data_dir, slug)["spec"]["shots"] if s["key"] == new["key"])["graphic"].get("look")
    st = prod.load_state(data_dir, slug)
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    # a generated shot becomes a graphic only with its graphic and a span
    with pytest.raises(prod.ProductionError, match="needs its graphic"):
        prod.update_shots(data_dir, slug, [{"key": "2", "kind": "graphic"}])
    with pytest.raises(prod.ProductionError, match="needs start_s and end_s"):
        prod.update_shots(data_dir, slug, [{"key": "2", "kind": "graphic", "graphic": LOWER}])
    prod.update_shots(data_dir, slug, [{"key": "2", "kind": "graphic", "graphic": LOWER, "span": {"start_s": 3, "end_s": 5}}])
    st = prod.load_state(data_dir, slug)
    two = next(s for s in st["spec"]["shots"] if s["key"] == "2")
    assert two["kind"] == "graphic" and two["clips"] == [] and "2" not in st["done"]["frames"]["items"] and "2" not in st["done"]["clips"]["items"]
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    prod.update_shots(data_dir, slug, [{"key": "2", "kind": "image"}])
    assert "graphic" not in next(s for s in prod.load_state(data_dir, slug)["spec"]["shots"] if s["key"] == "2")
    with pytest.raises(prod.ProductionError, match="kind is"):
        prod.update_shots(data_dir, slug, [{"key": "2", "kind": "hologram"}])


def test_changing_the_grammar_replaces_the_graphic(data_dir):
    slug = _paused(data_dir)
    prod.update_shots(data_dir, slug, [{"insert": {"after": "1", "kind": "graphic", "graphic": TITLE, "span": {"start_s": 0, "end_s": 2}}}])
    st = prod.load_state(data_dir, slug)
    st["status"] = "awaiting_review"
    prod.save_state(data_dir, st)
    prod.update_shots(data_dir, slug, [{"key": "3", "graphic": {"grammar": "outro_card", "data": {"title": "Fin"}}}])
    shot = next(s for s in prod.load_state(data_dir, slug)["spec"]["shots"] if s["key"] == "3")
    assert shot["graphic"]["grammar"] == "outro_card" and "subtitle" in shot["graphic"]["data"] and shot["prompt"] == "Outro card: Fin"


# -------------------------------------------------------------- the cut (pure)

def test_the_cut_turns_the_poster_clips_into_graphic_clips_and_overlays_into_a_track():
    visual = [{"asset_id": "a_img", "kind": "image", "start_s": 0.0, "duration_s": 2.0, "trim_start_s": 0.0,
               "ken_burns": {"zoom_start": 1.0, "zoom_end": 1.1, "pan": "left"}, "transition_in": {"type": "cut", "duration_s": 0.0}},
              {"asset_id": "a_poster", "kind": "image", "start_s": 2.0, "duration_s": 2.0, "trim_start_s": 0.0, "transition_in": {"type": "cut"}},
              {"asset_id": "a_poster", "kind": "image", "start_s": 4.0, "duration_s": 2.0, "trim_start_s": 0.0, "transition_in": {"type": "cut"}}]
    shots = [{"key": "4", "poster": "a_poster", "start_s": 2.0, "end_s": 6.0,
              "graphic": {"grammar": "kinetic_lyrics", "mode": "clip", "data": {"layout": "line", "snap_to_beats": True}}},
             {"key": "5", "poster": None, "start_s": 3.0, "end_s": 5.0, "graphic": {"grammar": "lower_third", "mode": "overlay", "data": {"name": "WISP"}}}]
    lyrics = [{"time_s": 2.0, "text": "uno dos"}, {"time_s": 4.0, "text": "tres cuatro"}, {"time_s": 7.0, "text": "fuera"}]
    out = tl.inject_graphics([{"type": "visual", "clips": visual}], shots, lyrics, [2.5, 3.0, 4.0, 9.0], 8.0)
    clips = out[0]["clips"]
    assert clips[0]["kind"] == "image" and clips[0]["ken_burns"]
    assert [c["kind"] for c in clips[1:]] == ["graphic", "graphic"]
    assert [c["graphic_offset_s"] for c in clips[1:]] == [0.0, 2.0]  # the second picks up where the first stopped
    spec = clips[1]["graphic"]
    assert spec["duration"] == 4.0 and spec["window_start_s"] == 2.0
    assert [(ln["text"], ln["start_s"], ln["end_s"]) for ln in spec["data"]["lines"]] == [("uno dos", 0.0, 2.0), ("tres cuatro", 2.0, 4.0)]
    assert spec["data"]["beats"] == [0.5, 1.0, 2.0]
    assert "ken_burns" not in clips[1] and clips[1]["asset_id"] == "a_poster"
    overlay = out[1]
    assert overlay["type"] == "graphics" and overlay["clips"][0]["start_s"] == 3.0 and overlay["clips"][0]["graphic"]["duration"] == 2.0
    # the input cut is untouched
    assert visual[1]["kind"] == "image"


# -------------------------------------------------------------- end to end

def test_a_production_with_graphic_shots_runs_end_to_end(client):
    c, app, _ = client
    r = c.post("/api/agent/studio_production_create", json={"name": "Graphics", "spec": graphic_spec(graphics={"style": "Neón nocturno"})})
    assert r.status_code == 200, r.text
    slug = r.json()["production"]["slug"]
    job = wait_job(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    store = app.state.store
    state = c.get(f"/api/productions/{slug}").json()
    frames = state["done"]["frames"]["items"]
    assert frames["3"]["graphic"] and frames["4"]["graphic"] and frames["5"] == {"variants": [], "best": None, "graphic": True}
    poster = store.get_asset(frames["3"]["best"])
    assert poster["kind"] == "image" and poster["recipe"]["operation"] == "graphic_poster"
    assert "3" not in state["done"]["clips"]["items"] if state["done"].get("clips") else True
    # the animatic already plays the graphics (it paused for review)
    animatic = c.get(f"/api/agent/studio_production?production={slug}").json()["animatic"]
    assert store.get_asset(animatic["renders"]["9:16"])["kind"] == "video"
    r = c.post(f"/api/agent/studio_production_continue?production={slug}")
    assert wait_job(c, r.json()["job"]["id"])["state"] == "done"
    state = c.get(f"/api/productions/{slug}").json()
    assert state["status"] == "done", state["message"]
    tl_id = state["done"]["timeline"]["timelines"]["9:16"]["timeline_id"]
    timeline = store.get_timeline(tl_id)
    visual = next(t for t in timeline["tracks"] if t["type"] == "visual")["clips"]
    kinds = [c["kind"] for c in visual]
    assert "graphic" in kinds
    title = next(c for c in visual if c["kind"] == "graphic" and c["graphic"]["grammar"] == "title_card")
    assert title["start_s"] == 0.0 and title["duration_s"] == 2.0 and title["graphic"]["look"]["palette"][2] == "#ff2e93"
    graphics = next(t for t in timeline["tracks"] if t["type"] == "graphics")["clips"]
    assert [(g["start_s"], g["end_s"], g["graphic"]["grammar"]) for g in graphics] == [(3.0, 5.0, "lower_third")]
    final = store.get_asset(state["done"]["timeline"]["timelines"]["9:16"]["renders"]["preview"])
    assert final["kind"] == "video" and abs(final["duration_s"] - 8.0) < 0.3


# ------------------------------------------------------------------------ QA

def _qa_state(store, project, shots, lrc=None):
    from prosperos_hoard import engine

    spec = prod.normalise_spec(graphic_spec())
    spec["shots"] = shots
    done = {}
    if lrc:
        done["lyrics"] = {"lyrics_asset_id": engine.create_lyrics(store, project["id"], lrc)["id"]}
    return {"project_id": project["id"], "slug": "t", "spec": spec, "done": done}


def _shots(*keys):
    """The graphic shots of `graphic_spec()` (all of them, or those with these keys)."""
    spec = prod.normalise_spec(graphic_spec())
    return [s for s in spec["shots"] if s.get("kind") == "graphic" and (not keys or s["key"] in keys)]


def test_qa_passes_well_set_graphics(store, project):
    from prosperos_hoard import qa

    state = _qa_state(store, project, _shots(),
                      lrc="[00:00.00]uno\n[00:03.00]dos tres\n[00:05.50]cuatro\n")
    items = qa.QA(store, state).check_graphics()
    assert {i["key"] for i in items} == {"3", "4", "5"}
    assert [i for i in items if i["verdict"] == "fail"] == [], [i["reasons"] for i in items]
    assert all(i["checks"]["determinism"]["deterministic"] for i in items)


def test_qa_flags_text_outside_the_safe_area_and_a_lower_third_in_the_caption_band(store, project):
    from prosperos_hoard import qa

    shots = _shots("3", "5")
    shots[0]["graphic"]["safe"] = {"top": 0.4, "bottom": 0.4, "side": 0.4}
    shots[1]["graphic"]["safe"] = {"bottom": 0.02}
    state = _qa_state(store, project, shots, lrc="[00:00.00]uno\n[00:03.00]dos tres\n[00:05.50]cuatro\n")
    by_key = {i["key"]: i for i in qa.QA(store, state).check_graphics()}
    assert by_key["3"]["verdict"] == "fail" and set(by_key["3"]["codes"]) & {"outside_safe", "text_clipped", "text_truncated"}
    assert by_key["5"]["verdict"] == "fail" and "text_in_subtitle_band" in by_key["5"]["codes"]
    # without lyrics there are no burned-in captions to keep clear of
    quiet = _qa_state(store, project, [shots[1]])
    assert "text_in_subtitle_band" not in qa.QA(store, quiet).check_graphics()[0]["codes"]


def test_qa_flags_kinetic_lyrics_without_lines_an_unknown_style_and_a_flaky_render(store, project, monkeypatch):
    from prosperos_hoard import motion_graphics, qa

    kinetic = _shots("4")
    kinetic[0]["graphic"]["style"] = "No existe"
    state = _qa_state(store, project, kinetic)
    item = qa.QA(store, state).check_graphics()[0]
    assert {"no_lines", "unknown_style"} <= set(item["codes"])
    monkeypatch.setattr(motion_graphics, "determinism_check", lambda *a, **k: {"deterministic": False, "frames": []})
    assert "not_deterministic" in qa.QA(store, _qa_state(store, project, _shots("3"))).check_graphics()[0]["codes"]


def test_the_graphics_stage_is_part_of_qa_and_leaves_generated_checks_alone():
    from prosperos_hoard import qa

    assert "graphics" in qa.CHECKED_STAGES and "graphics" not in qa.RETRYABLE_STAGES
