import { useEffect, useMemo, useRef, useState } from "react";
import { AudioLines, ListChecks, X } from "lucide-react";
import { api, type Job, type Project } from "../api";
import { useT } from "../i18n";
import { AUDIO_JOB_TYPES, jobLabel, jobMessage } from "../messages";
import { ConfirmButton, Empty, JobState, Progress, timeAgo, useApp } from "../components/ui";

const ACTIVE = ["queued", "waiting_gpu", "running"];
const PAGE = 50;

export function JobsView({ projects }: { projects: Project[] }) {
  const { t, lang } = useT();
  const app = useApp();
  const [filter, setFilter] = useState("");
  // older pages come from the server with the same filter; the app-wide poll
  // (every active job + the latest ones) keeps states fresh and adds new jobs
  const [older, setOlder] = useState<Job[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const request = useRef(0);
  const names = Object.fromEntries(projects.map((p) => [p.id, p.name]));
  const matches = (j: Job) => !filter || (filter === "active" ? ACTIVE.includes(j.state) : j.state === filter);

  useEffect(() => {
    const mine = ++request.current;
    setOlder([]);
    setNext(null);
    api.jobs({ state: filter || undefined, limit: PAGE })
      .then((r) => { if (mine === request.current) { setOlder(r.items); setNext(r.next_offset); } })
      .catch((e) => { if (mine === request.current) app.toast((e as Error).message, "bad"); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  const more = async () => {
    if (next === null || loading) return;
    setLoading(true);
    const mine = request.current;
    try {
      const r = await api.jobs({ state: filter || undefined, limit: PAGE, offset: next });
      if (mine !== request.current) return;
      setOlder((xs) => [...xs, ...r.items]);
      setNext(r.next_offset);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setLoading(false);
    }
  };

  // the poll holds every active job: an older one that shows as active here but is
  // missing there has finished since, so it is tracked to fetch its final state
  const { trackJobs } = app;
  useEffect(() => {
    const live = new Set(app.jobs.map((j) => j.id));
    const stale = older.filter((j) => ACTIVE.includes(j.state) && !live.has(j.id)).map((j) => j.id);
    if (stale.length) trackJobs(stale);
  }, [older, app.jobs, trackJobs]);

  const jobs = useMemo(() => {
    const byId = new Map<string, Job>();
    for (const j of older) byId.set(j.id, j);
    for (const j of app.jobs) byId.set(j.id, j);  // the fresher copy wins
    return [...byId.values()].filter(matches)
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [older, app.jobs, filter]);

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
          <div className="table-scroll" tabIndex={0} role="region" aria-label={t("tableScroll")}><table className="list">
            <thead><tr><th>{t("tool")}</th><th>{t("state")}</th><th style={{ width: "32%" }}>{t("message")}</th><th>{t("project")}</th><th>{t("when")}</th><th /></tr></thead>
            <tbody>
              {jobs.map((j) => {
                const ids = j.outputs?.asset_ids || (j.outputs?.asset_id ? [j.outputs.asset_id] : []);
                // the outputs are bare ids: the job type says whether they are songs, voices or stems
                const audioOut = AUDIO_JOB_TYPES.includes(j.type);
                const prompt = (j.params?.positive_prompt || j.params?.prompt || j.params?.voice_id) as string | undefined;
                return (
                  <tr key={j.id}>
                    <td>
                      <strong>{jobLabel(j.type, lang)}</strong> <span className="pill">{j.lane}</span>
                      <div className="mono muted small">{j.id}</div>
                      {prompt && <div className="small muted ellipsis" style={{ maxWidth: 320 }}>{prompt}</div>}
                    </td>
                    <td style={{ minWidth: 150 }}>
                      <JobState state={j.state} />
                      {ACTIVE.includes(j.state) && <div style={{ marginTop: 8 }}><Progress value={j.progress} waiting={j.state === "waiting_gpu"} /></div>}
                    </td>
                    <td className="small">
                      <span className={j.state === "failed" ? "err-text" : ""}>{jobMessage(j.message, lang)}</span>
                      {ids.length > 0 && (
                        <div className="job-thumbs">
                          {ids.slice(0, 8).map((id) => (
                            <button key={id} onClick={() => app.openAsset(id, ids)} title={id}>
                              {audioOut
                                ? <AudioLines size={16} style={{ color: "var(--muted)" }} />
                                : <img src={`/generation/api/assets/${id}/thumb`} alt="" loading="lazy" onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />}
                            </button>
                          ))}
                        </div>
                      )}
                    </td>
                    <td className="small">{j.project_id ? names[j.project_id] || j.project_id : "-"}</td>
                    <td className="small muted nowrap">{timeAgo(j.created_at, lang)}</td>
                    <td>{ACTIVE.includes(j.state) && (
                      <ConfirmButton onConfirm={() => cancel(j.id)}><X size={13} /> {t("cancelJob")}</ConfirmButton>
                    )}</td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
        </div>
      )}
      {next !== null && (
        <div style={{ textAlign: "center", marginTop: 16 }}>
          <button className="btn" onClick={more} disabled={loading}>{t("loadMore")}</button>
        </div>
      )}
    </>
  );
}
