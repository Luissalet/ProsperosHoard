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

export function Modal({ title, onClose, children, footer, wide }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
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
