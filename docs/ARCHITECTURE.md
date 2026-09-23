# Architecture

## Modules

```
prosperos_hoard/
  __main__.py      CLI: --port, --data-dir, --demo, --no-browser; uvicorn on 127.0.0.1
  api.py           FastAPI app: guard middleware, JSON errors, /api/agent/* (compact,
                   logged) and the richer UI routes, SPA serving
  engine.py        the business logic behind every route (no FastAPI imports)
  store.py, db.py  SQLite (WAL, per-thread connections, schema v2 with in-place upgrade)
  jobs.py          JobQueue: one GPU worker + one CPU worker thread, cancel, VRAM waits
  comfy_driver.py  workflow templates, custom workflow import/validation, parameter map,
                   /object_info pre-flight, checkpoint resolution, template hash
  workflows/       API-format *.json + *.params.json per built-in template, plus
    convert.py     UI-format -> API-format converter (see "Workflow conversion") and
                   validate_values(): the checks ComfyUI's /prompt runs
  backend.py       wrapper around the vendored Hoard Link: ComfyUI client on Hoard
                   Link's loop, free VRAM, "free ComfyUI memory", import folders,
                   overrides in data/backend.json, MusicBackend adapters
  hoard_link/      vendored shared model-backend resolver (see VENDORED.txt, never edited)
  design.py        Pillow layout renderer (rect/gradient, image with blur, text with
                   columns, holo, grain, vignette, frame, badge, qr placeholder),
                   blends, bleed
  templates.py     the 7 named layouts, their variants ("night" = horror look) and
                   their field contract
  fonts/           5 bundled OFL families + Special Elite (Apache 2.0)
  audio.py         ffmpeg decode/probe, waveform, onset envelope, tempo, beat tracker,
                   downbeats, sections, LRC, lyric timing from [Section] tags + bars
  voices.py        Piper (curated voices, atomic download) and Faustus TTS via Hoard Link
                   for character narration (studio_voice) - unrelated to the voice studio below
  voice_engines.py registry of pluggable TTS/STT engines (optional-import, install hints,
                   never downloads a model on its own); see VOICE.md
  voice_lab.py     voice library: sample processing (trim, loudnorm), quality check, storage
  voice_pipelines.py long-form narration: chapters/sentences, per-chapter synthesis job,
                   ffmpeg assembly (mp3/m4b), SRT/LRC export
  dubbing.py       dub pipeline: extract, transcribe, translate (Hoard Link), synthesize,
                   time-fit, mix/mux; per-segment re-run
  timeline.py      auto-cut, edit validation, clip updates, compact view (pure Python)
  video.py         ffmpeg command builders, ASS subtitles and escaping, renderer,
                   animated WebP -> mp4
  procutil.py      subprocess helpers: CREATE_NO_WINDOW on Windows, UTF-8 decoding
  mcp_server.py    standalone stdio MCP adapter (stdlib + httpx + mcp only)
  devtools/        fake_comfy.py (procedural ComfyUI stand-in), demo_seed.py (--demo data)
frontend/          React 19 + Vite + TypeScript UI, built to frontend/dist
```

`api.py` stays thin: each operation (`op_generate`, `op_timeline`, ...) is a
small function used by both the agent route (compact result, logged in
`agent_calls`) and the UI route (full rows). `mcp_server.py` never imports the
package; the MCP protocol test spawns it over stdio against a live server, so
it proves the HTTP surface alone is enough.

## Data model

One SQLite file, `<data>/prosperos.sqlite3`, WAL mode:

- `projects` (name, brief, cover asset)
- `assets` (kind image|video|audio|lyrics|font, name, file path relative to
  the data folder, mime, size, duration, thumbnail, waveform, cached audio
  analysis, tags, rating, favourite, notes, source
  import|generated|derived|rendered, `recipe_json`)
- `characters` (role, bio, prompt, negative, palette, references, canonical
  reference, voice), `groups` (ordered member ids, logo, colours)
- `style_presets` (6 built-in, refreshed on start-up; project presets possible)
- `boards` (ordered `{asset_id, note}` items), `timelines` (aspect, fps, size,
  song, `tracks_json`, `finishing_json` - colour grade/grain/vignette/
  letterbox/glitch/lyric style, added in schema v3 with an in-place `ALTER
  TABLE` on older databases)
- `jobs` (type, lane gpu|cpu, params, state, progress, message, outputs, log
  excerpt, cancel flag, timestamps), `agent_calls` (tool, arguments summary,
  duration, ok, error)
- `studio_voices` (schema v5): a saved voice-studio voice - engine, an
  optional processed sample path, language, whether it was cloned from a
  sample, a reference transcript, a quality report, named presets, tags

A **recipe** for a ComfyUI asset holds `operation, backend, template,
template_hash, checkpoint, params` (every friendly parameter written into the
workflow, including the seed), `input_asset_ids, elapsed_s, job_id,
created_at` and for re-runs `derived_from, rerun`. Re-running it builds the
same workflow graph, so the same backend returns the same image (the test uses
the deterministic demo backend and compares pixel hashes).

Files under `<data>/`: `assets/`, `thumbs/` (WebP 512), `voices/` (Piper),
`voice_studio/voices/<voice>/sample.wav` (processed voice-library samples),
`voice_studio/audiobooks/<job>/` and `voice_studio/dub/<job>/` (per-job
work directories: chapter/segment files, the manifest a dub job's
per-segment re-run reads back), `workflows/` (imported workflows), `inbox/`
(always-allowed import folder), `tmp/` (render and upload scratch),
`logs/app.log` (rotating), `backend.json` (overrides and the Faustus
token), `fake_comfy/` in demo mode.

## Threads and processes

- uvicorn's event loop serves HTTP; sync route handlers run in its thread pool.
  Every thread gets its own SQLite connection (a single shared connection
  interleaved transactions between the pool and the workers).
- `JobQueue` runs two daemon threads, `job-worker-gpu` and `job-worker-cpu`,
  each taking the oldest `queued` job of its lane. A GPU handler that finds too
  little free VRAM raises `WaitingForResources`: the job shows `waiting_gpu`
  with the reason and keeps its place in the lane, retrying every 15 s for up
  to 30 minutes. Cancelling a queued or waiting job is immediate; a running
  handler stops at its next progress call (ComfyUI polling asks ComfyUI to
  interrupt, ffmpeg is killed).
- Hoard Link's sync facade owns a private event-loop thread; ComfyUI calls run
  there through `Backend.run_async()`.
- On start-up, jobs left `running` or `waiting_gpu` by a crash go back to
  `queued` (restart recovery, tested).
- No `multiprocessing` anywhere; uvicorn runs the app object in-process with no
  reload or workers, so Windows' spawn start method is never involved.
- Child processes (ffmpeg, ffprobe) are started through `procutil` with
  `CREATE_NO_WINDOW` on Windows and UTF-8 decoding; `nvidia-smi` goes through
  Hoard Link, which does the same.

## Workflow conversion

ComfyUI saves what its editor shows (UI format: `nodes`, `links`,
`definitions.subgraphs`, widgets as positional `widgets_values`); `/prompt`
wants API format (`{id: {class_type, inputs}}`, every input a literal or a
`[node_id, slot]` link). `workflows/convert.py` walks the UI graph against an
`/object_info` (live, or the cached `data/comfy/object_info.json`):

- inputs are taken in schema order (required, then optional); a widget-typed
  input consumes the next `widgets_values` slot even when a link feeds it
  (the link wins), plus the UI-only `control_after_generate` slot after a
  seed; missing trailing "advanced" widgets fall back to their defaults
- a **dynamic combo** (`COMFY_DYNAMICCOMBO_V3`, e.g. `SaveVideo.format`)
  consumes its chosen option's own inputs next, emitted flattened as
  `format.codec` (recursively) - the server requires them
- **autogrow** sockets (`COMFY_AUTOGROW_V3`) are emitted under their own
  flattened names (`images.image_1`) when linked
- `PrimitiveNode` and `Reroute` resolve to the value or link they carry;
  bypassed nodes pass a same-typed input through, muted ones drop out;
  notes are skipped, and so are socketless UI widgets except the few the
  frontend still sends (`ImageCompare.compare_view` as `["", ""]`)
- a list-valued literal is sent as `{"__value__": [...]}` (the frontend's
  wrapping, so it is never read as a link); a combo with no saved slot and
  no schema default (the hidden legacy `SaveVideo.codec`) takes its first
  option, as the frontend's widget does
- **subgraph** instances are expanded in place (inner ids become
  `<instance>:<inner>`); a subgraph input is matched to the instance's socket
  by name, and an unlinked promoted widget takes its value from the
  instance's `widgets_values` (one per widget-typed subgraph input, in order)

`tests/fixtures/comfy/` holds the official templates of a real ComfyUI install
(comfyui-workflow-templates 0.11.68, ComfyUI 0.37) and, for each, the API
prompt the real frontend produced (`graphToPrompt()` in a headless browser);
`test_convert.py` requires the converter to match it input for input (only
seed values, which the frontend randomises, are not compared). The pre-0.37
ACE-Step export (`v0.34/`, saved with `SaveAudioMP3`) is kept as a
regression fixture.
`validate_values()` then runs what `/prompt` checks - required inputs
(dynamic-combo children included), links to existing output slots, combo
choices, number ranges - and `run_template` calls it on every workflow
before queueing; the fake ComfyUI rejects the same prompts the real server
would. A combo mismatch on a **model-file input** (`MODEL_FILE_INPUTS`:
`CheckpointLoaderSimple.ckpt_name`, `UNETLoader.unet_name`,
`CLIPLoader.clip_name`, `DualCLIPLoader.clip_name1`/`clip_name2`,
`VAELoader.vae_name`) gets its own `"model not installed: ... download it,
or choose one of: ..."` wording instead of the generic "is not available"
one, since that is a download away, not a broken workflow;
`comfy_driver.validate_against_object_info`'s `checkpoint_node` cross-check
(a single `CheckpointLoaderSimple`) gives the same friendly, pre-flight
version for SDXL/SD1.5/ACE-Step's checkpoints, with the installed list in
the message.

The built-in Flux/Kontext/Wan/ACE/Qwen-Image 2.1 templates were converted
this way and then hand-checked node by node; each `*.params.json` carries
its `defaults` (sampler, steps, cfg, size, fps, length) so no SDXL fallback
ever reaches a Flux, Wan or Qwen graph, and Kontext samples into an
`EmptySD3LatentImage` of the requested size (the reference still conditions
through `ReferenceLatent`). Qwen-Image 2.1's official templates additionally
use a `ResolutionSelector` (aspect-ratio preset + megapixel budget) that the
built-in templates replace with a literal, editable `width`/`height` on
`EmptyLatentImage`, and, for the edit template, a `ComfySwitchNode` whose
`switch` (the `custom_size` parameter) picks that canvas when `true` or
`TextEncodeQwenImage21`'s own reference-sized latent when `false` (the
default). Its `images.image_1..N` autogrow socket (1-10 references, the
first the edit target/main identity) is not fixed at build time: only
`image_1`/`image_2` ship in the template file, and
`comfy_driver.wire_reference_group()` adds or drops `LoadImage` nodes 3-10
and their `images.image_N` links at run time to match how many references
a call actually gives (`engine.run_template`'s `reference_group` spec key),
so the graph queued never carries an unused reference socket.

## Image engine choice

`engine.resolve_image_engine(object_info, requested)` turns `auto | qwen21
| flux | sdxl` into the engine a call actually gets: `auto` (the default,
everywhere) picks Qwen-Image 2.1 when both its node class
(`TextEncodeQwenImage21`) and a model file containing "qwen" are in
`/object_info`'s `UNETLoader.unet_name` list, else Flux schnell when a
"flux" checkpoint is installed, else SDXL, which - like Flux schnell -
always works, since both ship as built-in checkpoints/templates. An engine
requested by name that turns out not to be installed falls back the same
way, so a project already set to `qwen21` keeps rendering before the model
finishes downloading, instead of failing. `engine.ENGINE_TEMPLATES` maps
each engine to its `txt2img`/`edit` built-in template name;
`engine.generate_image` resolves this once per call (skipped entirely when
the caller names an exact `template`) and records which engine was used in
the asset's recipe (`image_engine`). The choice has two levels: a
project's own `image_engine` column (`auto` by default, set at
`studio_create_project` or `PATCH /api/projects/{id}`) and a per-call
`engine` parameter on `studio_generate_image`, which wins when given.
`consistent=true` character-consistency generation routes through the
resolved engine's `edit` template too (`engine.build_kontext_instruction`'s
`engine=` parameter phrases the instruction Qwen-Image 2.1's way,
addressing references as `<image1>`, `<image2>`..., or Flux Kontext's
"the reference image" otherwise).

## Rendering pipeline

1. Each visual clip is rendered to `clip_NNN.mp4` at the target size: images
   with a `zoompan` Ken Burns move on a 2x-scaled cover crop, videos trimmed,
   cover-cropped and padded with their last frame if shorter than the clip.
2. All-cut timelines are joined with the concat demuxer, listing clips by
   relative name. With any real transition, everything moves to the frame
   grid: each clip starts on the frame nearest its beat, is rendered a whole
   number of frames (`-frames:v`) plus the next overlap and one spare frame,
   and the clips are chained with `xfade` (each input pinned to the
   timeline's fps) at those exact starts - so cuts stay on the beat and the
   video keeps the song's length (float offsets drifting past frame-rounded
   clips used to end the chain early, silently).
3. If the timeline has a `finishing` config, its filters (colour grade
   `eq`/`colorbalance`/`curves`, `noise` grain, `vignette`, letterbox
   `drawbox` bars, downbeat `chromashift` glitches timed to the clips the
   auto-cut already marked as strong downbeats) are built into one `-vf`
   chain that runs on the whole joined cut, before the captions.
4. Lyrics become an ASS file (escaped: braces, backslash codes and newlines
   cannot inject tags or events; karaoke `\k` per word; the `horror` lyric
   style swaps in a condensed uppercase face that lights each word from
   fog grey to bone white as it is sung - weighted by syllables, within two
   bars - with a soft dark outline, a short fade and a per-line
   rotation/shear jitter seeded from the line text, sized from the short
   side and kept above the bottom fifth on vertical video). ffmpeg runs the final pass with the
   work folder as its cwd and `ass=lyrics.ass:fontsdir=fonts`, because the
   filter-graph parser treats the drive colon and the apostrophe of
   `C:\...\Prospero's Hoard\` specially.
5. The song is muxed with `-shortest`; progress comes from `-progress pipe:1`;
   stderr goes to a temporary file so it can never block the pipe.

## Audio analysis

Centred STFT frames, onset strength = positive log-flux summed over 40
log-spaced bands (so a kick counts as much as a broadband snare), minus a local
mean. Tempo = autocorrelation peak weighted by a one-octave log-normal prior
around 120 BPM, refined parabolically; beats = Ellis-style dynamic programming,
extended to the first and last onsets; BPM = regression slope of the beat grid.
Downbeat phase = the beat phase with the most low-frequency onset energy.
Sections: per-bar loudness and three band levels, boundaries at novelty peaks
(loudness change plus a quarter of the timbre change, at least 4 dB, two bars
apart), segments that sound alike share a letter, energy relative to the song.

## Decisions and deviations

- **Ken Burns** is `{zoom_start, zoom_end, pan}` rather than free start/end
  rectangles: it maps onto one `zoompan` filter.
- **No librosa**: a numpy/scipy tracker instead, because librosa's native
  chain is a risk on cp313 Windows; the tracker above is tested on click tracks,
  kick-and-snare patterns (90, 100 with hats, 120, 140 BPM) and the demo song.
- **Synchronous operations**: voice, import, analysis, design, photocard sets and
  timeline building return directly (each takes seconds at most); generation,
  edits, animation, renders and voice downloads are jobs. So are the voice
  studio's `audiobook` and `dub` pipelines and `install_voice_engine` (a
  `pip install` in the app's own interpreter, never run without the user
  asking); an audiobook or dub job re-run with the same inputs resumes from
  whatever chapters/segments already rendered instead of redoing them.
- **Voice engines are optional imports**: `voice_engines.py` checks
  installability with `importlib.util.find_spec` (cheap, no import) and only
  imports the real library inside a synthesis/transcription call, so a
  machine with none of them installed still starts instantly and reports
  clear install hints. See [VOICE.md](VOICE.md) for the pipelines themselves.
- **Default auto-cut pool** is the project's generated and imported pictures and
  clips; rendered designs (cards, covers, contact sheets) are used only when
  passed explicitly or through a board.
- **QR layer** renders a placeholder (no QR library pinned).
- **Polling, no SSE**: the UI polls `/api/jobs` (1.2 s while something runs, 5 s
  otherwise); agents use `studio_job(wait_s=...)`.
- **Hoard Link** is vendored byte for byte at the version in
  `hoard_link/VENDORED.txt`; only `backend.py` wraps it.
- **Import folders**: agents may pass paths, so imports are limited to the home
  folder, `data/inbox` and folders added in Settings, with symlinks and `..`
  resolved first and content checked against the extension.
- **Lyric timing is an estimate** (`studio_time_lyrics`): it reads the
  structure from the lyrics' `[Section]` tags and the song's bars, not from
  the vocals (no source separation or alignment model is in the dependency
  set); re-time it by ear in Audio > Lyrics timing.
- **`flux_kontext_edit` is single-reference only**: the official template's
  multi-reference image-stitching path is not exposed; `wan22_ti2v` is
  image-to-video only (a still becomes the start frame) - its pure
  text-to-video path (bypassing `LoadImage`) is not exposed either. Both are
  scope cuts, not converter limitations (the converter itself expands
  either path correctly).
- **Checkpoint resolution** (a style preset saying `sd_xl_base_1.0` finds
  `sd_xl_base_1.0.safetensors`) runs for `CheckpointLoaderSimple`-based
  templates; for every template, `validate_values()` checks every model file
  (UNet, text encoders, VAE included) against `/object_info` before queueing.
- **Finishing colour grades** are single-pass `eq`/`colorbalance`/`curves`
  approximations named for their look (teal-orange, sodium-night, bleach
  bypass), not a calibrated 3D LUT.
- **`include_image` defaults to false** on every MCP tool that can attach a
  picture: a text-only local model (e.g. behind llama.cpp) handed an
  `ImageContent` block mid-turn breaks; call `studio_show` explicitly, or
  pass `include_image=true`, once a picture is actually wanted.
