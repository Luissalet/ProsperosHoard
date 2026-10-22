# Perfiles de estilo y revisión por plano

En **Producciones → un vídeo → Revisión y estilo**, importa un vídeo local o
selecciona uno del proyecto y pulsa **Extraer perfil**. ffprobe mide duración,
resolución y FPS; ffmpeg estima cambios de plano según un umbral configurable.
La paleta se calcula sobre una muestra central de cada plano, no sobre todos
los fotogramas. Cámara, ángulo y movimiento permanecen sin observar hasta que
la persona los edita. La energía sugerida proviene de la duración del plano;
no es una medición del movimiento del sujeto.

El perfil conserva por separado las observaciones y la gramática editable:
pesos de duración, paleta, cámara y transición. Guarda el perfil para reutilizarlo
en otra producción con sus propios personajes y metraje. Aplicarlo no copia ni
incorpora el vídeo de referencia a las nuevas tomas.

La duración se distribuye entre los planos sin aprobar. Los intervalos y tomas
aprobados se conservan; si entran en conflicto con la duración elegida, la
operación informa del conflicto. Los planos existentes mantienen prompts,
referencias, reparto y relaciones de continuación. Las tomas invalidadas quedan
en su historial. Los nuevos planos requieren completar su contenido en el
storyboard. El perfil aporta instrucciones a los motores de imagen/vídeo; eso
no garantiza que un modelo las siga. Los tipos de transición admitidos por el
montaje nativo son corte, encadenado, fundido a negro y destello blanco.

El montaje utiliza la duración solicitada, acotada por el audio disponible.
Si la canción todavía se va a componer, su duración se adapta al perfil. Un
perfil no alarga automáticamente una canción existente ni inventa el audio
que falte. Las transiciones llegan al timeline y al renderer propios de Prospero.

Cada comentario registra el instante en la producción, el plano y una identidad
derivada de sus ajustes y tomas actuales. Un cambio desde el editor habitual,
una nueva toma o una regeneración marca el comentario como perteneciente a una
versión anterior. Resolverlo conserva su origen. **Regenerar plano y dependientes**
rehace el plano y sus continuaciones, manteniendo los independientes y sus tomas.
Los aprobados requieren su desbloqueo explícito en el editor antes de corregirlos.

## API y MCP

Las operaciones de interfaz y agente utilizan la misma implementación local:

| Operación | HTTP de persona | Herramienta MCP |
|---|---|---|
| Extraer | POST `/api/style-profiles/extract` | `studio_style_profile_extract` |
| Listar/leer | GET `/api/style-profiles`, GET `/api/style-profiles/{id}` | `studio_style_profile_list`, `studio_style_profile_get` |
| Editar | PUT `/api/style-profiles/{id}` | `studio_style_profile_update` |
| Borrar plantilla | DELETE `/api/style-profiles/{id}` | `studio_style_profile_delete` |
| Aplicar | POST `/api/productions/{slug}/apply-style-profile` | `studio_production_apply_style_profile` |
| Comentar/listar | POST/GET `/api/productions/{slug}/shot-comments` | `studio_production_shot_comment`, `studio_production_shot_comments` |
| Resolver | POST `/api/productions/{slug}/shot-comments/{id}/resolve` | `studio_production_shot_comment_resolve` |
| Corregir dependientes | POST `/api/productions/{slug}/regenerate-dependent` | `studio_production_regenerate_dependent` |

En el dueño fusionado se antepone `/generation`. El MCP conserva las reglas
comunes de motivos y sesiones. Los nuevos perfiles se guardan en `style_profiles`;
los comentarios, en `shot_reviews.json` de la producción. No se requiere ningún
servicio alojado ni API de terceros. El catálogo sigue disponible con los módulos
apagados, y consultar el perfil no inicia modelos.

## Alcance comprobado

Pruebas con vídeos sintéticos de colores conocidos, reutilización de un perfil,
comentarios persistentes, identidad tras cambios ordinarios, conservación de
tomas y aprobaciones, instrucciones reales a los motores y transiciones en el
timeline. QA local por API, MCP stdio y navegador, con vista de 390 píxeles.
Renderizados sintéticos nativos de 15 y 30 segundos, comprobados con ffprobe,
sin reutilizar la referencia. Estas pruebas acreditan el recorrido técnico y
el formato editable; no la calidad artística ni la adhesión de los modelos.

Procedimientos propios inspirados en [ReelMimic](https://github.com/edenfunf/reelmimic/tree/daa15e177605cd26363549d6aa0b9b1de8720e20)
(MIT) y [OpenDesign](https://github.com/nexu-io/open-design/tree/e03d5c23f308c39f4de7c057568df66c29a84385)
(Apache-2.0). Sus programas, agentes y servicios no son dependencias de ejecución.
