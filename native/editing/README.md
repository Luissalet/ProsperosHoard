# Lumiere native engines

The product includes its own compositor and editor code. The compositor supports layered compositions,
keyframes, masks, effects, expressions, script commands, editable project files, frame previews and
native video export. The editor supports media ingestion, sequences, timeline edits, effects, graphics,
captions, interchange, inspection, previews and export. Python exposes these through the ordinary
creative API and MCP tools; the web composition controls use the same service.

Saved projects can be listed and reopened in the web workshop or through creative_list
and creative_get. The seven creative operations are part of Lumiere's64 public MCP tools.
Motion exports and refreshed previews retain the captured source snapshot and SHA-256;
edits made while rendering stay saved, and older artifact sources are shown explicitly.

Build with Rust 1.95.0 and Python:

```text
python native/build.py
```

The build uses the source in this repository and pinned Rust library dependencies. It creates
`native/bin/lumiere-compositor` and `native/bin/lumiere-editor` (`.exe` on Windows) and a hash receipt.
No configuration pointing to another application's executable is read. Standard build output and
owned binaries are ignored by Git; source, locks, assets and licenses are versioned.

The runtime keys are `motion` and `edit`; project files use `.lumotion` and `.lumedit`.
Motion exposes 21 headless operations; editing exposes nine. Each operation exposes the linked
engine's full commands or document controls. Legacy source adapters previously listed unavailable
desktop UI operations; those external windows are not a product dependency.

Existing imported projects are migrated as separate files when first read. The old source file,
copied media and project identity are preserved. No ongoing upstream compatibility is promised.
Selection and the in-memory undo stack remain local to one command batch; editable saved content
persists across calls. HTML animation is a separate own workflow documented in `docs/HTML_MOTION.md`.

See [PROVENANCE.md](PROVENANCE.md) for detached-copy revisions and licenses. Native tests exercise
editable keyframes, actual PNG and H.264 output, source-copy preservation and both engine catalogues.
