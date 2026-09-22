---
name: idol-production
description: Use Prospero's Hoard to produce an invented group's visuals and a music video - cast with @mentions, reference portraits, photocards, cover, song analysis, beat-synced auto-cut and render - through the studio_* MCP tools.
---

# Idol production with Prospero's Hoard

Order matters. Each step returns ids; pass them to the next call.

1. `studio_status()` first. If `comfyui.reachable` is false, say so and do only
   GPU-free steps. `demo_backend: true` means pictures are placeholders.
2. `studio_create_project(name, brief)`, then one `studio_cast(project, "create",
   name=..., fields={"role", "prompt", "negative", "palette"})` per member. The
   `prompt` is the look (hair, face, outfit), inlined wherever `@Name` appears.
   Then the group: `kind="group", fields={"member_ids": [...in order...]}`.
3. Reference portraits: `studio_generate_image(project, "@Name studio portrait",
   style="Studio portrait", seed=<fixed>, count=2, wait_s=90)`, `studio_show`
   them, then set the chosen one with `studio_cast(..., "update", id=...,
   fields={"canonical_asset_id": ...})`. Later shots keep `@Name`; for a closer
   likeness add `use_character_reference=true` (strength 0.4-0.6).
4. Same shot again: `studio_edit_image(asset_id, "vary", count=3)`; exact
   reproduction: `"reuse"`.
5. `studio_photocard_set(project, group_id)` for fronts, backs and a sheet;
   `studio_design(project, "album_cover", {"title": ...}, image_asset_id=...,
   variant="bottom_band")` for the cover.
6. Song: `studio_import(project, "<absolute path>")`, `studio_analyze_audio(id)`.
7. `studio_timeline(project, "auto", song_asset_id=..., aspect="9:16")`, adjust
   with `action="update", patch={"clip_updates": [...]}`, then
   `studio_render(timeline_id, "preview")` + `studio_job(job_id, wait_s=120)`.
   Render "final" only after the user approves the preview.

Traps:
- `waiting_gpu` is normal on a shared GPU: keep polling, it has not failed.
- Never describe an image you have not looked at with `studio_show`.
- `unknown_mentions` means a name matched no character: fix it, do not ignore it.
- Choose the aspect before `studio_timeline`: it fixes the render size.
- Voices are generic Piper voices (first use downloads ~60 MB); never imitate a
  real person. No music model is installed: ask the user for an audio file.
