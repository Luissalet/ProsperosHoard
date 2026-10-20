# Explanatory videos

Generation → Productions → **New explainer** creates an independent source-linked narrated production. It reuses the existing narrated-production stages, local voice service, deterministic motion graphics, audio mix, timeline and resumable job queue. The original short/music-video controls remain available.

`studio_explainer` is the native MCP/agent operation (`create`, `get`, `edit`, `import`); the human UI uses `/generation/api/explainers`. Discovering its schema leaves both modules OFF. Explicit `run=true` starts the existing production queue. Draft creation, editing and import need no text model. Planning from a topic uses the configured family LLM through the existing studio facade; no additional text model/runtime is installed.

## Material and scenes

Sources have an ID, title, supplied text and explicit reference/version. Source text is evidence, not instructions. URLs are references: this feature does not claim to fetch/OCR arbitrary linked documents. Faustus can obtain material using the existing library/document tools before creating the production.

Scenes retain stable identities, title, narration, on-screen text, cited source IDs and an optional retained library image, native graphic spec or portable diagram capture. The UI edits/reorders/adds/removes scenes, selects images and narration voice/speed, edits source material and exports the source. The common Faustus panel stays available in the existing selected production. A manual scene correction and a native agent edit use the same production data.

Import a `.vfigure` in a scene to retain its `vitruvius.figure/1` PNG, full native source, document ID, revision and selected page or isometric state. The preview and source remain together in the scene's `figure` field, in the native production and in exported `explainer.json`. The UI shows the captured revision/selection, downloads the complete figure and opens its declared origin only on an explicit click. To update the capture, import a new file; editing the origin does not silently change an existing video. Selecting a library image or animated text page replaces the portable capture in the draft. API/MCP use the same optional `scenes[].figure` object with `create`, `edit` or `import`; `asset_id` is ignored for a scene with a portable figure.

Changing supplied source text/reference marks its dependent scenes for checking. This is content state, not a mandatory approval stage: production can continue. An optional scene checkbox records the user's check. Unrelated edits do not silently clear outstanding source changes; a later changed source invalidates the recorded check again. No semantic/scientific correctness is inferred from this flag.

Editors can supply `base_revision` to avoid overwriting a storyboard changed since their read. The UI retains its draft when that revision conflicts. An active native run is left intact; save/retry after it finishes or cancel it with the existing job control.

## Reuse and recovery

The voice cache stores native audio assets by sentence, voice parameters and explicit voice revision. The page cache stores native text posters by graphic and output dimensions, or portable images by their complete capture. Two states with identical pixels remain distinct; returning to the same capture reuses its existing image. A changed sentence generates new speech; unchanged sentences and pages retain their asset IDs. Changing a voice invalidates voice-dependent results. Mix, scene times and timeline are rebuilt using the existing pipeline as needed. Music and existing exported assets remain retained.

Cache entries are saved after each generated sentence/page. A failed/interrupted narration resumes using its completed sentences. Scene durations come from placed audio, not a declared target length. Per-word captions use the existing speech alignment when available, otherwise syllable-weighted estimated timings; sentence placement is retained separately.

Native text pages become animated graphic clips through the existing timeline renderer. A linked library image remains an asset reference; a missing explicit image is reported rather than replaced silently with a text page. A portable figure materializes as a native image with the exact captured PNG and source metadata. It has no automatic pan/zoom; productions containing these captures default to fit framing unless a framing choice is explicitly supplied. The first visual grammars are the existing native motion-graphics grammars. This is not a new general HTML video renderer or a claim of arbitrary infographic layout support.

## Outputs

The production keeps its normal MP4 render/timeline assets plus:

- `/generation/api/productions/<slug>/explainer.json`: editable `prospero.explainer/1` source, scenes, references, voice, music, captions and timeline options.
- `/generation/api/productions/<slug>/explainer.srt`: placed narration subtitles after a current completed report. Stale subtitles are not served after a source edit.

The native source can be imported through the new modal or MCP `action=import, bundle=...`. It creates a new production and preserves the supplied options and scene/source identities. Portable figures carry their own PNG/source and can be produced in a fresh profile without the original asset IDs. Other library media IDs still refer to retained assets in the studio; this JSON is not a complete archive of arbitrary media. Exporting an unsaved UI draft first saves it. Previous MP4 assets are immutable and do not change when the storyboard is edited.

## Verification and provenance

Isolated native MCP acceptance discovered169 cold tools, then created a70.019-second Spanish production using local Piper and CPU graphics/mixing/video. Correcting one scene retained the other two page IDs and all previous sentence assets. The MP4 was inspected through the native three-frame `studio_show` result; SRT and source were read. This does not evaluate the global LLM's planning quality. CPU regression tests cover source dependency changes, stale writes, project roundtrip, failure/recovery and unchanged derivatives. UI checks exercise scene save/reopen, draft export and the native produce action on desktop/mobile.

Research reference: [Frametell](https://github.com/sitor-ai/frametell), inspected at `63b9478bdb921f533935b27aa2bb87a46cea76d0`, declares AGPL-3.0. Its source-to-storyboard/page workflow informed the feature comparison. No upstream source, CLI, agent runtime or hosted voice dependency was incorporated. This implementation extends the studio's own `shorts`, `productions`, `motion_graphics` and `timeline` modules.
