import { useState } from "react";
import { FolderPlus, Sparkles } from "lucide-react";
import { api, thumbUrl, type Project } from "../api";
import { useT } from "../i18n";
import { Empty, timeAgo, useApp } from "../components/ui";

export function ProjectsView({ projects, reload }: { projects: Project[]; reload: () => void }) {
  const { t, lang } = useT();
  const app = useApp();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [brief, setBrief] = useState("");

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
            <button key={p.id} className="project-card" onClick={() => app.setProject(p.id)}>
              <div className="cover">
                {p.cover_asset_id && <img src={thumbUrl({ id: p.cover_asset_id, thumb_path: "x", kind: "image" })} alt="" />}
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
          ))}
        </div>
      )}
    </>
  );
}
