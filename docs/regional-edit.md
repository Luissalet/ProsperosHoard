# Regional image editing / Edición regional de imágenes

## English

Call `studio_regional_edit` with a source `asset_id` and exactly one selection. A `box` is `[left, top, right, bottom]` in source pixel coordinates. A `quad` gives four convex corners clockwise from top left. Coordinates are pixel edges. Use a `clip_polygon` to intersect the mask with the visible part of a surface, for example around a trimmed corner or a finger. `feather_px` defaults to 2 and fades inward from every selected edge. Source alpha is preserved.

- **Generate inside a region.** Set `mode="qwen"` and pass an editing `prompt`. The source is `<image1>`. Up to nine `reference_asset_ids` are passed as `<image2>` onward in order. The normal Qwen edit recipe generates a candidate, then Prospero composites only the selected part at source resolution.
- **Project existing artwork.** Set `mode="paste"` with `drawing_asset_id`. Use `rotation=0|90|180|270` clockwise before projection. Prospero transfers the drawing's pigment through a perspective transform, estimating the destination's paper color, light and grain from bare pixels inside the mask. `contrast` scales luminance and `saturation` scales the pigment’s colour difference from the paper (both default to 1, up to 2). This mode runs on the CPU.

Poll with `studio_job(job_id)`. The receipt records source and reference IDs and SHA-256 hashes, the selection and feather, the actual PNG mask URL and hash, the result's SHA-256 and the number and percentage of changed pixels at zero mask opacity. The comparison uses decoded 8-bit RGB or RGBA channels at source resolution. White mask pixels take the candidate, black pixels retain the source and intermediate values blend. `studio_lineage` returns the recipe and the replay arguments.

The result is a separate asset. Undo the agent session to move it to the trash, or withdraw that asset with `studio_delete_assets` and restore it with `studio_trash`. Finish or cancel a running edit before undoing its session. The job retains its receipt after undo. Restore the asset to open its mask again.

## Castellano

Llama a `studio_regional_edit` con el `asset_id` original y una sola selección. `box` usa `[izquierda, arriba, derecha, abajo]` en píxeles de la imagen. `quad` da cuatro esquinas convexas en sentido horario desde arriba a la izquierda. Las coordenadas se refieren a los bordes de los píxeles. `clip_polygon` permite recortar la máscara a la parte visible, por ejemplo para respetar una esquina cortada o un dedo. `feather_px` vale 2 por defecto y funde hacia dentro desde los bordes. Se conserva el canal alfa original.

- **Genera dentro de una región.** Usa `mode="qwen"` y escribe la instrucción en `prompt`. El original es `<image1>`. Las `reference_asset_ids`, hasta nueve, se envían en orden desde `<image2>`. La receta habitual de Qwen genera una imagen candidata y Prospero compone solo la zona seleccionada a la resolución original.
- **Proyecta un dibujo existente.** Usa `mode="paste"` con `drawing_asset_id`. `rotation=0|90|180|270` gira en sentido horario antes de proyectar. Prospero lleva la tinta a esa perspectiva y estima el papel, la luz y el grano del destino a partir de los píxeles de papel dentro de la máscara. `contrast` regula la luminancia y `saturation` la diferencia de color entre el pigmento y el papel (ambos valen 1 por defecto y admiten hasta 2). Este modo usa la CPU.

Sigue el trabajo con `studio_job(job_id)`. El recibo recoge los identificadores y las huellas SHA-256 del original y las referencias, la selección y el fundido, la URL y huella del PNG de la máscara, la huella del resultado y el número y porcentaje de píxeles cambiados con opacidad de máscara cero. La comparación usa los canales RGB o RGBA de 8 bits decodificados a resolución original. El blanco toma el resultado de la edición, el negro conserva el original y los grises funden ambos. `studio_lineage` devuelve la receta y los argumentos para repetirla.

La edición crea un recurso aparte. Deshaz la sesión del agente para mover el resultado a la papelera, o retíralo con `studio_delete_assets` y recupéralo con `studio_trash`. Termina o cancela una edición en marcha antes de deshacer su sesión. El trabajo conserva el recibo después de deshacer. Recupera el recurso para volver a abrir su máscara.
