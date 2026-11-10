import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../App.jsx";
import { api as appApi } from "../api.js";
import { useCopilotForm, useCopilotRefresh } from "../copilot-hooks.js";
import { Spinner } from "./ui.jsx";
import HtmlMotionSound from "./HtmlMotionSound.jsx";
import HtmlMotionVariants from "./HtmlMotionVariants.jsx";
import HtmlMotionDirection, { StylePicker } from "./HtmlMotionDirection.jsx";

const initial = { name: "", html: "", width: 1280, height: 720, fps: 30, duration: 3 };
const words = {
  en: { title: "HTML motion", choose: "Saved composition", blank: "New composition", name: "Name", source: "Editable HTML source", load: "Open HTML file", example: "Load example", width: "Width", height: "Height", fps: "Frames per second", duration: "Duration (seconds)", save: "Save composition", saved: "Saved revision", preview: "Review frame", time: "Frame time (seconds)", render: "Export video", download: "Download HTML", video: "Download video", receive: "Add video to library", timeline: "Add video to timeline", received: "Video added", restore: "Restore revision", hint: "Define window.seek(t) with time in seconds. Calculate the complete frame from t; embed assets for portable results. Declare sound events {t, role} below and the export carries the sound; without them the video is silent.", runtime: "Browser renderer needs the motion package. Install lumiere-hoard[motion].", empty: "Save HTML to review a frame and export a video.", dirty: "Save your changes to review or export them.", cancel: "Cancel export", remove: "Delete composition", removeConfirm: (name, n) => `Delete "${name}" with its ${n} revision(s) and exported videos? This cannot be undone.`, removed: (name) => `Deleted "${name}"` },
  es: { title: "Animación HTML", choose: "Composición guardada", blank: "Nueva composición", name: "Nombre", source: "Fuente HTML editable", load: "Abrir archivo HTML", example: "Cargar ejemplo", width: "Anchura", height: "Altura", fps: "Fotogramas por segundo", duration: "Duración (segundos)", save: "Guardar composición", saved: "Revisión guardada", preview: "Revisar fotograma", time: "Tiempo del fotograma (segundos)", render: "Exportar vídeo", download: "Descargar HTML", video: "Descargar vídeo", receive: "Añadir vídeo a biblioteca", timeline: "Añadir vídeo a timeline", received: "Vídeo añadido", restore: "Restaurar revisión", hint: "Define window.seek(t) con el tiempo en segundos. Calcula el fotograma completo a partir de t; incrusta los recursos para conservar un resultado portable. Declara abajo eventos de sonido {t, role} y la exportación lleva el sonido; sin ellos el vídeo es mudo.", runtime: "El renderizador necesita el paquete motion. Instala lumiere-hoard[motion].", empty: "Guarda el HTML para revisar fotogramas y exportar un vídeo.", dirty: "Guarda tus cambios para revisarlos o exportarlos.", cancel: "Cancelar exportación", remove: "Borrar composición", removeConfirm: (name, n) => `¿Borrar «${name}» con sus ${n} revisiones y los vídeos exportados? No se puede deshacer.`, removed: (name) => `Borrada «${name}»` },
};

const EXAMPLE = `<!doctype html><html lang="es"><meta charset="utf-8"><title>Proceso</title>
<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#f4f0e8;color:#222b30;font-family:Arial,sans-serif}main{position:absolute;inset:12%;display:flex;flex-direction:column;justify-content:center}h1{font-size:clamp(20px,5vw,72px);margin:0 0 10%}section{display:flex;justify-content:space-between;gap:4%}.step{font-size:clamp(12px,2.4vw,32px);flex:1;padding:4% 0;border-top:3px solid #b2b4b2}.active{border-color:#b5583d;color:#b5583d}#line{height:6px;background:#b5583d;margin-top:8%;transform-origin:left}</style>
<main><h1>De una idea a un resultado</h1><section><div class="step">Entrada</div><div class="step">Preparación</div><div class="step">Salida</div></section><div id="line"></div></main>
<script>window.seek=t=>{const p=Math.max(0,Math.min(1,t/3));document.getElementById('line').style.transform='scaleX('+p+')';document.querySelectorAll('.step').forEach((e,i)=>e.classList.toggle('active',p>=i/3));};window.seek(0);</script></html>`;

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.detail || `HTTP ${response.status}`);
  return data;
}

export default function HtmlMotionSection({ projectId = null, onReceived }) {
  const { lang, fail } = useApp();
  const t = words[String(lang).startsWith("es") ? "es" : "en"];
  const [draft, setDraft] = useState(initial);
  const [saved, setSaved] = useState(null);
  const [catalogue, setCatalogue] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [at, setAt] = useState(0);
  const [preview, setPreview] = useState(null);
  const [storyboard, setStoryboard] = useState(null);
  const [moments, setMoments] = useState('');
  const [job, setJob] = useState(null);
  const [restoreRevision, setRestoreRevision] = useState(1);
  const [received, setReceived] = useState(false);
  const [exportAudio, setExportAudio] = useState(true);
  const [cueCount, setCueCount] = useState(0);
  const [reviewMode, setReviewMode] = useState("sheet");
  const [span, setSpan] = useState({ from: "", to: "" });
  const [crop, setCrop] = useState({ x: "", y: "", selector: "" });
  const current = useRef({ draft, saved });
  current.current = { draft, saved };
  const active = job && !["done", "failed", "canceled"].includes(job.state);
  const dirty = !saved || Object.keys(initial).some((key) => draft[key] !== saved[key]);
  useCopilotForm("html-motion-draft", { initial, current: { ...draft, composition: saved?.id, revision: saved?.revision, project_id: projectId, at_s: at, storyboard_times_text: moments } });

  const refresh = useCallback(async () => {
    try {
      setCatalogue(await request("/editing/api/html-motion"));
      const selected = current.current.saved;
      if (!selected) return;
      const data = await request(`/editing/api/html-motion/${selected.id}`);
      const now = current.current;
      if (now.saved?.id !== data.id || now.saved.revision > data.revision) return;
      const clean = Object.keys(initial).every((key) => now.draft[key] === now.saved[key]);
      setSaved(data);
      if (clean) setDraft(Object.fromEntries(Object.keys(initial).map((key) => [key, data[key]])));
    } catch (e) { setError(e.message); }
  }, []);
  useEffect(() => { refresh(); }, []);
  useCopilotRefresh(refresh);
  useEffect(() => () => { if (preview?.url) URL.revokeObjectURL(preview.url); }, [preview]);
  useEffect(() => {
    if (!active) return undefined;
    let stopped = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await appApi.job(job.id);
        if (stopped) return;
        setJob(next);
        if (next.state === "done") {
          setSaved((previous) => previous?.id === next.result.id && previous.revision === next.result.revision ? { ...previous, ...next.result } : previous);
          refresh();
        } else if (next.state === "failed") setError(next.error || "Video export failed");
      } catch (e) { if (!stopped) setError(e.message); }
    }, 700);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [active, job?.id]);

  const operate = async (action) => {
    setBusy(true); setError(""); setReceived(false);
    try { await action(); } catch (e) { setError(e.message); fail?.(e); }
    finally { setBusy(false); }
  };
  const load = (id) => operate(async () => {
    if (!id) { setDraft(initial); setSaved(null); setPreview(null); setStoryboard(null); setJob(null); return; }
    const data = await request(`/editing/api/html-motion/${id}`);
    setSaved(data); setDraft(Object.fromEntries(Object.keys(initial).map((key) => [key, data[key]])));
    setPreview(null); setStoryboard(null); setJob(null); setAt(0); setRestoreRevision(data.revisions[0]);
  });
  const save = (event) => {
    event.preventDefault();
    operate(async () => {
      const submitted = { ...draft, composition: saved?.id || null, expected_revision: saved?.revision || null, project_id: projectId };
      const data = await request("/editing/api/html-motion", { method: "POST", body: JSON.stringify(submitted) });
      setSaved({ ...data, html: submitted.html, revisions: [...(saved?.revisions || []), data.revision] });
      setPreview(null); refresh();
    });
  };
  const review = () => operate(async () => {
    const response = await fetch(`${saved.frame_url}?at=${at}`);
    if (!response.ok) { const result = await response.json(); throw new Error(result.error || result.detail); }
    const blob = await response.blob();
    setPreview({ url: URL.createObjectURL(blob), revision: saved.revision, at });
  });
  const reviewMoments = () => operate(async () => {
    const times = moments.trim() ? moments.trim().split(/[\s,;]+/).map(Number) : undefined;
    if (times?.some((value) => !Number.isFinite(value))) throw new Error(String(lang).startsWith('es') ? 'Introduce tiempos en segundos.' : 'Enter times in seconds.');
    const body = { composition: saved.id, revision: saved.revision, mode: reviewMode };
    if (reviewMode !== "strip" && times) body.times = times;
    if (reviewMode === "strip" && (span.from !== "" || span.to !== "")) body.span = [Number(span.from || 0), Number(span.to === "" ? saved.duration : span.to)];
    if (reviewMode === "crop-at") {
      if (crop.selector.trim()) body.selector = crop.selector.trim();
      else if (crop.x !== "" && crop.y !== "") { body.x = Number(crop.x); body.y = Number(crop.y); }
      else throw new Error(String(lang).startsWith('es') ? 'Indica un punto x, y o un selector CSS.' : 'Give a point x, y or a CSS selector.');
    }
    const result = await request(`/editing/api/html-motion/${saved.id}/storyboard`, { method: 'POST', body: JSON.stringify(body) });
    if (current.current.saved?.id === result.composition) setStoryboard(result);
  });
  const render = () => operate(async () => {
    const next = await request(`/editing/api/html-motion/${saved.id}/render?revision=${saved.revision}${exportAudio ? "" : "&audio=false"}`, { method: "POST", body: "{}" });
    setJob(await appApi.job(next.job));
  });
  const receive = () => operate(async () => {
    await request(`/editing/api/html-motion/${saved.id}/receive`, { method: "POST", body: JSON.stringify({ project: projectId || "", revision: saved.revision }) });
    setReceived(true); onReceived?.();
  });
  const restore = () => operate(async () => {
    const result = await request("/editing/api/html-motion/restore", { method: "POST", body: JSON.stringify({ composition: saved.id, revision: restoreRevision, expected_revision: saved.revision }) });
    const data = await request(`/editing/api/html-motion/${result.id}`);
    setSaved(data); setDraft(Object.fromEntries(Object.keys(initial).map((key) => [key, data[key]]))); setPreview(null); refresh();
  });
  const [removedName, setRemovedName] = useState("");
  const remove = () => {
    if (!saved || !window.confirm(t.removeConfirm(saved.name, saved.revisions?.length || 1))) return;
    operate(async () => {
      const result = await request(`/editing/api/html-motion/${saved.id}`, { method: "DELETE" });
      setDraft(initial); setSaved(null); setPreview(null); setStoryboard(null); setJob(null);
      setRemovedName(result.name || saved.name); refresh();
    });
  };
  const change = (key, value) => setDraft((current) => ({ ...current, [key]: value }));

  const created = async (id) => { await refresh(); await load(id); };
  const dir = lang && String(lang).startsWith("es")
    ? { mode: "Tipo de revisión", sheet: "Hoja de momentos", strip: "Tira de fotogramas", crop: "Detalle ampliado", from: "Tramo desde (s)", to: "hasta (s)", point: "Punto x", pointY: "y", selector: "o selector CSS" }
    : { mode: "Review type", sheet: "Moments sheet", strip: "Frame strip", crop: "Enlarged detail", from: "Span from (s)", to: "to (s)", point: "Point x", pointY: "y", selector: "or CSS selector" };

  return (
    <section className="html-motion" aria-labelledby="html-motion-title">
      <h3 id="html-motion-title">{t.title}</h3>
      <StylePicker lang={lang} busy={busy || active} projectId={projectId} operate={operate} onCreated={created} />
      <label className="field-label"><span>{t.choose}</span>
        <select className="field" value={saved?.id || ""} disabled={busy || active} onChange={(e) => load(e.target.value)}>
          <option value="">{t.blank}</option>{catalogue?.compositions.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </label>
      {saved && <div className="html-motion-actions">
        <button type="button" className="btn btn-danger" data-testid="html-motion-delete" disabled={busy || active} onClick={remove}>{t.remove}</button>
      </div>}
      {!saved && removedName && <p role="status">{t.removed(removedName)}</p>}
      {catalogue && !catalogue.runtime.playwright && <p role="status">{t.runtime}</p>}
      <form onSubmit={save}>
        <label className="field-label"><span>{t.name}</span><input className="field" required value={draft.name} onChange={(e) => change("name", e.target.value)} /></label>
        <div className="html-motion-settings">
          {["width", "height", "fps", "duration"].map((key) => <label className="field-label" key={key}><span>{t[key]}</span><input className="field" type="number" min={key === "fps" || key === "duration" ? ".01" : "1"} step={key === "fps" || key === "duration" ? "any" : "1"} required value={draft[key]} onChange={(e) => change(key, Number(e.target.value))} /></label>)}
        </div>
        <div className="html-motion-actions">
          <label className="btn html-motion-file">{t.load}<input type="file" accept=".html,.htm,text/html" disabled={busy || active} onChange={(e) => { const file = e.target.files?.[0]; if (file) operate(async () => { change("html", await file.text()); }); e.target.value = ""; }} /></label>
          <button className="btn" type="button" disabled={busy || active} onClick={() => setDraft({ ...initial, name: "Proceso", html: EXAMPLE })}>{t.example}</button>
        </div>
        <label className="field-label"><span>{t.source}</span><textarea className="field html-motion-source" required rows={12} spellCheck={false} value={draft.html} onChange={(e) => change("html", e.target.value)} /></label>
        <p className="muted">{t.hint}</p>
        <button className="btn btn-primary" disabled={busy || active || !draft.name.trim() || !draft.html.trim()}>{busy ? <Spinner /> : null}{t.save}</button>
      </form>
      {error && <p role="alert" className="error">{error}</p>}
      {!saved && <p className="muted">{t.empty}</p>}
      {saved && <>
        <p role="status">{t.saved}: {saved.revision}{dirty ? ` · ${t.dirty}` : ""}</p>
        <div className="html-motion-actions">
          <label className="field-label"><span>{t.time}</span><input className="field" type="number" min="0" max={saved.duration} step="any" value={at} onChange={(e) => setAt(Number(e.target.value))} /></label>
          <button type="button" className="btn" disabled={busy || dirty || active} onClick={review}>{t.preview}</button>
          <button type="button" className="btn" disabled={busy || dirty || active} onClick={render}>{t.render}</button>
          <a href={saved.source_url} download>{t.download}</a>
        </div>
        <HtmlMotionSound saved={saved} dirty={dirty} busy={busy || active} lang={lang} exportAudio={exportAudio} setExportAudio={setExportAudio} onChanged={refresh} operate={operate} cuesOwn={cueCount > 0} />
        <HtmlMotionDirection saved={saved} dirty={dirty} busy={busy || active} lang={lang} operate={operate} onChanged={refresh} onCueCount={setCueCount} />
        <HtmlMotionVariants saved={saved} dirty={dirty} busy={busy || active} lang={lang} operate={(action) => operate(async () => { await action(); await refresh(); })} onOpen={load} />
        {preview && <figure><img src={preview.url} alt={`${t.preview} · ${preview.at} s · ${t.saved} ${preview.revision}`} /><figcaption>{preview.at} s · {t.saved}: {preview.revision}</figcaption></figure>}
        <div className="html-motion-review-mode">
          <label className="field-label"><span>{dir.mode}</span>
            <select className="field" value={reviewMode} onChange={(e) => setReviewMode(e.target.value)}>
              <option value="sheet">{dir.sheet}</option><option value="strip">{dir.strip}</option><option value="crop-at">{dir.crop}</option>
            </select></label>
          {reviewMode === "strip" && <>
            <label className="field-label"><span>{dir.from}</span><input className="field" type="number" min="0" step="any" value={span.from} onChange={(e) => setSpan({ ...span, from: e.target.value })} /></label>
            <label className="field-label"><span>{dir.to}</span><input className="field" type="number" min="0" step="any" value={span.to} onChange={(e) => setSpan({ ...span, to: e.target.value })} /></label></>}
          {reviewMode === "crop-at" && <>
            <label className="field-label"><span>{dir.point}</span><input className="field" type="number" step="any" value={crop.x} onChange={(e) => setCrop({ ...crop, x: e.target.value })} /></label>
            <label className="field-label"><span>{dir.pointY}</span><input className="field" type="number" step="any" value={crop.y} onChange={(e) => setCrop({ ...crop, y: e.target.value })} /></label>
            <label className="field-label"><span>{dir.selector}</span><input className="field" value={crop.selector} placeholder="#title" onChange={(e) => setCrop({ ...crop, selector: e.target.value })} /></label></>}
        </div>
        <div className="html-motion-actions">
          <label className="field-label"><span>{String(lang).startsWith('es') ? 'Momentos (segundos)' : 'Moments (seconds)'}</span>
            <input className="field" value={moments} aria-describedby="html-motion-moments-help" placeholder="0, 0.5, 1" onChange={(e) => setMoments(e.target.value)} /></label>
          <button type="button" className="btn" disabled={busy || dirty || active} onClick={reviewMoments}>{String(lang).startsWith('es') ? 'Revisar varios momentos' : 'Review multiple moments'}</button>
        </div>
        <p className="muted" id="html-motion-moments-help">{String(lang).startsWith('es') ? 'Separa los momentos con comas y usa punto decimal: 0, 0.5, 1. Déjalo vacío para cuatro momentos.' : 'Separate moments with commas and use a decimal point: 0, 0.5, 1. Leave empty for four moments.'}</p>
        {storyboard && storyboard.composition === saved.id && <figure className="html-motion-storyboard">
          <img src={storyboard.url} alt={`${String(lang).startsWith('es') ? 'Hoja de fotogramas' : 'Frame sheet'} · ${t.saved} ${storyboard.revision}`} />
          <figcaption>{t.saved}: {storyboard.revision} · {storyboard.times.length} {String(lang).startsWith('es') ? 'fotogramas' : 'frames'}</figcaption>
          {storyboard.revision !== saved.revision && <p className="muted">{String(lang).startsWith('es') ? 'Esta hoja corresponde a una versión anterior.' : 'This sheet uses an earlier revision.'}</p>}
          {storyboard.source_matches_revision === false && <p className="muted">{String(lang).startsWith('es') ? 'La fuente capturada difiere de la versión guardada. La hoja conserva el HTML que se ha renderizado.' : 'The captured source differs from the saved revision. The sheet keeps the HTML that was rendered.'}</p>}
          <div className="html-motion-actions"><a href={storyboard.url} download>{String(lang).startsWith('es') ? 'Descargar hoja' : 'Download sheet'}</a>
            {storyboard.source_url && <a href={storyboard.source_url} download>{String(lang).startsWith('es') ? 'Descargar fuente revisada' : 'Download reviewed source'}</a>}
            {storyboard.frames.map((frame, index) => <a key={index} href={frame.url} download>{frame.at_s.toLocaleString(String(lang).startsWith('es') ? 'es-ES' : 'en-GB', { maximumFractionDigits: 6 })} s</a>)}</div>
        </figure>}
        {job && <div role="status" className="html-motion-actions"><span>{String(lang).startsWith("es") ? ({ queued: "En cola", running: "Exportando", done: "Vídeo listo", failed: "Exportación fallida", canceled: "Exportación cancelada" }[job.state] || job.state) : job.state} · {Math.round(job.progress * 100)}%</span>{active && <button type="button" className="btn" onClick={() => operate(async () => setJob(await appApi.jobCancel(job.id)))}>{t.cancel}</button>}</div>}
        {saved.render_url && <><video controls preload="metadata" src={saved.render_url} aria-label={saved.name} /><div className="html-motion-actions"><a href={saved.render_url} download>{t.video}</a><button type="button" className="btn" disabled={busy || active || dirty} onClick={receive}>{projectId ? t.timeline : t.receive}</button>{received && <span role="status">{t.received}</span>}</div></>}
        {saved.revisions?.length > 1 && <div className="html-motion-actions"><label className="field-label"><span>{t.restore}</span><select className="field" value={restoreRevision} onChange={(e) => setRestoreRevision(Number(e.target.value))}>{saved.revisions.map((revision) => <option key={revision} value={revision}>{revision}</option>)}</select></label><button className="btn" type="button" disabled={busy || active} onClick={restore}>{t.restore}</button></div>}
      </>}
    </section>
  );
}
