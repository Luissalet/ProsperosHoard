// Spaces: a node canvas where references, prompts and generators are wired
// together (spaces.py on the server). The graph is what the person draws; the
// state (statuses, outputs, runs) is what runs write and is polled while
// something runs. Saves are versioned: a save over a newer version (an agent
// edited the space meanwhile) reloads instead of overwriting it.
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Background, BackgroundVariant, Controls, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange, type NodeProps, type OnConnectEnd,
  applyEdgeChanges, applyNodeChanges, useUpdateNodeInternals, NodeResizer,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  Aperture, ArrowLeft, AudioLines, Check, Copy, Film, Image as ImageIcon, LayoutTemplate, ListChecks, Loader2, Maximize,
  Music, Play, Plus, RotateCcw, StickyNote, Trash2, Type, User, Workflow, X, FastForward, Bot, ScanLine, Layers, Square, Captions,
  Grid3x3, Frame, LogIn, Star, AppWindow, Clock, Wand2, Layers2, ListEnd, Download, Upload, Search, ChevronRight, Undo2, Redo2, Save,
} from "lucide-react";
import {
  ApiError, api, type Character, type Space, type SpaceApp, type SpaceEdge, type SpaceEstimate, type SpaceGraph, type SpaceNodeState,
  type SpaceNodeType, type SpaceSummary,
} from "../api";
import { useT, type MessageKey } from "../i18n";
import { AssetPicker, ConfirmButton, Empty, timeAgo, useApp, useAsync } from "../components/ui";
import { useCinemaGuide, useSlashMenu } from "../components/Slash";
import { Enhance } from "../components/Enhance";
import { EditHistory, SaveQueue, freeNodePosition } from "../spaceEditing";

// ------------------------------------------------------------ the node model

type Port = "text" | "image" | "video" | "audio" | "any";
const INPUTS: Partial<Record<SpaceNodeType, [string, Port, boolean][]>> = {
  // [handle, accepts, many wires]
  image: [["prompt", "text", true], ["refs", "image", true], ["pose", "image", false], ["layout", "image", false]],
  video: [["start", "image", true], ["end", "image", false], ["prompt", "text", true], ["motion", "video", false], ["audio", "audio", false]],
  music: [["prompt", "text", true]],
  list: [["items", "any", true]],
  assistant: [["prompt", "text", true]],
  edit: [["image", "image", true]],
  combine: [["clips", "video", true], ["audio", "audio", false]],
  variations: [["image", "image", true], ["prompt", "text", true]],
  composite: [["background", "any", true], ["layers", "any", true]],
  clip_edit: [["clip", "video", true], ["prompt", "text", true], ["refs", "image", true], ["first", "image", false]],
};
const GENERATORS: SpaceNodeType[] = ["image", "video", "music", "assistant", "edit", "combine", "variations", "composite", "clip_edit"];
const APP_INPUTS: SpaceNodeType[] = ["text", "asset", "cast"];
const PORT_COLOR: Record<Port, string> = { text: "#7fa6d9", image: "#b48cf0", video: "#5bbf86", audio: "#69c5d4", any: "#9a95a6" };
const TYPE_ICON: Record<SpaceNodeType, typeof Type> = {
  text: Type, asset: ImageIcon, cast: User, image: ImageIcon, video: Film, music: Music, list: ListChecks, note: StickyNote,
  assistant: Bot, edit: ScanLine, combine: Layers, variations: Grid3x3, group: Frame, composite: Layers2, clip_edit: Wand2,
};
const TYPE_LABEL: Record<SpaceNodeType, MessageKey> = {
  text: "spNodeText", asset: "spNodeAsset", cast: "spNodeCast", image: "spNodeImage", video: "spNodeVideo",
  music: "spNodeMusic", list: "spNodeList", note: "spNodeNote", assistant: "spNodeAssistant", edit: "spNodeEdit", combine: "spNodeCombine",
  variations: "spNodeVariations", group: "spNodeGroup", composite: "spNodeComposite", clip_edit: "spNodeClipEdit",
};
const ADDABLE: SpaceNodeType[] = ["text", "asset", "cast", "image", "video", "clip_edit", "music", "assistant", "variations", "edit", "composite",
  "combine", "list", "note", "group"];
const DEFAULT_DATA: Record<SpaceNodeType, Record<string, unknown>> = {
  text: { text: "" }, asset: { kind: "image", asset_ids: [] }, cast: {}, image: { prompt: "", aspect: "1:1", count: 2 },
  video: { prompt: "", quality: "final", seconds: 5 }, music: { tags: "", lyrics: "[Instrumental]", duration: 30, count: 1 },
  list: { unticked: [] }, note: { text: "" },
  assistant: { prompt: "", as_list: true, items: 5 }, edit: { operation: "upscale", scale: 2 }, combine: { audio_start_s: 0 },
  variations: { mode: "angles", count: 4 }, group: { title: "", color: "#b48cf0" }, composite: { layers: [] },
  clip_edit: { prompt: "", mode: "auto", quality: "draft" },
};
const WIDTH: Record<SpaceNodeType, number> = {
  text: 280, asset: 290, cast: 290, image: 340, video: 340, music: 310, list: 280, note: 260, assistant: 310, edit: 280, combine: 310,
  variations: 270, group: 620, composite: 300, clip_edit: 290,
};

type NodeData = { kind: SpaceNodeType; data: Record<string, any> };
type SpNode = Node<NodeData, "sp" | "grp">;

function outputPort(kind: SpaceNodeType, data: Record<string, any>, handle?: string | null): Port | null {
  if (kind === "text") return "text";
  if (kind === "cast") return handle === "text" ? "text" : "image";
  if (kind === "video") return handle === "last" ? "image" : "video";
  if (kind === "image" || kind === "edit" || kind === "variations") return "image";
  if (kind === "assistant") return "text";
  if (kind === "combine" || kind === "clip_edit") return "video";
  if (kind === "composite") return "any";
  if (kind === "music") return "audio";
  if (kind === "asset") return (data.kind as Port) || "image";
  if (kind === "list") return "any";
  return null;
}

function toRf(graph: SpaceGraph): { nodes: SpNode[]; edges: Edge[] } {
  const nodes: SpNode[] = graph.nodes.map((n) => (n.type === "group" ? {
    id: n.id, type: "grp", position: { x: n.x, y: n.y }, data: { kind: n.type, data: n.data || {} },
    style: { width: n.w || WIDTH.group, height: n.h || 380 }, zIndex: -1,
  } : {
    id: n.id, type: "sp", position: { x: n.x, y: n.y }, data: { kind: n.type, data: n.data || {} },
    style: { width: n.w || WIDTH[n.type] },
  })) as SpNode[];
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const edges: Edge[] = graph.edges.map((e) => edgeOf(e, byId.get(e.source)?.type, byId.get(e.source)?.data));
  return { nodes, edges };
}

function edgeOf(e: SpaceEdge, kind?: SpaceNodeType, data?: Record<string, any>): Edge {
  const port = (kind && outputPort(kind, data || {}, e.source_handle)) || "any";
  return {
    id: e.id, source: e.source, target: e.target, sourceHandle: e.source_handle || "out", targetHandle: e.target_handle,
    style: { stroke: PORT_COLOR[port], strokeWidth: 2 }, data: { port },
  };
}

function toGraph(nodes: SpNode[], edges: Edge[], viewport?: { x: number; y: number; zoom: number }): SpaceGraph {
  return {
    nodes: nodes.map((n) => ({
      id: n.id, type: n.data.kind, x: Math.round(n.position.x), y: Math.round(n.position.y), data: n.data.data,
      ...(n.data.kind === "group" ? { w: Math.round(Number(n.width ?? n.style?.width ?? WIDTH.group)), h: Math.round(Number(n.height ?? n.style?.height ?? 380)) }
        : typeof n.style?.width === "number" && n.style.width !== WIDTH[n.data.kind] ? { w: n.style.width } : {}),
    })),
    edges: edges.map((e) => ({
      id: e.id, source: e.source, source_handle: e.sourceHandle && e.sourceHandle !== "out" ? e.sourceHandle : null,
      target: e.target, target_handle: e.targetHandle || "",
    })),
    ...(viewport ? { viewport } : {}),
  };
}

function newId(kind: SpaceNodeType, taken: Set<string>): string {
  let i = 1;
  while (taken.has(`${kind}${i}`)) i++;
  return `${kind}${i}`;
}

/** Would wiring source -> target close a loop? */
function makesLoop(edges: Edge[], source: string, target: string): boolean {
  const seen = new Set<string>();
  const stack = [target];
  while (stack.length) {
    const n = stack.pop()!;
    if (n === source) return true;
    if (seen.has(n)) continue;
    seen.add(n);
    for (const e of edges) if (e.source === n) stack.push(e.target);
  }
  return false;
}

// ------------------------------------------------------------ editor context

interface Ctx {
  projectId: string;
  state: Record<string, SpaceNodeState>;
  characters: Character[];
  edges: Edge[];
  nodes: SpNode[];
  update: (id: string, patch: Record<string, unknown>) => void;
  run: (id: string, mode: "node" | "downstream" | "upto") => void;
  remove: (id: string) => void;
  duplicate: (id: string) => void;
  setExcluded: (id: string, excluded: string[]) => void;
  pickRun: (id: string, outputs: string[]) => void;
  pickAssets: (id: string, kind: string) => void;
  appendAssets: (id: string, ids: string[]) => void;
  toast: (message: string) => void;
  open: (assetId: string, list: string[]) => void;
  touched: () => void;
  exportTechnique: (group?: string) => void;
}
const SpaceCtx = createContext<Ctx>(null as unknown as Ctx);

const NODE_PURPOSE: Record<SpaceNodeType, [string, string]> = {
  text: ["Un prompt reutilizable", "A reusable prompt"], asset: ["Fotos, vídeos o audio", "Photos, videos or audio"],
  cast: ["Personajes, lugares y objetos", "Characters, places and props"], image: ["Combinar referencias y crear imágenes", "Combine references and create images"],
  video: ["Animar, copiar movimiento o sincronizar labios", "Animate, transfer motion or sync lips"], music: ["Componer música y canciones", "Compose music and songs"],
  list: ["Elegir entradas y hacer lotes", "Select inputs and make batches"], note: ["Anotar una idea en el lienzo", "Keep an idea on the canvas"],
  assistant: ["Crear prompts o tomas con el asistente", "Write prompts or shots with the assistant"], edit: ["Ampliar, quitar fondo, pose o profundidad", "Upscale, remove background, pose or depth"],
  combine: ["Montar clips en el orden conectado", "Join clips in connection order"], variations: ["Ángulos, expresiones y storyboards", "Angles, expressions and storyboards"],
  group: ["Agrupar y guardar una técnica", "Group nodes and save a technique"], composite: ["Superponer imágenes y vídeos", "Layer images and videos"],
  clip_edit: ["Reimaginar un vídeo existente", "Reimagine an existing video"],
};

function NodePalette({ choices, onPick, onClose, connected }: {
  choices: { kind: SpaceNodeType; handle?: string }[]; onPick: (choice: { kind: SpaceNodeType; handle?: string }) => void; onClose: () => void; connected: boolean;
}) {
  const { t, lang } = useT();
  const [query, setQuery] = useState("");
  const found = choices.filter((c) => `${t(TYPE_LABEL[c.kind])} ${NODE_PURPOSE[c.kind][lang === "es" ? 0 : 1]} ${c.kind}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="sp-palette" role="dialog" aria-label={lang === "es" ? "Añadir nodo" : "Add node"} onKeyDown={(e) => {
    if (e.key === "Escape") { e.stopPropagation(); onClose(); }
    const buttons = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>(".sp-palette-option"));
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); buttons[(current + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length]?.focus(); }
    if (e.key === "Enter" && e.target instanceof HTMLInputElement && found.length) { e.preventDefault(); onPick(found[0]); }
  }}>
    <div className="sp-palette-head"><strong>{connected ? t("spMenuWire") : t("spMenuAdd")}</strong><button className="btn sm icon ghost" onClick={onClose} aria-label={t("close")}><X size={16} /></button></div>
    <label className="sp-palette-search"><Search size={16} /><input autoFocus value={query} onChange={(e) => setQuery(e.target.value)}
      aria-label={lang === "es" ? "Buscar nodo" : "Search nodes"} placeholder={lang === "es" ? "Buscar una herramienta…" : "Search a tool…"} /></label>
    <div className="sp-palette-results">{found.map((choice) => {
      const Icon = TYPE_ICON[choice.kind];
      return <button className="sp-palette-option" key={`${choice.kind}-${choice.handle || ""}`} onClick={() => onPick(choice)}>
        <Icon size={19} /><span><strong>{t(TYPE_LABEL[choice.kind])}</strong><small>{NODE_PURPOSE[choice.kind][lang === "es" ? 0 : 1]}</small></span><ChevronRight size={14} />
      </button>;
    })}{!found.length && <p className="small muted">{t("spMenuNone")}</p>}</div>
    <p className="sp-palette-help">{lang === "es" ? "↑ ↓ para elegir · Intro para añadir · Esc para cerrar" : "↑ ↓ to choose · Enter to add · Esc to close"}</p>
  </div>;
}

/** Client-side mirror of spaces.node_outputs, for previews (lists, wires). */
function resolveOutputs(ctx: Pick<Ctx, "nodes" | "edges" | "state" | "characters">, id: string, handle?: string | null, depth = 0): [Port, string][] {
  const node = ctx.nodes.find((n) => n.id === id);
  if (!node || depth > 20) return [];
  const { kind, data } = node.data;
  if (kind === "text") return data.text?.trim() ? [["text", data.text.trim()]] : [];
  if (kind === "cast") {
    const c = ctx.characters.find((x) => x.id === data.character_id);
    if (!c) return [];
    if (handle === "text") return [["text", `@${c.name}`]];
    return c.canonical_asset_id ? [["image", c.canonical_asset_id]] : [];
  }
  if (kind === "asset") return (data.asset_ids || []).map((a: string) => [data.kind || "image", a] as [Port, string]);
  if (kind === "assistant") {
    const st = ctx.state[id] || {};
    const ex = new Set(st.excluded || []);
    return (st.texts || []).filter((x) => !ex.has(x)).map((x) => ["text", x]);
  }
  if (GENERATORS.includes(kind)) {
    const st = ctx.state[id] || {};
    const ex = new Set(st.excluded || []);
    const kept = (st.outputs || []).filter((a) => !ex.has(a));
    if (kind === "video" && handle === "last") {
      const lf = st.last_frames || {};
      return kept.filter((a) => lf[a]).map((a) => ["image", lf[a]] as [Port, string]);
    }
    const port = outputPort(kind, data) || "image";
    return kept.map((a) => [port, a]);
  }
  if (kind === "list") {
    const out: [Port, string][] = [];
    const seen = new Set<string>(data.unticked || []);
    for (const e of ctx.edges) {
      if (e.target !== id) continue;
      for (const item of resolveOutputs(ctx, e.source, e.sourceHandle === "out" ? null : e.sourceHandle, depth + 1)) {
        if (seen.has(item[1])) continue;
        seen.add(item[1]);
        out.push(item);
      }
    }
    return out;
  }
  return [];
}

// ------------------------------------------------------------ small pieces

function Thumb({ id, port, dim, onClick }: { id: string; port: Port; dim?: boolean; onClick?: () => void }) {
  const [fallback, setFallback] = useState(false);
  return (
    <button className={`sp-thumb nodrag${dim ? " dim" : ""}`} onClick={onClick} title={id} aria-label={`Preview ${port}`} draggable
      onDragStart={(e) => { e.dataTransfer.setData("text/prospero-asset", id); e.dataTransfer.effectAllowed = "copy"; }}>
      {port === "audio" ? <div className="sp-thumb-icon"><AudioLines size={18} /></div>
        : fallback && port === "video" ? <video src={`/api/assets/${id}/file`} muted preload="metadata" />
          : <img src={fallback ? `/api/assets/${id}/file` : `/api/assets/${id}/thumb`} alt="" loading="lazy"
              onError={(e) => { if (!fallback) setFallback(true); else e.currentTarget.style.visibility = "hidden"; }} />}
    </button>
  );
}

function StatusChip({ st }: { st?: SpaceNodeState }) {
  const { t } = useT();
  if (!st?.status) return null;
  const busy = st.status === "queued" || st.status === "running";
  const progress = st.job_states?.length ? st.job_states.reduce((a, j) => a + (j.progress || 0), 0) / st.job_states.length : 0;
  const tone = st.status === "done" ? "ok" : st.status === "failed" ? "bad" : st.status === "partial" ? "warn" : "accent";
  const label: Record<string, MessageKey> = { queued: "spQueued", running: "spRunning", done: "spDone", failed: "spFailed", partial: "spPartial" };
  return (
    <span className={`pill ${tone} sp-status`}>
      {busy && <Loader2 size={11} className="spin" />}
      {t(label[st.status])}{st.status === "running" && progress > 0 ? ` ${Math.round(progress * 100)}%` : ""}
    </span>
  );
}

function PromptBox({ value, onChange, kind, placeholder, rows = 3 }: {
  value: string; onChange: (s: string) => void; kind: "image" | "video" | "music"; placeholder: string; rows?: number;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const slash = useSlashMenu(value, onChange, ref, { clips: kind === "video" });
  return (
    <div className="sp-prompt slash-wrap">
      <textarea ref={ref} className="nodrag nowheel" rows={rows} value={value} placeholder={placeholder}
        onChange={(e) => { onChange(e.target.value); if (kind !== "music") slash.update(e.target.value, e.target.selectionStart); }}
        onKeyDown={(e) => { slash.onKeyDown(e); }} onBlur={() => setTimeout(slash.close, 150)} />
      {slash.menu}
      <div className="sp-prompt-foot"><Enhance text={value} kind={kind} onDone={onChange} /></div>
    </div>
  );
}

/** The sung lines of the song wired into a clip's audio (speech to text),
 * to start the lip sync on one line and last as long as it. */
function LinePicker({ nodeId, onPick }: { nodeId: string; onPick: (start: number, seconds: number) => void }) {
  const { t } = useT();
  const app = useApp();
  const ctx = useContext(SpaceCtx);
  const [lines, setLines] = useState<{ start_s: number; end_s: number; text: string }[] | null>(null);
  const [busy, setBusy] = useState(false);
  const edge = ctx.edges.find((e) => e.target === nodeId && e.targetHandle === "audio");
  const audio = edge ? resolveOutputs(ctx, edge.source, edge.sourceHandle === "out" ? null : edge.sourceHandle).find(([p]) => p === "audio")?.[1] : undefined;
  const load = async () => {
    if (!audio) { app.toast(t("spLinesNoAudio"), "info"); return; }
    setBusy(true);
    try { setLines((await api.transcribe({ asset_id: audio })).segments.filter((x) => x.text.trim())); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  return (
    <span className="sp-lines">
      <button className="btn xs ghost nodrag" onClick={() => (lines ? setLines(null) : load())} disabled={busy} title={t("spLinesHint")}>
        {busy ? <Loader2 size={12} className="spin" /> : <Captions size={12} />} {t("spLines")}
      </button>
      {lines && (
        <div className="sp-lines-menu nowheel nodrag">
          {lines.length === 0 && <div className="small muted" style={{ padding: 6 }}>{t("spLinesNone")}</div>}
          {lines.map((l, i) => (
            <button key={i} onClick={() => { onPick(Math.max(0, +(l.start_s - 0.2).toFixed(2)), Math.min(19, Math.max(1, +(l.end_s - l.start_s + 0.5).toFixed(1)))); setLines(null); }}>
              <span className="mono small muted">{l.start_s.toFixed(1)}s</span> <span>{l.text}</span>
            </button>
          ))}
        </div>
      )}
    </span>
  );
}

const CAMERA_KEYS = ["shot", "angle", "move", "lens", "light", "composition"] as const;

/** Film language for a picture or a clip: the chosen terms join its prompt
 * on the server (cinema.camera_prompt). */
function CameraPicker({ value, onChange, clips }: { value?: Record<string, string>; onChange: (v: Record<string, string> | null) => void; clips: boolean }) {
  const { t, lang } = useT();
  const guide = useCinemaGuide();
  const [open, setOpen] = useState(false);
  const L = lang === "es" ? "es" : "en";
  const cam = value || {};
  if (!guide) return null;
  const chosen = CAMERA_KEYS.map((k) => guide.items.find((e) => e.id === cam[k] && e.category === k)).filter(Boolean);
  return (
    <div className="sp-camera nodrag">
      <button className="sp-camera-head" onClick={() => setOpen((o) => !o)}>
        <Aperture size={12} /> <span className="grow ellipsis">{chosen.length ? chosen.map((e) => e!.name[L]).join(" · ") : t("spCameraNone")}</span>
        <span className="muted">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="sp-camera-grid">
          {CAMERA_KEYS.filter((k) => clips || k !== "move").map((k) => {
            const cat = guide.categories.find((c) => c.id === k)!;
            return (
              <label key={k} className="sp-camera-row">
                <span>{cat.name[L]}</span>
                <select value={cam[k] || ""} onChange={(e) => {
                  const next: Record<string, string> = { ...cam, [k]: e.target.value };
                  if (!e.target.value) delete next[k];
                  onChange(Object.keys(next).length ? next : null);
                }}>
                  <option value="">—</option>
                  {guide.items.filter((e) => e.category === k && (clips || !e.video_only)).map((e) => <option key={e.id} value={e.id}>{e.name[L]}</option>)}
                </select>
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------ the node

const SpaceNodeView = memo(function SpaceNodeView({ id, data: nd, selected }: NodeProps<SpNode>) {
  const { t, lang } = useT();
  const ctx = useContext(SpaceCtx);
  const { kind, data } = nd;
  const st = ctx.state[id];
  const Icon = TYPE_ICON[kind];
  const set = (patch: Record<string, unknown>) => ctx.update(id, patch);
  const wired = new Set(ctx.edges.filter((e) => e.target === id).map((e) => e.targetHandle));
  const runnable = GENERATORS.includes(kind);
  const busy = st?.status === "queued" || st?.status === "running";
  const out = outputPort(kind, data);
  const [uploading, setUploading] = useState(false);
  const words = (es: string, en: string) => lang === "es" ? es : en;
  const roleSelect = <label className="sp-role nodrag">{words("Usar como", "Use as")}
    <select value={data.ref_role || ""} onChange={(e) => set({ ref_role: e.target.value })}>
      <option value="">{words("Referencia libre", "Free reference")}</option>
      <option value="identity">{words("Identidad / persona", "Identity / person")}</option>
      <option value="outfit">{words("Vestuario", "Outfit")}</option>
      <option value="setting">{words("Escenario y fondo", "Setting and background")}</option>
      <option value="style">{words("Estilo visual", "Visual style")}</option>
      <option value="pose">{words("Pose", "Pose")}</option>
    </select></label>;

  // what the text node feeds decides how "Improve" rewrites it
  const textKind = (): "image" | "video" | "music" => {
    const targets = ctx.edges.filter((e) => e.source === id).map((e) => ctx.nodes.find((n) => n.id === e.target)?.data.kind);
    return targets.includes("video") ? "video" : targets.includes("music") ? "music" : "image";
  };

  let body: ReactNode = null;
  if (kind === "text") {
    body = <PromptBox value={data.text || ""} onChange={(v) => set({ text: v })} kind={textKind()} placeholder={t("spTextPh")} rows={4} />;
  } else if (kind === "note") {
    body = <textarea className="nodrag nowheel sp-note-text" rows={6} value={data.text || ""} placeholder={t("spNotePh")}
      onChange={(e) => set({ text: e.target.value })} />;
  } else if (kind === "asset") {
    const ids: string[] = data.asset_ids || [];
    body = (
      <>
        <div className="seg nodrag sp-seg">
          {(["image", "video", "audio"] as const).map((k) => (
            <button key={k} className={(data.kind || "image") === k ? "on" : ""}
              onClick={() => { if (k !== (data.kind || "image")) set({ kind: k, asset_ids: [] }); }}>{t(k === "image" ? "spKindImage" : k === "video" ? "spKindVideo" : "spKindAudio")}</button>
          ))}
        </div>
        {(data.kind || "image") === "image" && roleSelect}
        <div className="sp-thumbs">
          {ids.map((a) => (
            <div key={a} className="sp-thumb-wrap">
              <Thumb id={a} port={(data.kind || "image") as Port} onClick={() => ctx.open(a, ids)} />
              <button className="sp-thumb-x nodrag" title={t("remove")} onClick={() => set({ asset_ids: ids.filter((x) => x !== a) })}><X size={11} /></button>
            </div>
          ))}
          <button className="sp-thumb sp-add nodrag" onClick={() => ctx.pickAssets(id, data.kind || "image")} title={t("spAddFromLibrary")}><Plus size={18} /></button>
        </div>
        <label className={`btn sm sp-upload nodrag${uploading ? " disabled" : ""}`}>
          {uploading ? <Loader2 size={14} className="spin" /> : <Upload size={14} />}{words("Subir archivo", "Upload file")}
          <input type="file" hidden multiple disabled={uploading} accept={`${data.kind || "image"}/*`} onChange={async (e) => {
            const files = Array.from(e.target.files || []); e.target.value = "";
            if (!files.length) return;
            setUploading(true);
            const added: string[] = [];
            try {
              for (const file of files) {
                const asset = await api.upload(ctx.projectId, file);
                if (asset.kind !== (data.kind || "image")) throw new Error(words("El archivo no es del tipo seleccionado", "The file does not match the selected media type"));
                added.push(asset.id);
              }
            } catch (error) { ctx.toast((error as Error).message); }
            finally {
              if (added.length) ctx.appendAssets(id, added);
              setUploading(false);
            }
          }} />
        </label>
      </>
    );
  } else if (kind === "cast") {
    const c = ctx.characters.find((x) => x.id === data.character_id);
    const groups: [string, MessageKey][] = [["character", "spCastCharacters"], ["location", "spCastPlaces"], ["prop", "spCastProps"]];
    body = (
      <>
        <select className="nodrag" value={data.character_id || ""} onChange={(e) => set({ character_id: e.target.value || null })}>
          <option value="">{t("spPickCast")}</option>
          {groups.map(([el, key]) => {
            const items = ctx.characters.filter((x) => (x.element || "character") === el);
            return items.length ? <optgroup key={el} label={t(key)}>{items.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</optgroup> : null;
          })}
        </select>
        {roleSelect}
        {c?.canonical_asset_id ? <div className="sp-cast-img">
          <Thumb id={c.canonical_asset_id} port="image" onClick={() => ctx.open(c.canonical_asset_id!, [c.canonical_asset_id!])} /></div>
          : c ? <p className="small muted">{t("spCastNoRef")}</p> : null}
      </>
    );
  } else if (kind === "image") {
    body = (
      <>
        <PromptBox value={data.prompt || ""} onChange={(v) => set({ prompt: v })} kind="image" placeholder={t("spImagePh")} />
        <div className="sp-opts nodrag">
          <select value={data.aspect || "1:1"} onChange={(e) => set({ aspect: e.target.value })} title={t("spAspect")}>
            {["1:1", "16:9", "9:16", "2:3", "3:2", "4:5"].map((a) => <option key={a}>{a}</option>)}
          </select>
          <select value={data.count || 1} onChange={(e) => set({ count: Number(e.target.value) })} title={t("spCount")}>
            {[1, 2, 3, 4, 6, 8].map((n) => <option key={n} value={n}>×{n}</option>)}
          </select>
          <select value={data.preset || ""} onChange={(e) => set({ preset: e.target.value || null })} title={t("spPreset")}>
            <option value="">{t("spPresetNone")}</option>
            <option value="sheet">{t("spPresetSheet")}</option>
          </select>
        </div>
        {data.preset !== "sheet" && <CameraPicker value={data.camera} onChange={(c) => set({ camera: c })} clips={false} />}
      </>
    );
  } else if (kind === "video") {
    body = (
      <>
        <PromptBox value={data.prompt || ""} onChange={(v) => set({ prompt: v })} kind="video" placeholder={t("spVideoPh")} />
        <div className="sp-opts nodrag">
          {!wired.has("audio") && !wired.has("motion") && !wired.has("end") && (
            <div className="seg">
              <button className={data.quality === "draft" ? "on" : ""} onClick={() => set({ quality: "draft" })} title={t("spDraftHint")}>{t("spDraft")}</button>
              <button className={data.quality !== "draft" ? "on" : ""} onClick={() => set({ quality: "final" })} title={t("spFinalHint")}>{t("spFinal")}</button>
            </div>
          )}
          {!wired.has("audio") && <label className="sp-num">{t("spLength")}<input type="number" min={1} max={wired.has("motion") ? 5 : 20} step={0.1}
            value={data.seconds ?? (wired.has("motion") ? 3 : 5)} onChange={(e) => set({ seconds: Number(e.target.value) })} />s</label>}
          {wired.has("audio") && <>
            <LinePicker nodeId={id} onPick={(start, secs) => set({ audio_start_s: start, seconds: secs })} />
            <label className="sp-num" title={t("spAudioStartHint")}>{t("spFrom")}<input type="number" min={0} step={0.1} value={data.audio_start_s ?? 0}
              onChange={(e) => set({ audio_start_s: Number(e.target.value) })} />s</label>
            <label className="sp-num" title={t("spSecondsHint")}>{t("spLength")}<input type="number" min={1}
              max={data.sing_engine === "s2v" ? 19 : 90} step={0.1} value={data.seconds ?? 4.8}
              onChange={(e) => set({ seconds: Number(e.target.value) })} />s</label>
            <select value={data.sing_engine || "auto"} onChange={(e) => set({ sing_engine: e.target.value })} title={t("spSingEngineHint")}>
              <option value="auto">{t("spSingAuto")}</option>
              <option value="s2v">{t("spSingS2v")}</option>
              <option value="infinitetalk">{t("spSingTalk")}</option>
            </select>
          </>}
          {wired.has("motion") && !wired.has("audio") && (
            <label className="sp-num" title={t("spMotionStartHint")}>{t("spFrom")}<input type="number" min={0} step={0.1} value={data.motion_start_s ?? 0}
              onChange={(e) => set({ motion_start_s: Number(e.target.value) })} />s</label>
          )}
        </div>
        {["audio", "motion", "end"].filter((h) => wired.has(h)).length > 1 && <p className="sp-error" role="alert">{words("Conecta una sola guía: audio, movimiento o fotograma final.", "Connect one guide: audio, motion or end frame.")}</p>}
        <div className="small muted sp-mode">{wired.has("audio") ? t("spModeSing") : wired.has("motion") ? t("spModeMotion")
          : wired.has("end") ? t("spModeEnd") : t("spModeAnimate")}</div>
        <CameraPicker value={data.camera} onChange={(c) => set({ camera: c })} clips />
        <label className="sp-num nodrag">{words("Semilla", "Seed")}<input type="number" min={0} value={data.seed ?? ""} placeholder={words("Aleatoria", "Random")}
          onChange={(e) => set({ seed: e.target.value === "" ? null : Number(e.target.value) })} /></label>
      </>
    );
  } else if (kind === "music") {
    body = (
      <>
        <PromptBox value={data.tags || ""} onChange={(v) => set({ tags: v })} kind="music" placeholder={t("spMusicPh")} rows={2} />
        <textarea className="nodrag nowheel" rows={3} value={data.lyrics ?? "[Instrumental]"} placeholder={t("spLyricsPh")}
          onChange={(e) => set({ lyrics: e.target.value })} />
        <div className="sp-opts nodrag">
          <label className="sp-num">{t("spLength")}<input type="number" min={10} max={240} value={data.duration ?? 30}
            onChange={(e) => set({ duration: Number(e.target.value) })} />s</label>
          <label className="sp-num">BPM<input type="number" min={40} max={220} value={data.bpm ?? ""} placeholder="auto"
            onChange={(e) => set({ bpm: e.target.value ? Number(e.target.value) : null })} /></label>
          <select value={data.count || 1} onChange={(e) => set({ count: Number(e.target.value) })}>
            {[1, 2, 3, 4].map((n) => <option key={n} value={n}>×{n}</option>)}
          </select>
        </div>
      </>
    );
  } else if (kind === "assistant") {
    body = (
      <>
        <textarea className="nodrag nowheel" rows={4} value={data.prompt || ""} placeholder={t("spAssistantPh")}
          onChange={(e) => set({ prompt: e.target.value })} />
        <div className="sp-opts nodrag">
          <label className="check small"><input type="checkbox" checked={!!data.as_list} onChange={(e) => set({ as_list: e.target.checked })} /> {t("spAsList")}</label>
          {data.as_list && <label className="sp-num">{t("spItems")}<input type="number" min={1} max={24} value={data.items ?? 5}
            onChange={(e) => set({ items: Number(e.target.value) })} /></label>}
        </div>
        <div className="small muted sp-mode">{data.as_list ? t("spAsListHint") : t("spAssistantHint")}</div>
      </>
    );
  } else if (kind === "edit") {
    const v = data.operation === "remove_background" ? "bg" : data.operation === "pose_map" ? "pose"
      : data.operation === "depth_map" ? "depth" : String(data.scale || 2) === "4" ? "x4" : "x2";
    body = (
      <>
        <select className="nodrag" value={v} onChange={(e) => {
          const x = e.target.value;
          set(x === "bg" ? { operation: "remove_background" } : x === "pose" ? { operation: "pose_map" }
            : x === "depth" ? { operation: "depth_map" } : { operation: "upscale", scale: x === "x4" ? 4 : 2 });
        }}>
          <option value="x2">{t("spUpscale2")}</option>
          <option value="x4">{t("spUpscale4")}</option>
          <option value="bg">{t("spRemoveBg")}</option>
          <option value="pose">{t("spPoseMap")}</option>
          <option value="depth">{t("spDepthMap")}</option>
        </select>
        {(v === "pose" || v === "depth") && <div className="small muted sp-mode">{t(v === "pose" ? "spPoseHint" : "spDepthHint")}</div>}
      </>
    );
  } else if (kind === "variations") {
    const modes = ["angles", "expressions", "ages", "lighting", "storyboard", "custom"];
    body = (
      <>
        <div className="sp-opts nodrag">
          <select value={data.mode || "angles"} onChange={(e) => set({ mode: e.target.value })}>
            {modes.map((m) => <option key={m} value={m}>{t(`spVar_${m}` as MessageKey)}</option>)}
          </select>
          <select value={data.count || 4} onChange={(e) => set({ count: Number(e.target.value) })} title={t("spCount")}>
            {[2, 3, 4, 6, 9].map((n) => <option key={n} value={n}>×{n}</option>)}
          </select>
        </div>
        {data.mode === "custom" && <textarea className="nodrag nowheel" rows={4} value={data.custom || ""} placeholder={t("spVarCustomPh")}
          onChange={(e) => set({ custom: e.target.value })} />}
        <div className="small muted sp-mode">{t("spVarHint")}</div>
      </>
    );
  } else if (kind === "composite") {
    const layerCount = ctx.edges.filter((e) => e.target === id && e.targetHandle === "layers")
      .reduce((n, e) => n + Math.max(1, resolveOutputs(ctx, e.source, e.sourceHandle === "out" ? null : e.sourceHandle).length), 0);
    const layers: Record<string, any>[] = Array.isArray(data.layers) ? data.layers : [];
    const setLayer = (i: number, patch: Record<string, unknown>) => {
      const next = Array.from({ length: Math.max(layers.length, i + 1) }, (_, k) => ({ ...(layers[k] || {}) }));
      next[i] = { ...next[i], ...patch };
      set({ layers: next });
    };
    body = (
      <>
        <div className="small muted">{t("spCompositeHint")}</div>
        {Array.from({ length: Math.min(8, layerCount) }, (_, i) => {
          const L = layers[i] || {};
          return (
            <div key={i} className="sp-layer nodrag">
              <span className="small mono">{t("spLayerN", { n: i + 1 })}</span>
              <select value={L.blend || "normal"} onChange={(e) => setLayer(i, { blend: e.target.value })} title={t("spBlend")}>
                {["normal", "screen", "multiply", "overlay", "add", "lighten", "darken", "softlight", "difference"].map((m) =>
                  <option key={m} value={m}>{t(`spBlend_${m}` as MessageKey)}</option>)}
              </select>
              <select value={L.key || ""} onChange={(e) => setLayer(i, { key: e.target.value || undefined })} title={t("spKeyHint")}>
                <option value="">{t("spKeyNone")}</option><option value="black">{t("spKeyBlack")}</option><option value="white">{t("spKeyWhite")}</option>
              </select>
              <label className="sp-num" title={t("spOpacity")}>α<input type="number" min={0} max={1} step={0.05} value={L.opacity ?? 1}
                onChange={(e) => setLayer(i, { opacity: Number(e.target.value) })} /></label>
              <label className="sp-num" title={t("spScaleHint")}>⤢<input type="number" min={0.05} max={4} step={0.05} value={L.scale ?? 1}
                onChange={(e) => setLayer(i, { scale: Number(e.target.value) })} /></label>
              <label className="sp-num" title={t("spPosHint")}>x<input type="number" min={0} max={1} step={0.05} value={L.x ?? 0.5}
                onChange={(e) => setLayer(i, { x: Number(e.target.value) })} /></label>
              <label className="sp-num" title={t("spPosHint")}>y<input type="number" min={0} max={1} step={0.05} value={L.y ?? 0.5}
                onChange={(e) => setLayer(i, { y: Number(e.target.value) })} /></label>
            </div>
          );
        })}
      </>
    );
  } else if (kind === "clip_edit") {
    body = (
      <>
        <PromptBox value={data.prompt || ""} onChange={(v) => set({ prompt: v })} kind="video" placeholder={t("clipEditPh")} />
        <div className="sp-opts nodrag">
          <select value={data.mode || "auto"} onChange={(e) => set({ mode: e.target.value })}>
            <option value="auto">{t("clipEditModeAuto")}</option>
            <option value="edit">{t("clipEditModeEdit")}</option>
            <option value="restyle">{t("clipEditModeRestyle")}</option>
            <option value="reference">{t("clipEditModeReference")}</option>
            <option value="propagate">{t("clipEditModePropagate")}</option>
          </select>
          <select value={data.quality || "draft"} onChange={(e) => set({ quality: e.target.value })}>
            <option value="draft">{t("sbDraft")}</option><option value="final">{t("sbFinal")}</option>
          </select>
          <label className="sp-num" title={t("spClipEditFromHint")}>{t("spFrom")}<input type="number" min={0} step={0.5} value={data.start_s ?? 0}
            onChange={(e) => set({ start_s: Number(e.target.value) })} />s</label>
        </div>
        <div className="small muted sp-mode">{wired.has("first") ? t("spClipEditFirst") : t("spClipEditHint")}</div>
      </>
    );
  } else if (kind === "combine") {
    body = (
      <>
        <div className="small muted">{t("spCombineHint")}</div>
        {wired.has("audio") && <div className="sp-opts nodrag">
          <label className="sp-num" title={t("spAudioStartHint")}>{t("spFrom")}<input type="number" min={0} step={0.1} value={data.audio_start_s ?? 0}
            onChange={(e) => set({ audio_start_s: Number(e.target.value) })} />s</label>
        </div>}
      </>
    );
  } else if (kind === "list") {
    // every incoming item, ticked or not
    const items: [Port, string][] = [];
    const seen = new Set<string>();
    for (const e of ctx.edges) {
      if (e.target !== id) continue;
      for (const it of resolveOutputs(ctx, e.source, e.sourceHandle === "out" ? null : e.sourceHandle)) {
        if (!seen.has(it[1])) { seen.add(it[1]); items.push(it); }
      }
    }
    const unticked: string[] = data.unticked || [];
    body = items.length === 0 ? <p className="small muted">{t("spListEmpty")}</p> : (
      <div className="sp-thumbs">
        {items.map(([port, v]) => port === "text"
          ? <button key={v} className={`sp-chip nodrag${unticked.includes(v) ? " dim" : ""}`}
              onClick={() => set({ unticked: unticked.includes(v) ? unticked.filter((x) => x !== v) : [...unticked, v] })}>{v.slice(0, 40)}</button>
          : <div key={v} className="sp-thumb-wrap">
              <Thumb id={v} port={port} dim={unticked.includes(v)} onClick={() => ctx.open(v, items.filter(([p]) => p !== "text").map(([, x]) => x))} />
              <button className={`sp-tick nodrag${unticked.includes(v) ? "" : " on"}`} title={t("spTick")}
                onClick={() => set({ unticked: unticked.includes(v) ? unticked.filter((x) => x !== v) : [...unticked, v] })}><Check size={11} /></button>
            </div>)}
      </div>
    );
  }

  // outputs of a generator: tick to pass on, click to view, earlier runs
  let outputs: ReactNode = null;
  if (kind === "assistant" && (st?.texts?.length || st?.error)) {
    const ex = new Set(st.excluded || []);
    outputs = (
      <div className="sp-outputs">
        {st.error && <div className="sp-error small">{st.error}</div>}
        {(st.texts || []).map((x) => (
          <button key={x} className={`sp-chip nodrag${ex.has(x) ? " dim" : ""}`} title={t("spTick")}
            onClick={() => ctx.setExcluded(id, ex.has(x) ? [...ex].filter((y) => y !== x) : [...ex, x])}>{x}</button>
        ))}
      </div>
    );
  } else if (runnable && (st?.outputs?.length || st?.error)) {
    const list = st.outputs || [];
    const ex = new Set(st.excluded || []);
    const port: Port = outputPort(kind, data) || "image";
    const runs = st.runs || [];
    const current = runs.findIndex((r) => r.outputs.join() === list.join());
    outputs = (
      <div className="sp-outputs">
        {st.error && <div className="sp-error small">{st.error}</div>}
        {list.length > 0 && <div className={`sp-thumbs${list.length === 1 ? " sp-single-result" : ""}`}>
          {list.map((a) => (
            <div key={a} className="sp-thumb-wrap">
              <Thumb id={a} port={port} dim={ex.has(a)} onClick={() => ctx.open(a, list)} />
              <button className={`sp-tick nodrag${ex.has(a) ? "" : " on"}`} title={t("spTick")} aria-pressed={!ex.has(a)}
                onClick={() => ctx.setExcluded(id, ex.has(a) ? [...ex].filter((x) => x !== a) : [...ex, a])}><Check size={11} /></button>
            </div>
          ))}
        </div>}
        {runs.length > 1 && (
          <select className="nodrag sp-runs" value={current} onChange={(e) => ctx.pickRun(id, runs[Number(e.target.value)].outputs)}>
            {current < 0 && <option value={-1}>—</option>}
            {runs.map((r, i) => <option key={i} value={i}>{t("spRunN", { n: i + 1, total: runs.length })} · {r.at.slice(11, 16)}</option>)}
          </select>
        )}
      </div>
    );
  }

  const appLabel = (data.app_input && APP_INPUTS.includes(kind)) || (data.app_output && runnable) ? (
    <input className="nodrag sp-app-label" value={data.app_label || ""} placeholder={t("spAppLabelPh")}
      onChange={(e) => set({ app_label: e.target.value })} />
  ) : null;
  const inputs = INPUTS[kind] || [];
  const outs: [string, Port][] = kind === "cast" ? [["image", "image"], ["text", "text"]]
    : kind === "video" ? [["out", "video"], ["last", "image"]] : out ? [["out", out]] : [];
  return (
    <div className={`sp-node sp-${kind}${selected ? " selected" : ""}${busy ? " busy" : ""}`}>
      <div className="sp-head">
        <Icon size={14} />
        <input className="sp-node-name nodrag" aria-label={words("Nombre del nodo", "Node name")} value={data.title ?? t(TYPE_LABEL[kind])}
          onChange={(e) => set({ title: e.target.value })} />
        <StatusChip st={st} />
      </div>
      <div className="sp-node-actions nodrag">
        {runnable && <>
          <button className="btn xs ghost nodrag" title={t("spRunNode")} disabled={busy} onClick={() => ctx.run(id, "node")}><Play size={13} />{words("Generar", "Generate")}</button>
          <button className="btn xs icon ghost nodrag" title={t("spRunUpto")} disabled={busy} onClick={() => ctx.run(id, "upto")}><ListEnd size={13} /></button>
          <button className="btn xs icon ghost nodrag" title={t("spRunDown")} disabled={busy} onClick={() => ctx.run(id, "downstream")}><FastForward size={13} /></button>
        </>}
        {APP_INPUTS.includes(kind) && <button className={`btn xs icon ghost nodrag${data.app_input ? " sp-on" : ""}`}
          title={t("spAppInputHint")} onClick={() => set({ app_input: !data.app_input })}><LogIn size={12} /></button>}
        {runnable && <button className={`btn xs icon ghost nodrag${data.app_output ? " sp-on" : ""}`}
          title={t("spAppOutputHint")} onClick={() => set({ app_output: !data.app_output })}><Star size={12} /></button>}
        <button className="btn xs icon ghost nodrag" title={t("spDuplicate")} onClick={() => ctx.duplicate(id)}><Copy size={12} /></button>
        <button className="btn xs icon ghost nodrag" title={t("delete")} onClick={() => ctx.remove(id)}><Trash2 size={12} /></button>
      </div>
      {(inputs.length > 0 || outs.length > 0) && (
        <div className="sp-ports">
          <div className="sp-ins">
            {inputs.map(([h, port, many]) => (
              <div key={h} className="sp-port in">
                <Handle type="target" position={Position.Left} id={h} style={{ background: PORT_COLOR[port] }} />
                <span style={{ color: PORT_COLOR[port] }}>{t(`spIn_${h}` as MessageKey)}</span>
                {!many && <span className="sp-one">1</span>}
              </div>
            ))}
          </div>
          <div className="sp-outs">
            {outs.map(([h, port]) => (
              <div key={h} className="sp-port out">
                <span style={{ color: PORT_COLOR[port] }}>{t(h === "last" ? "spOut_last" : `spOut_${port}` as MessageKey)}</span>
                <Handle type="source" position={Position.Right} id={h} style={{ background: PORT_COLOR[port] }} />
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="sp-body">{appLabel}{body}{outputs}</div>
      <div className="sp-node-foot"><span>{t(TYPE_LABEL[kind])}</span><span>{inputs.filter(([h]) => wired.has(h)).length ? words("Entradas conectadas", "Inputs connected") : id}</span></div>
    </div>
  );
});

/** A frame that holds nodes together: dragging it carries the nodes inside. */
const GroupNodeView = memo(function GroupNodeView({ id, data: nd, selected }: NodeProps<SpNode>) {
  const { t } = useT();
  const ctx = useContext(SpaceCtx);
  const color = nd.data.color || "#b48cf0";
  return (
    <div className="sp-group" style={{ borderColor: color, background: `${color}14` }}>
      <NodeResizer isVisible={selected} minWidth={200} minHeight={140} lineStyle={{ borderColor: color }}
        handleStyle={{ background: color }} onResizeEnd={() => ctx.touched()} />
      <div className="sp-group-head">
        <input className="nodrag" value={nd.data.title || ""} placeholder={t("spGroupPh")} style={{ color }}
          onChange={(e) => ctx.update(id, { title: e.target.value })} />
        <span className="grow" />
        {["#b48cf0", "#7fa6d9", "#5bbf86", "#eb80c6", "#e07a6e", "#9a95a6"].map((c) => (
          <button key={c} className="sp-group-dot nodrag" style={{ background: c }} onClick={() => ctx.update(id, { color: c })} />
        ))}
        <button className="btn xs icon ghost nodrag" title={t("spExportGroup")} onClick={() => ctx.exportTechnique(id)}><Download size={12} /></button>
        <button className="btn xs icon ghost nodrag" title={t("delete")} onClick={() => ctx.remove(id)}><Trash2 size={12} /></button>
      </div>
    </div>
  );
});

const NODE_TYPES = { sp: SpaceNodeView, grp: GroupNodeView };

// ------------------------------------------------------------ the app view

/** Spaces opened from the list with "use as app" start in the form view. */
const OPEN_AS_APP = new Set<string>();

/** A space as a form: the marked inputs as fields, the marked generators as
 * results. Running it writes the values into the graph and runs it all. */
function AppView({ spaceId, onRan, state, beforeRun }: { spaceId: string; onRan: () => void; state: Record<string, SpaceNodeState>; beforeRun: () => Promise<boolean> }) {
  const { t } = useT();
  const app = useApp();
  const ctx = useContext(SpaceCtx);
  const [view, setView] = useState<SpaceApp | null>(null);
  const [values, setValues] = useState<Record<string, any>>({});
  const [picking, setPicking] = useState<{ node: string; kind: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const stateKey = Object.entries(state).map(([k, v]) => `${k}:${v.status}:${(v.outputs || []).join(",")}:${(v.excluded || []).length}`).join("|");
  useEffect(() => {
    api.spaceApp(spaceId).then((v) => {
      setView(v);
      setValues((old) => Object.keys(old).length ? old : Object.fromEntries(v.inputs.map((f) => [f.node, f.value])));
    }).catch((e) => app.toast((e as Error).message, "bad"));
  }, [spaceId, stateKey]); // eslint-disable-line react-hooks/exhaustive-deps
  // an unnamed field or result shows its node's kind ("Variations 1"), not its id
  const labelOf = (node: string, label: string) => {
    const n = ctx.nodes.find((x) => x.id === node);
    if (label !== node || !n) return label;
    const num = node.match(/(\d+)$/)?.[1];
    return `${t(TYPE_LABEL[n.data.kind])}${num ? ` ${num}` : ""}`;
  };
  if (!view) return <div className="sp-app"><Loader2 size={18} className="spin" /></div>;
  const running = view.outputs.some((o) => o.status === "queued" || o.status === "running") || busy;
  const go = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      if (!await beforeRun()) return;
      const res = await api.runSpaceApp(spaceId, values);
      setView(res.app);
      onRan();
      app.refreshJobs();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { busyRef.current = false; setBusy(false); }
  };
  return (
    <div className="sp-app">
      <div className="sp-app-form card">
        <h2><AppWindow size={18} /> {view.name}</h2>
        {view.description && <p className="muted">{view.description}</p>}
        {view.inputs.length === 0 && <p className="small muted">{t("spAppNoInputs")}</p>}
        {view.inputs.map((f) => (
          <label key={f.node} className="field">
            <span>{labelOf(f.node, f.label)}</span>
            {f.type === "text" ? (
              <textarea rows={3} value={values[f.node] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [f.node]: e.target.value }))} />
            ) : f.type === "cast" ? (
              <select value={values[f.node] ?? ""} onChange={(e) => setValues((v) => ({ ...v, [f.node]: e.target.value }))}>
                <option value="">—</option>
                {ctx.characters.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            ) : (
              <div className="sp-app-assets">
                {((values[f.node] || []) as string[]).map((a) => (
                  <div key={a} className="sp-app-asset">
                    <Thumb id={a} port={(f.kind || "image") as Port} onClick={() => app.openAsset(a, values[f.node])} />
                    <button className="btn xs icon ghost" onClick={() => setValues((v) => ({ ...v, [f.node]: (v[f.node] || []).filter((x: string) => x !== a) }))}><X size={11} /></button>
                  </div>
                ))}
                <button className="btn sm" onClick={() => setPicking({ node: f.node, kind: f.kind || "image" })}><Plus size={13} /> {t("spAddFromLibrary")}</button>
              </div>
            )}
          </label>
        ))}
        <button className="btn primary" disabled={running} onClick={go}>
          {running ? <Loader2 size={14} className="spin" /> : <Play size={14} />} {t("spAppRun")}</button>
      </div>
      <div className="sp-app-results">
        {view.outputs.length === 0 && <p className="small muted">{t("spAppNoOutputs")}</p>}
        {view.outputs.map((o) => (
          <div key={o.node} className="card sp-app-out">
            <div className="row" style={{ gap: 8 }}><strong>{labelOf(o.node, o.label)}</strong><StatusChip st={state[o.node] || (o.status ? { status: o.status } as SpaceNodeState : undefined)} /></div>
            {o.error && <p className="small bad">{o.error}</p>}
            {o.texts?.length ? <ul className="small">{o.texts.map((x, i) => <li key={i}>{x}</li>)}</ul> : null}
            <div className="sp-app-grid">
              {o.outputs.map((a) => <Thumb key={a} id={a} port={(o.kind || "image") as Port} onClick={() => app.openAsset(a, o.outputs)} />)}
            </div>
          </div>
        ))}
      </div>
      {picking && (
        <AssetPicker projectId={app.projectId!} kind={picking.kind} allProjects title={t("spAddFromLibrary")}
          onClose={() => setPicking(null)}
          onPick={(a) => {
            setValues((v) => ({ ...v, [picking.node]: [...((v[picking.node] || []) as string[]).filter((x) => x !== a.id), a.id] }));
            setPicking(null);
          }} />
      )}
    </div>
  );
}

// ------------------------------------------------------------ the editor

type SaveState = "saved" | "dirty" | "saving" | "error";
type EditSnapshot = { graph: SpaceGraph; name: string };
type SpaceDraft = EditSnapshot & { baseVersion: number | null; updatedAt: string };

function Editor({ spaceId, onBack }: { spaceId: string; onBack: () => void }) {
  const { t, lang } = useT();
  const app = useApp();
  // the app context is a new object on every app render (jobs poll): read it through refs in timers
  const appRef = useRef(app);
  appRef.current = app;
  const langRef = useRef(lang);
  langRef.current = lang;
  const flow = useReactFlow();
  const updateInternals = useUpdateNodeInternals();
  const [space, setSpace] = useState<Space | null>(null);
  const [nodes, setNodes] = useState<SpNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [state, setState] = useState<Record<string, SpaceNodeState>>({});
  const [name, setName] = useState("");
  const [save, setSave] = useState<SaveState>("saved");
  const [dirty, setDirty] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [storageError, setStorageError] = useState(false);
  const [recovery, setRecovery] = useState<SpaceDraft | null>(null);
  const recoveryRef = useRef<SpaceDraft | null>(null);
  const [conflict, setConflict] = useState(false);
  const [runSubmitting, setRunSubmitting] = useState(false);
  const runRequest = useRef(false);
  const conflictRef = useRef(false);
  const [historyFlags, setHistoryFlags] = useState({ undo: false, redo: false });
  const history = useRef(new EditHistory<EditSnapshot>({ graph: { nodes: [], edges: [] }, name: "" }));
  const [tabId] = useState(() => {
    try {
      const existing = sessionStorage.getItem("prospero.editing-tab");
      if (existing) return existing;
      const id = crypto.randomUUID(); sessionStorage.setItem("prospero.editing-tab", id); return id;
    } catch { return crypto.randomUUID(); }
  });
  const draftPrefix = `prospero.space-draft.${spaceId}`;
  const draftKey = `${draftPrefix}.${tabId}`;
  const recoveredKey = useRef(draftKey);
  const [picking, setPicking] = useState<{ id: string; kind: string } | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; fx: number; fy: number; from?: { node: string; handle: string; type: "source" | "target" } } | null>(null);
  const [polling, setPolling] = useState(false);
  const version = useRef<number | null>(null);
  const viewport = useRef<{ x: number; y: number; zoom: number } | undefined>(undefined);
  const latest = useRef({ nodes, edges, name });
  latest.current = { nodes, edges, name };
  const liveNodes = useCallback((update: (value: SpNode[]) => SpNode[]) => {
    const next = update(latest.current.nodes); latest.current = { ...latest.current, nodes: next }; setNodes(next);
  }, []);
  const liveEdges = useCallback((update: (value: Edge[]) => Edge[]) => {
    const next = update(latest.current.edges); latest.current = { ...latest.current, edges: next }; setEdges(next);
  }, []);
  const snapshot = useCallback((includeViewport = false): EditSnapshot => ({
    graph: toGraph(latest.current.nodes, latest.current.edges, includeViewport ? viewport.current : undefined), name: latest.current.name,
  }), []);
  const persistDraft = useCallback(() => {
    try {
      localStorage.setItem(draftKey, JSON.stringify({ ...snapshot(true), baseVersion: version.current, updatedAt: new Date().toISOString() }));
      setStorageError(false);
    } catch { setStorageError(true); }
  }, [draftKey, snapshot]);
  const queueRef = useRef<SaveQueue<EditSnapshot> | null>(null);
  if (!queueRef.current) queueRef.current = new SaveQueue({
    read: () => snapshot(true),
    write: async (value) => {
      if (version.current === null || recoveryRef.current || conflictRef.current) throw new Error(langRef.current === "es" ? "Revisa el borrador antes de guardar." : "Review the draft before saving.");
      setSave("saving");
      const res = await api.saveSpace(spaceId, value.graph, version.current, value.name.trim() || undefined);
      version.current = res.version;
      if (latest.current.name === value.name && res.name !== value.name) { latest.current.name = res.name; setName(res.name); }
    },
    onSaved: () => {
      setSave("saved"); setSaveError("");
      if (!recoveryRef.current) { try { localStorage.removeItem(draftKey); } catch { /* do not claim a local copy */ } }
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) { conflictRef.current = true; setConflict(true); }
      setSave("error"); setSaveError((err as Error).message); persistDraft();
    },
  });
  const queue = queueRef.current;
  const updateHistoryFlags = useCallback(() => setHistoryFlags({ undo: history.current.canUndo, redo: history.current.canRedo }), []);
  const markDirty = useCallback((group?: string, recordHistory = true) => {
    if (version.current === null || recoveryRef.current) return;
    if (recordHistory) { history.current.commit(snapshot(), group); updateHistoryFlags(); }
    queue.changed(); persistDraft(); setSave("dirty"); setDirty((d) => d + 1);
  }, [queue, persistDraft, snapshot, updateHistoryFlags]);
  const wrap = useRef<HTMLDivElement>(null);
  const focusNode = (id: string) => {
    const bounds = wrap.current?.getBoundingClientRect();
    const node = flow.getInternalNode(id);
    if (bounds && bounds.width <= 600 && node) {
      // A tall card must stay readable: focus by width and pan vertically,
      // rather than shrinking every control to fit the available height.
      const width = node.measured.width || node.width || 340;
      const height = node.measured.height || node.height || 700;
      const zoom = Math.max(1, Math.min(1.15, (bounds.width - 32) / width));
      const left = Math.max(16, (bounds.width - width * zoom) / 2);
      const top = Math.max(16, (bounds.height - height * zoom) / 2);
      const position = node.internals.positionAbsolute;
      void flow.setViewport({ x: left - position.x * zoom, y: top - position.y * zoom, zoom }, { duration: 250 });
    } else {
      void flow.fitView({ nodes: [{ id }], padding: .3, maxZoom: 1, duration: 250 });
    }
  };
  const focusNodeRef = useRef(focusNode);
  focusNodeRef.current = focusNode;
  const addOpener = useRef<HTMLButtonElement>(null);
  const closeMenu = () => { setMenu(null); addOpener.current?.focus(); };
  const characters = useAsync(() => api.characters(app.projectId!), [app.projectId]);
  const chars = useMemo(() => (characters.data?.items || []), [characters.data]);

  const load = useCallback(async (fit = false, keepHistory = false) => {
    setLoadError("");
    try {
    const s = await api.space(spaceId);
    setSpace(s);
    setName(s.name);
    version.current = s.version;
    const rf = toRf(s.graph);
    latest.current = { nodes: rf.nodes, edges: rf.edges, name: s.name };
    setNodes(rf.nodes);
    setEdges(rf.edges);
    setState(s.state);
    viewport.current = s.graph.viewport;
    if (keepHistory) history.current.commit(snapshot()); else history.current.reset(snapshot());
    updateHistoryFlags(); queue.clean(); setSave("saved"); setSaveError(""); conflictRef.current = false; setConflict(false);
    let draft: SpaceDraft | null = null;
    try {
      // Separate tab journals: a successful save in one tab must not erase another tab's unsaved work.
      const candidates: { key: string; draft: SpaceDraft }[] = [];
      for (const key of Object.keys(localStorage).filter(k => k === draftPrefix || k.startsWith(`${draftPrefix}.`))) {
        try {
          const stored = JSON.parse(localStorage.getItem(key) || "null") as SpaceDraft | null;
          if (!stored || typeof stored.name !== "string" || !Array.isArray(stored.graph?.nodes) || !Array.isArray(stored.graph?.edges)
            || !stored.graph.nodes.every(n => typeof n.id === "string" && ADDABLE.includes(n.type) && typeof n.x === "number" && typeof n.y === "number")) continue;
          if (JSON.stringify({ graph: stored.graph, name: stored.name }) !== JSON.stringify({ graph: s.graph, name: s.name })) candidates.push({ key, draft: stored });
          else if (key === draftKey) localStorage.removeItem(key);
        } catch { /* one malformed journal must not hide other recoverable drafts */ }
      }
      const selected = candidates.find(c => c.key === draftKey) || candidates.sort((a, b) => String(b.draft.updatedAt).localeCompare(String(a.draft.updatedAt)))[0];
      if (selected) { draft = selected.draft; recoveredKey.current = selected.key; }
    } catch { /* malformed or unavailable storage does not block the server version */ }
    recoveryRef.current = draft; setRecovery(draft);
    // re-measure the ports once laid out (and once the fonts are in): a wire
    // whose handle was measured too early is otherwise not drawn
    const ids = rf.nodes.map((n) => n.id);
    for (const ms of [60, 400]) setTimeout(() => updateInternals(ids), ms);
    document.fonts?.ready.then(() => updateInternals(ids)).catch(() => undefined);
    if (fit) setTimeout(() => {
      if (s.graph.viewport && s.graph.nodes.length) flow.setViewport(s.graph.viewport);
      else flow.fitView({ padding: 0.2, maxZoom: 1 });
    }, 30);
    if (Object.values(s.state).some((x) => x.status === "queued" || x.status === "running")) setPolling(true);
    } catch (e) { setLoadError((e as Error).message); }
  }, [spaceId, flow, updateInternals, snapshot, updateHistoryFlags, queue, draftKey, draftPrefix]);

  useEffect(() => { void load(true); }, [load]);

  const flush = useCallback(async () => {
    if (version.current === null || recoveryRef.current || conflictRef.current) return false;
    return queue.flush();
  }, [queue]);
  // Leaving via global navigation must not cancel the pending debounce and
  // discard the last edit. The promise already in flight still runs in order.
  useEffect(() => () => { if (queue.isDirty && !conflictRef.current && !recoveryRef.current) void flush(); }, [flush, queue]);
  useEffect(() => {
    if (!dirty || conflict || recovery) return;
    const id = setTimeout(flush, 700);
    return () => clearTimeout(id);
  }, [dirty, flush, conflict, recovery]);
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (queue.isDirty || recoveryRef.current) { e.preventDefault(); e.returnValue = ""; }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [queue]);
  const restoreDraft = () => {
    const draft = recoveryRef.current;
    if (!draft) return;
    const rf = toRf(draft.graph);
    liveNodes(() => rf.nodes); liveEdges(() => rf.edges); latest.current.name = draft.name; setName(draft.name);
    viewport.current = draft.graph.viewport;
    if (draft.graph.viewport) void flow.setViewport(draft.graph.viewport);
    recoveryRef.current = null; setRecovery(null);
    if (draft.baseVersion !== version.current) { conflictRef.current = true; setConflict(true); }
    markDirty();
  };
  const undoRedo = (redo = false) => {
    if (recoveryRef.current) return;
    const value = redo ? history.current.redo() : history.current.undo();
    if (!value) return;
    const rf = toRf(value.graph);
    liveNodes(() => rf.nodes); liveEdges(() => rf.edges); latest.current.name = value.name; setName(value.name);
    updateHistoryFlags(); markDirty(undefined, false);
  };

  // poll the state while something runs
  useEffect(() => {
    if (!polling) return;
    const id = setInterval(async () => {
      try {
        const s = await api.space(spaceId);
        setState(s.state);
        if (!Object.values(s.state).some((x) => x.status === "queued" || x.status === "running")) {
          setPolling(false);
          appRef.current.bump();
        }
      } catch { /* keep trying */ }
    }, 1500);
    return () => clearInterval(id);
  }, [polling, spaceId]);

  const run = useCallback(async (mode: "node" | "downstream" | "upto" | "all", ids: string[] = [], force = false) => {
    if (runRequest.current) return;
    runRequest.current = true; setRunSubmitting(true);
    try {
      if (!await flush()) return;
      const res = await api.runSpace(spaceId, mode, ids, force);
      setState((s) => ({ ...s, ...Object.fromEntries(res.nodes.map((n) => [n, { ...(s[n] || {}), status: "queued" as const, error: null }])) }));
      setPolling(true);
      appRef.current.refreshJobs();
    } catch (e) {
      appRef.current.toast((e as Error).message, "bad");
    } finally { runRequest.current = false; setRunSubmitting(false); }
  }, [flush, spaceId]);

  const addNode = useCallback((kind: SpaceNodeType, at?: { x: number; y: number }, data?: Record<string, unknown>) => {
    const taken = new Set(latest.current.nodes.map((n) => n.id));
    const id = newId(kind, taken);
    let pos = at;
    if (!pos) {
      const r = wrap.current?.getBoundingClientRect();
      pos = flow.screenToFlowPosition({ x: (r?.left || 0) + (r?.width || 800) / 2, y: (r?.top || 0) + (r?.height || 600) / 2 });
      pos = { x: pos.x - WIDTH[kind] / 2 + (taken.size % 5) * 24, y: pos.y - 80 + (taken.size % 5) * 24 };
    }
    if (kind !== "group") pos = freeNodePosition(pos, { width: WIDTH[kind], height: ["image", "video", "clip_edit", "music"].includes(kind) ? 650 : 420 },
      latest.current.nodes.filter(n => n.data.kind !== "group").map(n => ({ ...n.position, width: n.measured?.width || Number(n.style?.width) || WIDTH[n.data.kind], height: n.measured?.height || 650 })));
    const node: SpNode = { id, type: kind === "group" ? "grp" : "sp", position: pos, data: { kind, data: { ...DEFAULT_DATA[kind], ...(data || {}) } },
      style: { width: WIDTH[kind], ...(kind === "group" ? { height: 380 } : {}) }, ...(kind === "group" ? { zIndex: -1 } : {}) };
    liveNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), { ...node, selected: true }]);
    markDirty(`add:${id}`);
    setTimeout(() => focusNodeRef.current(id), 60);
    return id;
  }, [flow, markDirty, liveNodes]);

  const connectOk = useCallback((c: Connection | Edge) => {
    const { nodes: ns, edges: es } = latest.current;
    const src = ns.find((n) => n.id === c.source);
    const dst = ns.find((n) => n.id === c.target);
    if (!src || !dst || src.id === dst.id) return false;
    const spec = (INPUTS[dst.data.kind] || []).find(([h]) => h === c.targetHandle);
    if (!spec) return false;
    const produced = outputPort(src.data.kind, src.data.data, c.sourceHandle === "out" ? null : c.sourceHandle);
    if (!produced) return false;
    if (spec[1] !== "any" && produced !== spec[1] && produced !== "any") return false;
    if (!spec[2] && es.some((e) => e.target === dst.id && e.targetHandle === c.targetHandle)) return false;
    if (es.some((e) => e.source === c.source && e.target === c.target && e.targetHandle === c.targetHandle && e.sourceHandle === c.sourceHandle)) return false;
    return !makesLoop(es, src.id, dst.id);
  }, []);

  const connect = useCallback((c: Connection, historyGroup?: string) => {
    if (!connectOk(c)) return;
    const src = latest.current.nodes.find((n) => n.id === c.source)!;
    const sh = c.sourceHandle && c.sourceHandle !== "out" ? c.sourceHandle : null;
    const e = edgeOf({ id: `e-${c.source}-${sh || "o"}-${c.target}-${c.targetHandle}`.slice(0, 120), source: c.source, source_handle: sh,
      target: c.target, target_handle: c.targetHandle || "" }, src.data.kind, src.data.data);
    liveEdges((es) => [...es, e]);
    markDirty(historyGroup);
  }, [connectOk, markDirty, liveEdges]);

  // dropping a wire on empty canvas offers the nodes that fit it
  const onConnectEnd: OnConnectEnd = useCallback((event, cs) => {
    if (cs.isValid || cs.toNode || !cs.fromNode || !cs.fromHandle) return;
    const p = "changedTouches" in event ? event.changedTouches[0] : event;
    const r = wrap.current?.getBoundingClientRect();
    const f = flow.screenToFlowPosition({ x: p.clientX, y: p.clientY });
    setMenu({ x: p.clientX - (r?.left || 0), y: p.clientY - (r?.top || 0), fx: f.x, fy: f.y,
      from: { node: cs.fromNode.id, handle: cs.fromHandle.id || "out", type: cs.fromHandle.type } });
  }, [flow]);

  const menuChoices = useMemo((): { kind: SpaceNodeType; handle?: string }[] => {
    if (!menu) return [];
    if (!menu.from) return ADDABLE.map((k) => ({ kind: k }));
    const from = nodes.find((n) => n.id === menu.from!.node);
    if (!from) return [];
    if (menu.from.type === "source") {
      const produced = outputPort(from.data.kind, from.data.data, menu.from.handle === "out" ? null : menu.from.handle);
      const out: { kind: SpaceNodeType; handle?: string }[] = [];
      for (const k of ADDABLE) {
        const spec = (INPUTS[k] || []).find(([, port]) => port === produced || port === "any" || produced === "any");
        if (spec) out.push({ kind: k, handle: spec[0] });
      }
      return out;
    }
    const accepts = (INPUTS[from.data.kind] || []).find(([h]) => h === menu.from!.handle)?.[1];
    const fits: Record<Port, SpaceNodeType[]> = {
      text: ["text", "cast"], image: ["image", "asset", "cast", "list"], video: ["video", "asset", "list"], audio: ["music", "asset", "list"], any: ADDABLE,
    };
    return (fits[accepts || "any"] || []).map((k) => ({ kind: k }));
  }, [menu, nodes]);

  const pickFromMenu = (choice: { kind: SpaceNodeType; handle?: string }) => {
    if (!menu) return;
    const from = menu.from;
    let data: Record<string, unknown> | undefined;
    if (from?.type === "target" && choice.kind === "asset") {
      const dst = nodes.find((n) => n.id === from.node);
      const accepts = (INPUTS[dst?.data.kind as SpaceNodeType] || []).find(([h]) => h === from.handle)?.[1];
      data = { kind: accepts && accepts !== "any" && accepts !== "text" ? accepts : "image", asset_ids: [] };
    }
    const at = { x: menu.fx - (from?.type === "target" ? WIDTH[choice.kind] : 0), y: menu.fy - 20 };
    const id = addNode(choice.kind, at, data);
    setMenu(null);
    if (!from) return;
    setTimeout(() => {
      if (from.type === "source") connect({ source: from.node, sourceHandle: from.handle, target: id, targetHandle: choice.handle || null }, `add:${id}`);
      else connect({ source: id, sourceHandle: choice.kind === "cast" ? (from.handle === "prompt" ? "text" : "image") : "out", target: from.node, targetHandle: from.handle }, `add:${id}`);
    }, 0);
  };

  const ctx: Ctx = {
    projectId: app.projectId!, state, characters: chars, edges, nodes,
    toast: (message) => app.toast(message, "bad"),
    appendAssets: (id, ids) => {
      liveNodes((ns) => ns.map((n) => n.id === id ? { ...n, data: { ...n.data, data: { ...n.data.data, asset_ids: [...new Set([...(n.data.data.asset_ids || []), ...ids])] } } } : n));
      markDirty();
    },
    update: (id, patch) => {
      liveNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, data: { ...n.data.data, ...patch } } } : n)));
      // an asset node switching kind re-colours (and may invalidate) its wires
      if ("kind" in patch) liveEdges((es) => es.filter((e) => e.source !== id));
      markDirty(`node:${id}:${Object.keys(patch).join(",")}`);
    },
    run: (id, mode) => run(mode, [id]),
    remove: (id) => {
      liveNodes((ns) => ns.filter((n) => n.id !== id));
      liveEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
      markDirty();
    },
    duplicate: (id) => {
      const n = latest.current.nodes.find((x) => x.id === id);
      if (n) addNode(n.data.kind, { x: n.position.x + 40, y: n.position.y + 40 }, JSON.parse(JSON.stringify(n.data.data)));
    },
    setExcluded: async (id, excluded) => {
      setState((s) => ({ ...s, [id]: { ...(s[id] || {}), excluded } }));
      try { setState((await api.spaceNode(spaceId, id, { excluded })).state); } catch (e) { app.toast((e as Error).message, "bad"); }
    },
    pickRun: async (id, outputs) => {
      try { setState((await api.spaceNode(spaceId, id, { outputs })).state); } catch (e) { app.toast((e as Error).message, "bad"); }
    },
    pickAssets: (id, kind) => setPicking({ id, kind }),
    open: (aid, list) => app.openAsset(aid, list),
    touched: markDirty,
    exportTechnique: async (group) => {
      try {
        if (!await flush()) return;
        const bundle = await api.exportSpace(spaceId, group);
        const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = `${String(bundle.name || "technique").replace(/[^\w\- ]+/g, "").trim() || "technique"}.technique.json`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 2000);
        app.toast(t("spExported", { models: (bundle.models || []).length }), "ok");
      } catch (e) { app.toast((e as Error).message, "bad"); }
    },
  };

  // a group carries the nodes that sit inside it when dragged
  const carried = useRef<{ group: { x: number; y: number }; nodes: Map<string, { x: number; y: number }> } | null>(null);
  const onNodeDragStart = useCallback((_: unknown, node: SpNode) => {
    if (node.type !== "grp") { carried.current = null; return; }
    const w = Number(node.width ?? node.style?.width ?? WIDTH.group);
    const h = Number(node.height ?? node.style?.height ?? 380);
    const inside = new Map<string, { x: number; y: number }>();
    for (const n of latest.current.nodes) {
      if (n.id === node.id || n.type === "grp" || n.selected) continue;
      const { x, y } = n.position;
      if (x >= node.position.x && y >= node.position.y && x <= node.position.x + w - 40 && y <= node.position.y + h - 40) inside.set(n.id, { x, y });
    }
    carried.current = { group: { ...node.position }, nodes: inside };
  }, []);
  const onNodeDrag = useCallback((_: unknown, node: SpNode) => {
    const c = carried.current;
    if (!c || node.type !== "grp" || !c.nodes.size) return;
    const dx = node.position.x - c.group.x;
    const dy = node.position.y - c.group.y;
    liveNodes((ns) => ns.map((n) => {
      const p0 = c.nodes.get(n.id);
      return p0 ? { ...n, position: { x: p0.x + dx, y: p0.y + dy } } : n;
    }));
  }, [liveNodes]);

  // what a full run would cost: renders, minutes and the biggest engine
  const [est, setEst] = useState<SpaceEstimate | null>(null);
  const runningKey = Object.entries(state).map(([k, v]) => `${k}:${v.status}:${(v.outputs || []).length}`).join("|");
  // the graph itself (a build or an import changes it without an edit of ours)
  const graphKey = `${nodes.map((n) => n.id).join(",")}|${edges.length}`;
  useEffect(() => {
    if (save !== "saved") return;
    const id = setTimeout(() => { api.spaceEstimate(spaceId).then(setEst).catch(() => setEst(null)); }, 500);
    return () => clearTimeout(id);
  }, [save, spaceId, runningKey, graphKey]);

  // the assistant that builds part of the graph from one sentence
  const [building, setBuilding] = useState<{ open: boolean; text: string; busy: boolean }>({ open: false, text: "", busy: false });
  const build = async () => {
    if (!building.text.trim() || building.busy) return;
    setBuilding((b) => ({ ...b, busy: true }));
    try {
      if (!await flush()) { setBuilding((b) => ({ ...b, busy: false })); return; }
      const res = await api.buildSpace(spaceId, building.text.trim());
      await load(false, true);
      setTimeout(() => flow.fitView({ padding: 0.2, maxZoom: 1, duration: 300 }), 120);
      app.toast(res.note || t("spBuilt", { n: res.added.length }), "ok");
      setBuilding({ open: false, text: "", busy: false });
    } catch (e) {
      app.toast(e instanceof ApiError && e.code === "llm_unavailable" ? t("spNoLlm") : (e as Error).message, "bad");
      setBuilding((b) => ({ ...b, busy: false }));
    }
  };
  const [appMode, setAppMode] = useState(() => { const v = OPEN_AS_APP.has(spaceId); OPEN_AS_APP.delete(spaceId); return v; });

  const onNodesChange = useCallback((changes: NodeChange<SpNode>[]) => {
    liveNodes((ns) => applyNodeChanges(changes, ns));
    if (changes.some((c) => c.type === "remove")) {
      const ids = new Set(latest.current.nodes.map((n) => n.id));
      liveEdges((es) => es.filter((e) => ids.has(e.source) && ids.has(e.target)));
    }
    if (changes.some((c) => c.type === "remove" || (c.type === "position" && c.dragging === false)
      || (c.type === "dimensions" && c.resizing === false))) markDirty();
  }, [markDirty, liveNodes, liveEdges]);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    liveEdges((es) => applyEdgeChanges(changes, es));
    if (changes.some((c) => c.type === "remove")) markDirty();
  }, [markDirty, liveEdges]);

  // Ctrl+D duplicates, Ctrl+Enter runs the selected generators
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el?.closest("input, textarea, select, [contenteditable=true], [role=dialog]")) return;
      if (recoveryRef.current || version.current === null) return;
      if ((e.ctrlKey || e.metaKey) && ["z", "y"].includes(e.key.toLowerCase())) {
        e.preventDefault(); undoRedo(e.shiftKey || e.key.toLowerCase() === "y"); return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); void flush(); return; }
      const sel = latest.current.nodes.filter((n) => n.selected);
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d" && sel.length) { e.preventDefault(); sel.forEach((n) => ctx.duplicate(n.id)); }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        const gens = sel.filter((n) => GENERATORS.includes(n.data.kind)).map((n) => n.id);
        if (gens.length) run("node", gens); else run("all");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // files dropped on the canvas become asset nodes
  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    const at = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    const libId = e.dataTransfer.getData("text/prospero-asset");
    try {
      if (libId) {
        const a = await api.asset(libId);
        if (["image", "video", "audio"].includes(a.kind)) addNode("asset", at, { kind: a.kind, asset_ids: [a.id] });
        return;
      }
      const files = [...e.dataTransfer.files];
      let i = 0;
      for (const f of files) {
        const a = await api.upload(app.projectId!, f);
        if (["image", "video", "audio"].includes(a.kind)) addNode("asset", { x: at.x + i * 30, y: at.y + i * 30 }, { kind: a.kind, asset_ids: [a.id] });
        i++;
      }
    } catch (err) { app.toast((err as Error).message, "bad"); }
  };

  const busyCount = Object.values(state).filter((x) => x.status === "queued" || x.status === "running").length;
  const dark = (document.documentElement.dataset.theme || "dark") === "dark";
  const words = (es: string, en: string) => lang === "es" ? es : en;
  if (!space) return <section className="card sp-load-state">
    <button className="btn" onClick={onBack}><ArrowLeft size={15} />{t("spAll")}</button>
    {loadError ? <div role="alert"><h2>{words("No se pudo abrir el flujo", "Could not open the workflow")}</h2><p>{loadError}</p>
      <button className="btn primary" onClick={() => void load(true)}>{t("recheck")}</button></div>
      : <p role="status"><Loader2 size={18} className="spin" /> {t("loading")}</p>}
  </section>;

  return (
    <SpaceCtx.Provider value={ctx}>
      <div className="space-editor">
        <div className="sp-toolbar">
          <button className="btn sm ghost" onClick={async () => { if (await flush()) onBack(); }}><ArrowLeft size={15} /> {t("spAll")}</button>
          <input className="sp-name" value={name} disabled={!!recovery} onChange={(e) => { latest.current.name = e.target.value; setName(e.target.value); markDirty("name"); }}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} aria-label={t("name")} />
          <span className={`sp-save small ${save}`} role="status">{t(save === "saved" ? "spSaved" : save === "saving" ? "spSaving" : save === "error" ? "spSaveError" : "spUnsaved")}</span>
          <div className="sp-edit-actions" role="group" aria-label={words("Historial de edición", "Edit history")}>
            <button className="btn sm ghost icon" aria-label={words("Deshacer", "Undo")} title={words("Deshacer cambios del lienzo · Ctrl+Z", "Undo canvas edits · Ctrl+Z")} disabled={!historyFlags.undo || !!recovery} onClick={() => undoRedo()}><Undo2 size={16} /></button>
            <button className="btn sm ghost icon" aria-label={words("Rehacer", "Redo")} title={words("Rehacer · Ctrl+Mayús+Z / Ctrl+Y", "Redo · Ctrl+Shift+Z / Ctrl+Y")} disabled={!historyFlags.redo || !!recovery} onClick={() => undoRedo(true)}><Redo2 size={16} /></button>
            <button className="btn sm ghost icon" aria-label={words("Guardar flujo", "Save workflow")} title={words("Guardar flujo · Ctrl+S", "Save workflow · Ctrl+S")} disabled={save === "saved" || save === "saving" || !!recovery || conflict} onClick={() => void flush()}><Save size={16} /></button>
          </div>
          {!appMode && <button ref={addOpener} disabled={!!recovery} className="btn sm sp-add-node" aria-expanded={!!menu} onClick={() => {
            if (menu) { closeMenu(); return; }
            const r = wrap.current?.getBoundingClientRect();
            const at = flow.screenToFlowPosition({ x: (r?.left || 0) + (r?.width || 800) / 2, y: (r?.top || 0) + 140 });
            setMenu({ x: 16, y: 16, fx: at.x, fy: at.y });
          }}><Plus size={16} />{lang === "es" ? "Añadir nodo" : "Add node"}</button>}
          {!appMode && nodes.length > 0 && <select className="sp-focus-node" aria-label={lang === "es" ? "Acercar a un nodo" : "Focus a node"} value="" onChange={(e) => {
            const id = e.target.value;
            if (!id) return;
            setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === id })));
            focusNode(id);
          }}><option value="">{lang === "es" ? "Acercar a…" : "Focus on…"}</option>{nodes.map((n) => <option key={n.id} value={n.id}>{n.data.data.title || t(TYPE_LABEL[n.data.kind])}</option>)}</select>}
          <span className="grow" />
          <div className="sp-build">
            <button disabled={!!recovery || conflict} className={`btn sm ghost${building.open ? " sp-on" : ""}`} title={t("spBuildHint")} onClick={() => setBuilding((b) => ({ ...b, open: !b.open }))}>
              <Wand2 size={14} /> {t("spBuild")}</button>
            {building.open && (
              <div className="sp-build-pop">
                <textarea autoFocus rows={3} value={building.text} placeholder={t("spBuildPh")} disabled={building.busy}
                  onChange={(e) => setBuilding((b) => ({ ...b, text: e.target.value }))}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); build(); } if (e.key === "Escape") setBuilding((b) => ({ ...b, open: false })); }} />
                <div className="row" style={{ gap: 6, justifyContent: "space-between" }}>
                  <span className="small muted">{t("spBuildNote")}</span>
                  <button className="btn sm primary" disabled={building.busy || !building.text.trim()} onClick={build}>
                    {building.busy ? <Loader2 size={13} className="spin" /> : <Wand2 size={13} />} {t("spBuildGo")}</button>
                </div>
              </div>
            )}
          </div>
          <button disabled={!!recovery || conflict} className={`btn sm ghost${appMode ? " sp-on" : ""}`} title={t("spAppHint")} onClick={async () => { if (await flush()) setAppMode((v) => !v); }}>
            {appMode ? <Workflow size={14} /> : <AppWindow size={14} />} {appMode ? t("spCanvas") : t("spApp")}</button>
          {est && est.renders > 0 && (
            <span className="pill sp-est" title={t("spEstHint", { vram: est.vram_mb ? `${Math.round(est.vram_mb / 1024)} GB` : "?" })}>
              <Clock size={11} /> {t("spEst", { min: est.minutes < 1 ? "<1" : String(Math.round(est.minutes)), n: est.renders })}
            </span>
          )}
          {busyCount > 0 && <span className="pill accent"><Loader2 size={11} className="spin" /> {busyCount}</span>}
          {busyCount > 0 && <button className="btn sm danger" title={t("spStopHint")} onClick={async () => {
            try { await api.stopSpace(spaceId); app.toast(t("spStopped"), "info"); setState((await api.space(spaceId)).state); }
            catch (e) { app.toast((e as Error).message, "bad"); }
          }}><Square size={12} /> {t("spStop")}</button>}
          <button disabled={!!recovery || conflict} className="btn sm ghost icon" title={t("spExportHint")} onClick={() => ctx.exportTechnique()}><Download size={15} /></button>
          <button className="btn sm ghost icon" title={t("spFit")} onClick={() => flow.fitView({ padding: 0.2, maxZoom: 1, duration: 300 })}><Maximize size={15} /></button>
          <details className="sp-workflow-more"><summary className="btn sm ghost">{lang === "es" ? "Opciones" : "Options"}</summary>
            <div><button disabled={!!recovery || conflict} className="btn sm" title={t("spRunAllForceHint")} onClick={() => run("all", [], true)}><RotateCcw size={14} /> {t("spRunAllForce")}</button>
              <p className="small muted">{lang === "es" ? "Rehacer también las ramas que no han cambiado." : "Regenerate even branches that have not changed."}</p></div>
          </details>
          <button disabled={!!recovery || conflict || runSubmitting} className="btn sm primary" title={t("spRunAllHint")} onClick={(e) => run("all", [], e.shiftKey)}>{runSubmitting ? <Loader2 size={14} className="spin" /> : <Play size={14} />} {t("spRunAll")}</button>
        </div>
        {recovery && <div className="sp-recovery" role="region" aria-label={words("Borrador recuperable", "Recoverable draft")}>
          <div><strong>{words("Hay cambios sin guardar en este navegador", "This browser has unsaved changes")}</strong>
            <p>{words(`Borrador: ${recovery.name}. Puedes recuperarlo o conservar la versión del servidor que ves debajo.`, `Draft: ${recovery.name}. Recover it, or keep the server version shown below.`)}</p></div>
          <div className="row wrap"><button className="btn primary" onClick={restoreDraft}>{words("Recuperar borrador", "Recover draft")}</button>
            <ConfirmButton className="btn" armedLabel={words("Confirmar descartar borrador", "Confirm discard draft")} onConfirm={() => {
              try { localStorage.removeItem(recoveredKey.current); } catch { setStorageError(true); }
              recoveryRef.current = null; setRecovery(null);
            }}>{words("Descartar borrador", "Discard draft")}</ConfirmButton></div>
        </div>}
        {(saveError || conflict) && !recovery && <div className="sp-recovery" role="alert">
          <div><strong>{conflict ? words("Hay otra versión en el servidor", "The server has another version") : words("El flujo no se ha guardado", "The workflow has not been saved")}</strong>
            <p>{conflict ? words("Tus cambios siguen aquí. Revisa la versión guardada o confirma que quieres sustituirla por la tuya.", "Your edits remain here. Review the saved version, or confirm that you want to replace it with yours.")
              : storageError ? words("Tus cambios siguen en esta pestaña. Reintenta el guardado antes de salir, ejecutar o exportar.", "Your edits remain in this tab. Retry saving before leaving, running or exporting.")
                : words("Conservamos un borrador local. Reintenta el guardado antes de ejecutar o exportar.", "A local draft is retained. Retry saving before running or exporting.")}</p>
            {saveError && <span className="small muted">{saveError}</span>}</div>
          <div className="row wrap">{conflict ? <>
            <button className="btn" onClick={() => void load(false)}>{words("Ver versión guardada", "View saved version")}</button>
            <ConfirmButton className="btn primary" armedLabel={words("Confirmar guardar mi versión", "Confirm save my version")} onConfirm={async () => {
              try {
                const current = await api.space(spaceId); version.current = current.version;
                conflictRef.current = false; setConflict(false); await flush();
              } catch (e) { setSaveError((e as Error).message); }
            }}>{words("Guardar mi versión", "Save my version")}</ConfirmButton>
          </> : <button className="btn primary" disabled={save === "saving"} onClick={() => void flush()}>{words("Reintentar guardado", "Retry save")}</button>}</div>
        </div>}
        {loadError && space && <div className="sp-recovery" role="alert"><p>{loadError}</p><button className="btn" onClick={() => void load(false)}>{t("recheck")}</button></div>}
        {storageError && <p className="sp-storage-error" role="alert">{words("No se pudo conservar una copia local. Mantén esta pestaña abierta hasta guardar.", "Could not retain a local copy. Keep this tab open until saved.")}</p>}
        {appMode && <AppView spaceId={spaceId} beforeRun={flush} onRan={() => { void load(false, true); setPolling(true); }} state={state} />}
        <div className="sp-canvas" inert={!!recovery} ref={wrap} onDragOver={(e) => e.preventDefault()} onDrop={onDrop} style={appMode ? { display: "none" } : undefined}>
          <ReactFlow<SpNode, Edge>
            nodes={nodes} edges={edges} nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={connect} onConnectEnd={onConnectEnd}
            onNodeDoubleClick={(_, n) => focusNode(n.id)}
            isValidConnection={connectOk} onNodeDragStart={onNodeDragStart} onNodeDrag={onNodeDrag}
            onMoveEnd={(_, vp) => {
              if (JSON.stringify(viewport.current) === JSON.stringify(vp)) return;
              viewport.current = vp; if (space) markDirty(undefined, false);
            }}
            onPaneClick={() => setMenu(null)}
            onPaneContextMenu={(e) => {
              e.preventDefault();
              const r = wrap.current?.getBoundingClientRect();
              const f = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
              setMenu({ x: e.clientX - (r?.left || 0), y: e.clientY - (r?.top || 0), fx: f.x, fy: f.y });
            }}
            colorMode={dark ? "dark" : "light"} minZoom={0.15} maxZoom={2} deleteKeyCode={["Delete", "Backspace"]}
            proOptions={{ hideAttribution: true }} defaultEdgeOptions={{ type: "default" }}>
            <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable nodeColor={(n) => PORT_COLOR[(outputPort((n as SpNode).data.kind, (n as SpNode).data.data) || "any") as Port]} />
          </ReactFlow>
          {nodes.length === 0 && space && (
            <div className="sp-empty">
              <Workflow size={30} />
              <h2>{lang === "es" ? "De una idea a un flujo" : "From an idea to a workflow"}</h2>
              <p>{lang === "es" ? "Añade una foto o arrastra archivos aquí. Conecta sus puertos con una imagen o un clip para empezar." : "Add a photo or drop files here. Connect its ports to a picture or a clip to begin."}</p>
              <button className="btn primary" onClick={() => addOpener.current?.click()}><Plus size={16} />{lang === "es" ? "Añadir el primer nodo" : "Add the first node"}</button>
            </div>
          )}
          <div className="sp-canvas-guide">{(["text", "image", "video", "audio"] as Port[]).map((p) => <span key={p}><i style={{ background: PORT_COLOR[p] }} />{t(`spOut_${p}` as MessageKey)}</span>)}
            <span className="sp-wire-help">{lang === "es" ? "Arrastra un puerto a otro nodo o al lienzo vacío" : "Drag a port to a node or onto empty canvas"}</span></div>
          {menu && (
            <div className="sp-menu" style={{ left: Math.max(8, Math.min(menu.x, (wrap.current?.clientWidth || 800) - 320)), top: Math.max(8, Math.min(menu.y, (wrap.current?.clientHeight || 600) - 460)) }}>
              <NodePalette key={`${menu.fx}-${menu.fy}-${menu.from?.node || ""}`} choices={menuChoices} onPick={pickFromMenu} onClose={closeMenu} connected={!!menu.from} />
            </div>
          )}
        </div>
      </div>
      {picking && (
        <AssetPicker projectId={app.projectId!} kind={picking.kind} allProjects title={t("spAddFromLibrary")}
          onClose={() => setPicking(null)}
          onPick={(a) => {
            const n = latest.current.nodes.find((x) => x.id === picking.id);
            const ids: string[] = n?.data.data.asset_ids || [];
            if (!ids.includes(a.id)) ctx.update(picking.id, { asset_ids: [...ids, a.id] });
            setPicking(null);
          }} />
      )}
    </SpaceCtx.Provider>
  );
}

// ------------------------------------------------------------ the list

const TEMPLATES: { id: string; icon: typeof Workflow; title: MessageKey; hint: MessageKey }[] = [
  { id: "blank", icon: Plus, title: "spTplBlank", hint: "spTplBlankHint" },
  { id: "character_outfit_motion", icon: User, title: "spTplIdentity", hint: "spTplIdentityHint" },
  { id: "reference_film", icon: Film, title: "spTplFilm", hint: "spTplFilmHint" },
  { id: "singing_shot", icon: AudioLines, title: "spTplSing", hint: "spTplSingHint" },
  { id: "short_film", icon: Bot, title: "spTplShort", hint: "spTplShortHint" },
];

export function SpacesView() {
  const { t, lang } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const spaceId = app.route.arg;
  const [trash, setTrash] = useState(false);
  const list = useAsync(() => api.spaces(pid, trash), [pid, trash, app.dataVersion]);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");

  if (spaceId) {
    return <ReactFlowProvider><Editor key={spaceId} spaceId={spaceId} onBack={() => app.go("spaces")} /></ReactFlowProvider>;
  }

  const create = async (template: string) => {
    setBusy(true);
    try {
      const tpl = TEMPLATES.find((x) => x.id === template)!;
      const sp = await api.createSpace(pid, template === "blank" ? t("spUntitled") : t(tpl.title), template);
      app.go("spaces", sp.id);
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const items: SpaceSummary[] = (list.data?.items || []).filter((s) => s.name.toLowerCase().includes(query.toLowerCase()));

  return (
    <>
      <div className="page-head">
        <div><h1>{t("spTitle")}</h1><p>{t("spLead")}</p></div>
        <div className="actions">
          {!trash && <label className="btn sm" title={t("spImportHint")}><Upload size={13} /> {t("spImport")}
            <input type="file" accept=".json,application/json" hidden onChange={async (e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (!f) return;
              try {
                const r = await api.importSpace(pid, JSON.parse(await f.text()));
                app.toast(t("spImported", { fill: r.to_fill.length, missing: r.cast_missing.join(", ") || "—" }), "ok");
                app.go("spaces", r.space.id);
              } catch (err) { app.toast((err as Error).message, "bad"); }
            }} /></label>}
          <div className="seg">
            <button className={!trash ? "on" : ""} onClick={() => setTrash(false)}>{t("spMine")}</button>
            <button className={trash ? "on" : ""} onClick={() => setTrash(true)}>{t("spTrash")}</button>
          </div>
        </div>
      </div>
      {!trash && (
        <>
        <section className="sp-start-workflow">
          <div className="sp-start-copy"><h2>{lang === "es" ? "Tu personaje. Otro vestuario. El movimiento que quieras." : "Your character. A new outfit. Your chosen movement."}</h2>
            <p>{lang === "es" ? "Combina tus referencias, revisa cada resultado y conecta la toma siguiente. Cada rama conserva sus versiones." : "Combine your references, review each result and connect the next shot. Every branch keeps its versions."}</p>
            <button className="btn primary" disabled={busy} onClick={() => create("character_outfit_motion")}><Workflow size={17} />{lang === "es" ? "Crear flujo con referencias" : "Create a reference workflow"}</button>
            <button className="btn ghost" disabled={busy} onClick={() => create("blank")}><Plus size={16} />{t("spTplBlank")}</button></div>
          <div className="sp-start-diagram" aria-label={lang === "es" ? "Persona y vestuario producen una imagen; imagen y movimiento producen un clip" : "Person and outfit create a picture; picture and motion create a clip"}>
            <div className="sp-start-sources"><span><User />{lang === "es" ? "Persona" : "Person"}</span><span><Layers2 />{lang === "es" ? "Vestuario" : "Outfit"}</span></div>
            <ChevronRight className="sp-start-link" /><div className="sp-start-middle"><span><ImageIcon />{t("spNodeImage")}</span><span><Film />{lang === "es" ? "Movimiento opcional" : "Optional motion"}</span></div>
            <ChevronRight className="sp-start-link" /><span className="sp-start-result"><Play />{t("spNodeVideo")}</span>
          </div>
        </section>
        <div className="sp-templates">
          {TEMPLATES.filter((tpl) => !["blank", "character_outfit_motion"].includes(tpl.id)).map((tpl) => (
            <button key={tpl.id} className="sp-template" disabled={busy} onClick={() => create(tpl.id)}>
              <tpl.icon size={20} />
              <strong>{t(tpl.title)}</strong>
              <span className="small muted">{t(tpl.hint)}</span>
            </button>
          ))}
        </div>
        </>
      )}
      <div className="sp-list-head"><h2>{trash ? t("spTrash") : lang === "es" ? "Tus flujos" : "Your workflows"}</h2>
        <label className="sp-palette-search"><Search size={15} /><input value={query} onChange={(e) => setQuery(e.target.value)} aria-label={lang === "es" ? "Buscar flujos" : "Search workflows"}
          placeholder={lang === "es" ? "Buscar por nombre" : "Search by name"} /></label></div>
      {items.length === 0 ? (!list.loading && <Empty icon={<LayoutTemplate size={34} />} text={trash ? t("spTrashEmpty") : t("spNone")} />) : (
        <div className="sp-grid">
          {items.map((s) => (
            <div key={s.id} className="sp-card">
              <div className="sp-cover">
                {s.cover ? <Thumb id={s.cover} port="image" onClick={() => app.openAsset(s.cover!)} />
                  : <button className="sp-cover-open" aria-label={`${t("spOpen")} · ${s.name}`} onClick={() => app.go("spaces", s.id)} disabled={trash}><Workflow size={30} /></button>}
              </div>
              <div className="sp-card-meta">
                <strong className="ellipsis">{s.name}{s.app && <span className="pill accent sp-app-badge"><AppWindow size={10} /> {t("spApp")}</span>}</strong>
                <span className="small muted">{t("spNodesN", { n: s.nodes })} · {timeAgo(s.updated_at, lang)}</span>
              </div>
              <div className="row" style={{ gap: 6 }}>
                {trash ? (
                  <button className="btn sm" onClick={async () => { await api.restoreSpace(s.id); list.reload(); }}><RotateCcw size={13} /> {t("spRestore")}</button>
                ) : (
                  <>
                    <button className="btn sm" onClick={() => app.go("spaces", s.id)}>{t("spOpen")}</button>
                    {s.app && <button className="btn sm" title={t("spAppHint")} onClick={() => { OPEN_AS_APP.add(s.id); app.go("spaces", s.id); }}><AppWindow size={13} /> {t("spApp")}</button>}
                    <ConfirmButton armedLabel={t("confirmDelete")} onConfirm={async () => { await api.deleteSpace(s.id); list.reload(); }}><Trash2 size={13} /></ConfirmButton>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
