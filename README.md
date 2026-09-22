# Prospero's Hoard
### Such stuff as dreams are made on: can an agent direct a whole production?
**A local media studio that drives your ComfyUI, ffmpeg and a local TTS to make consistent characters, photocards, album art and music videos cut on the beat, by hand or entirely over MCP, and remembers exactly how every asset was made.**

[Español](README.es.md) · [Run locally](#run-locally-on-windows) · [Connect an AI](docs/MCP.md) · [Portfolio](https://luissalet.github.io/Portfolio/#projects)

![Generate screen: two cast members mentioned with @, the final prompt with their look inlined, the parameter panel and earlier results](docs/media/01-generate.png)
*Actual application, demo data. Every picture comes from the bundled demo backend, a procedural stand-in for ComfyUI that draws labelled placeholder scenes; with your ComfyUI connected the same screens show real SDXL/SD1.5 output.*

## Why

A language model asked to help with a fan production (an invented idol
group, its photocards, a cover, a video for the single) can only describe
what it would do. It cannot run a node graph, it cannot keep a character's
face consistent across forty images, it cannot find the beat of a song, and
nobody can later answer "which seed and checkpoint made this card?". Doing
it by hand means juggling a node editor, an image editor, an audio tool and
a video editor that share no memory.

Prospero turns each step into a typed, queueable operation with a recorded
recipe: characters are mentioned as `@Name` and their description is
inlined every time, every asset keeps the exact parameters that made it
(and can be re-run to the byte on the same backend), songs are analysed for
tempo and sections, and a timeline is cut on the beat and rendered with
ffmpeg. The local model directs; the studio does the work and reports back
with ids and pictures.

![Timeline: 23 shots cut on the beat of the demo song, the lyric captions, the waveform, the rendered preview and the clip editor](docs/media/02-timeline.png)
*Actual application, demo data: the auto-cut of the 30 s demo song (120 BPM, quiet-loud-quiet), two beats per shot in the loud section, four in the quiet ones, and the preview it rendered with ffmpeg.*

## What is implemented

| Area | Available now | Boundary |
| --- | --- | --- |
| Projects and cast | Projects, characters (look prompt, negative, palette, canonical reference, voice), ordered groups, `@Name` mentions that match multi-word names and report unknown ones, 6 style presets | Single local user; names must be unique per project (they are the mention) |
| Generation (ComfyUI) | SDXL txt2img, img2img, inpaint and two-pass upscale, SD 1.5 txt2img, SVD image-to-video, all as API-format templates; pre-flight check of nodes, checkpoints, samplers and schedulers against `/object_info` with the installed options in the error; import of your own API-format workflows with prompt nodes found through the sampler links and an editable parameter map | Prospero hosts no model; UI-format exports are refused with instructions, not converted |
| GPU etiquette | VRAM estimate per workflow family (editable) checked against nvidia-smi or ComfyUI's `system_stats`; short jobs wait in `waiting_gpu` with the reason, every 15 s for up to 30 min; cancel at any time | Nothing is ever unloaded unless you press "Free ComfyUI memory" |
| Lineage | Every generated asset records template, template hash, checkpoint, every parameter and seed, inputs and timing; "Reuse recipe" reproduces an image byte for byte on the same backend (tested), "Vary seed" re-runs it with new seeds | Reproduction is only guaranteed on the same backend, models and ComfyUI version |
| Design | Pillow renderer, no browser: photocard front and back, album cover (3 layouts), teaser poster, lyric card, tracklist back, thumbnail; gradients, holographic foil, blends, letter spacing, shadows, shrink-to-fit text; photocard sets for a whole group with a contact sheet; print mode with 3 mm bleed at 300 dpi; 5 bundled OFL font families | The QR layer draws a placeholder box (no QR library is pinned) |
| Audio | Import (mp3, wav, flac, ogg, m4a), waveform, own beat tracker (band-balanced spectral flux, tempo prior, dynamic programming) tested within 1 BPM and 50 ms on click tracks and kick-and-snare patterns at 90-140 BPM, downbeat and section estimates, LRC lyrics with a tap-to-time tool | Section labels are "section A/B" with low/mid/high energy, not verse/chorus; very fast songs (170 BPM) are reported at half time |
| Voices | Piper TTS, six curated Spanish and English voices downloaded on first use; Faustus TTS through Hoard Link with Piper as fallback; per-character voice and speed | Generic synthetic voices only: no voice cloning of anyone |
| Music generation | A `MusicBackend` interface with two adapters (ComfyUI audio nodes, a small documented HTTP contract) | Not installed by default: both say so and explain how to add one; imported songs work fully |
| Video | Beat-synced auto-cut (density per energy, flashes on phrase downbeats, no immediate repeats, whole song covered) into an editable timeline; ffmpeg renderer with Ken Burns moves, cut/crossfade/dip/flash transitions that keep cuts on the beat, burned lyric captions with optional karaoke, the song muxed in; 540p preview or 1080p final; SVD clips converted to mp4 | Ken Burns is a zoom range plus pan direction, not free start/end rectangles |
| Agent control | 20 MCP tools mirroring `/api/agent/*`, compact id-first results, pictures of finished work, errors with a code and a next step, an audited "What the assistant did" log | Jobs are polled (`studio_job` can wait server-side); no push events |
| Interface | React studio: Overview, Cast, Generate, Library with lightbox, Designer, Audio, Timeline, Boards, Jobs, Backends, Assistant activity, Settings; dark and light, Spanish and English, keyboard shortcuts | Timeline editing is clip-level (duration, transition, camera, order, swap), not frame-level |

![Library lightbox on the photocard set: ten cards and the recipe panel with reuse, vary, upscale and animate](docs/media/03-photocards.png)
*Actual application, demo data: the photocard set rendered for the five invented members, opened in the lightbox with its recipe and inputs.*

## Connect it to Faustus

The app declares itself with [`faustus-plugin.json`](faustus-plugin.json).
Start it, then in Faustus open **Connectors -> Nearby apps -> Add**: Faustus
finds it on port 8815, reads the manifest and launches the MCP adapter
(`prosperos_hoard/mcp_server.py`, stdio). The model backend is shared: the
studio asks Hoard Link which ComfyUI and TTS are already running instead of
loading anything of its own.

| Tool | What it does | Read-only |
| --- | --- | --- |
| `studio_status` | Backends, checkpoints, free VRAM, queue | yes |
| `studio_projects` / `studio_create_project` | List or create productions | yes / no |
| `studio_cast` | List, create, update characters and groups | no (list is read-only) |
| `studio_generate_image` | Queue txt2img/img2img with @mentions and presets | no |
| `studio_edit_image` | img2img, inpaint, upscale, reuse recipe, vary seed | no |
| `studio_animate` | Image to short video (SVD) | no |
| `studio_voice` | Spoken line with a character's voice | no |
| `studio_import` | Import a local file from an allowed folder | no |
| `studio_analyze_audio` | Tempo, beats, sections | yes |
| `studio_design` / `studio_photocard_set` | Render a design / a whole photocard set | no |
| `studio_timeline` / `studio_render` | Auto-cut, read, edit a timeline / render it | no |
| `studio_jobs` / `studio_job` / `studio_cancel_job` | Queue, one job (with wait), cancel | yes / yes / no |
| `studio_assets` / `studio_show` / `studio_lineage` | Find assets, look at them, their recipe | yes |

It works with any MCP client over stdio too:

```json
{"mcpServers": {"prosperos-hoard": {
  "command": "C:/.../Prospero's Hoard/.venv/Scripts/python.exe",
  "args": ["C:/.../Prospero's Hoard/prosperos_hoard/mcp_server.py"],
  "env": {"PROSPERO_URL": "http://127.0.0.1:8815"}}}}
```

Arguments, result shapes and limits of every tool: [docs/MCP.md](docs/MCP.md).
The end-to-end recipe the agent follows: [skills/idol-production/SKILL.md](skills/idol-production/SKILL.md).

## Run locally on Windows

Double-click **`Iniciar Prospero's Hoard.cmd`** (or run `scripts\start.ps1`).
The first run creates `.venv` with Python 3.13, installs
`requirements-lock.txt`, builds the interface with npm and opens
http://127.0.0.1:8815. `Detener Prospero's Hoard.cmd` stops it (only after
checking that the port really belongs to Prospero).

Manual steps:

```powershell
C:\Python313\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
cd frontend; npm ci; npm run build; cd ..
.venv\Scripts\python.exe -m prosperos_hoard            # real data in .\data
.venv\Scripts\python.exe -m prosperos_hoard --demo     # demo data in .\data-demo
```

Flags: `--port`, `--data-dir` (or `PROSPERO_DATA_DIR`), `--demo`,
`--no-browser`. `--demo` starts the procedural demo backend, seeds an
original five-member group with portraits, stage shots, a cover, a photocard
set, a synthetic 30 s song with timed lyrics and an auto-cut timeline, and
renders its preview video. To use your ComfyUI, leave it on
127.0.0.1:8188 (Hoard Link finds it) or set its URL in Settings.

![Audio screen: the demo song at 120 BPM with its beat ticks and A/B/A sections, the lyrics timing tool and voice lines](docs/media/04-audio.png)
*Actual application, demo data: the synthetic demo song analysed by the built-in beat tracker, with the section estimates and the LRC lyrics timed to it.*

## Architecture

FastAPI + SQLite (WAL, one connection per thread) with a GPU worker and a
CPU worker thread over a persistent job table; business logic in plain
modules (`engine`, `comfy_driver`, `design`, `audio`, `timeline`, `video`,
`voices`) with no web imports; Hoard Link vendored for backend resolution;
the MCP adapter is a separate stdio script that only speaks HTTP to the app.
Details, data model and decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Every endpoint: [docs/API.md](docs/API.md).

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
cd frontend; npm ci; npm run build
```

The last full run: **TEST_COUNT tests passed** in about 40 s, offline, with
the demo backend standing in for ComfyUI. They cover: the MCP protocol end
to end (the adapter spawned over stdio against a live app: tool keywords and
annotations, generation with a picture, lineage, design, readable errors,
the app-not-running message); byte-identical reproduction through "reuse
recipe"; @mention edge cases; checkpoint, sampler and missing-node
validation; hostile workflow imports; import traversal, symlinks, renamed
files and the allowed-folder rule; SPA and asset path traversal; the
browser-attack guard; job restart recovery, VRAM waiting and cancellation;
the beat tracker on click tracks, drum patterns and the demo song; auto-cut
invariants; ASS escaping of hostile lyrics; real ffmpeg renders in a folder
named like the Windows install (apostrophe, spaces, accents); design
golden hashes, bleed and text fitting; thread safety of the store; and the
Faustus manifest check.

## Privacy and limits

Local only: the app binds 127.0.0.1, sends no telemetry and only goes
online when you ask for a Piper voice that is not downloaded yet (from the
rhasspy/piper-voices repository on Hugging Face). Writes from other
websites are refused (Host, Origin and Sec-Fetch-Site checks). Agents can
import files only from your home folder, `data/inbox` and folders you add
in Settings, and only files whose content matches their type. There is no
voice cloning, and the demo uses invented people only. Data stays in
`data/` (or your `--data-dir`); the Faustus token is stored in
`data/backend.json` and never returned by the API.

MIT licence. Fonts under the SIL Open Font License (see `prosperos_hoard/fonts/*/OFL.txt`).
