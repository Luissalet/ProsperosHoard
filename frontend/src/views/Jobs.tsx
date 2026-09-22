import { useState } from "react";
import { ListChecks, X } from "lucide-react";
import { api, type Project } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, Empty, JobState, Progress, timeAgo, useApp } from "../components/ui";

const LABEL: Record<string, Record<string, string>> = {
  en: { generate_image: "Generate", edit_image: "Edit", animate: "Animate", render_timeline: "Render", download_voice: "Voice download" },
  es: { generate_image: "Generar", edit_image: "Editar", animate: "Animar", render_timeline: "Renderizar", download_voice: "Descarga de voz" },
};

export function JobsView({ projects }: { projects: Project[] }) {
  const { t, lang } = useT();
  const app = useApp();
  const [filter, setFilter] = useState("");
  const names = Object.fromEntries(projects.map((p) => [p.id, p.name]));
  const jobs = app.jobs.filter((j) => !filter || (filter === "active" ? ["queued", "waiting_gpu", "running"].includes(j.state) : j.state === filter));

  const cancel = async (id: string) => {
    try {
      await api.cancelJob(id);
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  return (
    <>
      <div className="page-head">
        <div><h1>{t("jobsTitle")}</h1></div>
        <div className="actions">
          <div className="segmented">
            {[["", t("all")], ["active", t("stateRunning")], ["waiting_gpu", t("stateWaiting")], ["done", t("stateDone")], ["failed", t("stateFailed")]].map(([k, label]) => (
              <button key={k} className={filter === k ? "on" : ""} onClick={() => setFilter(k)}>{label}</button>
            ))}
          </div>
        </div>
      </div>
      {jobs.length === 0 ? <Empty icon={<ListChecks size={34} />} text={t("noJobs")} /> : (
        <div className="card" style={{ padding: 6 }}>
          <table className="list">
            <thead><tr><th>{t("tool")}</th><th>{t("state")}</th><th style={{ width: "32%" }}>{t("message")}</th><th>{t("project")}</th><th>{t("when")}</th><th /></tr></thead>
            <tbody>
              {jobs.map((j) => {
                const ids = j.outputs?.asset_ids || (j.outputs?.asset_id ? [j.outputs.asset_id] : []);
                const prompt = (j.params?.positive_prompt || j.params?.prompt || j.params?.voice_id) as string | undefined;
                return (
                  <tr key={j.id}>
                    <td>
                      <strong>{LABEL[lang]?.[j.type] || j.type}</strong> <span className="pill">{j.lane}</span>
                      <div className="mono muted small">{j.id}</div>
                      {prompt && <div className="small muted ellipsis" style={{ maxWidth: 320 }}>{prompt}</div>}
                    </td>
                    <td style={{ minWidth: 150 }}>
                      <JobState state={j.state} />
                      {["running", "waiting_gpu", "queued"].includes(j.state) && <div style={{ marginTop: 8 }}><Progress value={j.progress} waiting={j.state === "waiting_gpu"} /></div>}
                    </td>
                    <td className="small">
                      <span className={j.state === "failed" ? "err-text" : ""}>{j.message}</span>
                      {ids.length > 0 && (
                        <div className="job-thumbs">
                          {ids.slice(0, 8).map((id) => (
                            <button key={id} onClick={() => app.openAsset(id, ids)} title={id}>
                              <img src={`/api/assets/${id}/thumb`} alt="" loading="lazy" onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />
                            </button>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="small">{j.project_id ? names[j.project_id] || j.project_id : "-"}</td>
                    <td className="small muted nowrap">{timeAgo(j.created_at, lang)}</td>
                    <td>{["queued", "waiting_gpu", "running"].includes(j.state) && (
                      <ConfirmButton onConfirm={() => cancel(j.id)}><X size={13} /> {t("cancelJob")}</ConfirmButton>
                    )}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
