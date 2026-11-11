from __future__ import annotations

import pytest
from PIL import Image

from prosperos_hoard import qr_video as qr
from prosperos_hoard.engine import EngineError
from prosperos_hoard.jobs import JobCancelled


def ignore_progress(*args):
    pass


def render(store, project, **settings):
    params = {"project": project["id"], "fps": 4, "scenes": [
        {"payload": "Llegada: océano 🌊", "caption": "La llegada", "duration_s": .75},
        {"payload": "https://example.org/fin?a=1&b=2", "caption": "La salida", "duration_s": .5}], **settings}
    job = store.create_job("qr_video", "cpu", params, project_id=project["id"])
    return job, qr.render_job(store, job, ignore_progress)


@pytest.mark.parametrize("aspect,motion", [("9:16", "pulse"), ("16:9", "orbit"), ("1:1", "still")])
def test_every_compressed_frame_and_scene_boundary(store, project, aspect, motion):
    job, output = render(store, project, aspect=aspect, motion=motion)
    assert output["verified"] and output["checked_frames"] == output["expected_frames"] == 5
    report = qr.read_report(store, job["id"])
    assert [row["payload_index"] for row in report["frames"]] == [0, 0, 0, 1, 1]
    assert report["payloads"][0] == "Llegada: océano 🌊"
    assert all(row["matches"] and len(row["sha256"]) == 64 for row in report["frames"])
    asset = store.get_asset(output["asset_id"])
    assert asset["duration_s"] == 1.25 and asset["recipe"]["operation"] == "qr_video"
    assert not list(store.data_dir.glob("qr-*")) or list(store.data_dir.glob("qr-*")) == [store.data_dir / "qr-reports"]
    page = qr.read_report(store, job["id"], offset=3, limit=1)
    assert page["frames"][0]["frame"] == 3 and page["next_offset"] == 4
    assert qr.read_report(store, job["id"], limit=0)["frames"] == []
    check = store.create_job("qr_verify", "cpu", {"asset_id": asset["id"]}, project_id=project["id"])
    assert qr.verify_job(store, check, ignore_progress)["verified"]


@pytest.mark.parametrize("scenes,settings,code", [
    ([], {}, "qr_scenes"), ([{"payload": " "}], {}, "qr_payload"),
    ([{"payload": "x", "duration_s": float("nan")}], {}, "qr_duration"),
    ([{"payload": "x", "duration_s": -1}], {}, "qr_duration"),
    ([{"payload": "x"}], {"fps": 0}, "qr_settings"),
    ([{"payload": "x"}], {"motion": "fade"}, "qr_settings"),
    ([{"payload": "x"}], {"aspect": "2:1"}, "qr_settings"),
    ([{"payload": "x" * 4000}], {}, "qr_payload"),
])
def test_reject_invalid_inputs(scenes, settings, code):
    with pytest.raises(EngineError) as error:
        qr.plan(scenes, **settings)
    assert error.value.code == code


def test_duration_is_frame_exact():
    result = qr.plan([{"payload": "first", "duration_s": .3}, {"payload": "last", "duration_s": .01}], fps=24)
    assert [scene["frames"] for scene in result["scenes"]] == [7, 1]
    assert result["duration_s"] == 8 / 24


def test_wrong_payload_missing_frames_and_missing_qr_fail(store, project, tmp_path):
    job, output = render(store, project)
    path = store.data_dir / store.get_asset(output["asset_id"])["file_path"]
    spec = qr.plan([{"payload": "wrong", "duration_s": 2}], fps=4)
    work = tmp_path / "wrong"
    work.mkdir()
    report = qr.verify(path, work, ignore_progress, spec)
    assert not report["verified"] and report["mismatched_frames"] == 5
    assert report["checked_frames"] == 5 and report["expected_frames"] == 8
    blank = tmp_path / "blank"
    blank.mkdir()
    Image.new("RGB", (720, 720), "white").save(blank / "blank.png")
    qr._ffmpeg(["-loop", "1", "-i", "blank.png", "-frames:v", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "blank.mp4"], blank)
    report = qr.verify(blank / "blank.mp4", blank, ignore_progress)
    assert not report["verified"] and report["undecodable_frames"] == 2


def test_cancel_after_encoding_removes_unpublished_output(store, project):
    def cancel(value, message):
        if value >= .6:
            raise JobCancelled()
    job = store.create_job("qr_video", "cpu", {"project": project["id"], "fps": 1,
        "scenes": [{"payload": "cancel", "duration_s": 1}]}, project_id=project["id"])
    with pytest.raises(JobCancelled):
        qr.render_job(store, job, cancel)
    assert not list(store.assets_dir.glob("*.mp4"))
    assert not list(store.data_dir.glob("qr-*"))


def test_live_api_catalog_and_validation(client):
    http, app, _ = client
    project = app.state.store.create_project("QR test")
    response = http.post("/api/qr/video", json={"project": project["id"], "fps": 2,
        "scenes": [{"payload": "API final", "duration_s": 1}], "wait_s": 30})
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["state"] == "done", job
    report = http.get("/api/agent/studio_qr_report", params={"job_id": job["id"]}).json()
    assert report["verified"] and report["checked_frames"] == 2
    download = http.get(f"/api/qr/reports/{job['id']}")
    assert download.status_code == 200 and len(download.json()["frames"]) == 2
    bad = http.post("/api/agent/studio_qr_video", json={"project": project["id"], "scenes": []})
    assert bad.status_code >= 400
    catalog = http.get("/api/agent/tools").json()["tools"]
    assert {"studio_qr_video", "studio_qr_verify", "studio_qr_report"} <= {item["name"] for item in catalog}
