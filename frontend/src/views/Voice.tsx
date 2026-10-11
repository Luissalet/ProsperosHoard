import { Fragment, useEffect, useRef, useState } from "react";
import {
  BookOpen, CheckCircle2, Download, Languages, Loader2, Mic, Mic2, Plus, RefreshCw, Save, Sparkles,
  Square, Trash2, UploadCloud, Video, Volume2, Wand2, XCircle,
} from "lucide-react";
import { api, fileUrl, type Asset, type DubSegment, type DubSummary, type EngineStatus, type Job, type RetakeReport, type RetakeWord, type StudioVoice, type VoiceSpec } from "../api";
import { useT } from "../i18n";
import { jobMessage, voiceEngineLabel, voiceEngineText, voiceWarning } from "../messages";
import { AssetPicker, ConfirmButton, Empty, JobState, Progress, useApp, useAsync } from "../components/ui";
import { flattenTranscriptWords } from "../voiceRetakeUi";

type Tab = "engines" | "library" | "speak" | "transcribe" | "retake" | "audiobook" | "dub";

function EngineRow({ e, onInstalled }: { e: EngineStatus; onInstalled: () => void }) {
  const { t, lang } = useT();
  const app = useApp();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = app.jobs.find((j) => j.id === jobId) || null;
  useEffect(() => {
    if (job && job.state === "done") { app.toast(`${voiceEngineLabel(e.label, lang)}: ${t("installed")}`, "ok"); onInstalled(); setJobId(null); }
    if (job && job.state === "failed") { app.toast(jobMessage(job.message, lang) || t("installFailed"), "bad"); setJobId(null); }
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
        <strong className="small">{voiceEngineLabel(e.label, lang)}</strong>{" "}
        {e.capabilities.cloning && <span className="pill">{t("cloningCapable")}</span>}
        {e.capabilities.needs_gpu && <span className="pill">{t("needsGpu")}</span>}
        <div className="muted small">{voiceEngineText(e.installed ? e.reason : e.install_hint || e.reason, lang)}</div>
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
  const { t, lang } = useT();
  return (
    <div className="row wrap">
      <select value={spec.voice_id || ""} onChange={(e) => setSpec({ ...spec, voice_id: e.target.value || null, engine_id: e.target.value ? undefined : spec.engine_id })}>
        <option value="">{t("useEngineDirect")}</option>
        {voices.map((v) => <option key={v.id} value={v.id}>{v.name} ({v.engine_id})</option>)}
      </select>
      {!spec.voice_id && (
        <select value={spec.engine_id || ""} onChange={(e) => setSpec({ ...spec, engine_id: e.target.value || null })}>
          <option value="">{t("chooseVoice")}</option>
          {engines.filter((e) => e.installed).map((e) => <option key={e.id} value={e.id}>{voiceEngineLabel(e.label, lang)}</option>)}
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
  const { t, lang } = useT();
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
            {cloningEngines.map((e) => <option key={e.id} value={e.id}>{voiceEngineLabel(e.label, lang)}</option>)}
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
                <ConfirmButton onConfirm={async () => { await api.deleteStudioVoice(v.id); voices.reload(); }}><Trash2 size={13} /></ConfirmButton>
              </div>
              <div className="row small muted wrap">
                <span>{t("duration")}: {v.quality.duration_s}s</span>
                <span>{t("snr")}: {v.quality.snr_db} dB</span>
                <span>{t("clipping")}: {v.quality.clipping_pct}%</span>
                {v.has_sample && <audio src={api.voiceSampleUrl(v.id)} controls preload="none" style={{ height: 28, width: 200 }} />}
              </div>
              {v.quality.warnings.length > 0 && <div className="muted small">{v.quality.warnings.map((w) => voiceWarning(w, lang)).join(" · ")}</div>}
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

  const speak = async () => {
    if (!text.trim() || (!spec.engine_id && !spec.voice_id)) return;
    setSpeaking(true);
    try {
      const result = await api.speak(text.trim(), spec, app.projectId || undefined);
      if (result instanceof Blob) setAudioUrl(URL.createObjectURL(result));
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
  const chunks = useRef<Blob[]>([]);

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
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks.current = [];
      const rec = new MediaRecorder(stream);
      rec.ondataavailable = (e) => { if (e.data.size) chunks.current.push(e.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach((tr) => tr.stop());
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

function useJob(jobId: string | null): Job | null {
  const app = useApp();
  return app.jobs.find((j) => j.id === jobId) || null;
}

function AudiobookTab() {
  const { t, lang } = useT();
  const app = useApp();
  const engines = useAsync(() => api.voiceEngines(), []);
  const voices = useAsync(() => api.studioVoices(), []);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [format, setFormat] = useState<"mp3" | "m4b">("mp3");
  const [spec, setSpec] = useState<VoiceSpec>({});
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const job = useJob(jobId);

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
          <div className="row"><JobState state={job.state} /><span className="small muted">{jobMessage(job.message, lang)}</span></div>
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

function DubTab() {
  const { t, lang } = useT();
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
  const [translationMode, setTranslationMode] = useState<"direct" | "adapt">("adapt");
  const [fitMode, setFitMode] = useState<"audio" | "smart">("audio");
  const [stepsOpen, setStepsOpen] = useState<number | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const job = useJob(jobId);
  const [fixIndex, setFixIndex] = useState<number | null>(null);
  const [fixText, setFixText] = useState("");

  const submit = async () => {
    if (!videoAsset || !targetLanguage.trim() || (!spec.engine_id && !spec.voice_id)) return;
    setBusy(true);
    try {
      const r = await api.dub({
        video_asset_id: videoAsset.id, target_language: targetLanguage.trim(), source_language: sourceLanguage || undefined,
        voice: spec, translation_mode: translationMode, fit_mode: fitMode, project: dubProjectId || undefined, wait_s: 0,
      });
      setJobId(r.job.id);
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const outputs = (job?.outputs || {}) as { final_video?: string; subtitles?: string; segments?: DubSegment[]; summary?: DubSummary };

  const planTip = (s: DubSegment) => {
    const p = s.plan;
    if (!p) return "";
    const parts = [t("dubPlanTip", { est: p.est_s.toFixed(1), slot: p.slot_s.toFixed(1), factor: p.factor.toFixed(2) })];
    if (p.shortened_from) parts.push(t("dubShortened", { text: p.shortened_from }));
    if (p.shorten === "no_llm") parts.push(t("dubShortenNoLlm"));
    const st = s.fit?.strategy;
    if (st && st.kind !== "fits") {
      parts.push(t("dubFitTip", { gap: st.borrow_gap_s.toFixed(2), audio: st.audio_factor.toFixed(2), video: st.video_factor.toFixed(2), trim: st.trim_s.toFixed(2) }));
    }
    return parts.join("\n");
  };
  const planLabel = (c: "fits" | "tight" | "impossible") =>
    c === "fits" ? t("dubPlanFits") : c === "tight" ? t("dubPlanTight") : t("dubPlanImpossible");
  const qcRate = (s: DubSegment) => (s.qc?.status === "ok" ? (s.qc.metric === "cer" ? s.qc.cer : s.qc.wer) : undefined);

  const resynth = async (index: number) => {
    if (!job) return;
    try {
      await api.resynthesizeDubSegment(job.id, index, { text: fixText || undefined });
      app.toast(t("saved"), "ok");
      setFixIndex(null);
      setFixText("");
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

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
        <select value={translationMode} onChange={(e) => setTranslationMode(e.target.value as "direct" | "adapt")}
                title={t("dubTranslationModeTip")} aria-label={t("dubTranslationMode")} data-testid="dub-translation-mode">
          <option value="adapt">{t("dubModeAdapt")}</option>
          <option value="direct">{t("dubModeDirect")}</option>
        </select>
        <select value={fitMode} onChange={(e) => setFitMode(e.target.value as "audio" | "smart")}
                title={t("dubFitModeTip")} aria-label={t("dubFitMode")} data-testid="dub-fit-mode">
          <option value="audio">{t("dubFitAudio")}</option>
          <option value="smart">{t("dubFitSmart")}</option>
        </select>
      </div>
      <VoiceSpecPicker spec={spec} setSpec={setSpec} engines={engines.data?.tts || []} voices={voices.data?.items || []} />
      <div className="row">
        <button className="btn primary" onClick={submit} disabled={busy || !videoAsset || !targetLanguage.trim() || (!spec.engine_id && !spec.voice_id)}>
          {busy ? <Loader2 size={14} className="spin" /> : <Languages size={14} />} {t("create")}
        </button>
      </div>
      {job && (
        <div className="stack">
          <div className="row"><JobState state={job.state} /><span className="small muted">{jobMessage(job.message, lang)}</span></div>
          {["queued", "waiting_gpu", "running"].includes(job.state) && <Progress value={job.progress} />}
          {job.state === "failed" && <div className="row"><XCircle size={14} className="err-text" /><span className="small err-text">{jobMessage(job.message, lang)}</span></div>}
          {(outputs.segments?.length || 0) > 0 && (
            <div className="card" style={{ padding: 6 }}>
              {outputs.summary && (
                <div className="small muted" style={{ padding: "2px 6px 6px" }} data-testid="dub-summary">{t("dubSummary", {
                  fits: outputs.summary.fits, tight: outputs.summary.tight, impossible: outputs.summary.impossible,
                  shortened: outputs.summary.shortened, flagged: outputs.summary.flagged })}
                  {(outputs.summary.untranslated || 0) > 0 && (
                    <span className="err-text"> · {t("dubSummaryUntranslated", { n: outputs.summary.untranslated || 0 })}</span>
                  )}
                  {(outputs.summary.retimed || 0) > 0 && (
                    <span data-testid="dub-retimed"> · {t("dubSummaryRetimed", { n: outputs.summary.retimed || 0, s: (outputs.summary.added_s || 0).toFixed(1) })}</span>
                  )}</div>
              )}
              <div className="table-scroll" tabIndex={0} role="region" aria-label={t("tableScroll")}><table className="list">
                <thead><tr><th>#</th><th>{t("sourceText")}</th><th>{t("translatedText")}</th><th>{t("dubPlan")}</th><th>{t("dubQc")}</th><th /></tr></thead>
                <tbody>
                  {(outputs.segments || []).map((s) => (
                    <Fragment key={s.index}>
                    <tr data-flagged={s.qc?.flagged ? "true" : undefined}
                        style={s.qc?.flagged ? { background: "color-mix(in srgb, var(--bad) 10%, transparent)" } : undefined}>
                      <td className="mono small">{s.index}</td>
                      <td className="small">{s.source_text}</td>
                      <td className="small">
                        {fixIndex === s.index
                          ? <input value={fixText} onChange={(e) => setFixText(e.target.value)} style={{ width: "100%" }} />
                          : s.translated_text}
                        {s.translation === "untranslated" && (
                          <span className="pill bad" style={{ marginLeft: 6 }} data-testid="dub-untranslated"
                                title={t("dubUntranslatedTip")}>{t("dubUntranslated")}</span>
                        )}
                        {s.translation_steps && (
                          <button className="btn sm ghost" style={{ marginLeft: 6 }} data-testid="dub-steps-toggle"
                                  aria-expanded={stepsOpen === s.index} title={t("dubSteps")}
                                  onClick={() => setStepsOpen(stepsOpen === s.index ? null : s.index)}>
                            {s.translation_steps.issues.length > 0 ? `${s.translation_steps.issues.length} ` : ""}{t("dubSteps")}
                          </button>
                        )}
                        {(s.fit?.strategy?.video_factor ?? 1) > 1 && s.fit?.strategy && (
                          <span className="pill warn mono" style={{ marginLeft: 6 }} data-testid="dub-retime" title={planTip(s)}>
                            {t("dubPicture", { x: s.fit.strategy.video_factor.toFixed(2) })}
                          </span>
                        )}
                      </td>
                      <td>
                        {s.plan && (
                          <span className={`pill ${s.plan.class === "fits" ? "ok" : s.plan.class === "tight" ? "warn" : "bad"}`}
                                title={planTip(s)} data-testid="dub-plan">
                            {planLabel(s.plan.class)}{s.plan.shortened_from ? " *" : ""}
                          </span>
                        )}
                      </td>
                      <td>
                        {s.qc?.status === "ok" && (
                          <span className={`pill mono ${s.qc.flagged ? "bad" : "ok"}`} data-testid="dub-qc"
                                title={t("dubQcHeard", { text: s.qc.hyp || "" })}>
                            {s.qc.metric === "cer" ? "CER" : "WER"} {Math.round((qcRate(s) ?? 0) * 100)}%{s.qc.flagged ? ` · ${t("dubQcFlagged")}` : ""}
                          </span>
                        )}
                        {s.qc?.status === "skipped" && <span className="pill" title={s.qc.reason}>{t("dubQcSkipped")}</span>}
                      </td>
                      <td>
                        {fixIndex === s.index
                          ? <button className="btn sm" onClick={() => resynth(s.index)}><Save size={12} /></button>
                          : <button className="btn sm ghost" onClick={() => { setFixIndex(s.index); setFixText(s.translated_text); }}><Sparkles size={12} /> {t("dubResynth")}</button>}
                      </td>
                    </tr>
                    {s.translation_steps && stepsOpen === s.index && (
                      <tr data-testid="dub-steps">
                        <td />
                        <td colSpan={5} className="small">
                          <div><b>{t("dubStepLiteral")}:</b> {s.translation_steps.literal || "-"}</div>
                          <div><b>{t("dubStepIssues")}:</b> {s.translation_steps.issues.length ? s.translation_steps.issues.join(" · ") : t("dubStepNoIssues")}</div>
                          <div><b>{t("dubStepAdapted")}:</b> {s.translation_steps.adapted || "-"}
                            {s.translation_steps.fallback && <span className="muted"> ({t("dubStepFallback", { step: s.translation_steps.fallback })})</span>}</div>
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  ))}
                </tbody>
              </table></div>
            </div>
          )}
          {job.state === "done" && outputs.final_video && (
            <div className="stack">
              <video src={api.dubDownloadUrl(job.id, "video")} controls style={{ width: "100%", maxHeight: 360 }} />
              <div className="row">
                <a className="btn sm" href={api.dubDownloadUrl(job.id, "video")}><Download size={13} /> {t("dubFinalVideo")}</a>
                {outputs.subtitles && <a className="btn sm" href={api.dubDownloadUrl(job.id, "subtitles")}><Download size={13} /> {t("dubSubtitles")}</a>}
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

function RetakeTab() {
  const { t } = useT();
  const app = useApp();
  const engines = useAsync(() => api.voiceEngines(), []);
  const voices = useAsync(() => api.studioVoices(), []);
  const projects = useAsync(() => api.projects(), []);
  const [projectId, setProjectId] = useState<string>(app.projectId || "");
  const [picking, setPicking] = useState(false);
  const [line, setLine] = useState<Asset | null>(null);
  const [words, setWords] = useState<RetakeWord[]>([]);
  const [wordsFrom, setWordsFrom] = useState("");
  const [range, setRange] = useState<[number, number] | null>(null);
  const [text, setText] = useState("");
  const [spec, setSpec] = useState<VoiceSpec>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ asset: Asset; retake: RetakeReport; words_from: string } | null>(null);
  const auk = useAsync(() => api.aukStatus(), []);
  const [mode, setMode] = useState<"voice" | "context" | null>(null);
  const aukReady = !!auk.data?.installed;
  const inContext = (mode ?? (aukReady ? "context" : "voice")) === "context" && aukReady;

  const open = async (a: Asset) => {
    setLine(a); setResult(null); setRange(null); setText(""); setWords([]); setWordsFrom("");
    // list views and retake results can carry a trimmed asset: read the full one for its recipe's word timings
    let full = a;
    try { full = await api.asset(a.id); } catch { /* keep what we have */ }
    setLine(full);
    const recipeWords = (full.recipe?.words as RetakeWord[] | undefined) || [];
    setWords(recipeWords); setWordsFrom(recipeWords.length ? "recipe" : "");
  };

  const loadWords = async () => {
    if (!line) return;
    setBusy(true);
    try {
      const r = await api.transcribe({ asset_id: line.id, word_timestamps: true });
      const flat = flattenTranscriptWords(r.segments || []) as RetakeWord[];
      setWords(flat); setWordsFrom("transcription"); setRange(null);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const pick = (i: number, extend: boolean) => {
    if (range && (extend || range[0] === range[1])) {
      const a = Math.min(range[0], i), b = Math.max(range[1], i);
      setRange(extend || i !== range[0] ? [a, b] : [i, i]);
    } else {
      setRange([i, i]);
    }
    if (!text && !range) setText(words[i]?.word || "");
  };

  const run = async () => {
    if (!line || !range || !text.trim()) return;
    setBusy(true);
    try {
      const voice = !inContext && (spec.engine_id || spec.voice_id) ? spec : undefined;
      const r = await api.retakeWords({ asset_id: line.id, first_word: range[0], last_word: range[1], text: text.trim(), words, voice,
        context_engine: inContext ? "auk" : undefined });
      setResult(r);
      app.toast(t("saved"), "ok");
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const continueOnResult = () => { if (result) open(result.asset); };
  const selected = (i: number) => !!range && i >= range[0] && i <= range[1];

  return (
    <div className="card stack">
      <p className="muted small">{t("retakeLead")}</p>
      <div className="row wrap">
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)} aria-label={t("noProject")}>
          <option value="">{t("noProject")}</option>
          {(projects.data?.items || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button className="btn sm" onClick={() => setPicking(true)} disabled={!projectId}>
          <Volume2 size={13} /> {line ? line.name || line.id : t("retakePickLine")}
        </button>
        {line && (
          <button className="btn sm" onClick={loadWords} disabled={busy}>
            {busy ? <Loader2 size={13} className="spin" /> : <Languages size={13} />} {t("retakeLoadWords")}
          </button>
        )}
      </div>
      {line && <audio src={fileUrl(line.id)} controls style={{ width: "100%" }} data-testid="retake-before-audio" />}
      {line && !words.length && <p className="muted small">{t("retakeNoWords")}</p>}
      {words.length > 0 && (
        <div className="stack">
          <div className="muted small">{t("retakeWordsFrom")}: {wordsFrom} · {t("retakeSelectHint")}</div>
          <div className="row wrap" role="group" aria-label={t("retakeSelectHint")} data-testid="retake-words">
            {words.map((w, i) => (
              <button key={`${i}-${w.start_s}`} className={`btn sm${selected(i) ? " primary" : ""}`} aria-pressed={selected(i)}
                title={`${w.start_s.toFixed(2)}–${w.end_s.toFixed(2)} s`}
                aria-label={`${w.word} (${w.start_s.toFixed(2)}–${w.end_s.toFixed(2)} s)`} onClick={(e) => pick(i, e.shiftKey)}>
                {w.word}
              </button>
            ))}
          </div>
          {range && (
            <div className="stack">
              <input value={text} onChange={(e) => setText(e.target.value)} placeholder={t("retakeNewText")} aria-label={t("retakeNewText")} />
              <div className="segmented" role="group" aria-label={t("retakeModeContext")} data-testid="retake-mode">
                <button className={!inContext ? "on" : ""} aria-pressed={!inContext} onClick={() => setMode("voice")}>{t("retakeModeVoice")}</button>
                <button className={inContext ? "on" : ""} aria-pressed={inContext} disabled={!aukReady}
                  title={aukReady ? t("retakeContextHint") : `${t("retakeAukMissing")}: ${auk.data?.reason || ""}`}
                  onClick={() => setMode("context")}>{t("retakeModeContext")}</button>
              </div>
              {inContext ? <div className="muted small">{t("retakeContextHint")}</div> : (
                <>
                  <div className="muted small">{t("retakeVoiceDefault")}</div>
                  <VoiceSpecPicker spec={spec} setSpec={setSpec} engines={engines.data?.tts || []} voices={voices.data?.items || []} />
                </>
              )}
              <div className="row">
                <button className="btn primary" onClick={run} disabled={busy || !text.trim()}>
                  {busy ? <Loader2 size={14} className="spin" /> : <Wand2 size={14} />} {t("retakeRun")}
                </button>
              </div>
            </div>
          )}
        </div>
      )}
      {result && line && (
        <div className="stack" data-testid="retake-result">
          <div className="row wrap">
            <div className="grow"><div className="small">{t("retakeBefore")}</div><audio src={fileUrl(line.id)} controls style={{ width: "100%" }} /></div>
            <div className="grow"><div className="small">{t("retakeAfter")}</div><audio src={fileUrl(result.asset.id)} controls style={{ width: "100%" }} /></div>
          </div>
          <div className="muted small">
            {t("retakeCut")}: {result.retake.cut_start_s.toFixed(3)}–{result.retake.cut_end_s.toFixed(3)} s
            {result.retake.cut_by && <> · {t("retakeCutBy")}: {result.retake.cut_by.join(" / ")}</>}
            {" "}· «{result.retake.replaced}» → «{result.retake.new_text}»
          </div>
          {result.retake.auk && (
            <div className="muted small" data-testid="retake-auk-takes">
              {t("retakeAukTakes")} ({result.retake.auk.variant}):{" "}
              {result.retake.auk.candidates.map((c) => (
                <span key={c.seed} style={{ fontWeight: c.seed === result.retake.auk!.chosen_seed ? 600 : 400 }}>
                  {c.seed}. «{c.heard}»{c.matched ? " ✓" : ""}{"  "}
                </span>
              ))}
            </div>
          )}
          <button className="btn sm" style={{ width: "fit-content" }} onClick={continueOnResult}><RefreshCw size={13} /> {t("retakeUseResult")}</button>
        </div>
      )}
      {picking && projectId && (
        <AssetPicker projectId={projectId} kind="audio" title={t("retakePickLine")}
          onPick={(a) => { open(a); setPicking(false); }} onClose={() => setPicking(false)} />
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
            <button className={tab === "retake" ? "on" : ""} onClick={() => setTab("retake")}>{t("voiceTabRetake")}</button>
            <button className={tab === "audiobook" ? "on" : ""} onClick={() => setTab("audiobook")}>{t("voiceTabAudiobook")}</button>
            <button className={tab === "dub" ? "on" : ""} onClick={() => setTab("dub")}>{t("voiceTabDub")}</button>
          </div>
        </div>
      </div>
      {tab === "engines" && <EnginesTab />}
      {tab === "library" && <LibraryTab />}
      {tab === "speak" && <SpeakTab />}
      {tab === "transcribe" && <TranscribeTab />}
      {tab === "retake" && <RetakeTab />}
      {tab === "audiobook" && <AudiobookTab />}
      {tab === "dub" && <DubTab />}
    </>
  );
}
