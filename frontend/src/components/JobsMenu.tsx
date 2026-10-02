import { useEffect, useRef, useState } from "react";
import { Clapperboard, ListChecks, X } from "lucide-react";
import { api, type Job } from "../api";
import { useT } from "../i18n";
import { JOB_LABEL } from "../views/Jobs";
import { useApp } from "./ui";

const LIVE = ["queued", "waiting_gpu", "running"];
const elapsed = (j: Job) => {
  const s = j.started_at ? Math.max(0, (Date.now() - Date.parse(j.started_at)) / 1000) : 0;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};

/** The header's "N running" button: what is running right now, how far it
 * is, where its result goes, and a cancel - without leaving the page. */
export function JobsMenu() {
  const { t, lang } = useT();
  const app = useApp();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const live = app.jobs.filter((j) => LIVE.includes(j.state));
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);
  if (!live.length) return null;
  const label = (j: Job) => {
    const p = j.params as { slug?: string; name?: string; prompt?: string };
    const kind = JOB_LABEL[lang]?.[j.type] || j.type;
    if (j.type === "production") return `${kind} · ${p.name || p.slug || ""}`;
    return `${kind}${p.prompt ? ` · ${String(p.prompt).slice(0, 48)}` : ""}`;
  };
  const go = (j: Job) => {
    const slug = (j.params as { slug?: string }).slug;
    setOpen(false);
    if (j.type === "production" && slug) {
      window.location.hash = j.project_id ? `#/p/${j.project_id}/video/${slug}` : `#/productions/${slug}`;
    } else app.go("jobs");
  };
  return (
    <div className="jobs-menu" ref={box}>
      <button className="btn sm" onClick={() => setOpen(!open)}>
        <span className="dot warn" /> {t("activeJobs", { n: live.length })}
      </button>
      {open && (
        <div className="jobs-pop">
          {live.slice(0, 12).map((j) => (
            <div key={j.id} className="jobs-pop-item">
              <button className="jobs-pop-main" onClick={() => go(j)}>
                <div className="row" style={{ gap: 6 }}>
                  {j.type === "production" ? <Clapperboard size={13} /> : null}
                  <strong className="small ellipsis grow">{label(j)}</strong>
                  <span className="mono small muted">{j.state === "running" ? elapsed(j) : t(j.state === "waiting_gpu" ? "stateWaiting" : "stateQueued")}</span>
                </div>
                {j.message && <div className="small muted ellipsis">{j.message}</div>}
                {j.state === "running" && <div className="ns-bar"><i style={{ width: `${Math.round((j.progress || 0) * 100)}%` }} /></div>}
              </button>
              <button className="btn sm icon ghost" title={t("cancelJob")} onClick={async () => {
                try { await api.cancelJob(j.id); app.refreshJobs(); } catch (e) { app.toast((e as Error).message, "bad"); }
              }}><X size={13} /></button>
            </div>
          ))}
          <button className="btn sm ghost" style={{ width: "100%" }} onClick={() => { setOpen(false); app.go("jobs"); }}><ListChecks size={13} /> {t("jobsOpenAll")}</button>
        </div>
      )}
    </div>
  );
}
