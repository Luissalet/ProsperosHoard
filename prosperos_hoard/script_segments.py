"""Import and edit text-to-shot links without guessing that untimed text is aligned."""
from __future__ import annotations

import math
import re
from typing import Any
from .hoard_link.media import subs

STAMP = r"\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}"


def seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return round(result, 3)


def parse_text(text: str) -> list[dict[str, Any]]:
    """TXT paragraphs/lines, LRC, SRT or VTT. Explicit ends survive import."""
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("text must contain at most 20000 characters")
    rows: list[dict[str, Any]] = []
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if "-->" in text:
        cues = subs.parse_vtt(text) if text.startswith("WEBVTT") else subs.parse_srt(text)
        if any(cue.start_s is None or cue.end_s is None for cue in cues):
            raise ValueError("invalid subtitle timestamp")
        rows = [{"text": cue.text, "start_s": cue.start_s, "end_s": cue.end_s} for cue in cues]
        if not rows:
            raise ValueError("no subtitle cues found")
    elif re.search(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", text):
        cues = subs.parse_lrc(text, last_s=0)
        rows = [{"text": cue.text, "start_s": cue.start_s,
                 "end_s": cue.end_s if cue.end_s is not None and cue.end_s > cue.start_s else None} for cue in cues]
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line == "WEBVTT" or re.fullmatch(r"\[[^\]]+\]", line):
                continue
            if not line.startswith(("[ar:", "[ti:", "[al:", "[by:", "[offset:")):
                rows.append({"text": line, "start_s": None, "end_s": None})
    if len(rows) > 200:
        raise ValueError("at most 200 text segments")
    return [dict(row, id=f"seg_{i + 1}", shot_key=None) for i, row in enumerate(rows)]


def validate(rows: Any, shot_keys: set[str]) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not 0 <= len(rows) <= 200:
        raise ValueError("segments must be a list of at most 200 entries")
    result, seen = [], set()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"segment {i + 1} must be an object")
        key = str(row.get("id") or f"seg_{i + 1}")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key) or key in seen:
            raise ValueError("segment ids must be unique short identifiers")
        seen.add(key)
        body = str(row.get("text") or "").strip()
        if not body or len(body) > 4000:
            raise ValueError(f"segment {key} needs text (at most 4000 characters)")
        start, end = row.get("start_s"), row.get("end_s")
        for value in (start, end):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError(f"segment {key} has an invalid time")
        if end is not None and (start is None or end <= start):
            raise ValueError(f"segment {key}: end must follow start")
        shot = row.get("shot_key") or None
        if shot is not None and shot not in shot_keys:
            raise ValueError(f"segment {key} refers to an unknown shot")
        result.append({"id": key, "text": body, "start_s": start, "end_s": end, "shot_key": shot})
    return result
