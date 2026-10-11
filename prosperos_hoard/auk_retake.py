"""Word retakes in context with AuK (Tencent's speech editing model).

The plain retake (``voice_retake.retake_words``) synthesizes only the new words with a TTS voice and splices them in. AuK
instead edits the whole line from its own audio ("Replace 'diecisiete grados' with 'dieciocho grados'."), so the new
words come out in the speaker's own voice, pitch and pace, also for a recording Prospero did not make. Several seeds run;
each candidate is transcribed, the one whose words match the expected line wins, and only the stretch with the new words
is cut from it and spliced into the original: everything outside the cut keeps the original samples bit for bit.

AuK runs in its own environment (``auk_worker.py`` with its venv). Where it lives:
``PROSPERO_AUK_HOME`` (default ``<data>/engines/auk``) holds ``.venv`` (or ``venv``) and the ``AuK`` repository;
``PROSPERO_AUK_CKPT`` (default ``<home>/AuK-Flash/auk_flash.safetensors``, with ``config.yaml`` beside it) and
``PROSPERO_AUK_QWEN`` (default ``<home>/Qwen2.5-Omni-3B``); ``PROSPERO_AUK_PYTHON`` overrides the interpreter.
AuK-Flash runs 4 steps (what its model card asks; 32 steps make it garble); the base model runs 32.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import voice_retake as rt

SEEDS = (1, 2, 3)
VRAM_MB = 10_000          # measured peak 8.9 GiB with CPU offload on a 16 GB card
TIMEOUT_S = 900
MATCH_RATIO = 0.8


@dataclass
class AukSetup:
    python: Path
    repo: Path
    ckpt: Path
    qwen: Optional[Path]

    @property
    def flash(self) -> bool:
        return "flash" in self.ckpt.name.lower()

    @property
    def nfe(self) -> int:
        return 4 if self.flash else 32


def _python_in(home: Path) -> Optional[Path]:
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        p = home / rel
        if p.is_file():
            return p
    return None


def discover(data_dir: Path, env: Optional[dict[str, str]] = None) -> tuple[Optional[AukSetup], dict[str, Any]]:
    """(setup or None, status) from the environment variables and the default layout under ``data_dir``."""
    env = os.environ if env is None else env
    home = Path(env.get("PROSPERO_AUK_HOME") or (Path(data_dir) / "engines" / "auk"))
    python = Path(env["PROSPERO_AUK_PYTHON"]) if env.get("PROSPERO_AUK_PYTHON") else _python_in(home)
    repo = home / "AuK"
    ckpt = Path(env.get("PROSPERO_AUK_CKPT") or (home / "AuK-Flash" / "auk_flash.safetensors"))
    qwen = Path(env.get("PROSPERO_AUK_QWEN") or (home / "Qwen2.5-Omni-3B"))
    status: dict[str, Any] = {"engine": "auk", "home": str(home), "installed": False,
                              "variant": "AuK-Flash" if "flash" in ckpt.name.lower() else "AuK"}
    missing = []
    if python is None or not python.is_file():
        missing.append("its Python environment (.venv in PROSPERO_AUK_HOME or PROSPERO_AUK_PYTHON)")
    if not (repo / "src" / "auk").is_dir():
        missing.append("the AuK repository (AuK/ in PROSPERO_AUK_HOME)")
    if not ckpt.is_file() or not (ckpt.parent / "config.yaml").is_file():
        missing.append("the checkpoint with config.yaml beside it (PROSPERO_AUK_CKPT)")
    if not (qwen / "config.json").is_file():
        missing.append("Qwen2.5-Omni-3B (PROSPERO_AUK_QWEN)")
    if missing:
        status["reason"] = "missing " + "; ".join(missing)
        return None, status
    setup = AukSetup(python=python, repo=repo, ckpt=ckpt, qwen=qwen)
    status.update(installed=True, reason="ready", nfe=setup.nfe, vram_mb=VRAM_MB)
    return setup, status


# ---------------------------------------------------------------- text matching
_ES_UNITS = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce",
             "trece", "catorce", "quince", "dieciseis", "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno",
             "veintidos", "veintitres", "veinticuatro", "veinticinco", "veintiseis", "veintisiete", "veintiocho",
             "veintinueve"]
_ES_TENS = {3: "treinta", 4: "cuarenta", 5: "cincuenta", 6: "sesenta", 7: "setenta", 8: "ochenta", 9: "noventa"}
_ES_HUNDREDS = {1: "ciento", 2: "doscientos", 3: "trescientos", 4: "cuatrocientos", 5: "quinientos", 6: "seiscientos",
                7: "setecientos", 8: "ochocientos", 9: "novecientos"}
_EN_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
             "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty", 6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}


def _es_words(n: int) -> list[str]:
    if n < 30:
        return [_ES_UNITS[n]]
    if n < 100:
        t, u = divmod(n, 10)
        return [_ES_TENS[t]] + (["y", _ES_UNITS[u]] if u else [])
    if n < 1000:
        h, r = divmod(n, 100)
        if n == 100:
            return ["cien"]
        return [_ES_HUNDREDS[h]] + (_es_words(r) if r else [])
    th, r = divmod(n, 1000)
    return (["mil"] if th == 1 else _es_words(th) + ["mil"]) + (_es_words(r) if r else [])


def _en_words(n: int) -> list[str]:
    if n < 20:
        return [_EN_UNITS[n]]
    if n < 100:
        t, u = divmod(n, 10)
        return [_EN_TENS[t]] + ([_EN_UNITS[u]] if u else [])
    if n < 1000:
        h, r = divmod(n, 100)
        return [_EN_UNITS[h], "hundred"] + (_en_words(r) if r else [])
    th, r = divmod(n, 1000)
    return _en_words(th) + ["thousand"] + (_en_words(r) if r else [])


def tokens(text: str, language: Optional[str] = None) -> list[str]:
    """Lower case, no accents or punctuation, numbers in words (Spanish unless the language says English):
    the transcriber writes «18» where the line says «dieciocho»."""
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    out: list[str] = []
    english = (language or "").lower().startswith("en")
    for tok in re.findall(r"[a-z0-9]+", text):
        if tok.isdigit() and len(tok) <= 6:
            out.extend(_en_words(int(tok)) if english else _es_words(int(tok)))
        else:
            out.append(tok)
    return out


def _word_tokens(words: list[dict[str, Any]], language: Optional[str]) -> tuple[list[str], list[int]]:
    """Tokens of a word list and, for each token, the index of the word it came from."""
    toks: list[str] = []
    owner: list[int] = []
    for i, w in enumerate(words):
        for t in tokens(w.get("word", ""), language):
            toks.append(t)
            owner.append(i)
    return toks, owner


def find_span(words: list[dict[str, Any]], new_text: str, language: Optional[str] = None,
              before: str = "", after: str = "") -> Optional[tuple[int, int, float]]:
    """(first word, last word, score) of ``new_text`` inside a transcribed candidate, or None. An exact token match
    scores 1.0 (plus a little when the neighbours ``before``/``after`` sit right beside it); otherwise the closest
    window of the same length scores its similarity, kept only from ``MATCH_RATIO`` up."""
    toks, owner = _word_tokens(words, language)
    want = tokens(new_text, language)
    if not toks or not want:
        return None
    pre, post = tokens(before, language), tokens(after, language)
    best: Optional[tuple[int, int, float]] = None
    n = len(want)
    for size in sorted({n, max(1, n - 1), n + 1}):
        for s in range(0, len(toks) - size + 1):
            window = toks[s:s + size]
            ratio = 1.0 if window == want else difflib.SequenceMatcher(None, " ".join(window), " ".join(want)).ratio()
            if ratio < MATCH_RATIO:
                continue
            bonus = 0.0
            if pre and toks[max(0, s - len(pre)):s] == pre:
                bonus += 0.05
            if post and toks[s + size:s + size + len(post)] == post:
                bonus += 0.05
            score = ratio + bonus
            if best is None or score > best[2]:
                best = (owner[s], owner[s + size - 1], score)
    return best


# ---------------------------------------------------------------- instruction and length
def spoken(text: str, language: Optional[str] = None) -> str:
    """The text as it is said: figures written in words («17 grados» -> «diecisiete grados»), the rest untouched."""
    english = (language or "").lower().startswith("en")

    def say(m: re.Match) -> str:
        n = m.group(0)
        return " ".join(_en_words(int(n)) if english else _es_words(int(n))) if len(n) <= 6 else n
    return re.sub(r"\d+", say, str(text or ""))


def _spoken_len(text: str, language: Optional[str]) -> int:
    return len("".join(tokens(text, language)))


def plan(words: list[dict[str, Any]], first: int, last: int, new_text: str, duration_s: float,
         language: Optional[str] = None) -> dict[str, Any]:
    """The AuK instruction (the picked words with one neighbour on each side, which edits better than a lone word, with
    figures spelled out as they are said), the expected new line, the target length (the line's length scaled by the
    spoken text change) and how long the new words should last at the line's own pace."""
    def text(ws: list[dict[str, Any]]) -> str:
        return " ".join(str(w.get("word", "")).strip() for w in ws).strip()
    lo, hi = max(0, first - 1), min(len(words) - 1, last + 1)
    before, after = spoken(text(words[lo:first]), language), spoken(text(words[last + 1:hi + 1]), language)
    old_phrase = spoken(text(words[lo:hi + 1]), language)
    new_phrase = " ".join(p for p in (before, new_text.strip(), after) if p)
    ref_line = text(words)
    new_line = " ".join(p for p in (text(words[:first]), new_text.strip(), text(words[last + 1:])) if p)
    ratio = _spoken_len(new_line, language) / max(1, _spoken_len(ref_line, language))
    seconds = round(min(max(duration_s * ratio, duration_s * 0.6), duration_s * 1.8), 2)
    talk = sum(max(0.0, float(w["end_s"]) - float(w["start_s"])) for w in words)
    per_char = talk / max(1, _spoken_len(ref_line, language))
    expected_new_s = round(per_char * _spoken_len(new_text, language), 3)
    clean = lambda s: s.replace("'", "’")  # noqa: E731 - the template quotes with '
    return {"instruction": f"Replace '{clean(old_phrase)}' with '{clean(new_phrase)}'.", "before": before, "after": after,
            "old_phrase": old_phrase, "new_phrase": new_phrase, "expected_line": new_line, "gen_seconds": seconds,
            "expected_new_s": expected_new_s}


def piece_fits(seconds: float, expected_s: float) -> bool:
    """A stretch cut from a take can stand for the new words: not several words longer (the transcriber merged the
    new words with the rest of the line into one timing) nor a fragment of them."""
    if expected_s <= 0:
        return seconds > 0
    return 0.3 * expected_s <= seconds <= max(1.2, 2.2 * expected_s)


# ---------------------------------------------------------------- running AuK
def worker_script() -> Path:
    return Path(__file__).with_name("auk_worker.py")


def run_worker(setup: AukSetup, job: dict[str, Any], work_dir: Path, gpu: Optional[int] = None,
               timeout_s: float = TIMEOUT_S) -> dict[str, Any]:
    """Run ``auk_worker.py`` in AuK's environment, hidden (no console window), on ``gpu`` when given."""
    work_dir.mkdir(parents=True, exist_ok=True)
    job = {**job, "repo": str(setup.repo), "ckpt": str(setup.ckpt), "qwen": str(setup.qwen) if setup.qwen else None,
           "nfe": job.get("nfe") or setup.nfe, "out_dir": str(work_dir), "result": str(work_dir / "result.json")}
    (work_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}
    if gpu is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    with open(work_dir / "worker.log", "wb") as log:
        try:
            subprocess.run([str(setup.python), "-X", "utf8", str(worker_script()), str(work_dir / "job.json")],
                           cwd=str(setup.repo), env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout_s,
                           creationflags=flags, check=False)
        except subprocess.TimeoutExpired:
            raise rt.RetakeError("auk_timeout", f"AuK took longer than {int(timeout_s)} s") from None
    try:
        result = json.loads((work_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tail = (work_dir / "worker.log").read_text(encoding="utf-8", errors="replace")[-600:]
        raise rt.RetakeError("auk_failed", f"AuK stopped without a result: {tail}") from None
    if not result.get("ok"):
        raise rt.RetakeError("auk_failed", f"AuK failed: {result.get('error')}")
    return result


def _cut(data: bytes, t0: float, t1: float) -> bytes:
    x, rate = rt.decode_wav(data)
    return rt.encode_wav(x[int(round(t0 * rate)):int(round(t1 * rate))], rate)


def retake_with_auk(original: bytes, words: list[dict[str, Any]], first: int, last: int, new_text: str, *,
                    setup: AukSetup, transcribe: Callable[[Path], list[dict[str, Any]]], work_dir: Path,
                    language: Optional[str] = None, seeds: tuple[int, ...] = SEEDS, gpu: Optional[int] = None,
                    runner: Optional[Callable[..., dict[str, Any]]] = None) -> dict[str, Any]:
    """AuK edits the line, the best candidate's new words are spliced into the original. Same result shape as
    ``voice_retake.retake_words`` plus ``report.auk`` (instruction, every candidate's transcript, the chosen seed)."""
    if not new_text.strip():
        raise rt.RetakeError("empty_text", "give the replacement words")
    x, rate = rt.decode_wav(original)
    duration_s = len(x) / rate
    rt.word_cut(words, first, last, duration_s)                      # validates the range before any GPU work
    p = plan(words, first, last, new_text, duration_s, language)
    work_dir.mkdir(parents=True, exist_ok=True)
    source = work_dir / "source.wav"
    source.write_bytes(rt.encode_wav(x, rate))
    runner = runner or run_worker
    result = runner(setup, {"audio": str(source), "instruction": p["instruction"], "gen_seconds": p["gen_seconds"],
                            "seeds": list(seeds), "cpu_offload": True}, work_dir, gpu=gpu)
    tried: list[dict[str, Any]] = []
    best: Optional[dict[str, Any]] = None
    for cand in result.get("candidates") or []:
        path = Path(cand["file"])
        cwords = transcribe(path)
        heard = " ".join(str(w.get("word", "")).strip() for w in cwords).strip()
        span = find_span(cwords, new_text, language, p["before"], p["after"])
        line_ratio = difflib.SequenceMatcher(None, " ".join(tokens(heard, language)),
                                             " ".join(tokens(p["expected_line"], language))).ratio()
        entry: dict[str, Any] = {"seed": cand["seed"], "heard": heard, "matched": span is not None,
                                 "score": round((span[2] if span else 0.0) + 0.5 * line_ratio, 3),
                                 "line_similarity": round(line_ratio, 3)}
        cut: Optional[tuple[float, float]] = None
        if span is not None:
            cand_bytes = path.read_bytes()
            cx, crate = rt.decode_wav(cand_bytes)
            cut = rt.word_cut(cwords, span[0], span[1], len(cx) / crate)
            entry["piece_s"] = round(cut[1] - cut[0], 3)
            entry["fits"] = piece_fits(cut[1] - cut[0], p["expected_new_s"])
        tried.append(entry)
        if cut is not None and entry["fits"] and (best is None or entry["score"] > best["entry"]["score"]):
            best = {"entry": entry, "cut": cut, "path": path}
    if best is None:
        raise rt.RetakeError("auk_no_match", "AuK did not say the new words cleanly in any take: "
                             + " | ".join(f"seed {t['seed']}: {t['heard']}"
                                          + (f" (piece {t['piece_s']} s, expected ~{p['expected_new_s']} s)"
                                             if t.get("matched") and not t.get("fits") else "") for t in tried))
    c0, c1 = best["cut"]
    piece = _cut(best["path"].read_bytes(), c0, c1)
    out = rt.retake_words(original, words, first, last, new_text, lambda _text: piece, duration_s=duration_s)
    out["report"]["auk"] = {"instruction": p["instruction"], "variant": "AuK-Flash" if setup.flash else "AuK",
                            "nfe": setup.nfe, "gen_seconds": p["gen_seconds"], "expected_new_s": p["expected_new_s"],
                            "chosen_seed": best["entry"]["seed"],
                            "candidates": tried, "piece_from_take_s": [round(c0, 3), round(c1, 3)],
                            "load_s": result.get("load_s"), "peak_vram_gib": result.get("peak_vram_gib"), "gpu": gpu}
    return out
