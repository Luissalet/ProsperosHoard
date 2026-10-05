import { useEffect, useState } from "react";
import { Cpu, Loader2, MemoryStick, Play, Power, Trash2 } from "lucide-react";
import { api, GpuMemory, ImageEngines, ServicesStatus } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, useApp } from "../components/ui";

const LABELS: Record<string, string> = { qwen21: "Qwen-Image 2.1", flux: "FLUX.1", sdxl: "SDXL", sd15: "SD 1.5" };
const gb = (mb: number | null | undefined) => (mb == null ? "?" : `${(mb / 1024).toFixed(1)} GB`);

// The Generate screen's model header: engine and model file, where ComfyUI
// runs, whether the engine fits in the free VRAM, and what is holding the
// GPUs - with the actions to change it (start ComfyUI, free it, stop a
// language model) right here.
export function EngineBar({ sel, setSel, model, setModel, resolved, customs, engines }: {
  sel: string; setSel: (v: string) => void; model: string; setModel: (v: string) => void; resolved: string;
  customs: { template: string; name?: string | null }[]; engines: ImageEngines | null;
}) {
  const { t } = useT();
  const app = useApp();
  const [mem, setMem] = useState<GpuMemory | null>(null);
  const [svc, setSvc] = useState<ServicesStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [statusError, setStatusError] = useState("");

  const refresh = async () => {
    setChecking(true); setStatusError("");
    try {
      const [m, s] = await Promise.all([api.memory(), api.services()]);
      setMem(m); setSvc(s);
    } catch (err) { setStatusError((err as Error).message); }
    finally { setChecking(false); }
  };
  useEffect(() => {
    refresh();
    const h = setInterval(() => { if (!document.hidden) refresh(); }, 15000);
    return () => clearInterval(h);
  }, []);

  const act = async (id: string, what: () => Promise<{ ok?: boolean; error?: string; message?: string }>, done: string) => {
    setBusy(id);
    try {
      const r = await what();
      if (r.ok === false) app.toast(r.error || "failed", "bad"); else app.toast(r.message || done, "ok");
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(null);
      refresh();
    }
  };
  const startComfy = (gpu?: string) => act("comfyui", async () => {
    const r = await api.startService("comfyui", gpu);
    for (let i = 0; i < 90 && r.ok && !r.already; i++) {
      await new Promise((res) => setTimeout(res, 2000));
      const s = await api.services();
      setSvc(s);
      const main = s.items.find((x) => x.id === s.main_comfy);
      if (!main || main.state !== "starting") break;
    }
    return r;
  }, t("svcReady"));

  const mainId = svc?.main_comfy || mem?.main_comfy || "comfyui@8188";
  const main = svc?.items.find((x) => x.id === mainId);
  const comfyMem = mem?.services.find((x) => x.id === mainId);
  const comfyGpu = comfyMem?.gpus[0] ?? main?.gpu ?? null;
  const gpus = mem?.gpus || [];
  const target = comfyGpu != null ? gpus.find((g) => g.index === comfyGpu)
    : [...gpus].sort((a, b) => b.free_mb - a.free_mb)[0];
  const need = resolved === "custom" ? null : (engines?.vram_mb[resolved] ?? mem?.vram_estimates_mb[resolved] ?? null);
  const usable = target ? target.free_mb + (comfyMem?.held_mb || 0) : null;
  const fit = need == null || usable == null ? "unknown" : usable >= need ? "ok" : usable >= need * 0.45 ? "slow" : "tight";
  const holders = (mem?.services || []).filter((x) => x.id !== mainId && target && x.gpus.includes(target.index));
  const models = engines?.models[resolved] || [];
  const startable = (svc?.items || []).filter((x) => x.startable && x.kind !== "comfyui");

  return (
    <div className="card stack" style={{ gap: 10 }}>
      <div className="row wrap" style={{ gap: 10, alignItems: "flex-end" }}>
        <label className="field" style={{ minWidth: 220 }}>{t("imageEngine")}
          <select value={sel} onChange={(e) => { setSel(e.target.value); setModel(""); }}>
            <option value="auto">{t("engineAuto", { name: LABELS[engines?.auto_resolves_to || "qwen21"] })}</option>
            <option value="qwen21" disabled={engines ? !engines.installed.qwen21 : false}>Qwen-Image 2.1{engines && !engines.installed.qwen21 ? ` (${t("notInstalled")})` : ""}</option>
            <option value="flux" disabled={engines ? !engines.installed.flux : false}>FLUX.1 schnell / Kontext{engines && !engines.installed.flux ? ` (${t("notInstalled")})` : ""}</option>
            <option value="sdxl">SDXL</option>
            <option value="tmpl:sd15_txt2img">SD 1.5 · txt2img (low VRAM)</option>
            {customs.map((w) => <option key={w.template} value={`tmpl:${w.template}`}>{w.name || w.template}</option>)}
          </select>
        </label>
        {models.length > 0 && (
          <label className="field" style={{ minWidth: 240, flex: 1 }}>{t("modelFile")}
            <select value={model} onChange={(e) => setModel(e.target.value)} className="mono">
              <option value="">{t("modelDefault")}</option>
              {models.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
        )}
        <div className="grow" />
        <div className="row" style={{ gap: 6 }}>
          <span className={`dot ${!svc || statusError ? "warn" : main?.state === "running" ? "ok" : main?.state === "starting" ? "warn" : "bad"}`} />
          <span className="small" role="status" title={main?.busy ? t("svcBusyHint") : undefined}>ComfyUI {statusError ? t("engineStatusUnavailable") : !svc ? (checking ? t("loading") : t("engineStatusUnavailable")) : main?.state === "running" ? (comfyGpu != null ? `· GPU ${comfyGpu}` : "") + (main.busy ? ` · ${t("svcBusy")}` : "")
            : main?.state === "starting" ? t("svcStarting") : t("comfyOff")}</span>
          {main && main.startable && (
            <button className="btn sm primary" disabled={busy !== null} onClick={() => startComfy("auto")}>
              {busy === "comfyui" ? <Loader2 size={13} className="spin" /> : <Play size={13} />} {t("svcStart")}</button>
          )}
          {main?.state === "running" && (
            <ConfirmButton className="btn sm" onConfirm={() => act("free", async () => api.freeComfy(), t("freed"))}>
              <Trash2 size={13} /> {t("freeComfyShort")}</ConfirmButton>
          )}
          <button className={`btn sm${open ? " primary" : ""}`} onClick={() => setOpen(!open)}><MemoryStick size={13} /> {t("gpuMemory")}</button>
        </div>
      </div>
      {statusError && <div className="row wrap" role="alert"><span className="small">{statusError}</span><button className="btn sm" disabled={checking} onClick={refresh}>{t("recheck")}</button></div>}

      {target && need != null && (
        <div className={`small ${fit === "ok" ? "muted" : "err-text"}`}>
          <Cpu size={12} /> {t("fitLine", { engine: LABELS[resolved] || resolved, need: gb(need), gpu: String(target.index), free: gb(usable) })}{" "}
          {fit === "ok" ? t("fitOk") : fit === "slow" ? t("fitSlow") : t("fitTight")}
          {fit !== "ok" && holders.length > 0 && (
            <span> {t("fitHeldBy")} {holders.map((h) => `${h.label}${h.models[0]?.name ? ` (${h.models[0].name})` : ""}`).join(", ")}.</span>
          )}
          {fit !== "ok" && holders.filter((h) => h.stoppable).map((h) => (
            <ConfirmButton key={h.id} className="btn sm danger" onConfirm={() => act(h.id, () => api.stopService(h.id), t("svcStopped"))}>
              {busy === h.id ? <Loader2 size={12} className="spin" /> : <Power size={12} />} {t("svcStop")} {h.label}
            </ConfirmButton>
          ))}
        </div>
      )}

      {open && mem && (
        <div className="stack" style={{ gap: 8 }}>
          {mem.gpus.map((g) => (
            <div key={g.index} className="stack" style={{ gap: 4 }}>
              <div className="row small">
                <strong className="grow">GPU {g.index} · {g.name}</strong>
                <span className="mono">{gb(g.free_mb)} {t("svcFree")} / {gb(g.total_mb)}</span>
              </div>
              <div className="bar"><span style={{ width: `${100 - (g.free_mb / Math.max(1, g.total_mb)) * 100}%` }} /></div>
              <div className="muted small">
                {g.services.length === 0 && g.others === 0 && t("gpuEmpty")}
                {g.services.map((id) => {
                  const s = mem.services.find((x) => x.id === id);
                  if (!s) return null;
                  const names = s.models.map((m) => m.name).filter(Boolean).join(", ");
                  return <span key={id} className="pill" style={{ marginRight: 6 }}>{s.label}{names ? ` · ${names}` : ""}{s.held_mb ? ` · ${gb(s.held_mb)}` : ""}</span>;
                })}
                {g.others > 0 && <span>{t("gpuOthers", { n: g.others })}</span>}
              </div>
            </div>
          ))}
          <div className="row wrap" style={{ gap: 6 }}>
            {mem.services.filter((s) => s.stoppable).map((s) => (
              <ConfirmButton key={s.id} className="btn sm danger" onConfirm={() => act(s.id, () => api.stopService(s.id), t("svcStopped"))}>
                <Power size={12} /> {t("svcStop")} {s.label}</ConfirmButton>
            ))}
            {startable.map((s) => (
              <button key={s.id} className="btn sm" disabled={busy !== null}
                onClick={() => act(s.id, () => api.startService(s.id), t("svcReady"))}>
                {busy === s.id ? <Loader2 size={12} className="spin" /> : <Play size={12} />} {t("svcStart")} {s.label}</button>
            ))}
          </div>
          <div className="muted small">{t("gpuMemoryHint")}</div>
        </div>
      )}
    </div>
  );
}
