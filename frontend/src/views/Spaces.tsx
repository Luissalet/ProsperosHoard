// Spaces: a node canvas where references, prompts and generators are wired
// together (spaces.py on the server). The graph is what the person draws; the
// state (statuses, outputs, runs) is what runs write and is polled while
// something runs. Saves are versioned: a save over a newer version (an agent
// edited the space meanwhile) reloads instead of overwriting it.
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Background, BackgroundVariant, Controls, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange, type NodeProps, type OnConnectEnd,
  applyEdgeChanges, applyNodeChanges, useUpdateNodeInternals,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  Aperture, ArrowLeft, AudioLines, Check, Copy, FileText, Film, Image as ImageIcon, LayoutTemplate, ListChecks, Loader2, Maximize,
  Music, Play, Plus, RotateCcw, Sparkles, StickyNote, Trash2, Type, User, Workflow, X, FastForward, Bot, ScanLine, Layers, Square, Captions,
} from "lucide-react";
import {
  ApiError, api, type Character, type Space, type SpaceEdge, type SpaceGraph, type SpaceNodeState, type SpaceNodeType, type SpaceSummary,
} from "../api";
import { useT, type MessageKey } from "../i18n";
import { AssetPicker, ConfirmButton, Empty, timeAgo, useApp, useAsync } from "../components/ui";
import { useCinemaGuide, useSlashMenu } from "../components/Slash";

// ------------------------------------------------------------ the node model

type Port = "text" | "image" | "video" | "audio" | "any";
const INPUTS: Partial<Record<SpaceNodeType, [string, Port, boolean][]>> = {
  // [handle, accepts, many wires]
  image: [["prompt", "text", true], ["refs", "image", true]],
  video: [["start", "image", true], ["prompt", "text", true], ["motion", "video", false], ["audio", "audio", false]],
  music: [["prompt", "text", true]],
  list: [["items", "any", true]],
  assistant: [["prompt", "text", true]],
  edit: [["image", "image", true]],
  combine: [["clips", "video", true], ["audio", "audio", false]],
};
const GENERATORS: SpaceNodeType[] = ["image", "video", "music", "assistant", "edit", "combine"];
const PORT_COLOR: Record<Port, string> = { text: "#7fa6d9", image: "#b48cf0", video: "#5bbf86", audio: "#f0a04b", any: "#9a95a6" };
const TYPE_ICON: Record<SpaceNodeType, typeof Type> = {
  text: Type, asset: ImageIcon, cast: User, image: ImageIcon, video: Film, music: Music, list: ListChecks, note: StickyNote,
  assistant: Bot, edit: ScanLine, combine: Layers,
};
const TYPE_LABEL: Record<SpaceNodeType, MessageKey> = {
  text: "spNodeText", asset: "spNodeAsset", cast: "spNodeCast", image: "spNodeImage", video: "spNodeVideo",
  music: "spNodeMusic", list: "spNodeList", note: "spNodeNote", assistant: "spNodeAssistant", edit: "spNodeEdit", combine: "spNodeCombine",
};
const ADDABLE: SpaceNodeType[] = ["text", "asset", "cast", "image", "video", "music", "assistant", "edit", "combine", "list", "note"];
const DEFAULT_DATA: Record<SpaceNodeType, Record<string, unknown>> = {
  text: { text: "" }, asset: { kind: "image", asset_ids: [] }, cast: {}, image: { prompt: "", aspect: "1:1", count: 2 },
  video: { prompt: "", quality: "draft" }, music: { tags: "", lyrics: "[Instrumental]", duration: 30, count: 1 },
  list: { unticked: [] }, note: { text: "" },
  assistant: { prompt: "", as_list: true, items: 5 }, edit: { operation: "upscale", scale: 2 }, combine: { audio_start_s: 0 },
};
const WIDTH: Record<SpaceNodeType, number> = {
  text: 260, asset: 260, cast: 230, image: 300, video: 300, music: 290, list: 260, note: 220, assistant: 290, edit: 250, combine: 280,
};

type NodeData = { kind: SpaceNodeType; data: Record<string, any> };
type SpNode = Node<NodeData, "sp">;

function outputPort(kind: SpaceNodeType, data: Record<string, any>, handle?: string | null): Port | null {
  if (kind === "text") return "text";
  if (kind === "cast") return handle === "text" ? "text" : "image";
  if (kind === "video") return handle === "last" ? "image" : "video";
  if (kind === "image" || kind === "edit") return "image";
  if (kind === "assistant") return "text";
  if (kind === "combine") return "video";
  if (kind === "music") return "audio";
  if (kind === "asset") return (data.kind as Port) || "image";
  if (kind === "list") return "any";
  return null;
}

function toRf(graph: SpaceGraph): { nodes: SpNode[]; edges: Edge[] } {
  const nodes: SpNode[] = graph.nodes.map((n) => ({
    id: n.id, type: "sp", position: { x: n.x, y: n.y }, data: { kind: n.type, data: n.data || {} },
    style: { width: n.w || WIDTH[n.type] },
  }));
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
      ...(typeof n.style?.width === "number" && n.style.width !== WIDTH[n.data.kind] ? { w: n.style.width } : {}),
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
  run: (id: string, mode: "node" | "downstream") => void;
  remove: (id: string) => void;
  duplicate: (id: string) => void;
  setExcluded: (id: string, excluded: string[]) => void;
  pickRun: (id: string, outputs: string[]) => void;
  pickAssets: (id: string, kind: string) => void;
  open: (assetId: string, list: string[]) => void;
}
const SpaceCtx = createContext<Ctx>(null as unknown as Ctx);

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
    <button className={`sp-thumb${dim ? " dim" : ""}`} onClick={onClick} title={id}>
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

function Enhance({ text, kind, onDone }: { text: string; kind: "image" | "video" | "music"; onDone: (s: string) => void }) {
  const { t } = useT();
  const app = useApp();
  const [busy, setBusy] = useState(false);
  return (
    <button className="btn xs ghost nodrag" disabled={!text.trim() || busy} title={t("spEnhanceHint")}
      onClick={async () => {
        setBusy(true);
        try { onDone((await api.enhancePrompt(text, kind, app.projectId || undefined)).text); } catch (e) {
          app.toast(e instanceof ApiError && e.code === "llm_unavailable" ? t("spNoLlm") : (e as Error).message, "bad");
        }
        finally { setBusy(false); }
      }}>
      {busy ? <Loader2 size={12} className="spin" /> : <Sparkles size={12} />} {t("spEnhance")}
    </button>
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
  const { t } = useT();
  const ctx = useContext(SpaceCtx);
  const { kind, data } = nd;
  const st = ctx.state[id];
  const Icon = TYPE_ICON[kind];
  const set = (patch: Record<string, unknown>) => ctx.update(id, patch);
  const wired = new Set(ctx.edges.filter((e) => e.target === id).map((e) => e.targetHandle));
  const runnable = GENERATORS.includes(kind);
  const busy = st?.status === "queued" || st?.status === "running";
  const out = outputPort(kind, data);

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
        <div className="sp-thumbs">
          {ids.map((a) => (
            <div key={a} className="sp-thumb-wrap">
              <Thumb id={a} port={(data.kind || "image") as Port} onClick={() => ctx.open(a, ids)} />
              <button className="sp-thumb-x nodrag" title={t("remove")} onClick={() => set({ asset_ids: ids.filter((x) => x !== a) })}><X size={11} /></button>
            </div>
          ))}
          <button className="sp-thumb sp-add nodrag" onClick={() => ctx.pickAssets(id, data.kind || "image")} title={t("spAddFromLibrary")}><Plus size={18} /></button>
        </div>
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
        {c?.canonical_asset_id ? <div className="sp-cast-img" onClick={() => ctx.open(c.canonical_asset_id!, [c.canonical_asset_id!])}>
          <Thumb id={c.canonical_asset_id} port="image" /></div>
          : c ? <p className="small muted">{t("spCastNoRef")}</p> : null}
      </>
    );
  } else if (kind === "image") {
    body = (
      <>
        <PromptBox value={data.prompt || ""} onChange={(v) => set({ prompt: v })} kind="image" placeholder={t("spImagePh")} />
        <div className="sp-opts nodrag">
          <select value={data.aspect || "1:1"} onChange={(e) => set({ aspect: e.target.value })} title={t("spAspect")}>
            {["1:1", "16:9", "9:16", "4:3", "3:4", "21:9"].map((a) => <option key={a}>{a}</option>)}
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
          {!wired.has("audio") && !wired.has("motion") && (
            <div className="seg">
              <button className={data.quality === "draft" ? "on" : ""} onClick={() => set({ quality: "draft" })} title={t("spDraftHint")}>{t("spDraft")}</button>
              <button className={data.quality !== "draft" ? "on" : ""} onClick={() => set({ quality: "final" })} title={t("spFinalHint")}>{t("spFinal")}</button>
            </div>
          )}
          {wired.has("audio") && <>
            <LinePicker nodeId={id} onPick={(start, secs) => set({ audio_start_s: start, seconds: secs })} />
            <label className="sp-num" title={t("spAudioStartHint")}>{t("spFrom")}<input type="number" min={0} step={0.1} value={data.audio_start_s ?? 0}
              onChange={(e) => set({ audio_start_s: Number(e.target.value) })} />s</label>
            <label className="sp-num" title={t("spSecondsHint")}>{t("spLength")}<input type="number" min={1} max={19} step={0.1} value={data.seconds ?? 4.8}
              onChange={(e) => set({ seconds: Number(e.target.value) })} />s</label>
          </>}
          {wired.has("motion") && !wired.has("audio") && (
            <label className="sp-num" title={t("spMotionStartHint")}>{t("spFrom")}<input type="number" min={0} step={0.1} value={data.motion_start_s ?? 0}
              onChange={(e) => set({ motion_start_s: Number(e.target.value) })} />s</label>
          )}
        </div>
        <div className="small muted sp-mode">{wired.has("audio") ? t("spModeSing") : wired.has("motion") ? t("spModeMotion") : t("spModeAnimate")}</div>
        <CameraPicker value={data.camera} onChange={(c) => set({ camera: c })} clips />
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
    const v = data.operation === "remove_background" ? "bg" : String(data.scale || 2) === "4" ? "x4" : "x2";
    body = (
      <select className="nodrag" value={v} onChange={(e) => set(e.target.value === "bg" ? { operation: "remove_background" }
        : { operation: "upscale", scale: e.target.value === "x4" ? 4 : 2 })}>
        <option value="x2">{t("spUpscale2")}</option>
        <option value="x4">{t("spUpscale4")}</option>
        <option value="bg">{t("spRemoveBg")}</option>
      </select>
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
        {list.length > 0 && <div className="sp-thumbs">
          {list.map((a) => (
            <div key={a} className="sp-thumb-wrap">
              <Thumb id={a} port={port} dim={ex.has(a)} onClick={() => ctx.open(a, list)} />
              <button className={`sp-tick nodrag${ex.has(a) ? "" : " on"}`} title={t("spTick")}
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

  const inputs = INPUTS[kind] || [];
  const outs: [string, Port][] = kind === "cast" ? [["image", "image"], ["text", "text"]]
    : kind === "video" ? [["out", "video"], ["last", "image"]] : out ? [["out", out]] : [];
  return (
    <div className={`sp-node sp-${kind}${selected ? " selected" : ""}${busy ? " busy" : ""}`}>
      <div className="sp-head">
        <Icon size={14} />
        <span className="sp-title">{t(TYPE_LABEL[kind])}</span>
        <span className="sp-id">{id}</span>
        <StatusChip st={st} />
        <span className="grow" />
        {runnable && <>
          <button className="btn xs icon ghost nodrag" title={t("spRunNode")} disabled={busy} onClick={() => ctx.run(id, "node")}><Play size={13} /></button>
          <button className="btn xs icon ghost nodrag" title={t("spRunDown")} disabled={busy} onClick={() => ctx.run(id, "downstream")}><FastForward size={13} /></button>
        </>}
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
      <div className="sp-body">{body}{outputs}</div>
    </div>
  );
});

const NODE_TYPES = { sp: SpaceNodeView };

// ------------------------------------------------------------ the editor

type SaveState = "saved" | "dirty" | "saving" | "error";

function Editor({ spaceId, onBack }: { spaceId: string; onBack: () => void }) {
  const { t } = useT();
  const app = useApp();
  // the app context is a new object on every app render (jobs poll): read it through refs in timers
  const appRef = useRef(app);
  appRef.current = app;
  const tRef = useRef(t);
  tRef.current = t;
  const flow = useReactFlow();
  const updateInternals = useUpdateNodeInternals();
  const [space, setSpace] = useState<Space | null>(null);
  const [nodes, setNodes] = useState<SpNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [state, setState] = useState<Record<string, SpaceNodeState>>({});
  const [name, setName] = useState("");
  const [save, setSave] = useState<SaveState>("saved");
  const [dirty, setDirty] = useState(0);
  const [picking, setPicking] = useState<{ id: string; kind: string } | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; fx: number; fy: number; from?: { node: string; handle: string; type: "source" | "target" } } | null>(null);
  const [polling, setPolling] = useState(false);
  const version = useRef<number | null>(null);
  const viewport = useRef<{ x: number; y: number; zoom: number } | undefined>(undefined);
  const saving = useRef<Promise<void>>(Promise.resolve());
  const latest = useRef({ nodes, edges, name });
  latest.current = { nodes, edges, name };
  const wrap = useRef<HTMLDivElement>(null);
  const characters = useAsync(() => api.characters(app.projectId!), [app.projectId]);
  const chars = useMemo(() => (characters.data?.items || []), [characters.data]);

  const load = useCallback(async (fit = false) => {
    const s = await api.space(spaceId);
    setSpace(s);
    setName(s.name);
    version.current = s.version;
    const rf = toRf(s.graph);
    setNodes(rf.nodes);
    setEdges(rf.edges);
    setState(s.state);
    viewport.current = s.graph.viewport;
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
  }, [spaceId, flow, updateInternals]);

  useEffect(() => { load(true).catch((e) => appRef.current.toast((e as Error).message, "bad")); }, [load]);

  const markDirty = useCallback(() => { setSave("dirty"); setDirty((d) => d + 1); }, []);

  // debounced, versioned saves, one at a time
  const flush = useCallback(() => {
    saving.current = saving.current.then(async () => {
      const { nodes: n, edges: e, name: nm } = latest.current;
      setSave("saving");
      try {
        const res = await api.saveSpace(spaceId, toGraph(n, e, viewport.current), version.current, nm.trim() || undefined);
        version.current = res.version;
        setSave("saved");
      } catch (err) {
        if (err instanceof ApiError && err.status === 409) {
          appRef.current.toast(tRef.current("spStale"), "info");
          await load(false);
          setSave("saved");
        } else {
          setSave("error");
          appRef.current.toast((err as Error).message, "bad");
        }
      }
    });
    return saving.current;
  }, [spaceId, load]);
  useEffect(() => {
    if (!dirty) return;
    const id = setTimeout(flush, 700);
    return () => clearTimeout(id);
  }, [dirty, flush]);

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

  const run = useCallback(async (mode: "node" | "downstream" | "all", ids: string[] = [], force = false) => {
    await flush();
    try {
      const res = await api.runSpace(spaceId, mode, ids, force);
      setState((s) => ({ ...s, ...Object.fromEntries(res.nodes.map((n) => [n, { ...(s[n] || {}), status: "queued" as const, error: null }])) }));
      setPolling(true);
      appRef.current.refreshJobs();
    } catch (e) {
      appRef.current.toast((e as Error).message, "bad");
    }
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
    const node: SpNode = { id, type: "sp", position: pos, data: { kind, data: { ...DEFAULT_DATA[kind], ...(data || {}) } }, style: { width: WIDTH[kind] } };
    setNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), { ...node, selected: true }]);
    markDirty();
    return id;
  }, [flow, markDirty]);

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

  const connect = useCallback((c: Connection) => {
    if (!connectOk(c)) return;
    const src = latest.current.nodes.find((n) => n.id === c.source)!;
    const sh = c.sourceHandle && c.sourceHandle !== "out" ? c.sourceHandle : null;
    const e = edgeOf({ id: `e-${c.source}-${sh || "o"}-${c.target}-${c.targetHandle}`.slice(0, 120), source: c.source, source_handle: sh,
      target: c.target, target_handle: c.targetHandle || "" }, src.data.kind, src.data.data);
    setEdges((es) => [...es, e]);
    markDirty();
  }, [connectOk, markDirty]);

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
      if (from.type === "source") connect({ source: from.node, sourceHandle: from.handle, target: id, targetHandle: choice.handle || null });
      else connect({ source: id, sourceHandle: choice.kind === "cast" ? (from.handle === "prompt" ? "text" : "image") : "out", target: from.node, targetHandle: from.handle });
    }, 0);
  };

  const ctx: Ctx = {
    projectId: app.projectId!, state, characters: chars, edges, nodes,
    update: (id, patch) => {
      setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, data: { ...n.data.data, ...patch } } } : n)));
      // an asset node switching kind re-colours (and may invalidate) its wires
      if ("kind" in patch) setEdges((es) => es.filter((e) => e.source !== id));
      markDirty();
    },
    run: (id, mode) => run(mode, [id]),
    remove: (id) => {
      setNodes((ns) => ns.filter((n) => n.id !== id));
      setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
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
  };

  const onNodesChange = useCallback((changes: NodeChange<SpNode>[]) => {
    setNodes((ns) => applyNodeChanges(changes, ns));
    if (changes.some((c) => c.type === "remove" || (c.type === "position" && c.dragging === false))) markDirty();
  }, [markDirty]);
  const onEdgesChange = useCallback((changes: EdgeChange[]) => {
    setEdges((es) => applyEdgeChanges(changes, es));
    if (changes.some((c) => c.type === "remove")) markDirty();
  }, [markDirty]);

  // Ctrl+D duplicates, Ctrl+Enter runs the selected generators
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT")) return;
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

  return (
    <SpaceCtx.Provider value={ctx}>
      <div className="space-editor">
        <div className="sp-toolbar">
          <button className="btn sm ghost" onClick={async () => { await flush(); onBack(); }}><ArrowLeft size={15} /> {t("spAll")}</button>
          <input className="sp-name" value={name} onChange={(e) => setName(e.target.value)} onBlur={() => { if (space && name.trim() && name !== space.name) markDirty(); }}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} aria-label={t("name")} />
          <span className={`sp-save small ${save}`}>{t(save === "saved" ? "spSaved" : save === "saving" ? "spSaving" : save === "error" ? "spSaveError" : "spUnsaved")}</span>
          <div className="sp-add-bar">
            {ADDABLE.map((k) => {
              const I = TYPE_ICON[k];
              return <button key={k} className="btn sm ghost" title={t(TYPE_LABEL[k])} onClick={() => addNode(k)}><I size={14} /><span className="sp-add-label">{t(TYPE_LABEL[k])}</span></button>;
            })}
          </div>
          <span className="grow" />
          {busyCount > 0 && <span className="pill accent"><Loader2 size={11} className="spin" /> {busyCount}</span>}
          {busyCount > 0 && <button className="btn sm danger" title={t("spStopHint")} onClick={async () => {
            try { await api.stopSpace(spaceId); app.toast(t("spStopped"), "info"); setState((await api.space(spaceId)).state); }
            catch (e) { app.toast((e as Error).message, "bad"); }
          }}><Square size={12} /> {t("spStop")}</button>}
          <button className="btn sm ghost icon" title={t("spFit")} onClick={() => flow.fitView({ padding: 0.2, maxZoom: 1, duration: 300 })}><Maximize size={15} /></button>
          <button className="btn sm" title={t("spRunAllForceHint")} onClick={() => run("all", [], true)}><RotateCcw size={14} /> {t("spRunAllForce")}</button>
          <button className="btn sm primary" title={t("spRunAllHint")} onClick={(e) => run("all", [], e.shiftKey)}><Play size={14} /> {t("spRunAll")}</button>
        </div>
        <div className="sp-canvas" ref={wrap} onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
          <ReactFlow<SpNode, Edge>
            nodes={nodes} edges={edges} nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={connect} onConnectEnd={onConnectEnd}
            isValidConnection={connectOk}
            onMoveEnd={(_, vp) => { viewport.current = vp; }}
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
              <p>{t("spEmptyCanvas")}</p>
            </div>
          )}
          {menu && (
            <div className="sp-menu" style={{ left: menu.x, top: menu.y }} onMouseLeave={() => setMenu(null)}>
              <div className="sp-menu-head small muted">{menu.from ? t("spMenuWire") : t("spMenuAdd")}</div>
              {menuChoices.length === 0 && <div className="small muted" style={{ padding: 8 }}>{t("spMenuNone")}</div>}
              {menuChoices.map((c) => {
                const I = TYPE_ICON[c.kind];
                return <button key={`${c.kind}-${c.handle || ""}`} onClick={() => pickFromMenu(c)}><I size={14} /> {t(TYPE_LABEL[c.kind])}
                  {c.handle && <span className="muted small"> · {t(`spIn_${c.handle}` as MessageKey)}</span>}</button>;
              })}
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
  const items: SpaceSummary[] = list.data?.items || [];

  return (
    <>
      <div className="page-head">
        <div><h1>{t("spTitle")}</h1><p>{t("spLead")}</p></div>
        <div className="actions">
          <div className="seg">
            <button className={!trash ? "on" : ""} onClick={() => setTrash(false)}>{t("spMine")}</button>
            <button className={trash ? "on" : ""} onClick={() => setTrash(true)}>{t("spTrash")}</button>
          </div>
        </div>
      </div>
      {!trash && (
        <div className="sp-templates">
          {TEMPLATES.map((tpl) => (
            <button key={tpl.id} className="sp-template" disabled={busy} onClick={() => create(tpl.id)}>
              <tpl.icon size={20} />
              <strong>{t(tpl.title)}</strong>
              <span className="small muted">{t(tpl.hint)}</span>
            </button>
          ))}
        </div>
      )}
      {items.length === 0 ? (!list.loading && <Empty icon={<LayoutTemplate size={34} />} text={trash ? t("spTrashEmpty") : t("spNone")} />) : (
        <div className="sp-grid">
          {items.map((s) => (
            <div key={s.id} className="sp-card">
              <button className="sp-cover" onClick={() => !trash && app.go("spaces", s.id)} disabled={trash}>
                {s.cover ? <Thumb id={s.cover} port="image" /> : <Workflow size={30} />}
              </button>
              <div className="sp-card-meta">
                <strong className="ellipsis">{s.name}</strong>
                <span className="small muted">{t("spNodesN", { n: s.nodes })} · {timeAgo(s.updated_at, lang)}</span>
              </div>
              <div className="row" style={{ gap: 6 }}>
                {trash ? (
                  <button className="btn sm" onClick={async () => { await api.restoreSpace(s.id); list.reload(); }}><RotateCcw size={13} /> {t("spRestore")}</button>
                ) : (
                  <>
                    <button className="btn sm" onClick={() => app.go("spaces", s.id)}>{t("spOpen")}</button>
                    <ConfirmButton armedLabel={t("confirmDelete")} onConfirm={async () => { await api.deleteSpace(s.id); list.reload(); }}><Trash2 size={13} /></ConfirmButton>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
      <p className="small muted" style={{ marginTop: 18 }}><FileText size={12} /> {t("spAgentHint")}</p>
    </>
  );
}

