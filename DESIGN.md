---
name: "Prospero's Hoard"
description: "Prospero's original plum-and-magenta creative review bench with its preserved dragon-and-wand icon."
colors:
  primary: "#e65db6"
  primary-hover: "#eb80c6"
  primary-ink: "#1b1318"
  primary-soft: "rgba(230, 93, 182, 0.14)"
  bg: "#1d141a"
  bg-2: "#170f14"
  surface: "#251a22"
  surface-2: "#31242c"
  surface-3: "#392a34"
  border: "#44343e"
  border-strong: "#6c5664"
  text: "#ece1e8"
  text-2: "#beb4ba"
  muted: "#a69ba2"
  light-primary: "#d92d6b"
  light-primary-hover: "#b8215a"
  light-primary-ink: "#fff"
  light-primary-soft: "rgba(217, 45, 107, .1)"
  light-bg: "#f6f2f8"
  light-bg-2: "#efe9f3"
  light-surface: "#ffffff"
  light-surface-2: "#f7f3fa"
  light-surface-3: "#eee6f3"
  light-border: "#e2d8e9"
  light-border-strong: "#cfc1da"
  light-text: "#1c1422"
  light-text-2: "#4a3f55"
  light-muted: "#695d75"
  ok: "#5bbf86"
  warn: "#e2b24c"
  bad: "#e07a6e"
  info: "#7fa6d9"
  gold-soft: "rgba(226, 178, 76, 0.14)"
  light-ok: "#18945f"
  light-warn: "#a8741a"
  light-bad: "#c9373a"
  light-info: "#2f5fcf"
  light-gold-soft: "rgba(168, 116, 26, 0.12)"
  port-image: "#b48cf0"
  port-audio: "#f0a04b"
  port-any: "#9a95a6"
  monitor-black: "#08090a"
typography:
  display:
    fontFamily: "Cinzel, Iowan Old Style, Charter, Georgia, Liberation Serif, Noto Serif, Times New Roman, serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "0.005em"
  headline:
    fontFamily: "Cinzel, serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "0.005em"
  composer-title:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "22px"
    fontWeight: 600
    lineHeight: "normal"
    letterSpacing: "0.005em"
  section-title:
    fontFamily: "Cinzel, serif"
    fontSize: "18px"
    fontWeight: 700
    lineHeight: 1.45
    letterSpacing: "0.005em"
  card-title:
    fontFamily: "Cinzel, serif"
    fontSize: "15px"
    fontWeight: 600
    lineHeight: 1.45
    letterSpacing: "-0.005em"
  body:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  button:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "13px"
    fontWeight: 500
    lineHeight: 1.45
  label:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.45
  mono:
    fontFamily: "JetBrains Mono Variable, monospace"
    fontSize: "12.5px"
    fontWeight: 400
    lineHeight: 1.45
rounded:
  sm: "6px"
  media: "8px"
  input: "9px"
  composer: "10px"
  surface: "12px"
  pill: "999px"
spacing:
  4: "4px"
  6: "6px"
  8: "8px"
  10: "10px"
  12: "12px"
  14: "14px"
  16: "16px"
  18: "18px"
  20: "20px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-ink}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "36px"
  button-primary-hover:
    backgroundColor: "{colors.primary-hover}"
    textColor: "{colors.primary-ink}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "36px"
  button-secondary-hover:
    backgroundColor: "{colors.surface-2}"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-2}"
    typography: "{typography.button}"
    rounded: "{rounded.sm}"
    padding: "0 14px"
    height: "36px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.input}"
    padding: "7px 10px"
  rail-item-selected:
    backgroundColor: "{colors.primary-soft}"
    textColor: "{colors.primary}"
    rounded: "{rounded.sm}"
    width: "100%"
  pill:
    backgroundColor: "{colors.surface-3}"
    textColor: "{colors.text-2}"
    rounded: "{rounded.pill}"
    padding: "2px 9px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
    padding: "18px"
  node-selected:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
  vitals-summary:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "5px 10px"
  table-scroll:
    backgroundColor: "transparent"
    width: "100%"
  clip-node:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.surface}"
  inline-cast-editor:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    rounded: "{rounded.composer}"
    padding: "18px"
---

# Design System: Prospero's Hoard

## Overview

**Creative North Star: "The Shot Review Bench"**

The Shot Review Bench places real images, clips and references on a quiet dark-plum ground. Pale pink-white text and magenta controls establish a practical, concentrated workspace. The original dragon-and-wand icon and magenta/pink brand family are explicitly pinned by the user; media come from project assets.

The built system combines flat tonal panels, thin dividers and compact controls. Source Sans 3 carries the working interface and JetBrains Mono carries machine values. Legacy serif page headings still survive in the cascade; this record preserves that fact instead of describing an entirely sans interface. Light mode uses pale lilac surfaces and the original pink action.

**Key Characteristics:**
- Real project media occupy the inspection surfaces.
- Magenta identifies actions and selection; status and typed data retain their separate colors.
- Flat panels and thin dividers organize dense working controls.
- Compact sans working type coexists with inherited serif page headings.

The shared system was scanned on 5 October 2026 and extended from the built project dashboard/clip interface on 21 October 2026. Normative token values above follow the current cascade: `App.tsx` loads bundled fonts, then `styles.css`, `studio.css` and `project-studio.css`. The shared `hoard-theme.css` supplies the restored original Prospero dark palette and legacy serif stack. The final `studio.css` maps its dark roles to those `--hoard-*` tokens and supplies the light palette; the current light muted-text token is a legibility adjustment. Surface mode and strategy remain in `.impeccable/direction.md`, `.impeccable/surfaces/spaces.md` and `.impeccable/surfaces/project-dashboard-clips.md`; their compositions are not global requirements.

The supplied final reviews ship the Studio F1–F4 corrections and the Spaces mobile-focus F1 correction at those scopes. Studio's formal plates/hero workflow remains open/pending in `.impeccable/build/state.json`; no exact-comp pass or whole-product approval is asserted. The Spaces reviewer had no Operate QUALITY BAR card. Subsequent brand/monitor captures show populated Spanish image generation in both themes at desktop/mobile widths, plus comparison and intermediate-width states. These support the specific palette, header and media-fit observations, without establishing comprehensive light-theme or whole-product approval.

The 21 October dashboard/clip extension follows the user's reference-to-video topology within the established world. Its supplied finish review requested two copy corrections; the continuation at `6e350ac` resolves both and returns scoped ship. This does not close the historical October 5 composition/type gates or certify generation quality. The documenter read the current sources and opened all eleven extension captures; demonstration media establish interaction/layout evidence only.

## Colors

The review palette preserves original Prospero plum neutrals, pale pink-white text and the magenta/pink action family. Frontmatter keys without `light-` resolve the dark palette from `html[data-hoard-app="prospero"]`; `light-` keys preserve final `:root[data-theme="light"]` values. The user pins this brand family for actions, focus and selection.

### Primary

The explicit user brand correction supersedes the earlier delegated copper palette. Orange/copper must not become brand highlights; semantic warning/port colors and image content retain their actual meaning.

- **Prospero magenta action** (`primary`, `primary-hover`, `primary-ink`, `primary-soft`): generation, selected navigation, selected take outlines, focus and text selection. Light mode supplies its original pink, hover, ink and translucent wash counterparts.

### Neutral

- **Deep plum ground** (`bg`, `bg-2`): application background, rail and secondary ground.
- **Working panels** (`surface`, `surface-2`, `surface-3`): cards, controls, popovers, hover and selected tonal states.
- **Thin dividers** (`border`, `border-strong`): panel separation and stronger control boundaries.
- **Pale pink-white type** (`text`, `text-2`, `muted`): primary, secondary and supporting text.
- **Pale lilac ground** (`light-bg` through `light-muted`): source-defined light counterparts for the same roles.
- **Inspection black** (`monitor-black`): the media monitor keeps a dark ground in both themes.

### Semantic colors

Success, warning/gold, failure and information retain their `ok`, `warn`, `bad` and `info` values and source-defined light counterparts. `gold-soft` supplies the matching warning wash. These are functional state colors, not additional brand accents. Typed graph connections use text=`info`, image=`port-image`, video=`ok`, audio=`port-audio`, any=`port-any`; these literal port colors do not switch with light mode. Pills use a translucent tonal wash, including `color-mix()` for statuses.

**The Functional Accent Rule.** Use magenta for actionable or selected controls; retain distinct status and typed-port colors where they encode meaning.

## Typography

**Working font:** Source Sans 3, sans-serif, bundled through `@fontsource/source-sans-3`. **Machine-value font:** JetBrains Mono Variable, monospace, bundled through `@fontsource-variable/jetbrains-mono`.

**Inherited page-heading stack:** Cinzel, Iowan Old Style, Charter, Georgia, Liberation Serif, Noto Serif, Times New Roman, serif. The current `App.tsx` imports `@fontsource/cinzel`; `studio.css` assigns Cinzel to generic headings and the brand name, and preserves Source Sans 3 as `--font`. Space Grotesk Variable is still imported but is not the working-font token. The project dashboard and clip headings explicitly use `--font`, while the production/storyboard keeps inherited serif headings.

The hierarchy is compact and task-oriented, with heavier titles and quiet labels. It is not a single geometric sans display system: the more specific legacy page-heading selector overrides the later bare heading rule. Spaces' list H1 visibly uses that serif appearance.

- **Page heading / display:** inherited serif stack, 30px, 700, line-height 1.25, letter-spacing 0.005em. This actual cascade exception survives on page-head H1 elements.
- **Generic H1 / headline:** Cinzel/serif, 24px, 700, line-height 1.25 and 0.005em tracking. The project title explicitly uses the working sans at 26px (22px at ≤800px); the clip heading uses the working sans at 22px and 600 weight.
- **Section:** generic H2 is Cinzel/serif at 18px, 700; dashboard section headings override it with the working sans at 18px. Shared card titles are 15px, 600 with −0.005em tracking. Inherited body line-height is 1.45 except where source overrides it.
- **Body:** 14px, 400, line-height 1.45. Textareas use 1.5; supporting explanatory copy commonly uses 1.6–1.7.
- **Controls:** buttons are 13px, 500; primary buttons use 600. Field labels and base node copy are 12.5px. Supporting metadata and ports commonly range from 10px to 12px.
- **Machine values:** 12.5px mono for code and the mono utility, with local 10.5–12px variants for IDs, timing and key/value metadata.

This is the observed role hierarchy, not a mathematical type scale. The inherited serif page-heading stack is a disclosed formal TYPE difference from the selected contemporary-sans comp. Recording the cascade does not certify that difference as an approved adaptation or authorize silently changing it; it remains to be reconciled before a formal fidelity claim.

## Layout

The application fills the dynamic viewport and scrolls inside its content pane. Repeated working gaps and padding use the frontmatter spacing vocabulary, with 12–20px common for groups and panels. Narrow layouts wrap controls instead of truncating actions; tool access remains available through a drawer. The small rail uses a label below an icon, while wider top navigation uses text tabs.

Existing shell dimensions are evidence, not universal page templates: the desktop rail is 84px, header minimum 64px and content padding 20px. At 900px content becomes 14px; at 700px the rail yields to the mobile Tools trigger, header wraps at a 100px minimum and content uses 14px 12px. At 1360px the header wraps with 8px vertical padding. At 1200px the product name hides and tab gaps compact; the telemetry summary remains. Below 1100px only host/compute details hide in the closed summary. At 700px the summary moves to its own full-width row, with tabs below it, so runtime header height can exceed the minimum. Surface-specific grids and Spaces breakpoints are recorded in the sidecar and source, not promoted into global composition rules.

Cards, fields and form controls allow their intrinsic width to shrink: panels/fields have min-width:0 and controls have min-width:0 with max-width:100%. Shared two- and three-column grids use minmax(0,1fr) tracks and become one column below 700px. Mobile page-head actions take full width, align left, give selectors their own full-width row and wrap segmented controls with a 40px minimum target. Shared content tabs wrap at 700px with 44px minimum-height buttons. Production preview export and canvas-download rows also wrap.

**The Local Overflow Rule.** Keep wide tables and editing tracks inside their own focusable or scrollable regions; let the surrounding panel shrink and let controls wrap without losing actions.

Shared inspection overlays use a flexible media region and a 320px information region, becoming a stacked view below 700px. Media controls wrap and the information region continues to scroll. Empty, busy and error states occupy the same working flow.

The project dashboard is centered with a 1600px maximum width. Its compact identity row uses an 88px cover, brief/resource links and direct Create clip/New music video actions. Below it, project videos and recent assets occupy the main column; cast and project tools occupy the side column. The grid uses 1.7fr/minimum-300px tracks and a 36px gap; at ≤1100px it uses 1fr/minimum-280px tracks with a 24px gap and a full-width action row. At ≤800px it stacks main content before the side column, reduces the cover to 66px and lets the two main actions share available width. Cast may therefore sit below recent assets on mobile. These are local dashboard measurements.

On desktop, the clip canvas is a bounded dotted workspace, at least 680px tall or `100dvh - 190px`, with a 260px reference node at left and a 640px video node at right. Additional motion/end guides sit below the reference and connect to the video. Nodes drag by their headings; pan and zoom controls remain available. Connections are supplied by the interface, not user-wired. At ≤800px the graph is replaced with a vertical reference/guides/video stack (18px outer gap, 14px reference gap); it has no draggable nodes and uses matching mobile help. The video node retains preview, prompt and generation together. The prompt and Generate action share a row with a 122px action column, followed by camera/duration and Advanced.

## Elevation & Depth

Depth is primarily tonal. Cards, graph nodes, the brand mark and inspected media have their previous shadows removed by the final stylesheet. Menus and popovers retain a soft theme shadow: dark `0 16px 46px #0006`, light `0 16px 46px #3c1e501a`. Thin borders separate structure; the selected node adds a 2px magenta outline with 3px offset. The Tools drawer uses a translucent dark scrim, a stronger divider and a short reveal instead of ornamental lifting.

**The Tonal Panel Rule.** Cards and nodes rest flat. Menus and popovers may use the theme shadow; selected nodes use an outline rather than a lifted shadow.

## Shapes

The built vocabulary uses restrained rounded rectangles. Small controls, rail items and dividers use the 6px small radius; monitors and media wrappers commonly use 8px; fields use 9px; composers use 10px; cards, menus and nodes use the 12px surface radius. Small buttons retain a local 8px radius and large buttons an 11px radius. Status pills are fully rounded. Dashed borders identify add/upload affordances; regular panel separation uses a 1px stroke. The vocabulary has local variants, not one enforced radius for every component.

## Components

### Buttons

Compact, direct controls. Standard buttons are 36px high with 0 14px padding, a 1px border, 6px corners and 13px type. Primary uses magenta and accent ink, with 600 weight and the hover magenta token. Secondary is transparent with a divider border; hover uses surface-2 and the strong border. Ghost removes the border and uses secondary text, strengthening on hover. Active moves down 1px. Disabled opacity is 0.45. Small (28px) and large (42px) variants remain; context may enlarge targets. The global visible focus outline is 2px magenta, offset 3px.

### Inputs / Fields

Surface fill, strong 1px border, 9px corners and 7px 10px padding. Focus changes the border to magenta and adds a 3px translucent accent ring; the field selectors suppress the generic outline. Textareas resize vertically. Labels, hints, errors and optional disclosures remain explicit; defaults vary in denser graph nodes. Range and checkbox controls share the accent. Shared cards and fields keep min-width:0; input, select and textarea controls have min-width:0 and max-width:100%, so long values remain inside the panel. Mobile selectors and action groups wrap rather than displacing their neighboring fields.

### Chips / Status

Ordinary progress bars and production progress use the original magenta accent as a solid fill. Waiting, warning and failure bars retain semantic treatments. Library keyboard focus uses the same theme accent.

Pills use surface-3, secondary text, 2px 9px padding, 11.5px type and 550 weight. Semantic variants use their own colored text and translucent wash. Selected state is conveyed in text as well as magenta, such as the take strip's Selected label and pressed selection buttons.

### Cards / Containers

Surface fill, a thin divider border, 12px corners and 18px shared padding. Working Studio cards use a local 16px inset; composer uses 10px corners. Resting cards carry no shadow. Title rows align labels and actions without sacrificing wrap on narrow screens.

### Navigation / Drawers

The compact rail has 65px minimum item height, 10px unbroken labels, 8px 2px internal padding, 6px corners, muted default text and magenta over accent-soft when selected. Top tabs signal the active item with a 2px magenta bottom border. All eighteen routes remain in the searchable Tools drawer. Its width is min(350px, 92vw), and it retains focus trapping, Escape dismissal and connected-opener focus restoration. Its 180ms clip-path reveal uses cubic-bezier(.16,1,.3,1), disabled for reduced motion.

Shared mobile content tabs wrap with 44px minimum-height targets; page-head segmented choices, including Voice, wrap with a 40px minimum. The rail keeps the full Spanish Herramientas label on one line through its compact horizontal padding.

### Header telemetry

The closed Vitals button keeps one labeled VRAM percentage and 5px-high bar per reported GPU, plus aggregate used/total GB. The supplied machine has four cards; the component derives its count from live API data rather than hard-coding four. The summary uses 11px text, 10px mono GPU labels, 6px corners and 5px 10px padding. Bars use semantic success/warning/failure thresholds at 75% and 92%. At narrow widths only host/compute details leave the closed summary; per-card bars and total stay visible, and mobile gets a full-width row. The expandable panel retains every card, RAM/CPU, service/model information and refresh/free/stop actions. This records the current observability component, not a requirement to add diagnostics to every surface.

### Engine status / Retry

The Generate engine panel distinguishes an unanswered first check from a stopped backend. Before the initial memory/service request resolves, the live status reads Loading / Cargando with a warning indicator; it does not show ComfyUI off. A failed request shows status unavailable / estado no disponible and a separate alert containing the error with Re-check. Re-check is disabled while its request is pending. A completed response supplies the existing running, starting or down state and associated GPU information/actions. Later refreshes retain the last reported service state while checking; a request error overrides the status label with unavailable. The status text uses role=status and the request failure uses role=alert. Existing start, free-memory and service controls remain available according to their established state logic.

### Media inspection / Take selection

A reference or result opens the shared viewer in one click. Cast canonical portraits, Cast editing thumbnails and Designer reference thumbnails are named native buttons, so keyboard activation opens that same viewer. Selection for the monitor remains a distinct control with pressed semantics, a concise take number, measured duration and explicit selected text. Monitor images/videos use contain; their grid track and children have zero intrinsic minimum dimensions to preserve the entire frame inside a bounded region. Image-monitor pictures are positioned absolutely at inset 0 with full width/height and 100% maximum bounds, inside an overflow-hidden stage. Comparison images use clamp(220px, 45dvh, 420px) height and contain. Generation field grids use minmax(0, 1fr), zero field minimum width and width/max-width 100% inputs, so the optional negative-prompt control stays inside its panel. The viewer retains zoom, navigation, download, metadata and editing actions, real loading/error states and nested-dialog keyboard ownership.

### Asset picker / Library states

The reference picker combines a scrollable thumbnail grid and a bounded preview, with magenta selection, readable truncated asset names and separate Use/Cancel actions. Desktop has a flexible list and 300px preview; below 760px it stacks. Below 700px the grid retains two minmax(0,1fr) columns and a 38dvh maximum height, preview media cap at 180px, search takes a full row and footer help/actions wrap. The wider preview uses a 300px media cap and contained images/video.

Picker requests carry a generation guard: a later query or scope change invalidates older responses. Loading is explicit, failures provide retry, and Use stays disabled while loading, on errors, when the query has not reached its debounced value, or when the selected asset no longer belongs to the current results. Focused tiles use Enter to choose; Up/Down follow the actual rendered column count. Search keeps Left/Right for caret movement. Clicking selects; double-click or Enter uses the asset; the preview has a separate enlargement button.

Library has explicit loading and error/retry states, ignores stale responses and labels its search and filters accessibly. A filtered empty state offers Clear filters; it does not imply the project contains no media. These states preserve the panel's flow instead of silently displaying stale results as successful selection.

### Tables / Tracks / Compact visual labels

Activity, Backends, Jobs, Local Services, Shorts and Voice tables use a named horizontal region with tabIndex=0. The wrapper is full width, min-width:0, overflow-x:auto and overscroll-behavior-x:contain; cells can wrap long strings anywhere. Visible keyboard focus uses the global magenta outline with a local -2px offset, keeping the ring inside its viewport.

The SongTrack layout has a shrinkable main region and a 250px auxiliary region, collapsing to one column at 1100px. Its existing 600px minimum-width editing track scrolls inside the local bordered region with max-height:74vh; it does not stretch the whole application. Waveform section text is drawn only when measured text fits its available segment; the full section/time/energy legend remains available. These are surface-specific density adaptations, not a requirement that all content scroll horizontally.

### Graph nodes / Tool palette

Nodes are named, flat bordered panels with typed ports, direct media uploads, optional reference roles, generation/actions and versioned outputs. The searchable palette pairs each icon with a label and description. Selected nodes combine the magenta border with an offset outline. Fifteen node types remain available. Mobile node focus preserves the editing scale and permits panning; the existing Spaces brief owns this surface-specific strategy rather than a global layout requirement.

### Project dashboard / Inline cast

Project videos use actionable production cards with cover, title, status, stage/progress and an explicit Edit video label. They open the corresponding production directly. Resource counts are compact links to library, audio and timeline. The cover opens its asset in the viewer; a separate pencil chooses a replacement. The brief editor stays in the dashboard and exposes saving/error/cancel controls. Videos and recent assets have independent error/retry handling.

Cast rows pair a 48px canonical portrait or existing users icon with name and role/member count. Character and Group actions open the same inline editor used by their rows; there is no required cast modal or route change. The editor uses a surface fill, thin border, 10px corners and 18px inset. It contains name, description/concept, character-reference picker or group-member checkboxes, plus Save to project and Cancel. Busy state disables edits and actions; save failures remain inline. Manage cast still opens the full tool.

### Clip canvas / Generation state

A simple reference-to-video workspace extends the existing graph vocabulary without replacing the free-form Spaces editor. The desktop nodes are flat, with strong borders, 12px corners, 12px magenta handles and 2px magenta connections. The dotted ground belongs to the actual canvas. The reference offers library choice, upload/drop, remove and one-click enlargement; motion and end-frame guides appear as additional source nodes when requested. Both optional slots may be shown; a take can use a motion video or an end frame, with simultaneous selection disabled.

The video node groups its engine label, live job state, contained preview, prompt and Generate clip action. Its monitor is 210px tall on desktop and 220px at ≤800px. A vertical output remains fully visible with black letterboxing. Camera and 3/4/5-second duration controls remain visible; engine and seed live under Advanced. Comparison, enlargement, download, the project take strip and the return to project remain available. Take selection has a separate pressed control and explicit Selected text; clicking a thumbnail opens inspection.

State text follows the tracked job: recovering, queued, waiting for GPU, generating with progress, failed, cancelled, generated or finished without output. Recovery/read errors offer retry, in-flight jobs offer cancellation, and failed/cancelled jobs explain how to regenerate. Redundant raw status tokens are suppressed while substantive messages remain. If a previous selected take is shown during a new job, the state explains that it is an earlier take. Completion loads the asset ID returned by that job; a project video alone never proves that the current job produced it. Generation creates a project asset; entering from a storyboard frame preselects that reference and does not automatically bind the resulting clip back to a production shot.

### Production / Storyboard entry

The production keeps its main next-step action and storyboard/preview/text-and-takes access. Production process and the quality/history tools use native disclosures, with a return to project inside More tools. Storyboard shot cards retain insertion, inspection and editing; a shot with a chosen frame adds Create clip from this shot. This opens the clip studio with that frame as reference. Existing production stages and shot-linking operations remain in the production workflow.

## Do's and Don'ts

### Do:
- Do use the current theme variables for shared surfaces, text, borders, actions and focus.
- Do preserve a separate enlargement action and a visible selected state when inspecting media.
- Do retain readable labels, optional advanced disclosure and keyboard focus restoration.
- Do preserve full media frames in inspection views with object-fit: contain and bounded intrinsic sizes.
- Do contain table and editing-track overflow locally while wrapping surrounding actions.
- Do distinguish loading, failed requests and filtered-empty results before enabling a selection.
- Do distinguish an unanswered engine check and unavailable status from a completed backend-down response.
- Do keep the tracked generation state distinct from a previously selected project take.

### Don't:
- Don't use orange/copper for brand actions, focus or selection.
- Don't replace or recolor the original dragon-and-wand icon.
- Don't replace real project media with fictional branding or decorative QA images.
- Don't turn the Studio column widths, Spaces starter graph or surface-specific focus behavior into a mandatory layout for every route.
- Don't remove tools, node types or advanced operations to make a screen look simpler.
- Don't describe review verdicts as exact-comp approval or whole-product certification.
- Don't treat stale picker responses or a filtered-empty library as valid completed selection.
- Don't imply that creating a project clip automatically assigns it to a production shot.

Evidence: `frontend/src/studio.css`, `styles.css`, `hoard-theme.css`, `App.tsx`, `views/Studio.tsx`, `views/Spaces.tsx`, `components/ui.tsx` and `components/Lightbox.tsx`; final Studio desktop/mobile/user captures, Spaces list-desktop/mobile captures and `D:/LocalAI/qa/prospero-redesign-20261005/brand-monitor-{desktop-dark,desktop-light,mobile-dark,mobile-light,compare-dark,medium,mobile-results}.png`. Desktop-light and mobile-dark brand captures were opened for this refresh. Reviews: `.impeccable/review/finish-review.md` and `.impeccable/review/spaces-finish-review.md`. Source values remain authoritative when no rendered theme/state was reviewed.

The corrective deep pass additionally inspects `views/Library.tsx`, `Cast.tsx`, `Designer.tsx`, `Audio.tsx` and the table-bearing routes. The documenter opened `deep-reference-mobile-after.png` and `deep-storyboard-track-mobile-after.png` under the same QA folder; the parent reports opening the Cast, Voice, Audio, filtered-library, export, desktop-storyboard and user-generation captures. The parent reports a green build and 746 passed/3 skipped in `deep-tests.xml`, plus persisted optional script/shot edits and one measured real generation. Those checks are functional evidence for the tested cases, not artistic-quality certification. The deep finish reviewer passed the corrective scope with no unresolved material UI findings in the reviewed sample and resolved the FORM seed binding. Its formal disposition remains fix: inherited plates/hero reproduction, historical region mismatches and the serif TYPE discrepancy stay open. Hero reproduction evidence is a valid capture, not a measured reproduction pass. No new comp or whole-product approval is claimed. The review is `D:/LocalAI/qa/prospero-redesign-20261005/deep-review.md`.

The final EngineBar status-label correction was inspected in `frontend/src/views/EngineBar.tsx` and `i18n.ts`. The documenter opened `deep-engine-loading-after.png`; the parent opened the matching error/recovered captures and reports a 3-second delayed request, HTTP 503, Re-check and recovery to GPU 1 with no remaining alerts. The narrow reviewer continuation resolved this correction at its supplied scope in `D:/LocalAI/qa/prospero-redesign-20261005/deep-review.md`. This supplies no new formal-comp or whole-product approval.

### Spaces editing and continuation extension

The built canvas now exposes undo, redo and save. Its 50-step editing history
includes nodes, wires and names while excluding generation state and media.
Text inputs retain native undo. New nodes use measured bounds to find a free
position and are focused; existing user positions stay unchanged. Recovery and
conflict panels wrap their actions and long text in both themes and at mobile
widths. Initial load failure has an explicit retry rather than a blank runnable
canvas. A pending or failed save blocks dependent runs, App/build and exports;
per-tab local journals preserve edits when another tab saves. No conflict silently
replaces local edits. Thumbnail inspection stays separate from opening a workflow.

Evidence: `spaceEditing.ts`, `views/Spaces.tsx`, `studio.css`, nine editor tests,
reviewed `flow-desktop-light-after.png`, `flow-error-mobile-after.png`,
`flow-load-error-mobile-light.png`, `flow-tab-journals-after.png` and real video
contact sheets. The final server suite passed 751 cases with 3 skips; frontend
build and four repository demo screenshots passed. A UI-generated continuation
of an existing clip (6.0625 s) and a UI-wired combined render (9.1667 s / 220
frames) were played and inspected. Neither had a detected hold of at least 0.6 s;
the choreography remains approximate. The primary app was reloaded while idle,
preserving four active projects and five productions. See
`docs/PROSPERO_SPACES_CONTINUITY_QA_2026-10-05.md`. This extension has no new
independent review or formal composition/type gate closure.


Extension evidence (21 October 2026): `frontend/src/App.tsx`, `project-studio.css`, `studio.css`, `views/Overview.tsx`, `views/Studio.tsx`, `views/Productions.tsx`, `views/Storyboard.tsx`, `components/ClipCanvas.tsx` and `components/ProjectCast.tsx`; surface brief `.impeccable/surfaces/project-dashboard-clips.md`. Opened captures under `.impeccable/review/`: `dashboard-desktop.png`, `dashboard-light.png`, `dashboard-mobile.png`, `dashboard-user-1883.png`, `clip-desktop.png`, `clip-mobile.png`, `clip-mobile-controls.png`, `clip-user-1883.png`, `clip-guides.png`, `cast-inline.png` and `storyboard-user-1883.png`. The cast/editor and mobile-control captures use declared scroll positions; the guides capture uses Fit View. Finish evidence: `D:/LocalAI/research-inbox/prospero-dashboard1084/finish-review.md` and `finish-verdict.md`, with both copy findings resolved at `6e350ac`. Static captures do not certify transient motion, all job transitions or real model quality. Historical evidence above remains scoped to its original pass.
