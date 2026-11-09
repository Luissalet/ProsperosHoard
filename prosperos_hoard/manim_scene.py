"""Manim scenes (ManimGX) as video clips: 2D maths and 3D animation drawn by code on the GPU, rendered to an MP4.

ManimGX (``pip install manimgx``, MIT, a Rust/wgpu core with a Manim Community compatible Python API) needs Python 3.13+
and its own native wheels, so it lives in an environment of its own, ``<data>/engines/manimgx/venv``, built by the
explicit install action (never silently), and every call runs out of process with that interpreter::

    python -I -m manimgx inspect scene.py [Scene]   # checks the scene without a video: timeline, storyboard, layout problems
    python -I -m manimgx render  scene.py [Scene] -o out.mp4 -r WxH --fps N --preset P --crf C

A scene is Python code (``import manimgx as m``, a class deriving ``m.Scene`` or ``m.ThreeDScene`` with ``construct``), given
by the caller or written by the language model Prospero already uses for writing from a description. The code is checked
statically first (syntax, a scene class, no file/network/process access) and then by ``inspect``; a failure comes back as a
readable message (line, exception, hint) so the caller can correct the code and try again. The rendered MP4 becomes an
ordinary video asset of the project (recipe: the code, the scene, the size), usable in edit plans and timelines like any clip.

Engine settings live in ``<data>/engines/manimgx/engine.json``: ``{"env": {"VK_LOADER_DEVICE_ID_FILTER": "0x2d04"}}`` limits
which GPUs the Vulkan loader shows ManimGX (the render takes the first one it sees); ``PROSPERO_MANIM_GPU_FILTER`` sets the
same filter, ``PROSPERO_MANIM_PYTHON`` names the Python 3.13+ the environment is built with.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import engine
from .hoard_link import proc as hlproc
from .jobs import JobCancelled
from .store import Store

PIN = "manimgx==0.1.1"
MIN_PYTHON = (3, 13)
MAX_CODE_CHARS = 60_000
MAX_SCENE_SECONDS = 240.0
CHECK_TIMEOUT_S = 180.0
RENDER_TIMEOUT_S = 900.0
INSTALL_TIMEOUT_S = 1800.0
RECIPE_CODE_CHARS = 20_000
QUALITIES = {
    "draft": {"width": 1280, "height": 720, "fps": 30, "preset": "veryfast", "crf": 28.0},
    "standard": {"width": 1920, "height": 1080, "fps": 30, "preset": "medium", "crf": 23.0},
    "high": {"width": 1920, "height": 1080, "fps": 60, "preset": "medium", "crf": 20.0},
}
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
ENV_KEYS = ("VK_LOADER_DEVICE_ID_FILTER", "VK_LOADER_VENDOR_ID_FILTER", "VK_LOADER_DRIVER_ID_FILTER", "WGPU_ADAPTER_NAME",
            "WGPU_BACKEND", "MANIMGX_CACHE_DIR")

#: modules a scene has no business importing (files, processes, the network, the interpreter's own machinery)
DENIED_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "shutil", "ctypes", "importlib", "builtins", "multiprocessing", "threading", "signal",
    "pickle", "marshal", "shelve", "http", "urllib", "ftplib", "smtplib", "telnetlib", "requests", "httpx", "aiohttp", "webbrowser",
    "tempfile", "glob", "pathlib", "io", "inspect", "gc", "code", "codeop", "runpy", "pty", "asyncio", "sqlite3", "zipfile",
    "tarfile", "ssl", "platform", "site", "sysconfig", "pip", "setuptools",
})
DENIED_CALLS = frozenset({"exec", "eval", "compile", "open", "__import__", "input", "breakpoint", "globals", "locals", "vars",
                          "getattr", "setattr", "delattr", "exit", "quit"})
DENIED_ATTRS = frozenset({"__class__", "__subclasses__", "__globals__", "__builtins__", "__dict__", "__mro__", "__bases__",
                          "__code__", "__import__", "__getattribute__", "__loader__", "__spec__", "__reduce__", "__closure__"})


class ManimError(engine.EngineError):
    """A readable failure: ``code`` is a short slug, ``message`` says what to change, ``details`` carries the line and tail."""

    def __init__(self, code: str, message: str, details: Optional[dict[str, Any]] = None):
        super().__init__(code, message)
        self.details = details or {}


# ------------------------------------------------------------------ the engine's environment

def env_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "engines" / "manimgx"


def venv_python(data_dir: Path) -> Path:
    return env_dir(data_dir) / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def settings(data_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((env_dir(data_dir) / "engine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def engine_env(data_dir: Path) -> dict[str, str]:
    """The environment variables the engine runs with: its cache beside the environment, then engine.json, then the PC's own."""
    extra: dict[str, str] = {"MANIMGX_CACHE_DIR": str(env_dir(data_dir) / "cache")}
    saved = settings(data_dir).get("env")
    if isinstance(saved, dict):
        extra.update({k: str(v) for k, v in saved.items() if k in ENV_KEYS and str(v).strip()})
    filter_id = os.getenv("PROSPERO_MANIM_GPU_FILTER", "").strip()
    if filter_id:
        extra["VK_LOADER_DEVICE_ID_FILTER"] = filter_id
    return extra


def _python_version(exe: str) -> Optional[tuple[int, int]]:
    try:
        out = hlproc.run([exe, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)", (out.stdout or "").strip())
    return (int(match.group(1)), int(match.group(2))) if out.returncode == 0 and match else None


def find_base_python() -> Optional[str]:
    """A Python 3.13+ to build the environment with: PROSPERO_MANIM_PYTHON, this interpreter, the py launcher, then PATH."""
    candidates: list[str] = []
    explicit = os.getenv("PROSPERO_MANIM_PYTHON", "").strip().strip('"')
    if explicit:
        candidates.append(explicit)
    if sys.version_info[:2] >= MIN_PYTHON and sys.executable:
        candidates.append(sys.executable)
    launcher = shutil.which("py")
    if launcher:
        for version in ("3.14", "3.13"):
            try:
                out = hlproc.run([launcher, f"-{version}", "-c", "import sys; print(sys.executable)"], text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if out.returncode == 0 and (out.stdout or "").strip():
                candidates.append(out.stdout.strip())
    for name in ("python3.14", "python3.13", "python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    seen: set[str] = set()
    for exe in candidates:
        if exe in seen or not Path(exe).is_file():
            continue
        seen.add(exe)
        version = _python_version(exe)
        if version and version >= MIN_PYTHON:
            return exe
    return None


def _engine_version(data_dir: Path) -> Optional[str]:
    py = venv_python(data_dir)
    if not py.is_file():
        return None
    try:
        out = hlproc.run([str(py), "-I", "-c", "import importlib.metadata as m; print(m.version('manimgx'))"], text=True, timeout=60,
                         env=hlproc.build_env(**engine_env(data_dir)))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (out.stdout or "").strip() or None if out.returncode == 0 else None


def status(data_dir: Path) -> dict[str, Any]:
    version = _engine_version(data_dir)
    base = None if version else find_base_python()
    return {"installed": version is not None, "version": version, "pin": PIN, "python_required": "3.13+",
            "environment": str(env_dir(data_dir) / "venv"), "python_found": base, "env": engine_env(data_dir),
            "qualities": QUALITIES,
            "hint": None if version else ("install it with manim_engine(action='install') or the Install button"
                                          if base else "needs Python 3.13 or newer on this PC (or PROSPERO_MANIM_PYTHON)")}


def install(data_dir: Path, progress: Optional[Callable[[float, str], None]] = None) -> dict[str, Any]:
    """The explicit install action: an environment with a Python 3.13+, then ``pip install manimgx``."""
    say = progress or (lambda *_: None)
    root = env_dir(data_dir)
    base = find_base_python()
    if not base:
        raise ManimError("manim_needs_python", "ManimGX needs Python 3.13 or newer on this PC (or set PROSPERO_MANIM_PYTHON to one)")
    root.mkdir(parents=True, exist_ok=True)
    py = venv_python(data_dir)
    if not py.is_file():
        say(0.1, f"creating the ManimGX environment with {base}")
        made = hlproc.run([base, "-m", "venv", str(root / "venv")], text=True, timeout=600)
        if made.returncode != 0:
            raise ManimError("manim_install_failed", hlproc.tail_lines(made.stderr or made.stdout) or "could not create the environment")
    say(0.3, f"installing {PIN}")
    done = hlproc.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", PIN], text=True, timeout=INSTALL_TIMEOUT_S,
                      env=hlproc.build_env())
    if done.returncode != 0:
        raise ManimError("manim_install_failed", hlproc.tail_lines(done.stderr or done.stdout, 20, 2000) or "pip failed")
    version = _engine_version(data_dir)
    if not version:
        raise ManimError("manim_install_failed", "the package installed but cannot be imported")
    say(1.0, "ManimGX ready")
    return {"engine": "manimgx", "version": version, "python": base, "environment": str(root / "venv")}


# ------------------------------------------------------------------ the code

_IMPORT_FIX = [
    (re.compile(r"^(\s*)from\s+manim\s+import\b", re.M), r"\1from manimgx import"),
    (re.compile(r"^(\s*)import\s+manim(\s+as\s+\w+)?\s*$", re.M), lambda m: f"{m.group(1)}import manimgx{m.group(2) or ' as manim'}"),
]


def normalise_code(code: str) -> str:
    """Manim Community imports become ManimGX ones (``from manim import *``, ``import manim as m``); the rest stays as given."""
    text = str(code or "").replace("\r\n", "\n").lstrip("﻿")
    for pattern, repl in _IMPORT_FIX:
        text = pattern.sub(repl, text)
    return text if text.endswith("\n") else text + "\n"


def scene_classes(tree: ast.AST) -> list[str]:
    """Classes whose base looks like a scene (``Scene``, ``m.ThreeDScene``, ``MovingCameraScene``...) and that define construct."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else "" for b in node.bases]
        has_construct = any(isinstance(n, ast.FunctionDef) and n.name == "construct" for n in node.body)
        if has_construct and any(b.endswith("Scene") for b in bases):
            found.append(node.name)
    return found


def static_check(code: str, scene: Optional[str] = None) -> tuple[str, str]:
    """(normalised code, scene name) or a ManimError that says what to change. Not a sandbox: it keeps honest scenes honest."""
    if not isinstance(code, str) or not code.strip():
        raise ManimError("empty_scene", "the scene code is empty; give code or a description")
    if len(code) > MAX_CODE_CHARS:
        raise ManimError("scene_too_long", f"the scene code is {len(code)} characters; the limit is {MAX_CODE_CHARS}")
    text = normalise_code(code)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        line = exc.lineno or 0
        source = (text.splitlines()[line - 1].strip() if 0 < line <= len(text.splitlines()) else "")
        raise ManimError("syntax_error", f"syntax error at line {line}: {exc.msg}" + (f" -> {source}" if source else ""),
                         {"line": line, "source": source}) from None
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for name in names:
            if name.split(".")[0] in DENIED_MODULES:
                raise ManimError("forbidden_code", f"line {node.lineno}: a scene may not import '{name}' (files, processes and "
                                 "the network are off limits); draw with manimgx and use math/numpy/random/itertools for numbers",
                                 {"line": node.lineno})
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in DENIED_CALLS:
            raise ManimError("forbidden_code", f"line {node.lineno}: a scene may not call {node.func.id}()", {"line": node.lineno})
        if isinstance(node, ast.Attribute) and node.attr in DENIED_ATTRS:
            raise ManimError("forbidden_code", f"line {node.lineno}: a scene may not use .{node.attr}", {"line": node.lineno})
        if isinstance(node, ast.Name) and node.id in DENIED_ATTRS:
            raise ManimError("forbidden_code", f"line {node.lineno}: a scene may not use {node.id}", {"line": node.lineno})
    scenes = scene_classes(tree)
    if not scenes:
        raise ManimError("no_scene", "define a class that derives from m.Scene (or m.ThreeDScene) with a construct(self) method, "
                         "after `import manimgx as m`")
    if scene:
        if scene not in scenes:
            raise ManimError("unknown_scene", f"there is no scene '{scene}' in the code; its scenes: {', '.join(scenes)}")
        return text, scene
    return text, scenes[0]


# ------------------------------------------------------------------ running the engine

def _run_engine(data_dir: Path, args: list[str], cwd: Path, timeout: float, cancel: Optional[threading.Event] = None
                ) -> subprocess.CompletedProcess:
    """One call to the engine's CLI out of process. Tests replace this function with a simulated engine."""
    py = venv_python(data_dir)
    if not py.is_file():
        raise ManimError("manim_not_installed", "the ManimGX engine is not installed; install it with manim_engine(action='install') "
                         "or the Install button on the Manim scenes panel")
    return hlproc.run([str(py), "-I", "-m", "manimgx", *args], text=True, timeout=timeout, cwd=cwd, cancel=cancel,
                      env=hlproc.build_env(**engine_env(data_dir)))


def _tidy(text: str, workdir: Path) -> str:
    """The engine's text without the scratch path; its dashes arrive in the console code page, so a replacement character is a dash."""
    text = (text or "").replace("\ufffd", "-")
    return text.replace(str(workdir) + os.sep, "").replace(str(workdir), "").replace("\\", "/") if workdir else text


def readable_failure(output: str, workdir: Path, scene_file: str = "scene.py") -> ManimError:
    """A Python traceback or an engine error as one message: where (line), what (exception) and the engine's hint."""
    lines = [ln.rstrip() for ln in _tidy(output, workdir).splitlines() if ln.strip()]
    if not lines:
        return ManimError("render_failed", "the engine failed without saying why")
    exc_line = next((ln for ln in reversed(lines) if re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Warning)\b|^error:", ln)), lines[-1])
    hint = next((ln for ln in reversed(lines) if ln.lower().startswith("hint:")), "")
    where = next((ln for ln in lines if re.search(rf"{re.escape(scene_file)}:(\d+)", ln)), "")
    line_no = int(re.search(rf"{re.escape(scene_file)}:(\d+)", where).group(1)) if where else None
    source = ""
    if where:
        index = lines.index(where)
        if index + 1 < len(lines) and not lines[index + 1].startswith(("hint", "error")):
            source = lines[index + 1].strip()
    message = (f"scene error at line {line_no}: " if line_no else "scene error: ") + exc_line.strip()
    if source and source not in exc_line:
        message += f" -> {source}"
    if hint and hint not in exc_line:
        message += f" ({hint[5:].strip()})"
    if exc_line.startswith("error:") and "has no scene" in exc_line:
        return ManimError("unknown_scene", exc_line[6:].strip(), {"tail": "\n".join(lines[-6:])})
    return ManimError("scene_error", message, {"line": line_no, "source": source, "tail": "\n".join(lines[-12:])})


_HEADER = re.compile(r"^(?P<path>.+): (?P<scene>[^:,]+), (?P<dur>[\d.]+) s, (?P<plays>\d+) plays?")
_PLAY = re.compile(r"^\s*#(?P<n>\d+)\s+(?P<a>[\d.]+)\D+?(?P<b>[\d.]+)s\s+\S+:(?P<line>\d+)\s+(?P<what>.*)$")
_PROBLEM = re.compile(r"^\s*\[(?P<n>\d+)\]\s+(?P<what>.*)$")
_RENDERED = re.compile(r"(?P<dur>[\d.]+) s, (?P<w>\d+)x(?P<h>\d+) at (?P<fps>[\d.]+) fps, (?P<mb>[\d.]+) MB \(rendered in (?P<took>[\d.]+) s\)")


def parse_report(output: str, workdir: Path) -> dict[str, Any]:
    """The engine's plain report (timeline, layout problems, storyboard) as data."""
    report: dict[str, Any] = {"timeline": [], "layout_problems": [], "layout": None, "storyboard": None}
    for raw in _tidy(output, workdir).splitlines():
        line = raw.rstrip()
        header = _HEADER.match(line)
        if header and "storyboard:" not in line:
            report.update(scene=header.group("scene"), duration_s=float(header.group("dur")), plays=int(header.group("plays")))
            continue
        if line.startswith("storyboard:"):
            report["storyboard"] = line.split(":", 1)[1].strip()
            continue
        play = _PLAY.match(line)
        if play:
            report["timeline"].append({"n": int(play.group("n")), "start_s": float(play.group("a")), "end_s": float(play.group("b")),
                                       "line": int(play.group("line")), "what": play.group("what").strip()})
            continue
        if line.startswith("layout:"):
            report["layout"] = line.split(":", 1)[1].strip()
            continue
        problem = _PROBLEM.match(line)
        if problem:
            report["layout_problems"].append(problem.group("what").strip())
    rendered = _RENDERED.search(_tidy(output, workdir))
    if rendered:
        report["video"] = {"duration_s": float(rendered.group("dur")), "width": int(rendered.group("w")), "height": int(rendered.group("h")),
                           "fps": float(rendered.group("fps")), "render_s": float(rendered.group("took"))}
    return report


class _Scratch:
    """A scratch folder under the data dir for one call: the scene file and what the engine writes beside it."""

    def __init__(self, data_dir: Path, tag: str = "manim"):
        base = Path(data_dir) / "tmp" / "manim"
        base.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix=f"{tag}-", dir=base))

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_: Any) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _watch_cancel(should_cancel: Optional[Callable[[], bool]]) -> tuple[Optional[threading.Event], Callable[[], None]]:
    if should_cancel is None:
        return None, lambda: None
    event, stop = threading.Event(), threading.Event()

    def watch() -> None:
        while not stop.wait(0.4):
            if should_cancel():
                event.set()
                return

    threading.Thread(target=watch, name="prospero-manim-cancel", daemon=True).start()
    return event, stop.set


def check(data_dir: Path, code: str, scene: Optional[str] = None, *, times: Optional[list[str]] = None,
          keep_storyboard: Optional[Path] = None) -> dict[str, Any]:
    """Validate a scene without rendering a video: the static rules, then the engine's ``inspect`` (timeline, a storyboard picture,
    layout problems). ``ok`` is false only when the code cannot run; layout problems are warnings in ``layout_problems``."""
    text, name = static_check(code, scene)
    with _Scratch(data_dir, "check") as work:
        (work / "scene.py").write_text(text, encoding="utf-8")
        args = ["inspect", "scene.py", name, "-o", "storyboard.png"]
        for moment in times or []:
            args += ["-t", str(moment)]
        try:
            done = _run_engine(data_dir, args, work, CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ManimError("check_timeout", f"the check took longer than {int(CHECK_TIMEOUT_S)} s; shorten the scene or its loops") from None
        out = (done.stdout or "") + "\n" + (done.stderr or "")
        # exit 1 with a "layout:" line is a layout warning; exit 1 without it (or any other non-zero) is a scene that could not run
        if "Traceback" in out or done.returncode not in (0, 1) or (done.returncode == 1 and "layout:" not in out):
            raise readable_failure(out, work)
        report = parse_report(out, work)
        if report.get("duration_s", 0) > MAX_SCENE_SECONDS:
            raise ManimError("scene_too_long", f"the scene lasts {report['duration_s']:.0f} s; the limit is {int(MAX_SCENE_SECONDS)} s")
        board = work / "storyboard.png"
        if keep_storyboard is not None and board.is_file():
            keep_storyboard.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(board, keep_storyboard)
            report["storyboard_file"] = str(keep_storyboard)
    report.pop("storyboard", None)
    return {"ok": True, "scene": name, "code": text, "has_layout_problems": bool(report["layout_problems"]), **report}


def quality_settings(quality: Optional[str], width: Optional[int], height: Optional[int], fps: Optional[int],
                     preset: Optional[str], crf: Optional[float]) -> dict[str, Any]:
    base = dict(QUALITIES.get(quality or "standard") or {})
    if not base:
        raise ManimError("bad_quality", f"quality is one of {', '.join(QUALITIES)}")
    for key, value in (("width", width), ("height", height), ("fps", fps), ("preset", preset), ("crf", crf)):
        if value is not None:
            base[key] = value
    for key in ("width", "height"):
        if not isinstance(base[key], int) or not 64 <= base[key] <= 3840 or base[key] % 2:
            raise ManimError("bad_size", f"{key} must be an even whole number between 64 and 3840")
    if not isinstance(base["fps"], int) or not 12 <= base["fps"] <= 60:
        raise ManimError("bad_fps", "fps must be a whole number between 12 and 60")
    if base["preset"] not in PRESETS:
        raise ManimError("bad_preset", f"preset is one of {', '.join(PRESETS)}")
    if not 0 <= float(base["crf"]) <= 51:
        raise ManimError("bad_crf", "crf is between 0 (best) and 51 (smallest)")
    return base


def render_video(data_dir: Path, code: str, scene: Optional[str], out: Path, *, width: int, height: int, fps: int, preset: str,
                 crf: float, should_cancel: Optional[Callable[[], bool]] = None, timeout: float = RENDER_TIMEOUT_S) -> dict[str, Any]:
    """Render the scene to ``out`` (an MP4). Returns the engine's report; raises ManimError with a readable message."""
    text, name = static_check(code, scene)
    cancel, stop_watch = _watch_cancel(should_cancel)
    with _Scratch(data_dir, "render") as work:
        (work / "scene.py").write_text(text, encoding="utf-8")
        target = work / "scene.mp4"
        args = ["render", "scene.py", name, "-o", "scene.mp4", "-r", f"{width}x{height}", "--fps", str(fps), "--preset", preset,
                "--crf", str(crf)]
        try:
            done = _run_engine(data_dir, args, work, timeout, cancel)
        except subprocess.TimeoutExpired:
            raise ManimError("render_timeout", f"the render took longer than {int(timeout)} s; shorten the scene or lower the quality") from None
        except hlproc.Cancelled:
            raise JobCancelled("cancelled") from None
        finally:
            stop_watch()
        output = (done.stdout or "") + "\n" + (done.stderr or "")
        if "Traceback" in output or not target.is_file() or target.stat().st_size == 0 or "rendered in" not in output:
            raise readable_failure(output, work)
        report = parse_report(output, work)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(out))
    report.pop("storyboard", None)
    report["scene"] = name
    return report


# ------------------------------------------------------------------ writing the code from a description

SYSTEM_PROMPT = (
    "You write Python scenes for ManimGX, a Manim Community compatible animation engine. Answer with ONE Python file in a "
    "```python block and nothing else.\n"
    "Rules:\n"
    "- Start with `import manimgx as m` and use the `m.` prefix (m.Scene, m.ThreeDScene, m.Text, m.MathTex, m.Circle, m.Square, "
    "m.Axes, m.VGroup, m.Write, m.Create, m.FadeIn, m.FadeOut, m.Transform, m.ReplacementTransform, m.Rotate, m.UP, m.DOWN, "
    "m.LEFT, m.RIGHT, m.ORIGIN, m.DEGREES, m.PI, m.BLUE, m.RED, m.YELLOW, m.WHITE...).\n"
    "- One class deriving m.Scene (2D) or m.ThreeDScene (3D, call self.set_camera_orientation(phi=..., theta=...)), with a "
    "construct(self) method that builds the animation with self.play(...) and self.wait(...).\n"
    "- Formulas go in m.MathTex(r\"...\") (LaTeX syntax, raw strings); text in m.Text(\"...\").\n"
    "- Keep every object inside the frame (about 14 wide, 8 high; no text overlapping other text) and the whole scene between "
    "3 and 20 seconds (sum of run_time and waits).\n"
    "- Only manimgx, math, numpy, random and itertools may be imported. No files, no network, no input(), no sys/os.\n"
    "- Use only classes and functions that exist in Manim Community; never invent names."
)


def scene_messages(description: str, *, previous: Optional[str] = None, error: Optional[str] = None, language: str = "") -> list[dict[str, str]]:
    user = f"Write a ManimGX scene for this: {description.strip()}\n"
    if language:
        user += f"On-screen text language: {language}.\n"
    if previous and error:
        user += ("\nYour previous code failed:\n```python\n" + previous.strip() + "\n```\nThe engine said:\n" + error.strip() +
                 "\nReturn the complete corrected file.")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def extract_code(reply: str) -> str:
    text = str(reply or "")
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S | re.I)
    if blocks:
        return max(blocks, key=len).strip("\n") + "\n"
    open_block = re.search(r"```(?:python|py)?\s*\n(.*)$", text, re.S | re.I)
    return (open_block.group(1) if open_block else text).strip("\n") + "\n"


def write_scene(data_dir: Path, chat: Optional[Callable[..., str]], description: str, *, scene: Optional[str] = None,
                language: str = "", attempts: int = 3) -> dict[str, Any]:
    """Code from a description with the writing model, checked by the engine and corrected with its own error messages
    (up to ``attempts`` tries). Returns ``check()``'s result plus ``attempts`` and ``errors`` (what each failed try said)."""
    if chat is None:
        raise ManimError("no_llm", "there is no language model to write the scene from a description; pass the scene code instead "
                         "(or start a model in Faustus)")
    errors: list[str] = []
    code, error = None, None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            reply = chat(scene_messages(description, previous=code, error=error, language=language), 3000, 0.4)
        except Exception as exc:  # noqa: BLE001 - Hoard Link: no model resident, a stalled writer...
            raise ManimError("no_llm", f"could not reach a language model to write the scene ({type(exc).__name__}: {str(exc)[:200]}); "
                             "start one in Faustus or pass the scene code") from None
        code = extract_code(reply)
        try:
            result = check(data_dir, code, scene)
        except ManimError as exc:
            if exc.code in ("manim_not_installed", "check_timeout"):
                raise
            error = exc.message + (("\n" + exc.details["tail"]) if exc.details.get("tail") else "")
            errors.append(exc.message)
            continue
        return {**result, "attempts": attempt, "errors": errors}
    raise ManimError("write_failed", f"the model's scene still failed after {len(errors)} tries; last error: {errors[-1]}",
                     {"errors": errors, "code": code})


# ------------------------------------------------------------------ rendering into the project

def _recipe(code: str, name: str, quality: dict[str, Any], description: str, version: Optional[str]) -> dict[str, Any]:
    return {"operation": "manim_render", "engine": f"manimgx {version or ''}".strip(), "scene": name, "input_asset_ids": [],
            "width": quality["width"], "height": quality["height"], "fps": quality["fps"], "preset": quality["preset"],
            "crf": quality["crf"], "code": code[:RECIPE_CODE_CHARS], "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest()[:16],
            "description": (description or "")[:400]}


def render_job(store: Store, job: dict[str, Any], progress: Any, chat: Optional[Callable[..., str]] = None) -> dict[str, Any]:
    """The ``manim_render`` job: code (given, or written from a description) checked, rendered and saved as a video asset."""
    p = job["params"]
    data_dir = store.data_dir
    cancelled = getattr(progress, "cancelled", None)
    quality = quality_settings(p.get("quality"), p.get("width"), p.get("height"), p.get("fps"), p.get("preset"), p.get("crf"))
    code, scene, description = p.get("code"), p.get("scene"), (p.get("description") or "").strip()
    written = None
    if not code:
        if not description:
            raise ManimError("empty_scene", "give the scene code or a description of what to draw")
        progress(0.04, "writing the scene")
        written = write_scene(data_dir, chat, description, scene=scene, language=p.get("language") or "")
        code, scene = written["code"], written["scene"]
    else:
        progress(0.04, "checking the scene")
        check_result = check(data_dir, code, scene)
        code, scene = check_result["code"], check_result["scene"]
    progress(0.2, "rendering the scene")
    version = _engine_version(data_dir)
    name = (p.get("name") or scene)[:80]
    with _Scratch(data_dir, "clip") as work:
        out = work / "clip.mp4"
        report = render_video(data_dir, code, scene, out, width=quality["width"], height=quality["height"], fps=quality["fps"],
                              preset=quality["preset"], crf=float(quality["crf"]), should_cancel=cancelled)
        progress(0.92, "saving the clip")
        asset = engine.import_asset(store, p["project_id"], out, "video", original_name=f"{name}.mp4",
                                    recipe=_recipe(code, scene, quality, description, version), tags=["manim", "scene"], source="rendered")
    result: dict[str, Any] = {"asset_id": asset["id"], "asset_ids": [asset["id"]], "scene": scene, "code": code,
                              "duration_s": asset.get("duration_s"), "width": asset.get("width"), "height": asset.get("height"),
                              "fps": quality["fps"], "render_s": (report.get("video") or {}).get("render_s"),
                              "layout_problems": report.get("layout_problems") or []}
    if written:
        result["written_by_model"] = True
        result["attempts"] = written["attempts"]
    return result


def install_job(store: Store, job: dict[str, Any], progress: Any) -> dict[str, Any]:
    return install(store.data_dir, progress)


def check_in_project(store: Store, project_id: Optional[str], *, code: Optional[str] = None, description: str = "",
                     scene: Optional[str] = None, times: Optional[list[str]] = None, chat: Optional[Callable[..., str]] = None,
                     language: str = "") -> dict[str, Any]:
    """``check`` for the API: code, or a description the model writes into code first; the engine's storyboard picture is saved as
    an image asset of the project when a project is given (``storyboard_asset_id``)."""
    if project_id:
        store.get_project(project_id)
    data_dir = store.data_dir
    picture = Path(data_dir) / "tmp" / "manim" / f"storyboard-{hashlib.sha1(os.urandom(8)).hexdigest()[:10]}.png"
    try:
        if code and code.strip():
            result = check(data_dir, code, scene, times=times, keep_storyboard=picture if project_id else None)
        elif description.strip():
            result = write_scene(data_dir, chat, description, scene=scene, language=language)
            if project_id:   # the writer's check kept no picture: look again for the one the caller will see
                result = {**check(data_dir, result["code"], result["scene"], times=times, keep_storyboard=picture),
                          "attempts": result["attempts"], "errors": result["errors"], "written_by_model": True}
        else:
            raise ManimError("empty_scene", "give the scene code or a description of what to draw")
        board = result.pop("storyboard_file", None)
        if board and project_id and Path(board).is_file():
            asset = engine.import_asset(store, project_id, Path(board), "image", original_name=f"{result['scene']} storyboard.png",
                                        recipe={"operation": "manim_check", "scene": result["scene"], "input_asset_ids": [],
                                                "code_sha256": hashlib.sha256(result["code"].encode("utf-8")).hexdigest()[:16]},
                                        tags=["manim", "storyboard"], source="rendered")
            result["storyboard_asset_id"] = asset["id"]
        return result
    finally:
        picture.unlink(missing_ok=True)


# ------------------------------------------------------------------ examples

EXAMPLES: list[dict[str, str]] = [
    {"id": "formula_euler", "kind": "2d", "title": "Formula: Euler's identity",
     "description": "A formula written on screen with a title above it.",
     "code": '''import manimgx as m


class Euler(m.Scene):
    def construct(self):
        title = m.Text("Euler's identity", font_size=48).to_edge(m.UP)
        formula = m.MathTex(r"e^{i\\pi} + 1 = 0", font_size=96)
        self.play(m.FadeIn(title), run_time=1)
        self.play(m.Write(formula), run_time=2)
        self.wait(1)
'''},
    {"id": "grafica_seno", "kind": "2d", "title": "Plot: a sine wave",
     "description": "Axes with a curve drawn over them and a label.",
     "code": '''import math

import manimgx as m


class SineWave(m.Scene):
    def construct(self):
        axes = m.Axes(x_range=[-4, 4, 1], y_range=[-1.5, 1.5, 1], x_length=10, y_length=4.5)
        curve = axes.plot(lambda x: math.sin(x), color=m.YELLOW)
        label = m.MathTex(r"y = \\sin x").to_corner(m.UL)
        self.play(m.Create(axes), run_time=1.5)
        self.play(m.Create(curve), m.Write(label), run_time=2.5)
        self.wait(1)
'''},
    {"id": "formas_transformacion", "kind": "2d", "title": "Shapes: a square becomes a circle",
     "description": "Basic shapes and a transformation, with a caption.",
     "code": '''import manimgx as m


class Shapes(m.Scene):
    def construct(self):
        square = m.Square(side_length=3, color=m.BLUE, fill_opacity=0.5)
        circle = m.Circle(radius=1.6, color=m.RED, fill_opacity=0.5)
        caption = m.Text("Square to circle", font_size=40).to_edge(m.DOWN)
        self.play(m.Create(square), m.FadeIn(caption), run_time=1.5)
        self.play(m.Transform(square, circle), run_time=2)
        self.wait(1)
'''},
    {"id": "cubo_3d", "kind": "3d", "title": "3D: a cube",
     "description": "A translucent cube drawn in a 3D scene seen from above and aside.",
     "code": '''import manimgx as m


class Hello3D(m.ThreeDScene):
    def construct(self):
        self.set_camera_orientation(phi=65 * m.DEGREES, theta=-45 * m.DEGREES)
        cube = m.Cube(side_length=3, fill_opacity=0.3)
        self.play(m.Create(cube), run_time=2)
        self.wait(0.5)
'''},
]

NOTES = [
    "Code starts with `import manimgx as m`; a Manim Community `from manim import *` is rewritten to manimgx automatically.",
    "A scene is a class deriving m.Scene (2D) or m.ThreeDScene (3D) with construct(self); it runs self.play(...) and self.wait(...).",
    "Scenes may import math, numpy, random and itertools; files, processes and the network are refused.",
    "manim_check validates without rendering a video and returns the timeline, layout problems (overlapping or clipped text) and a storyboard.",
    "A render lasts at most 240 s of video; quality draft = 1280x720@30, standard = 1920x1080@30, high = 1920x1080@60 (or width/height/fps).",
    "The rendered clip is a project video asset: use it in edit_plan clips like any other video.",
]


def examples() -> dict[str, Any]:
    return {"examples": EXAMPLES, "notes": NOTES}
