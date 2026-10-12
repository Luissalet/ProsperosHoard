"""Dubbing plan and quality check, pure functions (no I/O, no model calls of
their own).

Before a dubbed line is voiced, `plan_segments` estimates how long it will
take to say (characters per second of the target language, optionally
calibrated from clips the voice has already produced) and compares that with
the slot the original line leaves for it. Lines that could only fit by a
heavy speed-up are rewritten shorter by `shorten_lines` through the local
model. After voicing, `qc_line` / `qc_segments` compare what a speech-to-text
pass hears in the synthetic clip with the text that was meant to be said, so a
mumbled, cut-off or hallucinated line shows up as a high word (or character)
error rate instead of going unnoticed.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from typing import Any, Callable, Iterable, Optional

from .hoard_link.errors import Unavailable

ChatFn = Callable[[list[dict[str, Any]]], str]

# Spoken characters per second (spaces included) at a natural pace. Languages
# written without spaces (ja, zh, ko, th) are counted in characters, which is
# why their numbers are much lower.
CHARS_PER_SECOND: dict[str, float] = {
    "es": 14.5, "en": 14.0, "fr": 14.0, "it": 14.5, "de": 13.0, "pt": 14.5, "ca": 14.5, "nl": 14.0,
    "sv": 13.0, "pl": 13.0, "tr": 13.0, "ru": 12.5, "ar": 12.0, "hi": 13.0,
    "ja": 7.5, "zh": 5.5, "ko": 7.0, "th": 10.0,
}
DEFAULT_CPS = 13.5

# Languages whose quality check uses characters instead of words (no spaces
# between words, so a "word" error rate would be meaningless).
CHAR_METRIC_LANGS = frozenset({"ja", "zh", "ko", "th", "yue", "lo", "km", "my"})
DEFAULT_WER_THRESHOLD = 0.35
DEFAULT_CER_THRESHOLD = 0.25

MAX_SPEEDUP = 1.25
HARD_SPEEDUP = 2.0
MAX_BORROWED_GAP_S = 0.6
MIN_CALIBRATION_SAMPLES = 3
MIN_SLOT_S = 0.05

_LANG_NAMES: dict[str, str] = {
    "english": "en", "inglés": "en", "ingles": "en", "spanish": "es", "español": "es", "espanol": "es",
    "castellano": "es", "french": "fr", "français": "fr", "francais": "fr", "francés": "fr", "frances": "fr",
    "italian": "it", "italiano": "it", "german": "de", "deutsch": "de", "alemán": "de", "aleman": "de",
    "portuguese": "pt", "português": "pt", "portugues": "pt", "catalan": "ca", "català": "ca", "catala": "ca",
    "dutch": "nl", "nederlands": "nl", "swedish": "sv", "polish": "pl", "polski": "pl", "turkish": "tr",
    "russian": "ru", "русский": "ru", "arabic": "ar", "hindi": "hi", "japanese": "ja", "日本語": "ja",
    "chinese": "zh", "mandarin": "zh", "中文": "zh", "korean": "ko", "한국어": "ko", "thai": "th",
}
_ISO3 = {"eng": "en", "spa": "es", "fra": "fr", "fre": "fr", "ita": "it", "deu": "de", "ger": "de", "por": "pt",
         "jpn": "ja", "zho": "zh", "chi": "zh", "kor": "ko", "rus": "ru", "ara": "ar", "hin": "hi", "tha": "th"}


def normalize_lang(value: Optional[str]) -> Optional[str]:
    """'Spanish', 'es', 'es-ES' and 'spa' all give 'es'; an unknown name gives None."""
    if not value:
        return None
    raw = str(value).strip().lower()
    if raw in _LANG_NAMES:
        return _LANG_NAMES[raw]
    head = re.split(r"[-_ ]", raw, maxsplit=1)[0]
    if head in _ISO3:
        return _ISO3[head]
    if head in _LANG_NAMES:
        return _LANG_NAMES[head]
    if re.fullmatch(r"[a-z]{2}", head):
        return head
    return None


# ------------------------------------------------------------- estimation --

def count_chars(text: str) -> int:
    return len(" ".join((text or "").split()))


def chars_per_second(lang: Optional[str], calibration: Optional[float] = None) -> float:
    if calibration and calibration > 0:
        return float(calibration)
    return CHARS_PER_SECOND.get(normalize_lang(lang) or "", DEFAULT_CPS)


def estimate_duration_s(text: str, lang: Optional[str], calibration: Optional[float] = None) -> float:
    """Seconds it takes to say `text` at the language's natural pace, or at the
    calibrated pace when one is given."""
    return round(count_chars(text) / chars_per_second(lang, calibration), 3)


def calibrate(samples: Iterable[tuple[str, float]]) -> Optional[float]:
    """Median characters per second measured from (text, seconds) pairs of
    clips that were really synthesized. Needs at least 3 usable samples (a
    non-empty text and a positive duration); a sample far from the median
    (a clip with a long silence, a cut-off one) is ignored. None when there is
    not enough to go on."""
    rates = []
    for text, seconds in samples:
        chars = count_chars(text)
        if chars >= 2 and seconds and seconds > 0:
            rates.append(chars / float(seconds))
    if len(rates) < MIN_CALIBRATION_SAMPLES:
        return None
    middle = statistics.median(rates)
    kept = [r for r in rates if middle / 1.5 <= r <= middle * 1.5]
    return round(statistics.median(kept), 3) if kept else None


# ------------------------------------------------------------------ plan --

def classify(factor: float, max_speedup: float = MAX_SPEEDUP) -> str:
    if factor <= 1.0:
        return "fits"
    if factor <= max_speedup:
        return "tight"
    return "impossible"


def _plan_item(seg: dict[str, Any], text: str, slot_s: float, own_s: float, gap_s: float, cps: float,
               max_speedup: float, hard_speedup: float) -> dict[str, Any]:
    est = round(count_chars(text) / cps, 3)
    factor = round(est / slot_s, 3) if slot_s > 0 else 0.0
    return {
        "index": seg.get("index"), "text": text, "start_s": seg["start_s"], "end_s": seg["end_s"],
        "own_s": round(own_s, 3), "gap_s": round(gap_s, 3), "slot_s": round(slot_s, 3), "est_s": est,
        "factor": factor, "class": classify(factor, max_speedup), "beyond_hard": factor > hard_speedup,
        "cps": round(cps, 3), "char_budget": int(slot_s * cps), "max_speedup": max_speedup,
        "hard_speedup": hard_speedup,
    }


def plan_segments(segments: list[dict[str, Any]], lang: Optional[str], calibration: Optional[float] = None,
                  max_speedup: float = MAX_SPEEDUP, hard_speedup: float = HARD_SPEEDUP,
                  max_gap_s: float = MAX_BORROWED_GAP_S, tail_gap_s: float = 0.0,
                  text_key: str = "translated_text") -> list[dict[str, Any]]:
    """One plan item per segment (in the order given). A segment's slot is its
    own duration plus the silence before the next one starts, borrowed up to
    `max_gap_s` (the last segment may borrow `tail_gap_s`). `factor` is the
    estimated speech time over that slot; the class is `fits` (1.0 or less,
    no speed-up), `tight` (up to `max_speedup`, a mild one) or `impossible`
    (more than that). `beyond_hard` is set where the factor also passes
    `hard_speedup`, the most a clip should ever be sped up."""
    cps = chars_per_second(lang, calibration)
    plan = []
    for i, seg in enumerate(segments):
        own = max(MIN_SLOT_S, float(seg["end_s"]) - float(seg["start_s"]))
        if i + 1 < len(segments):
            gap = float(segments[i + 1]["start_s"]) - float(seg["end_s"])
        else:
            gap = tail_gap_s
        gap = min(max(0.0, gap), max_gap_s)
        text = seg.get(text_key)
        if text is None:
            text = seg.get("text", "")
        plan.append(_plan_item(seg, text, own + gap, own, gap, cps, max_speedup, hard_speedup))
    return plan


# ------------------------------------------------------------- shortening --

_WRAPPING_QUOTE_PAIRS = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("«", "»")]


def clean_line(text: str) -> str:
    """A model reply as one plain line: whitespace collapsed (a blank line would
    break the SRT format) and one pair of quotes wrapping the whole line removed."""
    text = " ".join((text or "").split())
    if len(text) >= 2:
        for open_q, close_q in _WRAPPING_QUOTE_PAIRS:
            if text[0] == open_q and text[-1] == close_q:
                return text[1:-1].strip()
    return text


def build_shorten_messages(text: str, target_lang: str, char_budget: int, previous_chars: Optional[int] = None,
                           ) -> list[dict[str, Any]]:
    retry = ""
    if previous_chars is not None:
        retry = f" Your last attempt had {previous_chars} characters, which is still too long."
    system = (
        f"You shorten dubbed video dialogue in {target_lang} so it can be spoken inside a fixed time slot. "
        f"Rewrite the line in at most {char_budget} characters, spaces included. Keep the meaning, the register "
        f"and the speaker's tone; drop filler and repetition before dropping content.{retry} "
        "Return only the line: no quotes, no notes, no explanation."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": text}]


def shorten_lines(chat_fn: Optional[ChatFn], plan: list[dict[str, Any]], target_lang: str,
                  max_attempts: int = 2) -> list[dict[str, Any]]:
    """A new plan where every `impossible` line has been offered to the model
    for a shorter rewrite (at most `max_attempts` tries each; the shortest
    version seen wins, and the original stays when no try beat it). Each
    touched item gets `shorten` = {status, attempts: [{text, chars, est_s}]}
    with status `shortened`, `unchanged`, `no_llm` (no model available, or it
    went away mid-way) or `error`; a rewritten item also gets
    `shortened_from`. `chat_fn` None keeps every line and marks `no_llm`."""
    out: list[dict[str, Any]] = []
    llm_gone = chat_fn is None
    for item in plan:
        item = dict(item)
        if item["class"] != "impossible":
            out.append(item)
            continue
        if llm_gone:
            item["shorten"] = {"status": "no_llm", "attempts": []}
            out.append(item)
            continue
        original = item["text"]
        best_text, best_chars = original, count_chars(original)
        attempts: list[dict[str, Any]] = []
        status = "unchanged"
        previous: Optional[int] = None
        for _ in range(max(0, int(max_attempts))):
            try:
                reply = chat_fn(build_shorten_messages(best_text, target_lang, item["char_budget"], previous))  # type: ignore[misc]
            except Unavailable:
                llm_gone = True
                status = "no_llm" if not attempts else status
                break
            except Exception as exc:  # a flaky model must not sink the whole dub
                attempts.append({"text": "", "chars": 0, "est_s": 0.0, "error": str(exc)[:200]})
                status = "error" if status == "unchanged" else status
                break
            candidate = clean_line(reply)
            chars = count_chars(candidate)
            attempts.append({"text": candidate, "chars": chars, "est_s": round(chars / item["cps"], 3)})
            previous = chars
            if candidate and chars < best_chars:
                best_text, best_chars = candidate, chars
                status = "shortened"
                if classify(chars / item["cps"] / item["slot_s"], item["max_speedup"]) != "impossible":
                    break
        if status == "shortened":
            item = _plan_item({"index": item["index"], "start_s": item["start_s"], "end_s": item["end_s"]}, best_text,
                              item["slot_s"], item["own_s"], item["gap_s"], item["cps"], item["max_speedup"],
                              item["hard_speedup"])
            item["shortened_from"] = original
        item["shorten"] = {"status": status, "attempts": attempts}
        out.append(item)
    return out


# ------------------------------------------------------------- smart fit --

AUDIO_MAX = 1.5      # most a dub clip is sped up when the video may also slow down
SOFT_AUDIO = 1.2     # up to here only the audio changes: the speed-up is hardly noticeable
VIDEO_MAX = 2.0      # most a stretch of video is slowed down (2.0 = half speed)
MIN_VIDEO_SLOWDOWN = 1.05  # a slow-down below this is not worth a cut: the audio takes it (or the tail is trimmed)


def fit_strategy(clip_s: float, slot_s: float, gap_s: float, *, audio_max: float = AUDIO_MAX,
                 soft_audio: float = SOFT_AUDIO, video_max: float = VIDEO_MAX) -> dict[str, Any]:
    """How to make a synthetic clip of `clip_s` seconds fit the `slot_s` its
    original line occupies, using the silence after it (`gap_s`, already capped
    by the plan) before anything else:

    1. the free gap is borrowed, as much as the clip needs (`borrow_gap_s`);
    2. if the clip still does not fit the window (slot + borrowed gap), only the
       audio is sped up, up to `soft_audio`;
    3. beyond that the remaining need `r / soft_audio` is split as its square
       root between more audio speed (capped at `audio_max`) and slowing the
       video for that window (`video_factor`, a duration multiplier capped at
       `video_max`; 1.0 leaves the picture alone);
    4. whatever the caps leave over is `trim_s`, the tail of the clip that is cut.

    Returns {kind, borrow_gap_s, window_s, audio_factor, video_factor, trim_s}
    where `kind` is `fits` (nothing to do), `gap`, `audio`, `audio+video` or
    `trimmed`. After the fit the clip plays for `window_s * video_factor`
    seconds; a clip shorter than its slot is left to the caller to pad."""
    slot_s = max(MIN_SLOT_S, float(slot_s))
    clip_s = max(0.0, float(clip_s))
    gap_s = max(0.0, float(gap_s))
    out = {"kind": "fits", "borrow_gap_s": 0.0, "window_s": round(slot_s, 3), "audio_factor": 1.0,
           "video_factor": 1.0, "trim_s": 0.0}
    if clip_s <= slot_s + 1e-9:
        return out
    borrow = min(clip_s - slot_s, gap_s)
    window = slot_s + borrow
    out.update(borrow_gap_s=round(borrow, 3), window_s=round(window, 3))
    if clip_s <= window + 1e-9:
        out["kind"] = "gap"
        return out
    need = clip_s / window
    if need <= soft_audio:
        out.update(kind="audio", audio_factor=round(need, 4))
        return out
    audio = min(audio_max, soft_audio * math.sqrt(need / soft_audio))
    video = min(video_max, need / audio)
    if video < MIN_VIDEO_SLOWDOWN:
        video = 1.0
        audio = min(audio_max, need)
    kind = "audio+video" if video > 1.0 else "audio"
    trim = clip_s / audio - window * video
    if trim > 0.01:
        out["trim_s"] = round(trim, 3)
        kind = "trimmed"
    out.update(kind=kind, audio_factor=round(audio, 4), video_factor=round(video, 4))
    return out


def summarize_retime(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """{retimed, added_s} for a smart-fit job: how many lines slowed their
    picture down and how many seconds that added to the video in total."""
    retimed, added = 0, 0.0
    for seg in segments:
        st = (seg.get("fit") or {}).get("strategy") or {}
        factor = float(st.get("video_factor") or 1.0)
        if factor > 1.0:
            retimed += 1
            added += float(st.get("window_s") or 0.0) * (factor - 1.0)
    return {"retimed": retimed, "added_s": round(added, 3)}


# --------------------------------------------------------------------- QC --

def normalize_for_compare(text: str) -> str:
    """Lower-cased, accent-composed text with punctuation and symbols turned
    into spaces (apostrophes just dropped, so "don't" stays one word) and
    whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"['’`´]", "", text)
    text = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in text)
    return " ".join(text.split())


def _edit_distance(a: list[str], b: list[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


# -- numbers written as digits on one side and as words on the other ---------

_EN_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
            "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ES_ONES = ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
            "once", "doce", "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve",
            "veinte", "veintiuno", "veintidós", "veintitrés", "veinticuatro", "veinticinco", "veintiséis",
            "veintisiete", "veintiocho", "veintinueve"]
_ES_TENS = ["", "", "", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
_ES_HUNDREDS = ["", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos", "seiscientos",
                "setecientos", "ochocientos", "novecientos"]


def _en_words(n: int) -> str:
    if n < 20:
        return _EN_ONES[n]
    if n < 100:
        return _EN_TENS[n // 10] + ("" if n % 10 == 0 else " " + _EN_ONES[n % 10])
    if n < 1000:
        return _EN_ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + _en_words(n % 100))
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= size:
            head, rest = divmod(n, size)
            return _en_words(head) + " " + name + ("" if rest == 0 else " " + _en_words(rest))
    return str(n)


def _es_words(n: int, apocope: bool = False) -> str:
    if n < 30:
        word = _ES_ONES[n]
        if apocope and n in (1, 21):          # "un mil" never, but "veintiún mil", "un millón"
            word = word[:-1] if n == 1 else "veintiún"
        return word
    if n < 100:
        tens, ones = divmod(n, 10)
        return _ES_TENS[tens] + ("" if ones == 0 else " y " + _es_words(ones, apocope))
    if n == 100:
        return "cien"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ES_HUNDREDS[hundreds] + ("" if rest == 0 else " " + _es_words(rest, apocope))
    if n < 10**6:
        thousands, rest = divmod(n, 1000)
        head = "mil" if thousands == 1 else _es_words(thousands, apocope=True) + " mil"
        return head + ("" if rest == 0 else " " + _es_words(rest, apocope))
    if n < 10**9:
        millions, rest = divmod(n, 10**6)
        head = "un millón" if millions == 1 else _es_words(millions, apocope=True) + " millones"
        return head + ("" if rest == 0 else " " + _es_words(rest, apocope))
    return str(n)


def _en_year(n: int) -> Optional[str]:
    """English year reading for 1100-2099 when it differs from the cardinal:
    1999 nineteen ninety nine, 1905 nineteen oh five, 2024 twenty twenty four
    1900 nineteen hundred (2000-2009 are read as cardinals)."""
    if not 1100 <= n <= 2099 or 2000 <= n <= 2009 or n == 2100:
        return None
    head, tail = divmod(n, 100)
    if tail == 0:
        return _en_words(head) + " hundred"        # 1900 nineteen hundred
    return _en_words(head) + " " + ("oh " + _EN_ONES[tail] if tail < 10 else _en_words(tail))


_NUMBER_WORDS = {"en": _en_words, "es": _es_words}
_YEAR_WORDS = {"en": _en_year}


# A run of ASCII digits standing alone as a cardinal: not part of a word ("1st", "3d", "mp4"), not either side of a
# decimal or grouped number ("1.7", "1,7", "2.024"), not an ordinal ("1.º", "2ª", "3 º"). Decided on the original
# text, before punctuation is folded away; a sentence-final "17." still counts.
_STANDALONE_NUMBER = re.compile(r"(?<![\w.,])([0-9]{1,9})(?![0-9\w]|[.,][0-9]|\.?\s?[ºª°])")


def spell_numbers(text: str, lang: Optional[str], *, years: bool = False) -> str:
    """The compare-normalized text with each standalone cardinal (up to nine
    digits) written out in words, for the languages that have a speller (en,
    es); unchanged otherwise. "17" and "seventeen" then compare equal; decimals,
    ordinals and digits inside words are left as they are. With `years`, a
    four-digit year uses the language's year reading where it has one
    ("2024" -> "twenty twenty four" in English)."""
    code = normalize_lang(lang) or ""
    speller = _NUMBER_WORDS.get(code)
    if not speller:
        return normalize_for_compare(text)
    year_speller = _YEAR_WORDS.get(code) if years else None

    def spell(match: re.Match) -> str:
        token = match.group(1)
        n = int(token)
        words = (year_speller(n) if year_speller and len(token) == 4 else None) or speller(n)
        return f" {words} "

    raw = unicodedata.normalize("NFC", text or "")
    return normalize_for_compare(_STANDALONE_NUMBER.sub(spell, raw))


def _wer_tokens(r: list[str], h: list[str]) -> float:
    if not r:
        return 0.0 if not h else 1.0
    return round(_edit_distance(r, h) / len(r), 4)


def word_error_rate(ref: str, hyp: str, lang: Optional[str] = None) -> float:
    """Word-level Levenshtein distance over the reference length (case,
    punctuation and spacing ignored). An empty reference is 0.0 against an empty
    hypothesis and 1.0 otherwise. Can exceed 1.0 when the hypothesis is longer.
    With a `lang` that has a number speller (en, es), digits on either side are
    also tried written out in words, and the lower rate wins: a script saying
    "17" heard as "seventeen" is not an error."""
    base = _wer_tokens(normalize_for_compare(ref).split(), normalize_for_compare(hyp).split())
    code = normalize_lang(lang) or ""
    if code in _NUMBER_WORDS and re.search(r"\d", f"{ref} {hyp}"):
        rates = [base, _wer_tokens(spell_numbers(ref, lang).split(), spell_numbers(hyp, lang).split())]
        if code in _YEAR_WORDS:
            rates.append(_wer_tokens(spell_numbers(ref, lang, years=True).split(),
                                     spell_numbers(hyp, lang, years=True).split()))
        return min(rates)
    return base


def char_error_rate(ref: str, hyp: str) -> float:
    """Character-level counterpart for languages written without spaces
    (whitespace is ignored altogether)."""
    r = list(normalize_for_compare(ref).replace(" ", ""))
    h = list(normalize_for_compare(hyp).replace(" ", ""))
    if not r:
        return 0.0 if not h else 1.0
    return round(_edit_distance(r, h) / len(r), 4)


def metric_for(lang: Optional[str]) -> str:
    return "cer" if normalize_lang(lang) in CHAR_METRIC_LANGS else "wer"


def qc_line(ref: str, hyp: Optional[str], lang: Optional[str], threshold: Optional[float] = None) -> dict[str, Any]:
    """{metric, wer|cer, hyp, threshold, flagged} for one line, or
    {status: "skipped"} when there is no hypothesis (no transcriber)."""
    if hyp is None:
        return {"status": "skipped", "flagged": False}
    metric = metric_for(lang)
    if threshold is None:
        threshold = DEFAULT_CER_THRESHOLD if metric == "cer" else DEFAULT_WER_THRESHOLD
    value = char_error_rate(ref, hyp) if metric == "cer" else word_error_rate(ref, hyp, lang)
    return {"status": "ok", "metric": metric, metric: value, "hyp": hyp, "threshold": threshold,
            "flagged": value > threshold}


def qc_segments(pairs: Iterable[tuple[str, Optional[str]]], lang: Optional[str],
                threshold: Optional[float] = None) -> list[dict[str, Any]]:
    """`qc_line` over (requested text, heard text) pairs."""
    return [qc_line(ref, hyp, lang, threshold) for ref, hyp in pairs]


def summarize(segments: list[dict[str, Any]]) -> dict[str, int]:
    """Counts for a job: lines per plan class, how many were shortened, how many
    the quality check flagged or skipped, and how many lines the model never
    translated (the source line was kept and voiced as it was)."""
    summary = {"fits": 0, "tight": 0, "impossible": 0, "shortened": 0, "flagged": 0, "qc_skipped": 0, "untranslated": 0}
    for seg in segments:
        if seg.get("translation") == "untranslated":
            summary["untranslated"] += 1
        plan = seg.get("plan") or {}
        if plan.get("class") in ("fits", "tight", "impossible"):
            summary[plan["class"]] += 1
        if plan.get("shortened_from"):
            summary["shortened"] += 1
        qc = seg.get("qc") or {}
        if qc.get("flagged"):
            summary["flagged"] += 1
        if qc.get("status") == "skipped":
            summary["qc_skipped"] += 1
    return summary
