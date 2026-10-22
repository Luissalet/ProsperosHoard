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
the same graph. A video node accepts `engine: auto|wan14b|wan|svd`,
`lightning: true|false`, `seconds`, `prompt`, `seed` and its existing inputs.
Use `edit`, `run`, `get` and `stop` through the existing Spaces API.
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
