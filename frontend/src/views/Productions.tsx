import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, ArrowLeft, BookCopy, Check, Circle, CircleDot, Clapperboard, Download, Film, Loader2, Megaphone, Music,
  Pause as PauseIcon, Play, RotateCcw, Info, FileDown, Save, ShieldCheck, Repeat, Shuffle, Sparkles, Users, Ratio, Plus,
} from "lucide-react";
import {
  api, fileUrl, type Character, type Job, type Project, type ProductionState, type ProductionSummary, type RecipeSummary,
} from "../api";
import { useT, type Lang, type MessageKey } from "../i18n";
import { errorText, jobMessage, lineageEvent, stageName, translateText } from "../messages";
import { LookPanel } from "../components/LookPanel";
import { Modal, timeAgo, useApp, useAsync } from "../components/ui";
import { ShortDetail, ShortModal } from "./Shorts";
import { VideoModal } from "./VideoModal";
import { StoryboardCard, liveRun } from "./Storyboard";
import { SongTrackCard } from "./SongTrack";

const STATUS_TONE: Record<string, string> = {
  queued: "info", running: "accent", awaiting_review: "gold", done: "ok", failed: "bad", cancelled: "", partial: "warn",
};
const STATUS_KEY: Record<string, MessageKey> = {
  queued: "stateQueued", running: "stateRunning", awaiting_review: "statusAwaiting", done: "stateDone",
  failed: "stateFailed", cancelled: "stateCancelled", partial: "statusPartial",
};

const LIVE_JOB = ["queued", "waiting_gpu", "running"];

/** "queued" with no live job is not queued: an edit left it waiting for the person to press Continue. */
export function awaitsContinue(status: string, slug: string, jobId: string | null | undefined, jobs: Job[]): boolean {
  if (status !== "queued") return false;
  return !jobs.some((j) => LIVE_JOB.includes(j.state)
    && (j.id === jobId || (j.type === "production" && (j.params as { slug?: string }).slug === slug)));
}

export function StatusPill({ status, pending }: { status: string; pending?: boolean }) {
  const { t } = useT();
  if (status === "queued" && pending) return <span className="pill gold">{t("statusChangesPending")}</span>;
  return <span className={`pill ${STATUS_TONE[status] || ""}`}>{t(STATUS_KEY[status] || "stateQueued")}</span>;
}

/** "Recreate with…": pick a studio character (any project) or describe a new lead, then run the recipe. */
export function RecastModal({ recipe, fromProduction, defaultTitle, onClose, onStarted }: {
  recipe?: RecipeSummary; fromProduction?: ProductionSummary; defaultTitle?: string; onClose: () => void; onStarted: (slug: string) => void;
}) {
  const { t, lang } = useT();
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
      out.notes.forEach((n) => app.toast(translateText(n, lang), "info"));
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

type Tab = "storyboard" | "preview" | "qa" | "history";
const STAGE_TAB: Record<string, Tab> = {
  character: "storyboard", song: "storyboard", frames: "storyboard", lyrics: "storyboard", animatic: "preview", clips: "preview",
  photocards: "preview", album: "preview", timeline: "preview", report: "history",
};

/** A failure message in words the person can act on; the raw text stays one click away. */
export function humanError(message: string | null | undefined, t: ReturnType<typeof useT>["t"], lang: Lang = "en"): string {
  const m = message || "";
  if (/WinError (5|32)|Access is denied|PermissionError|being used by another process/i.test(m)) return t("errFileLocked");
  if (/ComfyUI is not answering|comfy\w* (is )?(down|unreachable|not reachable)|Connection refused/i.test(m)) return t("errComfyDown");
  if (/out of memory|CUDA error|OOM|not enough VRAM/i.test(m)) return t("errVram");
  if (/language model|llm unavailable|no llama\.cpp/i.test(m)) return t("errLlm");
  if (/the run stopped without finishing/i.test(m)) return t("errStopped");
  const first = errorText(undefined, m.replace(/^[a-z_]+:\s*/i, "").split(/(?<=[.;])\s/)[0], lang);
  return first.length > 220 ? `${first.slice(0, 219)}…` : first;
}

/** The stage a running production is really on: its job says so before the state file does. */
function liveStage(state: ProductionState, jobs: Job[]): string | null {
  const job = jobs.find((j) => j.id === state.job_id);
  const word = job?.message?.split(/[\s:·]/)[0] || "";
  return Object.keys(state.view.stages || {}).includes(word) ? word : null;
}

function PipelineStepper({ state, active, onPick }: { state: ProductionState; active: boolean; onPick: (stage: string) => void }) {
  const { t } = useT();
  const app = useApp();
  const stages = Object.entries(state.view.stages || {});
  const current = (active && liveStage(state, app.jobs)) || state.stage;
  return (
    <ol className="pipe">
      {stages.map(([stage, st]) => {
        const here = current === stage;
        let cls: string = st;
        if (here && active) cls = "running";
        else if (here && state.status === "awaiting_review") cls = "paused";
        else if (here && state.status === "failed") cls = "failed";
        const Icon = cls === "done" ? Check : cls === "running" ? Loader2 : cls === "paused" ? PauseIcon
          : cls === "failed" ? AlertTriangle : cls === "partial" ? CircleDot : Circle;
        return (
          <li key={stage} className={`pipe-step ${cls}`}>
            <button onClick={() => onPick(stage)} title={t(`stage_${stage}` as MessageKey)}>
              <Icon size={14} className={cls === "running" ? "spin" : ""} /> <span>{t(`stage_${stage}` as MessageKey)}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/** What would stop the next run, checked before it starts (ComfyUI, music model, ffmpeg, GPU memory). */
function Preflight({ slug }: { slug: string }) {
  const { t, lang } = useT();
  const app = useApp();
  const [items, setItems] = useState<Awaited<ReturnType<typeof api.productionPreflight>>["items"] | null>(null);
  const [starting, setStarting] = useState(false);
  const check = () => api.productionPreflight(slug).then((r) => setItems(r.items)).catch(() => setItems([]));
  useEffect(() => { check(); }, [slug]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!items || items.length === 0) return null;
  const start = async () => {
    setStarting(true);
    try { await api.startService("comfyui"); app.toast(t("pfComfyStarting"), "info"); setTimeout(check, 4000); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setStarting(false); }
  };
  return (
    <div className="preflight">
      {items.map((i) => (
        <div key={i.code} className={`pf-item ${i.level}`}>
          {i.level === "error" ? <AlertTriangle size={14} /> : i.level === "warn" ? <AlertTriangle size={14} /> : <Info size={14} />}
          <span className="grow">{(() => { const k = `pf_${i.code}` as MessageKey; const txt = t(k); return txt === k ? errorText(undefined, i.message, lang) : txt; })()}</span>
          {i.code === "comfy_down" && i.startable && <button className="btn sm" disabled={starting} onClick={start}>{starting ? <Loader2 size={12} className="spin" /> : <Play size={12} />} {t("pfStartComfy")}</button>}
        </div>
      ))}
    </div>
  );
}

/** One banner that says where the production is and the one thing to do now. */
function NextStep({ state, active, onChanged, setTab }: {
  state: ProductionState; active: boolean; onChanged: () => void; setTab: (tab: Tab) => void;
}) {
  const { t, lang } = useT();
  const app = useApp();
  const [busy, setBusy] = useState(false);
  const [details, setDetails] = useState(false);
  const job = app.jobs.find((j) => j.id === state.job_id);
  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    try { await fn(); if (ok) app.toast(ok, "ok"); onChanged(); } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const go = () => act(() => api.continueProduction(state.slug), t("productionQueued"));
  const jobStage = liveStage(state, app.jobs);
  const stage = (active && jobStage) || state.stage;
  const stageName = stage ? t(`stage_${stage}` as MessageKey) : "";
  const isShort = state.kind === "short";
  const legacy = Boolean(state.view.legacy);
  if (legacy) return null;

  if (active) {
    const waiting = job?.state === "queued" || job?.state === "waiting_gpu";
    const pct = Math.round((job?.progress || 0) * 100);
    return (
      <div className="next-step accent">
        <Loader2 size={18} className="spin" />
        <div className="grow">
          <strong>{waiting ? (job?.state === "waiting_gpu" ? t("nsWaitingGpu") : t("nsQueued")) : t("nsRunning", { stage: stageName || "…" })}</strong>
          {job?.message && job.message !== jobStage && <div className="small muted ellipsis">{jobMessage(job.message, lang)}</div>}
          {!waiting && <div className="ns-bar"><i style={{ width: `${pct}%` }} /></div>}
        </div>
        {state.job_id && <button className="btn sm" disabled={busy} onClick={() => act(() => api.cancelJob(state.job_id!), t("sbPaused"))}><PauseIcon size={13} /> {t("sbPause")}</button>}
      </div>
    );
  }
  if (state.status === "awaiting_review" && state.stage === "song" && !isShort) {
    const partial = (state.partial?.song || {}) as { song_asset_ids?: string[] };
    const takes = (state.done?.song?.song_asset_ids as string[] | undefined) || partial.song_asset_ids || [];
    return (
      <div className="next-step gold">
        <Music size={18} />
        <div className="grow stack" style={{ gap: 8 }}>
          <div><strong>{t("nsPickTake")}</strong><div className="small muted">{t("nsPickTakeLead")}</div></div>
          <Preflight slug={state.slug} />
          {takes.map((id, i) => (
            <div key={id} className="row" style={{ gap: 10 }}>
              <strong className="mono small">{t("takeN", { n: i + 1 })}</strong>
              <audio src={fileUrl(id)} controls preload="none" style={{ flex: 1, height: 32 }} />
              <button className="btn sm primary" disabled={busy} onClick={() => act(() => api.continueProduction(state.slug, i + 1), t("takeChosen", { n: i + 1 }))}>{t("useThisTake")}</button>
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (state.status === "awaiting_review") {
    const plan = (state.done?.animatic as { plan?: { clips_planned: number; gpu_minutes: number } } | undefined)?.plan;
    const animatic = state.stage === "animatic" && !isShort;
    // a video without clips goes straight from the animatic to the cut
    const noClips = animatic && !!plan && plan.clips_planned === 0;
    return (
      <div className="next-step gold">
        <PauseIcon size={18} />
        <div className="grow">
          <strong>{animatic ? (noClips ? t("nsAnimaticNoClips") : t("nsAnimatic")) : t("nsReview", { stage: stageName })}</strong>
          <div className="small muted">{animatic && plan ? (noClips ? t("nsAnimaticNoClipsLead")
            : t("nsAnimaticLead", { clips: plan.clips_planned, gpu: plan.gpu_minutes })) : jobMessage(state.message, lang)}</div>
          <Preflight slug={state.slug} />
        </div>
        {animatic && <button className="btn sm" onClick={() => setTab("preview")}><Film size={13} /> {t("nsWatch")}</button>}
        <button className="btn sm primary" disabled={busy} onClick={go}><Play size={13} /> {animatic ? (noClips ? t("nsAnimaticFinish") : t("nsAnimaticGo")) : t("continueProduction")}</button>
      </div>
    );
  }
  if (state.status === "failed" || state.status === "cancelled") {
    const failed = state.status === "failed";
    return (
      <div className={`next-step ${failed ? "bad" : ""}`}>
        {failed ? <AlertTriangle size={18} /> : <PauseIcon size={18} />}
        <div className="grow">
          <strong>{failed ? t("nsFailed", { stage: stageName || "—" }) : t("nsCancelled")}</strong>
          {failed && <div className="small">{humanError(state.message, t, lang)}</div>}
          {failed && state.message && (
            <button className="link-btn small" onClick={() => setDetails(!details)}>{details ? t("errHide") : t("errDetails")}</button>
          )}
          {details && <pre className="ns-details">{jobMessage(state.message, lang)}{lang === "es" && state.message && jobMessage(state.message, lang) !== state.message ? `\n\n${state.message}` : ""}</pre>}
          <Preflight slug={state.slug} />
        </div>
        <button className="btn sm primary" disabled={busy} onClick={go}><RotateCcw size={13} /> {t("resumeProduction")}</button>
      </div>
    );
  }
  if (state.status === "queued") {
    return (
      <div className="next-step info">
        <Shuffle size={18} />
        <div className="grow"><strong>{t("nsEdited")}</strong><div className="small muted">{jobMessage(state.message, lang) || t("nsEditedLead")}</div>
          <Preflight slug={state.slug} /></div>
        <button className="btn sm primary" disabled={busy} onClick={go}><Play size={13} /> {t("continueProduction")}</button>
      </div>
    );
  }
  if (state.status === "done") {
    return (
      <div className="next-step ok">
        <Check size={18} />
        <div className="grow"><strong>{t("nsDone")}</strong><div className="small muted">{isShort ? t("nsDoneLeadShort") : t("nsDoneLead")}</div></div>
        {!isShort && <button className="btn sm primary" onClick={() => setTab("preview")}><Film size={13} /> {t("nsDoneWatch")}</button>}
      </div>
    );
  }
  return null;
}

function ProductionDetail({ slug, reloadList, onStarted, onBack }: {
  slug: string; reloadList: () => void; onStarted: (slug: string) => void; onBack: () => void;
}) {
  const { t, lang } = useT();
  const app = useApp();
  const { data, reload } = useAsync(() => api.production(slug), [slug, app.dataVersion]);
  const [recast, setRecast] = useState(false);
  const [tab, setTab] = useState<Tab | null>(null);
  const [canvasBusy, setCanvasBusy] = useState(false);
  const [lumiereBusy, setLumiereBusy] = useState(false);
  const active = data ? liveRun(data, app.jobs) : false;
  useEffect(() => {
    if (!active) return;
    const id = setInterval(reload, 2500);
    return () => clearInterval(id);
  }, [active, reload]);
  const status = data?.status;
  useEffect(() => { if (status) reloadList(); }, [status]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!data) return <p className="muted">{t("loading")}</p>;
  const view = data.view;
  const legacy = Boolean(view.legacy);
  const isShort = data.kind === "short";
  const changed = () => { reload(); reloadList(); app.refreshJobs(); };
  const exportLumiere = async (timelineId: string, aspect: string) => {
    setLumiereBusy(true);
    try {
      const r = await api.exportToLumiere(slug, timelineId, aspect);
      if (r.ok) {
        app.toast(t("exportLumiereDone", { clips: String(r.imported_clips ?? 0) }), "ok");
        if (r.url) window.open(r.url, "_blank", "noopener");
      } else app.toast(t("exportLumiereFail", { error: errorText(undefined, r.error || "", lang) }), "bad");
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setLumiereBusy(false); }
  };
  const saveRecipe = async () => {
    try { const r = await api.exportRecipe(slug); app.toast(t("recipeSaved", { name: r.name }), "ok"); changed(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  // while a new look re-renders the cut, the previous cut stays on screen
  const stale = (view as { previous_renders?: Record<string, Record<string, string>> }).previous_renders || {};
  const renders: Record<string, Record<string, string>> = { ...Object.fromEntries(
    Object.entries(view.renders || {}).filter(([, q]) => q && Object.keys(q).length)), ...Object.fromEntries(
    Object.entries(stale).filter(([a]) => !(view.renders || {})[a] || !Object.keys((view.renders || {})[a] || {}).length)) };
  const settings = (data.settings || {}) as Record<string, unknown>;
  const autopilot = Boolean(settings.animatic_autocontinue) && !settings.song_review;
  const canvasId = ((data.done?.extras as { canvas?: string } | undefined) || {}).canvas;
  const makeCanvas = async () => {
    setCanvasBusy(true);
    try {
      const r = await api.makeCanvas(slug);
      app.toast(t("canvasMade", { start: r.start_s.toFixed(1), from: r.start_from }), "ok");
      changed();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setCanvasBusy(false); }
  };
  const timelineIds = (((data.done?.timeline as { timelines?: Record<string, { timeline_id?: string }> } | undefined)?.timelines) || {});
  const animatic = (data.done?.animatic as { renders?: Record<string, string> } | undefined)?.renders;
  const outputs = Object.keys(renders).length + (animatic ? Object.keys(animatic).length : 0);
  // the tab follows the production until the person picks one
  const autoTab: Tab = data.status === "awaiting_review" && data.stage === "animatic" ? "preview"
    : data.status === "done" && outputs ? "preview" : "storyboard";
  const current = tab || autoTab;
  const tabs: [Tab, string][] = [
    ["storyboard", t("tabStoryboard")], ["preview", `${t("tabPreview")}${outputs ? ` · ${outputs}` : ""}`],
    ["qa", t("tabQa")], ["history", t("tabHistory")],
  ];

  return (
    <div className="stack">
      <div className="card prod-head">
        <div className="row wrap" style={{ gap: 8 }}>
          <button className="btn sm ghost" onClick={onBack}><ArrowLeft size={14} /> {t("prodBack")}</button>
          <h2 className="grow" style={{ margin: 0 }}><Clapperboard size={17} /> {data.name || slug} <StatusPill status={view.status} pending={awaitsContinue(view.status, slug, data.job_id, app.jobs)} /></h2>
          {!isShort && !legacy && (
            <label className="switch" title={t("autopilotHint")}>
              <input type="checkbox" checked={autopilot} onChange={async (e) => {
                try { await api.setProductionSettings(slug, { autopilot: e.target.checked }); app.toast(e.target.checked ? t("autopilotOn") : t("autopilotOff"), "ok"); reload(); }
                catch (err) { app.toast((err as Error).message, "bad"); }
              }} /> <span>{t("autopilot")}</span>
            </label>
          )}
          {!isShort && !legacy && <button className="btn sm" onClick={saveRecipe}><Save size={13} /> {t("saveAsRecipe")}</button>}
          {!isShort && <button className="btn sm" onClick={() => setRecast(true)}><Users size={13} /> {t("recreateWith")}</button>}
        </div>
        {legacy && <p className="small muted">{t("legacyNote")}</p>}
        {data.recipe && <p className="small muted" style={{ margin: "6px 0 0" }}>{t("fromRecipe", { name: data.recipe.name })} · {data.recipe.cast.lead}</p>}
        {view.stages && <PipelineStepper state={data} active={active} onPick={(s) => setTab(STAGE_TAB[s] || "storyboard")} />}
        <NextStep state={data} active={active} onChanged={changed} setTab={setTab} />
      </div>
      {isShort ? <ShortDetail state={data} onChanged={changed} /> : (
        <>
          <div className="tabs-bar">
            {tabs.map(([k, label]) => (
              <button key={k} className={current === k ? "on" : ""} onClick={() => setTab(k)}>{label}</button>
            ))}
          </div>
          {current === "storyboard" && (
            <>
              {!legacy && (data.spec.shots || []).length > 0 && <StoryboardCard state={data} onChanged={changed} />}
              {!legacy && <SongTrackCard state={data} onChanged={changed} />}
            </>
          )}
          {current === "preview" && (
            <>
              {Object.keys(renders).length > 0 && (
                <div className="card">
                  <h2>{t("finalCut")}</h2>
                  <div className="row wrap" style={{ alignItems: "flex-start" }}>
                    {Object.entries(renders).map(([aspect, byQuality]) => {
                      const id = byQuality.final || byQuality.preview;
                      return id ? (
                        <div key={aspect} className="stack" style={{ gap: 4 }}>
                          <span className="small muted">{aspect}{byQuality.final ? "" : ` · ${t("previewQuality")}`}{stale[aspect] ? ` · ${t("lookPrevious")}` : ""}</span>
                          <div className="video-frame" style={{ width: aspect === "16:9" ? 480 : 260 }}>
                            <video src={fileUrl(id)} controls preload="metadata" poster={`/api/assets/${id}/thumb`} />
                          </div>
                          <div className="row" style={{ gap: 6 }}>
                            <a className="btn sm ghost" href={fileUrl(id)} download><Download size={13} /> {t("download")}</a>
                            {timelineIds[aspect]?.timeline_id && (
                              <a className="btn sm ghost" href={api.timelineExportUrl(timelineIds[aspect].timeline_id!, "zip", `${data.name || slug} ${aspect.replace(":", "x")}`)} download title={t("exportEditorsHint")}>
                                <FileDown size={13} /> {t("exportEditors")}</a>
                            )}
                            {timelineIds[aspect]?.timeline_id && (
                              <button className="btn sm ghost" disabled={lumiereBusy} title={t("exportLumiereHint")} data-testid="export-lumiere"
                                onClick={() => exportLumiere(timelineIds[aspect].timeline_id!, aspect)}>
                                <FileDown size={13} /> {t("exportLumiere")}</button>
                            )}
                          </div>
                        </div>
                      ) : null;
                    })}
                    {canvasId && (
                      <div className="stack" style={{ gap: 4 }}>
                        <span className="small muted">{t("canvasTitle")}</span>
                        <div className="video-frame" style={{ width: 150, aspectRatio: "9 / 16" }}>
                          <video src={fileUrl(canvasId)} poster={`/api/assets/${canvasId}/thumb`} autoPlay loop muted playsInline preload="metadata"
                            style={{ width: "100%", height: "100%", objectFit: "cover" }} />
                        </div>
                        <a className="btn sm ghost" href={`${fileUrl(canvasId)}?download=true`} download><Download size={13} /> {t("download")}</a>
                      </div>
                    )}
                  </div>
                  <div className="row" style={{ gap: 8, marginTop: 10 }}>
                    <button className="btn sm" disabled={canvasBusy} onClick={makeCanvas} title={t("canvasHint")}>
                      {canvasBusy ? <Loader2 size={13} className="spin" /> : <Repeat size={13} />} {canvasId ? t("canvasRemake") : t("canvasMake")}
                    </button>
                    <span className="hint">{t("canvasHint")}</span>
                  </div>
                </div>
              )}
              {!legacy && (
                <div className="card">
                  <h2><Ratio size={16} /> {t("reframeTitle")}</h2>
                  <p className="small muted">{t("reframeHint")}</p>
                  <div className="row wrap" style={{ gap: 8 }}>
                    <span className="small">{t("reframeHave", { aspects: ((data.spec.timeline || {}).aspects || ["9:16"]).join(" · ") })}</span>
                    <span className="grow" />
                    {["9:16", "16:9", "1:1"].filter((a) => !((data.spec.timeline || {}).aspects || ["9:16"]).includes(a)).map((a) => (
                      <button key={a} className="btn sm" disabled={active} onClick={async () => {
                        try {
                          const r = await api.reframeProduction(slug, [a], (data.spec.timeline?.finishing as { framing?: string } | undefined)?.framing || "blur");
                          app.toast(t("reframeQueued", { aspects: r.new.join(", ") }), "ok");
                          app.refreshJobs();
                          changed();
                        } catch (e) { app.toast((e as Error).message, "bad"); }
                      }}><Plus size={13} /> {t("reframeAdd", { aspect: a })}</button>
                    ))}
                  </div>
                </div>
              )}
              {!legacy && (
                <div className="card">
                  <h2><Sparkles size={16} /> {t("lookTitle")}</h2>
                  <LookPanel value={(data.spec.timeline || {}).finishing} note={t("lookNoteProduction")}
                    onApply={async (f) => {
                      try {
                        const r = await api.setProductionFinishing(slug, f, true);
                        app.toast(r.rerender.length ? t("lookRerendering", { aspects: r.rerender.join(", ") }) : t("lookApplied"), "ok");
                        app.refreshJobs();
                        changed();
                      } catch (e) { app.toast((e as Error).message, "bad"); }
                    }} />
                </div>
              )}
              <AnimaticCard state={data} onChanged={changed} />
              {outputs === 0 && !(view.stages?.frames === "done") && <p className="muted small">{t("previewEmpty")}</p>}
            </>
          )}
          {current === "qa" && <QaCard state={data} onRan={reload} />}
          {current === "history" && (
            <div className="card">
              <h2>{t("lineageTitle")}</h2>
              {data.message && <p className="small muted">{jobMessage(data.message, lang)}</p>}
              {legacy || !data.lineage?.length ? <p className="small muted">—</p> : (
                <ul className="small" style={{ margin: 0, paddingLeft: 18, maxHeight: 480, overflow: "auto" }}>
                  {data.lineage.slice(-120).reverse().map((e, i) => (
                    <li key={i}><span className="muted mono">{e.at.slice(0, 10)} {e.at.slice(11, 19)}</span> <strong>{stageName(e.stage, lang)}</strong> {lineageEvent(e.event, lang)}
                      {e.key ? ` · ${String(e.key)}` : ""}{e.reason ? ` · ${translateText(String(e.reason), lang)}` : ""}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
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
              <div className="video-frame" style={{ width: aspect === "16:9" ? 560 : 300 }}>
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
  const { t, lang } = useT();
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
            {" "}<span className="muted">{card.stage === "all" ? t("all") : stageName(card.stage, lang)} · {t("qaVision", { name: translateText(card.vision, lang) })}{retries > 0 ? ` · ${t("qaRetries", { n: retries })}` : ""}</span>
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
                    <strong>{stageName(i.stage, lang)} {i.key}</strong>{i.score != null && <span className="muted"> · {i.score}/10</span>}
                    <div className="err-text">{i.reasons.map((r) => translateText(r, lang)).join("; ")}</div>
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

export function ProductionCard({ p, projectName, onOpen, compact }: {
  p: ProductionSummary; projectName?: string; onOpen: () => void; compact?: boolean;
}) {
  const { t, lang } = useT();
  const app = useApp();
  const pct = p.progress && p.progress.total ? Math.round((p.progress.done / p.progress.total) * 100) : 0;
  const where = p.stage ? t(`stage_${p.stage}` as MessageKey) : "";
  const cardHint = () => {
    if (p.status === "done") return t("nsDone");
    if (p.status === "awaiting_review" && p.stage === "song" && p.kind !== "short") return t("nsPickTake");
    if (p.status === "awaiting_review" && p.stage === "animatic") return t("nsAnimatic");
    if (p.status === "failed") return `${t("nsFailed", { stage: where || "—" })} · ${humanError(p.message, t, lang)}`;
    return where ? `${where}${p.progress ? ` · ${p.progress.done}/${p.progress.total}` : ""}` : "";
  };
  return (
    <button className={`prod-card${compact ? " compact" : ""}`} onClick={onOpen}>
      <div className="prod-cover">
        {p.cover ? <img src={`/api/assets/${p.cover.asset_id}/thumb`} alt="" loading="lazy" /> : <Clapperboard size={compact ? 18 : 28} />}
        {p.cover?.kind === "video" && <span className="prod-play"><Play size={compact ? 10 : 14} /></span>}
      </div>
      <div className="prod-info">
        <div className="row" style={{ gap: 6 }}>
          <strong className="ellipsis grow">{p.name}</strong>
          {!compact && <StatusPill status={p.status} pending={awaitsContinue(p.status, p.slug, null, app.jobs)} />}
        </div>
        <span className="small muted ellipsis">
          {[p.kind === "short" ? t("shortBadge") : p.shot_count ? t("prodShotsN", { n: p.shot_count }) : "",
            projectName || "", timeAgo(p.updated_at, lang)].filter(Boolean).join(" · ")}
        </span>
        {!p.legacy && (
          <>
            <div className="prod-progress"><i style={{ width: `${pct}%` }} /></div>
            <span className={`small ellipsis ${p.status === "awaiting_review" ? "gold-text" : p.status === "failed" ? "err-text" : "muted"}`}>{cardHint()}</span>
          </>
        )}
      </div>
    </button>
  );
}

/** Music videos and shorts: a gallery to pick from (a project's own, or all
 * of them), and one production at a time with its pipeline, its next step
 * and its storyboard. */
export function ProductionsView({ projectId }: { projectId?: string }) {
  const { t } = useT();
  const app = useApp();
  const { data, reload } = useAsync(() => api.productions(), [app.dataVersion]);
  const { data: projects } = useAsync(() => api.projects(), [app.dataVersion]);
  const names = useMemo(() => Object.fromEntries((projects?.items || []).map((p: Project) => [p.id, p.name])), [projects]);
  const all = useMemo(() => data?.items || [], [data]);
  const items = projectId ? all.filter((p) => p.project_id === projectId) : all;
  const selected = app.route.arg;
  const open = (p: { slug: string; project_id?: string | null }) => {
    if (projectId) app.go("video", p.slug);
    else if (p.project_id && names[p.project_id]) window.location.hash = `#/p/${p.project_id}/video/${p.slug}`;
    else app.go("productions", p.slug);
    reload();
  };
  const openSlug = (slug: string) => {
    api.production(slug).then((s) => open({ slug, project_id: s.project_id })).catch(() => app.go("productions", slug));
  };
  const back = () => (projectId ? app.go("video") : app.go("productions"));
  const [newShort, setNewShort] = useState(false);
  const [newVideo, setNewVideo] = useState(false);
  const modals = (
    <>
      {newVideo && <VideoModal projectId={projectId} onClose={() => setNewVideo(false)} onStarted={(s) => { setNewVideo(false); openSlug(s); }} />}
      {newShort && <ShortModal onClose={() => setNewShort(false)} onStarted={(s) => { setNewShort(false); openSlug(s); }} />}
    </>
  );

  if (selected) {
    return (
      <>
        <ProductionDetail key={selected} slug={selected} reloadList={reload} onStarted={openSlug} onBack={back} />
        {modals}
      </>
    );
  }
  const title = projectId ? t("videoTitle") : t("productionsTitle");
  return (
    <>
      <div className="page-head">
        <div><h1>{title}</h1><p>{projectId ? t("videoLeadText") : t("productionsLead")}</p></div>
        <div className="actions">
          <button className="btn primary" onClick={() => setNewVideo(true)}><Clapperboard size={15} /> {t("newVideo")}</button>
          <button className="btn" onClick={() => setNewShort(true)}><Megaphone size={15} /> {t("newShort")}</button>
        </div>
      </div>
      {modals}
      {items.length === 0 ? (
        <div className="prod-empty card">
          <Clapperboard size={34} />
          <h2>{projectId ? t("prodNoneHere") : t("noProductionsTitle")}</h2>
          <p className="muted">{t("prodStartHere")}</p>
          <div className="row" style={{ gap: 8, justifyContent: "center" }}>
            <button className="btn primary" onClick={() => setNewVideo(true)}><Clapperboard size={15} /> {t("newVideo")}</button>
            <button className="btn" onClick={() => setNewShort(true)}><Megaphone size={15} /> {t("newShort")}</button>
          </div>
        </div>
      ) : (
        <div className="prod-grid">
          {items.map((p) => <ProductionCard key={p.slug} p={p} projectName={projectId ? undefined : names[p.project_id || ""]} onOpen={() => open(p)} />)}
        </div>
      )}
      <div style={{ marginTop: 18, maxWidth: 720 }}><RecipesCard onStarted={openSlug} /></div>
    </>
  );
}
