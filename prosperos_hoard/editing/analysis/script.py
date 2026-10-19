"""Script assembly: align a raw recording (read from a script or a teleprompter, with retakes and stumbles) to the script and
pick one take per segment.

Each script segment is aligned against the word-level transcript with a local alignment (Smith-Waterman over words,
fuzzy word equality). The best take is found, masked, and the search repeats to find the other takes. Per segment the
chosen take is the last complete one by default (people re-read a sentence until it comes out right) or the most complete
one."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Optional

from .speech import norm

MATCH, MISMATCH, GAP = 2.0, -1.0, -1.0


@dataclass
class Segment:
    index: int
    title: str
    text: str
    tokens: list[str]


def parse_script(text: str) -> list[Segment]:
    """Segments from a plan JSON ({segments: [{title, script}]}), Markdown (## headings; text before the first heading that is
    only metadata such as **Tema:** is ignored) or plain text (paragraphs)."""
    raw = (text or "").strip()
    pieces: list[tuple[str, str]] = []
    if raw.startswith("{") or raw.startswith("["):
        try:
            data = json.loads(raw)
            items = data.get("segments", []) if isinstance(data, dict) else data
            for i, item in enumerate(items):
                if isinstance(item, dict):
                    body = str(item.get("script") or item.get("text") or "").strip()
                    if body:
                        pieces.append((str(item.get("title") or f"Segmento {i + 1}"), body))
                elif isinstance(item, str) and item.strip():
                    pieces.append((f"Segmento {i + 1}", item.strip()))
        except ValueError:
            pieces = []
    if not pieces and re.search(r"(?m)^#{2,4}\s+\S", raw):
        parts = re.split(r"(?m)^#{2,4}\s+(.+)$", raw)
        for j in range(1, len(parts), 2):
            title = parts[j].strip()
            body = _clean_md(parts[j + 1] if j + 1 < len(parts) else "")
            if body:
                pieces.append((title, body))
    if not pieces:
        for i, para in enumerate(p for p in re.split(r"\n\s*\n", raw) if p.strip()):
            body = _clean_md(para)
            if body and not re.match(r"^\s*(#|\*\*[^*]+:\*\*)", para.strip()):
                pieces.append((f"Segmento {i + 1}", body))
    out = []
    for i, (title, body) in enumerate(pieces):
        tokens = [t for t in (norm(w) for w in body.split()) if t]
        if tokens:
            out.append(Segment(i, title, body, tokens))
    return out


def _clean_md(text: str) -> str:
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("---") or re.match(r"^\*\*[^*]{1,40}:\*\*", s) or s.startswith(">"):
            continue
        s = re.sub(r"[*_`]+", "", s)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        lines.append(s)
    return " ".join(lines).strip()


@lru_cache(maxsize=200_000)
def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4 or abs(len(a) - len(b)) > 2 or a[0] != b[0]:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def _align(seg: list[str], words: list[str], masked: list[bool]) -> Optional[tuple[float, int, int, int]]:
    """Best local alignment of seg inside words (ignoring masked words). Returns (score, start, end_exclusive, matches)."""
    n, m = len(seg), len(words)
    prev = [0.0] * (m + 1)
    prev_start = list(range(m + 1))
    prev_match = [0] * (m + 1)
    best = (0.0, 0, 0, 0)
    for i in range(1, n + 1):
        cur = [0.0] * (m + 1)
        cur_start = [0] * (m + 1)
        cur_match = [0] * (m + 1)
        token = seg[i - 1]
        for j in range(1, m + 1):
            if masked[j - 1]:
                cur[j], cur_start[j], cur_match[j] = 0.0, j, 0
                continue
            same = _same(token, words[j - 1])
            diag = prev[j - 1] + (MATCH if same else MISMATCH)
            up = prev[j] + GAP
            left = cur[j - 1] + GAP
            score = max(0.0, diag, up, left)
            if score == 0.0:
                cur[j], cur_start[j], cur_match[j] = 0.0, j, 0
            elif score == diag:
                cur[j], cur_start[j], cur_match[j] = score, prev_start[j - 1], prev_match[j - 1] + (1 if same else 0)
            elif score == up:
                cur[j], cur_start[j], cur_match[j] = score, prev_start[j], prev_match[j]
            else:
                cur[j], cur_start[j], cur_match[j] = score, cur_start[j - 1], cur_match[j - 1]
            if score > best[0]:
                best = (score, cur_start[j], j, cur_match[j])
        prev, prev_start, prev_match = cur, cur_start, cur_match
    return best if best[0] > 0 else None


def find_takes(seg: Segment, words: list[dict[str, Any]], tokens: list[str], *, min_coverage: float = 0.6, max_takes: int = 8) -> list[dict[str, Any]]:
    masked = [False] * len(tokens)
    takes: list[dict[str, Any]] = []
    for _ in range(max_takes):
        res = _align(seg.tokens, tokens, masked)
        if res is None:
            break
        score, a, b, matches = res
        coverage = matches / len(seg.tokens)
        if coverage < min_coverage or b <= a:
            break
        takes.append({"start_word": a, "end_word": b, "t0": words[a]["t0"], "t1": words[b - 1]["t1"], "coverage": round(coverage, 3),
                      "score": round(score, 1), "first": words[a]["id"], "last": words[b - 1]["id"]})
        for k in range(a, b):
            masked[k] = True
    takes.sort(key=lambda t: t["t0"])
    return takes


def assemble(segments: list[Segment], words: list[dict[str, Any]], *, take: str = "last", min_coverage: float = 0.6, pad_in: int = 120,
             pad_out: int = 220) -> dict[str, Any]:
    """Choose a take per segment and the source range to keep for it."""
    tokens = [norm(w["text"]) for w in words]
    chosen: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for seg in segments:
        takes = find_takes(seg, words, tokens, min_coverage=min_coverage)
        if not takes:
            missing.append({"segment": seg.index + 1, "title": seg.title, "text": seg.text[:120]})
            continue
        best_cov = max(t["coverage"] for t in takes)
        good = [t for t in takes if t["coverage"] >= best_cov - 0.1]
        pick = good[-1] if take == "last" else max(good, key=lambda t: (t["coverage"], t["t0"]))
        a, b = pick["start_word"], pick["end_word"]
        before = words[a - 1]["t1"] if a > 0 else 0
        after = words[b]["t0"] if b < len(words) else pick["t1"] + pad_out
        src_in = max(before, pick["t0"] - pad_in, 0)
        src_out = min(after, pick["t1"] + pad_out) if after > pick["t1"] else pick["t1"] + 60
        chosen.append({"segment": seg.index + 1, "title": seg.title, "src_in": int(src_in), "src_out": int(src_out), "coverage": pick["coverage"],
                       "takes": len(takes), "take_times": [[t["t0"], t["t1"], t["coverage"]] for t in takes]})
    return {"segments": chosen, "missing": missing, "total_segments": len(segments)}
