import { useEffect, useRef, useState } from "react";
import { Loader2, Plus, Sparkles, Trash2, Wand2 } from "lucide-react";
import { api, thumbUrl, type Asset, type Character, type Project, type VideoDraft, type VideoShot } from "../api";
import { useT } from "../i18n";
import { Modal, useApp } from "../components/ui";

const LANGS: [string, string][] = [["en", "English"], ["es", "Español"], ["fr", "Français"], ["it", "Italiano"],
  ["pt", "Português"], ["de", "Deutsch"], ["ja", "日本語"], ["ko", "한국어"]];
const SECTIONS = ["", "intro", "verse", "prechorus", "chorus", "bridge", "breakdown", "outro"];
const blankShot = (): VideoShot => ({ prompt: "", lead: true, motion: "move", motion_prompt: "subtle motion", section: "" });

// New music video, in the app: concept + lead + song -> a shot list the local
// model drafts (or you write) -> edit it -> the production runs (stills,
// animatic for review, Wan clips, the cut on the beat).
export function VideoModal({ onClose, onStarted, projectId: forced }: { onClose: () => void; onStarted: (slug: string) => void; projectId?: string }) {
  const { t } = useT();
  const app = useApp();
  const [chars, setChars] = useState<(Character & { projectName: string })[]>([]);
  const [charId, setCharId] = useState("");
  const [newName, setNewName] = useState("");
  const [newLook, setNewLook] = useState("");
  const [name, setName] = useState("");
  const [concept, setConcept] = useState("");
  const [songMode, setSongMode] = useState<"compose" | "asset">("compose");
  const [genre, setGenre] = useState("");
  const [language, setLanguage] = useState("en");
  const [duration, setDuration] = useState(120);
  const [songs, setSongs] = useState<Asset[]>([]);
  const [songId, setSongId] = useState("");
  const [lyrics, setLyrics] = useState("");
  const [shots, setShots] = useState(10);
  const [clips, setClips] = useState<"all" | "lead" | "none">("all");
  const [aspects, setAspects] = useState<string[]>(["16:9"]);
  const [takes, setTakes] = useState(2);
  const [autopilot, setAutopilot] = useState(false);
  const [draft, setDraft] = useState<VideoDraft | null>(null);
  const [busy, setBusy] = useState<"" | "plan" | "create">("");
  // how long the plan has been writing, and the way to stop waiting for it
  const [elapsed, setElapsed] = useState(0);
  const planAbort = useRef<AbortController | null>(null);
  useEffect(() => {
    if (busy !== "plan") { setElapsed(0); return; }
    const started = Date.now();
    const h = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(h);
  }, [busy]);

  useEffect(() => {
    api.projects().then(async (r) => {
      const lists = await Promise.all(r.items.map((p: Project) => api.characters(p.id)
        .then((c) => c.items.map((x) => ({ ...x, projectName: p.name }))).catch(() => [])));
      const all = lists.flat();
      setChars(all);
      const home = forced || app.projectId;
      const here = all.find((c) => c.project_id === home && c.canonical_asset_id) || all.find((c) => c.project_id === home);
      if (here) setCharId(here.id);
    }).catch((e) => app.toast((e as Error).message, "bad"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const lead = chars.find((c) => c.id === charId);
  // made from a project's page, the video lives in that project (its lead can come from any)
  const projectId = forced || lead?.project_id || app.projectId || "";
  useEffect(() => {
    if (!projectId) return;
    api.assets(projectId, { kind: "audio", limit: 60 }).then((r) => setSongs(r.items)).catch(() => setSongs([]));
  }, [projectId]);

  const leadBody = () => (charId ? { character_id: charId } : { lead_name: newName, lead_look: newLook });
  const plan = async () => {
    setBusy("plan");
    const ctl = new AbortController();
    planAbort.current = ctl;
    try {
      const r = await api.planVideo({
        concept, ...leadBody(), shots, language, genre: genre || null, duration_s: duration,
        song_asset_id: songMode === "asset" ? songId || null : null, lyrics: songMode === "asset" ? lyrics || null : null,
      }, ctl.signal);
      setDraft(r.draft);
      if (!name && r.draft.title) setName(r.draft.title);
    } catch (e) {
      if (!ctl.signal.aborted) app.toast((e as Error).message, "bad");
    } finally {
      planAbort.current = null;
      setBusy("");
    }
  };
  const writeMyself = () => setDraft({
    title: name, world_look: "", world_negative: "", shots: Array.from({ length: shots }, blankShot),
    ...(songMode === "compose" ? { song: { tags: genre, lyrics: "", bpm: 120, key: "C major", language, duration } } : {}),
  });
  const create = async () => {
    if (!draft) return;
    setBusy("create");
    try {
      const r = await api.videoFromPlan({
        name: name.trim() || draft.title || [lead?.name || newName.trim(), concept.trim().split(/[,.;\n]/)[0].slice(0, 40)].filter(Boolean).join(" - ") || "Music video", draft, ...leadBody(), clips, aspects, song_takes: takes,
        song_asset_id: songMode === "asset" ? songId || null : null, project: projectId || null, brief: concept || null,
        lyrics: songMode === "asset" ? lyrics || null : null,
        ...(autopilot ? { settings: { animatic_autocontinue: true, song_review: false } } : {}),
      });
      app.toast(t("videoStarted"), "ok");
      onStarted(r.production.slug);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy("");
    }
  };
  const setShot = (i: number, patch: Partial<VideoShot>) => draft && setDraft({ ...draft, shots: draft.shots.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  const leadOk = !!charId || (newName.trim() && newLook.trim());
  // what the run will cost on this machine, before anything starts (the animatic's measured rates)
  const nShots = draft ? draft.shots.length : shots;
  const nClips = clips === "none" ? 0 : draft ? draft.shots.filter((s) => s.motion === "move" && (clips === "all" || s.lead)).length
    : clips === "all" ? shots : Math.ceil(shots * 0.7);
  const gpuMin = Math.round(nShots * 1.19 + nClips * 9.5 + (songMode === "compose" ? takes * 0.7 : 0) + aspects.length * 1.75 * 2);
  const estimate = t("videoEstimate", { stills: nShots, clips: nClips, h: Math.floor(gpuMin / 60), m: gpuMin % 60 });
  const songOk = songMode === "asset" ? !!songId : true;

  return (
    <Modal title={t("newVideo")} onClose={onClose} wide footer={
      !draft ? (
        <>
          {busy === "plan" && elapsed >= 45 && <span className="hint grow" style={{ maxWidth: 360 }}>{t("videoPlanSlow")}</span>}
          {busy === "plan"
            ? <button className="btn ghost" onClick={() => planAbort.current?.abort()}>{t("videoPlanStop")}</button>
            : <button className="btn ghost" onClick={onClose}>{t("cancel")}</button>}
          <button className="btn" disabled={!leadOk || !songOk} onClick={() => { planAbort.current?.abort(); writeMyself(); }}>{t("videoWriteMyself")}</button>
          <button className="btn primary" disabled={!leadOk || !songOk || !concept.trim() || busy !== ""} onClick={plan}>
            {busy === "plan" ? <Loader2 size={15} className="spin" /> : <Sparkles size={15} />} {busy === "plan"
              ? `${t("videoPlanning")} ${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, "0")}` : t("videoPlan")}</button>
        </>
      ) : (
        <>
          <span className="hint grow">{estimate}</span>
          <label className="check" title={t("autopilotHint")}><input type="checkbox" checked={autopilot} onChange={(e) => setAutopilot(e.target.checked)} /> {t("autopilot")}</label>
          <button className="btn ghost" onClick={() => setDraft(null)}>{t("back")}</button>
          <button className="btn primary" disabled={busy !== "" || !draft.shots.some((s) => s.prompt.trim())} onClick={create}>
            {busy === "create" ? <Loader2 size={15} className="spin" /> : <Wand2 size={15} />} {t("videoCreate")}</button>
        </>
      )}>
      {!draft ? (
        <div className="stack">
          <label className="field">{t("videoName")}<input value={name} onChange={(e) => setName(e.target.value)} /></label>
          <div className="field">{t("videoLead")}
            <div className="row wrap" style={{ gap: 8 }}>
              <select value={charId} onChange={(e) => setCharId(e.target.value)} style={{ minWidth: 260 }}>
                <option value="">{t("videoNewLead")}</option>
                {chars.map((c) => <option key={c.id} value={c.id}>{c.name} · {c.projectName}{c.canonical_asset_id ? "" : ` (${t("videoNoCanonical")})`}</option>)}
              </select>
              {lead?.canonical_asset_id && <img src={thumbUrl({ id: lead.canonical_asset_id, thumb_path: "x", kind: "image" })} alt="" style={{ width: 44, height: 44, objectFit: "cover", borderRadius: 8 }} />}
            </div>
            {lead && !lead.canonical_asset_id && <span className="hint err-text">{t("videoNoCanonicalHint")}</span>}
          </div>
          {!charId && (
            <div className="grid-2">
              <label className="field">{t("name")}<input value={newName} onChange={(e) => setNewName(e.target.value)} /></label>
              <label className="field">{t("videoLook")}<input value={newLook} onChange={(e) => setNewLook(e.target.value)} /></label>
            </div>
          )}
          <label className="field">{t("videoConcept")}
            <textarea rows={3} value={concept} onChange={(e) => setConcept(e.target.value)} placeholder={t("videoConceptHint")} /></label>
          <div className="field">{t("videoSong")}
            <div className="segmented">
              <button className={songMode === "compose" ? "on" : ""} onClick={() => setSongMode("compose")}>{t("videoSongCompose")}</button>
              <button className={songMode === "asset" ? "on" : ""} onClick={() => setSongMode("asset")}>{t("videoSongExisting")}</button>
            </div>
          </div>
          {songMode === "compose" ? (
            <div className="grid-3">
              <label className="field">{t("videoGenre")}<input value={genre} onChange={(e) => setGenre(e.target.value)} placeholder="80s disco-funk, falsetto, slap bass" /></label>
              <label className="field">{t("videoLyricsLanguage")}
                <select value={language} onChange={(e) => setLanguage(e.target.value)}>{LANGS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
              <label className="field">{t("videoDuration")} <span className="mono">{duration} s</span>
                <input type="range" min={30} max={240} step={10} value={duration} onChange={(e) => setDuration(Number(e.target.value))} /></label>
            </div>
          ) : (
            <div className="grid-2">
              <label className="field">{t("videoPickSong")}
                <select value={songId} onChange={(e) => setSongId(e.target.value)}>
                  <option value="">—</option>
                  {songs.map((a) => <option key={a.id} value={a.id}>{a.name || a.id}{a.duration_s ? ` · ${Math.round(a.duration_s)} s` : ""}</option>)}
                </select>
                {songs.length === 0 && <span className="hint">{t("videoNoSongs")}</span>}
              </label>
              <label className="field">{t("videoLyricsOptional")}<textarea rows={3} value={lyrics} onChange={(e) => setLyrics(e.target.value)} /></label>
            </div>
          )}
          <div className="grid-3">
            <label className="field">{t("videoShots")} <span className="mono">{shots}</span>
              <input type="range" min={3} max={24} value={shots} onChange={(e) => setShots(Number(e.target.value))} /></label>
            <label className="field">{t("videoClips")}
              <select value={clips} onChange={(e) => setClips(e.target.value as "all" | "lead" | "none")}>
                <option value="all">{t("videoClipsAll")}</option><option value="lead">{t("videoClipsLead")}</option>
                <option value="none">{t("videoClipsNone")}</option></select></label>
            <div className="field">{t("videoAspects")}
              <div className="row" style={{ gap: 10 }}>
                {["16:9", "9:16", "1:1"].map((a) => (
                  <label key={a} className="check"><input type="checkbox" checked={aspects.includes(a)}
                    onChange={(e) => setAspects(e.target.checked ? [...aspects, a] : aspects.filter((x) => x !== a).length ? aspects.filter((x) => x !== a) : aspects)} /> {a}</label>
                ))}
              </div>
            </div>
          </div>
          {songMode === "compose" && (
            <label className="field">{t("videoTakes")} <span className="mono">{takes}</span>
              <input type="range" min={1} max={4} value={takes} onChange={(e) => setTakes(Number(e.target.value))} /></label>
          )}
          <p className="muted small">{t("videoHowItRuns")}</p>
          <p className="small"><strong>{estimate}</strong></p>
        </div>
      ) : (
        <div className="stack">
          {(draft.warnings || []).map((w) => <p key={w} className="err-text small">{w}</p>)}
          <div className="grid-2">
            <label className="field">{t("videoName")}<input value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field">{t("videoWorldLook")}<input value={draft.world_look} onChange={(e) => setDraft({ ...draft, world_look: e.target.value })} /></label>
          </div>
          <label className="field">{t("videoWorldNegative")}<input value={draft.world_negative} onChange={(e) => setDraft({ ...draft, world_negative: e.target.value })} /></label>
          {draft.song && (
            <div className="card stack" style={{ gap: 8 }}>
              <div className="grid-3">
                <label className="field">{t("videoSongTags")}<input value={draft.song.tags} onChange={(e) => setDraft({ ...draft, song: { ...draft.song!, tags: e.target.value } })} /></label>
                <label className="field">BPM<input type="number" min={40} max={220} value={draft.song.bpm} onChange={(e) => setDraft({ ...draft, song: { ...draft.song!, bpm: Number(e.target.value) } })} /></label>
                <label className="field">{t("videoKey")}<input value={draft.song.key} onChange={(e) => setDraft({ ...draft, song: { ...draft.song!, key: e.target.value } })} /></label>
              </div>
              <label className="field">{t("videoLyrics")} ({LANGS.find(([k]) => k === draft.song!.language)?.[1] || draft.song.language})
                <textarea rows={8} className="mono" value={draft.song.lyrics} onChange={(e) => setDraft({ ...draft, song: { ...draft.song!, lyrics: e.target.value } })} /></label>
            </div>
          )}
          <div className="stack" style={{ gap: 8 }}>
            {draft.shots.map((s, i) => (
              <div key={i} className="card" style={{ padding: 10 }}>
                <div className="row wrap" style={{ gap: 8, marginBottom: 6 }}>
                  <strong className="mono">#{i + 1}</strong>
                  <label className="check"><input type="checkbox" checked={s.lead} onChange={(e) => setShot(i, { lead: e.target.checked })} /> {t("videoShotLead")}</label>
                  <select value={s.motion} onChange={(e) => setShot(i, { motion: e.target.value as "move" | "still" })}>
                    <option value="move">{t("videoMove")}</option><option value="still">{t("videoStill")}</option></select>
                  <select value={s.section} onChange={(e) => setShot(i, { section: e.target.value })}>
                    {SECTIONS.map((x) => <option key={x} value={x}>{x || t("videoAnySection")}</option>)}</select>
                  <div className="grow" />
                  <button className="btn sm icon ghost" onClick={() => setDraft({ ...draft, shots: draft.shots.filter((_, j) => j !== i) })} title={t("deleteAsset")}><Trash2 size={13} /></button>
                </div>
                <textarea rows={2} value={s.prompt} placeholder={t("videoShotPrompt")} onChange={(e) => setShot(i, { prompt: e.target.value })} style={{ width: "100%" }} />
                {s.motion === "move" && <input value={s.motion_prompt} placeholder={t("videoMotionPrompt")} onChange={(e) => setShot(i, { motion_prompt: e.target.value })} style={{ width: "100%", marginTop: 6 }} />}
              </div>
            ))}
            <button className="btn sm" onClick={() => setDraft({ ...draft, shots: [...draft.shots, blankShot()] })}><Plus size={13} /> {t("videoAddShot")}</button>
          </div>
        </div>
      )}
    </Modal>
  );
}
