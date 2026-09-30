"""Plan a music video from a concept: the local language model drafts the
shot list (and the song's tags and lyrics when it is to be composed), the
person edits the draft, and `spec_from_draft` turns it into the spec of an
ordinary production (character -> song -> stills -> animatic -> clips ->
cut), the same pipeline an agent drives over MCP.

The draft is plain data the Productions screen shows and edits:

    {"title", "world_look", "world_negative",
     "song": {"tags", "lyrics", "bpm", "key", "language", "duration"},
     "shots": [{"prompt", "lead", "motion", "motion_prompt", "section"}]}
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from .productions import ProductionError

# A shot plan with lyrics is a few thousand tokens; a 27B split over four
# cards writes ~10 tokens/s, so the call gets minutes, not Hoard Link's 120 s.
WRITER_TIMEOUT_S = 900.0


# A healthy server starts answering a ~1.5k-token prompt in seconds and then
# never goes quiet for long. When a render has filled the cards the model
# lives on, prompt processing crawls to a halt instead: the call would sit
# there for the whole timeout. Streaming lets the planner notice.
WRITER_FIRST_TOKEN_S = 240.0
WRITER_STALL_S = 120.0


class WriterStalled(ProductionError):
    """The language model accepted the request and then stopped making progress."""


def _stream_text(lines: Any, api: str) -> Any:
    """The text pieces of a streamed chat reply (Ollama NDJSON or OpenAI SSE);
    yields "" for keep-alive lines so the caller's clock still sees traffic."""
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        if api == "ollama":
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            yield str((obj.get("message") or {}).get("content") or "")
            if obj.get("done"):
                return
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
        # content only: a reasoning model's thinking must not pass for progress on the JSON
        yield str(delta.get("content") or "")


def writer_chat(backend: Any, timeout_s: float = WRITER_TIMEOUT_S, first_token_s: float = WRITER_FIRST_TOKEN_S,
                stall_s: float = WRITER_STALL_S, busy: Optional[Callable[[], str]] = None
                ) -> Callable[[list[dict[str, Any]], int, float], str]:
    """A chat call for long structured writing: the language model Hoard Link
    resolves, asked directly with thinking off (a reasoning model would
    otherwise spend the whole token budget thinking before the JSON), a long
    overall timeout, and a stall watchdog: no first word within
    ``first_token_s`` or silence for ``stall_s`` ends the call with
    `WriterStalled` (closing the stream also frees the server's slot).
    ``busy()`` may name what is holding the GPUs, for the message."""
    import time

    import httpx

    from .hoard_link.errors import Unavailable
    from .hoard_link.link import _ollama_endpoint, _openai_endpoint

    def chat(messages: list[dict[str, Any]], max_tokens: int, temperature: float) -> str:
        res = backend.link.sync.resolve("llm")
        if not res.resolved or not res.url:
            raise Unavailable("llm", (res.details or {}).get("reasons") or [res.reason or "no language model"])
        if res.api == "ollama":
            url = _ollama_endpoint(res.url, "/api/chat")
            payload: dict[str, Any] = {"model": res.model, "messages": messages, "stream": True, "think": False,
                                       "options": {"temperature": temperature, "num_predict": max_tokens}}
        else:
            url = _openai_endpoint(res.url, "/chat/completions")
            payload = {"model": res.model, "messages": messages, "stream": True, "max_tokens": max_tokens,
                       "temperature": temperature, "chat_template_kwargs": {"enable_thinking": False},
                       "reasoning_budget": 0}
        started = time.monotonic()
        last = None  # time of the last piece of text
        parts: list[str] = []

        def stalled(waited: float, first: bool) -> WriterStalled:
            why = ""
            try:
                why = busy() if busy else ""
            except Exception:  # noqa: BLE001 - the diagnosis is a nicety
                why = ""
            what = (f"did not start answering within {int(waited)} s" if first
                    else f"stopped writing for {int(waited)} s")
            return WriterStalled("llm_stalled", f"The language model ({res.model}) {what}. " + (
                why or "Its GPUs are probably busy with a render or another request.") +
                " Wait for that to finish (or stop it) and try again, or write the shots yourself.")

        # the read timeout is the watchdog: a server that sends nothing at all
        # (prompt processing stuck) trips it without a byte arriving
        read_timeout = max(5.0, first_token_s, stall_s)
        try:
            with httpx.stream("POST", url, json=payload,
                              timeout=httpx.Timeout(timeout_s, connect=10.0, read=read_timeout)) as r:
                r.raise_for_status()
                for piece in _stream_text(r.iter_lines(), res.api):
                    now = time.monotonic()
                    if piece:
                        parts.append(piece)
                        last = now
                    if last is None and now - started > first_token_s:
                        raise stalled(now - started, True)
                    if last is not None and now - last > stall_s:
                        raise stalled(now - last, False)
                    if now - started > timeout_s:
                        raise WriterStalled("llm_timeout", f"The language model took more than {int(timeout_s)} s.")
        except httpx.ReadTimeout:
            now = time.monotonic()
            raise stalled(now - (last or started), last is None) from None
        return "".join(parts)

    return chat

LANGUAGE_NAMES = {"en": "English", "es": "Spanish (Spain)", "fr": "French", "it": "Italian", "pt": "Portuguese",
                  "de": "German", "ja": "Japanese", "ko": "Korean", "zh": "Chinese"}
SECTIONS = ("intro", "verse", "prechorus", "chorus", "bridge", "breakdown", "outro")


def plan_messages(concept: str, lead_name: str, lead_look: str, n_shots: int, language: str,
                  compose_song: bool, duration_s: float, lyrics: Optional[str] = None,
                  genre: Optional[str] = None) -> list[dict[str, Any]]:
    lang = LANGUAGE_NAMES.get(language, language)
    song_part = (
        "Also write the song: \"song\": {\"tags\": comma-separated sound description for a music model (genre, "
        "tempo feel, instruments, vocal type and mood, in English), \"lyrics\": the full lyrics with section tags "
        "on their own lines ([Intro], [Verse], [Pre-Chorus], [Chorus], [Bridge], [Outro]), "
        f"sized for about {int(duration_s)} seconds, \"bpm\": a number, \"key\": like \"A minor\"}}.\n"
        f"IMPORTANT: every sung line of the lyrics is in {lang}"
        + (" (only the section tags stay in English)" if language != "en" else "") + ".\n"
        + (f"The sound should be: {genre}.\n" if genre else "")
        if compose_song else
        ("The song already exists. Its lyrics, to follow section by section:\n" + lyrics.strip() + "\n"
         if lyrics and lyrics.strip() else "The song already exists; plan the shots for its usual sections.\n")
    )
    system = ("You are a music video director. You plan shot lists for an image-then-video generator. "
              "You answer with one JSON object and nothing else.")
    user = (
        f"Concept: {concept.strip()}\n"
        f"Lead character: {lead_name} - {lead_look.strip() or 'use the reference image'}\n"
        f"Plan {n_shots} shots.\n"
        "Rules for every shot:\n"
        "- \"prompt\": English, one concrete filmable frame: setting, action or pose, framing (wide/medium/close-up), "
        "light and colour. When the lead is in the shot, describe only the scene, pose and action, not the "
        "character's design (it comes from the reference image). No text, logos or captions in the picture.\n"
        "- \"lead\": true when the lead character is in the frame (most shots), false for scenery, crowd or detail shots.\n"
        "- \"motion\": \"move\" or \"still\"; \"motion_prompt\": English, what moves in the clip (camera and subject), "
        "short.\n"
        f"- \"section\": the song section it illustrates, one of {', '.join(SECTIONS)}.\n"
        "Vary framing and places so the cut does not repeat itself; keep one visual world.\n"
        "Also give \"title\" (short), \"world_look\" (English: the look shared by every shot - palette, era, "
        "lighting, lens) and \"world_negative\" (English: what to avoid).\n"
        + song_part
        + "Answer exactly in this shape: {\"title\": \"...\", \"world_look\": \"...\", \"world_negative\": \"...\", "
        + ("\"song\": {\"tags\": \"...\", \"lyrics\": \"...\", \"bpm\": 120, \"key\": \"...\"}, " if compose_song else "")
        + "\"shots\": [{\"prompt\": \"...\", \"lead\": true, \"motion\": \"move\", \"motion_prompt\": \"...\", "
        "\"section\": \"verse\"}]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _json_object(text: str) -> dict[str, Any]:
    raw = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ProductionError("plan_unreadable", "the model's reply held no JSON plan")
    # a bare list of shots ("[{...}, {...}]") is read as {"shots": [...]}
    lb = raw.find("[")
    if 0 <= lb < start and raw.rfind("]") > end:
        start, end, wrap = lb, raw.rfind("]"), True
    else:
        wrap = False
    blob = raw[start:end + 1]
    for attempt in (blob, re.sub(r",\s*([}\]])", r"\1", blob)):
        try:
            data = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if wrap and isinstance(data, list):
            return {"shots": data}
        if isinstance(data, dict):
            return data
    raise ProductionError("plan_unreadable", "the model's JSON plan did not parse (a reply cut short by the "
                                             "token limit looks like this: try fewer shots)")


# what models call the frame description when they do not use "prompt"
_PROMPT_KEYS = ("prompt", "image_prompt", "description", "visual", "scene", "frame", "shot", "text")
_SHOT_LIST_KEYS = ("shots", "shot_list", "shotlist", "scenes", "frames", "storyboard")


def _find_shots(data: Any, depth: int = 0) -> list:
    """The list of shots wherever the model put it: "shots" at the top (the
    asked-for shape), another usual name, or one level down inside a wrapper
    object ({"music_video": {"shots": [...]}})."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in _SHOT_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, list) and value:
            return value
    if depth < 2:
        for value in data.values():
            if isinstance(value, (dict, list)):
                found = _find_shots(value, depth + 1)
                if found and all(isinstance(x, dict) for x in found):
                    return found
    return []


def _shot_prompt(s: dict[str, Any]) -> str:
    for key in _PROMPT_KEYS:
        value = s.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalise_draft(data: Any, compose_song: bool, language: str, duration_s: float) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProductionError("bad_draft", "the draft must be an object")
    shots = []
    for i, s in enumerate(_find_shots(data)):
        if isinstance(s, str) and s.strip():
            s = {"prompt": s}
        if not isinstance(s, dict) or not _shot_prompt(s):
            continue
        section = str(s.get("section") or "").lower().replace("-", "").replace(" ", "")
        shots.append({"prompt": _shot_prompt(s)[:600], "lead": bool(s.get("lead", True)),
                      "motion": "still" if str(s.get("motion")) == "still" else "move",
                      "motion_prompt": str(s.get("motion_prompt") or "subtle motion").strip()[:200],
                      "section": section if section in SECTIONS else ""})
    if not shots:
        raise ProductionError("bad_draft", "the draft has no shots")
    draft: dict[str, Any] = {"title": str(data.get("title") or "").strip()[:80],
                             "world_look": str(data.get("world_look") or "").strip()[:400],
                             "world_negative": str(data.get("world_negative") or "").strip()[:300],
                             "shots": shots[:40]}
    song = data.get("song") if isinstance(data.get("song"), dict) else None
    if compose_song:
        song = song or {}
        try:
            bpm = int(float(song.get("bpm") or 120))
        except (TypeError, ValueError):
            bpm = 120
        draft["song"] = {"tags": str(song.get("tags") or "").strip()[:400], "lyrics": str(song.get("lyrics") or "").strip(),
                         "bpm": max(40, min(220, bpm)), "key": str(song.get("key") or "C major").strip()[:20],
                         "language": language, "duration": float(duration_s)}
    return draft


_EN_WORDS = {"the", "and", "you", "i", "my", "me", "to", "in", "of", "is", "it", "we", "your", "light", "night", "love",
             "sky", "all", "on", "up", "never", "every", "where", "but", "am"}


def looks_english(lyrics: str) -> bool:
    """True when most words of the sung lines are common English words - a
    model that ignored the requested lyrics language."""
    words = [w for line in lyrics.splitlines() if not line.strip().startswith("[")
             for w in re.findall(r"[a-zA-Z']+", line.lower())]
    if len(words) < 12:
        return False
    return sum(1 for w in words if w in _EN_WORDS) / len(words) > 0.18


def plan(chat: Callable[[list[dict[str, Any]], int, float], str], *, concept: str, lead_name: str, lead_look: str,
         n_shots: int = 10, language: str = "en", compose_song: bool = True, duration_s: float = 120.0,
         lyrics: Optional[str] = None, genre: Optional[str] = None,
         on_bad_reply: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """``on_bad_reply(text)`` sees a reply that could not be read (the app
    keeps the last one on disk to see what the model did)."""
    if not concept.strip():
        raise ProductionError("bad_plan", "describe the concept of the video")
    if not 2 <= n_shots <= 40:
        raise ProductionError("bad_plan", "shots must be between 2 and 40")
    # room for every shot (and the lyrics): a reply cut off by the limit
    # cannot be read at all
    budget = min(12000, 1000 + 220 * n_shots + (1800 if compose_song else 0))
    try:
        reply = chat(plan_messages(concept, lead_name, lead_look, n_shots, language, compose_song, duration_s, lyrics,
                                   genre), budget, 0.8)
    except ProductionError:
        raise
    except Exception as exc:  # noqa: BLE001 - no model resident, server down...
        raise ProductionError("no_llm", f"could not reach a language model to plan the video ({type(exc).__name__}: "
                                        f"{str(exc)[:200]}); start one (Backends) or write the shots yourself") from None
    try:
        draft = normalise_draft(_json_object(reply), compose_song, language, duration_s)
    except ProductionError as first:
        # one more try with the shape spelled out again: a model that
        # wandered off the format usually gets it right the second time
        if on_bad_reply:
            on_bad_reply(reply)
        messages = plan_messages(concept, lead_name, lead_look, n_shots, language, compose_song, duration_s, lyrics,
                                 genre)
        messages += [{"role": "assistant", "content": str(reply or "")[:6000]},
                     {"role": "user", "content": (
                         f"That answer could not be used ({first.message}). Answer again with only the JSON object, "
                         "exactly in the shape asked, with a \"shots\" array of " + str(n_shots) + " objects that each "
                         "have a \"prompt\". Keep each prompt under 60 words.")}]
        try:
            again = chat(messages, budget, 0.4)
        except ProductionError:
            raise
        except Exception:  # noqa: BLE001
            raise first from None
        try:
            draft = normalise_draft(_json_object(again), compose_song, language, duration_s)
        except ProductionError:
            if on_bad_reply:
                on_bad_reply(again)
            raise ProductionError(first.code, f"{first.message} (asked the model twice): try again, plan fewer "
                                              "shots or write them yourself") from None
    song = draft.get("song")
    if song and language != "en" and looks_english(song.get("lyrics") or ""):
        # asked for Spanish (or another language) and got English: ask once
        # more for the lyrics alone, in the right language
        lang = LANGUAGE_NAMES.get(language, language)
        try:
            again = chat([{"role": "system", "content": "You write song lyrics. Answer with one JSON object and nothing else."},
                          {"role": "user", "content": (
                              f"Rewrite these lyrics in {lang}: every sung line in {lang}, same meaning, same section tags "
                              "(they stay in English, on their own lines), singable, with rhyme where natural.\n"
                              + song["lyrics"] + "\nAnswer exactly as {\"lyrics\": \"...\"}")}], 2500, 0.7)
            lyrics = str(_json_object(again).get("lyrics") or "").strip()
            if lyrics and not looks_english(lyrics):
                song["lyrics"] = lyrics
        except Exception:  # noqa: BLE001 - keep the first draft; the person can edit it
            pass
        if looks_english(song.get("lyrics") or ""):
            draft["warnings"] = [f"the lyrics came back in English, not {lang}: edit them before creating"]
    return draft


def spec_from_draft(draft: dict[str, Any], *, lead: dict[str, Any], song_asset_id: Optional[str] = None,
                    clips: str = "all", aspects: Optional[list[str]] = None, song_takes: int = 2,
                    engine: str = "auto", brief: Optional[str] = None) -> dict[str, Any]:
    """A production spec from an (edited) draft. clips: "all" (every moving
    shot gets a Wan clip), "lead" (only shots with the lead) or "none"."""
    if clips not in ("all", "lead", "none"):
        raise ProductionError("bad_plan", "clips is all, lead or none")
    shots = []
    for i, s in enumerate(draft.get("shots") or []):
        if not str(s.get("prompt") or "").strip():
            continue
        moving = s.get("motion", "move") == "move"
        wants_clip = moving and (clips == "all" or (clips == "lead" and s.get("lead")))
        shots.append({"key": str(i + 1), "prompt": str(s["prompt"]).strip(), "lead": bool(s.get("lead")),
                      "motion": "move" if moving else "still", "motion_prompt": s.get("motion_prompt") or "subtle motion",
                      "clips": [0] if wants_clip else [], "aspect": "16:9", **({"section": s["section"]} if s.get("section") else {})})
    if not shots:
        raise ProductionError("bad_plan", "the plan has no shots")
    spec: dict[str, Any] = {"title": draft.get("title") or lead.get("name") or "Music video", "lead": lead,
                            "engine": engine, "shots": shots,
                            "world": {"look": draft.get("world_look") or "", "negative": draft.get("world_negative") or ""},
                            "timeline": {"aspects": aspects or ["16:9"], "qualities": ["preview", "final"]}}
    if brief:
        spec["brief"] = brief
    if song_asset_id:
        spec["song"] = {"asset_id": song_asset_id}
    else:
        song = dict(draft.get("song") or {})
        if not str(song.get("tags") or "").strip() or not str(song.get("lyrics") or "").strip():
            raise ProductionError("bad_plan", "the song needs tags and lyrics (or pick an existing song)")
        song["count"] = max(1, min(4, int(song_takes)))
        song["take"] = 1
        spec["song"] = song
    return spec
