import { useMemo, useState } from "react";
import { Clapperboard, Film, Images, Link2, Loader2, Pause, Plus, RefreshCw, Trash2, Upload, X } from "lucide-react";
import { api, thumbUrl, type Asset, type Job, type ProductionShot, type ProductionState, type ShotRef } from "../api";
import { useT } from "../i18n";
import { AssetPicker, Modal, useApp } from "../components/ui";
import { LinkDownload } from "../components/LinkDownload";

// the song sections a shot can illustrate (what the planner writes)
const SECTIONS = ["intro", "verse", "prechorus", "chorus", "bridge", "breakdown", "outro"];
const SECTION_KIND: Record<string, string> = { prechorus: "pre", breakdown: "bridge" };

const clock = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

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
  const shots = state.spec.shots || [];
  const frames = (state.done?.frames?.items || {}) as Record<string, { best?: string; variants?: string[] }>;
  const clips = (state.done?.clips?.items || {}) as Record<string, string>;
  const pendingFrames = (state.partial?.frames?.pending || {}) as Record<string, string>;
  const pendingClips = (state.partial?.clips?.pending || {}) as Record<string, string>;
  const running = ["running", "queued"].includes(state.status);
  const timing = state.timing;

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
        <button className="btn sm" onClick={() => setLyricsOpen(true)}>{t("sbLyrics")}</button>
        {running && state.job_id && <button className="btn sm" onClick={pause}><Pause size={13} /> {t("sbPause")}</button>}
      </h2>
      <p className="small muted" style={{ marginTop: -6 }}>{running ? t("sbRunningHint") : t("sbHint")}</p>
      <div className="sb-strip">
        {insertButton("start")}
        {shots.map((shot) => {
          const best = frames[shot.key]?.best;
          const st = status(shot.key);
          const when = timing?.shots?.[shot.key];
          const hasClip = Object.keys(clips).some((k) => k === shot.key || k.startsWith(`${shot.key}v`));
          return (
            <div key={shot.key} className="sb-item">
              <button className="tile sb-tile" title={shot.prompt} onClick={() => setEditing({ shot })}>
                {best ? <img src={`/api/assets/${best}/thumb`} alt="" loading="lazy" />
                  : <div className="media-icon">{st?.busy ? <Loader2 size={22} className="spin" /> : <Clapperboard size={22} />}</div>}
                <div className="tile-badges">
                  <span className="pill badge-dark">{shot.key}</span>
                  {shot.lead && <span className="pill badge-dark">{t("leadBadge")}</span>}
                  {hasClip && <span className="pill badge-dark"><Film size={10} /> {t("clipBadge")}</span>}
                  {(shot.refs || []).length > 0 && <span className="pill badge-dark"><Images size={10} /> {shot.refs!.length}</span>}
                </div>
                {st && <div className={`sb-status ${st.tone}`}>{st.busy && <Loader2 size={11} className="spin" />} {st.label}</div>}
                <div className="tile-meta stack" style={{ gap: 2, alignItems: "stretch" }}>
                  <span className="row small" style={{ gap: 6 }}>
                    {shot.section && <span className="pill">{t(`sec_${shot.section}` as never) || shot.section}</span>}
                    {when && when.length > 0 && <span className="mono muted" title={when.map((w) => `${clock(w.start_s)}–${clock(w.start_s + w.duration_s)}`).join("  ")}>
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
      {lyricsOpen && <LyricsModal state={state} running={running} onClose={() => setLyricsOpen(false)}
        onSaved={() => { setLyricsOpen(false); onChanged(); }} />}
    </div>
  );
}

function ShotEditor({ state, shot, after, running, onPause, onClose, onSaved }: {
  state: ProductionState; shot?: ProductionShot; after?: string | null; running: boolean;
  onPause: () => void; onClose: () => void; onSaved: () => void;
}) {
  const { t } = useT();
  const app = useApp();
  const isNew = !shot;
  const frames = (state.done?.frames?.items || {}) as Record<string, { best?: string; variants?: string[] }>;
  const entry = shot ? frames[shot.key] || {} : {};
  const variants = entry.variants || [];
  const [prompt, setPrompt] = useState(shot?.prompt || "");
  const [motionPrompt, setMotionPrompt] = useState(shot?.motion_prompt || "");
  const [lead, setLead] = useState(shot ? shot.lead : true);
  const [motion, setMotion] = useState<"still" | "move">(shot?.motion || "move");
  const [clip, setClip] = useState(shot ? shot.clips.length > 0 : true);
  const [section, setSection] = useState(shot?.section || "");
  const [refs, setRefs] = useState<ShotRef[]>(shot?.refs || []);
  const [best, setBest] = useState(Math.max(0, variants.indexOf(entry.best || "")));
  const [regenerate, setRegenerate] = useState(false);
  const [picking, setPicking] = useState<"image" | "video" | null>(null);
  const [linkOpen, setLinkOpen] = useState(false);
  const [framesOf, setFramesOf] = useState<Asset[] | null>(null);
  const [busy, setBusy] = useState(false);
  const projectId = state.project_id || app.projectId || "";
  const lines = (state.timing?.sections || []).filter((s) => sectionMatches(s.label, s.kind, section));
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
    if (isNew) {
      change = { insert: { after: after ?? "start", prompt: prompt.trim(), lead, motion, clip: motion === "move" && clip,
                           motion_prompt: motionPrompt.trim() || undefined, section: section || undefined, refs } };
    } else {
      change = { key: shot!.key };
      if (prompt.trim() !== shot!.prompt) change.prompt = prompt.trim();
      if (motionPrompt.trim() && motionPrompt.trim() !== (shot!.motion_prompt || "")) change.motion_prompt = motionPrompt.trim();
      if (lead !== shot!.lead) change.lead = lead;
      if (motion !== shot!.motion) change.motion = motion;
      if (clip !== shot!.clips.length > 0) change.clip = clip;
      if (section !== (shot!.section || "")) change.section = section;
      if (JSON.stringify(refs) !== JSON.stringify(shot!.refs || [])) change.refs = refs;
      if (variants.length > 1 && variants[best] !== entry.best) change.best = best;
      if (regenerate) change.regenerate = true;
      if (Object.keys(change).length === 1) { onClose(); return; }
    }
    setBusy(true);
    try {
      await api.changeShots(state.slug, [change], run);
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
      <fieldset disabled={running || busy} className="stack" style={{ border: 0, padding: 0, margin: 0 }}>
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
          <textarea rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} placeholder={t("sbPromptPh")} />
          <span className="hint">{lead ? t("sbPromptLeadHint") : t("sbPromptSceneHint")}</span>
        </label>
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
        </div>
        {lines.length > 0 && (
          <div className="sb-lyrics">
            {lines.map((s, i) => (
              <div key={i}><strong className="small">{s.label}{s.start_s != null ? ` · ${clock(s.start_s)}` : ""}</strong>
                <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{s.lines.join("\n")}</div></div>
            ))}
          </div>
        )}
        {motion === "move" && (
          <label className="field">{t("sbMotionPrompt")}
            <input value={motionPrompt} onChange={(e) => setMotionPrompt(e.target.value)} placeholder={t("sbMotionPh")} />
          </label>
        )}
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
            <span className="hint">{t("sbMotionLimit")}</span>
          </div>
        </div>
        {!isNew && variants.length > 0 && (
          <label className="check"><input type="checkbox" checked={regenerate} onChange={(e) => setRegenerate(e.target.checked)} /> {t("regenerateShot")}</label>
        )}
      </fieldset>
      {picking && projectId && (
        <AssetPicker projectId={projectId} kind={picking} title={picking === "video" ? t("sbRefFromVideo") : t("sbRefPick")}
          onPick={(a) => { if (picking === "video") pickedVideo(a); else { addRef(a); setPicking(null); } }} onClose={() => setPicking(null)} />
      )}
    </Modal>
  );
}

function LyricsModal({ state, running, onClose, onSaved }: { state: ProductionState; running: boolean; onClose: () => void; onSaved: () => void }) {
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
