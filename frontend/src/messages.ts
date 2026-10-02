// Texts that come from the backend in English (job labels and progress
// messages, error messages, analysis notes, design-template hints, voice
// engine status). The backend is not changed: they are translated here,
// at display time, when the UI is in Spanish. Anything unknown is returned
// unchanged, so English stays English and a new backend message never breaks.
import { dictionaries, type Lang, type MessageKey } from "./i18n";

// ------------------------------------------------------------------ job types

const JOB_LABELS: Record<Lang, Record<string, string>> = {
  en: {
    generate_image: "Generate", edit_image: "Edit", animate: "Animate", compose_song: "Compose song", render_timeline: "Render",
    download_voice: "Voice download", audiobook: "Audiobook", dub: "Dubbing", install_voice_engine: "Install voice engine",
    download_media: "Download video", reframe: "Reframe", retake: "Retake", clip_edit: "Edit clip", stems: "Stems",
    character_sheet: "Character sheet", train_lora: "Train LoRA", character_caption: "Caption dataset",
    character_identity: "Score likeness", production: "Production", space_run: "Space", production_qa: "QA pass", animatic: "Animatic",
  },
  es: {
    generate_image: "Generar", edit_image: "Editar", animate: "Animar", compose_song: "Componer canción", render_timeline: "Renderizar",
    download_voice: "Descarga de voz", audiobook: "Audiolibro", dub: "Doblaje", install_voice_engine: "Instalar motor de voz",
    download_media: "Descargar vídeo", reframe: "Reencuadre", retake: "Retoque de tramo", clip_edit: "Editar clip", stems: "Pistas",
    character_sheet: "Hoja de personaje", train_lora: "Entrenar LoRA", character_caption: "Describir dataset",
    character_identity: "Puntuar parecido", production: "Producción", space_run: "Espacio", production_qa: "Revisión QA", animatic: "Animático",
  },
};

export function jobLabel(type: string, lang: Lang): string {
  return JOB_LABELS[lang]?.[type] || JOB_LABELS.en[type] || type;
}

/** Job types whose outputs are audio files (their thumbnails are an audio tile, not a picture). */
export const AUDIO_JOB_TYPES = ["stems", "compose_song", "download_voice", "dub", "audiobook"];

// ------------------------------------------------------------------ small vocabularies

const KIND_ES: Record<string, string> = {
  image: "imagen", video: "vídeo", audio: "audio", lyrics: "letra", font: "fuente", sound: "sonido", gif: "GIF",
};
const kindEs = (k: string) => KIND_ES[k] || k;

const OBJECT_ES: Record<string, string> = {
  project: "proyecto", asset: "recurso", character: "personaje", group: "grupo", space: "espacio", board: "tablero",
  timeline: "montaje", job: "trabajo", voice: "voz", production: "producción", recipe: "receta", file: "archivo",
  preview: "vista previa", sample: "muestra", report: "informe", animatic: "animático", "trashed asset": "recurso en la papelera",
  style_preset: "estilo", short: "short", "style preset": "estilo",
};
const objectEs = (k: string) => OBJECT_ES[k] || k;

const STEM_ES: Record<string, string> = { vocals: "voz", drums: "batería", bass: "bajo", other: "resto", instrumental: "instrumental" };

const VIEW_ES: Record<string, string> = {
  front: "frente", three_quarter: "tres cuartos", profile: "perfil", back: "espalda", closeup: "primer plano", happy: "contento",
  angry: "enfadado", scared: "asustado", surprised: "sorprendido", action: "acción", sitting: "sentado", night: "noche",
};

const EXTRA_STAGES: Record<Lang, Record<string, string>> = {
  en: { review: "Review", recipe: "Recipe", extras: "Extras", canvas: "Canvas" },
  es: { review: "Revisión", recipe: "Receta", extras: "Extras", canvas: "Canvas" },
};

/** The name of a pipeline stage (character, frames, clips...) in the UI language. */
export function stageName(stage: string, lang: Lang): string {
  const key = `stage_${stage}` as MessageKey;
  return dictionaries[lang][key] ?? EXTRA_STAGES[lang][stage] ?? stage;
}
const isStage = (s: string) => `stage_${s}` in dictionaries.en || s in EXTRA_STAGES.en;
const lower = (s: string) => s.charAt(0).toLowerCase() + s.slice(1);

/** What a sub-job wait label means ("frames: 3/10 done"). */
const WAIT_LABEL_ES: Record<string, string> = {
  "reference sheet": "hoja de referencia", song: "canción", frames: "fotogramas", clips: "clips", photocards: "photocards",
  renders: "renders", music: "música", pictures: "imágenes",
};

// ------------------------------------------------------------------ job and state messages

type Fn = (m: RegExpExecArray) => string;
type Rule = [RegExp, string | Fn];

const EXACT_ES: Record<string, string> = {
  starting: "empezando",
  done: "hecho",
  cancelled: "cancelado",
  retrying: "reintentando",
  "cancelling...": "cancelando…",
  "requeued after restart": "puesto otra vez en cola tras reiniciar la app",
  "cancelled before it started": "cancelado antes de empezar",
  "cancelled while waiting for the GPU": "cancelado mientras esperaba la GPU",
  "finished before the cancel took effect": "terminó antes de que la cancelación surtiera efecto",
  "outcome_unknown: interrupted image job; not resubmitted automatically":
    "interrumpido (la app se cerró a mitad); no se ha relanzado solo",
  "checking free VRAM": "comprobando la VRAM libre",
  "validating against ComfyUI": "validando con ComfyUI",
  "imported outputs": "resultados importados",
  "reading the clip": "leyendo el clip",
  "contact sheet": "hoja de contactos",
  "sheet ready": "hoja lista",
  "exporting dataset": "exportando el dataset",
  "installing the adapter": "instalando el adaptador",
  "adapter ready": "adaptador listo",
  "adapter saved (set Training -> LoRA folder to install it)": "adaptador guardado (indica la carpeta de LoRA en Entrenamiento para instalarlo)",
  "extracting audio": "extrayendo el audio",
  transcribing: "transcribiendo",
  "mixing audio": "mezclando el audio",
  "muxing final video": "montando el vídeo final",
  "installing Demucs": "instalando Demucs",
  "decoding the song": "descodificando la canción",
  "mixing the instrumental": "mezclando el instrumental",
  assembling: "ensamblando",
  "scoring identity": "puntuando el parecido",
  "cutting the animatic": "cortando el animático",
  "joined clips": "clips unidos",
  "encoding with audio and lyrics": "codificando con audio y letra",
  "lyrics changed": "letra cambiada",
  "song changed": "canción cambiada",
  "the job no longer exists": "el trabajo ya no existe",
  "no vision model": "sin modelo de visión",
  "the run stopped without finishing (the app closed or the job ended); resume it":
    "la ejecución se paró sin terminar (se cerró la app o el trabajo acabó); reanúdala",
  "QA regenerated outputs; the production runs again to rebuild what depends on them":
    "La revisión QA ha regenerado piezas; la producción vuelve a correr para rehacer lo que depende de ellas",
  "silent audio: no beats found": "audio en silencio: no se han encontrado pulsos",
  "sections from the song's timed lyrics; downbeats are estimates":
    "secciones sacadas de la letra sincronizada de la canción; los compases son estimados",
  "downbeats and section labels are estimates (energy/timbre changes), not verse/chorus detection":
    "los tiempos fuertes y las secciones son estimaciones (cambios de energía y timbre), no detectan estrofas ni estribillos",
  // recipe notes
  "the song's lyrics name the original lead, so a new song is composed": "La letra de la canción nombra al protagonista original, así que se compone una canción nueva.",
  "the original song is no longer in the library, so a new one is composed": "La canción original ya no está en la biblioteca, así que se compone una nueva.",
  "clips are only reused together with their frames (a clip starts on its own still)": "Los clips solo se reutilizan junto con sus fotogramas (cada clip arranca de su propio fotograma).",
  "the song's lyrics were not in its recipe": "La letra de la canción no estaba en su receta.",
};

const JOB_RULES: Rule[] = [
  [/^rendering (\d+)\/(\d+) on ComfyUI$/, "renderizando $1/$2 en ComfyUI"],
  [/^rendered clip (\d+)\/(\d+)$/, "clip $1/$2 renderizado"],
  [/^reframing to (.+)$/, "reencuadrando a $1"],
  [/^splitting on (.+)$/, "separando en $1"],
  [/^saving (.+)$/, (m) => `guardando ${STEM_ES[m[1]] || m[1]}`],
  [/^downloading (.+) from Hugging Face$/, "descargando $1 de Hugging Face"],
  [/^installing (.+)$/, "instalando $1"],
  [/^caption (\d+)\/(\d+)$/, "describiendo $1/$2"],
  [/^translating (\d+) segment\(s\) to (.+)$/, "traduciendo $1 segmento(s) a $2"],
  [/^voicing segment (\d+)\/(\d+)$/, "locutando el segmento $1/$2"],
  [/^chapter (\d+)\/(\d+): (.*)$/, "capítulo $1/$2: $3"],
  [/^step (\d+)\/(\d+) · loss (.+)$/, "paso $1/$2 · pérdida $3"],
  [/^step (\d+)\/(\d+)$/, "paso $1/$2"],
  [/^checking (.+)$/, (m) => `revisando ${lower(stageName(m[1], "es"))}`],
  [/^wave (\d+)\/(\d+): (.*)$/, "oleada $1/$2: $3"],
  [/^waiting for (\d+) job\(s\)$/, "esperando $1 trabajo(s)"],
  [/^narration: (\d+)\/(\d+) sentences$/, "locución: $1/$2 frases"],
  [/^visuals: (\d+)\/(\d+)$/, "imágenes: $1/$2"],
  [/^(.+): (\d+)\/(\d+) done$/, (m) => `${WAIT_LABEL_ES[m[1]] || m[1]}: ${m[2]}/${m[3]} hechos`],
  [/^paused after (.+) for review$/, (m) => `en pausa tras «${lower(stageName(m[1], "es"))}» para que lo revises`],
  [/^cancelled during (.+)$/, (m) => `cancelado durante «${lower(stageName(m[1], "es"))}»`],
  [/^changed shot\(s\) (.+)$/, "planos cambiados: $1"],
  [/^background cast changed: redraw shot\(s\) (.+)$/, "reparto de fondo cambiado: se rehacen los planos $1"],
  [/^regenerating (\w+) of shot\(s\) (.+)$/, (m) => `rehaciendo ${lower(stageName(m[1], "es"))} de los planos ${m[2]}`],
  [/^rendering (\d+) draft clip\(s\) as final$/, "renderizando $1 clip(s) borrador como final"],
  [/^no handler registered for job type '(.+)'$/, "no hay ningún manejador para el tipo de trabajo «$1»"],
  [/^timed out waiting: (.*)$/, (m) => `se acabó el tiempo de espera: ${translateText(m[1], "es")}`],
  [/^waiting for (\d+) MB of free VRAM for a (\w+) job \((\d+) MB free now\); retrying every 15 s for up to 30 min\. Nothing is unloaded automatically - use Backends > Free ComfyUI memory if you want to make room\.$/,
    "esperando $1 MB de VRAM libre para un trabajo $2 (ahora hay $3 MB libres); se reintenta cada 15 s hasta 30 min. No se descarga nada solo: usa Backends > Liberar memoria de ComfyUI si quieres hacer sitio."],
  [/^waiting for the family hub's GPU lease \((.*)\); retrying every 15 s for up to 30 min\.$/,
    "esperando la reserva de GPU del hub de la familia ($1); se reintenta cada 15 s hasta 30 min."],
  [/^GPU (\d+) has (\d+) MB free; training needs about (\d+) MB$/, "la GPU $1 tiene $2 MB libres; el entrenamiento necesita unos $3 MB"],
  [/^the freest GPU \((\d+)\) has (\d+) MB free; training needs about (\d+) MB - stop other models or use Backends > Free ComfyUI memory$/,
    "la GPU más libre (la $1) tiene $2 MB libres; el entrenamiento necesita unos $3 MB: para otros modelos o usa Backends > Liberar memoria de ComfyUI"],
  [/^no GPU has the ~(\d+) GB this needs free(?:; (.+) holds memory \(stop it in the header's GPU panel\))?$/,
    (m) => `ninguna GPU tiene libres los ~${m[1]} GB que hacen falta${m[2] ? `; ${m[2]} ocupa memoria (páralo en el panel de GPU de la cabecera)` : ""}`],
  [/^shot (.+) sings but is not placed on its lines in the song track$/, "el plano $1 canta pero no está colocado sobre sus versos en la pista de la canción"],
  [/^animatic (.+?): (.*)$/, (m) => `animático ${m[1]}: ${translateText(m[2], "es")}`],
  // QA reasons
  [/^flat picture \(spread (.+) < (.+)\)$/, "imagen plana (dispersión $1 < $2)"],
  [/^looks like noise \(neighbour\/spread (.+) > (.+)\)$/, "parece ruido (vecino/dispersión $1 > $2)"],
  [/^(top|bottom|left|right) edge band(?: at (\d+%))?, rgb (.+)$/, (m) => `banda en el borde ${({ top: "superior", bottom: "inferior", left: "izquierdo", right: "derecho" } as Record<string, string>)[m[1]]}${m[2] ? ` al ${m[2]}` : ""}, rgb ${m[3]}`],
  [/^the subject touches the top edge \(headroom (.+)\)$/, "el sujeto toca el borde superior (espacio sobre la cabeza $1)"],
  [/^exposure jump of (.+) at frame (\d+) \(~(.+) s\)$/, "salto de exposición de $1 en el fotograma $2 (~$3 s)"],
  [/^moves too much for a still shot \(energy (.+) > (.+)\)$/, "se mueve demasiado para un plano fijo (energía $1 > $2)"],
  [/^barely moves \(energy (.+) < (.+)\)$/, "apenas se mueve (energía $1 < $2)"],
  [/^scored (.+)\/10 on (.+?): (.*)$/, "puntúa $1/10 en $2: $3"],
  [/^only (\d+)\/(\d+) written lines are aligned$/, "solo $1/$2 líneas escritas están alineadas"],
  [/^([\d.]+) s instead of the planned ([\d.]+) s$/, "$1 s en vez de los $2 s previstos"],
  [/^estimated timing \(no vocal alignment to measure\)$/, "tiempos estimados (no hay alineación vocal que medir)"],
  [/^unreadable: (.*)$/, "ilegible: $1"],
  [/^unreadable lyrics: (.*)$/, "letra ilegible: $1"],
  [/^(.+) failed for (.+)$/, (m) => `fallo en ${WAIT_LABEL_ES[m[1]] || m[1]}: ${translateText(m[2], "es")}`],
];

function fill(tpl: string, m: RegExpExecArray): string {
  return tpl.replace(/\$(\d)/g, (_, i: string) => m[Number(i)] ?? "");
}

const PREFIX = /^([\w .-]{1,24}): ([\s\S]+)$/;

/** Translate a backend progress / state / error text. Unknown text comes back unchanged. */
export function translateText(msg: string, lang: Lang, depth = 0): string {
  if (lang !== "es" || !msg || depth > 4) return msg;
  const exact = EXACT_ES[msg.trim()];
  if (exact) return exact;
  if (isStage(msg.trim())) return stageName(msg.trim(), "es");
  for (const [re, to] of JOB_RULES) {
    const m = re.exec(msg);
    if (m) return typeof to === "string" ? fill(to, m) : to(m);
  }
  for (const rule of ERROR_RULES) {
    if (!rule.re) continue;
    const m = rule.re.exec(msg);
    if (m) return render(rule.es, m, msg);
  }
  if (msg.includes("; ")) {  // a list of reasons, one per item ("4: cancelled; 3: out of memory")
    const parts = msg.split("; ");
    const done = parts.map((x) => translateText(x, lang, depth + 1));
    if (done.some((x, i) => x !== parts[i])) return done.join("; ");
  }
  const p = PREFIX.exec(msg);
  if (p) {
    const known = isStage(p[1]) ? stageName(p[1], "es") : VIEW_ES[p[1]];
    const tail = translateText(p[2], lang, depth + 1);
    if (known) return `${known}: ${tail}`;
    if (tail !== p[2]) return `${p[1]}: ${tail}`;
  }
  return msg;
}

/** A job's progress or state message. */
export function jobMessage(msg: string | null | undefined, lang: Lang): string {
  return msg ? translateText(msg, lang) : "";
}

// ------------------------------------------------------------------ API errors

interface ErrorRule {
  code?: string;
  re?: RegExp;
  es: string | Fn;
}

function render(es: string | Fn, m: RegExpExecArray | null, original: string): string {
  if (typeof es === "function") return m ? es(m) : original;
  const out = m ? fill(es, m) : es;
  return out.split("$msg").join(original);
}

const R = (code: string | undefined, es: string | Fn, re?: RegExp): ErrorRule => ({ code, es, re });

const ERROR_RULES: ErrorRule[] = [
  // --- generic and transport
  R("bad_host", "La app no acepta peticiones con ese nombre de host"),
  R("bad_origin", "Escritura rechazada: la petición viene de otro origen"),
  R("cross_site", "Escritura rechazada: la petición viene de otra web"),
  R("stale", "El espacio ha cambiado en otro sitio (versión $1, no $2): recárgalo", /^stale: the space is at version (\d+), not (\d+)$/),
  R("stale", "Se ha editado en otro sitio: recarga y vuelve a intentarlo"),
  R("invalid_arguments", "Datos no válidos: $msg"),
  R("internal_error", "Error interno: $msg"),
  R("backend_error", "Un servidor externo (ComfyUI, el modelo de lenguaje...) ha fallado: $msg"),
  R("bad_workflow", "ComfyUI no acepta este flujo: $msg"),
  R("bad_timeline", "Montaje no válido: $msg"),
  R("bad_design", "Diseño no válido: $msg"),
  R("bad_request", "Petición no válida: $msg"),
  R("not_found", "No se ha encontrado el archivo del pack", /^pack file not found$/),
  R("not_found", "No se ha encontrado la exportación; vuelve a exportar el personaje", /^export not found; export the character again$/),
  R("not_found", "No existe esa ruta de la API: $1", /^no API route \/api\/(.*)$/),
  R("not_found", (m) => `${objectEs(m[1]).charAt(0).toUpperCase()}${objectEs(m[1]).slice(1)} no encontrado: ${m[2]}`, /^([\w ]+) not found: (.+)$/),
  R("no_thumb", "Sin vista previa guardada", /^no preview kept$/),
  R("no_thumb", "Este recurso no tiene miniatura", /^asset has no thumbnail$/),
  R("asset_in_use", (m) => `El recurso ${m[1]} está en uso (${m[2].replace(/\btimeline '/g, "montaje '").replace(/\bcharacter '/g, "personaje '").replace(/\bproduction '/g, "producción '").replace(/\bboard '/g, "tablero '").replace(/\bspace '/g, "espacio '")}); quítalo antes del montaje`, /^asset (\S+) is in use \((.*)\); take it out of the timeline first$/),
  R("asset_in_use", (m) => `El recurso ${m[1]} está en uso (${m[2].replace(/\btimeline '/g, "montaje '").replace(/\bcharacter '/g, "personaje '").replace(/\bproduction '/g, "producción '").replace(/\bboard '/g, "tablero '").replace(/\bspace '/g, "espacio '")}); fuerza el borrado para desvincularlo y borrarlo`, /^asset (\S+) is in use \((.*)\); pass force=true to detach it and delete$/),
  R("project_busy", (m) => `El proyecto tiene ${m[2]} trabajo(s) en cola o en marcha (${m[3].split(", ").map((t) => jobLabel(t, "es").toLowerCase()).join(", ")}): cancélalos o espera a que acaben`, /^project (\S+) has (\d+) job\(s\) queued or running \((.*)\); cancel them or wait for them to finish$/),
  R(undefined, (m) => `Motor de imágenes no disponible: ${translateText(m[1], "es")}`, /^image unavailable: (.*)$/),
  R(undefined, (m) => `Música no disponible: ${translateText(m[1], "es")}`, /^music unavailable: (.*)$/),
  R(undefined, (m) => `Modelo de lenguaje no disponible: ${translateText(m[1], "es")}`, /^llm unavailable: (.*)$/),
  R(undefined, (m) => `Voz (TTS) no disponible: ${translateText(m[1], "es")}`, /^tts unavailable: (.*)$/),
  R(undefined, "ComfyUI no responde ($1): arráncalo en Backends > Servicios locales (o activa el arranque automático)",
    /ComfyUI is not answering \((.*?)\): start it in Backends > Local services \(or studio_service_start\), or turn on autostart/),
  R(undefined, "no se llega a ComfyUI", /^ComfyUI is not reachable$/),
  R(undefined, "ComfyUI no está en marcha; arranca solo cuando la ejecución lo necesita (tarda un minuto)", /^ComfyUI is not running; it starts by itself when the run needs it \(about a minute\)$/),
  R("image_unavailable", "El motor de imágenes no está disponible: $msg"),
  R("music_unavailable", "No hay un modelo de música disponible: $msg"),
  R("llm_unavailable", "No responde ningún modelo de lenguaje local: arranca uno en Backends o escribe tú los planos"),
  R("tts_unavailable", "No hay un motor de voz disponible: $msg"),
  R("http_400", "Petición no válida: $msg"),
  R("http_404", "No se ha encontrado lo que pedías"),
  R("http_409", "Conflicto: $msg"),
  R("http_413", "El archivo es demasiado grande"),
  R("http_422", "Datos no válidos: $msg"),
  R("http_500", "Error interno del servidor: $msg"),
  R("http_502", "Un servidor externo ha fallado: $msg"),
  R("http_503", "El servicio no está disponible ahora mismo"),

  // --- a-b
  R("adapter_required", "La acción «$1» necesita adapter_id", /^'(.+)' needs adapter_id$/),
  R("animate_missing", "Wan Animate 2 no está instalado en ComfyUI (wan_animate_2_distill_int8_convrot.safetensors + clip_vision_h)"),
  R("animatic_failed", "No se ha podido hacer el animático: $msg"),
  R("animatic_needs_frames", "El animático necesita antes los fotogramas (la etapa de fotogramas)"),
  R("animatic_needs_song", "El animático necesita antes la canción (la etapa de canción)"),
  R("arch_unsupported", "El entrenador «$1» no admite la arquitectura $2", /^trainer kind (.+) does not support architecture (.+)$/),
  R("audio_cut_failed", "No se han podido cortar $1 s de audio desde el segundo $2", /^could not cut ([\d.]+) s of audio at ([\d.]+) s$/),
  R("audio_not_audio", "audio_asset_id debe ser una canción o un sonido", /^audio_asset_id must be a song or a sound$/),
  R("audio_not_audio", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}, no audio`, /^asset (\S+) is (\w+), not audio$/),
  R("audio_required", "La plantilla «$1» necesita el audio para cantar o hablar (audio_asset_id, audio_start_s, audio_seconds)", /^template '(.+)' needs the audio to sing or speak/),
  R("bad_animation_params", "Fotogramas entre 4 y 50, fps entre 1 y 30 y movimiento entre 1 y 255"),
  R("bad_arch", "La arquitectura no es válida: $msg"),
  R("bad_aspect", "La proporción debe ser una de: $1", /^aspect must be one of (.+)$/),
  R("bad_blend", "El modo de mezcla no es válido: $msg"),
  R("bad_canvas", "Un Canvas dura entre $1 y $2 segundos", /^a Canvas lasts between (\d+) and (\d+) seconds$/),
  R("bad_crop", "Para recortar hace falta una referencia canónica (la hoja de referencia)", /^canonical_crop needs a canonical_asset_id/),
  R("bad_crop", "El recorte mide menos de 64 px; elige un área mayor", /^the crop is smaller than 64 px/),
  R("bad_crop", (m) => `El recurso ${m[1]} no es una imagen de este proyecto`, /^asset (\S+) is not an image of this project$/),
  R("bad_device", "El dispositivo es cpu o cuda:N"),
  R("bad_draft", "El borrador no es válido: $msg"),
  R("bad_epub", "No se ha podido leer la estructura del EPUB: $1", /^could not read the EPUB structure: (.*)$/),
  R("bad_focus", "focus_x y focus_y van de 0 (izquierda/arriba) a 1 (derecha/abajo)"),
  R("bad_format", "El formato debe ser mp3 o m4b", /^format must be 'mp3' or 'm4b'$/),
  R("bad_format", "El formato debe ser zip, xml o edl", /^format must be zip, xml or edl$/),
  R("bad_fps", "Los fps deben ser 24, 25 o 30"),
  R("bad_framing", "El encuadre no es válido: $msg"),
  R("bad_graph", "El grafo del espacio no es válido: $msg"),
  R("bad_item", "Cada elemento del dataset necesita asset_id", /^each dataset item needs asset_id$/),
  R("bad_item", (m) => `${m[1]} no es una imagen de este proyecto`, /^(\S+) is not an image of this project$/),
  R("bad_kind", "El tipo no es válido: $msg"),
  R("bad_lora", "lora_name es el nombre de archivo que lista ComfyUI (p. ej. prospero/farolchr_qwen_image.safetensors)"),
  R("bad_lyrics", "Envía el texto (LRC o plano) o las líneas [{time_s, text}]", /^send text \(LRC or plain\) or lines/),
  R("bad_lyrics", "Cada línea necesita time_s (segundos) y text", /^each line needs time_s/),
  R("bad_lyrics", "La letra debe ser texto de 512 KB como máximo", /^lyrics must be text of at most 512 KB$/),
  R("bad_lyrics", "La letra debe ser texto (20 000 caracteres como máximo)", /^lyrics must be text \(at most 20000 characters\)$/),
  R("bad_mode", "El modo no es válido: $msg"),
  R("bad_motion_ref", (m) => `La referencia de movimiento ${m[1]} no es un vídeo`, /^motion reference (\S+) is not a video$/),
  R("bad_name", "El nombre no puede estar vacío"),
  R("bad_operation", "La operación no es válida: $msg"),
  R("bad_options", "Las opciones no son válidas: $msg"),
  R("bad_pack", "No es un pack .hoardchar", /^not a \.hoardchar pack$/),
  R("bad_pack", "No es un pack .hoardchar (falta character.json)", /^not a \.hoardchar pack \(no character\.json\)$/),
  R("bad_pack", "No es un pack .hoardchar (zip o json ilegible)", /^not a \.hoardchar pack \(unreadable zip or json\)$/),
  R("bad_pack", "El personaje del pack no tiene nombre", /^the pack's character has no name$/),
  R("bad_pack", "Ruta no segura dentro del pack: $1", /^unsafe path inside the pack: (.*)$/),
  R("bad_pack", "character.json es demasiado grande", /^character\.json is too large$/),
  R("bad_parameter", "cfg debe estar entre 0 y 30", /^cfg must be between 0 and 30$/),
  R("bad_parameter", "La fuerza debe estar entre 0 y 1", /^strength must be between 0 and 1$/),
  R("bad_parameter", "El número debe estar entre 1 y $1", /^count must be between 1 and (\d+)$/),
  R("bad_parameter", "Los BPM deben estar entre 40 y 220", /^bpm must be between 40 and 220$/),
  R("bad_parameter", "La duración debe estar entre 4 y 240 segundos", /^duration must be between 4 and 240 seconds$/),
  R("bad_parameter", "$1 debe estar entre $2 y $3", /^(\w+) must be between ([\d.]+) and ([\d.]+)$/),
  R("bad_parameter", (m) => `${m[1]} debe estar entre ${m[2]} y ${m[3]}`, /^(\w+) must be between (\S+) and (\S+)$/),
  R("bad_parameter", "El instante debe estar entre 0 y la duración del clip ($1 s)", /^at_s must be between 0 and the clip's length \((.*) s\)$/),
  R("bad_path", "Indica la ruta absoluta de un archivo local"),
  R("bad_plan", "Describe el concepto del vídeo", /^describe the concept of the video$/),
  R("bad_plan", "Los planos deben ser entre 2 y 40", /^shots must be between 2 and 40$/),
  R("bad_plan", "El plan no tiene planos", /^the plan has no shots$/),
  R("bad_plan", "La canción necesita estilo y letra (o elige una existente)", /^the song needs tags and lyrics/),
  R("bad_production", "«$1» no es un nombre de producción válido (minúsculas, dígitos, _ y -)", /^'(.*)' is not a production name/),
  R("bad_quality", "La calidad es draft (borrador) o final"),
  R("bad_range", "El inicio debe ser anterior al final", /^start_s must be before end_s$/),
  R("bad_range", "El inicio y el final son segundos dentro del vídeo", /^start_s\/end_s are seconds into the video$/),
  R("bad_range", "El inicio está al final del clip (o más allá)", /^start_s is at \(or past\) the end of the clip$/),
  R("bad_range", "El final debe ir después del inicio", /^end_s must come after start_s$/),
  R("bad_recipe", "«$1» no es una receta de producción", /^'(.*)' is not a production recipe$/),
  R("bad_recipe", "La receta «$1» no se puede leer: $2", /^recipe '(.*)' is unreadable: (.*)$/),
  R("bad_recipe_name", "Una receta necesita un nombre con letras o dígitos"),
  R("bad_reference", (m) => `La referencia ${m[1]} no es una imagen (saca antes fotogramas del vídeo o GIF)`, /^reference (\S+) is not an image/),
  R("bad_resolution", "La resolución debe ser múltiplo de 64 entre 256 y 1536", /^resolution must be a multiple of 64/),
  R("bad_rank", "El rango no es válido: $msg"),
  R("bad_lr", "La tasa de aprendizaje no es válida: $msg"),
  R("bad_steps", "El número de pasos no es válido: $msg"),
  R("bad_script", "El guion no es válido: $msg"),
  R("bad_seeds", "good_seeds debe ser una lista de enteros"),
  R("bad_segment", "El índice del segmento debe estar entre 0 y $1", /^segment index must be between 0 and (\d+)$/),
  R("bad_settings", "Los ajustes no son válidos: $msg"),
  R("bad_shot", "El plano $1 necesita texto", /^shot (\d+) needs text$/),
  R("bad_song", "Indica exactamente uno de asset_id, take o compose", /^give exactly one of asset_id, take or compose$/),
  R("bad_song", "Falta el estilo (tags)", /^tags \(the style\) cannot be empty$/),
  R("bad_song", "La canción no es válida: $msg"),
  R("bad_speed", "La velocidad debe estar entre 0.5 y 2.0"),
  R("bad_stage", "La etapa no es válida: $msg"),
  R("bad_strength", "La fuerza va de 0 a 2 (1 = como se entrenó)"),
  R("bad_threshold", "El umbral de parecido va de 0 a 10"),
  R("bad_trainer_config", "El entrenador personalizado no tiene comando: $msg"),
  R("bad_trigger", "La palabra de activación debe tener 3-32 letras, dígitos o _ (una palabra rara, p. ej. farolchr)"),
  R("bad_url", "Pega un enlace http(s) completo (una página de YouTube, X o Instagram)", /^paste a full http\(s\) link/),
  R("bad_url", "Solo se pueden descargar páginas públicas de vídeo", /^only public video pages can be downloaded$/),
  R("bad_url", "«$1» no es una URL http(s)", /^'(.*)' is not an http\(s\) URL$/),
  R("bad_view", "Elige entre 1 y 12 vistas", /^pick 1-12 views$/),
  R("bad_view", "Hay vistas desconocidas: $msg"),
  R("build_failed", "El modelo local no ha dado un grafo utilizable; prueba con palabras más sencillas o móntalo con operaciones. Detalle: $msg"),
  R("busy", "La producción está en marcha; páusala o espera a que se detenga"),
  R("cancelled", "Entrenamiento cancelado por el usuario", /^training cancelled by user$/),
  R("cancelled", "Cancelado"),
  R("canvas_failed", "No se ha podido crear el Canvas: $msg"),
  R("character_required", "Indica el personaje (character_id)"),
  R("clip_too_short", "Editar un clip necesita al menos medio segundo de vídeo", /^a clip edit needs at least about half a second of video$/),
  R("clip_too_short", "Queda muy poco clip desde ese punto de inicio", /^too little of the clip is left from start_s$/),
  R("cloning_needs_sample", "El motor «$1» necesita una muestra para clonar la voz (una voz con muestra procesada, o voice_ref)", /^engine '(.+)' needs a cloning sample/),
  R("combine_failed", "No se han podido unir los clips: $msg"),
  R("comfy_timeout", "ComfyUI no terminó el trabajo $1 en $2 s. Si la GPU está compartida u ocupada, libérala primero (la cabecera muestra quién la usa) o sube PROSPERO_COMFY_TIMEOUT_S", /^ComfyUI did not finish job (\S+) within (\d+) s/),
  R("comfy_validation", "ComfyUI rechazaría este flujo: $1", /^ComfyUI would reject this workflow: (.*)$/),
  R("comfy_validation", "ComfyUI ha rechazado el flujo: $msg"),
  R("composite_failed", "No se han podido componer las capas: $msg"),
  R("concat_failed", "No se ha podido unir el audio: $msg"),
  R("consistent_needs_reference", "Para mantener la coherencia hace falta una referencia canónica: menciona a un miembro del reparto que la tenga o pasa reference_asset_id"),
  R("control_required", "La plantilla «$1» necesita un vídeo de control y su máscara", /^template '(.+)' needs a control video and its mask$/),
  R("cut_too_short", "El montaje dura $1 s; un bucle de $2 s necesita uno más largo", /^the cut lasts ([\d.]+) s; a (\d+) s loop needs a longer one$/),
  R("cycle", "Las conexiones forman un bucle: un nodo no puede alimentarse a sí mismo"),
  R("dataset_full", "Un dataset admite como máximo $1 imágenes", /^a dataset holds at most (\d+) images$/),
  R("dataset_not_ready", "El dataset no está listo: $msg"),
  R("decode_failed", "No se ha podido descodificar $1", /^could not decode (.*?): .*$/),
  R("decode_failed", "No se ha podido descodificar la canción", /^the song could not be decoded$/),
  R("decode_failed", "No se ha podido leer $1", /^could not read (.*?): .*$/),
  R("download_failed", "El enlace no ha dado ningún archivo (demasiado largo, privado o no es un vídeo)", /^the link gave no file/),
  R("download_failed", "No se ha podido descargar ese enlace: $1", /^could not download that link: (.*)$/),
  R("driving_cut_failed", "No se han podido cortar $1 s del vídeo guía desde el segundo $2 (¿es lo bastante largo?)", /^could not cut ([\d.]+) s of the driving video at ([\d.]+) s/),
  R("driving_not_video", "El vídeo guía (driving_asset_id) debe ser un vídeo: es el movimiento que se copia", /^driving_asset_id must be a video/),
  R("driving_not_video", (m) => `El recurso guía ${m[1]} es ${kindEs(m[2])}, no un vídeo`, /^driving asset (\S+) is (\w+), not a video$/),
  R("driving_video_required", "Animar copia el movimiento de un vídeo: indica driving_asset_id", /^animate copies the motion of a video/),
  R("driving_video_required", "La plantilla «$1» necesita un vídeo guía (driving_asset_id: el movimiento a copiar)", /^template '(.+)' needs a driving video/),
  R("empty_answer", "El modelo local no ha respondido: prueba otra vez o usa otro modelo"),
  R("empty_group", "El grupo «$1» no tiene miembros; añade member_ids", /^group '(.*)' has no members/),
  R("empty_library_entry", "$1 no tiene ninguna versión guardada", /^(\S+) has no saved version$/),
  R("empty_lyrics", "Hace falta la letra (con etiquetas de sección como [Verse] o [Chorus], en inglés)"),
  R("empty_patch", "Hace falta al menos un cambio: nombre, proporción, fps, audio, clips, pistas o acabado"),
  R("empty_pool", "No hay imágenes ni vídeos que cortar; genera o importa alguno, o indica asset_ids / board_id"),
  R("empty_prompt", "Escribe algo para mejorar primero", /^write something to improve first$/),
  R("empty_prompt", "Di qué hay que cambiar en el clip", /^say what to change in the clip$/),
  R("empty_prompt", "El prompt está vacío; describe la imagen (menciona a los personajes como @Nombre)"),
  R("empty_query", "Escribe una búsqueda (unas pocas palabras clave en inglés funcionan mejor)"),
  R("empty_request", "Di qué quieres hacer"),
  R("empty_tags", "El estilo describe el sonido (género, ánimo, instrumentos, tipo de voz)"),
  R("empty_text", "Escribe el texto que se va a locutar", /^give the text to speak$/),
  R("empty_text", "La fuente no tiene texto que narrar", /^the source has no text to narrate$/),
  R("empty_text", "Escribe la frase que se va a locutar", /^give the line to speak$/),
  R("encode_failed", "No se ha podido codificar el audio: $msg"),
  R("end_image_required", "La plantilla «$1» necesita la imagen en la que acaba el clip (end_asset_id)", /^template '(.+)' needs the image the clip ends on/),
  R("end_not_image", "El fotograma final debe ser una imagen", /^end_asset_id must be an image/),
  R("end_not_image", (m) => `El fotograma final ${m[1]} es ${kindEs(m[2])}, no una imagen`, /^end frame (\S+) is (\w+), not an image$/),
  R("engine_not_installed", "«$1» no está instalado: $2", /^'(.+)' is not installed: (.*)$/),
  R("engine_required", "La voz necesita engine_id o voice_id (una voz guardada en la biblioteca)"),
  R("extract_failed", "No se ha podido extraer el audio: $msg"),
  R("ffmpeg_missing", "No se encuentra ffmpeg: instala ffmpeg o el paquete imageio-ffmpeg"),
  R("file_not_found", "No existe el archivo: $1", /^no such file: (.*)$/),
  R("first_frame_required", "Propagar necesita el primer fotograma editado (first_frame_asset_id): saca el primer fotograma del clip, edítalo como imagen y pásalo"),
  R("group_required", "Indica group_id (una tarjeta por miembro) o character_id + cards (una por aspecto)"),
  R("hires_needs_recipe", (m) => `Ampliar vuelve a ejecutar una receta SDXL txt2img a más resolución; el recurso ${m[1]} no se hizo así (usa img2img)`, /^upscale re-runs an SDXL txt2img recipe at a higher resolution; asset (\S+) was not made that way/),
  R("id_required", "Falta el identificador: $msg"),
  R("in_use", (m) => `${m[1]} es el protagonista de ${m[2]}, que no ha terminado. Borrarlo igualmente es seguro: la producción conserva su aspecto.`, /^(.+) is the lead of (.+), which has not finished\. Deleting it anyway is safe/),
  R("inside_app_data", "No se pueden importar archivos de la propia carpeta de datos de la app (solo data/inbox/)"),
  R("install_failed", "No se ha podido instalar: $msg"),
  R("invalid_font", "$1 no es una fuente TrueType/OpenType", /^(.*) is not a TrueType\/OpenType font$/),
  R("invalid_image", "$1 no es una imagen legible", /^(.*) is not a readable image$/),
  R("invalid_lyrics", "$1 no es un archivo de texto", /^(.*) is not a text file$/),
  R("invalid_lyrics", "$1 no es texto UTF-8", /^(.*) is not UTF-8 text$/),
  R("job_not_done", "El trabajo de doblaje aún no ha producido su carpeta de trabajo"),
  R("kind_mismatch", "Un archivo «$1» no se puede importar como $2", /^a '(.*)' file cannot be imported as (\w+);/),
  R("legacy_incomplete", "La producción con guion no tiene paso de personaje (2) del que tomar al protagonista"),
  R("legacy_production", "Una producción con guion no se puede editar aquí: ejecútala desde una receta"),
  R("lyrics_untimed", "La letra no tiene marcas [mm:ss.xx]; sincronízala antes en Audio > Sincronizar letra"),
  R("mask_required", "Para repintar hace falta una máscara (blanco = repintar)"),
  R("missing_argument", "Falta el argumento: $msg"),
  R("missing_fields", "A la plantilla le faltan campos: $msg"),
  R("missing_frame", "El plano $1 no tiene fotograma que animar", /^shot (\S+) has no still to animate$/),
  R("missing_music", "Se ha perdido la música ($1)", /^music asset (\S+) is gone$/),
  R("missing_visuals", "Faltan imágenes para los planos $1", /^no picture for shot\(s\) (.*)$/),
  R("mix_failed", "No se ha podido mezclar el audio: $msg"),
  R("model_missing", "Falta un modelo: «$1» no está en la carpeta $2 de ComfyUI", /^model not installed: '(.*?)' is not in ComfyUI's (\S+) folder/),
  R("mux_failed", "No se ha podido montar el vídeo final: $msg"),
  R("name_required", "Falta un nombre"),
  R("name_taken", "Este proyecto ya tiene un personaje llamado «$1»; usa otro nombre u otro proyecto", /^this project already has a character called '(.*?)'/),
  R("needs_background", "Conecta una imagen o un clip al fondo"),
  R("needs_clip", "Conecta el clip que se va a editar"),
  R("needs_clips", "Conecta clips, en el orden en que se reproducen"),
  R("needs_image", "Conecta imágenes", /^wire pictures into it$/),
  R("needs_image", "Conecta la imagen que se va a variar", /^wire the picture to vary$/),
  R("needs_layers", "Conecta a las capas las imágenes o clips que van encima"),
  R("needs_prompt", "Escribe lo que muestra la imagen o conecta un texto a su prompt", /^write what the picture shows/),
  R("needs_prompt", "Describe el sonido (género, tempo, instrumentos, voz)", /^describe the sound/),
  R("needs_prompt", "Dile al asistente qué escribir, o conecta textos", /^tell the assistant what to write/),
  R("needs_prompt", "Escribe qué cambia en el clip, o conecta un primer fotograma editado", /^write what changes in the clip/),
  R("needs_prompt", "Escribe un cambio por línea", /^write one change per line$/),
  R("needs_start", "Un clip arranca de una imagen: conecta una imagen a su inicio"),
  R("newer_pack", "Este pack usa el formato v$1; actualiza Prospero's Hoard para leerlo", /^this pack uses format v(\S+);/),
  R("no_assets", "Indica uno o más recursos"),
  R("no_base_model", "No hay modelo base para $1: indícalo en los ajustes de entrenamiento", /^no base model set for (\S+):/),
  R("no_bernini", "Falta el modelo Bernini en ComfyUI/models para editar el clip: $msg"),
  R("no_canonical", "$1 necesita antes una imagen canónica (Reparto > editar > referencia)", /^(.+) needs a canonical image first/),
  R("no_canonical", "$1 no tiene imagen canónica de la que dibujar la hoja", /^(.+) has no canonical image to draw the sheet from$/),
  R("no_clips", "Conecta al menos un clip"),
  R("no_cut", "La producción aún no tiene un montaje renderizado; termínala antes"),
  R("no_cut_yet", "Esta producción aún no tiene montaje: continúala hasta la etapa de montaje"),
  R("no_ffmpeg", "Hace falta ffmpeg para esto y no se encuentra"),
  R("no_flf", "Un clip que acaba en un fotograma concreto necesita Wan 2.2 14B de imagen a vídeo (los dos expertos y los LoRA de 4 pasos) en ComfyUI"),
  R("no_frames", "No se ha podido leer ningún fotograma"),
  R("no_group", "No hay ningún grupo $1 en este espacio", /^no group (\S+) in this space$/),
  R("no_inputs", "Este espacio no tiene entradas de app: marca nodos de texto, medios o reparto como entradas"),
  R("no_installer", "«$1» no tiene un instalador conocido; mira docs/VOICE.md", /^'(.*)' has no known installer/),
  R("no_layers", "Conecta al menos una capa sobre el fondo"),
  R("no_llm", "No se ha podido contactar con un modelo de lenguaje; arranca uno en Backends o escribe tú los planos. Detalle: $msg"),
  R("no_manifest", "No hay manifiesto de doblaje"),
  R("no_music", "El trabajo de música no ha producido audio"),
  R("no_music_library", "La música en modo biblioteca elige una pista de la carpeta indicada; pon alguna antes"),
  R("no_node", (m) => `No existe el nodo ${m[1]}`, /^there is no node (.+)$/),
  R("no_node", "No hay nodo $1 en el espacio", /^no node (.+) in this space$/),
  R("no_output", "El entrenador terminó bien pero no produjo ningún .safetensors"),
  R("no_outputs", "ComfyUI terminó pero no guardó ninguna salida; revisa el nodo de guardado del flujo"),
  R("no_python", "No se encuentra el Python de ComfyUI (Backends), donde se ejecutan las pistas", /^stems run in ComfyUI's Python/),
  R("no_python", "No se encuentra Python para el entrenador ai_toolkit"),
  R("no_qa_yet", "Aún no se ha hecho ninguna revisión QA de esta producción; lánzala primero"),
  R("no_reference", "La hoja de referencia no ha producido ninguna imagen"),
  R("no_reference_images", "Ningún miembro del grupo tiene imagen de referencia: genera una por miembro, fija la canónica o pasa image_asset_ids"),
  R("no_song", "La especificación no tiene canción: añade una (estilo + letra) o un asset_id", /^the spec has no song/),
  R("no_song", "El trabajo de canción no ha producido audio", /^the song job produced no audio$/),
  R("no_song", "Esta producción no tiene canción", /^this production has no song$/),
  R("no_song", "Un plano cantado necesita la canción", /^a singing shot needs the song$/),
  R("no_song_yet", "La canción aún no está: elige una o deja que la producción la componga"),
  R("no_speech", "No se ha detectado voz en la pista de audio de este vídeo"),
  R("no_takes", "Aún no hay tomas compuestas; compón antes la canción"),
  R("no_trainer", "No hay ningún entrenador de LoRA configurado; añade uno en Reparto > Kit > Entrenamiento > Configurar"),
  R("no_tts", "No hay ningún motor de voz disponible"),
  R("no_vace", "Falta el modelo VACE en ComfyUI/models para el retoque en borrador: $msg"),
  R("not_a_file", "$1 no es un archivo normal", /^(.*) is not a regular file$/),
  R("not_a_short", "«$1» no es un short narrado", /^'(.*)' is not a narrated short$/),
  R("not_an_image", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}; editar necesita una imagen`, /^asset (\S+) is (\w+); edits need an image$/),
  R("not_an_image", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}; animar necesita una imagen`, /^asset (\S+) is (\w+); animate needs an image$/),
  R("not_an_image", "Solo las imágenes pueden usarse como $1", /^only images can be used as (.*)$/),
  R("not_an_input", "$1 no es una entrada de esta app", /^(.+) is not an input of this app$/),
  R("not_audio", "Las pistas salen de una canción (o del sonido de un vídeo)", /^stems come out of a song/),
  R("not_audio", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}; las pistas salen de una canción o del sonido de un vídeo`, /^asset (\S+) is (\w+): stems come out of a song/),
  R("not_audio", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}; el análisis necesita un recurso de audio (o vídeo)`, /^asset (\S+) is (\w+); analysis needs an audio/),
  R("not_audio", (m) => `${m[1]} es ${kindEs(m[2])}, no una canción (audio)`, /^(\S+) is (\w+), not a song \(audio\)$/),
  R("not_for_shorts", "Un short narrado cambia a través de su guion", /^a narrated short changes through its script/),
  R("not_for_shorts", "Un short narrado hace su propio animático cuando tiene clips que renderizar", /^a narrated short makes its animatic itself/),
  R("not_for_shorts", "Las recetas son para videoclips; para hacer variantes de un short crea varias versiones", /^recipes are for music videos/),
  R("not_for_shorts", "La revisión QA comprueba videoclips; mira el render de un short con la vista previa", /^the QA director checks music videos/),
  R("not_lyrics", "Solo se pueden editar como texto los recursos de letra", /only lyrics assets can be edited as text/),
  R("not_reproducible", (m) => `El recurso ${m[1]} no se generó en ComfyUI, así que no tiene receta que repetir; usa img2img`, /^asset (\S+) was not generated on ComfyUI, so it has no recipe/),
  R("not_reproducible", "Este recurso no se generó en ComfyUI: solo se pueden repetir imágenes y animaciones generadas", /was not generated on ComfyUI \(/),
  R("not_runnable", "Los nodos «$1» no se ejecutan", /^(.+) nodes do not run$/),
  R("not_video", "Un retoque rehace parte de un clip", /^a retake redoes part of a clip$/),
  R("not_video", "Solo se puede editar así un clip", /^only a clip can be edited this way$/),
  R("not_video", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}, no un vídeo`, /^asset (\S+) is (\w+), not video$/),
  R("not_video", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}; solo se pueden unir clips`, /^asset (\S+) is (\w+); only clips can be joined$/),
  R("not_video", (m) => `El recurso ${m[1]} es ${kindEs(m[2])}; un fotograma sale de un vídeo`, /^asset (\S+) is (\w+); (?:a frame|frames|a last frame) comes? out of/),
  R("not_visual", "Solo se pueden reencuadrar imágenes y clips"),
  R("nothing_to_run", "No hay ningún nodo de imagen, clip o canción que ejecutar"),
  R("outside_import_folders", "Ese archivo está fuera de las carpetas desde las que Prospero puede importar. Muévelo a una de ellas o añade su carpeta en Ajustes > Carpetas de importación."),
  R("path_required", "Importar e inspeccionar necesitan una ruta (un archivo .hoardchar)"),
  R("piper_not_installed", "Piper no está instalado en este entorno: vuelve a ejecutar «pip install -r requirements-lock.txt»"),
  R("plan_unreadable", "No se ha podido leer el plan JSON del modelo (una respuesta cortada por el límite de tokens se ve así: prueba con menos planos)", /did not parse/),
  R("plan_unreadable", "La respuesta del modelo no traía ningún plan JSON"),
  R("process_failed", "No se ha podido procesar el audio: $msg"),
  R("production_exists", "Ya existe una producción llamada «$1»", /^a production called '(.*)' already exists$/),
  R("production_not_finished", "La producción «$1» aún no tiene fotogramas terminados", /^production '(.*)' has no finished frames yet$/),
  R("production_required", "Indica la producción (slug)"),
  R("production_running", "La producción está en marcha: espera a que se pause o termine", /^the production is running; wait for it to pause or finish$/),
  R("production_running", "La producción está en marcha; su propia etapa de animático hace uno", /its own animatic stage makes one$/),
  R("production_running", "La producción está en marcha; ella misma sincroniza la letra", /it times the lyrics itself$/),
  R("production_running", "La producción está en marcha: la revisión QA corre dentro de ella (si está activada) o cuando se pause o termine", /^the production is running; QA runs inline/),
  R("production_running", "El short está en marcha; cancélalo o espera antes de cambiar su guion", /^the short is running/),
  R("project_required", "Hace falta un proyecto"),
  R("prompt_too_long", "El prompt tiene más de 4000 caracteres"),
  R("recipe_required", "Indica el nombre de la receta"),
  R("reference_not_image", (m) => `La referencia ${m[1]} es ${kindEs(m[2])}, no una imagen`, /^reference (\S+) is (\w+), not an image$/),
  R("reference_not_image", "La referencia $1 no es una imagen", /^reference (\S+) is not a picture$/),
  R("reference_required", "El modo referencia necesita al menos una imagen de referencia (o el @Nombre de un personaje con imagen)", /^the reference mode needs/),
  R("reference_required", "La plantilla «$1» necesita una imagen de referencia", /^template '(.+)' needs a reference image/),
  R("reference_required", "La plantilla «$1» necesita al menos $2 imágenes de referencia", /^template '(.+)' needs at least (\d+) reference image/),
  R("reframe_failed", "No se ha podido reencuadrar: $msg"),
  R("render_failed", "El render ha fallado: $msg"),
  R("retake_too_long", "Un retoque rehace como mucho $1 s de una vez: divídelo en dos", /^a retake redoes at most (.*?) s at a time/),
  R("retake_too_short", "Elige al menos un cuarto de segundo para rehacer", /^pick at least a quarter of a second/),
  R("retake_too_short", "Ese tramo está más allá del final del clip", /^that stretch is past the end of the clip$/),
  R("run_required", "Falta el identificador de la ejecución (run_id)"),
  R("script_unreadable", "La respuesta del modelo no traía ningún guion JSON", /^the model's reply held no JSON script$/),
  R("script_unreadable", "No se ha podido leer el guion JSON del modelo: $1", /^the model's JSON script did not parse: (.*)$/),
  R("sheet_needs_edit_engine", "La hoja de modelo necesita un motor de edición que conserve la identidad (Qwen-Image 2.1 o Flux Kontext) y ninguno está instalado en ComfyUI"),
  R("shot_locked", "El plano $1 está aprobado (bloqueado); desbloquéalo para cambiar $2", /^shot (\S+) is approved \(locked\); unlock it to change (.*)$/),
  R("shots_required", "Indica al menos un plano: {text, duration_s?, image?}"),
  R("silent_sample", "$1 está en silencio (o muy bajo) de principio a fin: no queda nada tras recortar los silencios", /^(.*) is silent \(or too quiet\)/),
  R("sing_needs_span", "El plano $1 canta: colócalo antes sobre sus versos en la pista de la canción", /^shot (\S+) sings: place it on its lines/),
  R("song_required", "La acción «auto» necesita song_asset_id (un recurso de audio)"),
  R("source_required", "Indica asset_id o path", /^give asset_id or path$/),
  R("source_required", "Indica source_path o video_asset_id", /^give source_path or video_asset_id$/),
  R("source_required", "La plantilla «$1» necesita el vídeo que se va a editar", /^template '(.+)' needs the video to edit$/),
  R("space_required", "Indica el espacio (id)"),
  R("split_failed", "Demucs no ha podido separar la canción: $msg"),
  R("stock_download_failed", "No se ha podido descargar el vídeo de archivo: $msg"),
  R("stock_key_rejected", "$1 ha rechazado la clave de API (HTTP $2); revísala en Ajustes", /^(\S+) rejected the API key \(HTTP (\d+)\)/),
  R("stock_no_file", "$1 no tiene archivo descargable", /^(.*) has no downloadable file$/),
  R("stock_not_configured", "Ningún proveedor de vídeo de archivo tiene clave de API: añade una clave gratuita de Pexels o Pixabay en Ajustes > Vídeo de archivo (o PEXELS_API_KEY / PIXABAY_API_KEY)"),
  R("stock_rate_limited", "$1 ha alcanzado su límite de peticiones; prueba de nuevo dentro de un rato", /^(\S+) rate limit reached/),
  R("stock_search_failed", "La búsqueda de vídeo de archivo ha fallado: $msg"),
  R("stt_not_installed", "No hay ningún motor de voz a texto instalado; instala faster-whisper (pip install faster-whisper)"),
  R("sub_job_failed", (m) => `Fallo en ${WAIT_LABEL_ES[m[1]] || m[1]}: ${translateText(m[2], "es")}`, /^(.+?) failed for (.+)$/),
  R("text_required", "Indica un texto o source_path (.txt/.md/.epub)"),
  R("text_too_long", "Una línea de voz admite 1500 caracteres como máximo; divide el texto", /^a voice line is limited to 1500 characters/),
  R("text_too_long", "Máximo 20 000 caracteres por llamada; divide el texto", /^at most 20000 characters per call/),
  R("time_fit_failed", "No se ha podido ajustar la duración del audio: $msg"),
  R("timeline_required", "Indica el montaje (timeline_id), o la producción y la proporción"),
  R("title_required", "Una producción necesita un título"),
  R("too_big", "El pack pesa más de 2 GB"),
  R("too_few_images", "El dataset tiene $1 imagen(es) utilizable(s); añade al menos 8", /^the dataset has (\d+) usable image/),
  R("too_few_images", "Hacen falta al menos 4 imágenes de dataset; hay $1", /^need at least 4 dataset images, got (\d+)$/),
  R("too_large", "El pack pesa más de 2 GB", /^the pack is larger than 2 GB$/),
  R("too_large", "El archivo supera el límite de subida", /exceeds the upload size limit/),
  R("too_large", "Los clips de dictado están limitados a 30 MB", /^dictation clips are limited to 30 MB$/),
  R("too_large", "El archivo supera $1 MB", /^file exceeds (\d+) MB$/),
  R("too_large", "Las imágenes están limitadas a $1 MB", /^images are limited to (\d+) MB$/),
  R("too_large", "Los archivos de audio y vídeo están limitados a 2 GB", /^audio and video files are limited to 2 GB$/),
  R("too_large", "La imagen mide $1x$2; el límite es de $3 megapíxeles", /^image is (\d+)x(\d+); the limit is (\d+) megapixels$/),
  R("too_large", "Los archivos de letra están limitados a 512 KB", /^lyrics files are limited to 512 KB$/),
  R("too_large", "Las fuentes están limitadas a 20 MB", /^fonts are limited to 20 MB$/),
  R("too_large", "Ampliar la fuente de $1x$2 por x$3 daría $4x$5; el máximo es $6 px por lado", /^upscaling the (\d+)x(\d+) source by x(\d+) would give (\d+)x(\d+); the maximum is (\d+) px on a side/),
  R("too_large", "El archivo de vídeo de archivo pesa más de 400 MB", /^the stock file is over 400 MB$/),
  R("too_long", "El texto supera el máximo de caracteres: $msg"),
  R("too_many", "Se pueden mostrar como mucho 24 recursos a la vez"),
  R("too_many_references", "La plantilla «$1» admite como máximo $2 imágenes de referencia", /^template '(.+)' accepts at most (\d+) reference images$/),
  R("too_many_shots", "Como mucho $1 planos; divide el storyboard", /^at most (\d+) shots/),
  R("trainer_failed", "El entrenador ha fallado: $msg"),
  R("trainer_unavailable", "Ningún entrenador configurado puede entrenar ahora: $msg"),
  R("tts_not_installed", "No hay ningún motor de voz instalado (Voz > Motores instala Piper u otro)"),
  R("unknown_adapter", "El personaje no tiene el adaptador $1", /has no adapter (\S+)$/),
  R("unknown_asset", "El recurso no existe: $msg"),
  R("unknown_engine", "Motor desconocido: $msg"),
  R("unknown_library_entry", "No existe el personaje de biblioteca $1", /^no library character (\S+)$/),
  R("unknown_ref", "Busca primero; referencias desconocidas: $1", /^search first; unknown ref\(s\): (.*)$/),
  R("unknown_style", "Estilo desconocido: $msg"),
  R("unknown_template", "Plantilla desconocida: $msg"),
  R("unknown_trainer", "No hay ningún entrenador con ese nombre: $msg"),
  R("unknown_voice", "Voz desconocida «$1»", /^unknown voice '(.*?)'/),
  R("unknown_voice_backend", "Motor de voz desconocido: usa «piper» o «faustus»"),
  R("unreadable", "$1 no es un archivo de audio legible", /^(.*) is not a readable audio file$/),
  R("unreadable", "$1 no contiene muestras de audio", /^(.*) decoded to no audio samples$/),
  R("unsupported_file", "Tipo de archivo no admitido «$1»; usa .txt, .md o .epub", /^unsupported source file type '(.*)'/),
  R("unsupported_kind", "No se pueden importar archivos «$1»", /^cannot import '(.*?)' files/),
  R("video_not_found", "No se encuentra el vídeo: $1", /^video not found: (.*)$/),
  R("voice_download_failed", "No se ha podido descargar la voz «$1» de Hugging Face: $2", /^could not download voice '(.*?)' from Hugging Face: (.*)$/),
  R("wrong_kind", "$1 admite $2, no $3", /^(\S+) takes (\w+), not (\w+)$/),
  R("wrong_project", "Ese elemento pertenece a otro proyecto"),
  R("ytdlp_missing", "yt-dlp no está instalado en el entorno de Prospero: pip install yt-dlp"),

  // --- validation errors that mostly an agent hits: a short Spanish lead, the detail stays as the backend wrote it
  R("bad_count", "El número de versiones debe estar entre 1 y 8", /^count must be between 1 and 8$/),
  R("bad_colour", "Color no válido en un campo: $msg"),
  R("bad_variant", "Variante no válida: $msg"),
  R("unknown_field", "La plantilla no tiene ese campo: $msg"),
  R("unknown_preset", "La voz no tiene ese preset: $msg"),
  R("unknown_version", "Esa versión no existe: $msg"),
  R("unknown_argument", "Argumento desconocido: $msg"),
  R("unknown_patch_field", "Campo de cambio desconocido: $msg"),
  R("bad_cast", "Reparto no válido: $msg"),
  R("bad_changes", "Cambios no válidos: $msg"),
  R("bad_spec", "Especificación no válida: $msg"),
  R("bad_node", "Nodo no válido: $msg"),
  R("bad_edge", "Conexión no válida: $msg"),
  R("bad_op", "Operación no válida: $msg"),
  R("bad_orientation", "Orientación no válida: $msg"),
  R("bad_aspects", "Proporciones no válidas: $msg"),
  R("bad_cards", "Tarjetas no válidas: $msg"),
  R("bad_fields", "Campos no válidos: $msg"),
  R("bad_finishing", "Acabado no válido: $msg"),
  R("bad_outputs", "Salidas no válidas: $msg"),
  R("bad_source", "Origen no válido: $msg"),
  R("bad_technique", "No es una técnica de espacio válida: $msg"),
  R("bad_trainer_kind", "Tipo de entrenador desconocido: $msg"),
  R("bad_audio", "Audio no válido: $msg"),
  R("bad_file", "Archivo no válido: $msg"),
  R("bad_setting", "Ajuste no válido: $msg"),
  R("bad_stock_keys", "Claves de vídeo de archivo no válidas: $msg"),
  R("bad_action", "Acción no válida: $msg"),
  R("bad_adapter", "Adaptador no válido: $msg"),
  R("bad_run", "Entrenamiento desconocido: $msg"),
  R("bad_sort", "Orden no válido: $msg"),
  R("bad_take", "Toma no válida: $msg"),

  // --- text without a code that jobs record
  R(undefined, "ffmpeg ha fallado", /^ffmpeg failed$/),
  R(undefined, "pip install ha fallado", /^pip install failed$/),
  R(undefined, "el servicio de voz no ha devuelto audio", /^the TTS backend returned no audio$/),
  R(undefined, "el motor de voz no ha devuelto audio", /^no audio returned$/),
];

/** The Spanish text of an API error, or the original message when it is unknown (or the UI is English). */
export function errorText(code: string | undefined | null, message: string, lang: Lang): string {
  if (lang !== "es" || !message) return message;
  // the code and the shape of the message together
  for (const rule of ERROR_RULES) {
    if (!rule.re || (rule.code && rule.code !== code)) continue;
    const m = rule.re.exec(message);
    if (m) return render(rule.es, m, message);
  }
  // the code alone
  for (const rule of ERROR_RULES) {
    if (rule.re || !rule.code || rule.code !== code) continue;
    return render(rule.es, null, message);
  }
  return translateText(message, lang);
}

// ------------------------------------------------------------------ production lineage

const LINEAGE_ES: Record<string, string> = {
  album: "arte del single hecho", animatic: "animático hecho", approved: "aprobado", canonical_reference: "referencia canónica fijada",
  canvas: "Canvas creado", chain_fallback: "encadenado sin éxito: se usó el fotograma del plano", changed_cast: "reparto cambiado",
  changed_finishing: "acabado cambiado", changed_lyrics: "letra cambiada", changed_settings: "ajustes cambiados", changed_shot: "plano cambiado",
  changed_song: "canción cambiada", clip: "clip hecho", copied_character: "personaje copiado", created_character: "personaje creado",
  created_from_recipe: "creada desde una receta", created_project: "proyecto creado", deleted_shot: "plano borrado", failed: "falló",
  frame: "fotograma hecho", generated_shot: "plano generado", inserted_shot: "plano insertado", kept_canonical_reference: "referencia canónica conservada",
  locked_shot: "plano bloqueado", mix: "mezcla hecha", narration: "locución hecha", paused_for_review: "en pausa para revisar",
  promoted_clips: "clips pasados a final", qa_gave_up: "QA se rinde", qa_retry: "QA repite", qa_swap_variant: "QA cambia de variante",
  reframed: "reencuadrado", regenerated_unlocked: "rehechos los no aprobados", render: "render hecho", reused_clip: "clip reutilizado",
  reused_frame: "fotograma reutilizado", reused_song: "canción reutilizada", script: "guion hecho", script_replaced: "guion reemplazado",
  song_takes: "tomas de la canción", song_takes_for_review: "tomas de la canción para elegir", stale_run_recovered: "ejecución interrumpida recuperada",
  stock_fallback: "sin vídeo de archivo: se genera", stock_fetch_failed: "falló la descarga del vídeo de archivo",
  stock_not_configured: "sin clave de vídeo de archivo", stock_search_failed: "falló la búsqueda de vídeo de archivo", stock_shot: "plano con vídeo de archivo",
  stt_failed: "falló la transcripción", sub_job_failed: "falló un subtrabajo", timed_lyrics: "letra sincronizada", timeline: "montaje hecho",
  used_take: "toma elegida", voice_fallback: "voz alternativa",
};

/** A lineage (history) event name, e.g. `reused_clip` -> "clip reutilizado". */
export function lineageEvent(event: string, lang: Lang): string {
  return lang === "es" ? LINEAGE_ES[event] || event.replace(/_/g, " ") : event.replace(/_/g, " ");
}

// ------------------------------------------------------------------ audio analysis

/** "section A" -> "sección A". User lyric sections (Verse, Chorus) stay as written. */
const SECTION_ES: Record<string, string> = {
  intro: "intro", verse: "estrofa", "pre-chorus": "preestribillo", prechorus: "preestribillo", "pre chorus": "preestribillo",
  chorus: "estribillo", "post-chorus": "postestribillo", hook: "gancho", bridge: "puente", breakdown: "breakdown",
  drop: "drop", interlude: "interludio", solo: "solo", outro: "final", instrumental: "instrumental", refrain: "estribillo",
};
/** A song section's name: "section A" (audio guess) or a lyric tag like "Verse 2" / "Chorus". */
export function sectionLabel(label: string, lang: Lang): string {
  if (lang !== "es") return label;
  const m = /^section ([A-Z])$/.exec(label);
  if (m) return `sección ${m[1]}`;
  const t = /^([a-z][a-z -]*?)\s*(\d*)$/i.exec(label.trim());
  if (t && SECTION_ES[t[1].toLowerCase()]) return `${SECTION_ES[t[1].toLowerCase()]}${t[2] ? " " + t[2] : ""}`;
  return label;
}

const ENERGY_ES: Record<string, string> = { low: "baja", mid: "media", high: "alta" };
/** The energy word of a section (low / mid / high). */
export function energyWord(energy: string, lang: Lang): string {
  return lang === "es" ? ENERGY_ES[energy] || energy : energy;
}

// ------------------------------------------------------------------ design templates

const FIELD_NAME_ES: Record<string, string> = {
  image: "imagen", member_name: "nombre del miembro", role: "puesto", group_name: "nombre del grupo", accent: "color de acento",
  group_logo: "logo del grupo", message: "mensaje", serial: "número de serie", cover_image: "imagen de portada", title: "título",
  subtitle: "subtítulo", artist: "artista", tagline: "eslogan", date: "fecha", quote: "cita", attribution: "atribución",
  tracks: "canciones", credits: "créditos",
};
const FIELD_HINT_ES: Record<string, string> = {
  "member photo": "foto del miembro",
  "name printed on the card": "nombre impreso en la tarjeta",
  "position, e.g. 'Main Vocal'": "puesto, p. ej. «Voz principal»",
  "small group name above the member name": "nombre pequeño del grupo, sobre el del miembro",
  "frame and name colour": "color del marco y del nombre",
  name: "nombre",
  "used for the monogram when there is no logo": "se usa para el monograma cuando no hay logo",
  "logo image": "imagen del logo",
  "handwritten-style message": "mensaje con letra manuscrita",
  "e.g. 'No. 007/250'": "p. ej. «N.º 007/250»",
  "gradient and frame colour": "color del degradado y del marco",
  "cover art": "arte de la portada",
  "album or group title": "título del álbum o del grupo",
  "e.g. '1st Mini Album'": "p. ej. «1.er mini álbum»",
  "artist line (night variant: small, top left)": "línea del artista (variante noche: pequeña, arriba a la izquierda)",
  "subtitle colour": "color del subtítulo",
  "key visual": "imagen principal",
  headline: "titular",
  "second line": "segunda línea",
  "release date line": "línea con la fecha de salida",
  "tagline colour": "color del eslogan",
  "background image": "imagen de fondo",
  "the lyric": "la letra",
  "song / artist line": "línea de canción / artista",
  "attribution colour": "color de la atribución",
  "small cover (night variant: blurred full-bleed backdrop)": "portada pequeña (variante noche: fondo desenfocado a toda la página)",
  "group name": "nombre del grupo",
  "release title under the name": "título del lanzamiento bajo el nombre",
  "one track per line": "una canción por línea",
  "small print at the bottom": "letra pequeña al pie",
  "title colour": "color del título",
  background: "fondo",
  "video title": "título del vídeo",
};
const VARIANT_ES: Record<string, string> = {
  center_title: "título centrado", bottom_band: "banda inferior", corner_minimal: "esquina minimalista", night: "noche", classic: "clásica",
};

/** A design-template field's name (`member_name` -> "nombre del miembro"). */
export function fieldName(name: string, lang: Lang): string {
  return lang === "es" ? FIELD_NAME_ES[name] || name.replace(/_/g, " ") : name.replace(/_/g, " ");
}
/** A design-template field's hint, which the backend sends in English. */
export function fieldHint(description: string, lang: Lang): string {
  return lang === "es" ? FIELD_HINT_ES[description] || description : description;
}
/** A design-template variant (`bottom_band` -> "banda inferior"). */
export function variantName(variant: string, lang: Lang): string {
  return lang === "es" ? VARIANT_ES[variant] || variant.replace(/_/g, " ") : variant.replace(/_/g, " ");
}

// ------------------------------------------------------------------ voice studio

const ENGINE_LABEL_ES: Record<string, string> = {
  "Piper (curated voices)": "Piper (voces seleccionadas)",
  "ComfyUI TTS node pack": "Paquete de nodos TTS de ComfyUI",
  "Base TTS": "TTS base",
  "Base STT": "STT base",
};
/** A voice engine's name. */
export function voiceEngineLabel(label: string, lang: Lang): string {
  return lang === "es" ? ENGINE_LABEL_ES[label] || label : label;
}

const ENGINE_TEXT_RULES: Rule[] = [
  [/^installed and ready$/, "instalado y listo"],
  [/^not installed - (.*)$/, (m) => `no instalado - ${voiceEngineText(m[1], "es")}`],
  [/^no installer known$/, "sin instalador conocido"],
  [/^install a TTS custom node pack in ComfyUI \(e\.g\. (.*)\) and restart it$/, "instala un paquete de nodos TTS en ComfyUI (p. ej. $1) y reinícialo"],
  [/^ComfyUI has TTS node\(s\): (.*) \(no built-in workflow template yet\)$/, "ComfyUI tiene nodos TTS: $1 (todavía sin plantilla de flujo propia)"],
];
/** An engine's status sentence or install hint. Commands such as `pip install X` stay as they are. */
export function voiceEngineText(text: string | null | undefined, lang: Lang): string {
  if (!text) return "";
  if (lang !== "es") return text;
  for (const [re, to] of ENGINE_TEXT_RULES) {
    const m = re.exec(text);
    if (m) return typeof to === "string" ? fill(to, m) : to(m);
  }
  return text;
}

const VOICE_WARNING_RULES: Rule[] = [
  [/^very short sample \(([\d.]+)s\) - at least 3-10s of clean speech is recommended$/, "muestra muy corta ($1 s): se recomiendan al menos 3-10 s de voz limpia"],
  [/^long sample \(([\d.]+) min\) - most cloning engines only need 6-30s$/, "muestra larga ($1 min): la mayoría de motores de clonación solo necesitan 6-30 s"],
  [/^low signal-to-noise ratio \(~(.*) dB\) - background noise or room echo will bleed into the clone$/, "relación señal/ruido baja (~$1 dB): el ruido de fondo o el eco de la sala pasarán a la voz clonada"],
  [/^clipping detected \(([\d.]+)% of samples near full scale\) - re-record a bit quieter$/, "saturación detectada ($1 % de las muestras al máximo): vuelve a grabar un poco más bajo"],
];
/** A voice-sample quality warning. */
export function voiceWarning(text: string, lang: Lang): string {
  if (lang !== "es") return text;
  for (const [re, to] of VOICE_WARNING_RULES) {
    const m = re.exec(text);
    if (m) return typeof to === "string" ? fill(to, m) : to(m);
  }
  return text;
}

// ------------------------------------------------------------------ asset kinds and sources

const KIND_LABEL: Record<Lang, Record<string, string>> = {
  en: { image: "image", video: "video", audio: "audio", lyrics: "lyrics", font: "font", layout: "layout", design: "design" },
  es: { image: "imagen", video: "vídeo", audio: "audio", lyrics: "letra", font: "fuente", layout: "maqueta", design: "diseño" },
};
const SOURCE_LABEL: Record<Lang, Record<string, string>> = {
  en: { generated: "generated", rendered: "rendered", import: "imported", derived: "derived", stock: "stock" },
  es: { generated: "generado", rendered: "renderizado", import: "importado", derived: "derivado", stock: "de archivo" },
};
/** An asset kind as a word of the UI language. */
export function kindName(kind: string, lang: Lang): string {
  return KIND_LABEL[lang]?.[kind] || kind;
}
/** Where an asset came from, as a word of the UI language. */
export function sourceName(source: string, lang: Lang): string {
  return SOURCE_LABEL[lang]?.[source] || source;
}

// ------------------------------------------------------------------ names the studio gives its outputs

const NAME_PREFIX_ES: Record<string, string> = {
  upscaled: "ampliada", pose: "pose", depth: "profundidad", "background removed": "sin fondo", vary: "variación",
  reuse: "reutilizada", animated: "animada", img2img: "retocada", inpaint: "repintada", outpaint: "ampliada por los bordes",
  hires: "en alta", edit: "editada", relight: "reiluminada", restyle: "con otro estilo", upscale: "ampliada",
  remove_background: "sin fondo", pose_map: "pose", depth_map: "profundidad",
};
const NAME_RULES: [RegExp, (m: RegExpExecArray) => string][] = [
  [/^Auto-cut - (.+)$/, (m) => `Montaje automático - ${assetName(m[1], "es")}`],
  [/^(.+) - timed lyrics$/, (m) => `${assetName(m[1], "es")} - letra sincronizada`],
  [/^(.+) \((final|preview)\)$/, (m) => `${assetName(m[1], "es")} (${m[2] === "final" ? "final" : "vista previa"})`],
  [/^(.+) retake ([\d.]+-[\d.]+s)$/, (m) => `${assetName(m[1], "es")} · retoma ${m[2]}`],
  [/^(.+) edited(?: ([\d.]+-[\d.]+s))?$/, (m) => `${assetName(m[1], "es")} · editado${m[2] ? ` ${m[2]}` : ""}`],
  [/^(.+) frame ([\d.]+s)(\.\w+)?$/, (m) => `${assetName(m[1], "es")} · fotograma ${m[2]}${m[3] || ""}`],
  [/^(.+) frame (\d+)(\.\w+)?$/, (m) => `${assetName(m[1], "es")} · fotograma ${m[2]}${m[3] || ""}`],
  [/^(.+) last frame(\.\w+)?$/, (m) => `${assetName(m[1], "es")} · último fotograma${m[2] || ""}`],
  [/^joined (\d+) clips(\.\w+)?$/, (m) => `${m[1]} clips unidos${m[2] || ""}`],
  [/^composite of (\d+)(\.\w+)?$/, (m) => `composición de ${m[1]}${m[2] || ""}`],
  [/^upscaled x(\d+): (.+)$/, (m) => `ampliada x${m[1]}: ${assetName(m[2], "es")}`],
];

/** The name of an asset as the reader's language would put it: the names the studio made up itself
 * ("Auto-cut - ...", "... - timed lyrics", "upscaled: ...") are translated; a name someone wrote stays as it is. */
export function assetName(name: string | null | undefined, lang: Lang, depth = 0): string {
  if (!name || lang !== "es" || depth > 4) return name || "";
  for (const [re, to] of NAME_RULES) {
    const m = re.exec(name);
    if (m) return to(m);
  }
  const p = /^([a-z][a-z0-9 _]{1,24}): (.+)$/.exec(name);
  if (p && NAME_PREFIX_ES[p[1]]) return `${NAME_PREFIX_ES[p[1]]}: ${assetName(p[2], lang, depth + 1)}`;
  return name;
}

/** A production's status as a message key (queued, running, awaiting_review...). */
export const PRODUCTION_STATUS_KEY: Record<string, MessageKey> = {
  queued: "stateQueued", running: "stateRunning", awaiting_review: "statusAwaiting", done: "stateDone",
  failed: "stateFailed", cancelled: "stateCancelled", partial: "statusPartial",
};
