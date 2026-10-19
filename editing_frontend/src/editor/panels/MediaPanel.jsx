import React, { useEffect, useMemo, useState } from "react";
import { api } from "../../api.js";
import { useApp } from "../../App.jsx";
import { Bar, Icon, Seg, Spinner } from "../../components/ui.jsx";
import ImportDialog from "../../components/ImportDialog.jsx";
import { DropUpload, MediaThumb, useMediaLibrary, useUploadPicker } from "../../components/Media.jsx";
import { useEd } from "../EditorContext.js";
import { PanelHead } from "./Shared.jsx";

// Other projects of the library, to drop onto this timeline as nested sequences (the server refuses loops).
function ProjectsAsClips() {
  const { t } = useApp();
  const ed = useEd();
  const [open, setOpen] = useState(false);
  const [list, setList] = useState(null);
  useEffect(() => {
    if (!open) return;
    api.projects().then((r) => setList((r.projects || []).filter((p) => p.id !== ed.projectId && !p.is_template))).catch(() => setList([]));
  }, [open, ed.projectId, ed.view.rev]);
  const add = async (p) => {
    const res = await ed.edit([{ op: "add_sequence", project: p.id }], t("lbl_add_sequence"));
    const clip = res?.results?.[0]?.clip;
    if (clip) ed.setSelection({ ids: [clip], track: null });
  };
  return (
    <div style={{ marginTop: 16 }} data-testid="projects-as-clips">
      <button type="button" className="btn btn-ghost btn-sm" style={{ paddingLeft: 0 }} aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <Icon name={open ? "chevdown" : "chevron"} size={13} /><Icon name="nest" size={13} />{t("projects_as_clips")}
      </button>
      {open ? (
        <div style={{ marginTop: 6 }}>
          <div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>{t("projects_as_clips_help")}</div>
          {!list ? <Spinner /> : list.map((p) => (
            <div key={p.id} style={{ display: "flex", alignItems: "center", gap: 6, padding: "4px 0", borderTop: "1px solid var(--line)" }}>
              <span className="ellipsis" style={{ flex: 1, minWidth: 0, fontSize: 12 }} title={p.name}>{p.name}</span>
              <span className="mono muted" style={{ fontSize: 10.5 }}>{p.duration}</span>
              <button type="button" className="btn btn-sm btn-icon" title={t("add_as_clip")} aria-label={`${t("add_as_clip")}: ${p.name}`} disabled={!p.duration_ms} onClick={() => add(p)}><Icon name="plus" size={13} /></button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function MediaPanel() {
  const { t, jobs } = useApp();
  const ed = useEd();
  const { media, refresh } = useMediaLibrary();
  const [importing, setImporting] = useState(false);
  const [kind, setKind] = useState("all");
  const [q, setQ] = useState("");
  const pickUpload = useUploadPicker(refresh);
  const list = useMemo(() => (media || []).filter((m) => (kind === "all" || m.kind === kind) && (!q || m.name.toLowerCase().includes(q.toLowerCase()))), [media, kind, q]);
  const prep = (id) => jobs.active.find((j) => j.media_id === id && j.kind === "prepare");

  return (
    <div className="ed-panel">
      <PanelHead title={t("tab_media")}>
        <button type="button" className="btn btn-sm" onClick={pickUpload} title={t("upload_files")}><Icon name="upload" size={14} />{t("upload")}</button>
        <button type="button" className="btn btn-sm btn-primary" onClick={() => setImporting(true)}><Icon name="plus" size={14} />{t("import_btn")}</button>
      </PanelHead>
      <div style={{ padding: "8px 12px 0", display: "flex", gap: 8, alignItems: "center" }}>
        <input className="field" style={{ flex: 1, minWidth: 0, width: "auto" }} placeholder={t("search")} value={q} onChange={(e) => setQ(e.target.value)} aria-label={t("search")} />
        <Seg value={kind} onChange={setKind} options={[{ value: "all", label: t("all") }, { value: "video", label: <Icon name="film" size={13} />, title: t("kind_video") }, { value: "audio", label: <Icon name="music" size={13} />, title: t("kind_audio") }, { value: "image", label: <Icon name="image" size={13} />, title: t("kind_image") }]} />
      </div>
      <DropUpload onUploaded={refresh} style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        <div className="ed-panel-body">
          {!media ? <Spinner /> : list.length === 0 ? (
            <div className="muted" style={{ textAlign: "center", padding: "30px 10px", fontSize: 12.5 }}>
              <Icon name="upload" size={26} style={{ color: "var(--dim)" }} />
              <div style={{ marginTop: 8 }}>{t("media_empty")}</div>
            </div>
          ) : (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              {list.map((m) => {
                const job = prep(m.id);
                return (
                  <div key={m.id} style={{ minWidth: 0 }}>
                    <MediaThumb m={m} onAdd={(x) => ed.actions.addMedia(x.id)}>
                      {job ? <div style={{ position: "absolute", left: 4, right: 4, bottom: 4 }}><Bar value={job.progress} /></div> : null}
                    </MediaThumb>
                    <div className="ellipsis" style={{ fontSize: 12, marginTop: 4 }} title={m.name}>{m.name}</div>
                    {job ? <div className="muted" style={{ fontSize: 10.5 }}>{t("media_preparing")} {Math.round(job.progress * 100)}% {job.detail ? `· ${job.detail}` : ""}</div> : null}
                  </div>
                );
              })}
            </div>
          )}
          <div className="muted" style={{ fontSize: 11.5, marginTop: 14 }}>{t("media_tip")}</div>
          <ProjectsAsClips />
        </div>
      </DropUpload>
      {importing ? <ImportDialog onClose={() => setImporting(false)} onImported={refresh} /> : null}
    </div>
  );
}
