# Prospero video workspace

Una hoja de personaje guarda cada vista terminada en su trabajo, kit y dataset. `studio_retry_job` reanuda un intento fallido con las vistas ya guardadas y reconstruye la hoja de contactos al terminar. El reintento con una semilla nueva genera una nueva variante. Las tandas de generación y edición guardan los resultados completos de cada semilla. Los servidores de render locales se asocian a su GPU física y solicitan su reserva de memoria; los que comparten tarjeta o servidor esperan su turno. Los fallos de generación, edición y hoja se publican como eventos de trabajo y se pueden vincular a una tarea del Ágora.

`website_walkthrough` records a real running website as a CPU job. Supply `url`, `title`, optional `project`, `hold_s` (0.5–10 seconds), and up to 24 steps: `click` with `selector`, `navigate` with `url`, `scroll` with `y`, or `wait`. Each step may carry `caption`. An omitted project creates a new one. Poll `studio_job(job_id)` for the H.264 MP4, contact sheet and actual visited page titles; inspect the saved video with `studio_show`. Captions are drawn over the real page and remain literal text. Local apps are allowed. The capture context starts without existing cookies or login; use the intended reachable preview. Failed steps fail the job and do not publish a partial video. The output recipe retains the requested steps and the actual visited URLs. Faustus calls the same operation from its turn summary and `task_walkthrough`.

In the standalone workspace this operation belongs to the existing Generation module. Enable that module explicitly with the module control or `studio_module_set`; catalogue reads and the Faustus client do not enable it or undo a manual stop.

The primary entry resumes a persistent, connectable Spaces canvas. MCP
`studio_spaces` supports the `quick_video` starter and explicit video engine,
fast/full schedule, prompts, references and durations in the same graph.
`studio_video_engines` exposes live complete-workflow readiness without loading
models. `studio_video_from_plan` continues automatically unless
`autopilot=false` opts into review. See [video workflow](docs/video-workspace.md).

Video nodes with one included result offer **Continue from last frame**. This
adds and focuses a wired successor, retains generation settings and leaves the
next action blank; no render starts. It uses the selected upstream take and
undoes as one edit. MCP has the same add-node/last-to-start graph operations.

A video node's optional final-frame input also preserves the fast/full
schedule. Switching fast generation off requests twenty steps for the
first-and-last-frame workflow, as it does for a start-only clip.

Motion-wired video nodes accept `motion_prompt` separately from the appearance
and setting in `prompt`, plus native `pose_strength` and
`reference_image_strength` (0–10, trained default 1). Do not assume stronger
conditioning yields better results. Inspect actual takes and keep failed ones.

`engine="ltx23"` selects LTX2.3 distilled image-to-video explicitly. A motion
node's `motion_engine="ltx23_control"` selects Union Control IC-LoRA: Canny
structure from the guide, appearance from the reference. Animate2 remains the
motion default; automatic image-to-video selection stays unchanged. LTX uses
24fps and rounds duration to its eight-frame grid, with native guide offset and
motion strength. No local prompt-generating LLM runs. These are generation
controls, not guarantees of choreography, anatomy or identity continuity.
MCP `studio_generate_image` exposes the same `driving_asset_id`,
`driving_start_s` and declared `template_params`; references and guide IDs remain
in the resulting recipe. LTX weights use the LTX-2 Community License.
Image-only dimensions must be multiples of32; ref0.5 structure control requires
multiples of64 (896x512 default). Invalid sizes/frame counts fail before rendering.
The image-only graph records its effective eight-step `manual_sigmas` schedule
and denoise1; incompatible sampling overrides return an explicit explanation.
Control maps its denoise setting to the sampler. Managed local servers reserve
their physical GPU using the effective URL (including environment overrides).
A worker starting a server under its lease uses the granted card. HTTP
preflight may start the server process to read its schema before enqueueing;
this does not load or execute the checkpoint. Model execution follows the lease.

Imported UI workflows preserve numeric union widgets such as `FLOAT,INT`,
declared input order and named widget values. The node-schema cache keeps
nested field order: an audio frame rate must not silently become a batch count,
and cached resize controls must not exchange width and height.

`studio_review_clip(asset_id, prompt?, reference_asset_ids?, check_supporting_cast?)` reviews a video
without a production, through the same optional action available in its node.
It reports technical checks and, if vision is available, a chronological sample
of up to 24 frames with motion, anatomy, setting and cast scores. Respect
`visual_review.status`, `unassessed`, missing references and
`full_playback_reviewed=false`: neither job completion nor a technical pass is
an artistic approval. It does not pause or gate generation. Named secondary
characters still use the existing individual cast/reference controls; this
change adds no multi-reference video model.
Without a prompt override, review uses the selected asset's saved appearance
and action, including Animate2's pose prompt. It never substitutes newer node
edits for an older take. Use `check_supporting_cast=true` to require a secondary
identity assessment; null then leaves the review incomplete. Observed low cast
scores are retained even when this flag is omitted. Temporal scores require
valid model JSON: explicit nulls and malformed replies cannot become approval
through numbers quoted in the explanation.

Real video comparisons are separate from simulated interaction checks: one
four-second walking/waving scene took 250.60 s at four-step 14B versus 951.09 s
at the full schedule, with both subject continuity and the action observed.
These single runs do not establish universal quality or speed.

## Scene shots

`studio_scene_suggest` uses the configured vision service to plan from a
scene dossier and a character sheet. Save the plan with `studio_scene_plan`
using a first and last image or a structure guide. The production checkpoints
individual GPU jobs and assembles the cut with a fingerprint for each take.
`studio_scene_review` checks the first, middle and last frames within the
mounted shot duration. `studio_scene_record_review` stores the observations
against the exact take. `studio_scene` marks the cut provisional until every
mounted take passes this review. Use the storyboard’s per-shot redo or revise its controls with
`studio_scene_revise_shot`. See [scene production](docs/MCP.md#scene-video).

## Existing project and shot tools

The dashboard remains available from Project. Its inline character/group editors use the existing native cast APIs; MCP keeps the same `studio_cast` contract. Clip creation uses the existing animation/generation APIs, direct job polling and actual output IDs. Native `studio_animate`, `studio_job` and `studio_video_from_plan` were exercised through stdio MCP on isolated synthetic data.

Clicking an image-shot storyboard card opens its own connected frame/video workspace, including when no frame exists. It saves through `studio_production_shots` semantics and continues the native production; it retains the shot identity, selected frame and actual output binding. Rendering may complete other pending shots too. The standalone clip studio creates separate project assets. Free-form Spaces and native production take selection remain available. See [project and clip workflow](docs/PROJECT-STUDIO.md).

Shot readiness follows its frame/clip records independently of a production job finishing. The visible References node adds, uploads, replaces, describes and removes up to six image references through the existing `studio_production_shots` payload; no new MCP contract is introduced. Production failures retain their concrete message or log fallback.

No user media, lyrics, production specifications, model configuration or GPU services are changed by this interface update. Model quality is outside the synthetic interaction checks.

Spaces has a searchable library with28 shortcuts across all16 native node types.
Three video starters prepare asset nodes and wires: motion=start:image plus
motion:video, lip sync=start:image plus audio:audio, between-frames=start:image
plus end:image. Creation starts no render and is one undo step. Agents use the
existing studio_spaces add_node/connect operations and ordinary data fields.
Combine displays clip wires in execution order with up/down controls; agents
reorder via disconnect/connect in the desired order. Each wire contributes its
included takes. No new model installation or quality guarantee is implied.

Extract frame is native `type="frame"`, `data={"at_s":1.2}`, video input `clip`
(many), image output `out`. Use `studio_spaces` add_node/connect/run normally.
It extracts one still per included clip locally without a model; its result can
feed refs or a video start. Selects the frame presented at the requested time,
including the last interval and variable frame timings. Recipes retain requested
`at_s` and actual `frame_time_s`. Originals remain intact; invalid times fail
before batch work. The UI offers source playback, seconds and Use playhead time.

Spaces LTX image-to-video also accepts `refine=true`: optional native2x latent upscaling and3 fixed refinement steps after the8-step base, exposed by the node checkbox. Direct API/MCP template: `ltx23_i2v_hq`. Requires the LTX2.3 spatial-upscaler-x2-1.1 weight; catalog availability checks it separately, without hiding the base model. Both stages reuse the caller seed/sampler; immutable recipes record `video_refinement`. This improves the detail workflow, not a guarantee of artistic continuity or dynamic camera.


HTML animations in Montaje can carry sound: `motion_sound_events` declares events `{t, role}` and a music bed, `motion_sound_preview` mixes them and reports the sync error measured on every effect, and `motion_export` (or `html_motion_render`) returns an H.264 + AAC MP4 at -14 LUFS under -1 dBTP. Effects are generated from code and placed by their measured onset/peak/end; `audio_sync_points` and `timeline_edit add_media {align, at}` do the same for any imported sound. A mix of a few sparse effects without music reports `reached: false` instead of faking the loudness. See `docs/HTML_MOTION.md`.

Direction flow for HTML animations (Montaje): `motion_styles` searches 74 imported direction cards (each with licence and source, and split into safe / character / essence / free parts), `motion_treatment` stores the treatment before any HTML (three structures, the shots with why each is there, the cue map), `motion_from_style` makes a scaffold from a card, `motion_cues` owns the cues that drive picture and sound (`motion_sound_events` refuses to change events while cues exist), `motion_cuecheck` measures each cue against the frame where the picture hits and lists hits with no cue, `motion_readcheck` checks letters/15 s + 1.5 s, `motion_facts` keeps FACTS.md for informative videos, and `html_motion_storyboard` has `strip` and `crop-at` modes. The scaffold is a starting point, not a finished film. See `docs/HTML_MOTION.md`.

Timeline as text: `timeline_text` returns the whole edit as stable text with its revision, `timeline_apply_text` changes it with exact `{old,new}` replacements (all or nothing, refused on another revision, proved on a copy), `timeline_check` and `dry_run` simulate, and `cut_proposals` proposes filler words, hesitations, repeats and pauses with confidence and reason as a separate document that names transcript word ids and expires when the transcript changes; accepting is one undo step. See `docs/TIMELINE_TEXT.md`.

## Edit plans, delivery verification, workflow preflight, treatment-first productions (03-11-2026)

Edit plans (Generación > Entrega / Edit & deliver; `prosperos_hoard/edit_plans.py`): a declarative edit (clips with
in/out trims and transitions, including native xfade names, captions, image overlays, a music bed ducked by the
clips' sound, a loudness target, platform `shorts|reels|tiktok|youtube`) is compiled into the exact ffmpeg steps
(reusing `video.py`'s clip, join and mux builders) and saved as `data/edit_plans/<ep_...>/plan.json` with a
fingerprint of every input (size, mtime, sha256 of the first MiB). `edit_run_plan` refuses with `inputs_changed`
when any input changed, renders on the CPU lane, registers the video and verifies it. `edit_verify` checks a video
against the platform spec (resolution and shape, fps, duration limit, codecs, stereo, sample rate, EBU R128
integrated loudness and true peak through ffmpeg `ebur128`; pass/warn/fail per check). `edit_contact_sheet` tiles
every Nth frame (`select` + `tile`) into an image asset. REST: `/api/edit-plans`, `/api/edit-plans/{id}/run`,
`/api/edit-plans/verify`, `/api/assets/{id}/contact-sheet`. An edit's `fps` is a whole number (29.97 is refused,
not rounded down), a transition may not be longer than the clip it enters, and a caption or overlay end past the
edit's end means "until the end". The frame-rate check accepts the NTSC rates (23.976, 29.97, 59.94).
Deleting a plan (`edit_plan_delete`, `DELETE /api/edit-plans/{id}?outputs=true|false`, trash button in the Plans
list with a confirm and a "move its videos to the trash" checkbox) removes its folder (plan.json, captions, concat
list, leftover intermediates) and, with `outputs`, moves the videos its runs rendered (assets whose recipe names the
plan) and their contact sheets to the trash, restorable; inputs are never touched and a render a timeline uses is
kept (`outputs_kept`). Refuses with `plan_running` while an `edit_plan_run` job of the plan is queued or running.

Workflow preflight (`workflow_preflight.py`, MCP `workflow_preflight`, `POST /api/workflows/preflight`): every node
class of a ComfyUI workflow against `/object_info` and every model file (checkpoints, LoRAs, diffusion models, VAEs,
text encoders, upscalers, audio encoders...) against the loader's list, with the node pack that usually provides a
missing node and the `models/<folder>` for each missing file, plus a copyable list. Never starts ComfyUI: off, it
uses the last saved node list (`source=cache`) or says nothing was checked. The production preflight shows the same
checklist (a warning at most) and every production run records it in its lineage before starting.

Treatment-first productions (`treatment.py`, tab Tratamiento, MCP `production_treatment_draft` with
`draft|get|update|approve`): logline, three structures and the pick, benchmark learn/avoid, shot list (framing,
camera, duration, why) and beat sheet, drafted by the family model or by rules from the planned shots when no model
answers (`source: rules` with the reason). A production with a treatment pauses at its `treatment` stage;
"Approve and render" (or `studio_production_continue`) approves and continues. `production_storyboard_sheet` tiles
6-9 key shots: the production's own stills, ComfyUI stills queued with `generate=true` when ComfyUI is up, else
placeholders drawn from the shot text. A redraft or an edit of an approved treatment needs approving again; a
treatment drafted, edited or approved while the production is running is kept (the run merges it when it saves);
approving it while the production waits at another review (song, script, animatic) does not approve that review.

HTML motion (Montaje): `motion_lint` (button "Comprobar que no es un pase de diapositivas") counts targets whose
only animation is opacity in the composition's GSAP tweens (including tweens whose vars nest objects such as
`stagger: {each}` or `scrollTrigger`) and CSS; 3 or more give `reject` with a re-prompt. The
seek-stepped Playwright renderer with sound (`html_motion_render`, `motion_export`) already existed.
`html_motion_delete` (editing module, `DELETE /editing/api/html-motion/{id}`, button "Borrar composición" under the
composition picker, with a confirm) removes the composition's folder `data/editing/creative/html/<id>`: every
revision, render, storyboard, sound preview, treatment, cues and facts. Refuses with `html_motion_busy` while an
export of it is queued or running and with `html_motion_in_use` while a library media still reads one of its
renders (the library imports a render where it is). Like every editing tool it needs the editing module ON.

Video enhancement (Montaje, Lumiere): a saved video or the span a timeline clip uses is enhanced on a spare GPU: noise reduction (`light` on the CPU,
`medium` and `strong` on the GPU), upscaling ×1.5/×2/×3 (up to 3840 px wide), sharpening 0..1, frame-rate interpolation (`blend`, or `mci` motion
compensated and slow; target `interpolate_fps`) and HDR10 (HEVC Main10, PQ, BT.2020). Tools: `enhance_info` (effects, GPU, Vulkan and NVENC on this
machine), `media_enhance` (a library video becomes "<name> (mejorado)", the original stays), `clip_enhance` (the clip is pointed at the enhanced span
and keeps its length), `enhance_benchmark` (ms per frame of the baseline and of each effect, and whether the chain keeps up with the frame rate);
all three run as jobs (`job_status`) and need at least one effect. REST: `POST /editing/api/media/{id}/enhance`, `POST /editing/api/projects/{id}/enhance`,
`GET /editing/api/media/enhance/info`; the inspector of a video clip has a "Mejorar vídeo" section. The chain is ffmpeg: Vulkan filters
(libplacebo, `nlmeans_vulkan`) and NVENC on the GPU in `LUMIERE_ENHANCE_GPU` (default 2, the Vulkan and NVENC index), x264/x265 and CPU filters when
Vulkan or NVENC are missing; what could not be applied is in the job result under `skipped`. Like every editing tool it needs the editing module ON.

## Biblia de continuidad, reparación de tramos y revisión de uniones (03-11-2026)

Qué es: una **biblia de continuidad** por producción (`prosperos_hoard/continuity.py`, guardada en
`data/continuity/<cb_...>/bible.json`), la **reparación de un tramo** de un vídeo y la **revisión de las uniones**
(`prosperos_hoard/segment_repair.py`). Interfaz: sección Continuidad / Continuity. REST bajo `/api/continuity`
(biblias, `entries/{id}`, `shots/{id}`, `shots/{id}/clip`, `corrections`, `repair`, `review-join`, `consistency`).
MCP: `continuity_bible`, `continuity_entry`, `continuity_shot`, `continuity_repair_segment`,
`continuity_review_join`, `continuity_check_consistency`.

Qué guarda la biblia:
- **Entradas** con revisión e historial: `identity` (paleta `#rrggbb`, estilo, imágenes de referencia de estilo) y
  `timing` (fps, tamaño, frecuencia y canales de audio) existen siempre; además objetos, personajes, voces
  (`voice_id`, `settings`) y estilos, con `descriptor` e imágenes de referencia. Un cambio real crea una revisión
  nueva; guardar lo mismo no crea nada.
- **Planos** con rango, descripción, cámara, prompt, referencias, entradas que usan (todos usan `identity`) y
  `regions` (dónde aparece una entrada, en fracciones del fotograma) para la comprobación de color.
- **Clip aceptado** (`accept_clip`): guarda el id del clip, la operación y la revisión de **cada** entrada y las
  imágenes de referencia con las que se hizo (procedencia). Un plano está obsoleto cuando alguna entrada que usó
  tiene hoy otra revisión (o la entrada se quitó, o se añadió después) o cuando cambió el propio plano (descripción,
  cámara, prompt, rango, referencias). Solo esos planos salen en `must_regenerate`; el resto sigue en
  `kept_accepted` con su clip. Cambiar `identity` marca todos los planos (todos la usan); cambiar `timing` no marca
  ninguno, porque la reparación conforma los clips.
- **Correcciones** ligadas a un tramo o región de un plano; al reparar con `correction_id` pasan a `applied`.
- **Procedencia** (hasta 500 registros): clip, plano, operación, entradas con revisión y referencias. Una reparación
  la graba también en la receta del asset nuevo (`recipe.continuity`).

Reparar un tramo (`continuity_repair_segment`, trabajo en el carril de CPU, `wait_s` para esperar):
1. El rango (o el del plano) se ajusta a fotogramas; el sustituto (cualquier asset de vídeo; no se genera aquí) se
   conforma a resolución, fps, formato de píxel y audio (frecuencia y canales) del vídeo, con `framing` fit o fill.
2. Se buscan los fotogramas clave alrededor del rango. Los GOP antes y después se copian sin recodificar; solo el
   GOP que toca el rango se recodifica con x264 (`reencoded_range_s` lo dice). Si el origen no es H.264 en MP4 o las
   partes copiadas no decodifican a los mismos fotogramas, se recodifica todo y sale `exact_outside=false` con la
   razón en `notes`.
3. Corte seco (con `declick_ms` de fundido de audio en cada unión, 0 para quitarlo) o fundido encadenado de
   `crossfade_s` dentro del rango reparado: la duración y el número de fotogramas no cambian. El audio se reconstruye
   desde muestras decodificadas y se codifica una vez (AAC 192k); un sustituto sin audio deja silencio y lo avisa.
4. `untouched` compara los hashes de fotograma del original y del resultado fuera del rango recodificado
   (`frames_identical` = `frames_compared`). El origen nunca se modifica.

Revisión de uniones (`continuity_review_join`, también automática tras reparar): por cada unión, fotogramas negros
(luma media < 12 con el contexto por encima de 30), fotogramas congelados (4 o más idénticos tocando el corte, salvo
que el contenido sea estático), salto de color (cambio máximo del color medio por canal y distancia de histograma
entre 3 fotogramas antes y 3 después), salto de nivel de audio (RMS de 150 ms a cada lado: aviso 6 dB, fallo 12 dB)
y chasquido (mayor escalón entre muestras en ±4 ms frente al percentil 99,9 del resto y al menos 0,08 de fondo de
escala); en conjunto, duración y fotogramas conservados. Los umbrales están en `segment_repair.THRESHOLDS` y se
cambian con `thresholds`. Guarda una tira de 6 fotogramas consecutivos por unión (el corte cae entre las miniaturas
3 y 4). Son heurísticas: un corte legítimo entre escenas distintas puede dar aviso de color y un pase no aprueba el
montaje; hay que mirar la tira.

Consistencia (`continuity_check_consistency`): firma de color (CIELAB medio e histograma RGB de 64 casillas) de 5
fotogramas del clip del plano, dentro de la región declarada para la entrada o del fotograma entero (entonces el
fondo cuenta), frente a las referencias o la paleta de la entrada: aviso `colour_drift` con delta E >= 18 o
histograma >= 0,45; además `entry_not_named`, `descriptor_colour_missing` y `no_reference`. Todo son avisos; **no
detecta ni garantiza que un personaje u objeto sea el mismo**.

Límites: no genera el tramo sustituto ni engancha los generadores existentes (`accept_clip` se llama a mano o desde
el asistente); los clips generados por otras vías solo quedan en la procedencia si se registran con `accept_clip`; sin ffmpeg las funciones de
vídeo dan `no_ffmpeg` y los tests se saltan; con GOP largos (por defecto 250 fotogramas en x264) la zona recodificada
puede ser mucho mayor que el rango pedido, y se informa. Tests: `tests/test_continuity.py` (medios sintéticos con
lavfi `testsrc`/`sine`).

## 04-11-2026 · Borrar una biblia de continuidad (Hangar E39)

La biblia de continuidad, la reparación de tramos y la revisión de uniones (4aebd37) se verificaron en vivo para el programa Hangar: vídeo de 20 s con 5 s (8–13 s) sustituidos por otro clip en 3,3 s, duración conservada (20,000 s), las dos uniones con salto de color detectado (fuentes distintas a propósito) y la reparación anotada en el plano con las revisiones de las entradas usadas. Faltaba poder quitar una biblia: ahora `continuity_bible(action="delete")`, `DELETE /api/continuity/{id}` y el botón «Borrar biblia» (con confirmación) eliminan la biblia y sus decisiones, planos, correcciones y procedencia; los clips y las imágenes de referencia siguen en el proyecto. Prueba en `tests/test_continuity.py`.


## 04-11-2026 · Estado obsoleto después de reparar un tramo (Hangar E39, prospero#2287)

Problema visto en vivo: tras cambiar la entrada `lamp` (r1→r2) y reparar 8–13 s del plano `dos` (10–20 s) con la revisión vigente, el plano seguía marcado obsoleto sin decir por qué. Decisión: una reparación cuenta como hecha con las revisiones vigentes **solo dentro del tramo que sustituyó**. `record_repair` anota el tramo como segmento del clip aceptado (`clip.segments`, con las revisiones y la revisión del plano); `clip_pieces` parte el rango del plano por lo que hizo cada trozo y `stale_reasons` comprueba cada trozo: los motivos llevan `ranges_s` (lo que sigue hecho con la revisión antigua) y `repaired_ranges_s`; `must_regenerate` añade `stale_ranges_s`. La respuesta de la reparación trae `provenance.up_to_date` (planos que la reparación dejó al día) y `provenance.still_stale`. Aceptar un clip para el plano entero empieza de nuevo con un solo trozo. Interfaz (Continuidad > Planos): «Por regenerar: dos (lamp r1→r2; aún antiguo en 13–20 s; reparado 10–13 s)», una nota que explica la regla, «Reparado: 8–13 s (lamp r2)» bajo el clip y el botón «Aceptar vídeo reparado» para cuando en lo que queda no aparece la entrada cambiada. En vivo: reparar 8–13 s deja `dos` obsoleto solo en 13–20 s; reparar 13–20 s sobre el vídeo reparado lo deja aceptado sin volver a aceptarlo (E39-STALE-QA.json, E39-STALE-UI-QA.json, E39-UI-OBSOLETO-parcial.png, E39-UI-OBSOLETO-vigente.png). Pruebas en `tests/test_continuity.py` (2 nuevas). Sigue en prospero#2287: reparación por región, voz de la biblia en la reparación y regenerar planos obsoletos.

## 05-11-2026 · Reparar una región, la voz de la biblia en la reparación y regenerar lo obsoleto (Hangar E39, prospero#2299)

- **Reparar solo una región** (`repair_segment(region=[x, y, w, h] | "correction", feather_px)`): dentro del tramo solo se sustituye ese rectángulo (o el de la corrección con `correction_id`); el resto de cada fotograma y el sonido se quedan como estaban (el audio se copia sin tocar). `review_region` mira tres fotogramas del tramo: fuera del rectángulo nada cambió (diferencia media < 4), el borde no se nota más que la misma línea en el original (< 15) y el rectángulo sí cambió, con un fotograma de muestra con el rectángulo dibujado. `feather_px` suaviza el borde (alfa por `geq`). La región se puede combinar con `transition=crossfade` (revisión independiente 05-11: antes ffmpeg la rechazaba por la base de tiempos de la rama de superposición). Una reparación solo de región da el tramo por hecho con las revisiones vigentes aunque el resto del fotograma siga siendo el antiguo: decisión de producto abierta, anotada en PENDIENTES.
- **Una frase con la voz de la biblia** (`voice={entry_id, text, at_s?, max_speed?}`): el texto se dice con el `voice_id` y los ajustes de la entrada de voz por el estudio de voz (biblioteca si existe esa voz; si no, como `voice_ref` del motor de los ajustes), se coloca en el tramo desde `at_s`, se acelera hasta 1,5x si no cabe y se corta al final; es el sonido del tramo y la revisión de la entrada queda en la reparación (`voice` en el resultado y en la receta del asset).
- **Regenerar lo obsoleto** (`prosperos_hoard/continuity_regen.py`, `continuity_regenerate` plan/run/use_clip, `POST /api/continuity/{id}/regenerate`): el plan da por plano obsoleto el prompt con la revisión vigente de cada entrada que usa (descriptor, estilo, paleta, cámara), las imágenes de referencia, el fotograma inicial, los tramos obsoletos (tras una reparación parcial, solo esos) y los que se conservan. `run` es un trabajo orquestador: toma nueva por la cola de render (fotograma inicial si no hay imagen de referencia, después `auto_clip` del tramo) y la empalma solo sobre los tramos obsoletos con reparaciones selladas con las revisiones vigentes; `use_clip` hace lo mismo con una toma hecha en otro sitio. Cada regeneración queda en `shot.regenerations` y en la procedencia de la biblia. Un plano cuya toma falla sigue obsoleto y dice por qué.
- **Reparaciones encadenadas exactas**: la parte recodificada va sin B-frames y con marcas de tiempo rehechas por índice de fotograma, la cola se copia con búsqueda no exacta desde su fotograma clave y cada parte lleva su duración en la unión; antes una reparación sobre un vídeo ya reparado (o de un tramo que solo usaba el sustituto) perdía fotogramas y acababa recodificándolo todo.
- Pantalla Continuidad: «Qué sustituir» (fotograma entero, una región con x/y/w/h, la región de una corrección, borde suave), «sustituto desde s», la voz de la biblia con su frase, la revisión de la región con su fotograma, y en cada plano obsoleto «Regenerar…» con el plan, «Hacer una toma nueva» y «Usar esta toma».
- En vivo (módulo Generation encendido solo para la prueba y apagado después; Piper es_ES-davefx-medium): vídeo de 20 s, lámpara r1→r2; frase «La lámpara ya es de latón envejecido.» en 15–17 s por el puente MCP (2,09 s dichos a 1,16x); región de la corrección en 12–15 s desde la pantalla, 425/425 fotogramas idénticos fuera del tramo, fuera de la región 0,36, borde 0,21; «Regenerar…» con el plan (toma de 10 s usada en 10–12 y 17–20 s, se conserva 12–17 s) y «Usar esta toma» → plano al día; lámpara r3 y regenerar por MCP → al día otra vez. La toma nueva con el estudio (ComfyUI y modelo de vídeo en GPU) no se lanzó en vivo; el trabajo orquestador está probado con un generador de prueba. Recibos E39-2299-REST-MCP-QA.json, E39-2299-UI-QA.json, E39-UI-REGION.png, E39-UI-REGENERAR-PLAN.png, E39-UI-REGENERAR-HECHO.png; proyecto purgado.
- Pruebas: `tests/test_continuity.py` (15, 4 nuevas), `tests/test_mcp.py`, `tests/test_studio_fusion.py` (215 herramientas), `tests/test_jobs.py`, `tests/test_voice_lab.py`, `tests/test_productions.py`.

## 10-11-2026 · Promo desde una web (prospero#2362)

- **Qué hace**: `promo_from_url(project, url, length_s=30, formats=["16:9","9:16"], vibe="keynote"|"launch"|"minimal")` crea un vídeo promocional de una web como un director de motion. `brand_scan` analiza antes la marca (logo, tres colores, tipografías con licencia abierta, titular, propuesta de valor, frases) con un navegador sin ventana y guarda el kit en `data/promo/kits/<id>` y, con proyecto, en la biblioteca (logo y tablero de marca con etiqueta `brand`). `contact_sheet(asset_id, fps=4)` saca la hoja de contactos de cualquier vídeo.
- **Cómo**: `promo_brand.py` (análisis y kit), `promo_compose.py` (guion de plantilla o escrito por el modelo local, HTML con `window.seek/measure`), `promo_web.py` (revisión sobre hoja a 4 fps: texto cortado o fuera del cuadro, solapes, contraste, texto pequeño, colores fuera de la marca, planos congelados, saltos en los cortes; hasta 2 correcciones por reglas; render fotograma a fotograma a ffmpeg a 1920x1080 y 1080x1920, 60 fps, yuv420p; hoja de contactos del vídeo ya terminado) y `promo_sound.py` (sonido en código con numpy: pulso al tempo de los planos, whoosh en cada movimiento, golpe en el logo; masterizado a unos -14 LUFS con loudnorm en dos pasadas; o mezcla con la música propia `music_asset_id`).
- **Interfaz**: Herramientas > «Promo desde una web» (en inglés «Promo from a web page»): dirección, duración, formatos, estilo, fps, idioma del texto, música; «Analizar la marca» y «Crear el promo»; progreso por fotograma, kit de marca, resultado con vídeos, comprobaciones, sonómetro y hoja de contactos.
- **En vivo** (python.org, 12 s, ambos formatos, por el puente MCP real): trabajo en 66 s; 1920x1080 y 1080x1920, h264 yuv420p, 60 fps, 720 fotogramas, AAC 48 kHz estéreo, -14,7 LUFS; 0 avisos en la revisión; hoja y fotogramas vistos; la pantalla probada con Playwright (analizar, crear, progreso 68 %, resultado). Proyecto de prueba purgado, kit y temporales borrados; módulo de generación apagado como estaba.
- **Límites**: necesita Playwright con Chromium y ffmpeg; la web se ve sin iniciar sesión; 30 s en dos formatos tardan unos 4 minutos en CPU. Vigilado: en el plano vertical del logo el subtítulo aún se está revelando a los 7 s en un vídeo de 12 s.
- Pruebas: `tests/test_promo_web.py` (24: marca, guion, comprobaciones, sonido, REST, un render real corto), `tests/test_mcp.py` y `tests/test_studio_fusion.py` (227 herramientas); toda la suite en verde (1520 pasadas, 3 saltadas).

## Plantillas de animación HTML y variantes con datos (10-11-2026, Darwin 1021)

Auditoría de Remotion (marcada «cubierta» porque Prospero ya compone vídeo con HTML y ffmpeg). Inventario de Remotion: composiciones como componentes con propiedades por defecto y esquema editable en su estudio, render de muchas versiones desde datos, reproductor embebible, fotogramas sueltos, subtítulos, transiciones, Lottie/Three, render repartido en la nube. Prospero ya tenía: composiciones HTML con `window.seek(t)`, revisiones, fotograma exacto, hojas de momentos, sonido declarado, exportación H.264 verificada y entrega a la línea de tiempo. Faltaba lo central de Remotion para producir en serie: **parámetros y variantes desde datos**.

- **Qué hace**: una animación se vuelve plantilla escribiendo `{{nombre}}` y, para su script, un bloque `<script type="application/json" id="motion-props">` con los valores por defecto. `html_motion_props` lista cada parámetro (por defecto, tipo texto/número/color/booleano, si está en el marcado y/o en el script). `html_motion_variants` crea una composición por fila (lista de objetos, JSON o CSV con cabecera, coma o punto y coma), con los valores escapados, el bloque JSON con los valores fusionados y las celdas CSV con el tipo de su valor por defecto; comprueba todas las filas antes de guardar nada (columna desconocida o parámetro sin valor ni defecto = rechazo entero) y puede dejar cada variante en cola para MP4. La plantilla no cambia. 50 variantes por llamada como mucho.
- **Cómo**: `prosperos_hoard/editing/html_motion_props.py`; REST `GET /editing/api/html-motion/{id}/props` y `POST /editing/api/html-motion/{id}/variants`; herramientas en el catálogo (`studio_catalog.json`, `studio_mcp_catalog.json`, 229 herramientas); interfaz «Variantes con datos» en el taller de animación HTML (`HtmlMotionVariants.jsx`): parámetros, filas precargadas con la cabecera, columna que da nombre, «Exportar también cada una a vídeo», lista de variantes que se abren en el taller.
- **En vivo** (8815, paquete reinstalado en `.venv` y app reiniciada desde el Hub, módulo de edición encendido otra vez): por MCP real (stdio) `html_motion_props` y `html_motion_variants` con un CSV de dos filas y `render: true`: dos composiciones y dos MP4 terminados; una columna mal escrita devuelve 400 con los parámetros válidos; en Playwright la sección enseña los parámetros, crea una variante y la lista. Datos de prueba borrados.
- **Pruebas**: `editing_tests/test_html_motion_props.py` (6) en verde; `tests/test_studio_fusion.py` con el nuevo recuento. Las pruebas de navegador de `test_html_motion.py` agotaron hoy el tiempo de arranque del navegador (60 s) con la CPU al 100 %; no tocan este código.
- **No se hace**: reproductor embebible en otras webs, render repartido en la nube, Lottie/Three (el HTML ya admite cualquier librería incrustada).

## Director: de una idea a una película corta, plano a plano

- **Qué hace**: una idea se convierte en un corto narrado. El modelo de lenguaje de la familia escribe la lista de planos (frase del narrador, fotograma inicial en inglés, qué se mueve, encuadre y movimiento de cámara); la dirección común (género, cámara y lente, luz, película, atmósfera, etalonaje y década de 1960 a 2020) entra en cada plano. Cada frase se locuta con subtítulos palabra a palabra, cada plano tiene una o varias imágenes (Qwen-Image cuando está instalado; hasta 4 por plano en un lote) y se anima con LTX 2.3, Wan 2.2 14B o 5B, o el mejor instalado. Opcional: que cada clip acabe en la imagen del plano siguiente (modelo de primer y último fotograma de Wan 14B) y un efecto de sonido sintetizado en cada corte. Música compuesta en local como en los shorts. Sin modelo de lenguaje, la idea se parte en un plano por frase y la respuesta lo dice (`source: "split"`).
- **Cómo**: `studio_director_catalog()` da los 28 presets de movimiento (cámara, cámara épica, efectos y ambiente) con las palabras que añaden al clip, los ejes con sus opciones (un género rellena los ejes que siguen en automático) y los motores de vídeo; con `production` da la película plano a plano (imagen en uso y sus tomas, clip, sitio en la locución) y los renders. `studio_director(action="plan", idea)` devuelve la lista de planos sin crear nada; enséñasela a la persona. `studio_director(action="create", idea | shots, director={picks, era, move, effects, engine, takes, end_frames, sfx, cast}, options={voice, music, timeline})` la rueda como una producción reanudable que para en el animático salvo `settings.animatic_autocontinue`. `studio_director(action="edit", production, changes=[...])` cambia un plano (`text`, `visual`, `action`, `framing`, `move`, `effects`, `cast`, `animate`), `redo: "image"|"clip"`, `take: asset_id` para volver a otra imagen, `delete`, `after` para añadir y `move_to`; solo se rehace lo que depende del cambio: una frase nueva rehace la voz y el montaje, una imagen nueva rehace esa imagen y su clip, un movimiento nuevo solo el clip. En la web, Producciones > **Nueva película dirigida** (planificar, editar la lista y rodar) y la tarjeta **Planos** del detalle (editar, otra imagen, otro clip, elegir toma).
- **No se hace**: efectos de sonido generados por modelo (los del corte son sintéticos), música con secciones o bucles a medida más allá de lo que da ACE-Step con su letra, ni rampas de velocidad con interpolación.

## Skills for the agents that connect

This app carries procedures from the skill library of the family's workspace for the agents that connect to it: prospero: `character-consistency-pack`, `image-composition`, `source-explainer-production`, `deterministic-motion-production`, `ffmpeg-video-editing`, `revision-aware-media`, `local-voice-comparison`, `browser-motion-graphics`; lumiere: `ffmpeg-video-editing`, `revision-aware-media`, `deterministic-motion-production`, `source-explainer-production`. They are named in what an agent reads on connecting, read with `skills_list` and `skill_read`, searched and assigned with `skills_library` and `skills_assign`, and offered as prompts. The person chooses them on the skills page (`/api/agent/skills/page`) or in the hub's Skills tab; the `skills` block of `faustus-plugin.json` holds what the app asks for by default. The Hub starts the MCP server from this folder's virtual environment, where the package is installed in editable mode (`python -m pip install -e . --no-deps`, which `scripts/start.ps1` repeats when it is missing), so the program an agent connects to is the code in this repository and cannot fall behind it. The former apps that live on as modules answer to their old names through the same `compat_mcp` program (`compatibility/<id>/`), with their own skills: lumiere: `ffmpeg-video-editing`, `revision-aware-media`, `deterministic-motion-production`, `source-explainer-production`.

## QR films

Generation's **QR film / Corto QR** screen renders timed QR scenes on CPU. Each scene has `payload`, `duration_s` and optional `caption`. `studio_qr_video(project, scenes, aspect="9:16", fps=24, motion="pulse", name=...)` queues a normal resumable CPU job. Other aspects are `16:9` and `1:1`. Other motions are `still` and `orbit`. The shortest side is 720 pixels. QR correction is H, the white quiet zone is four modules and each module occupies at least four whole pixels. Durations round to the nearest frame, with at least one frame per scene. Use short captions and payloads that fit this raster.

The renderer extracts and independently decodes **every compressed MP4 frame**, checking the exact expected scene payload and total frame count before saving a normal library video asset. A failed check fails the render and retains a diagnostic report. `studio_qr_verify(asset_id)` checks any existing video and compares scene payloads when it carries a QR-film recipe. `studio_qr_report(job_id, offset=0, limit=50)` pages through per-frame hashes and payload indices. Set `limit=0` for its summary. The full JSON downloads from `/generation/api/qr/reports/{job_id}`. Reports include the video SHA-256, exact frame counts, unreadable/mismatched counts and decoded payloads. Verification describes the encoded file. Physical scanning also depends on display size and camera conditions. Ordinary editing or recompression requires another verification pass.
