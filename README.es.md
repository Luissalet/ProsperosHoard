<img src="app-icon.png" width="96" alt="">

# Prospero's Hoard
### Estamos hechos de la misma materia que los sueños: ¿puede un agente dirigir una producción entera?
**Un estudio multimedia local que maneja tu ComfyUI, ffmpeg y una voz sintética local para crear personajes coherentes, photocards, portadas y videoclips montados al ritmo, a mano o por completo desde MCP, y que recuerda exactamente cómo se hizo cada recurso.**

[English](README.md) · [Inicio rápido](#inicio-rápido) · [Conectar con Faustus](#conectarlo-a-faustus) · [Referencia MCP](docs/MCP.md) · [Portfolio](https://luissalet.github.io/Portfolio/#projects)

![Pantalla Generar: dos miembros del reparto mencionados con @, el prompt final con su aspecto insertado, el panel de parámetros y resultados anteriores](docs/media/01-generate.png)
*Aplicación real, datos de demostración sintéticos. Todas las imágenes salen del backend de demostración incluido, un sustituto procedural de ComfyUI que dibuja escenas de relleno rotuladas; con tu ComfyUI conectado, las mismas pantallas muestran resultados reales de los modelos.*

## Por qué

Un modelo de lenguaje al que se le pide ayuda con una pequeña producción
musical (un grupo inventado, sus photocards, una portada, un vídeo para el
single) solo puede describir lo que haría. No puede ejecutar un grafo de
nodos, no puede mantener la cara de un personaje en cuarenta imágenes, no
encuentra el pulso de una canción y nadie sabrá después qué semilla y qué
checkpoint hicieron esa tarjeta. Hacerlo a mano obliga a saltar entre un
editor de nodos, un editor de imagen, una herramienta de audio y un editor
de vídeo que no comparten memoria.

Prospero convierte cada paso en una operación tipada, encolable y con su
receta guardada: los personajes se mencionan como `@Nombre` y su
descripción se inserta cada vez, cada recurso conserva los parámetros
exactos que lo produjeron (y se puede repetir byte a byte en el mismo
backend), las canciones se analizan para sacar tempo y secciones, y un
montaje se corta al ritmo y se renderiza con ffmpeg. El modelo local dirige;
el estudio hace el trabajo y responde con identificadores e imágenes.

![Montaje: 23 planos cortados al ritmo de la canción de demostración, los subtítulos de la letra, la forma de onda, el render de vista previa y el editor de planos](docs/media/02-timeline.png)
*Aplicación real, datos de demostración sintéticos: el montaje automático de la canción de 30 s (120 BPM, suave-fuerte-suave), dos pulsos por plano en la parte fuerte, cuatro en las suaves, y la vista previa que renderizó con ffmpeg.*

## Casos de uso

- **Un músico independiente con un single terminado** importa la canción,
  deja que el detector de pulsos encuentre el tempo y las secciones,
  sincroniza la letra y obtiene una portada, una tarjeta de letra y un vídeo
  9:16 cortado al ritmo, sin abrir un editor de nodos ni uno de vídeo.
- **Una guionista o diseñadora de juegos con un reparto original** da a
  cada personaje un prompt de aspecto y una referencia canónica y pide
  «@Iris y @Mika entre bastidores»: vuelven las mismas caras en decenas de
  imágenes (`consistent=true`) y cada tarjeta dice qué semilla y qué
  checkpoint la hicieron.
- **Un modelo local en Faustus** al que se le pide «haz un teaser del
  single» llama a `studio_generate_image`, `studio_timeline` y
  `studio_render`, recibe identificadores cortos, solo mira una imagen cuando
  la pide y cada llamada que hace queda en **Actividad del asistente**.
- **Quien ya usa ComfyUI con sus propios flujos** importa la exportación en
  formato de interfaz, la ve convertida, comprobada contra la lista de nodos
  en vivo y con sus parámetros con nombre, y a partir de ahí genera con ella
  desde el estudio o desde un agente, con linaje.

## Qué hay implementado

| Área | Disponible ahora | Límite |
| --- | --- | --- |
| Proyectos y reparto | Proyectos, personajes (prompt de aspecto, negativo, paleta, referencia canónica, voz), grupos ordenados, menciones `@Nombre` que reconocen nombres de varias palabras y avisan de los desconocidos, 6 estilos predefinidos | Un único usuario local; los nombres no se pueden repetir en un proyecto (son la mención) |
| Generación (ComfyUI) | Qwen-Image 2.1 (int8) txt2img y edición multi-referencia (1 a 10 referencias, hasta 2K nativo), SDXL txt2img, img2img, inpaint y ampliación en dos pasadas («hires fix»), SD 1.5 txt2img, SVD imagen a vídeo, FLUX.1 schnell txt2img, FLUX.1 Kontext (edición guiada por referencia), Wan 2.2 TI2V imagen a vídeo, todo como plantillas en formato API, cada una con sus propios valores de sampler y tamaño; motor de imagen `auto \| qwen21 \| flux \| sdxl` por proyecto y por llamada («auto» usa Qwen-Image 2.1 si está instalado, si no Flux, si no SDXL, e indica siempre cuál usó); el prompt entero se comprueba contra `/object_info` antes de encolarlo (nodos, cada archivo de modelo, samplers, opciones, rangos), un archivo de modelo que falta se reporta como «descárgalo», nunca como un fallo, con las opciones instaladas en el error; importador de flujos en formato de interfaz **o** API cuyo conversor coincide entrada a entrada con la exportación del propio frontend de ComfyUI 0.37 en las plantillas oficiales (subgrafos y widgets promovidos, combos dinámicos, sockets autogrow, `PrimitiveNode`/`Reroute`, bypass/mute), con mapa de parámetros editable y una copia de la lista de nodos para cuando ComfyUI está apagado; `consistent=true` mantiene el diseño exacto de un `@Personaje` vía una plantilla de edición (Qwen-Image 2.1 o Kontext, según el motor) y su referencia canónica, recortada a una pose de la hoja de referencia | Prospero no aloja ningún modelo; Kontext admite una sola referencia (Qwen-Image 2.1 hasta 10), Wan solo imagen a vídeo |
| Uso compartido de la GPU | VRAM estimada por familia de flujo (editable), comparada con la tarjeta en la que corre de verdad ComfyUI (su propio `system_stats`, donde los modelos que tiene en caché cuentan como libres; nvidia-smi si no responde); si falta memoria, el trabajo espera en `waiting_gpu` con el motivo, reintentando cada 15 s hasta 30 min; se puede cancelar en cualquier momento | Nunca se descarga nada salvo que pulses «Liberar memoria de ComfyUI» |
| Linaje | Cada recurso generado guarda plantilla, hash de la plantilla, checkpoint, todos los parámetros y la semilla, entradas y tiempos; «Repetir receta» reproduce una imagen byte a byte en el mismo backend (probado), «Variar semilla» la repite con semillas nuevas | La reproducción solo está garantizada con el mismo backend, modelos y versión de ComfyUI |
| Diseño | Renderizador con Pillow, sin navegador: photocard anverso y reverso, portada de álbum (4 composiciones), cartel teaser, tarjeta de letra, contraportada con lista de canciones, miniatura, con una variante «night» de terror/thriller para portada, cartel, tarjeta de letra y contraportada; degradados, lámina holográfica, modos de fusión, viñeta, espaciado de letras, sombras, texto que se encoge para caber y columnas para la lista de canciones; sets de photocards de un grupo entero o de un solista en varios looks, con hoja de contactos; modo imprenta con 3 mm de sangrado a 300 ppp; 6 familias tipográficas incluidas | La capa QR dibuja un recuadro de relleno (no hay librería de QR fijada) |
| Audio | Importación (mp3, wav, flac, ogg, m4a), forma de onda, detector de pulsos propio (flujo espectral equilibrado por bandas, preferencia de tempo y programación dinámica) probado a menos de 1 BPM y 50 ms con metrónomos y patrones de bombo y caja de 90 a 140 BPM, estimación de tiempos fuertes y secciones, letras LRC con herramienta para sincronizarlas pulsando una tecla y una primera sincronización automática a partir de las etiquetas `[Section]` de la letra y los compases | La sincronización automática es una estimación por estructura, no alineación con la voz; las secciones se llaman «section A/B» con energía baja/media/alta, no estrofa/estribillo; las canciones muy rápidas (170 BPM) se detectan a la mitad |
| Voces | Piper TTS con seis voces seleccionadas en español e inglés que se descargan la primera vez que se usan; TTS de Faustus mediante Hoard Link con Piper como respaldo; voz y velocidad por personaje | Solo voces sintéticas genéricas: no se clona la voz de nadie |
| Generación de música | `studio_compose` (etiquetas, letra, bpm, tonalidad, idioma) vía ACE-Step 1.5 en ComfyUI (`ComfyMusic`, se activa solo al instalar el checkpoint) o una API HTTP mínima documentada para otro servidor local; las canciones compuestas guardan linaje y se analizan automáticamente | Necesita el checkpoint de ACE-Step en ComfyUI; aún no se ha compuesto ninguna canción en una GPU real; las canciones importadas funcionan del todo igualmente |
| Vídeo | Montaje automático al ritmo (densidad según la energía - según las marcas de estrofa/estribillo de la letra cuando las hay - destellos al inicio de cada frase musical, plano nuevo en cada sección y, si se pide, en cada verso cantado, guiones por sección en orden de historia, sin repetir plano seguido, cubre la canción entera) en un montaje editable; renderizador ffmpeg con movimientos Ken Burns, transiciones de corte, fundido, fundido a negro y destello que mantienen los cortes en el pulso, subtítulos de la letra incrustados con karaoke opcional y la canción mezclada; vista previa a 540p o final a 1080p; los clips de SVD/Wan se convierten a mp4; acabado opcional (gradación de color, grano, viñeta, barras de cine, destellos glitch en los tiempos fuertes, estilo de letra en mayúsculas condensadas para terror) | El Ken Burns es un rango de zoom más una dirección de desplazamiento, no rectángulos libres de inicio y fin; las gradaciones de color son aproximaciones con `eq`/`colorbalance`/`curves`, no una LUT 3D |
| Control por agentes | 22 herramientas MCP equivalentes a `/api/agent/*`, resultados compactos con identificadores, imágenes solo cuando se piden explícitamente (`include_image=true`), errores con código y siguiente paso, y un registro auditable «Lo que hizo el asistente» | Los trabajos se consultan (`studio_job` puede esperar en el servidor); no hay eventos push |
| Interfaz | Estudio en React: Resumen, Reparto, Generar, Biblioteca con visor, Diseño, Audio, Montaje, Tableros, Trabajos, Backends, Actividad del asistente y Ajustes; tema oscuro y claro, español e inglés, atajos de teclado | El montaje se edita por planos (duración, transición, cámara, orden, sustitución), no fotograma a fotograma |

![Visor de la Biblioteca con el set de photocards: diez tarjetas y el panel de receta con repetir, variar, ampliar y animar](docs/media/03-photocards.png)
*Aplicación real, datos de demostración sintéticos: el set de photocards de los cinco miembros inventados, abierto en el visor con su receta y sus entradas.*

## Modelos

| Familia | Checkpoint / archivos | VRAM (aprox.) | Mejor para |
| --- | --- | --- | --- |
| Qwen-Image 2.1 (int8) | `qwen_image_2.1_int8_convrot.safetensors` (difusión), `qwen3vl_8b_int8_convrot.safetensors` (encoder de texto), `qwen_image_2.1_vae_bf16.safetensors` (VAE) | ~7,3 GB + 9,4 GB cargados uno tras otro; pico ~10-12 GB a 1 MP, más a 2K nativo | Mejor fidelidad al prompt, tipografía dentro de la imagen, identidad multi-referencia (1-10 imágenes) |
| FLUX.1 schnell | `flux1-schnell-fp8.safetensors` | ~13 GB | Bocetos más rápidos (4 pasos) |
| FLUX.1 Kontext dev | `flux1-dev-kontext_fp8_scaled.safetensors` + CLIP/VAE | ~13 GB | Edición con una sola referencia |
| SDXL / SD 1.5 | `sd_xl_base_1.0.safetensors` / `v1-5-pruned-emaonly-fp16.safetensors` | ~7 GB / ~3,5 GB | Alternativa siempre disponible, boceto con poca VRAM |
| SVD | `svd_xt.safetensors` | ~10 GB | Imagen a vídeo corto |
| Wan 2.2 TI2V (5B) | `wan2.2_ti2v_5B_fp16.safetensors` + VAE | ~12 GB | Imagen a vídeo, 1280x704 nativo |
| ACE-Step 1.5 | `ace_step_1.5_turbo_aio.safetensors` | ~8 GB | Composición de canciones con voz |

El parámetro `engine` de `studio_generate_image` (y el ajuste `image_engine`
de cada proyecto) elige entre las familias de imagen: `auto` (por defecto)
usa Qwen-Image 2.1 si sus clases de nodo y sus archivos de modelo están
instalados, si no Flux schnell, si no SDXL - el resultado siempre dice cuál
usó realmente. Un `template` explícito siempre gana a `engine` cuando se
dan los dos. Cómo el conversor pasa la exportación en formato de interfaz
de cada plantilla al formato API de arriba, y cómo se reporta un archivo de
modelo que falta: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Inicio rápido

```
git clone https://github.com/Luissalet/ProsperosHoard.git
cd ProsperosHoard
```

Hace falta Python 3.11 o posterior (se recomienda 3.13), Node.js 22 para la
interfaz y ffmpeg en el PATH (si no, se usa el binario incluido en
`imageio-ffmpeg`). ComfyUI es opcional: `--demo` lo ejecuta todo contra un
sustituto procedural.

### Windows

Haz doble clic en **`Iniciar Prospero's Hoard.cmd`** (o ejecuta
`scripts\start.ps1`). La primera vez crea `.venv` con Python 3.13, instala
`requirements-lock.txt`, compila la interfaz con npm y abre
http://127.0.0.1:8815; las siguientes veces solo reinstala si cambió el
archivo de bloqueo. **`Detener Prospero's Hoard.cmd`** la para (solo después
de comprobar que el puerto es de verdad de Prospero).

Pasos manuales:

```powershell
C:\Python313\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
cd frontend; npm ci; npm run build; cd ..
.venv\Scripts\python.exe -m prosperos_hoard            # datos reales en .\data
.venv\Scripts\python.exe -m prosperos_hoard --demo     # datos de demostración en .\data-demo
```

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
(cd frontend && npm ci && npm run build)
.venv/bin/python -m prosperos_hoard --demo --no-browser
```

La primera ejecución con `--demo` prepara sus datos (alrededor de un minuto
con dos CPU) antes de que el servidor responda. Después abre
<http://127.0.0.1:8815> (`curl http://127.0.0.1:8815/api/health` responde
`"service": "prosperos-hoard"`).

Opciones: `--port`, `--data-dir` (o `PROSPERO_DATA_DIR`), `--demo` y
`--no-browser`. `--demo` arranca el backend procedural de demostración y
crea un grupo original de cinco miembros con retratos, fotos en el
escenario, una portada, un set de photocards, una canción sintética de 30 s
con la letra sincronizada y un montaje automático, y renderiza su vista
previa. Para usar tu ComfyUI, déjalo en 127.0.0.1:8188 (Hoard Link lo
encuentra) o pon su URL en Ajustes. Si la GPU la comparte un modelo de
lenguaje, los renders van mucho más lentos; `PROSPERO_COMFY_TIMEOUT_S` sube
cuánto puede tardar un trabajo (por defecto: vídeo 1 h, audio 30 min, imagen 20 min).

![Pantalla de audio: la canción de demostración a 120 BPM con sus pulsos y secciones A/B/A, la herramienta para sincronizar la letra y las frases habladas](docs/media/04-audio.png)
*Aplicación real, datos de demostración sintéticos: la canción sintética analizada con el detector de pulsos integrado, con la estimación de secciones y la letra LRC sincronizada.*

## Conectarlo a Faustus

Prospero's Hoard es un plugin de [Faustus](https://github.com/Luissalet/Faustus),
el espacio de trabajo de IA local, y se declara con
[`faustus-plugin.json`](faustus-plugin.json). Arráncala y, en Faustus, abre **Connectors -> Nearby apps -> Add**:
Faustus la encuentra en el puerto 8815, lee el manifiesto y lanza el
adaptador MCP (`prosperos_hoard/mcp_server.py`, stdio). El backend de
modelos es compartido: el estudio pregunta a Hoard Link qué ComfyUI y qué
TTS están ya en marcha en vez de cargar nada propio.

### Herramientas MCP

| Herramienta | Qué hace | Solo lectura |
| --- | --- | --- |
| `studio_status` | Backends, checkpoints, VRAM libre, cola | sí |
| `studio_projects` / `studio_create_project` | Listar o crear producciones | sí / no |
| `studio_cast` | Listar, crear y editar personajes y grupos | no (listar sí lo es) |
| `studio_generate_image` | Encolar txt2img/edición con @menciones, estilos y motor de imagen | no |
| `studio_edit_image` | img2img, inpaint, ampliar, repetir receta, variar semilla | no |
| `studio_animate` | Imagen a vídeo corto (SVD) | no |
| `studio_compose` | Componer una canción con voz (ACE-Step) | no |
| `studio_voice` | Frase hablada con la voz de un personaje | no |
| `studio_import` | Importar un archivo local de una carpeta permitida | no |
| `studio_analyze_audio` | Tempo, pulsos y secciones | sí |
| `studio_design` / `studio_photocard_set` | Renderizar un diseño / un set de photocards | no |
| `studio_timeline` / `studio_render` | Montaje automático, lectura y edición / renderizado | no |
| `studio_jobs` / `studio_job` / `studio_cancel_job` | Cola, un trabajo (con espera), cancelar | sí / sí / no |
| `studio_assets` / `studio_show` / `studio_lineage` | Buscar recursos, verlos y su receta | sí |

También funciona con cualquier cliente MCP por stdio:

```json
{"mcpServers": {"prosperos-hoard": {
  "command": "C:/.../Prospero's Hoard/.venv/Scripts/python.exe",
  "args": ["C:/.../Prospero's Hoard/prosperos_hoard/mcp_server.py"],
  "env": {"PROSPERO_URL": "http://127.0.0.1:8815"}}}}
```

Argumentos, formato de las respuestas y límites de cada herramienta:
[docs/MCP.md](docs/MCP.md). La receta completa que sigue el agente:
[skills/idol-production/SKILL.md](skills/idol-production/SKILL.md).

## Modelos compartidos (HoardLink)

Prospero no aloja ningún modelo. Pide a [HoardLink](https://github.com/Luissalet/HoardLink)
(incluido en [`prosperos_hoard/hoard_link/`](prosperos_hoard/hoard_link),
copia byte a byte de HoardLink 0.1.1) el backend de `image`/`video`
(ComfyUI) y de `tts` (el TTS de Faustus, con Piper como alternativa local),
el mismo resolutor que usan todos los plugins de Faustus. El orden de
resolución, en una línea: un ajuste explícito en Ajustes, en
`data/backend.json` o en una variable de entorno `HOARD_*`; después el
registro de una instancia de Faustus en marcha; después un servidor que ya
escuche en loopback (ComfyUI en el 8188). La música sale de ACE-Step a
través del mismo ComfyUI o de un pequeño servidor HTTP documentado al que
apuntes con `HOARD_MUSIC_URL`. La pantalla **Backends** y `studio_status`
dicen siempre qué se ha encontrado y por qué; nada se carga ni se descarga
a tus espaldas.

## Ejemplo de producción

[`scripts/productions/no_mires_atras.py`](scripts/productions/no_mires_atras.py)
produce un sencillo, «NO MIRES ATRÁS» de FAROL (una criatura nocturna
original: cabeza de farolillo de papel, siempre quieta, siempre un poco más
cerca), de principio a fin **solo a través del adaptador MCP**: lanza
`mcp_server.py` por stdio, el mismo camino que usa Faustus, y falla si algún
resultado trae una imagen que no pidió. Nueve pasos: el proyecto (con un
motor `--engine auto|qwen21|flux`, guardado como su valor por defecto); una
hoja de referencia recortada a su vista frontal como referencia canónica de
FAROL; la canción con ACE-Step; doce fotogramas (una plantilla de edición
con esa referencia cuando FAROL sale en plano, un txt2img nuevo con la
misma estética nocturna cuando no - Qwen-Image 2.1 si está instalado, si no
Flux); clips de Wan a partir de los mejores fotogramas; un set de
photocards de solista en cinco looks idol con su hoja de contactos; la
portada, contraportada, cartel teaser y tarjeta de letra en variante
«night»; la letra sincronizada con los compases; montajes 9:16 y 16:9 que
siguen un guion por sección y cambian de plano en cada verso cantado, con
gradación sodium-night, grano, viñeta y destello + glitch RGB en los
tiempos fuertes del estribillo, y subtítulos karaoke de terror; y un
`REPORT.md` con cada id de recurso, los tiempos y qué revisar. Guarda cada
fotograma, clip, tarjeta y render según termina, así que una ejecución
interrumpida sigue donde se quedó; `--only <paso>` repite un paso.

En Windows, con la aplicación en marcha y ComfyUI arrancado en una tarjeta
de 16 GB:

```powershell
# el backend de demostración (sin GPU), unos minutos
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend fake --quality draft
# la producción real contra la aplicación y ComfyUI en marcha
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final
# fijar el motor de imagen en vez de dejar que «auto» use Qwen-Image 2.1 primero
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --engine flux
# tras resincronizar la letra de oído en Audio > Sincronizar letra y exportar el LRC
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --only timeline --lrc-path C:\Users\<tu-usuario>\Music\no_mires_atras.lrc
```

Qué se ha ejecutado y qué no: la producción completa se ejecutó en una
máquina Linux sin GPU con `--backend fake --engine auto`, el sustituto
procedural que usa `--demo`, así que sus imágenes, clips y canción son
marcadores de posición etiquetados, no salida de Qwen-Image 2.1, Flux,
Kontext, Wan ni ACE-Step (el backend falso reporta Qwen-Image 2.1 como
instalado, así que «auto» usó `qwen21_txt2img` para la hoja de referencia y
`qwen21_edit` para los fotogramas de FAROL - confirmado en el
`recipe.image_engine` de cada recurso). Lo que sí demuestra es todo lo que
decide el propio Prospero: cada paso termina y se produce cada tipo de
recurso; composiciones, tipografía, gradación, grano, glitch, ritmo de
corte, guion y sincronización del karaoke se renderizan como deben (revisar
esos fotogramas es lo que trajo las variantes «night», el rediseño de los
subtítulos y la corrección de las transiciones al fotograma). El ComfyUI
falso sirve la lista de nodos de una instalación real de ComfyUI 0.37 y
rechaza cualquier prompt que rechazaría el servidor real, y las seis
plantillas se han comparado entrada a entrada con lo que exporta el
frontend real, pero **aún no ha habido ninguna ejecución en una GPU
real**: ese es el siguiente paso, con el mismo script.

## Arquitectura

FastAPI y SQLite (WAL, una conexión por hilo) con un hilo de trabajo para la
GPU y otro para la CPU sobre una tabla de trabajos persistente; la lógica
vive en módulos sin dependencias web (`engine`, `comfy_driver`, `design`,
`audio`, `timeline`, `video`, `voices`); Hoard Link va incluido para resolver
los backends; el adaptador MCP es un script stdio aparte que solo habla HTTP
con la aplicación.

```mermaid
flowchart LR
  UI["Interfaz React"] -->|"/api/*"| API["Aplicación FastAPI<br/>127.0.0.1:8815"]
  MCP["Adaptador MCP stdio"] -->|"/api/agent/*"| API
  API --> DB[("SQLite<br/>proyectos, recursos, linaje, trabajos")]
  API --> JOBS["Hilos de trabajo GPU + CPU"]
  JOBS --> COMFY["ComfyUI<br/>(imagen, vídeo, música)"]
  JOBS --> FF["ffmpeg<br/>(renders, acabado)"]
  API --> DESIGN["Diseñador con Pillow"]
  API --> AUDIO["Detector de pulsos"]
  API --> LINK["HoardLink"] -. "resuelve" .-> COMFY
  LINK -. "opcional" .-> TTS["TTS de Faustus / Piper"]
```

Detalles, modelo de datos y decisiones:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Todos los endpoints:
[docs/API.md](docs/API.md).

## Privacidad y seguridad

Todo en local: la aplicación escucha en 127.0.0.1, no envía telemetría y
solo sale a internet cuando pides una voz de Piper que aún no está
descargada (del repositorio rhasspy/piper-voices en Hugging Face). Se
rechazan las peticiones con una cabecera `Host` ajena (DNS rebinding) y las
escrituras que llegan desde otras webs (comprobación de `Origin` y
`Sec-Fetch-Site`). Los agentes solo pueden importar archivos de tu carpeta
personal, de `data/inbox` y de las carpetas que añadas en Ajustes, y solo si
el contenido coincide con su tipo; los archivos se sirven siempre por id,
nunca por una ruta que mande el cliente. Cada llamada que hace un asistente
se guarda en la tabla de auditoría `agent_calls` y se ve en **Actividad del
asistente** (herramienta, resumen de argumentos, duración y resultado). No
hay clonación de voz y la demostración usa únicamente personas inventadas.
Los datos se quedan en `data/` (o en tu `--data-dir`); el token de Faustus
se guarda en `data/backend.json` y la API nunca lo devuelve.

## Desarrollo

```powershell
.venv\Scripts\python.exe -m pytest -q
cd frontend; npm ci; npm run build
```

En Linux/macOS, lo mismo con `.venv/bin/python`. **Pasan 189 pruebas**
en unos dos minutos en una máquina Linux compartida de 2 CPU, sin red, sin GPU
y sin descargar modelos (el backend de demostración sustituye a ComfyUI). Cubren el
protocolo MCP de principio a fin (el adaptador lanzado por stdio contra la
aplicación en marcha: palabras clave y anotaciones de cada herramienta,
generación con imagen, linaje, diseño, errores legibles y el mensaje de
aplicación apagada); el conversor de flujos de formato de interfaz a API
contra siete plantillas oficiales de ComfyUI (ComfyUI 0.37, incluidas las
dos de Qwen-Image 2.1, sus subgrafos de widgets promovidos, combos
dinámicos y sockets autogrow), comparado entrada a entrada con lo que
exporta el frontend real (subgrafos y widgets promovidos, combos
dinámicos, entradas autogrow, bypass/mute, `PrimitiveNode`/`Reroute`) y la
importación de exportaciones de interfaz por la API con y sin ComfyUI; las
seis plantillas convertidas así (Flux schnell, edición Kontext, Wan 2.2
TI2V, canción ACE-Step, Qwen-Image 2.1 txt2img y edición) pasando la
validación del servidor con sus propios valores - incluido que un archivo
de modelo ausente de `/object_info` se reporta como «descárgalo», nunca
como un fallo - y generando contra el `/object_info` real del backend
falso; el motor de imagen `auto|qwen21|flux|sdxl` (detección de modelo
instalado, valor por defecto por proyecto, anulación por llamada, reserva
elegante) y la edición multi-referencia de Qwen-Image 2.1 (1-10 imágenes,
los nodos de referencia extra conectados o retirados según haga falta); la
sincronización de letras por secciones, los guiones por sección y el corte
por verso; un render largo con transiciones que debe conservar su
duración; el enrutado de consistencia de
personaje y su error `consistent_needs_reference`; los grafos de filtro del
acabado (gradación de color, grano, viñeta, barras, glitch) probados por
comparación exacta más un renderizado real con ffmpeg; la reproducción byte
a byte con «repetir receta»; casos límite de las @menciones; la validación
de checkpoints, samplers y nodos que faltan; importaciones hostiles de
flujos; rutas con `..`, enlaces
simbólicos, archivos renombrados y carpetas permitidas al importar; rutas
maliciosas contra la SPA y los recursos; la protección contra ataques desde
el navegador; la recuperación de trabajos tras un reinicio, la espera de
VRAM y la cancelación; el detector de pulsos con metrónomos, patrones de
batería y la canción de demostración; las invariantes del montaje
automático; el escapado ASS de letras hostiles; renders reales con ffmpeg
en una carpeta llamada como la instalación de Windows (apóstrofo, espacios y
tildes); hashes de referencia del diseño, sangrado y ajuste de texto; el uso
del almacén desde varios hilos y la comprobación del manifiesto de Faustus.

`npm run build` en `frontend/` termina sin errores de TypeScript. La
[CI](.github/workflows/ci.yml) ejecuta las pruebas en Ubuntu y Windows con
Python 3.11, 3.12 y 3.13 y compila la interfaz con Node.js 22.

## Hoja de ruta y límites conocidos

- **Aún no hay ninguna ejecución en una GPU real**: las plantillas se
  validan contra la lista de nodos de un ComfyUI 0.37 real y el conversor
  coincide con la exportación del frontend real, pero desde este repositorio
  todavía no se ha generado ninguna imagen, clip ni canción en una GPU. El
  ejemplo de producción es el script para esa primera ejecución.
- `consistent=true` mantiene el diseño de un personaje a partir de su
  referencia canónica; Kontext admite una sola referencia (Qwen-Image 2.1
  hasta 10) y Wan solo hace imagen a vídeo.
- La sincronización automática de la letra es una estimación a partir de la
  estructura de la canción, no una alineación con la voz; para el montaje
  final, resincronízala de oído en **Audio > Sincronizar letra**.
- Las secciones se llaman «section A/B» con un nivel de energía, no
  estrofa/estribillo; las canciones muy rápidas (unos 170 BPM) se detectan a
  la mitad.
- Los trabajos se consultan (`studio_job` puede esperar en el servidor); no
  hay eventos push.
- El montaje se edita por planos, el Ken Burns es un rango de zoom más una
  dirección de desplazamiento y las gradaciones de color son aproximaciones
  con filtros, no LUT 3D.
- La capa QR del diseñador dibuja un recuadro de relleno.
- Aún sin probar: una ejecución completa en Windows contra un ComfyUI real
  con GPU.

## Licencia

MIT - ver [LICENSE](LICENSE). Las tipografías incluidas conservan sus
licencias: SIL Open Font License (`prosperos_hoard/fonts/*/OFL.txt`), salvo
Special Elite (Apache License 2.0, `prosperos_hoard/fonts/SpecialElite/LICENSE.txt`).
