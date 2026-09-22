# Prospero's Hoard - HTTP API

Base URL: `http://127.0.0.1:8815`. All bodies are JSON unless noted. Every
route (except `GET /api/health`) is protected by the browser-attack guard:
requests must have a matching `Host` header, and non-GET requests are
rejected if `Origin` is cross-origin or `Sec-Fetch-Site: cross-site`.

Errors are always `{"error": "<code>", "message": "<actionable text>"}`
with a 4xx/5xx status. Common codes: `not_found` (404),
`bad_request`/validation errors (400/422), `<capability>_unavailable`
(409, e.g. `image_unavailable`), `backend_error` (502, ComfyUI/Faustus
answered with an error), `internal_error` (500).

## Health and backend

### `GET /api/health`
No auth, no guard check on Host beyond the standard middleware. Faustus
fingerprints the app by `service`.
```json
{"service": "prosperos-hoard", "name": "Prospero's Hoard", "version": "0.1.0", "status": "ok", "projects": 3}
```

### `GET /api/backend`
Hoard Link status per capability, ComfyUI reachability/checkpoints/VRAM,
ffmpeg, bundled fonts, VRAM estimate table, and music-generation adapter
status (both usually "not installed").
```json
{
  "hoard_link": {"llm": {"capability": "llm", "state": "unavailable", "reason": "...", ...}, "image": {...}, "tts": {...}},
  "comfy": {"reachable": true, "url": "http://127.0.0.1:8188", "checkpoints": ["sd_xl_base_1.0.safetensors"], "vram_free_mb": 18000},
  "ffmpeg": {"found": true, "path": "/usr/bin/ffmpeg", "version": "ffmpeg version 6.1.1 ..."},
  "fonts_bundled": ["BebasNeue", "Caveat", "Inter", "PlayfairDisplay", "SpaceGrotesk"],
  "vram_estimates_mb": {"sdxl": 7000, "sd15": 3500, "svd": 10000},
  "music": [{"name": "comfy_music", "available": false, "reason": "music: not installed - ..."},
            {"name": "http_music", "available": false, "reason": "music: not installed - ..."}],
  "token_set": false
}
```

### `POST /api/backend`
Sets manual overrides (persisted to `data/backend.json`). Any field may be
omitted.
```json
{"faustus_url": "http://127.0.0.1:7000", "faustus_token": "ody_...", "comfy_url": "http://127.0.0.1:8188",
 "vram_estimates_mb": {"sdxl": 8000}}
```
Response is the same shape as `GET /api/backend`; the token is never
echoed back, only `token_set: true`.

### `GET /api/agent-calls?limit=20`
The audit log ("what the assistant did"): every `/api/agent/*` call with
tool name, a truncated args summary, duration, ok/error.

## Projects

- `GET /api/projects?query=&limit=20&offset=0` -> `{"items": [...], "has_more": bool, "next_offset": int|null}`
- `GET /api/projects/{id}`
- `GET /api/projects/{id}/characters` -> `{"items": [...]}`
- `GET /api/projects/{id}/groups` -> `{"items": [...]}`
- `GET /api/projects/{id}/timelines` -> `{"items": [...]}`
- `GET /api/projects/{id}/assets?kind=&query=&tag=&favourite=&limit=60&offset=0`
- `GET /api/style-presets?project=` -> 6 built-in presets plus any project-specific ones
- `POST /api/projects/{id}/boards` `{"name": "...", "kind": "moodboard"}`
- `GET /api/projects/{id}/boards`
- `PUT /api/boards/{id}/items` `{"items": [{"asset_id": "a_...", "note": "..."}]}`
- `POST /api/projects/{id}/import-upload` (multipart `file`, optional `kind` query param) - size cap 50 MB
  for images, 2 GB for audio/video.

## Assets

- `GET /api/assets/{id}` - full asset record (tags, rating, favourite, recipe/lineage, waveform if audio).
- `PATCH /api/assets/{id}` `{"tags": [...], "rating": 0-5, "favourite": bool, "notes": "..."}`
- `GET /api/assets/{id}/file` - the original file, resolved by id only (never by client path - traversal is refused with 404).
- `GET /api/assets/{id}/thumb` - a 512px WebP thumbnail (images only).
- `GET /api/jobs?state=&limit=30&offset=0`, `GET /api/jobs/{id}`
- `GET /api/voices` - curated Piper voices (`{"items": [{"id", "lang", "label"}]}`).
- `GET /api/design/templates` - the 7 built-in design template names.

## Agent tools (`/api/agent/*`)

Every one of these is exactly what the matching MCP tool returns (see
`docs/MCP.md`), so the UI can call them directly for the same effect an
agent gets. Full parameter documentation lives with the MCP tool
docstrings; this section gives one example per route.

### `POST /api/agent/studio_create_project`
```json
// request
{"name": "Neon Static", "brief": "debut single"}
// response: the project row
```

### `GET /api/agent/studio_projects?query=&limit=10`
### `POST /api/agent/studio_cast?project={id}`
```json
// request
{"action": "create", "kind": "character", "name": "Aria",
 "fields": {"prompt": "an idol with silver hair", "negative": "extra fingers", "palette": ["#ff4d8d"]}}
// action="list" ignores kind/name/fields and returns {"characters": [...], "groups": [...]}
```

### `POST /api/agent/studio_generate_image?project={id}`
```json
// request
{"prompt": "@Aria singing on stage", "aspect": "9:16", "count": 1, "seed": 42, "wait_s": 20}
// response
{"job": {"id": "job_...", "state": "done", "progress": 1.0,
         "outputs": {"asset_ids": ["a_..."], "assets": [{"...": "full asset row"}]}},
 "matched_characters": ["Aria"], "final_prompt": "an idol with silver hair singing on stage"}
```
If `wait_s=0` the job is returned immediately in `state: "queued"` -
poll `GET /api/agent/studio_job?job_id=...`. A GPU shortage returns
`state: "waiting_gpu"` with a `message` explaining why, not an error.

### `POST /api/agent/studio_edit_image`
```json
{"asset_id": "a_...", "operation": "img2img", "prompt": "add neon rim light", "strength": 0.5, "wait_s": 20}
```
`operation` is `img2img` | `inpaint` (needs `mask_asset_id`, white = repaint) | `hires` | `vary`.

### `POST /api/agent/studio_animate`
```json
{"asset_id": "a_...", "frames": 14, "fps": 7, "motion": 127, "wait_s": 60}
```

### `POST /api/agent/studio_voice?project={id}`
```json
{"text": "Hello from the stage!", "character_id": "char_...", "speed": 1.0}
// response: the created audio asset (runs synchronously, not a job)
```

### `POST /api/agent/studio_import?project={id}`
```json
{"path": "/home/user/photo.png", "kind": "image"}
```
For a browser upload use `POST /api/projects/{id}/import-upload` instead
(multipart, not JSON).

### `POST /api/agent/studio_analyze_audio?asset_id={id}`
No body. Response:
```json
{"duration_s": 30.0, "tempo_bpm": 120.2, "beat_times": [0.46, 0.96, ...],
 "downbeats": [0.46, 1.96, ...], "sections": [{"label": "section A", "start_s": 0.0, "end_s": 10.0, "energy": "low"}, ...]}
```

### `POST /api/agent/studio_design?project={id}`
```json
{"template": "album_cover", "variant": "bottom_band",
 "fields": {"cover_image": "a_...", "title": "NEON HEARTS", "subtitle": "Deluxe Edition", "accent": "#f5c26b"}}
// response: the rendered image asset
```
Template field contracts are documented at the top of `prosperos_hoard/templates.py`.

### `POST /api/agent/studio_photocard_set?project={id}`
```json
{"group_id": "grp_...", "template_front": "photocard_front", "template_back": "photocard_back"}
// response: {"asset_ids": [...], "assets": [...], "contact_sheet": {"...": "asset row"}}
```

### `POST /api/agent/studio_timeline?project={id}`
```json
// action="auto"
{"action": "auto", "song_asset_id": "a_song", "asset_ids": ["a_1", "a_2", "a_3"], "aspect": "9:16",
 "options": {"flash_on_strong_downbeats": true, "ken_burns_variety": true}}
// action="get"
{"action": "get", "timeline_id": "tl_..."}
// action="update"
{"action": "update", "timeline_id": "tl_...", "patch": {"tracks": [/* full tracks array */]}}
```
A timeline's `tracks` shape is documented at the top of
`prosperos_hoard/timeline.py` (visual clips with `ken_burns`/
`transition_in`, an optional lyrics track).

### `POST /api/agent/studio_render`
```json
{"timeline_id": "tl_...", "quality": "preview", "wait_s": 0}
// response: {"job": {"id": "job_...", "state": "queued", ...}}
```
Poll `studio_job` for progress (0..1) and, on `state: "done"`,
`job.outputs.asset_id` is the rendered mp4 asset - fetch it at
`GET /api/assets/{asset_id}/file`.

### `GET /api/agent/studio_jobs?state=&limit=10`
### `GET /api/agent/studio_job?job_id={id}&wait_s=0`
Job shape:
```json
{"id": "job_...", "type": "render_timeline", "lane": "cpu", "state": "running",
 "progress": 0.42, "message": "rendered clip 3/7", "outputs": null,
 "created_at": "...", "started_at": "...", "finished_at": null}
```
States: `queued -> (waiting_gpu <-> queued) -> running -> done|failed|cancelled`.

### `GET /api/agent/studio_assets?project={id}&kind=&query=&tag=&favourite=&limit=12`
### `GET /api/agent/studio_show?asset_ids=a_1,a_2&size=768`
```json
{"items": [{"asset_id": "a_1", "kind": "image", "mime": "image/jpeg", "base64": "..."}]}
```
Video returns a 3-frame contact sheet; audio returns a waveform image.

### `GET /api/agent/studio_lineage?asset_id={id}`
```json
{"asset_id": "a_...", "source": "generated",
 "recipe": {"operation": "generate_image", "backend": "comfyui", "template": "sdxl_txt2img",
            "checkpoint": "sd_xl_base_1.0.safetensors", "params": {"...": "every parameter incl. seed"}}}
```

## Polling pattern for the UI

There is no SSE/websocket endpoint in this version (documented boundary).
Jobs and the "Assistant activity" panel are meant to be polled:
`GET /api/jobs?limit=30` every 1-2 seconds while any job is
`queued`/`waiting_gpu`/`running`, and `GET /api/agent-calls?limit=20` on
the same cadence for the activity feed. Both are cheap SQLite reads.

## Asset URLs for the UI

- Full-size / original file: `/api/assets/{id}/file`
- Thumbnail (images only): `/api/assets/{id}/thumb`
- A rendered video's own `/file` is playable directly in an `<video>` tag
  (H.264 + AAC, `faststart`).
