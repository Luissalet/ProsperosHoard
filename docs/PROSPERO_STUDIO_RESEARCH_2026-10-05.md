# Prospero Studio: investigación, cambios y pruebas — 5 octubre 2026

## Criterio de producto

Crear desde una idea y una referencia debe ser inmediato. Los papeles de las
referencias, el guion, las letras, el reparto de fondo y los nodos son recursos
opcionales para dirigir con precisión. No son requisitos de entrada. La revisión
de una imagen o una toma tiene prioridad sobre los paneles de configuración.

Se conserva el catálogo de herramientas, los motores, las recetas, los
personajes, los shorts narrados, las producciones, el diseñador, el audio y la
voz. La navegación principal reduce la cantidad de entradas visibles, mientras
el cajón de herramientas mantiene las rutas anteriores y permite buscarlas.

## Referencias investigadas

Lectura de documentación pública y código primario. No se han contratado ni
probado las cuentas de estos servicios; sus capacidades publicadas no son
resultados medidos en Prospero.

| Sistema | Qué aporta a este trabajo | Fuente primaria |
| --- | --- | --- |
| Magnific / Spaces | Un espacio de trabajo centrado en los medios; conexiones por tipo, referencias y salidas reutilizables; controles de vídeo condicionados por el modelo. Inspira continuidad entre creación y revisión, no una lista de proveedores que Prospero no tiene. | [Video nodes](https://www.magnific.com/ai/docs/video-nodes), [Nodes and connections](https://www.magnific.com/se/ai/docs/nodes-and-connections), [Spaces](https://www.magnific.com/spaces) |
| Runway | Separar la apariencia del fotograma de la acción descrita; referencias identificables y reutilizables; iterar tomas cortas antes de construir secuencias. | [Gen 4.5](https://help.runwayml.com/hc/en-us/articles/46974685288467-Creating-with-Gen-4-5), [Image-to-video prompting](https://help.runwayml.com/hc/en-us/articles/48324313115155-Image-to-Video-Prompting-Guide), [Reference media](https://help.runwayml.com/hc/en-us/articles/52963720640275-Using-reference-media-to-guide-your-generations) |
| LTX Studio | Conservar personajes, objetos y lugares como elementos reutilizables; combinar creación libre con planificación por tomas y montaje. El guion puede servir de entrada sin convertirse en la única entrada. | [Sitio oficial del estudio](https://ltx.io/studio) |
| Luma | Referencias maestras de personaje y producto, organización por tableros y reutilización directa. Volver a una referencia canónica evita encadenar ediciones que acumulen cambios. | [Character and product consistency](https://lumalabs.ai/learning-hub/keep-character-product-consistency-in-luma-reference-guide), [Using references](https://lumalabs.ai/learning-center/articles/how-to-use-references-properly), [Favorites](https://lumalabs.ai/learning-hub/how-to-use-favorites), [Modify video](https://lumalabs.ai/learning-hub/modify-video) |
| Adobe Firefly | Exponer capacidades según el modelo: referencia de cámara, duración, primer/último fotograma y límites de archivo. Las combinaciones publicadas cambian con versiones; se validan para cada motor local. | [Match camera motion, actualización de junio 2026](https://helpx.adobe.com/ie/firefly/web/work-with-audio-and-video/work-with-video/match-camera-motion-to-reference-video.html) |
| InvokeAI | Biblioteca como parte de la creación: ampliar, organizar y reutilizar parámetros; proyectos de canvas con referencias propias. | [Gallery](https://invoke.ai/features/gallery/), [Canvas projects](https://invoke.ai/features/canvas/canvas-projects/) |
| Hypernatural | Entrada por idea o guion; revisión de imágenes antes de animar; acciones y diálogo por toma; cambios locales de duración y voz sin regenerar una producción entera. | [First video](https://intercom.help/hypernatural/en/articles/9883799-start-here-create-your-first-video), [Cinematic video](https://intercom.help/hypernatural/en/articles/14812087-create-a-cinematic-video), [Editing](https://intercom.help/hypernatural/en/collections/19692907-edit-your-video), [Dialogue](https://intercom.help/hypernatural/en/articles/15996913-add-dialogue-to-your-video-or-shot) |
| ComfyUI / Wan | El grafo local es la autoridad sobre fotogramas, segundos y entradas compatibles. Una opción de interfaz solo se considera integrada cuando llega al trabajo y al modelo real. | [Wan 2.2 oficial](https://docs.comfy.org/tutorials/video/wan/wan2_2) |
| FreeVideo / Video DiT Net | Referencias por tipo, historial, generación en dos pases y cuantización adaptada al hardware. Es una propuesta de motor adicional, no un motor local ya disponible. | [Repositorio oficial](https://github.com/FlashML-org/FreeVideo), [VDN](https://arxiv.org/abs/2609.20744) |

También se consultaron los catálogos previos de Faustus: `13_CATALOGO_FEATURES.md`,
`01_FUENTES_Y_DONANTES`, `14_FUENTES_COMPLETAS`, `04_MODELOS_Y_RECURSOS`,
`06_ADAPTADORES_MULTIMEDIA`, el radar del 29 de septiembre y las fuentes del
4 de octubre. Las propuestas UX03 (papeles de referencias), UX05 (comparación),
UX06 (borradores), UX09 (paleta) y UX10 (adaptación a pantalla) orientan cambios
concretos. Las 156 propuestas de aquel catálogo no se presentan como implementadas.

## Lo que se implementa

- Estudio de imagen/vídeo con un monitor amplio, composición compacta, tomas del
  proyecto y comparación de dos vídeos con reproducción conjunta desde el inicio.
  Audio, voz y canvas de nodos siguen accesibles.
- Referencias subidas, elegidas de la biblioteca o arrastradas. Papeles opcionales
  de identidad, ropa, lugar, estilo y pose. Se conserva la referencia canónica y
  se explicita su función en la instrucción. No se descartan silenciosamente
  referencias cuando un motor admite menos.
- El resultado de imagen pasa directamente a animación. Referencias de otro
  proyecto guardan la nueva toma en el proyecto activo. Borradores y referencias
  se restauran en la sesión por proyecto.
- Movimiento descrito como acción, cámara con instrucciones efectivas, duración
  y semilla; guía de vídeo para Wan Animate y fotograma final para FLF. La guía
  y el fotograma final no se combinan cuando el motor no admite esa combinación.
- Ampliación con un clic en referencias, biblioteca, imágenes de storyboard y
  variantes. Seleccionar una variante y examinarla son acciones independientes.
  Los visores superpuestos respetan foco, Escape y el orden de apertura.
- Mesa opcional de texto y tomas: TXT/Markdown, LRC, SRT y VTT; fragmentos
  editables, división por líneas, tiempos y enlaces a tomas existentes o nuevas.
  Los tiempos vacíos permanecen sin sincronizar. Los subtítulos conservan sus
  finales explícitos y las frases LRC repetidas conservan posiciones distintas.
- Varios fragmentos en una toma usan el rango conjunto. Los solapamientos entre
  tomas se rechazan antes de guardar. Un cambio de tiempo conserva imágenes y
  clips y recalcula lo que depende del montaje. Una toma aprobada debe
  desbloquearse antes de cambiar su tiempo. Desvincular texto conserva el tiempo
  manual de la toma; no borra el montaje por sorpresa.
- El reparto de fondo existente sigue disponible. Los nombres explícitos por
  toma permiten mantener el grupo; superar el límite de referencias provoca
  un error claro en lugar de descartar personajes silenciosamente.
- Montaje con ajuste limitado de fuentes cortas. Una falta pequeña de duración
  se resuelve cambiando velocidad; una falta grande se rechaza con un siguiente
  paso. El fotograma congelado solo se extiende al elegir expresamente `hold`.
  Los vídeos sincronizados con audio requieren cobertura real. El registro del
  render conserva los ajustes aplicados.
- QA de congelaciones continuas, además del promedio de movimiento. Detecta una
  cola parada aunque el comienzo se mueva. Analiza todos los fotogramas de un
  clip dentro del límite; un vídeo mayor de 18.000 fotogramas se declara fuera
  del límite, nunca se aprueba tras revisar únicamente su principio. Los cálculos
  usan bloques para contener el consumo de memoria.

No se simula una transferencia de movimiento con una instrucción de texto: la
opción de vídeo guía entrega un archivo real al grafo de Wan Animate. Los
controles no convierten en disponibles modelos o pesos ausentes.

## Pruebas locales y límites de la evidencia

Almacenamiento aislado: `D:/LocalAI/qa/prospero-redesign-20261005/live-data`.
No se han regenerado ni sustituido las producciones del usuario. Las imágenes
de partida de este ensayo son material ficticio creado para la prueba.

| Ejecución real desde la interfaz | Resultado observado |
| --- | --- |
| Imagen original → baile con Wan 14B | 5,062 s, 81 fotogramas; giro y brazos abiertos visibles; energía 3,407; ninguna congelación continua de al menos 0,6 s detectada. Tiempo de generación 192,89 s. |
| Identidad de la primera imagen + vestuario de una segunda, con Qwen 2.1 Edit | Cambio a chaqueta verde, pantalón negro y zapatillas; la inspección visual conserva de forma reconocible personaje y estudio. 117,86 s. |
| Imagen editada → baile con Wan 14B | 5,062 s, 81 fotogramas; giro y movimiento de ropa visibles; energía 1,932; ninguna congelación continua de al menos 0,6 s detectada. 185,75 s. |
| Comparación en el monitor | Ambos vídeos reproducidos, sin audio, desde el inicio; estados reales `readyState=4`, `paused=false`, diferencia inicial de reproducción inferior a 0,01 s. No se afirma sincronización continua de reproducción ni edición multipista. |
| SRT → fragmentos → enlaces → guardar → recargar | Las tomas conservan 1–3 y 3–5 segundos, imágenes existentes y enlaces; ampliación desde el fragmento comprobada. Sin canción ni documento obligatorio. |

La inspección de seis fotogramas por toma permite juzgar continuidad básica;
no demuestra consistencia universal, ausencia de todos los defectos anatómicos
ni calidad equivalente a servicios comerciales. Movimiento medio mide cambio
de imagen, no calidad artística. La revisión humana de la reproducción sigue
siendo necesaria. Recibos: `generated-motion.json`,
`generated-outfit-motion.json`, hojas de contacto y registros de trabajos en
la carpeta de QA.

La auditoría de medios anteriores encontró fuentes cortas y clips casi estáticos;
queda en `baseline-media.json`. El arreglo del relleno evita esa causa de
congelación en renders nuevos; no repara retroactivamente los archivos existentes.

## Ensayos adicionales

### Duración, movimiento y montaje

Wan Animate, desde el estudio y con vídeo guía real, produjo 3,042 s / 73
fotogramas en 519,8 s. Se observan giro, ropa verde y escenario reconocibles;
energía 1,304 y ninguna congelación continua de al menos 0,6 s detectada.
Wan 5B produjo la duración solicitada de 3 s (3,042 s / 73 fotogramas), con
pasos y giro visibles, energía 3,746 y sin esos intervalos congelados; 300,62 s.
No se afirma que un borrador sea siempre más rápido en este ordenador.

Un montaje real de tres clips dura 9,792 s / 235 fotogramas. Al cambiar en la
interfaz el ajuste de una fuente de 5,062 s a `error`, el render rechaza cubrir
5,792 s. Al guardar `stretch`, termina y registra el ajuste limitado aplicado.
No se detectan intervalos congelados de al menos 0,6 s; los saltos entre planos
son cortes deliberados. La reproducción del montaje en la interfaz real llegó
al final, con duración 9,792 s y estado `ended=true`; captura
`montage-playback.png`. Recibos: `generated-guided-motion.json`,
`generated-wan5-motion.json`, `generated-cut-motion.json` y hojas de contacto.

### Spaces: ejecución y referencias

La petición explícita de nodos amplía el editor existente: buscador de las
quince herramientas, nombres editables, enfoque de un nodo, subida directa,
propósitos opcionales, duración efectiva, revisión ampliada y reutilización
de salidas. El inicio persona + vestuario → imagen → clip tiene guía de
movimiento opcional. Se conservan las técnicas, formularios de app, versiones,
lotes, selección de salidas y ejecución de dependencias.

La revisión del código encontró dos pérdidas silenciosas: se cortaban las
referencias a ocho y se descartaban parámetros de duración al resolver
`auto_clip`. Ahora se valida la capacidad de diez referencias y se preserva o
traduce la duración al parámetro real del motor. Las guías incompatibles
producen un error; un movimiento solicitado no se convierte en otro motor que
ignora el vídeo guía. Los propósitos se traducen a instrucciones de `<imageN>`
según el orden real de archivos, también a través de listas.

El flujo de prueba se creó y ejecutó desde el navegador, subiendo las tres
referencias. La imagen intermedia generada por Qwen conserva de forma
reconocible la persona y el estudio, y viste chaqueta verde, pantalón negro y
zapatillas de la segunda referencia. Wan Animate terminó en 517,73 s y produjo
3,042 s / 73 fotogramas, con pasos y giro visibles, energía 1,299 y sin
congelaciones continuas de al menos 0,6 s detectadas. El resultado se abrió y
reprodujo en la ampliación real de la interfaz.

Arrastrar la salida de imagen a un punto vacío ofreció herramientas compatibles;
elegir Clip creó la conexión de inicio. Esa rama alternativa, de 3 s con Wan
14B, terminó en 104,36 s: 3,062 s / 49 fotogramas, energía 1,785, sin esos
intervalos congelados. Ejecutar el grafo conservó los IDs de resultados
anteriores y sólo generó la rama nueva. Los tiempos incluyen el trabajo de la
cola; la receta conserva además el tiempo interno del motor.

Se comprobó el buscador de nodos con teclado, la subida directa, el enfoque de
un nodo y el regreso tras cambiar de vista. Ese último recorrido descubrió
que salir antes del guardado diferido perdía la última edición: ahora la salida
guarda los cambios pendientes. La nota se escribió, se abandonó inmediatamente
el lienzo y se recuperó al volver y recargar. Recibos: `spaces-final.json`,
`spaces-guided-job.json`, `spaces-branch-job.json`, métricas y hojas de contacto
`generated-spaces-*`, en la carpeta de QA.

## FreeVideo: integración aún pendiente

El código se estudió en una copia de investigación. Requiere Python 3.12
(`>=3.12,<3.13`), mientras el ComfyUI actual utiliza 3.13. El paquete per-tensor
consultado ronda 21,34 GB, además del codificador de texto requerido. Las cifras
de rendimiento del repositorio son del autor, no mediciones locales. Falta un
entorno aislado compatible, descargar sus pesos, adaptar el trabajo al catálogo
y verificar generación, cancelación y procedencia de resultados. No se añade
una opción engañosa a la interfaz antes de esa validación.

## Diseño y verificación visual

Dirección: banco de revisión de tomas, carbón neutro, blanco cálido y acción
cobre; contenido multimedia dominante y configuración progresiva. Se conservan
las fuentes del proyecto. Las composiciones y las placas de `.impeccable` son
material de diseño, no resultados del motor local ni fotografías de pruebas.
La interfaz muestra los medios reales del proyecto activo.

La suite completa terminó con **746 pruebas aprobadas y 3 omitidas**
(`final-spaces-tests.xml`). La compilación TypeScript y las cuatro capturas
automatizadas del repositorio terminaron correctamente. Los recorridos manuales
usaron medios ficticios en almacenamiento de QA aislado; las cinco producciones
del usuario se conservaron al recargar el servicio principal. La revisión del
estudio puntuó sus cuatro correcciones como resueltas, con disposición `ship`
limitada a esa lista. La revisión de Spaces se registra por separado. Los
controles de enfoque móvil se corrigieron para conservar una escala legible y
permitir desplazamiento vertical; la revisión independiente puntuó ese hallazgo
como resuelto, con disposición `ship` limitada a la corrección. Los ocho
encuadres originales fueron válidos; los dos móviles se recapturaron tras el
arreglo y se añadió evidencia de acceso a la vista previa mediante desplazamiento.
La exportación del grafo se verificó por API: siete nodos, cinco conexiones,
formato `prospero-technique/1`, propósitos conservados y referencias de archivos
de QA retiradas. La descarga del navegador integrado no devolvió un recibo al
controlador y no se cuenta como descarga manual comprobada.

Los gates de coincidencia exacta con la composición se registran por separado: una
buena revisión funcional no se presenta como un gate de píxeles aprobado.

## Corrección posterior: identidad visual, resultados y ocupación

La indicación posterior del usuario sustituye la propuesta de cobre: se conserva
el icono original de Prospero y la paleta original de ciruela, magenta y rosa.
Los acentos de marca usan los tokens de `hoard-theme.css`; el tema claro conserva
su variante rosa/lila. Los colores de advertencia siguen teniendo su significado.

La captura del usuario reveló defectos reales en Generar: el tamaño intrínseco de
un retrato desbordaba el monitor y tapaba la comparación; el campo Negativo
desbordaba su columna; las reglas de cabecera ocultaban la ocupación y toda la
pill en ventanas estrechas. El monitor contiene ahora la imagen completa, las
comparaciones tienen altura acotada, los campos encajan en sus columnas y la
pill cerrada muestra porcentaje de VRAM y barra para cada GPU, más el total.
El desplegable conserva los detalles de servicios, memoria, temperatura y CPU.

Se reprodujo el caso con los recursos existentes de FAROL sin generar ni cambiar
sus medios. Se comprobó en navegador real a 1821, 1440, 1035 y 390 píxeles,
incluidos los dos temas, comparación y visor grande con Escape y restauración
del foco. Las capturas se guardan con el prefijo `brand-monitor-` en el directorio
de QA. Estas comprobaciones cubren la corrección y no certifican todas las
pantallas de Prospero ni la calidad artística de sus generaciones.

Tras esta corrección, la batería completa volvió a aprobar 746 pruebas, con 3
omitidas (`brand-monitor-tests.xml`, 531,38 s). La compilación TypeScript y las
cuatro capturas automatizadas del repositorio terminaron correctamente. La
página principal no mostró errores de consola en el recorrido comprobado.

La nueva revisión independiente comparó la captura original con siete capturas
válidas del resultado. Confirmó la corrección del monitor, comparación, campo
Negativo, ocupación visible y marca original. Su único hallazgo pendiente era la
documentación de cobre; tras actualizar DESIGN.md y el sidecar, lo puntuó como
resuelto con disposición `ship` limitada a esta corrección. Recibos:
`brand-monitor-review.md` y `brand-monitor-validation.json`. No cambia el estado
de los gates históricos de coincidencia exacta con composiciones.

## Segunda revisión tras los fallos de entrada

El [informe de recorridos de usuario](PROSPERO_USER_QA_2026-10-05.md) registra
otra revisión de los 18 destinos, con formularios, referencias, montaje,
storyboard, tablas, audio y exportaciones en escritorio y móvil. Se corrigieron
desbordamientos adicionales, acceso por teclado a miniaturas, filtros vacíos,
respuestas de búsqueda fuera de orden y el estado del motor durante carga o
fallo. Los ensayos locales de solo lectura introdujeron latencia y HTTP 503;
se verificaron bloqueo de referencias antiguas, error, reintento y recuperación.

Una toma nueva generada desde Spaces produjo 3,0625 s / 49 fotogramas; se
reprodujo y no se detectaron congelaciones continuas de al menos 0,6 s. Los
fragmentos opcionales de guion conservaron texto, enlaces y tiempos al recargar,
incluido un fragmento sin tiempos. Las cinco producciones principales se
conservaron; los cambios de contenido de las pruebas usaron QA aislada.

La batería principal aprobó 746 pruebas, con 3 omitidas y 0 fallos. La última
corrección del estado del motor se comprobó después mediante compilación y
navegador con carga, fallo y recuperación; no se le atribuye otra ejecución
completa. Las cuatro capturas del repositorio se generaron y revisaron. El
[recibo de esta revisión](D:/LocalAI/qa/prospero-redesign-20261005/deep-validation.json)
conserva el alcance y las pruebas. La revisión independiente valida las
correcciones concretas; la disposición formal sigue siendo `fix` por los gates
históricos de composición y tipografía abiertos. La calidad y consistencia de
producciones largas siguen sin certificarse.

### Continuación: fiabilidad de edición y encadenado

La revisión posterior añade deshacer/rehacer, guardado ordenado antes de ejecutar,
diarios locales por pestaña, conflictos explícitos y colocación de nodos sin
superposición automática. La prueba real corrigió la extracción tardía del
último fotograma de un clip ya generado. Desde la UI se produjo una continuación
de 6,0625 s y se montó con la toma anterior en 9,1667 s; no se detectaron
retenciones de al menos 0,6 s en los 220 fotogramas. La coreografía sigue siendo
aproximada. Batería final: 751 aprobadas, 3 omitidas, más 9 pruebas del editor.
El servidor principal ya carga el arreglo y conserva cuatro proyectos y cinco
producciones. [Informe](PROSPERO_SPACES_CONTINUITY_QA_2026-10-05.md).
