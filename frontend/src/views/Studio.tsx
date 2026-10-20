import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { ArrowRight, Download, Film, ImagePlus, Maximize2, Plus, Upload, X, Columns2 } from "lucide-react";
import { api, fileUrl, thumbUrl, type Asset, type Job } from "../api";
import { useT } from "../i18n";
import { AssetPicker, AssetTile, Progress, useApp, useAsync } from "../components/ui";
import { GenerateView } from "./Generate";
const ClipCanvas = lazy(() => import("../components/ClipCanvas").then(module => ({ default: module.ClipCanvas })));

const CAMERAS = [
  ["fixed", "Cámara fija", "Locked-off camera, stable framing."],
  ["track", "Seguimiento lateral", "The camera tracks sideways alongside the subject."],
  ["push", "Acercamiento", "Smooth dolly push-in toward the subject."],
  ["orbit", "Órbita", "The camera travels in a smooth arc around the subject."],
  ["pull", "Alejarse", "The camera pulls back smoothly, revealing the surroundings."],
];
const CAMERA_EN = ["Locked camera", "Side tracking", "Dolly in", "Orbit", "Dolly out"];
function savedDraft(key: string): Record<string, unknown> {
  try { return JSON.parse(sessionStorage.getItem(key) || "{}"); } catch { return {}; }
}

export function StudioView() {
  const app = useApp();
  const { lang, t } = useT();
  const [mode, setMode] = useState<"image" | "video">(() => app.route.arg ? "video" : sessionStorage.getItem("prospero.studio-mode") === "image" ? "image" : "video");
  const [start, setStart] = useState<Asset | null>(null);
  useEffect(() => {
    const id = app.route.arg;
    if (!id || id === "clip") return;
    let alive = true;
    api.asset(id).then(asset => { if (alive && asset.kind === "image") { setStart(asset); setMode("video"); } })
      .catch(error => app.toast((error as Error).message, "bad"));
    return () => { alive = false; };
  }, [app.route.arg]);
  useEffect(() => { sessionStorage.setItem("prospero.studio-mode", mode); }, [mode]);
  return <>
    <div className="creation-tabs" aria-label={lang === "es" ? "Tipo de creación" : "Creation type"}>
      <button className={mode === "image" ? "on" : ""} onClick={() => setMode("image")}><ImagePlus size={17} />{lang === "es" ? "Imagen" : "Image"}</button>
      <button className={mode === "video" ? "on" : ""} onClick={() => setMode("video")}><Film size={17} />{lang === "es" ? "Vídeo" : "Video"}</button>
      <button onClick={() => app.go("audio")}>{t("navAudio")}</button>
      <button onClick={() => app.go("voice")}>{t("navVoice")}</button>
      <span className="spacer" />
      <button onClick={() => app.go("spaces")}>{lang === "es" ? "Spaces · Crear con nodos" : "Spaces · Create with nodes"}<ArrowRight size={15} /></button>
    </div>
    {mode === "image" ? <GenerateView onAnimate={(asset) => { setStart(asset); setMode("video"); }} />
      : <VideoStudio key={app.route.arg || "free"} initial={start} onImage={() => setMode("image")} />}
  </>;
}

function VideoStudio({ initial, onImage }: { initial: Asset | null; onImage: () => void }) {
  const app = useApp();
  const { lang, t } = useT();
  const es = lang === "es";
  const pid = app.projectId!;
  const draftKey = `prospero.video-draft.${pid}.${app.route.arg || "free"}`;
  const [draft] = useState(() => Object.keys(savedDraft(draftKey)).length ? savedDraft(draftKey) : !app.route.arg ? savedDraft(`prospero.video-draft.${pid}`) : {});
  useEffect(() => { if (initial) setStart(initial); }, [initial]);
  const [hydrated, setHydrated] = useState(false);
  const [start, setStart] = useState<Asset | null>(initial);
  const [end, setEnd] = useState<Asset | null>(null);
  const [drive, setDrive] = useState<Asset | null>(null);
  const [picker, setPicker] = useState<"start" | "end" | "drive" | null>(null);
  const [motion, setMotion] = useState(() => sessionStorage.getItem(`prospero.motion.${pid}`) || "");
  const [camera, setCamera] = useState("fixed");
  const [seconds, setSeconds] = useState(5);
  const [driveStart, setDriveStart] = useState(0);
  const [engine, setEngine] = useState("auto");
  const [seed, setSeed] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [jobIds, setJobIds] = useState<string[]>([]);
  const [completed, setCompleted] = useState<Asset | null>(null);
  const [completedJob, setCompletedJob] = useState<string | null>(typeof draft.completedJob === "string" ? draft.completedJob : null);
  const [latestJob, setLatestJob] = useState<Job | null>(null);
  const [recoveringJob, setRecoveringJob] = useState(() => Array.isArray(draft.jobs) && draft.jobs.some(id => typeof id === "string"));
  const [jobReadError, setJobReadError] = useState("");
  const [jobRetry, setJobRetry] = useState(0);
  const [guideSlots, setGuideSlots] = useState({ drive: false, end: false });
  const [selected, setSelected] = useState<string | null>(null);
  const [compare, setCompare] = useState(false);
  const videoRefs = useRef<(HTMLVideoElement | null)[]>([]);
  const composerRef = useRef<HTMLElement>(null);
  const [playing, setPlaying] = useState(false);
  useEffect(() => {
    let alive = true;
    Promise.all(["start", "end", "drive"].map((slot) => typeof draft[slot] === "string" ? api.asset(draft[slot] as string).catch(() => null) : Promise.resolve(null)))
      .then(([savedStart, savedEnd, savedDrive]) => {
        if (!alive) return;
        setStart(current => initial || current || savedStart); setEnd(savedEnd); setDrive(savedDrive);
        if (typeof draft.camera === "string" && CAMERAS.some((c) => c[0] === draft.camera)) setCamera(draft.camera);
        if ([3, 4, 5].includes(Number(draft.seconds))) setSeconds(Number(draft.seconds));
        if (["auto", "wan14b", "wan", "svd"].includes(String(draft.engine))) setEngine(String(draft.engine));
        if (typeof draft.seed === "string") setSeed(draft.seed);
        if (typeof draft.driveStart === "number") setDriveStart(draft.driveStart);
        if (Array.isArray(draft.jobs)) setJobIds(draft.jobs.filter((id): id is string => typeof id === "string"));
        if (typeof draft.selected === "string") setSelected(draft.selected);
        setHydrated(true);
      });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    if (!hydrated) return;
    try { sessionStorage.setItem(draftKey, JSON.stringify({ start: start?.id, end: end?.id, drive: drive?.id, camera, seconds, engine, seed, driveStart, jobs: jobIds, selected, completedJob })); } catch { /* storage unavailable */ }
  }, [hydrated, start, end, drive, camera, seconds, engine, seed, driveStart, jobIds, selected, completedJob, draftKey]);
  const recent = useAsync(() => api.assets(pid, { kind: "video", source: "generated", limit: 18 }), [pid, app.dataVersion]);
  const items = completed ? [completed, ...(recent.data?.items || []).filter(asset => asset.id !== completed.id)] : recent.data?.items || [];
  const lastJobId = jobIds[0];
  useEffect(() => {
    if (!lastJobId) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const job = await api.job(lastJobId);
        if (!alive) return;
        setLatestJob(job); setRecoveringJob(false); setJobReadError("");
        if (job.state === "done") {
          const id = job.outputs?.asset_ids?.[0] || job.outputs?.asset_id;
          if (id) { const asset = await api.asset(id); if (alive) { setCompleted(asset); if (draft.completedJob !== job.id || !draft.selected) setSelected(asset.id); setCompletedJob(job.id); } }
          app.bump();
        } else if (!["failed", "cancelled"].includes(job.state)) timer = setTimeout(poll, 1800);
      } catch (err) { if (alive) { setJobReadError((err as Error).message); timer = setTimeout(poll, 5000); } }
    };
    poll();
    return () => { alive = false; clearTimeout(timer); };
  }, [lastJobId, jobRetry]);
  const inFlight = !!latestJob && ["queued", "waiting_gpu", "running"].includes(latestJob.state);
  const jobLabel = latestJob ? ({ queued: es ? "En cola" : "Queued", waiting_gpu: es ? "Esperando GPU" : "Waiting for GPU", running: es ? "Generando clip" : "Generating clip", done: latestJob.outputs?.asset_ids?.[0] || latestJob.outputs?.asset_id ? (es ? "Clip generado" : "Clip generated") : (es ? "Terminado sin resultado" : "Finished without output"), failed: es ? "La generación ha fallado" : "Generation failed", cancelled: es ? "Cancelado" : "Cancelled" })[latestJob.state] : "";
  const take = items.find((a) => a.id === selected) || null;
  const comparison = take ? [take, ...items.filter((a) => a.id !== take.id)].slice(0, 2) : [];
  const takeLabel = (asset: Asset) => `${es ? "Toma" : "Take"} ${String(items.length - items.findIndex((a) => a.id === asset.id)).padStart(2, "0")}${asset.duration_s ? ` · ${asset.duration_s.toFixed(1)} s` : ""}`;
  useEffect(() => { sessionStorage.setItem(`prospero.motion.${pid}`, motion); }, [motion, pid]);
  useEffect(() => { setPlaying(false); videoRefs.current.forEach((video) => video?.pause()); }, [compare, selected]);
  const choose = (asset: Asset) => { if (picker === "start") setStart(asset); else if (picker === "end") setEnd(asset); else setDrive(asset); setPicker(null); };
  const upload = async (file: File, slot: "start" | "end" | "drive") => {
    setBusy(true); setError("");
    try {
      const asset = await api.upload(pid, file);
      if (asset.kind !== (slot === "drive" ? "video" : "image")) throw new Error(es ? "Elige un archivo del tipo indicado." : "Choose the indicated media type.");
      if (slot === "start") setStart(asset); else if (slot === "end") setEnd(asset); else setDrive(asset);
      app.bump();
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  const queue = async () => {
    if (!hydrated || recoveringJob || !start || !motion.trim() || busy || inFlight) return;
    setBusy(true); setError("");
    try {
      const prompt = [motion.trim(), CAMERAS.find((c) => c[0] === camera)?.[2], "Preserve the subject's identity, clothing and background throughout the shot."].join(" ");
      if (end && drive) throw new Error(es ? "Elige fotograma final o vídeo de movimiento para esta toma." : "Choose either an end frame or a motion video for this take.");
      const body = { project_id: pid, prompt, seconds, engine: drive ? "animate" : engine, driving_asset_id: drive?.id, driving_start_s: driveStart,
        ...(engine === "svd" && !drive && !end ? { frames: seconds === 5 ? 25 : 24, fps: seconds === 5 ? 5 : 24 / seconds } : {}),
        pose_prompt: motion, seed: seed.trim() ? Number(seed) : undefined };
      const result = end && !drive
        ? await api.generate(pid, { prompt, template: "wan22_flf2v", reference_asset_id: start.id, end_asset_id: end.id,
            template_params: { seconds }, seed: body.seed, count: 1 })
        : await api.animate(start.id, body);
      setLatestJob(result.job); setCompleted(null); setJobReadError("");
      setJobIds((ids) => [result.job.id, ...ids].slice(0, 24));
      app.refreshJobs();
      app.toast(es ? "Toma en cola. Puedes seguir preparando referencias." : "Take queued. You can keep preparing references.", "ok");
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  const slot = (key: "start" | "end" | "drive", asset: Asset | null, label: string) => (
    <div className={`video-reference ${asset ? "filled" : ""}`} onDragOver={(e) => e.preventDefault()} onDrop={async (e) => {
      e.preventDefault();
      const id = e.dataTransfer.getData("text/prospero-asset");
      if (id) { try { const a = await api.asset(id); if (a.kind !== (key === "drive" ? "video" : "image")) return;
        if (key === "start") setStart(a); else if (key === "end") setEnd(a); else setDrive(a); } catch (err) { setError((err as Error).message); } }
      else if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0], key);
    }}>
      <span className="field-caption">{label}</span>
      {asset ? <><button className="reference-preview" onClick={() => app.openAsset(asset.id)} aria-label={es ? "Ampliar referencia" : "Enlarge reference"}>
        <img src={thumbUrl(asset)} alt={asset.name || label} /><span><Maximize2 size={14} />{es ? "Ampliar" : "Enlarge"}</span></button>
        <div className="row"><span className="ellipsis grow small">{asset.name}</span><button className="btn icon ghost" aria-label={es ? "Quitar referencia" : "Remove reference"} onClick={() => { if (key === "start") setStart(null); else if (key === "end") setEnd(null); else setDrive(null); }}><X size={15} /></button></div></>
        : <button className="reference-add" onClick={() => setPicker(key)}><Plus size={23} />{es ? "Elegir de la biblioteca" : "Choose from library"}</button>}
      <div className="row"><button className="btn sm ghost" onClick={() => setPicker(key)}>{es ? "Cambiar" : "Choose"}</button>
        <label className="btn sm ghost upload-label"><Upload size={14} />{es ? "Subir" : "Upload"}<input type="file" hidden accept={key === "drive" ? "video/*" : "image/*"} onChange={(e) => { if (e.target.files?.[0]) upload(e.target.files[0], key); e.target.value = ""; }} /></label></div>
    </div>
  );
  const recoveryPanel = (recoveringJob || jobReadError) && <div className="clip-run-state" role="status" aria-live="polite">
    <strong>{recoveringJob ? (es ? "Recuperando el estado del trabajo…" : "Recovering job status…") : (es ? "No se pudo actualizar el trabajo" : "Could not update job status")}</strong>
    {jobReadError && <><span>{jobReadError}</span><button className="btn sm" onClick={() => setJobRetry(value => value + 1)}>{es ? "Reintentar estado" : "Retry status"}</button></>}
  </div>;
  const statusPanel = latestJob && <div className={`clip-run-state ${latestJob.state}`} role="status" aria-live="polite">
    <strong>{jobLabel}{latestJob.state === "running" ? ` · ${Math.round(latestJob.progress * 100)}%` : ""}</strong>
    {inFlight && <Progress value={latestJob.progress} waiting={latestJob.state !== "running"} />}
    <span>{latestJob.state === "failed" ? latestJob.message || latestJob.log_excerpt : latestJob.message === "done" ? "" : latestJob.message}</span>
    {inFlight && <button className="btn sm" onClick={async () => { try { await api.cancelJob(latestJob.id); app.refreshJobs(); } catch (err) { setError((err as Error).message); } }}>{es ? "Cancelar generación" : "Cancel generation"}</button>}
    {latestJob.state === "done" && !completed && <span>{latestJob.outputs?.asset_ids?.[0] || latestJob.outputs?.asset_id ? (es ? "Cargando el resultado…" : "Loading the result…") : (es ? "El trabajo terminó sin devolver un vídeo. Abre el trabajo para revisar el motivo." : "The job finished without returning a video. Open the job to inspect the reason.")}</span>}
    {["failed", "cancelled"].includes(latestJob.state) && <span>{es ? "Puedes ajustar el prompt y volver a generar desde este nodo." : "Adjust the prompt and generate again from this node."}</span>}
    {inFlight && take && <span className="hint">{es ? "La vista previa muestra una toma anterior hasta que termine este trabajo." : "The preview shows a previous take until this job finishes."}</span>}
  </div>;
  const preview = <>      <div className="monitor-toolbar"><strong title={take?.name || undefined}>{take ? takeLabel(take) : (es ? "Previsualización" : "Preview")}</strong><span className="spacer" />
        {take && <><span className="pill selected-take-label">{es ? "Seleccionada" : "Selected"}</span><button className="btn sm" aria-pressed={compare} disabled={items.length < 2} onClick={() => setCompare(!compare)}><Columns2 size={15} />{t("compare")}</button>
          <button className="btn sm" onClick={() => app.openAsset(take.id, items.map((a) => a.id))}><Maximize2 size={15} />{es ? "Ampliar" : "Enlarge"}</button>
          <a className="btn sm" href={`${fileUrl(take.id)}?download=true`}><Download size={15} />{t("download")}</a></>}
      </div>
      <div className={`clip-monitor${compare ? " comparing" : ""}`}>
        {compare && comparison.length === 2 ? comparison.map((asset, i) => <div key={asset.id}><video ref={(el) => { videoRefs.current[i] = el; }} src={fileUrl(asset.id)} muted playsInline preload="metadata" poster={thumbUrl(asset)} onEnded={() => { videoRefs.current.forEach((v) => v?.pause()); setPlaying(false); }} /><span className="monitor-caption" title={asset.name || undefined}>{takeLabel(asset)}</span></div>)
          : take ? <video key={take.id} src={fileUrl(take.id)} controls playsInline preload="metadata" poster={thumbUrl(take)} />
          : start ? <button className="monitor-image" onClick={() => app.openAsset(start.id)}><img src={fileUrl(start.id)} alt={start.name || ""} /><span><Maximize2 size={16} />{es ? "Ampliar imagen" : "Enlarge image"}</span></button>
          : <div className="monitor-empty"><Film size={38} /><h2>{es ? "Aquí aparecerá tu clip" : "Your clip will appear here"}</h2><p>{es ? "Elige una referencia y describe su movimiento. Aquí podrás ver, ampliar y comparar los resultados." : "Choose a reference and describe its motion. View, enlarge and compare the results here."}</p><button className="btn" onClick={() => setPicker("start")}><ImagePlus size={17} />{es ? "Elegir imagen de partida" : "Choose a start image"}</button></div>}
      </div>
      {compare && comparison.length === 2 && <div className="row"><button className="btn" onClick={async () => {
        const videos = videoRefs.current.filter((v): v is HTMLVideoElement => !!v);
        if (playing) { videos.forEach((v) => v.pause()); setPlaying(false); }
        else { videos.forEach((v) => { v.currentTime = 0; v.loop = false; }); const results = await Promise.allSettled(videos.map((v) => v.play()));
          if (results.some((r) => r.status === "rejected")) { videos.forEach((v) => v.pause()); app.toast(es ? "No se pudo reproducir una toma. Ábrela para revisarla." : "A take could not play. Open it to review.", "bad"); } else setPlaying(true); }
      }}>{playing ? (es ? "Pausar ambas" : "Pause both") : (es ? "Reproducir ambas desde el inicio" : "Play both from start")}</button><span className="hint">{es ? "Comparación sin audio" : "Muted comparison"}</span></div>}
</>;
  const composer = <section className="node-composer" ref={composerRef}>
      
      
      <label className="field">{es ? "¿Qué hace el sujeto?" : "What does the subject do?"}
        <textarea rows={4} value={motion} placeholder={es ? "Da dos pasos, gira y abre los brazos. El abrigo se mueve con el giro…" : "Takes two steps, turns and opens their arms. The coat moves with the turn…"} onChange={(e) => setMotion(e.target.value)} /></label>
      <button className="btn primary generate-clip" disabled={!hydrated || recoveringJob || !start || !motion.trim() || busy || inFlight || (!!end && !!drive)} onClick={queue}><Film size={17} />{recoveringJob ? (es ? "Recuperando…" : "Recovering…") : busy ? (es ? "Preparando…" : "Preparing…") : (inFlight ? jobLabel : (es ? "Generar clip" : "Generate clip"))}<ArrowRight size={17} /></button>
      <div className="motion-suggestions">{[["Bailar", "Dance", "The subject takes two quick steps, spins fully and opens both arms; the clothing responds naturally to the continuous movement."], ["Caminar", "Walk", "The subject walks forward continuously, taking several natural steps, with visible arm and leg movement."]].map(([esLabel, enLabel, text]) =>
        <button key={enLabel} className="btn xs" onClick={() => setMotion(text)}>{es ? esLabel : enLabel}</button>)}</div>
      <div className="shot-controls"><label className="field">{es ? "Cámara" : "Camera"}<select value={camera} onChange={(e) => setCamera(e.target.value)}>{CAMERAS.map((c, i) => <option key={c[0]} value={c[0]}>{es ? c[1] : CAMERA_EN[i]}</option>)}</select></label>
      <label className="field">{t("duration")}<select value={seconds} onChange={(e) => setSeconds(Number(e.target.value))}>{[3, 4, 5].map((n) => <option key={n} value={n}>{n} s</option>)}</select></label></div>
      <details className="studio-details"><summary>{t("advanced")}</summary>
        <label className="field">{es ? "Motor" : "Engine"}<select value={drive ? "animate" : end ? "flf" : engine} disabled={!!drive || !!end} onChange={(e) => setEngine(e.target.value)}>
          <option value="animate" disabled={!drive}>Wan Animate · vídeo de movimiento</option><option value="flf" disabled={!end}>Wan 14B · primer / último fotograma</option>
          <option value="auto">Auto · Wan 14B / Animate</option><option value="wan14b">Wan 2.2 14B</option><option value="wan">Wan 2.2 5B</option><option value="svd">SVD</option></select></label>
        {engine === "svd" && !drive && !end && <p className="hint">{es ? "SVD genera hasta 25 fotogramas: 5–8 fps para estas duraciones. Para movimiento fluido, usa Wan." : "SVD generates up to 25 frames: 5–8 fps at these durations. Use Wan for fluid motion."}</p>}
        <label className="field">{t("seed")}<input value={seed} type="number" min={0} onChange={(e) => setSeed(e.target.value)} placeholder={es ? "Aleatoria" : "Random"} /></label>
      </details>
      {error && <div className="studio-error" role="alert">{error}<button className="btn sm" onClick={() => app.go("backends")}>{es ? "Revisar motores" : "Check engines"}</button></div>}
      {end && drive && <p className="studio-error" role="alert">{es ? "Usa una sola guía: quita el vídeo de movimiento o el fotograma final para generar." : "Use one guide: remove either the motion video or the end frame to generate."}</p>}
      {!start && <p className="hint">{es ? "Empieza con una imagen. No necesitas un guion ni una producción." : "Start with an image. No script or production required."}</p>}
    </section>;
  return <div className="node-video-studio">
    <div className="clip-studio-heading"><h1>{es ? "Estudio de clips" : "Clip studio"}</h1>
      <span className="hint">{es ? "Referencias → vídeo. Arrastra los nodos para organizar el lienzo." : "References → video. Drag nodes to arrange the canvas."}</span>
      <button className="btn sm" onClick={() => app.go("overview")}>{es ? "Volver al proyecto" : "Back to project"}</button></div>
    <Suspense fallback={<p>{t("loading")}</p>}><ClipCanvas label={es ? "Lienzo de generación de clips" : "Clip generation canvas"}
      reference={<><h2>{es ? "Referencia" : "Reference"}</h2>{slot("start", start, es ? "Imagen de partida" : "Start image")}<div className="clip-add-guides"><button className="btn sm ghost" onClick={onImage}>{es ? "Crear la imagen con varias referencias" : "Create an image with multiple references"}<ArrowRight size={14} /></button><button className="btn sm" onClick={() => setGuideSlots(slots => ({ ...slots, drive: true }))}><Plus size={14} />{es ? "Guía de movimiento" : "Motion guide"}</button><button className="btn sm" onClick={() => setGuideSlots(slots => ({ ...slots, end: true }))}><Plus size={14} />{es ? "Fotograma final" : "End frame"}</button></div></>}
      guides={[
        ...(drive || guideSlots.drive ? [{ id: "motion-guide", content: <><h2>{es ? "Movimiento" : "Motion"}</h2>{slot("drive", drive, es ? "Vídeo de movimiento" : "Motion video")}<label className="field clip-guide-start">{es ? "Desde el segundo" : "Start at second"}<input type="number" min={0} step={0.1} value={driveStart} onChange={event => setDriveStart(Number(event.target.value))} /></label></> }] : []),
        ...(end || guideSlots.end ? [{ id: "end-frame", content: <><h2>{es ? "Fotograma final" : "End frame"}</h2>{slot("end", end, es ? "Imagen de llegada" : "End image")}</> }] : []),
      ]}
        video={<><div className="clip-node-title"><Film size={17} /><strong>{es ? "Vídeo" : "Video"}</strong><span>{drive ? "Wan Animate" : end ? "Wan 14B · FLF" : engine === "wan" ? "Wan 5B" : engine === "svd" ? "SVD" : "Wan 14B"}</span></div>{recoveryPanel}{statusPanel}{preview}{composer}</>} /></Suspense>
    <section className="take-desk clip-take-history">
      {recent.error && <div className="studio-error" role="alert">{recent.error}<button className="btn" onClick={recent.reload}>{es ? "Reintentar" : "Retry"}</button></div>}
      {items.length > 0 && <><h2 className="take-heading">{es ? "Tomas del proyecto" : "Project takes"}<span className="muted small">{items.length}</span></h2>
        <div className="take-filmstrip">{items.map((asset) => <div key={asset.id} className={asset.id === take?.id ? "take-option selected" : "take-option"}>
          <div className="take-identity"><span>{takeLabel(asset)}</span>{asset.id === take?.id && <span>{es ? "Seleccionada" : "Selected"}</span>}</div>
          <AssetTile asset={asset} selected={asset.id === take?.id} onClick={() => app.openAsset(asset.id, items.map((a) => a.id))} />
          <button className="btn sm" aria-pressed={asset.id === take?.id} aria-label={`${es ? "Ver en el monitor" : "View in monitor"}: ${takeLabel(asset)}`} onClick={() => { setSelected(asset.id); setCompare(false); }}>{asset.id === take?.id ? (es ? "En el monitor" : "In monitor") : (es ? "Ver en el monitor" : "View in monitor")}</button></div>)}</div></>}
    </section>
    {picker && <AssetPicker projectId={pid} kind={picker === "drive" ? "video" : "image"} title={es ? "Elegir referencia" : "Choose reference"} onPick={choose} onClose={() => setPicker(null)} allProjects />}
  </div>;
}
