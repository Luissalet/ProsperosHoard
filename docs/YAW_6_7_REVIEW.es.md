# YAW 6.7: acabado independiente de vídeo

[English](YAW_6_7_REVIEW.md)

Revisión del ZIP proporcionado el 2026-10-06. Su único JSON contiene 426
nodos de interfaz y 567 enlaces. SHA-256:
`62b06d0470658cac9a115d5d7f44bfbaf4f23f70e534e4e87d3335d723dbe816`.
Las notas y rutas del archivo se trataron como datos de referencia.

## Capacidades verificadas

- **Interpolación de fotogramas:** FILM VFI, nodo 86, multiplicador 2;
  salida a 48 fps. No es interpolación de prompts: guardar prompts es
  una función separada.
- **Pausa tras la previsualización:** Preview Chooser, nodo 181. Prospero
  ya tiene animáticos, clips borrador y promoción a final, pero no integra
  esa pausa interactiva de ComfyUI.
- **Sonido condicionado al vídeo:** MMAudio, con rama independiente.
  Es una posible ampliación; las canciones y TTS actuales no lo sustituyen.
- **Extensión de vídeo:** selección de fotogramas, I2V y unión del vídeo
  anterior con el nuevo. Es diferente de extender S2V con audio o encadenar
  planos de una producción.
- **TeaCache para Hunyuan**, wildcards y ramas de elección aleatoria de
  LoRAs con sustitución de triggers.

El catálogo real de ComfyUI no incluye FILM, MMAudio, ese sampler TeaCache
ni los nodos de carga/unión de VideoHelperSuite. La conversión sin ejecutar
falló por `MathExpression|pysssss` (nodo 97). También falta resolver las
variables de GetNode/SetNode en el conversor. Los 426 nodos visuales no
demuestran por sí solos que se supere el límite de 400 nodos API.

## Operación disponible

`studio_interpolate(asset_id, fps=48, wait_s=0)` aplica interpolación local
de movimiento con CPU y [FFmpeg minterpolate](https://ffmpeg.org/ffmpeg-filters.html#minterpolate).
Se consulta con `studio_job` y se puede cancelar. Produce otra toma,
conserva el original y registra método, fps y recurso de origen. Cada
llamada crea una toma nueva. Rutas HTTP equivalentes:
`POST /api/agent/studio_interpolate` y
`POST /api/assets/{asset_id}/interpolate`.

Conserva tamaño y ritmo del vídeo; la primera pista de audio se convierte
a AAC manteniendo su desfase respecto al vídeo. El relleno necesario para
la anticipación del filtro se recorta a
la duración real del vídeo. Los fps deben superar los originales y estar
entre 1 y 120; hacen falta tres fotogramas de origen como mínimo. Puede
producir artefactos con movimiento difícil. **No usa FILM ni se afirma que
alcance su calidad neuronal.** No añade dependencias ni descarga modelos.

Las [referencias y versiones exactas](YAW_6_7_REVIEW.md#references-and-remaining-work)
están en la revisión inglesa. No se incluyó código externo ni el workflow.
FILM y MMAudio quedan para adaptadores posteriores con sus propias pruebas
reales. La caché de tensores aparece en proyectos más nuevos del autor;
no se confirmó como función de este ZIP 6.7.
