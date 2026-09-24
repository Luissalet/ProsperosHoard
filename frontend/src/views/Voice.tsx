import { useEffect, useRef, useState } from "react";
import {
  BookOpen, CheckCircle2, Download, Languages, Loader2, Mic, Mic2, Plus, RefreshCw, Save, Sparkles,
  Square, Trash2, UploadCloud, Video, Volume2, Wand2, XCircle,
} from "lucide-react";
import { api, type DubSegment, type EngineStatus, type StudioVoice, type VoiceSpec } from "../api";
import { useT } from "../i18n";
import { AssetPicker, ConfirmButton, Empty, JobState, Progress, useApp, useAsync, useSessionState, useTrackedJob } from "../components/ui";

type Tab = "engines" | "library" | "speak" | "transcribe" | "audiobook" | "dub";

function EngineRow({ e, onInstalled }: { e: EngineStatus; onInstalled: () => void }) {
  const { t } = useT();
  const app = useApp();
  // kept per engine for the session, so leaving the tab does not lose a running install
  const [jobId, setJobId] = useSessionState(`prospero.voice.install.${e.kind}.${e.id}`);
  const job = useTrackedJob(jobId);
  useEffect(() => {
    if (job && job.state === "done") { app.toast(`${e.label}: ${t("installed")}`, "ok"); onInstalled(); setJobId(null); }
    if (job && (job.state === "failed" || job.state === "cancelled")) { app.toast(job.message || t("installFailed"), "bad"); setJobId(null); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.state]);

  const install = async () => {
    try {
      const j = await api.installVoiceEngine(e.id, e.kind);
      setJobId(j.id);
      app.refreshJobs();
    } catch (err) {
      app.toast((err as Error).message, "bad");
    }
  };

  return (
    <div className="row" style={{ background: "var(--surface-2)", borderRadius: 8, padding: "8px 12px" }}>
      <span className={`dot ${e.installed ? "ok" : "bad"}`} />
      <span className="grow">
        <strong className="small">{e.label}</strong>{" "}
        {e.capabilities.cloning && <span className="pill">{t("cloningCapable")}</span>}
        {e.capabilities.needs_gpu && <span className="pill">{t("needsGpu")}</span>}
        <div className="muted small">{e.installed ? e.reason : e.install_hint || e.reason}</div>
      </span>
      {e.installed ? (
        <span className="pill ok"><CheckCircle2 size={13} /> {t("installed")}</span>
      ) : jobId ? (
        <span className="pill"><Loader2 size={13} className="spin" /> {t("installing")}</span>
      ) : e.install_hint ? (
        <button className="btn sm" onClick={install}><Download size={13} /> {t("installEngine")}</button>
      ) : null}
    </div>
  );
}

function EnginesTab() {
  const { t } = useT();
  const engines = useAsync(() => api.voiceEngines(), []);
  return (
    <div className="stack">
      <div className="row"><button className="btn sm" onClick={engines.reload}><RefreshCw size={13} /> {t("refresh")}</button></div>
      <div className="card stack">
        <h2><Volume2 size={16} /> {t("ttsEngines")}</h2>
        {(engines.data?.tts || []).map((e) => <EngineRow key={e.id} e={e} onInstalled={engines.reload} />)}
      </div>
      <div className="card stack">
        <h2><Mic size={16} /> {t("sttEngines")}</h2>
        {(engines.data?.stt || []).map((e) => <EngineRow key={e.id} e={e} onInstalled={engines.reload} />)}
      </div>
    </div>
  );
}

function VoiceSpecPicker({ spec, setSpec, engines, voices }: {
  spec: VoiceSpec; setSpec: (s: VoiceSpec) => void; engines: EngineStatus[]; voices: StudioVoice[];
}) {
  const { t } = useT();
  return (
    <div className="row wrap">
      <select value={spec.voice_id || ""} onChange={(e) => setSpec({ ...spec, voice_id: e.target.value || null, engine_id: e.target.value ? undefined : spec.engine_id })}>
        <option value="">{t("useEngineDirect")}</option>
        {voices.map((v) => <option key={v.id} value={v.id}>{v.name} ({v.engine_id})</option>)}
      </select>
      {!spec.voice_id && (
        <select value={spec.engine_id || ""} onChange={(e) => setSpec({ ...spec, engine_id: e.target.value || null })}>
          <option value="">{t("chooseVoice")}</option>
          {engines.filter((e) => e.installed).map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
        </select>
      )}
      <label className="field" style={{ width: 110 }}>{t("speedLabel")}
        <input type="number" step={0.1} min={0.5} max={2} placeholder="1.0" value={spec.speed ?? ""}
          onChange={(e) => setSpec({ ...spec, speed: e.target.value ? Number(e.target.value) : null })} />
      </label>
    </div>
  );
}

function LibraryTab() {
  const { t } = useT();
  const app = useApp();
  const voices = useAsync(() => api.studioVoices(), []);
  const engines = useAsync(() => api.voiceEngines(), []);
  const [name, setName] = useState("");
  const [engineId, setEngineId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [presetName, setPresetName] = useState<Record<string, string>>({});

  const cloningEngines = (engines.data?.tts || []).filter((e) => e.installed && e.capabilities.cloning);

  const upload = async () => {
    if (!file || !name.trim() || !engineId) return;
    setUploading(true);
    try {
      await api.uploadStudioVoice(file, name.trim(), engineId);
      app.toast(t("saved"), "ok");
      setName(""); setFile(null);
      voices.reload();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setUploading(false);
    }
  };

  const addPreset = async (id: string) => {
    const pname = (presetName[id] || "").trim();
    if (!pname) return;
    try {
      await api.addVoicePreset(id, { name: pname });
      setPresetName({ ...presetName, [id]: "" });
      voices.reload();
      app.toast(t("saved"), "ok");
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  return (
    <div className="stack">
      <div className="card stack">
        <h2><Plus size={16} /> {t("newVoice")}</h2>
        {cloningEngines.length === 0 && <p className="muted small">{t("noCloningEngines")}</p>}
        <div className="row wrap">
          <input placeholder={t("voiceName")} value={name} onChange={(e) => setName(e.target.value)} style={{ minWidth: 200 }} />
          <select value={engineId} onChange={(e) => setEngineId(e.target.value)}>
            <option value="">{t("voiceEngine")}</option>
            {cloningEngines.map((e) => <option key={e.id} value={e.id}>{e.label}</option>)}
          </select>
          <label className="btn sm">
            <UploadCloud size={13} /> {file ? file.name : t("chooseFile")}
            <input type="file" accept="audio/*" hidden onChange={(e) => setFile(e.target.files?.[0] || null)} />
          </label>
          <button className="btn primary sm" onClick={upload} disabled={uploading || !file || !name.trim() || !engineId}>
            {uploading ? <Loader2 size={13} className="spin" /> : <Save size={13} />} {t("create")}
          </button>
        </div>
      </div>

      {(voices.data?.items.length || 0) === 0 ? <Empty icon={<Mic2 size={34} />} text={t("noVoicesYet")} /> : (
        <div className="stack">
          {(voices.data?.items || []).map((v) => (
            <div key={v.id} className="card stack">
              <div className="row">
                <strong className="grow">{v.name}</strong>
                <span className="pill mono">{v.engine_id}</span>
                <span className={`pill ${v.quality.ok ? "ok" : "warn"}`}>{v.quality.ok ? t("qualityOk") : t("qualityWarn")}</span>
                <ConfirmButton label={t("deleteVoice")} onConfirm={async () => {
                  try { await api.deleteStudioVoice(v.id); voices.reload(); } catch (err) { app.toast((err as Error).message, "bad"); }
                }}><Trash2 size={13} /></ConfirmButton>
              </div>
              <div className="row small muted wrap">
                <span>{t("duration")}: {v.quality.duration_s}s</span>
                <span>{t("snr")}: {v.quality.snr_db} dB</span>
                <span>{t("clipping")}: {v.quality.clipping_pct}%</span>
                {v.has_sample && <audio src={api.voiceSampleUrl(v.id)} controls preload="none" style={{ height: 28, width: 200 }} />}
              </div>
              {v.quality.warnings.length > 0 && <div className="muted small">{v.quality.warnings.join(" · ")}</div>}
              <div className="row wrap small">
                {v.presets.map((p) => <span key={p} className="pill">{p}</span>)}
                <input placeholder={t("presetName")} value={presetName[v.id] || ""} style={{ width: 130 }}
                  onChange={(e) => setPresetName({ ...presetName, [v.id]: e.target.value })} />
                <button className="btn sm" onClick={() => addPreset(v.id)}><Plus size={12} /> {t("addPreset")}</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SpeakTab() {
  const { t } = useT();
  const app = useApp();
  const engines = useAsync(() => api.voiceEngines(), []);
  const voices = useAsync(() => api.studioVoices(), []);
  const [text, setText] = useState("");
  const [spec, setSpec] = useState<VoiceSpec>({});
  const [speaking, setSpeaking] = useState(false);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  // each take replaces the last one: free the old blob (and the last one on unmount)
  const urlRef = useRef<string | null>(null);
  const showAudio = (url: string | null) => {
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = url;
    setAudioUrl(url);
  };
  useEffect(() => () => { if (urlRef.current) URL.revokeObjectURL(urlRef.current); }, []);

  const speak = async () => {
    if (!text.trim() || (!spec.engine_id && !spec.voice_id)) return;
    setSpeaking(true);
    try {
      const result = await api.speak(text.trim(), spec, app.projectId || undefined);
      if (result instanceof Blob) showAudio(URL.createObjectURL(result));
      else app.toast(t("saved"), "ok");
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setSpeaking(false);
    }
  };

  return (
    <div className="card stack">
      <textarea rows={4} placeholder={t("speakPlaceholder")} value={text} onChange={(e) => setText(e.target.value)} />
      <VoiceSpecPicker spec={spec} setSpec={setSpec} engines={engines.data?.tts || []} voices={voices.data?.items || []} />
      <div className="row">
        <button className="btn primary" onClick={speak} disabled={speaking || !text.trim() || (!spec.engine_id && !spec.voice_id)}>
          {speaking ? <Loader2 size={14} className="spin" /> : <Wand2 size={14} />} {t("voiceSpeakBtn")}
        </button>
        {app.projectId && <span className="muted small">{t("savesToProject")}</span>}
      </div>
      {audioUrl && <audio src={audioUrl} controls autoPlay style={{ width: "100%" }} />}
    </div>
  );
}

function TranscribeTab() {
  const { t } = useT();
  const app = useApp();
  const [result, setResult] = useState<{ text: string; srt: string; vtt: string; txt: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState(false);
  const [dictated, setDictated] = useState("");
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunks = useRef<Blob[]>([]);
  const mounted = useRef(true);
  // leaving the tab mid-recording turns the microphone off (and sends nothing)
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const rec = recorder.current;
      if (rec && rec.state !== "inactive") { rec.onstop = null; rec.ondataavailable = null; rec.stop(); }
      stream.current?.getTracks().forEach((tr) => tr.stop());
      recorder.current = null;
      stream.current = null;
    };
  }, []);

  const upload = async (file: File) => {
    setBusy(true);
    try {
      const r = await api.transcribeUpload(file);
      setResult(r);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const download = (name: string, content: string) => {
    const a = document.createElement("a");
    a.href = `data:text/plain;charset=utf-8,${encodeURIComponent(content)}`;
    a.download = name;
    a.click();
  };

  const recordingSupported = typeof window !== "undefined" && typeof window.MediaRecorder !== "undefined"
    && !!navigator.mediaDevices?.getUserMedia;

  const startRecording = async () => {
    if (!recordingSupported) { app.toast(t("recordingUnsupported"), "bad"); return; }
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mounted.current) { media.getTracks().forEach((tr) => tr.stop()); return; }
      stream.current = media;
      chunks.current = [];
      const rec = new MediaRecorder(media);
      rec.ondataavailable = (e) => { if (e.data.size) chunks.current.push(e.data); };
      rec.onstop = async () => {
        media.getTracks().forEach((tr) => tr.stop());
        stream.current = null;
        const blob = new Blob(chunks.current, { type: "audio/webm" });
        setBusy(true);
        try {
          const r = await api.dictate(blob);
          setDictated(r.text);
        } catch (e) {
          app.toast((e as Error).message, "bad");
        } finally {
          setBusy(false);
        }
      };
      recorder.current = rec;
      rec.start();
      setRecording(true);
    } catch {
      app.toast(t("micDenied"), "bad");
    }
  };
  const stopRecording = () => { recorder.current?.stop(); setRecording(false); };

  return (
    <div className="stack">
      <div className="card stack">
        <h2><UploadCloud size={16} /> {t("transcribeUpload")}</h2>
        <label className="btn sm" style={{ width: "fit-content" }}>
          <UploadCloud size={13} /> {t("chooseFile")}
          <input type="file" accept="audio/*,video/*" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); }} />
        </label>
        {busy && <Loader2 size={14} className="spin" />}
        {result && (
          <div className="stack">
            <textarea readOnly rows={4} value={result.text} />
            <div className="row">
              <button className="btn sm" onClick={() => download("transcript.srt", result.srt)}><Download size={13} /> SRT</button>
              <button className="btn sm" onClick={() => download("transcript.vtt", result.vtt)}><Download size={13} /> VTT</button>
              <button className="btn sm" onClick={() => download("transcript.txt", result.txt)}><Download size={13} /> TXT</button>
            </div>
          </div>
        )}
      </div>
      <div className="card stack">
        <h2><Mic size={16} /> {t("dictateTitle")}</h2>
        {recordingSupported ? (
          <div className="row">
            {!recording ? (
              <button className="btn primary" onClick={startRecording}><Mic size={14} /> {t("recordButton")}</button>
            ) : (
              <button className="btn danger" onClick={stopRecording}><Square size={14} /> {t("stopRecording")}</button>
            )}
          </div>
        ) : (
          <p className="muted small">{t("recordingUnsupported")}</p>
        )}
        {dictated && <textarea readOnly rows={3} value={dictated} />}
      </div>
    </div>
  );
}

function AudiobookTab() {
  const { t } = useT();
  const app = useApp();
  const engines = useAsync(() => api.voiceEngines(), []);
  const voices = useAsync(() => api.studioVoices(), []);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [format, setFormat] = useState<"mp3" | "m4b">("mp3");
  const [spec, setSpec] = useState<VoiceSpec>({});
  // kept for the session: switching tabs or sections does not lose a running audiobook
  const [jobId, setJobId] = useSessionState("prospero.voice.audiobookJob");
  const [busy, setBusy] = useState(false);
  const job = useTrackedJob(jobId);

  const loadFile = (file: File) => {
    if (!/\.(txt|md)$/i.test(file.name)) { app.toast(t("audiobookFileHint"), "info"); return; }
    file.text().then(setText);
  };

  const submit = async () => {
    if (!text.trim() || (!spec.engine_id && !spec.voice_id)) return;
    setBusy(true);
    try {
      const r = await api.audiobook({ text: text.trim(), title: title || undefined, voice: spec, format, project: app.projectId || undefined, wait_s: 0 });
      setJobId(r.job.id);
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const outputs = (job?.outputs || {}) as { chapters?: { index: number; title: string; duration_s: number }[]; final_file?: string; srt_file?: string };

  return (
    <div className="card stack">
      <div className="row wrap">
        <input placeholder={t("name")} value={title} onChange={(e) => setTitle(e.target.value)} style={{ minWidth: 200 }} />
        <select value={format} onChange={(e) => setFormat(e.target.value as "mp3" | "m4b")}>
          <option value="mp3">MP3</option>
          <option value="m4b">M4B ({t("audiobookChapters")})</option>
        </select>
        <label className="btn sm">
          <UploadCloud size={13} /> {t("audiobookFile")}
          <input type="file" accept=".txt,.md" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) loadFile(f); }} />
        </label>
      </div>
      <textarea rows={6} placeholder={t("audiobookText")} value={text} onChange={(e) => setText(e.target.value)} />
      <VoiceSpecPicker spec={spec} setSpec={setSpec} engines={engines.data?.tts || []} voices={voices.data?.items || []} />
      <div className="row">
        <button className="btn primary" onClick={submit} disabled={busy || !text.trim() || (!spec.engine_id && !spec.voice_id)}>
          {busy ? <Loader2 size={14} className="spin" /> : <BookOpen size={14} />} {t("create")}
        </button>
      </div>
      {job && (
        <div className="stack">
          <div className="row"><JobState state={job.state} /><span className="small muted">{job.message}</span></div>
          {["queued", "waiting_gpu", "running"].includes(job.state) && <Progress value={job.progress} />}
          {job.state === "done" && (
            <div className="stack">
              <div className="row wrap">
                {(outputs.chapters || []).map((c) => <span key={c.index} className="pill">{c.title} · {c.duration_s.toFixed(0)}s</span>)}
              </div>
              <div className="row">
                <a className="btn sm" href={api.audiobookDownloadUrl(job.id, "final")}><Download size={13} /> {t("download")}</a>
                {outputs.srt_file && <a className="btn sm" href={api.audiobookDownloadUrl(job.id, "srt")}><Download size={13} /> SRT</a>}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

type DubEdits = { jobId: string | null; v: number; segments: Record<number, DubSegment> };

function parseDubEdits(raw: string | null, jobId: string | null): DubEdits {
  try {
    const parsed = raw ? (JSON.parse(raw) as DubEdits) : null;
    if (parsed && parsed.jobId === jobId && parsed.segments) return parsed;
  } catch { /* a corrupt entry is just ignored */ }
  return { jobId, v: 0, segments: {} };
}

function DubTab() {
  const { t } = useT();
  const app = useApp();
  const engines = useAsync(() => api.voiceEngines(), []);
  const voices = useAsync(() => api.studioVoices(), []);
  const projects = useAsync(() => api.projects(), []);
  const [dubProjectId, setDubProjectId] = useState<string>(app.projectId || "");
  const [videoAsset, setVideoAsset] = useState<{ id: string; name: string | null } | null>(null);
  const [picking, setPicking] = useState(false);
  const [targetLanguage, setTargetLanguage] = useState("en");
  const [sourceLanguage, setSourceLanguage] = useState("");
  const [spec, setSpec] = useState<VoiceSpec>({});
  // kept for the session: switching tabs or sections does not lose a running dub
  const [jobId, setJobIdRaw] = useSessionState("prospero.voice.dubJob");
  const [busy, setBusy] = useState(false);
  const job = useTrackedJob(jobId);
  const [fixIndex, setFixIndex] = useState<number | null>(null);
  const [fixText, setFixText] = useState("");
  // Re-synthesized segments: the job's outputs keep the first run's text, so the
  // server's answer is kept here (per job), and `v` busts the cached video.
  const [editsRaw, setEditsRaw] = useSessionState("prospero.voice.dubEdits");
  const edits = parseDubEdits(editsRaw, jobId);
  const setJobId = (id: string | null) => { setJobIdRaw(id); setEditsRaw(null); };

  const submit = async () => {
    if (!videoAsset || !targetLanguage.trim() || (!spec.engine_id && !spec.voice_id)) return;
    setBusy(true);
    try {
      const r = await api.dub({
        video_asset_id: videoAsset.id, target_language: targetLanguage.trim(), source_language: sourceLanguage || undefined,
        voice: spec, project: dubProjectId || undefined, wait_s: 0,
      });
      setJobId(r.job.id);
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const outputs = (job?.outputs || {}) as { final_video?: string; subtitles?: string; segments?: DubSegment[] };

  const resynth = async (index: number) => {
    if (!job) return;
    try {
      const r = await api.resynthesizeDubSegment(job.id, index, { text: fixText || undefined });
      const next: DubEdits = { jobId: job.id, v: Date.now(), segments: { ...edits.segments, [index]: r.segment } };
      setEditsRaw(JSON.stringify(next));
      app.toast(t("saved"), "ok");
      setFixIndex(null);
      setFixText("");
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };
  const segments = (outputs.segments || []).map((s) => edits.segments[s.index] || s);
  const bust = edits.v ? `&v=${edits.v}` : "";

  return (
    <div className="card stack">
      <div className="row wrap">
        <select value={dubProjectId} onChange={(e) => setDubProjectId(e.target.value)}>
          <option value="">{t("noProject")}</option>
          {(projects.data?.items || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button className="btn sm" onClick={() => setPicking(true)} disabled={!dubProjectId}>
          <Video size={13} /> {videoAsset ? videoAsset.name || videoAsset.id : t("dubVideoAsset")}
        </button>
        <input placeholder={t("targetLanguage")} value={targetLanguage} onChange={(e) => setTargetLanguage(e.target.value)} style={{ width: 90 }} />
        <input placeholder={t("sourceLanguage")} value={sourceLanguage} onChange={(e) => setSourceLanguage(e.target.value)} style={{ width: 90 }} />
      </div>
      <VoiceSpecPicker spec={spec} setSpec={setSpec} engines={engines.data?.tts || []} voices={voices.data?.items || []} />
      <div className="row">
        <button className="btn primary" onClick={submit} disabled={busy || !videoAsset || !targetLanguage.trim() || (!spec.engine_id && !spec.voice_id)}>
          {busy ? <Loader2 size={14} className="spin" /> : <Languages size={14} />} {t("create")}
        </button>
      </div>
      {job && (
        <div className="stack">
          <div className="row"><JobState state={job.state} /><span className="small muted">{job.message}</span></div>
          {["queued", "waiting_gpu", "running"].includes(job.state) && <Progress value={job.progress} />}
          {job.state === "failed" && <div className="row"><XCircle size={14} className="err-text" /><span className="small err-text">{job.message}</span></div>}
          {(outputs.segments?.length || 0) > 0 && (
            <div className="card table-scroll" style={{ padding: 6 }}>
              <table className="list">
                <thead><tr><th>#</th><th>{t("sourceText")}</th><th>{t("translatedText")}</th><th /></tr></thead>
                <tbody>
                  {segments.map((s) => (
                    <tr key={s.index}>
                      <td className="mono small">{s.index}</td>
                      <td className="small">{s.source_text}</td>
                      <td className="small">
                        {fixIndex === s.index
                          ? <input value={fixText} onChange={(e) => setFixText(e.target.value)} style={{ width: "100%" }} />
                          : s.translated_text}
                      </td>
                      <td>
                        {fixIndex === s.index
                          ? <button className="btn sm" onClick={() => resynth(s.index)} aria-label={t("saveSegment")} title={t("saveSegment")}><Save size={12} /></button>
                          : <button className="btn sm ghost" onClick={() => { setFixIndex(s.index); setFixText(s.translated_text); }}><Sparkles size={12} /> {t("dubResynth")}</button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {job.state === "done" && outputs.final_video && (
            <div className="stack">
              <video key={bust} src={`${api.dubDownloadUrl(job.id, "video")}${bust}`} controls style={{ width: "100%", maxHeight: 360 }} />
              <div className="row">
                <a className="btn sm" href={`${api.dubDownloadUrl(job.id, "video")}${bust}`}><Download size={13} /> {t("dubFinalVideo")}</a>
                {outputs.subtitles && <a className="btn sm" href={`${api.dubDownloadUrl(job.id, "subtitles")}${bust}`}><Download size={13} /> {t("dubSubtitles")}</a>}
              </div>
            </div>
          )}
        </div>
      )}
      {picking && dubProjectId && (
        <AssetPicker projectId={dubProjectId} kind="video" title={t("dubVideoAsset")}
          onPick={(a) => { setVideoAsset({ id: a.id, name: a.name }); setPicking(false); }} onClose={() => setPicking(false)} />
      )}
    </div>
  );
}

export function VoiceView() {
  const { t } = useT();
  const [tab, setTab] = useState<Tab>("engines");
  return (
    <>
      <div className="page-head">
        <div><h1><Mic2 size={20} /> {t("voiceTitle")}</h1><p>{t("voiceLead")}</p></div>
        <div className="actions">
          <div className="segmented">
            <button className={tab === "engines" ? "on" : ""} onClick={() => setTab("engines")}>{t("voiceTabEngines")}</button>
            <button className={tab === "library" ? "on" : ""} onClick={() => setTab("library")}>{t("voiceTabLibrary")}</button>
            <button className={tab === "speak" ? "on" : ""} onClick={() => setTab("speak")}>{t("voiceTabSpeak")}</button>
            <button className={tab === "transcribe" ? "on" : ""} onClick={() => setTab("transcribe")}>{t("voiceTabTranscribe")}</button>
            <button className={tab === "audiobook" ? "on" : ""} onClick={() => setTab("audiobook")}>{t("voiceTabAudiobook")}</button>
            <button className={tab === "dub" ? "on" : ""} onClick={() => setTab("dub")}>{t("voiceTabDub")}</button>
          </div>
        </div>
      </div>
      {tab === "engines" && <EnginesTab />}
      {tab === "library" && <LibraryTab />}
      {tab === "speak" && <SpeakTab />}
      {tab === "transcribe" && <TranscribeTab />}
      {tab === "audiobook" && <AudiobookTab />}
      {tab === "dub" && <DubTab />}
    </>
  );
}
