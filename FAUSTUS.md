# Prospero video workspace

The primary entry resumes a persistent, connectable Spaces canvas. MCP
`studio_spaces` supports the `quick_video` starter and explicit video engine,
fast/full schedule, prompts, references and durations in the same graph.
`studio_video_engines` exposes live complete-workflow readiness without loading
models. `studio_video_from_plan` continues automatically unless
`autopilot=false` opts into review. See [video workflow](docs/video-workspace.md).

Video nodes with one included result offer **Continue from last frame**. This
adds and focuses a wired successor, retains generation settings and leaves the
next action blank; no render starts. It uses the selected upstream take and
undoes as one edit. MCP has the same add-node/last-to-start graph operations.

A video node's optional final-frame input also preserves the fast/full
schedule. Switching fast generation off requests twenty steps for the
first-and-last-frame workflow, as it does for a start-only clip.

Motion-wired video nodes accept `motion_prompt` separately from the appearance
and setting in `prompt`, plus native `pose_strength` and
`reference_image_strength` (0–10, trained default 1). Do not assume stronger
conditioning yields better results. Inspect actual takes and keep failed ones.

`engine="ltx23"` selects LTX2.3 distilled image-to-video explicitly. A motion
node's `motion_engine="ltx23_control"` selects Union Control IC-LoRA: Canny
structure from the guide, appearance from the reference. Animate2 remains the
motion default; automatic image-to-video selection stays unchanged. LTX uses
24fps and rounds duration to its eight-frame grid, with native guide offset and
motion strength. No local prompt-generating LLM runs. These are generation
controls, not guarantees of choreography, anatomy or identity continuity.
MCP `studio_generate_image` exposes the same `driving_asset_id`,
`driving_start_s` and declared `template_params`; references and guide IDs remain
in the resulting recipe. LTX weights use the LTX-2 Community License.
Image-only dimensions must be multiples of32; ref0.5 structure control requires
multiples of64 (896x512 default). Invalid sizes/frame counts fail before rendering.
The image-only graph records its effective eight-step `manual_sigmas` schedule
and denoise1; incompatible sampling overrides return an explicit explanation.
Control maps its denoise setting to the sampler. Managed local servers reserve
their physical GPU using the effective URL (including environment overrides).
A worker starting a server under its lease uses the granted card. HTTP
preflight may start the server process to read its schema before enqueueing;
this does not load or execute the checkpoint. Model execution follows the lease.

Imported UI workflows preserve numeric union widgets such as `FLOAT,INT`,
declared input order and named widget values. The node-schema cache keeps
nested field order: an audio frame rate must not silently become a batch count,
and cached resize controls must not exchange width and height.

`studio_review_clip(asset_id, prompt?, reference_asset_ids?, check_supporting_cast?)` reviews a video
without a production, through the same optional action available in its node.
It reports technical checks and, if vision is available, a chronological sample
of up to 24 frames with motion, anatomy, setting and cast scores. Respect
`visual_review.status`, `unassessed`, missing references and
`full_playback_reviewed=false`: neither job completion nor a technical pass is
an artistic approval. It does not pause or gate generation. Named secondary
characters still use the existing individual cast/reference controls; this
change adds no multi-reference video model.
Without a prompt override, review uses the selected asset's saved appearance
and action, including Animate2's pose prompt. It never substitutes newer node
edits for an older take. Use `check_supporting_cast=true` to require a secondary
identity assessment; null then leaves the review incomplete. Observed low cast
scores are retained even when this flag is omitted. Temporal scores require
valid model JSON: explicit nulls and malformed replies cannot become approval
through numbers quoted in the explanation.

Real video comparisons are separate from simulated interaction checks: one
four-second walking/waving scene took 250.60 s at four-step 14B versus 951.09 s
at the full schedule, with both subject continuity and the action observed.
These single runs do not establish universal quality or speed.

## Existing project and shot tools

The dashboard remains available from Project. Its inline character/group editors use the existing native cast APIs; MCP keeps the same `studio_cast` contract. Clip creation uses the existing animation/generation APIs, direct job polling and actual output IDs. Native `studio_animate`, `studio_job` and `studio_video_from_plan` were exercised through stdio MCP on isolated synthetic data.

Clicking an image-shot storyboard card opens its own connected frame/video workspace, including when no frame exists. It saves through `studio_production_shots` semantics and continues the native production; it retains the shot identity, selected frame and actual output binding. Rendering may complete other pending shots too. The standalone clip studio creates separate project assets. Free-form Spaces and native production take selection remain available. See [project and clip workflow](docs/PROJECT-STUDIO.md).

Shot readiness follows its frame/clip records independently of a production job finishing. The visible References node adds, uploads, replaces, describes and removes up to six image references through the existing `studio_production_shots` payload; no new MCP contract is introduced. Production failures retain their concrete message or log fallback.

No user media, lyrics, production specifications, model configuration or GPU services are changed by this interface update. Model quality is outside the synthetic interaction checks.

Spaces has a searchable library with27 shortcuts across all15 native node types.
Three video starters prepare asset nodes and wires: motion=start:image plus
motion:video, lip sync=start:image plus audio:audio, between-frames=start:image
plus end:image. Creation starts no render and is one undo step. Agents use the
existing studio_spaces add_node/connect operations and ordinary data fields.
Combine displays clip wires in execution order with up/down controls; agents
reorder via disconnect/connect in the desired order. Each wire contributes its
included takes. No new model installation or quality guarantee is implied.

Spaces LTX image-to-video also accepts `refine=true`: optional native2x latent upscaling and3 fixed refinement steps after the8-step base, exposed by the node checkbox. Direct API/MCP template: `ltx23_i2v_hq`. Requires the LTX2.3 spatial-upscaler-x2-1.1 weight; catalog availability checks it separately, without hiding the base model. Both stages reuse the caller seed/sampler; immutable recipes record `video_refinement`. This improves the detail workflow, not a guarantee of artistic continuity or dynamic camera.
