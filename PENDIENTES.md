# Pendientes que requieren una decisión

- **Reparar solo una región y el estado obsoleto** (decisión de producto). Hoy una reparación de región (`repair_segment(region=...)`) da el tramo por hecho con las revisiones vigentes de la biblia aunque el resto del fotograma siga siendo el antiguo, así que un plano obsoleto puede quedar «al día» tras corregir solo un rectángulo. Recomendación: dejarlo así cuando la reparación nace de una corrección (`correction_id`, es justo lo que se quería arreglar) y no resellar cuando se hace una región sin corrección. Hace falta tu decisión antes de cambiarlo.
- **Primera toma real con el estudio** (GPU: ComfyUI y modelo de vídeo): «Hacer una toma nueva» en un plano obsoleto solo está probado con un generador de prueba. Lánzalo tú cuando la GPU esté libre.
