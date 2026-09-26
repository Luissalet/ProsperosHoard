import { useEffect, useState } from "react";
import { Clapperboard, Copy, Film, Megaphone, Pencil, Play, Save } from "lucide-react";
import {
  api, fileUrl, type CuratedVoice, type ProductionState, type ShortScript, type ShortShot, type StockStatus, type StudioVoice,
} from "../api";
import { useT, type MessageKey } from "../i18n";
import { Modal, useApp } from "../components/ui";

const SOURCES = ["auto", "stock", "generate", "mix"] as const;
const MUSIC = ["none", "compose", "library"] as const;
const CAPTIONS = ["bold", "default", "horror", "none"] as const;
const ASPECTS = ["9:16", "16:9", "1:1"] as const;

/** "New short": a topic (the script is written for you) or your own script, and the few choices that matter. */
export function ShortModal({ onClose, onStarted }: { onClose: () => void; onStarted: (slug: string) => void }) {
  const { t, lang } = useT();
  const app = useApp();
  const [mode, setMode] = useState<"topic" | "script">("topic");
  const [topic, setTopic] = useState("");
  const [script, setScript] = useState("");
  const [name, setName] = useState("");
  const [language, setLanguage] = useState(lang === "es" ? "es" : "en");
  const [duration, setDuration] = useState(45);
  const [tone, setTone] = useState("");
  const [voice, setVoice] = useState("");
  const [curated, setCurated] = useState<CuratedVoice[]>([]);
  const [studio, setStudio] = useState<StudioVoice[]>([]);
  const [source, setSource] = useState<(typeof SOURCES)[number]>("auto");
  const [clips, setClips] = useState(0);
  const [music, setMusic] = useState<(typeof MUSIC)[number]>("compose");
  const [musicTags, setMusicTags] = useState("lo-fi ambient instrumental, soft pads, gentle beat");
  const [captions, setCaptions] = useState<(typeof CAPTIONS)[number]>("bold");
  const [aspects, setAspects] = useState<string[]>(["9:16"]);
  const [final, setFinal] = useState(false);
  const [review, setReview] = useState(false);
  const [count, setCount] = useState(1);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.voices().then((r) => setCurated(r.items)).catch(() => undefined);
    api.studioVoices().then((r) => setStudio(r.items)).catch(() => undefined);
  }, []);

  const voiceSpec = (): Record<string, unknown> | undefined => {
    if (!voice) return undefined;
    if (voice.startsWith("voice_")) return { voice_id: voice };
    return { backend: "piper", voice_id: voice };
  };

  const start = async () => {
    setBusy(true);
    try {
      const options: Record<string, unknown> = {
        language, duration_s: duration, ...(tone.trim() ? { tone: tone.trim() } : {}),
        ...(voiceSpec() ? { voice: voiceSpec() } : {}),
        visuals: { source, clips },
        music: music === "compose" ? { mode: "compose", tags: musicTags.trim() || undefined } : { mode: music },
        captions: { style: captions },
        timeline: { aspects, qualities: final ? ["preview", "final"] : ["preview"] },
      };
      const out = await api.createShort({
        name: name.trim() || undefined,
        ...(mode === "topic" ? { topic: topic.trim() } : { script: script.trim() }),
        options, settings: { script_review: review }, count, project: app.projectId || undefined,
      });
      const first = "items" in out ? out.items[0] : out;
      app.toast(t("shortQueued"), "ok");
      app.refreshJobs();
      onStarted(first.production.slug);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };
  const ready = (mode === "topic" ? topic.trim() : script.trim()) && aspects.length > 0;
  const toggleAspect = (a: string) => setAspects(aspects.includes(a) ? aspects.filter((x) => x !== a) : [...aspects, a]);

  return (
    <Modal title={t("shortModalTitle")} onClose={onClose} wide
      footer={<button className="btn primary" disabled={!ready || busy} onClick={start}><Play size={14} /> {t("startShort")}</button>}>
      <div className="stack">
        <p className="small muted" style={{ margin: 0 }}>{t("shortLead")}</p>
        <div className="segmented" style={{ alignSelf: "flex-start" }}>
          <button className={mode === "topic" ? "on" : ""} onClick={() => setMode("topic")}>{t("shortFromTopic")}</button>
          <button className={mode === "script" ? "on" : ""} onClick={() => setMode("script")}>{t("shortFromScript")}</button>
        </div>
        {mode === "topic" ? (
          <label className="field">{t("shortTopic")}
            <input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder={t("shortTopicHint")} autoFocus /></label>
        ) : (
          <label className="field">{t("shortScriptText")}
            <textarea rows={7} value={script} onChange={(e) => setScript(e.target.value)} /></label>
        )}
        <div className="grid-2">
          <label className="field">{t("shortName")}<input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="field">{t("shortTone")}<input value={tone} onChange={(e) => setTone(e.target.value)} /></label>
          <label className="field">{t("shortLanguage")}
            <select value={language} onChange={(e) => { setLanguage(e.target.value); setVoice(""); }}>
              <option value="es">Español</option><option value="en">English</option>
              <option value="fr">Français</option><option value="it">Italiano</option><option value="pt">Português</option>
              <option value="de">Deutsch</option>
            </select></label>
          <label className="field">{t("shortDuration")}
            <input type="number" min={10} max={180} step={5} value={duration} onChange={(e) => setDuration(Number(e.target.value) || 45)} /></label>
          <label className="field">{t("shortVoice")}
            <select value={voice} onChange={(e) => setVoice(e.target.value)}>
              <option value="">{t("shortVoiceDefault")}</option>
              {studio.map((v) => <option key={v.id} value={v.id}>{v.name} · {v.engine_id}</option>)}
              {curated.filter((v) => v.lang.startsWith(language)).map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
            </select></label>
          <label className="field">{t("shortPictures")}
            <select value={source} onChange={(e) => setSource(e.target.value as (typeof SOURCES)[number])}>
              {SOURCES.map((s) => <option key={s} value={s}>{t(`source_${s}` as MessageKey)}</option>)}
            </select></label>
          <label className="field">{t("shortMusic")}
            <select value={music} onChange={(e) => setMusic(e.target.value as (typeof MUSIC)[number])}>
              {MUSIC.map((m) => <option key={m} value={m}>{t(`music_${m}` as MessageKey)}</option>)}
            </select></label>
          {music === "compose" ? (
            <label className="field">{t("shortMusicTags")}<input value={musicTags} onChange={(e) => setMusicTags(e.target.value)} /></label>
          ) : <span />}
          <label className="field">{t("shortCaptions")}
            <select value={captions} onChange={(e) => setCaptions(e.target.value as (typeof CAPTIONS)[number])}>
              {CAPTIONS.map((c) => <option key={c} value={c}>{t(`caption_${c}` as MessageKey)}</option>)}
            </select></label>
          <label className="field">{t("shortClips")}
            <input type="number" min={0} max={20} value={clips} onChange={(e) => setClips(Math.max(0, Math.min(20, Number(e.target.value) || 0)))} /></label>
        </div>
        <div className="row wrap" style={{ gap: 14 }}>
          <span className="small muted">{t("shortAspects")}:</span>
          {ASPECTS.map((a) => (
            <label key={a} className="row small" style={{ gap: 5 }}>
              <input type="checkbox" checked={aspects.includes(a)} onChange={() => toggleAspect(a)} /> {a}
            </label>
          ))}
          <label className="row small" style={{ gap: 5 }}>
            <input type="checkbox" checked={final} onChange={(e) => setFinal(e.target.checked)} /> {t("shortFinal")}
          </label>
        </div>
        <div className="row wrap" style={{ gap: 14 }}>
          <label className="row small" style={{ gap: 5 }}>
            <input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} /> {t("shortScriptReview")}
          </label>
          <label className="row small" style={{ gap: 5 }}>{t("shortVariants")}
            <input type="number" min={1} max={8} value={count} style={{ width: 60 }}
              onChange={(e) => setCount(Math.max(1, Math.min(8, Number(e.target.value) || 1)))} />
          </label>
        </div>
      </div>
    </Modal>
  );
}

function fmt(s?: number) {
  return s == null ? "" : `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
}

/** A short's own cards: the script (and its review), the pictures, the renders and the publish text. */
export function ShortDetail({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [editing, setEditing] = useState(false);
  const script = (state.done?.script || state.spec.script) as ShortScript | undefined;
  const visuals = (state.done?.visuals || {}) as { plan?: ShortShot[]; items?: Record<string, { asset_id: string; source: string; kind: string; ref?: string }> };
  const clips = (state.done?.clips?.items || {}) as Record<string, string>;
  const animatic = state.done?.animatic as { renders?: Record<string, string>; plan?: { clips_planned: number; gpu_minutes: number } } | undefined;
  const renders = state.view.renders || {};
  const publish = state.view.publish;
  const pausedAtScript = state.status === "awaiting_review" && state.stage === "script";
  const cont = async () => {
    try { await api.continueProduction(state.slug); app.refreshJobs(); onChanged(); } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  return (
    <>
      {script && (
        <div className="card">
          <h2>
            <Megaphone size={16} /> {t("scriptTitle")}{script.title ? ` · ${script.title}` : ""}
            <div className="card-actions">
              {pausedAtScript && <button className="btn sm primary" onClick={cont}><Play size={13} /> {t("approveScript")}</button>}
              {state.status !== "running" && state.status !== "queued" && (
                <button className="btn sm" onClick={() => setEditing(true)}><Pencil size={13} /> {t("editScript")}</button>
              )}
            </div>
          </h2>
          {script.description && <p className="small muted" style={{ marginTop: -6 }}>{script.description}</p>}
          <table className="list small">
            <tbody>
              {script.segments.map((seg, i) => (
                <tr key={i}>
                  <td className="mono muted nowrap" style={{ width: 90 }}>{seg.start_s != null ? `${fmt(seg.start_s)}` : i + 1}</td>
                  <td>{seg.text}{seg.visual && <div className="muted" style={{ fontSize: 11.5 }}>{seg.visual}</div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {state.status === "awaiting_review" && animatic?.renders && (
        <div className="card">
          <h2><Film size={16} /> {t("animaticTitle")}
            <div className="card-actions"><button className="btn sm primary" onClick={cont}><Play size={13} /> {t("continueProduction")}</button></div>
          </h2>
          {animatic.plan && <p className="small">{t("animaticPlan", { cuts: (visuals.plan || []).length, clips: animatic.plan.clips_planned, gpu: animatic.plan.gpu_minutes })}</p>}
          <div className="row wrap">
            {Object.entries(animatic.renders).map(([aspect, id]) => (
              <div key={aspect} className="video-frame" style={{ width: aspect === "16:9" ? 380 : 220 }}>
                <video src={fileUrl(id)} controls preload="metadata" poster={`/api/assets/${id}/thumb`} />
              </div>
            ))}
          </div>
        </div>
      )}
      {Object.keys(renders).length > 0 && (
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
      {publish && (
        <div className="card">
          <h2>{t("publishTitle")}
            <div className="card-actions">
              <button className="btn sm" onClick={() => navigator.clipboard?.writeText(publish).then(() => app.toast(t("copied"), "ok"))}>
                <Copy size={13} /> {t("copyText")}</button>
            </div>
          </h2>
          <pre className="small" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{publish}</pre>
        </div>
      )}
      {(visuals.plan || []).length > 0 && (
        <div className="card">
          <h2>{t("picturesTitle")}</h2>
          <div className="thumb-grid">
            {(visuals.plan || []).map((shot) => {
              const item = visuals.items?.[shot.key];
              const shown = clips[shot.key] || item?.asset_id;
              return (
                <button key={shot.key} className="tile" title={item?.ref || shot.key} onClick={() => shown && app.openAsset(shown)}>
                  {shown ? <img src={`/api/assets/${shown}/thumb`} alt="" loading="lazy" /> : <div className="media-icon"><Clapperboard size={22} /></div>}
                  <div className="tile-badges">
                    <span className="pill badge-dark">{shot.key}</span>
                    {item && <span className="pill badge-dark">{t(item.source === "stock" ? "sourceStockBadge" : "sourceGeneratedBadge")}</span>}
                    {clips[shot.key] && <span className="pill badge-dark">{t("clipBadge")}</span>}
                  </div>
                  <div className="tile-meta"><span className="ellipsis grow">{fmt(shot.start_s)} · {shot.duration_s.toFixed(1)} s</span></div>
                </button>
              );
            })}
          </div>
        </div>
      )}
      {editing && script && <ScriptModal slug={state.slug} script={script} onClose={() => setEditing(false)}
        onSaved={() => { setEditing(false); onChanged(); }} />}
    </>
  );
}

function ScriptModal({ slug, script, onClose, onSaved }: { slug: string; script: ShortScript; onClose: () => void; onSaved: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [title, setTitle] = useState(script.title || "");
  const [text, setText] = useState(script.segments.map((s) => s.text).join("\n\n"));
  const save = async () => {
    const paragraphs = text.split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean);
    if (!paragraphs.length) return;
    // keep a segment's picture prompt and stock query when its text is unchanged
    const segments = paragraphs.map((p) => {
      const same = script.segments.find((s) => s.text === p);
      return same ? { text: p, visual: same.visual, query: same.query } : { text: p };
    });
    try {
      await api.shortScript(slug, { title: title.trim() || undefined, segments });
      app.toast(t("scriptSaved"), "ok");
      app.refreshJobs();
      onSaved();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };
  return (
    <Modal title={t("editScript")} onClose={onClose} wide footer={<button className="btn primary" onClick={save}><Save size={14} /> {t("saveScript")}</button>}>
      <div className="stack">
        <p className="small muted" style={{ margin: 0 }}>{t("scriptEditHint")}</p>
        <label className="field">{t("productionTitleField")}<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <textarea rows={14} value={text} onChange={(e) => setText(e.target.value)} />
      </div>
    </Modal>
  );
}

/** Settings card: the Pexels / Pixabay keys (never shown back, only their last four characters). */
export function StockKeysCard() {
  const { t } = useT();
  const app = useApp();
  const [status, setStatus] = useState<StockStatus | null>(null);
  const [keys, setKeys] = useState<Record<string, string>>({});
  useEffect(() => { api.stockStatus().then(setStatus).catch(() => undefined); }, []);
  const save = async (patch: Record<string, string>) => {
    try {
      setStatus(await api.setStockKeys(patch));
      setKeys({});
      app.toast(t("saved"), "ok");
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };
  return (
    <div className="card stack">
      <h2>{t("stockTitle")}</h2>
      <p className="small muted" style={{ marginTop: -6 }}>{t("stockLead")}</p>
      {status && Object.entries(status.providers).map(([p, info]) => (
        <label key={p} className="field">
          <span className="row" style={{ gap: 8 }}>
            <strong style={{ textTransform: "capitalize" }}>{p}</strong>
            {info.configured && <span className="pill ok">{t("stockConfigured")} {info.key_hint}</span>}
            <a className="small" href={info.get_key} target="_blank" rel="noreferrer">{t("stockGetKey")}</a>
          </span>
          <span className="row" style={{ gap: 6 }}>
            <input type="password" className="mono grow" autoComplete="off" value={keys[p] || ""}
              onChange={(e) => setKeys({ ...keys, [p]: e.target.value })} placeholder={info.configured ? "••••••••" : ""} />
            <button className="btn sm" disabled={!keys[p]?.trim()} onClick={() => save({ [p]: keys[p].trim() })}>{t("save")}</button>
            {info.configured && <button className="btn sm ghost" onClick={() => save({ [p]: "" })}>{t("stockClear")}</button>}
          </span>
        </label>
      ))}
    </div>
  );
}
