# HTTP API

Base URL `http://127.0.0.1:8815`. JSON in and out. Errors are always
`{"error": "<code>", "message": "<what to do>"}` with a 4xx status (404
`not_found`, 400 for validation, 409 `<capability>_unavailable` when no
backend resolves, 422 `invalid_arguments` for malformed bodies, 502
`backend_error` when ComfyUI/Faustus failed a call).

**Guard (every route):** the `Host` header must be `127.0.0.1:<port>`,
`localhost:<port>` or `[::1]:<port>` (DNS rebinding); writes (not
GET/HEAD/OPTIONS) with a foreign `Origin` or `Sec-Fetch-Site: cross-site` are
refused. No CORS headers are sent. Unknown `/api/...` paths answer JSON 404;
everything else serves `frontend/dist` with an SPA fallback that never leaves
that folder.

## Agent routes (what the MCP tools call)

Compact, id-first results; every call is logged in `agent_calls`.

| Method | Path | Body / query |
| --- | --- | --- |
| GET | `/api/agent/studio_status` | - |
| GET | `/api/agent/studio_projects` | `?query&limit&offset` |
| POST | `/api/agent/studio_create_project` | `{name, brief?}` |
| POST | `/api/agent/studio_cast?project=` | `{action, kind, id?, name?, fields}` |
| POST | `/api/agent/studio_generate_image?project=` | `{prompt, style?, negative?, aspect?, width?, height?, steps?, cfg?, sampler?, scheduler?, seed?, count, reference_asset_id?, strength?, template?, checkpoint?, use_character_reference, consistent, wait_s}` |
| POST | `/api/agent/studio_edit_image` | `{asset_id, operation, prompt?, strength?, mask_asset_id?, count, seed?, width?, height?, wait_s}` |
| POST | `/api/agent/studio_animate` | `{asset_id, frames, fps, motion, seed?, wait_s}` |
| POST | `/api/agent/studio_compose?project=` | `{tags, lyrics, bpm, duration, key, language, time_signature, seed?, count, wait_s}` -> job (ACE-Step 1.5; an mp3/wav audio asset) |
| POST | `/api/agent/studio_voice?project=` | `{text, character_id?, voice?, speed?}` |
| POST | `/api/agent/studio_import?project=` | `{path, kind?}` |
| POST | `/api/agent/studio_analyze_audio?asset_id=` | - |
| POST | `/api/agent/studio_design?project=` | `{template, fields, image_asset_id?, variant?, options}` |
| POST | `/api/agent/studio_photocard_set?project=` | `{group_id, template_front, template_back, image_asset_ids?}` |
| POST | `/api/agent/studio_timeline?project=` | `{action, song_asset_id?, asset_ids?, board_id?, aspect, lyrics_asset_id?, options, timeline_id?, patch}` |
| POST | `/api/agent/studio_render` | `{timeline_id, quality, wait_s}` |
| GET | `/api/agent/studio_jobs` | `?state&limit&offset` |
| GET | `/api/agent/studio_job` | `?job_id&wait_s` |
| POST | `/api/agent/studio_cancel_job?job_id=` | - |
| GET | `/api/agent/studio_assets` | `?project&kind&query&tag&favourite&limit&offset` |
| GET | `/api/agent/studio_show` | `?asset_ids=a,b,c&size=768` -> `{items:[{asset_id, kind, mime, base64, order?}]}` |
| GET | `/api/agent/studio_lineage` | `?asset_id` |

Shapes and limits: see [MCP.md](MCP.md).

Example:

```http
POST /api/agent/studio_generate_image?project=proj_01M35C...
{"prompt": "@Iris Volt studio portrait", "style": "Studio portrait", "seed": 11, "wait_s": 30}

200 {"job": {"id": "job_01M35D...", "type": "generate_image", "state": "done", "progress": 1.0,
             "message": "done", "asset_ids": ["a_01M35D..."], "assets": [{"id": "a_01M35D...", "kind": "image",
             "name": "studio portrait photography, ...", "source": "generated", "width": 1024, "height": 1024,
             "recipe": {"operation": "generate_image", "template": "sdxl_txt2img", "seed": 11, "prompt": "..."}}]},
     "final_prompt": "studio portrait photography, softbox lighting, shallow depth of field, a confident young pop idol ...",
     "negative_prompt": "extra limbs, blurry face, blurry, deformed, extra fingers, watermark, text",
     "matched_characters": ["Iris Volt"], "unknown_mentions": [], "template": "sdxl_txt2img", "seed": 11}
```

## UI routes

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | `{service: "prosperos-hoard", name, version, status: "ok", demo, projects, active_jobs}` |
| GET / POST | `/api/backend` | status: Hoard Link per capability, ComfyUI (checkpoints, devices, VRAM), ffmpeg, Piper, fonts, music adapters, VRAM estimates, overrides, `token_set`; POST `{faustus_url?, faustus_token?, comfy_url?, vram_estimates_mb?, import_roots?}` (empty string clears) |
| POST | `/api/backend/comfy/free` | ask ComfyUI to unload models (only on user request) |
| GET | `/api/agent-calls?limit=` | the audit log |
| GET / POST | `/api/projects` | list (with counts) / create `{name, brief?}` |
| GET / PATCH | `/api/projects/{id}` | `{name?, brief?, cover_asset_id?}` |
| GET / POST | `/api/projects/{id}/characters` | create `{name, fields}` |
| PATCH | `/api/characters/{id}` | `{name?, fields}` |
| GET / POST | `/api/projects/{id}/groups` | create `{name, fields: {concept, member_ids, colours, logo_asset_id}}` |
| PATCH | `/api/groups/{id}` | `{name?, fields}` |
| GET | `/api/style-presets?project=` | presets with defaults |
| POST | `/api/projects/{id}/compose-prompt` | `{prompt, negative?, style?}` -> final prompt preview (`positive_prompt, negative_prompt, matched_characters, unknown_mentions, reference_asset_id, style_defaults`) |
| POST | `/api/projects/{id}/generate` | same body as the agent route; returns the full job |
| POST | `/api/assets/{id}/edit` | `{asset_id, operation, ...}` |
| POST | `/api/assets/{id}/animate` | `{asset_id, frames, fps, motion}` |
| GET | `/api/workflows` | `{builtin: [spec], custom: [spec]}` (built-ins now include `flux_schnell_txt2img`, `flux_kontext_edit`, `wan22_ti2v`, `ace15_song` alongside SDXL/SD1.5/SVD) |
| POST | `/api/workflows/import` | `{name, workflow}` (UI **or** API format - a UI export with `nodes`/`links`/subgraphs is converted first) -> proposed spec with `map` |
| POST | `/api/workflows/import-file` | multipart `file` (.json, at most 2 MB) |
| PATCH | `/api/workflows/{wf_id}` | `{name?, map?, vram_class?, kind?, output_node?, reference_node?}` (validated against the graph) |
| POST | `/api/projects/{id}/compose` | same body as the agent route; returns the full job |
| POST | `/api/projects/{id}/voice` | `{text, character_id?, voice?, speed?}` -> audio asset |
| GET | `/api/voices` | curated voices with `downloaded`, `piper_installed` |
| POST | `/api/voices/{voice_id}/download` | job `download_voice` |
| POST | `/api/projects/{id}/import-upload` | multipart `file` (images 50 MB, audio/video 2 GB), `?kind=` |
| POST | `/api/projects/{id}/import-path` | `{path, kind?}` (same rules as `studio_import`) |
| POST | `/api/assets/{id}/analyze?force=` | full analysis (all beats) |
| GET / PUT | `/api/assets/{id}/lyrics` | read `{text, lines}` / write `{text}` or `{lines:[{time_s, text}]}`; lyrics assets only |
| POST | `/api/projects/{id}/lyrics` | create a lyrics asset `{text, name?}` |
| GET | `/api/design/templates` | templates with dimensions, variants and fields |
| POST | `/api/design/preview` | `{template, fields, variant?}` -> `image/jpeg` (max side 720, nothing stored) |
| POST | `/api/projects/{id}/design` | `{template, fields, image_asset_id?, variant?, options: {print?}}` -> asset |
| POST | `/api/projects/{id}/photocard-set` | `{group_id}` |
| GET | `/api/projects/{id}/timelines` | full timelines |
| POST | `/api/projects/{id}/timelines/auto` | auto-cut body (as `studio_timeline` auto) |
| GET / PATCH | `/api/timelines/{id}` | PATCH body = `patch` of `studio_timeline` (also accepts `finishing`, see below) |
| POST | `/api/timelines/{id}/render` | `{timeline_id, quality}` -> job |
| GET | `/api/jobs?state&project&limit&offset` | full jobs (`state=active` for queued+waiting+running) |
| GET | `/api/jobs/{id}` | one job |
| POST | `/api/jobs/{id}/cancel` | cancel |
| GET | `/api/projects/{id}/assets` | `?kind&query&tag&favourite&source&min_rating&limit&offset` |
| GET / PATCH | `/api/assets/{id}` | full asset (waveform, analysis, recipe) / `{tags?, rating?, favourite?, notes?, name?}` |
| GET | `/api/assets/{id}/file?download=` | the file, resolved by id only |
| GET | `/api/assets/{id}/thumb` | WebP thumbnail |
| GET | `/api/assets/{id}/lineage` | as `studio_lineage` |
| GET / POST | `/api/projects/{id}/boards` | create `{name, kind: moodboard|storyboard|shotlist}` |
| PATCH / DELETE | `/api/boards/{id}` | rename / delete a board (assets are not touched) |
| PUT | `/api/boards/{id}/items` | `{items: [{asset_id, note}]}` (same project only) |

## Timeline finishing

`PATCH /api/timelines/{id}` (and `studio_timeline` action `update`) accepts
a `finishing` object, applied once over the whole joined cut at render
time, before the lyric captions are burned on top. Every key is optional;
`{}` (the default for a new timeline) renders exactly as before this
feature existed:

```json
{"color_grade": "teal_orange", "grain": 0.3, "vignette": true,
 "letterbox": true, "glitch_on_downbeats": true, "lyric_style": "horror"}
```

`color_grade` is one of `teal_orange`, `sodium_night`, `bleach_bypass`
(single-pass `eq`/`colorbalance`/`curves` approximations, not a 3D LUT).
`grain` is 0-1 (ffmpeg `noise`). `glitch_on_downbeats` times an RGB-split
flash (`rgbashift`) to the clips the auto-cut already marks as strong
downbeats. `lyric_style: "horror"` swaps the caption font for a condensed
uppercase face with a small per-line rotation/shear jitter, seeded from
each line's own text so a re-render is byte-identical.

## Configuration file

`data/backend.json` (written by Settings; can be edited by hand):

```json
{
  "comfy": {"url": "http://127.0.0.1:8188"},
  "faustus": {"url": "http://127.0.0.1:8000", "token": "..."},
  "capabilities": {"music": {"url": "http://127.0.0.1:9000"}},
  "vram_estimates_mb": {"sdxl": 7000, "sd15": 3500, "svd": 10000,
                        "flux": 13000, "kontext": 13000, "wan": 12000, "ace": 8000},
  "import_roots": ["D:\\Music", "E:\\Photos"]
}
```

Environment: `PROSPERO_DATA_DIR`, and Hoard Link's `HOARD_*_URL` overrides
(for example `HOARD_COMFY_URL`, `HOARD_MUSIC_URL`).

## Music generation

Two adapters implement `MusicBackend`: `ComfyMusic` resolves automatically
once ComfyUI's `/object_info` has `TextEncodeAceStepAudio1.5` and a
checkpoint named `ace_step*` (the `ace15_song` template), and `HttpMusic`
below is the fallback for anything else.

### Music backend contract (HttpMusic)

Any local server implementing `POST {url}/generate` with
`{"prompt", "lyrics", "duration_s", "seed"}` and answering audio bytes (wav or
mp3) can be configured as `capabilities.music.url`. `studio_status`'s
`music_generation` list says which adapter (if any) is available.
