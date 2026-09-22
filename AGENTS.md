# AGENTS.md

Reglas para agentes de código que trabajen en este repositorio.

1. **Nada de FastAPI fuera de `api.py`.** `engine.py`, `store.py`, `db.py`,
   `comfy_driver.py`, `design.py`, `audio.py`, `video.py`, `timeline.py`,
   `voices.py` y `backend.py` son lógica pura y se prueban sin servidor.
2. **`mcp_server.py` es un script independiente**: solo stdlib, `httpx` y
   `mcp`. Nunca `from . import ...`. Se lanza por ruta absoluta.
3. **No edites `prosperos_hoard/hoard_link/*`** (copia exacta, ver
   `VENDORED.txt`). Si falta algo, envuélvelo en `backend.py`.
4. **Cada operación visible tiene su ruta `/api/agent/<tool>` y su
   herramienta MCP** con el mismo nombre; las respuestas de agente son
   compactas (ids primero, sin rutas de archivo ni listas enormes) y los
   docstrings llevan una línea `Keywords:` en inglés y español.
5. **Los archivos se sirven por id**, nunca por una ruta del cliente. Las
   importaciones por ruta pasan por `engine.resolve_import_path`.
6. **Procesos hijos siempre con `procutil`** (CREATE_NO_WINDOW en Windows,
   UTF-8). Nada de `multiprocessing`. `pathlib` y `encoding="utf-8"` siempre.
7. **ffmpeg**: los filtros reciben nombres relativos con `cwd` en la carpeta de
   trabajo (la carpeta de Windows se llama «Prospero's Hoard»); la letra pasa
   por `video.ass_escape`.
8. **Antes de dar un cambio por bueno**: `pytest -q` en verde,
   `npm run build` sin errores de TypeScript y, si tocas la interfaz,
   `python3 scripts/screenshots.py` para revisar las capturas.
9. **Commits**: identidad `Luissalet <luissalet@users.noreply.github.com>`,
   mensajes en inglés con prefijo convencional, sin nombres de otros productos
   ni datos personales.
