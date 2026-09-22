---
name: idol-production
description: End-to-end recipe for producing an idol group's visuals and a beat-cut music video with Prospero's Hoard - cast, reference sheets, photocards, cover art, song analysis, auto-cut editing, and rendering.
---

# Idol production, start to finish

1. **Check the studio first.** Call `studio_status`. If ComfyUI is
   unreachable or VRAM is tight, say so before promising images - jobs will
   sit in `waiting_gpu`, which is normal, not broken.
2. **Create the project and cast** with `studio_create_project`, then
   `studio_cast` (action="create") for each member and the group. Write a
   real `prompt` fragment per character (hair, build, styling) and a
   shared `negative` - this is what makes @mentions work later.
3. **Lock a reference image per member first.** `studio_generate_image`
   with `seed` set and `count=1`, then `studio_cast` (action="update") to
   set `canonical_asset_id`. Everything after this reuses that seed/recipe
   via `studio_lineage` so the member's look stays consistent - do not
   re-roll the seed once a reference is picked.
4. **Photocards and cover** come from `studio_photocard_set` (needs
   canonical references) and `studio_design` (template="album_cover").
   Look at results with `studio_show` before telling the user they are
   done - a fake/demo backend still returns a valid file, but it will not
   look like a real photo.
5. **Song first, visuals second, for a music video.** Import the song
   (`studio_import`), then `studio_analyze_audio` to get BPM/beats/
   sections. Only then call `studio_timeline` (action="auto") with the
   image/video pool - it needs the analysis to place cuts on beats.
6. **Inspect the timeline before rendering.** `studio_timeline`
   (action="get") returns real clips with Ken Burns/transition fields -
   review or `patch` it, then `studio_render` with `quality="preview"`
   first (fast, 540p) and only "final" once the cut is approved.
7. **Traps**: VRAM waits can take minutes on a shared GPU - poll
   `studio_job`, do not assume failure. `studio_edit_image`/`studio_animate`
   need an existing asset id, not a prompt. Aspect choice (9:16 vs 16:9)
   must be decided before `studio_timeline`, since it fixes the render
   resolution.
