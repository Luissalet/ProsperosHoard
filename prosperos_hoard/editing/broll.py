"""B-roll suggestions: for a range of the edit, or for each sentence of its main-track transcript, find library clips that could
cover it. A clip matches through what it says (its own transcript, sentence by sentence), what it is called (file name) and what
it is labelled (the media's tags). Matching is plain keyword overlap weighted by how rare a word is in the library (TF-IDF
style, with a light stemmer for Spanish and English); the local model, when asked, adds the visual nouns a sentence is about
(a sentence that says "salimos de casa" may want "puerta, calle"). Each suggestion carries the source range to use, and the
``add_overlay`` operation places it over the sentence (muted, fit cover)."""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Optional

from . import analyze
from . import commands
from . import media as media_store
from . import projects as project_store
from .analysis import moments
from .analysis.speech import norm
from .errors import LumiereError
from .util import ms_to_tc

if TYPE_CHECKING:
    from .services import Services

STOP = set("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque bien cada casi como con contra cual cuando de del desde donde dos el ella ellas ello ellos en entre era eran
es esa esas ese eso esos esta estaba estan estar este esto estos fue fueron ha han hasta hay la las le les lo los mas me mi mis mucho muy nada ni no nos nosotros nuestro o os otra otro para
pero poco por porque que quien se sea ser si sido sin sobre son su sus tambien tan tanto te tiene todo todos tu tus un una unas uno unos usted va vamos van vez voy y ya yo
a about after all also am an and any are as at be because been before being but by can could did do does doing down for from had has have he her here him his how i if in into is it its just
like me more most my no not now of on one only or other our out over she should so some than that the their them then there these they this to too up us very was we were what when where which
while who why will with would you your eh ehm um o sea pues entonces vale bueno oye
""".split())
MIN_TOKEN = 3
MAX_SENTENCES = 60
MAX_SENTENCE_MS = 12000


def stem(token: str) -> str:
    """A rough root shared by the forms of a word: folded, lower-case, without a plural ending ('coches' and 'coche' meet at
    'coch', 'cars' and 'car' at 'car', 'playas' and 'playa' at 'play') and without a final a/e/o. Crude on purpose: the match is
    between two short texts about the same footage, and a rare false friend costs one wrong suggestion."""
    t = norm(token)
    if len(t) > 5 and t.endswith("es"):
        t = t[:-2]
    elif len(t) > 3 and t.endswith("s"):
        t = t[:-1]
    elif len(t) > 6 and t.endswith("ing"):
        t = t[:-3]
    if len(t) > 4 and t[-1] in "aeo":
        t = t[:-1]
    return t


def token_pairs(text: str) -> list[tuple[str, str]]:
    """[(root, word as written)] for the content words of ``text`` (stop words and very short words removed); file-name
    separators and camelCase split."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
    text = re.sub(r"[_\-./\\]+", " ", text)
    out = []
    for raw in text.split():
        w = norm(raw)
        if len(w) >= MIN_TOKEN and w not in STOP and not w.isdigit():
            out.append((stem(w), w))
    return out


def tokens_of(text: str) -> list[str]:
    return [root for root, _ in token_pairs(text)]


# ---------------------------------------------------------------- the main track's sentences

def main_sentences(svc: "Services", p: Any, start: Optional[int], end: Optional[int]) -> list[dict[str, Any]]:
    """Sentences of the words heard on the main track (timeline times). With start/end, the whole range is one query."""
    words = commands.timeline_transcript(svc, p)["words"]
    if start is not None or end is not None:
        lo, hi = start or 0, end if end is not None else p.duration
        sel = [w for w in words if w["t"] >= lo and w["t1"] <= hi + 200]
        text = " ".join(w["text"] for w in sel)
        return [{"i": 0, "t0": lo, "t1": hi, "text": text, "media": sel[0]["media"] if sel else None}] if sel or hi > lo else []
    sents = moments.sentences_of({"words": [{"t0": w["t"], "t1": w["t1"], "text": w["text"], "media": w["media"]} for w in words]})
    out = []
    for s in sents[:MAX_SENTENCES]:
        if s["t1"] - s["t0"] > MAX_SENTENCE_MS:
            s = {**s, "t1": s["t0"] + MAX_SENTENCE_MS}
        out.append(s)
    return out


# ---------------------------------------------------------------- the library's words

def _segments_of(svc: "Services", info: dict[str, Any]) -> list[dict[str, Any]]:
    """What can be matched in one media: its name and tags (the whole media) and each sentence of its own transcript (that range)."""
    dur = int(info["duration_ms"] or 0)
    segs: list[dict[str, Any]] = []
    label = tokens_of(info["name"]) + [t for tag in info.get("tags") or [] for t in tokens_of(str(tag))]
    if label:
        segs.append({"via": "name", "tokens": label, "t0": 0, "t1": dur})
    tr = media_store.get_analysis(svc, info["id"], "transcript")
    if tr and tr.get("words"):
        scenes = [c["t"] for c in (media_store.get_analysis(svc, info["id"], "scenes") or {}).get("cuts", [])]
        for s in moments.sentences_of(tr):
            toks = tokens_of(s["text"])
            if not toks:
                continue
            t0 = s["t0"]
            before = [x for x in scenes if x <= t0]
            if before and t0 - before[-1] < 4000:  # start the shot where its scene starts, not mid-way
                t0 = before[-1]
            segs.append({"via": "transcript", "tokens": toks, "t0": max(0, t0), "t1": s["t1"]})
    return segs


class Index:
    """Keyword index of the library's video and image media: segments with tokens, and the rarity (IDF) of every token."""

    def __init__(self, svc: "Services", exclude: set[str]):
        self.segments: list[dict[str, Any]] = []
        self.media: dict[str, dict[str, Any]] = {}
        for info in media_store.list_media(svc, limit=500):
            if info["kind"] == "audio" or not (info["has_video"] or info["kind"] == "image") or info["missing"] or info["id"] in exclude:
                continue
            self.media[info["id"]] = info
            for seg in _segments_of(svc, info):
                self.segments.append({**seg, "media": info["id"]})
        df: dict[str, int] = {}
        for seg in self.segments:
            for tok in set(seg["tokens"]):
                df[tok] = df.get(tok, 0) + 1
        n = max(1, len(self.segments))
        self.idf = {tok: math.log(1 + n / c) for tok, c in df.items()}

    def best(self, query: dict[str, float], length: int, per: int) -> list[dict[str, Any]]:
        """Top ``per`` media (best segment of each) for a weighted query {token: weight}."""
        total = sum(self.idf.get(t, 1.0) * w for t, w in query.items()) or 1.0
        winners: dict[str, dict[str, Any]] = {}
        for seg in self.segments:
            have = set(seg["tokens"])
            hit = [t for t in query if t in have]
            if not hit:
                continue
            score = sum(self.idf.get(t, 1.0) * query[t] for t in hit) / total
            if seg["via"] == "name":
                score *= 1.15  # a name or tag is a deliberate label
            if score > winners.get(seg["media"], {}).get("score", 0):
                winners[seg["media"]] = {"score": score, "seg": seg, "matched": hit}
        out = []
        for mid, w in sorted(winners.items(), key=lambda kv: -kv[1]["score"])[:per]:
            seg, info = w["seg"], self.media[mid]
            dur = int(info["duration_ms"] or 0)
            if info["kind"] == "image":
                a, b = 0, length
            else:
                a = min(seg["t0"], max(0, dur - 500))
                b = min(dur, a + length)
                if b - a < min(length, 1500) and dur >= length:  # too little left after the match: show the last stretch instead
                    a, b = max(0, dur - length), dur
            out.append({"media": mid, "name": info["name"], "kind": info["kind"], "src_in": int(a), "src_out": int(b), "length": int(b - a),
                        "score": round(min(1.0, w["score"]), 3), "matched": sorted(set(w["matched"])), "via": seg["via"],
                        "at": f"{ms_to_tc(a)}–{ms_to_tc(b)}"})
        return out


def model_keywords(svc: "Services", sentences: list[dict[str, Any]]) -> tuple[dict[int, list[tuple[str, str]]], Optional[str], Optional[str]]:
    """Visual keywords per sentence from the local model, one compact line each ('3: coche, carretera, noche'). Returns
    ({sentence index: [(root, word)]}, model name, a reason when it could not be used)."""
    from .errors import ModelUnavailable
    from .generate import chat_text

    system = ("For each numbered sentence of a video, list 2 to 5 single words naming what could be SHOWN on screen while it is said (objects, "
              "places, people, actions), in the language of the sentence. Answer one line per sentence and nothing else, exactly like:\n"
              "3: coche, carretera, noche\nUse '-' when nothing can be shown.")
    got: dict[int, list[tuple[str, str]]] = {}
    name: Optional[str] = None
    for k in range(0, len(sentences), 25):
        batch = sentences[k: k + 25]
        user = "\n".join(f"{s['i']}: {s['text']}" for s in batch)
        try:
            text, name = chat_text(svc, [{"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens=600, effort="off")
        except ModelUnavailable as error:
            return got, name, f"the local model is not reachable ({str(error)[:100]})"
        for line in text.splitlines():
            m = re.match(r"^\W*(\d+)\s*[:.)-]\s*(.+)$", line.strip())
            if m and m.group(2).strip() not in ("-", "—"):
                got[int(m.group(1))] = [pair for w in re.split(r"[,;]", m.group(2)) for pair in token_pairs(w)]
    return got, name, None if got else "the model gave no keywords"


# ---------------------------------------------------------------- the suggestions

def suggest(svc: "Services", project_id: str, *, start: Optional[int] = None, end: Optional[int] = None, per_sentence: int = 3, use_model: bool = False,
            min_score: float = 0.18, place: str = "none") -> dict[str, Any]:
    """Library clips for each sentence of the main track's speech (or for the range start..end as a whole). Each suggestion lists up to
    ``per_sentence`` options with the source range, the matching words and why. ``place='best'`` puts the best option of every
    suggestion over its sentence as one undo step (muted overlays, fit cover) and returns what it placed."""
    p = project_store.doc(svc, project_id)
    if place not in ("none", "best"):
        raise LumiereError("place must be 'none' or 'best'.")
    tl = commands.timeline_transcript(svc, p)
    if tl["missing"]:
        jobs: list[dict[str, Any]] = []
        for mid in tl["missing"]:
            jobs += analyze.schedule(svc, mid, ["transcript"])
        if not tl["words"]:
            raise commands.NeedsAnalysis("B-roll suggestions need the transcript of the main track; it is running now. Repeat when it finishes.", jobs)
    sentences = main_sentences(svc, p, start, end)
    if not sentences:
        raise LumiereError("There is no speech on the main track in that range to find pictures for. Give start and end to look for a range anyway.")
    used = {c.media for c in (p.main_track().clips if p.main_track() else []) if c.media}
    index = Index(svc, exclude=used)
    if not index.segments:
        raise LumiereError("The library has no other videos or images to suggest (import some b-roll; name or tag them to make them easy to find).")
    note: Optional[str] = None
    extra: dict[int, list[tuple[str, str]]] = {}
    model_name: Optional[str] = None
    if use_model:
        extra, model_name, note = model_keywords(svc, sentences)
    results = []
    for s in sentences:
        query: dict[str, float] = {}
        shown: dict[str, str] = {}  # root -> the word as said, for the answer
        for root, word in token_pairs(s["text"]):
            query[root] = query.get(root, 0) + 1.0
            shown.setdefault(root, word)
        for root, word in extra.get(s["i"], []):
            query[root] = query.get(root, 0) + 1.2
            shown.setdefault(root, word)
        length = max(1500, min(int(s["t1"] - s["t0"]), MAX_SENTENCE_MS))
        options = [o for o in index.best(query, length, per_sentence) if o["score"] >= min_score] if query else []
        for o in options:
            o["matched"] = sorted({shown.get(t, t) for t in o["matched"]})
        results.append({"sentence": s["text"][:200], "start_ms": int(s["t0"]), "end_ms": int(s["t1"]), "range": f"{ms_to_tc(s['t0'])}–{ms_to_tc(s['t1'])}",
                        "keywords": [shown[k] for k in sorted(query, key=lambda k: -query[k])[:8]], "options": options})
    out: dict[str, Any] = {"project": project_id, "suggestions": results, "with_options": sum(1 for r in results if r["options"]),
                           "library_checked": len(index.media), "model": {"requested": use_model, "used": bool(extra), "name": model_name, "note": note}}
    if place == "best":
        ops = [{"op": "add_overlay", "media": r["options"][0]["media"], "start": r["start_ms"], "length": r["end_ms"] - r["start_ms"],
                "src_in": r["options"][0]["src_in"]} for r in results if r["options"]]
        if ops:
            res = project_store.edit(svc, project_id, ops, label="B-roll sugerido", actor="agent")
            out["placed"] = [{"start": op["start"], "media": op["media"]} for op in ops]
            out["rev"] = res["rev"]
        else:
            out["placed"] = []
    return out
