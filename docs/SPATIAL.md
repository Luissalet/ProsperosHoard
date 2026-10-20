# Editable image composition

Generation → Design → Image composition retains objects, exact text, reference
images and pixel boxes before rendering. Templates remain available in the
adjacent mode. The same operation is exposed by `/generation/api/spatial` and
the native `studio_spatial` MCP tool. Discovery remains cold and modules OFF
until explicitly enabled.

Sources are ordinary `layout` assets. Saving creates an immutable copy; it does
not rewrite the previous source. `parent_id` names the source edited to create
the copy. Concurrent saves can therefore form branches without overwriting
each other. Open an exact asset id to recover its source; JSON export is editable.
External reference-image files remain references, rather than embedded media.

```json
{
  "format": "prospero.composition/1",
  "width": 1024,
  "height": 768,
  "caption": "Una comparación sobre fondo claro",
  "items": [
    {"id": "title", "kind": "text", "caption": "Título azul",
     "text": "¿Qué cambió? 25 €", "box": [96, 32, 832, 128]},
    {"id": "left", "kind": "object", "caption": "Un cuenco rojo a la izquierda",
     "box": [64, 256, 320, 320]}
  ]
}
```

Boxes use `[x, y, width, height]` pixels from the top left, must have positive
size and fit the canvas. Preserve each item id when moving or changing it.
Optional `reference_asset_id` points to a retained image. `fill` controls the
schematic preview background; `colour` controls its text and adds a colour
request to the compiled prompt. The caption remains the description of the
requested scene; the layout is not an image-model constraint solver.

The editor supports adding, selecting, dragging and deleting items, keyboard
arrows (Shift for ten pixels), position/size fields, local undo/redo and a
session draft per project. A canvas resize retains object coordinates; fix
boxes that no longer fit before saving. Switching modes preserves the draft.
Saved-source listing exposes normal pagination (`next_offset`).

`studio_spatial` actions:

- `save(project, document, name, parent_id?)`: retain a new immutable source.
- `get(asset_id)`: full source, project and parent id.
- `list(project, offset=0)`: sources, `has_more` and `next_offset`.
- `compile(document or asset_id, target="caption"|"moss")`: source to prompt,
  dimensions, ordered image references and notes; no model is called.
- `preview(asset_id)`: retain a schematic PNG linked to the saved source.

The shared Faustus panel receives the current draft, selected item and saved
source id. The assistant can read and save through the same tool; opening a
new copy is explicit. This surface does not itself invoke a planning model.

`studio_generate_image(composition_asset_id=..., prompt="", engine="qwen21")`
uses the source dimensions, appends the compiled composition to the prompt and
passes its ordered references through the existing image-generation route.
The job and resulting image recipe retain the exact source asset id. Inspect
the generated picture: placement and exact spelling can still fail. The
existing engines' dimensions and reference limits still apply.

MOSS export uses integer centered `yxyx` coordinates (`abs_centralized`) with
the same generation dimensions. It does not contain reference-image
conditioning or install an engine. MOSS code/model availability and actual
image-model quality are separate from successful source compilation. Krea
access and licensing are not inferred from public screenshots.

The schematic PNG draws labels and optional retained reference images. It is
not a generated illustration, semantic graph, physical simulation or alpha
guarantee. Use Vitruvius for diagrams with relations and editable graph meaning.

## Provenance and verification

Original native implementation. The exchange convention follows OpenMOSS's
Apache-2.0 documentation at `26cfa3224177e2bb75d24d8c1caa20fdb1602aad`;
no upstream CLI, skill folder or runtime is executed by the product.

Tests cover source immutability, preserved objects, centered coordinates on a
rectangular canvas, Spanish text, invalid geometry, actual reference pixels,
pagination and generated-recipe provenance. API/MCP and desktop/mobile editor
acceptance use isolated profiles. A native demo backend verifies generation
transport, not model adherence. Real-model comparisons are recorded separately
in `D:/LocalAI/research-inbox/spatial953`; no quality conclusion follows from
the demo image or from compiler success.
