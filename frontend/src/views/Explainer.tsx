import { useEffect, useState } from "react";
import { api, type ExplainerProject, type ExplainerScene, type ProductionState } from "../api";
import { useT } from "../i18n";
import { useApp } from "../components/ui";
import { liveRun } from "./Storyboard";

export function ExplainerScenes({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { lang } = useT(), app = useApp();
  const es = lang === "es", incoming = state.spec.explainer as ExplainerProject;
  const running = liveRun(state, app.jobs);
  const [draft, setDraft] = useState<ExplainerProject>(structuredClone(incoming));
  const [dirty, setDirty] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [assets, setAssets] = useState<{ id: string; name?: string | null }[]>([]);
  const currentVoice = (state.spec.voice || {}) as { voice_id?: string; speed?: number };
  const [voices, setVoices] = useState<{ id: string; label: string }[]>([]), [voice, setVoice] = useState(currentVoice.voice_id || ""), [speed, setSpeed] = useState(currentVoice.speed || 1);
  useEffect(() => { api.voices().then(r => setVoices(r.items)).catch(() => undefined); }, []);
  useEffect(() => { if (state.project_id) api.assets(state.project_id, { kind: "image", limit: 200 }).then(r => setAssets(r.items)).catch(() => undefined); }, [state.project_id]);
  useEffect(() => { if (!dirty) setDraft(structuredClone(incoming)); }, [state.slug, incoming.revision]);
  const change = (fn: (next: ExplainerProject) => void) => { setDraft(previous => { const next = structuredClone(previous); fn(next); return next; }); setDirty(true); };
  const sceneChange = (index: number, values: Partial<ExplainerScene>) => change(next => { Object.assign(next.scenes[index], values); });
  const move = (index: number, direction: number) => change(next => { const [item] = next.scenes.splice(index, 1); next.scenes.splice(index + direction, 0, item); });
  const save = async (run: boolean) => {
    setBusy(true); setError("");
    try { const voiceChanged = voice !== (currentVoice.voice_id || "") || speed !== (currentVoice.speed || 1); const next = await api.explainer({ action: "edit", production: state.slug, base_revision: draft.revision, scenes: draft.scenes, sources: draft.sources, ...(voiceChanged ? { voice: { ...(voice ? { voice_id: voice } : {}), ...(voice.startsWith("voice_") ? {} : { backend: "piper" }), speed } } : {}), run }); setDraft(next.explainer); setDirty(false); app.refreshJobs(); onChanged(); return next.explainer; }
    catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  };
  const exportDraft = async (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (!dirty) return;
    event.preventDefault(); const current = await save(false);
    if (current) { const link = document.createElement("a"); link.href = `/generation/api/productions/${state.slug}/explainer.json`; link.download = ""; document.body.append(link); link.click(); link.remove(); }
  };
  return <section className="card stack" aria-label={es ? "Vídeo explicativo" : "Explanatory video"}>
    <h2>{es ? "Escenas y fuentes" : "Scenes and sources"}</h2>
    <p className="small muted">{es ? "Cada escena conserva su narración, figura y fuentes. Al corregirla se reutilizan las voces y páginas que no cambian." : "Each scene retains narration, figures and sources. Corrections reuse unchanged voices and pages."}</p>
    <div className="grid-2"><label className="field">{es ? "Voz de narración" : "Narration voice"}<select value={voice} onChange={event => { setVoice(event.target.value); setDirty(true); }}><option value="">{es ? "Predeterminada del idioma" : "Language default"}</option>{voice && !voices.some(v => v.id === voice) && <option value={voice}>{es ? "Voz actual" : "Current voice"}</option>}{voices.map(v => <option value={v.id} key={v.id}>{v.label}</option>)}</select></label><label className="field">{es ? "Velocidad de la voz" : "Voice speed"}<input type="number" min="0.5" max="2" step="0.05" value={speed} onChange={event => { setSpeed(Number(event.target.value)); setDirty(true); }}/></label></div>
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
        <label className="field">{es ? "Figura de la biblioteca" : "Library figure"}<select value={scene.asset_id || ""} onChange={event => sceneChange(index, { asset_id: event.target.value || undefined })}><option value="">{es ? "Página de texto animado" : "Animated text page"}</option>{assets.map(asset => <option value={asset.id} key={asset.id}>{asset.name || asset.id}</option>)}</select></label>
        <fieldset><legend>{es ? "Fuentes de esta escena" : "Scene sources"}</legend>{draft.sources.map(source => <label className="row" key={source.id}><input type="checkbox" checked={scene.source_ids.includes(source.id)} onChange={event => sceneChange(index, { source_ids: event.target.checked ? [...scene.source_ids, source.id] : scene.source_ids.filter(id => id !== source.id) })}/>{source.title}</label>)}</fieldset>
        {!!scene.source_ids.length && <label className="row"><input type="checkbox" checked={!!scene.source_reviewed} onChange={event => sceneChange(index, { source_reviewed: event.target.checked })}/>{es ? "He comprobado esta escena con sus fuentes" : "I checked this scene against its sources"}</label>}
        <div className="row wrap"><button className="btn" disabled={!index} onClick={() => move(index, -1)}>{es ? "Subir" : "Move up"}</button><button className="btn" disabled={index === draft.scenes.length - 1} onClick={() => move(index, 1)}>{es ? "Bajar" : "Move down"}</button><button className="btn" disabled={draft.scenes.length < 2} onClick={() => change(next => { next.scenes.splice(index, 1); })}>{es ? "Eliminar escena" : "Delete scene"}</button></div>
      </div>
    </details>)}
    <div className="row wrap"><button className="btn" onClick={() => change(next => { next.scenes.push({ id: crypto.randomUUID(), title: es ? "Nueva escena" : "New scene", narration: "", body: "", source_ids: [] }); })}>{es ? "Añadir escena" : "Add scene"}</button><button className="btn primary" disabled={busy || !dirty} onClick={() => save(false)}>{es ? "Guardar escenas" : "Save scenes"}</button><button className="btn" disabled={busy || running} onClick={() => save(true)}>{es ? "Guardar y producir" : "Save and produce"}</button><a className="btn" href={`/generation/api/productions/${state.slug}/explainer.json`} onClick={exportDraft} download>{dirty ? (es ? "Guardar y exportar proyecto" : "Save and export project") : (es ? "Proyecto editable" : "Editable project")}</a>{!!state.done.narration && !!state.done.report && <a className="btn" href={`/generation/api/productions/${state.slug}/explainer.srt`} download>SRT</a>}</div>
  </section>;
}
