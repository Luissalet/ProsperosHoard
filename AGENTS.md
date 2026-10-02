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
6. **Escrituras atómicas con `util.replace_with_retry`** (o
   `util.write_text_atomic`), nunca `Path.replace`/`os.replace` a pelo: en
   Windows el reemplazo falla (WinError 5/32) mientras otro lector tiene el
   archivo abierto.
7. **Procesos hijos siempre con `procutil`** (CREATE_NO_WINDOW en Windows,
   UTF-8). Nada de `multiprocessing`. `pathlib` y `encoding="utf-8"` siempre.
8. **ffmpeg**: los filtros reciben nombres relativos con `cwd` en la carpeta de
   trabajo (la carpeta de Windows se llama «Prospero's Hoard»); la letra pasa
   por `video.ass_escape`.
9. **Antes de dar un cambio por bueno**: `pytest -q` en verde,
   `npm run build` sin errores de TypeScript y, si tocas la interfaz,
   `python3 scripts/screenshots.py` para revisar las capturas.
10. **Commits**: identidad `Luissalet <luissalet@users.noreply.github.com>`,
   mensajes en inglés con prefijo convencional, sin nombres de otros productos
   ni datos personales.

## Para el asistente que dirige el estudio (MCP)

Qué hacer cuando el usuario pide, con sus palabras:

- **«ComfyUI no está» / «no encuentra el backend» / "start ComfyUI"**:
  `studio_services`; si el servidor que falta está `down` y `startable`,
  `studio_service_start(id)` (espera a que responda) y repite lo que fallaba.
  Prospero no necesita a Faustus. Si está `unavailable`, di el `problem`
  (no instalado, falta su Python) en vez de reintentar. Solo se paran con
  `studio_service_stop` los que arrancó la familia Hoard.
- **«Hazme un videoclip de X»**: pregunta el idioma de la letra si no lo
  dice; `studio_cast(action="list")` para el protagonista; `studio_video_plan`
  y enséñale el borrador (planos y letra) antes de `studio_video_from_plan`.
  Para los clips, si un LLM ocupa las GPU, díselo (`studio_gpu_memory`).
- **«Borra eso» / «quita los malos»**: `studio_delete_assets(ids)` con los
  ids que el usuario señala (van a la papelera; `studio_trash` los recupera).
  Si alguno está en uso, di dónde y pregunta antes de `force=true`. Vaciar la
  papelera solo si lo pide.
- **«Va lentísimo» / «por qué tarda»**: `studio_gpu_memory`. Si el motor no
  cabe (Qwen-Image ~12 GB) y la GPU la ocupa un modelo de lenguaje, dilo y
  pregunta antes de pararlo: nunca pares un LLM por tu cuenta.

- **«Borra el proyecto X»**: `studio_projects` para su id y lo que tiene;
  díselo y `studio_delete_project(project)` (papelera, con sus
  producciones). Para siempre solo si lo pide:
  `studio_trash(action="empty", projects=[id])`.
- **«Hazlo todo sin pararte» / «piloto automático»**:
  `studio_production_settings(production, autopilot=true)` y
  `studio_production_continue`.
- **«Que pegue con la música» / «zoom en el bombo» / «dale un look de
  terror»**: `studio_production_finishing(production, finishing={...})`
  con `beat_fx` (`source` kick/beats/downbeats; `zoom`, `flash`, `shake`
  de 0 a 1) y el resto del look; si ya estaba renderizado, se renderiza de
  nuevo solo el montaje. Enséñale el resultado con `studio_show`.
- **«Hazme el Canvas de Spotify» / «un bucle para Spotify»**:
  `studio_canvas(production)` (estribillo, 8 s, sin letra) y dale el
  enlace de descarga.
- **«Móntame un espacio / un lienzo» / «hazlo con nodos» / «conecta las
  fichas al plano»**: `studio_spaces(action="create", template=...)`
  (`reference_film` para personaje+criatura+lugar → fotograma → clip,
  `singing_shot` para lip sync) o en blanco y `action="edit"` con `ops`
  (`add_node`, `connect`...). Ejecuta con `action="run", mode="all"` y
  sigue el job; lee `state` con `action="get"` y enséñale las salidas con
  `studio_show`. Un `cast` da `image` (su referencia) y `text` (`@Nombre`).
  Para mejorar un prompt: `studio_prompt_enhance`.
- **«Pásalo a Premiere / DaVinci»**: `studio_export_timeline(production,
  aspect)` y dale el enlace de descarga.
- **«Cambia la canción» / «usa esta canción» / "swap the song"**:
  `studio_assets(kind="audio")` en el proyecto o en otros para encontrarla y
  `studio_production_song(production, asset_id=...)`; «otra toma» →
  `take=N`; «hazla más lenta / otro estilo» → `compose={...}`. Si devuelve
  `lyrics_source: "previous"`, la letra es la de la canción anterior:
  pregúntale si encaja o pide la nueva (`studio_production_lyrics`).
- **«Pon el plano 3 en el estribillo» / «este plano con estos versos»**:
  `studio_production_timing(production)` para los versos con sus tiempos y
  `studio_production_shots` con `{"key": "3", "span": {"start_s",
  "end_s"}}` (los tramos no se solapan; `span: null` lo libera).
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
- **«Quita a X del reparto»**: `studio_cast(action="delete", id)`; se
  recupera con `action="restore"` (`action="deleted"` los lista). Si
  responde `in_use`, di qué producción lo usa y borra con `force=true` solo
  si el usuario lo confirma.
- **«Que cante este verso» / «que mueva los labios»**: el plano tiene que
  estar fijado en sus versos (`studio_production_timing` para los tiempos,
  `studio_production_shots` con `span`); después `{"key": "3", "sing":
  true}`. Su clip se hace con Wan 2.2 S2V desde su tramo de la canción
  (unos 5 min por cada 4,8 s en una tarjeta de 16 GB) y el montaje lo pone
  en su segundo exacto. Fuera de una producción: `studio_generate_image`
  con `template="wan22_s2v"`, `reference_asset_id`, `audio_asset_id`,
  `audio_start_s` y `audio_seconds`.
- **«Que el escenario / la guitarra salga siempre igual»**: un lugar o un
  objeto es una entrada del reparto: `studio_cast(action="create",
  kind="location"|"prop", name, fields={"prompt": aspecto})`; genera su
  imagen (`studio_generate_image` con «@Nombre, wide establishing shot,
  empty» o «@Nombre, the object alone, plain background»), enséñala y, si
  le gusta, `studio_cast(action="update", id, fields={"canonical_asset_id"})`.
  Desde ahí cada `@Nombre` en un prompt (también en los planos de una
  producción) mete su imagen como referencia numerada con Qwen-Image 2.1.
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
- **«Sigue esta serie / respeta lo que decidimos en los episodios anteriores»**:
  reutiliza el mismo `project` en `studio_short_create` y añade
  `options={"use_project_memory": true, "continuity_notes": [...]}` si hay
  decisiones nuevas. El guion indica los slugs consultados en `memory_sources`.
- **«Busca b-roll de X»**: `studio_stock_search(query en inglés, aspect, project,
  take=N)`; cuenta de quién es cada clip (`author`).
