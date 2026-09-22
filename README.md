# Prospero's Hoard
### One studio, every step remembered - could an agent direct a whole production?
**A local-first media studio that drives ComfyUI, ffmpeg and TTS to build consistent characters, photocards, album art and beat-cut music videos - by hand or entirely over MCP.**

[Español](README.es.md) · [Run locally](#run-locally-on-windows) · [Connect an AI](docs/MCP.md) · [Portfolio](https://luissalet.github.io/Portfolio/#projects)

![Frontend placeholder showing live API health data from the demo project](docs/media/01-frontend-placeholder.png)
*Actual application, demo data, fake generation backend (the real studio UI is not built in this pass - see "Boundaries" below). The panel is real JSON from a running instance with a seeded demo project.*

![An album cover rendered by the design engine over a fake-backend generated image](docs/media/04-album-cover.png)
*Actual output of `studio_design` (template `album_cover`, variant `bottom_band`) - the small caption burned into the art names the seed and prompt fragment, from the demo/test backend, not a real generator.*

![A full photocard set: front + back for all five demo cast members](docs/media/05-photocard-set.png)
*`studio_photocard_set` output: one front, one back with a serial number, per member - each using that member's own accent colour.*

## Why

Making a fan-style production (an idol group's photocards, album art,
character voices, a music video cut to a song) today means juggling a
node editor, an image editor, a DAW and a video editor by hand, and none
of them remember how any asset was made - re-creating a consistent
character means re-guessing the prompt and seed every time. A language
model asked to help is stuck describing what it would do; it cannot
actually run ComfyUI, cut a video on the beat, or tell you the exact
recipe that produced an image. Prospero turns each of those steps into a
single, typed, queueable operation with recorded lineage, so an agent
(or a human) can direct the whole production and reproduce any asset
exactly.

## What is implemented

| Area | Available now | Boundary |
| --- | --- | --- |
| Domain model | Projects, assets with full lineage (recipe -> reproducible), characters/@mentions, groups, 6 style presets, boards, timelines, a persistent job queue (SQLite, survives restart) | No multi-user/auth - single local user by design |
| ComfyUI generation | txt2img/img2img/inpaint/hires/SD1.5/image-to-video templates, `/object_info` pre-flight validation, VRAM estimate + wait (never unloads models), custom API-format workflow import with auto-detected parameter mapping | UI-format workflow paste is rejected with a clear message, not auto-converted |
| Demo backend | A procedural fake ComfyUI (deterministic Pillow renders) so the whole pipeline runs and is testable with no GPU | Obviously not real generative output - every render is watermarked with its seed/prompt in a caption |
| Design engine | 7 templates (photocard front/back, album cover x3 variants, teaser poster, lyric card, tracklist back, thumbnail), 5 bundled OFL font families, holo-foil and grain effects, text auto-fit, print/bleed mode | QR layer is recognised but renders a placeholder box - no QR library is pinned |
| Audio | ffmpeg import/decode, waveform peaks, a from-scratch beat tracker (spectral flux + autocorrelation + DP), LRC lyrics, Piper TTS with on-demand voice download, Hoard Link (Faustus) TTS fallback | Section labels ("section A/B", energy level) are heuristic guesses, not verified verse/chorus detection; no voice cloning of real people, anywhere |
| Music generation | A typed `MusicBackend` interface with two adapters (ComfyUI audio nodes, a documented minimal HTTP contract) | Neither is installed on the reference machine - both say so plainly with instructions to add one |
| Video | Image-to-video (SVD), a real ffmpeg timeline renderer (Ken Burns via `zoompan`, cut/crossfade/dip/flash transitions via `xfade`, burned ASS subtitles with karaoke), an auto-cut algorithm that lands cuts on beats | Ken Burns is a documented `{zoom, pan}` parameterisation, not arbitrary per-frame crop rects (see `docs/ARCHITECTURE.md`) |
| HTTP + MCP | Every capability has an `/api/agent/*` endpoint and a matching MCP tool with the same shape; a browser-attack guard middleware; an audited `agent_calls` log | No SSE/websocket - jobs and the activity log are meant to be polled (cheap SQLite reads) |
| Frontend | A minimal placeholder page proving the API/build pipeline works end to end | The real studio UI (sidebar, Generate/Library/Designer/Audio/Timeline views) is a separate, follow-up pass - `docs/API.md` documents every route it needs |

## Connect it to Faustus

The app declares itself with `faustus-plugin.json`. Start it, then in
Faustus: **Connectors -> Nearby apps -> Add**. It also works with any
MCP client over stdio - see `docs/MCP.md` for the config snippet and the
full tool table:

| tool | read-only | what |
| --- | --- | --- |
| `studio_status` | yes | backend/GPU/ffmpeg status |
| `studio_generate_image` / `studio_edit_image` / `studio_animate` | no | ComfyUI jobs |
| `studio_design` / `studio_photocard_set` | no | graphic design renders |
| `studio_analyze_audio` / `studio_voice` / `studio_import` | no/yes | audio pipeline |
| `studio_timeline` / `studio_render` | no | beat-synced video editing |
| `studio_show` / `studio_lineage` / `studio_assets` / `studio_jobs` | yes | inspection |

## Run locally on Windows

Double-click **`Iniciar Prospero's Hoard.cmd`**, or from PowerShell:
```powershell
scripts\start.ps1 --demo
```
Manual steps:
```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-lock.txt
.venv\Scripts\pip install -e .
cd frontend; npm ci; npm run build; cd ..
.venv\Scripts\python -m prosperos_hoard --demo
```
`--demo` seeds an original (invented) idol group, generates its
photocards/cover with the fake backend, synthesizes a real 120 BPM
demo song, and renders a beat-cut preview video, so the whole pipeline
can be tried and screenshotted with no ComfyUI installed. Real hardware:
point `data/backend.json` at your ComfyUI (`comfy.url`) - see
`docs/API.md`'s `/api/backend` section.

## Architecture

Modules, data model, threads and every documented deviation from the
original spec: `docs/ARCHITECTURE.md`. Every endpoint with request/response
examples: `docs/API.md`.

## Tests

```
pytest -q
```
60 tests, ~35s, entirely offline (the fake ComfyUI backend and a
synthetic click track stand in for real hardware). Covers: workflow
parameter mapping round trips and `/object_info` validation, UI-format
workflow rejection, job-queue persistence across a simulated restart and
the VRAM-wait/retry path, deterministic design-renderer golden hashes and
text auto-fit, the beat tracker against 90/120/140 BPM synthetic click
tracks (within ±1 BPM, beat times within 50 ms) and section detection,
auto-cut invariants (cuts on beats, minimum clip length, no immediate
repeats, covers the full song), ffmpeg command-construction snapshots
plus one real render, ASS karaoke timing, asset-file path-traversal
refusal, and a real MCP-protocol round trip (the adapter spawned over
stdio against the live app).

## Privacy and limits

Local-only: binds `127.0.0.1`, no telemetry, no network access beyond
what a triggered feature obviously needs (a Piper voice download from
Hugging Face, shown in the UI). No voice cloning of real people. The
`--demo` seed uses only invented characters. Data lives under `data/`
(gitignored) unless `--data-dir`/`PROSPERO_DATA_DIR` says otherwise.
