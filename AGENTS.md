# AGENTS.md

Reglas para agentes de código que trabajen en este repositorio.

1. **Nada de FastAPI fuera de `api.py` y `family_api.py`** (este último
   solo monta las rutas de la familia: catálogo y llamada comunes, las cuatro
   herramientas `production_export_lumiere`, `cast_import_character`,
   `production_from_storyboard` y `voice_tts`, y los ajustes; su lógica está en
   `family_tools.py`, `family_settings.py`, `jobevents.py` y `gpu_lease.py`,
   sin FastAPI; `PROSPERO_GPU_LEASE=0` apaga la reserva de GPU). `engine.py`, `store.py`, `db.py`,
   `comfy_driver.py`, `design.py`, `audio.py`, `video.py`, `timeline.py`,
   `voices.py`, `backend.py`, `shorts.py`, `stock.py` y `soundtrack.py` son
   lógica pura y se prueban sin servidor (lo externo de un short pasa por el
   `Studio`; en los tests, `app.state.short_hooks`).
2. **`mcp_server.py` es un script independiente**: solo stdlib, `httpx` y
   `mcp`. Nunca `from . import ...`. Se lanza por ruta absoluta.
3. **Lo que ya hace la librería común no se reescribe aquí**: descargas de
   enlaces (`fam_media.download`), voz a texto (`fam_media.transcribe`),
   guardia de Host/Origin (`hoard_link.guard`), avisos (`fam_notify.Router`),
   comprobación SSRF (`hoard_link.web.safety`), espera máxima de 150 s
   (`hoard_link.waiting`).
   **No edites `prosperos_hoard/hoard_link/*`** (copia exacta, ver
   `VENDORED.txt`). Si falta algo, envuélvelo en `backend.py`.
4. **Cada operación visible tiene su ruta `/api/agent/<tool>` y su
   herramienta MCP** con el mismo nombre; las respuestas de agente son
   compactas (ids primero, sin rutas de archivo ni listas enormes) y los
   docstrings llevan una línea `Keywords:` en inglés y español.
5. **Los archivos se sirven por id**, nunca por una ruta del cliente. Las
   importaciones por ruta pasan por `engine.resolve_import_path`.
6. **Escrituras atómicas con `util.write_text_atomic`** (o
   `util.replace_with_retry`; ambos son `hoard_link.atomic`, `util` solo
   los reexporta), nunca `Path.write_text`/`Path.replace`/`os.replace` a pelo
   sobre un archivo de estado o de ajustes (`backend.json`, `state.json`,
   recetas): en Windows el reemplazo falla (WinError 5/32) mientras otro lector
   tiene el archivo abierto.
7. **Procesos hijos siempre con `procutil`** (envuelve `hoard_link.proc`:
   CREATE_NO_WINDOW en Windows, UTF-8, un tiempo agotado o una cancelación
   matan el árbol; para cancelar usa `hoard_link.proc.kill_tree`, no
   `proc.kill()`). ffmpeg, ffprobe y yt-dlp se buscan con
   `hoard_link.media.bins` (`backend.ffmpeg_path()` es un atajo). Nada de
   `multiprocessing`. `pathlib` y `encoding="utf-8"` siempre.
8. **ffmpeg**: los filtros reciben nombres relativos con `cwd` en la carpeta de
   trabajo (la carpeta de Windows se llama «Prospero's Hoard»); la letra pasa
   por `video.ass_escape` (que es `hoard_link.media.subs.ass_escape`).
   SRT/VTT/TXT/LRC también son `hoard_link.media.subs`; `ids.py` es
   `hoard_link.ids`.
9. **Antes de dar un cambio por bueno**: `pytest -q` en verde,
   `npm run build` sin errores de TypeScript y, si tocas la interfaz,
   `python3 scripts/screenshots.py` para revisar las capturas.
10. **Commits**: identidad `Luissalet <luissalet@users.noreply.github.com>`,
   mensajes en inglés con prefijo convencional, sin nombres de otros productos
   ni datos personales.

## Para el asistente que dirige el estudio (MCP)

- **Guion o letra opcionales por tomas**: `studio_production_segments` importa
  texto o guarda la lista completa con enlaces y tiempos. No inicia generación.
  No confundirlo con `studio_production_script` de los shorts narrados. Mantén
  vacíos los tiempos no dados, conserva los finales SRT/VTT y no retimes tomas
  aprobadas. Para una animación de una referencia de otro proyecto,
  `studio_animate(project_id=...)` guarda el resultado en el proyecto elegido.
- **Fuente de vídeo demasiado corta**: `source_fit="stretch"` permite solo
  un ajuste limitado; `"error"` exige cobertura; `"hold"` es una congelación
  explícita. Una fuente sincronizada con audio exige `"error"`. No uses una
  congelación para esconder un clip incompleto.

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
  con `beat_fx` (`source` kick/beats/downbeats o, con la canción ya separada,
  drums/bass/vocals/other; `zoom`, `flash`, `shake` de 0 a 1) y el resto del look; si ya estaba renderizado, se renderiza de
  nuevo solo el montaje. Enséñale el resultado con `studio_show`.
- **«Hazme el Canvas de Spotify» / «un bucle para Spotify»**:
  `studio_canvas(production)` (estribillo, 8 s, sin letra) y dale el
  enlace de descarga.
- **«Móntame un espacio / un lienzo» / «hazlo con nodos» / «conecta las
  fichas al plano»**: `studio_spaces(action="create", template=...)`
  (`character_outfit_motion` para persona+vestuario → imagen → clip con guía
  opcional; `reference_film` para personaje+criatura+lugar → fotograma → clip,
  `singing_shot` para lip sync) o en blanco y `action="edit"` con `ops`
  (`add_node`, `connect`...). Ejecuta con `action="run", mode="all"` y
  sigue el job; lee `state` con `action="get"` y enséñale las salidas con
  `studio_show`. Un `cast` da `image` (su referencia) y `text` (`@Nombre`).
  Para mejorar un prompt: `studio_prompt_enhance`.
- **«Hazlo en contrapicado» / «un primer plano» / «con luz de neón» / «que
  la cámara se acerque»**: `studio_cinema(query)` da el id y las palabras;
  pásalo como `camera={"shot": ..., "angle": ..., "light": ...}` a
  `studio_generate_image` o en `data.camera` de un nodo (los movimientos,
  `move`, solo en clips).
- **«Encadena los planos» / «que siga donde acabó» / «júntalo todo»**: en
  un espacio, conecta la salida `last` de un clip al `start` del
  siguiente y todos los clips a un nodo `combine` (con la canción en su
  `audio`). Para varias ideas a la vez: un `assistant` con
  `as_list=true` o una `list` en el `prompt` de una imagen hace un render
  por elemento. «Para» → `studio_spaces(action="stop")`.
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
- **«Sepárame la voz» / «dame solo la batería» / «una pista instrumental»**:
  `studio_stems(asset_id)` (voz, batería, bajo y resto como recursos de
  audio, más la mezcla `instrumental`; la primera vez instala Demucs y
  tarda). Después el lip sync usa la voz limpia solo y los efectos `kick`
  leen la batería. Si no hay GPU con
  3 GB libres, va a la CPU: díselo.
- **«Hazlo vertical» / «ahora cuadrado» / «versión horizontal»**: una
  imagen o un clip, `studio_reframe(asset_id, aspect, framing)`; un
  videoclip entero, `studio_production_reframe(production, aspects,
  framing)` (solo se vuelve a renderizar el montaje). `framing`: `fill`
  recorta, `blur` encaja sobre un fondo desenfocado, `fit` pone barras.
- **«Aprueba este plano» / «rehaz el resto»**: `studio_production_shots`
  con `{"key": "4", "locked": true}` y después
  `studio_production_regenerate(production, stage="clips"|"frames")`: solo
  se rehacen los no aprobados, con semillas nuevas.
- **«Que el plano 4 siga al 3 sin corte»**: `studio_production_shots` con
  `{"key": "4", "continue_from": "3"}` (su clip empieza en el último
  fotograma del 3 y acaba en su propio fotograma; hace falta el modelo de
  primer a último fotograma, si no el final se descarta).
- **«Quédate con la toma anterior» / «compara tomas»**:
  `studio_production_takes(production, key)` lista las tomas de un plano
  (fotogramas y clips, la actual marcada; se guardan hasta 12 por plano);
  enséñaselas con `studio_show` y devuelve la elegida con
  `studio_production_shots` y `{"key": "3", "take": asset_id}` (un
  fotograma trae su juego y descarta los clips hechos con el otro; un clip
  sustituye al actual).
- **«Arregla el segundo 2 al 3 del clip»**: `studio_retake(asset_id,
  start_s=2, end_s=3, prompt, quality="draft"|"final")`: rehace solo ese
  tramo (hasta ~4 s) con Wan VACE y empalma un clip nuevo; el original se
  queda. Enséñalo con `studio_show` y, si es el clip de un plano, aparece
  como toma suya. Si da `no_vace`, faltan los modelos en ComfyUI. Todavía
  no se ha ejecutado en una GPU real (los modelos están instalados, pero
  las tarjetas estaban ocupadas): avísale de que es la primera vez.
- **«Que sea de noche» / «ponle un abrigo rojo» / «cambia la guitarra por
  un violín» / «conviértelo en acuarela»**: `studio_clip_edit(asset_id,
  prompt, quality="draft"|"final")` rehace el clip entero (hasta 5 s; uno
  más largo, por ventanas desde `start_s`) con Bernini-R siguiendo la
  instrucción y manteniendo el movimiento; sale un clip nuevo con el
  tamaño, los fps y el sonido del original, y el original se queda. Con
  `preview=true` enséñale antes el texto que recibirá el modelo (la
  instrucción se reescribe salvo con `exact=true`). `mode`: `edit`
  (sustituir, añadir, quitar, recolorear), `restyle` (otro aspecto, luz o
  pose), `reference` (hasta 4 imágenes en `reference_asset_ids`; el
  `@Nombre` de un personaje con imagen vale). Para un cambio muy concreto,
  «desde un primer fotograma editado»: `studio_video_frames(asset_id,
  at_s=0)` -> edítalo con `studio_generate_image(prompt=el cambio,
  reference_asset_ids=[fotograma])` (edición por instrucción) ->
  `studio_clip_edit(asset_id, mode="propagate",
  first_frame_asset_id=...)`. Si es el clip de un plano, aparece como
  toma suya. En un espacio hace lo mismo el nodo `clip_edit` (Editar clip;
  entradas `clip`, `prompt`, `refs` y `first`; `data.mode`, `data.quality`,
  `data.start_s`): una edición por cada clip conectado. Si da
  `no_bernini`, faltan los modelos en ComfyUI. Todavía no
  se ha ejecutado en una GPU real (las tarjetas están ocupadas con un
  modelo de lenguaje): avísale de que es la primera vez.
- **«Pon este recorte encima» / «un logo sobre el vídeo»**: en un espacio,
  un nodo `composite` (Capas) con el fondo en `background` y lo que va
  encima en `layers` (hasta 8); por capa `data.layers[i]` con `blend`,
  `opacity`, `scale`, `x`/`y` y `key` (black/white para quitar un fondo
  liso). Sale un clip si alguna entrada lo es y, si no, una imagen. Para
  probarlo sin rehacer lo de arriba: `action="run", mode="upto"`.
- **«Guárdalo como técnica» / «úsalo en otro proyecto»**:
  `studio_spaces(action="export", space, group?)` da la técnica (sin los
  medios del proyecto; el reparto por nombre) y
  `studio_spaces(project=otro, action="import", bundle, name?, space?)` la
  crea o la añade a un espacio; revisa `to_fill` (medios por poner),
  `cast_missing` (personajes que faltan en ese proyecto) y `models`.
- **«Que el zoom vaya con la voz»**: separa antes con `studio_stems` y usa
  `studio_production_finishing` con `beat_fx` `source` `vocals` (o `drums`,
  `bass`, `other`); sin separar, vuelve al bombo.
- **«La versión instrumental»**: `studio_stems(asset_id)`; el recurso
  `instrumental` es la canción sin voz.
- **«Haz borradores rápidos»**: `studio_production_settings(production,
  clip_quality="draft")` y, cuando le guste el montaje,
  `studio_production_promote(production)` para pasarlos a final con las
  mismas semillas.
- **«Letra palabra a palabra» / «letra a máquina» / «estilo cine»**:
  `studio_production_finishing` con `lyric_style` `pop`, `typewriter`,
  `cinema`, `pulse` o `handwritten`.
- **«Úsalo como app» / «déjamelo como formulario»**: marca las entradas
  (`data.app_input`, con `data.app_label`) y las salidas (`data.app_output`)
  del espacio con `studio_spaces(action="edit")`; mira el formulario con
  `action="app"` y ejecútalo con `action="app_run", values={nodo: valor}`.
  Antes de una ejecución larga, `action="estimate"` da minutos y renders.
- **«Móntamelo tú» en un espacio**: `studio_spaces(action="build",
  request="...")` (necesita un modelo local); revisa el resultado con
  `action="get"` antes de ejecutar y corrige con `ops`.
