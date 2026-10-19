import React, { useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Bar, Icon } from "./ui.jsx";

const chip = { queued: "chip", running: "chip chip-info", done: "chip chip-ok", failed: "chip chip-err", canceled: "chip chip-warn" };

export default function JobsDrawer({ onClose }) {
  const { t, fail, jobs } = useApp();
  const [recent, setRecent] = useState([]);
  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const { jobs: list } = await api.jobs({ limit: 30 });
        if (live) setRecent(list);
      } catch { /* retry on the next tick */ }
    };
    load();
    const timer = setInterval(load, 1500);
    return () => { live = false; clearInterval(timer); };
  }, [jobs.doneTick]);
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose?.(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const cancel = async (id) => {
    try { await api.jobCancel(id); jobs.poke(); } catch (e) { fail(e); }
  };
  return (
    <aside className="drawer" aria-label={t("jobs_title")}>
      <div className="ed-panel-head">
        <h3 style={{ fontSize: 13.5 }}>{t("jobs_title")}</h3>
        <button type="button" className="btn btn-ghost btn-icon btn-sm" onClick={onClose} aria-label="×"><Icon name="x" /></button>
      </div>
      <div className="ed-panel-body">
        {recent.length === 0 ? <div className="muted" style={{ textAlign: "center", padding: 20 }}>{t("jobs_empty")}</div> : null}
        {recent.map((j) => {
          const live = j.state === "queued" || j.state === "running";
          return (
            <div key={j.id} className="step" data-job={j.id}>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span className="ellipsis" style={{ flex: 1, fontWeight: 600, fontSize: 12.5 }} title={j.label}>{j.label || j.kind}</span>
                <span className={chip[j.state]}>{t(`job_${j.state}`)}</span>
              </div>
              {live ? <div style={{ margin: "6px 0 2px" }}><Bar value={j.progress} /></div> : null}
              <div className="muted" style={{ fontSize: 11.5, display: "flex", justifyContent: "space-between", gap: 8 }}>
                <span className="ellipsis">{j.error || j.detail || j.kind}{live ? ` · ${Math.round(j.progress * 100)}%` : ""}</span>
                {live ? <button type="button" className="btn btn-sm btn-ghost" style={{ height: 20 }} onClick={() => cancel(j.id)}>{t("cancel")}</button> : null}
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
