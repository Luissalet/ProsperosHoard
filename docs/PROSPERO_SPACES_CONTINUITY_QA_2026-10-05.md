# Prospero: edición segura y continuidad real en Spaces

Revisión adicional del 5 de octubre de 2026, posterior al recorrido de los 18 destinos. Se prueba el lienzo como usuario: editar, conectar, equivocarse, recuperar cambios y continuar un vídeo existente. Se conservan el icono original, la marca magenta/rosa, los quince tipos de nodo y las operaciones anteriores. Guiones y letras siguen siendo opcionales.

## Fallos reproducidos y cambios

| Caso | Antes | Ahora |
|---|---|---|
| HTTP 503 al guardar | Ejecutar enviaba generación sobre la versión antigua. | Guardado serializado; ejecutar, construir, exportar y entrar en App esperan éxito. El fallo conserva el borrador y ofrece reintento. |
| Editar durante un guardado lento | La acción dependiente podía usar una instantánea anterior. | Con dos segundos de latencia se guardaron las dos revisiones en orden antes de intentar ejecutar la más reciente. |
| Volver al lienzo después de un fallo | Se podía perder el texto. | Borrador inmediato, aviso antes de salir y recuperación explícita. Se probó navegando fuera y entrando de nuevo; la recarga cancelada por el aviso no se cuenta como recarga completada. |
| Dos pestañas / HTTP 409 | La recarga automática podía sustituir cambios propios. | Conflicto visible, revisión de la versión del servidor y confirmación para guardar la propia. El guardado en B no borra el borrador pendiente de A. |
| Deshacer y rehacer | No había historial completo de edición. | Botones, Ctrl+Z / Ctrl+Mayús+Z / Ctrl+Y y Ctrl+S. Recuperar un nodo recupera sus cables; añadir y conectar es una operación. Deshacer no altera trabajos ni borra medios; los campos mantienen su historial nativo. |
| Nuevos nodos | Se superponían a nodos existentes. | Hueco libre según tamaños reales y enfoque del nodo nuevo. Se conservan posiciones elegidas por el usuario. Grupos con altura y profundidad correctas desde su creación. |
| Portada de Spaces | Miniatura dentro de otro botón. | Miniatura abre el visor grande; abrir el flujo mantiene su acción propia. |
| «Último fotograma» conectado después de generar | El cable era válido pero la continuación fallaba porque no existía el fotograma. | Extracción del clip existente al necesitarlo, reutilización y reparación de referencias derivadas borradas. Funciona a través de listas y respeta clips excluidos, sin regenerar la fuente. Los errores de extracción impiden encolar una continuación inválida. |

El proxy de fallos solo admitió escrituras en un espacio desechable de QA; bloqueó todos los POST de generación. Su registro contiene una petición de ejecución anterior al arreglo y otra posterior al guardado lento correcto: no se presenta como un registro con cero POST. Los casos de guardado fallido después del arreglo no enviaron generación. El proxy se cerró al terminar.

## Vídeo real desde la interfaz

Se conectó una toma ya generada a un nodo nuevo con «último fotograma → inicio», se escribió una continuación de seis segundos y se pulsó Generar. La primera prueba reprodujo el fallo; después del arreglo terminó correctamente. Se conectaron ambos clips a «Unir clips» en el orden original → continuación y se renderizó y reprodujo el montaje.

| Resultado | Duración / fotogramas | Movimiento medio entre fotogramas | Retenciones detectadas ≥ 0,6 s |
|---|---|---|---|
| Original `a_01M45RMKRDCC80QZNFYD852SP2` | 3,0625 s / 49, 16 fps | 1,644 | Ninguna |
| Continuación `a_01M45WQ7TM488WK6NGD0WS30JA` | 6,0625 s / 97, 16 fps | 2,199 | Ninguna |
| Montaje `a_01M45X3X8RACRT7YXR2CZ2J1YZ` | 9,1667 s / 220, 24 fps | 1,353 | Ninguna |

Continuación: Wan 2.2 I2V 14B, semilla 20261006, calidad Final, 234,51 segundos de generación. Fotograma derivado: `a_01M45WG23B0P15RW01XS27QNTC`. Ambos reproductores mostraron `readyState=4`, reproducción activa y avance del tiempo. Se abrieron y examinaron sus hojas de fotogramas.

La persona, chaqueta verde, pantalón negro, zapatillas y estudio iluminado se mantienen visualmente en esta muestra. Hay cambios de brazos, pasos y giro, pero no se ejecuta toda la coreografía con precisión. La diferencia media de gris entre el último fotograma original y el inicial nuevo es 3,326/255: hay una variación pequeña, no identidad píxel a píxel. El detector analiza todos los fotogramas reducidos a 160×90, con diferencia máxima de 0,15 y retenciones de al menos 0,6 s. No certifica anatomía, identidad universal, sincronía musical ni producciones largas.

## Validación y activación

- Servidor: **751 aprobadas, 3 omitidas, 0 fallos**, 565,32 s; tres avisos de dependencias obsoletas. `flow-tests-final.xml`.
- Editor: **9 pruebas aprobadas** de historial, agrupación, colocación y guardado serializado. Compilación correcta: `index-BNVBZXIk.js`, `Spaces-BXLeyOJM.js`.
- Navegador: edición, teclas, borradores, conflictos, latencia, HTTP 503, carga inicial fallida y reintento; escritorio 1440 y móvil 390; claro/oscuro; español/inglés. Capturas `flow-*`.
- Se ejecutó `scripts/screenshots.py` y se abrieron las cuatro imágenes de Generar, Montaje, Photocards y Audio. Son demostraciones procedurales de UI, no evidencia de generación IA. También se abrieron error móvil, escritorio claro, dos pestañas y hojas de vídeo.
- `git diff --check` correcto. Los avisos CRLF de archivos ya modificados no son errores del diff.
- Se recargó solo el servidor principal después de identificar proceso y almacenamiento y comprobar cero trabajos activos. Los **cuatro proyectos activos y cinco producciones conservaron sus identificadores**. ComfyUI y los modelos siguieron funcionando. La UI principal volvió a mostrar GPU 1 y no emitió errores/avisos en la consola observada.

[Recibo de revisión](D:/LocalAI/qa/prospero-redesign-20261005/flow-validation.json), [análisis de vídeo](D:/LocalAI/qa/prospero-redesign-20261005/flow-video-motion.json) y [recarga del servidor](D:/LocalAI/qa/prospero-redesign-20261005/flow-main-reload.json). Las capturas y medios permanecen en esa carpeta de QA.

No se realizó una nueva revisión independiente ni se cierran los gates históricos de composición y tipografía. La revisión independiente anterior se refiere a sus correcciones anteriores. FreeVideo y las integraciones de otros Hoards no se dan por completados en esta pasada.
