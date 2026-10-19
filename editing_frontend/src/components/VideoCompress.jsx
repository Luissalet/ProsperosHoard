import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Field, Modal, Spinner } from "./ui.jsx";
import { fmtBytes } from "../editor/time.js";

export default function VideoCompress({ media = [], onClose }) {
  const { t, fail } = useApp();
  const [path, setPath] = useState("");
  const [maximum, setMaximum] = useState(25);
  const [job, setJob] = useState(null);
  const [busy, setBusy] = useState(false);
  const running = job && ["queued", "running"].includes(job.state);
  useEffect(() => {
    if (!running) return undefined;
    let stopped = false, timer;
    const poll = async () => {
      try { const next = await api.job(job.id); if (!stopped) setJob(next); }
      catch (error) { if (!stopped) fail(error); }
      if (!stopped) timer = setTimeout(poll, 1000);
    };
    timer = setTimeout(poll, 500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [job?.id, running, fail]);
  const start = async (event) => {
    event.preventDefault(); setBusy(true);
    try { setJob(await api.videoCompress({ path: path.trim(), max_mb: Number(maximum) })); }
    catch (error) { fail(error); }
    finally { setBusy(false); }
  };
  return <Modal title={t("compress_title")} onClose={onClose} width={580}>
    <form onSubmit={start}>
      <p className="muted">{t("compress_help")}</p>
      {media.some((item) => item.kind === "video") && <Field label={t("compress_library")}>
        <select className="field" value={media.some((item) => item.path === path) ? path : ""} disabled={running || busy} onChange={(e) => setPath(e.target.value)}>
          <option value="">{t("compress_pick")}</option>
          {media.filter((item) => item.kind === "video").map((item) => <option key={item.id} value={item.path}>{item.name}</option>)}
        </select>
      </Field>}
      <Field label={t("compress_path")}><input className="field" required value={path} disabled={running || busy} onChange={(e) => setPath(e.target.value)} /></Field>
      <Field label={t("compress_limit")}><input className="field" type="number" min="0.001" step="any" required value={maximum} disabled={running || busy} onChange={(e) => setMaximum(e.target.value)} /></Field>
      <p className="muted">{t("compress_mb_help")}</p>
      <button className="btn btn-primary" type="submit" disabled={running || busy || !path.trim()}>{running || busy ? <Spinner /> : null}{t("compress_title")}</button>
      {running && <button className="btn" type="button" onClick={async () => { try { setJob(await api.jobCancel(job.id)); } catch (error) { fail(error); } }}>{t("cancel")}</button>}
    </form>
    {job && <div role="status" style={{ marginTop: 16 }}>
      <div>{t(`job_${job.state}`)} {running ? `${Math.round((job.progress || 0) * 100)}%` : ""}</div>
      {running && <progress aria-label={t("compress_title")} value={job.progress || 0} max="1" style={{ width: "100%" }} />}
      {job.state === "failed" && <p role="alert">{job.error}</p>}
      {job.state === "done" && job.result && <>
        <p className="num">{fmtBytes(job.result.bytes)} · {job.result.width}×{job.result.height}</p>
        <p style={{ overflowWrap: "anywhere" }}>{job.result.path}</p>
        <a className="btn" href={job.result.url} download>{t("compress_download")}</a>
      </>}
    </div>}
  </Modal>;
}
