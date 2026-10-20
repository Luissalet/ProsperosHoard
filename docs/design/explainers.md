---
name: "Prospero — Explainers"
description: "Built scene, source and portable-figure editing inside the existing production workbench."
colors:
  action: "var(--accent)"
  action-hover: "var(--accent-2)"
  action-ink: "var(--accent-ink)"
  action-soft: "var(--accent-soft)"
  panel: "var(--surface)"
  hover: "var(--surface-2)"
  divider: "var(--border)"
  control-border: "var(--border-strong)"
  text: "var(--text)"
  label: "var(--text-2)"
  supporting: "var(--muted)"
typography:
  heading:
    fontFamily: "Cinzel, serif"
    fontSize: "15px"
    fontWeight: 600
    letterSpacing: "-0.005em"
  body:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "14px"
    lineHeight: 1.45
  label:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "12.5px"
  button:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "13px"
    fontWeight: 500
rounded:
  control: "6px"
  field: "9px"
  panel: "12px"
spacing:
  label: "5px"
  row: "10px"
  group: "12px"
  panel: "18px"
components:
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.control}"
    padding: "0 14px"
    height: "36px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0 14px"
    height: "36px"
  field:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.field}"
    padding: "7px 10px"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: "18px"
---

# Design System: Prospero — Explainers

## Overview

**Creative North Star: "The Shot Review Bench"**

This is the built Explainers surface for task #961 and its portable-figure expansion #1056, an **Operate** extension of Prospero's existing production workbench. Production identity, pipeline and current job action precede the scene editor. The editor groups narration, on-screen text, a library or retained portable figure and source associations so one scene can be corrected and produced again while unchanged page and sentence-audio assets are reused.

The surface inherits the plum ground, magenta actions, dragon-and-wand identity and shared cards, fields and buttons. It does not establish a replacement visual world. `PRODUCT.md` and root `DESIGN.md` remain incumbent context; this scoped record does not close the old Studio comp gates or repair root metadata. Source is authoritative for the current cascade: `studio.css` now declares Source Sans 3 body type and Cinzel headings, both imported by `App.tsx`; the older root document's Space Grotesk body description is not repeated as built truth here.

**Key Characteristics:**

- Production state stays above editable scene content.
- Native disclosures keep sources and repeated scenes manageable.
- Explicit save, produce and editable-project export actions retain the working context.
- Responsive shared controls preserve the same scene fields at narrow widths.
- Portable figures keep their capture, source and revision together, with local inspection before saving.

Evidence inspected for this record: `frontend/src/views/Explainer.tsx`, the scoped `Shorts.tsx` and `Productions.tsx` diff, `styles.css`, `studio.css`, `App.tsx`, and all four `explainer961-{desktop,mobile,desktop-editor,mobile-editor}.png` captures in `D:/LocalAI/research-inbox/expansiones-fusiones-20261020/`. The top and scrolled-editor pairs are viewport captures of the internal content pane, not full-page captures. `finish-review961.txt` returns **ship** with no material fixes in this scope.

The supplied native receipt reports 169 MCP tools, a real local Piper CPU render lasting 70.019 seconds, unchanged page IDs retained and unchanged sentence audio retained after correcting the second of three synthetic scenes. The separate browser receipt reports draft export, reopening a scene, the native produce button, no browser errors and no page overflow at the two captured sizes. The author reports 49 area tests passing and the supplied one-time detector result is `[]`. This documentation pass inspected those receipts and captures; it did not rerun tests or apps. The native receipt's historical `browser_pending: true` remains intact; later browser checks are in their separate receipt.

These checks cover the synthetic interaction and render described. They do not establish LLM planning quality, universal production reliability, every concurrent state, all themes or a complete accessibility audit. Word timings in that voice run were estimated. The main profile was not used for the supplied acceptance run.

The functional follow-up records source correction `933cafa` after independent submission-1 findings: external voice refresh could be overwritten by a text-only save, and unavailable SRT exports raised an exception error. The component now tracks explicit local voice edits separately, and the export route imports the HTTP exception used for controlled missing-export responses. `qa961-voice-r2-results.json` and `qa961-voice-r2b.log` now report the native author regression passing: an external speed (1.3) survives text-only save, explicit local voice edits save, and invalidated/unknown-extension exports return 404. The later source delta wraps editable controls in a borderless fieldset disabled only while saving and prevents editable-project export clicks during that request. Its frontend build passed in `prospero-r2b-build.log`.

The independent submission-2 receipt `review-r961/r2/verify.json` confirms external voice refresh, retained voice after text-only save, explicit local voice editing, retained local draft on conflict, current SRT (200), unavailable/invalidated/unknown-extension SRT (404), no browser errors and no overflow at desktop/mobile sizes. The reviewer reports 24 area tests, three own checks and build passing against `933cafa`; the subsequent temporary-disabled delta still needs its own submission/review. The new global R2 suite was still running at this refresh; no final pass is claimed. The earlier **ship** remains a visual-scope disposition. Layout and the four existing viewport captures are unchanged; this documentation pass inspected source/receipts without running a browser or generation.

For expansion #1056 this record additionally inspects `Explainer.tsx`, `api.ts`, `components/ui.tsx`, `prosperos_hoard/explainers.py` and `figures.py` at `ee52c0c`, the brief and submission packets, and `qa1056/results-final.json`. The four opened final captures are `figure-final-1440.png`, `figure-final-390.png`, `preview-final-1440.png` and `preview-final-390.png`. The figure pairs are scrolled viewport views of the actual inherited panel; the preview pairs show its local inspector. They use a real retained PNG exported from a synthetic diagram, not a new composition reference or evidence of model quality.

The #1056 author reports 20 backend area tests passing, including atomic invalid-input rejection, distinct states with identical PNG pixels, selective page/voice reuse and reimport into an independent store. The final supplied receipt reports 170 MCP tools, unchanged cold modules, exact retained source and import/export, revision 3/state `inside`, and atomic invalidity. Browser checks at widths 1440/390 report save/reopen, exact source download and dirty-project export, replacement with text, invalid import retaining the draft, keyboard preview/zoom/fit with the draft intact, no JavaScript errors and no page overflow. The final TS/Vite build passed. The full suite was still running without a terminal result at this documentation refresh; no full-suite pass is claimed. Initial helper expectations and the corrected `useDialog` build mistake remain recorded in `prospero1056-checker-errors.txt` and the original logs.

`qa1056/FINISH.txt` first returned **fix** for F1, the missing full-size capture inspection. After the scoped fix, `qa1056/FINISH-F1.txt` resolves F1 and returns **ship only for F1**, based on the four final captures with interaction receipts as supporting evidence. This does not approve the whole surface, close root Studio gates, or certify global accessibility. This documentation pass ran no apps, models, browser checks or product tests; it inspected supplied evidence and preserved earlier receipts and verdict scopes.

## Colors

The frontmatter binds to live CSS roles rather than creating a second palette. Primary action, hover, ink and soft focus use the inherited magenta/pink family. Flat working panels, dividers and secondary text use the current theme's neutral roles. The reviewed captures are dark theme; light-theme values remain inherited source behavior without a new rendered-theme claim.

**The Inherited Accent Rule.** Use the shared action and focus variables for this editor; source checkboxes and native field focus already use them.

## Typography

The built panel heading is the shared card heading: Cinzel serif, with compact source-defined size and weight. Source Sans 3 carries fields, body copy and actions. Supporting copy uses the existing small-text treatment (12px); narrative textareas retain the shared line-height (1.5) and vertical resize behavior. No Explainers-specific font or type scale is introduced.

**The Working Copy Rule.** Preserve explicit field labels and short action names in Spanish and English; narration and on-screen copy remain separately editable.

## Layout

The inherited shell fills the dynamic viewport and scrolls inside its content pane. At the top, production name/status and pipeline remain visible before “Escenas y fuentes”. Inside that panel the order is explanation, voice and speed, optional material disclosure, ordered scenes and final actions. Older short-detail content follows the new panel.

The shared two-column grid places voice and speed side by side with a group gap from the frontmatter; it becomes one column at the shell's narrow breakpoint (700px). Scenes use a vertical stack. Fields and panels have zero intrinsic minimum width, and inputs/selects/textareas have bounded maximum width. Move/delete/save/export controls use wrapping rows. Desktop retains the left rail; mobile uses the existing Tools trigger and wrapped header/pipeline.

The opened desktop evidence (1440×1000) shows production state and editable scene content in the first viewport. At mobile (390×844), the taller inherited header and wrapped pipeline leave room for the voice controls and start of the first scene; narration and on-screen text are visible in the scrolled-editor capture. Both supplied browser overflow checks are false. Keep the internal-scroll distinction when taking future evidence.

The portable-figure block follows the existing image selector and labelled file input. Its compact PNG is contained at the panel width and capped in height (240px), followed by title/revision/page or state, wrapping actions, and a retained-source explanation. Its image button uses intrinsic height so the shared compact button height does not overlap the caption. The inspector fills the viewport with a wrapping toolbar and a flexible, focusable scroll region; actual-size and 2× images scroll inside that region rather than widening the page. The final 1440×1000 and 390×844 captures show the same revision and state; mobile wraps the actions and toolbar while retaining every control.

## Elevation & Depth

The editor is a flat shared card separated by a thin border. Source groups use native fieldset boundaries; scenes use native disclosure markers. The #1056 inspector reuses the existing dark full-viewport overlay and shared dialog layering, with the retained PNG as its content. No new shadow, decorative media or form entrance animation is introduced. Existing asynchronous job progress remains in the production header; inherited button feedback and focus rings remain available.

## Shapes

Use the shared rounded panel, field and button primitives recorded in the frontmatter. Disclosure triangles, source checkboxes and fieldset legends retain native semantics and appearance. There is no Explainers-specific card silhouette or icon system.

## Components

### Entry and creation

`ProductionsView` adds “Nuevo explicativo” beside existing new-video and new-short actions, including the empty state. It opens the existing short modal with explanatory mode selected. This mode defaults to landscape (16:9) and no music, offers optional source material and editable JSON import, and preserves topic/script entry, voice, music, captions and output-aspect controls. Script paragraphs become initial scenes. Picture-source, clip-count and variant-count controls remain available in ordinary short mode. These additions preserve earlier video/short entry points.

### Scene panel and disclosures

`ExplainerScenes` appears when the short production contains an explainer spec. Its named section uses the shared card/stack primitives. “Material y referencias” is initially collapsed and contains source title, reference/version and supplied content, plus Add source. Each scene has its own stable ID and native `details`/`summary`; scenes are initially open when there are fewer than four.

Each scene exposes title, narration, on-screen text, an image-library selector and portable-figure import. No selected image uses “Página de texto animado”. An imported figure adds an explicit selected option; choosing a library image or the text-page option clears that portable figure. A fieldset associates sources by ID; a source-review checkbox appears when that scene has source associations. Move up/down preserves the scene object and ID, boundary moves are disabled, and deletion is disabled for the last remaining scene. Add scene creates a fresh ID with empty editable content.

### Voice and revision state

Voice defaults to the language choice when no explicit voice is selected. An existing voice absent from the returned catalogue remains visible as “Voz actual”. Speed uses a numeric field with range (0.5–2) and step (0.05). Source image choices are loaded for the current project with a limit (200); library choices remain a selector. Portable figures have the separate retained-capture preview described below.

Local edits mark the draft dirty and retain it across incoming revisions. Voice has a separate local-edit flag: incoming voice ID and speed update their controls while the user has not explicitly changed either voice field, including when a text-only draft is pending. Once the user changes a voice field, its local values remain until a successful save clears that flag. A text-only save omits the voice payload, so an externally refreshed voice is not sent back as an apparent local edit. Native author and independent submission-2 receipts now verify this correction in their described cases.

If a newer revision arrives while dirty, a visible alert states that the draft is retained; save submits its `base_revision`. API failures appear as alerts. Changed-source scene IDs appear in a status message. Failed catalogue/image loading is caught without a dedicated retry message in this component; the document does not imply one.

### Save, produce and export

“Guardar escenas” is the primary action and is disabled when clean or busy. “Guardar y producir” saves with `run: true` and is disabled while busy or a production run is live. Voice settings are sent only after an explicit local voice-field edit. Successful save takes the returned explainer revision, clears both draft and voice dirty flags and refreshes production/jobs.

During a save request, a borderless fieldset temporarily disables descendant form controls, including voice, scene/source fields and editing buttons. This prevents new typing from being replaced by the returning saved draft. The fieldset adds no visible inset or border, and controls are re-enabled when the request settles, including on failure. Native disclosure summaries are not form controls and are not described as disabled by the fieldset. Editable-project export has its own busy check that prevents the download click during saving; this is not a new visual layout or a claim that every link is disabled.

Editable JSON export uses the existing production endpoint. A dirty draft changes the label to “Guardar y exportar proyecto”; the click first saves, then downloads only after save succeeds. A clean draft downloads directly. SRT is offered when both narration and report are done. The corrected export route returns a controlled HTTP 404 when the SRT file is absent, narration/report are not done, or an unknown export extension is requested; invalidated subtitles are not served as current. Current-route regression assertions cover draft, valid, invalidated and unknown-extension exports, with test execution reported in the submission-2 packet. The reuse promise is supported by the supplied native correction receipt; it should continue to be checked with retained asset IDs rather than inferred from the visual layout.

**The Scene Identity Rule.** Preserve scene/source IDs while editing and moving existing content; use returned revisions and retained assets as the evidence for selective reproduction.

### Portable figures and provenance

“Importar o sustituir figura (.vfigure)” reads the selected JSON into the current scene draft by stable scene ID and clears its old `asset_id`. The asynchronous read applies to the latest draft through the functional state update; the existing busy fieldset protects pending editing, and a failed parse/basic format check displays an alert without replacing the draft. This local check requires the format, PNG declaration/data and source content. Full backend validation occurs before saving: document identity, positive revision, native diagram source, selected page or isometric state, valid retained PNG and HTTP(S) origin links without credentials. Invalid-save atomicity is supported by the supplied tests and receipt.

The complete `vitruvius.figure/1` object stays in `scenes[].figure`, including PNG, full native source, revision, page or state and optional fields. The compact image and caption expose the retained title and selection. “Descargar figura y fuente” downloads that complete local object as `.vfigure`; “Abrir diagrama de origen” is an explicit link when a valid editor URL exists. The explanation says that another revision or state requires importing a new figure. Import, inspection and download do not fetch, synchronize or start the originating application.

Editable-project export retains the same full figure. Reimport into another profile does not depend on the old `asset_id`: validation removes it for portable scenes, and production materializes the exact PNG as a native image with the complete figure in its recipe. The page-cache signature includes the whole figure, so different retained states remain distinct even when their PNG pixels match. Existing sentence-voice caching remains separate. The author reports selective reuse and independent-store tests; those are scoped backend checks, not a new universal reliability claim. Portable captures omit automatic Ken Burns motion; finishing defaults to fit when a figure exists unless framing is explicit.

**The Retained Figure Rule.** Keep the capture and complete source together; use explicit import to change revision or state and an explicit link to visit the origin.

### Local capture inspection

Clicking the PNG or “Ampliar figura” opens the retained draft capture immediately, before save or production and without an asset ID or origin connection. Both openers are native buttons with an accessible name, so keyboard activation follows normal button behavior. The inspector uses `useDialog` for dialog ordering, focus containment, Escape and return to the connected opener. Its labelled modal contains Close, Fit, Actual size and Zoom 2× actions; scale buttons expose their pressed state.

Inspection starts at actual size. Fit bounds the capture to the available region; actual size preserves native pixels, and 2× doubles its natural width. A labelled, focusable native scroll region allows pointer and keyboard panning when the image exceeds the viewport. Closing unmounts only the inspector and retains the scene draft. The final receipt verifies Enter activation, measured 640px native width to 1280px at 2×, mobile keyboard scrolling, fit, Escape/focus return and exact pending text. The four final captures support legible enlarged labels, separated caption/actions and wrapping controls; this scoped keyboard evidence is not a full accessibility certification.

### Shared controls and accessibility

Primary and secondary buttons retain hover, active, disabled and visible-focus states. Fields use the shared surface, strong border and magenta focus ring. Labels wrap their controls; source associations use fieldset/legend, disclosures use native summary, and errors/status use their corresponding roles. These observed semantics support the local review without claiming a full accessibility certification.

## Do's and Don'ts

### Do:

- Do keep production state ahead of the scene editor and preserve existing short/video entry points.
- Do retain the shared theme roles, native disclosures, labelled fields and wrapping action rows.
- Do preserve local edits when a newer revision appears and show the conflict warning.
- Do save a dirty draft before exporting its editable project.
- Do verify selective reproduction with unchanged page and sentence-audio asset identities.
- Do retain the complete portable figure and expose its revision and page or state beside the capture.
- Do allow local capture inspection before saving, with keyboard closure and focus return to the working scene.

### Don't:

- Don't promote this surface composition into a global Studio template.
- Don't add form entrance motion or a replacement visual identity to this scoped editor.
- Don't describe the synthetic render as proof of LLM planning, scientific accuracy or universal reliability.
- Don't treat these viewport captures or the scoped ship disposition as closure of inherited Studio comp gates.
- Don't imply that opening or importing a retained figure synchronizes it with its origin.
