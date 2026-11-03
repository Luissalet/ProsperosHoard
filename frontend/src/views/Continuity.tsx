// Continuity bible: decisions that survive versions (entries with revisions, shots with accepted clips and the revision they
// used), repair of one time range of a video, the join review of its seams and colour/wording consistency warnings.
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, Plus, ShieldCheck, Wrench, XCircle } from "lucide-react";
import { api, fileUrl, type Asset, type ConsistencyReport, type ContinuityBible, type JoinReview, type RepairResult } from "../api";
import { useT } from "../i18n";
import { useApp, useAsync } from "../components/ui";

const L = (lang: string, en: string, es: string) => (lang === "es" ? es : en);
const KINDS = ["object", "character", "voice", "style"];
const CHECK_ES: Record<string, string> = {
  black_frames: "Fotogramas negros", frozen_frames: "Fotogramas congelados", colour_jump: "Salto de color", audio_level_jump: "Salto de nivel de audio",
  audio_click: "Chasquido de audio", duration_preserved: "Duración conservada", frames_preserved: "Fotogramas conservados",
};

function JoinReviewCard({ review }: { review: JoinReview }) {
  const { lang } = useT();
  const app = useApp();
  const icon = (s: string) => s === "pass" ? <CheckCircle2 size={14} className="wf-ok" /> : s === "warn" ? <AlertTriangle size={14} className="wf-warn" /> : <XCircle size={14} className="wf-bad" />;
  const rows = (checks: JoinReview["overall"]) => checks.map((c) => <tr key={c.id} className={c.status}><td>{icon(c.status)}</td>
    <td>{lang === "es" ? CHECK_ES[c.id] || c.label : c.label}</td><td className="mono">{String(c.expected)}</td>
    <td className="mono">{typeof c.actual === "object" ? JSON.stringify(c.actual) : String(c.actual)}</td></tr>);
  return (
    <div className="verify-report" data-testid="join-review">
      <div className="row wrap" style={{ gap: 8 }}><ShieldCheck size={16} /> <strong>{L(lang, "Join review", "Revisión de uniones")}</strong>
        <span className={`pill ${review.failures ? "bad" : review.warnings ? "warn" : "ok"}`}>
          {review.failures ? L(lang, `${review.failures} failure(s)`, `${review.failures} fallo(s)`) : review.warnings ? L(lang, `${review.warnings} warning(s)`, `${review.warnings} aviso(s)`) : L(lang, "no findings", "sin hallazgos")}</span></div>
      <p className="small muted">{L(lang, "Automatic heuristics. A pass does not approve the edit: look at the strips.", "Heurísticas automáticas. Que pase no aprueba el montaje: mira las tiras.")}</p>
      {review.seams.map((s) => (
        <div key={s.frame}>
          <h4 className="small">{s.t_s.toFixed(2)} s · {L(lang, "frame", "fotograma")} {s.frame}</h4>
          <table className="verify-table small"><tbody>{rows(s.checks)}</tbody></table>
          {s.strip_asset_id && <img className="tr-sheet" src={fileUrl(s.strip_asset_id)} alt={L(lang, "Frames around the cut", "Fotogramas alrededor del corte")} onClick={() => app.openAsset(s.strip_asset_id!)} />}
        </div>
      ))}
      {review.overall.length > 0 && <table className="verify-table small"><tbody>{rows(review.overall)}</tbody></table>}
    </div>
  );
}

export function ContinuityView() {
  const { t, lang } = useT();
  const app = useApp();
  const projectId = app.projectId!;
  const { data: list, reload: reloadList } = useAsync(() => api.continuityList(projectId), [projectId, app.dataVersion]);
  const { data: assetPage } = useAsync(() => api.assets(projectId, { limit: 100 }), [projectId, app.dataVersion]);
  const videos = (assetPage?.items || []).filter((a: Asset) => a.kind === "video");
  const images = (assetPage?.items || []).filter((a: Asset) => a.kind === "image");
  const [bibleId, setBibleId] = useState("");
  const [bible, setBible] = useState<ContinuityBible | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [entry, setEntry] = useState({ id: "", kind: "object", name: "", descriptor: "", ref: "" });
  const [shot, setShot] = useState({ id: "", start_s: "", end_s: "", description: "", uses: "" });
  const [rep, setRep] = useState({ asset: "", replacement: "", start: "", end: "", shot: "", transition: "cut", crossfade: 0.3 });
  const [jobId, setJobId] = useState<string | null>(null);
  const [result, setResult] = useState<RepairResult | null>(null);
  const [consistency, setConsistency] = useState<ConsistencyReport | null>(null);
  const job = app.jobs.find((j) => j.id === jobId);
  const reload = async (id = bibleId) => { if (id) setBible(await api.continuity(id)); reloadList(); };
  useEffect(() => { if (bibleId) api.continuity(bibleId).then(setBible).catch(() => undefined); }, [bibleId]);
  useEffect(() => {
    if (!list?.items.length || bibleId) return;
    setBibleId(list.items[0].bible_id);
  }, [list]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!job || !["done", "failed", "cancelled"].includes(job.state)) return;
    if (job.state === "done") { setResult((job.outputs?.repair as RepairResult) || null); reload().catch(() => undefined); app.bump(); }
    else app.toast(job.message || job.state, "bad");
    setJobId(null);
  }, [job?.state]); // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    try { await fn(); } catch (e) { app.toast((e as Error).message, "bad"); } finally { setBusy(null); }
  };
  const num = (v: string) => (v === "" ? undefined : Number(v));
  const create = () => act("create", async () => { const b = await api.createContinuity(projectId, name || "Continuity", undefined); setName(""); setBibleId(b.bible_id); await reload(b.bible_id); });
  const saveEntry = () => act("entry", async () => {
    const fields: Record<string, unknown> = { kind: entry.kind, name: entry.name, descriptor: entry.descriptor };
    if (entry.ref) fields.reference_asset_ids = [entry.ref];
    const r = await api.continuityEntry(bibleId, entry.id, fields);
    app.toast(r.changed.length ? L(lang, `Revision ${r.revision}; ${r.newly_stale.length} shot(s) to regenerate`, `Revisión ${r.revision}; ${r.newly_stale.length} plano(s) por regenerar`) : L(lang, "No change", "Sin cambios"), "info");
    await reload();
  });
  const saveShot = () => act("shot", async () => {
    await api.continuityShot(bibleId, shot.id, { start_s: num(shot.start_s), end_s: num(shot.end_s), description: shot.description,
      uses: shot.uses.split(",").map((s) => s.trim()).filter(Boolean) });
    await reload();
  });
  const accept = (sid: string, assetId: string) => act("accept", async () => { await api.continuityAcceptClip(bibleId, sid, assetId); await reload(); });
  const repair = () => act("repair", async () => {
    const r = await api.continuityRepair({ asset_id: rep.asset, replacement_asset_id: rep.replacement, start_s: num(rep.start), end_s: num(rep.end),
      shot_id: rep.shot || undefined, bible_id: bibleId || undefined, transition: rep.transition, crossfade_s: rep.crossfade });
    setJobId(r.job.id); app.refreshJobs(); app.toast(L(lang, "Repairing the segment", "Reparando el tramo"), "info");
  });
  const check = () => act("check", async () => setConsistency(await api.continuityConsistency(bibleId)));
  const stale = new Set(bible?.stale_shots || []);

  return (
    <div className="stack continuity">
      <div className="card">
        <h2><ShieldCheck size={17} /> {t("navContinuity")}</h2>
        <p className="small muted">{L(lang, "The bible keeps the decisions of a production: identity, recurring objects, characters and voices, and which version of each decision every accepted clip used. Change a decision and only the shots that used it become stale.",
          "La biblia guarda las decisiones de una producción: identidad, objetos, personajes y voces recurrentes, y con qué versión de cada decisión se hizo cada clip aceptado. Cambia una decisión y solo quedan obsoletos los planos que la usaron.")}</p>
        <div className="row wrap" style={{ gap: 8 }}>
          <select value={bibleId} onChange={(e) => setBibleId(e.target.value)} aria-label={L(lang, "Bible", "Biblia")}><option value="">—</option>
            {(list?.items || []).map((b) => <option key={b.bible_id} value={b.bible_id}>{b.name} · r{b.revision}{b.stale_shots.length ? ` · ${b.stale_shots.length} ${L(lang, "stale", "obsoletos")}` : ""}</option>)}</select>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={L(lang, "New bible name", "Nombre de la biblia nueva")} />
          <button className="btn" disabled={!!busy} onClick={create} data-testid="cb-create">{busy === "create" ? <Loader2 size={14} className="spin" /> : <Plus size={14} />} {L(lang, "Create", "Crear")}</button>
        </div>
      </div>
      {bible && (<>
        <div className="card">
          <h3>{L(lang, "Entries", "Entradas")} <span className="small muted">r{bible.revision}</span></h3>
          <table className="verify-table small"><thead><tr><th>id</th><th>{L(lang, "Kind", "Tipo")}</th><th>{L(lang, "Name", "Nombre")}</th><th>{L(lang, "Descriptor", "Descriptor")}</th><th>{L(lang, "Rev.", "Rev.")}</th></tr></thead>
            <tbody>{Object.entries(bible.entries).map(([id, e]) => <tr key={id}><td className="mono">{id}</td><td>{e.kind}</td><td>{e.name}</td><td>{e.descriptor || (e.palette || []).join(" ")}</td><td>{e.revision}</td></tr>)}</tbody></table>
          <div className="ep-row" style={{ marginTop: 8 }}>
            <input placeholder="id" value={entry.id} onChange={(e) => setEntry({ ...entry, id: e.target.value })} />
            <select value={entry.kind} onChange={(e) => setEntry({ ...entry, kind: e.target.value })}>{KINDS.map((k) => <option key={k}>{k}</option>)}</select>
            <input placeholder={L(lang, "Name", "Nombre")} value={entry.name} onChange={(e) => setEntry({ ...entry, name: e.target.value })} />
            <input className="grow" placeholder={L(lang, "Descriptor", "Descriptor")} value={entry.descriptor} onChange={(e) => setEntry({ ...entry, descriptor: e.target.value })} />
            <select value={entry.ref} onChange={(e) => setEntry({ ...entry, ref: e.target.value })} aria-label={L(lang, "Reference image", "Imagen de referencia")}><option value="">{L(lang, "No reference", "Sin referencia")}</option>
              {images.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}</option>)}</select>
            <button className="btn sm" disabled={!entry.id || !!busy} onClick={saveEntry} data-testid="cb-entry">{L(lang, "Save entry", "Guardar entrada")}</button>
          </div>
        </div>
        <div className="card">
          <h3>{L(lang, "Shots", "Planos")}</h3>
          {bible.must_regenerate.length > 0 && <p className="small" data-testid="cb-stale"><AlertTriangle size={13} className="wf-warn" /> {L(lang, "To regenerate", "Por regenerar")}: {bible.must_regenerate.map((m) =>
            `${m.shot_id} (${m.reasons.map((r) => r.entry_id ? `${r.entry_id} r${r.used_revision ?? "–"}→r${r.current_revision ?? "–"}` : r.why).join(", ")})`).join("; ")}</p>}
          <table className="verify-table small"><thead><tr><th>id</th><th>s</th><th>{L(lang, "Description", "Descripción")}</th><th>{L(lang, "Uses", "Usa")}</th><th>{L(lang, "Clip", "Clip")}</th><th /></tr></thead>
            <tbody>{Object.values(bible.shots).map((s) => <tr key={s.id} className={stale.has(s.id) ? "warn" : ""}>
              <td className="mono">{s.id}</td><td>{s.start_s ?? "–"}–{s.end_s ?? "–"}</td><td>{s.description}</td><td>{s.uses.join(", ")}</td>
              <td>{s.clip ? <>{s.clip.asset_id} {stale.has(s.id) ? <span className="pill warn">{L(lang, "stale", "obsoleto")}</span> : <span className="pill ok">{L(lang, "accepted", "aceptado")}</span>}</> : "–"}</td>
              <td><select value="" onChange={(e) => e.target.value && accept(s.id, e.target.value)} aria-label={L(lang, "Accept clip", "Aceptar clip")}><option value="">{L(lang, "Accept clip…", "Aceptar clip…")}</option>
                {videos.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}</option>)}</select></td></tr>)}</tbody></table>
          <div className="ep-row" style={{ marginTop: 8 }}>
            <input placeholder="id" value={shot.id} onChange={(e) => setShot({ ...shot, id: e.target.value })} />
            <input type="number" step={0.1} placeholder="start" value={shot.start_s} onChange={(e) => setShot({ ...shot, start_s: e.target.value })} />
            <input type="number" step={0.1} placeholder="end" value={shot.end_s} onChange={(e) => setShot({ ...shot, end_s: e.target.value })} />
            <input className="grow" placeholder={L(lang, "Description", "Descripción")} value={shot.description} onChange={(e) => setShot({ ...shot, description: e.target.value })} />
            <input placeholder={L(lang, "entries (a, b)", "entradas (a, b)")} value={shot.uses} onChange={(e) => setShot({ ...shot, uses: e.target.value })} />
            <button className="btn sm" disabled={!shot.id || !!busy} onClick={saveShot} data-testid="cb-shot">{L(lang, "Save shot", "Guardar plano")}</button>
          </div>
          <div className="row" style={{ gap: 8, marginTop: 8 }}>
            <button className="btn sm" disabled={!!busy} onClick={check} data-testid="cb-check">{busy === "check" ? <Loader2 size={14} className="spin" /> : <ShieldCheck size={14} />} {L(lang, "Check consistency", "Comprobar consistencia")}</button>
          </div>
          {consistency && <div data-testid="cb-warnings"><p className="small muted">{consistency.note}</p>
            {consistency.warnings.length === 0 ? <p className="small">{L(lang, "No warnings (this does not prove anything is the same).", "Sin avisos (eso no prueba que algo sea lo mismo).")}</p>
              : <ul className="small">{consistency.warnings.map((w, i) => <li key={i}><AlertTriangle size={12} className="wf-warn" /> {w.message}</li>)}</ul>}</div>}
        </div>
      </>)}
      <div className="card">
        <h3><Wrench size={15} /> {L(lang, "Repair a segment", "Reparar un tramo")}</h3>
        <p className="small muted">{L(lang, "Replaces a time range with another clip; the rest of the video is not redone. The new clip is conformed to the video's size, frame rate and audio.", "Sustituye un rango por otro clip; el resto del vídeo no se rehace. El clip nuevo se conforma al tamaño, los fps y el audio del vídeo.")}</p>
        <div className="ep-row">
          <select value={rep.asset} onChange={(e) => setRep({ ...rep, asset: e.target.value })} aria-label={L(lang, "Video", "Vídeo")}><option value="">{L(lang, "Video", "Vídeo")}…</option>
            {videos.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}{a.duration_s ? ` · ${a.duration_s.toFixed(1)} s` : ""}</option>)}</select>
          <select value={rep.replacement} onChange={(e) => setRep({ ...rep, replacement: e.target.value })} aria-label={L(lang, "Replacement", "Sustituto")}><option value="">{L(lang, "Replacement", "Sustituto")}…</option>
            {videos.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}</option>)}</select>
          <input type="number" step={0.1} min={0} placeholder="start s" value={rep.start} onChange={(e) => setRep({ ...rep, start: e.target.value })} />
          <input type="number" step={0.1} min={0} placeholder="end s" value={rep.end} onChange={(e) => setRep({ ...rep, end: e.target.value })} />
          {bible && <select value={rep.shot} onChange={(e) => setRep({ ...rep, shot: e.target.value })} aria-label={L(lang, "Shot", "Plano")}><option value="">{L(lang, "Range above", "Rango de arriba")}</option>
            {Object.keys(bible.shots).map((s) => <option key={s} value={s}>{s}</option>)}</select>}
          <select value={rep.transition} onChange={(e) => setRep({ ...rep, transition: e.target.value })}><option value="cut">{L(lang, "Hard cut", "Corte seco")}</option><option value="crossfade">{L(lang, "Crossfade", "Fundido encadenado")}</option></select>
          {rep.transition === "crossfade" && <input type="number" min={0.1} max={2} step={0.1} value={rep.crossfade} onChange={(e) => setRep({ ...rep, crossfade: Number(e.target.value) })} />}
          <button className="btn primary" disabled={!rep.asset || !rep.replacement || !!busy || !!jobId} onClick={repair} data-testid="cb-repair">
            {busy === "repair" || jobId ? <Loader2 size={14} className="spin" /> : <Wrench size={14} />} {L(lang, "Repair", "Reparar")}</button>
          {job && <span className="small muted">{job.state} · {Math.round((job.progress || 0) * 100)}% {job.message || ""}</span>}
        </div>
        {result && (<div data-testid="cb-result">
          <div className="row wrap" style={{ gap: 12, alignItems: "flex-start", marginTop: 10 }}>
            <div className="video-frame" style={{ width: 300 }}><video src={fileUrl(result.asset_id)} controls preload="metadata" poster={`/generation/api/assets/${result.asset_id}/thumb`} /></div>
            <div className="grow small">
              <p>{result.range_s[0]}–{result.range_s[1]} s · {result.exact_outside ? L(lang, "frames outside the touched GOPs are identical", "los fotogramas fuera de los GOP tocados son idénticos") : L(lang, "everything was re-encoded", "se recodificó todo")}</p>
              {result.untouched && <p className="muted">{result.untouched.frames_identical}/{result.untouched.frames_compared} {L(lang, "frames compared identical; re-encoded", "fotogramas idénticos; recodificado")} {result.untouched.reencoded_range_s[0]}–{result.untouched.reencoded_range_s[1]} s</p>}
              {result.notes.map((n) => <p key={n} className="muted">{n}</p>)}
            </div>
          </div>
          {result.join_review && <JoinReviewCard review={result.join_review} />}
        </div>)}
      </div>
    </div>
  );
}
