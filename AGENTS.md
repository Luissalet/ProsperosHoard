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

## Para el asistente que dirige el estudio (MCP)

Qué hacer cuando el usuario pide, con sus palabras:

- **«Recrea esto con X» / "remake this with X"**: la producción de origen es
  la que acaba de terminar o la que nombre (`studio_productions`). Si aún no
  es receta, `studio_recipe_export(production)`; lee sus `warnings` (prompts
  que describen objetos del protagonista anterior) y díselos. Busca a X con
  `studio_cast(action="list")` en sus proyectos: si existe, `cast={"lead":
  "<char_id>"}`; si no, pide o redacta un aspecto de un párrafo y usa
  `cast={"lead": {"name": "X", "look": "..."}}`. Luego
  `studio_recipe_run(recipe, cast, options={"title": ...})` y sigue el
  progreso con `studio_production(slug)`. Por defecto reutiliza la canción y
  los planos sin protagonista; di qué se reutilizó (`notes`).
- Una producción en `failed` o `cancelled` se reanuda con
  `studio_production_continue(production)`: no se rehace lo terminado.
- Nunca describas un fotograma sin mirarlo con `studio_show`.
- **«Revisa la producción» / "check the production"**:
  `studio_qa_run(production)` (solo informa, `dry_run=true`). Resume los
  fallos con su motivo; si el usuario quiere que se arreglen,
  `studio_qa_run(production, stage, dry_run=false)`: regenera con otra
  semilla y un arreglo concreto, hasta el límite de reintentos, y vuelve a
  encolar la producción. Sin modelo de visión la hoja dice «no vision model»:
  dilo, no lo ocultes.
- **«¿Por qué el clip 11 está mal?»**: `studio_qa_report(production)`; si no
  hay revisión o no incluye ese plano, `studio_qa_run(production,
  stage="clips", keys=["11"])`. Explica el `why` (salto de exposición en el
  segundo X, se mueve cuando debía estar quieto, nota baja contra la
  biblia…) y ofrece `dry_run=false` o `studio_production_shots` para
  cambiarlo.
- **«Enséñame el animático antes de renderizar»**: las producciones nuevas
  ya lo hacen solas (`settings.animatic`, activo por defecto) y se paran en
  `awaiting_review`; si no existe o hay que rehacerlo,
  `studio_animatic(production)`. Muéstralo (`studio_show` del render da tres
  fotogramas) y resume el plan: cortes, planos sin usar, cuántos clips de
  Wan quedan y los minutos de GPU estimados. Después, o
  `studio_production_shots` para cambiar planos (se rehace el animático y
  vuelve a parar), o `studio_production_continue` para renderizar los clips.
  No continúes sin que el usuario lo apruebe.
