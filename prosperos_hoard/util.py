"""Small helpers shared across the app. The atomic file writes are Hoard Link's (``hoard_link.atomic``); the names stay here for
the modules that already import them."""

from __future__ import annotations

import datetime as _dt

from .hoard_link.atomic import replace_with_retry, write_json_atomic, write_text_atomic  # noqa: F401 - re-exported


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
