import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { go, useApp } from "../App.jsx";
import { Bar, Icon, Logo, Spinner } from "../components/ui.jsx";
import JobsDrawer from "../components/JobsDrawer.jsx";
import ShortcutsDialog from "../components/ShortcutsDialog.jsx";
import { useActions } from "../editor/actions.js";
import { EditorCtx } from "../editor/EditorContext.js";
import ExportDialog from "../editor/ExportDialog.jsx";
import ContactSheetDialog from "../editor/ContactSheetDialog.jsx";
import Inspector from "../editor/Inspector.jsx";
import { Playback } from "../editor/playback.js";
import Player from "../editor/Player.jsx";
import TextView from "../editor/TextView.jsx";
import Timeline from "../editor/Timeline.jsx";
import { clipEnd, projectDuration, trackRows } from "../editor/time.js";
import { useMediaInfo, useProject } from "../editor/useProject.js";
import { CONTEXT_REQUEST } from "../copilot-core.js";
import { useCopilotForm, useCopilotRefresh } from "../copilot-hooks.js";
import AssistantPanel from "../editor/panels/AssistantPanel.jsx";
import CaptionsPanel from "../editor/panels/CaptionsPanel.jsx";
import EffectsPanel from "../editor/panels/EffectsPanel.jsx";
import MediaPanel from "../editor/panels/MediaPanel.jsx";
import TextPanel from "../editor/panels/TextPanel.jsx";
import ToolsPanel from "../editor/panels/ToolsPanel.jsx";
import TransitionsPanel from "../editor/panels/TransitionsPanel.jsx";

const TABS = [
  { key: "media", icon: "film", label: "tab_media", C: MediaPanel },
  { key: "text", icon: "type", label: "tab_text", C: TextPanel },
  { key: "transitions", icon: "transition", label: "tab_transitions", C: TransitionsPanel },
  { key: "effects", icon: "effects", label: "tab_effects", C: EffectsPanel },
  { key: "captions", icon: "subtitles", label: "tab_captions", C: CaptionsPanel },
  { key: "assistant", icon: "sparkles", label: "tab_assistant", C: AssistantPanel },
  { key: "tools", icon: "wand", label: "tab_tools", C: ToolsPanel },
];

const isTyping = (el) => !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);

function TopBar({ ed, onExport, onContactSheet, onJobs, onHelp }) {
  const { t, presets, jobs } = useApp();
  const { view, doc } = ed;
  const [name, setName] = useState(view.name);
  const lastServerName = useRef(view.name);
  // A rename made elsewhere (e.g. by the agent) refreshes the field, unless the user has an unsaved edit in it.
  useEffect(() => {
    setName((cur) => (cur === lastServerName.current ? view.name : cur));
    lastServerName.current = view.name;
  }, [view.name]);
  useCopilotForm("project-form", { objectId: view.id, initial: { name: view.name }, current: { name: name.trim() } });
  const commit = () => { if (name.trim() && name.trim() !== view.name) ed.rename(name.trim()); else setName(view.name); };
  const current = presets ? Object.entries(presets.canvas_presets).find(([, p]) => p.width === doc.canvas.width && p.height === doc.canvas.height && p.fps === doc.canvas.fps)?.[0] || "" : "";
  const issues = (view.issues || []).filter((i) => i.level === "error");
  return (
    <div className="ed-top">
      <button type="button" className="btn btn-ghost btn-icon" onClick={() => go("")} title={t("back")} aria-label={t("back")}><Icon name="back" size={18} /></button>
      <a href="#/" title="Lumiere's Hoard" style={{ display: "flex" }}><Logo size={22} /></a>
      <input className="field hoard-display" style={{ width: 230, background: "transparent", border: "1px solid transparent", fontWeight: 700, fontSize: 15, height: 30, letterSpacing: "0.01em" }} value={name} aria-label={t("project_name")} onChange={(e) => setName(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); if (e.key === "Escape") { setName(view.name); e.target.blur(); } }} onFocus={(e) => e.target.select()} />
      <span style={{ width: 1, height: 20, background: "var(--line-2)", margin: "0 4px" }} />
      <button type="button" className="btn btn-ghost btn-icon" disabled={!view.can_undo} onClick={ed.undo} title={`${t("undo")}${view.undo_label ? `: ${view.undo_label}` : ""} (Ctrl+Z)`} aria-label={t("undo")}><Icon name="undo" size={17} /></button>
      <button type="button" className="btn btn-ghost btn-icon" disabled={!view.can_redo} onClick={ed.redo} title={`${t("redo")}${view.redo_label ? `: ${view.redo_label}` : ""} (Ctrl+Shift+Z)`} aria-label={t("redo")}><Icon name="redo" size={17} /></button>
      <span style={{ width: 1, height: 20, background: "var(--line-2)", margin: "0 4px" }} />
      <select className="field" style={{ width: 200 }} value={current} aria-label={t("canvas")} onChange={(e) => e.target.value && ed.edit([{ op: "canvas", preset: e.target.value }], t("lbl_canvas"))}>
        <option value="">{`${doc.canvas.width}×${doc.canvas.height} · ${doc.canvas.fps}fps`}</option>
        {Object.entries(presets?.canvas_presets || {}).map(([k, p]) => <option key={k} value={k}>{p.label}</option>)}
      </select>
      {issues.length ? <span className="chip chip-err" title={issues.map((i) => i.message).join("\n")}><Icon name="help" size={12} />{t("issues_n", { n: issues.length })}</span> : null}
      {ed.busy > 0 ? <Spinner /> : null}
      <div style={{ flex: 1 }} />
      <button type="button" className={`btn btn-sm ${jobs.active.length ? "btn-on" : ""}`} onClick={onJobs} title={t("jobs_title")} data-testid="jobs-btn">
        {jobs.active.length ? <span className="spinner" style={{ width: 11, height: 11 }} /> : <Icon name="activity" size={14} />}
        {t("jobs_n", { n: jobs.active.length })}
      </button>
      <button type="button" className="btn btn-sm" onClick={onContactSheet} title={t("contact_sheet_title")} data-testid="contact-sheet-btn"><Icon name="grid" size={14} />{t("contact_sheet_short")}</button>
      <button type="button" className="btn btn-ghost btn-icon" onClick={onHelp} title={`${t("shortcuts")} (?)`} aria-label={t("shortcuts")}><Icon name="help" size={17} /></button>
      <button type="button" className="btn btn-primary" onClick={onExport} title="Ctrl+E" data-testid="export-btn"><Icon name="download" size={14} />{t("export")}</button>
    </div>
  );
}

function AnalysisBanner({ analysis }) {
  const { t, jobs } = useApp();
  if (!analysis) return null;
  const mine = jobs.active.filter((j) => analysis.ids.includes(j.id));
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 14px", background: "color-mix(in srgb, var(--info) 22%, var(--panel))", borderBottom: "1px solid color-mix(in srgb, var(--info) 50%, var(--panel))", fontSize: 12.5 }}>
      <Spinner />
      <span style={{ flex: 1 }}>{t("analysis_banner")} {mine.map((j) => `${j.label} ${Math.round(j.progress * 100)}%`).join(" · ")}</span>
      {mine[0] ? <div style={{ width: 160 }}><Bar value={mine[0].progress} /></div> : null}
    </div>
  );
}

function EditorInner({ proj }) {
  const app = useApp();
  const { t, notify, fail, jobs } = app;
  const { view, doc } = proj;
  const projectId = view.id;
  const pb = useMemo(() => new Playback(), []);
  const [selection, setSelection] = useState({ ids: [], track: null });
  const [marks, setMarks] = useState({ in: null, out: null });
  const [leftTab, setLeftTab] = useState("media");
  const [bottomTab, setBottomTab] = useState("timeline");
  const [exportOpen, setExportOpen] = useState(false);
  const [contactSheetOpen, setContactSheetOpen] = useState(false);
  const [jobsOpen, setJobsOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [transcript, setTranscript] = useState(null);
  const [bottomH, setBottomH] = useState(() => {
    try { const v = parseInt(localStorage.getItem("lumiere-bottom-h"), 10); return v ? Math.max(200, Math.min(700, v)) : 330; } catch { return 330; }
  });
  const tlApi = useRef(null);
  const textApi = useRef(null);
  const duration = useMemo(() => projectDuration(doc), [doc]);
  const mediaInfo = useMediaInfo(view.media);

  useEffect(() => { pb.configure(duration, doc.canvas.fps); }, [pb, duration, doc.canvas.fps]);
  useEffect(() => () => pb.destroy(), [pb]);
  useEffect(() => { document.title = `${view.name} · Lumiere's Hoard`; return () => { document.title = "Lumiere's Hoard"; }; }, [view.name]);

  // keep the selection valid when clips disappear
  useEffect(() => {
    const all = new Set(doc.tracks.flatMap((tr) => tr.clips.map((c) => c.id)));
    setSelection((s) => {
      const ids = s.ids.filter((id) => all.has(id));
      const track = s.track && doc.tracks.some((tr) => tr.id === s.track) ? s.track : null;
      return ids.length === s.ids.length && track === s.track ? s : { ids, track };
    });
  }, [doc]);

  // transcript of the timeline: needed for captions in the preview and for the text view
  const needTranscript = doc.captions.enabled || bottomTab === "text" || leftTab === "captions";
  const reloadTranscript = useCallback(async () => {
    try { setTranscript(await api.transcript(projectId)); } catch { /* shown as empty */ }
  }, [projectId]);
  useEffect(() => { if (needTranscript) reloadTranscript(); }, [needTranscript, view.rev, reloadTranscript]);
  useCopilotRefresh(() => { if (needTranscript) reloadTranscript(); });

  // analyses and proxies finishing change what the editor can show
  useEffect(() => jobs.onDone((job) => {
    if (["render", "preview_render", "copy_cut"].includes(job.kind)) return;
    proj.reload();
    reloadTranscript();
  }), [jobs, proj.reload, reloadTranscript]); // eslint-disable-line react-hooks/exhaustive-deps

  const ed = {
    projectId, ...proj, pb, selection, setSelection, marks, setMarks, duration, mediaInfo, transcript, reloadTranscript, tlApi, textApi,
    leftTab, setLeftTab, bottomTab, setBottomTab,
  };
  ed.actions = useActions(ed, { notify, fail, t, jobs });
  const edRef = useRef(ed);
  edRef.current = ed;

  // tell the copilot which clips are selected (it resolves them against the project it reads)
  useEffect(() => {
    const answer = (event) => event.detail?.provideSelection?.(edRef.current.selection.ids);
    window.addEventListener(CONTEXT_REQUEST, answer);
    return () => window.removeEventListener(CONTEXT_REQUEST, answer);
  }, []);

  // ------------------------------------------------------------ keyboard
  useEffect(() => {
    const onKey = (e) => {
      const cur = edRef.current;
      if (document.querySelector(".modal-back")) return;
      const target = e.target;
      if (e.key === " " && target?.tagName === "BUTTON") { e.preventDefault(); cur.pb.toggle(); return; }
      if (isTyping(target)) return;
      const ctrl = e.ctrlKey || e.metaKey;
      const k = e.key;
      const a = cur.actions;
      const fps = cur.doc.canvas.fps;
      const stop = () => e.preventDefault();
      if (ctrl) {
        if (k.toLowerCase() === "z") { stop(); if (e.shiftKey) cur.redo(); else cur.undo(); }
        else if (k.toLowerCase() === "y") { stop(); cur.redo(); }
        else if (k.toLowerCase() === "d") { stop(); a.duplicate(); }
        else if (k.toLowerCase() === "c") { stop(); a.copy(); }
        else if (k.toLowerCase() === "x") { stop(); a.cut(); }
        else if (k.toLowerCase() === "v") { stop(); a.paste(); }
        else if (k.toLowerCase() === "e") { stop(); setExportOpen(true); }
        else if (k.toLowerCase() === "a") { stop(); setSelection({ ids: cur.doc.tracks.filter((tr) => !tr.locked).flatMap((tr) => tr.clips.map((c) => c.id)), track: null }); }
        return;
      }
      switch (k) {
        case " ": stop(); cur.pb.toggle(); break;
        case "k": case "K": cur.pb.pause(); break;
        case "l": case "L": stop(); cur.pb.play(cur.pb.playing ? Math.min(8, cur.pb.rate * 2) : 1); break;
        case "j": case "J": cur.pb.seek(cur.pb.t - 5000); break;
        case "ArrowLeft": stop(); if (e.shiftKey) cur.pb.seek(cur.pb.t - 1000); else cur.pb.step(-1); break;
        case "ArrowRight": stop(); if (e.shiftKey) cur.pb.seek(cur.pb.t + 1000); else cur.pb.step(1); break;
        case "ArrowUp": case "ArrowDown": {
          stop();
          const pts = [0, cur.duration, ...cur.doc.tracks.flatMap((tr) => tr.clips.flatMap((c) => [c.start, clipEnd(c)]))].sort((x, y) => x - y);
          const now = cur.pb.t;
          const next = k === "ArrowDown" ? pts.find((p) => p > now + 1) : [...pts].reverse().find((p) => p < now - 1);
          if (next !== undefined) cur.pb.seek(next);
          break;
        }
        case "Home": stop(); cur.pb.seek(0); break;
        case "End": stop(); cur.pb.seek(cur.duration); break;
        case "s": case "S": case "c": case "C": stop(); a.split(); break;
        case "Delete": case "Backspace":
          stop();
          if (cur.bottomTab === "text" && cur.textApi.current?.hasSelection()) cur.textApi.current.cut();
          else a.remove(!e.shiftKey);
          break;
        case "i": case "I": cur.setMarks((m) => ({ ...m, in: Math.round(cur.pb.t) })); break;
        case "o": case "O": cur.setMarks((m) => ({ ...m, out: Math.round(cur.pb.t) })); break;
        case "m": case "M": a.addMarker(); break;
        case "+": case "=": cur.tlApi.current?.zoomIn(); break;
        case "-": case "_": cur.tlApi.current?.zoomOut(); break;
        case "\\": cur.tlApi.current?.fit(); break;
        case "Escape": setSelection({ ids: [], track: null }); break;
        case "?": setHelpOpen(true); break;
        default: void fps;
      }
    };
    const onKeyUp = (e) => { if (e.key === " " && e.target?.tagName === "BUTTON") e.preventDefault(); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKeyUp);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("keyup", onKeyUp); };
  }, []);

  // ------------------------------------------------------------ resizable timeline
  const [resizing, setResizing] = useState(false);
  const startResize = (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startH = bottomH;
    setResizing(true);
    const move = (ev) => setBottomH(Math.max(180, Math.min(window.innerHeight - 260, startH + (startY - ev.clientY))));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      setResizing(false);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  useEffect(() => { try { localStorage.setItem("lumiere-bottom-h", String(Math.round(bottomH))); } catch { /* storage blocked */ } }, [bottomH]);

  const Active = TABS.find((x) => x.key === leftTab)?.C || MediaPanel;

  return (
    <EditorCtx.Provider value={ed}>
      <div className="ed">
        <TopBar ed={ed} onExport={() => setExportOpen(true)} onContactSheet={() => setContactSheetOpen(true)} onJobs={() => setJobsOpen((v) => !v)} onHelp={() => setHelpOpen(true)} />
        <AnalysisBanner analysis={proj.analysis} />
        <div className="ed-main">
          <div className="ed-left">
            <nav className="ed-rail" role="tablist" aria-label={t("tools")}>
              {TABS.map((tab) => (
                <button key={tab.key} type="button" role="tab" className="rail-btn" aria-selected={leftTab === tab.key} onClick={() => setLeftTab(tab.key)} data-tab={tab.key}>
                  <Icon name={tab.icon} size={19} />
                  <span>{t(tab.label)}</span>
                </button>
              ))}
            </nav>
            <Active />
          </div>
          <Player />
          <Inspector />
        </div>
        <div className="ed-bottom" style={{ height: bottomH }}>
          <div className={`splitter${resizing ? " on" : ""}`} onPointerDown={startResize} role="separator" aria-orientation="horizontal" />
          <div className="tabs" role="tablist" style={{ padding: "0 8px", borderBottom: "1px solid var(--line)", flex: "none" }}>
            <button type="button" role="tab" className="tab" aria-selected={bottomTab === "timeline"} onClick={() => setBottomTab("timeline")} data-tab="timeline"><Icon name="clip" size={13} /> {t("tab_timeline")}</button>
            <button type="button" role="tab" className="tab" aria-selected={bottomTab === "text"} onClick={() => setBottomTab("text")} data-tab="textview"><Icon name="type" size={13} /> {t("tab_transcript")}</button>
          </div>
          {bottomTab === "timeline" ? <Timeline /> : <TextView />}
        </div>
        {jobsOpen ? <JobsDrawer onClose={() => setJobsOpen(false)} /> : null}
        {exportOpen ? <ExportDialog onClose={() => setExportOpen(false)} /> : null}
        {contactSheetOpen ? <ContactSheetDialog projectId={projectId} onClose={() => setContactSheetOpen(false)} /> : null}
        {helpOpen ? <ShortcutsDialog onClose={() => setHelpOpen(false)} /> : null}
      </div>
    </EditorCtx.Provider>
  );
}

export default function Editor({ projectId }) {
  const app = useApp();
  const { t, fail, notify, jobs } = app;
  const proj = useProject(projectId, { fail, notify, jobs, t });
  if (!proj.view && proj.loadError) {
    return <div style={{ padding: 40 }}><div className="chip chip-err" style={{ height: "auto", padding: "8px 12px" }}>{proj.loadError}</div> <a href="#/" style={{ marginLeft: 16, color: "var(--accent)" }}>{t("back")}</a></div>;
  }
  if (!proj.view) {
    return <div style={{ padding: 40, display: "flex", gap: 12, alignItems: "center" }}><Spinner /> {t("loading")} <a href="#/" style={{ marginLeft: 20, color: "var(--accent)" }}>{t("back")}</a></div>;
  }
  return <EditorInner proj={proj} />;
}
