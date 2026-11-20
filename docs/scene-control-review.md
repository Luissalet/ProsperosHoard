# Guided takes / Tomas guiadas

Import the blocking MP4 with `studio_import` and use the new asset ID as
`control_asset_id` in `studio_scene_plan` or `studio_scene_revise_shot`.
When the shot renders, Prospero compares the guide and take at the first,
quarter, middle and last frames of the cut. The check uses the production's
character sheet for identity. It reports the score, both hand-to-arm regions
and relevant props at each sample in `studio_scene` → `shots[].control_review`.
A warning needs inspection. You can revise the shot and render it again using
the existing shot tools. The automatic check keeps the take and leaves its
manual continuity review in place.

Importa el MP4 del bloqueo con `studio_import` y usa el ID nuevo como
`control_asset_id` en `studio_scene_plan` o `studio_scene_revise_shot`.
Al renderizar, Prospero compara la guía y la toma al inicio, al cuarto, a la
mitad y en el último fotograma del corte. Usa la hoja de personaje para
comprobar la identidad. Las muestras de `studio_scene` →
`shots[].control_review` recogen la puntuación, cada mano con su brazo y
el atrezo pertinente. Si hay un aviso, revisa la muestra y modifica el plano
con las herramientas existentes si necesita una retoma. La comprobación
conserva la toma y su revisión manual de continuidad.
