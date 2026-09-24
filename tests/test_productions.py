"""In-app productions and recipes: spec validation, the pipeline end to end
against the fake ComfyUI (through the real HTTP API and job queue), recipe
export with the lead abstracted into a `{lead}` slot (from an app-made and
from a scripted production), and running a recipe with a new lead."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PIL import Image

from prosperos_hoard import productions as prod
from prosperos_hoard import recipes

LYRICS = "[Verse]\nlamps along the road\nshadows at my back\n[Chorus]\nkeep on walking home\ndon't look back"


def tiny_spec(**overrides):
    spec = {
        "title": "Night Walk",
        "lead": {"name": "WISP", "look": "WISP, a small glowing moth spirit with paper wings and ember eyes",
                 "negative": "cartoon, text", "palette": ["#F28C28", "#1B1D22"], "bio": "A moth that follows lamps."},
        "reference": {"seed": 11, "count": 1, "aspect": "16:9", "crop": "left_third"},
        "world": {"look": "night street, sodium lamps, fog", "negative": "daylight, text"},
        "song": {"tags": "dark synth, 120 bpm", "lyrics": LYRICS, "bpm": 120, "duration": 8, "seed": 21},
        "shots": [
            {"key": "1", "lead": True, "prompt": "hovering under a lamp", "seed": 31, "variants": 2, "width": 512, "height": 288,
             "clips": [0], "motion": "still", "motion_prompt": "wings flutter, the lamp flickers"},
            {"key": "2", "lead": False, "prompt": "an empty wet street", "seed": 41, "width": 512, "height": 288,
             "clips": [0], "motion": "move", "motion_prompt": "rain falling"},
        ],
        "album": [{"template": "album_cover", "variant": "night", "image_shot": "1",
                   "fields": {"title": "Night Walk", "artist": "WISP", "accent": "#F28C28"}}],
        "timeline": {"aspects": ["9:16"], "qualities": ["preview"], "options": {"fps": 24, "karaoke": True},
                     "storyboard": {"Verse": ["1", "2"], "Chorus": ["2", "1v2"]}, "finishing": {"vignette": True}},
    }
    spec.update(overrides)
    return spec


def wait_job(c, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("queued", "waiting_gpu", "running"):
            return job
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish")


# ------------------------------------------------------------------ units

def test_slug_and_keys():
    assert prod.slugify("NO MIRES ATRÁS") == "no_mires_atras"
    assert prod.split_key("3") == ("3", 0) and prod.split_key("3v2") == ("3", 1)
    assert prod.shot_key("3", 1) == "3v2"


def test_normalise_spec_fills_defaults_and_rejects_bad_fields():
    spec = prod.normalise_spec(tiny_spec())
    assert spec["shots"][0]["best"] == 0 and spec["shots"][1]["clips"] == [0]
    assert spec["clip_settings"]["template"] == "wan22_ti2v"
    with pytest.raises(prod.ProductionError, match="lead"):
        prod.normalise_spec(tiny_spec(lead={"name": "x"}))
    with pytest.raises(prod.ProductionError, match="unknown shot"):
        prod.normalise_spec(tiny_spec(timeline={"storyboard": {"Verse": ["9"]}}))
    with pytest.raises(prod.ProductionError, match="variant indices"):
        bad = tiny_spec()
        bad["shots"][1]["clips"] = [3]
        prod.normalise_spec(bad)
    settings = prod.normalise_settings({"qa": {"enabled": True, "max_retries": 1}})
    assert settings["qa"] == {"enabled": True, "thresholds": {}, "max_retries": 1} and settings["animatic"] is True


def test_abstract_and_fill_round_trip():
    spec = prod.normalise_spec(tiny_spec())
    abstract, warnings, legend = recipes.abstract_spec(spec)
    text = json.dumps(abstract)
    assert "WISP" not in text and "moth spirit" not in text
    assert abstract["lead"]["name"] == "{lead}" and abstract["album"][0]["fields"]["artist"] == "{lead}"
    assert abstract["album"][0]["fields"]["accent"] == "{lead.palette[0]}"
    assert abstract["album"][0]["fields"]["title"] == "{title}"
    assert "{lead.palette[1]}" in legend
    new_lead = {"name": "KOI", "look": "KOI, a paper koi kite", "palette": ["#3366FF"], "negative": "", "bio": ""}
    filled = recipes.fill(abstract, new_lead, "Kite Song")
    assert filled["album"][0]["fields"] == {"title": "Kite Song", "artist": "KOI", "accent": "#3366FF"}


def test_look_words_in_shot_prompts_are_flagged():
    spec = tiny_spec()
    spec["shots"][0]["prompt"] = "its paper wings glowing under a lamp"
    _, warnings, _ = recipes.abstract_spec(prod.normalise_spec(spec))
    assert any("shot 1" in w and "wings" in w for w in warnings)


# ------------------------------------------------------------- end to end

def test_production_runs_end_to_end_and_exports_a_recipe(client):
    c, app, _ = client
    r = c.post("/api/agent/studio_production_create", json={"name": "Night Walk", "spec": tiny_spec()})
    assert r.status_code == 200, r.text
    body = r.json()
    slug = body["production"]["slug"]
    job = wait_job(c, body["job"]["id"])
    assert job["state"] == "done", job
    # the animatic (on by default) pauses the production before any clip is made
    paused = c.get(f"/api/agent/studio_production?production={slug}").json()
    assert paused["status"] == "awaiting_review" and paused["stages"]["clips"] == "pending", paused
    assert paused["animatic"]["clips_planned"] == 2 and paused["animatic"]["gpu_minutes_estimate"] == 19.0
    store = app.state.store
    animatic_video = store.get_asset(paused["animatic"]["renders"]["9:16"])
    assert animatic_video["kind"] == "video" and animatic_video["height"] == 1280
    assert abs(animatic_video["duration_s"] - 8.0) < 0.3
    plan = c.get(f"/api/productions/{slug}/animatic").json()
    assert plan["cuts_total"] >= 2 and {s["key"] for s in plan["shots"]} == {"1", "2"}
    r = c.post(f"/api/agent/studio_production_continue?production={slug}")
    assert r.status_code == 200, r.text
    job = wait_job(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    state = c.get(f"/api/productions/{slug}").json()
    assert state["status"] == "done", state["message"]
    assert all(v == "done" for v in state["view"]["stages"].values()), state["view"]["stages"]
    done = state["done"]
    # the lead's canonical reference was cropped from the sheet
    assert done["character"]["canonical_asset_id"] and done["character"]["sheet_asset_id"]
    assert len(done["frames"]["items"]["1"]["variants"]) == 2
    assert set(done["clips"]["items"]) == {"1", "2"}
    assert state["review"]["animatic_approved"] == done["animatic"]["made_at"]
    # a still subject gets the stillness negative (no walking), a moving one the stock negative
    still_clip = store.get_asset(done["clips"]["items"]["1"])["recipe"]["params"]["negative_prompt"]
    moving_clip = store.get_asset(done["clips"]["items"]["2"])["recipe"]["params"]["negative_prompt"]
    assert "walking" in still_clip and "walking" not in moving_clip
    # the lead shot is an edit from the canonical reference; the other a txt2img
    lead_frame = store.get_asset(done["frames"]["items"]["1"]["best"])["recipe"]
    assert done["character"]["canonical_asset_id"] in lead_frame["input_asset_ids"]
    assert store.get_asset(done["frames"]["items"]["2"]["best"])["recipe"]["input_asset_ids"] == []
    render = done["timeline"]["timelines"]["9:16"]["renders"]["preview"]
    assert store.get_asset(render)["kind"] == "video"
    report = c.get(f"/api/productions/{slug}/report").text
    assert "Night Walk" in report and "| 1 | yes |" in report
    events = [e["event"] for e in state["lineage"]]
    assert "canonical_reference" in events and "render" in events
    assert events.index("paused_for_review") < events.index("clip")

    # an animatic on demand, in one aspect
    r = c.post("/api/agent/studio_animatic", json={"production": slug, "aspects": ["16:9"], "wait_s": 60})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["state"] == "done" and list(r.json()["animatic"]["renders"]) == ["16:9"]

    # export: the lead becomes a slot, what does not depend on it stays reusable
    r = c.post("/api/agent/studio_recipe_export", json={"production": slug, "name": "night walk"})
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["name"] == "night_walk" and summary["original_lead"] == "WISP"
    assert summary["reusable"] == {"song": True, "frames": 1, "clips": 1}
    recipe = c.get("/api/recipes/night_walk").json()
    assert "WISP" not in json.dumps(recipe["spec"]) and "moth spirit" not in json.dumps(recipe["spec"])
    assert recipe["cast"]["lead"]["example"]["name"] == "WISP"
    assert c.get("/api/agent/studio_recipes_list").json()["items"][0]["name"] == "night_walk"
    got = c.get("/api/agent/studio_recipe_get?recipe=night_walk").json()
    assert got["shot_list"][0]["lead"] is True and "{lead}" in json.dumps(got["cast"])

    # run it with a new lead: lead shots are made again, the rest is reused
    # (with the QA director inline; no vision model here, so model checks skip)
    app.state.qa_vision = (None, "no vision model")
    r = c.post("/api/agent/studio_recipe_run",
               json={"recipe": "night_walk", "cast": {"lead": {"name": "KOI", "look": "KOI, a paper koi kite with gold scales",
                                                               "palette": ["#3366FF"]}},
                     "name": "Night Walk KOI", "options": {"settings": {"animatic": False, "qa": {"enabled": True}}}})
    assert r.status_code == 200, r.text
    run = r.json()
    job = wait_job(c, run["job"]["id"])
    assert job["state"] == "done", job
    new = c.get(f"/api/productions/{run['production']['slug']}").json()
    assert new["status"] == "done", new["message"]
    assert new["spec"]["lead"]["name"] == "KOI"
    assert new["recipe"]["name"] == "night_walk" and new["recipe"]["reused_frames"] == ["2"]
    new_events = {e["event"] for e in new["lineage"]}
    assert {"reused_song", "reused_frame", "reused_clip", "created_character"} <= new_events
    new_store_frame = store.get_asset(new["done"]["frames"]["items"]["1"]["best"])
    assert "KOI" in (new_store_frame["recipe"].get("matched_characters") or [])
    assert new["project_id"] != state["project_id"]
    cover = store.get_asset(new["done"]["album"]["designs"][0]["asset_id"])["recipe"]["fields"]
    assert cover["artist"] == "KOI" and cover["accent"] == "#3366FF"
    checked = {h["stage"] for h in new["qa"]["history"]}
    assert {"character", "frames", "clips", "timeline"} <= checked
    assert new["qa"]["last"]["vision"] == "no vision model"


def test_change_shots_invalidates_what_depends_on_them(data_dir):
    spec = prod.normalise_spec(tiny_spec())
    state = prod.create_production(data_dir, "Edit Me", spec, {"animatic": False})
    state["status"] = "awaiting_review"
    state["done"] = {"frames": {"complete": True, "items": {"1": {"variants": ["a_1", "a_2"], "best": "a_1"},
                                                            "2": {"variants": ["a_3"], "best": "a_3"}}},
                     "clips": {"complete": True, "items": {"1": "c_1", "2": "c_2"}},
                     "timeline": {"complete": True, "timelines": {}}, "report": {"path": "REPORT.md"}}
    prod.save_state(data_dir, state)
    out = prod.update_shots(data_dir, state["slug"], [{"key": "1", "best": 1}, {"key": "2", "regenerate": True},
                                                      {"key": "2", "clip": False}])
    assert out["changed"] == ["1", "2", "2"] and out["status"] == "queued"
    after = prod.load_state(data_dir, state["slug"])
    assert after["done"]["frames"]["items"]["1"]["best"] == "a_2"
    assert "2" not in after["done"]["frames"]["items"] and after["done"]["frames"]["complete"] is False
    assert after["done"]["clips"]["items"] == {} and after["done"]["clips"]["complete"] is False
    assert "timeline" not in after["done"] and "report" not in after["done"]
    assert after["spec"]["shots"][1]["seed"] == 41 + 1000 and after["spec"]["shots"][1]["clips"] == []
    with pytest.raises(prod.ProductionError):
        prod.update_shots(data_dir, state["slug"], [{"key": "9"}])


# ---------------------------------------------------- scripted production

def _png(store, name: str) -> str:
    path = store.data_dir / "assets" / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), (40, 40, 60)).save(path)
    return path.relative_to(store.data_dir).as_posix()


def _asset(store, pid, kind="image", **recipe):
    return store.create_asset(project_id=pid, kind=kind, file_path=_png(store, f"x{time.monotonic_ns()}"), source="generated",
                              recipe=recipe)["id"]


def test_recipe_from_a_scripted_production_state(store, project):
    """A fake state.json in the production script's format: the spec is
    rebuilt from the ids it recorded and each asset's own recipe."""
    pid = project["id"]
    look = "FAROL, a tall urban night creature, its head an old paper lantern with a candle flame inside"
    night = "cinematic 35mm film still, night, sodium lamps, fog"
    char = store.create_character(pid, "FAROL", prompt=look, negative="cartoon, cute", palette=["#F28C28", "#1B1D22"],
                                  role="lead", bio="Always a little closer.")
    sheet = _asset(store, pid, operation="generate_image", prompt=f"{look}, character turnaround reference sheet",
                   params={"seed": 1001}, image_engine="qwen21")
    canonical = _asset(store, pid, operation="crop")
    song = _asset(store, pid, kind="audio", operation="compose_song", tags="electronic rock, horror anthem", bpm=130,
                  key="D minor", language="en", params={"lyrics": "[Verse]\nI follow you, FAROL is my name", "duration": 150,
                                                        "timesignature": "4", "seed": 2001})
    s1 = [_asset(store, pid, prompt=f"@FAROL standing under a far lamp, {night}", matched_characters=["FAROL"],
                 params={"seed": 3010 + i, "width": 1344, "height": 768}) for i in range(2)]
    s4 = [_asset(store, pid, prompt=f"a phone in a trembling hand, {night}",
                 params={"seed": 3040, "width": 1344, "height": 768, "negative_prompt": "cartoon, cute"})]
    c1 = _asset(store, pid, kind="video", prompt="rain falling; the figure stays still", template="wan22_ti2v",
                input_asset_ids=[s1[0]], params={"seed": 5001, "negative_prompt": "..., walking, stepping"})
    c1b = _asset(store, pid, kind="video", prompt="rain falling", template="wan22_ti2v", input_asset_ids=[s1[1]],
                 params={"seed": 5101, "negative_prompt": "..., walking"})
    c4 = _asset(store, pid, kind="video", prompt="the screen glow flickers", template="wan22_ti2v", input_asset_ids=[s4[0]],
                params={"seed": 5004, "negative_prompt": "stock"})
    photo = _asset(store, pid, prompt="@FAROL posing on a pastel backdrop, full-length portrait, the whole figure in frame "
                                      "with clear empty space above the lantern head", params={"seed": 4001})
    front = _asset(store, pid, operation="design", template="photocard_front", fields={"role": "Visual", "accent": "#F4A7C0"})
    back = _asset(store, pid, operation="design", template="photocard_back", fields={"message": "thanks for coming"})
    cover = _asset(store, pid, operation="design", template="album_cover", variant="night",
                   fields={"image": s1[0], "title": "DON'T LOOK BACK", "artist": "FAROL", "accent": "#F28C28"})
    folder = store.data_dir / "productions" / "dont_look_back"
    folder.mkdir(parents=True)
    legacy = {"done": {
        "1": {"project_id": pid},
        "2": {"character_id": char["id"], "reference_asset_ids": [sheet], "sheet_asset_id": sheet,
              "canonical_asset_id": canonical, "canonical_crop": "left_third"},
        "3": {"song_asset_ids": [song], "song_asset_id": song, "duration_s": 150},
        "4": {"stills": {"1": {"aspect_16_9": s1, "best": s1[0]}, "4": {"aspect_16_9": s4, "best": s4[0]}}},
        "5": {"clips": {"1": c1, "1v2": c1b, "4": c4}},
        "6": {"photo_ids": [photo], "front_ids": [front], "back_ids": [back], "contact_sheet_id": "a_x"},
        "7": {"cover_id": cover},
        "8": {"timelines": {"16:9": {"timeline_id": "tl_missing", "renders": {"preview": "a_r", "final": "a_f"}}},
              "finishing": {"color_grade": "sodium_night", "grain": 0.3}, "lyrics_asset_id": None},
    }}
    (folder / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
    listed = prod.list_productions(store.data_dir)
    assert listed[0]["legacy"] is True and listed[0]["slug"] == "dont_look_back"

    recipe = recipes.export_recipe(store, "dont_look_back", "farol anthem")
    spec = recipe["spec"]
    assert recipe["title"] == "DON'T LOOK BACK"
    assert spec["world"]["look"] == night and spec["world"]["negative"] == "cartoon, cute"
    shots = {s["key"]: s for s in spec["shots"]}
    assert shots["1"]["lead"] is True and shots["1"]["prompt"] == "standing under a far lamp"
    assert shots["1"]["clips"] == [0, 1] and shots["1"]["motion"] == "still" and shots["1"]["clip_seed"] == 5001
    assert shots["4"]["lead"] is False and shots["4"]["motion"] == "move" and "negative" not in shots["4"]
    assert spec["reference"]["prompt"].startswith("{look}") and spec["reference"]["crop"] == "left_third"
    assert spec["song"]["lyrics"].endswith("{lead} is my name") and spec["song"]["bpm"] == 130
    assert recipe["reusable"]["song"]["mentions_lead"] is True
    assert recipe["reusable"]["frames"] == {"4": s4} and recipe["reusable"]["clips"] == {"4": c4}
    assert spec["photocards"]["looks"][0]["prompt"] == "posing on a pastel backdrop"
    assert spec["photocards"]["looks"][0]["role"] == "Visual" and spec["photocards"]["looks"][0]["message"] == "thanks for coming"
    assert spec["album"][0]["image_shot"] == "1" and spec["album"][0]["fields"]["artist"] == "{lead}"
    assert spec["album"][0]["fields"]["title"] == "{title}" and spec["album"][0]["fields"]["accent"] == "{lead.palette[0]}"
    assert spec["timeline"]["aspects"] == ["16:9"] and spec["timeline"]["qualities"] == ["preview", "final"]
    assert "FAROL" not in json.dumps(spec)
    assert any("lantern" in w for w in recipe["warnings"])  # the framing names the old lead's head
    assert (store.data_dir / "recipes" / "farol_anthem.json").is_file()

    # a new lead: the song names the old one, so it is composed again; the
    # phone shot (no lead) and its clip are reused
    spec2, settings, meta = recipes.plan_run(store, recipe, {"lead": {"name": "KOI", "look": "KOI, a paper kite"}},
                                             {"title": "KITE"})
    assert "asset_id" not in spec2["song"] and "KOI is my name" in spec2["song"]["lyrics"]
    assert any("name the original lead" in n for n in meta["notes"])
    phone = next(s for s in spec2["shots"] if s["key"] == "4")
    assert phone["reuse_asset_ids"] == s4 and phone["reuse_clips"] == {"4": c4}
    assert "reuse_asset_ids" not in next(s for s in spec2["shots"] if s["key"] == "1")
    assert spec2["album"][0]["fields"]["title"] == "KITE"
    # an existing character from another project fills the slot by id
    other = store.create_project("Other")
    koi = store.create_character(other["id"], "KOI", prompt="KOI, a paper kite", palette=["#3366FF"])
    spec3, _, meta3 = recipes.plan_run(store, recipe, {"lead": koi["id"]}, {"reuse": ["frames"]})
    assert spec3["lead"]["character_id"] == koi["id"] and meta3["reuse"] == ["frames"]
    assert "reuse_clips" not in next(s for s in spec3["shots"] if s["key"] == "4")
    with pytest.raises(recipes.RecipeError):
        recipes.plan_run(store, recipe, {"lead": {"name": "x"}})


def test_cancelling_a_production_cancels_its_sub_jobs(store, project):
    from prosperos_hoard.jobs import JobCancelled

    class SlowStudio:
        def __init__(self):
            self.cancelled, self.n = [], 0

        def generate(self, project_id, body):
            self.n += 1
            return {"id": f"job_slow_{self.n}", "state": "queued"}

        def job(self, job_id):
            return {"id": job_id, "state": "running"}

        def cancel(self, job_id):
            self.cancelled.append(job_id)

    class Progress:
        calls = 0

        def __call__(self, *_a, **_k):
            pass

        def check_cancel(self):
            Progress.calls += 1
            if Progress.calls > 1:
                raise JobCancelled("cancelled")

    spec = prod.normalise_spec(tiny_spec())
    state = prod.create_production(store.data_dir, "Cancel Me", spec, {"animatic": False}, project_id=project["id"])
    state["done"] = {"character": {"character_id": None}, "song": {"song_asset_id": "a_x"}}
    prod.save_state(store.data_dir, state)
    studio = SlowStudio()
    with pytest.raises(JobCancelled):
        prod.run_production(store, studio, state["slug"], Progress())
    after = prod.load_state(store.data_dir, state["slug"])
    assert sorted(studio.cancelled) == ["job_slow_1", "job_slow_2"]  # both frame jobs it had queued
    assert after["status"] == "cancelled" and after["partial"]["frames"]["pending"] == {}


# ------------------------------------------------------ fixes and features

def test_changing_a_reused_shot_stops_reusing_it(data_dir):
    spec = tiny_spec()
    spec["shots"][1].update({"reuse_asset_ids": ["a_src1"], "reuse_clips": {"2": "a_srcclip"}})
    spec["shots"].append({"key": "3", "prompt": "a door", "clips": [0], "reuse_asset_ids": ["a_src3"],
                          "reuse_clips": {"3": "a_srcclip3"}})
    spec["shots"].append({"key": "4", "prompt": "a window", "clips": [0], "variants": 2,
                          "reuse_asset_ids": ["a_s4", "a_s4b"], "reuse_clips": {"4": "a_srcclip4"}})
    state = prod.create_production(data_dir, "Reused", prod.normalise_spec(spec), {"animatic": False})
    state["status"] = "done"
    state["done"] = {"frames": {"complete": True, "items": {k: {"variants": [f"a_{k}"], "best": f"a_{k}"} for k in "123"}
                                | {"4": {"variants": ["a_4", "a_4b"], "best": "a_4"}}},
                     "clips": {"complete": True, "items": {"2": "c_2", "3": "c_3", "4": "c_4"}}}
    prod.save_state(data_dir, state)
    prod.update_shots(data_dir, state["slug"], [{"key": "2", "regenerate": True}, {"key": "3", "motion": "still"},
                                                {"key": "4", "best": 1}])
    shots = {s["key"]: s for s in prod.load_state(data_dir, state["slug"])["spec"]["shots"]}
    # a regenerated shot is made, not copied from the recipe's source again
    assert "reuse_asset_ids" not in shots["2"] and "reuse_clips" not in shots["2"]
    # a new motion or a new best still: the still stays, the clip is made again
    assert shots["3"]["reuse_asset_ids"] == ["a_src3"] and "reuse_clips" not in shots["3"]
    assert "reuse_clips" not in shots["4"]
    after = prod.load_state(data_dir, state["slug"])
    assert after["done"]["clips"]["items"] == {}
    prod.update_shots(data_dir, state["slug"], [{"key": "1", "motion_prompt": "the lamp sways"}])
    assert prod.load_state(data_dir, state["slug"])["done"]["clips"]["items"] == {}


def test_change_shots_without_a_run_does_not_claim_to_be_queued(data_dir):
    state = prod.create_production(data_dir, "No Run", prod.normalise_spec(tiny_spec()), {"animatic": False})
    state["status"] = "done"
    state["done"] = {"frames": {"complete": True, "items": {"1": {"variants": ["a_1", "a_2"], "best": "a_1"},
                                                            "2": {"variants": ["a_3"], "best": "a_3"}}}}
    prod.save_state(data_dir, state)
    out = prod.update_shots(data_dir, state["slug"], [{"key": "1", "best": 1}], requeue=False)
    after = prod.load_state(data_dir, state["slug"])
    assert out["status"] == "cancelled" and after["status"] == "cancelled" and "continue" in after["message"]
    assert prod.compact_view(after)["next"].startswith("studio_production_continue")
    assert prod.update_shots(data_dir, state["slug"], [{"key": "1", "best": 0}])["status"] == "queued"


def test_slugs_are_claimed_and_a_cancelled_job_shows(data_dir):
    a = prod.create_production(data_dir, "Twin", prod.normalise_spec(tiny_spec()))
    b = prod.create_production(data_dir, "Twin", prod.normalise_spec(tiny_spec()))
    assert (a["slug"], b["slug"]) == ("twin", "twin_2")
    claimed = prod.unique_slug(data_dir, "Twin", claim=True)
    assert claimed == "twin_3" and (prod.productions_dir(data_dir) / "twin_3").is_dir()
    assert prod.unique_slug(data_dir, "Twin", claim=True) == "twin_4"
    with pytest.raises(prod.ProductionError, match="already exists"):
        prod.create_production(data_dir, "Twin", prod.normalise_spec(tiny_spec()), slug="twin")
    # a production whose job was cancelled while still queued
    a["job_id"] = "job_x"
    prod.save_state(data_dir, a)
    jobs = {"job_x": {"id": "job_x", "state": "cancelled", "message": "cancelled"}}
    listed = {p["slug"]: p for p in prod.list_productions(data_dir, jobs.get)}
    assert listed["twin"]["status"] == "cancelled" and listed["twin_2"]["status"] == "queued"
    view = prod.compact_view(prod.reconcile(prod.load_state(data_dir, "twin"), jobs.get))
    assert view["status"] == "cancelled" and view["next"].startswith("studio_production_continue")
    assert prod.load_state(data_dir, "twin")["status"] == "queued"  # a view, nothing written


def test_saving_state_retries_while_a_reader_holds_the_file(data_dir, monkeypatch):
    state = prod.create_production(data_dir, "Locked", prod.normalise_spec(tiny_spec()))
    real = Path.replace
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError("the file is open in another process")
        return real(self, target)

    monkeypatch.setattr(prod, "FILE_RETRY_S", 0.001)
    monkeypatch.setattr(Path, "replace", flaky)
    state["message"] = "saved"
    prod.save_state(data_dir, state)
    monkeypatch.setattr(Path, "replace", real)
    assert calls["n"] == 4 and prod.load_state(data_dir, state["slug"])["message"] == "saved"
    assert not list(prod.production_dir(data_dir, state["slug"]).glob("*.tmp"))


def _audio(store, pid) -> str:
    path = store.data_dir / "assets" / f"song{time.monotonic_ns()}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF")
    return store.create_asset(project_id=pid, kind="audio", file_path=path.relative_to(store.data_dir).as_posix(),
                              duration_s=8.0, source="generated")["id"]


def _finished(store, project, title="Rain", lyrics="[Verse]\nRain on the Rainbow road\nRain again"):
    pid = project["id"]
    spec = tiny_spec(title=title, song={"tags": f"synth, {title}", "lyrics": lyrics, "duration": 8})
    spec["shots"][1]["prompt"] = "a Rainbow over the empty street called Rain"
    state = prod.create_production(store.data_dir, title, prod.normalise_spec(spec), {"animatic": True}, project_id=pid)
    state["status"] = "done"
    state["done"] = {"song": {"song_asset_id": _audio(store, pid)},
                     "frames": {"complete": True, "items": {"1": {"variants": [_asset(store, pid)] * 1, "best": None},
                                                            "2": {"variants": [s2 := _asset(store, pid)], "best": s2}}},
                     "clips": {"complete": True, "items": {"2": _asset(store, pid, kind="video")}}}
    state["done"]["frames"]["items"]["1"]["best"] = state["done"]["frames"]["items"]["1"]["variants"][0]
    prod.save_state(store.data_dir, state)
    return state


def test_title_placeholder_is_word_bounded_and_a_reused_song_keeps_its_words(store, project):
    state = _finished(store, project)
    recipe = recipes.export_recipe(store, state["slug"], "rain")
    shot2 = next(s for s in recipe["spec"]["shots"] if s["key"] == "2")
    assert shot2["prompt"] == "a Rainbow over the empty street called {title}"
    assert recipe["spec"]["song"]["lyrics"] == "[Verse]\n{title} on the Rainbow road\n{title} again"
    assert recipe["reusable"]["song"]["lyrics"].startswith("[Verse]\nRain on")
    spec, _, meta = recipes.plan_run(store, recipe, {"lead": {"name": "KOI", "look": "KOI, a kite"}}, {"title": "Snow"})
    # the old audio is reused: the karaoke must show what it sings
    assert spec["song"]["asset_id"] and spec["song"]["lyrics"] == "[Verse]\nRain on the Rainbow road\nRain again"
    assert spec["song"]["tags"] == "synth, Rain"
    assert any("keeps its own words" in n for n in meta["notes"])
    assert next(s for s in spec["shots"] if s["key"] == "2")["prompt"].endswith("called Snow")
    # a new song (not reused) sings the new title
    spec2, _, _ = recipes.plan_run(store, recipe, {"lead": {"name": "KOI", "look": "KOI, a kite"}},
                                   {"title": "Snow", "reuse": ["frames"]})
    assert "asset_id" not in spec2["song"] and spec2["song"]["lyrics"].startswith("[Verse]\nSnow on the Rainbow")


def test_exporting_over_another_productions_recipe_needs_overwrite(store, project):
    state = _finished(store, project)
    other = _finished(store, project, title="Hail")
    recipes.export_recipe(store, state["slug"], "mine")
    recipes.export_recipe(store, state["slug"], "mine")  # the same production refreshes its own recipe
    with pytest.raises(recipes.RecipeError) as err:
        recipes.export_recipe(store, other["slug"], "Mine")
    assert err.value.code == "recipe_exists"
    assert recipes.get_recipe(store.data_dir, "mine")["source"]["production"] == state["slug"]
    replaced = recipes.export_recipe(store, other["slug"], "mine", overwrite=True)
    assert replaced["name"] == "mine" and replaced["source"]["production"] == other["slug"]


def test_recipe_run_preview_creates_nothing(store, project):
    state = _finished(store, project)
    recipes.export_recipe(store, state["slug"], "preview me")
    before = {p["slug"] for p in prod.list_productions(store.data_dir)}
    out = recipes.preview_run(store, "preview_me", {"lead": {"name": "KOI", "look": "KOI, a kite"}}, {"dry_run": True})
    assert {p["slug"] for p in prod.list_productions(store.data_dir)} == before
    shots = {s["key"]: s for s in out["shots"]}
    assert shots["1"]["still"] == "generate x2" and shots["1"]["clips_to_render"] == ["1"]
    assert shots["2"]["still"] == "reused" and shots["2"]["clips_reused"] == ["2"] and "clips_to_render" not in shots["2"]
    assert out["song"] == "reused" and out["reused_frames"] == ["2"] and out["lead"] == "KOI"
    # 2 variants of the lead shot + a 1-image reference sheet, and one clip
    assert out["stills_to_generate"] == 3 and out["clips_to_render"] == 1
    assert out["gpu_minutes_estimate"] == pytest.approx(round(3 * 57 / 48 + 9.5, 1), abs=0.1)
    assert out["animatic_review"] is True


def test_a_scripted_production_reuses_its_chosen_still_first(store, project):
    pid = project["id"]
    char = store.create_character(pid, "FAROL", prompt="FAROL, a lantern creature")
    first, chosen = _asset(store, pid, prompt="a phone", params={"seed": 1}), _asset(store, pid, prompt="a phone", params={"seed": 2})
    folder = store.data_dir / "productions" / "farol_best"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"done": {
        "1": {"project_id": pid}, "2": {"character_id": char["id"]},
        "4": {"stills": {"4": {"aspect_16_9": [first, chosen], "best": chosen}}}}}), encoding="utf-8")
    recipe = recipes.export_recipe(store, "farol_best", "farol best")
    assert recipe["reusable"]["frames"]["4"] == [chosen, first]
    spec, _, _ = recipes.plan_run(store, recipe, {"lead": {"name": "KOI", "look": "KOI, a kite"}})
    shot = next(s for s in spec["shots"] if s["key"] == "4")
    assert shot["reuse_asset_ids"][shot["best"]] == chosen


def test_api_recipe_preview_export_conflict_and_qa_while_running(client):
    c, app, _ = client
    store = app.state.store
    project = store.create_project("API fixes")
    state = _finished(store, project)
    other = _finished(store, project, title="Hail")
    r = c.post("/api/agent/studio_recipe_export", json={"production": other["slug"], "name": "api rain"})
    assert r.status_code == 200, r.text
    r = c.post("/api/agent/studio_recipe_export", json={"production": state["slug"], "name": "api rain"})
    assert r.status_code == 400 and r.json()["error"] == "recipe_exists"
    assert c.post("/api/agent/studio_recipe_export",
                  json={"production": state["slug"], "name": "api rain", "overwrite": True}).status_code == 200
    r = c.post("/api/agent/studio_recipe_run", json={"recipe": "api_rain", "cast": {"lead": {"name": "KOI", "look": "KOI"}},
                                                     "options": {"dry_run": True}})
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True and "gpu_minutes_estimate" in r.json()
    assert len(c.get("/api/productions").json()["items"]) == 2  # the preview created none
    # a read-only QA check runs on the cpu lane even while the production runs
    running = prod.load_state(store.data_dir, state["slug"])
    running["status"] = "running"
    prod.save_state(store.data_dir, running)
    app.state.qa_vision = (None, "no vision model")
    r = c.post("/api/agent/studio_qa_run", json={"production": state["slug"], "stage": "frames", "wait_s": 30})
    assert r.status_code == 200, r.text
    assert r.json()["job"]["state"] == "done" and r.json()["scorecard"]["stage"] == "frames"
    assert prod.load_state(store.data_dir, state["slug"])["qa"]["last"]["dry_run"] is True
    # a queued production whose job was cancelled before it started shows as cancelled
    queued = prod.load_state(store.data_dir, state["slug"])
    job = store.create_job("production", "cpu", {"slug": "not_run"}, None, None, state="cancelled")  # never claimed
    queued.update(status="queued", job_id=job["id"])
    prod.save_state(store.data_dir, queued)
    assert c.get(f"/api/agent/studio_production?production={state['slug']}").json()["status"] == "cancelled"
