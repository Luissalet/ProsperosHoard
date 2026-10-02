import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { AudioLines, FileText, Film, Heart, Star, Type, X } from "lucide-react";
import { api, thumbUrl, type Asset, type Job } from "../api";
import { useT, type MessageKey } from "../i18n";

// ------------------------------------------------------------ app context

export type Route = { section: string; projectId: string | null; arg?: string };

export interface AppCtx {
  route: Route;
  go: (section: string, arg?: string) => void;
  projectId: string | null;
  setProject: (id: string | null) => void;
  toast: (text: string, tone?: "ok" | "bad" | "info") => void;
  openAsset: (assetId: string, list?: string[]) => void;
  jobs: Job[];
  refreshJobs: () => void;
  demo: boolean;
  dataVersion: number;
  bump: () => void;
}

export const AppContext = createContext<AppCtx>(null as unknown as AppCtx);
export const useApp = () => useContext(AppContext);

// ------------------------------------------------------------------ hooks

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fn()
      .then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e) => { if (alive) setError(e?.message || String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((x) => x + 1) };
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

// ------------------------------------------------------------- primitives

export function Empty({ icon, text, children }: { icon: ReactNode; text: string; children?: ReactNode }) {
  return (
    <div className="empty">
      {icon}
      <p>{text}</p>
      {children}
    </div>
  );
}

/** Two-step confirmation: first click arms, second click acts (no window.confirm). */
export function ConfirmButton({ onConfirm, children, className = "btn sm danger", armedLabel }: {
  onConfirm: () => void; children: ReactNode; className?: string; armedLabel?: string;
}) {
  const { t } = useT();
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(false), 3500);
    return () => clearTimeout(id);
  }, [armed]);
  return (
    <button
      className={`${className}${armed ? " armed" : ""}`}
      onClick={() => { if (armed) { setArmed(false); onConfirm(); } else setArmed(true); }}
    >
      {armed ? armedLabel || t("confirm") : children}
    </button>
  );
}

export function Stars({ value, onChange }: { value: number; onChange?: (v: number) => void }) {
  return (
    <span className="stars">
      {[1, 2, 3, 4, 5].map((n) => (
        <button key={n} className={n <= value ? "on" : ""} onClick={() => onChange?.(n === value ? 0 : n)} aria-label={`${n}`}>
          <Star size={15} fill={n <= value ? "currentColor" : "none"} />
        </button>
      ))}
    </span>
  );
}

const STATE_KEY: Record<string, MessageKey> = {
  queued: "stateQueued", waiting_gpu: "stateWaiting", running: "stateRunning", done: "stateDone",
  failed: "stateFailed", cancelled: "stateCancelled",
};
const STATE_TONE: Record<string, string> = {
  queued: "info", waiting_gpu: "warn", running: "accent", done: "ok", failed: "bad", cancelled: "",
};

export function JobState({ state }: { state: string }) {
  const { t } = useT();
  return <span className={`pill ${STATE_TONE[state] || ""}`}>{t(STATE_KEY[state] || "stateQueued")}</span>;
}

export function Progress({ value, waiting }: { value: number; waiting?: boolean }) {
  return (
    <div className={`bar${waiting ? " waiting" : ""}`}>
      <span style={{ width: `${Math.max(3, Math.round((waiting ? 1 : value) * 100))}%` }} />
    </div>
  );
}

// open modals, newest last: Escape closes only the one on top
const modalStack: symbol[] = [];

export function Modal({ title, onClose, children, footer, wide }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  const [me] = useState(() => Symbol("modal"));
  useEffect(() => {
    modalStack.push(me);
    return () => { const i = modalStack.indexOf(me); if (i >= 0) modalStack.splice(i, 1); };
  }, [me]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // one Escape closes one modal (the top one is unmounted before the next listener runs)
      if (e.key !== "Escape" || (e as KeyboardEvent & { modalDone?: boolean }).modalDone) return;
      if (modalStack[modalStack.length - 1] !== me) return;
      (e as KeyboardEvent & { modalDone?: boolean }).modalDone = true;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, me]);
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={wide ? { width: "min(980px, 100%)" } : undefined} role="dialog" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="btn ghost icon" style={{ marginLeft: "auto" }} onClick={onClose} aria-label="close"><X size={17} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function KindIcon({ kind, size = 28 }: { kind: string; size?: number }) {
  if (kind === "audio") return <AudioLines size={size} />;
  if (kind === "video") return <Film size={size} />;
  if (kind === "font") return <Type size={size} />;
  return <FileText size={size} />;
}

export function AssetTile({ asset, onClick, selected, focused, square, draggable = true, selecting }: {
  asset: Asset; onClick?: () => void; selected?: boolean; focused?: boolean; square?: boolean; draggable?: boolean; selecting?: boolean;
}) {
  const src = thumbUrl(asset);
  return (
    <button
      className={`tile${selected ? " selected" : ""}${focused ? " focused" : ""}`}
      onClick={onClick}
      draggable={draggable}
      onDragStart={(e) => { e.dataTransfer.setData("text/prospero-asset", asset.id); e.dataTransfer.effectAllowed = "copy"; }}
      title={asset.name || asset.id}
    >
      {src ? (
        <img src={src} alt="" loading="lazy" style={square ? { aspectRatio: "1 / 1", objectFit: "cover" } : undefined}
          onError={(e) => {
            // a missing thumbnail: the picture itself for an image, the kind's icon otherwise
            const img = e.currentTarget;
            if (asset.kind === "image" && !img.dataset.fallback) { img.dataset.fallback = "1"; img.src = `/api/assets/${asset.id}/file`; }
            else img.style.visibility = "hidden";
          }} />
      ) : (
        <div className="media-icon"><KindIcon kind={asset.kind} /></div>
      )}
      <div className="tile-badges">
        {asset.kind !== "image" && <span className="pill badge-dark">{asset.kind}</span>}
        {asset.source === "rendered" && asset.kind === "image" && <span className="pill badge-dark">design</span>}
      </div>
      {selecting && <span className="tile-select">{selected ? "✓" : ""}</span>}
      {asset.favourite && <Heart className="fav" size={16} fill="currentColor" />}
      <div className="tile-meta">
        <span className="ellipsis grow">{asset.name || asset.id}</span>
        {asset.rating > 0 && <span className="nowrap">{"★".repeat(asset.rating)}</span>}
      </div>
    </button>
  );
}

/** A searchable library menu: type to filter, click a tile to see it in the
 * preview (image, video playing, song playing), "Use" or a double click to
 * take it. `allProjects` adds the "all projects" scope. */
export function AssetPicker({ projectId, kind = "image", onPick, onClose, title, allProjects = false }: {
  projectId: string; kind?: string; onPick: (a: Asset) => void; onClose: () => void; title?: string; allProjects?: boolean;
}) {
  const { t } = useT();
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState<"project" | "all">("project");
  const [items, setItems] = useState<(Asset & { project_name?: string })[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [focus, setFocus] = useState<(Asset & { project_name?: string }) | null>(null);
  const dq = useDebounced(query, 220);
  const load = useCallback(async (offset: number) => {
    setLoading(true);
    try {
      const params = { kind, query: dq || undefined, limit: 60, offset };
      const res = scope === "all" ? await api.allAssets(params) : await api.assets(projectId, params);
      // an empty project opens on every project's library instead
      if (!offset && !dq && scope === "project" && allProjects && !res.items.length) { setScope("all"); return; }
      setItems((prev) => (offset ? [...prev, ...res.items] : res.items));
      setNext(res.has_more ? res.next_offset : null);
      if (!offset) setFocus((f) => (f && res.items.some((a) => a.id === f.id) ? f : res.items[0] || null));
    } catch { if (!offset) setItems([]); }
    finally { setLoading(false); }
  }, [projectId, kind, dq, scope, allProjects]);
  useEffect(() => { load(0); }, [load]);
  const move = (step: number) => {
    if (!items.length) return;
    const i = Math.max(0, items.findIndex((a) => a.id === focus?.id));
    setFocus(items[Math.min(items.length - 1, Math.max(0, i + step))]);
  };
  return (
    <Modal title={title || t("pickImage")} onClose={onClose} wide footer={(
      <>
        <span className="hint grow">{t("pickerHint")}</span>
        <button className="btn" onClick={onClose}>{t("cancel")}</button>
        <button className="btn primary" disabled={!focus} onClick={() => focus && onPick(focus)}>{t("pickerUse")}</button>
      </>
    )}>
      <div className="picker2">
        <div className="picker2-list">
          <div className="row" style={{ gap: 6, marginBottom: 10 }}>
            <input className="grow" placeholder={t("search")} value={query} autoFocus
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); move(e.key === "ArrowDown" ? 4 : 1); }
                if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); move(e.key === "ArrowUp" ? -4 : -1); }
                if (e.key === "Enter" && focus) onPick(focus);
              }} />
            {allProjects && (
              <div className="seg">
                <button className={scope === "project" ? "on" : ""} onClick={() => setScope("project")}>{t("pickerThisProject")}</button>
                <button className={scope === "all" ? "on" : ""} onClick={() => setScope("all")}>{t("pickerAllProjects")}</button>
              </div>
            )}
          </div>
          <div className="picker-grid picker2-grid">
            {items.map((a) => (
              <button key={a.id} className={focus?.id === a.id ? "on" : ""} title={a.name || a.id}
                onClick={() => setFocus(a)} onDoubleClick={() => onPick(a)}>
                {thumbUrl(a) ? <img src={thumbUrl(a)} alt="" loading="lazy" /> : <div className="media-icon"><KindIcon kind={a.kind} /></div>}
                <span className="picker2-name ellipsis">{a.name || a.id}</span>
                {scope === "all" && a.project_name && <span className="picker2-proj ellipsis">{a.project_name}</span>}
              </button>
            ))}
          </div>
          {!loading && items.length === 0 && <p className="small muted">{t("pickerEmpty")}</p>}
          {next != null && <button className="btn sm ghost" style={{ marginTop: 8 }} disabled={loading} onClick={() => load(next)}>{t("pickerMore")}</button>}
        </div>
        <div className="picker2-preview">
          {focus ? (
            <>
              {focus.kind === "video" ? <video key={focus.id} src={`/api/assets/${focus.id}/file`} controls autoPlay muted loop playsInline />
                : focus.kind === "audio" ? (
                  <div className="stack" style={{ gap: 8, width: "100%" }}>
                    <div className="media-icon" style={{ height: 120 }}><KindIcon kind="audio" size={40} /></div>
                    <audio key={focus.id} src={`/api/assets/${focus.id}/file`} controls autoPlay style={{ width: "100%" }} />
                  </div>
                ) : <img src={focus.kind === "image" ? `/api/assets/${focus.id}/file` : thumbUrl(focus)} alt="" />}
              <strong className="ellipsis" style={{ maxWidth: "100%" }}>{focus.name || focus.id}</strong>
              <span className="small muted">
                {[focus.project_name, focus.width && focus.height ? `${focus.width}×${focus.height}` : "",
                  focus.duration_s ? `${Math.floor(focus.duration_s / 60)}:${String(Math.round(focus.duration_s % 60)).padStart(2, "0")}` : "",
                  focus.created_at?.slice(0, 10)].filter(Boolean).join(" · ")}
              </span>
              {focus.tags?.length > 0 && <span className="small muted ellipsis">{focus.tags.join(", ")}</span>}
              <button className="btn primary" onClick={() => onPick(focus)}>{t("pickerUse")}</button>
            </>
          ) : <span className="small muted">{t("pickerNothing")}</span>}
        </div>
      </div>
    </Modal>
  );
}

// -------------------------------------------------------------- toasts

export function useToasts() {
  const [toasts, setToasts] = useState<{ id: number; text: string; tone: string }[]>([]);
  const counter = useRef(0);
  const toast = useCallback((text: string, tone: "ok" | "bad" | "info" = "info") => {
    const id = ++counter.current;
    setToasts((ts) => [...ts, { id, text, tone }]);
    setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), tone === "bad" ? 7000 : 3800);
  }, []);
  const host = (
    <div className="toast-host" aria-live="polite">
      {toasts.map((x) => <div key={x.id} className={`toast ${x.tone}`}>{x.text}</div>)}
    </div>
  );
  return { toast, host };
}

export function timeAgo(iso: string | null | undefined, lang: string): string {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
  if (s < 60) return rtf.format(-Math.round(s), "second");
  if (s < 3600) return rtf.format(-Math.round(s / 60), "minute");
  if (s < 86400) return rtf.format(-Math.round(s / 3600), "hour");
  return rtf.format(-Math.round(s / 86400), "day");
}

export const fmtTime = (s: number) => {
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return `${m}:${sec.toFixed(1).padStart(4, "0")}`;
};
