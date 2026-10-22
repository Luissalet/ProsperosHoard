# Spaces editor

Operate. Extend the media-first review bench with Prospero's original magenta
brand palette, as explicitly pinned by the user. Preserve its dragon icon.
The user explicitly requests node workflows resembling Magnific Spaces, with
free references and optional scripts; preserve all fifteen node types, app
forms, techniques, history, selection, batches and upstream/downstream runs.

The existing canvas composition stays authoritative as a graph editor. Replace
its icon wall with a searchable tool palette; make the actual workflow legible
through named nodes, typed ports, direct uploads and role-aware references.
Person + outfit → image; image + optional motion → clip is a starter, not a
required document or fixed workflow. Focus a node makes controls usable without
forcing the whole graph to remain legible at overview zoom.

No new visual-world tournament or exact-comp claim for this local extension.
The separate Studio comp gates remain open. Quality evidence must come from
real local generation and user interactions, with device-sized captures.

The built editing pass adds visible undo/redo/save controls, a 50-step graph
history, per-tab draft recovery and explicit conflict resolution. Failed or
pending saves block dependent generation/export/App operations. Distinguish
initial loading, load failure, unsaved, saving, failed-save and concurrent-version
states. Never silently replace local edits with a server version. Text fields
retain native undo. New nodes find free space using measured bounds and focus
themselves; existing user placements remain authoritative. Thumbnail inspection
and opening a workflow are separate actions.

Late-wire continuation was verified with an existing clip: adding a last-frame
wire after generation derives the missing frame without rerendering the source.
A UI-generated 6.0625-second continuation and 9.1667-second combined render were
played and every frame analyzed. No sustained hold of at least 0.6 seconds was
detected; choreography and universal consistency remain unproven. See
docs/PROSPERO_SPACES_CONTINUITY_QA_2026-10-05.md. Desktop/light, mobile/dark and
mobile/light error/recovery captures were reviewed. No new independent review
or exact-comp approval is claimed by this extension.

## Direct video workspace extension — task 1167, 23 October 2026

### Purpose and inherited direction

Operate. Open a usable, connected video canvas immediately, resume existing work
and let the user generate, inspect and revise clips in place. The inherited
`@xyflow/react` graph editor remains the working surface. Keep the original
dragon-and-wand icon, dark plum ground, magenta actions/selection, shared light
theme and compact working sans typography. Root `DESIGN.md` supplies inherited
tokens; this extension introduces no replacement identity or composition claim.
The separate Studio composition gates and root metadata remain unchanged.

### Entry and editing

The primary Create video entry opens the selected project's remembered canvas,
falling back to an existing canvas. If none exists, it creates a `quick_video`
graph; if no project exists, it creates one. A library image entry creates a new
connected graph with that reference selected. Opening failures show Retry. The
Spaces list and editor also offer New video/New workflow without a compulsory
document, wizard or production review screen.

The starter is Media → Clip, not a fixed workflow. Upload/pick references in the
source node, describe movement in the video node and generate there. Create an
image in this node converts the source to an image generator while retaining
existing images in a connected upstream reference node. Editable ports, branches,
last-frame continuation, Combine, all existing node tools, run scopes, templates,
Apps and save/recovery controls remain available. Production tools stay
accessible; new music-video productions continue automatically by default,
with song/animatic review still available by explicit choice.

### Model and output controls

The video node exposes model choice, duration and an engine status with Check.
Its catalog checks each workflow's required nodes and exact model-file choices;
cached inventory is labelled offline rather than live readiness. Automatic
selects an available Wan 14B installation, otherwise Wan 5B. Four-step generation
is selectable for the 14B path. Readiness describes dependencies, not successful
generation or artistic quality. Motion, end-frame and audio connections retain
their corresponding engine controls.

Play the selected output inline using native controls. Download, Enlarge, run
selection and take selection stay with the result. Compare takes adds a second
player below the current output when the previous run has an output in the same
position; it does not imply synchronized playback or an arbitrary take pairing.
Regeneration remains a node action. Monitors contain the full frame with dark
letterboxing; they do not crop portrait output to fill a landscape box.

### Local geometry and responsive focus

Video and Combine nodes are 440px wide in graph coordinates. Output video uses
full width, a 16:9 monitor with a 280px maximum height and 6px corners. Result
actions wrap. These are surface geometry, not global design tokens. Selected
nodes keep the existing 2px magenta outline with 3px offset and 12px handles.
At a canvas width of 600px or less, Focus uses the canvas rectangle intersected
with the visible window width and reserves 48px plus handle/outline allowance.
It fits by width and permits vertical panning rather than reducing a tall node
to fit its height. At 700px or less, the toolbar wraps and the secondary module
and vitals regions recede; their tools remain reachable through navigation.

### Evidence and limits

Inspected `frontend/src/App.tsx`, `views/VideoWorkspace.tsx`, `views/Spaces.tsx`,
`studio.css`, `prosperos_hoard/video_engines.py`, production defaults and
`docs/video-workspace.md`. Opened all three final captures in
`D:/LocalAI/research-inbox/prospero1167/ui/`: `desktop.png` and
`desktop-compare.png` at 1280×800, and the replaced `mobile.png` at 390×844.
They show the declared demo backend. Desktop shows the connected source/clip
and comparison; mobile shows both port edges and the selection outline after
the focus correction. Tall lower controls continue beyond that viewport.

The scoped finish review found F1 (mobile clipping); `finish-verdict.md` resolves
F1 with the replaced capture and returns ship for that correction. The documenter
did not rerun tests or operate the app. Static captures do not establish all
job transitions, keyboard behavior, comprehensive accessibility or real model
quality. Real-generation evidence remains separate coordinator work. Historical
continuation measurements above belong to their original pass. No exact-comp,
root-gate closure or whole-product approval follows from this extension.
