import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { go, useApp } from "../App.jsx";
import { ConfirmButton, Field, Icon, Modal, Spinner } from "../components/ui.jsx";
import VideoCompress from "../components/VideoCompress.jsx";
import ImportDialog from "../components/ImportDialog.jsx";
import TimelineImportDialog from "../components/TimelineImportDialog.jsx";
import CompositionsSection from "../components/CompositionsSection.jsx";
import { DropUpload, MediaThumb, useMediaLibrary, useUploadPicker } from "../components/Media.jsx";
import { SaveTemplateDialog, TemplatesSection } from "../components/Templates.jsx";
import { fmtBytes, fmtDate, fmtMs } from "../editor/time.js";
import { useCopilotForm, useCopilotRefresh } from "../copilot-hooks.js";

const NEW_PROJECT_DEFAULTS = { name: "", preset: "youtube", media: [] };
const PRESET_ORDER = ["youtube", "reels", "square", "portrait_4_5", "youtube_60", "hd720", "cinema", "youtube_4k", "shorts", "tiktok"];

export function PresetShape({ w, h }) {
  const max = 30;
  const k = max / Math.max(w, h);
  return <span style={{ display: "inline-block", width: Math.max(8, w * k), height: Math.max(8, h * k), border: "1.5px solid currentColor", borderRadius: 2 }} />;
}

function NewProject({ onClose }) {
  const { t, presets, fail } = useApp();
  const { media } = useMediaLibrary();
  const [name, setName] = useState("");
  const [preset, setPreset] = useState("youtube");
  const [picked, setPicked] = useState([]);
  const [busy, setBusy] = useState(false);
  const list = presets ? PRESET_ORDER.filter((k) => presets.canvas_presets[k]) : [];
  useCopilotForm("project-draft", { initial: NEW_PROJECT_DEFAULTS, current: { name: name.trim(), preset, media: picked } });

  const create = async () => {
    setBusy(true);
    try {
      const p = await api.projectCreate({ name: name.trim() || t("project_default_name"), preset, media: picked });
      go(`p/${p.id}`);
    } catch (e) {
      fail(e);
      setBusy(false);
    }
  };
  const toggle = (id) => setPicked((l) => (l.includes(id) ? l.filter((x) => x !== id) : [...l, id]));

  return (
    <Modal
      title={t("new_project")}
      onClose={onClose}
      width={780}
      footer={(
        <>
          <button type="button" className="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={create}>{busy ? <Spinner /> : null}{t("create")}</button>
        </>
      )}
    >
      <Field label={t("name")}>
        <input className="field" autoFocus value={name} placeholder={t("project_default_name")} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
      </Field>
      <span className="label">{t("canvas")}</span>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(170px, 1fr))", gap: 8, marginBottom: 14 }}>
        {list.map((k) => {
          const p = presets.canvas_presets[k];
          return (
            <button key={k} type="button" className="tile" aria-pressed={preset === k} onClick={() => setPreset(k)} style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <span style={{ width: 34, display: "flex", justifyContent: "center", color: preset === k ? "var(--accent)" : "var(--muted)" }}><PresetShape w={p.width} h={p.height} /></span>
              <span style={{ minWidth: 0 }}>
                <span style={{ display: "block", fontSize: 12.5, fontWeight: 600 }} className="ellipsis">{p.label}</span>
                <span className="muted num" style={{ fontSize: 11 }}>{p.width}×{p.height} · {p.fps} fps</span>
              </span>
            </button>
          );
        })}
      </div>
      <span className="label">{t("new_project_media")}</span>
      {!media ? <Spinner /> : media.length === 0 ? <div className="muted" style={{ fontSize: 12 }}>{t("media_empty_short")}</div> : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(110px, 1fr))", gap: 8, maxHeight: 200, overflow: "auto" }}>
          {media.map((m) => (
            <MediaThumb key={m.id} m={m} draggable={false} selected={picked.includes(m.id)} onClick={() => toggle(m.id)}>
              {picked.includes(m.id) ? <span className="chip chip-ok" style={{ position: "absolute", left: 4, top: 4 }}>{picked.indexOf(m.id) + 1}</span> : null}
            </MediaThumb>
          ))}
        </div>
      )}
    </Modal>
  );
}

function ProjectCard({ p, onChanged, onTemplate }) {
  const { t, fail, lang } = useApp();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(p.name);
  const save = async () => {
    setEditing(false);
    if (name.trim() && name !== p.name) {
      try { await api.projectRename(p.id, name.trim()); onChanged(); } catch (e) { fail(e); }
    } else setName(p.name);
  };
  return (
    <div className="pcard" onClick={() => !editing && go(`p/${p.id}`)} role="link" tabIndex={0} onKeyDown={(e) => e.key === "Enter" && !editing && go(`p/${p.id}`)} aria-label={p.name}>
      <div className="thumb" style={p.thumb ? { backgroundImage: `url(${p.thumb})` } : undefined}>
        {!p.thumb ? <Icon name="film" size={34} /> : null}
        <span className="dur num">{p.duration_ms ? fmtMs(p.duration_ms).replace(/\.\d+$/, "") : "0:00"}</span>
      </div>
      <div style={{ padding: "9px 11px 10px" }} onClick={(e) => editing && e.stopPropagation()}>
        {editing ? (
          <input className="field" autoFocus value={name} onChange={(e) => setName(e.target.value)} onBlur={save} onKeyDown={(e) => { if (e.key === "Enter") save(); if (e.key === "Escape") { setName(p.name); setEditing(false); } }} onClick={(e) => e.stopPropagation()} />
        ) : (
          <div className="ellipsis" style={{ fontWeight: 600, fontSize: 13.5 }} title={p.name}>{p.name}</div>
        )}
        <div className="muted num" style={{ fontSize: 11.5, marginTop: 2 }}>{p.canvas} · {t("n_clips", { n: p.clips })}</div>
        <div className="dim" style={{ fontSize: 11 }}>{fmtDate(p.updated_ts, lang)}</div>
        <div style={{ display: "flex", gap: 6, marginTop: 8 }} onClick={(e) => e.stopPropagation()}>
          <button type="button" className="btn btn-sm" onClick={() => setEditing(true)}>{t("rename")}</button>
          <button type="button" className="btn btn-sm" onClick={() => onTemplate(p)} title={t("template_save")} data-testid="project-template">{t("templates_short")}</button>
          <ConfirmButton label={t("delete")} confirmLabel={t("confirm_delete")} cancelLabel={t("cancel")} onConfirm={async () => { try { await api.projectDelete(p.id); onChanged(); } catch (e) { fail(e); } }} />
        </div>
      </div>
    </div>
  );
}

function MediaCard({ m, onChanged }) {
  const { t, fail } = useApp();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(m.name);
  const save = async () => {
    setEditing(false);
    if (name.trim() && name !== m.name) {
      try { await api.mediaPatch(m.id, { name: name.trim() }); onChanged(); } catch (e) { fail(e); }
    }
  };
  const remove = async () => {
    try {
      await api.mediaDelete(m.id, false);
      onChanged();
    } catch (e) {
      if (e.code === "media_in_use" && window.confirm(`${e.message}\n\n${t("media_force_delete")}`)) {
        try { await api.mediaDelete(m.id, true); onChanged(); } catch (e2) { fail(e2); }
      } else if (e.code !== "media_in_use") fail(e);
    }
  };
  return (
    <div>
      <MediaThumb m={m} draggable={false} />
      {editing ? (
        <input className="field" style={{ marginTop: 5 }} autoFocus value={name} onChange={(e) => setName(e.target.value)} onBlur={save} onKeyDown={(e) => e.key === "Enter" && e.target.blur()} />
      ) : (
        <div className="ellipsis" style={{ marginTop: 5, fontSize: 12.5, fontWeight: 600 }} title={m.path}>{m.name}</div>
      )}
      <div className="muted num" style={{ fontSize: 11 }}>
        {m.kind === "image" ? `${m.width}×${m.height}` : m.kind === "audio" ? t("kind_audio") : `${m.width}×${m.height}`} · {fmtBytes(m.bytes)}
      </div>
      <div style={{ display: "flex", gap: 5, marginTop: 5 }}>
        <button type="button" className="btn btn-sm" onClick={() => setEditing(true)}>{t("rename")}</button>
        <ConfirmButton label={t("delete")} confirmLabel={t("confirm_delete")} cancelLabel={t("cancel")} onConfirm={remove} />
      </div>
    </div>
  );
}

export default function Home() {
  const { t, fail } = useApp();
  const [projects, setProjects] = useState(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [compressing, setCompressing] = useState(false);
  const [importingTimeline, setImportingTimeline] = useState(false);
  const [creativeMediaId, setCreativeMediaId] = useState("");
  const [savingTemplate, setSavingTemplate] = useState(null);
  const [tplKey, setTplKey] = useState(0);
  const { media, refresh: refreshMedia } = useMediaLibrary();
  const pickUpload = useUploadPicker(refreshMedia);

  const load = useCallback(async () => {
    try { setProjects((await api.projects()).projects.filter((p) => !p.is_template)); } catch (e) { fail(e); }
  }, [fail]);
  useEffect(() => { load(); }, [load]);
  useCopilotRefresh(load);

  return (
    <div className="page">
      <div className="page-inner">
        <div className="section-title" style={{ marginTop: 0 }}>
          <h2>{t("nav_projects")}</h2>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <button type="button" className="btn" onClick={() => setImportingTimeline(true)} data-testid="timeline-import-open">{t("timeline_import")}</button>
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}><Icon name="plus" size={14} />{t("new_project")}</button>
          </div>
        </div>
        {!projects ? <Spinner /> : projects.length === 0 ? (
          <div className="panel" style={{ padding: 28, textAlign: "center" }}>
            <Icon name="film" size={34} style={{ color: "var(--dim)" }} />
            <div style={{ margin: "10px 0 14px" }} className="muted">{t("projects_empty")}</div>
            <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>{t("new_project")}</button>
          </div>
        ) : (
          <div className="grid-cards">
            {projects.map((p) => <ProjectCard key={p.id} p={p} onChanged={load} onTemplate={setSavingTemplate} />)}
          </div>
        )}

        <TemplatesSection media={media} refreshKey={tplKey} />

        <div className="section-title">
          <h2>{t("media_library")} {media ? <span className="muted" style={{ fontWeight: 400 }}>({media.length})</span> : null}</h2>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <button type="button" className="btn" onClick={() => setCompressing(true)}>{t("compress_title")}</button>
            <button type="button" className="btn" onClick={pickUpload}><Icon name="upload" size={14} />{t("upload_files")}</button>
            <button type="button" className="btn" onClick={() => setImporting(true)}><Icon name="folder" size={14} />{t("import_media")}</button>
          </div>
        </div>
        <DropUpload onUploaded={refreshMedia} style={{ minHeight: 120 }}>
          {!media ? <Spinner /> : media.length === 0 ? (
            <div className="panel muted" style={{ padding: 28, textAlign: "center" }}>{t("media_empty")}</div>
          ) : (
            <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(190px, 1fr))" }}>
              {media.map((m) => <MediaCard key={m.id} m={m} onChanged={refreshMedia} />)}
            </div>
          )}
        </DropUpload>
        <Field label={t("creative_video_source")}>
          <select className="field" value={creativeMediaId} onChange={e => setCreativeMediaId(e.target.value)}>
            <option value="">{t("creative_video_pick")}</option>
            {(media || []).filter(item => item.kind === "video").map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </Field>
        <CompositionsSection mediaId={creativeMediaId || null} onReceived={load} />
      </div>
      {creating ? <NewProject onClose={() => setCreating(false)} /> : null}
      {importingTimeline ? <TimelineImportDialog onClose={() => { setImportingTimeline(false); load(); }} /> : null}
      {savingTemplate ? <SaveTemplateDialog project={savingTemplate} onClose={() => setSavingTemplate(null)} onSaved={() => { setTplKey((k) => k + 1); load(); }} /> : null}
      {compressing ? <VideoCompress media={media || []} onClose={() => setCompressing(false)} /> : null}
      {importing ? <ImportDialog onClose={() => setImporting(false)} onImported={refreshMedia} /> : null}
    </div>
  );
}
