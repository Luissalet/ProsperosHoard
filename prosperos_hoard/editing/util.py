"""Small helpers shared by every module."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def new_id(prefix: str, n: int = 8) -> str:
    """Short, readable, stable id such as ``clp_7f3a9k2m`` (agents refer to things by these)."""
    return f"{prefix}_" + "".join(secrets.choice(_ALPHABET) for _ in range(n))


def clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def slug(text: str, limit: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:limit].strip("-") or "video")


def safe_filename(text: str, limit: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text or "").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    return text[:limit].strip() or "video"


def fingerprint(path: Path) -> str:
    """Fast content fingerprint: size + first and last MiB. Identifies a file across renames without reading gigabytes."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as fh:
        h.update(fh.read(1 << 20))
        if size > (2 << 20):
            fh.seek(-(1 << 20), os.SEEK_END)
            h.update(fh.read(1 << 20))
    return h.hexdigest()[:32]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ms_to_tc(ms: int | float, fps: float | None = None) -> str:
    """12:03.250 (or 1:02:03.250); with fps, the frame number replaces milliseconds (hh:mm:ss:ff)."""
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    if fps:
        ff = int(frac * fps / 1000)
        return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"
    return (f"{h}:{m:02d}:{s:02d}.{frac:03d}" if h else f"{m}:{s:02d}.{frac:03d}")


_TC = re.compile(r"^\s*(?:(\d+):)?(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*(ms|s)?\s*$")


def parse_time(value: Any) -> int:
    """Seconds (number), '1:02.5', '01:02:03', '1500ms', '2.5s' -> milliseconds."""
    if isinstance(value, bool):
        raise ValueError("Not a time.")
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1000))
    m = _TC.match(str(value))
    if not m:
        raise ValueError(f"Not a time: {value!r}")
    a, b, c, unit = m.groups()
    secs = float(c.replace(",", "."))
    if unit == "ms":
        if a or b:
            raise ValueError(f"Not a time: {value!r}")
        return int(round(secs))
    parts = [int(x) for x in (a, b) if x is not None]
    if len(parts) == 2:
        secs += parts[0] * 3600 + parts[1] * 60
    elif len(parts) == 1:
        secs += parts[0] * 60
    return int(round(secs * 1000))


def now() -> float:
    return time.time()


def atomic_write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{secrets.token_hex(3)}")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)
