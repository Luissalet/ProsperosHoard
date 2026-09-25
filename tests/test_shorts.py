"""Narrated shorts: spec/script handling, word alignment and captions, the
shot plan, the `bold` caption style, the ducked soundtrack, and the whole
pipeline through the real API and job queue (fake ComfyUI, fake LLM/TTS,
mocked stock service)."""

from __future__ import annotations

import io
import json
import subprocess
import time
import wave

import httpx
import numpy as np
import pytest

from prosperos_hoard import shorts, soundtrack, video
from prosperos_hoard import timeline as timeline_mod
from prosperos_hoard.backend import ffmpeg_path
from test_stock import make_mp4, transport

SCRIPT_REPLY = """Sure! Here it is:
```json
{"title": "Why the sea glows", "description": "Tiny algae light up the waves.",
 "hashtags": ["#ocean", "science"],
 "segments": [
  {"text": "Have you ever seen the sea glow at night?", "visual": "dark beach at night, blue glowing waves", "query": "glowing waves night"},
  {"text": "Tiny algae make light when the water moves. It is called bioluminescence.", "visual": "microscope view of plankton", "query": "plankton microscope"},
  {"text": "Next time, walk by the shore after dark.", "visual": "person walking on a beach at night", "query": "beach night walk"},
 ]}
```"""


def fake_wav(text: str, sr: int = 22050) -> bytes:
    seconds = 0.12 + 0.28 * len(text.split())
    t = np.arange(int(seconds * sr)) / sr
    samples = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((samples * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def wait_job(c, job_id: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("queued", "waiting_gpu", "running"):
            return job
        time.sleep(0.3)
    raise AssertionError(f"job {job_id} did not finish")


# ------------------------------------------------------------------ units

def test_spec_defaults_and_errors():
    spec = shorts.normalise_spec({"topic": "the sea"})
    assert spec["language"] == "es" and spec["visuals"]["source"] == "auto" and spec["captions"]["style"] == "bold"
    assert spec["music"]["mode"] == "none" and spec["timeline"]["aspects"] == ["9:16"]
    assert shorts.normalise_spec({"topic": "x", "music": {"tags": "lofi"}})["music"]["mode"] == "compose"
    with pytest.raises(shorts.ShortError, match="topic"):
        shorts.normalise_spec({})
    with pytest.raises(shorts.ShortError, match="source"):
        shorts.normalise_spec({"topic": "x", "visuals": {"source": "magic"}})
    with pytest.raises(shorts.ShortError, match="asset_id"):
        shorts.normalise_spec({"topic": "x", "music": {"mode": "asset"}})


def test_script_from_text_and_from_a_chatty_reply():
    script = shorts.normalise_script("Uno dos tres. Cuatro cinco seis. Siete ocho nueve diez.")
    assert [s["text"] for s in script["segments"]] == ["Uno dos tres. Cuatro cinco seis.", "Siete ocho nueve diez."]
    parsed = shorts.parse_script_reply(SCRIPT_REPLY)
    assert parsed["title"] == "Why the sea glows" and parsed["hashtags"] == ["ocean", "science"]
    assert len(parsed["segments"]) == 3 and parsed["segments"][1]["query"] == "plankton microscope"
    with pytest.raises(shorts.ShortError, match="JSON"):
        shorts.parse_script_reply("I cannot help with that")
    msgs = shorts.script_messages("the sea", "es", 40, "curious")
    assert "Spanish (Spain)" in msgs[1]["content"] and "108 words" in msgs[1]["content"]


def test_align_words_keeps_script_spelling_and_fills_gaps():
    script = [{"text": w, "start_s": i * 1.0, "end_s": i * 1.0 + 0.9} for i, w in enumerate(["Hola,", "qué", "tal", "estás?"])]
    heard = [{"text": " hola", "start_s": 0.2, "end_s": 0.5}, {"text": " que", "start_s": 0.6, "end_s": 0.8},
             {"text": " estas", "start_s": 1.4, "end_s": 1.9}]
    words, share = shorts.align_words(script, heard)
    assert share == 0.75 and [w["text"] for w in words] == ["Hola,", "qué", "tal", "estás?"]
    assert words[0]["start_s"] == 0.2 and words[3]["start_s"] == 1.4
    assert 0.8 <= words[2]["start_s"] < words[2]["end_s"] <= 1.4  # squeezed between its neighbours


def test_caption_clips_group_words():
    words = [{"text": t, "start_s": s, "end_s": s + 0.3} for t, s in
             [("One", 0.0), ("two", 0.35), ("three", 0.7), ("four.", 1.05), ("Five", 1.6), ("six", 2.5)]]
    clips = shorts.caption_clips(words, max_words=3, total_s=3.2)
    assert [c["text"] for c in clips] == ["One two three", "four.", "Five", "six"]
    assert clips[0]["end_s"] == clips[1]["start_s"] and clips[-1]["end_s"] <= 3.2
    assert clips[0]["words"][1] == {"text": "two", "start_s": 0.35, "end_s": 0.65}


def test_plan_shots_and_sources():
    segs = [{"start_s": 0.15}, {"start_s": 5.0}, {"start_s": 7.2}]
    plan = shorts.plan_shots(segs, 12.0, 3.0)
    assert plan[0]["start_s"] == 0.0 and [p["segment"] for p in plan] == [0, 0, 1, 2, 2]
    assert abs(sum(p["duration_s"] for p in plan) - 12.0) < 0.01
    assert shorts.shot_source("auto", {}, 0, False) == "generate"
    assert shorts.shot_source("mix", {}, 1, True) == "generate" and shorts.shot_source("mix", {}, 2, True) == "stock"
    assert shorts.shot_source("generate", {"source": "stock"}, 0, True) == "stock"


def test_bold_captions_and_words_in_timeline():
    clip = {"text": "Hola mundo", "start_s": 1.0, "end_s": 2.0, "karaoke": True,
            "words": [{"text": "Hola", "start_s": 1.2, "end_s": 1.5}, {"text": "mundo", "start_s": 1.5, "end_s": 1.9}]}
    ass = video.build_ass(1080, 1920, [clip], "bold")
    events = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert len(events) == 3  # lead-in without highlight, then one per word
    assert events[1].endswith(f"{{{video.BOLD_ACTIVE}}}Hola{{\\r}} mundo") and "0:00:01.50" in events[2]
    assert "Style: Lyrics,Inter,108," in ass
    tracks = timeline_mod.normalise_tracks(
        [{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2}]}, {"type": "lyrics", "clips": [clip]}],
        lambda aid: {"id": aid, "kind": "image"})
    assert tracks[1]["clips"][0]["words"][1] == {"text": "mundo", "start_s": 1.5, "end_s": 1.9}
    with pytest.raises(timeline_mod.TimelineError, match="word"):
        timeline_mod.normalise_tracks([{"type": "visual", "clips": [{"asset_id": "a1", "duration_s": 2}]},
                                       {"type": "lyrics", "clips": [dict(clip, words=[{"text": "x"}])]}],
                                      lambda aid: {"id": aid, "kind": "image"})


def test_soundtrack_filter_and_mix(tmp_path):
    f = soundtrack.build_mix_filter(10.0, True, -18, "normal")
    assert "sidechaincompress" in f and "volume=-18.0dB" in f and "loudnorm=I=-14.0" in f and f.endswith("[out]")
    assert "sidechaincompress" not in soundtrack.build_mix_filter(10.0, True, duck="off")
    assert soundtrack.build_mix_filter(10.0, False).startswith("[0:a]")
    ff = ffmpeg_path()
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=300:d=3", str(tmp_path / "v.wav")], check=True)
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=f=900:d=1", str(tmp_path / "m.wav")], check=True)
    soundtrack.mix(tmp_path / "v.wav", tmp_path / "m.wav", tmp_path / "o.m4a", 3.0)
    from prosperos_hoard import audio

    assert abs(audio.probe_duration_s(tmp_path / "o.m4a") - 3.6) < 0.15  # the music looped under the whole voice + tail


# ------------------------------------------------------------- end to end

def hooks(app, mp4: bytes, transcribe=None, chat=None):
    calls = {"chat": 0, "tts": []}

    def fake_chat(messages, max_tokens, temperature):
        calls["chat"] += 1
        return SCRIPT_REPLY

    def fake_tts(voice, text):
        calls["tts"].append((voice.get("voice_id"), text))
        return fake_wav(text), "fake-tts"

    app.state.short_hooks = {
        "chat": chat or fake_chat, "synthesize": fake_tts, "transcribe": transcribe or (lambda path, lang: None),
        "stock_keys": lambda: {"pexels": "PKEY"},
        "stock_client": lambda: httpx.Client(transport=transport(mp4)),
    }
    return calls


def test_short_end_to_end(client, tmp_path):
    c, app, _ = client
    mp4 = make_mp4(tmp_path / "stock.mp4", seconds=6)
    calls = hooks(app, mp4)
    r = c.post("/api/agent/studio_short_create", json={
        "topic": "why the sea glows at night", "name": "Sea glow",
        "options": {"language": "en", "duration_s": 20, "visuals": {"source": "mix", "shot_s": 2.5, "clips": 1},
                    "music": {"mode": "compose", "tags": "calm ambient"},
                    "timeline": {"aspects": ["9:16"], "qualities": ["preview"]}}})
    assert r.status_code == 200, r.text
    slug = r.json()["production"]["slug"]
    job = wait_job(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    view = c.get(f"/api/agent/studio_production?production={slug}").json()
    # one generated still becomes a Wan clip -> the animatic pauses it
    assert view["status"] == "awaiting_review" and view["kind"] == "short", view
    assert view["title"] == "Why the sea glows" and view["segments"] == 3 and view["script_source"] == "llm"
    assert view["shots"]["stock"] >= 1 and view["shots"]["generate"] >= 1
    assert view["animatic"]["clips_planned"] == 1 and "animatic" in view["next"]
    assert calls["chat"] == 1 and all(v == "en_US-lessac-medium" for v, _ in calls["tts"])
    r = c.post(f"/api/agent/studio_production_continue?production={slug}")
    job = wait_job(c, r.json()["job"]["id"])
    assert job["state"] == "done", job
    view = c.get(f"/api/agent/studio_production?production={slug}").json()
    assert view["status"] == "done", view
    store = app.state.store
    render = store.get_asset(view["renders"]["9:16"]["preview"])
    assert render["kind"] == "video" and render["height"] == 960
    narration = view["narration"]
    assert narration["timing"] == "estimated" and abs(render["duration_s"] - (narration["duration_s"] + 0.6)) < 0.3
    state = json.loads((store.data_dir / "productions" / slug / "state.json").read_text(encoding="utf-8"))
    tl = store.get_timeline(state["done"]["timeline"]["timelines"]["9:16"]["timeline_id"])
    assert tl["finishing"]["lyric_style"] == "bold"
    captions = next(t for t in tl["tracks"] if t["type"] == "lyrics")["clips"]
    assert captions[0]["text"].startswith("Have you") and captions[0]["words"]
    visual = next(t for t in tl["tracks"] if t["type"] == "visual")["clips"]
    assert len(visual) == len(state["done"]["visuals"]["plan"]) and any(v["kind"] == "video" for v in visual)
    soundtrack_asset = store.get_asset(view["soundtrack_asset_id"])
    assert soundtrack_asset["recipe"]["operation"] == "short_mix" and len(soundtrack_asset["recipe"]["input_asset_ids"]) == 2
    publish = view["publish"]
    assert publish.startswith("Why the sea glows") and "#ocean #science" in publish and "Video by Author" in publish
    report = c.get(f"/api/productions/{slug}/report").text
    assert "## Shots" in report and "Stock credits" in report
    assert c.get(f"/api/productions/{slug}/publish").text == publish

    # a new script redoes the narration and everything after it, keeps the music
    music_id = state["done"]["music"]["asset_id"]
    r = c.post("/api/agent/studio_production_script",
               json={"production": slug, "script": "The sea glows. Tiny algae do it. Go and see it tonight."})
    assert r.status_code == 200, r.text
    job = wait_job(c, r.json()["job"]["id"])
    view = c.get(f"/api/agent/studio_production?production={slug}").json()
    assert job["state"] == "done" and view["status"] in ("awaiting_review", "done"), view
    if view["status"] == "awaiting_review":
        job = wait_job(c, c.post(f"/api/agent/studio_production_continue?production={slug}").json()["job"]["id"])
        view = c.get(f"/api/agent/studio_production?production={slug}").json()
    assert view["status"] == "done" and view["script_source"] == "given" and view["title"] == "Why the sea glows"
    state = json.loads((store.data_dir / "productions" / slug / "state.json").read_text(encoding="utf-8"))
    assert state["done"]["music"]["asset_id"] == music_id and calls["chat"] == 1
    # guards: music-video tools refuse a short
    assert c.post("/api/agent/studio_recipe_export", json={"production": slug}).status_code == 400
    r = c.post("/api/agent/studio_production_shots?production=" + slug, json={"changes": [{"key": "1", "best": 0}]})
    assert r.status_code == 400 and "not_for_shorts" in r.text


def test_short_with_speech_to_text_script_review_and_batch(client, tmp_path):
    c, app, _ = client
    mp4 = make_mp4(tmp_path / "stock.mp4", seconds=6)

    def heard(path, lang):  # "speech-to-text" that hears every word, a bit later than estimated
        words = []
        t = 0.2
        for w in "Uno dos tres cuatro. Cinco seis siete.".split():
            words.append({"text": " " + w.strip(".").lower(), "start_s": round(t, 3), "end_s": round(t + 0.25, 3)})
            t += 0.3
        return words

    hooks(app, mp4, transcribe=heard)
    r = c.post("/api/agent/studio_short_create", json={
        "script": "Uno dos tres cuatro. Cinco seis siete.", "name": "Números", "count": 2,
        "settings": {"script_review": True},
        "options": {"visuals": {"source": "stock"}, "captions": {"max_words": 2}}})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 2 and items[0]["production"]["slug"] != items[1]["production"]["slug"]
    for it in items:
        wait_job(c, it["job"]["id"])
    slug = items[1]["production"]["slug"]
    view = c.get(f"/api/agent/studio_production?production={slug}").json()
    assert view["status"] == "awaiting_review" and view["stages"]["narration"] == "pending" and "script" in view["next"]
    seen = c.post("/api/agent/studio_production_script", json={"production": slug}).json()
    assert seen["script"]["segments"][0]["text"] == "Uno dos tres cuatro. Cinco seis siete."
    job = wait_job(c, c.post(f"/api/agent/studio_production_continue?production={slug}").json()["job"]["id"])
    assert job["state"] == "done", job
    view = c.get(f"/api/agent/studio_production?production={slug}").json()
    assert view["status"] == "done" and view["narration"]["timing"] == "speech-to-text", view
    state = json.loads((app.state.store.data_dir / "productions" / slug / "state.json").read_text(encoding="utf-8"))
    assert state["spec"]["seed"] == 101 and all(i["source"] == "stock" for i in state["done"]["visuals"]["items"].values())
    assert state["done"]["narration"]["words"][0] == {"text": "Uno", "start_s": 0.2, "end_s": 0.45}
    assert state["done"]["animatic"]["skipped"] is True and state["done"]["music"]["skipped"] is True


def test_short_without_a_language_model_fails_clearly(client, tmp_path):
    c, app, _ = client

    def no_llm(*_a):
        raise RuntimeError("no model resident")

    hooks(app, b"", chat=no_llm)
    r = c.post("/api/agent/studio_short_create", json={"topic": "anything", "options": {"visuals": {"source": "generate"}}})
    job = wait_job(c, r.json()["job"]["id"])
    assert job["state"] == "failed"
    view = c.get(f"/api/agent/studio_production?production={r.json()['production']['slug']}").json()
    assert view["status"] == "failed" and "spec.script" in view["message"] and "resumes" in view["next"]
