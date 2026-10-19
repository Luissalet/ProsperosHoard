import React, { useCallback, useEffect, useState } from "react";
import { api } from "../../api.js";
import { useApp } from "../../App.jsx";
import { Bar, Icon, Spinner, Toggle } from "../../components/ui.jsx";
import { useEd } from "../EditorContext.js";
import { fmtMs } from "../time.js";

// One stored translation: its state, the cues to review and fix, the downloads.
function Translation({ tr, projectId, onChange }) {
  const { t, fail } = useApp();
  const [open, setOpen] = useState(false);
  const [cues, setCues] = useState(null);
  const [dual, setDual] = useState(false);
  const load = useCallback(async () => {
    try { setCues((await api.subtitlesShow(projectId, tr.language, { limit: 400 })).items); } catch (e) { fail(e); }
  }, [projectId, tr.language, fail]);
  useEffect(() => { if (open) load(); }, [open, load]);
  const save = async (cue, text) => {
    const value = text.trim();
    if (!value || value === cue.text) return;
    try { await api.subtitlesFix(projectId, tr.language, [{ n: cue.n, text: value }]); await load(); onChange(); } catch (e) { fail(e); }
  };
  const remove = async () => { try { await api.subtitlesDelete(projectId, tr.language); onChange(); } catch (e) { fail(e); } };
  return (
    <div className="step" style={{ display: "block", marginBottom: 8 }} data-testid={`translation-${tr.language}`}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <b style={{ fontSize: 12.5 }}>{tr.name || tr.language}</b>
        <span className="chip">{t("cap_tr_lines", { n: tr.cues })}</span>
        <span className={`chip ${tr.stale ? "chip-warn" : "chip-ok"}`}>{tr.stale ? t("cap_tr_stale") : t("cap_tr_fresh")}</span>
        {tr.edited?.length ? <span className="chip">{t("cap_tr_edited")} {tr.edited.length}</span> : null}
      </div>
      {tr.fallback?.length ? <div className="chip chip-warn" style={{ height: "auto", whiteSpace: "normal", padding: "4px 8px", marginTop: 6 }}>{t("cap_tr_fallback", { n: tr.fallback.length })}</div> : null}
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
        <button type="button" className="btn btn-sm" onClick={() => setOpen(!open)} aria-expanded={open}>{t("cap_tr_review")}</button>
        <button type="button" className="btn btn-sm btn-ghost" onClick={remove} title={t("cap_tr_delete")} aria-label={t("cap_tr_delete")}><Icon name="trash" size={13} /></button>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
        <span className="muted" style={{ fontSize: 11.5 }}>{t("cap_tr_files")}</span>
        {["srt", "vtt", "ass"].map((f) => (
          <a key={f} className="btn btn-sm" href={api.subtitlesUrl(projectId, f, tr.language, dual)} download>{f.toUpperCase()}</a>
        ))}
      </div>
      <div style={{ marginTop: 6 }}><Toggle checked={dual} onChange={setDual} label={t("cap_tr_dual")} /></div>
      {open ? (
        <div style={{ marginTop: 8, display: "grid", gap: 6, maxHeight: 280, overflow: "auto" }}>
          {cues === null ? <Spinner /> : cues.map((c) => (
            <div key={c.n} style={{ fontSize: 12 }}>
              <div className="muted" style={{ fontSize: 11 }}>{c.n} · {fmtMs(c.start_ms)} · {c.source}</div>
              <input className="field" defaultValue={c.text} key={`${c.n}-${c.text}`} onBlur={(e) => save(c, e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function TranslateSubtitles({ busy }) {
  const { t, fail, notify, jobs } = useApp();
  const ed = useEd();
  const pid = ed.projectId;
  const [info, setInfo] = useState(null);
  const [lang, setLang] = useState("en");
  const [keep, setKeep] = useState("");
  const [glossary, setGlossary] = useState("");
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try { setInfo(await api.subtitles(pid)); } catch { /* optional */ }
  }, [pid]);
  useEffect(() => { refresh(); }, [refresh, ed.view.rev]);

  const running = job && (job.state === "queued" || job.state === "running");
  const existing = (info?.translations || []).find((x) => x.language === lang);
  const go = async () => {
    setError("");
    const terms = {};
    for (const line of glossary.split("\n")) {
      const [a, ...b] = line.split("=");
      if (a && a.trim() && b.join("=").trim()) terms[a.trim()] = b.join("=").trim();
    }
    try {
      const j = await api.subtitlesTranslate(pid, { language: lang, keep: keep.split(",").map((x) => x.trim()).filter(Boolean), glossary: terms, force: !!existing });
      setJob(j);
      jobs.poke();
      const final = await jobs.watch(j.id, setJob);
      setJob(final);
      if (final.state === "done") { notify(t("cap_tr_done")); } else setError(final.error || final.state);
      await refresh();
    } catch (e) { fail(e); setJob(null); }
  };

  const sourceLang = info?.source_language;
  const languages = Object.entries(info?.languages || { en: "English", es: "Spanish" }).filter(([c]) => c !== sourceLang);
  return (
    <div style={{ marginTop: 18, paddingTop: 12, borderTop: "1px solid var(--line)" }} data-testid="translate-subtitles">
      <h4 style={{ fontSize: 13, marginBottom: 4 }}>{t("cap_tr_title")}</h4>
      <div className="muted" style={{ fontSize: 12, marginBottom: 10, lineHeight: 1.45 }}>{t("cap_tr_help")}</div>
      {info && info.cues === 0 ? <div className="chip chip-warn" style={{ height: "auto", whiteSpace: "normal", padding: "6px 10px", marginBottom: 10 }}>{t("cap_tr_no_captions")}</div> : null}
      <label style={{ display: "block", marginBottom: 8 }}>
        <span className="label">{t("cap_tr_lang")}</span>
        <select className="field" value={lang} onChange={(e) => setLang(e.target.value)} disabled={!!running || busy} data-testid="translate-lang">
          {languages.map(([code, name]) => <option key={code} value={code}>{name} ({code})</option>)}
        </select>
      </label>
      <label style={{ display: "block", marginBottom: 8 }}>
        <span className="label">{t("cap_tr_keep")}</span>
        <input className="field" value={keep} onChange={(e) => setKeep(e.target.value)} disabled={!!running} data-testid="translate-keep" />
      </label>
      <label style={{ display: "block", marginBottom: 8 }}>
        <span className="label">{t("cap_tr_glossary")}</span>
        <textarea className="field" rows={2} value={glossary} onChange={(e) => setGlossary(e.target.value)} disabled={!!running} />
      </label>
      <button type="button" className="btn btn-primary" onClick={go} disabled={!!running || busy || (info && info.cues === 0)} data-testid="translate-go">
        {running ? <Spinner /> : <Icon name="wand" size={14} />}{existing ? t("cap_tr_again") : t("cap_tr_go")}
      </button>
      {job && running ? (
        <div style={{ marginTop: 8 }}>
          <Bar value={job.progress} />
          <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>{t("cap_tr_running")} {job.detail} {Math.round((job.progress || 0) * 100)}%</div>
        </div>
      ) : null}
      {error ? <div className="chip chip-err" data-testid="translate-error" style={{ height: "auto", whiteSpace: "normal", padding: "6px 10px", marginTop: 10 }}>{error}</div> : null}
      <div style={{ marginTop: 12 }}>
        {info && info.translations.length === 0 ? <div className="muted" style={{ fontSize: 12 }}>{t("cap_tr_none")}</div> : null}
        {(info?.translations || []).map((tr) => <Translation key={tr.language} tr={tr} projectId={pid} onChange={refresh} />)}
      </div>
    </div>
  );
}
