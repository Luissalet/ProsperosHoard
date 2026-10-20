---
name: "Prospero — Image composition"
description: "Editable spatial sources inside the existing plum production workbench."
colors:
  action: "var(--accent)"
  action-hover: "var(--accent-2)"
  action-ink: "var(--accent-ink)"
  action-soft: "var(--accent-soft)"
  panel: "var(--surface)"
  hover: "var(--surface-2)"
  selected-mode: "var(--surface-3)"
  divider: "var(--border)"
  control-border: "var(--border-strong)"
  text: "var(--text)"
  label: "var(--text-2)"
  canvas: "#fff"
  box-fill: "#e4edf3"
  box-ink: "#172c3e"
  box-border: "#798b98"
  reference-label: "#fffffff0"
  empty-ink: "#435667"
typography:
  heading:
    fontFamily: "Cinzel, Iowan Old Style, Charter, Georgia, Liberation Serif, Noto Serif, Times New Roman, serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "0.005em"
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
  box:
    fontFamily: "Source Sans 3, sans-serif"
    fontSize: "14px"
    lineHeight: 1.25
rounded:
  box: "0"
  control: "6px"
  mode: "7px"
  field: "9px"
spacing:
  label: "5px"
  box: "8px"
  row: "10px"
  stack: "12px"
  toolbar-block: "18px"
  workbench: "24px"
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
  mode-selected:
    backgroundColor: "{colors.selected-mode}"
    textColor: "{colors.text}"
    rounded: "{rounded.mode}"
    padding: "5px 11px"
  spatial-box:
    backgroundColor: "{colors.box-fill}"
    textColor: "{colors.box-ink}"
    rounded: "{rounded.box}"
    padding: "8px"
  artboard:
    backgroundColor: "{colors.canvas}"
    width: "100%"
---

# Design System: Prospero — Image composition

## Overview

**Creative North Star: "Place content, retain its source, then generate and inspect it."**

The built surface is Generation → Design → Image composition, an **Operate**
extension of the existing Prospero workbench. A saved-source selector and
Guardar copia precede the editable canvas. The inspector edits scene intent,
literal text, pixel geometry and retained image references. Plantillas remains
the adjacent mode and the initial Designer choice.

The visual authority is the incumbent plum application with magenta actions
and selection, shared fields and compact buttons. The white canvas is the
artifact being composed. This scoped record was completed after implementation
and the independent visual finish review; the Direction contract below was
itself written after the first build. Neither is evidence of an earlier comp
or workshop. The north-star wording comes directly from that existing contract.

**Key Characteristics:**

- Source selection and save remain above the editable artifact.
- Rectangular boxes distinguish requested placement from a generated image.
- Parallel desktop inspection becomes sequential mobile inspection.
- Compiled instructions, export, preview and generation follow the work area.

Source inspected: `frontend/src/views/Spatial.tsx`, `spatial.css`, `Designer.tsx`,
`frontend/src/api.ts`, `components/FaustusCopilot.tsx`, `styles.css`, `studio.css`,
`hoard-theme.css` and `App.tsx`; functional boundaries are recorded in
`docs/SPATIAL.md`. `PRODUCT.md` and root `DESIGN.md` remain historical context.
Current CSS declares Source Sans 3 for the working sans and Cinzel for headings;
the older root description of Space Grotesk is not current surface evidence.
This pass does not repair root metadata or close inherited Studio comp gates.

The documenter opened all five captures in
`D:/LocalAI/research-inbox/spatial953/`: `spatial953-desktop.png`,
`spatial953-mobile.png`, `spatial953-mobile-inspector.png`,
`spatial953-desktop-actions.png` and `spatial953-mobile-actions.png`.
Desktop viewports are 1440×1000; mobile viewports are 390×844. The inspector and
action captures show named scrolled regions of the internal content pane,
not full-page coverage. `finish-review953.txt` returns **ship**, with no material
fixes within these five captured views. It explicitly excludes FaustusCopilot,
functional execution, model quality and whole-product certification. Empty,
loading, error and focus states were not part of that captured sample.

The supplied `qa-native-results.json` records retained sources, an edited copy,
compilation, schematic CPU preview and a generated recipe retaining the source
ID. The parent reports 170 native MCP tools. `qa-browser-results.json` reports
keyboard movement, drag/undo, export/reopen, a context snapshot, no browser errors
and no desktop/mobile overflow. Generation used the native demo backend solely
to verify transport. Supplied logs report eight spatial tests, then 40 area tests
passing and frontend build passing; the one-time detector result is `[]`.
This documentation pass inspected source, receipts, logs and captures; it ran
no apps, models, live chat or tests. There is no real-model adherence or artistic
quality result, nor a live Faustus conversation check. No new shipping raster
assets were added; QA screenshots are evidence outside the packaged application.

Scope: Designer → Image composition. Operate mode. Extension of the existing
plum workbench; no replacement world, comp round or unrelated metadata repair.
Luis requested editable boxes/text/references before image generation, alongside
the diagram editor in Vitruvius. Pixel composition is not a semantic diagram.

## Direction contract

THESIS: Place content, retain its source, then generate and inspect it.

OWN-WORLD: Existing plum surfaces, magenta actions, shared fields and buttons;
the white image canvas is the working artifact.

STORY: Open a saved source or add objects/text, move boxes, edit exact words,
save an immutable copy and compile or generate from that source.

FIRST VIEWPORT: Source controls above the canvas, inspector alongside on desktop
and below on mobile; save is visible above the working area.

FORM: Existing workbench extension; code-led, no new visual-world seed or comp.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

Verification distinguishes schematic PNG previews, synthetic demo generation
transport and unmeasured real model adherence. No new shipping raster assets.
Context was loaded once for this project at task961; its root metadata warnings
remain outside scope. This scoped record was written after the first build and
is the contract passed to the independent finish review, not an earlier comp.

## Colors

The frontmatter binds application controls to current theme variables. Shared
primary actions and selected-box outlines use the magenta accent. The canvas
keeps its literal white background; default boxes use pale blue-grey fill,
dark ink and a muted blue-grey border. These artifact colors remain distinct
from the surrounding application theme. Box fill and text color can be edited;
a reference image receives a nearly opaque white label backing for legibility.
The five captures show the dark application theme. Light-mode control behavior
is inherited from source, without a newly reviewed light-theme capture.

**The Artifact Ground Rule.** Preserve the white composition canvas and use the
shared theme accent for application actions, focus and box selection.

## Typography

The page heading uses the inherited serif stack through the shared page-head
rule. Body, labels, canvas text and controls inherit the current Source Sans 3
working type. Canvas text has the tighter line-height in the frontmatter,
preserves explicit line breaks and wraps long tokens within its box. Numeric
fields use tabular numerals. The surface adds no display font or new type scale.
Literal canvas text is editable content, not typographic proof of what an image
engine will reproduce.

## Layout

The inherited shell occupies the dynamic viewport and scrolls inside its content
pane. The editor follows the mode selector with a local top margin from the
workbench spacing token. Source controls, save, add-object/text and history
controls precede the workbench. Toolbar rows wrap with the row gap; the source
field grows and retains its 180px minimum. The actual canvas aspect ratio follows
document width and height rather than a fixed screenshot ratio.

Above 900px, a flexible canvas sits beside a 280px inspector, separated by the
workbench gap. At 900px and below, the workbench becomes one column, with the
inspector after the canvas and an additional 8px inspector top margin. Position,
size and color pairs keep two columns with the row gap. Shared zero intrinsic
minimum widths and bounded inputs preserve the narrow panel. The inherited
shell switches to its mobile Tools trigger at 700px.

Below both workbench columns come the compile/MOSS/JSON/import/PNG/generation
actions, saved-source ID and read-only compiled output. These are reached through
internal scroll. Mobile wraps the downstream actions over several rows; source
IDs and instructions wrap inside the panel. The reviewed top view intentionally
does not imply the inspector and output all fit in the first mobile viewport.

## Elevation & Depth

The canvas is flat and isolated, clips content to its bounds and introduces no
shadow. Boxes use a thin border, with a magenta inset outline for selection and
visible keyboard focus (3px, offset −3px). Shared fields retain their soft focus
ring and magenta border; the transparent import control gains a visible
focus-within outline. The editor adds no entrance animation. Shared buttons
retain brief background/border feedback and a one-pixel active translation.

## Shapes

The artifact uses square-cornered rectangular boxes; the application keeps its
rounded controls, fields and segmented mode selector from the frontmatter.
The outer editor fieldset is borderless and has no padding. The inspector is a
vertical stack rather than an added card. References cover the item rectangle
and clip with it; this is composition editing rather than the shared full-image
inspection view, whose separate contain behavior remains unchanged.

## Components

### Mode and source controls

The shared segmented control keeps Plantillas and Composición de imagen
adjacent. Nueva resets to a blank 1024×1024 document. The saved-source select
opens a retained source; Más fuentes appears when pagination is available.
Guardar copia creates a new immutable source, with parent identity retained by
the API. The visible saved-source ID identifies that copy. The project-specific
session draft survives switching Designer modes; it is not a durable saved asset.

### Canvas and selected box

Object and text items are native buttons with pressed-state selection and
descriptive text. Their percentage geometry maps the pixel document into the
responsive canvas. Pointer drag scales movement to document coordinates;
keyboard arrows move by one pixel and Shift+arrow by ten, clamped to the canvas.
Selection is also available from the inspector's element selector. Shared local
undo/redo includes document edits and completed drags. Empty documents display
an invitation to add an object or text; this state is source-inspected only.

### Inspector and references

Labels wrap the name, general description, canvas dimensions, element selection,
caption, exact text, geometry and preview-color controls. Exact text appears for
text items. The shared image picker attaches a retained reference; a separate
button removes it. Delete removes the selected item. Resizing the canvas retains
existing coordinates; boxes that no longer fit must be corrected before save.
Preview fill is schematic styling, while text color also becomes a color request
in compiled instructions. Reference files remain retained references rather than
media embedded into JSON.

### Downstream actions and request states

Ver instrucciones and Formato MOSS compile the document into the labeled
read-only output. JSON exports the editable source; Importar JSON parses and
validates through the caption compiler before replacing the draft. Guardar vista
previa PNG first saves, then opens a retained schematic preview in the existing
asset viewer. Generar con Qwen first saves, then enqueues the existing qwen21
route with that source ID and reports the compiled final prompt. Generation is
disabled for an empty item list; a pending request disables the editor fieldset.
Working status uses `role=status`, API/list errors use `role=alert`, and save/queue
confirmation uses the existing toast. These source behaviors do not expand the
finish review's captured-state coverage.

### Faustus context

The editor emits the draft, selected item and saved-source ID to the shared
Faustus context provider, clearing it on unmount. A saved composition supplies
the active asset; the selection snapshot contains the current document. The
supplied browser receipt checks this snapshot. The panel itself and live model
conversation were outside the visual finish review and were not exercised here.

**The Retained Source Rule.** Keep editable source identity separate from the
schematic preview and generated image; inspect the engine result before claiming
placement or spelling adherence.

## Do's and Don'ts

### Do:

- Do preserve the source selector and save action before the canvas.
- Do retain the white artifact, shared plum controls and magenta selection.
- Do keep the desktop inspector alongside and mobile inspector after the canvas.
- Do retain labeled exact text, numeric geometry and wrapping downstream actions.
- Do distinguish retained source, schematic preview and generated result in evidence.

### Don't:

- Don't replace Plantillas or introduce a new visual world for this extension.
- Don't treat a session draft as a durable saved composition.
- Don't equate successful compilation or demo transport with real-model adherence.
- Don't describe viewport captures or scoped ship as whole-product or old-comp approval.
- Don't package QA screenshots as application imagery or imply new shipping raster assets.
