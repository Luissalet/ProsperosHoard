import type { SpaceNodeType } from "./api";

type Words = readonly [string, string];
export type NodeTool = {
  id: string; kind: SpaceNodeType; section: string; label: Words; hint: Words;
  data?: Record<string, unknown>;
  inputs?: { port: string; media: "image" | "video" | "audio"; label: Words }[];
};
export type ToolChoice = { kind: SpaceNodeType; handle?: string; tool?: NodeTool; data?: Record<string, unknown> };

export const TOOL_SECTIONS: { id: string; label: Words }[] = [
  { id: "sources", label: ["Referencias y texto", "References and text"] },
  { id: "generate", label: ["Generar", "Generate"] },
  { id: "transform", label: ["Transformar", "Transform"] },
  { id: "assemble", label: ["Montar", "Assemble"] },
  { id: "organize", label: ["Organizar", "Organize"] },
];

// Shortcuts configure native node operations; saved graphs remain portable.
export const NODE_TOOLS: NodeTool[] = [
  { id: "image-reference", kind: "asset", section: "sources", label: ["Referencia de imagen", "Image reference"], hint: ["Subir o elegir fotos de la biblioteca", "Upload or choose library pictures"], data: { kind: "image" } },
  { id: "motion-reference", kind: "asset", section: "sources", label: ["Vídeo de movimiento", "Motion reference"], hint: ["Una guía para el movimiento del personaje", "A guide for the character's movement"], data: { kind: "video" } },
  { id: "audio-reference", kind: "asset", section: "sources", label: ["Audio o canción", "Audio or song"], hint: ["Audio para labios o para el montaje", "Audio for lip sync or assembly"], data: { kind: "audio" } },
  { id: "cast", kind: "cast", section: "sources", label: ["Personaje del reparto", "Cast character"], hint: ["Imagen y descripción de una ficha guardada", "Image and description from a saved entry"], data: { ref_role: "identity" } },
  { id: "setting", kind: "cast", section: "sources", label: ["Escenario del reparto", "Cast location"], hint: ["Referencia de un lugar guardado", "Reference from a saved location"], data: { ref_role: "setting" } },
  { id: "text", kind: "text", section: "sources", label: ["Texto y prompt", "Text and prompt"], hint: ["Descripción reutilizable entre nodos", "A description reusable across nodes"] },
  { id: "image", kind: "image", section: "generate", label: ["Crear imagen", "Create image"], hint: ["Texto y referencias → imagen", "Text and references → image"] },
  { id: "video", kind: "video", section: "generate", label: ["Animar imagen", "Animate image"], hint: ["Imagen inicial → clip; admite una guía opcional", "Start image → clip; accepts one optional guide"] },
  { id: "motion", kind: "video", section: "generate", label: ["Copiar movimiento", "Transfer motion"], hint: ["Personaje y vídeo guía → clip; crea las entradas", "Character and guide video → clip; adds inputs"], data: { motion_engine: "animate", seconds: 3 }, inputs: [
    { port: "start", media: "image", label: ["Imagen del personaje", "Character image"] },
    { port: "motion", media: "video", label: ["Guía de movimiento", "Motion guide"] },
  ] },
  { id: "sing", kind: "video", section: "generate", label: ["Sincronizar labios", "Lip sync"], hint: ["Personaje y audio → clip; crea las entradas", "Character and audio → clip; adds inputs"], data: { seconds: 4.8 }, inputs: [
    { port: "start", media: "image", label: ["Imagen del personaje", "Character image"] },
    { port: "audio", media: "audio", label: ["Voz o canción", "Voice or song"] },
  ] },
  { id: "first-last", kind: "video", section: "generate", label: ["Entre dos fotogramas", "Between two frames"], hint: ["Imagen inicial y final → clip; crea las entradas", "Start and end image → clip; adds inputs"], inputs: [
    { port: "start", media: "image", label: ["Fotograma inicial", "Start frame"] },
    { port: "end", media: "image", label: ["Fotograma final", "End frame"] },
  ] },
  { id: "music", kind: "music", section: "generate", label: ["Música y canción", "Music and song"], hint: ["Estilo y letra → audio", "Style and lyrics → audio"] },
  { id: "assistant", kind: "assistant", section: "generate", label: ["Asistente de prompts", "Prompt assistant"], hint: ["Ideas o descripciones → texto o lista", "Ideas or descriptions → text or list"] },
  { id: "upscale", kind: "edit", section: "transform", label: ["Ampliar imagen", "Upscale image"], hint: ["Imagen → imagen de mayor resolución", "Image → higher resolution image"], data: { operation: "upscale", scale: 2 } },
  { id: "background", kind: "edit", section: "transform", label: ["Quitar fondo", "Remove background"], hint: ["Imagen → recorte con transparencia", "Image → cutout with transparency"], data: { operation: "remove_background" } },
  { id: "pose", kind: "edit", section: "transform", label: ["Extraer pose", "Extract pose"], hint: ["Imagen → guía de postura", "Image → pose guide"], data: { operation: "pose_map" } },
  { id: "depth", kind: "edit", section: "transform", label: ["Extraer profundidad", "Extract depth"], hint: ["Imagen → guía de profundidad", "Image → depth guide"], data: { operation: "depth_map" } },
  { id: "clip-edit", kind: "clip_edit", section: "transform", label: ["Editar vídeo", "Edit video"], hint: ["Clip y descripción → nueva versión del clip", "Clip and description → a new clip version"] },
  { id: "angles", kind: "variations", section: "transform", label: ["Variar ángulos", "Vary angles"], hint: ["Imagen → distintos puntos de vista", "Image → different viewpoints"], data: { mode: "angles" } },
  { id: "expressions", kind: "variations", section: "transform", label: ["Variar expresiones", "Vary expressions"], hint: ["Imagen → distintas expresiones", "Image → different expressions"], data: { mode: "expressions" } },
  { id: "lighting", kind: "variations", section: "transform", label: ["Variar iluminación", "Vary lighting"], hint: ["Imagen → variaciones de luz", "Image → lighting variations"], data: { mode: "lighting" } },
  { id: "storyboard", kind: "variations", section: "transform", label: ["Storyboard", "Storyboard"], hint: ["Imagen → secuencia de encuadres", "Image → a sequence of framings"], data: { mode: "storyboard" } },
  { id: "composite", kind: "composite", section: "assemble", label: ["Superponer capas", "Composite layers"], hint: ["Fondo y capas → imagen o vídeo", "Background and layers → image or video"] },
  { id: "combine", kind: "combine", section: "assemble", label: ["Montar clips y audio", "Assemble clips and audio"], hint: ["Clips ordenados y canción → vídeo", "Ordered clips and song → video"] },
  { id: "list", kind: "list", section: "organize", label: ["Selección y lotes", "Selection and batches"], hint: ["Elegir qué entradas pasan al siguiente nodo", "Choose which inputs reach the next node"] },
  { id: "note", kind: "note", section: "organize", label: ["Nota", "Note"], hint: ["Anotar decisiones junto al flujo", "Keep decisions alongside the workflow"] },
  { id: "group", kind: "group", section: "organize", label: ["Grupo y técnica", "Group and technique"], hint: ["Agrupar nodos y guardar una técnica reutilizable", "Group nodes and save a reusable technique"] },
];

export function searchNodeTools(choices: ToolChoice[], query: string, section = "all"): ToolChoice[] {
  const normalize = (s: string) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const terms = normalize(query).trim().split(/\s+/).filter(Boolean);
  return choices.filter(c => {
    if (section !== "all" && c.tool?.section !== section) return false;
    const text = normalize([c.kind, c.tool?.id, ...(c.tool?.label || []), ...(c.tool?.hint || [])].join(" "));
    return terms.every(term => text.includes(term));
  });
}
