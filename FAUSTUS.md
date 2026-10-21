# Prospero project navigation

Project selection opens the dashboard. Its inline character/group editors use the existing native cast APIs; MCP keeps the same `studio_cast` contract. Clip creation uses the existing animation/generation APIs, direct job polling and actual output IDs. Native `studio_animate`, `studio_job` and `studio_video_from_plan` were exercised through stdio MCP on isolated synthetic data.

Clicking an image-shot storyboard card opens its own connected frame/video workspace, including when no frame exists. It saves through `studio_production_shots` semantics and continues the native production; it retains the shot identity, selected frame and actual output binding. Rendering may complete other pending shots too. The standalone clip studio creates separate project assets. Free-form Spaces and native production take selection remain available. See [project and clip workflow](docs/PROJECT-STUDIO.md).

No user media, lyrics, production specifications, model configuration or GPU services are changed by this interface update. Model quality is outside the synthetic interaction checks.
