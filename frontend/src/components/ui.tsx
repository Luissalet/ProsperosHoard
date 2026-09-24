import { Component, createContext, useCallback, useContext, useEffect, useRef, useState, type ErrorInfo, type ReactNode, type RefObject } from "react";
import { AudioLines, FileText, Film, Heart, RefreshCw, Star, Type, X } from "lucide-react";
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
  /** Keep these jobs in `jobs` even once they fall out of the recent/active window. */
  trackJobs: (ids: (string | null | undefined)[]) => void;
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
  // a stable identity, so effects and intervals that depend on it are not reset every render
  const reload = useCallback(() => setTick((x) => x + 1), []);
  return { data, error, loading, reload };
}

/** A job by id from the app-wide poll; the id is tracked so it stays there. */
export function useTrackedJob(jobId: string | null | undefined): Job | null {
  const app = useApp();
  const { trackJobs, refreshJobs } = app;
  useEffect(() => {
    if (!jobId) return;
    trackJobs([jobId]);
    refreshJobs();
  }, [jobId, trackJobs, refreshJobs]);
  return (jobId && app.jobs.find((j) => j.id === jobId)) || null;
}

function readSession(key: string): string | null {
  try { return sessionStorage.getItem(key); } catch { return null; }
}
function writeSession(key: string, value: string | null) {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch { /* storage disabled */ }
}

/** useState mirrored to sessionStorage (survives tab switches and navigation; storage errors are ignored). */
export function useSessionState(key: string, initial: string | null = null): [string | null, (v: string | null) => void] {
  const [value, setValue] = useState<string | null>(() => readSession(key) ?? initial);
  const set = useCallback((v: string | null) => { setValue(v); writeSession(key, v); }, [key]);
  return [value, set];
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), video[controls], audio[controls], [tabindex]:not([tabindex="-1"])';

// open dialogs, innermost last: only the top one traps Tab and answers Escape
const dialogStack: RefObject<HTMLElement | null>[] = [];
export const isTopDialog = (el: HTMLElement | null) => !dialogStack.length || dialogStack[dialogStack.length - 1].current === el;

/** Dialog focus handling: move focus in on open, keep Tab inside, give it back on close. */
export function useDialogFocus(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialogStack.push(ref);
    const root = ref.current;
    if (root && !root.contains(document.activeElement)) root.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      const el = ref.current;
      if (e.key !== "Tab" || !el || dialogStack[dialogStack.length - 1] !== ref) return;
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((x) => x.offsetParent !== null || x === document.activeElement);
      if (!items.length) { e.preventDefault(); el.focus(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !el.contains(active) || active === el)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (active === last || !el.contains(active))) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const at = dialogStack.indexOf(ref);
      if (at >= 0) dialogStack.splice(at, 1);
      if (previous && document.contains(previous)) previous.focus({ preventScroll: true });
    };
    // mount/unmount only: the dialog keeps its focus while its content changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
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

/** A failed load with a way to try again (instead of "Loading..." forever). */
export function ErrorNote({ error, onRetry }: { error: string; onRetry?: () => void }) {
  const { t } = useT();
  return (
    <div className="row wrap" role="alert">
      <span className="err-text">{t("loadError", { error })}</span>
      {onRetry && <button className="btn sm" onClick={onRetry}><RefreshCw size={13} /> {t("retry")}</button>}
    </div>
  );
}

/** Loading / error / content for one useAsync result. */
export function Loadable<T>({ state, children }: {
  state: { data: T | null; error: string | null; reload: () => void }; children: (data: T) => ReactNode;
}) {
  const { t } = useT();
  if (state.data !== null) return <>{children(state.data)}</>;
  if (state.error) return <ErrorNote error={state.error} onRetry={state.reload} />;
  return <p className="muted">{t("loading")}</p>;
}

function CrashNote({ error, onReset }: { error: Error; onReset: () => void }) {
  const { t } = useT();
  return (
    <div className="card stack" role="alert">
      <h2>{t("viewCrashed")}</h2>
      <p className="err-text mono">{error.message}</p>
      <div className="row">
        <button className="btn primary sm" onClick={onReset}><RefreshCw size={13} /> {t("reloadView")}</button>
      </div>
    </div>
  );
}

/** Keeps one broken view from blanking the whole app. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error(error, info.componentStack); }
  render() {
    if (this.state.error) return <CrashNote error={this.state.error} onReset={() => this.setState({ error: null })} />;
    return this.props.children;
  }
}

/** Two-step confirmation: first click arms, second click acts (no window.confirm). */
export function ConfirmButton({ onConfirm, children, className = "btn sm danger", armedLabel, label }: {
  onConfirm: () => void; children: ReactNode; className?: string; armedLabel?: string;
  /** accessible name, for icon-only buttons */
  label?: string;
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
      aria-label={armed ? undefined : label}
      title={label}
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

export function Modal({ title, onClose, children, footer, wide }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  const { t } = useT();
  const ref = useRef<HTMLDivElement>(null);
  useDialogFocus(ref);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && isTopDialog(ref.current)) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} tabIndex={-1} className="modal" style={wide ? { width: "min(980px, 100%)" } : undefined} role="dialog" aria-modal="true" aria-label={title}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="btn ghost icon" style={{ marginLeft: "auto" }} onClick={onClose} aria-label={t("close")} title={t("close")}><X size={17} /></button>
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

export function AssetTile({ asset, onClick, selected, focused, square, draggable = true }: {
  asset: Asset; onClick?: () => void; selected?: boolean; focused?: boolean; square?: boolean; draggable?: boolean;
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
        <img src={src} alt={asset.name || ""} loading="lazy" style={square ? { aspectRatio: "1 / 1", objectFit: "cover" } : undefined} />
      ) : (
        <div className="media-icon"><KindIcon kind={asset.kind} /></div>
      )}
      <div className="tile-badges">
        {asset.kind !== "image" && <span className="pill badge-dark">{asset.kind}</span>}
        {asset.source === "rendered" && asset.kind === "image" && <span className="pill badge-dark">design</span>}
      </div>
      {asset.favourite && <Heart className="fav" size={16} fill="currentColor" />}
      <div className="tile-meta">
        <span className="ellipsis grow">{asset.name || asset.id}</span>
        {asset.rating > 0 && <span className="nowrap">{"★".repeat(asset.rating)}</span>}
      </div>
    </button>
  );
}

/** Modal grid to pick one image asset of the current project. */
export function AssetPicker({ projectId, kind = "image", onPick, onClose, title }: {
  projectId: string; kind?: string; onPick: (a: Asset) => void; onClose: () => void; title?: string;
}) {
  const { t } = useT();
  const [query, setQuery] = useState("");
  const dq = useDebounced(query, 250);
  const { data } = useAsync(() => api.assets(projectId, { kind, query: dq, limit: 60 }), [projectId, kind, dq]);
  return (
    <Modal title={title || t("pickImage")} onClose={onClose} wide>
      <input style={{ width: "100%", marginBottom: 12 }} placeholder={t("search")} value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
      <div className="picker-grid">
        {(data?.items || []).map((a) => (
          <button key={a.id} onClick={() => onPick(a)} title={a.name || a.id}>
            {thumbUrl(a) ? <img src={thumbUrl(a)} alt="" loading="lazy" /> : <div className="media-icon"><KindIcon kind={a.kind} /></div>}
          </button>
        ))}
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
