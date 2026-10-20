---
name: "Prospero — Explainers"
description: "Built scene-and-source editing inside the existing production workbench."
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

This is the built Explainers surface for task #961, an **Operate** extension of Prospero's existing production workbench. Production identity, pipeline and current job action precede the scene editor. The editor groups narration, on-screen text, a library figure and source associations so one scene can be corrected and produced again while unchanged page and sentence-audio assets are reused.

The surface inherits the plum ground, magenta actions, dragon-and-wand identity and shared cards, fields and buttons. It does not establish a replacement visual world. `PRODUCT.md` and root `DESIGN.md` remain incumbent context; this scoped record does not close the old Studio comp gates or repair root metadata. Source is authoritative for the current cascade: `studio.css` now declares Source Sans 3 body type and Cinzel headings, both imported by `App.tsx`; the older root document's Space Grotesk body description is not repeated as built truth here.

**Key Characteristics:**

- Production state stays above editable scene content.
- Native disclosures keep sources and repeated scenes manageable.
- Explicit save, produce and editable-project export actions retain the working context.
- Responsive shared controls preserve the same scene fields at narrow widths.

Evidence inspected for this record: `frontend/src/views/Explainer.tsx`, the scoped `Shorts.tsx` and `Productions.tsx` diff, `styles.css`, `studio.css`, `App.tsx`, and all four `explainer961-{desktop,mobile,desktop-editor,mobile-editor}.png` captures in `D:/LocalAI/research-inbox/expansiones-fusiones-20261020/`. The top and scrolled-editor pairs are viewport captures of the internal content pane, not full-page captures. `finish-review961.txt` returns **ship** with no material fixes in this scope.

The supplied native receipt reports 169 MCP tools, a real local Piper CPU render lasting 70.019 seconds, unchanged page IDs retained and unchanged sentence audio retained after correcting the second of three synthetic scenes. The separate browser receipt reports draft export, reopening a scene, the native produce button, no browser errors and no page overflow at the two captured sizes. The author reports 49 area tests passing and the supplied one-time detector result is `[]`. This documentation pass inspected those receipts and captures; it did not rerun tests or apps. The native receipt's historical `browser_pending: true` remains intact; later browser checks are in their separate receipt.

These checks cover the synthetic interaction and render described. They do not establish LLM planning quality, universal production reliability, every concurrent state, all themes or a complete accessibility audit. Word timings in that voice run were estimated. The main profile was not used for the supplied acceptance run.

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

## Elevation & Depth

The editor is a flat shared card separated by a thin border. Source groups use native fieldset boundaries; scenes use native disclosure markers. No new shadow, decorative media, overlay or form entrance animation is introduced. Existing asynchronous job progress remains in the production header; inherited button feedback and focus rings remain available.

## Shapes

Use the shared rounded panel, field and button primitives recorded in the frontmatter. Disclosure triangles, source checkboxes and fieldset legends retain native semantics and appearance. There is no Explainers-specific card silhouette or icon system.

## Components

### Entry and creation

`ProductionsView` adds “Nuevo explicativo” beside existing new-video and new-short actions, including the empty state. It opens the existing short modal with explanatory mode selected. This mode defaults to landscape (16:9) and no music, offers optional source material and editable JSON import, and preserves topic/script entry, voice, music, captions and output-aspect controls. Script paragraphs become initial scenes. Picture-source, clip-count and variant-count controls remain available in ordinary short mode. These additions preserve earlier video/short entry points.

### Scene panel and disclosures

`ExplainerScenes` appears when the short production contains an explainer spec. Its named section uses the shared card/stack primitives. “Material y referencias” is initially collapsed and contains source title, reference/version and supplied content, plus Add source. Each scene has its own stable ID and native `details`/`summary`; scenes are initially open when there are fewer than four.

Each scene exposes title, narration, on-screen text and an image-library selector. No selected image uses “Página de texto animado”. A fieldset associates sources by ID; a source-review checkbox appears when that scene has source associations. Move up/down preserves the scene object and ID, boundary moves are disabled, and deletion is disabled for the last remaining scene. Add scene creates a fresh ID with empty editable content.

### Voice and revision state

Voice defaults to the language choice when no explicit voice is selected. An existing voice absent from the returned catalogue remains visible as “Voz actual”. Speed uses a numeric field with range (0.5–2) and step (0.05). Source image choices are loaded for the current project with a limit (200); this is a selector, not an image-preview component.

Local edits mark the draft dirty and retain it across incoming revisions. If a newer revision arrives while dirty, a visible alert states that the draft is retained; save submits its `base_revision`. API failures appear as alerts. Changed-source scene IDs appear in a status message. Failed catalogue/image loading is caught without a dedicated retry message in this component; the document does not imply one.

### Save, produce and export

“Guardar escenas” is the primary action and is disabled when clean or busy. “Guardar y producir” saves with `run: true` and is disabled while busy or a production run is live. Unchanged voice settings are omitted from the edit request. Successful save takes the returned explainer revision, clears dirty state and refreshes production/jobs.

Editable JSON export uses the existing production endpoint. A dirty draft changes the label to “Guardar y exportar proyecto”; the click first saves, then downloads only after save succeeds. A clean draft downloads directly. SRT is offered when both narration and report are done. The reuse promise is supported by the supplied native correction receipt; it should continue to be checked with retained asset IDs rather than inferred from the visual layout.

**The Scene Identity Rule.** Preserve scene/source IDs while editing and moving existing content; use returned revisions and retained assets as the evidence for selective reproduction.

### Shared controls and accessibility

Primary and secondary buttons retain hover, active, disabled and visible-focus states. Fields use the shared surface, strong border and magenta focus ring. Labels wrap their controls; source associations use fieldset/legend, disclosures use native summary, and errors/status use their corresponding roles. These observed semantics support the local review without claiming a full accessibility certification.

## Do's and Don'ts

### Do:

- Do keep production state ahead of the scene editor and preserve existing short/video entry points.
- Do retain the shared theme roles, native disclosures, labelled fields and wrapping action rows.
- Do preserve local edits when a newer revision appears and show the conflict warning.
- Do save a dirty draft before exporting its editable project.
- Do verify selective reproduction with unchanged page and sentence-audio asset identities.

### Don't:

- Don't promote this surface composition into a global Studio template.
- Don't add form entrance motion or a replacement visual identity to this scoped editor.
- Don't describe the synthetic render as proof of LLM planning, scientific accuracy or universal reliability.
- Don't treat these viewport captures or the scoped ship disposition as closure of inherited Studio comp gates.
