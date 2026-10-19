"""Translated subtitles: the caption cues of a project translated with the local model, one answer per cue.

The model never sees times. It gets the cues as numbered lines (``12|text``) and answers the same numbers; the times
of every translated cue are copied from the original cue, so a translation can never drift out of sync. A model that
skips, merges or renumbers lines is repaired: the missing or suspicious cues are asked again (smaller batches, then
one by one) and, as a last resort, keep their original text and are reported.

A translation is stored per project and language together with a fingerprint of the cues it answers (their text and
timing). When the transcript, the cuts or the caption grouping change, the fingerprint changes and the translation is
stale: exports refuse it with a clear message and translating again reuses every cue whose text did not change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from typing import TYPE_CHECKING, Any, Callable, Optional

from . import generate
from . import media as media_store
from . import projects as project_store
from .errors import LumiereError, ModelUnavailable, NotFound
from .render import ass
from .util import clip, dumps

if TYPE_CHECKING:
    from .jobs import JobCtx
    from .services import Services
    from .timeline import Project

log = logging.getLogger("lumiere.subtitles")

# code -> (name for the model, names people use for it)
LANGUAGES: dict[str, tuple[str, tuple[str, ...]]] = {
    "en": ("English", ("inglés", "ingles", "english")), "es": ("Spanish", ("español", "castellano", "spanish")),
    "fr": ("French", ("francés", "frances", "french", "français")), "de": ("German", ("alemán", "aleman", "german", "deutsch")),
    "it": ("Italian", ("italiano", "italian")), "pt": ("Portuguese", ("portugués", "portugues", "portuguese")),
    "ca": ("Catalan", ("catalán", "catala", "catalan")), "gl": ("Galician", ("gallego", "galician")),
    "eu": ("Basque", ("euskera", "vasco", "basque")), "nl": ("Dutch", ("neerlandés", "holandés", "dutch")),
    "sv": ("Swedish", ("sueco", "swedish")), "da": ("Danish", ("danés", "danish")), "no": ("Norwegian", ("noruego", "norwegian")),
    "fi": ("Finnish", ("finés", "finnish")), "pl": ("Polish", ("polaco", "polish")), "cs": ("Czech", ("checo", "czech")),
    "ro": ("Romanian", ("rumano", "romanian")), "hu": ("Hungarian", ("húngaro", "hungarian")), "el": ("Greek", ("griego", "greek")),
    "tr": ("Turkish", ("turco", "turkish")), "ru": ("Russian", ("ruso", "russian")), "uk": ("Ukrainian", ("ucraniano", "ukrainian")),
    "ar": ("Arabic", ("árabe", "arabe", "arabic")), "he": ("Hebrew", ("hebreo", "hebrew")), "hi": ("Hindi", ("hindi",)),
    "zh": ("Chinese", ("chino", "chinese", "mandarín", "mandarin")), "ja": ("Japanese", ("japonés", "japones", "japanese")),
    "ko": ("Korean", ("coreano", "korean")), "id": ("Indonesian", ("indonesio", "indonesian")), "vi": ("Vietnamese", ("vietnamita", "vietnamese")),
    "th": ("Thai", ("tailandés", "thai")),
}
BATCH_CUES = 28
BATCH_CHARS = 2400
ROUNDS = 3
LINE = re.compile(r"^\s*(?:[-*•]\s*)?(\d+)\s*[|:.)\]]\s?(.*)$")


def resolve_language(value: str) -> tuple[str, str]:
    """(code, name for the model) from a code (en), an English or a Spanish name; any other text is used as the name."""
    raw = (value or "").strip()
    if not raw:
        raise LumiereError("Say which language to translate the subtitles into (en, fr, 'inglés'...).", code="language_required")
    folded = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode().lower().strip()
    short = folded.split("-")[0].split("_")[0]
    for code, (name, aliases) in LANGUAGES.items():
        known = {code, name.lower(), *(unicodedata.normalize("NFKD", a).encode("ascii", "ignore").decode().lower() for a in aliases)}
        if folded in known or (len(folded) <= 5 and short == code):
            return code, name
    key = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:24]
    if not key:
        raise LumiereError(f"{raw!r} is not a language name.", code="language_unknown")
    return key, raw[:40]


def language_name(code: str) -> str:
    return LANGUAGES[code][0] if code in LANGUAGES else code


# ---------------------------------------------------------------- source cues

def words_for(svc: "Services") -> Callable[[str], Optional[list[dict[str, Any]]]]:
    return lambda mid: (media_store.get_analysis(svc, mid, "transcript") or {}).get("words")


def source_cues(svc: "Services", project: "Project") -> list[tuple[int, int, str]]:
    """The caption cues of the project (timeline ms, text as heard)."""
    return ass.raw_cues(project, words_for(svc))


def source_language(svc: "Services", project: "Project") -> str:
    tracks = project.captions.tracks or ([project.main_track().id] if project.main_track() else [])
    for t in project.tracks:
        if t.id in tracks:
            for c in t.clips:
                if c.media:
                    lang = (media_store.get_analysis(svc, c.media, "transcript") or {}).get("language")
                    if lang:
                        return str(lang)
    return ""


def signature(cues: list[tuple[int, int, str]]) -> str:
    return hashlib.sha1(dumps([[a, b, t] for a, b, t in cues]).encode("utf-8")).hexdigest()[:20]


def _terms_hash(glossary: dict[str, str], keep: list[str]) -> str:
    return hashlib.sha1(dumps([sorted(glossary.items()), sorted(keep)]).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- storage

def get_record(svc: "Services", project_id: str, code: str) -> Optional[dict[str, Any]]:
    row = svc.db.one("SELECT data, signature FROM translations WHERE project_id = ? AND language = ?", (project_id, code))
    if row is None:
        return None
    rec = json.loads(row["data"])
    rec["signature"] = row["signature"]
    return rec


def _put_record(svc: "Services", project_id: str, code: str, rec: dict[str, Any]) -> None:
    svc.db.execute("INSERT INTO translations(project_id, language, signature, data, updated_ts) VALUES (?, ?, ?, ?, ?) "
                   "ON CONFLICT(project_id, language) DO UPDATE SET signature = excluded.signature, data = excluded.data, "
                   "updated_ts = excluded.updated_ts", (project_id, code, rec["signature"], dumps(rec), time.time()))


def _summary(rec: dict[str, Any], fresh: bool) -> dict[str, Any]:
    return {"language": rec["language"], "name": rec.get("name"), "cues": len(rec["cues"]), "fresh": fresh,
            "stale": not fresh, "model": rec.get("model"), "source_language": rec.get("source_language"),
            "fallback": [c["i"] + 1 for c in rec["cues"] if c.get("fallback")], "edited": [c["i"] + 1 for c in rec["cues"] if c.get("edited")],
            "glossary": rec.get("glossary") or {}, "keep": rec.get("keep") or [], "updated_ts": rec.get("updated_ts")}


def status(svc: "Services", project_id: str) -> dict[str, Any]:
    """Which languages are translated for the project and whether each still matches the current captions."""
    p = project_store.doc(svc, project_id)
    cues = source_cues(svc, p)
    sig = signature(cues)
    items = []
    for row in svc.db.query("SELECT language FROM translations WHERE project_id = ? ORDER BY language", (project_id,)):
        rec = get_record(svc, project_id, row["language"])
        if rec:
            items.append(_summary(rec, rec["signature"] == sig))
    return {"project": project_id, "cues": len(cues), "source_language": source_language(svc, p), "translations": items,
            "languages": {c: n for c, (n, _) in LANGUAGES.items()}}


def show(svc: "Services", project_id: str, language: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
    code, _ = resolve_language(language)
    rec = get_record(svc, project_id, code)
    if rec is None:
        raise NotFound(f"No {language} translation for this project yet: translate it first.")
    p = project_store.doc(svc, project_id)
    fresh = rec["signature"] == signature(source_cues(svc, p))
    cues = [{"n": c["i"] + 1, "start_ms": c["start"], "end_ms": c["end"], "source": c["source"], "text": c["text"],
             **({"fallback": True} if c.get("fallback") else {}), **({"edited": True} if c.get("edited") else {})}
            for c in rec["cues"][offset: offset + limit]]
    return {**_summary(rec, fresh), "offset": offset, "total": len(rec["cues"]), "items": cues}


def fix(svc: "Services", project_id: str, language: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Correct translated cues by number ([{n, text}]); the timing stays the original one."""
    code, _ = resolve_language(language)
    rec = get_record(svc, project_id, code)
    if rec is None:
        raise NotFound(f"No {language} translation for this project yet: translate it first.")
    by_n = {c["i"] + 1: c for c in rec["cues"]}
    changed = 0
    for ch in changes:
        try:
            n = int(ch.get("n"))
        except (TypeError, ValueError) as error:
            raise LumiereError("Each change needs the cue number n and the new text.") from error
        cue = by_n.get(n)
        text = " ".join(str(ch.get("text", "")).split())
        if cue is None:
            raise NotFound(f"No cue {n} in this translation (1..{len(by_n)}).")
        if not text:
            raise LumiereError(f"Cue {n} cannot be empty.")
        if cue["text"] != text:
            cue["text"], cue["edited"] = text[:400], True
            cue.pop("fallback", None)
            changed += 1
    rec["updated_ts"] = time.time()
    _put_record(svc, project_id, code, rec)
    return {"project": project_id, "language": code, "changed": changed}


def delete(svc: "Services", project_id: str, language: str) -> dict[str, Any]:
    code, _ = resolve_language(language)
    svc.db.execute("DELETE FROM translations WHERE project_id = ? AND language = ?", (project_id, code))
    return {"project": project_id, "language": code, "deleted": True}


# ---------------------------------------------------------------- the model

def _system(src_name: str, dst_name: str, n: int, glossary: dict[str, str], keep: list[str]) -> str:
    rules = [
        f"You are a professional subtitle translator. Translate subtitle lines from {src_name or 'the original language'} into {dst_name}.",
        "Input: numbered lines, one subtitle per line, written N|text. Output: the same numbers in the same order, one line per input line, "
        "written N|translation.",
        f"Rules: answer exactly {n} lines, one for each input line. Never merge, split, skip, add or renumber lines, even when a sentence "
        "continues in the next line: translate each line on its own so it still reads naturally as a subtitle.",
        "Keep the meaning, tone and register; keep numbers and proper names; keep every translation about as short as the original.",
        "Answer only with the lines: no comments, no quotes around them, no code block.",
    ]
    if keep:
        rules.append("Do not translate these names or terms, copy them exactly as written: " + "; ".join(keep) + ".")
    if glossary:
        rules.append("Use these fixed translations: " + "; ".join(f"{a} => {b}" for a, b in glossary.items()) + ".")
    return "\n".join(rules)


def parse_numbered(text: str, wanted: set[int]) -> dict[int, str]:
    """{number: translation} from an answer in N|text lines. Numbers that were not asked for are ignored, the first answer for
    a number wins, and a line without number right under a numbered one (no blank line, no code fence between) is the rest of a
    translation the model wrapped; comments before, after or apart from the lines are dropped."""
    out: dict[int, str] = {}
    last: Optional[int] = None
    blank = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            blank = True
            continue
        if line.startswith("```"):
            last, blank = None, True
            continue
        m = LINE.match(line)
        if m is None:
            if last is not None and not blank:
                out[last] = (out[last] + " " + line).strip()
            continue
        blank = False
        n = int(m.group(1))
        if n not in wanted or n in out:
            last = None
            continue
        out[n] = m.group(2).strip()
        last = n
    return {n: t for n, t in out.items() if t}


def _suspect(batch: dict[int, str], got: dict[int, str]) -> set[int]:
    """Numbers whose answer looks like two cues glued together (much longer than the original, next number missing)."""
    bad: set[int] = set()
    for n, text in got.items():
        src = batch[n]
        longer = len(text) > 2.6 * len(src) + 30
        merged = (n + 1) in batch and (n + 1) not in got and len(text) > len(src) + 0.5 * len(batch[n + 1]) + 8
        if longer or merged:
            bad.add(n)
    return bad


def _ask(svc: "Services", system: str, lines: dict[int, str], context: list[str], strict: bool, seen: Optional[list[str]] = None) -> str:
    user = []
    if context:
        user.append("Earlier lines, only for context (do not translate or output them):\n" + "\n".join(f"- {c}" for c in context))
    if strict:
        user.append("Your previous answer skipped or merged lines. Answer again with one line for every number below.")
    user.append("\n".join(f"{n}|{text}" for n, text in lines.items()))
    chars = sum(len(t) for t in lines.values())
    text, model = generate.chat_text(svc, [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user)}],
                                     max_tokens=min(8000, 400 + 3 * chars + 12 * len(lines)), effort="off")
    if seen is not None and model and model not in seen:
        seen.append(model)
    return text


def translate_batch(svc: "Services", src_name: str, dst_name: str, batch: dict[int, str], context: list[str], glossary: dict[str, str],
                    keep: list[str], check: Callable[[], None] = lambda: None, seen: Optional[list[str]] = None) -> tuple[dict[int, str], list[int]]:
    """Translate {number: text}; returns ({number: translation}, numbers that could not be translated). Repairs the answer:
    missing or glued cues are asked again in rounds, the rest one by one."""
    done: dict[int, str] = {}
    todo = dict(batch)
    for round_no in range(ROUNDS):
        if not todo:
            break
        check()
        got = parse_numbered(_ask(svc, _system(src_name, dst_name, len(todo), glossary, keep), todo, context if round_no == 0 else [], round_no > 0, seen),
                             set(todo))
        bad = _suspect(todo, got)
        for n, text in got.items():
            if n not in bad:
                done[n] = text
        todo = {n: t for n, t in todo.items() if n not in done}
        if round_no == 0 and len(todo) > 1 and not got:
            break  # not a single usable line: go straight to one by one
    failed: list[int] = []
    for n, text in todo.items():
        check()
        one = parse_numbered(_ask(svc, _system(src_name, dst_name, 1, glossary, keep), {n: text}, [], True, seen), {n})
        if n in one and len(one[n]) <= 2.6 * len(text) + 30:
            done[n] = one[n]
        else:
            failed.append(n)
    return done, failed


def _batches(cues: list[tuple[int, int, str]], todo: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    cur: list[int] = []
    chars = 0
    for i in todo:
        size = len(cues[i][2])
        if cur and (len(cur) >= BATCH_CUES or chars + size > BATCH_CHARS):
            out.append(cur)
            cur, chars = [], 0
        cur.append(i)
        chars += size
    if cur:
        out.append(cur)
    return out


def _clean_terms(glossary: Optional[dict[str, str]], keep: Optional[list[str]]) -> tuple[dict[str, str], list[str]]:
    g = {str(a).strip()[:60]: str(b).strip()[:60] for a, b in (glossary or {}).items() if str(a).strip() and str(b).strip()}
    k = []
    for item in keep or []:
        item = str(item).strip()[:60]
        if item and item not in k:
            k.append(item)
    return dict(list(g.items())[:80]), k[:80]


def translate(svc: "Services", project_id: str, language: str, *, glossary: Optional[dict[str, str]] = None, keep: Optional[list[str]] = None,
              force: bool = False, progress: Callable[[float, str], None] = lambda p, d: None, check: Callable[[], None] = lambda: None) -> dict[str, Any]:
    """Translate the project's caption cues into ``language`` and store the result. Needs the local model; says so when it is missing."""
    code, name = resolve_language(language)
    p = project_store.doc(svc, project_id)
    cues = source_cues(svc, p)
    if not cues:
        raise LumiereError("There are no captions to translate: the project has no transcript on its captioned tracks. "
                           "Transcribe the media first (media_analyze: transcript).", code="no_transcript")
    src_code = source_language(svc, p)
    if src_code and src_code.split("-")[0] == code and not force:
        raise LumiereError(f"The speech is already in {name} (transcript language {src_code}); pass force=true to translate anyway.", code="same_language")
    sig = signature(cues)
    g, k = _clean_terms(glossary, keep)
    terms = _terms_hash(g, k)
    old = get_record(svc, project_id, code)
    if old and old["signature"] == sig and old.get("terms") == terms and not force:
        return {**_summary(old, True), "cached": True, "translated": 0, "reused": len(cues)}
    reuse: dict[str, str] = {}
    if old and old.get("terms") == terms and not force:
        reuse = {c["source"]: c["text"] for c in old["cues"] if not c.get("fallback")}
    texts: dict[int, str] = {}
    for i, (_, _, text) in enumerate(cues):
        if text in reuse:
            texts[i] = reuse[text]
    todo = [i for i in range(len(cues)) if i not in texts]
    fallback: set[int] = set()
    seen: list[str] = []
    src_name = language_name(src_code.split("-")[0]) if src_code else ""
    batches = _batches(cues, todo)
    for b_no, batch in enumerate(batches):
        check()
        progress(b_no / max(1, len(batches)), f"traduciendo {b_no + 1}/{len(batches)}")
        first = batch[0]
        context = [cues[j][2] for j in range(max(0, first - 2), first)]
        try:
            done, failed = translate_batch(svc, src_name, name, {i + 1: cues[i][2] for i in batch}, context, g, k, check, seen)
        except ModelUnavailable as error:
            raise ModelUnavailable("No local model is available to translate the subtitles. Start the model server that Hoard Link points at "
                                   f"(or choose a model in the settings) and try again. ({clip(str(error), 160)})", code="model_unavailable") from error
        for n, text in done.items():
            texts[n - 1] = text
        fallback.update(n - 1 for n in failed)
    model_name = seen[-1] if seen else (old.get("model") if old else None)
    rec = {"language": code, "name": name, "source_language": src_code, "signature": sig, "terms": terms, "glossary": g, "keep": k,
           "model": model_name, "updated_ts": time.time(),
           "cues": [{"i": i, "start": a, "end": b, "source": t, "text": texts.get(i, t), **({"fallback": True} if i in fallback else {})}
                    for i, (a, b, t) in enumerate(cues)]}
    # a stale answer the person corrected by hand survives when the cue is the same
    if old:
        hand = {c["source"]: c["text"] for c in old["cues"] if c.get("edited")}
        for c in rec["cues"]:
            if c["source"] in hand and c["i"] not in fallback:
                c["text"], c["edited"] = hand[c["source"]], True
    _put_record(svc, project_id, code, rec)
    progress(1.0, "listo")
    out = {**_summary(rec, True), "cached": False, "translated": len(todo) - len(fallback), "reused": len(cues) - len(todo)}
    if fallback:
        out["warning"] = (f"{len(fallback)} cue(s) could not be translated and keep the original text: "
                          + ", ".join(str(i + 1) for i in sorted(fallback)[:20]))
    svc.emit("lumiere.subtitles.translated", {"project": project_id, "language": code, "cues": len(cues), "fallback": len(fallback)})
    return out


def translate_job(svc: "Services", ctx: "JobCtx") -> dict[str, Any]:
    a = ctx.params
    return translate(svc, a["project"], a["language"], glossary=a.get("glossary"), keep=a.get("keep"), force=bool(a.get("force")),
                     progress=lambda p, d: ctx.progress(p, d), check=ctx.check)


def start_translation(svc: "Services", project_id: str, language: str, *, glossary: Optional[dict[str, str]] = None, keep: Optional[list[str]] = None,
                      force: bool = False) -> dict[str, Any]:
    """Queue the translation as a background job (the model can take a while on a long video)."""
    code, name = resolve_language(language)
    p = project_store.doc(svc, project_id)
    if not source_cues(svc, p):
        raise LumiereError("There are no captions to translate: the project has no transcript on its captioned tracks. "
                           "Transcribe the media first (media_analyze: transcript).", code="no_transcript")
    params: dict[str, Any] = {"project": project_id, "language": code, "force": force}
    g, k = _clean_terms(glossary, keep)
    if g:
        params["glossary"] = g
    if k:
        params["keep"] = k
    return svc.jobs.submit("translate_subtitles", params, label=f"Traducir subtítulos · {name}", project_id=project_id, dedupe=True)


# ---------------------------------------------------------------- reading a translation back

def translated_cues(svc: "Services", project_id: str, language: str, *, dual: bool = False, p: Optional["Project"] = None) -> ass.CueLines:
    """The cues of a fresh translation as (start, end, lines). dual: the original line first, the translation under it."""
    code, name = resolve_language(language)
    p = p or project_store.doc(svc, project_id)
    rec = get_record(svc, project_id, code)
    if rec is None:
        raise LumiereError(f"This project has no {name} translation yet: run subtitles_translate first.", code="translation_missing")
    if rec["signature"] != signature(source_cues(svc, p)):
        raise LumiereError(f"The {name} translation is out of date (the transcript, the cuts or the caption grouping changed since it was made): "
                           "translate again (unchanged cues are reused).", code="translation_stale")
    return [(c["start"], c["end"], [c["source"], c["text"]] if dual else [c["text"]]) for c in rec["cues"]]


def is_fresh(svc: "Services", project_id: str, language: str, p: Optional["Project"] = None) -> bool:
    code, _ = resolve_language(language)
    rec = get_record(svc, project_id, code)
    p = p or project_store.doc(svc, project_id)
    return bool(rec and rec["signature"] == signature(source_cues(svc, p)))


def shift_cues(cues: ass.CueLines, start: int, total: int) -> ass.CueLines:
    """The cues of the part [start, start + total) of the timeline, as the trimmed project times them."""
    out: ass.CueLines = []
    for a, b, lines in cues:
        a2, b2 = max(0, a - start), min(total, b - start)
        if b2 - a2 >= 40:
            out.append((a2, b2, lines))
    return out
