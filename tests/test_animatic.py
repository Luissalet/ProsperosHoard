"""The animatic: the final cut's cut points made from the stills only, the
ffmpeg command lines (with a fake ffmpeg, the repo's pattern of testing
the argv builders without running them) and the plan."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PIL import Image

from prosperos_hoard import animatic, engine
from prosperos_hoard import productions as prod
from prosperos_hoard import video
from prosperos_hoard.devtools.fake_comfy import render_fake_song

LYRICS = "[Intro]\nhum\n[Verse]\nlamps along the road\nshadows at my back\n[Chorus]\nkeep on walking home\ndon't look back\n[Outro]\ngone"


def _image(store, pid, colour, w=640, h=360) -> str:
    aid = f"a_{time.monotonic_ns()}"
    path = store.path_for_asset_file(aid, ".png")
    Image.new("RGB", (w, h), colour).save(path)
    return store.create_asset(project_id=pid, kind="image", file_path=path.relative_to(store.data_dir).as_posix(),
                              width=w, height=h, source="generated", asset_id=aid)["id"]


def _fake_video(store, pid, seconds=5.04) -> str:
    aid = f"a_{time.monotonic_ns()}"
    path = store.path_for_asset_file(aid, ".mp4")
    path.write_bytes(b"not decoded by the cut")
    return store.create_asset(project_id=pid, kind="video", file_path=path.relative_to(store.data_dir).as_posix(),
                              duration_s=seconds, source="generated", asset_id=aid)["id"]


@pytest.fixture
def production(store, project):
    pid = project["id"]
    song_path = store.path_for_asset_file("a_song", ".wav")
    song_path.write_bytes(render_fake_song(3, "synth", LYRICS, 120, 20.0, "A minor"))
    song = store.create_asset(project_id=pid, kind="audio", file_path=song_path.relative_to(store.data_dir).as_posix(),
                              duration_s=20.0, source="generated", asset_id="a_song",
                              recipe={"params": {"bpm": 120}})["id"]
    lyrics = engine.time_lyrics(store, pid, song, LYRICS)["id"]
    spec = {"title": "Walk", "lead": {"name": "WISP", "look": "WISP, a moth"},
            "shots": [{"key": "1", "lead": True, "prompt": "under a lamp", "variants": 2, "clips": [0, 1], "motion": "still"},
                      {"key": "2", "prompt": "an empty street", "clips": [0]},
                      {"key": "3", "prompt": "a door"}],
            "song": {"tags": "synth", "lyrics": LYRICS, "duration": 20},
            "timeline": {"aspects": ["9:16", "16:9"], "qualities": ["preview", "final"],
                         "options": {"fps": 24, "cut_on_lyrics": True, "karaoke": True},
                         "storyboard": {"Verse": ["1", "2", "3"], "Chorus": ["3", "1v2", "2"]},
                         "finishing": {"vignette": True}}}
    state = prod.create_production(store.data_dir, "Walk", spec, {"animatic": True}, project_id=pid)
    s1 = [_image(store, pid, (200, 120, 40)), _image(store, pid, (190, 110, 50))]
    state["done"] = {
        "song": {"song_asset_id": song}, "lyrics": {"lyrics_asset_id": lyrics, "source": "estimated"},
        "frames": {"complete": True, "items": {"1": {"variants": s1, "best": s1[0]},
                                               "2": {"variants": [s2 := _image(store, pid, (20, 40, 90))], "best": s2},
                                               "3": {"variants": [s3 := _image(store, pid, (60, 60, 60))], "best": s3}}},
    }
    prod.save_state(store.data_dir, state)
    return state


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    """Record every ffmpeg argv the renderer builds and pretend it ran."""
    calls: list[list[str]] = []

    def run(cmd, cwd=None):
        calls.append(list(cmd))
        Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(cmd[-1]).write_bytes(b"fake")

    def run_progress(cmd, total, on_progress=None, cwd=None, should_cancel=None):
        calls.append(list(cmd))
        Path(cmd[-1]).write_bytes(b"fake mp4")
        if on_progress:
            on_progress(1.0)

    monkeypatch.setattr(video, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(video, "_run", run)
    monkeypatch.setattr(video, "run_ffmpeg_with_progress", run_progress)
    return calls


def test_animatic_cut_points_equal_the_final_cut(store, production):
    pid = production["project_id"]
    cut = animatic.build(store, production)
    visual = next(t for t in cut["tracks"] if t["type"] == "visual")["clips"]
    assert all(c["kind"] == "image" and c.get("ken_burns") for c in visual)
    assert all(c["transition_in"]["type"] == "crossfade" for c in visual[1:])
    # the final cut: the same song, lyrics and options, the clips instead of the stills
    clips = {"1": _fake_video(store, pid), "1v2": _fake_video(store, pid), "2": _fake_video(store, pid)}
    final_state = json.loads(json.dumps(production))
    final_state["done"]["clips"] = {"complete": True, "items": clips}
    pool, pools, _ = animatic.cut_inputs(final_state, prefer_clips=True)
    options = {**animatic.cut_options(final_state), "section_pools": pools, "video_lead_in_s": 1.0}
    final = engine.timeline_auto(store, pid, production["done"]["song"]["song_asset_id"], pool, None, "9:16",
                                 production["done"]["lyrics"]["lyrics_asset_id"], options)
    final_visual = next(t for t in final["tracks"] if t["type"] == "visual")["clips"]
    assert [c["start_s"] for c in visual] == [c["start_s"] for c in final_visual]
    assert any(c["kind"] == "video" for c in final_visual)  # the final plays the clips there


def test_animatic_renders_both_aspects_with_fake_ffmpeg_and_writes_the_plan(store, production, fake_ffmpeg):
    entry = animatic.make(store, production)
    assert set(entry["renders"]) == {"9:16", "16:9"}
    cut = animatic.build(store, production)
    n = len(next(t for t in cut["tracks"] if t["type"] == "visual")["clips"])
    images = [c for c in fake_ffmpeg if "-loop" in c]
    assert len(images) == 2 * n  # one Ken Burns clip per cut, per aspect
    vf_916 = images[0][images[0].index("-vf") + 1]
    assert "zoompan" in vf_916 and "s=720x1280" in vf_916  # 720p, vertical
    vf_169 = images[n][images[n].index("-vf") + 1]
    assert "s=1280x720" in vf_169
    xfades = [c for c in fake_ffmpeg if "-filter_complex" in c]
    assert len(xfades) == 2 and "xfade=transition=fade" in xfades[0][xfades[0].index("-filter_complex") + 1]
    mux = [c for c in fake_ffmpeg if "-progress" in c]
    assert len(mux) == 2
    assert mux[0][mux[0].index("-crf") + 1] == "26" and mux[0][mux[0].index("-preset") + 1] == "veryfast"
    assert any(arg.endswith("a_song.wav") for arg in mux[0])  # the chosen take, muxed
    assert "vignette=PI/5" in mux[0][mux[0].index("-vf") + 1] and "ass=lyrics.ass" in mux[0][mux[0].index("-vf") + 1]
    asset = store.get_asset(entry["renders"]["9:16"])
    assert asset["kind"] == "video" and asset["recipe"]["operation"] == "animatic" and "animatic" in asset["tags"]

    plan = json.loads((prod.production_dir(store.data_dir, production["slug"]) / "animatic" / "plan.json").read_text())
    assert plan["cuts_total"] == n and plan["renders"] == entry["renders"]
    shots = {s["key"]: s for s in plan["shots"]}
    assert shots["1"]["will_be_clip"] and shots["1"]["clip_keys"] == ["1", "1v2"]
    assert shots["3"]["will_be_clip"] is False and shots["3"]["clips_to_render"] == []
    assert plan["clips_planned"] == 3 and plan["gpu_minutes"] == pytest.approx(3 * 9.5)
    assert sum(s["screen_time_s"] for s in plan["shots"]) == pytest.approx(plan["duration_s"], abs=0.05)
    assert {c["shot"] for c in plan["cuts"]} <= {"1", "1v2", "2", "3"}
    assert entry["plan"]["gpu_minutes"] == plan["gpu_minutes"]


def test_gpu_minutes_are_configurable_and_count_only_missing_clips(store, production):
    state = json.loads(json.dumps(production))
    state["done"]["clips"] = {"complete": False, "items": {"1": "a_done"}}
    state["settings"]["gpu_minutes"] = {"clip": 4.0}
    plan = animatic.plan(state, animatic.build(store, state))
    assert plan["clips_to_render"] == ["1v2", "2"] and plan["gpu_minutes"] == 8.0
    assert plan["cpu_minutes_renders"] == pytest.approx(4 * 1.75)


def test_the_pipeline_pauses_after_the_animatic_until_continued(store, production, fake_ffmpeg):
    class NoStudio:
        def generate(self, *a):
            raise AssertionError("no GPU work before the review")

    def progress(*_a, **_k):
        pass

    state = prod.load_state(store.data_dir, production["slug"])
    state["done"]["character"] = {"character_id": None}
    prod.save_state(store.data_dir, state)
    out = prod.run_production(store, NoStudio(), production["slug"], progress)
    assert out == {"slug": production["slug"], "status": "awaiting_review", "stage": "animatic"}
    after = prod.load_state(store.data_dir, production["slug"])
    assert after["status"] == "awaiting_review" and set(after["done"]["animatic"]["renders"]) == {"9:16", "16:9"}
    assert prod.compact_view(after)["next"].startswith("look at the animatic")
    # changing a shot throws the animatic away (it is remade and reviewed again)
    prod.update_shots(store.data_dir, production["slug"], [{"key": "1", "best": 1}])
    assert "animatic" not in prod.load_state(store.data_dir, production["slug"])["done"]


# ------------------------------------------------------- the review gate

class StillStudio:
    """Makes a still at once for any generate call; no clips expected."""

    def __init__(self, store):
        self.store, self.calls, self.jobs = store, [], {}

    def generate(self, project_id, body):
        if body.get("reference_asset_id"):
            raise AssertionError("no clip may be rendered before the animatic is approved")
        self.calls.append(body)
        ids = [_image(self.store, project_id, (30 + 20 * i, 90, 140)) for i in range(body.get("count") or 1)]
        job = {"id": f"job_{len(self.jobs) + 1}", "state": "done", "outputs": {"asset_ids": ids}}
        self.jobs[job["id"]] = job
        return job

    def job(self, job_id):
        return self.jobs[job_id]

    def cancel(self, job_id):
        pass


AFTER = {"clips": lambda run: None, "photocards": lambda run: None, "album": lambda run: None,
         "timeline": lambda run: None, "report": lambda run: None}


def _quiet(*_a, **_k):
    pass


def test_an_animatic_made_on_demand_before_every_still_is_only_a_preview(store, production, fake_ffmpeg):
    """The auditor's case: frames failed with shot 2 missing, studio_animatic
    was called (shot 1 and 3 only), then continue. The run must make shot 2,
    make its own animatic and pause - never render the clips unreviewed."""
    slug = production["slug"]
    state = prod.load_state(store.data_dir, slug)
    state["done"]["character"] = {"character_id": None}
    state["done"]["frames"]["items"].pop("2")
    state["done"]["frames"]["complete"] = False
    state["status"] = "failed"
    prod.save_state(store.data_dir, state)
    entry = animatic.make_for_production(store, slug)
    after = prod.load_state(store.data_dir, slug)
    assert entry["preview"] is True and "animatic" not in after["done"]
    assert after["animatic_preview"]["renders"] and prod.compact_view(after)["animatic_preview"]["renders"]
    studio = StillStudio(store)
    out = prod.run_production(store, studio, slug, _quiet, stage_hooks=dict(AFTER))
    assert out["status"] == "awaiting_review" and out["stage"] == "animatic"
    assert [c["prompt"].startswith("an empty street") for c in studio.calls] == [True]
    final = prod.load_state(store.data_dir, slug)
    assert "2" in final["done"]["frames"]["items"] and final["done"]["animatic"]["renders"]
    assert "clips" not in final["done"]


def test_the_gate_holds_for_an_animatic_made_outside_the_run(store, production, fake_ffmpeg):
    slug = production["slug"]
    state = prod.load_state(store.data_dir, slug)
    state["done"]["character"] = {"character_id": None}
    state["status"] = "failed"
    prod.save_state(store.data_dir, state)
    entry = animatic.make_for_production(store, slug)  # every still exists: it becomes the production's animatic
    assert "preview" not in entry
    reached: list[str] = []
    hooks = {k: (lambda run, k=k: reached.append(k)) for k in AFTER}
    # continue from "failed" records no approval: the run stops at the gate
    out = prod.run_production(store, StillStudio(store), slug, _quiet, stage_hooks=hooks)
    assert out["status"] == "awaiting_review" and reached == []
    state = prod.load_state(store.data_dir, slug)
    assert prod.animatic_review_pending(state) and state["done"]["animatic"]["made_at"] == entry["made_at"]
    # approved (what studio_production_continue does from awaiting_review): on to the clips
    assert prod.approve_animatic(state) == entry["made_at"]
    prod.save_state(store.data_dir, state)
    out = prod.run_production(store, StillStudio(store), slug, _quiet, stage_hooks=hooks)
    assert out["status"] == "done" and reached[0] == "clips"


def test_an_animatic_remade_after_qa_is_reviewed_again(store, production, fake_ffmpeg):
    from prosperos_hoard import qa

    slug = production["slug"]
    state = prod.load_state(store.data_dir, slug)
    state["done"]["character"] = {"character_id": None}
    state["done"]["animatic"] = animatic.make(store, state)
    prod.approve_animatic(state)
    old = state["done"]["animatic"]["made_at"]
    qa._invalidate_after(state, "frames")  # QA regenerated a still
    assert "animatic" not in state["done"] and state["review"]["animatic_approved"] == old
    state["status"] = "queued"
    prod.save_state(store.data_dir, state)
    out = prod.run_production(store, StillStudio(store), slug, _quiet, stage_hooks=dict(AFTER))
    assert out["status"] == "awaiting_review"
    after = prod.load_state(store.data_dir, slug)
    assert after["done"]["animatic"]["made_at"] != old and not prod.animatic_approved(after)


def test_an_animatic_finished_while_a_run_started_stays_a_preview(store, production, monkeypatch):
    slug = production["slug"]
    real = {"renders": {"9:16": "a_fake"}, "plan": {"gpu_minutes": 0, "clips_planned": 0}, "made_at": "t"}

    def make_while_a_run_starts(store_, state, aspects=None, progress=None, should_cancel=None):
        with prod.lock_for(slug):
            current = prod.load_state(store.data_dir, slug)
            current["status"] = "running"
            prod.save_state(store.data_dir, current)
        return dict(real)

    monkeypatch.setattr(animatic, "make", make_while_a_run_starts)
    entry = animatic.make_for_production(store, slug)
    after = prod.load_state(store.data_dir, slug)
    assert entry["preview"] is True and "animatic" not in after["done"] and after["animatic_preview"]["made_at"] == "t"


def test_the_shot_contact_sheet_labels_every_shot(store, production, fake_ffmpeg):
    entry = animatic.make(store, production)
    sheet = store.get_asset(entry["contact_sheet_id"])
    assert sheet["kind"] == "image" and "contact_sheet" in sheet["tags"]
    assert sheet["recipe"]["labels"] == ["1 - clip - lead", "2 - clip", "3"]
    frames = production["done"]["frames"]["items"]
    assert sheet["recipe"]["input_asset_ids"] == [frames[k]["best"] for k in ("1", "2", "3")]
    with Image.open(store.data_dir / sheet["file_path"]) as img:
        assert img.width == 3 * 320
    state = json.loads(json.dumps(production))
    state["done"]["animatic"] = entry
    view = prod.compact_view(state)
    assert view["animatic"]["contact_sheet_id"] == entry["contact_sheet_id"] and view["animatic"]["approved"] is False
