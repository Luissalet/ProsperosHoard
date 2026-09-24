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
   With Flux/Kontext installed, the stronger path: a `flux_schnell_txt2img`
   turnaround sheet (front / three-quarter / back on grey), set it with
   `fields={"canonical_asset_id": sheet, "canonical_crop": "left_third"}` (one
   pose), then `consistent=true` on every shot where the character is in
   frame; shots without them use a plain template, or Kontext puts them in.
4. Same shot again: `studio_edit_image(asset_id, "vary", count=3)`; exact
   reproduction: `"reuse"`.
5. `studio_photocard_set(project, group_id)` for fronts, backs and a sheet (a
   solo artist: `character_id=..., cards=[{image_asset_id, role, message,
   accent}]`); `studio_design(project, "album_cover", {"title": ...},
   image_asset_id=..., variant="bottom_band")` for the cover (`variant="night"`
   on cover, poster, lyric card and tracklist for a horror/thriller look).
6. Song: `studio_import(project, "<absolute path>")` or `studio_compose(...)`
   (when `studio_status` lists ACE-Step), then `studio_analyze_audio(id)`.
   Lyrics with [Section] tags: `studio_time_lyrics(project, song_id, lyrics)`
   gives a first-pass LRC - tell the user it is an estimate to re-time by ear.
7. `studio_timeline(project, "auto", song_asset_id=..., aspect="9:16",
   lyrics_asset_id=..., options={"karaoke": true, "cut_on_lyrics": true,
   "section_pools": {"Verse 1": [...], "Chorus": [...]}})`, adjust
   with `action="update", patch={"clip_updates": [...]}`, then
   `studio_render(timeline_id, "preview")` + `studio_job(job_id, wait_s=120)`.
   Render "final" only after the user approves the preview.

Whole productions and recipes:
- A finished production becomes a recipe: `studio_recipe_export(production,
  name)` abstracts the lead into a `{lead}` slot. "Recreate this with X":
  `studio_recipe_run(recipe, cast={"lead": "<char id>"} or {"lead": {"name",
  "look"}}, options={"title": ...})`, then poll `studio_production(slug)`.
  Tell the user the recipe's `warnings` (prompts about the old lead's props)
  and what was reused (`notes`).
- A failed or cancelled production resumes with
  `studio_production_continue(production)`; `studio_production_shots` swaps
  a still (`best`), turns a clip on/off or rewrites a shot.

- "Show me the animatic before rendering": new productions make one after
  the stills and song and stop at `awaiting_review`; otherwise
  `studio_animatic(production)`. Summarise its plan (cuts, unused shots,
  clips to render, GPU minutes) and wait for the user before
  `studio_production_continue`.
- "Check the production" / "why is clip 11 wrong?": `studio_qa_run(production,
  stage="clips", keys=["11"])` (dry run by default) or `studio_qa_report`;
  explain each `why`; `dry_run=false` regenerates the failures with a new
  seed and a targeted fix, up to the retry cap. Say so when the scorecard's
  `vision` is "no vision model" (only the model-free checks ran).

Traps:
- `waiting_gpu` is normal on a shared GPU: keep polling, it has not failed.
- Never describe an image you have not looked at with `studio_show`.
- `unknown_mentions` means a name matched no character: fix it, do not ignore it.
- Choose the aspect before `studio_timeline`: it fixes the render size.
- Voices are generic Piper voices (first use downloads ~60 MB); never imitate a
  real person. Without a music model (see `studio_status`), ask the user for
  an audio file.
