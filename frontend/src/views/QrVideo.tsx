import { useEffect, useState } from "react";
import { Download, Film, Plus, ScanLine, Trash2 } from "lucide-react";
import { api, fileUrl, type Asset, type QrReport, type QrScene } from "../api";
import { useT } from "../i18n";
import { JobState, Progress, useApp, useAsync, useSessionState, useTrackedJob } from "../components/ui";
import "./qr-video.css";

export function QrVideoView() {
  const { lang } = useT();
  const es = lang === "es";
  const app = useApp();
  const project = app.projectId!;
  const [scenes, setScenes] = useState<QrScene[]>([{ payload: "", caption: "", duration_s: 3 }]);
  const [name, setName] = useState("");
  const [aspect, setAspect] = useState("9:16");
  const [fps, setFps] = useState(24);
  const [motion, setMotion] = useState("pulse");
  const [jobId, setJobId] = useSessionState(`qr-job-${project}`, null);
  const [assetId, setAssetId] = useState("");
  const [asset, setAsset] = useState<Asset | null>(null);
  const [report, setReport] = useState<QrReport | null>(null);
  const [reportJobId, setReportJobId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const job = useTrackedJob(jobId);
  const active = busy || Boolean(jobId && (!job || ["running", "queued", "waiting_gpu"].includes(job.state)));
  const { data: videos, reload } = useAsync(() => api.assets(project, { kind: "video", limit: 100 }), [project, app.dataVersion]);
  const duration = scenes.reduce((total, scene) => total + Math.max(1, Math.round((scene.duration_s || 0) * fps)), 0) / fps;

  useEffect(() => {
    if (!assetId) { setAsset(null); return; }
    let current = true;
    api.asset(assetId).then(value => { if (current) setAsset(value); }).catch(e => { if (current) setError(e.message); });
    return () => { current = false; };
  }, [assetId]);

  useEffect(() => {
    if (!job || !["done", "failed", "cancelled"].includes(job.state)) return;
    let current = true;
    if (job.state === "done") {
      const result = job.outputs;
      if (typeof result?.asset_id === "string") setAssetId(result.asset_id);
      reload();
    }
    if (job.state === "failed") setError(job.message || (es ? "El trabajo ha fallado" : "The job failed"));
    if (job.state !== "cancelled") api.qrReport(job.id).then(value => {
      if (current) { setReport(value); setReportJobId(job.id); }
    }).catch(e => { if (current && job.state === "done") setError(e.message); });
    return () => { current = false; };
  }, [job?.id, job?.state]);

  const update = (index: number, patch: Partial<QrScene>) => setScenes(scenes.map((scene, i) => i === index ? { ...scene, ...patch } : scene));
  const run = async (verify = false) => {
    setBusy(true); setError(""); setReport(null); setReportJobId(null);
    if (!verify) setAssetId("");
    try {
      const result = verify ? await api.qrVerify(assetId) : await api.qrVideo({ project, scenes, name, aspect, fps, motion });
      setJobId(result.job.id); app.refreshJobs();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  return <div className="qr-film stack">
    <header><h1>{es ? "Corto QR" : "QR film"}</h1>
      <p className="muted">{es ? "Texto o enlace por escena. El archivo final se comprueba fotograma a fotograma." : "A text or link for each scene. The final file is checked frame by frame."}</p></header>
    <div className="qr-workspace">
      <form className="stack" onSubmit={e => { e.preventDefault(); void run(); }}>
        <label>{es ? "Nombre" : "Name"}<input value={name} onChange={e => setName(e.target.value)} placeholder={es ? "Corto QR" : "QR film"} /></label>
        <fieldset disabled={active} className="qr-scenes stack"><legend>{es ? "Escenas" : "Scenes"}</legend>
          {scenes.map((scene, index) => <section className="qr-scene stack" key={index}>
            <div className="row"><h2>{es ? "Escena" : "Scene"} {index + 1}</h2>
              <button type="button" className="btn ghost" disabled={scenes.length === 1} aria-label={`${es ? "Quitar escena" : "Remove scene"} ${index + 1}`}
                onClick={() => setScenes(scenes.filter((_, i) => i !== index))}><Trash2 size={16} /></button></div>
            <label>{es ? "Contenido del QR" : "QR payload"}<textarea required rows={2} value={scene.payload}
              onChange={e => update(index, { payload: e.target.value })} placeholder={es ? "Texto o URL" : "Text or URL"} /></label>
            <div className="qr-scene-fields"><label>{es ? "Texto visible" : "Visible caption"}<input value={scene.caption} onChange={e => update(index, { caption: e.target.value })} /></label>
              <label>{es ? "Segundos" : "Seconds"}<input type="number" min={1 / fps} step="any" required value={scene.duration_s}
                onChange={e => update(index, { duration_s: Number(e.target.value) })} /></label></div>
          </section>)}
          <button type="button" className="btn" onClick={() => setScenes([...scenes, { payload: "", caption: "", duration_s: 3 }])}><Plus size={16} />{es ? "Añadir escena" : "Add scene"}</button>
        </fieldset>
        <div className="qr-settings">
          <label>{es ? "Formato" : "Aspect"}<select value={aspect} disabled={active} onChange={e => setAspect(e.target.value)}>{["9:16", "16:9", "1:1"].map(v => <option key={v}>{v}</option>)}</select></label>
          <label>FPS<input type="number" min={1} max={60} required value={fps} disabled={active} onChange={e => setFps(Number(e.target.value))} /></label>
          <label>{es ? "Movimiento" : "Motion"}<select value={motion} disabled={active} onChange={e => setMotion(e.target.value)}>
            <option value="pulse">{es ? "Pulso" : "Pulse"}</option><option value="orbit">{es ? "Órbita" : "Orbit"}</option><option value="still">{es ? "Fijo" : "Still"}</option></select></label>
        </div>
        <div className="row wrap"><button className="btn primary" disabled={active}><Film size={16} />{es ? "Crear y comprobar" : "Create and verify"}</button>
          <span className="muted">{Number.isFinite(duration) ? duration.toFixed(2) : "0"} s · {Math.round(duration * fps) || 0} {es ? "fotogramas" : "frames"}</span></div>
      </form>
      <section className="qr-output stack" aria-label={es ? "Resultado" : "Output"}>
        {asset ? <video key={asset.id} src={fileUrl(asset.id)} controls playsInline aria-label={asset.name || "QR film"} /> : <div className="qr-monitor-empty"><ScanLine size={48} /><p>{es ? "El vídeo aparecerá aquí" : "Your video will appear here"}</p></div>}
        {job && <div className="stack" role="status">{(active || !report) && <JobState state={job.state} />}{active && <Progress value={job.progress} />}
          {job.message && !["done", "cancelled", "failed"].includes(job.message) && <span>{job.message}</span>}
          {active && <button type="button" className="btn" onClick={() => void api.cancelJob(job.id).then(app.refreshJobs).catch(e => setError(e.message))}>{es ? "Cancelar" : "Cancel"}</button>}</div>}
        {error && <p className="error" role="alert">{error}</p>}
        {report && <div className="qr-report stack" role="status">
          <h2>{report.verified ? (es ? "Todos los fotogramas se decodifican" : "Every frame decodes") : (es ? "Comprobación con fallos" : "Verification found failures")}</h2>
          <p>{report.checked_frames}/{report.expected_frames} {es ? "fotogramas comprobados" : "frames checked"} · {report.undecodable_frames} {es ? "sin lectura" : "unreadable"} · {report.mismatched_frames} {es ? "con contenido distinto" : "payload mismatches"}</p>
          <details><summary>{es ? "Contenido leído" : "Decoded payloads"}</summary>{report.payloads.map((text, i) => <p className="qr-payload" key={i}>{text}</p>)}</details>
          <a href={`/generation/api/qr/reports/${reportJobId}`} download><Download size={15} /> {es ? "Informe por fotograma (JSON)" : "Per-frame report (JSON)"}</a>
        </div>}
        {asset && <a href={`${fileUrl(asset.id)}?download=true`} download><Download size={15} /> {es ? "Descargar vídeo" : "Download video"}</a>}
        <label>{es ? "Comprobar un vídeo del proyecto" : "Verify a project video"}<select value={assetId} disabled={active} onChange={e => { setAssetId(e.target.value); setReport(null); setReportJobId(null); }}>
          <option value="">{es ? "Elegir vídeo" : "Choose video"}</option>{videos?.items.map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></label>
        <button type="button" className="btn" disabled={active || !assetId} onClick={() => void run(true)}><ScanLine size={16} />{es ? "Comprobar todos los fotogramas" : "Verify every frame"}</button>
      </section>
    </div>
  </div>;
}
