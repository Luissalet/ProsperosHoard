import { useMemo, useRef, useState } from "react";
import { Box, Clapperboard, Mic, Film, Images, Link2, Loader2, MapPin, Pause, Pin, Plus, RefreshCw, Trash2, Upload, Users, X, Lock, Unlock, ArrowRightToLine, Dices, Gauge, Columns2, Check } from "lucide-react";
import { api, thumbUrl, type Asset, type CastMember, type Job, type MotionRef, type ProductionShot, type ProductionState, type ShotRef } from "../api";
import { useT } from "../i18n";
import { AssetPicker, Modal, useApp, useAsync } from "../components/ui";
import { LinkDownload } from "../components/LinkDownload";
import { useSlashMenu } from "../components/Slash";
import { Enhance } from "../components/Enhance";

// the song sections a shot can illustrate (what the planner writes)
export const SECTIONS = ["intro", "verse", "prechorus", "chorus", "bridge", "breakdown", "outro"];
const SECTION_KIND: Record<string, string> = { prechorus: "pre", breakdown: "bridge" };

const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const parseClock = (text: string) => {
  const parts = text.trim().split(":").map(Number);
  return parts.some((n) => Number.isNaN(n)) ? 0 : parts.reduce((a, n) => a * 60 + n, 0);
};
// camera moves the 14B image-to-video follows well, added to the motion text
const CAMERA = ["camOrbit", "camCrane", "camPush", "camTrack", "camLow", "camHandheld"] as const;
const CAMERA_TEXT: Record<string, string> = {
  camOrbit: "orbit shot: the camera travels in a half circle around the character, from the front to the side to behind, the background sweeping past",
  camCrane: "the shot starts as a close-up of the feet and the camera cranes up along the body to end on the head",
  camPush: "slow dolly push-in towards the character",
  camTrack: "the camera tracks sideways alongside the character",
  camLow: "low-angle camera looking up, slowly rising",
  camHandheld: "energetic handheld camera, slight shake, quick reframes",
};

const clockTenths = (s: number) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;

/** A run really owns the production: running, or queued with its job alive
 * ("queued" is also what an edit leaves behind, waiting for a continue). */
export function liveRun(state: ProductionState, jobs: Job[]): boolean {
  if (state.status === "running") return true;
  if (state.status !== "queued" || !state.job_id) return false;
  const job = jobs.find((j) => j.id === state.job_id);
  return Boolean(job && ["queued", "waiting_gpu", "running"].includes(job.state));
}

function sectionMatches(label: string, kind: string | null, section?: string) {
  if (!section) return false;
  const want = SECTION_KIND[section] || section;
  const l = label.toLowerCase().replace(/[\s\d-]+$/g, "").replace(/[\s-]/g, "");
  return (kind || "").toLowerCase() === want || l === section || l === want;
}

/** The shot list of a production as an editable storyboard: each shot with
 * its still, its place in the song and its render progress; click to edit,
 * + to add one in between. */
export function StoryboardCard({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [editing, setEditing] = useState<{ shot?: ProductionShot; after?: string | null } | null>(null);
  const [lyricsOpen, setLyricsOpen] = useState(false);
  const [castOpen, setCastOpen] = useState(false);
  const castCount = (state.spec.cast || []).length;
  const shots = state.spec.shots || [];
  const frames = (state.done?.frames?.items || {}) as Record<string, { best?: string; variants?: string[] }>;
  const clips = (state.done?.clips?.items || {}) as Record<string, string>;
  const pendingFrames = (state.partial?.frames?.pending || {}) as Record<string, string>;
  const pendingClips = (state.partial?.clips?.pending || {}) as Record<string, string>;
  const running = liveRun(state, app.jobs);
  const timing = state.timing;
  const quality = ((state.done?.clips?.quality || {}) as Record<string, string>);
  const drafts = Object.keys(clips).filter((k) => quality[k] === "draft");
  const draftMode = state.settings?.clip_quality === "draft";
  const lockedN = shots.filter((s) => s.locked).length;
  const [acting, setActing] = useState(false);
  const act = async (fn: () => Promise<unknown>, ok: string) => {
    setActing(true);
    try { await fn(); app.toast(ok, "ok"); onChanged(); app.refreshJobs(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setActing(false); }
  };
  const toggleLock = (shot: ProductionShot) => act(() => api.changeShots(state.slug, [{ key: shot.key, locked: !shot.locked }], false),
    shot.locked ? t("sbUnlocked", { n: shot.key }) : t("sbLocked", { n: shot.key }));

  // a typical render time on this machine right now, for the estimate
  const typical = useMemo(() => {
    const times = app.jobs.filter((j) => j.type === "generate_image" && j.state === "done" && j.started_at && j.finished_at)
      .map((j) => (Date.parse(j.finished_at!) - Date.parse(j.started_at!)) / 1000).filter((s) => s > 5).sort((a, b) => a - b);
    return times.length ? times[Math.floor(times.length / 2)] : null;
  }, [app.jobs]);

  const status = (key: string): { label: string; tone: string; busy: boolean } | null => {
    const jobId = pendingClips[key] || Object.entries(pendingClips).find(([k]) => k.startsWith(`${key}v`))?.[1] || pendingFrames[key];
    if (!jobId) return null;
    const job: Job | undefined = app.jobs.find((j) => j.id === jobId);
    const what = pendingFrames[key] === jobId ? t("sbStill") : t("sbClip");
    if (!job || job.state === "queued" || job.state === "waiting_gpu") return { label: `${what} · ${t("sbQueued")}`, tone: "", busy: false };
    if (job.state === "running") {
      const el = job.started_at ? (Date.now() - Date.parse(job.started_at)) / 1000 : 0;
      return { label: `${what} · ${clock(el)}${typical && pendingFrames[key] === jobId ? ` / ~${clock(typical)}` : ""}`, tone: "accent", busy: true };
    }
    if (job.state === "failed") return { label: t("sbFailed"), tone: "bad", busy: false };
    return null;
  };

  const pause = async () => {
    if (!state.job_id) return;
    try { await api.cancelJob(state.job_id); app.toast(t("sbPaused"), "ok"); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const insertButton = (after: string | null) => (
    <button className="sb-insert" disabled={running} title={running ? t("sbPauseFirst") : t("sbInsert")}
      onClick={() => setEditing({ after })}><Plus size={14} /></button>
  );

  return (
    <div className="card">
      <h2 className="row" style={{ gap: 8 }}>
        <span className="grow">{t("shotsTitle")} <span className="muted small">· {shots.length}</span></span>
        <button className="btn sm" onClick={() => setCastOpen(true)} title={t("castHint")}><Users size={13} /> {t("bgCastTitle")}{castCount ? ` · ${castCount}` : ""}</button>
        <button className="btn sm" onClick={() => setLyricsOpen(true)}>{t("sbLyrics")}</button>
        {running && state.job_id && <button className="btn sm" onClick={pause}><Pause size={13} /> {t("sbPause")}</button>}
      </h2>
      <p className="small muted" style={{ marginTop: -6 }}>{running ? t("sbRunningHint") : t("sbHint")}</p>
      <div className="row wrap sb-takes" style={{ gap: 8, marginBottom: 8 }}>
        <span className="small muted"><Gauge size={12} /> {t("sbClipQuality")}</span>
        <div className="seg">
          <button className={draftMode ? "on" : ""} disabled={running || acting} title={t("sbDraftHint")}
            onClick={() => !draftMode && act(() => api.setProductionSettings(state.slug, { clip_quality: "draft" }), t("sbDraftOn"))}>{t("sbDraft")}</button>
          <button className={!draftMode ? "on" : ""} disabled={running || acting} title={t("sbFinalHint")}
            onClick={() => draftMode && act(() => api.setProductionSettings(state.slug, { clip_quality: "final" }), t("sbFinalOn"))}>{t("sbFinal")}</button>
        </div>
        {drafts.length > 0 && <button className="btn sm" disabled={running || acting} title={t("sbPromoteHint")}
          onClick={() => act(() => api.promoteClips(state.slug), t("sbPromoted", { n: drafts.length }))}><Film size={13} /> {t("sbPromote", { n: drafts.length })}</button>}
        <span className="grow" />
        <span className="small muted"><Lock size={12} /> {t("sbLockedN", { n: lockedN, total: shots.length })}</span>
        <button className="btn sm" disabled={running || acting || lockedN === shots.length} title={t("sbRerollClipsHint")}
          onClick={() => act(() => api.regenerateUnlocked(state.slug, "clips"), t("sbRerolling"))}><Dices size={13} /> {t("sbRerollClips")}</button>
        <button className="btn sm" disabled={running || acting || lockedN === shots.length} title={t("sbRerollFramesHint")}
          onClick={() => act(() => api.regenerateUnlocked(state.slug, "frames"), t("sbRerolling"))}><Dices size={13} /> {t("sbRerollFrames")}</button>
      </div>
      <div className="sb-strip">
        {insertButton("start")}
        {shots.map((shot) => {
          const best = frames[shot.key]?.best;
          const st = status(shot.key);
          const when = timing?.shots?.[shot.key];
          const hasClip = Object.keys(clips).some((k) => k === shot.key || k.startsWith(`${shot.key}v`));
          return (
            <div key={shot.key} className={`sb-item${shot.locked ? " locked" : ""}`}>
              <button className="btn xs icon sb-lock" disabled={running || acting} title={shot.locked ? t("sbUnlockHint") : t("sbLockHint")}
                onClick={() => toggleLock(shot)}>{shot.locked ? <Lock size={12} /> : <Unlock size={12} />}</button>
              <button className="tile sb-tile" title={shot.prompt} onClick={() => setEditing({ shot })}>
                {best ? <img src={`/api/assets/${best}/thumb`} alt="" loading="lazy" />
                  : <div className="media-icon">{st?.busy ? <Loader2 size={22} className="spin" /> : <Clapperboard size={22} />}</div>}
                <div className="tile-badges">
                  <span className="pill badge-dark">{shot.key}</span>
                  {shot.lead && <span className="pill badge-dark">{t("leadBadge")}</span>}
                  {hasClip && <span className="pill badge-dark" title={t("clipBadge")}><Film size={10} /></span>}
                  {shot.sing && <span className="pill badge-dark" title={t("sbSing")}><Mic size={10} /></span>}

                  {(shot.refs || []).length > 0 && <span className="pill badge-dark"><Images size={10} /> {shot.refs!.length}</span>}
                  {(shot.crowd || (shot.cast || []).length > 0) && castCount > 0 && <span className="pill badge-dark" title={t("castCrowd")}><Users size={10} /></span>}
                </div>
                {st && <div className={`sb-status ${st.tone}`}>{st.busy && <Loader2 size={11} className="spin" />} {st.label}</div>}
                <div className="tile-meta stack" style={{ gap: 2, alignItems: "stretch" }}>
                  <span className="row small" style={{ gap: 6 }}>
                    {shot.section && <span className="pill">{t(`sec_${shot.section}` as never) || shot.section}</span>}
                    {shot.continue_from && <span className="pill accent" title={t("sbContinuesN", { n: shot.continue_from })}><ArrowRightToLine size={10} /> {shot.continue_from}</span>}
                    {Object.keys(clips).some((k) => (k === shot.key || k.startsWith(`${shot.key}v`)) && quality[k] === "draft") && <span className="pill warn" title={t("sbDraftHint")}>{t("sbDraft")}</span>}
                    {timing?.spans?.[shot.key] && <span className="mono" title={t("trackPinned")}><Pin size={10} /> {clock(timing.spans[shot.key].start_s)}–{clock(timing.spans[shot.key].end_s)}</span>}
                    {!timing?.spans?.[shot.key] && when && when.length > 0 && <span className="mono muted" title={when.map((w) => `${clock(w.start_s)}–${clock(w.start_s + w.duration_s)}`).join("  ")}>
                      {clock(when[0].start_s)}–{clock(when[0].start_s + when[0].duration_s)}{when.length > 1 ? ` ×${when.length}` : ""}</span>}
                  </span>
                  <span className="ellipsis">{shot.prompt}</span>
                </div>
              </button>
              {insertButton(shot.key)}
            </div>
          );
        })}
      </div>
      {editing && (
        <ShotEditor state={state} shot={editing.shot} after={editing.after} running={running}
          onPause={pause} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); onChanged(); }} />
      )}
      {castOpen && <CastModal state={state} running={running} onClose={() => setCastOpen(false)}
        onSaved={() => { setCastOpen(false); onChanged(); }} />}
      {lyricsOpen && <LyricsModal state={state} running={running} onClose={() => setLyricsOpen(false)}
        onSaved={() => { setLyricsOpen(false); onChanged(); }} />}
    </div>
  );
}

/** Every take of a shot (stills and clips), the one in use marked; pick two
 * to see them side by side, or put an earlier one back. */
function TakesRow({ state, shotKey, disabled, onUsed }: { state: ProductionState; shotKey: string; disabled: boolean; onUsed: () => void }) {
  const { t } = useT();
  const app = useApp();
  const takes = useAsync(() => api.productionTakes(state.slug, shotKey), [state.slug, shotKey]);
  const [pick, setPick] = useState<string[]>([]);
  const [compare, setCompare] = useState(false);
  const [busy, setBusy] = useState(false);
  const data = takes.data?.shots[0];
  if (!data) return null;
  const clipTakes = Object.values(data.clips).flat();
  const stillTakes = data.stills.map((s) => ({ asset_id: s.best || s.variants[0], current: s.current, still: true }));
  const all = [...stillTakes.map((s) => ({ ...s, kind: "image" as const })), ...clipTakes.map((c) => ({ ...c, kind: "video" as const, still: false }))];
  if (stillTakes.length < 2 && clipTakes.length < 2) return null;
  const toggle = (id: string) => setPick((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  const use = async (id: string) => {
    setBusy(true);
    try { await api.changeShots(state.slug, [{ key: shotKey, take: id }], false); app.toast(t("sbTakeUsed"), "ok"); onUsed(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const kindOf = (id: string) => all.find((x) => x.asset_id === id)?.kind || "image";
  return (
    <div className="field sb-takes-row">
      <div className="row" style={{ gap: 8 }}>
        <span className="grow">{t("sbTakes")} <span className="muted small">· {t("sbTakesHint")}</span></span>
        <button type="button" className="btn sm" disabled={pick.length !== 2} onClick={() => setCompare(true)}><Columns2 size={13} /> {t("sbCompare")}</button>
      </div>
      <div className="row wrap" style={{ gap: 6 }}>
        {all.map((x) => (
          <div key={x.asset_id} className={`sb-take${x.current ? " current" : ""}${pick.includes(x.asset_id) ? " picked" : ""}`}>
            <button type="button" className="tile" style={{ width: 112 }} onClick={() => toggle(x.asset_id)}
              onDoubleClick={() => app.openAsset(x.asset_id, all.map((y) => y.asset_id))} title={x.kind === "video" ? t("sbClip") : t("sbStill")}>
              <img src={`/api/assets/${x.asset_id}/thumb`} alt="" />
              <span className="pill badge-dark sb-take-kind">{x.kind === "video" ? <Film size={10} /> : <Images size={10} />}{"quality" in x && x.quality === "draft" ? ` ${t("sbDraft")}` : ""}{"retake" in x && x.retake ? ` ${t("sbRetake")}` : ""}{"edit" in x && x.edit ? ` ${t("sbEdited")}` : ""}</span>
            </button>
            {x.current ? <span className="small muted">{t("sbTakeCurrent")}</span>
              : <button type="button" className="btn xs" disabled={disabled || busy} onClick={() => use(x.asset_id)}>{t("sbTakeUse")}</button>}
          </div>
        ))}
      </div>
      {compare && pick.length === 2 && (
        <Modal title={t("sbCompare")} wide onClose={() => setCompare(false)}>
          <div className="sb-compare">
            {pick.map((id) => (
              <div key={id} className="stack" style={{ gap: 6 }}>
                {kindOf(id) === "video"
                  ? <video src={`/api/assets/${id}/file`} controls autoPlay loop muted playsInline />
                  : <img src={`/api/assets/${id}/file`} alt="" />}
                <button type="button" className="btn sm" disabled={disabled || busy || all.find((x) => x.asset_id === id)?.current}
                  onClick={() => { setCompare(false); use(id); }}><Check size={13} /> {t("sbTakeUse")}</button>
              </div>
            ))}
          </div>
        </Modal>
      )}
    </div>
  );
}

export function ShotEditor({ state, shot, after, running, onPause, onClose, onSaved, initialSpan, initialSection }: {
  state: ProductionState; shot?: ProductionShot; after?: string | null; running: boolean;
  onPause: () => void; onClose: () => void; onSaved: () => void;
  initialSpan?: { start: number; end: number }; initialSection?: string;
}) {
  const { t } = useT();
  const app = useApp();
  const isNew = !shot;
  const frames = (state.done?.frames?.items || {}) as Record<string, { best?: string; variants?: string[] }>;
  const entry = shot ? frames[shot.key] || {} : {};
  const variants = entry.variants || [];
  const [prompt, setPrompt] = useState(shot?.prompt || "");
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const slash = useSlashMenu(prompt, setPrompt, promptRef, { clips: true });
  const [motionPrompt, setMotionPrompt] = useState(shot?.motion_prompt || "");
  const motionInputRef = useRef<HTMLInputElement>(null);
  const motionSlash = useSlashMenu(motionPrompt, setMotionPrompt, motionInputRef, { clips: true });
  const [lead, setLead] = useState(shot ? shot.lead : true);
  const [motion, setMotion] = useState<"still" | "move">(shot?.motion || "move");
  const [clip, setClip] = useState(shot ? shot.clips.length > 0 : true);
  const [section, setSection] = useState(shot?.section || initialSection || "");
  const ownSpan = shot && shot.start_s != null && shot.end_s != null ? { start: shot.start_s, end: shot.end_s } : null;
  const [span, setSpan] = useState<{ start: number; end: number } | null>(initialSpan || ownSpan);
  const [spanText, setSpanText] = useState(() => {
    const s0 = initialSpan || ownSpan;
    return s0 ? [clockTenths(s0.start), clockTenths(s0.end)] : ["", ""];
  });
  const [refs, setRefs] = useState<ShotRef[]>(shot?.refs || []);
  const [motionRef, setMotionRef] = useState<MotionRef | null>(shot?.motion_ref || null);
  const [crowd, setCrowd] = useState(!!shot?.crowd);
  const [sing, setSing] = useState(!!shot?.sing);
  const [castNames, setCastNames] = useState<string[]>(shot?.cast || []);
  const cast = state.spec.cast || [];
  const [motionStart, setMotionStart] = useState(shot?.motion_ref ? clock(shot.motion_ref.start_s) : "0:00");
  const [motionPick, setMotionPick] = useState<null | "library" | "link">(null);
  const [best, setBest] = useState(Math.max(0, variants.indexOf(entry.best || "")));
  const [regenerate, setRegenerate] = useState(false);
  const [locked, setLocked] = useState(!!shot?.locked);
  const [continueFrom, setContinueFrom] = useState(shot?.continue_from || "");
  const earlier = (() => {
    const all = state.spec.shots || [];
    const idx = shot ? all.findIndex((s) => s.key === shot.key) : (after && after !== "start" ? all.findIndex((s) => s.key === after) + 1 : 0);
    return all.slice(0, idx < 0 ? all.length : idx).filter((s) => s.clips.length > 0 && s.key !== shot?.key);
  })();
  const [picking, setPicking] = useState<"image" | "video" | null>(null);
  const [linkOpen, setLinkOpen] = useState(false);
  const [framesOf, setFramesOf] = useState<Asset[] | null>(null);
  const [busy, setBusy] = useState(false);
  const projectId = state.project_id || app.projectId || "";
  const castOfProject = useAsync(() => (projectId ? api.characters(projectId) : Promise.resolve({ items: [] })), [projectId]);
  const elements = (castOfProject.data?.items || []).filter((c) => c.element === "location" || c.element === "prop");
  const esc = (x: string) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const mentioned = (name: string) => new RegExp(`(^|[^\\w])@${esc(name)}(?![\\w])`, "iu").test(prompt);
  // one click puts ", @Stage" at the end of the prompt and a second click
  // takes that back; a mention written inside a sentence is left alone
  const toggleElement = (name: string) => setPrompt((p) => {
    if (!mentioned(name)) return `${p.trim()}${p.trim() ? ", " : ""}@${name}`;
    const tail = new RegExp(`(^|,\\s*)@${esc(name)}\\s*$`, "iu");
    return tail.test(p.trim()) ? p.trim().replace(tail, "").trim() : p;
  });
  const lines = (state.timing?.sections || []).filter((s) => sectionMatches(s.label, s.kind, section));
  const spanLines = span ? (state.timing?.lines || []).filter((l) => l.time_s < span.end - 0.05 && (l.end_s ?? l.time_s + 1) > span.start + 0.05) : [];
  const applySpanText = (a: string, b: string) => {
    setSpanText([a, b]);
    const s0 = parseClock(a), s1 = parseClock(b);
    if (a.trim() && b.trim() && s1 - s0 >= 0.5) setSpan({ start: s0, end: s1 });
  };
  const firstTag = lead ? 2 : 1;

  const addRef = (a: Asset) => {
    if (refs.some((r) => r.asset_id === a.id) || refs.length >= 6) return;
    setRefs([...refs, { asset_id: a.id, use: "" }]);
  };
  const pickedVideo = async (a: Asset) => {
    setPicking(null);
    setBusy(true);
    try { setFramesOf((await api.videoFrames(a.id, 8)).items); app.bump(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const upload = async (file: File) => {
    setBusy(true);
    try {
      const a = await api.upload(projectId, file);
      app.bump();
      if (a.kind === "video") setFramesOf((await api.videoFrames(a.id, 8)).items);
      else addRef(a);
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };

  const save = async (run: boolean) => {
    if (!prompt.trim()) { app.toast(t("sbNeedPrompt"), "bad"); return; }
    let change: Record<string, unknown>;
    const extraChange: Record<string, unknown>[] = [];
    if (isNew) {
      change = { insert: { after: after ?? "start", prompt: prompt.trim(), lead, motion, clip: motion === "move" && clip,
                           motion_prompt: motionPrompt.trim() || undefined, section: section || undefined, refs, crowd,
                           sing: sing && !!span ? true : undefined,
                           motion_ref: motionRef ? { ...motionRef, start_s: parseClock(motionStart) } : undefined,
                           span: span ? { start_s: span.start, end_s: span.end } : undefined } };
      if (continueFrom) extraChange.push({ continue_from: continueFrom });
    } else {
      change = { key: shot!.key };
      if (prompt.trim() !== shot!.prompt) change.prompt = prompt.trim();
      if (motionPrompt.trim() && motionPrompt.trim() !== (shot!.motion_prompt || "")) change.motion_prompt = motionPrompt.trim();
      if (lead !== shot!.lead) change.lead = lead;
      if (motion !== shot!.motion) change.motion = motion;
      if (clip !== shot!.clips.length > 0) change.clip = clip;
      if (section !== (shot!.section || "")) change.section = section;
      if (JSON.stringify(refs) !== JSON.stringify(shot!.refs || [])) change.refs = refs;
      if (crowd !== !!shot!.crowd) change.crowd = crowd;
      if (sing !== !!shot!.sing) change.sing = sing;
      if (JSON.stringify(castNames) !== JSON.stringify(shot!.cast || [])) change.cast = castNames;
      const mr = motionRef ? { ...motionRef, start_s: parseClock(motionStart) } : null;
      if (JSON.stringify(mr) !== JSON.stringify(shot!.motion_ref || null)) change.motion_ref = mr;
      if (variants.length > 1 && variants[best] !== entry.best) change.best = best;
      if (regenerate) change.regenerate = true;
      if (JSON.stringify(span) !== JSON.stringify(ownSpan)) change.span = span ? { start_s: span.start, end_s: span.end } : null;
      if (continueFrom !== (shot!.continue_from || "")) change.continue_from = continueFrom || null;
      if (locked && shot!.locked) {
        // an approved shot only changes its place in the song
        change = Object.fromEntries(Object.entries(change).filter(([k]) => ["key", "section"].includes(k)));
      }
      if (locked !== !!shot!.locked) change.locked = locked;
      if (Object.keys(change).length === 1) { onClose(); return; }
    }
    setBusy(true);
    try {
      const later = isNew && (castNames.length > 0 || extraChange.length > 0);
      const sent = await api.changeShots(state.slug, [change], run && !later);
      // a new shot's own cast (and chaining) goes in a second change, once it has a key
      if (later) {
        const key = (sent as { changed?: string[] }).changed?.[0];
        if (key) await api.changeShots(state.slug, [{ key, ...(castNames.length ? { cast: castNames } : {}), ...Object.assign({}, ...extraChange) }], run);
      }
      app.toast(run ? t("sbSavedRun") : t("sbSaved"), "ok");
      onSaved();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!shot || !window.confirm(t("sbDeleteConfirm", { n: shot.key }))) return;
    setBusy(true);
    try { await api.changeShots(state.slug, [{ key: shot.key, delete: true }], false); onSaved(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={isNew ? t("sbNewShot") : t("sbShotN", { n: shot!.key })} onClose={onClose} wide footer={running ? (
      <>
        <span className="hint grow">{t("sbPauseFirst")}</span>
        {state.job_id && <button className="btn primary" onClick={onPause}><Pause size={14} /> {t("sbPause")}</button>}
      </>
    ) : (
      <>
        {!isNew && <button className="btn ghost" disabled={busy} onClick={remove}><Trash2 size={14} /> {t("sbDelete")}</button>}
        <span className="grow" />
        <button className="btn" disabled={busy} onClick={() => save(false)}>{t("sbSave")}</button>
        <button className="btn primary" disabled={busy} onClick={() => save(true)}>{busy ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} {t("sbSaveRun")}</button>
      </>
    )}>
      {!isNew && (
        <label className={`check sb-approve${locked ? " on" : ""}`} title={t("sbLockHint")}>
          <input type="checkbox" checked={locked} disabled={running || busy} onChange={(e) => setLocked(e.target.checked)} />
          {locked ? <Lock size={13} /> : <Unlock size={13} />} {t("sbApproved")}
          <span className="hint">{locked ? t("sbApprovedHint") : t("sbLockHint")}</span>
        </label>
      )}
      <fieldset disabled={running || busy || (locked && !!shot?.locked)} className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
        {!isNew && <TakesRow state={state} shotKey={shot!.key} disabled={running || busy || (locked && !!shot?.locked)} onUsed={onSaved} />}
        {variants.length > 0 && (
          <div className="field">{t("sbStillPick")}
            <div className="row wrap" style={{ gap: 6 }}>
              {variants.map((id, i) => (
                <button key={id} type="button" className={`tile${i === best ? " selected" : ""}`} style={{ width: 120 }}
                  onClick={() => setBest(i)} onDoubleClick={() => app.openAsset(id, variants)}>
                  <img src={`/api/assets/${id}/thumb`} alt="" />
                </button>
              ))}
            </div>
          </div>
        )}
        <label className="field">{t("sbPrompt")}
          <div className="slash-wrap">
            <textarea ref={promptRef} rows={3} value={prompt} placeholder={t("sbPromptPh")}
              onChange={(e) => { setPrompt(e.target.value); slash.update(e.target.value, e.target.selectionStart); }}
              onKeyDown={(e) => { slash.onKeyDown(e); }} />
            {slash.menu}
          </div>
          <span className="row" style={{ gap: 8 }}><span className="hint grow">{lead ? t("sbPromptLeadHint") : t("sbPromptSceneHint")}</span>
            <Enhance text={prompt} kind="image" onDone={setPrompt} className="btn xs" /></span>
        </label>
        {elements.length > 0 && (
          <div className="row wrap" style={{ gap: 6, marginTop: -4 }}>
            <span className="muted small">{t("sbElements")}</span>
            {elements.map((c) => (
              <button key={c.id} type="button" className={`btn sm ${mentioned(c.name) ? "primary" : "ghost"}`}
                title={c.canonical_asset_id ? t("elementRefNote") : c.prompt || ""} onClick={() => toggleElement(c.name)}>
                {c.element === "location" ? <MapPin size={12} /> : <Box size={12} />} @{c.name}
              </button>
            ))}
          </div>
        )}
        <div className="row wrap" style={{ gap: 16 }}>
          <label className="check"><input type="checkbox" checked={lead} onChange={(e) => setLead(e.target.checked)} /> {t("sbLead")}</label>
          <label className="field" style={{ minWidth: 170 }}>{t("sbSection")}
            <select value={section} onChange={(e) => setSection(e.target.value)}>
              <option value="">{t("sbSectionAny")}</option>
              {SECTIONS.map((s) => <option key={s} value={s}>{t(`sec_${s}` as never) || s}</option>)}
            </select>
          </label>
          <label className="field" style={{ minWidth: 150 }}>{t("sbMotion")}
            <select value={motion} onChange={(e) => setMotion(e.target.value as "still" | "move")}>
              <option value="move">{t("sbMove")}</option>
              <option value="still">{t("sbStillOnly")}</option>
            </select>
          </label>
          {motion === "move" && <label className="check"><input type="checkbox" checked={clip} onChange={(e) => setClip(e.target.checked)} /> {t("makeClip")}</label>}
          {motion === "move" && earlier.length > 0 && !sing && (
            <label className="field" style={{ minWidth: 190 }} title={t("sbContinueHint")}>{t("sbContinue")}
              <select value={continueFrom} onChange={(e) => { setContinueFrom(e.target.value); if (e.target.value) setClip(true); }}>
                <option value="">{t("sbContinueNone")}</option>
                {earlier.map((s) => <option key={s.key} value={s.key}>{t("sbContinueOf", { n: s.key })}</option>)}
              </select>
            </label>
          )}
          <label className="check" title={span ? t("sbSingHint") : t("sbSingNeedsSpan")}>
            <input type="checkbox" checked={sing} disabled={!span && !sing}
              onChange={(e) => { setSing(e.target.checked); if (e.target.checked) { setMotion("move"); setClip(true); } }} />
            <Mic size={13} /> {t("sbSing")}
          </label>
        </div>
        {sing && <p className="hint" style={{ marginTop: -6 }}>{span ? t("sbSingHint") : t("sbSingNeedsSpan")}</p>}
        <div className="field">{t("spanTitle")}
          <span className="hint">{span ? t("spanPinnedHint") : t("spanFreeHint")}</span>
          <div className="row wrap" style={{ gap: 8 }}>
            <label className="row small" style={{ gap: 4 }}>{t("trackFrom")}
              <input style={{ width: 80 }} value={spanText[0]} placeholder="0:12.4" onChange={(e) => applySpanText(e.target.value, spanText[1])} /></label>
            <label className="row small" style={{ gap: 4 }}>{t("trackTo")}
              <input style={{ width: 80 }} value={spanText[1]} placeholder="0:19.8" onChange={(e) => applySpanText(spanText[0], e.target.value)} /></label>
            {span && <span className="small muted">{(span.end - span.start).toFixed(1)} s</span>}
            {span && <button type="button" className="btn sm ghost" onClick={() => { setSpan(null); setSpanText(["", ""]); }}><X size={12} /> {t("spanFree")}</button>}
          </div>
          {spanLines.length > 0 && (
            <div className="sb-lyrics" style={{ gridTemplateColumns: "1fr" }}>
              {spanLines.map((l, i) => <div key={i} className="small"><span className="mono muted">{clockTenths(l.time_s)}</span> {l.text}</div>)}
            </div>
          )}
        </div>
        {!span && lines.length > 0 && (
          <div className="sb-lyrics">
            {lines.map((s, i) => (
              <div key={i}><strong className="small">{s.label}{s.start_s != null ? ` · ${clock(s.start_s)}` : ""}</strong>
                <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{s.lines.join("\n")}</div></div>
            ))}
          </div>
        )}
        {motion === "move" && (
          <label className="field">{t("sbMotionPrompt")}
            <span className="slash-wrap" style={{ display: "block" }}>
              <input ref={motionInputRef} value={motionPrompt} placeholder={t("sbMotionPh")} style={{ width: "100%" }}
                onChange={(e) => { setMotionPrompt(e.target.value); motionSlash.update(e.target.value, e.target.selectionStart); }}
                onKeyDown={(e) => { motionSlash.onKeyDown(e); }} />
              {motionSlash.menu}
            </span>
            <span className="row wrap" style={{ gap: 4, marginTop: 4 }}>
              <span className="hint">{t("sbCamera")}</span>
              {CAMERA.map((c) => (
                <button key={c} type="button" className="btn sm ghost" onClick={() => setMotionPrompt((m) => (m.trim() ? `${m.trim().replace(/[.,;]$/, "")}, ` : "") + CAMERA_TEXT[c])}>{t(c)}</button>
              ))}
            </span>
          </label>
        )}
        {motion === "move" && (
          <div className="field">{t("sbMotionRef")}
            <span className="hint">{t("sbMotionRefHint")}</span>
            {motionRef ? (
              <div className="row wrap" style={{ gap: 8 }}>
                <img src={thumbUrl({ id: motionRef.asset_id, thumb_path: "x", kind: "video" })} alt="" style={{ width: 96, height: 54, objectFit: "cover", borderRadius: 6 }} />
                <label className="row" style={{ gap: 4 }}>{t("sbMotionFrom")}
                  <input style={{ width: 70 }} value={motionStart} onChange={(e) => setMotionStart(e.target.value)} /></label>
                <input className="grow" value={motionRef.prompt} placeholder={t("sbMotionRefPromptPh")}
                  onChange={(e) => setMotionRef({ ...motionRef, prompt: e.target.value })} />
                <button type="button" className="btn sm icon ghost" onClick={() => setMotionRef(null)}><X size={13} /></button>
              </div>
            ) : (
              <div className="row wrap" style={{ gap: 6 }}>
                <button type="button" className="btn sm" onClick={() => setMotionPick("library")}><Film size={13} /> {t("sbRefPick")}</button>
                <button type="button" className={`btn sm${motionPick === "link" ? " primary" : ""}`} onClick={() => setMotionPick(motionPick === "link" ? null : "link")}><Link2 size={13} /> {t("sbRefLink")}</button>
              </div>
            )}
            {motionPick === "link" && projectId && (
              <LinkDownload projectId={projectId} compact onDone={(a) => {
                setMotionPick(null);
                if (a.kind === "video") { setMotionRef({ asset_id: a.id, start_s: 0, prompt: "" }); setMotionStart("0:00"); }
              }} />
            )}
            <span className="hint">{t("sbMotionRefNote")}</span>
          </div>
        )}
        <div className="field">{t("bgCastTitle")}
          {cast.length === 0 ? <span className="hint">{t("castNone")}</span> : (
            <>
              <label className="check"><input type="checkbox" checked={crowd || castNames.length > 0} disabled={castNames.length > 0}
                onChange={(e) => setCrowd(e.target.checked)} /> {t("castCrowd")}</label>
              <span className="hint">{castNames.length ? t("castPickedHint") : t("castAutoHint", { n: state.spec.cast_per_shot || 3 })}</span>
              <div className="row wrap" style={{ gap: 6 }}>
                {cast.map((m) => {
                  const on = castNames.includes(m.name);
                  return (
                    <button key={m.name} type="button" className={`tile${on ? " selected" : ""}`} style={{ width: 76 }} title={m.note || m.name}
                      onClick={() => setCastNames(on ? castNames.filter((n) => n !== m.name) : castNames.length >= 6 ? castNames : [...castNames, m.name])}>
                      <img src={thumbUrl({ id: m.asset_id, thumb_path: "x", kind: "image" })} alt="" />
                      <div className="tile-meta small ellipsis">{m.name}</div>
                    </button>
                  );
                })}
              </div>
            </>
          )}
        </div>
        <div className="field">{t("sbRefs")}
          <span className="hint">{t("sbRefsHint")}</span>
          <div className="stack" style={{ gap: 6 }}>
            {refs.map((r, i) => (
              <div key={r.asset_id} className="row" style={{ gap: 8 }}>
                <img src={thumbUrl({ id: r.asset_id, thumb_path: "x", kind: "image" })} alt="" style={{ width: 48, height: 48, objectFit: "cover", borderRadius: 6 }} />
                <span className="mono small">{`<image${firstTag + i}>`}</span>
                <input className="grow" value={r.use} placeholder={t("sbRefUsePh")}
                  onChange={(e) => setRefs(refs.map((x, j) => (j === i ? { ...x, use: e.target.value } : x)))} />
                <button type="button" className="btn sm icon ghost" onClick={() => setRefs(refs.filter((_, j) => j !== i))}><X size={13} /></button>
              </div>
            ))}
            <div className="row wrap" style={{ gap: 6 }}>
              <button type="button" className="btn sm" disabled={refs.length >= 6} onClick={() => setPicking("image")}><Images size={13} /> {t("sbRefPick")}</button>
              <button type="button" className="btn sm" disabled={refs.length >= 6} onClick={() => setPicking("video")}><Film size={13} /> {t("sbRefFromVideo")}</button>
              <button type="button" className={`btn sm${linkOpen ? " primary" : ""}`} disabled={refs.length >= 6} onClick={() => setLinkOpen(!linkOpen)}><Link2 size={13} /> {t("sbRefLink")}</button>
              <label className="btn sm"><Upload size={13} /> {t("sbRefUpload")}
                <input type="file" accept="image/*,video/*,.gif" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }} /></label>
              {busy && <Loader2 size={14} className="spin" />}
            </div>
            {linkOpen && projectId && (
              <LinkDownload projectId={projectId} compact onDone={async (a) => {
                setLinkOpen(false);
                if (a.kind !== "video") return;
                setBusy(true);
                try { setFramesOf((await api.videoFrames(a.id, 8)).items); } catch (e) { app.toast((e as Error).message, "bad"); }
                finally { setBusy(false); }
              }} />
            )}
            {framesOf && (
              <div className="stack" style={{ gap: 4 }}>
                <span className="hint">{t("sbFramesPick")}</span>
                <div className="row wrap" style={{ gap: 6 }}>
                  {framesOf.map((f) => (
                    <button key={f.id} type="button" className={`tile${refs.some((r) => r.asset_id === f.id) ? " selected" : ""}`} style={{ width: 92 }}
                      onClick={() => addRef(f)}><img src={thumbUrl(f)} alt="" /></button>
                  ))}
                </div>
              </div>
            )}
            <span className="hint">{t("sbRefsStillNote")}</span>
          </div>
        </div>
        {!isNew && variants.length > 0 && (
          <label className="check"><input type="checkbox" checked={regenerate} onChange={(e) => setRegenerate(e.target.checked)} /> {t("regenerateShot")}</label>
        )}
      </fieldset>
      {motionPick === "library" && projectId && (
        <AssetPicker projectId={projectId} kind="video" allProjects title={t("sbMotionRef")}
          onPick={(a) => { setMotionRef({ asset_id: a.id, start_s: 0, prompt: "" }); setMotionStart("0:00"); setMotionPick(null); }}
          onClose={() => setMotionPick(null)} />
      )}
      {picking && projectId && (
        <AssetPicker projectId={projectId} kind={picking} allProjects title={picking === "video" ? t("sbRefFromVideo") : t("sbRefPick")}
          onPick={(a) => { if (picking === "video") pickedVideo(a); else { addRef(a); setPicking(null); } }} onClose={() => setPicking(null)} />
      )}
    </Modal>
  );
}

/** The production's background cast: the only characters allowed behind
 * the lead. One image and a name each; crowd shots take a few of them. */
function CastModal({ state, running, onClose, onSaved }: { state: ProductionState; running: boolean; onClose: () => void; onSaved: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [members, setMembers] = useState<CastMember[]>(state.spec.cast || []);
  const [perShot, setPerShot] = useState(state.spec.cast_per_shot || 3);
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const projectId = state.project_id || app.projectId || "";
  const nameFor = (raw: string) => {
    let base = raw.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim().slice(0, 60) || t("castMember");
    let name = base;
    for (let i = 2; members.some((m) => m.name.toLowerCase() === name.toLowerCase()); i++) name = `${base} ${i}`;
    return name;
  };
  const add = (a: Asset) => setMembers((ms) => ms.some((m) => m.asset_id === a.id) || ms.length >= 24 ? ms
    : [...ms, { asset_id: a.id, name: nameFor(a.name || ""), note: "" }]);
  const upload = async (files: FileList) => {
    setBusy(true);
    try {
      for (const f of Array.from(files).slice(0, 24)) add(await api.upload(projectId, f));
      app.bump();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const save = async (run: boolean) => {
    if (members.some((m) => !m.name.trim())) { app.toast(t("castNeedName"), "bad"); return; }
    setBusy(true);
    try {
      const out = await api.setProductionCast(state.slug, members.map((m) => ({ ...m, name: m.name.trim() })), perShot, run);
      app.toast(out.redraw.length ? t("castSavedRedraw", { n: out.redraw.join(", ") }) : t("castSaved"), "ok");
      onSaved();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  return (
    <Modal title={t("bgCastTitle")} onClose={onClose} wide footer={running ? <span className="hint">{t("sbPauseFirst")}</span> : (
      <>
        <span className="grow" />
        <button className="btn" disabled={busy} onClick={() => save(false)}>{t("sbSave")}</button>
        <button className="btn primary" disabled={busy} onClick={() => save(true)}>{busy ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} {t("sbSaveRun")}</button>
      </>
    )}>
      <fieldset disabled={running || busy} className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
        <p className="small muted">{t("castHint")}</p>
        <div className="stack" style={{ gap: 6 }}>
          {members.map((m, i) => (
            <div key={m.asset_id} className="row" style={{ gap: 8 }}>
              <img src={thumbUrl({ id: m.asset_id, thumb_path: "x", kind: "image" })} alt="" style={{ width: 56, height: 56, objectFit: "contain", borderRadius: 6, background: "var(--panel-2, #0002)" }} />
              <input style={{ width: 180 }} value={m.name} placeholder={t("castNamePh")} maxLength={60}
                onChange={(e) => setMembers(members.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
              <input className="grow" value={m.note || ""} placeholder={t("castNotePh")} maxLength={200}
                onChange={(e) => setMembers(members.map((x, j) => (j === i ? { ...x, note: e.target.value } : x)))} />
              <button type="button" className="btn sm icon ghost" onClick={() => setMembers(members.filter((_, j) => j !== i))}><X size={13} /></button>
            </div>
          ))}
          {members.length === 0 && <span className="hint">{t("castEmpty")}</span>}
        </div>
        <div className="row wrap" style={{ gap: 6 }}>
          <button type="button" className="btn sm" disabled={members.length >= 24} onClick={() => setPicking(true)}><Images size={13} /> {t("sbRefPick")}</button>
          <label className="btn sm"><Upload size={13} /> {t("castUpload")}
            <input type="file" accept="image/*" multiple hidden onChange={(e) => { if (e.target.files?.length) upload(e.target.files); e.target.value = ""; }} /></label>
          <span className="grow" />
          <label className="row small" style={{ gap: 6 }}>{t("castPerShot")}
            <select value={perShot} onChange={(e) => setPerShot(Number(e.target.value))}>
              {[1, 2, 3, 4, 5, 6].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        </div>
        <span className="hint">{t("castHowTo")}</span>
      </fieldset>
      {picking && projectId && (
        <AssetPicker projectId={projectId} kind="image" allProjects title={t("bgCastTitle")}
          onPick={(a) => { add(a); setPicking(false); }} onClose={() => setPicking(false)} />
      )}
    </Modal>
  );
}

export function LyricsModal({ state, running, onClose, onSaved }: { state: ProductionState; running: boolean; onClose: () => void; onSaved: () => void }) {
  const { t } = useT();
  const app = useApp();
  const song = (state.spec.song || {}) as { lyrics?: string };
  const [text, setText] = useState(song.lyrics || "");
  const save = async () => {
    try { await api.setProductionLyrics(state.slug, text, false); app.toast(t("sbLyricsSaved"), "ok"); onSaved(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  return (
    <Modal title={t("sbLyrics")} onClose={onClose} wide footer={running ? <span className="hint">{t("sbPauseFirst")}</span>
      : <button className="btn primary" onClick={save}>{t("sbSave")}</button>}>
      <p className="small muted">{t("sbLyricsHint")}</p>
      <textarea rows={18} style={{ width: "100%" }} value={text} disabled={running} onChange={(e) => setText(e.target.value)}
        placeholder={"[Verse]\n...\n[Chorus]\n..."} />
    </Modal>
  );
}
