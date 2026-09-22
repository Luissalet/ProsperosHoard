"""Subprocess helpers shared by every module that shells out (ffmpeg,
ffprobe, nvidia-smi through Hoard Link does its own).

On Windows a console child started from a windowless parent (the app is
launched by Faustus or a .cmd shortcut) flashes a console window for every
call unless `CREATE_NO_WINDOW` is passed. Text output is always decoded as
UTF-8 with replacement so a non-ASCII file name in an ffmpeg error message
can never raise `UnicodeDecodeError` under a cp1252 locale.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def no_window_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def run(cmd: list[str], *, text: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    if "stdout" not in kwargs and "stderr" not in kwargs:
        kwargs.setdefault("capture_output", True)
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    if text:
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **no_window_kwargs(), **kwargs)


def popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if kwargs.get("text") or kwargs.get("universal_newlines"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.Popen(cmd, **no_window_kwargs(), **kwargs)
