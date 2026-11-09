"""Manim scenes (ManimGX): the static rules, the engine's reports, the model writing code, the API and the clip it registers.

The engine itself (a Python 3.13 environment with native wheels) is replaced by a simulated one that reads the scene file and
answers like the real CLI did when it was run by hand; the real engine is exercised in the live check, not here.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from prosperos_hoard import manim_scene as ms
from prosperos_hoard.backend import ffmpeg_path

GOOD = '''import manimgx as m


class Hello(m.Scene):
    def construct(self):
        t = m.Text("hola")
        self.play(m.Write(t), run_time=2)
        self.wait(0.5)
'''
BAD_NAME = GOOD.replace("self.play(m.Write(t), run_time=2)", "self.play(m.Foo(t))  # RAISE_NAME")
OVERLAP = GOOD + "# OVERLAP\n"
TRACE = ("scene.py:6: in construct\n    self.play(m.Foo(t))\nAttributeError: module 'manimgx' has no attribute 'Foo'\n"
         "hint: ManimGX has no Foo\n")


def _png(path: Path) -> None:
    from PIL import Image
    Image.new("RGB", (64, 36), (20, 40, 90)).save(path)


class FakeEngine:
    """Answers `inspect` and `render` like the CLI; records what it was asked."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, data_dir, args, cwd, timeout, cancel=None):
        self.calls.append(list(args))
        source = (Path(cwd) / "scene.py").read_text(encoding="utf-8")
        name = args[2]
        if "RAISE_NAME" in source:
            return subprocess.CompletedProcess(args, 1, "", TRACE)
        if args[0] == "inspect":
            _png(Path(cwd) / "storyboard.png")
            out = f"{cwd}{ms.os.sep}scene.py: {name}, 2.50 s, 2 plays\nstoryboard: {cwd}{ms.os.sep}storyboard.png\n"
            out += "  #0   0.00\u2013 2.00s  scene.py:6  Write(Text 'hola')\n  #1   2.00\u2013 2.50s  scene.py:7  wait\n"
            if "OVERLAP" in source:
                out += "layout: 1 problems\n  [1] t=1\ufffd2s (scene.py:6)  a (Text 'x') and b (Text 'y'): texts overlap\n"
                return subprocess.CompletedProcess(args, 1, out, "")
            return subprocess.CompletedProcess(args, 0, out + "layout: no problems in 2 samples\n", "")
        width, height = (int(v) for v in args[args.index("-r") + 1].split("x"))
        fps = args[args.index("--fps") + 1]
        target = Path(cwd) / args[args.index("-o") + 1]
        subprocess.run([ffmpeg_path(), "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x203060:s={width}x{height}:d=1:r={fps}",
                        "-pix_fmt", "yuv420p", str(target)], check=True)
        return subprocess.CompletedProcess(args, 0, f"{target}  1.00 s, {width}x{height} at {fps} fps, 0.0 MB (rendered in 0.1 s)\n", "")


@pytest.fixture
def fake(monkeypatch):
    engine = FakeEngine()
    monkeypatch.setattr(ms, "_run_engine", engine)
    monkeypatch.setattr(ms, "_engine_version", lambda data_dir: "0.1.1")
    return engine


@pytest.fixture
def installed(client, fake):
    c, app, _ = client
    py = ms.venv_python(app.state.store.data_dir)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"")
    return c, app, fake


def _wait(c, job_id, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("queued", "waiting_gpu", "running"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


# ------------------------------------------------------------------ the static rules

def test_the_code_is_normalised_and_the_scene_found():
    text, scene = ms.static_check("from manim import *\nclass A(Scene):\n    def construct(self):\n        pass\n")
    assert text.startswith("from manimgx import *") and scene == "A"
    text, _ = ms.static_check("import manim as m\nclass A(m.ThreeDScene):\n    def construct(self):\n        pass\n")
    assert text.startswith("import manimgx as m")
    two = GOOD + "\n\nclass Other(m.ThreeDScene):\n    def construct(self):\n        pass\n"
    assert ms.static_check(two)[1] == "Hello" and ms.static_check(two, "Other")[1] == "Other"
    with pytest.raises(ms.ManimError) as unknown:
        ms.static_check(two, "Nope")
    assert unknown.value.code == "unknown_scene" and "Hello, Other" in unknown.value.message


@pytest.mark.parametrize("code, slug, needle", [
    ("", "empty_scene", "empty"),
    ("import manimgx as m\nclass A(m.Scene):\n    def construct(self)\n        pass\n", "syntax_error", "line 3"),
    ("import manimgx as m\nx = 1\n", "no_scene", "m.Scene"),
    ("class A:\n    def construct(self):\n        pass\n", "no_scene", "m.Scene"),
    ("import os\n" + GOOD, "forbidden_code", "line 1"),
    ("from subprocess import run\n" + GOOD, "forbidden_code", "subprocess"),
    (GOOD.replace("m.Text(\"hola\")", "open('x').read()"), "forbidden_code", "open()"),
    (GOOD.replace("m.Text(\"hola\")", "eval('1')"), "forbidden_code", "eval()"),
    (GOOD.replace("m.Text(\"hola\")", "(1).__class__"), "forbidden_code", "__class__"),
    pytest.param("x" * (ms.MAX_CODE_CHARS + 1), "scene_too_long", "limit", id="too_long"),
])
def test_a_scene_that_breaks_the_rules_is_refused_with_a_readable_message(code, slug, needle):
    with pytest.raises(ms.ManimError) as caught:
        ms.static_check(code)
    assert caught.value.code == slug and needle in caught.value.message


def test_numbers_and_maths_modules_are_allowed():
    ms.static_check("import math\nimport random\nimport numpy as np\nimport itertools\n" + GOOD)


def test_every_example_passes_the_static_rules():
    for example in ms.EXAMPLES:
        text, scene = ms.static_check(example["code"])
        assert scene and text.startswith(("import", "from"))
    listing = ms.examples()
    assert {e["kind"] for e in listing["examples"]} == {"2d", "3d"} and listing["notes"]


def test_quality_presets_and_overrides_are_validated():
    assert ms.quality_settings(None, None, None, None, None, None)["width"] == 1920
    assert ms.quality_settings("draft", None, None, None, None, None)["height"] == 720
    custom = ms.quality_settings("standard", 1080, 1920, 24, "fast", 20)
    assert (custom["width"], custom["height"], custom["fps"], custom["preset"]) == (1080, 1920, 24, "fast")
    for args, slug in [(("best", None, None, None, None, None), "bad_quality"), (("draft", 1281, None, None, None, None), "bad_size"),
                       (("draft", None, 99999, None, None, None), "bad_size"), (("draft", None, None, 120, None, None), "bad_fps"),
                       (("draft", None, None, None, "turbo", None), "bad_preset"), (("draft", None, None, None, None, 99), "bad_crf")]:
        with pytest.raises(ms.ManimError) as caught:
            ms.quality_settings(*args)
        assert caught.value.code == slug


# ------------------------------------------------------------------ the engine's words

def test_a_traceback_becomes_one_readable_message(tmp_path):
    error = ms.readable_failure(TRACE.replace("scene.py", f"{tmp_path}\\scene.py"), tmp_path)
    assert error.code == "scene_error"
    assert "line 6" in error.message and "AttributeError" in error.message and "ManimGX has no Foo" in error.message
    assert "self.play(m.Foo(t))" in error.message and error.details["line"] == 6
    named = ms.readable_failure("error: scene.py has no scene 'Nope'. Its scenes: Hello\n", tmp_path)
    assert named.code == "unknown_scene" and "Hello" in named.message
    assert ms.readable_failure("", tmp_path).code == "render_failed"


def test_the_inspect_report_is_parsed(tmp_path):
    out = (f"{tmp_path}\\scene.py: Hello, 2.50 s, 2 plays\nstoryboard: {tmp_path}\\storyboard.png\n"
           "  #0   0.00\u2013 2.00s  scene.py:6  Write(Text 'hola')\n  #1   2.00\u2013 2.50s  scene.py:7  wait\n"
           "layout: 2 problems\n  [1] t=1\ufffd2s (scene.py:6)  c (Text 'z'): 4.39 past the right edge\n"
           "  [2] t=1-2s (scene.py:6)  a and b: texts overlap\n")
    report = ms.parse_report(out, tmp_path)
    assert (report["scene"], report["duration_s"], report["plays"]) == ("Hello", 2.5, 2)
    assert [p["line"] for p in report["timeline"]] == [6, 7] and report["timeline"][0]["what"].startswith("Write")
    assert report["layout_problems"][0].startswith("t=1-2s") and len(report["layout_problems"]) == 2
    video = ms.parse_report("clip.mp4  4.00 s, 1280x720 at 30 fps, 0.0 MB (rendered in 0.8 s)\n", tmp_path)["video"]
    assert video == {"duration_s": 4.0, "width": 1280, "height": 720, "fps": 30.0, "render_s": 0.8}


def test_code_is_taken_out_of_a_models_reply():
    assert ms.extract_code("Here:\n```python\nprint(1)\n```\nbye").strip() == "print(1)"
    assert ms.extract_code("```py\na=1\n```\n```python\nlonger = 12345\n```").strip() == "longer = 12345"
    assert ms.extract_code("import manimgx as m").strip() == "import manimgx as m"
    assert ms.extract_code("```python\nunclosed = 1").strip() == "unclosed = 1"


# ------------------------------------------------------------------ check and write (simulated engine)

def test_check_reports_the_timeline_and_layout_warnings(tmp_path, fake):
    ok = ms.check(tmp_path, GOOD)
    assert ok["ok"] and ok["scene"] == "Hello" and ok["duration_s"] == 2.5 and ok["plays"] == 2 and not ok["has_layout_problems"]
    assert fake.calls[0][:3] == ["inspect", "scene.py", "Hello"]
    warned = ms.check(tmp_path, OVERLAP, times=["1.5", "end"])
    assert warned["ok"] and warned["has_layout_problems"] and "texts overlap" in warned["layout_problems"][0]
    assert fake.calls[-1].count("-t") == 2
    with pytest.raises(ms.ManimError) as caught:
        ms.check(tmp_path, BAD_NAME)
    assert caught.value.code == "scene_error" and "AttributeError" in caught.value.message
    assert not list((tmp_path / "tmp" / "manim").glob("*")), "the scratch folders are removed"


def test_the_engine_must_be_installed(tmp_path):
    with pytest.raises(ms.ManimError) as caught:
        ms.check(tmp_path, GOOD)
    assert caught.value.code == "manim_not_installed" and "install" in caught.value.message


def test_a_scene_longer_than_the_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "_run_engine", lambda *a, **k: subprocess.CompletedProcess([], 0, "scene.py: Hello, 999.00 s, 3 plays\nlayout: ok\n", ""))
    with pytest.raises(ms.ManimError) as caught:
        ms.check(tmp_path, GOOD)
    assert caught.value.code == "scene_too_long"


def test_the_model_writes_code_and_corrects_it_with_the_engines_errors(tmp_path, fake):
    replies = ["```python\n" + BAD_NAME + "```", "```python\n" + GOOD + "```"]
    seen = []

    def chat(messages, max_tokens, temperature):
        seen.append(messages)
        return replies[len(seen) - 1]

    out = ms.write_scene(tmp_path, chat, "a greeting", language="Spanish")
    assert out["attempts"] == 2 and out["scene"] == "Hello" and len(out["errors"]) == 1 and "AttributeError" in out["errors"][0]
    assert "a greeting" in seen[0][1]["content"] and "Spanish" in seen[0][1]["content"]
    assert "m.Foo" in seen[1][1]["content"] and "AttributeError" in seen[1][1]["content"]   # the second try sees the code and the error


def test_without_a_model_or_with_a_model_that_keeps_failing_the_error_says_so(tmp_path, fake):
    with pytest.raises(ms.ManimError) as none:
        ms.write_scene(tmp_path, None, "x")
    assert none.value.code == "no_llm" and "pass the scene code" in none.value.message

    def down(*_):
        raise ConnectionError("no model resident")
    with pytest.raises(ms.ManimError) as unreachable:
        ms.write_scene(tmp_path, down, "x")
    assert unreachable.value.code == "no_llm" and "ConnectionError" in unreachable.value.message
    with pytest.raises(ms.ManimError) as stuck:
        ms.write_scene(tmp_path, lambda *_: BAD_NAME, "x", attempts=2)
    assert stuck.value.code == "write_failed" and len(stuck.value.details["errors"]) == 2 and "Foo" in stuck.value.details["code"]


# ------------------------------------------------------------------ the API

def test_examples_and_status_routes(client):
    c, app, _ = client
    listing = c.get("/api/manim/examples").json()
    assert {"formula_euler", "grafica_seno", "cubo_3d"} <= {e["id"] for e in listing["examples"]}
    assert c.get("/api/agent/manim_examples").json()["notes"]
    status = c.get("/api/manim/status").json()
    assert status["installed"] is False and status["pin"].startswith("manimgx==") and status["qualities"]["draft"]["height"] == 720
    assert c.post("/api/agent/manim_engine", json={"action": "status"}).json()["installed"] is False
    assert c.post("/api/manim/engine", json={"action": "remove"}).json()["error"] == "bad_action"


def test_installing_runs_as_a_job_and_is_a_noop_when_installed(client, monkeypatch):
    c, app, _ = client
    monkeypatch.setattr(ms, "install", lambda data_dir, progress=None: {"engine": "manimgx", "version": "0.1.1"})
    posted = c.post("/api/agent/manim_engine", json={"action": "install", "wait_s": 30}).json()
    assert posted["job"]["state"] == "done" and posted["job"]["result"]["version"] == "0.1.1"
    monkeypatch.setattr(ms, "_engine_version", lambda data_dir: "0.1.1")
    again = c.post("/api/manim/engine", json={"action": "install"}).json()
    assert again["installed"] is True and again["already_installed"] is True


def test_a_scene_renders_into_a_project_clip_that_an_edit_plan_can_use(installed):
    c, app, fake = installed
    pid = c.post("/api/agent/studio_create_project", json={"name": "Manim"}).json()["id"]
    r = c.post("/api/agent/manim_render", json={"project": pid, "code": GOOD, "name": "Saludo", "quality": "draft", "width": 320,
                                                 "height": 180, "fps": 24, "wait_s": 60})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["state"] == "done", job
    asset = job["assets"][0]
    assert asset["kind"] == "video" and (asset["width"], asset["height"]) == (320, 180) and asset["name"] == "Saludo.mp4"
    assert job["result"]["scene"] == "Hello" and "m.Text" in job["result"]["code"] and job["result"]["fps"] == 24
    assert any(call[0] == "render" and "320x180" in call and "24" in call for call in fake.calls)
    stored = c.get(f"/api/assets/{asset['id']}").json()
    assert "manim" in stored["tags"] and stored["recipe"]["operation"] == "manim_render" and stored["recipe"]["scene"] == "Hello"
    assert stored["recipe"]["code"].startswith("import manimgx") and stored["recipe"]["input_asset_ids"] == []
    listed = c.get(f"/api/agent/studio_assets", params={"project": pid, "kind": "video"}).json()
    assert asset["id"] in str(listed)
    plan = c.post("/api/agent/edit_plan", json={"project": pid, "name": "Con escena", "edit": {
        "width": 320, "height": 180, "fps": 24, "clips": [{"asset_id": asset["id"]}, {"asset_id": asset["id"], "out_s": 0.5,
                                                                                       "transition": {"type": "crossfade", "duration_s": 0.2}}]}})
    assert plan.status_code == 200, plan.text
    run = c.post("/api/agent/edit_run_plan", json={"plan_id": plan.json()["plan_id"], "wait_s": 90}).json()
    assert run["job"]["state"] == "done", run
    assert run["job"]["assets"][0]["kind"] == "video"


def test_a_broken_scene_is_refused_before_it_queues_and_a_failing_one_fails_the_job_readably(installed):
    c, app, fake = installed
    pid = c.post("/api/agent/studio_create_project", json={"name": "Manim"}).json()["id"]
    post = lambda **body: c.post("/api/agent/manim_render", json={"project": pid, **body})   # noqa: E731
    syntax = post(code="import manimgx as m\nclass A(m.Scene):\n    def construct(self)\n        pass\n")
    assert syntax.status_code == 400 and syntax.json()["error"] == "syntax_error" and "line 3" in syntax.json()["message"]
    forbidden = post(code="import os\n" + GOOD)
    assert forbidden.status_code == 400 and forbidden.json()["error"] == "forbidden_code"
    assert post().json()["error"] == "empty_scene"
    assert post(code=GOOD, quality="best").json()["error"] == "bad_quality"
    assert c.post("/api/agent/manim_render", json={"code": GOOD}).json()["error"] == "project_required"
    assert c.post("/api/agent/manim_render", json={"project": "nope", "code": GOOD}).status_code == 404
    assert not fake.calls, "nothing reached the engine"
    failing = post(code=BAD_NAME, wait_s=60).json()["job"]
    assert failing["state"] == "failed"
    assert "line 6" in failing["error"] and "AttributeError" in failing["error"] and "ManimGX has no Foo" in failing["error"]
    assert c.get("/api/agent/studio_assets", params={"project": pid, "kind": "video"}).json()["items"] == []


def test_without_the_engine_the_render_says_how_to_install_it(client, fake):
    c, app, _ = client
    pid = c.post("/api/agent/studio_create_project", json={"name": "Manim"}).json()["id"]
    r = c.post("/api/agent/manim_render", json={"project": pid, "code": GOOD})
    assert r.status_code == 400 and r.json()["error"] == "manim_not_installed" and "manim_engine" in r.json()["message"]


def test_a_description_is_written_by_the_model_inside_the_job(installed):
    c, app, fake = installed
    pid = c.post("/api/agent/studio_create_project", json={"name": "Manim"}).json()["id"]
    app.state.short_hooks = {"chat": lambda messages, max_tokens, temperature: "```python\n" + GOOD + "```"}
    job = c.post("/api/agent/manim_render", json={"project": pid, "description": "say hello", "quality": "draft", "width": 320,
                                                   "height": 180, "wait_s": 60}).json()["job"]
    assert job["state"] == "done" and job["result"]["written_by_model"] is True and job["result"]["attempts"] == 1
    app.state.short_hooks = {"chat": lambda *_: (_ for _ in ()).throw(ConnectionError("model down"))}
    down = c.post("/api/agent/manim_render", json={"project": pid, "description": "again", "wait_s": 60}).json()["job"]
    assert down["state"] == "failed" and "pass the scene code" in down["error"]


def test_check_returns_the_report_and_saves_the_storyboard_as_an_image(installed):
    c, app, fake = installed
    pid = c.post("/api/agent/studio_create_project", json={"name": "Manim"}).json()["id"]
    r = c.post("/api/agent/manim_check", json={"code": OVERLAP, "project": pid, "times": ["1.5"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["scene"] == "Hello" and body["has_layout_problems"] and body["timeline"][0]["line"] == 6
    shot = c.get(f"/api/assets/{body['storyboard_asset_id']}").json()
    assert shot["kind"] == "image" and "storyboard" in shot["tags"]
    plain = c.post("/api/manim/check", json={"code": GOOD}).json()
    assert plain["ok"] and "storyboard_asset_id" not in plain
    bad = c.post("/api/agent/manim_check", json={"code": BAD_NAME})
    assert bad.status_code == 400 and bad.json()["error"] == "scene_error" and "line 6" in bad.json()["message"]
    app.state.short_hooks = {"chat": lambda *_: GOOD}
    written = c.post("/api/agent/manim_check", json={"description": "hello", "project": pid}).json()
    assert written["written_by_model"] is True and written["storyboard_asset_id"]
    assert c.post("/api/agent/manim_check", json={}).json()["error"] == "empty_scene"


def test_the_ui_route_queues_a_render_for_the_project(installed):
    c, app, fake = installed
    pid = c.post("/api/agent/studio_create_project", json={"name": "Manim"}).json()["id"]
    r = c.post(f"/api/projects/{pid}/manim/render", json={"code": GOOD, "quality": "draft", "width": 320, "height": 180})
    assert r.status_code == 200 and r.json()["job"]["type"] == "manim_render"
    job = _wait(c, r.json()["job"]["id"])
    assert job["state"] == "done"
