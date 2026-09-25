---
name: narrated-shorts
description: Turn a topic or a script into a narrated vertical short (TikTok, Reels, YouTube Shorts) with Prospero's Hoard - script, voice-over, word-by-word captions, stock or generated pictures, ducked music, publish text - through studio_short_create and friends.
---

# Narrated shorts with Prospero's Hoard

## Make one
1. `studio_short_create(topic="...", options={...})`. Useful options:
   - `language` ("es", "en"...), `duration_s` (45), `tone` ("curious, warm").
   - `voice`: omit for the language's default Piper voice; `{"voice_id": "voice_..."}`
     for a voice-studio voice (`voice_list`), or `{"backend": "piper",
     "voice_id": "es_ES-sharvard-medium", "speed": 1.05}`.
   - `visuals.source`: "auto" (stock footage when a Pexels/Pixabay key is set,
     else generated stills), "stock", "generate" or "mix"; `visuals.clips` N
     animates the N longest generated stills with Wan (slow: ~9.5 GPU minutes
     each, and the short pauses on an animatic first).
   - `music`: `{"mode": "compose", "tags": "calm ambient, soft piano"}`
     (ACE-Step instrumental), `{"mode": "asset", "asset_id": ...}`,
     `{"mode": "library"}` (a track from data/music) or `{"mode": "none"}`.
   - `captions.style`: "bold" (default, word by word), "default", "horror", "none".
   - `timeline.aspects` (["9:16"]), `timeline.qualities` (["preview"], add "final").
   The user's own script goes in `script` (plain text, one paragraph per
   segment) instead of `topic`.
2. Poll `studio_production(production)`; the stages are script, narration,
   music, visuals, animatic, clips, mix, timeline, report.
3. When it is done: look at it (`studio_show` on `renders["9:16"]["preview"]`)
   before describing it, and hand the user `publish` exactly as it is (title,
   description, hashtags and the footage credits - never drop the credits).

## Review the script first
Pass `settings={"script_review": true}` when the topic involves facts,
health, money or anything the user must check. The short stops at
`awaiting_review` after the script: show it (`studio_production_script
(production)`), apply the user's edits with `studio_production_script
(production, script=...)`, or approve it with `studio_production_continue`.

## Change it afterwards
`studio_production_script(production, script=...)` replaces the script and
redoes the voice, pictures, mix and render; the music is kept.

## Variants
`count=3` makes three shorts from one brief with other seeds: other footage,
other generated pictures and, from a topic, another script.

## Footage by hand
`studio_stock_search("glowing waves night", aspect="9:16", project=p, take=2)`
imports the first two results with their credit; `refs=[...]` imports exact
ones from an earlier search. Without a key, tell the user to add a free
Pexels or Pixabay key in Settings > Stock footage.

## Limits to say out loud
- The script's facts come from the local model; they are not checked.
- Without faster-whisper installed, caption word times are estimated from
  the sentence timing (close, not exact).
- Nothing is uploaded to TikTok/YouTube; the user posts the render.
