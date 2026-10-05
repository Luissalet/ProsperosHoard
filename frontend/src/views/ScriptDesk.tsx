import { useEffect, useState } from "react";
import { FileText, Plus, Save, Upload, Maximize2 } from "lucide-react";
import { api, type ProductionState, type ScriptSegment } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, useApp } from "../components/ui";
import { liveRun } from "./Storyboard";

const normal = (text: string) => text.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();

export function ScriptDesk({ state, onChanged, onStoryboard }: {
  state: ProductionState; onChanged: () => void; onStoryboard: () => void;
}) {
  const app = useApp();
  const { lang } = useT();
  const es = lang === "es";
  const saved = (state.spec.script_segments || []) as ScriptSegment[];
  const draftKey = `prospero.script-draft.${state.slug}`;
  const [rows, setRows] = useState<ScriptSegment[]>(() => {
    try { const draft = sessionStorage.getItem(draftKey); return draft ? JSON.parse(draft) : saved; } catch { return saved; }
  });
  const [dirty, setDirty] = useState(() => { try { return !!sessionStorage.getItem(draftKey); } catch { return false; } });
  const [raw, setRaw] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [importing, setImporting] = useState(saved.length === 0);
  const running = liveRun(state, app.jobs);
  const shots = state.spec.shots || [];
  const signature = JSON.stringify(saved);
  useEffect(() => { if (!dirty) setRows(saved); }, [signature]); // preserve unsaved text during background refresh
  useEffect(() => { try { if (dirty) sessionStorage.setItem(draftKey, JSON.stringify(rows)); else sessionStorage.removeItem(draftKey); } catch { /* storage unavailable */ } }, [rows, dirty, draftKey]);
  const change = (id: string, patch: Partial<ScriptSegment>) => {
    setRows((value) => value.map((row) => row.id === id ? { ...row, ...patch } : row)); setDirty(true);
  };
  const act = async (body: { text?: string; segments?: ScriptSegment[] }) => {
    setBusy(true); setError("");
    try {
      const result = await api.setProductionSegments(state.slug, body);
      setRows(result.segments); setDirty(false); setImporting(false); onChanged();
      app.toast(es ? "Fragmentos guardados. Las tomas vinculadas conservan sus tiempos." : "Segments saved. Linked shots use their selected times.", "ok");
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  const align = () => {
    let cursor = 0, count = 0;
    const lines = state.timing?.lines || [];
    const next = rows.map((row) => {
      const index = lines.findIndex((line, i) => i >= cursor && normal(line.text) === normal(row.text));
      if (index < 0) return row;
      cursor = index + 1; count++;
      return { ...row, start_s: lines[index].time_s, end_s: lines[index].end_s };
    });
    setRows(next); setDirty(true);
    app.toast(es ? `${count} fragmentos coinciden con la letra temporizada. Revisa los tiempos antes de guardar.` : `${count} segments match timed lyrics. Review times before saving.`, "info");
  };
  const addShot = async (row: ScriptSegment) => {
    if (dirty) { setError(es ? "Guarda los cambios antes de crear una toma." : "Save your changes before creating a shot."); return; }
    setBusy(true); setError("");
    try {
      const result = await api.changeShots(state.slug, [{ insert: { after: shots.at(-1)?.key || "start",
        prompt: row.text, lead: true, motion: "move", clip: true } }], false);
      const key = result.changed[0];
      if (key) {
        const updated = rows.map((segment) => segment.id === row.id ? { ...segment, shot_key: key } : segment);
        const out = await api.setProductionSegments(state.slug, { segments: updated });
        setRows(out.segments);
      }
      onChanged();
      app.toast(es ? "Toma creada. Edita su imagen y movimiento en el storyboard." : "Shot created. Direct its image and motion in the storyboard.", "ok");
    } catch (err) { setError((err as Error).message); onChanged(); }
    finally { setBusy(false); }
  };
  return <section className="card script-desk">
    <div className="script-toolbar"><FileText size={20} /><h2 style={{ margin: 0 }}>{es ? "Texto y tomas" : "Text and shots"}</h2>
      <span className="spacer" />{dirty && <span className="pill warn">{es ? "Sin guardar" : "Unsaved"}</span>}
      <button className="btn sm" onClick={onStoryboard}>{es ? "Ver storyboard" : "View storyboard"}</button>
      <button className="btn sm" disabled={running || busy} onClick={() => setImporting(!importing)}><Upload size={15} />{es ? "Importar texto" : "Import text"}</button>
      <button className="btn primary" disabled={running || busy || !dirty} onClick={() => act({ segments: rows })}><Save size={15} />{es ? "Guardar vínculos" : "Save links"}</button></div>
    <p className="muted small" style={{ margin: 0 }}>{es
      ? "Opcional. Puedes crear sin guion. Si ya tienes uno o una letra, vincula cada fragmento a una toma y ajusta sus tiempos. Los segundos vacíos permanecen sin temporizar."
      : "Optional. You can create without a script. If you have one or lyrics, link fragments to shots and adjust times. Empty times remain untimed."}</p>
    {running && <p className="hint">{es ? "Pausa la producción para editar los vínculos." : "Pause the production to edit links."}</p>}
    {importing && <div className="script-import">
      <label className="field">{es ? "Pegar guion o letra · TXT, LRC, SRT, VTT" : "Paste script or lyrics · TXT, LRC, SRT, VTT"}<textarea value={raw} onChange={(e) => setRaw(e.target.value)} disabled={running || busy} placeholder={es ? "Una frase por línea, o subtítulos con sus tiempos…" : "One phrase per line, or subtitles with their timestamps…"} /></label>
      <div className="script-toolbar">
        <label className="btn upload-label"><Upload size={15} />{es ? "Abrir archivo" : "Open file"}<input type="file" hidden accept=".txt,.md,.lrc,.srt,.vtt" disabled={running || busy} onChange={async (e) => {
          const file = e.target.files?.[0]; e.target.value = "";
          if (!file) return;
          if (file.size > 80000) { setError(es ? "El archivo es demasiado grande (máximo 80 KB)." : "File is too large (maximum 80 KB)."); return; }
          try { setRaw(await file.text()); } catch (err) { setError((err as Error).message); }
        }} /></label>
        <button className="btn primary" disabled={running || busy || !raw.trim() || rows.length > 0} onClick={() => act({ text: raw })}>{es ? "Importar fragmentos" : "Import segments"}</button>
        {rows.length > 0 && <span className="hint">{es ? "Tus fragmentos están guardados. Añade o edita filas; la importación no los sustituye." : "Your segments are saved. Add or edit rows; importing does not replace them."}</span>}
      </div></div>}
    {rows.length === 0 && !importing && <p className="muted">{es ? "No hay texto vinculado. El storyboard funciona por sí solo." : "No linked text. The storyboard works on its own."}</p>}
    {rows.length > 0 && <div className="script-toolbar"><span className="small muted">{rows.length} {es ? "fragmentos" : "segments"}</span>
      <button className="btn sm" disabled={running || busy || !state.timing?.lines?.length} onClick={align}>{es ? "Tomar tiempos de la letra" : "Use lyric timings"}</button>
      <span className="hint">{es ? "Vincular varios fragmentos a una toma une sus tiempos. Se rechazan solapamientos entre tomas." : "Multiple fragments on one shot use their combined range. Overlapping shot ranges are rejected."}</span></div>}
    <div>{rows.map((row) => {
      const frame = row.shot_key ? state.done?.frames?.items?.[row.shot_key]?.best : null;
      return <div className="script-row" key={row.id}>
        <textarea aria-label={es ? "Texto del fragmento" : "Segment text"} value={row.text} disabled={running || busy} onChange={(e) => change(row.id, { text: e.target.value })} />
        <label>{es ? "Inicio · segundos" : "Start · seconds"}<input type="number" min={0} step={0.01} value={row.start_s ?? ""} disabled={running || busy} onChange={(e) => change(row.id, { start_s: e.target.value === "" ? null : Number(e.target.value) })} /></label>
        <label>{es ? "Fin · segundos" : "End · seconds"}<input type="number" min={0} step={0.01} value={row.end_s ?? ""} disabled={running || busy} onChange={(e) => change(row.id, { end_s: e.target.value === "" ? null : Number(e.target.value) })} /></label>
        <label>{es ? "Toma vinculada" : "Linked shot"}<select value={row.shot_key || ""} disabled={running || busy} onChange={(e) => change(row.id, { shot_key: e.target.value || null })}><option value="">{es ? "Sin vincular" : "Unlinked"}</option>{shots.map((shot) => <option key={shot.key} value={shot.key}>{shot.key} · {shot.prompt.slice(0, 30)}</option>)}</select></label>
        {frame ? <button className="btn sm" onClick={() => app.openAsset(frame)}><Maximize2 size={14} />{es ? "Ver imagen" : "View image"}</button>
          : !row.shot_key ? <button className="btn sm" disabled={running || busy || dirty || !row.text.trim()} onClick={() => addShot(row)}><Plus size={14} />{es ? "Crear toma" : "Create shot"}</button> : null}
        {!running && !busy && <div className="row wrap">
          <button className="btn sm" disabled={!row.text.includes("\n")} onClick={() => {
            const parts = row.text.split(/\n+/).map((text) => text.trim()).filter(Boolean);
            if (parts.length < 2) return;
            setRows((value) => value.flatMap((item) => item.id === row.id ? parts.map((text, index) => ({ ...item, id: index ? `seg_${Date.now()}_${index}` : item.id, text })) : [item]));
            setDirty(true);
          }}>{es ? "Separar líneas" : "Split lines"}</button>
          <ConfirmButton onConfirm={() => { setRows((value) => value.filter((item) => item.id !== row.id)); setDirty(true); }}>{es ? "Quitar fragmento" : "Remove segment"}</ConfirmButton>
        </div>}
      </div>;
    })}</div>
    <button className="btn" style={{ alignSelf: "start" }} disabled={running || busy} onClick={() => { setRows([...rows, { id: `seg_${Date.now()}`, text: "", start_s: null, end_s: null, shot_key: null }]); setDirty(true); }}><Plus size={15} />{es ? "Añadir fragmento" : "Add segment"}</button>
    {error && <div className="studio-error" role="alert">{error}</div>}
  </section>;
}
