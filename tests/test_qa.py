"""The QA director: model-free checks on synthetic pictures and clips, the
vision scores through a fake Link, the retry policy (new seed + targeted
fix, the retry cap, lineage entries) and the "no vision model" path."""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from prosperos_hoard.ids import new_id
from prosperos_hoard import procutil
from prosperos_hoard import productions as prod
from prosperos_hoard import qa
from prosperos_hoard.backend import ffmpeg_path

RNG = np.random.default_rng(7)


def scene(w: int = 320, h: int = 180, seed: int = 1) -> Image.Image:
    """A picture with gradients and shapes (not flat, not noise)."""
    rng = np.random.default_rng(seed)
    x = np.linspace(0, 1, w)[None, :, None]
    y = np.linspace(0, 1, h)[:, None, None]
    base = (40 + 120 * x * np.array([1.0, 0.6, 0.3]) + 60 * y * np.array([0.2, 0.4, 1.0])).astype(np.uint8)
    img = Image.fromarray(np.broadcast_to(base, (h, w, 3)).copy())
    d = ImageDraw.Draw(img)
    for _ in range(5):
        x0, y0 = int(rng.integers(0, w - 60)), int(rng.integers(0, h - 60))
        d.ellipse([x0, y0, x0 + 50, y0 + 40], fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    return img


def flat(w: int = 320, h: int = 180) -> Image.Image:
    return Image.new("RGB", (w, h), (90, 92, 95))


def noise(w: int = 320, h: int = 180) -> Image.Image:
    return Image.fromarray(RNG.integers(0, 255, (h, w, 3), dtype=np.uint8))


# ------------------------------------------------------------ measurements

def test_flat_noisy_and_normal_pictures():
    assert qa.image_stats(flat())["std"] < 1
    n = qa.image_stats(noise())
    assert n["noise_ratio"] > 0.9
    s = qa.image_stats(scene())
    assert s["std"] > 20 and s["noise_ratio"] < 0.3


def test_edge_bands_find_the_gbrp_stripe_not_natural_edges():
    img = scene(1080, 608)
    a = np.asarray(img).copy()
    a[:, -8:] = (120, 20, 18)  # the red stripe of the ffmpeg 8 gbrp round trip
    found = qa.edge_bands(Image.fromarray(a))
    assert [b["side"] for b in found] == ["right"] and found[0]["rgb"][0] > 100
    b = np.asarray(img).copy()
    b[-8:, :] = 0  # a black band at the bottom
    assert [x["side"] for x in qa.edge_bands(Image.fromarray(b))] == ["bottom"]
    assert qa.edge_bands(img) == []
    c = np.asarray(img).copy()
    c[:200, -30:] = 10  # a dark doorway reaching the edge on part of the height is not a band
    assert qa.edge_bands(Image.fromarray(c)) == []


def test_headroom_on_photocard_photos():
    def figure(top: int) -> Image.Image:
        img = Image.new("RGB", (400, 600), (235, 200, 215))
        d = ImageDraw.Draw(img)
        d.ellipse([150, top, 250, top + 110], fill=(40, 30, 30))       # the head
        d.rectangle([120, top + 100, 280, 600], fill=(60, 40, 40))     # the body
        return img
    cropped = qa.headroom(figure(-40))
    assert cropped["subject"] and cropped["headroom"] == 0
    roomy = qa.headroom(figure(90))
    assert roomy["subject"] and roomy["headroom"] > 0.1
    assert qa.headroom(Image.new("RGB", (200, 300), (200, 200, 200)))["subject"] is False


def test_clip_motion_on_frame_sequences():
    steady = np.full((20, 36, 64), 80, np.float32)
    jump = steady.copy()
    jump[12:] = 170
    m = qa.clip_motion(jump)
    assert m["max_jump"] == pytest.approx(90) and m["jump_at"] == 12
    assert qa.clip_motion(steady)["energy"] == 0
    busy = RNG.integers(0, 255, (20, 36, 64)).astype(np.float32)
    assert qa.clip_motion(busy)["energy"] > 50


def test_lyric_coverage_and_score_parsing():
    written = "[Verse]\nOne little light at the end of your street\n(shh)\nTwo little lights where the shadows meet\n[Chorus]\nDon't look back"
    lrc = [{"time_s": 1, "text": "one little light at the end of your street"}, {"time_s": 3, "text": "Don't look back!"}]
    cov = qa.lyric_coverage(written, lrc)
    assert cov["lines"] == 3 and cov["matched"] == 2 and cov["missing"] == ["two little lights where the shadows meet"]
    s = qa.parse_scores('Sure:\n```json\n{"bible": 8, "prompt": 3.5, "reference": null, "reason": "the lamp is missing"}\n```')
    assert s == {"bible": 8.0, "prompt": 3.5, "reference": None, "reason": "the lamp is missing"}
    loose = qa.parse_scores("bible: 7, prompt=12, reference 4")
    assert loose["bible"] == 7 and loose["prompt"] == 10 and loose["reference"] is None


# ------------------------------------------------------------ with a store

def _save(store, pid: str, img: Image.Image, **recipe) -> str:
    aid = new_id("a")  # monotonic_ns ticks every ~15 ms on Windows: ids collided
    path = store.path_for_asset_file(aid, ".png")
    img.save(path)
    return store.create_asset(project_id=pid, kind="image", file_path=path.relative_to(store.data_dir).as_posix(),
                              width=img.width, height=img.height, source="generated", recipe=recipe, asset_id=aid)["id"]


def _clip(store, pid: str, frames: np.ndarray, source: str | None = None) -> str:
    exe = ffmpeg_path()
    if not exe:
        pytest.skip("ffmpeg missing")
    aid = new_id("a")  # monotonic_ns ticks every ~15 ms on Windows: ids collided
    path = store.path_for_asset_file(aid, ".mp4")
    n, h, w = frames.shape
    proc = procutil.run([exe, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-r", "24",
                         "-i", "-", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
                        input=frames.astype(np.uint8).tobytes())
    assert proc.returncode == 0, proc.stderr
    return store.create_asset(project_id=pid, kind="video", file_path=path.relative_to(store.data_dir).as_posix(),
                              duration_s=n / 24, source="generated", asset_id=aid,
                              recipe={"input_asset_ids": [source] if source else []})["id"]


class FakeStudio:
    """Generates at once: `make(body)` returns the pictures for a call."""

    def __init__(self, store, make):
        self.store, self.make, self.calls = store, make, []
        self.jobs: dict[str, dict] = {}
        self.ids = itertools.count(1)

    def generate(self, project_id, body):
        self.calls.append(body)
        ids = [_save(self.store, project_id, img, params={"seed": body.get("seed")}) for img in self.make(body)]
        job = {"id": f"job_fake_{next(self.ids)}", "state": "done", "outputs": {"asset_ids": ids}}
        self.jobs[job["id"]] = job
        return job

    compose = render = None

    def job(self, job_id):
        return self.jobs[job_id]

    def cancel(self, job_id):
        pass


def _production(store, project, shots, settings=None, **spec_extra):
    spec = {"title": "QA Test", "lead": {"name": "WISP", "look": "WISP, a moth spirit", "palette": ["#F28C28"]},
            "world": {"look": "night lamps"}, "shots": shots, "timeline": {"aspects": ["9:16"]}, **spec_extra}
    state = prod.create_production(store.data_dir, f"qa {new_id('p')[-8:].lower()}", spec,
                                   settings or {"qa": {"enabled": True, "max_retries": 2}}, project_id=project["id"])
    return state


def test_retry_policy_regenerates_with_a_new_seed_up_to_the_cap(store, project):
    pid = project["id"]
    shots = [{"key": "1", "prompt": "an empty street", "seed": 100}, {"key": "2", "prompt": "a doorway", "seed": 200}]
    state = _production(store, project, shots)
    # shot 1: flat at its first seed, fine after a retry; shot 2: always flat
    state["done"] = {"frames": {"complete": True, "items": {
        "1": {"variants": [a := _save(store, pid, flat())], "best": a},
        "2": {"variants": [b := _save(store, pid, flat())], "best": b}}}}
    prod.save_state(store.data_dir, state)
    studio = FakeStudio(store, lambda body: [scene(seed=body["seed"]) if "street" in body["prompt"] else flat()])
    run = prod.Run(store, studio, state["slug"], lambda *a, **k: None)
    card = qa.run_stage_with_policy(run, "frames", qa.QA(store, run.state, None))
    by_key = {i["key"]: i for i in card["items"]}
    assert by_key["1"]["verdict"] == "pass" and by_key["1"]["retries"] == 1
    assert by_key["2"]["verdict"] == "fail" and by_key["2"]["retries"] == 2 and "flat" in by_key["2"]["codes"]
    seeds_2 = [c["seed"] for c in studio.calls if "doorway" in c["prompt"]]
    assert seeds_2 == [200 + 7919, 200 + 7919 + 7919 * 2]  # a new seed per attempt, capped at 2
    events = [(e["event"], e.get("key")) for e in run.state["lineage"]]
    assert ("qa_retry", "1") in events and events.count(("qa_retry", "2")) == 2 and ("qa_gave_up", "2") in events
    retry = next(e for e in run.state["lineage"] if e["event"] == "qa_retry")
    assert retry["reason"].startswith("flat picture") and retry["fix"]["seed"] == 100 + 7919
    # the "no vision model" path: model checks are skipped, never blocking
    assert by_key["1"]["model"] == {"skipped": "no vision model"}
    saved = prod.load_state(store.data_dir, state["slug"])
    assert saved["spec"]["shots"][0]["seed"] == 100 + 7919


def test_a_passing_variant_is_swapped_in_before_regenerating(store, project):
    pid = project["id"]
    state = _production(store, project, [{"key": "1", "prompt": "a street", "seed": 5, "variants": 2}])
    bad, good = _save(store, pid, noise()), _save(store, pid, scene())
    state["done"] = {"frames": {"complete": True, "items": {"1": {"variants": [bad, good], "best": bad}}},
                     "clips": {"complete": True, "items": {"1": "a_old_clip"}}}
    prod.save_state(store.data_dir, state)
    studio = FakeStudio(store, lambda body: pytest.fail("no GPU call expected"))
    run = prod.Run(store, studio, state["slug"], lambda *a, **k: None)
    card = qa.run_stage_with_policy(run, "frames", qa.QA(store, run.state, None))
    assert card["items"][0]["verdict"] == "pass" and card["items"][0]["asset_id"] == good
    assert run.state["done"]["frames"]["items"]["1"]["best"] == good
    assert run.state["done"]["clips"]["items"] == {}  # made from the old still
    assert any(e["event"] == "qa_swap_variant" for e in run.state["lineage"])


def test_targeted_fixes_for_known_failures():
    lead_shot = {"key": "3", "lead": True, "seed": 10, "clip_seed": 50, "motion_prompt": "rain"}
    assert qa.fix_for("frames", ["noisy"], lead_shot, 1) == {"seed": 10 + 7919, "strength": 1.0}
    fix = qa.fix_for("clips", ["exposure_jump", "moves_when_still"], lead_shot, 2)
    assert fix["clip_seed"] == 50 + 2 * 7919 and fix["clip_sampler"] == "euler" and fix["clip_cfg"] == 4.0
    assert "walking" in fix["clip_negative"] and fix["motion_prompt"].endswith("stays perfectly still")
    assert qa.fix_for("photocards", ["head_cropped"], {"seed": 4001, "framing": False}, 1) == {"seed": 4001 + 7919, "framing": None}


def test_clip_checks_and_model_scores(store, project):
    pid = project["id"]
    still = _save(store, pid, scene())
    steady = np.full((30, 36, 64), 70, np.uint8)
    jumpy = steady.copy()
    jumpy[15:] = 190
    busy = RNG.integers(0, 255, (30, 36, 64)).astype(np.uint8)
    shots = [{"key": "1", "prompt": "a lamp", "lead": True, "motion": "still", "clips": [0]},
             {"key": "2", "prompt": "rain", "motion": "move", "clips": [0]},
             {"key": "3", "prompt": "a door", "motion": "still", "clips": [0]}]
    state = _production(store, project, shots)
    state["done"] = {"clips": {"complete": True, "items": {"1": _clip(store, pid, busy, still), "2": _clip(store, pid, steady, still),
                                                           "3": _clip(store, pid, jumpy, still)}}}
    prompts: list[str] = []

    def vision(images, prompt):
        prompts.append(prompt)
        assert len(images) == 2 and images[0][:2] == b"\xff\xd8"  # the frame and its source still, as JPEG
        return '{"bible": 4, "prompt": 9, "reference": 8, "reason": "the palette drifted to blue"}'

    check = qa.QA(store, state, vision, "fake-vision")
    items = {i["key"]: i for i in check.stage_items("clips")}
    assert "moves_when_still" in items["1"]["codes"]
    assert "still_when_moving" in items["2"]["codes"]
    assert "exposure_jump" in items["3"]["codes"] and "frame 15" in "; ".join(items["3"]["reasons"])
    assert items["1"]["score"] == 4 and "low_score" in items["1"]["codes"]
    assert "palette drifted" in "; ".join(items["1"]["reasons"])
    assert "WISP, a moth spirit" in prompts[0] and "should be in this shot" in prompts[0]
    assert "should NOT be in this shot" in prompts[1]

    def broken(images, prompt):
        raise RuntimeError("model crashed")

    item = qa.QA(store, state, broken).stage_items("clips", {"2"})[0]
    assert item["model"]["skipped"].startswith("vision call failed")
    card = qa.compact_scorecard(qa.scorecard("x", "clips", list(items.values()), "fake-vision", True))
    assert card["failed"] == 3 and card["items"][0]["why"]


def test_qa_over_the_api_and_the_report(client):
    c, app, _ = client
    store = app.state.store
    project = store.create_project("QA API")
    pid = project["id"]
    state = prod.create_production(store.data_dir, "QA API", {
        "title": "QA API", "lead": {"name": "WISP", "look": "WISP, a moth spirit"},
        "shots": [{"key": "1", "prompt": "a street"}, {"key": "2", "prompt": "a door"}],
        "song": {"tags": "synth", "lyrics": "[Verse]\nla la", "duration": 30}}, {}, project_id=pid)
    state["status"] = "done"
    state["done"] = {"frames": {"complete": True, "items": {"1": {"variants": [a := _save(store, pid, scene())], "best": a},
                                                            "2": {"variants": [b := _save(store, pid, flat())], "best": b}}}}
    prod.save_state(store.data_dir, state)
    app.state.qa_vision = (None, "no vision model")
    r = c.post("/api/agent/studio_qa_run", json={"production": state["slug"], "stage": "frames", "wait_s": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job"]["state"] == "done" and body["requeued"] is False
    card = body["scorecard"]
    assert card["failed"] == 1 and card["passed"] == 1 and card["vision"] == "no vision model"
    assert card["items"][0]["key"] == "2" and "flat" in card["items"][0]["why"]
    report = c.get(f"/api/agent/studio_qa_report?production={state['slug']}").json()
    assert report["stage"] == "frames" and report["items"][0]["verdict"] == "fail"
    assert c.get(f"/api/productions/{state['slug']}/qa").json()["last"]["failed"] == 1
    assert c.post("/api/agent/studio_qa_run", json={"production": state["slug"], "stage": "nope"}).status_code == 400


def test_scripted_production_is_checked_read_only(store, project):
    pid = project["id"]
    char = store.create_character(pid, "FAROL", prompt="FAROL, a lantern creature")
    s1 = _save(store, pid, flat(), prompt="@FAROL under a lamp, night", matched_characters=["FAROL"], params={"seed": 1})
    folder = store.data_dir / "productions" / "farol"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"done": {
        "1": {"project_id": pid}, "2": {"character_id": char["id"], "canonical_asset_id": None},
        "4": {"stills": {"1": {"aspect_16_9": [s1], "best": s1}}}}}), encoding="utf-8")
    out = qa.run_qa(store, None, "farol", "frames", dry_run=True)
    assert out["scorecard"]["failed"] == 1 and out["requeue"] is False
    saved = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    assert saved["qa"]["last"]["items"][0]["codes"] == ["flat"] and "4" in saved["done"]  # the script's steps are kept
    with pytest.raises(qa.QAError):
        qa.run_qa(store, None, "farol", "frames", dry_run=False)
