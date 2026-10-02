import { useEffect, useState } from "react";
import { FolderPlus, Loader2, RotateCcw, Sparkles, Trash2 } from "lucide-react";
import { api, thumbUrl, type Project, type TrashedProject } from "../api";
import { useT } from "../i18n";
import { PRODUCTION_STATUS_KEY } from "../messages";
import { ConfirmButton, Empty, Modal, timeAgo, useApp } from "../components/ui";

export function ProjectsView({ projects, reload }: { projects: Project[]; reload: () => void }) {
  const { t, lang } = useT();
  const app = useApp();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [brief, setBrief] = useState("");
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [trash, setTrash] = useState<TrashedProject[]>([]);
  const loadTrash = () => api.projectTrash().then((r) => setTrash(r.items)).catch(() => undefined);
  useEffect(() => { loadTrash(); }, [projects]);
  const restore = async (p: TrashedProject) => {
    try { await api.restoreProject(p.id); app.toast(t("projRestored", { name: p.name }), "ok"); reload(); loadTrash(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const purge = async (p: TrashedProject) => {
    try {
      const out = await api.purgeProject(p.id);
      app.toast(t("projPurged", { name: p.name, n: out.assets }), "ok");
      loadTrash();
    } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const create = async () => {
    if (!name.trim()) return;
    try {
      const p = await api.createProject(name.trim(), brief.trim() || undefined);
      setCreating(false);
      setName("");
      setBrief("");
      reload();
      app.setProject(p.id);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("projectsTitle")}</h1>
          <p>{t("projectsLead")}</p>
        </div>
        <div className="actions">
          <button className="btn primary" onClick={() => setCreating(true)}><FolderPlus size={16} /> {t("newProject")}</button>
        </div>
      </div>
      {creating && (
        <div className="card stack" style={{ marginBottom: 18, maxWidth: 620 }}>
          <label className="field">{t("projectName")}
            <input value={name} onChange={(e) => setName(e.target.value)} autoFocus onKeyDown={(e) => e.key === "Enter" && create()} />
          </label>
          <label className="field">{t("brief")} <span className="hint">{t("optional")}</span>
            <textarea value={brief} onChange={(e) => setBrief(e.target.value)} placeholder={t("briefPlaceholder")} />
          </label>
          <div className="row">
            <button className="btn primary" onClick={create} disabled={!name.trim()}>{t("create")}</button>
            <button className="btn ghost" onClick={() => setCreating(false)}>{t("cancel")}</button>
          </div>
        </div>
      )}
      {projects.length === 0 && !creating ? (
        <Empty icon={<Sparkles size={34} />} text={t("noProjects")}>
          <button className="btn primary" onClick={() => setCreating(true)}><FolderPlus size={16} /> {t("newProject")}</button>
        </Empty>
      ) : (
        <div className="project-cards">
          {projects.map((p) => (
            <div key={p.id} className="project-card-wrap">
            <button className="project-card-del btn sm icon ghost" title={t("projDelete")} aria-label={t("projDelete")}
              onClick={() => setDeleting(p)}><Trash2 size={14} /></button>
            <button className="project-card" onClick={() => app.setProject(p.id)}>
              <div className="cover">
                {(p.cover_asset_id || p.auto_cover_asset_id) && <img src={thumbUrl({ id: (p.cover_asset_id || p.auto_cover_asset_id)!, thumb_path: "x", kind: "image" })} alt="" />}
              </div>
              <div className="info">
                <strong style={{ fontSize: 16 }}>{p.name}</strong>
                {p.brief && <span className="muted small" style={{ display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical", overflow: "hidden" }}>{p.brief}</span>}
                <div className="row wrap small">
                  <span className="pill">{t("assetsCount", { n: p.counts.assets })}</span>
                  <span className="pill">{t("charactersCount", { n: p.counts.characters })}</span>
                  <span className="pill">{t("timelinesCount", { n: p.counts.timelines })}</span>
                  <span className="muted" style={{ marginLeft: "auto" }}>{timeAgo(p.updated_at, lang)}</span>
                </div>
              </div>
            </button>
            </div>
          ))}
        </div>
      )}
      {trash.length > 0 && (
        <div className="card" style={{ marginTop: 22 }}>
          <h2><Trash2 size={16} /> {t("projTrashTitle")} <span className="muted small">· {trash.length}</span></h2>
          <p className="small muted" style={{ marginTop: -6 }}>{t("projTrashLead")}</p>
          <div className="stack" style={{ gap: 6 }}>
            {trash.map((p) => (
              <div key={p.id} className="row" style={{ gap: 10, padding: "6px 8px", borderRadius: 8, background: "var(--surface-2)" }}>
                <strong className="grow">{p.name}
                  <span className="small muted" style={{ fontWeight: 400 }}> · {t("assetsCount", { n: p.counts.assets })}
                    {p.productions.length ? ` · ${t("projProductionsN", { n: p.productions.length })}` : ""} · {timeAgo(p.deleted_at, lang)}</span></strong>
                <button className="btn sm" onClick={() => restore(p)}><RotateCcw size={13} /> {t("projRestore")}</button>
                <ConfirmButton onConfirm={() => purge(p)} armedLabel={t("projPurgeSure")}><Trash2 size={13} /> {t("projPurge")}</ConfirmButton>
              </div>
            ))}
          </div>
        </div>
      )}
      {deleting && <DeleteProjectModal project={deleting} onClose={() => setDeleting(null)}
        onDeleted={() => { if (app.projectId === deleting.id) app.setProject(null); setDeleting(null); reload(); loadTrash(); }} />}
    </>
  );
}

/** What goes with the project, then to the trash (restorable). */
function DeleteProjectModal({ project, onClose, onDeleted }: { project: Project; onClose: () => void; onDeleted: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [info, setInfo] = useState<Awaited<ReturnType<typeof api.projectDeletePreview>> | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.projectDeletePreview(project.id).then(setInfo).catch((e) => app.toast((e as Error).message, "bad")); }, [project.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = async () => {
    setBusy(true);
    try {
      await api.deleteProject(project.id);
      app.toast(t("projDeleted", { name: project.name }), "ok");
      onDeleted();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const live = info?.live_jobs.length || 0;
  return (
    <Modal title={t("projDeleteTitle", { name: project.name })} onClose={onClose} footer={(
      <>
        <span className="grow" />
        <button className="btn" onClick={onClose}>{t("cancel")}</button>
        <button className="btn danger" disabled={!info || busy || live > 0} onClick={go}>
          {busy ? <Loader2 size={14} className="spin" /> : <Trash2 size={14} />} {t("projDeleteGo")}</button>
      </>
    )}>
      {!info ? <p className="muted">{t("loading")}</p> : (
        <div className="stack">
          <p>{t("projDeleteLead")}</p>
          <div className="row wrap small" style={{ gap: 6 }}>
            <span className="pill">{t("assetsCount", { n: info.counts.assets })}</span>
            <span className="pill">{t("charactersCount", { n: info.counts.characters })}</span>
            <span className="pill">{t("timelinesCount", { n: info.counts.timelines })}</span>
          </div>
          {info.productions.length > 0 && (
            <div className="small">{t("projDeleteProductions")}
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {info.productions.map((x) => <li key={x.slug}><strong>{x.name || x.slug}</strong> <span className="muted">· {PRODUCTION_STATUS_KEY[x.status] ? t(PRODUCTION_STATUS_KEY[x.status]) : x.status}</span></li>)}
              </ul>
            </div>
          )}
          {live > 0 && <p className="small err-text">{t("projDeleteBusy", { n: live })}</p>}
        </div>
      )}
    </Modal>
  );
}
