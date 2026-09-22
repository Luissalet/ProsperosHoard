# Prospero's Hoard - MCP tools

Transport: stdio. Launch: `python prosperos_hoard/mcp_server.py` with env
`PROSPERO_URL` set to the running app (default `http://127.0.0.1:8815`;
non-loopback URLs are refused). The adapter is a thin HTTP client over
`/api/agent/*` - it has no direct database or filesystem access.

Any MCP client works, not just Faustus:
```json
{
  "mcpServers": {
    "prospero": {
      "command": "/path/to/prosperos-hoard/.venv/bin/python",
      "args": ["/path/to/prosperos-hoard/prosperos_hoard/mcp_server.py"],
      "env": {"PROSPERO_URL": "http://127.0.0.1:8815"}
    }
  }
}
```

Server instructions given to the model: *"Prospero's Hoard is a media
studio you direct... Generation is slow and shares the GPU with other
models: queue a job, then poll studio_job. Mention characters as @Name...
Call studio_show before describing an image... Every asset records
exactly how it was made; call studio_lineage."*

If the app is not running, every tool raises a `ToolError` with the
message `prosperos_hoard_unavailable: Prospero's Hoard is not running.
Start it from Faustus (Apps) or with 'Iniciar Prospero's Hoard.cmd', then
retry.` On a 4xx from the app, the tool error is the app's own
`message` field.

| tool | read-only | purpose |
| --- | --- | --- |
| `studio_status()` | yes | Hoard Link + ComfyUI + ffmpeg status, recent jobs |
| `studio_projects(query?, limit=10)` | yes | list projects with counts |
| `studio_create_project(name, brief?)` | no | new project |
| `studio_cast(project, action="list", kind="character", id?, name?, fields?)` | no* | list/create/update characters and groups (*list is read-only) |
| `studio_generate_image(project, prompt, style?, negative?, aspect?, width?, height?, steps?, cfg?, sampler?, scheduler?, seed?, count=1, reference_asset_id?, strength?, template?, wait_s=0)` | no | queue txt2img/img2img; expands @Characters |
| `studio_edit_image(asset_id, operation, prompt?, strength?, mask_asset_id?, count=1, seed?, wait_s=0)` | no | img2img / inpaint / hires / vary |
| `studio_animate(asset_id, frames=14, fps=7, motion=127, seed?, wait_s=0)` | no | image-to-video (SVD) |
| `studio_voice(project, text, character_id?, voice?, speed?)` | no | spoken line -> audio asset (synchronous) |
| `studio_import(project, path, kind?)` | no | import a local file as an asset |
| `studio_analyze_audio(asset_id)` | yes | BPM, beats, sections |
| `studio_design(project, template, fields, image_asset_id?, variant?, options?)` | no | render a photocard/cover/poster/lyric card |
| `studio_photocard_set(project, group_id, template_front?, template_back?, image_asset_ids?)` | no | one card per member + contact sheet |
| `studio_timeline(project, action="auto"|"get"|"update", song_asset_id?, asset_ids?, board_id?, aspect?, lyrics_asset_id?, options?, timeline_id?, patch?)` | no | build/inspect/edit a timeline |
| `studio_render(timeline_id, quality="preview", wait_s=0)` | no | render to mp4 (job) |
| `studio_jobs(state?, limit=10)` | yes | queue summary |
| `studio_job(job_id, wait_s=0)` | yes | one job (poll) |
| `studio_assets(project, kind?, query?, tag?, favourite?, limit=12)` | yes | find assets |
| `studio_show(asset_ids, size=768)` | yes | real images: up to 4, or a contact sheet; video -> frame sheet; audio -> waveform |
| `studio_lineage(asset_id)` | yes | the exact recipe that made an asset |

Every tool's Python docstring (in `mcp_server.py`) carries a `Keywords:`
line with English and Spanish trigger words for retrieval-based tool
selection, and honest `ToolAnnotations` (`readOnlyHint`,
`destructiveHint=False` everywhere - nothing here deletes anything,
`idempotentHint`, `openWorldHint=False`).

## Proof it works

`tests/test_mcp.py::test_mcp_protocol_end_to_end` starts the real FastAPI
app (with the fake ComfyUI backend) in a background thread, spawns
`mcp_server.py` as a real subprocess over stdio with
`mcp.client.stdio`, and drives it through `list_tools` and several
`call_tool`s including one that returns a real `ImageContent`. Run it
directly with:
```
pytest tests/test_mcp.py -v
```
