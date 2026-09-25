# HTTP API

Base URL `http://127.0.0.1:8815`. JSON in and out. Errors are always
`{"error": "<code>", "message": "<what to do>"}` with a 4xx status (404
`not_found`, 400 for validation, 409 `<capability>_unavailable` when no
backend resolves, 422 `invalid_arguments` for malformed bodies, 502
`backend_error` when ComfyUI/Faustus failed a call). Character-kit routes
raise `charkit.KitError`, `charpack.PackError` or `trainers.TrainingError`,
which all answer 400 with their own `code` the same way (e.g. `no_canonical`,
`bad_pack`, `trainer_unavailable`).

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
| POST | `/api/agent/studio_create_project` | `{name, brief?, image_engine?}` |
| POST | `/api/agent/studio_cast?project=` | `{action, kind, id?, name?, fields}` |
| POST | `/api/agent/studio_generate_image?project=` | `{prompt, style?, negative?, aspect?, width?, height?, steps?, cfg?, sampler?, scheduler?, seed?, count, reference_asset_id?, reference_asset_ids?, strength?, template?, engine?, checkpoint?, use_character_reference, consistent, characters?, use_adapters, prefer_adapter, wait_s}` -> adds `adapters`/`adapter_notes` (only when non-empty) and `route: "adapter"` (only when `prefer_adapter` took that path) to the usual result |
| POST | `/api/agent/studio_edit_image` | `{asset_id, operation, prompt?, strength?, mask_asset_id?, count, seed?, width?, height?, wait_s}` |
| POST | `/api/agent/studio_animate` | `{asset_id, frames, fps, motion, seed?, wait_s}` |
| POST | `/api/agent/studio_compose?project=` | `{tags, lyrics, bpm, duration, key, language, time_signature, seed?, count, wait_s}` -> job (ACE-Step 1.5; an mp3/wav audio asset) |
| POST | `/api/agent/studio_voice?project=` | `{text, character_id?, voice?, speed?}` |
| POST | `/api/agent/studio_import?project=` | `{path, kind?}` |
| POST | `/api/agent/studio_analyze_audio?asset_id=` | - |
| POST | `/api/agent/studio_time_lyrics?project=` | `{song_asset_id, lyrics, name?}` -> `{id, lines, sections, note}`: a lyrics asset timed from the `[Section]` tags and the song's bars |
| POST | `/api/agent/studio_design?project=` | `{template, fields, image_asset_id?, variant?, options}` |
| POST | `/api/agent/studio_photocard_set?project=` | group `{group_id, template_front, template_back, image_asset_ids?}` or solo `{character_id, cards:[{image_asset_id, role?, message?, accent?}], set_name?}` |
| POST | `/api/agent/studio_timeline?project=` | `{action, song_asset_id?, asset_ids?, board_id?, aspect, lyrics_asset_id?, options, timeline_id?, patch}` |
| POST | `/api/agent/studio_render` | `{timeline_id, quality, wait_s}` |
| GET | `/api/agent/studio_jobs` | `?state&limit&offset` |
| GET | `/api/agent/studio_job` | `?job_id&wait_s` |
| POST | `/api/agent/studio_cancel_job?job_id=` | - |
| GET | `/api/agent/studio_assets` | `?project&kind&query&tag&favourite&limit&offset` |
| GET | `/api/agent/studio_show` | `?asset_ids=a,b,c&size=768` -> `{items:[{asset_id, kind, mime, base64, order?}]}` |
| GET | `/api/agent/studio_lineage` | `?asset_id` |
| GET | `/api/agent/studio_productions` | - -> `{items:[{slug, name, status, stage, project_id, recipe, legacy?}]}` |
| GET | `/api/agent/studio_production` | `?production=<slug>` -> compact view: `{slug, status, stages{stage: done\|partial\|pending}, character_id, song_asset_id, renders, animatic?, qa?, next}` |
| POST | `/api/agent/studio_production_create` | `{name, spec, settings?, project?}` -> `{production, job}` |
| POST | `/api/agent/studio_production_continue?production=` | - -> `{production, job}` (approves a paused production, resumes a failed/cancelled one) |
| POST | `/api/agent/studio_production_shots?production=` | `{changes:[{key, best?, clip?, prompt?, motion_prompt?, motion?, seed?, regenerate?}], run}` -> `{changed, status, job?, production}` |
| POST | `/api/agent/studio_recipe_export` | `{production, name?}` -> recipe summary + `cast`, `warnings`, `notes` |
| GET | `/api/agent/studio_recipes_list` | - -> `{items:[recipe summary]}` |
| GET | `/api/agent/studio_recipe_get` | `?recipe=<name>` -> summary, `cast`, `placeholders`, song, world, `shot_list`, timeline, settings, warnings |
| POST | `/api/agent/studio_recipe_run` | `{recipe, cast:{lead: <character id, "lib_..." library id, {library: "lib_...", version?}, or {name, look, negative?, palette?, bio?}>}, name?, options:{reuse?, title?, project?, settings?, engine?}}` -> `{production, job, notes}` |
| POST | `/api/agent/studio_animatic` | `{production, aspects?, wait_s=0}` -> `{job, animatic?: {renders{aspect: asset_id}, plan{cuts_total, clips_planned, gpu_minutes, cpu_minutes_renders, unused_shots, duration_s}}}` |
| POST | `/api/agent/studio_qa_run` | `{production, stage="all", dry_run=true, keys?, wait_s=120}` -> `{job, scorecard?, requeued?}`; scorecard `{stage, vision, passed, failed, skipped, items:[{stage, key, asset_id, verdict, score?, why}]}` |
| GET | `/api/agent/studio_qa_report` | `?production=` -> the last scorecard (failures first) + `retries[{at, stage, key, attempt, reason, fix}]` |
| POST | `/api/agent/studio_short_create` | `{topic?, script?, name?, options{language, duration_s, tone, seed, engine, voice, visuals, music, captions, timeline}, settings?{script_review}, count=1, project?}` -> `{production, job}` (or `{items:[...]}` for count > 1) |
| POST | `/api/agent/studio_production_script` | `{production, script?, run=true}` -> without `script` `{production, script}`; with it `{production, job?}` (narration, pictures, mix and render redone) |
| POST | `/api/agent/studio_stock_search` | `{query, kind="video"\|"image", aspect?\|orientation?, providers?, per_page=12, page=1, min_duration_s=0, project?, take=0, refs?}` -> `{query, providers, errors?, items:[{ref, kind, width, height, duration_s, author, page_url, preview_url, tags}], imported?:[asset]}` |
| POST | `/api/agent/studio_character_sheet` | `{character_id, views?, engine?, seed?, wait_s}` -> `{job, views}`; done job outputs `{asset_ids, contact_sheet_id, views, engine}` |
| POST | `/api/agent/studio_character_dataset` | `{character_id, action="get"\|"report"\|"build"\|"update"\|"caption", sources?, min_identity?, replace, items?, only_missing, wait_s}` -> get/build/update: `{character_id, trigger, items[], report}`; report: the report alone; caption: `{job}` (done outputs add `captioned, method, model`) |
| POST | `/api/agent/studio_character_train` | `{action="plan"\|"trainers"\|"settings"\|"start"\|"status"\|"log", character_id?, arch?, trainer?, overrides, run_id?, training?, wait_s}` -> action-dependent, see [MCP.md](MCP.md#character-kit-tools) |
| POST | `/api/agent/studio_character_adapters` | `{character_id, action="list"\|"available"\|"attach"\|"update"\|"remove"\|"settings", adapter_id?, lora_name?, arch?, strength?, trigger?, enabled?, settings}` -> action-dependent |
| POST | `/api/agent/studio_character_takes` | `{character_id, action="list"\|"score"\|"act", sort="recent"\|"identity", kind?, limit, asset_ids?, asset_id?, take_action?, force, wait_s}` -> list: `{character_id, name, total, shots, items[]}`; score: `{job}` (done outputs `results[], below_threshold[], method`); act: `{asset_id, action}` |
| POST | `/api/agent/studio_character_pack?project=` | `{action="export"\|"import"\|"inspect", character_id?, path?, rename?, include_dataset, include_adapters}` -> export: pack summary + `download`; inspect: pack contents; import: `{character_id, name, ..., notes}` |
| POST | `/api/agent/studio_character_library?project=` | `{action="list"\|"save"\|"use"\|"history"\|"delete", character_id?, id?, version?, note?, query?, rename?}` -> action-dependent, see [MCP.md](MCP.md#character-kit-tools) |
| GET | `/api/agent/voice_engines` | - -> `{tts:[...], stt:[...]}` engine status |
| POST | `/api/agent/voice_create` | `{name, engine_id, source_path, language?, project?}` |
| GET | `/api/agent/voice_list` | `?project` |
| POST | `/api/agent/voice_speak` | `{text, voice: {engine_id?, voice_id?, voice_ref?, preset?, speed?, language?}, project?}` |
| POST | `/api/agent/voice_transcribe` | `{path?, asset_id?, language?, engine_id?}` |
| POST | `/api/agent/voice_audiobook` | `{text?, source_path?, title?, voice, format, project?, wait_s}` |
| POST | `/api/agent/voice_dub` | `{source_path?, video_asset_id?, target_language, source_language?, glossary?, voice, stt_engine_id?, title?, project?, wait_s}` |
| POST | `/api/agent/voice_resynthesize_segment?job_id=&index=` | `{text?, voice?, remix}` |
| GET | `/api/agent/voice_job` | `?job_id&wait_s` |

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
| GET / PATCH | `/api/projects/{id}` | `{name?, brief?, cover_asset_id?, image_engine?}` (`image_engine`: `auto` \| `qwen21` \| `flux` \| `sdxl`, default `auto`) |
| GET / POST | `/api/projects/{id}/characters` | list (each with its `kit` summary) / create `{name, fields}` |
| GET | `/api/characters/{id}` | the full character row + `kit` (trigger, adapters, dataset, sheet, identity threshold, library, history) |
| PATCH | `/api/characters/{id}` | `{name?, fields}` |
| GET | `/api/characters/{id}/kit` | kit summary + `history` (last 30), `sheet_asset_ids`, `views`, `default_views` |
| POST | `/api/characters/{id}/sheet` | body: `studio_character_sheet` without `character_id` -> `{job, views}` |
| POST | `/api/characters/{id}/dataset` | body: `studio_character_dataset` without `character_id` |
| POST | `/api/characters/{id}/train` | body: `studio_character_train` without `character_id` |
| POST | `/api/training` | `{action: "trainers"\|"settings"\|"log", ...}` - the trainer-only actions of `studio_character_train`, with no character |
| POST | `/api/characters/{id}/adapters` | body: `studio_character_adapters` without `character_id` |
| POST | `/api/characters/{id}/takes` | body: `studio_character_takes` without `character_id` |
| POST | `/api/characters/{id}/pack` | export this character -> `{..., download: "/api/character-packs/{file}"}` |
| GET | `/api/character-packs/{file}` | the exported `.hoardchar` file |
| POST | `/api/projects/{id}/character-packs` | multipart `file` (a `.hoardchar`, up to 2 GB) + `?rename=` -> import into this project |
| POST | `/api/character-packs/inspect` | multipart `file` -> pack contents without importing |
| POST | `/api/library/characters?project=` | body: `studio_character_library`; `use` without `project` casts into the "Casting" project |
| GET | `/api/library/characters/{lib_id}/preview` | the library entry's contact-sheet preview, `image/png` |
| GET / POST | `/api/projects/{id}/groups` | create `{name, fields: {concept, member_ids, colours, logo_asset_id}}` |
| PATCH | `/api/groups/{id}` | `{name?, fields}` |
| GET | `/api/style-presets?project=` | presets with defaults |
| POST | `/api/projects/{id}/compose-prompt` | `{prompt, negative?, style?}` -> final prompt preview (`positive_prompt, negative_prompt, matched_characters, unknown_mentions, reference_asset_id, style_defaults`) |
| POST | `/api/projects/{id}/generate` | same body as the agent route; returns the full job |
| POST | `/api/assets/{id}/edit` | `{asset_id, operation, ...}` |
| POST | `/api/assets/{id}/animate` | `{asset_id, frames, fps, motion}` |
| GET | `/api/workflows` | `{builtin: [spec], custom: [spec]}` (built-ins now include `flux_schnell_txt2img`, `flux_kontext_edit`, `wan22_ti2v`, `ace15_song` alongside SDXL/SD1.5/SVD) |
| POST | `/api/workflows/import` | `{name, workflow}` (UI **or** API format - a UI export with `nodes`/`links`/subgraphs is converted first, against the live `/object_info` or, with ComfyUI off, the copy cached in `data/comfy/object_info.json`) -> proposed spec with `map` (and `converted_from: "ui"`) |
| POST | `/api/workflows/import-file` | multipart `file` (.json, at most 2 MB) |
| PATCH | `/api/workflows/{wf_id}` | `{name?, map?, vram_class?, kind?, output_node?, reference_node?}` (validated against the graph) |
| POST | `/api/projects/{id}/compose` | same body as the agent route; returns the full job |
| POST | `/api/projects/{id}/voice` | `{text, character_id?, voice?, speed?}` -> audio asset |
| GET | `/api/voices` | curated voices with `downloaded`, `piper_installed` |
| POST | `/api/voices/{voice_id}/download` | job `download_voice` |
| POST | `/api/projects/{id}/import-upload` | multipart `file` (images 50 MB, audio/video 2 GB), `?kind=` |
| POST | `/api/projects/{id}/import-path` | `{path, kind?}` (same rules as `studio_import`) |
| POST | `/api/assets/{id}/analyze?force=` | full analysis (all beats) |
| GET / PUT | `/api/assets/{id}/lyrics` | read `{text, lines, sections, all_lines}` (`lines` = sung lines; `sections` from timed `[Section]` marker lines; `all_lines` includes the markers, for the editor) / write `{text}` or `{lines:[{time_s, text}]}`; lyrics assets only |
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
| GET | `/api/voice/engines` | full engine status (same shape `voice_engines` compacts) |
| POST | `/api/voice/engines/{engine_id}/install` | `{kind: "tts"\|"stt"}` -> job `install_voice_engine` (a `pip install`, explicit and user-triggered) |
| GET / POST | `/api/voice/voices` | list `?project&engine_id` / create `{name, engine_id, source_path, language?, project?, tags?}` |
| POST | `/api/voice/voices/upload` | multipart `file` + query `name, engine_id, language?, project?` |
| GET / PATCH / DELETE | `/api/voice/voices/{id}` | full voice / `{name?, tags?, notes?}` / delete |
| POST | `/api/voice/voices/{id}/presets` | `{name, speed?, pitch?, style?}` |
| GET | `/api/voice/voices/{id}/sample` | the processed sample, `audio/wav` |
| POST | `/api/voice/voices/{id}/preview` | `{text, voice}` -> `audio/wav` (does not save an asset) |
| POST | `/api/voice/speak` | `{text, voice, project?}` -> `audio/wav`, or the asset when `project` is given |
| POST | `/api/voice/transcribe` | `{path?, asset_id?, language?, engine_id?, word_timestamps}` -> transcript + `srt, vtt, txt` |
| POST | `/api/voice/transcribe/upload` | multipart `file` + query `language?, engine_id?` |
| POST | `/api/voice/dictate` | multipart `file` (<=30 MB, for a short mic clip) + query `language?` -> `{text, language, engine_id}` fast, no timestamps |
| POST | `/api/voice/audiobook` | `{text?, source_path?, title?, voice, format, project?, wait_s}` -> `{job}` |
| GET | `/api/voice/audiobook/{job_id}` | the job |
| GET | `/api/voice/audiobook/{job_id}/download?file=final\|srt\|lrc` | the file |
| POST | `/api/voice/dub` | `{source_path?, video_asset_id?, target_language, source_language?, glossary?, voice, stt_engine_id?, title?, project?, wait_s}` -> `{job}` |
| GET | `/api/voice/dub/{job_id}` | the job |
| GET | `/api/voice/dub/{job_id}/download?file=video\|subtitles` | the file |
| POST | `/api/voice/dub/{job_id}/segments/{index}/resynthesize` | `{text?, voice?, remix}` -> `{segment}` |

Full pipeline details, engines and install commands: [VOICE.md](VOICE.md).

## Productions and recipes (UI routes)

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/productions` | every production folder under `data/productions/` (app-made and scripted, `legacy: true`) |
| POST | `/api/productions` | `{name, spec, settings?, project?}`: create and queue |
| GET | `/api/productions/{slug}` | the full `state.json` plus `view` (the compact agent view) |
| POST | `/api/productions/{slug}/continue` | approve / resume |
| PATCH | `/api/productions/{slug}/shots` | `{changes, run}` |
| GET | `/api/productions/{slug}/report` | `REPORT.md` (written on demand when missing) |
| POST | `/api/productions/{slug}/recipe` | `{name?}` -> recipe summary |
| GET | `/api/recipes` / `/api/recipes/{name}` | list / the whole recipe JSON |
| POST | `/api/recipes/{name}/run` | `{cast, name?, options}` -> `{production, job, notes}` |
| POST | `/api/productions/{slug}/animatic` | `{aspects?}` -> `{job}` (an `animatic` job on the cpu lane) |
| GET | `/api/productions/{slug}/animatic` | `plan.json`: `{cuts[{index, start_s, duration_s, shot, still, section}], shots[{key, lead, still, screen_time_s, cuts, will_be_clip, clip_keys, clips_to_render}], unused_shots, clips_planned, gpu_minutes, minutes_per, renders}` |
| POST | `/api/productions/{slug}/qa` | `{stage, dry_run, keys?}` -> `{job, scorecard?}` |
| POST | `/api/shorts` | the same body as `studio_short_create` |
| PUT | `/api/productions/{slug}/script` | `{script?, run}`: read (no script) or replace a short's script |
| GET | `/api/productions/{slug}/publish` | a short's `publish.txt` (title, description, hashtags, footage credits) |
| POST | `/api/stock/search` | the same body as `studio_stock_search` |
| GET / PUT | `/api/backend/stock` | `{providers{pexels, pixabay: {configured, key_hint, get_key}}}` / `{pexels?, pixabay?}` ("" removes a key) |
| GET | `/api/productions/{slug}/qa` | `{last: <full scorecard with every check's measurements>, history}` |

A production's settings: `{"animatic": true, "animatic_autocontinue": false,
"qa": {"enabled": false, "thresholds": {}, "max_retries": 2},
"gpu_minutes": {"clip": 9.5, "still": 1.19, "final_render": 1.75}}`.

A production's spec (all optional except `lead`):

```json
{"title": "DON'T LOOK BACK", "engine": "auto", "lead_route": "auto",
 "lead": {"name": "FAROL", "look": "...", "negative": "...", "palette": ["#F28C28"], "bio": "..."},
 "reference": {"prompt": "{look}, character turnaround reference sheet...", "seed": 1001, "count": 4, "pick": 3, "crop": "left_third"},
 "world": {"look": "cinematic 35mm film still, night...", "negative": "cartoon, cute, ..."},
 "song": {"tags": "...", "lyrics": "[Verse 1]...", "bpm": 130, "key": "D minor", "language": "en", "duration": 150, "seed": 2001, "count": 4, "take": 4},
 "shots": [{"key": "1", "lead": true, "prompt": "standing perfectly still under a far lamp...", "seed": 3010, "variants": 3,
            "width": 1344, "height": 768, "clips": [0, 1], "motion": "still", "motion_prompt": "light rain falling...", "clip_seed": 5001}],
 "photocards": {"looks": [{"prompt": "...", "seed": 4001, "role": "Visual", "message": "thanks for coming", "accent": "#F4A7C0"}]},
 "album": [{"template": "album_cover", "variant": "night", "image_shot": "3", "fields": {"title": "DON'T LOOK BACK", "artist": "FAROL"}}],
 "timeline": {"aspects": ["9:16", "16:9"], "qualities": ["preview", "final"], "options": {"fps": 24, "cut_on_lyrics": true, "karaoke": true},
              "storyboard": {"Chorus": ["3", "2", "11", "10v2"]}, "finishing": {"color_grade": "sodium_night", "grain": 0.3}}}
```

`lead_route` (default `"auto"`) picks how the lead stays consistent across
the production's shots: `"auto"` renders through the lead's LoRA adapter
when it has one for the resolved engine (free poses, no drift toward a
reference's framing), else falls back to an edit of the lead's canonical
reference image; `"reference"` always uses the canonical-image edit, even
when an adapter is available. `lead` also accepts a casting-library id in
place of an inline description - see `studio_recipe_run`'s `cast.lead` in
[MCP.md](MCP.md#character-kit-tools).

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
`grain` is 0-1 (ffmpeg `noise`). `glitch_on_downbeats` times a colour-split
flash (`chromashift`) to the clips the auto-cut already marks as strong
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
  "import_roots": ["D:\\Music", "E:\\Photos"],
  "render_pool": ["http://127.0.0.1:8189", "http://127.0.0.1:8190"],
  "stock": {"pexels": "<api key>", "pixabay": "<api key>"}
}
```

`stock` holds the free Pexels / Pixabay API keys narrated shorts and
`studio_stock_search` use (also read from `PEXELS_API_KEY` /
`PIXABAY_API_KEY`); the API only ever shows their last four characters.

`render_pool` lists extra ComfyUI servers, one per GPU (the same ComfyUI
install started again with `--cuda-device N --port P`). Each gets its own
GPU worker, and all of them take jobs from the one GPU queue, so a batch of
clips renders on every card at once. A pool server that is not answering
takes no jobs. The VRAM check for a job runs against the card of the
server that takes it. `POST /api/backend` accepts `render_pool` too, and a
restart applies a changed list.

Environment: `PROSPERO_DATA_DIR`, `PROSPERO_COMFY_TIMEOUT_S` (how long a job
may run on ComfyUI before it is reported as timed out; defaults 3600 s for
video, 1800 s for audio, 1200 s for images - raise it when the GPU is shared
with a language model), and Hoard Link's `HOARD_*_URL` overrides (for example
`HOARD_COMFY_URL`, `HOARD_MUSIC_URL`).

## Music generation

Two adapters implement `MusicBackend`: `ComfyMusic` resolves automatically
once ComfyUI's `/object_info` has `TextEncodeAceStepAudio1.5` and a
checkpoint named `ace_step*` (the `ace15_song` template), and `HttpMusic`
below is the fallback for anything else.

### Music backend API (HttpMusic)

Any local server implementing `POST {url}/generate` with
`{"prompt", "lyrics", "duration_s", "seed"}` and answering audio bytes (wav or
mp3) can be configured as `capabilities.music.url`. `studio_status`'s
`music_generation` list says which adapter (if any) is available.
