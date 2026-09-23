# Producción de ejemplo: «DON'T LOOK BACK» de FAROL

[English](no-mires-atras.md) · [Volver al README](../../README.es.md#ejemplo-de-producción)

Una canción de terror de dos minutos y medio con un personaje original,
hecha de principio a fin en una sola máquina local con
[`scripts/productions/no_mires_atras.py`](../../scripts/productions/no_mires_atras.py).
Tiene dos versiones que comparten todas las imágenes y clips: **DON'T LOOK
BACK**, un himno de terror cantado en inglés y contado por la criatura
(`--lang en --motion full`, la que se muestra aquí), y la primera toma,
**NO MIRES ATRÁS**, un rap de terror en español (la opción por defecto). La
inglesa se hizo con `--reuse-from` a partir de la española, así que solo se
rehicieron la canción, los clips que faltaban, los diseños y el montaje. El script maneja Prospero **solo a través de su adaptador MCP**, el mismo
camino que usa Faustus. Aquí está todo lo necesario para entenderlo o
repetirlo: la biblia del personaje, cada prompt y cada negativo, las
semillas, los ajustes de modelo y sampler, los tiempos reales y los
problemas que encontró la ejecución (todos corregidos en la app). Los
prompts van en inglés porque es lo que mejor entienden los modelos.

![Portada: primer plano de la cabeza-farolillo con el título en la capa tipográfica de Prospero](../media/farol/cover.jpg)

| | |
| --- | --- |
| Hardware | ComfyUI 0.37 en tarjetas de 16 GB (RTX 5060 Ti): una para la versión española y tres como grupo de render para la inglesa; Prospero, ffmpeg y el diseñador en la CPU |
| Modelo de imagen | Qwen-Image 2.1 (int8): la hoja de referencia, los fotogramas y las photocards; la plantilla de edición lleva la referencia de FAROL |
| Modelo de vídeo | Wan 2.2 TI2V 5B: 18 clips de 5 s a 1280×704, 24 fps (todos los planos, más segundos clips para las secciones que más se repiten) |
| Modelo de música | ACE-Step 1.5 turbo: cuatro tomas de 150 s para la versión inglesa y dos de 120 s para la española |
| Sincronía de la letra | marcas de tiempo por palabra de faster-whisper (`small`, fuera de Prospero), alineadas con la letra escrita e importadas como LRC |
| Tiempo real | hoja de referencia 4,7 min · 48 fotogramas 57 min · primeros 7 clips ≈ 70 min en una tarjeta · otros 11 clips ≈ 40 min en tres · cuatro tomas de canción 2 min · 5 photocards ≈ 10 min · diseño del álbum 5 s · dos montajes con vista previa y render final a 1080p ≈ 7 min |
| Elegido a ojo (y a oído) | la referencia canónica (hoja 4 de 4), la mejor de las tres variantes de cada uno de los 12 planos y la toma de la canción |

## 1. La biblia

**FAROL**: una criatura urbana de la noche. Muy alto y delgado (2,3 m),
hecho de papel negro mojado y alambre. Su cabeza es un viejo farolillo de
papel ovalado (washi, ámbar) con rasgones, con una llama de vela visible
dentro y una sonrisa torcida pintada en rojo desvaído. Tiene dedos largos
como varillas de paraguas dobladas y una gabardina oscura, rota y demasiado
larga. Nunca se le ve a mitad de paso: siempre quieto, siempre un poco más
cerca.

- Paleta: naranja sodio `#F28C28`, asfalto mojado `#1B1D22`, gris niebla `#8A9099`, rojo sonrisa desvaído `#A33A2E`.
- Tono: miedo callado, cine, 35 mm, poca profundidad de campo, lluvia, niebla, solo luz práctica.
- Escenario: una calle española vacía de noche, con farolas de sodio, asfalto mojado y ningún cartel ni marca legible.
- Contraste: un set de photocards de idol, con la misma criatura en una sesión de estudio pastel y brillante.

## 2. Personaje: hoja de referencia → referencia canónica

Cuatro semillas (1001…1004) de una hoja de giro 16:9 con `qwen21_txt2img`.
La cuarta era la mejor: se ve la vela, está la sonrisa roja torcida y los
dedos parecen varillas de paraguas. Su tercio izquierdo (la vista frontal)
pasó a ser la referencia canónica de FAROL (`studio_cast update
canonical_asset_id + canonical_crop=left_third`). A partir de ahí, cada
imagen de FAROL es una edición desde ese recorte: `consistent=true` va a
`qwen21_edit` con la referencia como `image_1`.

Aspecto (todos los prompts de FAROL parten de él):

```text
FAROL, a very tall and thin urban night creature about 2.3 meters tall, made of wet black paper and wire,
its head an old oval paper street lantern (washi, amber) with rips, a candle flame visible inside, a crooked
smile painted in faded red on the paper, long fingers like bent umbrella ribs, a torn too-long dark raincoat,
never shown mid-stride, always still, always a little closer; palette sodium orange, wet asphalt dark grey,
fog grey, faded smile red; quiet dread, cinematic, 35mm, shallow depth of field, rain, fog, practical light only
```

Hoja: el aspecto y después `character turnaround reference sheet, three
full-body poses side by side on a neutral grey seamless studio backdrop:
front view, three-quarter view, back view, identical design and proportions
in every pose, even studio lighting, reference photography`.

Negativo para los planos sin FAROL (un txt2img no tiene referencia que
sostenga el aspecto): `cartoon, cute, bright daylight, sunny, comic style,
text, watermark, extra limbs, deformed hands`.

## 3. Canción

`studio_compose` con ACE-Step 1.5 turbo, cuatro semillas (2001…2004). El
single es la toma que canta la letra entera y en orden.

- Etiquetas: `electronic rock, horror anthem, catchy sung chorus, melodic male vocals, vocal harmonies, dark synth, distorted synth bass, driving drums, music box intro, eerie, dramatic, minor key, 130 bpm, english, storytelling, cinematic`.
- 130 bpm, re menor, 4/4, idioma `en`, 150 s.
- La letra (`SONG_LYRICS_EN` en el script) la canta el propio FAROL, al estilo de las canciones de fans sobre villanos de videojuegos de terror: el camino a casa contado por lo que te sigue. El gancho: *So don't look back, don't look back, / I'm the glow on the glass, I'm the light through the crack, / every step that you take, I'm a little more near, / when the streetlight dies, you'll find me here.*
- La toma española es otra canción: `dark trap, horror rap, eerie music box melody, detuned piano, heavy 808, half-time 140 bpm, whispered ad-libs, male rap vocals, spanish…`, 140 bpm, fa sostenido menor, 120 s (`SONG_LYRICS`). El primer intento en inglés también fue un rap. Sonaba a rap de calle más que a canción de terror, y se sustituyó por el himno.

Sincronía: `studio_time_lyrics`, de Prospero, estima cada verso a partir de
las secciones y los compases. Para el montaje final, cada toma se transcribió
con faster-whisper (`small`, con marcas por palabra), cada verso escrito se
emparejó con las palabras (difflib) y se guardó como LRC **con marcas de
`[Sección]` con tiempo**, y el resultado se importó
(`--only timeline --lrc-path <archivo>`). Eso sirvió también para comparar
las tomas: la elegida tenía 41 de sus 42 versos reconocidos, mientras que las
otras tres se saltaban trozos de la primera estrofa o metían coletillas que
no están en la letra. El estribillo entra en 0:46,7 y 1:44,6, y el puente
en 1:33,7.

Dos lecciones del alineador:

- **A whisper solo hay que darle el título como pista.** Whisper toma la pista como texto ya escuchado, así que darle los primeros versos le hacía saltárselos.
- **Hay que conservar las marcas de sección.** Sin ellas, el montaje automático usa secciones por energía, los grupos del guion nunca coinciden y cada sección saca planos de un único grupo barajado.

## 4. Fotogramas: 12 planos

Cada plano tiene tres variantes 16:9 a 1344×768 (semillas `3000 + 10·n`, +1 y
+2) y un post 4:5 (semilla `3000 + 10·n + 1`). Si FAROL sale en el plano, el
prompt es `@FAROL <plano>, <look de noche>` con `consistent=true`, es decir,
una edición de Qwen desde la referencia canónica. Si no sale, es `<plano>,
<look de noche>` por txt2img, con el negativo de arriba.

Look de noche, añadido a cada plano:

```text
cinematic 35mm film still, night, sodium-vapour street lamps, wet asphalt, fog, light rain, shallow depth
of field, practical light only, deep shadows, subtle film grain, no text, no readable signs, no logos
```

![El mejor fotograma de cada uno de los 12 planos](../media/farol/stills.jpg)

| # | FAROL | Prompt del plano |
| --- | --- | --- |
| 1 | sí | standing perfectly still under a far flickering sodium lamp at the end of an empty Spanish-style street at 2:15 a.m., seen small in the distance, fog |
| 2 | sí | seen over the shoulder of someone looking back down an empty wet street, standing under a closer sodium lamp, still, a little closer than before |
| 3 | sí | extreme close-up of the paper lantern head: the crooked painted smile, the candle flame inside, raindrops beading on the wet paper |
| 4 | no | close-up of a phone held in a trembling hand at night, the screen's cold glow on a wet frightened face, rainy street blurred behind, the screen itself blank |
| 5 | sí | reflected in the fogged glass of a night bus shelter, standing right beside the viewer's reflection, while the real bench beside the viewer is empty |
| 6 | sí | only its long paper fingers curling over a stairwell rail two floors up, seen from below, dim stairwell light |
| 7 | sí | seated perfectly still among empty plastic chairs in a laundromat at 3 a.m., washing machines spinning, flickering fluorescent tubes |
| 8 | sí | an amber lantern glow in an elevator mirror, just behind the viewer's shoulder, the lantern head barely visible |
| 9 | no | close-up of a wet doormat outside a flat door at night, a trail of wet footprints that are not human, long and thin like bent umbrella ribs, leading to the door |
| 10 | sí | seen from inside a dark flat through a rain-streaked window: standing on the empty street below, lantern lit, looking up at the window |
| 11 | sí | a quick fragmented insert: flickering sodium lamps, the crooked painted smile, long paper fingers and the candle flame, extreme close framing |
| 12 | no | a dark bedroom a second after the light switch was turned off, a hand still on the switch, the room slowly filling with a sodium-orange glow from the window |

Ajustes de la edición de Qwen: 25 pasos, cfg 1, euler / simple, denoise 1,
`resolution` 1024, `custom_size` activado con el lienzo explícito de
1344×768. Unos 70 s por imagen.

## 5. Clips: Wan 2.2 TI2V, todos los planos se mueven

Cada clip tiene 121 fotogramas a 24 fps (5,04 s), a 1280×704. Ajustes: 20
pasos, cfg 5, shift 8, uni_pc / simple, semilla `5000 + n` (`+100` en un
segundo clip). Unos 9,5 min por clip en una tarjeta.

- **Primera pasada (la versión española):** 7 clips, para los planos 1, 2, 3, 5, 7, 10 y 12. Con solo siete, la mayoría de los ~130 cortes eran fotogramas fijos con un zoom lento, y el vídeo parecía un pase de diapositivas.
- **Segunda pasada (`--motion full`):** 11 clips más: los cinco planos que no tenían (4, 6, 8, 9 y 11) y un segundo clip, hecho desde el segundo mejor fotograma, para los seis planos que más usa el montaje (1, 2, 3, 5, 10 y 11). El script los encola todos antes de esperar, y un grupo de render de tres servidores ComfyUI, uno por tarjeta de 16 GB, los sacó de tres en tres en unos 40 min.

<p><img src="../media/farol/clip-over-shoulder.gif" width="32%" alt="Clip 2: por encima del hombro, FAROL quieto bajo la farola más cercana, acercamiento lento">
<img src="../media/farol/clip-lantern.gif" width="32%" alt="Clip 3: la llama temblando dentro del farolillo, gotas en el papel">
<img src="../media/farol/clip-fingers.gif" width="32%" alt="Clip 6: los dedos largos de papel curvándose sobre la barandilla"></p>

| # | Prompt de movimiento |
| --- | --- |
| 1 | light rain falling, the far lamp flickering, fog drifting; the tall figure under the lamp stands perfectly still and does not walk |
| 2 | light rain falling, a subtle slow push-in; the figure under the lamp stays perfectly still |
| 3 | the candle flame flickering gently inside the lantern, raindrops sliding down the paper |
| 4 | the phone screen's cold glow flickering on the frightened wet face, the hand trembling, rain falling behind |
| 5 | rain streaking down the glass, a slow push-in; the reflected figure stays perfectly still |
| 6 | the long paper fingers slowly curling tighter around the stairwell rail, the dim light flickering |
| 7 | a washing machine spinning, faint fluorescent flicker; the seated figure stays perfectly still |
| 8 | the amber glow in the elevator mirror slowly brightening, a faint flicker; the lantern head stays perfectly still |
| 9 | rain dripping, the wet footprints glistening on the doormat, a slow push-in towards the door |
| 10 | rain on the window glass, the street lamp flickering; the figure on the street stays perfectly still, looking up |
| 11 | the candle flame flaring and flickering, sodium lamps strobing, the painted smile lit by the flicker; the figure stays perfectly still |
| 12 | the room slowly brightening into sodium orange, a very slow push-in |

**La quietud necesita su propio negativo.** El negativo de serie de Wan pide
evitar lo «estático» y los «fotogramas inmóviles», así que en el primer
intento FAROL caminaba hacia la cámara. En los planos donde se le ve (1, 2,
5, 7, 8, 10 y 11) el negativo es el de serie sin esos términos de quietud, más el
caminar (`走路，迈步, walking, stepping, striding, moving figure, turning
around`). El resto conserva el negativo de serie. La toma del plano 1 en la
que camina sigue en el proyecto, por si quieres un inserto de «se ha
movido».

## 6. Photocards: el contraste idol

Cinco looks a 2:3 (semillas 4001…4005), cada uno `@FAROL <look>, full-length
portrait, the whole figure in frame with clear empty space above the lantern
head` con `consistent=true`. El encuadre se omite en la tira de fotomatón.
Después, `studio_photocard_set` compone los anversos, los reversos (cada uno
con una nota entre dulce y siniestra) y una hoja de contactos. Unos 2 min
por foto.

![El set de photocards: cinco anversos con sus reversos](../media/farol/photocards.jpg)

| Rol | Look (resumido; los prompts completos son `IDOL_LOOKS` en el script) | Reverso |
| --- | --- | --- |
| Visual | fondo rosa pastel, un ramo de rosas marchitas, luz de belleza suave, retoque de revista, la llama brillando cálida | «thanks for coming» («gracias por venir») |
| Main Rapper | sentado en un taburete de madera con un jersey de punto crema, fondo crema cálido, la llama brillando suave | «wrap up, it's cold out» («abrígate, fuera hace frío») |
| Center | la uve de la victoria con un dedo largo de papel, tira de fotomatón de cuatro fotos, flash, cortina azul bebé | «click!» («¡clic!») |
| Lead Vocal | junto a una ventana con lluvia y lucecitas cálidas, bokeh suave, sonrisa amable pintada en el farolillo | «I can see you from here» («te veo desde aquí») |
| Maknae | con una notita escrita a mano que dice "sorry" entre sus dos manos de papel, fondo lila | «sorry for following you» («perdón por seguirte») |

## 7. Diseño del álbum

La capa tipográfica de Prospero (texto exacto, tipografías incluidas), en
las variantes de noche, con el acento `#F28C28`:

- Portada (3000×3000): fotograma 3, el primer plano del farolillo. Título «DON'T LOOK BACK», artista «FAROL», «single».
- Contraportada con la lista de canciones: el mismo fotograma desenfocado, con «01 DON'T LOOK BACK 2:30» y los créditos.
- Póster teaser: fotograma 1, el plano de situación. «FAROL», lema «Don't look back», línea «Always a little closer».
- Tarjeta con letra: fotograma 11, el inserto del estribillo, con el principio del gancho («Don't look back, don't look back, I'm the glow on the glass, I'm the light through the crack»).

La versión española imprime los mismos diseños en español: «NO MIRES ATRÁS», «Siempre un poco más cerca», los reversos «gracias por venir», etcétera.

<p><img src="../media/farol/poster.jpg" width="32%" alt="Póster teaser: FAROL pequeño bajo la farola y el nombre en letra grande">
<img src="../media/farol/lyric-card.jpg" width="32%" alt="Tarjeta con letra: los cuatro versos del gancho sobre el inserto oscuro">
<img src="../media/farol/back.jpg" width="32%" alt="Contraportada"></p>

## 8. El montaje

`studio_timeline action=auto` en 9:16 y 16:9, con los 12 mejores fotogramas y
los 18 clips:

- **Corte:** plano nuevo en cada verso cantado y en cada inicio de sección. La intro, el puente y el outro aguantan cada plano dos compases, las estrofas cambian una vez por compás (o por verso, lo que llegue antes) y el estribillo corta cada medio compás. Cada sección saca los planos de su propio grupo, en el orden de la historia (`STORYBOARD_EN` en el script); por ejemplo, el preestribillo es la llama que se aviva y luego el clip de la parada de autobús. Todas las entradas son clips.
- **Puntos de entrada del vídeo:** los clips de Wan empiezan en su fotograma de origen, así que cada corte de vídeo entra un segundo después (`video_lead_in_s`) y cada vez que un clip se repite entra más adelante (`video_rotate_offsets`). Un clip que suena cuatro veces en un estribillo enseña cuatro momentos distintos.
- **Acabado:** gradación `sodium_night`, grano 0,3, viñeta, un destello blanco con glitch de separación de color en los tiempos fuertes del estribillo y karaoke de terror (tipografía estrecha en mayúsculas, un temblor por verso y resaltado progresivo).
- **Renders:** primero una vista previa y después el final: 1080×1920 y 1920×1080 en H.264 con la canción en AAC (CRF 18, unos 75 MB por minuto de final).

![Fotogramas del montaje 9:16, de la intro al último verso](../media/farol/cut-9x16.jpg)

![Fotogramas del montaje 16:9](../media/farol/cut-16x9.jpg)

## 9. Lo que encontró la ejecución (corregido en la app)

- **Las ediciones de Qwen salían como ruido.** La plantilla de edición le
  pasaba al sampler un latente vacío con denoise 0,6, y el modelo lo pinta
  como textura. Ahora las ediciones de Qwen van a denoise 1 salvo que pidas
  una intensidad.
- **Los vídeos se cortaban en una tarjeta compartida.** Un modelo de lenguaje
  se metió en la misma GPU y un clip tardó 43 minutos, así que la espera fija
  de 900 s lo dio por fallido. Ahora la espera es de 1 h para vídeo, 30 min
  para audio y 20 min para imagen, y `PROSPERO_COMFY_TIMEOUT_S` la cambia.
- **Un trabajo esperaba detrás de su propio renderizador.** El control de VRAM
  miraba con nvidia-smi la tarjeta más libre y contaba como ocupados los
  modelos que ComfyUI tiene en caché. Ahora mira la tarjeta en la que de
  verdad corre ComfyUI (`/system_stats`), donde los modelos en caché cuentan
  como libres.
- **Una franja roja recorría cada fotograma.** En ffmpeg 8, pasar por RGB
  planar (`gbrp`) pone en negro las últimas 8 columnas de un fotograma de
  1080 de ancho, y la gradación las teñía de rojo. Los filtros de gradación
  que solo trabajan en RGB van ahora en RGB empaquetado, y el glitch usa
  `chromashift`, que se queda en YUV.
- **El render final pasó a pesar 820 MB.** Al dejar de pasar la gradación por
  RGB planar, el grano caía también en los planos de color. Ese moteado de
  color casi no se comprime, y el final de 2 min a 1080p salió ocho veces más
  grande con el mismo CRF. Ahora el grano va solo en la luminancia, como el
  grano de película, y el final baja a unos 150 MB.
- **A las photocards se les cortaba la cabeza.** Un sujeto alto llenaba su
  foto 2:3 y la tarjeta recortaba por un punto fijo. Ahora el anverso usa un
  recorte que busca al sujeto, y los prompts de idol piden aire por encima de
  la cabeza.
- **FAROL caminaba.** Mira la sección de clips.
- **El vídeo parecía un pase de diapositivas.** Siete clips para unos 130
  cortes hacían que la mayoría fueran fotogramas fijos con un zoom lento, y
  cada corte de vídeo empezaba en el fotograma fijo del propio clip.
  `--motion full` hace un clip por plano, el montaje salta el arranque de cada
  clip y varía sus repeticiones, y un grupo de render reparte los clips entre
  todas las tarjetas.
- **El guion no se respetaba.** El LRC de whisper no llevaba marcas de
  `[Sección]`, así que el montaje usaba secciones por energía cuyas etiquetas
  no coincidían con ningún grupo del guion, y los planos salían de un único
  grupo barajado. El alineador ahora escribe las marcas.

## Cómo repetirla

Arranca ComfyUI en una tarjeta de 16 GB o más, con Qwen-Image 2.1, Wan 2.2
TI2V 5B y ACE-Step 1.5 instalados. Después arranca Prospero y, desde la raíz
del repositorio:

```powershell
# la producción entera, reanudable (cada fotograma, clip, tarjeta y render queda guardado)
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final
# la versión inglesa encima: mismas imágenes, canción cantada (4 tomas para elegir) y un clip por plano
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --lang en --reuse-from data\productions\no_mires_atras --motion full --only song --song-takes 4
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --lang en --motion full --only clips --keep
# después de escucharlas: monta con la toma elegida y su LRC alineado
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --lang en --motion full --use-take 4 --lrc-path C:\Users\<tú>\Music\dont_look_back.lrc
```

Para el grupo de render, arranca el mismo ComfyUI una vez por tarjeta
(`main.py --cuda-device N --port P` con sus propios `--output-directory`,
`--temp-directory`, `--user-directory` y `--database-url`), añade los
servidores extra en Ajustes o en `data/backend.json` (`"render_pool":
["http://127.0.0.1:8189", "http://127.0.0.1:8190"]`) y reinicia Prospero.

Las semillas son fijas, pero solo dan las mismas imágenes con los mismos
modelos, la misma versión de ComfyUI y la misma tarjeta. El linaje de cada
recurso (`studio_lineage`) guarda su receta exacta.
