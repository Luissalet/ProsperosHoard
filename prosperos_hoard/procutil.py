"""Subprocess helpers for the modules that shell out (ffmpeg, ffprobe, trainers). Hoard Link's ``hoard_link.proc`` does the work:
no console window on Windows, UTF-8 text decoding with replacement, a closed stdin, and a timed-out command is killed with its whole
process tree. The names stay here because many modules import them; ``run`` keeps the old default of bytes unless ``text=True``.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - callers use procutil.subprocess.PIPE / STDOUT
from typing import Any

from .hoard_link import proc as _proc

no_window_kwargs = _proc.no_window_kwargs
popen = _proc.popen


def run(cmd: list[str], *, text: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    kwargs.pop("capture_output", None)   # the shared runner always captures stdout and stderr
    return _proc.run(cmd, text=text, **kwargs)
