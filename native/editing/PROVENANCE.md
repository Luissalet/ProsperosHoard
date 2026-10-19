# Incorporated native source

These are detached source copies maintained as part of Lumiere. No Git remotes, submodules,
source Git dependencies, release downloads or third-party executables are used by product execution.
`motion`, `edit`, and `codecbase` are local Rust workspaces. Standard crates.io library dependencies
are pinned in the two application Cargo.lock files. `native/build.py` compiles Lumiere's private
workers and places their binaries in `native/bin`; the runtime uses those product-owned files only.
Their private line protocol is `lumiere-worker/1`, not a connection to another application's MCP server.

Historical starting points (source research on 2026-10-13):

| Local tree | Historical source | Revision | Terms |
| --- | --- | --- | --- |
| motion | [storytold/effectcraft](https://github.com/storytold/effectcraft/tree/e6c817bb3f382b7fd71307baba9bb3427ce77fd9) | e6c817bb3f382b7fd71307baba9bb3427ce77fd9, v0.3.1 | MIT OR Apache-2.0 |
| edit | [storytold/filmcraft](https://github.com/storytold/filmcraft/tree/61ab6f5c7dfc37097b8a28aac235a3e31a84a6ae) | 61ab6f5c7dfc37097b8a28aac235a3e31a84a6ae, v0.2.1 | MIT OR Apache-2.0 |
| codecbase | same media codebase | 2c5ba7a72619935f0481f99d59409e0d1844b234 | MIT OR Apache-2.0 |

The older codec source revision is included separately because the compositor's original media API
used it. It is an owned local source dependency, not a future compatibility obligation or network
dependency. The editing engine carries its own newer codec implementation. Future consolidation can
follow Lumiere's requirements without referring to either historical repository.

Unmodified LICENSE-MIT and LICENSE-APACHE notices are retained in each tree, including original
copyright attribution. Bundled fonts retain their original license files in assets/fonts.
The project and module identifiers are Lumiere's own. Historical project imports keep the original
file and save a separate `.lumotion` or `.lumedit` copy; later edits use the own formats. This is
a data-preservation import, not a promise to track changes in the old applications.

Original desktop launchers, upstream agent instructions, Git state and update infrastructure were
not incorporated. The full core crates are present, including rendering, edit commands, project
models, codecs, keyframes, masks, expressions, scripts and interchange. Optional desktop control
code remains internal source where core dependencies reference it; the shipped workers instantiate
only their own in-process sessions and expose headless operations. No external desktop UI is driven.
Capabilities requiring downloaded modality models retain their separate model requirements; copying
source does not prove universal parity or create those model files.
