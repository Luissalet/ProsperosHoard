"""Moments that stand on their own, found by reading the transcript: sentences with timestamps, chunks small enough for a
local model, the compact line prompt and its tolerant parser. The model's answer is one short line per moment, the way the
script alignment asks for ranges, so a small model can follow it and a wrong line costs one moment, not the whole answer."""

from __future__ import annotations

import re
from typing import Any, Optional

from ..util import ms_to_tc

KINDS = ("hook", "punchline", "thought", "story", "tip")
PAUSE_MS = 700          # a pause this long ends a sentence when the transcript has no punctuation
MAX_SENTENCE_WORDS = 28
CHUNK_CHARS = 5200      # about 1.3k tokens of transcript per call: fits small local models with room for the answer
MAX_CHUNKS = 12

SYSTEM = (
    "You pick the moments of a recording that stand on their own as a short clip: a hook that grabs attention, a punchline, a complete "
    "thought or story with its payoff, a clear tip. Skip greetings, filler, transitions and anything that needs what came before to make "
    "sense. A moment is a run of consecutive sentences, about 6 to 60 seconds long. Answer with one line per moment and nothing else, "
    "exactly like:\n12-15|5|punchline|short reason in the language of the talk\n"
    "(first sentence number - last sentence number | strength 1 to 5 | hook, punchline, thought, story or tip | reason). "
    "At most {n} lines, best first. Answer '-' alone when nothing here stands on its own."
)


def sentences_of(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """[{i, t0, t1, text}] from the transcript's own segments, or built from the words (end of sentence at . ? ! or a pause)."""
    out: list[dict[str, Any]] = []
    segs = [s for s in transcript.get("segments") or [] if str(s.get("text", "")).strip() and "t0" in s and "t1" in s]
    if segs:
        for s in segs:
            out.append({"i": len(out), "t0": int(s["t0"]), "t1": int(s["t1"]), "text": str(s["text"]).strip()})
        return out
    cur: list[dict[str, Any]] = []

    def flush() -> None:
        if cur:
            out.append({"i": len(out), "t0": int(cur[0]["t0"]), "t1": int(cur[-1]["t1"]), "text": " ".join(w["text"] for w in cur)})
            cur.clear()

    for w in transcript.get("words") or []:
        if cur and (w["t0"] - cur[-1]["t1"] > PAUSE_MS or len(cur) >= MAX_SENTENCE_WORDS):
            flush()
        cur.append(w)
        if w["text"][-1:] in ".?!…":
            flush()
    flush()
    return out


def chunks_of(sentences: list[dict[str, Any]], limit: int = CHUNK_CHARS, overlap: int = 2) -> list[list[dict[str, Any]]]:
    """Consecutive sentences grouped under ``limit`` characters; each chunk repeats the last ``overlap`` sentences of the one
    before so a moment on the border is still seen whole."""
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    size = 0
    for s in sentences:
        line = len(s["text"]) + 14
        if cur and size + line > limit:
            chunks.append(cur)
            cur = cur[-overlap:] if overlap else []
            size = sum(len(x["text"]) + 14 for x in cur)
        cur.append(s)
        size += line
    if cur and (not chunks or len(cur) > overlap):
        chunks.append(cur)
    return chunks


def prompt_for(chunk: list[dict[str, Any]], n: int = 3) -> list[dict[str, str]]:
    lines = [f"{s['i']}\t[{ms_to_tc(s['t0'])}]\t{s['text']}" for s in chunk]
    return [{"role": "system", "content": SYSTEM.format(n=n)}, {"role": "user", "content": "RECORDING (sentence number, time, text)\n" + "\n".join(lines)}]


_LINE = re.compile(r"^\W*(\d+)\s*(?:-|–|—|to|a)\s*(\d+)\s*[|;,:]\s*([1-5])\s*[|;,:]?\s*([a-zA-Z]*)\s*[|;,:]?\s*(.*)$")
_SINGLE = re.compile(r"^\W*(\d+)\s*[|;,:]\s*([1-5])\s*[|;,:]?\s*([a-zA-Z]*)\s*[|;,:]?\s*(.*)$")


def parse_answer(text: str, valid: Optional[set[int]] = None) -> list[dict[str, Any]]:
    """[{first, last, strength, kind, why}] from the model's lines; lines that do not parse (or point outside ``valid``) are
    dropped. Raises ValueError when the answer has text but not a single usable line (so the caller can say the model did not
    follow the format) and returns [] for the explicit '-' answer."""
    found: list[dict[str, Any]] = []
    saw_text = False
    for raw in (text or "").splitlines():
        line = raw.strip().strip("`*").strip()
        if not line or line in ("-", "—", "none", "ninguno"):
            continue
        saw_text = True
        m = _LINE.match(line)
        if m:
            a, b, strength, kind, why = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).lower(), m.group(5).strip()
        else:
            m = _SINGLE.match(line)
            if not m:
                continue
            a = b = int(m.group(1))
            strength, kind, why = int(m.group(2)), m.group(3).lower(), m.group(4).strip()
        if a > b:
            a, b = b, a
        if valid is not None and not (a in valid and b in valid):
            continue
        found.append({"first": a, "last": b, "strength": strength, "kind": kind if kind in KINDS else "thought", "why": why[:160]})
    if not found and saw_text:
        raise ValueError("no 'first-last|strength|kind|reason' lines in the answer")
    return found


def moment_window(sentences: list[dict[str, Any]], first: int, last: int, *, min_ms: int = 8000, max_ms: int = 60000) -> tuple[int, int]:
    """Start and end (ms) of sentences first..last, widened with the following sentences when it is too short to stand alone and
    cut back sentence by sentence when it runs too long."""
    by_i = {s["i"]: s for s in sentences}
    last = max(first, last)
    while by_i[last]["t1"] - by_i[first]["t0"] > max_ms and last > first:
        last -= 1
    while by_i[last]["t1"] - by_i[first]["t0"] < min_ms and last + 1 in by_i and by_i[last + 1]["t1"] - by_i[first]["t0"] <= max_ms:
        last += 1
    return max(0, by_i[first]["t0"] - 150), by_i[last]["t1"] + 250
