// Manim scenes: ManimGX code (or a description the local model turns into code) is checked without a video, rendered and
// saved as a video clip of the project, ready for the edit plans (Entrega) like any other clip.
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Download, Film, Loader2, Play, Search, Sigma, Wand2 } from "lucide-react";
import { api, fileUrl, type Asset, type ManimCheck, type ManimStatus } from "../api";
import { useT } from "../i18n";
import { useApp, useAsync, useTrackedJob } from "../components/ui";

const L = (lang: string, en: string, es: string) => (lang === "es" ? es : en);
const QUALITIES: [string, string, string][] = [
  ["draft", "Draft 1280x720 · 30 fps", "Borrador 1280x720 · 30 fps"],
  ["standard", "Standard 1920x1080 · 30 fps", "Estándar 1920x1080 · 30 fps"],
  ["high", "High 1920x1080 · 60 fps", "Alta 1920x1080 · 60 fps"],
];

export function ManimScenesView() {
  const { lang } = useT();
  const app = useApp();
  const projectId = app.projectId!;
  const { data: status, reload: reloadStatus } = useAsync<ManimStatus>(() => api.manimStatus(), []);
  const { data: examples } = useAsync(() => api.manimExamples(), []);
  const { data: assetPage, reload: reloadAssets } = useAsync(() => api.assets(projectId, { kind: "video", limit: 100 }), [projectId, app.dataVersion]);
  const [code, setCode] = useState("");
  const [description, setDescription] = useState("");
  const [name, setName] = useState("");
  const [quality, setQuality] = useState("standard");
  const [busy, setBusy] = useState<string | null>(null);
  const [report, setReport] = useState<ManimCheck | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [renderJobId, setRenderJobId] = useState<string | null>(null);
  const [installJobId, setInstallJobId] = useState<string | null>(null);
  const renderJob = useTrackedJob(renderJobId);
  const installJob = useTrackedJob(installJobId);
  const installed = !!status?.installed;
  const clips = (assetPage?.items || []).filter((a: Asset) => a.tags.includes("manim"));
  const finished = renderJob && ["done", "failed", "cancelled"].includes(renderJob.state) ? renderJob : null;
  const rendering = !!renderJob && !finished;
  const installing = !!installJob && !["done", "failed", "cancelled"].includes(installJob.state);

  useEffect(() => {
    if (!finished) return;
    if (finished.state === "done") { app.bump(); reloadAssets(); app.toast(L(lang, "Scene rendered", "Escena renderizada"), "ok"); setError(null); }
    else setError(finished.message || finished.state);
  }, [finished?.id, finished?.state]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (installJob?.state === "done") reloadStatus();
    else if (installJob?.state === "failed") setError(installJob.message || "install failed");
  }, [installJob?.state]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (what: string, fn: () => Promise<void>) => {
    setBusy(what); setError(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  };
  const install = () => act("install", async () => {
    const r = await api.manimEngine("install");
    if (r.job) { setInstallJobId(r.job.id); app.refreshJobs(); } else reloadStatus();
  });
  const check = () => act("check", async () => { setReport(await api.manimCheck({ code, project: projectId })); app.bump(); });
  const write = () => act("write", async () => {
    const r = await api.manimCheck({ description, project: projectId, language: lang === "es" ? "Spanish" : "English" });
    setCode(r.code); setReport(r); app.bump();
  });
  const render = () => act("render", async () => {
    const body = { code: code.trim() ? code : undefined, description: code.trim() ? undefined : description, name: name || undefined, quality,
      language: lang === "es" ? "Spanish" : "English" };
    const r = await api.manimRender(projectId, body);
    setRenderJobId(r.job.id); app.refreshJobs(); app.toast(L(lang, "Rendering the scene", "Renderizando la escena"), "info");
  });
  const result = finished?.state === "done" ? finished.outputs : null;
  const resultId = (result?.asset_id as string | undefined) || undefined;

  return (
    <div className="stack manim-scenes">
      <div className="card">
        <h2><Sigma size={17} /> {L(lang, "Manim scenes", "Escenas Manim")}
          {installed && <span className="pill ok" style={{ marginLeft: 8 }} data-testid="manim-version">ManimGX {status?.version}</span>}</h2>
        <p className="small muted">{L(lang,
          "2D maths and 3D animation drawn by code (ManimGX, Manim Community compatible). Write the scene, or describe it and the local model writes it; check it without rendering, then render it: the clip joins the project and works in any edit plan.",
          "Matemáticas en 2D y animación 3D dibujadas con código (ManimGX, compatible con Manim Community). Escribe la escena, o descríbela y el modelo local la escribe; compruébala sin renderizar y luego renderízala: el clip entra en el proyecto y sirve en cualquier plan de montaje.")}</p>
        {status && !installed && (
          <div className="row wrap" style={{ gap: 8 }} data-testid="manim-install">
            <span className="pill warn">{L(lang, "Engine not installed", "Motor sin instalar")}</span>
            <span className="small muted grow">{status.hint ? status.hint : ""}{" "}{L(lang, "It gets its own Python 3.13 environment (about 100 MB) and is never installed silently.", "Tiene su propio entorno de Python 3.13 (unos 100 MB) y nunca se instala solo.")}</span>
            <button className="btn primary" disabled={!!busy || installing || !status.python_found} onClick={install} data-testid="manim-install-btn">
              {installing || busy === "install" ? <Loader2 size={14} className="spin" /> : <Download size={14} />} {L(lang, "Install engine", "Instalar motor")}</button>
            {installJob && <span className="small muted">{installJob.state} · {Math.round((installJob.progress || 0) * 100)}% {installJob.message || ""}</span>}
          </div>
        )}
        <div className="row wrap" style={{ gap: 8, marginTop: 10 }}>
          <select aria-label={L(lang, "Examples", "Ejemplos")} value="" data-testid="manim-examples"
            onChange={(e) => { const ex = examples?.examples.find((x) => x.id === e.target.value); if (ex) { setCode(ex.code); setName(ex.title); setReport(null); } }}>
            <option value="">{L(lang, "Start from an example…", "Empezar desde un ejemplo…")}</option>
            {(examples?.examples || []).map((x) => <option key={x.id} value={x.id}>{x.kind.toUpperCase()} · {x.title}</option>)}
          </select>
          <input className="grow" value={name} onChange={(e) => setName(e.target.value)} placeholder={L(lang, "Clip name (optional)", "Nombre del clip (opcional)")} />
          <select value={quality} onChange={(e) => setQuality(e.target.value)} aria-label={L(lang, "Quality", "Calidad")}>
            {QUALITIES.map(([id, en, es]) => <option key={id} value={id}>{L(lang, en, es)}</option>)}</select>
        </div>
        <h3 className="small" style={{ marginTop: 12 }}>{L(lang, "Describe the scene (optional)", "Describe la escena (opcional)")}</h3>
        <div className="row wrap" style={{ gap: 8 }}>
          <input className="grow" value={description} onChange={(e) => setDescription(e.target.value)} data-testid="manim-description"
            placeholder={L(lang, "A cube rotating while the formula for its volume appears", "Un cubo que gira mientras aparece la fórmula de su volumen")} />
          <button className="btn" disabled={!!busy || rendering || !description.trim()} onClick={write} data-testid="manim-write"
            title={L(lang, "The local writing model writes the code and corrects it with the engine's errors", "El modelo de escritura local escribe el código y lo corrige con los errores del motor")}>
            {busy === "write" ? <Loader2 size={14} className="spin" /> : <Wand2 size={14} />} {L(lang, "Write with the model", "Escribir con el modelo")}</button>
        </div>
        <h3 className="small" style={{ marginTop: 12 }}>{L(lang, "Scene code", "Código de la escena")}</h3>
        <textarea className="manim-code" value={code} onChange={(e) => setCode(e.target.value)} spellCheck={false} data-testid="manim-code"
          placeholder={"import manimgx as m\n\n\nclass Escena(m.Scene):\n    def construct(self):\n        ..."} />
        <div className="row wrap" style={{ gap: 8, marginTop: 10 }}>
          <button className="btn" disabled={!!busy || rendering || !code.trim() || !installed} onClick={check} data-testid="manim-check">
            {busy === "check" ? <Loader2 size={14} className="spin" /> : <Search size={14} />} {L(lang, "Check without rendering", "Comprobar sin renderizar")}</button>
          <button className="btn primary" disabled={!!busy || rendering || !installed || (!code.trim() && !description.trim())} onClick={render} data-testid="manim-render">
            {rendering || busy === "render" ? <Loader2 size={14} className="spin" /> : <Play size={14} />} {L(lang, "Render clip", "Renderizar clip")}</button>
          {renderJob && !finished && <span className="small muted">{renderJob.state} · {Math.round((renderJob.progress || 0) * 100)}% {renderJob.message || ""}</span>}
        </div>
        {error && <div className="manim-error" role="alert" data-testid="manim-error"><AlertTriangle size={15} /> <span>{error}</span></div>}
      </div>
      {report && (
        <div className="card" data-testid="manim-report">
          <h2><CheckCircle2 size={16} className="wf-ok" /> {L(lang, "The scene runs", "La escena funciona")} <span className="pill">{report.scene}</span></h2>
          <p className="small muted">{report.duration_s?.toFixed(2)} s · {report.plays} {L(lang, "plays", "animaciones")} · {report.layout}
            {report.written_by_model ? ` · ${L(lang, `written by the model in ${report.attempts} try(ies)`, `escrita por el modelo en ${report.attempts} intento(s)`)}` : ""}</p>
          {report.layout_problems.length > 0 && <div className="manim-warn" data-testid="manim-layout"><strong><AlertTriangle size={14} /> {L(lang, "Layout warnings (it still renders)", "Avisos de composición (se renderiza igual)")}</strong>
            <ul className="small">{report.layout_problems.map((p) => <li key={p}>{p}</li>)}</ul></div>}
          <ol className="small manim-timeline">{report.timeline.map((s) => <li key={s.n}><span className="mono">{s.start_s.toFixed(2)}–{s.end_s.toFixed(2)} s</span> · {L(lang, "line", "línea")} {s.line} · {s.what}</li>)}</ol>
          {report.storyboard_asset_id && <img className="tr-sheet" src={fileUrl(report.storyboard_asset_id)} alt={L(lang, "Storyboard", "Guion gráfico")} onClick={() => app.openAsset(report.storyboard_asset_id!)} />}
        </div>
      )}
      {resultId && (
        <div className="card" data-testid="manim-result">
          <h2><Film size={16} /> {L(lang, "Rendered clip", "Clip renderizado")}</h2>
          <div className="video-frame" style={{ width: 480, maxWidth: "100%" }}>
            <video src={fileUrl(resultId)} controls preload="metadata" poster={`/generation/api/assets/${resultId}/thumb`} data-testid="manim-video" />
          </div>
          <p className="small muted">{Number(result?.duration_s || 0).toFixed(2)} s · {String(result?.width)}x{String(result?.height)} · {String(result?.fps)} fps</p>
          <button className="btn sm" onClick={() => app.go("delivery")}>{L(lang, "Use it in an edit plan", "Usar en un plan de montaje")}</button>
        </div>
      )}
      {clips.length > 0 && (
        <div className="card">
          <h2><Film size={16} /> {L(lang, "Scenes of this project", "Escenas de este proyecto")}</h2>
          <div className="manim-clips">{clips.map((a) => (
            <div key={a.id} className="manim-clip" data-testid="manim-clip">
              <video src={fileUrl(a.id)} controls preload="metadata" poster={`/generation/api/assets/${a.id}/thumb`} />
              <div className="small">{a.name} · {a.duration_s?.toFixed(1)} s</div>
              <button className="btn sm ghost" onClick={() => { const c = (a.recipe as { code?: string } | null)?.code; if (c) { setCode(c); setName((a.name || "").replace(/\.mp4$/, "")); setReport(null); window.scrollTo({ top: 0 }); } }}
                disabled={!(a.recipe as { code?: string } | null)?.code}>{L(lang, "Load its code", "Cargar su código")}</button>
            </div>))}</div>
        </div>
      )}
    </div>
  );
}
