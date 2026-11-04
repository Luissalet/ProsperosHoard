// Continuity bible: decisions that survive versions (entries with revisions, shots with accepted clips and the revision they
// used), repair of one time range of a video, the join review of its seams and colour/wording consistency warnings.
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2, Plus, RefreshCw, ShieldCheck, Trash2, Wrench, XCircle } from "lucide-react";
import { api, fileUrl, type Asset, type ConsistencyReport, type ContinuityBible, type JoinReview, type RegeneratePlan, type RegionReview,
  type RepairResult } from "../api";
import { useT } from "../i18n";
import { useApp, useAsync } from "../components/ui";

const L = (lang: string, en: string, es: string) => (lang === "es" ? es : en);
const KINDS = ["object", "character", "voice", "style"];
const fmtS = (v: number) => (Math.round(v * 100) / 100).toString();
const spans = (ranges: [number, number][]) => ranges.map(([a, b]) => `${fmtS(a)}–${fmtS(b)} s`).join(", ");
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

function RegeneratePanel({ plan, shot, videos, take, setTake, busy, onRun, onUse, onClose }: {
  plan: RegeneratePlan; shot: string; videos: Asset[]; take: string; setTake: (v: string) => void; busy: boolean;
  onRun: () => void; onUse: () => void; onClose: () => void;
}) {
  const { lang } = useT();
  const item = plan.items.find((i) => i.shot_id === shot);
  if (!item) return <p className="small muted" data-testid="cb-regen">{L(lang, "This shot is not stale any more.", "Este plano ya no está obsoleto.")} <button className="btn sm" onClick={onClose}>OK</button></p>;
  return (
    <div className="verify-report" data-testid="cb-regen">
      <div className="row wrap" style={{ gap: 8 }}><RefreshCw size={15} /> <strong>{L(lang, "Regenerate", "Regenerar")} {shot}</strong>
        <button className="btn sm" onClick={onClose}>{L(lang, "Close", "Cerrar")}</button></div>
      {item.error ? <p className="small"><AlertTriangle size={13} className="wf-warn" /> {item.error}</p> : (<>
        <p className="small">{L(lang, "New take", "Toma nueva")}: {item.seconds} s · {L(lang, "used over", "se usa en")} {spans(item.ranges_s || [])}
          {(item.kept_ranges_s || []).length > 0 && <> · {L(lang, "kept as it is", "se conserva")} {spans(item.kept_ranges_s || [])}</>}</p>
        <p className="small muted">{L(lang, "Prompt with the current revisions", "Prompt con las revisiones vigentes")}: <span className="mono">{item.prompt}</span></p>
        <p className="small muted">{L(lang, "References", "Referencias")}: {(item.references || []).join(", ") || "—"}
          {item.start_image && <> · {L(lang, "start frame", "fotograma inicial")} {item.start_image}</>}
          {" · "}{Object.entries(item.entries || {}).map(([e, r]) => `${e} r${r}`).join(", ")}</p>
        <div className="row wrap" style={{ gap: 8 }}>
          <button className="btn primary sm" disabled={busy} onClick={onRun} data-testid="cb-regen-run"
            title={L(lang, "A start frame and a clip through the render queue (ComfyUI and a video model)", "Un fotograma inicial y un clip por la cola de render (ComfyUI y un modelo de vídeo)")}>
            <RefreshCw size={13} /> {L(lang, "Make a new take", "Hacer una toma nueva")}</button>
          <select value={take} onChange={(e) => setTake(e.target.value)} aria-label={L(lang, "Take made elsewhere", "Toma hecha en otro sitio")} data-testid="cb-regen-take">
            <option value="">{L(lang, "Or use a take already made…", "O usar una toma ya hecha…")}</option>
            {videos.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}{a.duration_s ? ` · ${a.duration_s.toFixed(1)} s` : ""}</option>)}</select>
          <button className="btn sm" disabled={busy || !take} onClick={onUse} data-testid="cb-regen-use">{L(lang, "Use this take", "Usar esta toma")}</button>
        </div>
        <p className="small muted">{plan.note}</p>
      </>)}
    </div>
  );
}

function RegenerateOutcome({ out }: { out: Record<string, unknown> }) {
  const { lang } = useT();
  const results = (out.results as Record<string, unknown>[] | undefined) || [out];
  return (
    <div className="small" data-testid="cb-regen-out">{results.map((r, i) => (
      <p key={i}>{r.ok === false ? <XCircle size={13} className="wf-bad" /> : <CheckCircle2 size={13} className="wf-ok" />} {String(r.shot_id)}:{" "}
        {r.ok === false ? String(r.error) : <>{L(lang, "new take", "toma nueva")} {String(r.take_asset_id)} → {String(r.result_asset_id)}
          {" · "}{r.up_to_date ? L(lang, "up to date", "al día") : L(lang, "still stale", "sigue obsoleto")}</>}</p>))}</div>
  );
}

function RegionReviewCard({ review }: { review: RegionReview }) {
  const { lang } = useT();
  const app = useApp();
  const ES: Record<string, string> = { outside_preserved: "Imagen fuera de la región sin cambios", region_edge: "Borde de la región", region_changed: "Región sustituida" };
  return (
    <div className="verify-report" data-testid="cb-region-review">
      <div className="row wrap" style={{ gap: 8 }}><ShieldCheck size={16} /> <strong>{L(lang, "Region review", "Revisión de la región")}</strong>
        <span className={`pill ${review.failures ? "bad" : review.warnings ? "warn" : "ok"}`}>{review.rect_px.join(" · ")} px</span></div>
      <table className="verify-table small"><tbody>{review.checks.map((c) => <tr key={c.id} className={c.status}>
        <td>{c.status === "pass" ? <CheckCircle2 size={14} className="wf-ok" /> : c.status === "warn" ? <AlertTriangle size={14} className="wf-warn" /> : <XCircle size={14} className="wf-bad" />}</td>
        <td>{lang === "es" ? ES[c.id] || c.label : c.label}</td><td className="mono">{String(c.expected)}</td><td className="mono">{String(c.actual)}</td></tr>)}</tbody></table>
      {review.preview_asset_id && <img className="tr-sheet" style={{ maxWidth: 320 }} src={fileUrl(review.preview_asset_id)} alt={L(lang, "Repaired frame with the region", "Fotograma reparado con la región")}
        onClick={() => app.openAsset(review.preview_asset_id!)} />}
      <p className="small muted">{review.note}</p>
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
  const [rep, setRep] = useState({ asset: "", replacement: "", start: "", end: "", shot: "", transition: "cut", crossfade: 0.3, repIn: "" });
  const [jobId, setJobId] = useState<string | null>(null);
  const [result, setResult] = useState<RepairResult | null>(null);
  const [consistency, setConsistency] = useState<ConsistencyReport | null>(null);
  // region repair (only a rectangle of the frame) and a line in a bible voice for the repaired range
  const [area, setArea] = useState({ mode: "frame", x: 0.25, y: 0.25, w: 0.5, h: 0.5, feather: 0, correction: "" });
  const [voiceLine, setVoiceLine] = useState({ entry: "", text: "", at: 0 });
  // regenerating a stale shot: the plan, a new take made by the studio or given, spliced over the stale ranges only
  const [regen, setRegen] = useState<{ shot: string; plan: RegeneratePlan } | null>(null);
  const [regenTake, setRegenTake] = useState("");
  const [regenJobId, setRegenJobId] = useState<string | null>(null);
  const [regenOut, setRegenOut] = useState<Record<string, unknown> | null>(null);
  const regenJob = app.jobs.find((j) => j.id === regenJobId);
  const job = app.jobs.find((j) => j.id === jobId);
  const reload = async (id = bibleId) => { if (id) setBible(await api.continuity(id)); reloadList(); };
  useEffect(() => { if (bibleId) api.continuity(bibleId).then(setBible).catch(() => undefined); }, [bibleId]);
  useEffect(() => {
    if (!list?.items.length || bibleId) return;
    setBibleId(list.items[0].bible_id);
  }, [list]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!regenJob || !["done", "failed", "cancelled"].includes(regenJob.state)) return;
    if (regenJob.state === "done") { setRegenOut((regenJob.outputs?.regenerate as Record<string, unknown>) || null); setRegen(null); reload().catch(() => undefined); app.bump(); }
    else app.toast(regenJob.message || regenJob.state, "bad");
    setRegenJobId(null);
  }, [regenJob?.state]); // eslint-disable-line react-hooks/exhaustive-deps
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
    const region = area.mode === "region" ? [area.x, area.y, area.w, area.h] : area.mode === "correction" ? "correction" : undefined;
    const voice = voiceLine.entry && voiceLine.text.trim() ? { entry_id: voiceLine.entry, text: voiceLine.text.trim(), at_s: voiceLine.at } : undefined;
    const r = await api.continuityRepair({ asset_id: rep.asset, replacement_asset_id: rep.replacement, start_s: num(rep.start), end_s: num(rep.end),
      shot_id: rep.shot || undefined, bible_id: bibleId || undefined, transition: rep.transition, crossfade_s: rep.crossfade,
      replacement_in_s: num(rep.repIn) ?? 0, region, feather_px: region ? area.feather : 0, correction_id: area.mode === "correction" ? area.correction || undefined : undefined, voice });
    setJobId(r.job.id); app.refreshJobs(); app.toast(L(lang, "Repairing the segment", "Reparando el tramo"), "info");
  });
  const check = () => act("check", async () => setConsistency(await api.continuityConsistency(bibleId)));
  const planRegen = (sid: string) => act("regen", async () => {
    setRegenOut(null); setRegenTake("");
    setRegen({ shot: sid, plan: await api.continuityRegenerate(bibleId, { action: "plan", shot_ids: [sid] }) });
  });
  const startRegen = (body: Record<string, unknown>) => act("regen", async () => {
    const r = await api.continuityRegenerate(bibleId, body);
    if (r.job) { setRegenJobId(r.job.id); app.refreshJobs(); app.toast(L(lang, "Regenerating the stale ranges", "Regenerando los tramos obsoletos"), "info"); }
  });
  const removeBible = () => {
    const b = (list?.items || []).find((x) => x.bible_id === bibleId);
    if (!b || !window.confirm(L(lang, `Delete the bible "${b.name}"? Its entries, shots, corrections and provenance go; the clips and reference images stay in the project.`,
      `¿Borrar la biblia «${b.name}»? Se van sus entradas, planos, correcciones y procedencia; los clips y las imágenes de referencia se quedan en el proyecto.`))) return;
    act("delete", async () => { await api.deleteContinuity(bibleId); setBible(null); setBibleId(""); reloadList(); });
  };
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
          <button className="btn" disabled={!bibleId || !!busy} onClick={removeBible} data-testid="cb-delete"><Trash2 size={14} /> {L(lang, "Delete bible", "Borrar biblia")}</button>
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
            `${m.shot_id} (${m.reasons.map((r) => r.entry_id ? `${r.entry_id} r${r.used_revision ?? "–"}→r${r.current_revision ?? "–"}` : r.why).join(", ")}` +
            (m.stale_ranges_s ? `; ${L(lang, "still old in", "aún antiguo en")} ${spans(m.stale_ranges_s)}` +
              (m.repaired_ranges_s?.length ? `; ${L(lang, "repaired", "reparado")} ${spans(m.repaired_ranges_s)}` : "") : "") + ")").join("; ")}</p>}
          {bible.must_regenerate.some((m) => m.stale_ranges_s) && <p className="small muted" data-testid="cb-stale-repair-note">{L(lang,
            "A repair counts as made with the bible's current revisions only inside the range it replaced; the rest of the shot keeps the revisions of the original clip. Repair the ranges still listed, or, if they do not show the changed entry, accept the repaired video as the shot's clip.",
            "Una reparación cuenta como hecha con las revisiones vigentes solo dentro del tramo que sustituyó; el resto del plano conserva las del clip original. Repara los tramos que siguen en la lista o, si en ellos no aparece la entrada cambiada, acepta el vídeo reparado como clip del plano.")}</p>}
          <table className="verify-table small"><thead><tr><th>id</th><th>s</th><th>{L(lang, "Description", "Descripción")}</th><th>{L(lang, "Uses", "Usa")}</th><th>{L(lang, "Clip", "Clip")}</th><th /></tr></thead>
            <tbody>{Object.values(bible.shots).map((s) => <tr key={s.id} className={stale.has(s.id) ? "warn" : ""}>
              <td className="mono">{s.id}</td><td>{s.start_s ?? "–"}–{s.end_s ?? "–"}</td><td>{s.description}</td><td>{s.uses.join(", ")}</td>
              <td>{s.clip ? <>{s.clip.asset_id} {stale.has(s.id) ? <span className="pill warn">{L(lang, "stale", "obsoleto")}</span> : <span className="pill ok">{L(lang, "accepted", "aceptado")}</span>}
                {(s.clip.segments || []).length > 0 && <div className="muted" data-testid={`cb-segments-${s.id}`}>{L(lang, "Repaired", "Reparado")}: {(s.clip.segments || []).map((g) =>
                  `${fmtS(g.start_s)}–${fmtS(g.end_s)} s (${Object.entries(g.entries).filter(([e]) => e !== "identity").map(([e, r]) => `${e} r${r}`).join(", ") || "identity"})`).join("; ")}</div>}
                {stale.has(s.id) && (s.clip.segments || []).length > 0 && <button className="btn sm" disabled={!!busy} onClick={() => accept(s.id, s.clip!.asset_id)} data-testid={`cb-accept-repaired-${s.id}`}
                  title={L(lang, "Accept the repaired video as the whole shot with the current revisions", "Aceptar el vídeo reparado como el plano entero con las revisiones vigentes")}>{L(lang, "Accept repaired video", "Aceptar vídeo reparado")}</button>}
                {stale.has(s.id) && <button className="btn sm" disabled={!!busy || !!regenJobId} onClick={() => planRegen(s.id)} data-testid={`cb-regen-plan-${s.id}`}
                  title={L(lang, "A new take made with the current revisions, used only over the stale ranges", "Una toma nueva hecha con las revisiones vigentes, usada solo en los tramos obsoletos")}>
                  <RefreshCw size={13} /> {L(lang, "Regenerate…", "Regenerar…")}</button>}</> : "–"}</td>
              <td><select value="" onChange={(e) => e.target.value && accept(s.id, e.target.value)} aria-label={L(lang, "Accept clip", "Aceptar clip")}><option value="">{L(lang, "Accept clip…", "Aceptar clip…")}</option>
                {videos.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}</option>)}</select></td></tr>)}</tbody></table>
          {regen && <RegeneratePanel plan={regen.plan} shot={regen.shot} videos={videos} take={regenTake} setTake={setRegenTake}
            busy={!!busy || !!regenJobId} onRun={() => startRegen({ action: "run", shot_ids: [regen.shot] })}
            onUse={() => startRegen({ action: "use_clip", shot_id: regen.shot, clip_asset_id: regenTake })} onClose={() => setRegen(null)} />}
          {regenJob && <p className="small muted" data-testid="cb-regen-job">{regenJob.state} · {Math.round((regenJob.progress || 0) * 100)}% {regenJob.message || ""}</p>}
          {regenOut && <RegenerateOutcome out={regenOut} />}
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
          <select value={rep.asset} onChange={(e) => setRep({ ...rep, asset: e.target.value })} data-testid="cb-rep-asset" aria-label={L(lang, "Video", "Vídeo")}><option value="">{L(lang, "Video", "Vídeo")}…</option>
            {videos.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}{a.duration_s ? ` · ${a.duration_s.toFixed(1)} s` : ""}</option>)}</select>
          <select value={rep.replacement} onChange={(e) => setRep({ ...rep, replacement: e.target.value })} data-testid="cb-rep-replacement" aria-label={L(lang, "Replacement", "Sustituto")}><option value="">{L(lang, "Replacement", "Sustituto")}…</option>
            {videos.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}</option>)}</select>
          <input type="number" step={0.1} min={0} placeholder="start s" data-testid="cb-rep-start" value={rep.start} onChange={(e) => setRep({ ...rep, start: e.target.value })} />
          <input type="number" step={0.1} min={0} placeholder="end s" data-testid="cb-rep-end" value={rep.end} onChange={(e) => setRep({ ...rep, end: e.target.value })} />
          <input type="number" step={0.1} min={0} placeholder={L(lang, "replacement from s", "sustituto desde s")} title={L(lang, "Where the replacement starts being used", "Desde qué segundo del sustituto se usa")}
            value={rep.repIn} onChange={(e) => setRep({ ...rep, repIn: e.target.value })} data-testid="cb-rep-in" />
          {bible && <select value={rep.shot} onChange={(e) => setRep({ ...rep, shot: e.target.value })} data-testid="cb-rep-shot" aria-label={L(lang, "Shot", "Plano")}><option value="">{L(lang, "Range above", "Rango de arriba")}</option>
            {Object.keys(bible.shots).map((s) => <option key={s} value={s}>{s}</option>)}</select>}
          <select value={rep.transition} onChange={(e) => setRep({ ...rep, transition: e.target.value })}><option value="cut">{L(lang, "Hard cut", "Corte seco")}</option><option value="crossfade">{L(lang, "Crossfade", "Fundido encadenado")}</option></select>
          {rep.transition === "crossfade" && <input type="number" min={0.1} max={2} step={0.1} value={rep.crossfade} onChange={(e) => setRep({ ...rep, crossfade: Number(e.target.value) })} />}
          <button className="btn primary" disabled={!rep.asset || !rep.replacement || !!busy || !!jobId} onClick={repair} data-testid="cb-repair">
            {busy === "repair" || jobId ? <Loader2 size={14} className="spin" /> : <Wrench size={14} />} {L(lang, "Repair", "Reparar")}</button>
          {job && <span className="small muted">{job.state} · {Math.round((job.progress || 0) * 100)}% {job.message || ""}</span>}
        </div>
        <div className="ep-row" style={{ marginTop: 8 }} data-testid="cb-area">
          <select value={area.mode} onChange={(e) => setArea({ ...area, mode: e.target.value })} aria-label={L(lang, "What to replace", "Qué sustituir")} data-testid="cb-area-mode">
            <option value="frame">{L(lang, "Whole frame", "Fotograma entero")}</option>
            <option value="region">{L(lang, "Only a region", "Solo una región")}</option>
            <option value="correction" disabled={!(bible?.corrections || []).some((c) => c.region)}>{L(lang, "The region of a correction", "La región de una corrección")}</option>
          </select>
          {area.mode === "region" && (["x", "y", "w", "h"] as const).map((k) => (
            <label key={k} className="small">{k} <input type="number" min={0} max={1} step={0.05} style={{ width: 70 }} value={area[k]}
              onChange={(e) => setArea({ ...area, [k]: Number(e.target.value) })} data-testid={`cb-area-${k}`} /></label>))}
          {area.mode === "correction" && <select value={area.correction} onChange={(e) => setArea({ ...area, correction: e.target.value })} data-testid="cb-area-correction" aria-label={L(lang, "Correction", "Corrección")}>
            <option value="">{L(lang, "Correction…", "Corrección…")}</option>
            {(bible?.corrections || []).filter((c) => c.region).map((c) => <option key={c.id} value={c.id}>{c.id} · {c.text}</option>)}</select>}
          {area.mode !== "frame" && <label className="small">{L(lang, "soft edge px", "borde suave px")} <input type="number" min={0} max={40} step={1} style={{ width: 60 }} value={area.feather}
            onChange={(e) => setArea({ ...area, feather: Number(e.target.value) })} /></label>}
          {area.mode !== "frame" && <span className="small muted">{L(lang, "The rest of the frame and the sound stay the original.", "El resto del fotograma y el sonido se quedan como estaban.")}</span>}
        </div>
        {bible && Object.values(bible.entries).some((e) => e.kind === "voice") && (
          <div className="ep-row" style={{ marginTop: 8 }} data-testid="cb-voice">
            <select value={voiceLine.entry} onChange={(e) => setVoiceLine({ ...voiceLine, entry: e.target.value })} aria-label={L(lang, "Voice of the bible", "Voz de la biblia")} data-testid="cb-voice-entry">
              <option value="">{L(lang, "No new line", "Sin frase nueva")}</option>
              {Object.entries(bible.entries).filter(([, e]) => e.kind === "voice").map(([id, e]) => <option key={id} value={id}>{e.name || id} · r{e.revision}</option>)}</select>
            {voiceLine.entry && <>
              <input className="grow" placeholder={L(lang, "What the voice says in the range", "Lo que dice la voz en el tramo")} value={voiceLine.text}
                onChange={(e) => setVoiceLine({ ...voiceLine, text: e.target.value })} data-testid="cb-voice-text" />
              <label className="small">{L(lang, "from s", "desde s")} <input type="number" min={0} step={0.1} style={{ width: 70 }} value={voiceLine.at}
                onChange={(e) => setVoiceLine({ ...voiceLine, at: Number(e.target.value) })} /></label></>}
            <span className="small muted">{L(lang, "Spoken with the entry's voice and settings, placed in the range (sped up to 1.5x if it is long).", "Se dice con la voz y los ajustes de la entrada y se coloca en el tramo (hasta 1,5x más rápido si es larga).")}</span>
          </div>)}
        {result && (<div data-testid="cb-result">
          <div className="row wrap" style={{ gap: 12, alignItems: "flex-start", marginTop: 10 }}>
            <div className="video-frame" style={{ width: 300 }}><video src={fileUrl(result.asset_id)} controls preload="metadata" poster={`/generation/api/assets/${result.asset_id}/thumb`} /></div>
            <div className="grow small">
              <p>{result.range_s[0]}–{result.range_s[1]} s · {result.exact_outside ? L(lang, "frames outside the touched GOPs are identical", "los fotogramas fuera de los GOP tocados son idénticos") : L(lang, "everything was re-encoded", "se recodificó todo")}</p>
              {result.untouched && <p className="muted">{result.untouched.frames_identical}/{result.untouched.frames_compared} {L(lang, "frames compared identical; re-encoded", "fotogramas idénticos; recodificado")} {result.untouched.reencoded_range_s[0]}–{result.untouched.reencoded_range_s[1]} s</p>}
              {result.notes.map((n) => <p key={n} className="muted">{n}</p>)}
              {result.voice && <p data-testid="cb-result-voice">{L(lang, "Voice", "Voz")} {result.voice.name} r{result.voice.revision} ({result.voice.engine_id}): «{result.voice.text}» · {result.voice.spoken_s} s
                {result.voice.speed > 1 ? ` · ${result.voice.speed}x` : ""} · {L(lang, "from", "desde")} {result.voice.at_s} s{result.voice.cut ? ` · ${L(lang, "cut at the end of the range", "cortada al final del tramo")}` : ""}</p>}
            </div>
          </div>
          {result.region_review && <RegionReviewCard review={result.region_review} />}
          {result.join_review && <JoinReviewCard review={result.join_review} />}
        </div>)}
      </div>
    </div>
  );
}
