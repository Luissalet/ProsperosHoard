# Architecture

## Modules

```
prosperos_hoard/
  db.py           SQLite schema (WAL) + connection cache (one conn per thread)
  store.py        Store: every CRUD operation, no FastAPI/HTTP knowledge
  ids.py          ULID-like sortable ids ("a_...", "job_...", "proj_...")
  jobs.py         JobQueue: one GPU + one CPU worker thread, SQLite-backed
  hoard_link/     vendored shared model backend (see VENDORED.txt)
  backend.py      Prospero's own wrapper: VRAM estimates, ffmpeg/font status,
                  MusicBackend adapters (ComfyMusic, HttpMusic - neither installed)
  comfy_driver.py workflow templates, parameter mapping, object_info validation
  workflows/      *.json (API-format) + *.params.json (friendly-name map) per template
  devtools/fake_comfy.py   procedural ComfyUI stand-in for --demo and tests
  design.py       Pillow layout renderer (rect/image/text/holo/grain/frame/badge)
  templates.py    the 7 named design layouts
  fonts/          5 bundled OFL font families
  audio.py        ffmpeg decode, waveform peaks, beat tracker, LRC
  voices.py       Piper (on-demand HF download) + Hoard Link TTS
  timeline.py     Timeline domain model + auto-cut algorithm (pure Python)
  video.py        ffmpeg command builders + the actual renderer
  engine.py       the business logic behind every /api/agent/* route
  api.py          FastAPI app, guard middleware, all HTTP routes
  mcp_server.py   standalone stdio MCP adapter (httpx + mcp only)
  __main__.py     CLI entrypoint
```

`engine.py` is the seam: it has no FastAPI imports and is what
`tests/test_engine.py` exercises directly. `api.py` is a thin HTTP
wrapper around it (parsing request bodies, enqueueing jobs, formatting
errors), and `mcp_server.py` never imports the package at all - it only
calls the HTTP surface `api.py` exposes, so the MCP protocol test is a
genuine end-to-end proof rather than a shortcut.

## Data model

SQLite, WAL mode, one file at `<data_dir>/prosperos.sqlite3`. Tables:
`projects`, `assets` (with `recipe_json` = full lineage), `characters`,
`groups`, `style_presets` (6 built-in + project-specific), `boards`,
`timelines` (tracks stored as JSON), `jobs`, `agent_calls` (the audit
log). JSON columns are read/written through `store.py` only; nothing
else touches SQL directly.

Files live under `<data_dir>/`: `assets/` (originals), `thumbs/`
(512px WebP), `voices/` (downloaded Piper models), `fake_comfy/`
(demo backend's own input/output), `tmp/<job_id>/` (render scratch,
deleted after each render), `logs/app.log` (rotating).

## Threads and processes

- The FastAPI app runs on uvicorn's asyncio event loop in the main
  process/thread.
- `JobQueue` starts two daemon threads (`job-worker-gpu`,
  `job-worker-cpu`) that each loop: pop the oldest `queued` job for their
  lane, run its handler, write the result back to SQLite. A GPU handler
  that raises `WaitingForResources` is requeued and retried every 15s (up
  to a 30-minute timeout) instead of failing.
- ComfyUI calls go through vendored Hoard Link's async `Link`/
  `ComfyClient`, which runs its own background asyncio loop in a third
  daemon thread (`hoard-link-sync`); `Backend.run_async()` bridges a
  worker thread's synchronous call into that loop.
- `FakeComfyServer` (demo/tests) runs its own uvicorn instance in a
  fourth daemon thread on a free port.
- ffmpeg is always a plain `subprocess.run`/`Popen` from whichever worker
  thread is rendering; no shared ffmpeg process.

On restart, `Store.requeue_running_jobs()` (called from
`JobQueue.start()`) flips any job stuck `running`/`waiting_gpu` back to
`queued`, so an interrupted render or generation is retried rather than
lost or stuck forever.

## Decisions and deviations

- **Ken Burns parameterisation**: a design describing raw per-frame
  "start/end rect" pans; this implementation instead exposes
  `{zoom_start, zoom_end, pan: left|right|up|down|none}`, which maps onto
  a single ffmpeg `zoompan` invocation (the standard technique for this
  exact effect) instead of a bespoke per-frame `crop` expression graph.
  Documented in `timeline.py` and `docs/API.md`.
- **No librosa**: the beat tracker is a from-scratch spectral-flux onset
  envelope + autocorrelation tempo + a compact Ellis-style DP beat
  tracker (numpy/scipy only), as a deliberate fallback, to avoid
  librosa's heavier native-wheel chain (numba/llvmlite) on Windows
  cp313. Verified against 90/120/140 BPM synthetic click tracks
  (`tests/test_audio.py`).
- **studio_voice / studio_import / studio_analyze_audio / studio_design /
  studio_photocard_set / studio_timeline run synchronously**, not as
  jobs - their tools take no `wait_s` parameter,
  and each is fast enough (Pillow render, ffmpeg probe, or a Piper
  synth) that a job round trip would only add latency and UI complexity.
  Only ComfyUI generation/edit/animate (GPU) and timeline rendering
  (CPU, potentially slow) are real jobs.
- **QR layer**: recognised by the design renderer's layout schema but
  renders a placeholder box, not a real QR code - no QR library is in
  the pinned dependency set (see `design.py`).
- **Polling, not SSE**: the HTTP API has no streaming endpoint; the UI is
  expected to poll `/api/jobs` and `/api/agent-calls` (see `docs/API.md`).
- **Hoard Link vendoring**: it was still being built by a sibling agent
  when this repository was scaffolded; work proceeded against the public
  API in its own spec, and the final available snapshot was vendored at
  the end (see `prosperos_hoard/hoard_link/VENDORED.txt`). No vendored
  file was ever edited - `backend.py` only wraps it.
