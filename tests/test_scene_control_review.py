import json
from types import SimpleNamespace

from PIL import Image

from prosperos_hoard import scene_control_review as review, scene_shots


def test_guided_shot_scores_four_mounted_frames_and_warns_at_an_incomplete_hand(tmp_path, monkeypatch):
    Image.new("RGB", (16, 16), "red").save(tmp_path / "sheet.png")
    assets = {
        "sheet": {"kind": "image", "project_id": "p", "file_path": "sheet.png"},
        "clip": {"kind": "video", "project_id": "p", "file_path": "clip.mp4"},
        "guide": {"kind": "video", "project_id": "p", "file_path": "guide.mp4"},
    }
    store = SimpleNamespace(data_dir=tmp_path, get_asset=assets.__getitem__)
    state = {"project_id": "p", "spec": {"scene_shots": {
        "character_sheet_asset_id": "sheet", "description": "Pass the map", "preserve_details": ["map drawing"],
    }}}
    shot = {"scene_control": {"duration_s": 2, "control_asset_id": "guide", "action": "Pass the map"}}
    requested_frames = []

    def video_frame(path, time, output_fps):
        assert path.parent == tmp_path and output_fps == 24
        requested_frames.append((path.stem, round(time * output_fps)))
        return Image.new("RGB", (16, 16), "green")

    monkeypatch.setattr(review.qa, "video_frame", video_frame)
    no_vision = review.inspect(store, state, shot, "clip", None, "unavailable")
    assert no_vision["status"] == "unavailable"
    assert requested_frames == []

    passed = {"verdict": "pass", "note": "visible"}
    samples = [{"position": label, "identity": 7, "why": "same hair and cloak",
                "left_hand": passed, "right_hand": passed, "props": passed}
               for label in ("first", "quarter", "middle", "last")]
    samples[-1]["right_hand"] = {"verdict": "defect", "note": "hand has no visible forearm"}

    def vision(images, prompt):
        assert len(images) == 5
        assert "map drawing" in prompt
        return json.dumps({"samples": samples})

    result = review.inspect(store, state, shot, "clip", vision, "configured vision")
    assert requested_frames == [(source, frame) for frame in (0, 12, 24, 47) for source in ("guide", "clip")]
    assert result["clip_asset_id"] == "clip"
    assert result["control_asset_id"] == "guide"
    assert result["status"] == "warning"
    assert result["samples"][3]["identity"]["passed"]
    assert result["samples"][3]["right_hand"]["verdict"] == "defect"

    samples[-1]["right_hand"] = passed
    samples[1]["identity"] = 6
    assert review.inspect(store, state, shot, "clip", vision, "configured vision")["status"] == "warning"
    samples[1]["identity"] = 7
    assert review.inspect(store, state, shot, "clip", vision, "configured vision")["status"] == "pass"
    assert len(review._parsed(f"```json\n{json.dumps(samples)}\n```")) == 4
    samples[1]["position"] = "wrong"
    assert review.inspect(store, state, shot, "clip", vision, "configured vision")["status"] == "unavailable"


def test_guided_warning_follows_the_take_and_unavailable_measurement_can_retry(monkeypatch):
    control = {"control_asset_id": "guide", "duration_s": 2}
    shot = {"key": "1", "scene_control": control}
    spec = {"shots": [shot], "scene_shots": {"aspect": "1:1"}}
    items = {"1": "take"}
    state = {"slug": "scene", "project_id": "p", "spec": spec, "done": {"clips": {"items": items}}}
    saved = []
    run = SimpleNamespace(state=state, spec=spec, store=None, items=lambda _: items,
                          save=lambda: saved.append(True), log=lambda *_a, **_kw: None,
                          tick=lambda *_: None, stage_fraction=lambda fraction: fraction)
    results = iter(["unavailable", "warning"])

    def inspect(store, current, source, aid, vision, model):
        return {"clip_asset_id": aid, "control_asset_id": source["scene_control"]["control_asset_id"],
                "sample_window_s": 2, "status": next(results)}

    monkeypatch.setattr(review, "inspect", inspect)
    provider = lambda: (None, None)
    review.record(run, provider)
    assert scene_shots.view(state)["shots"][0]["control_review"]["status"] == "unavailable"
    review.record(run, provider)
    review.record(run, provider)
    assert len(saved) == 2
    assert scene_shots.view(state)["shots"][0]["control_review"]["status"] == "warning"
    items["1"] = "replacement"
    assert "control_review" not in scene_shots.view(state)["shots"][0]
