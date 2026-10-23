# Prospero video workspace

The primary entry resumes a persistent, connectable Spaces canvas. MCP
`studio_spaces` supports the `quick_video` starter and explicit video engine,
fast/full schedule, prompts, references and durations in the same graph.
`studio_video_engines` exposes live complete-workflow readiness without loading
models. `studio_video_from_plan` continues automatically unless
`autopilot=false` opts into review. See [video workflow](docs/video-workspace.md).

A video node's optional final-frame input also preserves the fast/full
schedule. Switching fast generation off requests twenty steps for the
first-and-last-frame workflow, as it does for a start-only clip.

Real video comparisons are separate from simulated interaction checks: one
four-second walking/waving scene took 250.60 s at four-step 14B versus 951.09 s
at the full schedule, with both subject continuity and the action observed.
These single runs do not establish universal quality or speed.

## Existing project and shot tools

The dashboard remains available from Project. Its inline character/group editors use the existing native cast APIs; MCP keeps the same `studio_cast` contract. Clip creation uses the existing animation/generation APIs, direct job polling and actual output IDs. Native `studio_animate`, `studio_job` and `studio_video_from_plan` were exercised through stdio MCP on isolated synthetic data.

Clicking an image-shot storyboard card opens its own connected frame/video workspace, including when no frame exists. It saves through `studio_production_shots` semantics and continues the native production; it retains the shot identity, selected frame and actual output binding. Rendering may complete other pending shots too. The standalone clip studio creates separate project assets. Free-form Spaces and native production take selection remain available. See [project and clip workflow](docs/PROJECT-STUDIO.md).

Shot readiness follows its frame/clip records independently of a production job finishing. The visible References node adds, uploads, replaces, describes and removes up to six image references through the existing `studio_production_shots` payload; no new MCP contract is introduced. Production failures retain their concrete message or log fallback.

No user media, lyrics, production specifications, model configuration or GPU services are changed by this interface update. Model quality is outside the synthetic interaction checks.
