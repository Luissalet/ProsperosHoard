# Windows distribution

The wheel contains both built native interfaces, the common shell/fonts/icon, workflow and SDK data resources, and the product-owned motion/editing workers with their licences. It is tagged `py3-none-win_amd64`: Python code has no CPython ABI extension, but the included PE workers require Windows x64. Linux/macOS need native builds; this wheel does not advertise those platforms.

From the isolated source environment, build both interfaces and then the wheel:

```powershell
npm --prefix frontend run build
npm --prefix editing_frontend run build
.venv/Scripts/python.exe -m pip wheel --no-deps --wheel-dir dist/cas3 .
```

The workers must already exist in native/editing/bin; normal wheel building never invokes Rust, downloads engines or falls back to another repository. `native/editing/build.py` is the separate native build step when required. The wheel builder rejects absent/tiny HTML build fixtures and non-x64 PE inputs, uses a fresh normal build directory, and records SHA256/size for installed owned resources. Built UI/native resources are included; environments, data, caches and native build trees are excluded. No symlink/junction or old app runtime is used.

Install with pip in the selected product environment; ordinary installation resolves the pinned Python dependencies. The packaged console entrypoints are `prosperos-hoard` and `prosperos-hoard-mcp`. For existing data choose the root explicitly:

```powershell
prosperos-hoard --data-dir "C:/Users/luism/Desktop/Proyectos independientes/Prospero's Hoard/data" --no-browser
```

Installed code/resources do not contain user data or secrets. Without `--data-dir`/PROSPERO_DATA_DIR, an installed Windows launcher uses `%LOCALAPPDATA%/ProsperosHoard/data`. With an explicit existing root it loads optional `.env` from that root's parent; real environment values still win. Development-tree defaults are unchanged. The installed default does not relocate an existing profile: migration934 must set the intended existing root and retain tokens/configuration explicitly. Do not move `.env` into the distribution.

To verify an installation without using the source import tree, install into a fresh target and run the verifier from another directory with Python `-I`:

```powershell
.venv/Scripts/python.exe -m pip install --no-deps --target dist/wheel-smoke-cas3-assets dist/cas3/prosperos_hoard-0.1.0-py3-none-win_amd64.whl
# From a directory outside the source checkout:
D:/LocalAI/worktrees/prospero-fisiones929/.venv/Scripts/python.exe -I D:/LocalAI/worktrees/prospero-fisiones929/scripts/verify_distribution.py --installed D:/LocalAI/worktrees/prospero-fisiones929/dist/wheel-smoke-cas3-assets --wheel D:/LocalAI/worktrees/prospero-fisiones929/dist/cas3/prosperos_hoard-0.1.0-py3-none-win_amd64.whl --receipt <fresh-receipt.json>
```

This verification uses the worktree's own Python dependency environment and imports product code only from the installed target. It checks cold API/MCP discovery, installed launcher paths, both native module lifetimes and referenced UI assets, embedded resource hashes, the two real headless native tool catalogues and a saved/reopened editable motion composition. Family inference/events and media-tool discovery are inert; no model, render, GPU, user data, server listener or browser is used. Synthetic QA data is retained under the target's parent for review. Existing broad-suite failures remain separate evidence; this is not a claim that all historical tests now pass.
