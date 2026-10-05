import { useEffect, useRef, useState } from "react";
import { Cpu, MemoryStick, Power, RefreshCw, Trash2 } from "lucide-react";
import { api, GpuMemory } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, useApp } from "./ui";

const gb = (mb?: number | null) => (mb == null ? "?" : (mb / 1024).toFixed(1));
const lvl = (p: number) => (p >= 92 ? "bad" : p >= 75 ? "warn" : "ok");

// The machine at a glance in the header: GPU use, one tank per card (as wide
// as the card is big), RAM and CPU; click for every card, what each server
// keeps loaded there and the actions to change it.
export function Vitals() {
  const { t } = useT();
  const app = useApp();
  const [d, setD] = useState<GpuMemory | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  const refresh = async () => { try { setD(await api.memory()); } catch { /* shown as n/a */ } };
  useEffect(() => {
    refresh();
    const h = setInterval(() => { if (!document.hidden) refresh(); }, open ? 4000 : 10000);
    return () => clearInterval(h);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);

  const act = async (id: string, fn: () => Promise<{ ok?: boolean; error?: string; message?: string }>, done: string) => {
    setBusy(id);
    try {
      const r = await fn();
      if (r.ok === false) app.toast(r.error || "failed", "bad"); else app.toast(r.message || done, "ok");
    } catch (e) { app.toast((e as Error).message, "bad"); } finally { setBusy(null); refresh(); }
  };

  const gpus = d?.gpus || [];
  const used = gpus.reduce((a, g) => a + g.used_mb, 0);
  const total = gpus.reduce((a, g) => a + g.total_mb, 0);
  const utils = gpus.map((g) => g.util).filter((u): u is number => u != null);
  const util = utils.length ? Math.round(utils.reduce((a, b) => a + b, 0) / utils.length) : null;
  const ram = d?.host?.ram;
  const ramPct = ram ? Math.round((ram.used_mb / Math.max(1, ram.total_mb)) * 100) : null;
  const temps = gpus.map((g) => g.temp).filter((x): x is number => x != null);
  const hot = temps.length ? Math.max(...temps) : null;

  return (
    <div className="vitals" ref={box}>
      <button className="vitals-pill" onClick={() => setOpen(!open)} title={t("vitalsTitle")} aria-expanded={open}>
        {!d || (!gpus.length && !ram && d.host?.cpu_pct == null) ? <span className="muted small">{t("vitalsNa")}</span> : (
          <>
            {gpus.length > 0 && (
              <>
                <span className="vitals-gpus">
                  {gpus.map((g) => {
                    const p = Math.min(100, Math.max(0, (g.used_mb / Math.max(1, g.total_mb)) * 100));
                    const label = `GPU ${g.index} · VRAM ${gb(g.used_mb)}/${gb(g.total_mb)} GB (${Math.round(p)}%)`;
                    return <span key={g.index} className="vitals-gpu" title={`${g.name} · ${label}`} aria-label={label}>
                      <span className="vitals-gpu-label mono">G{g.index} <strong>{Math.round(p)}%</strong></span>
                      <span className={`tank ${lvl(p)}`} aria-hidden="true"><span style={{ width: `${p}%` }} /></span>
                    </span>;
                  })}
                </span>
                <span className="mono small vitals-total">{gb(used)}<span className="muted">/{Math.round(total / 1024)} GB</span></span>
                {util != null && <span className="mono small vitals-compute">GPU {util}%</span>}
                {hot != null && <span className={`mono small vitals-host ${hot >= 85 ? "err-text" : hot >= 75 ? "warn-text" : "muted"}`}>{Math.round(hot)}°</span>}
              </>
            )}
            {ramPct != null && <span className="mono small vitals-host">RAM <span className={ramPct >= 90 ? "err-text" : ""}>{ramPct}%</span></span>}
            {d.host?.cpu_pct != null && <span className="mono small vitals-host">CPU {Math.round(d.host.cpu_pct)}%</span>}
          </>
        )}
      </button>
      {open && d && (
        <div className="vitals-pop card stack">
          <div className="row">
            <strong className="grow">{t("vitalsTitle")}</strong>
            <button className="btn sm icon ghost" onClick={refresh} title={t("recheck")}><RefreshCw size={13} /></button>
          </div>
          {gpus.map((g) => {
            const p = (g.used_mb / Math.max(1, g.total_mb)) * 100;
            return (
              <div key={g.index} className="stack" style={{ gap: 4 }}>
                <div className="row small">
                  <strong className="grow">GPU {g.index} · {g.name.replace("NVIDIA GeForce ", "")}</strong>
                  <span className="mono muted">{g.util != null ? `${Math.round(g.util)}% · ` : ""}{g.temp != null ? `${Math.round(g.temp)}°C · ` : ""}{g.power != null ? `${Math.round(g.power)} W` : ""}</span>
                </div>
                <div className="row small" style={{ gap: 8 }}>
                  <div className={`bar grow ${lvl(p)}`}><span style={{ width: `${p}%` }} /></div>
                  <span className="mono">{gb(g.used_mb)} / {gb(g.total_mb)} GB</span>
                </div>
                <div className="muted small">
                  {g.services.length === 0 && g.others === 0 && t("gpuEmpty")}
                  {g.services.map((id) => {
                    const s = d.services.find((x) => x.id === id);
                    if (!s) return null;
                    const names = s.models.map((m) => m.name).filter(Boolean).join(", ");
                    return <span key={id} className="pill" style={{ marginRight: 6 }}>{s.label}{names ? ` · ${names}` : ""}{s.held_mb ? ` · ${gb(s.held_mb)} GB` : ""}</span>;
                  })}
                  {g.others > 0 && <span>{t("gpuOthers", { n: g.others })}</span>}
                </div>
              </div>
            );
          })}
          {d.host?.ram && (
            <div className="stack" style={{ gap: 4 }}>
              <div className="row small"><MemoryStick size={13} /> <strong className="grow">RAM</strong>
                <span className="mono">{gb(d.host.ram.used_mb)} / {gb(d.host.ram.total_mb)} GB</span></div>
              <div className={`bar ${lvl(ramPct || 0)}`}><span style={{ width: `${ramPct}%` }} /></div>
              {d.host.commit && <div className="muted small mono">{t("vitalsCommit")}: {gb(d.host.commit.used_mb)} / {gb(d.host.commit.total_mb)} GB</div>}
            </div>
          )}
          {d.host?.cpu_pct != null && (
            <div className="stack" style={{ gap: 4 }}>
              <div className="row small"><Cpu size={13} /> <strong className="grow">CPU</strong>
                <span className="mono">{Math.round(d.host.cpu_pct)}%{d.host.cpu_count ? ` · ${d.host.cpu_count} ${t("vitalsThreads")}` : ""}</span></div>
              <div className={`bar ${lvl(d.host.cpu_pct)}`}><span style={{ width: `${d.host.cpu_pct}%` }} /></div>
            </div>
          )}
          <div className="row wrap" style={{ gap: 6 }}>
            {d.services.filter((s) => s.id === d.main_comfy && s.state === "running").map((s) => (
              <ConfirmButton key={`free-${s.id}`} className="btn sm" onConfirm={() => act(`free-${s.id}`, () => api.freeComfy(), t("freed"))}>
                <Trash2 size={12} /> {t("freeComfyShort")}</ConfirmButton>
            ))}
            {d.services.filter((s) => s.stoppable).map((s) => (
              <ConfirmButton key={s.id} className="btn sm danger" onConfirm={() => act(s.id, () => api.stopService(s.id), t("svcStopped"))}>
                <Power size={12} /> {busy === s.id ? t("svcStarting") : `${t("svcStop")} ${s.label}`}</ConfirmButton>
            ))}
          </div>
          <div className="muted small">{t("gpuMemoryHint")}</div>
        </div>
      )}
    </div>
  );
}
