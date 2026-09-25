import { useEffect, useMemo, useState } from "react";
import { BookCopy, Clapperboard, Film, Megaphone, Play, RotateCcw, Save, ShieldCheck, Shuffle, Users } from "lucide-react";
import {
  api, fileUrl, type Character, type Project, type ProductionState, type ProductionSummary, type RecipeSummary,
} from "../api";
import { useT, type MessageKey } from "../i18n";
import { Empty, Modal, timeAgo, useApp, useAsync } from "../components/ui";
import { ShortDetail, ShortModal } from "./Shorts";

const STATUS_TONE: Record<string, string> = {
  queued: "info", running: "accent", awaiting_review: "gold", done: "ok", failed: "bad", cancelled: "", partial: "warn",
};
const STATUS_KEY: Record<string, MessageKey> = {
  queued: "stateQueued", running: "stateRunning", awaiting_review: "statusAwaiting", done: "stateDone",
  failed: "stateFailed", cancelled: "stateCancelled", partial: "statusPartial",
};
const STAGE_TONE: Record<string, string> = { done: "ok", partial: "warn", pending: "" };

export function StatusPill({ status }: { status: string }) {
  const { t } = useT();
  return <span className={`pill ${STATUS_TONE[status] || ""}`}>{t(STATUS_KEY[status] || "stateQueued")}</span>;
}

/** "Recreate with…": pick a studio character (any project) or describe a new lead, then run the recipe. */
export function RecastModal({ recipe, fromProduction, defaultTitle, onClose, onStarted }: {
  recipe?: RecipeSummary; fromProduction?: ProductionSummary; defaultTitle?: string; onClose: () => void; onStarted: (slug: string) => void;
}) {
  const { t } = useT();
  const app = useApp();
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [chars, setChars] = useState<(Character & { projectName: string })[]>([]);
  const [charId, setCharId] = useState("");
  const [name, setName] = useState("");
  const [look, setLook] = useState("");
  const [palette, setPalette] = useState("");
  const [title, setTitle] = useState(recipe?.title || defaultTitle || "");
  const [reuse, setReuse] = useState<Record<string, boolean>>({ song: true, frames: true, clips: true });
  const [qaInline, setQaInline] = useState(false);
  const [animaticFirst, setAnimaticFirst] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api.projects().then(async (r) => {
      const lists = await Promise.all(r.items.map((p: Project) => api.characters(p.id)
        .then((c) => c.items.map((ch) => ({ ...ch, projectName: p.name }))).catch(() => [])));
      if (alive) setChars(lists.flat());
    }).catch(() => undefined);
    return () => { alive = false; };
  }, []);

  const start = async () => {
    setBusy(true);
    try {
      let recipeName = recipe?.name;
      if (!recipeName && fromProduction) recipeName = (await api.exportRecipe(fromProduction.slug)).name;
      if (!recipeName) return;
      const lead = mode === "existing" ? charId
        : { name: name.trim(), look: look.trim(), palette: palette.split(",").map((c) => c.trim()).filter(Boolean) };
      const out = await api.runRecipe(recipeName, {
        cast: { lead },
        options: {
          reuse: Object.entries(reuse).filter(([, v]) => v).map(([k]) => k), ...(title.trim() ? { title: title.trim() } : {}),
          settings: { qa: { enabled: qaInline }, animatic: animaticFirst },
        },
      });
      app.toast(t("productionQueued"), "ok");
      out.notes.forEach((n) => app.toast(n, "info"));
      app.refreshJobs();
      onStarted(out.production.slug);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };
  const ready = mode === "existing" ? Boolean(charId) : Boolean(name.trim() && look.trim());

  return (
    <Modal title={t("recreateWith")} onClose={onClose}
      footer={<button className="btn primary" disabled={!ready || busy} onClick={start}><Play size={14} /> {t("startProduction")}</button>}>
      <div className="stack">
        <div className="segmented">
          <button className={mode === "existing" ? "on" : ""} onClick={() => setMode("existing")}>{t("existingCharacter")}</button>
          <button className={mode === "new" ? "on" : ""} onClick={() => setMode("new")}>{t("newLead")}</button>
        </div>
        {mode === "existing" ? (
          <select value={charId} onChange={(e) => setCharId(e.target.value)} aria-label={t("existingCharacter")}>
            <option value="">-</option>
            {chars.map((c) => <option key={c.id} value={c.id}>{c.name} · {c.projectName}</option>)}
          </select>
        ) : (
          <>
            <label className="stack" style={{ gap: 4 }}><span className="small muted">{t("leadName")}</span>
              <input value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="stack" style={{ gap: 4 }}><span className="small muted">{t("leadLook")}</span>
              <textarea rows={4} value={look} onChange={(e) => setLook(e.target.value)} /></label>
            <label className="stack" style={{ gap: 4 }}><span className="small muted">{t("leadPalette")}</span>
              <input value={palette} onChange={(e) => setPalette(e.target.value)} placeholder="#F28C28, #1B1D22" /></label>
          </>
        )}
        <label className="stack" style={{ gap: 4 }}><span className="small muted">{t("productionTitleField")}</span>
          <input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <div className="row wrap">
          <span className="small muted">{t("reuseLabel")}:</span>
          {(["song", "frames", "clips"] as const).map((k) => (
            <label key={k} className="row small" style={{ gap: 5 }}>
              <input type="checkbox" checked={reuse[k]} onChange={(e) => setReuse({ ...reuse, [k]: e.target.checked })} />
              {t(k === "song" ? "reuseSong" : k === "frames" ? "reuseFrames" : "reuseClips")}
            </label>
          ))}
        </div>
        <label className="row small" style={{ gap: 5 }}>
          <input type="checkbox" checked={animaticFirst} onChange={(e) => setAnimaticFirst(e.target.checked)} /> {t("animaticSetting")}
        </label>
        <label className="row small" style={{ gap: 5 }}>
          <input type="checkbox" checked={qaInline} onChange={(e) => setQaInline(e.target.checked)} /> {t("qaInline")}
        </label>
      </div>
    </Modal>
  );
}

function ProductionDetail({ slug, reloadList, onStarted }: { slug: string; reloadList: () => void; onStarted: (slug: string) => void }) {
  const { t } = useT();
  const app = useApp();
  const { data, reload } = useAsync(() => api.production(slug), [slug, app.dataVersion]);
  const [recast, setRecast] = useState(false);
  const active = data && ["queued", "running"].includes(data.status);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(reload, 2500);
    return () => clearInterval(id);
  }, [active, reload]);

  if (!data) return <p className="muted">{t("loading")}</p>;
  const view = data.view;
  const legacy = Boolean(view.legacy);
  const isShort = data.kind === "short";
  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); reload(); reloadList(); app.refreshJobs(); } catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const saveRecipe = () => act(async () => { const r = await api.exportRecipe(slug); app.toast(t("recipeSaved", { name: r.name }), "ok"); });
  const renders = view.renders || {};
  const frames = (data.done?.frames?.items || {}) as Record<string, { best?: string; variants?: string[] }>;
  const clips = (data.done?.clips?.items || {}) as Record<string, string>;

  return (
    <div className="stack">
      <div className="card">
        <h2><Clapperboard size={17} /> {data.name || slug} <StatusPill status={view.status} /></h2>
        <div className="row wrap" style={{ gap: 6, marginBottom: 10 }}>
          {!legacy && ["failed", "cancelled"].includes(view.status) && (
            <button className="btn sm primary" onClick={() => act(() => api.continueProduction(slug))}>
              <RotateCcw size={13} /> {t("resumeProduction")}
            </button>
          )}
          {!isShort && <button className="btn sm" onClick={saveRecipe}><Save size={13} /> {t("saveAsRecipe")}</button>}
          {!isShort && <button className="btn sm" onClick={() => setRecast(true)}><Users size={13} /> {t("recreateWith")}</button>}
          {view.project_id && <button className="btn sm ghost" onClick={() => app.setProject(view.project_id!)}>{t("openProject")}</button>}
        </div>
        {legacy && <p className="small muted">{t("legacyNote")}</p>}
        {data.recipe && <p className="small muted">{t("fromRecipe", { name: data.recipe.name })} · {data.recipe.cast.lead}</p>}
        {data.message && <p className={`small ${view.status === "failed" ? "err-text" : "muted"}`}>{data.message}</p>}
        {view.stages && (
          <div className="row wrap" style={{ gap: 6 }}>
            {Object.entries(view.stages).map(([stage, st]) => (
              <span key={stage} className={`pill ${STAGE_TONE[st] || ""}${data.stage === stage && active ? " accent" : ""}`}>
                {t((`stage_${stage}`) as MessageKey)}
              </span>
            ))}
          </div>
        )}
      </div>
      {isShort && <ShortDetail state={data} onChanged={() => { reload(); reloadList(); }} />}
      {!isShort && view.status !== "done" && <AnimaticCard state={data} onChanged={() => { reload(); reloadList(); app.refreshJobs(); }} />}
      {!isShort && Object.keys(renders).length > 0 && (
        <div className="card">
          <h2>{t("finalCut")}</h2>
          <div className="row wrap" style={{ alignItems: "flex-start" }}>
            {Object.entries(renders).map(([aspect, byQuality]) => {
              const id = byQuality.final || byQuality.preview;
              return id ? (
                <div key={aspect} className="stack" style={{ gap: 4 }}>
                  <span className="small muted">{aspect}</span>
                  <div className="video-frame" style={{ width: aspect === "16:9" ? 380 : 220 }}>
                    <video src={fileUrl(id)} controls preload="metadata" poster={`/api/assets/${id}/thumb`} />
                  </div>
                </div>
              ) : null;
            })}
          </div>
        </div>
      )}
      {!isShort && view.status === "done" && <AnimaticCard state={data} onChanged={() => { reload(); reloadList(); app.refreshJobs(); }} />}
      {!legacy && (data.spec.shots || []).length > 0 && (
        <div className="card">
          <h2>{t("shotsTitle")}</h2>
          <div className="thumb-grid">
            {(data.spec.shots || []).map((shot) => {
              const best = frames[shot.key]?.best;
              return (
                <button key={shot.key} className="tile" title={shot.prompt} onClick={() => best && app.openAsset(best, frames[shot.key]?.variants || [best])}>
                  {best ? <img src={`/api/assets/${best}/thumb`} alt="" loading="lazy" /> : <div className="media-icon"><Clapperboard size={22} /></div>}
                  <div className="tile-badges">
                    <span className="pill badge-dark">{shot.key}</span>
                    {shot.lead && <span className="pill badge-dark">{t("leadBadge")}</span>}
                    {Object.keys(clips).some((k) => k === shot.key || k.startsWith(`${shot.key}v`)) && <span className="pill badge-dark">{t("clipBadge")}</span>}
                  </div>
                  <div className="tile-meta"><span className="ellipsis grow">{shot.prompt}</span></div>
                </button>
              );
            })}
          </div>
        </div>
      )}
      {!isShort && <QaCard state={data} onRan={reload} />}
      {!legacy && data.lineage?.length > 0 && (
        <div className="card">
          <h2>{t("lineageTitle")}</h2>
          <ul className="small" style={{ margin: 0, paddingLeft: 18, maxHeight: 220, overflow: "auto" }}>
            {data.lineage.slice(-40).reverse().map((e, i) => (
              <li key={i}><span className="muted mono">{e.at.slice(11, 19)}</span> <strong>{e.stage}</strong> {e.event}
                {e.key ? ` · ${String(e.key)}` : ""}{e.reason ? ` · ${String(e.reason)}` : ""}</li>
            ))}
          </ul>
        </div>
      )}
      {recast && <RecastModal fromProduction={view} defaultTitle={legacy ? undefined : data.spec.title} onClose={() => setRecast(false)} onStarted={(s) => { setRecast(false); onStarted(s); }} />}
    </div>
  );
}

function AnimaticCard({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [editing, setEditing] = useState(false);
  const legacy = Boolean(state.view.legacy);
  const entry = (legacy ? (state as unknown as { animatic?: Record<string, any> }).animatic : state.done?.animatic) as
    { renders?: Record<string, string>; plan?: { cuts_total: number; clips_planned: number; gpu_minutes: number; unused_shots?: string[] } } | undefined;
  const framesReady = legacy || state.view.stages?.frames === "done";
  if (!entry?.renders && !framesReady) return null;
  const make = async () => {
    try { await api.makeAnimatic(state.slug); app.toast(t("animaticQueued"), "info"); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const plan = entry?.plan;
  return (
    <div className="card">
      <h2>
        <Film size={16} /> {t("animaticTitle")}
        <div className="card-actions">
          {state.status === "awaiting_review" && (
            <button className="btn sm primary" onClick={async () => {
              try { await api.continueProduction(state.slug); onChanged(); } catch (e) { app.toast((e as Error).message, "bad"); }
            }}><Play size={13} /> {t("continueProduction")}</button>
          )}
          {!legacy && state.spec.shots?.length ? (
            <button className="btn sm" onClick={() => setEditing(true)}><Shuffle size={13} /> {t("changeShots")}</button>
          ) : null}
          {state.status !== "running" && <button className="btn sm ghost" onClick={make}>{t("makeAnimatic")}</button>}
        </div>
      </h2>
      <p className="small muted" style={{ marginTop: -6 }}>{t("animaticLead")}</p>
      {plan && (
        <p className="small">
          {t("animaticPlan", { cuts: plan.cuts_total, clips: plan.clips_planned, gpu: plan.gpu_minutes })}
          {plan.unused_shots && plan.unused_shots.length > 0 && <span className="muted"> · {t("unusedShots", { list: plan.unused_shots.join(", ") })}</span>}
        </p>
      )}
      {entry?.renders && (
        <div className="row wrap" style={{ alignItems: "flex-start" }}>
          {Object.entries(entry.renders).map(([aspect, id]) => (
            <div key={aspect} className="stack" style={{ gap: 4 }}>
              <span className="small muted">{aspect}</span>
              <div className="video-frame" style={{ width: aspect === "16:9" ? 380 : 220 }}>
                <video src={fileUrl(id)} controls preload="metadata" poster={`/api/assets/${id}/thumb`} />
              </div>
            </div>
          ))}
        </div>
      )}
      {editing && <ShotsModal state={state} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); onChanged(); }} />}
    </div>
  );
}

type ShotEdit = { best?: number; clip?: boolean; regenerate?: boolean; prompt?: string };

function ShotsModal({ state, onClose, onSaved }: { state: ProductionState; onClose: () => void; onSaved: () => void }) {
  const { t } = useT();
  const app = useApp();
  const frames = (state.done?.frames?.items || {}) as Record<string, { best?: string; variants?: string[] }>;
  const [edits, setEdits] = useState<Record<string, ShotEdit>>({});
  const set = (key: string, patch: ShotEdit) => setEdits({ ...edits, [key]: { ...edits[key], ...patch } });
  const save = async () => {
    const changes = Object.entries(edits).map(([key, e]) => {
      const shot = (state.spec.shots || []).find((s) => s.key === key)!;
      const change: Record<string, unknown> = { key };
      if (e.best !== undefined) change.best = e.best;
      if (e.clip !== undefined && e.clip !== shot.clips.length > 0) change.clip = e.clip;
      if (e.regenerate) change.regenerate = true;
      if (e.prompt !== undefined && e.prompt.trim() && e.prompt !== shot.prompt) change.prompt = e.prompt.trim();
      return change;
    }).filter((c) => Object.keys(c).length > 1);
    if (!changes.length) { onClose(); return; }
    try {
      const out = await api.changeShots(state.slug, changes);
      app.toast(t("shotsChanged", { n: out.changed.length }), "ok");
      onSaved();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };
  return (
    <Modal title={t("changeShots")} onClose={onClose} wide footer={<button className="btn primary" onClick={save}>{t("applyChanges")}</button>}>
      <div className="stack">
        {(state.spec.shots || []).map((shot) => {
          const entry = frames[shot.key] || {};
          const variants = entry.variants || [];
          const e = edits[shot.key] || {};
          const bestIndex = e.best ?? Math.max(0, variants.indexOf(entry.best || ""));
          return (
            <div key={shot.key} className="row" style={{ alignItems: "flex-start", gap: 12 }}>
              <strong style={{ width: 28 }}>{shot.key}</strong>
              <div className="row wrap" style={{ gap: 6, flex: "none" }}>
                {variants.map((id, i) => (
                  <button key={id} className={`tile${i === bestIndex ? " selected" : ""}`} style={{ width: 84 }}
                    onClick={() => set(shot.key, { best: i })} title={id}>
                    <img src={`/api/assets/${id}/thumb`} alt="" loading="lazy" />
                  </button>
                ))}
              </div>
              <div className="stack grow" style={{ gap: 6 }}>
                <textarea rows={2} defaultValue={shot.prompt} onChange={(ev) => set(shot.key, { prompt: ev.target.value })} />
                <div className="row small" style={{ gap: 14 }}>
                  {shot.lead && <span className="pill">{t("leadBadge")}</span>}
                  <label className="row" style={{ gap: 5 }}>
                    <input type="checkbox" checked={e.clip ?? shot.clips.length > 0} onChange={(ev) => set(shot.key, { clip: ev.target.checked })} /> {t("makeClip")}
                  </label>
                  <label className="row" style={{ gap: 5 }}>
                    <input type="checkbox" checked={Boolean(e.regenerate)} onChange={(ev) => set(shot.key, { regenerate: ev.target.checked })} /> {t("regenerateShot")}
                  </label>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </Modal>
  );
}

function QaCard({ state, onRan }: { state: ProductionState; onRan: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [busy, setBusy] = useState(false);
  const card = state.qa?.last;
  const legacy = Boolean(state.view.legacy);
  const retries = (state.lineage || []).filter((e) => e.event === "qa_retry").length;
  const run = async (dryRun: boolean) => {
    setBusy(true);
    try {
      const out = await api.runQa(state.slug, { dry_run: dryRun });
      if (out.job.state !== "done") app.toast(t("qaQueued"), "info");
      app.refreshJobs();
      setTimeout(onRan, 1500);
      onRan();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };
  const failing = (card?.items || []).filter((i) => i.verdict === "fail");
  return (
    <div className="card">
      <h2>
        <ShieldCheck size={16} /> {t("qaTitle")}
        <div className="card-actions">
          <button className="btn sm" disabled={busy} onClick={() => run(true)}>{t("qaCheck")}</button>
          {!legacy && <button className="btn sm" disabled={busy} onClick={() => run(false)}>{t("qaFix")}</button>}
        </div>
      </h2>
      {!card ? <p className="small muted">{t("qaNone")}</p> : (
        <>
          <p className="small">
            <span className={`pill ${card.failed ? "bad" : "ok"}`}>{t("qaSummary", { passed: card.passed, failed: card.failed, skipped: card.skipped })}</span>
            {" "}<span className="muted">{card.stage} · {t("qaVision", { name: card.vision })}{retries > 0 ? ` · ${t("qaRetries", { n: retries })}` : ""}</span>
          </p>
          {failing.length > 0 && (
            <div className="stack" style={{ gap: 8 }}>
              {failing.slice(0, 24).map((i) => (
                <div key={`${i.stage}-${i.key}`} className="row" style={{ alignItems: "flex-start" }}>
                  {i.asset_id && (
                    <button className="tile" style={{ width: 64, flex: "none" }} onClick={() => app.openAsset(i.asset_id!)}>
                      <img src={`/api/assets/${i.asset_id}/thumb`} alt="" loading="lazy" onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />
                    </button>
                  )}
                  <div className="small">
                    <strong>{i.stage} {i.key}</strong>{i.score != null && <span className="muted"> · {i.score}/10</span>}
                    <div className="err-text">{i.reasons.join("; ")}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function RecipesCard({ onStarted }: { onStarted: (slug: string) => void }) {
  const { t } = useT();
  const app = useApp();
  const { data } = useAsync(() => api.recipes(), [app.dataVersion]);
  const [running, setRunning] = useState<RecipeSummary | null>(null);
  const items = data?.items || [];
  return (
    <div className="card">
      <h2><BookCopy size={16} /> {t("recipesTitle")}</h2>
      <p className="small muted" style={{ marginTop: -6 }}>{t("recipesLead")}</p>
      {items.length === 0 ? <p className="small muted">{t("noRecipes")}</p> : (
        <div className="stack" style={{ gap: 8 }}>
          {items.map((r) => (
            <div key={r.name} className="row" style={{ justifyContent: "space-between" }}>
              <div className="small">
                <strong>{r.title || r.name}</strong> <span className="mono muted">{r.name}</span>
                <div className="muted">{t("shotsCount", { n: r.shots, lead: r.lead_shots })} · {r.original_lead}
                  {r.warnings > 0 && <> · {t("recipeWarnings", { n: r.warnings })}</>}</div>
              </div>
              <button className="btn sm" onClick={() => setRunning(r)}><Users size={13} /> {t("runWith")}</button>
            </div>
          ))}
        </div>
      )}
      {running && <RecastModal recipe={running} onClose={() => setRunning(null)} onStarted={(s) => { setRunning(null); onStarted(s); }} />}
    </div>
  );
}

export function ProductionsView() {
  const { t, lang } = useT();
  const app = useApp();
  const { data, reload } = useAsync(() => api.productions(), [app.dataVersion]);
  const items = useMemo(() => data?.items || [], [data]);
  const selected = app.route.arg || items[0]?.slug;
  const open = (slug: string) => { app.go("productions", slug); reload(); };
  const [newShort, setNewShort] = useState(false);

  return (
    <>
      <div className="page-head">
        <div><h1>{t("productionsTitle")}</h1><p>{t("productionsLead")}</p></div>
        <button className="btn primary" onClick={() => setNewShort(true)}><Megaphone size={15} /> {t("newShort")}</button>
      </div>
      {newShort && <ShortModal onClose={() => setNewShort(false)} onStarted={(s) => { setNewShort(false); open(s); }} />}
      <div className="productions-grid">
        <div className="stack">
          <div className="card" style={{ padding: 6 }}>
            {items.length === 0 ? <Empty icon={<Clapperboard size={30} />} text={t("noProductions")} /> : (
              <table className="list">
                <tbody>
                  {items.map((p) => (
                    <tr key={p.slug} onClick={() => open(p.slug)} style={{ cursor: "pointer" }} className={p.slug === selected ? "selected" : ""}>
                      <td>
                        <strong>{p.name}</strong>{p.kind === "short" && <span className="pill info" style={{ marginLeft: 6 }}>{t("shortBadge")}</span>}
                        <div className="mono muted small">{p.slug}</div>
                      </td>
                      <td><StatusPill status={p.status} /></td>
                      <td className="small muted nowrap">{timeAgo(p.updated_at, lang)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <RecipesCard onStarted={open} />
        </div>
        <div>
          {selected ? <ProductionDetail key={selected} slug={selected} reloadList={reload} onStarted={open} />
            : <p className="muted">{t("pickProduction")}</p>}
        </div>
      </div>
    </>
  );
}
