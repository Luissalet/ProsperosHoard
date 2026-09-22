<img src="app-icon.png" width="28" height="28" alt="" align="left" />

# Prospero's Hoard
### Estamos hechos de la misma materia que los sueños: ¿puede un agente dirigir una producción entera?
**Un estudio multimedia local que maneja tu ComfyUI, ffmpeg y una voz sintética local para crear personajes coherentes, photocards, portadas y videoclips montados al ritmo, a mano o por completo desde MCP, y que recuerda exactamente cómo se hizo cada recurso.**

[English](README.md) · [Ejecutar en local](#ejecutar-en-local-en-windows) · [Conectar una IA](docs/MCP.md) · [Portfolio](https://luissalet.github.io/Portfolio/#projects)

![Pantalla Generar: dos miembros del reparto mencionados con @, el prompt final con su aspecto insertado, el panel de parámetros y resultados anteriores](docs/media/01-generate.png)
*Aplicación real, datos de demostración. Todas las imágenes salen del backend de demostración incluido, un sustituto procedural de ComfyUI que dibuja escenas de relleno rotuladas; con tu ComfyUI conectado, las mismas pantallas muestran resultados reales de SDXL/SD1.5.*

## Por qué

Un modelo de lenguaje al que se le pide ayuda con una producción de fans
(un grupo idol inventado, sus photocards, una portada, un vídeo para el
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
*Aplicación real, datos de demostración: el montaje automático de la canción de 30 s (120 BPM, suave-fuerte-suave), dos pulsos por plano en la parte fuerte, cuatro en las suaves, y la vista previa que renderizó con ffmpeg.*

## Qué hay implementado

| Área | Disponible ahora | Límite |
| --- | --- | --- |
| Proyectos y reparto | Proyectos, personajes (prompt de aspecto, negativo, paleta, referencia canónica, voz), grupos ordenados, menciones `@Nombre` que reconocen nombres de varias palabras y avisan de los desconocidos, 6 estilos predefinidos | Un único usuario local; los nombres no se pueden repetir en un proyecto (son la mención) |
| Generación (ComfyUI) | SDXL txt2img, img2img, inpaint y ampliación en dos pasadas («hires fix»), SD 1.5 txt2img, SVD imagen a vídeo, FLUX.1 schnell txt2img, FLUX.1 Kontext (edición guiada por referencia), Wan 2.2 TI2V imagen a vídeo, todo como plantillas en formato API; comprobación previa de nodos, checkpoints, samplers y schedulers contra `/object_info`, con las opciones instaladas en el error; conversor/importador de flujos en formato de interfaz **o** API (subgrafos, `PrimitiveNode`/`Reroute`, bypass/mute) con mapa de parámetros editable; `consistent=true` mantiene el diseño exacto de un `@Personaje` vía Kontext y su referencia canónica | Prospero no aloja ningún modelo; Kontext admite una sola referencia, Wan solo imagen a vídeo |
| Uso compartido de la GPU | VRAM estimada por familia de flujo (editable), comparada con nvidia-smi o con `system_stats` de ComfyUI; si falta memoria, el trabajo espera en `waiting_gpu` con el motivo, reintentando cada 15 s hasta 30 min; se puede cancelar en cualquier momento | Nunca se descarga nada salvo que pulses «Liberar memoria de ComfyUI» |
| Linaje | Cada recurso generado guarda plantilla, hash de la plantilla, checkpoint, todos los parámetros y la semilla, entradas y tiempos; «Repetir receta» reproduce una imagen byte a byte en el mismo backend (probado), «Variar semilla» la repite con semillas nuevas | La reproducción solo está garantizada con el mismo backend, modelos y versión de ComfyUI |
| Diseño | Renderizador con Pillow, sin navegador: photocard anverso y reverso, portada de álbum (3 composiciones), cartel teaser, tarjeta de letra, contraportada con lista de canciones, miniatura; degradados, lámina holográfica, modos de fusión, espaciado de letras, sombras y texto que se encoge para caber; sets de photocards de un grupo entero con hoja de contactos; modo imprenta con 3 mm de sangrado a 300 ppp; 5 familias tipográficas OFL incluidas | La capa QR dibuja un recuadro de relleno (no hay librería de QR fijada) |
| Audio | Importación (mp3, wav, flac, ogg, m4a), forma de onda, detector de pulsos propio (flujo espectral equilibrado por bandas, preferencia de tempo y programación dinámica) probado a menos de 1 BPM y 50 ms con metrónomos y patrones de bombo y caja de 90 a 140 BPM, estimación de tiempos fuertes y secciones, letras LRC con herramienta para sincronizarlas pulsando una tecla | Las secciones se llaman «section A/B» con energía baja/media/alta, no estrofa/estribillo; las canciones muy rápidas (170 BPM) se detectan a la mitad |
| Voces | Piper TTS con seis voces seleccionadas en español e inglés que se descargan la primera vez que se usan; TTS de Faustus mediante Hoard Link con Piper como respaldo; voz y velocidad por personaje | Solo voces sintéticas genéricas: no se clona la voz de nadie |
| Generación de música | `studio_compose` (etiquetas, letra, bpm, tonalidad, idioma) vía ACE-Step 1.5 en ComfyUI (`ComfyMusic`, se activa solo al instalar el checkpoint) o un contrato HTTP mínimo documentado para otro servidor local; las canciones compuestas guardan linaje y se analizan automáticamente | Ninguno de los dos viene instalado por defecto; las canciones importadas funcionan del todo igualmente |
| Vídeo | Montaje automático al ritmo (densidad según la energía, destellos al inicio de cada frase musical, sin repetir plano seguido, cubre la canción entera) en un montaje editable; renderizador ffmpeg con movimientos Ken Burns, transiciones de corte, fundido, fundido a negro y destello que mantienen los cortes en el pulso, subtítulos de la letra incrustados con karaoke opcional y la canción mezclada; vista previa a 540p o final a 1080p; los clips de SVD/Wan se convierten a mp4; acabado opcional (gradación de color, grano, viñeta, barras de cine, destellos glitch en los tiempos fuertes, estilo de letra en mayúsculas condensadas para terror) | El Ken Burns es un rango de zoom más una dirección de desplazamiento, no rectángulos libres de inicio y fin; las gradaciones de color son aproximaciones con `eq`/`colorbalance`/`curves`, no una LUT 3D |
| Control por agentes | 21 herramientas MCP equivalentes a `/api/agent/*`, resultados compactos con identificadores, imágenes solo cuando se piden explícitamente (`include_image=true`), errores con código y siguiente paso, y un registro auditable «Lo que hizo el asistente» | Los trabajos se consultan (`studio_job` puede esperar en el servidor); no hay eventos push |
| Interfaz | Estudio en React: Resumen, Reparto, Generar, Biblioteca con visor, Diseño, Audio, Montaje, Tableros, Trabajos, Backends, Actividad del asistente y Ajustes; tema oscuro y claro, español e inglés, atajos de teclado | El montaje se edita por planos (duración, transición, cámara, orden, sustitución), no fotograma a fotograma |

![Visor de la Biblioteca con el set de photocards: diez tarjetas y el panel de receta con repetir, variar, ampliar y animar](docs/media/03-photocards.png)
*Aplicación real, datos de demostración: el set de photocards de los cinco miembros inventados, abierto en el visor con su receta y sus entradas.*

## Conectarlo a Faustus

La aplicación se declara con [`faustus-plugin.json`](faustus-plugin.json).
Arráncala y, en Faustus, abre **Connectors -> Nearby apps -> Add**:
Faustus la encuentra en el puerto 8815, lee el manifiesto y lanza el
adaptador MCP (`prosperos_hoard/mcp_server.py`, stdio). El backend de
modelos es compartido: el estudio pregunta a Hoard Link qué ComfyUI y qué
TTS están ya en marcha en vez de cargar nada propio.

| Herramienta | Qué hace | Solo lectura |
| --- | --- | --- |
| `studio_status` | Backends, checkpoints, VRAM libre, cola | sí |
| `studio_projects` / `studio_create_project` | Listar o crear producciones | sí / no |
| `studio_cast` | Listar, crear y editar personajes y grupos | no (listar sí lo es) |
| `studio_generate_image` | Encolar txt2img/img2img con @menciones y estilos | no |
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

## Ejecutar en local en Windows

Haz doble clic en **`Iniciar Prospero's Hoard.cmd`** (o ejecuta
`scripts\start.ps1`). La primera vez crea `.venv` con Python 3.13, instala
`requirements-lock.txt`, compila la interfaz con npm y abre
http://127.0.0.1:8815. `Detener Prospero's Hoard.cmd` la para (solo después
de comprobar que el puerto es de verdad de Prospero).

Pasos manuales:

```powershell
C:\Python313\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
cd frontend; npm ci; npm run build; cd ..
.venv\Scripts\python.exe -m prosperos_hoard            # datos reales en .\data
.venv\Scripts\python.exe -m prosperos_hoard --demo     # datos de demostración en .\data-demo
```

Opciones: `--port`, `--data-dir` (o `PROSPERO_DATA_DIR`), `--demo` y
`--no-browser`. `--demo` arranca el backend procedural de demostración y
crea un grupo original de cinco miembros con retratos, fotos en el
escenario, una portada, un set de photocards, una canción sintética de 30 s
con la letra sincronizada y un montaje automático, y renderiza su vista
previa. Para usar tu ComfyUI, déjalo en 127.0.0.1:8188 (Hoard Link lo
encuentra) o pon su URL en Ajustes.

![Pantalla de audio: la canción de demostración a 120 BPM con sus pulsos y secciones A/B/A, la herramienta para sincronizar la letra y las frases habladas](docs/media/04-audio.png)
*Aplicación real, datos de demostración: la canción sintética analizada con el detector de pulsos integrado, con la estimación de secciones y la letra LRC sincronizada.*

## Ejemplo de producción

[`scripts/productions/no_mires_atras.py`](scripts/productions/no_mires_atras.py)
dirige un sencillo completo de principio a fin **solo a través del
adaptador MCP** (lanza `mcp_server.py` por stdio, el mismo camino que usa
Faustus): un proyecto, un personaje original consistente, una canción
compuesta, doce fotogramas, varios clips de imagen a vídeo con Wan, un set
de photocards estilo idol, las artes del álbum y un montaje sincronizado
al ritmo, con gradación de color, renderizado a mp4 - terminando con un
`REPORT.md` que lista cada id de recurso y qué revisar.

```powershell
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend fake --quality draft
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --lrc-path C:\letra.lrc
```

Con honestidad sobre qué se ejecutó y dónde: la ejecución sin GPU de
este propio repositorio usó `--backend fake` (el mismo sustituto
procedural que usa la opción `--demo`), por lo que sus imágenes llevan la
etiqueta de marcador de posición en vez de una salida real de
Flux/Kontext/Wan/ACE-Step. Aun así demuestra toda la tubería: cada paso
termina, se produce cada tipo de recurso, el montaje se renderiza con su
gradación de color, grano, viñeta y destellos de glitch, y la letra se
graba a tiempo. La ejecución real - con el ComfyUI del dueño y los
checkpoints de Flux, Kontext, Wan y ACE-Step que ya tiene en disco - está
pendiente de sus GPUs; `--backend real` dirige el mismo script contra
ella en cuanto ComfyUI esté arriba, sin cambiar nada de código. El script
es idempotente (`--only <paso>` repite un paso; borra su `state.json` para
empezar de cero) y nunca nombra otro producto o franquicia, ni en sus
prompts ni en su salida.

## Arquitectura

FastAPI y SQLite (WAL, una conexión por hilo) con un hilo de trabajo para la
GPU y otro para la CPU sobre una tabla de trabajos persistente; la lógica
vive en módulos sin dependencias web (`engine`, `comfy_driver`, `design`,
`audio`, `timeline`, `video`, `voices`); Hoard Link va incluido para resolver
los backends; el adaptador MCP es un script stdio aparte que solo habla HTTP
con la aplicación. Detalles, modelo de datos y decisiones:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Todos los endpoints:
[docs/API.md](docs/API.md).

## Pruebas

```powershell
.venv\Scripts\python.exe -m pytest -q
cd frontend; npm ci; npm run build
```

La última ejecución completa: **139 pruebas superadas** en unos 90 s,
sin red, con el backend de demostración en lugar de ComfyUI. Cubren: el
protocolo MCP de principio a fin (el adaptador lanzado por stdio contra la
aplicación en marcha: palabras clave y anotaciones de cada herramienta,
generación con imagen, linaje, diseño, errores legibles y el mensaje de
aplicación apagada); el conversor de flujos de formato de interfaz a API
contra las cinco plantillas oficiales de ComfyUI (subgrafos, bypass/mute,
`PrimitiveNode`/`Reroute`); las cuatro plantillas nuevas (Flux schnell,
edición Kontext, Wan 2.2 TI2V, canción ACE-Step) generando contra el
`/object_info` real del backend falso; el enrutado de consistencia de
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

## Privacidad y límites

Todo en local: la aplicación escucha en 127.0.0.1, no envía telemetría y
solo sale a internet cuando pides una voz de Piper que aún no está
descargada (del repositorio rhasspy/piper-voices en Hugging Face). Se
rechazan las escrituras que llegan desde otras webs (comprobación de Host,
Origin y Sec-Fetch-Site). Los agentes solo pueden importar archivos de tu
carpeta personal, de `data/inbox` y de las carpetas que añadas en Ajustes, y
solo si el contenido coincide con su tipo. No hay clonación de voz y la
demostración usa únicamente personas inventadas. Los datos se quedan en
`data/` (o en tu `--data-dir`); el token de Faustus se guarda en
`data/backend.json` y la API nunca lo devuelve.

Licencia MIT. Tipografías con licencia SIL Open Font License (ver `prosperos_hoard/fonts/*/OFL.txt`).
