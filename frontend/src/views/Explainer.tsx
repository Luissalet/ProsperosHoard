import { useEffect, useRef, useState } from "react";
import { Maximize2, X } from "lucide-react";
import { api, type ExplainerProject, type ExplainerScene, type PortableFigure, type ProductionState } from "../api";
import { useT } from "../i18n";
import { useApp, useDialog } from "../components/ui";
import { liveRun } from "./Storyboard";

function originLink(value?: string) {
  try { const url = new URL(value || ""); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}

function downloadFigure(figure: PortableFigure) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(figure, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = `${figure.document_id}-r${figure.revision}.vfigure`;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function FigurePreview({ figure, es, onClose }: { figure: PortableFigure; es: boolean; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null), { layer } = useDialog(ref, onClose);
  const [scale, setScale] = useState<"fit" | 1 | 2>(1), [width, setWidth] = useState(0);
  return <div ref={ref} tabIndex={-1} className="overlay" role="dialog" aria-modal="true" aria-label={es ? "Inspeccionar figura" : "Inspect figure"} style={{ zIndex: layer, display: "flex", flexDirection: "column", gap: 12, padding: 16 }}>
    <div className="row wrap"><button className="btn" onClick={onClose}><X size={16}/>{es ? "Cerrar" : "Close"}</button><button className="btn" onClick={() => setScale("fit")} aria-pressed={scale === "fit"}>{es ? "Ajustar" : "Fit"}</button><button className="btn" onClick={() => setScale(1)} aria-pressed={scale === 1}>{es ? "Tamaño real" : "Actual size"}</button><button className="btn" onClick={() => setScale(2)} aria-pressed={scale === 2}>{es ? "Ampliar 2×" : "Zoom 2×"}</button><span>{figure.title}</span></div>
    <div tabIndex={0} aria-label={es ? "Captura ampliada; desplázate para explorar" : "Enlarged capture; scroll to explore"} style={{ overflow: "auto", width: "100%", minHeight: 0, flex: 1 }}>
      <img src={`data:image/png;base64,${figure.preview.data}`} alt={figure.title || (es ? "Figura importada" : "Imported figure")} onLoad={event => setWidth(event.currentTarget.naturalWidth)} style={{ display: "block", margin: "auto", width: scale === 2 && width ? width * 2 : "auto", maxWidth: scale === "fit" ? "100%" : "none", maxHeight: scale === "fit" ? "100%" : "none", objectFit: "contain" }}/>
    </div>
  </div>;
}

export function ExplainerScenes({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { lang } = useT(), app = useApp();
  const es = lang === "es", incoming = state.spec.explainer as ExplainerProject;
  const running = liveRun(state, app.jobs);
  const [draft, setDraft] = useState<ExplainerProject>(structuredClone(incoming));
  const [dirty, setDirty] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [preview, setPreview] = useState<PortableFigure | null>(null);
  const [assets, setAssets] = useState<{ id: string; name?: string | null }[]>([]);
  const currentVoice = (state.spec.voice || {}) as { voice_id?: string; speed?: number };
  const [voices, setVoices] = useState<{ id: string; label: string }[]>([]), [voice, setVoice] = useState(currentVoice.voice_id || ""), [speed, setSpeed] = useState(currentVoice.speed || 1);
  const [voiceDirty, setVoiceDirty] = useState(false);
  useEffect(() => { if (!voiceDirty) { setVoice(currentVoice.voice_id || ""); setSpeed(currentVoice.speed || 1); } }, [currentVoice.voice_id, currentVoice.speed, voiceDirty]);
  useEffect(() => { api.voices().then(r => setVoices(r.items)).catch(() => undefined); }, []);
  useEffect(() => { if (state.project_id) api.assets(state.project_id, { kind: "image", limit: 200 }).then(r => setAssets(r.items)).catch(() => undefined); }, [state.project_id]);
  useEffect(() => { if (!dirty) setDraft(structuredClone(incoming)); }, [state.slug, incoming.revision]);
  const change = (fn: (next: ExplainerProject) => void) => { setDraft(previous => { const next = structuredClone(previous); fn(next); return next; }); setDirty(true); };
  const sceneChange = (index: number, values: Partial<ExplainerScene>) => change(next => { Object.assign(next.scenes[index], values); });
  const importFigure = async (id: string, file: File) => {
    setBusy(true); setError("");
    try {
      const figure = JSON.parse(await file.text()) as PortableFigure;
      if (figure.format !== "vitruvius.figure/1" || figure.preview?.mime !== "image/png" || typeof figure.preview.data !== "string" || !figure.content) throw new Error(es ? "Elige un archivo .vfigure con su fuente y captura PNG." : "Choose a .vfigure file with its source and PNG capture.");
      change(next => { const scene = next.scenes.find(item => item.id === id); if (scene) { scene.figure = figure; delete scene.asset_id; } });
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  };
  const move = (index: number, direction: number) => change(next => { const [item] = next.scenes.splice(index, 1); next.scenes.splice(index + direction, 0, item); });
  const save = async (run: boolean) => {
    setBusy(true); setError("");
    try { const next = await api.explainer({ action: "edit", production: state.slug, base_revision: draft.revision, scenes: draft.scenes, sources: draft.sources, ...(voiceDirty ? { voice: { ...(voice ? { voice_id: voice } : {}), ...(voice.startsWith("voice_") ? {} : { backend: "piper" }), speed } } : {}), run }); setDraft(next.explainer); setDirty(false); setVoiceDirty(false); app.refreshJobs(); onChanged(); return next.explainer; }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  };
  const exportDraft = async (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (busy) { event.preventDefault(); return; }
    if (!dirty) return;
    event.preventDefault(); const current = await save(false);
    if (current) { const link = document.createElement("a"); link.href = `/generation/api/productions/${state.slug}/explainer.json`; link.download = ""; document.body.append(link); link.click(); link.remove(); }
  };
  return <section className="card stack" aria-label={es ? "Vídeo explicativo" : "Explanatory video"}>
    <fieldset className="stack" disabled={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
    <h2>{es ? "Escenas y fuentes" : "Scenes and sources"}</h2>
    <p className="small muted">{es ? "Cada escena conserva su narración, figura y fuentes. Al corregirla se reutilizan las voces y páginas que no cambian." : "Each scene retains narration, figures and sources. Corrections reuse unchanged voices and pages."}</p>
    <div className="grid-2"><label className="field">{es ? "Voz de narración" : "Narration voice"}<select value={voice} onChange={event => { setVoice(event.target.value); setVoiceDirty(true); setDirty(true); }}><option value="">{es ? "Predeterminada del idioma" : "Language default"}</option>{voice && !voices.some(v => v.id === voice) && <option value={voice}>{es ? "Voz actual" : "Current voice"}</option>}{voices.map(v => <option value={v.id} key={v.id}>{v.label}</option>)}</select></label><label className="field">{es ? "Velocidad de la voz" : "Voice speed"}<input type="number" min="0.5" max="2" step="0.05" value={speed} onChange={event => { setSpeed(Number(event.target.value)); setVoiceDirty(true); setDirty(true); }}/></label></div>
    {error && <p role="alert">{error}</p>}
    {dirty && incoming.revision !== draft.revision && <p role="alert">{es ? "Hay una revisión más reciente. Tu borrador se conserva." : "A newer revision is available. Your draft is retained."}</p>}
    {!!state.view.explainer?.review_needed?.length && <p role="status">{es ? "Fuentes modificadas; revisar escenas: " : "Changed sources; check scenes: "}{state.view.explainer.review_needed.join(", ")}</p>}
    <details><summary>{es ? "Material y referencias" : "Material and references"}</summary><div className="stack">
      {draft.sources.map((source, index) => <fieldset className="stack" key={source.id}><legend>{source.title}</legend>
        <label className="field">{es ? "Nombre de la fuente" : "Source name"}<input value={source.title} onChange={event => change(next => { next.sources[index].title = event.target.value; })}/></label>
        <label className="field">{es ? "Referencia y versión" : "Reference and version"}<input value={source.reference} onChange={event => change(next => { next.sources[index].reference = event.target.value; })}/></label>
        <label className="field">{es ? "Contenido aportado" : "Supplied content"}<textarea aria-label={es ? "Contenido aportado" : "Supplied content"} rows={4} value={source.text} onChange={event => change(next => { next.sources[index].text = event.target.value; })}/></label>
      </fieldset>)}
      <button className="btn" onClick={() => change(next => { next.sources.push({ id: crypto.randomUUID(), title: es ? "Nueva fuente" : "New source", text: "", reference: "" }); })}>{es ? "Añadir fuente" : "Add source"}</button>
    </div></details>
    {draft.scenes.map((scene, index) => <details key={scene.id} open={draft.scenes.length < 4}>
      <summary>{index + 1}. {scene.title}</summary><div className="stack">
        <label className="field">{es ? "Título de la escena" : "Scene title"}<input value={scene.title} onChange={event => sceneChange(index, { title: event.target.value })}/></label>
        <label className="field">{es ? "Narración" : "Narration"}<textarea aria-label={es ? "Narración" : "Narration"} rows={4} value={scene.narration} onChange={event => sceneChange(index, { narration: event.target.value })}/></label>
        <label className="field">{es ? "Texto en pantalla" : "On-screen text"}<textarea aria-label={es ? "Texto en pantalla" : "On-screen text"} rows={2} value={scene.body} onChange={event => sceneChange(index, { body: event.target.value })}/></label>
        <label className="field">{es ? "Figura de la biblioteca" : "Library figure"}<select value={scene.figure ? "portable" : scene.asset_id || ""} onChange={event => sceneChange(index, { asset_id: event.target.value || undefined, figure: null })}>{scene.figure && <option value="portable">{es ? "Figura importada con su fuente" : "Imported figure with source"}</option>}<option value="">{es ? "Página de texto animado" : "Animated text page"}</option>{assets.map(asset => <option value={asset.id} key={asset.id}>{asset.name || asset.id}</option>)}</select></label>
        <label className="field">{es ? "Importar o sustituir figura (.vfigure)" : "Import or replace figure (.vfigure)"}<input type="file" accept=".vfigure,application/json" onChange={event => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void importFigure(scene.id, file); }}/></label>
        {scene.figure && <figure style={{ margin: 0 }} className="stack">
          <button className="btn" aria-label={es ? "Ampliar figura" : "Enlarge figure"} onClick={() => setPreview(scene.figure!)} style={{ display: "block", width: "100%", height: "auto", padding: 0, border: 0, background: "transparent" }}><img src={`data:image/png;base64,${scene.figure.preview.data}`} alt={scene.figure.title || scene.title} style={{ display: "block", width: "100%", maxHeight: 240, objectFit: "contain" }}/></button>
          <figcaption className="small">{scene.figure.title || scene.title} · {es ? "Revisión" : "Revision"} {scene.figure.revision}{scene.figure.page_id ? ` · ${es ? "Página" : "Page"} ${scene.figure.page_id}` : ""}{scene.figure.state_id ? ` · ${es ? "Estado" : "State"} ${scene.figure.state_id}` : ""}</figcaption>
          <div className="row wrap"><button className="btn" onClick={() => setPreview(scene.figure!)}><Maximize2 size={16}/>{es ? "Ampliar figura" : "Enlarge figure"}</button><button className="btn" onClick={() => downloadFigure(scene.figure!)}>{es ? "Descargar figura y fuente" : "Download figure and source"}</button>{originLink(scene.figure.origin?.editor_url) && <a className="btn" href={originLink(scene.figure.origin?.editor_url)} target="_blank" rel="noreferrer">{es ? "Abrir diagrama de origen" : "Open source diagram"}</a>}</div>
          <p className="small muted">{es ? "Se conserva esta captura y su fuente. Para usar otra revisión o estado, importa la nueva figura." : "This capture and its source are retained. Import a new figure to use another revision or state."}</p>
        </figure>}
        <fieldset><legend>{es ? "Fuentes de esta escena" : "Scene sources"}</legend>{draft.sources.map(source => <label className="row" key={source.id}><input type="checkbox" checked={scene.source_ids.includes(source.id)} onChange={event => sceneChange(index, { source_ids: event.target.checked ? [...scene.source_ids, source.id] : scene.source_ids.filter(id => id !== source.id) })}/>{source.title}</label>)}</fieldset>
        {!!scene.source_ids.length && <label className="row"><input type="checkbox" checked={!!scene.source_reviewed} onChange={event => sceneChange(index, { source_reviewed: event.target.checked })}/>{es ? "He comprobado esta escena con sus fuentes" : "I checked this scene against its sources"}</label>}
        <div className="row wrap"><button className="btn" disabled={!index} onClick={() => move(index, -1)}>{es ? "Subir" : "Move up"}</button><button className="btn" disabled={index === draft.scenes.length - 1} onClick={() => move(index, 1)}>{es ? "Bajar" : "Move down"}</button><button className="btn" disabled={draft.scenes.length < 2} onClick={() => change(next => { next.scenes.splice(index, 1); })}>{es ? "Eliminar escena" : "Delete scene"}</button></div>
      </div>
    </details>)}
    <div className="row wrap"><button className="btn" onClick={() => change(next => { next.scenes.push({ id: crypto.randomUUID(), title: es ? "Nueva escena" : "New scene", narration: "", body: "", source_ids: [] }); })}>{es ? "Añadir escena" : "Add scene"}</button><button className="btn primary" disabled={busy || !dirty} onClick={() => save(false)}>{es ? "Guardar escenas" : "Save scenes"}</button><button className="btn" disabled={busy || running} onClick={() => save(true)}>{es ? "Guardar y producir" : "Save and produce"}</button><a className="btn" href={`/generation/api/productions/${state.slug}/explainer.json`} onClick={exportDraft} download>{dirty ? (es ? "Guardar y exportar proyecto" : "Save and export project") : (es ? "Proyecto editable" : "Editable project")}</a>{!!state.done.narration && !!state.done.report && <a className="btn" href={`/generation/api/productions/${state.slug}/explainer.srt`} download>SRT</a>}</div>
    </fieldset>
    {preview && <FigurePreview figure={preview} es={es} onClose={() => setPreview(null)}/>}
  </section>;
}
