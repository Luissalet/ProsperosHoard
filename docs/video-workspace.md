# Video workspace

Open Prospero to resume the selected project's last node canvas. If generation
is off, **Open video canvas** enables it explicitly and opens the canvas; it does
not resume old jobs. Module/project management remains at `/?manage=1`.

The initial graph connects a reference node to a video generator. Upload or
choose an image, describe the movement, and generate in that node. **Create an
image in this node** replaces the reference with an image generator and retains
existing references in a connected upstream node. Animating an image from the
library opens a connected canvas with that image already selected.

Ports are editable: connect text, images, motion guides, an end frame or audio;
branch into clips; connect a clip's last-frame output to the next clip; and join
clips with Combine. Existing templates, library, cast, production editor,
audio and native montage tools remain available.

Video outputs play inside their generator, with download, full-size viewing
and comparison with the same output position in the preceding generation.
Runs, selected takes, positions, prompts and viewport remain saved. Generation
has no review checkpoint. New music-video productions continue automatically;
explicit song/animatic review remains an option. Recomposing several song takes
preserves that review preference.

## Engines

`GET /api/video-engines` and MCP `studio_video_engines` report the exact
workflow's required model files and nodes, missing dependencies, live
availability and Auto's choice. Cached inventory is offline, not live
availability. Discovery never starts a server or loads a model. Auto chooses a
complete Wan 14B installation, otherwise a complete Wan 5B installation. SVD
remains explicitly selectable and is labelled as not following motion prompts.

14B's **Fast generation** uses the four-step schedule; switching it off uses
the full schedule. Explicit modes, seeds and durations remain editable.

## MCP

`studio_spaces(action="create", project=..., template="quick_video")` creates
the same graph. A video node accepts `engine: auto|wan14b|wan|svd|ltx23`,
`lightning: true|false`, `seconds`, `prompt`, `seed` and its existing inputs.
Use `edit`, `run`, `get` and `stop` through the existing Spaces API.
With motion connected, `motion_engine: animate|ltx23_control` chooses pose
transfer or Canny structure control; Animate2 remains the default. LTX retains
the image reference, guide offset, `motion_prompt` and `pose_strength`, rounds
duration to8*n+1frames at24fps, and records both media IDs. MCP
`studio_generate_image` also accepts `driving_asset_id`, `driving_start_s` and
the template's declared `template_params`. LTX's image-only workflow uses a
fixed eight-sigma schedule; the control workflow exposes its native sampler.
The recipe records the effective `8/manual_sigmas/denoise1` image-only settings;
incompatible overrides are explained before rendering. Control accepts denoise.
Appearance and guide action are combined within the API's4000-character budget
without discarding either field when both fit. GPU reservations follow the
managed local server's physical card and effective URL. A worker starting a
server under its lease uses the granted card. HTTP preflight may start the
process to read schema before enqueueing; model execution follows the lease. A remote
or unowned server's physical card remains unknown when the launcher cannot identify it.
Image-only dimensions are multiples of32; structure control needs multiples
of64 (896x512 by default), because its guide uses half-sized latent dimensions.
`studio_video_from_plan(autopilot=false)` opts into review; its default continues.

## Verification scope

Isolated simulated-generator checks prove interaction and transport, not model
quality. Separate real GPU comparisons retain sources, prompts, seeds, jobs,
encoded videos and wall-clock times. One four-second walking/waving scene took
250.60 s with 14B four-step and 951.09 s with the full schedule on one RTX 5060
Ti. Both preserved the subject and performed the action in inspected frames.
These are individual runs, not a general performance guarantee. Further scenes
are recorded in the task's QA evidence, not inferred from the UI demo.

The same walking source and seed took 541.43 s with Wan 5B (97 frames).
It also walked and waved, with less stable hand detail. The existing Blacephalon
reference remained recognizable in the four-step 14B clip (211.94 s rendering,
331.35 s including queueing): arm and body motion succeeded, while the requested
half spin did not. These checks support the fast 14B default; they do not promise
exact choreography or universal character consistency.

Style/review fixes retain unique full IDs and invalidate montage on a target
duration change even when all shots are approved; approved clips stay intact.

For image-only LTX nodes, `refine: true` selects `ltx23_i2v_hq`. The default stays the single-stage model. The optional native graph doubles base width/height and adds3 fixed Euler-sigma refinement steps, reanchors the original first frame, and reuses the caller seed/sampler. The extra upscaler is checked by the catalog entry `ltx23_hq` (mode `refinement`, outside the model selector); the recipe records `video_refinement`. Base steps8/manual_sigmas/denoise1 and the8*n+1 frame grid still apply. More detail does not establish correct anatomy, feet, supporting cast or a camera arc.
