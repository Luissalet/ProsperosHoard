"""The settings Prospero's side of the Hoard family keeps: who tells the person that a production needs them.

`notify.via`: `auto` (the family hub, when it answers), `hub` (always ask it) or `off` (nobody is told). Prospero has no channel of
its own, so `off` means silence. `notify.language`: `es` or `en`, the language of the notices. Stored in `data/family.json`
(atomic writes); a missing or damaged file means the defaults. Pure logic: no FastAPI here.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .util import write_text_atomic

DEFAULTS: dict[str, str] = {"notify.via": "auto", "notify.language": "es"}
CHOICES: dict[str, tuple[str, ...]] = {"notify.via": ("auto", "hub", "off"), "notify.language": ("es", "en")}


class SettingsError(ValueError):
    """A setting that does not exist or a value it does not accept."""


class FamilySettings:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "family.json"
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def get(self, key: str) -> str:
        value = str(self._read().get(key) or "").strip().lower()
        return value if value in CHOICES.get(key, ()) else DEFAULTS[key]

    def all(self) -> dict[str, Any]:
        return {**{k: self.get(k) for k in DEFAULTS}, "choices": {k: list(v) for k, v in CHOICES.items()}}

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(patch) - set(DEFAULTS))
        if unknown:
            raise SettingsError(f"unknown settings: {', '.join(unknown)}; known: {', '.join(DEFAULTS)}")
        clean: dict[str, str] = {}
        for key, value in patch.items():
            value = str(value or "").strip().lower()
            if value not in CHOICES[key]:
                raise SettingsError(f"{key} must be one of {', '.join(CHOICES[key])}")
            clean[key] = value
        with self._lock:
            current = self._read()
            current.update(clean)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self.path, json.dumps(current, indent=2, ensure_ascii=False))
        return self.all()
