import { useEffect, useRef, useState } from "react";
import { Cpu, Download, HardDrive, Loader2, Music, RefreshCw, Server, Type as TypeIcon, Volume2, Zap } from "lucide-react";
import { api } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, ErrorNote, useApp, useAsync } from "../components/ui";

export function BackendsView() {
  const { t } = useT();
  const app = useApp();
  const status = useAsync(() => api.backend(), []);
  const voices = useAsync(() => api.voices(), [app.dataVersion]);
  const [vram, setVram] = useState<Record<string, number> | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const s = status.data;

  const saveVram = async () => {
    if (!vram) return;
    try {
      await api.setBackend({ vram_estimates_mb: vram });
      app.toast(t("saved"), "ok");
      setVram(null);
      status.reload();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  // the download poll stops when the view goes away
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const download = async (id: string) => {
    setDownloading(id);
    try {
      const job = await api.downloadVoice(id);
      app.refreshJobs();
      for (let i = 0; i < 240 && alive.current; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (!alive.current) return;
        const j = await api.job(job.id);
        if (j.state === "done") { app.toast(`${t("downloaded")}: ${id}`, "ok"); break; }
        if (j.state === "failed" || j.state === "cancelled") { app.toast(j.message || t("downloadFailed"), "bad"); break; }
      }
      if (alive.current) voices.reload();
    } catch (e) {
      if (alive.current) app.toast(`${t("downloadFailed")}: ${(e as Error).message}`, "bad");
    } finally {
      if (alive.current) setDownloading(null);
    }
  };

  return (
    <>
      <div className="page-head">
        <div><h1>{t("backendsTitle")}</h1><p>{t("backendsLead")}</p></div>
        <div className="actions">
          <button className="btn" onClick={status.reload} disabled={status.loading}>
            {status.loading ? <Loader2 size={15} className="spin" /> : <RefreshCw size={15} />} {t("recheck")}
          </button>
        </div>
      </div>
      {!s ? (status.error ? <ErrorNote error={status.error} onRetry={status.reload} /> : <p className="muted">{t("loading")}</p>) : (
        <div className="stack">
          {s.demo && <div className="demo-banner"><Zap size={14} /> {t("demoHint")}</div>}
          <div className="card" style={{ padding: 6 }}>
            <div className="table-scroll"><table className="list">
              <thead><tr><th>{t("capability")}</th><th>{t("state")}</th><th>{t("provider")}</th><th>{t("model")}</th><th style={{ width: "44%" }}>{t("reason")}</th></tr></thead>
              <tbody>
                {Object.values(s.hoard_link).map((r) => (
                  <tr key={r.capability}>
                    <td><strong>{r.capability}</strong></td>
                    <td><span className={`dot ${r.state === "resolved" ? "ok" : r.state === "busy" ? "warn" : "bad"}`} /> <span className="small">{r.state}</span></td>
                    <td className="mono small">{r.provider || "-"}</td>
                    <td className="mono small">{r.model || "-"}</td>
                    <td className="small muted">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </div>

          <div className="grid-2" style={{ alignItems: "start" }}>
            <div className="card stack">
              <h2><Server size={16} /> {t("comfyui")}
                <span className={`pill ${s.comfy.reachable ? "ok" : "bad"}`}>{s.comfy.reachable ? t("reachable") : t("unreachable")}</span>
                <div className="card-actions">
                  <ConfirmButton className="btn sm" onConfirm={async () => {
                    try { const r = await api.freeComfy(); app.toast(r.message, "ok"); status.reload(); } catch (e) { app.toast((e as Error).message, "bad"); }
                  }}>{t("freeComfy")}</ConfirmButton>
                </div>
              </h2>
              <div className="mono small muted">{s.comfy.url} {s.comfy.version && `· ${s.comfy.version}`}</div>
              <div className="muted small">{t("freeComfyHint")}</div>
              <div>
                <div className="panel-title">{t("checkpoints")}</div>
                <div className="row wrap">{(s.comfy.checkpoints || []).map((c) => <span key={c} className="pill mono">{c}</span>)}</div>
              </div>
              {s.comfy.templates && Object.keys(s.comfy.templates).length > 0 && (
                <div>
                  <div className="panel-title">{t("templateReadiness")}</div>
                  <div className="stack" style={{ gap: 4 }}>
                    {Object.entries(s.comfy.templates).map(([name, r]) => (
                      <div key={name} className="row small" style={{ alignItems: "baseline", gap: 8 }}>
                        <span className={`pill ${r === "ready" ? "ok" : "bad"}`}>{r === "ready" ? t("ready") : t("missing")}</span>
                        <span className="mono">{name}</span>
                        {r !== "ready" && <span className="muted ellipsis" title={r.missing.join(", ")}>{r.missing.join(", ")}</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              <div>
                <div className="panel-title"><Cpu size={14} /> {t("gpu")}</div>
                {(s.comfy.devices || []).map((d) => (
                  <div key={d.name} className="stack" style={{ gap: 4, marginBottom: 8 }}>
                    <div className="row small"><span className="grow">{d.name}</span><span className="mono">{t("vramFree", { free: d.vram_free_mb, total: d.vram_total_mb })}</span></div>
                    <div className="bar"><span style={{ width: `${100 - (d.vram_free_mb / Math.max(1, d.vram_total_mb)) * 100}%` }} /></div>
                  </div>
                ))}
              </div>
              <div>
                <div className="panel-title">{t("vramTable")}</div>
                <div className="grid-3">
                  {Object.entries(vram || s.vram_estimates_mb).map(([k, v]) => (
                    <label key={k} className="field">{k}
                      <input type="number" value={v} min={256} className="mono" onChange={(e) => setVram({ ...(vram || s.vram_estimates_mb), [k]: Number(e.target.value) })} />
                    </label>
                  ))}
                </div>
                {vram && <button className="btn sm primary" style={{ marginTop: 10 }} onClick={saveVram}>{t("save")}</button>}
              </div>
            </div>

            <div className="stack">
              <div className="card stack">
                <h2><HardDrive size={16} /> {t("ffmpeg")} <span className={`pill ${s.ffmpeg.found ? "ok" : "bad"}`}>{s.ffmpeg.found ? "ok" : t("notFound")}</span></h2>
                <div className="mono small muted" style={{ overflowWrap: "anywhere" }}>{s.ffmpeg.version}</div>
                <div className="mono small muted ellipsis">{s.ffmpeg.path}</div>
              </div>
              <div className="card stack">
                <h2><Volume2 size={16} /> {t("piper")} <span className={`pill ${s.piper.installed ? "ok" : "bad"}`}>{s.piper.installed ? "ok" : t("notFound")}</span></h2>
                <div className="muted small">{t("downloadNote")} {t("noVoiceCloning")}</div>
                {(voices.data?.items || []).map((v) => (
                  <div key={v.id} className="row">
                    <span className="grow"><strong className="small">{v.label}</strong><div className="mono muted small">{v.id} · {v.size_mb} MB</div></span>
                    {v.downloaded ? <span className="pill ok">{t("downloaded")}</span> : (
                      <button className="btn sm" onClick={() => download(v.id)} disabled={downloading !== null}>
                        {downloading === v.id ? <Loader2 size={13} className="spin" /> : <Download size={13} />} {t("downloadVoice")}
                      </button>
                    )}
                  </div>
                ))}
              </div>
              <div className="card stack">
                <h2><Music size={16} /> {t("music")}</h2>
                {s.music.map((m) => (
                  <div key={m.name} className="stack" style={{ gap: 4 }}>
                    <div className="row"><span className={`dot ${m.available ? "ok" : "bad"}`} /><strong className="small mono">{m.name}</strong></div>
                    <div className="muted small">{m.reason}</div>
                  </div>
                ))}
              </div>
              <div className="card">
                <h2><TypeIcon size={16} /> {t("fonts")}</h2>
                <div className="row wrap">{s.fonts_bundled.map((f) => <span key={f} className="pill">{f}</span>)}</div>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
