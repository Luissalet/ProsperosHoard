# AGENTS.md

Reglas para agentes de código que trabajen en este repositorio.

1. **No hay imports de FastAPI en `engine.py`, `store.py`, `db.py`,
   `comfy_driver.py`, `design.py`, `audio.py`, `video.py`, `timeline.py`
   ni `voices.py`.** Toda lógica HTTP vive en `api.py`.
2. **`mcp_server.py` es un script independiente**: solo puede importar
   stdlib, `httpx` y `mcp`. Nunca `from . import ...`. Se lanza por ruta
   absoluta, no con `-m`.
3. **Nunca edites `prosperos_hoard/hoard_link/*`** (vendorizado). Si algo
   falta, envuélvelo en `backend.py`.
4. **Cada operación visible para el usuario necesita un endpoint
   `/api/agent/<tool>` y una tool MCP correspondiente**, con el mismo
   nombre y forma de retorno.
5. **Toda ruta que sirve un archivo lo hace resolviendo por id en la
   base de datos**, nunca aceptando una ruta de archivo del cliente.
6. **Commits**: identidad `Luissalet <luissalet@users.noreply.github.com>`,
   mensajes en inglés, nunca nombres de productos de la competencia ni
   datos personales.
7. **Antes de dar por terminado un cambio**: `pytest -q` en verde y, si
   tocaste `video.py` o `audio.py`, confirma que sigue pasando el test
   con ffmpeg real (`test_video.py::test_real_render_end_to_end`).
