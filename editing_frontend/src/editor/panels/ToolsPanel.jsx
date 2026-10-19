import React, { useEffect, useState } from "react";
import { api } from "../../api.js";
import { go, useApp } from "../../App.jsx";
import { Icon, NumInput, Seg, SliderRow, Spinner, Toggle } from "../../components/ui.jsx";
import { useMediaLibrary } from "../../components/Media.jsx";
import { useEd } from "../EditorContext.js";
import { fmtMs } from "../time.js";
import MulticamTool from "./MulticamTool.jsx";
import { Hint, PanelHead } from "./Shared.jsx";
import { BrollTool, MusicPicker } from "./ToolsExtra.jsx";

const secs = (ms) => `${(Number(ms || 0) / 1000).toFixed(1)}`;

// One sentence per tool instead of raw keys.
function describe(id, s, t) {
  const n = (v) => Number(v || 0);
  switch (id) {
    case "remove_silences": return t(s.mode === "speed" ? "sum_silences_speed" : "sum_silences", { n: n(s.cuts), s: secs(s.saved_ms) });
    case "remove_fillers": return t("sum_fillers", { n: n(s.removed) });
    case "split_scenes": return t(s.mode === "markers" ? "sum_scenes_markers" : "sum_scenes", { n: n(s.scenes) });
    case "reframe": return t("sum_reframe", { n: n(s.clips), canvas: s.canvas, mode: t(`reframe_${s.mode}`) }) + (s.detector ? ` · ${t("sum_detector", { d: s.face_detector || s.detector })}` : "");
    case "match_loudness": return t("sum_loudness", { n: n(s.clips), lufs: s.target_lufs });
    case "zoom_cuts": return t("sum_zoom", { n: n(s.clips), scale: s.scale, every: s.every });
    case "beat_sync": return t("sum_beat", { n: n(s.cuts), bpm: Math.round(n(s.bpm)), step: s.beats_per_cut, length: s.length });
    case "script_assemble": {
      const parts = [t("sum_script_found", { a: n(s.segments), b: n(s.of) })];
      if (n(s.retakes)) parts.push(t("script_retakes", { n: n(s.retakes) }));
      if (s.duration) parts.push(t("sum_duration", { d: s.duration }));
      return parts.join(" · ");
    }
    default: return null;
  }
}

function Summary({ id, res }) {
  const { t } = useApp();
  if (!res) return null;
  const s = res.summary || {};
  const sentence = describe(id, s, t);
  const rows = sentence ? [] : Object.entries(s).filter(([, v]) => v !== null && typeof v !== "object");
  return (
    <div style={{ marginTop: 8, padding: 8, background: "var(--field)", borderRadius: 6, fontSize: 12 }} data-testid="tool-summary">
      <b>{res.preview ? t("tool_preview_result") : t("tool_applied")}</b>
      {sentence ? <div style={{ marginTop: 3 }}>{sentence}</div> : null}
      {res.duration_before && id !== "script_assemble" ? <div className="muted num">{res.duration_before} → {res.duration_after}</div> : null}
      {rows.map(([k, v]) => <div key={k} className="muted"><span className="mono">{k}</span>: {String(v)}</div>)}
      {id === "reframe" && s.detector_note ? <div className="muted" style={{ marginTop: 3 }} data-testid="detector-note">{s.detector_note}</div> : null}
      {s.mode === "meaning" && s.notes ? <div className="muted" style={{ marginTop: 4 }}>{s.notes}</div> : null}
      {Array.isArray(s.examples) && s.examples.length ? <div className="muted" style={{ marginTop: 4 }}>{s.examples.slice(0, 5).map((x) => `${x.at} ${x.text}`).join(" · ")}</div> : null}
    </div>
  );
}

function ToolCard({ id, icon, title, help, children, run, canRun = true, extra, defaultOpen = false, slow = false }) {
  const { t, fail } = useApp();
  const ed = useEd();
  const [busy, setBusy] = useState(null);
  const [res, setRes] = useState(null);
  const locked = !!busy || ed.busy > 0;
  const go_ = async (preview) => {
    setBusy(preview ? "preview" : "apply");
    try {
      setRes(await run(preview));
    } catch (e) { fail(e); } finally { setBusy(null); }
  };
  return (
    <details className="panel" style={{ marginBottom: 10 }} data-tool={id} open={defaultOpen || undefined}>
      <summary style={{ padding: "9px 12px", display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
        <Icon name={icon} size={16} style={{ color: "var(--accent)" }} />{title}
      </summary>
      <div style={{ padding: "0 12px 12px" }}>
        <Hint>{help}</Hint>
        {children}
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button type="button" className="btn btn-sm" disabled={locked || !canRun} onClick={() => go_(true)}>{busy === "preview" ? <Spinner /> : <Icon name="eye" size={14} />}{t("preview")}</button>
          <button type="button" className="btn btn-sm btn-primary" disabled={locked || !canRun} onClick={() => go_(false)}>{busy === "apply" ? <Spinner /> : <Icon name="check" size={14} />}{t("apply")}</button>
        </div>
        {busy && slow ? <div className="muted" style={{ marginTop: 8, fontSize: 11.5 }}><Spinner /> {t("tool_slow")}</div> : null}
        <Summary id={id} res={res} />
        {extra ? extra(res) : null}
      </div>
    </details>
  );
}

function pct(v) {
  const n = Number(v);
  if (Number.isNaN(n)) return "";
  return `${Math.round((n <= 1 ? n * 100 : n))}%`;
}

function ScriptResult({ res }) {
  const { t } = useApp();
  const s = res?.summary;
  if (!s) return null;
  const chosen = Array.isArray(s.chosen) ? s.chosen : [];
  const missing = Array.isArray(s.missing) ? s.missing : [];
  const cell = { padding: "3px 6px", borderBottom: "1px solid var(--line)", verticalAlign: "top" };
  return (
    <div style={{ marginTop: 8 }} data-testid="script-result">
      {chosen.length ? (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, tableLayout: "fixed" }}>
          <colgroup><col style={{ width: 20 }} /><col /><col style={{ width: 104 }} /><col style={{ width: 40 }} /><col style={{ width: 42 }} /></colgroup>
          <thead>
            <tr className="muted" style={{ textAlign: "left" }}>
              <th style={cell}>#</th><th style={cell}>{t("script_col_title")}</th><th style={cell}>{t("script_col_at")}</th><th style={cell}>{t("script_col_takes")}</th><th style={cell} title={t("script_col_cov")}>{t("script_col_cov_short")}</th>
            </tr>
          </thead>
          <tbody>
            {chosen.map((c, i) => (
              <tr key={i}>
                <td style={cell} className="num">{c.segment}</td>
                <td style={{ ...cell, wordBreak: "break-word" }}>{c.title}</td>
                <td style={{ ...cell, whiteSpace: "nowrap" }} className="num">{c.at}</td>
                <td style={cell} className="num">{c.takes ?? "—"}</td>
                <td style={{ ...cell, color: c.coverage === null || c.coverage === undefined ? undefined : Number(c.coverage) < 0.75 ? "var(--warn, #f5b700)" : "var(--accent)" }} className="num">{c.coverage === null || c.coverage === undefined ? "—" : pct(c.coverage)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      {missing.length ? (
        <div style={{ marginTop: 6, fontSize: 12 }} data-testid="script-missing">
          <b style={{ color: "var(--danger-ink)" }}>{t("script_missing", { n: missing.length })}</b>
          <ul style={{ margin: "3px 0 0", paddingLeft: 18 }} className="muted">
            {missing.map((m, i) => <li key={i}>{typeof m === "object" ? `${m.segment ?? ""} ${m.title ?? ""}`.trim() : String(m)}</li>)}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function ScriptTool({ videos }) {
  const { t, fail } = useApp();
  const ed = useEd();
  const mainMedia = (() => {
    const main = ed.doc.tracks.find((tr) => tr.role === "main") || ed.doc.tracks.find((tr) => tr.kind === "video");
    const first = main?.clips.find((c) => c.type === "media");
    return first?.media || "";
  })();
  const [mediaId, setMediaId] = useState("");
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState("");
  const [take, setTake] = useState("last");
  const [mode, setMode] = useState("auto");
  const [markers, setMarkers] = useState(true);
  const fileRef = React.useRef(null);
  const chosenMedia = mediaId || mainMedia || videos[0]?.id || "";
  const load = (file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { setText(String(reader.result || "")); setFileName(file.name); };
    reader.onerror = () => fail(new Error(t("script_read_error")));
    reader.readAsText(file);
  };
  const waiting = ed.analysis;
  return (
    <ToolCard id="script_assemble" icon="clip" title={t("tool_script")} help={t("tool_script_help")} defaultOpen canRun={!!chosenMedia && !!text.trim()}
      slow={mode !== "words"}
      run={(preview) => ed.runCommand("script_assemble", { media: chosenMedia, script: text, take, markers, mode }, { preview })}
      extra={(res) => (<>
        {waiting ? <div className="chip chip-info" style={{ marginTop: 8, height: "auto", padding: "4px 8px", whiteSpace: "normal" }}><Spinner /> {waiting.message || t("analysis_wait")}</div> : null}
        <ScriptResult res={res} />
      </>)}>
      <Labeled label={t("script_recording")}>
        <select className="field" value={chosenMedia} onChange={(e) => setMediaId(e.target.value)} data-testid="script-media">
          {videos.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
        </select>
      </Labeled>
      <textarea className="field" style={{ width: "100%", minHeight: 96, resize: "vertical", height: "auto", fontFamily: "inherit", marginTop: 4 }} placeholder={t("script_placeholder")} value={text} onChange={(e) => { setText(e.target.value); setFileName(""); }} data-testid="script-text" />
      <div style={{ display: "flex", gap: 8, alignItems: "center", margin: "6px 0" }}>
        <button type="button" className="btn btn-sm" onClick={() => fileRef.current?.click()}><Icon name="upload" size={14} />{t("script_load")}</button>
        {fileName ? <span className="muted ellipsis" style={{ fontSize: 11.5 }}>{fileName}</span> : null}
        <input ref={fileRef} type="file" accept=".md,.txt,.json,text/plain,text/markdown,application/json" style={{ display: "none" }} data-testid="script-file" onChange={(e) => { load(e.target.files?.[0]); e.target.value = ""; }} />
      </div>
      <Labeled label={t("script_mode")}>
        <select className="field" value={mode} onChange={(e) => setMode(e.target.value)} data-testid="script-mode">
          <option value="auto">{t("script_mode_auto")}</option>
          <option value="words">{t("script_mode_words")}</option>
          <option value="meaning">{t("script_mode_meaning")}</option>
        </select>
      </Labeled>
      <div className="muted" style={{ fontSize: 11.5, margin: "-3px 0 7px" }}>{t(`script_mode_help_${mode}`)}</div>
      <Labeled label={t("script_take")}>
        <select className="field" value={take} disabled={mode === "meaning"} onChange={(e) => setTake(e.target.value)} data-testid="script-take">
          <option value="last">{t("script_take_last")}</option>
          <option value="best">{t("script_take_best")}</option>
        </select>
      </Labeled>
      <Toggle checked={markers} onChange={setMarkers} label={t("script_markers")} />
      <div style={{ height: 4 }} />
      {waiting ? null : <div style={{ height: 0 }} />}
    </ToolCard>
  );
}

function Labeled({ label, children }) {
  return <div className="row"><span className="l">{label}</span><div style={{ minWidth: 0 }}>{children}</div></div>;
}

function Highlights({ media }) {
  const { t, fail, notify } = useApp();
  const ed = useEd();
  const [mediaId, setMediaId] = useState("");
  const [count, setCount] = useState(5);
  const [len, setLen] = useState(30);
  const [smart, setSmart] = useState(false);
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const videos = (media || []).filter((m) => m.kind === "video");
  useEffect(() => { if (!mediaId && videos.length) setMediaId(videos[0].id); }, [mediaId, videos]);
  const find = async () => {
    setBusy(true);
    try { setRes(await ed.withAnalysis(() => api.highlights(mediaId, { count, length_s: len, mode: smart ? "model" : "signals" }))); } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const make = async (h, i) => {
    setBusy(true);
    try {
      const r = await api.short(mediaId, { start: h.start_ms, end: h.end_ms, name: `${t("short_name")} ${i + 1}`, preset: "reels", captions: true });
      notify(t("short_created"), "ok");
      go(`p/${r.project}`);
    } catch (e) { fail(e); setBusy(false); }
  };
  return (
    <details className="panel" style={{ marginBottom: 10 }} data-tool="highlights">
      <summary style={{ padding: "9px 12px", display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}><Icon name="star" size={16} style={{ color: "var(--accent)" }} />{t("tool_highlights")}</summary>
      <div style={{ padding: "0 12px 12px" }}>
        <Hint>{t("tool_highlights_help")}</Hint>
        <Labeled label={t("tool_media")}>
          <select className="field" value={mediaId} onChange={(e) => { setMediaId(e.target.value); setRes(null); }}>
            <option value="">—</option>
            {videos.map((m) => <option key={m.id} value={m.id}>{m.name} ({fmtMs(m.duration_ms).replace(/\.\d+$/, "")})</option>)}
          </select>
        </Labeled>
        <Labeled label={t("count")}><NumInput value={count} min={1} max={20} step={1} decimals={0} onCommit={(v) => setCount(Math.round(v))} /></Labeled>
        <Labeled label={t("clip_length_s")}><NumInput value={len} min={5} max={180} step={5} decimals={0} onCommit={(v) => setLen(Math.round(v))} /></Labeled>
        <Toggle checked={smart} onChange={setSmart} label={t("highlights_model")} />
        <div style={{ height: 8 }} />
        <button type="button" className="btn btn-sm btn-primary" disabled={!mediaId || busy} onClick={find} data-testid="highlights-find">{busy ? <Spinner /> : <Icon name="search" size={14} />}{t("highlights_find")}</button>
        {res ? (
          <div style={{ marginTop: 10 }}>
            <div className="muted" style={{ fontSize: 11.5, marginBottom: 6 }} data-testid="highlights-signals">{t("signals")}: {(res.signals || []).join(", ") || "—"}</div>
            {res.model ? <div className="muted" style={{ fontSize: 11.5, marginBottom: 6 }} data-testid="highlights-model">{res.model.used ? t("highlights_model_used", { n: res.model.moments, m: res.model.model || "" }) : t("highlights_model_off", { r: res.model.reason || "" })}</div> : null}
            {(res.highlights || []).map((h, i) => (
              <div key={i} className="step" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="mono num" style={{ fontSize: 12 }}>{h.range || `${fmtMs(h.start_ms)}–${fmtMs(h.end_ms)}`}</div>
                  <div className="muted" style={{ fontSize: 11.5 }}>{(h.reasons || []).map((r) => t(`reason_${r}`)).join(", ") || "—"} · {h.score}</div>
                  {h.why ? <div className="muted" style={{ fontSize: 11.5, fontStyle: "italic" }}>{h.why}</div> : null}
                </div>
                <button type="button" className="btn btn-sm" disabled={busy} onClick={() => make(h, i)}>{t("short_make")}</button>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </details>
  );
}

export default function ToolsPanel() {
  const { t } = useApp();
  const ed = useEd();
  const { media } = useMediaLibrary();
  const [sil, setSil] = useState({ auto: true, threshold: -35, min: 500, margin: 150, mode: "cut", speed: 4 });
  const [fill, setFill] = useState({ repeats: true, strict: false });
  const [scn, setScn] = useState({ mode: "split" });
  const [rf, setRf] = useState({ aspect: "9:16", mode: "auto" });
  const [ld, setLd] = useState({ target: -16 });
  const [zc, setZc] = useState({ scale: 1.12, every: 2 });
  const [bs, setBs] = useState({ music: "", source: "", beats: 2, mode: "scenes" });
  const audios = (media || []).filter((m) => m.has_audio && m.kind === "audio");
  const videos = (media || []).filter((m) => m.kind === "video");
  const cmd = (name, args) => (preview) => ed.runCommand(name, args, { preview });

  return (
    <div className="ed-panel">
      <PanelHead title={t("tab_tools")} />
      <div className="ed-panel-body">
        <Hint>{t("tools_help")}</Hint>

        <MulticamTool />

        <ScriptTool videos={videos} />

        <ToolCard id="remove_silences" icon="waveform" title={t("tool_silences")} help={t("tool_silences_help")}
          run={cmd("remove_silences", { ...(sil.auto ? {} : { threshold_db: sil.threshold }), min_silence_ms: sil.min, margin_ms: sil.margin, mode: sil.mode, ...(sil.mode === "speed" ? { speed: sil.speed } : {}) })}>
          <Labeled label={t("threshold")}><Toggle checked={sil.auto} onChange={(v) => setSil({ ...sil, auto: v })} label={t("auto")} /></Labeled>
          {!sil.auto ? <SliderRow label="dB" value={sil.threshold} min={-60} max={-10} step={1} decimals={0} onChange={(v) => setSil({ ...sil, threshold: v })} /> : null}
          <Labeled label={t("min_silence")}><NumInput value={sil.min} min={100} max={5000} step={50} decimals={0} onCommit={(v) => setSil({ ...sil, min: Math.round(v) })} /></Labeled>
          <Labeled label={t("margin")}><NumInput value={sil.margin} min={0} max={1000} step={10} decimals={0} onCommit={(v) => setSil({ ...sil, margin: Math.round(v) })} /></Labeled>
          <Labeled label={t("mode")}><Seg value={sil.mode} onChange={(v) => setSil({ ...sil, mode: v })} options={[{ value: "cut", label: t("mode_cut") }, { value: "speed", label: t("mode_speed") }]} /></Labeled>
          {sil.mode === "speed" ? <Labeled label={t("speed")}><NumInput value={sil.speed} min={1.5} max={16} step={0.5} decimals={1} onCommit={(v) => setSil({ ...sil, speed: v })} /></Labeled> : null}
        </ToolCard>

        <ToolCard id="remove_fillers" icon="mic" title={t("tool_fillers")} help={t("tool_fillers_help")} run={cmd("remove_fillers", { repeats: fill.repeats, strict: fill.strict })}>
          <Toggle checked={fill.repeats} onChange={(v) => setFill({ ...fill, repeats: v })} label={t("fillers_repeats")} />
          <div style={{ height: 6 }} />
          <Toggle checked={fill.strict} onChange={(v) => setFill({ ...fill, strict: v })} label={t("fillers_strict")} />
        </ToolCard>

        <ToolCard id="split_scenes" icon="split" title={t("tool_scenes")} help={t("tool_scenes_help")} run={cmd("split_scenes", { mode: scn.mode })}>
          <Labeled label={t("mode")}><Seg value={scn.mode} onChange={(v) => setScn({ mode: v })} options={[{ value: "split", label: t("scenes_split") }, { value: "markers", label: t("scenes_markers") }]} /></Labeled>
        </ToolCard>

        <ToolCard id="reframe" icon="crop" title={t("tool_reframe")} help={t("tool_reframe_help")} run={cmd("reframe", { aspect: rf.aspect, mode: rf.mode })}>
          <Labeled label={t("aspect")}><Seg value={rf.aspect} onChange={(v) => setRf({ ...rf, aspect: v })} options={["9:16", "1:1", "4:5", "16:9"].map((a) => ({ value: a, label: a }))} /></Labeled>
          <Labeled label={t("mode")}>
            <select className="field" value={rf.mode} onChange={(e) => setRf({ ...rf, mode: e.target.value })}>
              {["auto", "track", "stable", "center", "blur"].map((m) => <option key={m} value={m}>{t(`reframe_${m}`)}</option>)}
            </select>
          </Labeled>
        </ToolCard>

        <ToolCard id="zoom_cuts" icon="crop" title={t("tool_zoom")} help={t("tool_zoom_help")} run={cmd("zoom_cuts", { scale: zc.scale, every: zc.every })}>
          <SliderRow label={t("zoom_scale")} value={zc.scale} min={1.05} max={1.4} step={0.01} decimals={2} defaultValue={1.12} onChange={(v) => setZc({ ...zc, scale: v })} />
          <Labeled label={t("zoom_every")}><NumInput value={zc.every} min={1} max={10} step={1} decimals={0} onCommit={(v) => setZc({ ...zc, every: Math.max(1, Math.round(v)) })} /></Labeled>
        </ToolCard>

        <ToolCard id="match_loudness" icon="volume" title={t("tool_loudness")} help={t("tool_loudness_help")} run={cmd("match_loudness", { target_lufs: ld.target })}>
          <Labeled label="LUFS"><NumInput value={ld.target} min={-40} max={-5} step={1} decimals={0} onCommit={(v) => setLd({ target: v })} /></Labeled>
        </ToolCard>

        <ToolCard id="beat_sync" icon="music" title={t("tool_beat")} help={t("tool_beat_help")} canRun={!!bs.music && (bs.mode === "clips" || !!bs.source)}
          run={cmd("beat_sync", { music: bs.music, beats_per_cut: bs.beats, mode: bs.mode, ...(bs.mode === "scenes" && bs.source ? { source: bs.source } : {}) })}>
          <Labeled label={t("beat_music")}>
            <select className="field" value={bs.music} onChange={(e) => setBs({ ...bs, music: e.target.value })}>
              <option value="">—</option>
              {audios.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            </select>
          </Labeled>
          <Labeled label={t("mode")}><Seg value={bs.mode} onChange={(v) => setBs({ ...bs, mode: v })} options={[{ value: "clips", label: t("beat_clips") }, { value: "scenes", label: t("beat_scenes") }]} /></Labeled>
          {bs.mode === "scenes" ? (
            <Labeled label={t("beat_source")}>
              <select className="field" value={bs.source} onChange={(e) => setBs({ ...bs, source: e.target.value })}>
                <option value="">—</option>
                {videos.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
              </select>
            </Labeled>
          ) : null}
          <Labeled label={t("beats_per_cut")}><NumInput value={bs.beats} min={1} max={16} step={1} decimals={0} onCommit={(v) => setBs({ ...bs, beats: Math.round(v) })} /></Labeled>
        </ToolCard>

        <MusicPicker />

        <BrollTool />

        <Highlights media={media} />
      </div>
    </div>
  );
}
