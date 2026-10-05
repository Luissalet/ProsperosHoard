import { useEffect, useState } from "react";
import { FileText, Loader2, Play, Power, Save, Server } from "lucide-react";
import { api, LocalService, ServicesStatus } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, useApp } from "../components/ui";

// Start/stop the shared servers (ComfyUI, the render pool, Ollama, configured
// commands) without Faustus. Polls while something is starting.
export function LocalServices({ initial, onChange }: { initial?: ServicesStatus; onChange: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [svc, setSvc] = useState<ServicesStatus | null>(initial || null);
  const [busy, setBusy] = useState<string | null>(null);
  const [gpu, setGpu] = useState<Record<string, string>>({});
  const [form, setForm] = useState<Record<string, string> | null>(null);

  const refresh = async () => {
    try { setSvc(await api.services()); } catch (e) { app.toast((e as Error).message, "bad"); }
  };
  useEffect(() => { if (!initial) refresh(); else setSvc(initial); }, [initial]);

  const starting = (svc?.items || []).some((i) => i.state === "starting");
  useEffect(() => {
    if (!starting) return;
    const h = setInterval(refresh, 2000);
    return () => clearInterval(h);
  }, [starting]);

  const waitFor = async (ids: string[]) => {
    for (let n = 0; n < 150; n++) {
      await new Promise((r) => setTimeout(r, 2000));
      const s = await api.services();
      setSvc(s);
      const rows = s.items.filter((i) => ids.includes(i.id));
      if (rows.every((i) => i.state !== "starting")) return rows;
    }
    return [];
  };

  const start = async (item: LocalService | null) => {
    const id = item ? item.id : "render_pool";
    setBusy(id);
    try {
      const res = await api.startService(id, item ? gpu[item.id] : undefined);
      const results = res.results || [res];
      const bad = results.find((r) => !r.ok);
      if (bad) { app.toast(bad.error || "failed", "bad"); await refresh(); return; }
      const ids = results.map((r) => r.service || id);
      const rows = await waitFor(ids);
      for (const row of rows) {
        app.toast(`${row.label} ${row.state === "running" ? t("svcReady") : t("svcNotReady")}`, row.state === "running" ? "ok" : "bad");
      }
      onChange();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(null);
    }
  };

  const stop = async (item: LocalService) => {
    setBusy(item.id);
    try {
      const res = await api.stopService(item.id);
      if (!res.ok) app.toast(res.error || "failed", "bad");
      else app.toast(`${item.label}: ${t("svcStopped")}`, "ok");
      await refresh();
      onChange();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (!form || !svc) return;
    try {
      const patch: Record<string, unknown> = {};
      if (form.dir !== undefined) patch.comfyui_dir = form.dir;
      if (form.python !== undefined) patch.comfyui_python = form.python;
      if (form.gpu !== undefined) patch.comfyui_gpu = form.gpu;
      if (form.args !== undefined) patch.comfyui_args = form.args.split(/\s+/).filter(Boolean);
      if (form.ollama !== undefined) patch.ollama_exe = form.ollama;
      setSvc(await api.setLaunch(patch));
      setForm(null);
      app.toast(t("saved"), "ok");
      onChange();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const setAutostart = async (on: boolean) => {
    try { setSvc(await api.setLaunch({ autostart_comfy: on })); } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  if (!svc) return null;
  const hasPool = svc.items.some((i) => i.role === "render_pool");
  const running = svc.items.filter((i) => i.state === "running").length;
  const f = (k: string, fallback: string) => (form && form[k] !== undefined ? form[k] : fallback);
  const edit = (k: string, v: string) => setForm({ ...(form || {}), [k]: v });

  return (
    <div className="card stack">
      <h2><Server size={16} /> {t("servicesTitle")}
        <span className={`pill ${running ? "ok" : "bad"}`}>{running}/{svc.items.length}</span>
        {hasPool && !svc.demo && (
          <div className="card-actions">
            <button className="btn sm" onClick={() => start(null)} disabled={busy !== null}>
              {busy === "render_pool" ? <Loader2 size={13} className="spin" /> : <Play size={13} />} {t("svcStartPool")}
            </button>
          </div>
        )}
      </h2>
      <div className="muted small">{t("servicesLead")}</div>
      <div className="table-scroll" tabIndex={0} role="region" aria-label={t("tableScroll")}><table className="list">
        <tbody>
          {svc.items.map((i) => (
            <tr key={i.id}>
              <td style={{ width: "26%" }}>
                <strong className="small">{i.label}</strong>{" "}
                {i.role === "main" && <span className="pill accent">{t("svcMain")}</span>}
                {i.role === "render_pool" && <span className="pill">{t("svcPool")}</span>}
                <div className="muted small">{i.capabilities.join(" · ")}</div>
              </td>
              <td style={{ width: "14%" }}>
                <span className={`dot ${i.state === "running" ? "ok" : i.state === "starting" ? "warn" : "bad"}`} />{" "}
                <span className="small">{i.state === "starting" ? t("svcStarting") : i.state}</span>
              </td>
              <td className="small">
                <div className="mono muted">{i.url}{i.gpu !== null && i.gpu !== undefined ? ` · GPU ${i.gpu}` : ""}</div>
                {i.problem && i.state !== "running" && <div className="muted">{i.problem}</div>}
                {i.state === "running" && (
                  <div className="muted">{i.started_by ? `${t("svcStartedBy")} ${i.started_by}` : t("svcStartedElsewhere")}</div>
                )}
                {i.log && <div className="mono muted ellipsis" title={i.log}><FileText size={11} /> {i.log}</div>}
              </td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                {i.startable && !svc.demo && (
                  <span className="row" style={{ justifyContent: "flex-end", gap: 6 }}>
                    {i.kind === "comfyui" && svc.gpus.length > 0 && (
                      <select className="sm" style={{ width: "auto", maxWidth: 280 }} value={gpu[i.id] || "auto"} onChange={(e) => setGpu({ ...gpu, [i.id]: e.target.value })}>
                        <option value="auto">{t("svcGpuAuto")}</option>
                        {svc.gpus.map((g) => <option key={g.index} value={String(g.index)}>GPU {g.index} · {g.name} · {g.free_mb} MB {t("svcFree")}</option>)}
                      </select>
                    )}
                    <button className="btn sm primary" onClick={() => start(i)} disabled={busy !== null}>
                      {busy === i.id ? <Loader2 size={13} className="spin" /> : <Play size={13} />} {t("svcStart")}
                    </button>
                  </span>
                )}
                {i.stoppable && (
                  <ConfirmButton className="btn sm danger" onConfirm={() => stop(i)}><Power size={13} /> {t("svcStop")}</ConfirmButton>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table></div>

      <label className="row small" style={{ gap: 8 }}>
        <input type="checkbox" checked={svc.autostart_comfy} disabled={svc.demo} onChange={(e) => setAutostart(e.target.checked)} />
        {t("svcAutostart")}
      </label>

      <details>
        <summary className="panel-title" style={{ cursor: "pointer" }}>{t("svcSettings")}</summary>
        <div className="grid-2" style={{ marginTop: 10 }}>
          <label className="field">{t("svcComfyDir")}
            <input className="mono" value={f("dir", svc.comfyui.dir || "")} placeholder={t("svcNotFound")} onChange={(e) => edit("dir", e.target.value)} />
            {svc.comfyui.problem && <span className="hint">{svc.comfyui.problem}</span>}
          </label>
          <label className="field">{t("svcComfyPython")}
            <input className="mono" value={f("python", svc.comfyui.python || "")} placeholder={t("svcDetected")} onChange={(e) => edit("python", e.target.value)} />
          </label>
          <label className="field">{t("svcDefaultGpu")}
            <select value={f("gpu", String(svc.comfyui.gpu ?? "auto"))} onChange={(e) => edit("gpu", e.target.value)}>
              <option value="auto">{t("svcGpuAuto")}</option>
              {svc.gpus.map((g) => <option key={g.index} value={String(g.index)}>GPU {g.index} · {g.name}</option>)}
            </select>
          </label>
          <label className="field">{t("svcComfyArgs")}
            <input className="mono" value={f("args", (svc.comfyui.args || []).join(" "))} placeholder="--lowvram" onChange={(e) => edit("args", e.target.value)} />
          </label>
          <label className="field">{t("svcOllamaExe")}
            <input className="mono" value={f("ollama", svc.ollama.exe || "")} placeholder={t("svcNotFound")} onChange={(e) => edit("ollama", e.target.value)} />
          </label>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <span className="grow muted small">{t("svcSharedConfig")} <span className="mono">{svc.config_path}</span></span>
          {form && <button className="btn sm primary" onClick={save}><Save size={13} /> {t("save")}</button>}
        </div>
      </details>
    </div>
  );
}
