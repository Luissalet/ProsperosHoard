"""Narrated shorts: a topic (or a finished script) becomes a vertical video
with a voice-over, word-timed captions, a music bed that ducks under the
voice and pictures cut to what is being said - as a resumable production
(`state["kind"] == "short"`, same folder, jobs, lineage and review pause as
a music video; see productions.py).

Stages (`SHORT_STAGES`):

1. **script** - the given script (text or segments), or one written by the
   family's LLM through Hoard Link: a hook, 5-9 segments, each with the
   narration, an English image prompt and English stock keywords, plus a
   title, a description and hashtags. `settings.script_review` pauses here.
2. **narration** - every sentence voiced (a voice-studio voice spec, or
   Piper / Faustus TTS), joined with short pauses; the sentence and
   segment times are exact (they are where the audio was placed). Word
   times come from speech-to-text when one is installed (the script's own
   words aligned onto the recognised ones, so captions keep the script's
   spelling) and otherwise from a syllable-weighted estimate per sentence.
3. **music** - none, an existing asset, one composed with ACE-Step
   (instrumental), or a track from `data/music/` picked by seed.
4. **visuals** - the shot plan (each segment split into ~`shot_s` shots)
   and one picture per shot: stock footage (Pexels/Pixabay, see stock.py)
   or a generated still (the project's image engine), per `visuals.source`.
5. **animatic** - only when stills will become Wan clips: a 720p preview
   with the stills, then a pause for review.
6. **clips** - the `visuals.clips` longest generated stills animated.
7. **mix** - voice + ducked music, -14 LUFS (soundtrack.py).
8. **timeline** - one timeline per aspect (shots on the narration's clock,
   captions from the word times, `bold` caption style by default) and its
   renders.
9. **report** - REPORT.md and `publish.txt` (title, description, hashtags
   and the stock credits, ready to paste).

The runner talks to the outside world only through the `Studio` facade
(`chat`, `synthesize`, `transcribe`, `stock_keys`, `stock_client` besides
the usual `generate`/`compose`/`render`), so the tests drive it with fakes.
"""

from __future__ import annotations

import difflib
import io
import json
import random
import re
import unicodedata
import wave
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import engine, soundtrack, stock
from . import productions as prod
from . import timeline as timeline_mod
from . import video as video_mod
from . import voice_pipelines as vp
from .ids import new_id
from .util import now_iso

KIND = "short"
SHORT_STAGES = ("script", "narration", "music", "visuals", "animatic", "clips", "mix", "timeline", "report")
SOURCES = ("auto", "stock", "generate", "mix")
MUSIC_MODES = ("none", "asset", "compose", "library")
CAPTION_STYLES = ("bold", "default", "horror", "none")
SAMPLE_RATE = 24000
LEAD_IN_S = 0.15
SENTENCE_GAP_S = 0.22
SEGMENT_GAP_S = 0.38
TAIL_S = 0.6
WORDS_PER_SECOND = {"es": 2.7, "en": 2.5}
LANGUAGE_NAMES = {"es": "Spanish (Spain)", "en": "English", "fr": "French", "it": "Italian", "pt": "Portuguese", "de": "German"}
DEFAULT_VOICES = {"es": "es_ES-davefx-medium", "en": "en_US-lessac-medium"}
DEFAULT_LOOK = "cinematic photograph, natural light, shallow depth of field, rich detail"
DEFAULT_NEGATIVE = "text, letters, watermark, logo, subtitles, caption, frame, border, blurry, deformed"
FRAMINGS = ("wide establishing shot", "medium shot", "close-up detail shot", "low angle shot", "high angle shot",
            "over-the-shoulder shot")
MOTION_PROMPT = "slow cinematic camera push-in, subtle natural motion, steady"
MUSIC_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a")


class ShortError(prod.ProductionError):
    pass


def is_short(state: dict[str, Any]) -> bool:
    return (state or {}).get("kind") == KIND


# ------------------------------------------------------------------ spec

def _num(value: Any, where: str, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ShortError("bad_spec", f"{where} must be a number") from None
    if not lo <= v <= hi:
        raise ShortError("bad_spec", f"{where} must be between {lo:g} and {hi:g}")
    return v


def normalise_spec(spec: Any) -> dict[str, Any]:
    """Validate a short's spec and fill the defaults. Raises ShortError
    naming the offending field."""
    if not isinstance(spec, dict):
        raise ShortError("bad_spec", "spec must be an object")
    spec = json.loads(json.dumps(spec))
    spec["kind"] = KIND
    script = spec.get("script")
    topic = str(spec.get("topic") or "").strip()
    if not topic and not script:
        raise ShortError("bad_spec", "a short needs a topic (the script is written for you) or a script")
    spec["topic"] = topic[:600]
    if script is not None:
        spec["script"] = normalise_script(script)
    lang = str(spec.get("language") or "es").strip().lower()[:5]
    spec["language"] = lang
    spec["duration_s"] = _num(spec.get("duration_s", 45), "spec.duration_s", 10, 180)
    spec["tone"] = str(spec.get("tone") or "")[:200]
    spec["seed"] = int(_num(spec.get("seed", 0), "spec.seed", 0, 2**31 - 2))
    spec.setdefault("title", None)
    spec.setdefault("engine", "auto")
    if spec["engine"] not in engine.IMAGE_ENGINES:
        raise ShortError("bad_spec", f"spec.engine must be one of {', '.join(engine.IMAGE_ENGINES)}")
    voice = spec.get("voice") or {}
    if not isinstance(voice, dict):
        raise ShortError("bad_spec", "spec.voice must be an object (a voice-studio spec, or {backend, voice_id, speed})")
    if voice.get("speed") is not None:
        voice["speed"] = _num(voice["speed"], "spec.voice.speed", 0.5, 2.0)
    spec["voice"] = voice
    visuals = spec.get("visuals") or {}
    if not isinstance(visuals, dict):
        raise ShortError("bad_spec", "spec.visuals must be an object")
    visuals.setdefault("source", "auto")
    if visuals["source"] not in SOURCES:
        raise ShortError("bad_spec", f"spec.visuals.source must be one of {', '.join(SOURCES)}")
    visuals["shot_s"] = _num(visuals.get("shot_s", 3.0), "spec.visuals.shot_s", 1.5, 8.0)
    visuals["clips"] = int(_num(visuals.get("clips", 0), "spec.visuals.clips", 0, 20))
    visuals.setdefault("look", DEFAULT_LOOK)
    visuals.setdefault("negative", DEFAULT_NEGATIVE)
    visuals.setdefault("stock_kind", "video")
    if visuals["stock_kind"] not in stock.KINDS:
        raise ShortError("bad_spec", "spec.visuals.stock_kind is 'video' or 'image'")
    providers = visuals.get("providers") or list(stock.PROVIDERS)
    if not isinstance(providers, list) or any(p not in stock.PROVIDERS for p in providers):
        raise ShortError("bad_spec", f"spec.visuals.providers holds {', '.join(stock.PROVIDERS)}")
    visuals["providers"] = providers
    visuals.setdefault("motion_prompt", MOTION_PROMPT)
    spec["visuals"] = visuals
    music = spec.get("music") or {}
    if not isinstance(music, dict):
        raise ShortError("bad_spec", "spec.music must be an object")
    music.setdefault("mode", "compose" if music.get("tags") else ("asset" if music.get("asset_id") else "none"))
    if music["mode"] not in MUSIC_MODES:
        raise ShortError("bad_spec", f"spec.music.mode must be one of {', '.join(MUSIC_MODES)}")
    if music["mode"] == "asset" and not music.get("asset_id"):
        raise ShortError("bad_spec", "spec.music.mode 'asset' needs spec.music.asset_id")
    if music["mode"] == "compose" and not str(music.get("tags") or "").strip():
        music["tags"] = "lo-fi ambient instrumental, soft pads, gentle beat, no vocals"
    music["volume_db"] = _num(music.get("volume_db", soundtrack.DEFAULT_MUSIC_DB), "spec.music.volume_db", -40, 0)
    music.setdefault("duck", "normal")
    if music["duck"] not in ("off", *soundtrack.DUCK_PRESETS):
        raise ShortError("bad_spec", f"spec.music.duck must be off or one of {', '.join(soundtrack.DUCK_PRESETS)}")
    music["bpm"] = int(_num(music.get("bpm", 90), "spec.music.bpm", 40, 220))
    spec["music"] = music
    captions = spec.get("captions") or {}
    if not isinstance(captions, dict):
        raise ShortError("bad_spec", "spec.captions must be an object")
    captions.setdefault("style", "bold")
    if captions["style"] not in CAPTION_STYLES:
        raise ShortError("bad_spec", f"spec.captions.style must be one of {', '.join(CAPTION_STYLES)}")
    captions["max_words"] = int(_num(captions.get("max_words", 3), "spec.captions.max_words", 1, 8))
    spec["captions"] = captions
    tl = spec.get("timeline") or {}
    if not isinstance(tl, dict):
        raise ShortError("bad_spec", "spec.timeline must be an object")
    tl.setdefault("aspects", ["9:16"])
    tl.setdefault("qualities", ["preview"])
    tl.setdefault("finishing", {})
    tl.setdefault("transitions", "cut")
    for aspect in tl["aspects"]:
        if aspect not in timeline_mod.ASPECTS:
            raise ShortError("bad_spec", f"spec.timeline.aspects: '{aspect}' is not one of {', '.join(timeline_mod.ASPECTS)}")
    for quality in tl["qualities"]:
        if quality not in ("preview", "final"):
            raise ShortError("bad_spec", "spec.timeline.qualities holds 'preview' and/or 'final'")
    if tl["transitions"] not in ("cut", "crossfade"):
        raise ShortError("bad_spec", "spec.timeline.transitions is 'cut' or 'crossfade'")
    try:
        video_mod.validate_finishing({k: v for k, v in tl["finishing"].items() if k != "lyric_style"})
    except video_mod.RenderError as exc:
        raise ShortError("bad_spec", f"spec.timeline.finishing: {exc}") from None
    spec["timeline"] = tl
    return spec


def normalise_script(script: Any) -> dict[str, Any]:
    """A script is plain text (split into segments of one or two sentences)
    or {title?, description?, hashtags?, segments: [{text, visual?, query?,
    source?}]}."""
    if isinstance(script, str):
        text = script.strip()
        if not text:
            raise ShortError("bad_script", "the script is empty")
        return {"segments": segments_from_text(text)}
    if not isinstance(script, dict):
        raise ShortError("bad_script", "script is text or {title, segments: [{text, visual, query}]}")
    segments = script.get("segments")
    if isinstance(segments, str) or (not segments and script.get("text")):
        segments = segments_from_text(str(segments or script.get("text")))
    if not isinstance(segments, list) or not segments or len(segments) > 40:
        raise ShortError("bad_script", "script.segments must be a list of 1-40 segments")
    clean = []
    for i, seg in enumerate(segments):
        if isinstance(seg, str):
            seg = {"text": seg}
        if not isinstance(seg, dict) or not str(seg.get("text") or "").strip():
            raise ShortError("bad_script", f"script.segments[{i}] needs text")
        entry = {"text": " ".join(str(seg["text"]).split())[:600]}
        for key in ("visual", "query"):
            if str(seg.get(key) or "").strip():
                entry[key] = " ".join(str(seg[key]).split())[:400]
        if seg.get("source"):
            if seg["source"] not in ("stock", "generate"):
                raise ShortError("bad_script", f"script.segments[{i}].source is 'stock' or 'generate'")
            entry["source"] = seg["source"]
        clean.append(entry)
    out: dict[str, Any] = {"segments": clean}
    for key in ("title", "description"):
        if str(script.get(key) or "").strip():
            out[key] = str(script[key]).strip()[:300]
    tags = script.get("hashtags")
    if isinstance(tags, list):
        out["hashtags"] = [re.sub(r"[^\w]", "", str(t).lstrip("#"))[:40] for t in tags if str(t).strip()][:10]
    return out


def segments_from_text(text: str, max_words: int = 28) -> list[dict[str, Any]]:
    sentences = vp.split_into_sentences(text)
    segments: list[list[str]] = []
    for s in sentences:
        if segments and len(segments[-1]) < 2 and len(" ".join(segments[-1] + [s]).split()) <= max_words:
            segments[-1].append(s)
        else:
            segments.append([s])
    return [{"text": " ".join(parts)} for parts in segments]


# --------------------------------------------------------- script (LLM)

def script_messages(topic: str, language: str, duration_s: float, tone: str) -> list[dict[str, Any]]:
    words = int(duration_s * WORDS_PER_SECOND.get(language, 2.5))
    lang = LANGUAGE_NAMES.get(language, language)
    system = ("You write scripts for vertical short videos (TikTok, Reels, YouTube Shorts). You answer with one JSON "
              "object and nothing else.")
    user = (
        f"Topic: {topic}\n"
        f"Narration language: {lang}\n"
        f"Length: about {words} words of narration (~{int(duration_s)} seconds spoken).\n"
        + (f"Tone: {tone}\n" if tone else "")
        + "Open with a hook that makes people stay; one idea per segment; end with a short closing line. "
        "Facts must be correct - leave out anything you are unsure of. No emojis, no stage directions.\n"
        "Split it into 5 to 9 segments of one or two sentences each. For every segment give:\n"
        f"- \"text\": the narration, in {lang};\n"
        "- \"visual\": an English prompt for an image generator: one concrete, filmable shot that illustrates the "
        "segment (subject, setting, light), never text or logos in the picture;\n"
        "- \"query\": 2 to 4 English keywords to search stock footage for that shot.\n"
        f"Also give \"title\" (at most 60 characters, in {lang}), \"description\" (one or two sentences, in {lang}) "
        "and \"hashtags\" (3 to 6 words without #).\n"
        "Answer exactly in this shape: {\"title\": \"...\", \"description\": \"...\", \"hashtags\": [\"...\"], "
        "\"segments\": [{\"text\": \"...\", \"visual\": \"...\", \"query\": \"...\"}]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_script_reply(text: str) -> dict[str, Any]:
    """The JSON object inside an LLM reply (code fences and chatter around
    it tolerated), normalised as a script."""
    raw = str(text or "")
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ShortError("script_unreadable", "the model's reply held no JSON script")
    blob = raw[start:end + 1]
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        try:
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", blob))  # trailing commas
        except json.JSONDecodeError as exc:
            raise ShortError("script_unreadable", f"the model's JSON script did not parse: {exc}") from None
    return normalise_script(data)


# ------------------------------------------------------------ narration

_WORD_NORM = re.compile(r"[^\w]+", re.UNICODE)


def _norm_word(word: str) -> str:
    word = unicodedata.normalize("NFKD", word.lower())
    word = "".join(ch for ch in word if not unicodedata.combining(ch))
    return _WORD_NORM.sub("", word)


def align_words(script_words: list[dict[str, Any]], heard: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    """Give the script's words (with estimated times) the times of the words
    speech-to-text heard, matched by `difflib` on normalised spelling;
    unmatched words keep an estimate squeezed between their matched
    neighbours. Returns (words, share matched)."""
    a = [_norm_word(w["text"]) for w in script_words]
    b = [_norm_word(w["text"]) for w in heard]
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = [dict(w) for w in script_words]
    matched = [False] * len(out)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            i, j = block.a + k, block.b + k
            out[i]["start_s"], out[i]["end_s"] = heard[j]["start_s"], heard[j]["end_s"]
            matched[i] = True
    # unmatched runs: spread between the matched neighbours (or keep the estimate)
    i = 0
    while i < len(out):
        if matched[i]:
            i += 1
            continue
        j = i
        while j < len(out) and not matched[j]:
            j += 1
        lo = out[i - 1]["end_s"] if i > 0 else out[i]["start_s"]
        hi = out[j]["start_s"] if j < len(out) else out[j - 1]["end_s"]
        if hi > lo:
            step = (hi - lo) / (j - i)
            for k in range(i, j):
                out[k]["start_s"] = round(lo + (k - i) * step, 3)
                out[k]["end_s"] = round(lo + (k - i + 1) * step, 3)
        i = j
    share = sum(matched) / len(out) if out else 0.0
    return out, round(share, 3)


def caption_clips(words: list[dict[str, Any]], max_words: int = 3, max_chars: int = 22, total_s: Optional[float] = None,
                  pause_s: float = 0.3) -> list[dict[str, Any]]:
    """Group timed words into captions of at most `max_words` words /
    `max_chars` characters, breaking after sentence punctuation and at
    pauses; each caption lasts until the next one starts (or a little after
    its last word)."""
    groups: list[list[dict[str, Any]]] = []
    for w in words:
        text = str(w["text"]).strip()
        if not text:
            continue
        cur = groups[-1] if groups else None
        if (cur is None or len(cur) >= max_words
                or len(" ".join(x["text"] for x in cur) + " " + text) > max_chars
                or re.search(r"[.!?…:;]$", cur[-1]["text"])
                or float(w["start_s"]) - float(cur[-1]["end_s"]) > pause_s):
            groups.append([{"text": text, "start_s": float(w["start_s"]), "end_s": float(w["end_s"])}])
        else:
            cur.append({"text": text, "start_s": float(w["start_s"]), "end_s": float(w["end_s"])})
    clips = []
    for i, g in enumerate(groups):
        start = g[0]["start_s"]
        last_end = g[-1]["end_s"] + 0.25
        nxt = groups[i + 1][0]["start_s"] if i + 1 < len(groups) else (total_s if total_s else last_end)
        end = min(nxt, max(last_end, g[-1]["end_s"] + 0.05)) if i + 1 < len(groups) else min(nxt, last_end)
        if end - start < 0.2:
            end = start + 0.2
        clips.append({"text": " ".join(x["text"] for x in g), "start_s": round(start, 3), "end_s": round(end, 3),
                      "karaoke": True, "words": [{k: (round(v, 3) if k != "text" else v) for k, v in x.items()} for x in g]})
    return clips


def wav_bytes_mono(samples: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    pcm = np.clip(samples, -1.0, 1.0)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((pcm * 32767.0).astype("<i2").tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- plan

def plan_shots(segments: list[dict[str, Any]], total_s: float, shot_s: float) -> list[dict[str, Any]]:
    """Each segment's screen time (from its narration start to the next
    segment's, the first from 0 and the last to the end) split into shots of
    about `shot_s` seconds."""
    shots = []
    for i, seg in enumerate(segments):
        a = 0.0 if i == 0 else float(seg["start_s"])
        b = float(segments[i + 1]["start_s"]) if i + 1 < len(segments) else total_s
        span = max(0.5, b - a)
        n = max(1, min(int(round(span / shot_s)), int(span // 1.0) or 1))
        step = span / n
        for k in range(n):
            shots.append({"key": f"s{i + 1}_{k + 1}", "segment": i, "index": k, "start_s": round(a + k * step, 3),
                          "duration_s": round(step if k < n - 1 else b - (a + k * step), 3)})
    return shots


def shot_source(visual_source: str, segment: dict[str, Any], shot_n: int, stock_ready: bool) -> str:
    if segment.get("source"):
        wanted = segment["source"]
    elif visual_source == "mix":
        wanted = "stock" if shot_n % 2 == 0 else "generate"
    elif visual_source == "auto":
        wanted = "stock" if stock_ready else "generate"
    else:
        wanted = visual_source
    if wanted == "stock" and not stock_ready:
        return "generate"
    return wanted


def keywords(text: str, n: int = 4) -> str:
    """A crude stock query from narration when the script has none."""
    words = [w for w in re.findall(r"[^\W\d_]{4,}", text.lower())]
    seen: list[str] = []
    for w in sorted(words, key=len, reverse=True):
        if w not in seen:
            seen.append(w)
    return " ".join(seen[:n]) or text[:40]


# ------------------------------------------------------------------- run

class ShortRun(prod.Run):
    """The pipeline of a narrated short (see the module docstring)."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        if not is_short(self.state):
            raise ShortError("not_a_short", f"'{self.slug}' is not a narrated short")

    # -- helpers
    def _segments(self) -> list[dict[str, Any]]:
        return (self.state["done"].get("script") or {}).get("segments") or []

    def _aspect(self) -> str:
        return ((self.spec.get("timeline") or {}).get("aspects") or ["9:16"])[0]

    def _stock_keys(self) -> dict[str, str]:
        getter = getattr(self.studio, "stock_keys", None)
        return getter() if getter else {}

    # -- stages
    def stage_script(self) -> Optional[str]:
        spec = self.spec
        if spec.get("script"):
            script = json.loads(json.dumps(spec["script"]))
            source = "given"
            missing = [s for s in script["segments"] if not s.get("visual") or not s.get("query")]
            if missing:
                for seg in missing:
                    seg.setdefault("visual", seg["text"])
                    seg.setdefault("query", keywords(seg["text"]))
        else:
            chat = getattr(self.studio, "chat", None)
            if chat is None:
                raise ShortError("no_llm", "no language model to write the script; pass spec.script")
            try:
                reply = chat(script_messages(spec["topic"], spec["language"], spec["duration_s"], spec.get("tone") or ""),
                             1600, 0.8)
            except prod.ProductionError:
                raise
            except Exception as exc:  # noqa: BLE001 - Hoard Link: no model resident, etc.
                raise ShortError("no_llm", f"could not reach a language model to write the script ({type(exc).__name__}: "
                                           f"{str(exc)[:200]}); start one in Faustus or pass spec.script") from None
            script = parse_script_reply(reply)
            source = "llm"
            for seg in script["segments"]:
                seg.setdefault("visual", seg["text"])
                seg.setdefault("query", keywords(seg.get("visual") or seg["text"]))
        script.setdefault("title", spec.get("title") or (spec.get("topic") or self.state["name"])[:60])
        script["source"] = source
        self.state["done"]["script"] = script
        self.log("script", source=source, segments=len(script["segments"]),
                 words=sum(len(s["text"].split()) for s in script["segments"]))
        if (self.state.get("settings") or {}).get("script_review") and not (self.state.get("review") or {}).get("script_approved"):
            return "pause"
        return None

    def stage_narration(self) -> None:
        synth = getattr(self.studio, "synthesize", None)
        if synth is None:
            raise ShortError("no_tts", "no text-to-speech available")
        voice = dict(self.spec.get("voice") or {})
        if not voice.get("engine_id") and not voice.get("voice_id"):
            voice.setdefault("backend", "piper")
            voice["voice_id"] = DEFAULT_VOICES.get(self.spec["language"], DEFAULT_VOICES["en"])
            if self.spec["language"] not in DEFAULT_VOICES:
                self.log("voice_fallback", note=f"no default voice for '{self.spec['language']}': an English Piper voice "
                                                "reads it; pass spec.voice (a voice-studio voice) for a native one")
        script = self.state["done"]["script"]
        segments = script["segments"]
        chunks: list[np.ndarray] = [np.zeros(int(LEAD_IN_S * SAMPLE_RATE), dtype=np.float32)]
        t = LEAD_IN_S
        words: list[dict[str, Any]] = []
        engines: set[str] = set()
        timed = []
        n_sent = sum(len(vp.split_into_sentences(s["text"]) or [s["text"]]) for s in segments)
        done_n = 0
        for i, seg in enumerate(segments):
            sentences = vp.split_into_sentences(seg["text"]) or [seg["text"]]
            seg_start = t
            for j, sentence in enumerate(sentences):
                if hasattr(self.progress, "check_cancel"):
                    self.progress.check_cancel()
                wav, engine_id = synth(voice, sentence)
                engines.add(engine_id)
                samples, sr = vp._wav_bytes_to_float(wav)
                samples = vp._resample(samples, sr, SAMPLE_RATE)
                s_start, s_end = t, t + len(samples) / SAMPLE_RATE
                chunks.append(samples)
                words.extend(dict(w, segment=i) for w in video_mod.estimate_word_times(sentence, s_start + 0.03,
                                                                                        max(s_start + 0.06, s_end - 0.05)))
                t = s_end
                gap = SENTENCE_GAP_S if j < len(sentences) - 1 else (SEGMENT_GAP_S if i < len(segments) - 1 else 0.0)
                if gap:
                    chunks.append(np.zeros(int(gap * SAMPLE_RATE), dtype=np.float32))
                    t += gap
                done_n += 1
                self.tick(self.stage_fraction(0.8 * done_n / max(1, n_sent)), f"narration: {done_n}/{n_sent} sentences")
            timed.append({"start_s": round(seg_start, 3), "end_s": round(t, 3)})
        samples = np.concatenate(chunks)
        duration = len(samples) / SAMPLE_RATE
        asset_id = new_id("a")
        dest = self.store.path_for_asset_file(asset_id, ".wav")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(wav_bytes_mono(samples, SAMPLE_RATE))
        asset = self.store.create_asset(
            project_id=self.project_id, kind="audio", file_path=engine._rel(self.store, dest), mime="audio/wav",
            duration_s=round(duration, 3), source="generated", asset_id=asset_id,
            name=f"Narration - {script.get('title') or self.state['name']}"[:100], tags=["narration", "short"],
            recipe={"operation": "short_narration", "production": self.slug, "voice": voice, "engines": sorted(engines),
                    "text": " ".join(s["text"] for s in segments)[:4000], "created_at": now_iso()})
        timing, matched = "estimated", None
        transcribe = getattr(self.studio, "transcribe", None)
        if transcribe is not None:
            try:
                heard = transcribe(dest, self.spec["language"])
            except Exception as exc:  # noqa: BLE001 - STT is an optional refinement
                heard = None
                self.log("stt_failed", message=str(exc)[:200])
            if heard:
                aligned, matched = align_words(words, heard)
                if matched >= 0.5:
                    words, timing = aligned, "speech-to-text"
        self.state["done"]["narration"] = {
            "asset_id": asset["id"], "duration_s": round(duration, 3), "engines": sorted(engines), "timing": timing,
            "matched": matched, "segments": timed, "words": [{k: w[k] for k in ("text", "start_s", "end_s")} for w in words]}
        for seg, tm in zip(segments, timed):
            seg.update(tm)
        self.log("narration", asset_id=asset["id"], duration_s=round(duration, 1), timing=timing, engines=sorted(engines))

    def stage_music(self) -> None:
        music = self.spec.get("music") or {}
        mode = music.get("mode", "none")
        narration = self.state["done"]["narration"]
        if mode == "none":
            self.state["done"]["music"] = {"skipped": True}
            return
        if mode == "asset":
            if not prod._asset_ok(self.store, music["asset_id"]):
                raise ShortError("missing_music", f"music asset {music['asset_id']} is gone")
            asset = prod.copy_asset(self.store, music["asset_id"], self.project_id)
            self.state["done"]["music"] = {"asset_id": asset["id"], "mode": mode, "from_asset_id": music["asset_id"]}
            return
        if mode == "library":
            folder = self.store.data_dir / "music"
            tracks = sorted(p for p in folder.glob("*") if p.suffix.lower() in MUSIC_EXTS) if folder.is_dir() else []
            if not tracks:
                raise ShortError("no_music_library", f"music.mode 'library' picks a track from {folder}; put some there first")
            pick = tracks[random.Random(self.spec.get("seed", 0)).randrange(len(tracks))]
            asset = engine.import_asset(self.store, self.project_id, pick, kind_hint="audio",
                                        recipe={"operation": "music_library", "file": pick.name, "created_at": now_iso()},
                                        tags=["music", "short"])
            self.state["done"]["music"] = {"asset_id": asset["id"], "mode": mode, "file": pick.name}
            return
        pending = self.pending("music")
        partial = self.state["partial"].setdefault("music", {})
        if "take" not in pending and not partial.get("asset_ids"):
            body = {"tags": music["tags"], "lyrics": "[Instrumental]", "bpm": music.get("bpm", 90),
                    "duration": float(min(240, max(10, narration["duration_s"] + 3))), "seed": music.get("seed", 7000 + self.spec.get("seed", 0)),
                    "count": 1, "language": "en"}
            pending["take"] = self.studio.compose(self.project_id, body)["id"]
            self.save()

        def done(_key: str, job: dict[str, Any]) -> None:
            partial["asset_ids"] = prod._asset_ids(job)

        self.wait_jobs("music", done, "music")
        ids = partial.get("asset_ids") or []
        if not ids:
            raise ShortError("no_music", "the music job produced no audio")
        self.state["done"]["music"] = {"asset_id": ids[0], "mode": mode, "tags": music["tags"]}

    def _gen_body(self, seg: dict[str, Any], shot: dict[str, Any], n: int) -> dict[str, Any]:
        visuals = self.spec["visuals"]
        framing = FRAMINGS[(shot["index"] + shot["segment"]) % len(FRAMINGS)] if shot["index"] else ""
        prompt = ", ".join(p for p in (seg.get("visual") or seg["text"], framing, visuals.get("look")) if p)
        return {"prompt": prompt, "negative": visuals.get("negative") or None, "aspect": self._aspect(), "count": 1,
                "seed": 100_000 + 1000 * self.spec.get("seed", 0) + 17 * n, "engine": self.spec.get("engine")}

    def stage_visuals(self) -> None:
        segments = self._segments()
        narration = self.state["done"]["narration"]
        total = narration["duration_s"] + TAIL_S
        visuals = self.spec["visuals"]
        entry = self.state["done"].setdefault("visuals", {"complete": False, "items": {}})
        if not entry.get("plan"):
            entry["plan"] = plan_shots(segments, total, visuals["shot_s"])
            self.save()
        items = self.items("visuals")
        pending = self.pending("visuals")
        keys = self._stock_keys()
        stock_ready = any(p in keys for p in visuals["providers"])
        if visuals["source"] == "stock" and not stock_ready:
            self.log("stock_not_configured", note="no Pexels/Pixabay key: generating the pictures instead")
        used = {i.get("ref") for i in items.values() if i.get("ref")}
        client_fn = getattr(self.studio, "stock_client", None)
        client = client_fn() if client_fn else None
        orientation = stock.aspect_orientation(self._aspect())
        searches: dict[int, list[dict[str, Any]]] = {}
        try:
            for n, shot in enumerate(entry["plan"]):
                key = shot["key"]
                if key in items or key in pending:
                    continue
                seg = segments[shot["segment"]]
                source = shot_source(visuals["source"], seg, n, stock_ready)
                if source == "stock":
                    got = self._stock_shot(keys, seg, shot, n, used, orientation, client, searches)
                    if got:
                        items[key] = got
                        used.add(got["ref"])
                        self.save()
                        self.tick(self.stage_fraction(0.9 * len(items) / len(entry["plan"])),
                                  f"visuals: {len(items)}/{len(entry['plan'])}")
                        continue
                    self.log("stock_fallback", key=key, query=seg.get("query"))
                pending[key] = self.studio.generate(self.project_id, self._gen_body(seg, shot, n))["id"]
                self.save()
        finally:
            if client is not None and hasattr(client, "close") and not getattr(self.studio, "keep_stock_client", False):
                client.close()

        def done(key: str, job: dict[str, Any]) -> None:
            ids = prod._asset_ids(job)
            if ids:
                items[key] = {"asset_id": ids[0], "source": "generate", "kind": "image"}
                self.log("generated_shot", key=key, asset_id=ids[0])

        self.wait_jobs("visuals", done, "pictures")
        missing = [s["key"] for s in entry["plan"] if s["key"] not in items]
        if missing:
            raise ShortError("missing_visuals", f"no picture for shot(s) {', '.join(missing)}")
        entry["complete"] = True

    def _stock_shot(self, keys: dict[str, str], seg: dict[str, Any], shot: dict[str, Any], n: int, used: set[str],
                    orientation: str, client: Any, searches: dict[int, list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
        visuals = self.spec["visuals"]
        seg_i = shot["segment"]
        if seg_i not in searches:
            query = seg.get("query") or keywords(seg["text"])
            try:
                found = stock.search(keys, query, kind=visuals["stock_kind"], orientation=orientation,
                                     providers=visuals["providers"], per_page=15, min_duration_s=0, client=client)["items"]
            except stock.StockError as exc:
                self.log("stock_search_failed", key=shot["key"], query=query, message=exc.message)
                found = []
            # the variant seed rotates the results, so two variants of the
            # same short pick different footage
            if found:
                r = self.spec.get("seed", 0) % len(found)
                found = found[r:] + found[:r]
            searches[seg_i] = found
        candidates = [it for it in searches[seg_i] if it["ref"] not in used]
        if visuals["stock_kind"] == "video":
            long_enough = [it for it in candidates if not it.get("duration_s") or it["duration_s"] >= shot["duration_s"]]
            candidates = long_enough or candidates
        if not candidates:
            return None
        item = candidates[0]
        short_side = 1080 if "final" in (self.spec.get("timeline") or {}).get("qualities", []) else 720
        try:
            asset = stock.fetch(self.store, self.project_id, item, short_side=short_side,
                                query=seg.get("query"), client=client,
                                should_cancel=getattr(self.progress, "cancelled", None))
        except stock.StockError as exc:
            self.log("stock_fetch_failed", key=shot["key"], ref=item["ref"], message=exc.message)
            used.add(item["ref"])
            return self._stock_shot(keys, seg, shot, n, used, orientation, client, searches) if len(candidates) > 1 else None
        except engine.EngineError as exc:
            self.log("stock_fetch_failed", key=shot["key"], ref=item["ref"], message=exc.message)
            used.add(item["ref"])
            return None
        self.log("stock_shot", key=shot["key"], ref=item["ref"], asset_id=asset["id"], author=item.get("author"))
        return {"asset_id": asset["id"], "source": "stock", "kind": asset["kind"], "ref": item["ref"],
                "duration_s": asset.get("duration_s")}

    def _clip_targets(self) -> list[str]:
        n = int(self.spec["visuals"].get("clips") or 0)
        if not n:
            return []
        plan = {s["key"]: s for s in (self.state["done"].get("visuals") or {}).get("plan") or []}
        gen = [k for k, v in self.items("visuals").items() if v.get("source") == "generate" and v.get("kind") == "image"]
        gen.sort(key=lambda k: -plan.get(k, {}).get("duration_s", 0))
        return gen[:n]

    def stage_animatic(self) -> Optional[str]:
        settings = self.state.get("settings") or {}
        targets = self._clip_targets()
        if not targets or not settings.get("animatic", True):
            self.state["done"]["animatic"] = {"skipped": True, "reason": "no clips to render" if not targets else "off"}
            return None
        aspect = self._aspect()
        tracks = self.build_tracks(aspect, use_clips=False, audio_duration=self.state["done"]["narration"]["duration_s"] + TAIL_S)
        from . import animatic as animatic_mod

        cut = {"tracks": tracks, "fps": 30, "song_asset_id": self.state["done"]["narration"]["asset_id"]}
        asset = animatic_mod.render(self.store, {**self.state, "spec": {"timeline": {"finishing": self._finishing()}}}, cut,
                                    aspect, lambda frac, msg=None: self.tick(self.stage_fraction(frac), msg or "animatic"),
                                    getattr(self.progress, "cancelled", None))
        plan = {"clips_planned": len(targets), "gpu_minutes": round(len(targets) * 9.5, 1), "clip_keys": targets}
        self.state["done"]["animatic"] = {"renders": {aspect: asset["id"]}, "plan": plan}
        self.log("animatic", renders={aspect: asset["id"]}, clips_planned=len(targets))
        if settings.get("animatic_autocontinue") or (self.state.get("review") or {}).get("animatic_approved"):
            return None
        return "pause"

    def stage_clips(self) -> None:
        targets = self._clip_targets()
        items = self.items("clips")
        if not targets:
            self.state["done"]["clips"] = {"skipped": True, "items": {}}
            return
        pending = self.pending("clips")
        visuals = self.items("visuals")
        for n, key in enumerate(targets):
            if key in items or key in pending:
                continue
            body = {"template": "wan22_ti2v", "reference_asset_id": visuals[key]["asset_id"],
                    "prompt": self.spec["visuals"].get("motion_prompt") or MOTION_PROMPT,
                    "seed": 200_000 + 1000 * self.spec.get("seed", 0) + n}
            pending[key] = self.studio.generate(self.project_id, body)["id"]
            self.save()

        def done(key: str, job: dict[str, Any]) -> None:
            ids = prod._asset_ids(job)
            if ids:
                items[key] = ids[0]
                self.log("clip", key=key, asset_id=ids[0])

        self.wait_jobs("clips", done, "clips")
        self.state["done"]["clips"]["complete"] = True

    def stage_mix(self) -> None:
        narration = self.state["done"]["narration"]
        music = self.state["done"].get("music") or {}
        spec_music = self.spec.get("music") or {}
        voice_path = self.store.data_dir / self.store.get_asset(narration["asset_id"])["file_path"]
        music_path = self.store.data_dir / self.store.get_asset(music["asset_id"])["file_path"] if music.get("asset_id") else None
        tmp = self.store.data_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        out = tmp / f"mix_{new_id('x')}.m4a"
        try:
            soundtrack.mix(voice_path, music_path, out, narration["duration_s"], spec_music.get("volume_db", soundtrack.DEFAULT_MUSIC_DB),
                           spec_music.get("duck", "normal"))
            asset = engine.import_asset(
                self.store, self.project_id, out, kind_hint="audio", source="derived", tags=["soundtrack", "short"],
                recipe={"operation": "short_mix", "production": self.slug, "input_asset_ids": [a for a in (narration["asset_id"], music.get("asset_id")) if a],
                        "music_db": spec_music.get("volume_db"), "duck": spec_music.get("duck"), "loudness_lufs": soundtrack.TARGET_LUFS,
                        "created_at": now_iso()})
        except soundtrack.MixError as exc:
            raise ShortError("mix_failed", str(exc)) from None
        finally:
            out.unlink(missing_ok=True)
        self.store.update_asset(asset["id"], name=f"Soundtrack - {self.state['done']['script'].get('title') or self.state['name']}"[:100])
        self.state["done"]["mix"] = {"asset_id": asset["id"], "duration_s": asset.get("duration_s")}
        self.log("mix", asset_id=asset["id"], music=music.get("asset_id"))

    def _finishing(self) -> dict[str, Any]:
        tl = self.spec.get("timeline") or {}
        finishing = dict(tl.get("finishing") or {})
        style = (self.spec.get("captions") or {}).get("style", "bold")
        if style not in ("none", "default"):
            finishing["lyric_style"] = style
        return finishing

    def build_tracks(self, aspect: str, use_clips: bool = True, audio_duration: Optional[float] = None) -> list[dict[str, Any]]:
        """The visual track (one clip per planned shot, on the narration's
        clock) and the caption track."""
        visuals = self.state["done"]["visuals"]
        plan = visuals["plan"]
        items = visuals["items"]
        clips = self.items("clips") if use_clips else {}
        rng = random.Random(f"{self.slug}:{self.spec.get('seed', 0)}")
        total = audio_duration or (self.state["done"].get("mix") or {}).get("duration_s") or plan[-1]["start_s"] + plan[-1]["duration_s"]
        crossfade = (self.spec.get("timeline") or {}).get("transitions") == "crossfade"
        out, t = [], 0.0
        pans = ["left", "right", "up", "down"]
        for i, shot in enumerate(plan):
            dur = shot["duration_s"] if i < len(plan) - 1 else max(0.5, total - t)
            item = items[shot["key"]]
            clip_id = clips.get(shot["key"])
            asset_id = clip_id or item["asset_id"]
            asset = self.store.get_asset(asset_id)
            new_segment = i > 0 and shot["segment"] != plan[i - 1]["segment"]
            transition = ({"type": "crossfade", "duration_s": round(min(0.3, dur / 3, out[-1]["duration_s"] / 3), 3)}
                          if crossfade and new_segment and dur >= 0.9 and out and out[-1]["duration_s"] >= 0.9
                          else {"type": "cut", "duration_s": 0.0})
            clip: dict[str, Any] = {"asset_id": asset_id, "kind": asset["kind"], "duration_s": round(dur, 3),
                                    "trim_start_s": 0.0, "transition_in": transition}
            if asset["kind"] == "video":
                length = float(asset.get("duration_s") or 0)
                spare = max(0.0, length - dur - 0.2)
                lead = 1.0 if clip_id else 0.0  # a Wan clip opens on its still
                clip["trim_start_s"] = round(min(spare, lead + rng.random() * max(0.0, spare - lead)), 2) if spare else 0.0
            else:
                zoom_in = rng.random() < 0.75
                amount = round(rng.uniform(1.08, 1.16), 3)
                clip["ken_burns"] = {"zoom_start": 1.0 if zoom_in else amount, "zoom_end": amount if zoom_in else 1.0,
                                     "pan": pans[i % len(pans)]}
            out.append(clip)
            t += dur
        tracks: list[dict[str, Any]] = [{"type": "visual", "clips": out}]
        captions = self.spec.get("captions") or {}
        if captions.get("style", "bold") != "none":
            words = self.state["done"]["narration"]["words"]
            lyric = caption_clips(words, captions.get("max_words", 3), total_s=total)
            if lyric:
                tracks.append({"type": "lyrics", "clips": lyric})

        def lookup(aid: str) -> Optional[dict[str, Any]]:
            try:
                return self.store.get_asset(aid)
            except prod.NotFound:
                return None

        try:
            return timeline_mod.normalise_tracks(tracks, lookup)
        except timeline_mod.TimelineError as exc:
            raise ShortError("bad_timeline", str(exc)) from None

    def stage_timeline(self) -> None:
        tl = self.spec.get("timeline") or {}
        mix = self.state["done"]["mix"]
        entry = self.state["done"].setdefault("timeline", {"complete": False, "timelines": {}})
        timelines = entry.setdefault("timelines", {})
        pending = self.pending("timeline")
        title = self.state["done"]["script"].get("title") or self.state["name"]
        for aspect in tl.get("aspects") or ["9:16"]:
            info = timelines.get(aspect)
            if info is None:
                width, height = timeline_mod.ASPECTS[aspect]
                built = self.store.create_timeline(self.project_id, name=f"Short - {title}"[:100], aspect=aspect, fps=30,
                                                   width=width, height=height, audio_asset_id=mix["asset_id"],
                                                   tracks=self.build_tracks(aspect, audio_duration=mix.get("duration_s")),
                                                   finishing=video_mod.validate_finishing(self._finishing()))
                info = timelines[aspect] = {"timeline_id": built["id"], "renders": {}}
                self.log("timeline", aspect=aspect, timeline_id=built["id"])
                self.save()
            for quality in tl.get("qualities") or ["preview"]:
                key = f"{aspect}|{quality}"
                if quality in info["renders"] or key in pending:
                    continue
                pending[key] = self.studio.render(info["timeline_id"], quality)["id"]
                self.save()

        def done(key: str, job: dict[str, Any]) -> None:
            aspect, quality = key.split("|", 1)
            ids = prod._asset_ids(job)
            if ids:
                timelines[aspect]["renders"][quality] = ids[0]
                self.log("render", aspect=aspect, quality=quality, asset_id=ids[0])

        self.wait_jobs("timeline", done, "renders")
        entry["complete"] = True

    def stage_report(self) -> None:
        path = write_report(self.store, self.state)
        self.state["done"]["report"] = {"path": path.name, "publish": publish_text(self.store, self.state)}


# ------------------------------------------------------------- outputs

def stock_assets(store: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in ((state.get("done") or {}).get("visuals") or {}).get("items", {}).values():
        if item.get("source") == "stock":
            try:
                out.append(store.get_asset(item["asset_id"]))
            except prod.NotFound:
                continue
    return out


def publish_text(store: Any, state: dict[str, Any]) -> str:
    """Title, description, hashtags and credits, ready to paste."""
    script = (state.get("done") or {}).get("script") or {}
    lines = [script.get("title") or state.get("name") or ""]
    if script.get("description"):
        lines += ["", script["description"]]
    if script.get("hashtags"):
        lines += ["", " ".join(f"#{t}" for t in script["hashtags"])]
    credits = stock.credits(stock_assets(store, state))
    if credits:
        lines += ["", "Footage:"] + [f"- {c}" for c in credits]
    text = "\n".join(lines).strip() + "\n"
    path = prod.production_dir(store.data_dir, state["slug"]) / "publish.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def write_report(store: Any, state: dict[str, Any]) -> Path:
    spec = state.get("spec") or {}
    done = state.get("done") or {}
    script = done.get("script") or {}
    lines = [f"# {script.get('title') or state.get('name')} - short report", "",
             f"Production `{state['slug']}` - project `{state.get('project_id')}` - status **{state.get('status')}**", "",
             f"- Topic: {spec.get('topic') or '(script given)'}", f"- Language: {spec.get('language')}; script: {script.get('source', '-')}",
             ""]
    narration = done.get("narration") or {}
    if narration:
        lines += ["## Narration", "", f"- `{narration.get('asset_id')}` - {narration.get('duration_s')} s, "
                  f"voice engine(s) {', '.join(narration.get('engines') or [])}, word timing {narration.get('timing')}"
                  + (f" ({int(100 * narration['matched'])}% of the words heard)" if narration.get("matched") is not None else ""), ""]
    if script.get("segments"):
        lines += ["## Script", "", "| # | time | narration | picture |", "| --- | --- | --- | --- |"]
        for i, seg in enumerate(script["segments"], start=1):
            when = f"{seg.get('start_s', '-')}-{seg.get('end_s', '-')}" if seg.get("start_s") is not None else "-"
            lines.append(f"| {i} | {when} | {prod._clip_text(seg.get('text'), 140)} | {prod._clip_text(seg.get('visual'), 80)} |")
        lines.append("")
    visuals = done.get("visuals") or {}
    if visuals.get("plan"):
        items = visuals.get("items") or {}
        clips = (done.get("clips") or {}).get("items") or {}
        lines += ["## Shots", "", "| shot | start | s | source | asset | clip |", "| --- | --- | --- | --- | --- | --- |"]
        for shot in visuals["plan"]:
            it = items.get(shot["key"]) or {}
            lines.append(f"| {shot['key']} | {shot['start_s']} | {shot['duration_s']} | {it.get('source', '-')}"
                         f"{' ' + it['ref'] if it.get('ref') else ''} | `{it.get('asset_id', '-')}` | {clips.get(shot['key'], '-')} |")
        lines.append("")
    music = done.get("music") or {}
    mix = done.get("mix") or {}
    lines += ["## Sound", "", f"- Music: {music.get('asset_id') or 'none'} ({music.get('mode') or 'none'})",
              f"- Mix: `{mix.get('asset_id')}` (voice + ducked music, {soundtrack.TARGET_LUFS} LUFS)", ""]
    timeline = done.get("timeline") or {}
    if timeline.get("timelines"):
        lines += ["## Renders", ""]
        for aspect, info in timeline["timelines"].items():
            lines.append(f"- {aspect}: timeline `{info.get('timeline_id')}`, renders {info.get('renders')}")
        lines.append("")
    credits = stock.credits(stock_assets(store, state))
    if credits:
        lines += ["## Stock credits", ""] + [f"- {c}" for c in credits] + [""]
    if state.get("timings"):
        lines += ["## Timings", "", "| Stage | seconds |", "| --- | --- |"]
        lines += [f"| {k} | {v} |" for k, v in state["timings"].items()]
        lines.append("")
    path = prod.production_dir(store.data_dir, state["slug"]) / "REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def compact_extras(state: dict[str, Any]) -> dict[str, Any]:
    done = state.get("done") or {}
    script = done.get("script") or {}
    out: dict[str, Any] = {"kind": KIND, "topic": (state.get("spec") or {}).get("topic") or None}
    if script:
        out["title"] = script.get("title")
        out["segments"] = len(script.get("segments") or [])
        out["script_source"] = script.get("source")
    narration = done.get("narration") or {}
    if narration:
        out["narration"] = {"asset_id": narration.get("asset_id"), "duration_s": narration.get("duration_s"),
                            "timing": narration.get("timing")}
    visuals = done.get("visuals") or {}
    if visuals.get("items"):
        sources: dict[str, int] = {}
        for it in visuals["items"].values():
            sources[it.get("source", "?")] = sources.get(it.get("source", "?"), 0) + 1
        out["shots"] = {"planned": len(visuals.get("plan") or []), **sources}
    if (done.get("mix") or {}).get("asset_id"):
        out["soundtrack_asset_id"] = done["mix"]["asset_id"]
    if (done.get("report") or {}).get("publish"):
        out["publish"] = done["report"]["publish"][:1200]
    if state.get("status") == "awaiting_review":
        if (state.get("review") or {}).get("script_approved") is None and done.get("script") and not done.get("narration"):
            out["next"] = ("read the script (studio_production_script to see or change it), then studio_production_continue "
                           "to voice it")
        else:
            out["next"] = "watch the animatic, then studio_production_continue to render the clips"
    return out


# ---------------------------------------------------------------- edits

AFTER_SCRIPT = ("narration", "visuals", "animatic", "clips", "mix", "timeline", "report")


def replace_script(data_dir: Path, slug: str, script: Any) -> dict[str, Any]:
    """New script text or segments: everything that depends on it is redone
    on the next run (the music is kept - the mix loops or trims it)."""
    with prod.lock_for(slug):
        state = prod.load_state(data_dir, slug)
        if not is_short(state):
            raise ShortError("not_a_short", f"'{slug}' is not a narrated short")
        if state.get("status") == "running":
            raise ShortError("production_running", "the short is running; cancel it or wait before changing its script")
        clean = normalise_script(script)
        old = (state.get("done") or {}).get("script") or {}
        for key in ("title", "description", "hashtags"):
            if key not in clean and old.get(key):
                clean[key] = old[key]
        state["spec"]["script"] = clean
        for stage in ("script", *AFTER_SCRIPT):
            state["done"].pop(stage, None)
            state.get("partial", {}).pop(stage, None)
        state.setdefault("review", {})["script_approved"] = True
        state.setdefault("review", {}).pop("animatic_approved", None)
        prod.log(state, "script", "script_replaced", segments=len(clean["segments"]))
        prod.save_state(data_dir, state)
    return state


def create_short(data_dir: Path, name: str, spec: dict[str, Any], settings: Optional[dict[str, Any]] = None,
                 project_id: Optional[str] = None) -> dict[str, Any]:
    spec = normalise_spec(spec)
    settings = dict(settings or {})
    script_review = bool(settings.pop("script_review", False))
    clean = prod.normalise_settings(settings)
    clean["script_review"] = script_review
    if not isinstance(name, str) or not name.strip():
        raise ShortError("name_required", "a short needs a name")
    slug = prod.unique_slug(data_dir, name)
    state = prod.new_state(slug, name.strip()[:120], spec, clean, project_id)
    state["kind"] = KIND
    state["stages"] = list(SHORT_STAGES)
    prod.save_state(data_dir, state)
    return state


def create_batch(data_dir: Path, name: str, spec: dict[str, Any], settings: Optional[dict[str, Any]] = None,
                 project_id: Optional[str] = None, count: int = 1) -> list[dict[str, Any]]:
    """`count` variants of one short: the same brief with different seeds
    (other footage, other generated pictures and, without a fixed script,
    another script)."""
    count = int(count)
    if not 1 <= count <= 8:
        raise ShortError("bad_count", "count must be between 1 and 8")
    base = int((spec or {}).get("seed") or 0)
    out = []
    for k in range(count):
        variant = dict(spec or {}, seed=base + 101 * k)
        out.append(create_short(data_dir, name if count == 1 else f"{name} #{k + 1}", variant, settings, project_id))
    return out
