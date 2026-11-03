// Edit plans: a declarative edit compiled into saved ffmpeg commands (Plan), rendered exactly as planned (Run plan)
// and checked against a platform's delivery spec with a contact sheet (Verify).
import { useEffect, useMemo, useState } from "react";
import { CheckCircle2, ClipboardList, Film, Loader2, Play, Plus, ShieldCheck, Trash2, XCircle, AlertTriangle } from "lucide-react";
import { api, fileUrl, type Asset, type EditPlan, type VerifyReport } from "../api";
import { useT } from "../i18n";
import { useApp, useAsync } from "../components/ui";

const L = (lang: string, en: string, es: string) => (lang === "es" ? es : en);
const PLATFORMS = [["shorts", "YouTube Shorts"], ["reels", "Instagram Reels"], ["tiktok", "TikTok"], ["youtube", "YouTube"]];
const TRANSITIONS = ["cut", "crossfade", "dip_black", "flash_white", "dissolve", "wipeleft", "wiperight", "slideup", "slidedown", "circleopen", "radial", "pixelize", "zoomin"];

const CHECK_ES: Record<string, string> = {
  video: "Pista de vídeo", resolution: "Resolución", fps: "Fotogramas por segundo", video_codec: "Códec de vídeo", duration: "Duración",
  audio: "Pista de audio", audio_layout: "Canales de audio", sample_rate: "Frecuencia de muestreo", audio_codec: "Códec de audio",
  loudness: "Sonoridad integrada", true_peak: "Pico real",
};

interface ClipRow { asset_id: string; in_s?: number; out_s?: number; duration_s?: number; transition: string; transition_s: number }
interface CaptionRow { text: string; start_s: number; end_s: number }
interface OverlayRow { asset_id: string; start_s: number; end_s: number; x: number; y: number; scale: number }

/** pass / warn / fail table of a verification, with the contact sheet. */
export function VerifyReportCard({ report }: { report: VerifyReport }) {
  const { lang } = useT();
  const app = useApp();
  const icon = (s: string) => s === "pass" ? <CheckCircle2 size={14} className="wf-ok" /> : s === "warn" ? <AlertTriangle size={14} className="wf-warn" /> : <XCircle size={14} className="wf-bad" />;
  return (
    <div className="verify-report" data-testid="verify-report">
      <div className="row wrap" style={{ gap: 8 }}>
        <ShieldCheck size={16} /> <strong>{report.label}</strong>
        <span className={`pill ${report.ok ? (report.warnings ? "warn" : "ok") : "bad"}`}>
          {report.ok ? (report.warnings ? L(lang, `ready, ${report.warnings} warning(s)`, `lista, ${report.warnings} aviso(s)`) : L(lang, "ready to upload", "lista para subir"))
            : L(lang, "does not meet the spec", "no cumple la especificación")}</span>
        {report.media.integrated_lufs != null && <span className="small muted">{String(report.media.integrated_lufs)} LUFS · {String(report.media.true_peak_dbfs ?? "—")} dBTP</span>}
      </div>
      <table className="verify-table small">
        <thead><tr><th /><th>{L(lang, "Check", "Comprobación")}</th><th>{L(lang, "Expected", "Esperado")}</th><th>{L(lang, "Actual", "Real")}</th></tr></thead>
        <tbody>{report.checks.map((c) => <tr key={c.id} className={c.status}><td>{icon(c.status)}</td><td>{lang === "es" ? CHECK_ES[c.id] || c.label : c.label}</td>
          <td className="mono">{String(c.expected)}</td><td className="mono">{String(c.actual)}</td></tr>)}</tbody>
      </table>
      {report.contact_sheet_asset_id && <img className="tr-sheet" src={fileUrl(report.contact_sheet_asset_id)} alt={L(lang, "Contact sheet", "Hoja de contactos")}
        onClick={() => app.openAsset(report.contact_sheet_asset_id!)} />}
      {report.contact_sheet_error && <p className="small muted">{report.contact_sheet_error}</p>}
    </div>
  );
}

export function EditPlansView() {
  const { t, lang } = useT();
  const app = useApp();
  const projectId = app.projectId!;
  const { data: assetPage } = useAsync(() => api.assets(projectId, { limit: 100 }), [projectId, app.dataVersion]);
  const { data: plans, reload: reloadPlans } = useAsync(() => api.editPlans(projectId), [projectId, app.dataVersion]);
  const assets = assetPage?.items || [];
  const byKind = (...kinds: string[]) => assets.filter((a: Asset) => kinds.includes(a.kind));
  const visuals = byKind("video", "image"), images = byKind("image"), sounds = byKind("audio", "video"), videos = byKind("video");
  const label = (a: Asset) => `${a.name || a.id} · ${a.kind}${a.duration_s ? ` · ${a.duration_s.toFixed(1)} s` : ""}`;
  const [name, setName] = useState("");
  const [platform, setPlatform] = useState("shorts");
  const [quality, setQuality] = useState<"final" | "preview">("final");
  const [framing, setFraming] = useState("fill");
  const [clips, setClips] = useState<ClipRow[]>([]);
  const [captions, setCaptions] = useState<CaptionRow[]>([]);
  const [overlays, setOverlays] = useState<OverlayRow[]>([]);
  const [music, setMusic] = useState({ asset_id: "", volume_db: -8, duck: true });
  const [plan, setPlan] = useState<EditPlan | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [verifyAsset, setVerifyAsset] = useState("");
  const [verifyPlatform, setVerifyPlatform] = useState("shorts");
  const [report, setReport] = useState<VerifyReport | null>(null);
  const job = app.jobs.find((j) => j.id === jobId);
  useEffect(() => {
    if (!plan || !job || !["done", "failed", "cancelled"].includes(job.state)) return;
    if (job.state === "done") api.editPlan(plan.plan_id).then((p) => { setPlan(p); reloadPlans(); }).catch(() => undefined);
    else app.toast(job.message || job.state, "bad");
    setJobId(null);
  }, [job?.state]); // eslint-disable-line react-hooks/exhaustive-deps
  const edit = useMemo(() => ({
    platform, quality, framing,
    clips: clips.filter((c) => c.asset_id).map((c) => ({ asset_id: c.asset_id, in_s: c.in_s, out_s: c.out_s, duration_s: c.duration_s,
      transition: { type: c.transition, duration_s: c.transition_s } })),
    captions: captions.filter((c) => c.text.trim()), overlays: overlays.filter((o) => o.asset_id),
    ...(music.asset_id ? { music } : {}),
  }), [platform, quality, framing, clips, captions, overlays, music]);
  const act = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    try { await fn(); } catch (e) { app.toast((e as Error).message, "bad"); } finally { setBusy(null); }
  };
  const makePlan = () => act("plan", async () => { setPlan(await api.createEditPlan(projectId, edit, name || undefined)); reloadPlans(); });
  const runPlan = () => act("run", async () => {
    const r = await api.runEditPlan(plan!.plan_id);
    setJobId(r.job.id); app.refreshJobs(); app.toast(L(lang, "Rendering the plan", "Renderizando el plan"), "info");
  });
  const [trashOutputs, setTrashOutputs] = useState(true);
  const removePlan = (p: EditPlan) => {
    const what = trashOutputs ? L(lang, " Its rendered videos and contact sheets go to the trash.", " Sus vídeos renderizados y hojas de contactos irán a la papelera.") : "";
    if (!window.confirm(L(lang, `Delete the plan "${p.name}"?`, `¿Borrar el plan «${p.name}»?`) + what)) return;
    act(`delete:${p.plan_id}`, async () => {
      const r = await api.deleteEditPlan(p.plan_id, trashOutputs);
      if (plan?.plan_id === p.plan_id) setPlan(null);
      reloadPlans(); app.bump();
      const kept = r.outputs_kept?.length ? L(lang, ` · ${r.outputs_kept.length} kept (in use)`, ` · ${r.outputs_kept.length} conservado(s) (en uso)`) : "";
      app.toast(L(lang, `Plan deleted · ${r.outputs_trashed.length} file(s) to the trash`, `Plan borrado · ${r.outputs_trashed.length} archivo(s) a la papelera`) + kept, "info");
    });
  };
  const verify = () => act("verify", async () => { setReport(await api.verifyVideo({ asset_id: verifyAsset, platform: verifyPlatform })); app.bump(); });
  const clip = (i: number, patch: Partial<ClipRow>) => setClips(clips.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const kindOf = (id: string) => assets.find((a: Asset) => a.id === id)?.kind;
  const num = (v: string) => (v === "" ? undefined : Number(v));

  return (
    <div className="stack edit-plans">
      <div className="card">
        <h2><ClipboardList size={17} /> {t("navDelivery")}</h2>
        <p className="small muted">{L(lang, "Describe the edit, press Plan to see the exact ffmpeg commands, then Run plan. A plan refuses to run if one of its files changed after planning, and every render is checked against the platform's spec.",
          "Describe el montaje, pulsa Planificar para ver los comandos exactos de ffmpeg y luego Ejecutar plan. Un plan no se ejecuta si alguno de sus archivos cambió después de planificarlo, y cada render se comprueba contra la especificación de la plataforma.")}</p>
        <div className="row wrap" style={{ gap: 8 }}>
          <input className="grow" value={name} onChange={(e) => setName(e.target.value)} placeholder={L(lang, "Name (optional)", "Nombre (opcional)")} />
          <select value={platform} onChange={(e) => setPlatform(e.target.value)} aria-label={L(lang, "Platform", "Plataforma")}>
            {PLATFORMS.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
          <select value={framing} onChange={(e) => setFraming(e.target.value)} aria-label={L(lang, "Framing", "Encuadre")}>
            <option value="fill">{L(lang, "Fill (crop)", "Llenar (recorta)")}</option><option value="blur">{L(lang, "Blurred fill", "Fondo desenfocado")}</option><option value="fit">{L(lang, "Fit (bars)", "Encajar (barras)")}</option></select>
          <select value={quality} onChange={(e) => setQuality(e.target.value as "final" | "preview")} aria-label={L(lang, "Quality", "Calidad")}>
            <option value="final">{L(lang, "Final", "Final")}</option><option value="preview">{L(lang, "Fast preview", "Vista previa rápida")}</option></select>
        </div>
        <h3 className="small" style={{ marginTop: 12 }}>{L(lang, "Clips", "Clips")}</h3>
        {clips.map((c, i) => (
          <div key={i} className="ep-row" data-testid="ep-clip">
            <select value={c.asset_id} onChange={(e) => clip(i, { asset_id: e.target.value })}><option value="">—</option>
              {visuals.map((a) => <option key={a.id} value={a.id}>{label(a)}</option>)}</select>
            {kindOf(c.asset_id) === "image"
              ? <input type="number" min={0.1} step={0.1} value={c.duration_s ?? 3} onChange={(e) => clip(i, { duration_s: num(e.target.value) })} title={L(lang, "Seconds", "Segundos")} />
              : <><input type="number" min={0} step={0.1} value={c.in_s ?? ""} placeholder="in" onChange={(e) => clip(i, { in_s: num(e.target.value) })} />
                <input type="number" min={0} step={0.1} value={c.out_s ?? ""} placeholder="out" onChange={(e) => clip(i, { out_s: num(e.target.value) })} /></>}
            <select value={c.transition} disabled={i === 0} onChange={(e) => clip(i, { transition: e.target.value })} aria-label={L(lang, "Transition in", "Transición de entrada")}>
              {TRANSITIONS.map((tr) => <option key={tr} value={tr}>{tr}</option>)}</select>
            {c.transition !== "cut" && i > 0 && <input type="number" min={0.05} max={3} step={0.05} value={c.transition_s} onChange={(e) => clip(i, { transition_s: Number(e.target.value) })} />}
            <button className="btn sm icon ghost" onClick={() => setClips(clips.filter((_, j) => j !== i))} aria-label={L(lang, "Remove", "Quitar")}><Trash2 size={13} /></button>
          </div>
        ))}
        <button className="btn sm ghost" onClick={() => setClips([...clips, { asset_id: "", transition: "cut", transition_s: 0.4 }])} data-testid="ep-add-clip"><Plus size={13} /> {L(lang, "Add clip", "Añadir clip")}</button>
        <h3 className="small" style={{ marginTop: 12 }}>{L(lang, "Captions", "Rótulos")}</h3>
        {captions.map((c, i) => (
          <div key={i} className="ep-row">
            <input className="grow" value={c.text} onChange={(e) => setCaptions(captions.map((x, j) => j === i ? { ...x, text: e.target.value } : x))} />
            <input type="number" min={0} step={0.1} value={c.start_s} onChange={(e) => setCaptions(captions.map((x, j) => j === i ? { ...x, start_s: Number(e.target.value) } : x))} />
            <input type="number" min={0} step={0.1} value={c.end_s} onChange={(e) => setCaptions(captions.map((x, j) => j === i ? { ...x, end_s: Number(e.target.value) } : x))} />
            <button className="btn sm icon ghost" onClick={() => setCaptions(captions.filter((_, j) => j !== i))} aria-label={L(lang, "Remove", "Quitar")}><Trash2 size={13} /></button>
          </div>
        ))}
        <button className="btn sm ghost" onClick={() => setCaptions([...captions, { text: "", start_s: 0, end_s: 2 }])}><Plus size={13} /> {L(lang, "Add caption", "Añadir rótulo")}</button>
        <h3 className="small" style={{ marginTop: 12 }}>{L(lang, "Overlays (logo, sticker)", "Superposiciones (logo, pegatina)")}</h3>
        {overlays.map((o, i) => (
          <div key={i} className="ep-row">
            <select value={o.asset_id} onChange={(e) => setOverlays(overlays.map((x, j) => j === i ? { ...x, asset_id: e.target.value } : x))}><option value="">—</option>
              {images.map((a) => <option key={a.id} value={a.id}>{label(a)}</option>)}</select>
            {(["start_s", "end_s", "x", "y", "scale"] as const).map((k) => <input key={k} type="number" step={k.endsWith("_s") ? 0.1 : 0.05} value={o[k]} title={k}
              onChange={(e) => setOverlays(overlays.map((x, j) => j === i ? { ...x, [k]: Number(e.target.value) } : x))} />)}
            <button className="btn sm icon ghost" onClick={() => setOverlays(overlays.filter((_, j) => j !== i))} aria-label={L(lang, "Remove", "Quitar")}><Trash2 size={13} /></button>
          </div>
        ))}
        <button className="btn sm ghost" onClick={() => setOverlays([...overlays, { asset_id: "", start_s: 0, end_s: 5, x: 0.95, y: 0.05, scale: 0.2 }])}><Plus size={13} /> {L(lang, "Add overlay", "Añadir superposición")}</button>
        <h3 className="small" style={{ marginTop: 12 }}>{L(lang, "Music bed", "Música de fondo")}</h3>
        <div className="ep-row">
          <select value={music.asset_id} onChange={(e) => setMusic({ ...music, asset_id: e.target.value })}><option value="">{L(lang, "No music", "Sin música")}</option>
            {sounds.map((a) => <option key={a.id} value={a.id}>{label(a)}</option>)}</select>
          <label className="row small" style={{ gap: 4 }}>dB <input type="number" min={-60} max={12} value={music.volume_db} onChange={(e) => setMusic({ ...music, volume_db: Number(e.target.value) })} /></label>
          <label className="row small" style={{ gap: 4 }}><input type="checkbox" checked={music.duck} onChange={(e) => setMusic({ ...music, duck: e.target.checked })} /> {L(lang, "Duck under the clips' sound", "Bajar bajo el sonido de los clips")}</label>
        </div>
        <div className="row wrap" style={{ gap: 8, marginTop: 14 }}>
          <button className="btn primary" disabled={!!busy || !edit.clips.length} onClick={makePlan} data-testid="ep-plan">
            {busy === "plan" ? <Loader2 size={14} className="spin" /> : <ClipboardList size={14} />} {L(lang, "Plan", "Planificar")}</button>
          <button className="btn" disabled={!!busy || !plan || !!jobId} onClick={runPlan} data-testid="ep-run">
            {busy === "run" || jobId ? <Loader2 size={14} className="spin" /> : <Play size={14} />} {L(lang, "Run plan", "Ejecutar plan")}</button>
          {job && <span className="small muted">{job.state} · {Math.round((job.progress || 0) * 100)}% {job.message || ""}</span>}
        </div>
      </div>
      {plan && (
        <div className="card" data-testid="ep-plan-card">
          <h2><ClipboardList size={16} /> {plan.name} <span className="pill">{plan.status}</span></h2>
          <p className="small muted">{plan.width}x{plan.height} · {plan.fps} fps · {plan.duration_s.toFixed(2)} s · {plan.clips} clips
            {plan.captions ? ` · ${plan.captions} ${L(lang, "captions", "rótulos")}` : ""}{plan.music ? ` · ${L(lang, "music", "música")}` : ""} · {plan.loudness.target_lufs} LUFS</p>
          <ol className="small">{plan.steps.map((s) => <li key={s}>{s}</li>)}</ol>
          {plan.commands && <details><summary className="small">{L(lang, "ffmpeg commands", "Comandos de ffmpeg")}</summary>
            <pre className="ep-commands">{plan.commands.join("\n\n")}</pre></details>}
          {plan.last_run && (
            <div className="row wrap" style={{ gap: 12, alignItems: "flex-start", marginTop: 10 }}>
              <div className="video-frame" style={{ width: plan.width > plan.height ? 420 : 220 }}>
                <video src={fileUrl(plan.last_run.asset_id)} controls preload="metadata" poster={`/generation/api/assets/${plan.last_run.asset_id}/thumb`} />
              </div>
              {plan.last_verify && <div className="grow"><VerifyReportCard report={plan.last_verify} /></div>}
            </div>
          )}
        </div>
      )}
      <div className="card">
        <h2><ShieldCheck size={16} /> {L(lang, "Verify a video", "Verificar un vídeo")}</h2>
        <div className="row wrap" style={{ gap: 8 }}>
          <select className="grow" value={verifyAsset} onChange={(e) => setVerifyAsset(e.target.value)} data-testid="ep-verify-asset"><option value="">—</option>
            {videos.map((a) => <option key={a.id} value={a.id}>{label(a)}</option>)}</select>
          <select value={verifyPlatform} onChange={(e) => setVerifyPlatform(e.target.value)}>{PLATFORMS.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
          <button className="btn" disabled={!verifyAsset || !!busy} onClick={verify} data-testid="ep-verify">
            {busy === "verify" ? <Loader2 size={14} className="spin" /> : <ShieldCheck size={14} />} {L(lang, "Verify", "Verificar")}</button>
        </div>
        {report && <VerifyReportCard report={report} />}
      </div>
      {(plans?.items || []).length > 0 && (
        <div className="card">
          <h2><Film size={16} /> {L(lang, "Plans", "Planes")}</h2>
          <label className="row small" style={{ gap: 4 }}><input type="checkbox" checked={trashOutputs} onChange={(e) => setTrashOutputs(e.target.checked)} data-testid="ep-delete-outputs" />
            {L(lang, "Deleting a plan also moves its rendered videos and contact sheets to the trash", "Al borrar un plan, sus vídeos renderizados y hojas de contactos van a la papelera")}</label>
          <ul className="ep-plans small">{plans!.items.map((p) => <li key={p.plan_id} data-testid="ep-plan-item">
            <button className="link-btn" onClick={() => api.editPlan(p.plan_id).then(setPlan)}>{p.name}</button>
            <span className="muted"> · {p.status} · {p.duration_s.toFixed(1)} s{p.platform ? ` · ${p.platform}` : ""}
              {p.last_run ? (p.last_run.verify_ok ? ` · ${L(lang, "spec ok", "cumple")}` : ` · ${L(lang, "spec failed", "no cumple")}`) : ""}</span>
            <button className="btn sm icon ghost danger" disabled={!!busy} onClick={() => removePlan(p)} data-testid="ep-plan-delete"
              aria-label={L(lang, `Delete plan ${p.name}`, `Borrar plan ${p.name}`)} title={L(lang, "Delete plan", "Borrar plan")}>
              {busy === `delete:${p.plan_id}` ? <Loader2 size={13} className="spin" /> : <Trash2 size={13} />}</button></li>)}</ul>
        </div>
      )}
    </div>
  );
}
