# AGENTS.md

Reglas para agentes de código que trabajen en este repositorio.

1. **Nada de FastAPI fuera de `api.py`.** `engine.py`, `store.py`, `db.py`,
   `comfy_driver.py`, `design.py`, `audio.py`, `video.py`, `timeline.py`,
   `voices.py`, `backend.py`, `shorts.py`, `stock.py` y `soundtrack.py` son
   lógica pura y se prueban sin servidor (lo externo de un short pasa por el
   `Studio`; en los tests, `app.state.short_hooks`).
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
- **«Que el personaje salga siempre igual» / "keep X consistent"**: mira su
  kit (`studio_character_adapters(action="list")`). Sin adaptador para el
  motor del proyecto: `studio_character_sheet` (necesita canónica) →
  `studio_character_dataset(action="build", sources=[...])` → lee el
  `report` y díselo → `studio_character_train(action="plan")`. Entrenar
  ocupa una GPU mucho tiempo: enseña el plan (minutos, VRAM, entrenador) y
  pide permiso antes de `action="start"`. Si `trainer_problem` dice que no
  hay entrenador o falta el modelo base, explícalo; no inventes rutas.
- Con adaptador, no hace falta nada: cada `@Nombre` lo carga solo (mira
  `adapters` en la respuesta de `studio_generate_image`). Para posturas
  libres usa `consistent=true, prefer_adapter=true`.
- **«¿Cuál es la mejor toma?» / "which take is best"**:
  `studio_character_takes(action="score")`, luego `list` con
  `sort="identity"`. Di si la nota es del modelo de visión o `rough`.
- **«Guárdalo para otros proyectos» / "save this character"**:
  `studio_character_library(action="save")`; para llevarlo a otro proyecto,
  `action="use"`; para un fichero, `studio_character_pack(action="export")`.
- **«Hazme un short / vídeo corto / TikTok sobre X»**: `studio_short_create(topic=X,
  options={"language": ..., "duration_s": ...})`. Si el usuario quiere revisar
  el texto o el tema es delicado (datos, salud, dinero), pasa
  `settings={"script_review": true}`: se para tras el guion; enséñaselo
  (`studio_production_script(production)`), aplica sus cambios con
  `studio_production_script(production, script=...)` o sigue con
  `studio_production_continue`. Si trae su propio guion, pásalo en `script`.
  Para las imágenes, `visuals.source` «auto» usa vídeo de archivo si hay clave
  de Pexels/Pixabay; si `studio_production` dice que no hay clave y el
  usuario quiere metraje real, dile que la añada en Ajustes. Al terminar,
  enséñale el render (`studio_show`) y dale el `publish` tal cual (título,
  descripción, hashtags y créditos): los créditos del metraje no se quitan.
- **«Hazme tres versiones»**: `count=3` en `studio_short_create`.
- **«Busca b-roll de X»**: `studio_stock_search(query en inglés, aspect, project,
  take=N)`; cuenta de quién es cada clip (`author`).
