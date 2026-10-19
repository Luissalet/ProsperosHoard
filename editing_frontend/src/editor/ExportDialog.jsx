import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Bar, Field, Icon, Modal, Seg, Spinner, Toggle } from "../components/ui.jsx";
import { useEd } from "./EditorContext.js";
import { useCopilotForm, useCopilotRefresh } from "../copilot-hooks.js";
import { fmtBytes, fmtDate, fmtMs } from "./time.js";

function Result({ r, title }) {
  const { t, fail } = useApp();
  const qc = r.qc || {};
  const audioOnly = /\.(mp3|wav)$/i.test(r.path || "");
  const gif = /\.gif$/i.test(r.path || "");
  const [noPlay, setNoPlay] = useState(false);
  const reveal = async () => { try { await api.reveal(r.path); } catch (e) { fail(e); } };
  return (
    <div data-testid="export-result">
      {title ? <div style={{ marginBottom: 6 }}><span className="chip chip-info">{title}</span></div> : null}
      <div className="mono" style={{ wordBreak: "break-all", marginBottom: 8 }}>{r.path}</div>
      {audioOnly ? <audio controls src={api.renderUrl(r.id)} style={{ width: "100%" }} /> : gif ? <img src={api.renderUrl(r.id)} alt="" style={{ maxWidth: "100%", borderRadius: 6 }} /> : (
        <video controls src={api.renderUrl(r.id)} onError={() => setNoPlay(true)} style={{ width: "100%", maxHeight: 280, background: "#000", borderRadius: 6 }} />
      )}
      {noPlay ? <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>{t("no_inline_play")}</div> : null}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, margin: "10px 0" }}>
        {"ok" in qc ? <span className={`chip ${qc.ok ? "chip-ok" : "chip-err"}`}>{qc.ok ? t("qc_ok") : t("qc_problems")}</span> : null}
        {qc.integrated_lufs !== undefined && qc.integrated_lufs !== null ? <span className="chip">{qc.integrated_lufs} LUFS</span> : null}
        {r.bytes ? <span className="chip">{fmtBytes(r.bytes)}</span> : null}
        {r.width ? <span className="chip">{r.width}×{r.height}</span> : null}
        {r.duration_ms ? <span className="chip">{fmtMs(r.duration_ms).replace(/\.\d+$/, "")}</span> : null}
      </div>
      {(qc.problems || []).length ? <ul style={{ margin: "0 0 10px", paddingLeft: 18, color: "var(--danger-ink)", fontSize: 12.5 }}>{qc.problems.map((p, i) => <li key={i}>{typeof p === "string" ? p : JSON.stringify(p)}</li>)}</ul> : null}
      <div style={{ display: "flex", gap: 8 }}>
        <button type="button" className="btn" onClick={reveal}><Icon name="folder" size={14} />{t("reveal")}</button>
        <a className="btn" href={api.renderUrl(r.id, true)}><Icon name="download" size={14} />{t("download")}</a>
      </div>
    </div>
  );
}

const CANVASES = ["16:9", "9:16", "1:1", "4:5"];

export default function ExportDialog({ onClose }) {
  const { t, presets, fail, jobs } = useApp();
  const ed = useEd();
  const { marks } = ed;
  const [preset, setPreset] = useState("final");
  const [filename, setFilename] = useState(ed.view.name);
  const [folder, setFolder] = useState("");
  const [lufs, setLufs] = useState("default");
  const [useRange, setUseRange] = useState(marks.in !== null || marks.out !== null);
  const [srt, setSrt] = useState(false);
  const [copy, setCopy] = useState(false);
  const [multi, setMulti] = useState(false);
  const [canvases, setCanvases] = useState(["16:9", "9:16"]);
  const [reframe, setReframe] = useState("auto");
  const [trs, setTrs] = useState([]);
  const [trLang, setTrLang] = useState("");
  const [trDual, setTrDual] = useState(false);
  const [job, setJob] = useState(null);
  const [result, setResult] = useState(null);
  const [outputs, setOutputs] = useState([]);
  const [error, setError] = useState("");
  const [previous, setPrevious] = useState([]);
  const hasRange = marks.in !== null || marks.out !== null;
  const spec = (presets?.export_presets || []).find((p) => p.id === preset);

  const loadPrev = useCallback(async () => {
    try { setPrevious((await api.renders(ed.projectId)).renders); } catch { /* optional */ }
  }, [ed.projectId]);
  useCopilotRefresh(loadPrev);
  // what the dialog opened with: the copilot reports only what the user has changed since
  const initial = useRef({ preset: "final", filename: ed.view.name, folder: "", lufs: "default", useRange: marks.in !== null || marks.out !== null,
    srt: false, copy: false, multi: false, canvases: ["16:9", "9:16"], reframe: "auto", trLang: "", trDual: false });
  useCopilotForm("export-draft", { initial: initial.current, current: { preset, filename: filename.trim(), folder: folder.trim(), lufs, useRange, srt, copy, multi, canvases, reframe, trLang, trDual } });
  useEffect(() => {
    loadPrev();
    api.settings().then((s) => { if (s.default_export) { initial.current.preset = s.default_export; setPreset(s.default_export); } }).catch(() => {});
    api.subtitles(ed.projectId).then((s) => setTrs(s.translations || [])).catch(() => {});
  }, [loadPrev, ed.projectId]);

  const start = async () => {
    setError("");
    setResult(null);
    setOutputs([]);
    try {
      const body = { preset, filename: filename.trim(), folder: folder.trim(), subtitles: srt, mode: copy ? "copy" : "render" };
      if (lufs === "null") body.lufs = null; else if (lufs !== "default") body.lufs = Number(lufs);
      if (multi && !copy) { body.formats = canvases; body.reframe = reframe; }
      if (trLang && !copy) { body.captions_language = trLang; body.captions_dual = trDual; }
      if (useRange && hasRange) { if (marks.in !== null) body.start = marks.in; if (marks.out !== null) body.end = marks.out; }
      const j = await api.render(ed.projectId, body);
      setJob(j);
      jobs.poke();
      const final = await jobs.watch(j.id, setJob);
      setJob(final);
      if (final.state === "done") { setResult(final.result); setOutputs(final.result?.outputs || []); loadPrev(); } else setError(final.error || final.state);
    } catch (e) { fail(e); setJob(null); }
  };

  const toggleCanvas = (c) => setCanvases((cur) => (cur.includes(c) ? cur.filter((x) => x !== c) : CANVASES.filter((x) => x === c || cur.includes(x))));
  const noFormats = multi && !copy && canvases.length === 0;
  const stepOf = (job?.detail || "").match(/\((\d+)\/(\d+)\)/);
  const running = job && (job.state === "queued" || job.state === "running");
  const cancel = async () => { try { await api.jobCancel(job.id); } catch (e) { fail(e); } };

  return (
    <Modal title={t("export_title")} onClose={onClose} width={900}
      footer={<><button type="button" className="btn" onClick={onClose}>{t("close")}</button><button type="button" className="btn btn-primary" disabled={!!running || ed.duration === 0 || noFormats} onClick={start}>{running ? <Spinner /> : <Icon name="download" size={14} />}{t("export_start")}</button></>}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 22 }}>
        <div>
          <a className="btn" href={api.otioUrl(ed.projectId)} style={{ marginBottom: 14 }} data-testid="export-otio"><Icon name="download" size={14} />{t("export_otio")}</a>
          <span className="label">{t("export_preset")}</span>
          <div style={{ display: "grid", gap: 6, marginBottom: 12 }}>
            {(presets?.export_presets || []).map((p) => (
              <button key={p.id} type="button" className="tile" aria-pressed={preset === p.id} onClick={() => setPreset(p.id)} disabled={!!running} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 10px" }}>
                <span style={{ fontSize: 12.5 }}>{p.label}</span>
                <span className="chip">{p.id}</span>
              </button>
            ))}
          </div>
          <Field label={t("export_filename")}><input className="field" value={filename} onChange={(e) => setFilename(e.target.value)} disabled={!!running} /></Field>
          <Field label={t("export_folder")} help={t("export_folder_help")}><input className="field" value={folder} placeholder={t("export_folder_ph")} onChange={(e) => setFolder(e.target.value)} disabled={!!running} /></Field>
          <Field label={t("export_loudness")}>
            <select className="field" value={lufs} onChange={(e) => setLufs(e.target.value)} disabled={!!running}>
              <option value="default">{t("lufs_default")}{spec?.lufs ? ` (${spec.lufs})` : ""}</option>
              <option value="-14">−14 LUFS (YouTube / redes)</option>
              <option value="-16">−16 LUFS (podcast)</option>
              <option value="null">{t("lufs_untouched")}</option>
            </select>
          </Field>
          <div style={{ display: "grid", gap: 8 }}>
            <Toggle checked={useRange} disabled={!hasRange || !!running} onChange={setUseRange} label={hasRange ? t("export_range", { a: marks.in === null ? "0:00" : fmtMs(marks.in), b: marks.out === null ? fmtMs(ed.duration) : fmtMs(marks.out) }) : t("export_range_none")} />
            <Toggle checked={srt} disabled={!!running} onChange={setSrt} label={t("export_srt")} />
            <Toggle checked={copy} disabled={!!running} onChange={setCopy} label={t("export_copy")} />
            {copy ? <div className="muted" style={{ fontSize: 11.5, marginLeft: 22 }}>{t("export_copy_help")}</div> : null}
            <Toggle checked={multi && !copy} disabled={!!running || copy} onChange={setMulti} label={t("export_multi")} />
          </div>
          {copy ? <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>{t("export_copy_no_multi")}</div> : null}
          {multi && !copy ? (
            <div data-testid="export-multi" style={{ marginTop: 10, padding: 10, background: "var(--panel-2)", border: "1px solid var(--line)", borderRadius: 8 }}>
              <div className="muted" style={{ fontSize: 11.5, marginBottom: 8 }}>{t("export_multi_help")}</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
                {CANVASES.map((c) => (
                  <button key={c} type="button" className="tile" aria-pressed={canvases.includes(c)} disabled={!!running} onClick={() => toggleCanvas(c)} data-testid={`canvas-${c.replace(":", "x")}`} style={{ padding: "6px 12px", fontSize: 12.5 }}>{c}</button>
                ))}
              </div>
              <span className="label">{t("export_multi_reframe")}</span>
              <Seg value={reframe} onChange={setReframe} options={["auto", "center", "blur"].map((v) => ({ value: v, label: t(`reframe_${v}`) }))} />
              {noFormats ? <div className="chip chip-warn" style={{ marginTop: 8 }}>{t("export_multi_none")}</div> : null}
            </div>
          ) : null}
          {!copy ? (
            <div data-testid="export-tr" style={{ marginTop: 10 }}>
              <Field label={t("export_tr")} help={trs.length ? t("export_tr_help") : t("cap_tr_none")}>
                <select className="field" value={trLang} onChange={(e) => setTrLang(e.target.value)} disabled={!!running || trs.length === 0} data-testid="export-tr-lang">
                  <option value="">{t("export_tr_none")}</option>
                  {trs.map((x) => <option key={x.language} value={x.language} disabled={x.stale}>{x.name || x.language}{x.stale ? ` (${t("export_tr_stale")})` : ""}</option>)}
                </select>
              </Field>
              {trLang ? <Toggle checked={trDual} disabled={!!running} onChange={setTrDual} label={t("export_tr_dual")} /> : null}
            </div>
          ) : null}
        </div>
        <div style={{ position: "sticky", top: 0, alignSelf: "start" }}>
          {job ? (
            <div style={{ marginBottom: 14 }} data-testid="export-job">
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
                <b style={{ fontSize: 12.5 }}>{job.label}</b>
                <span className={`chip ${job.state === "done" ? "chip-ok" : job.state === "failed" ? "chip-err" : "chip-info"}`}>{t(`job_${job.state}`)}</span>
              </div>
              {multi && !copy ? (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 8 }} data-testid="export-steps">
                  {canvases.map((c, i) => {
                    const cur = stepOf ? Number(stepOf[1]) - 1 : -1;
                    const done = job.state === "done" || (cur > i);
                    return <span key={c} className={`chip ${done ? "chip-ok" : cur === i && running ? "chip-info" : ""}`}>{c}{done ? " ✓" : ""}</span>;
                  })}
                </div>
              ) : null}
              <Bar value={job.state === "done" ? 1 : job.progress} />
              <div className="muted" style={{ fontSize: 11.5, marginTop: 4, display: "flex", justifyContent: "space-between" }}>
                <span>{job.detail} {Math.round((job.progress || 0) * 100)}%</span>
                {running ? <button type="button" className="btn btn-sm btn-ghost" onClick={cancel}>{t("cancel")}</button> : null}
              </div>
            </div>
          ) : null}
          {error ? <div className="chip chip-err" style={{ height: "auto", whiteSpace: "normal", padding: "6px 10px", marginBottom: 10 }}>{error}</div> : null}
          {outputs.length > 1 ? (
            <div data-testid="export-outputs">
              <h4 className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", margin: "0 0 8px" }}>{t("export_outputs")}</h4>
              <div style={{ display: "grid", gap: 6, marginBottom: 10 }}>
                {outputs.map((o) => (
                  <button key={o.id} type="button" className="tile" aria-pressed={result?.id === o.id} onClick={() => setResult(o)} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: "6px 10px" }}>
                    <span className="ellipsis mono" style={{ fontSize: 12 }} title={o.path}>{o.path.split(/[\\/]/).pop()}</span>
                    <span style={{ display: "flex", gap: 4 }}>
                      <span className="chip">{o.width}×{o.height}</span>
                      {o.qc && "ok" in o.qc ? <span className={`chip ${o.qc.ok ? "chip-ok" : "chip-err"}`}>{o.qc.ok ? t("qc_ok") : t("qc_problems")}</span> : null}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {result ? <Result r={result} title={result.variant && outputs.length > 1 ? result.variant : null} /> : null}
          <h4 className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", margin: "18px 0 8px" }}>{t("export_previous")}</h4>
          {previous.length === 0 ? <div className="muted" style={{ fontSize: 12 }}>{t("export_none")}</div> : null}
          {previous.map((r) => (
            <div key={r.id} className="step" style={{ display: "flex", alignItems: "center", gap: 8, cursor: r.exists ? "pointer" : "default" }} onClick={() => r.exists && setResult(r)}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div className="ellipsis mono" title={r.path}>{r.path.split(/[\\/]/).pop()}</div>
                <div className="muted" style={{ fontSize: 11.5 }}>{r.preset}{r.variant ? ` · ${r.variant}` : ""} · {fmtBytes(r.bytes)} · {fmtDate(r.created_ts)} {r.qc && "ok" in r.qc ? (r.qc.ok ? "· ✓" : "· ⚠") : ""}</div>
              </div>
              {!r.exists ? <span className="chip chip-warn">{t("export_gone")}</span> : null}
            </div>
          ))}
        </div>
      </div>
    </Modal>
  );
}
