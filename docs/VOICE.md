# Voice studio

A local voice pipeline sitting next to the rest of Prospero: an engine
registry, a voice library, transcription/dictation, long-form narration and
video dubbing - all on engines that run on your own machine, installed only
when you ask, never downloaded silently. `voices.py`/`studio_voice` (Piper
for quick character narration) is untouched; the voice studio is the
separate, opt-in layer built on top for cloning, dictation, audiobooks and
dubbing. Routes: `/api/voice/*` ([API.md](API.md)); MCP tools: `voice_*`
([MCP.md](MCP.md)); UI: the **Voice** screen.

## Consent

Voice cloning only ever happens from a sample you explicitly upload or
record and a voice you explicitly choose to reuse - nothing is inferred or
collected automatically. Only clone a voice you have the right to
reproduce; using someone else's voice without their consent is on the
person doing it, not the tool.

## Engine registry (`voice_engines.py`)

Every engine is an **optional import**: `is_installed()` only checks
whether the package is on disk (`importlib.util.find_spec`, no heavy
import), and the real library is imported inside `synthesize`/`transcribe`,
the first time it is actually used. `GET /api/voice/engines` (or
`voice_engines` over MCP) reports every engine's install state, languages,
whether it can clone a voice, whether it needs a GPU, and a copy-pasteable
`pip install ...` hint for anything missing. Nothing installs itself - the
Voice screen's "Install" button (or `POST /api/voice/engines/{id}/install`)
runs `pip install` in the app's own interpreter as a background job, only
when asked.

### Text-to-speech

| Engine | id | Cloning | Languages | Install |
| --- | --- | --- | --- | --- |
| Piper | `piper` | no (curated voices) | the 10 curated voices (es, en, fr, it, pt, de) | already used by `studio_voice`; `pip install piper-tts` |
| Coqui XTTS-v2 | `xtts` | yes, from a 6-30s sample | 17 languages | `pip install TTS` |
| F5-TTS | `f5-tts` | yes, from a sample + its transcript | en, zh | `pip install f5-tts` (GPU recommended) |
| Kokoro | `kokoro` | no (named voice packs), streaming | en, es, fr, it, pt, ja, zh, hi | `pip install kokoro>=0.9.4 soundfile` |
| Chatterbox | `chatterbox` | yes, English-focused, an "exaggeration" style control | en | `pip install chatterbox-tts` (GPU recommended) |
| ComfyUI TTS node pack | `comfy-tts` | status-only: reports a TTS node pack ComfyUI already has (VibeVoice, F5-TTS, Kokoro, IndexTTS custom nodes); no built-in workflow template yet, so it cannot synthesize | - | install a TTS custom node pack in ComfyUI and restart it |
| KittenTTS 0.8 | `kittentts` | no (eight named voices: Bella, Jasper, Luna, Bruno, Rosie, Hugo, Kiki, Leo); ONNX on the CPU only, nano 15 M / micro 40 M / mini 80 M | en | the **Install** button: builds its own environment in `data/voices/engines/kittentts` with a Python 3.10-3.12 from the PC (`PROSPERO_KITTEN_PYTHON` wins), installs the kittentts 0.8.1 wheel and downloads the default model |
| VoxCPM2 | `voxcpm` | yes (a reference WAV, or the voice library's sample; optional transcript for a closer clone) and voice design (`voice_ref` = `(a calm older man)`) | 30 languages incl. es/en, 48 kHz, streaming | the **Install** button: its own environment in `data/voices/engines/voxcpm` (Python 3.10-3.12, CUDA 12.8 torch, `voxcpm`, ~8 GB `openbmb/VoxCPM2`, Apache-2.0); runs on the GPU in `PROSPERO_VOXCPM_GPU` (default 2) |

VoxCPM2 is out of process like KittenTTS (`voxcpm_worker.py`). After each line `voice_engines` reports `last_run`
(model load, total seconds, first audio when streaming, audio seconds) so engines can be compared on the same text.

Measured here (Ágora #783, 04-11-2026, GPU 2, one Spanish sentence of 24 words with proper names; the audio was not auditioned or scored by a listener, so naturalness and name pronunciation are not rated): VoxCPM2 took 29.8-32.9 s per call as an out-of-process call, of which 18.9 s is loading the model each time and 9.6 s generating 7.7 s of 48 kHz audio (0.3 s to the first chunk when streaming). Piper (`es_ES-davefx-medium`, CPU, 22 kHz) took 0.25 s for 8 s of audio once warm. XTTS-v2, F5-TTS and Kokoro are not installed on this PC, so they were not measured. Keeping the model resident in a long-lived worker is the next step if VoxCPM2 is wanted for many short lines.

KittenTTS needs Python < 3.13 (its phonemiser) and this app runs on 3.13, so it never goes into the app's venv: each
line is synthesised out of process by `kitten_worker.py` in that environment. `voice_ref` is a voice name or
`<size>/<voice>` (`nano/Luna`); the default model is mini (`PROSPERO_KITTEN_MODEL=nano|micro|mini` or a Hugging Face
id). The engine's status lists its voices, and the Voice screen shows them in a second selector next to the engine.
The 1.7 B «KittenTTS 2» family (Spanish voice) was measured at 16-20x real time on this CPU (Ágora #250) and is not
used.

### Speech-to-text

| Engine | id | Notes | Install |
| --- | --- | --- | --- |
| faster-whisper | `faster-whisper` | primary; word-level timestamps. Asks Funes's Hoard through the hub first (one Whisper for the family); otherwise the shared transcriber runs here, on the GPU when the hub lends it, else on the CPU. Silence hallucinations are filtered | `pip install faster-whisper` (only needed without Funes) |
| OpenAI Whisper | `whisper` | heavier fallback for setups that already have it | `pip install openai-whisper` |

`best_installed_tts`/`best_installed_stt` pick the first installed engine
(optionally cloning-capable, optionally a preferred id) so a pipeline can
say plainly "no engine installed" instead of guessing.

**Loaded models are cached.** Each engine loads its model once per
`(model, device, language)` and keeps it (`voice_engines.cached_model`),
with one lock per engine around load *and* inference - an audiobook calls
the engine once per sentence, a dub once per line. The app's free-memory
action (`POST /api/backend/comfy/free`) also calls
`voice_engines.unload_models()`, which drops every cached voice model and
empties the CUDA cache.

**Languages.** A `language` anywhere (voice spec, dub target, transcription)
may be a code or a name - `es`, `es-ES`, `es_MX`, `Spanish`, `español`,
`Castellano` all mean `es` (`normalize_language`); anything unrecognised
fails with `bad_language`/`unsupported_language`. Each engine maps the code
to its own convention: XTTS uses `zh-cn` for Chinese, Kokoro its one-letter
`lang_code` (`en`->`a`, `en-GB`->`b`, `es`->`e`, `fr`->`f`, `hi`->`h`,
`it`->`i`, `ja`->`j`, `pt`->`p`, `zh`->`z`) with a default voice pack that
speaks that language. For transcription, `auto` (or nothing) detects it.

## Voice library (`voice_lab.py`, table `studio_voices`)

`POST /api/voice/voices` (or the upload form, or `voice_create`) turns a
sample into a reusable voice:

1. **Process** (`process_sample`, two ffmpeg passes to avoid a filtergraph
   hang seen when resample+trim+loudnorm run in one pass on silence-free
   audio): trim leading/trailing silence (`silenceremove`+`areverse` both
   ends), then EBU R128 loudness normalisation (`loudnorm=I=-19:TP=-2:LRA=7`)
   to a clean 44.1 kHz mono WAV. A sample that is silent end to end fails
   clearly (`silent_sample`) instead of a confusing "unreadable" error.
2. **Quality check** (`quality_check`): duration, a dependency-free SNR
   estimate (RMS of the loudest decile of frames over the quietest decile),
   a clipping percentage, and warnings - short (<1s), long (>5 min), noisy
   (<15 dB estimated SNR) or clipped (>0.1% of samples near full scale)
   samples say so; `ok` is true only with no warnings.
3. **Reference transcript**: when an STT engine is installed, the sample is
   transcribed automatically (best-effort; a failure never blocks voice
   creation).
4. **Stored**: `name, engine_id, sample path (relative to the data folder),
   language, cloned flag, reference transcript, quality report, tags,
   presets[{name, speed?, pitch?, style?, lexicon?}], lexicon, project_id?`.
   A preset is a named shortcut for `speed`/`pitch`/`style` on that voice.
5. **Deleted** with the voice: `DELETE /api/voice/voices/{id}` also removes
   the voice's own folder under `data/voice_studio/voices/` (the processed
   sample is a recording of someone's voice; it is not left behind).

A `voice` request anywhere in the studio (speak, audiobook, dub) is a
**spec**: `{engine_id?, voice_id?, voice_ref?, preset?, speed?, pitch?,
style?, language?, lexicon?}`. `resolve_voice_spec` fills it in: a
`voice_id` supplies its own engine, sample, language and reference
transcript unless `engine_id`/`voice_ref` override them, a named `preset`
merges in its saved parameters, and a cloning engine with neither a library
sample nor an explicit `voice_ref` fails clearly (`cloning_needs_sample`).
F5-TTS receives the voice's reference transcript as its `ref_text`.
`pitch` is in semitones (-12..12) and applied after synthesis with ffmpeg
(`asetrate`+`aresample`+`atempo`, duration unchanged) since no engine here
shifts pitch natively. A Piper `voice_ref` must be a plain voice id
(letters, digits, `_`, `.`, `-`).

**Pronunciation lexicon.** `{"Prospero": "PROS-per-oh"}`: each written word
is replaced by its spoken form before synthesis - whole words only,
case-insensitively, longest term first - so it covers speak, audiobooks and
dubs alike. It merges voice < preset < request: the voice's own lexicon
(`PATCH /api/voice/voices/{id}` with `{"lexicon": {...}}`, `{}` clears it;
kept as a reserved `_lexicon` entry in the voice's presets, so preset names
cannot start with `_`), a preset's `lexicon`, then the request's
`voice.lexicon`. At most 500 entries.

Audiobook and dub requests validate the voice (engine known and installed,
pitch in range, language supported by the engine) **before** the job is
queued, so a bad voice fails the request rather than the job.

## Transcription and dictation

`POST /api/voice/transcribe` (a `path` or `asset_id`) and
`/api/voice/transcribe/upload` (a file) return the full transcript -
language, text, per-segment `start_s`/`end_s`/`text`/word timestamps - plus
`srt`, `vtt` and `txt` exports built from those segments.
`POST /api/voice/dictate` is the short-clip path for a browser microphone
recording: no timestamps, tuned for a fast turnaround (a 30 MB cap). All
three go through `best_installed_stt`, so with nothing installed the error
says exactly what to `pip install`. The upload routes run the processing and
transcription in a worker thread, so the rest of the app keeps answering
while a long file is transcribed.

## Audiobooks (`voice_pipelines.py`, job type `audiobook`)

`POST /api/voice/audiobook` (`text`, or `source_path` for a `.txt`/`.md`/an
`.epub` read with `zipfile`+`xml.etree` and no extra dependency) narrates as
a background job:

1. **Split into chapters** (`split_into_chapters`): a markdown `#`/`##`/`###`
   heading, or a short line that is *only* a heading - "Chapter 3",
   "Capítulo IV: El mar", "Part II", "Book One" (at most 80 characters; a
   roman numeral must be upper case; a title after the number is short and
   does not end like a sentence) - starts a new chapter; otherwise the whole
   text is one. Narration that merely starts with "Part I think..." is never
   taken for a heading, and a heading with no text under it (a "Part One"
   right before "Chapter 1") stays as a chapter that narrates its title. In
   an EPUB, every block element ends a paragraph and `<h1>`-`<h6>` become
   heading lines, so a chapter's first paragraph never merges into its title;
   manifest `href`s are URL-decoded and normalised.
2. **Split into sentences** (`split_into_sentences`): paragraph breaks, then
   `.`/`!`/`?`/`…` followed by a capital letter, digit or opening quote - a
   regex splitter, not a language model, good enough to pace narration and
   to give the transcript a sentence per timed line.
3. **Synthesize** each sentence with the chosen voice spec, resampled to a
   common 24 kHz and joined with a 0.35 s pause between sentences and a
   0.9 s pause between chapters (`SENTENCE_PAUSE_S`, `CHAPTER_PAUSE_S`,
   `COMMON_SR` in `voice_pipelines.py`); each chapter is streamed to its own
   WAV file. Progress is reported, and a cancel honoured, per sentence (a
   book with no chapters can be cancelled too). A single chapter over ~24.8
   hours would overflow the WAV size field and fails with `chapter_too_long`.
4. **Assemble** with ffmpeg into one file, reading the chapter files through
   the concat demuxer (no single joined WAV, so no 4 GB RIFF limit for the
   book): `mp3` (loudness-normalised), or `m4b` with real chapter markers
   (an ffmetadata sidecar). An aligned SRT and LRC (one entry per sentence,
   timed to where it landed) are written alongside.
5. **Resume**: the job's work directory is keyed by a hash of its text and
   voice (plus the library voice's lexicon, when it has one); re-running the
   same audiobook after a crash or a cancel picks up from whichever chapters
   already rendered instead of re-synthesizing them. A chapter's transcript
   sidecar is written before its WAV is renamed into place, so an existing
   chapter file always means a complete chapter.

`GET /api/voice/audiobook/{job_id}/download?file=final|srt|lrc` serves the
outputs; `project` on the request also saves the final file as an audio
asset. A text over 2,000,000 characters is rejected up front
(`text_too_long`) rather than starting a job that cannot finish.

## Dubbing (`dubbing.py`, job type `dub`)

`POST /api/voice/dub` (a video by `source_path` or `video_asset_id`, plus
`target_language` - a code or a name, normalised to a code and checked
before the job is queued) runs, as one background job:

1. **Extract** the video's audio track with ffmpeg (to a temporary name,
   renamed into place when complete - a retry reuses it).
2. **Transcribe** it with timestamps (the chosen or best installed STT
   engine; `source_language` `auto`/empty detects it).
3. **Translate** each segment through the local model behind Hoard Link
   (`Link.chat()`), with an optional `glossary` (`{"term": "translation"}`)
   pinned into every instruction; with no local model resolved, the job fails
   clearly (`llm_unavailable`) instead of silently skipping translation - this
   is the one stage that needs a language model, and only for this step.
   The reply budget scales with the line's length. Every dubbing call asks the model for no reasoning (`effort="off"`): the
   lines are short, and a reasoning model left on its default spent the whole
   token budget thinking and returned an empty line after ~100 s.
   `translation_mode` picks how:
   - `adapt` (the default) works in three steps per line. **Literal**: a
     faithful translation. **Reflect**: the model critiques that line as a
     dubbing line - tone and register against the source, whether it can be
     spoken in the room the original leaves (a character budget, `slot x
     chars/s`, worked out from the transcript before translating), glossary
     terms as given, naturalness - and answers compact JSON `{"issues": [...]}`
     (anything that is not that JSON counts as no issues). **Adapt**: the final
     line, fixing the issues inside the budget ("return only the line"). Adapt
     is skipped when reflect finds nothing and the literal line already fits
     the budget, and a failing step (an error, an empty or echoed reply, a
     rewrite that is no shorter and still over budget) leaves the literal line,
     so a flaky model costs polish, never the dub. At most three calls per
     line plus the literal retry below. Each segment keeps
     `translation_steps = {literal, issues, adapted[, fallback]}` and
     `translated_text` is the adapted line; the Dub tab shows the three steps
     in an expandable row.
   - `direct`: one call per line, with a "keep it about as long as the
     original" hint (the behaviour before the three steps).

   In both modes a literal reply that is empty or just repeats the source line
   (a reasoning model that spent its budget thinking, or one that ignored the
   target language) is retried once with a stricter instruction; if it still is
   not a translation the source line is kept and the segment says
   `translation: "untranslated"` (`"ok"`/`"retried"` otherwise), counted in
   `outputs.summary.untranslated` and shown in the Dub tab, instead of being
   voiced in the wrong language without a word. Reflect and adapt are skipped
   for such a line.
4. **Plan** (`plan`, on by default; `dub_plan.py`): each translated line's
   spoken length is estimated from the target language's characters per
   second (`CHARS_PER_SECOND`: es 14.5, en 14.0, fr 14.0, it 14.5, de 13.0,
   pt 14.5, ja 7.5, zh 5.5, ko 7.0, 13.5 for any other) and compared with
   its *slot*: the original line's own duration plus the silence before the
   next line, borrowed up to 0.6 s. The line is `fits` (no speed-up needed),
   `tight` (up to x1.25) or `impossible` (more); `beyond_hard` marks more than
   x2. Each `impossible` line goes back to the local model, up to two tries,
   asking for a version with at most `slot x chars/s` characters that keeps
   meaning and register ("return only the line"); the shortest version seen is
   kept and the original is saved as `plan.shortened_from`. With no model
   available (or none left mid-way) the line stays as it was and is marked
   `plan.shorten = "no_llm"`. A job can calibrate the pace with
   `dub_plan.calibrate` (median chars/s of clips already synthesized, at
   least 3, outliers ignored): `resynthesize_segment` does it from the job's
   other lines; the first plan of a job uses the table, because nothing has
   been synthesized yet.
5. **Synthesize** each translated segment in the chosen voice.
6. **Time-fit** each synthesized segment to its original segment's
   duration with ffmpeg's `atempo` (chained to cover 0.25x-4x, since a
   single `atempo` only covers 0.5x-2x - `MIN_ATEMPO`/`MAX_ATEMPO`/
   `MAX_OVERALL_FACTOR` in `dubbing.py`), then padded or trimmed to land
   exactly on the original timing so the mix stays in sync. A clip shorter
   than its slot is eased at most to `MIN_SLOWDOWN` (0.92x) and the rest is
   silence: stretching speech further drawls it (seen live at 0.55x, where
   the quality check heard different words).
   With the plan on, a clip longer than the line's own duration first takes
   the gap it was planned to borrow (`fit.borrowed_gap_s`; the original is
   ducked for that long too) and is sped up only for what is left.

   `fit_mode` (`audio` by default) can be `smart`, which also lets the
   *picture* give way (`dub_plan.fit_strategy`, `video_retime.py`). For each
   line, once its clip has been synthesized: (1) the free gap after the line
   is borrowed as far as the clip needs; (2) if it still does not fit, only
   the audio is sped up, up to x1.2 (`soft_audio`); (3) beyond that the rest
   of the need, `r / 1.2`, is split as its square root between more audio
   speed (at most x1.5, `audio_max`) and slowing the picture over that
   line's window (at most x2, `video_max`; a slow-down under x1.05 is not
   worth a cut and goes to the audio); (4) whatever is still left is cut off
   the end of the clip (`trim_s`). The choice is stored per segment in
   `fit.strategy` = `{kind: fits|gap|audio|audio+video|trimmed, borrow_gap_s,
   window_s, audio_factor, video_factor, trim_s}`. A video factor is a
   duration multiplier (2.0 is half speed). Lines that need no slow-down leave
   the video exactly as it was.

   When any line slows the picture, the video is cut into consecutive chunks
   (untouched ones at speed 1.0, the slowed windows stretched with
   `trim`/`setpts`) and joined again; more than 40 chunks are processed in
   batches that each read only their own stretch of the source and are joined
   with the concat demuxer (`-c copy`); a result that rounding left short is
   held on its last frame (`tpad`) and cut to the exact length. The same chunks
   are applied to the original or separated background track with `atempo`, so
   it stays in sync. Everything after a slowed window moves later: each
   segment's place in the new video is `timeline = {start_s, end_s}` (its
   `start_s`/`end_s` stay the source times), and the mix and the subtitles use
   it. The retimed video and background are kept in the work directory
   (`retimed.mp4`, `retimed_background.wav`) and reused by
   `resynthesize_segment` while the chunks do not change. The video is
   re-encoded (x264, CRF 18) only in this mode; `outputs.summary` adds
   `retimed` (lines with a slowed picture) and `added_s` (seconds the video
   grew). Slowing 25 fps footage to half speed repeats frames: it looks like
   slow motion, not interpolation. The background is stretched with `atempo`, which keeps its
   pitch; in a x2 window music and ambience audibly drag. Two source lines that
   overlap in time share one slowed window, so the second clip can come out a
   little longer than its place (rare: windows end at the next line).
7. **Quality check** (`qc`, on by default): the finished clip is transcribed
   again with the same speech-to-text engine as the source, in the target
   language, and compared with the text that was meant to be said: word error
   rate (case, punctuation and spacing ignored), or character error rate for
   ja, zh, ko and th. In Spanish and English, digits on either side are also
   tried written out in words (`dub_plan.spell_numbers`, standalone cardinals only; English years also as
   "twenty twenty four") and the lower rate wins, so a script saying "17"
   heard as "seventeen" is not an error. A line over the threshold
   (`qc_threshold`; default 0.35 WER, 0.25 CER) is `flagged`. Each segment gets `qc` = `{status, metric,
   wer|cer, hyp, threshold, flagged}`; without a working transcriber the
   segment gets `{status: "skipped", reason}` and the job still finishes. It
   runs on whatever the source transcription used (CPU-friendly with
   faster-whisper), so it adds roughly one short transcription per line.
   The clip is transcribed with its silent edges trimmed (Whisper invents
   words on a padded silent tail), and a line that is itself one of the
   phrases the transcriber drops as a silence artifact ("Thanks for
   watching") is `skipped` with reason `filtered_phrase` when nothing is
   heard, instead of being flagged.
8. **Mix and mux**: the dubbed line plays at the segment's start (in the
   retimed video, its `timeline.start_s`). The mix is built in numpy,
   streamed in 10 s chunks - one ffmpeg decoder for the background, each line
   read only while it plays - with the background ducked under each line by a
   gain envelope (`DUCK_DB = -18 dB`, 80 ms ramps) or fully replaced where a
   background separator is installed (`python -m demucs`; if it is missing or
   fails to start, the job falls back to ducking; a separated background is
   reused, never recomputed). One ffmpeg pass then loudness-normalises it, so
   hundreds of lines are never hundreds of ffmpeg inputs and nothing depends on
   filter options only newer ffmpeg builds have. The video stream is copied and
   muxed with the dubbed audio and target-language subtitles at its full length.

The job's `outputs.segments` (and the manifest) carry `plan` and `qc` per
segment, and `outputs.summary` counts `fits`, `tight`, `impossible`,
`shortened`, `flagged`, `qc_skipped` and `untranslated` (plus `retimed` and
`added_s` with `fit_mode="smart"`). Through MCP, `voice_job` on a finished
dub returns the first lines of that table under `result` (without file paths, with `segments_total` when there are more; `voice_dub_segments` pages through the rest); the Dub tab shows
a timing badge and the error rate per line, highlights flagged lines and shows
what was heard on hover.

Every stage's files are kept in the job's work directory (a `manifest.json`
records each segment's timing, source/translated text, translation steps, plan, fit, QC and engine,
and the job's `options` including `translation_mode` and `fit_mode`), so
`POST /api/voice/dub/{job_id}/segments/{index}/resynthesize` (or
`voice_resynthesize_segment`) can fix one line's translation and/or voice
and re-run just that segment (its plan is re-estimated and its QC redone) - and, with `remix=true` (the default), rebuild
the mixed audio and re-mux the final video - without repeating
transcription or translation for the rest. A new voice applies to that line
only (the dub's default voice is kept), the target language is passed to the
voice, one re-synthesis runs per dub at a time, the fixed line is written back
to the job's outputs, and when the dub was saved to a project the remixed
video becomes a new video asset (its id is returned).
`GET /api/voice/dub/{job_id}/download?file=video|subtitles` serves the
outputs; downloads only ever serve files under `data/voice_studio/`.

Agents see a compact view: `voice_job` shows a finished dub's first 20
lines and their total, and `voice_dub_segments(job_id, offset, limit)`
(`GET /api/agent/voice_dub_segments`) pages through the rest - no file
paths either way.

## Errors

Every voice-studio error is `{"error": "<code>", "message": "..."}`, 400
unless noted: `engine_not_installed`, `unknown_engine`,
`cloning_needs_sample`, `unknown_preset`, `silent_sample`, `unreadable`,
`stt_not_installed`, `text_required`/`too_long`, `source_required`,
`bad_format`, `bad_segment`, `job_not_done`, `no_installer`,
`unknown_voice`, `voice_download_failed`, `bad_language`,
`unsupported_language`, `bad_pitch`, `bad_lexicon`, `bad_preset_name`,
`chapter_too_long`, `not_dub_job`, and (409) `llm_unavailable` when
dubbing's translation step finds no local model resolved through Hoard Link.
