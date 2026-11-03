"""Edit plans: plan -> run -> verify, fingerprints, platform checks and contact sheets (fixture media made with ffmpeg)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from prosperos_hoard import edit_plans, procutil
from prosperos_hoard.backend import ffmpeg_path
from prosperos_hoard.engine import EngineError

pytestmark = pytest.mark.skipif(ffmpeg_path() is None, reason="ffmpeg not installed")


def _make(store, project, name: str, args: list[str], kind: str) -> dict:
    path = store.path_for_asset_file(name, ".png" if kind == "image" else ".mp4" if kind == "video" else ".wav")
    path.parent.mkdir(parents=True, exist_ok=True)
    procutil.run([ffmpeg_path(), "-y", "-loglevel", "error", *args, str(path)], check=True)
    return store.create_asset(project_id=project["id"], kind=kind, file_path=path.relative_to(store.data_dir).as_posix(),
                              asset_id=name, name=name, duration_s=None if kind == "image" else 2.0)


@pytest.fixture
def media(store, project):
    clip = _make(store, project, "a_clip", ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
                                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
                                            "-pix_fmt", "yuv420p", "-c:a", "aac"], "video")
    still = _make(store, project, "a_still", ["-f", "lavfi", "-i", "color=c=red:size=200x200", "-frames:v", "1"], "image")
    song = _make(store, project, "a_song", ["-f", "lavfi", "-i", "sine=frequency=220:duration=6"], "audio")
    return clip, still, song


def _spec(platform="shorts"):
    return {"platform": platform, "width": 270, "height": 480, "fps": 25, "quality": "preview",
            "clips": [{"asset_id": "a_clip", "in_s": 0.2, "out_s": 1.8},
                      {"asset_id": "a_still", "duration_s": 1.5, "transition": {"type": "wipeleft", "duration_s": 0.4}}],
            "captions": [{"text": "Hola mundo", "start_s": 0.1, "end_s": 1.2}],
            "overlays": [{"asset_id": "a_still", "start_s": 0.5, "end_s": 2.0, "scale": 0.2, "x": 1, "y": 0}],
            "music": {"asset_id": "a_song", "volume_db": -6, "duck": True}}


def test_plan_compiles_steps_with_fingerprints_and_no_paths(store, project, media):
    plan = edit_plans.create(store, project["id"], _spec(), name="Teaser")
    folder = edit_plans.plan_dir(store.data_dir, plan["id"])
    assert (folder / "plan.json").is_file() and (folder / "captions.ass").is_file()
    assert [s["name"] for s in plan["steps"]] == ["clip 1", "clip 2", "join with transitions", "overlays",
                                                  "audio mix with music and loudness", "encode with audio and captions"]
    assert {i["asset_id"] for i in plan["inputs"]} == {"a_clip", "a_still", "a_song"}
    assert all(len(i["fingerprint"]["head_sha256"]) == 64 for i in plan["inputs"])
    assert plan["edit"]["duration_s"] == pytest.approx(3.1)
    shown = edit_plans.view(plan, full=True)
    text = json.dumps(shown)
    assert str(store.data_dir) not in text and "@in_" not in text
    assert "xfade=transition=wipeleft" in " ".join(shown["commands"])
    assert "sidechaincompress" in shown["commands"][4] and "loudnorm=I=-14.0" in shown["commands"][4]


def test_bad_edits_are_refused(store, project, media):
    with pytest.raises(EngineError):
        edit_plans.create(store, project["id"], {"clips": [{"asset_id": "a_clip", "out_s": 9}]})
    with pytest.raises(EngineError):
        edit_plans.create(store, project["id"], {"clips": [{"asset_id": "a_clip"}, {"asset_id": "a_still", "transition": "spin"}]})
    with pytest.raises(EngineError):
        edit_plans.create(store, project["id"], {"clips": [{"asset_id": "a_song"}]})


def _progress(*_a, **_k):
    return None


def test_run_renders_verifies_and_makes_a_contact_sheet(store, project, media):
    plan = edit_plans.create(store, project["id"], _spec())
    out = edit_plans.run_job(store, {"id": "job_x", "params": {"plan_id": plan["id"]}}, _progress)
    asset = store.get_asset(out["asset_id"])
    assert asset["kind"] == "video" and (asset["width"], asset["height"]) == (270, 480)
    assert asset["duration_s"] == pytest.approx(3.1, abs=0.15)
    report = edit_plans.load(store.data_dir, plan["id"])["last_verify"]
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["resolution"]["status"] == "fail"  # 270x480 is the right shape but far below 1080x1920
    assert checks["duration"]["status"] == "pass" and checks["audio_layout"]["status"] == "pass"
    assert report["media"]["integrated_lufs"] is not None
    sheet = store.get_asset(report["contact_sheet_asset_id"])
    assert sheet["kind"] == "image" and "contact_sheet" in sheet["tags"]
    assert not list(edit_plans.plan_dir(store.data_dir, plan["id"]).glob("clip_*.mp4"))  # intermediates cleaned


def test_run_refuses_when_an_input_changed(store, project, media):
    plan = edit_plans.create(store, project["id"], {"clips": [{"asset_id": "a_clip"}], "width": 160, "height": 120})
    path = store.data_dir / store.get_asset("a_song")["file_path"]
    other = edit_plans.create(store, project["id"], {"clips": [{"asset_id": "a_clip"}], "music": {"asset_id": "a_song"},
                                                     "width": 160, "height": 120})
    with path.open("ab") as fh:
        fh.write(b"\0" * 16)
    os.utime(path, None)
    assert edit_plans.check_inputs(store, plan) == []
    with pytest.raises(EngineError) as err:
        edit_plans.run_job(store, {"params": {"plan_id": other["id"]}}, _progress)
    assert err.value.code == "inputs_changed"


def test_check_media_against_platform_specs():
    good = {"has_video": True, "has_audio": True, "width": 1080, "height": 1920, "fps": 30, "duration_s": 20,
            "video_codec": "h264", "audio_codec": "aac", "channels": 2, "sample_rate": 48000}
    loud = {"integrated_lufs": -14.3, "true_peak_dbfs": -1.2}
    assert {c["status"] for c in edit_plans.check_media(good, loud, "shorts")} == {"pass"}
    wide = dict(good, width=1920, height=1080, duration_s=200, channels=1)
    status = {c["id"]: c["status"] for c in edit_plans.check_media(wide, {"integrated_lufs": -24}, "shorts")}
    assert status["resolution"] == "fail" and status["duration"] == "fail"
    assert status["audio_layout"] == "warn" and status["loudness"] == "fail"
    assert {c["id"]: c["status"] for c in edit_plans.check_media(wide, loud, "youtube")}["resolution"] == "pass"
