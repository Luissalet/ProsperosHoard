// Thin typed client for the Prospero's Hoard HTTP API (see docs/API.md).

export type Kind = "image" | "video" | "audio" | "lyrics" | "font" | "layout";

export interface Recipe {
  operation?: string;
  backend?: string;
  template?: string;
  template_hash?: string;
  checkpoint?: string;
  params?: Record<string, unknown>;
  input_asset_ids?: string[];
  elapsed_s?: number;
  prompt?: string;
  style?: string;
  fields?: Record<string, unknown>;
  variant?: string;
  derived_from?: string;
  rerun?: string;
  text?: string;
  provider?: string;
  timeline_id?: string;
  quality?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface Section {
  label: string;
  start_s: number;
  end_s: number;
  energy: "low" | "mid" | "high";
}

export interface Analysis {
  duration_s: number;
  tempo_bpm: number | null;
  beat_times: number[];
  downbeats: number[];
  sections: Section[];
  notes?: string;
}

export interface Asset {
  id: string;
  project_id: string;
  kind: Kind;
  name: string | null;
  file_path: string;
  mime: string | null;
  width: number | null;
  height: number | null;
  duration_s: number | null;
  thumb_path: string | null;
  tags: string[];
  rating: number;
  favourite: boolean;
  notes: string | null;
  source: "import" | "generated" | "derived" | "rendered";
  recipe: Recipe | null;
  created_at: string;
  waveform?: number[] | null;
  analysis?: Analysis | null;
}

export interface Paged<T> {
  items: T[];
  has_more: boolean;
  next_offset: number | null;
}

export interface Project {
  id: string;
  name: string;
  brief: string | null;
  cover_asset_id: string | null;
  /** "auto" (default) | "qwen21" | "flux" | "sdxl": what Generate and productions use unless a run overrides it */
  image_engine?: ImageEngine | null;
  created_at: string;
  updated_at: string;
  counts: Record<string, number>;
}

export type ImageEngine = "auto" | "qwen21" | "flux" | "sdxl";
export const IMAGE_ENGINES: ImageEngine[] = ["auto", "qwen21", "flux", "sdxl"];
/** Model names are not translated; "auto" is (see i18n engineAuto). */
export const ENGINE_NAMES: Record<Exclude<ImageEngine, "auto">, string> = { qwen21: "Qwen-Image 2.1", flux: "Flux", sdxl: "SDXL" };

export interface Voice {
  backend?: string;
  voice_id?: string;
  speed?: number;
}

export interface Character {
  id: string;
  project_id: string;
  name: string;
  role: string | null;
  bio: string | null;
  prompt: string | null;
  negative: string | null;
  palette: string[];
  reference_asset_ids: string[];
  canonical_asset_id: string | null;
  voice: Voice | null;
  notes: string | null;
}

export interface Group {
  id: string;
  project_id: string;
  name: string;
  concept: string | null;
  member_ids: string[];
  logo_asset_id: string | null;
  colours: string[];
}

export interface StylePreset {
  id: string;
  name: string;
  prompt_prefix: string;
  prompt_suffix: string;
  negative: string;
  defaults: Record<string, string | number>;
  is_builtin: number;
}

export type JobState = "queued" | "waiting_gpu" | "running" | "done" | "failed" | "cancelled";

export interface Job {
  id: string;
  project_id: string | null;
  type: string;
  lane: "gpu" | "cpu";
  state: JobState;
  progress: number;
  message: string | null;
  params: Record<string, unknown>;
  outputs: { asset_ids?: string[]; asset_id?: string; [k: string]: unknown } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  log_excerpt: string | null;
  cancel_requested: boolean;
}

export interface Clip {
  asset_id: string;
  kind: "image" | "video";
  start_s: number;
  duration_s: number;
  trim_start_s?: number;
  ken_burns?: { zoom_start: number; zoom_end: number; pan: string };
  transition_in?: { type: string; duration_s: number };
}

export interface LyricClip {
  text: string;
  start_s: number;
  end_s: number;
  karaoke: boolean;
}

export interface Timeline {
  id: string;
  project_id: string;
  name: string;
  aspect: string;
  fps: number;
  width: number;
  height: number;
  audio_asset_id: string | null;
  tracks: ({ type: "visual"; clips: Clip[] } | { type: "lyrics"; clips: LyricClip[] })[];
  created_at: string;
  updated_at: string;
}

export interface Board {
  id: string;
  project_id: string;
  name: string;
  kind: string;
  items: { asset_id: string; note: string }[];
}

export interface TemplateField {
  name: string;
  type: "text" | "image" | "colour";
  required: boolean;
  description: string;
}

export interface DesignTemplate {
  template: string;
  width: number;
  height: number;
  variants: string[];
  fields: TemplateField[];
}

export interface Composed {
  positive_prompt: string;
  negative_prompt: string;
  matched_characters: string[];
  unknown_mentions: string[];
  reference_asset_id: string | null;
  style: string | null;
  style_defaults: Record<string, string | number>;
}

export interface Resolution {
  capability: string;
  provider: string | null;
  url: string | null;
  model: string | null;
  state: string;
  reason: string;
  details: Record<string, unknown>;
}

export interface BackendStatus {
  demo: boolean;
  hoard_link: Record<string, Resolution>;
  comfy: {
    reachable: boolean;
    url?: string | null;
    reason?: string | null;
    checkpoints?: string[];
    vram_free_mb?: number | null;
    devices?: { name: string; vram_total_mb: number; vram_free_mb: number }[];
    version?: string;
  };
  ffmpeg: { found: boolean; path: string | null; version: string | null };
  piper: { installed: boolean };
  fonts_bundled: string[];
  vram_estimates_mb: Record<string, number>;
  music: { name: string; available: boolean; reason: string }[];
  overrides: {
    faustus_url?: string | null; comfy_url?: string | null;
    /** every folder imports may read from: the built-in ones first, then the configured ones (resolved, deduplicated) */
    import_roots: string[];
    /** only the folders configured in Settings, as saved (older servers omit it) */
    import_roots_user?: string[];
  };
  token_set: boolean;
}

export interface AgentCall {
  id: string;
  tool: string;
  args_summary: string;
  duration_ms: number;
  ok: boolean;
  error: string | null;
  created_at: string;
}

export interface WorkflowSpec {
  template: string;
  name?: string;
  kind: string;
  vram_class: string;
  map: Record<string, string>;
  builtin: boolean;
  requires_reference?: boolean;
  auto_detected?: boolean;
  output_node?: string;
}

export interface CuratedVoice {
  id: string;
  lang: string;
  label: string;
  size_mb: number;
  downloaded: boolean;
}

// ------------------------------------------------------------ voice studio

export interface EngineCapabilities {
  languages: string[];
  cloning: boolean;
  streaming: boolean;
  needs_gpu: boolean;
  multi_speaker: boolean;
}

export interface EngineStatus {
  id: string;
  label: string;
  kind: "tts" | "stt";
  installed: boolean;
  capabilities: EngineCapabilities;
  install_hint: string | null;
  reason: string;
}

export interface VoiceSpec {
  engine_id?: string | null;
  voice_id?: string | null;
  voice_ref?: string | null;
  preset?: string | null;
  speed?: number | null;
  pitch?: number | null;
  style?: string | null;
  language?: string | null;
}

export interface VoiceQuality {
  duration_s: number;
  snr_db: number;
  clipping_pct: number;
  warnings: string[];
  ok: boolean;
}

export interface StudioVoice {
  id: string;
  name: string;
  engine_id: string;
  language: string | null;
  cloned: boolean;
  has_sample: boolean;
  quality: VoiceQuality;
  presets: string[];
  tags: string[];
  project_id: string | null;
  created_at: string;
}

export interface TranscriptSegment {
  start_s: number;
  end_s: number;
  text: string;
  words: { start_s: number; end_s: number; word: string }[];
}

export interface Transcript {
  engine_id: string;
  language: string | null;
  text: string;
  segments: TranscriptSegment[];
  srt: string;
  vtt: string;
  txt: string;
}

export interface DubSegment {
  index: number;
  start_s: number;
  end_s: number;
  source_text: string;
  translated_text: string;
  engine_id?: string;
  fit?: { source_duration_s: number; target_duration_s: number; applied_factor: number; clamped: boolean };
}

export interface AudiobookOutputs {
  title: string;
  chapters: { index: number; title: string; start_s: number; end_s: number; duration_s: number }[];
  sentence_count: number;
  format: string;
  duration_s: number;
  final_file?: string;
  srt_file?: string;
  lrc_file?: string;
  asset_ids?: string[];
}

export interface DubOutputs {
  title: string;
  final_video?: string;
  subtitles?: string;
  background_separated?: boolean;
  work_dir?: string;
  segments?: DubSegment[];
  asset_ids?: string[];
}

// ---------------------------------------------------------------- productions

export type ProductionStatus = "queued" | "running" | "awaiting_review" | "done" | "failed" | "cancelled" | "partial";

export interface ProductionSummary {
  slug: string;
  name: string;
  status: ProductionStatus;
  stage?: string | null;
  project_id?: string | null;
  updated_at?: string;
  recipe?: string | null;
  message?: string | null;
  legacy?: boolean;
  animatic?: boolean;
}

export interface ProductionShot {
  key: string;
  lead: boolean;
  prompt: string;
  seed: number;
  variants: number;
  best: number;
  clips: number[];
  motion: "still" | "move";
  motion_prompt?: string;
}

/** `view.animatic` of a production that has one (compact_view); `false` otherwise. */
export interface ProductionAnimaticView {
  renders: Record<string, string>;
  gpu_minutes_estimate?: number | null;
  clips_planned?: number | null;
}

export interface ProductionView extends Omit<ProductionSummary, "animatic"> {
  stages?: Record<string, "done" | "partial" | "pending">;
  lead?: string;
  shots?: number;
  character_id?: string;
  song_asset_id?: string;
  renders?: Record<string, Record<string, string>>;
  animatic?: ProductionAnimaticView | boolean;
  qa?: { stage: string; passed: number; failed: number; skipped: number; at: string };
  qa_retries?: number;
  next?: string;
  note?: string;
}

/** `animatic/plan.json` (GET /api/productions/{slug}/animatic). */
export interface AnimaticPlan {
  production: string;
  made_at: string;
  duration_s: number;
  fps: number;
  cuts_total: number;
  shots: { key: string; lead: boolean; prompt: string | null; motion?: string | null; still: string | null;
           screen_time_s: number; cuts: number; will_be_clip: boolean; clip_keys: string[]; clips_to_render: string[] }[];
  unused_shots: string[];
  clips_planned: number;
  clips_to_render: string[];
  gpu_minutes: number;
  cpu_minutes_renders?: number;
  renders?: Record<string, string>;
  note?: string;
}

export interface ProductionState {
  slug: string;
  name: string;
  status: ProductionStatus;
  stage: string | null;
  project_id: string | null;
  message: string | null;
  job_id: string | null;
  recipe: { name: string; reuse: string[]; cast: { lead: string } } | null;
  spec: { title?: string; lead?: { name: string; look: string; palette?: string[] }; shots?: ProductionShot[];
          timeline?: { aspects?: string[] } } & Record<string, unknown>;
  settings: { animatic: boolean; animatic_autocontinue: boolean; qa: { enabled: boolean; max_retries: number } };
  done: Record<string, any>;
  lineage: { at: string; stage: string; event: string; [k: string]: unknown }[];
  qa?: { last?: QaScorecard };
  view: ProductionView;
  done_keys?: string[];
}

export interface QaItem {
  stage: string;
  key: string;
  asset_id: string | null;
  verdict: "pass" | "fail" | "skip";
  score?: number | null;
  reasons: string[];
  checks: Record<string, unknown>;
  model?: { bible?: number; prompt?: number; reference?: number; reason?: string; skipped?: string } | null;
}

export interface QaScorecard {
  production: string;
  stage: string;
  at: string;
  passed: number;
  failed: number;
  skipped: number;
  vision: string;
  items: QaItem[];
}

export interface RecipeSummary {
  name: string;
  title: string;
  created_at: string;
  from_production: string;
  original_lead: string;
  shots: number;
  lead_shots: number;
  clips: number;
  reusable: { song: boolean; frames: number; clips: number };
  warnings: number;
}

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  const type = res.headers.get("content-type") || "";
  if (!res.ok) {
    let code = `http_${res.status}`;
    let message = res.statusText;
    if (type.includes("json")) {
      const data = await res.json();
      code = data.error || code;
      message = data.message || message;
    }
    throw new ApiError(code, message, res.status);
  }
  if (type.includes("json")) return (await res.json()) as T;
  return (await res.blob()) as unknown as T;
}

const q = (params: Record<string, string | number | boolean | undefined | null>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") s.set(k, String(v));
  const text = s.toString();
  return text ? `?${text}` : "";
};

export const fileUrl = (id: string) => `/api/assets/${id}/file`;
export const thumbUrl = (a: Pick<Asset, "id" | "thumb_path" | "kind">) =>
  a.thumb_path ? `/api/assets/${a.id}/thumb` : a.kind === "image" ? fileUrl(a.id) : "";

export const api = {
  health: () => request<{ service: string; version: string; demo: boolean; active_jobs: number }>("GET", "/api/health"),
  projects: () => request<Paged<Project>>("GET", "/api/projects"),
  createProject: (name: string, brief?: string) => request<Project>("POST", "/api/projects", { name, brief }),
  project: (id: string) => request<Project>("GET", `/api/projects/${id}`),
  updateProject: (id: string, patch: Partial<Pick<Project, "name" | "brief" | "cover_asset_id" | "image_engine">>) =>
    request<Project>("PATCH", `/api/projects/${id}`, patch),

  characters: (pid: string) => request<{ items: Character[] }>("GET", `/api/projects/${pid}/characters`),
  createCharacter: (pid: string, name: string, fields: Partial<Character>) =>
    request<Character>("POST", `/api/projects/${pid}/characters`, { name, fields }),
  updateCharacter: (id: string, fields: Partial<Character>) => request<Character>("PATCH", `/api/characters/${id}`, { fields }),
  groups: (pid: string) => request<{ items: Group[] }>("GET", `/api/projects/${pid}/groups`),
  createGroup: (pid: string, name: string, fields: Partial<Group>) =>
    request<Group>("POST", `/api/projects/${pid}/groups`, { name, fields }),
  updateGroup: (id: string, fields: Partial<Group>) => request<Group>("PATCH", `/api/groups/${id}`, { fields }),

  styles: (pid?: string) => request<{ items: StylePreset[] }>("GET", `/api/style-presets${q({ project: pid })}`),
  compose: (pid: string, prompt: string, negative: string, style: string | null) =>
    request<Composed>("POST", `/api/projects/${pid}/compose-prompt`, { prompt, negative: negative || null, style }),
  generate: (pid: string, body: Record<string, unknown>) =>
    request<{ job: Job; final_prompt: string; seed: number; unknown_mentions: string[] }>("POST", `/api/projects/${pid}/generate`, body),
  edit: (assetId: string, body: Record<string, unknown>) =>
    request<{ job: Job }>("POST", `/api/assets/${assetId}/edit`, { asset_id: assetId, ...body }),
  animate: (assetId: string, body: Record<string, unknown>) =>
    request<{ job: Job }>("POST", `/api/assets/${assetId}/animate`, { asset_id: assetId, ...body }),
  workflows: () => request<{ builtin: WorkflowSpec[]; custom: WorkflowSpec[] }>("GET", "/api/workflows"),
  importWorkflow: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<WorkflowSpec>("POST", `/api/workflows/import-file`, fd);
  },
  updateWorkflow: (id: string, patch: Partial<WorkflowSpec>) => request<WorkflowSpec>("PATCH", `/api/workflows/${id}`, patch),

  voice: (pid: string, text: string, character_id?: string, voice?: string) =>
    request<Asset>("POST", `/api/projects/${pid}/voice`, { text, character_id, voice }),
  voices: () => request<{ items: CuratedVoice[]; piper_installed: boolean }>("GET", "/api/voices"),
  downloadVoice: (id: string) => request<Job>("POST", `/api/voices/${id}/download`),

  upload: (pid: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<Asset>("POST", `/api/projects/${pid}/import-upload`, fd);
  },
  importPath: (pid: string, path: string) => request<Asset>("POST", `/api/projects/${pid}/import-path`, { path }),

  analyze: (assetId: string, force = false) => request<Analysis>("POST", `/api/assets/${assetId}/analyze${q({ force })}`),
  lyrics: (assetId: string) => request<{ text: string; lines: { time_s: number; text: string }[]; all_lines?: { time_s: number; text: string }[] }>("GET", `/api/assets/${assetId}/lyrics`),
  timeLyrics: (pid: string, songAssetId: string, lyrics: string, name?: string) =>
    request<{ id: string; lines: number; sections: { label: string; energy: string; start_s: number; end_s: number }[]; note: string }>(
      "POST", `/api/projects/${pid}/lyrics/time`, { song_asset_id: songAssetId, lyrics, name }),
  saveLyrics: (assetId: string, text: string) => request<{ lines: { time_s: number; text: string }[] }>("PUT", `/api/assets/${assetId}/lyrics`, { text }),
  createLyrics: (pid: string, text: string, name: string) => request<Asset>("POST", `/api/projects/${pid}/lyrics`, { text, name }),

  templates: () => request<{ items: DesignTemplate[] }>("GET", "/api/design/templates"),
  preview: (template: string, fields: Record<string, string>, variant: string | null) =>
    request<Blob>("POST", "/api/design/preview", { template, fields, variant }),
  design: (pid: string, template: string, fields: Record<string, string>, variant: string | null, print: boolean) =>
    request<Asset>("POST", `/api/projects/${pid}/design`, { template, fields, variant, options: { print } }),
  photocardSet: (pid: string, group_id: string) =>
    request<{ front_ids: string[]; back_ids: string[]; contact_sheet_id: string; skipped_members?: string[] }>(
      "POST", `/api/projects/${pid}/photocard-set`, { group_id }),

  timelines: (pid: string) => request<{ items: Timeline[] }>("GET", `/api/projects/${pid}/timelines`),
  timeline: (id: string) => request<Timeline>("GET", `/api/timelines/${id}`),
  autoCut: (pid: string, body: Record<string, unknown>) => request<Timeline>("POST", `/api/projects/${pid}/timelines/auto`, body),
  patchTimeline: (id: string, patch: Record<string, unknown>) => request<Timeline>("PATCH", `/api/timelines/${id}`, patch),
  render: (id: string, quality: "preview" | "final") =>
    request<{ job: Job }>("POST", `/api/timelines/${id}/render`, { timeline_id: id, quality }),

  jobs: (params: { state?: string; project?: string; limit?: number; offset?: number } = {}) => request<Paged<Job>>("GET", `/api/jobs${q(params)}`),
  job: (id: string) => request<Job>("GET", `/api/jobs/${id}`),
  cancelJob: (id: string) => request<Job>("POST", `/api/jobs/${id}/cancel`),

  assets: (pid: string, params: Record<string, string | number | boolean | undefined> = {}) =>
    request<Paged<Asset>>("GET", `/api/projects/${pid}/assets${q(params)}`),
  asset: (id: string) => request<Asset>("GET", `/api/assets/${id}`),
  updateAsset: (id: string, patch: Partial<Pick<Asset, "tags" | "rating" | "favourite" | "notes" | "name">>) =>
    request<Asset>("PATCH", `/api/assets/${id}`, patch),
  lineage: (id: string) => request<{ recipe: Recipe | null; inputs?: { asset_id: string; operation?: string; seed?: number }[] }>(
    "GET", `/api/assets/${id}/lineage`),

  boards: (pid: string) => request<{ items: Board[] }>("GET", `/api/projects/${pid}/boards`),
  createBoard: (pid: string, name: string, kind: string) => request<Board>("POST", `/api/projects/${pid}/boards`, { name, kind }),
  updateBoard: (id: string, patch: { name?: string; kind?: string }) => request<Board>("PATCH", `/api/boards/${id}`, patch),
  deleteBoard: (id: string) => request<{ ok: boolean }>("DELETE", `/api/boards/${id}`),
  setBoardItems: (id: string, items: Board["items"]) => request<Board>("PUT", `/api/boards/${id}/items`, { items }),

  backend: () => request<BackendStatus>("GET", "/api/backend"),
  setBackend: (patch: Record<string, unknown>) => request<BackendStatus>("POST", "/api/backend", patch),
  freeComfy: () => request<{ message: string }>("POST", "/api/backend/comfy/free"),
  agentCalls: (limit = 100) => request<{ items: AgentCall[] }>("GET", `/api/agent-calls${q({ limit })}`),

  // -------------------------------------------------------------- voice studio
  voiceEngines: () => request<{ tts: EngineStatus[]; stt: EngineStatus[] }>("GET", "/api/voice/engines"),
  installVoiceEngine: (engineId: string, kind: "tts" | "stt") =>
    request<Job>("POST", `/api/voice/engines/${engineId}/install`, { kind }),

  studioVoices: (project?: string, engineId?: string) =>
    request<{ items: StudioVoice[] }>("GET", `/api/voice/voices${q({ project, engine_id: engineId })}`),
  studioVoice: (id: string) => request<StudioVoice>("GET", `/api/voice/voices/${id}`),
  createStudioVoice: (name: string, engineId: string, sourcePath: string, opts: { language?: string; project?: string; tags?: string[] } = {}) =>
    request<StudioVoice>("POST", "/api/voice/voices", { name, engine_id: engineId, source_path: sourcePath, ...opts }),
  uploadStudioVoice: (file: File, name: string, engineId: string, opts: { language?: string; project?: string } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<StudioVoice>("POST", `/api/voice/voices/upload${q({ name, engine_id: engineId, ...opts })}`, fd);
  },
  updateStudioVoice: (id: string, patch: { name?: string; tags?: string[]; notes?: string }) =>
    request<StudioVoice>("PATCH", `/api/voice/voices/${id}`, patch),
  deleteStudioVoice: (id: string) => request<{ ok: boolean }>("DELETE", `/api/voice/voices/${id}`),
  addVoicePreset: (id: string, preset: { name: string; speed?: number; pitch?: number; style?: string }) =>
    request<StudioVoice>("POST", `/api/voice/voices/${id}/presets`, preset),
  voiceSampleUrl: (id: string) => `/api/voice/voices/${id}/sample`,
  previewStudioVoice: (id: string, text: string, spec: VoiceSpec = {}) =>
    request<Blob>("POST", `/api/voice/voices/${id}/preview`, { text, voice: spec }),

  speak: (text: string, voice: VoiceSpec, project?: string) =>
    request<Blob | Asset>("POST", "/api/voice/speak", { text, voice, project }),

  transcribe: (body: { path?: string; asset_id?: string; language?: string; engine_id?: string; word_timestamps?: boolean }) =>
    request<Transcript & { text: string; language: string | null }>("POST", "/api/voice/transcribe", body),
  transcribeUpload: (file: File, opts: { language?: string; engineId?: string } = {}) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<Transcript & { text: string; language: string | null }>(
      "POST", `/api/voice/transcribe/upload${q({ language: opts.language, engine_id: opts.engineId })}`, fd);
  },
  dictate: (file: File | Blob, language?: string) => {
    const fd = new FormData();
    fd.append("file", file, "clip.wav");
    return request<{ text: string; language: string | null; engine_id: string }>(
      "POST", `/api/voice/dictate${q({ language })}`, fd);
  },

  audiobook: (body: { text?: string; source_path?: string; title?: string; voice: VoiceSpec; format?: "mp3" | "m4b"; project?: string; wait_s?: number }) =>
    request<{ job: Job }>("POST", "/api/voice/audiobook", body),
  audiobookStatus: (jobId: string) => request<Job>("GET", `/api/voice/audiobook/${jobId}`),
  audiobookDownloadUrl: (jobId: string, file: "final" | "srt" | "lrc" = "final") =>
    `/api/voice/audiobook/${jobId}/download${q({ file })}`,

  dub: (body: { source_path?: string; video_asset_id?: string; target_language: string; source_language?: string;
                glossary?: Record<string, string>; voice: VoiceSpec; stt_engine_id?: string; title?: string;
                project?: string; wait_s?: number }) =>
    request<{ job: Job }>("POST", "/api/voice/dub", body),
  dubStatus: (jobId: string) => request<Job>("GET", `/api/voice/dub/${jobId}`),
  dubDownloadUrl: (jobId: string, file: "video" | "subtitles" = "video") => `/api/voice/dub/${jobId}/download${q({ file })}`,
  // ------------------------------------------------------------- productions
  productions: () => request<{ items: ProductionSummary[] }>("GET", "/api/productions"),
  production: (slug: string) => request<ProductionState>("GET", `/api/productions/${slug}`),
  continueProduction: (slug: string) => request<{ production: ProductionView; job: Job }>("POST", `/api/productions/${slug}/continue`),
  changeShots: (slug: string, changes: Record<string, unknown>[], run = true) =>
    request<{ changed: string[]; production: ProductionView }>("PATCH", `/api/productions/${slug}/shots`, { changes, run }),
  exportRecipe: (slug: string, name?: string) => request<RecipeSummary>("POST", `/api/productions/${slug}/recipe`, { production: slug, name }),
  runQa: (slug: string, body: { stage?: string; dry_run?: boolean; keys?: string[] }) =>
    request<{ job: Job; scorecard?: QaScorecard }>("POST", `/api/productions/${slug}/qa`, { production: slug, ...body }),
  makeAnimatic: (slug: string, aspects?: string[]) =>
    request<{ job: Job }>("POST", `/api/productions/${slug}/animatic`, { production: slug, aspects }),
  animaticPlan: (slug: string) => request<AnimaticPlan>("GET", `/api/productions/${slug}/animatic`),
  productionReport: async (slug: string) => {
    const body = await request<Blob>("GET", `/api/productions/${slug}/report`);
    return typeof body === "string" ? body : await body.text();
  },
  recipes: () => request<{ items: RecipeSummary[] }>("GET", "/api/recipes"),
  runRecipe: (name: string, body: { cast: Record<string, unknown>; name?: string; options?: Record<string, unknown> }) =>
    request<{ production: ProductionView; job: Job; notes: string[] }>("POST", `/api/recipes/${name}/run`, body),
  resynthesizeDubSegment: (jobId: string, index: number, opts: { text?: string; voice?: VoiceSpec; remix?: boolean } = {}) =>
    request<{ segment: DubSegment }>("POST", `/api/voice/dub/${jobId}/segments/${index}/resynthesize`, opts),
};
