// Treatment-first productions (treatment tab, "Approve and render", storyboard sheet) and the workflow checklist.
import { useEffect, useState } from "react";
import { Check, ClipboardCopy, LayoutGrid, Loader2, Play, Save, Sparkles, X, Plus, Trash2 } from "lucide-react";
import { api, fileUrl, type ProductionState, type Treatment, type TreatmentShot, type WorkflowPreflight } from "../api";
import { useT } from "../i18n";
import { useApp } from "../components/ui";

const L = (lang: string, en: string, es: string) => (lang === "es" ? es : en);
const lines = (v: string[]) => v.join("\n");
const unlines = (v: string) => v.split("\n").map((s) => s.trim()).filter(Boolean);

/** Red/green checklist of the nodes and model files the run's ComfyUI workflows need, with "Copy list". */
export function WorkflowChecklist({ report, compact }: { report: WorkflowPreflight; compact?: boolean }) {
  const { lang } = useT();
  const app = useApp();
  const missing = report.missing_nodes + report.missing_models;
  const copy = async () => {
    try { await navigator.clipboard.writeText(report.copy_text); app.toast(L(lang, "List copied", "Lista copiada"), "ok"); }
    catch { app.toast(report.copy_text, "info"); }
  };
  const mark = (ok: boolean | null) => ok === true ? <Check size={13} className="wf-ok" /> : ok === false ? <X size={13} className="wf-bad" />
    : <span className="wf-unknown">?</span>;
  return (
    <details className="wf-check" open={!compact && missing > 0} data-testid="workflow-checklist">
      <summary>
        <span className={`pill ${report.ok === false ? "bad" : report.ok ? "ok" : ""}`}>
          {report.ok === null ? L(lang, "not checked", "sin comprobar") : missing ? L(lang, `${missing} missing`, `faltan ${missing}`) : L(lang, "all present", "todo instalado")}
        </span> {L(lang, "ComfyUI workflow checklist", "Lista de comprobación de ComfyUI")}
        {report.source === "cache" && <span className="small muted"> · {L(lang, "from the last saved node list", "según la última lista guardada")}</span>}
      </summary>
      {report.note && <p className="small muted">{report.note}</p>}
      {report.workflows.map((w) => (
        <div key={w.template || w.workflow || "wf"} className="wf-block">
          <div className="small"><strong>{w.workflow || w.template}</strong> {w.template && <span className="mono muted">{w.template}</span>}</div>
          <ul className="wf-list">
            {w.nodes.map((n) => <li key={n.class_type} className={n.ok === false ? "bad" : ""}>{mark(n.ok)} <span className="mono">{n.class_type}</span>
              {n.ok === false && <span className="small muted"> · {n.pack ? <a href={n.pack.url} target="_blank" rel="noreferrer">{n.pack.name}</a> : L(lang, "search in ComfyUI Manager", "búscalo en ComfyUI Manager")}</span>}</li>)}
            {w.models.map((m) => <li key={`${m.node_id}.${m.input}`} className={m.ok === false && !m.fallback ? "bad" : ""}>{mark(m.ok === false && m.fallback ? true : m.ok)} <span className="mono">{m.file}</span>
              <span className="small muted"> · models/{m.folder}{m.ok === false && m.fallback ? ` · ${L(lang, "uses", "usa")} ${m.fallback}` : ""}</span></li>)}
          </ul>
        </div>
      ))}
      <button className="btn sm" onClick={copy}><ClipboardCopy size={13} /> {L(lang, "Copy list", "Copiar lista")}</button>
    </details>
  );
}

const FRAMINGS = ["extreme wide", "wide", "full", "medium", "medium close-up", "close-up", "extreme close-up", "insert", "over the shoulder"];

/** The director's treatment of a production: draft (model or rules), edit, approve and render, storyboard sheet. */
export function TreatmentCard({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { lang } = useT();
  const app = useApp();
  const saved = (state as unknown as { treatment?: Treatment | null }).treatment || null;
  const [t, setT] = useState<Treatment | null>(saved);
  const [busy, setBusy] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [language, setLanguage] = useState(lang === "es" ? "es" : "en");
  const [generate, setGenerate] = useState(false);
  const [sheet, setSheet] = useState<string | null>(saved?.storyboard?.asset_id || null);
  useEffect(() => { setT(saved); setSheet(saved?.storyboard?.asset_id || null); }, [JSON.stringify(saved)]); // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(t) !== JSON.stringify(saved);
  const act = async (name: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(name);
    try { await fn(); if (ok) app.toast(ok, "ok"); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(null); }
  };
  const draft = () => act("draft", () => api.treatment(state.slug, { action: "draft", language, notes: notes || undefined }),
    L(lang, "Treatment drafted", "Tratamiento redactado"));
  const save = () => act("save", () => api.treatment(state.slug, { action: "update", treatment: t! }), L(lang, "Treatment saved", "Tratamiento guardado"));
  const approve = () => act("approve", async () => {
    if (dirty) await api.treatment(state.slug, { action: "update", treatment: t! });
    await api.treatment(state.slug, { action: "approve", run: true });
    app.refreshJobs();
  }, L(lang, "Approved: rendering", "Aprobado: renderizando"));
  const board = () => act("sheet", async () => {
    const r = await api.storyboardSheet(state.slug, generate);
    setSheet(r.asset_id);
    if (r.note) app.toast(r.note, "info");
    if (r.queued_jobs.length) { app.toast(L(lang, `${r.queued_jobs.length} stills queued; make the sheet again when they finish`,
      `${r.queued_jobs.length} fotogramas en cola; vuelve a hacer la hoja cuando terminen`), "info"); app.refreshJobs(); }
  });
  const shot = (i: number, patch: Partial<TreatmentShot>) => setT({ ...t!, shot_list: t!.shot_list.map((s, j) => j === i ? { ...s, ...patch } : s) });
  const beat = (i: number, patch: Partial<Treatment["beat_sheet"][number]>) =>
    setT({ ...t!, beat_sheet: t!.beat_sheet.map((b, j) => j === i ? { ...b, ...patch } : b) });

  if (!t) {
    return (
      <div className="card treatment" data-testid="treatment-card">
        <h2><Sparkles size={16} /> {L(lang, "Treatment", "Tratamiento")}</h2>
        <p className="small muted">{L(lang, "Before anything renders: logline, three candidate structures and the pick, what to learn from and avoid, a shot list with framing, camera, duration and why, and a beat sheet. The production then waits for your approval.",
          "Antes de renderizar nada: logline, tres estructuras candidatas y la elegida, qué aprender y qué evitar, lista de planos con encuadre, cámara, duración y porqué, y un mapa de momentos. La producción espera a que lo apruebes.")}</p>
        <div className="row wrap" style={{ gap: 8 }}>
          <select value={language} onChange={(e) => setLanguage(e.target.value)} aria-label={L(lang, "Language", "Idioma")}>
            <option value="es">Español</option><option value="en">English</option>
          </select>
          <input className="grow" value={notes} onChange={(e) => setNotes(e.target.value)} placeholder={L(lang, "Notes for the director (optional)", "Notas para la dirección (opcional)")} />
          <button className="btn primary" disabled={!!busy} onClick={draft} data-testid="treatment-draft">
            {busy === "draft" ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />} {L(lang, "Draft treatment", "Redactar tratamiento")}</button>
        </div>
      </div>
    );
  }
  const approved = t.status === "approved" && !dirty;
  return (
    <div className="card treatment" data-testid="treatment-card">
      <div className="row wrap" style={{ gap: 8 }}>
        <h2 className="grow" style={{ margin: 0 }}><Sparkles size={16} /> {L(lang, "Treatment", "Tratamiento")}{" "}
          <span className={`pill ${approved ? "ok" : "gold"}`}>{approved ? L(lang, "approved", "aprobado") : L(lang, "draft", "borrador")}</span>{" "}
          <span className="small muted">{t.source === "model" ? L(lang, "drafted by the model", "redactado por el modelo") : L(lang, "drafted from the planned shots", "redactado desde los planos")}</span></h2>
        <button className="btn sm" disabled={!!busy || !dirty} onClick={save}>{busy === "save" ? <Loader2 size={13} className="spin" /> : <Save size={13} />} {L(lang, "Save", "Guardar")}</button>
        <button className="btn sm" disabled={!!busy} onClick={draft}>{busy === "draft" ? <Loader2 size={13} className="spin" /> : <Sparkles size={13} />} {L(lang, "Redraft", "Redactar de nuevo")}</button>
        <button className="btn sm primary" disabled={!!busy || approved} onClick={approve} data-testid="treatment-approve">
          {busy === "approve" ? <Loader2 size={13} className="spin" /> : <Play size={13} />} {L(lang, "Approve and render", "Aprobar y renderizar")}</button>
      </div>
      {t.note && <p className="small muted">{t.note}</p>}
      <label className="stack" style={{ gap: 4 }}><span className="small muted">Logline</span>
        <textarea rows={2} value={t.logline} onChange={(e) => setT({ ...t, logline: e.target.value })} /></label>
      <div className="small muted" style={{ marginTop: 10 }}>{L(lang, "Structures (pick one)", "Estructuras (elige una)")}</div>
      <div className="tr-structures">
        {t.structures.map((s, i) => (
          <label key={i} className={`tr-structure${t.pick.index === i ? " on" : ""}`}>
            <input type="radio" name="pick" checked={t.pick.index === i} onChange={() => setT({ ...t, pick: { ...t.pick, index: i } })} />
            <strong>{s.name}</strong><span className="small">{s.summary}</span>
            {s.beats.length > 0 && <span className="small muted">{s.beats.join(" → ")}</span>}
          </label>
        ))}
      </div>
      <input value={t.pick.why} onChange={(e) => setT({ ...t, pick: { ...t.pick, why: e.target.value } })} placeholder={L(lang, "Why this one", "Por qué esta")} />
      <div className="row wrap" style={{ gap: 10, marginTop: 10, alignItems: "stretch" }}>
        <label className="stack grow" style={{ gap: 4 }}><span className="small muted">{L(lang, "Benchmark: learn from (one per line)", "Referencia: aprender de (una por línea)")}</span>
          <textarea rows={3} value={lines(t.benchmark.learn)} onChange={(e) => setT({ ...t, benchmark: { ...t.benchmark, learn: unlines(e.target.value) } })} /></label>
        <label className="stack grow" style={{ gap: 4 }}><span className="small muted">{L(lang, "Avoid (one per line)", "Evitar (una por línea)")}</span>
          <textarea rows={3} value={lines(t.benchmark.avoid)} onChange={(e) => setT({ ...t, benchmark: { ...t.benchmark, avoid: unlines(e.target.value) } })} /></label>
      </div>
      <h3 className="small" style={{ marginTop: 14 }}>{L(lang, "Shot list", "Lista de planos")}</h3>
      <datalist id="tr-framings">{FRAMINGS.map((f) => <option key={f} value={f} />)}</datalist>
      <div className="tr-table">
        <div className="tr-row head small muted"><span>#</span><span>{L(lang, "Framing", "Encuadre")}</span><span>{L(lang, "Camera", "Cámara")}</span><span>s</span><span>{L(lang, "Why", "Por qué")}</span><span /></div>
        {t.shot_list.map((s, i) => (
          <div key={i} className="tr-row">
            <input value={s.key} onChange={(e) => shot(i, { key: e.target.value })} aria-label="key" />
            <input list="tr-framings" value={s.framing} onChange={(e) => shot(i, { framing: e.target.value })} />
            <input value={s.camera} onChange={(e) => shot(i, { camera: e.target.value })} />
            <input type="number" min={0.3} step={0.1} value={s.duration_s} onChange={(e) => shot(i, { duration_s: Number(e.target.value) })} />
            <input value={s.why} onChange={(e) => shot(i, { why: e.target.value })} />
            <button className="btn sm icon ghost" onClick={() => setT({ ...t, shot_list: t.shot_list.filter((_, j) => j !== i) })} aria-label={L(lang, "Remove", "Quitar")}><Trash2 size={13} /></button>
          </div>
        ))}
      </div>
      <button className="btn sm ghost" onClick={() => setT({ ...t, shot_list: [...t.shot_list, { key: String(t.shot_list.length + 1), framing: "medium", camera: "static", duration_s: 3, why: "" }] })}>
        <Plus size={13} /> {L(lang, "Add shot", "Añadir plano")}</button>
      <h3 className="small" style={{ marginTop: 14 }}>{L(lang, "Beat sheet / cue map", "Mapa de momentos")}</h3>
      <div className="tr-table beats">
        {t.beat_sheet.map((b, i) => (
          <div key={i} className="tr-row">
            <input type="number" min={0} step={0.1} value={b.t_s} onChange={(e) => beat(i, { t_s: Number(e.target.value) })} aria-label="t" />
            <input value={b.section} onChange={(e) => beat(i, { section: e.target.value })} />
            <input value={b.cue} onChange={(e) => beat(i, { cue: e.target.value })} />
            <button className="btn sm icon ghost" onClick={() => setT({ ...t, beat_sheet: t.beat_sheet.filter((_, j) => j !== i) })} aria-label={L(lang, "Remove", "Quitar")}><Trash2 size={13} /></button>
          </div>
        ))}
      </div>
      <button className="btn sm ghost" onClick={() => setT({ ...t, beat_sheet: [...t.beat_sheet, { t_s: 0, section: "", cue: "" }] })}><Plus size={13} /> {L(lang, "Add cue", "Añadir momento")}</button>
      <div className="row wrap" style={{ gap: 8, marginTop: 14 }}>
        <button className="btn sm" disabled={!!busy} onClick={board} data-testid="storyboard-sheet">
          {busy === "sheet" ? <Loader2 size={13} className="spin" /> : <LayoutGrid size={13} />} {L(lang, "Storyboard sheet", "Hoja de storyboard")}</button>
        <label className="row small" style={{ gap: 5 }}><input type="checkbox" checked={generate} onChange={(e) => setGenerate(e.target.checked)} />
          {L(lang, "Generate missing stills with ComfyUI", "Generar con ComfyUI los fotogramas que faltan")}</label>
      </div>
      {sheet && <img className="tr-sheet" src={fileUrl(sheet)} alt={L(lang, "Storyboard sheet", "Hoja de storyboard")} onClick={() => app.openAsset(sheet)} />}
    </div>
  );
}
