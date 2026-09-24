---
name: voice-studio
description: Clone a voice, narrate an audiobook or dub a video with Prospero's Hoard's Voice studio - engines, voice library, speak, transcribe/dictate, audiobook and dub - through the voice_* MCP tools.
---

# Voice studio with Prospero's Hoard

`voice_engines()` first. It lists every TTS/STT engine and whether it is
installed; nothing installs itself, so point the user at the app's Voice >
Engines screen (or the install hint) when the one a workflow needs is
missing. Piper (no cloning) always works if `studio_status` shows it; the
cloning engines (xtts, f5-tts, chatterbox) and dictation (faster-whisper)
need an explicit install first.

## Clone a voice
1. Get a clean sample onto the machine (a short, quiet recording, 6-30s is
   plenty) and note its absolute path.
2. `voice_create(name, engine_id, source_path)` - trims silence, normalises
   loudness, and reports a quality check (`duration_s`, `snr_db`,
   `clipping_pct`, `warnings`). Low SNR or clipping means "re-record it",
   not "ignore it": tell the user before using a bad sample.
3. `voice_speak(text, voice_id=...)` to preview it. `voice_list()` shows
   what is saved. A saved voice can also be a character's voice in
   `studio_cast`: `fields={"voice": {"backend": "studio", "voice_id": "..."}}`.
4. Names mispronounced? Pass `lexicon={"Prospero": "PROS-per-oh"}` (whole
   words) to `voice_speak`/`voice_audiobook`/`voice_dub`; the voice's own
   lexicon (set in the app) is merged underneath.

## Narrate an audiobook
`voice_audiobook(text=... or source_path=..., voice_id=..., format="mp3"|"m4b")`
returns a job - poll with `voice_job(job_id, wait_s=60)`; when done its
`outputs` list the chapters (title, start, duration) and `asset_ids`, never
file paths. Chapters come from markdown headings or short standalone
"Chapter N" / "Capítulo IV" lines; re-running the exact same text and voice
resumes chapters already rendered instead of redoing them. `format: "m4b"`
adds chapter markers; either way an SRT/LRC aligned transcript comes with it.

## Dub a video
`voice_dub(source_path=..., target_language="es" or "Spanish", voice_id=...)`
needs a local LLM for translation (see `studio_status`'s `llm` capability) -
it fails clearly, not silently, when none is reachable. The voice speaks
the target language. It is a job: poll with `voice_job`, whose finished
`outputs` show the first 20 lines (source text, translated text, timing);
`voice_dub_segments(job_id, offset=20)` pages through the rest. A wrong line
does not need a full re-run - `voice_resynthesize_segment(job_id, index,
text=...)` fixes just that one (a new voice applies to that line only),
remixes the final video and, when the dub was saved to a project, returns
the new video's `asset_id`.

## Transcribe / dictate
`voice_transcribe(path=... or asset_id=...)` for a file (word timestamps
when the engine supports them - export SRT/VTT/TXT client-side from the
segments); the app's Dictate screen is the same engine wired to a
browser mic recording for a quick turnaround.

Traps:
- Never claim a real person's voice was cloned from a sample without their
  consent - this is a local tool with no identity checks of its own.
- A voice needing cloning (its engine's `capabilities.cloning` is true) with
  no sample fails clearly (`cloning_needs_sample`); Piper never clones, it
  only speaks with its own curated voices.
- `voice_dub`/`voice_audiobook` jobs can take a while on CPU: prefer
  `wait_s=60-120` over polling in a tight loop.
