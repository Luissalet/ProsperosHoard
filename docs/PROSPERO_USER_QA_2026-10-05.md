# Prospero: segunda revisión con recorridos de usuario

5 de octubre de 2026. La captura y el rechazo del usuario mostraron que las
comprobaciones anteriores no bastaban. Esta revisión amplía el recorrido de
entrada a formularios, referencias, tablas, reproducción, guardado y errores.

## Alcance y datos

Se recorrieron los 18 destinos de navegación y las pestañas de una producción
real. Las pruebas de escritorio y móvil usaron el proyecto NO MIRES ATRÁS en
8815 en modo de consulta. Se probaron anchos de 390, 1035, 1440 y 1821 píxeles
según la superficie. Esto no equivale a ejecutar cada operación de cada motor.

Los cambios de contenido se hicieron en el proyecto aislado de QA de 8817.
Las cinco producciones existentes del servicio principal conservan sus slugs;
el recibo final compara el listado con el anterior. No se pararon modelos ni
servicios del usuario. Los servidores temporales de fallos eran de solo lectura
y se cerraron al terminar.

## Defectos reproducidos y correcciones

| Caso | Antes | Después y comprobación |
| --- | --- | --- |
| Referencias en móvil | Búsqueda, ámbitos y mosaicos escapaban del diálogo. | Dos columnas acotadas, búsqueda y ámbitos adaptables, vista previa y pie accesibles. Se abrió y cerró el visor anidado. |
| Edición de personaje | Motor, voz y velocidad se desbordaban. | Campos en una columna móvil, controles dentro del diálogo. Se canceló sin guardar datos reales. |
| Ampliación accesible | La canónica ya se ampliaba con ratón, pero era una imagen sin botón de teclado. Miniaturas de edición/diseño no ofrecían el mismo acceso. | Botones con nombre accesible abren el visor existente. Intro, Escape y restauración del foco comprobados. |
| Montaje | Un selector largo expulsaba los botones de render fuera de la pantalla. | Selector y acciones se distribuyen en varias filas; permanecen render previo/final y exportación. |
| Storyboard | La pista ampliaba toda la página y dejaba la lista de planos fuera. | Contenedor acotado; se conserva el desplazamiento horizontal interno del editor. Flecha derecha desplazó la pista. |
| Voz | Audiolibro y Doblaje quedaban recortados. | El grupo de pestañas se reparte en filas, sin quitar destinos. |
| Tablas | Trabajos, Actividad y otras tablas ampliaban el contenido o dejaban columnas inaccesibles. | Seis tablas conservan sus columnas en una región horizontal desplazable y enfocada con teclado. Flecha derecha desplazó Trabajos. |
| Producción | La exportación a Lumiere escapaba del ancho móvil. | Descarga, Premiere/DaVinci, Lumiere y acciones de Canvas caben en filas. Se comprobó su presencia; no se envió una exportación externa. |
| Biblioteca vacía por filtro | Decía que no había recursos aunque el proyecto tuviera cientos. | Mensaje de ausencia de coincidencias y botón Limpiar filtros. Carga y errores tienen estados distintos. |
| Búsquedas simultáneas | Biblioteca y selector aceptaban respuestas sin comprobar si seguían siendo actuales. | Una respuesta lenta anterior no sustituye la rápida posterior. Uso bloqueado durante la búsqueda y en caso de error. |
| Audio | Los nombres de secciones se solapaban en la forma de onda pequeña. | Se dibujan solo si caben; la leyenda completa conserva nombres y tiempos. Se reprodujo el audio real de 48 s y se pausó. |
| Menú de escritorio | Herramientas terminaba con una «s» en otra línea. | Se conserva el nombre completo dentro de su botón. |
| Estado del motor | Mostraba ComfyUI apagado antes de consultar un servicio que estaba funcionando. | Cargando mientras consulta; estado no disponible si falla, con error y Comprobar; estado real al recuperar. No se deduce que el motor está apagado de una consulta fallida. |

Se mantienen el icono original, las acciones magenta/rosa, los controles
avanzados y los destinos existentes. Los colores de estado conservan su función.

## Pruebas funcionales

Un proxy local de solo lectura introdujo respuestas en orden inverso: la
búsqueda `slow` tardaba 1,5 s y `fast` 0,02 s. Tanto Biblioteca como el selector
conservaron FAST después de llegar SLOW. Al simular HTTP 503, aparecieron error
y Comprobar; la biblioteca recuperó sus 15 recursos de QA y el selector no dejó
usar una referencia antigua. Intro sobre la miniatura aplicó la referencia y
cerró el diálogo. Las flechas izquierda/derecha dentro de la búsqueda movieron
el cursor del texto.

Para el motor se retrasó tres segundos la consulta de servicios y se simuló
otro HTTP 503. Se comprobaron Cargando, estado no disponible y recuperación
manual a GPU 1, sin alertas restantes. El servicio real de 8815 también mostró
GPU 1 después de consultar; su API confirmó ComfyUI activo en 8188.

En Texto y tomas se añadió un fragmento largo con emoji y sin tiempos, se
guardó, se creó su toma y se recargó. Persistieron los enlaces a las tomas 1,
2 y 3: las dos primeras conservaron 1–3 s y 3–5 s, y la tercera permaneció sin
temporizar. No se obligó a aportar un guion completo ni a llenar los tiempos.

## Generación real

Se ejecutó desde la interfaz un nodo de vídeo de Spaces, con la imagen de
persona y vestuario que ya tenía el flujo. La nueva toma usó Wan 14B y semilla
20261005; terminó en 100,76 s y produjo 832×480, 49 fotogramas a 16 fps,
3,0625 s. Se abrió y reprodujo en el visor, con avance del tiempo y sin error
del elemento de vídeo.

La inspección de fotogramas muestra movimiento de brazos y pasos, con la sala
y el vestuario aparentes conservados en esta muestra. El análisis del vídeo
completo no encontró intervalos congelados continuos de al menos 0,6 s; energía
media 1,644. Esos números no prueban obediencia exacta al prompt, anatomía
perfecta, calidad artística ni consistencia en una producción larga. No se
presenta esta toma como solución universal a los problemas de generación.

## Validación y límites

La batería completa de la corrección principal terminó con **746 aprobadas,
3 omitidas, 0 fallos**, en 507,54 s (`deep-tests.xml`). La última corrección de
etiquetas/estado del motor se validó después mediante compilación y pruebas
de navegador con carga, fallo y recuperación; no se atribuye una nueva batería
completa a esa última modificación. La compilación final produjo
`index-C5EutQsT.js`. Las cuatro capturas del repositorio se generaron y abrieron.
La consola del navegador principal no mostró errores en el recorrido final.

La revisión independiente comparó la captura original del usuario con las
correcciones y puntuó como resueltos los problemas de este alcance, incluidos
el rótulo del menú, las exportaciones y los estados del motor. Su disposición
formal sigue siendo **fix**: los gates históricos de reproducción de la
composición y la discrepancia tipográfica permanecen abiertos. La captura
actual `hero-repro.png` es evidencia de interfaz, no una medición aprobada de
fidelidad a la composición. No se declara aprobada toda la aplicación.

Recibos y capturas: [deep-validation.json](D:/LocalAI/qa/prospero-redesign-20261005/deep-validation.json),
[revisión independiente](D:/LocalAI/qa/prospero-redesign-20261005/deep-review.md),
[movimiento del vídeo](D:/LocalAI/qa/prospero-redesign-20261005/deep-video-motion.json).
Los archivos `deep-*` del mismo directorio conservan los estados antes/después.
