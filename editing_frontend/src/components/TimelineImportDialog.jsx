import React, { useState } from "react";
import { api } from "../api.js";
import { go, useApp } from "../App.jsx";
import { Field, Modal, Spinner } from "./ui.jsx";

export default function TimelineImportDialog({ onClose }) {
  const { t, fail } = useApp();
  const [path, setPath] = useState("");
  const [title, setTitle] = useState("");
  const [format, setFormat] = useState("auto");
  const [folders, setFolders] = useState("");
  const [fps, setFps] = useState("24");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const start = async () => {
    setBusy(true);
    try {
      const body = { path: path.trim(), format, title: title.trim(), media_dirs: folders.split(/\r?\n/).map(x => x.trim()).filter(Boolean) };
      if (format === "edl" || /\.edl$/i.test(path.trim())) body.fps = Number(fps);
      setResult(await api.timelineImport(body));
    } catch (error) { fail(error); }
    finally { setBusy(false); }
  };
  return <Modal title={t("timeline_import")} onClose={onClose} width={640}
    footer={<><button type="button" className="btn" onClick={onClose}>{t("close")}</button>
      {result ? <button type="button" className="btn btn-primary" onClick={() => go(`p/${result.project_id}`)}>{t("timeline_open")}</button>
        : <button type="button" className="btn btn-primary" disabled={busy || !path.trim()} onClick={start}>{busy ? <Spinner /> : null}{t("import_btn")}</button>}</>}>
    {result ? <div data-testid="timeline-import-result">
      <p>{t("timeline_imported", { clips: result.clips, tracks: result.tracks })}</p>
      {(result.report || result.skipped || []).length ? <details><summary>{t("timeline_import_report")}</summary>
        <ul>{(result.report || result.skipped).map((row, index) => <li key={index}>{row.item || row.name || row.path}: {row.message || row.reason}</li>)}</ul>
      </details> : null}
    </div> : <>
      <Field label={t("timeline_path")}><input className="field" autoFocus value={path} onChange={e => setPath(e.target.value)} data-testid="timeline-import-path" /></Field>
      <Field label={t("name")}><input className="field" value={title} onChange={e => setTitle(e.target.value)} placeholder={t("timeline_name_auto")} /></Field>
      <Field label={t("timeline_format")}><select className="field" value={format} onChange={e => setFormat(e.target.value)}>
        <option value="auto">{t("auto")}</option><option value="otio">OpenTimelineIO (.otio)</option><option value="fcpxml">FCP7 XML (.xml)</option><option value="edl">CMX EDL (.edl)</option>
      </select></Field>
      {format === "edl" || /\.edl$/i.test(path.trim()) ? <Field label="FPS"><input className="field" type="number" min="1" max="240" step="any" value={fps} onChange={e => setFps(e.target.value)} /></Field> : null}
      <Field label={t("timeline_media_dirs")} help={t("timeline_media_dirs_help")}><textarea className="field" rows={3} value={folders} onChange={e => setFolders(e.target.value)} /></Field>
    </>}
  </Modal>;
}
