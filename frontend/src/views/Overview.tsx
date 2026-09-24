import { useState } from "react";
import { CheckCircle2, Circle, Pencil } from "lucide-react";
import { api, ENGINE_NAMES, fileUrl, IMAGE_ENGINES, thumbUrl, type ImageEngine } from "../api";
import { useT, type MessageKey } from "../i18n";
import { AssetTile, ErrorNote, useApp, useAsync } from "../components/ui";

export function OverviewView() {
  const { t } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const project = useAsync(() => api.project(pid), [pid, app.dataVersion]);
  const recent = useAsync(() => api.assets(pid, { limit: 12 }), [pid, app.dataVersion]);
  const cast = useAsync(() => api.characters(pid), [pid, app.dataVersion]);
  const timelines = useAsync(() => api.timelines(pid), [pid, app.dataVersion]);
  const [editing, setEditing] = useState(false);
  const [brief, setBrief] = useState("");
  const [savingEngine, setSavingEngine] = useState(false);

  const saveBrief = async () => {
    try {
      await api.updateProject(pid, { brief });
      setEditing(false);
      project.reload();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };
  const saveEngine = async (engine: ImageEngine) => {
    setSavingEngine(true);
    try {
      await api.updateProject(pid, { image_engine: engine });
      app.toast(t("saved"), "ok");
      project.reload();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setSavingEngine(false);
    }
  };

  const p = project.data;
  if (!p) return project.error ? <ErrorNote error={project.error} onRetry={project.reload} /> : <p className="muted">{t("loading")}</p>;
  const c = p.counts;
  const chars = cast.data?.items || [];
  const renders = (recent.data?.items || []).filter((a) => a.kind === "video" && a.source === "rendered");
  const steps: [MessageKey, boolean, string][] = [
    ["stepCast", c.characters > 0, "cast"],
    ["stepGenerate", chars.length > 0 && chars.every((x) => x.canonical_asset_id), "generate"],
    ["stepCards", (recent.data?.items || []).some((a) => a.source === "rendered" && a.kind === "image"), "designer"],
    ["stepSong", c.audio > 0, "audio"],
    ["stepCut", (timelines.data?.items || []).length > 0 && renders.length > 0, "timeline"],
  ];

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{p.name}</h1>
          {!editing && <p>{p.brief}</p>}
        </div>
        <div className="actions">
          {!editing && <button className="btn sm ghost" onClick={() => { setBrief(p.brief || ""); setEditing(true); }}><Pencil size={14} /> {t("editBrief")}</button>}
        </div>
      </div>
      {editing && (
        <div className="card stack" style={{ marginBottom: 18 }}>
          <textarea value={brief} onChange={(e) => setBrief(e.target.value)} rows={3} />
          <div className="row">
            <button className="btn primary sm" onClick={saveBrief}>{t("save")}</button>
            <button className="btn ghost sm" onClick={() => setEditing(false)}>{t("cancel")}</button>
          </div>
        </div>
      )}
      <div className="overview-grid">
        <div className="stack">
          <div className="card" style={{ padding: 12 }}>
            {p.cover_asset_id ? (
              <img className="cover-img" src={fileUrl(p.cover_asset_id)} alt={t("overviewCover")} onClick={() => app.openAsset(p.cover_asset_id!)} style={{ cursor: "zoom-in" }} />
            ) : <div className="cover-img" style={{ background: "linear-gradient(135deg, var(--accent-soft), var(--gold-soft))" }} />}
          </div>
          <div className="card">
            <h2>{t("overviewNext")}</h2>
            <ul className="steps">
              {steps.map(([key, done, section]) => (
                <li key={key} className={done ? "done" : ""} onClick={() => app.go(section)} style={{ cursor: "pointer" }}>
                  {done ? <CheckCircle2 size={17} /> : <Circle size={17} />} {t(key)}
                </li>
              ))}
            </ul>
          </div>
          <div className="card stack">
            <h2>{t("projectSettings")}</h2>
            <label className="field">{t("imageEngine")}
              <select value={p.image_engine || "auto"} disabled={savingEngine} onChange={(e) => saveEngine(e.target.value as ImageEngine)}>
                {IMAGE_ENGINES.map((x) => <option key={x} value={x}>{x === "auto" ? t("engineAuto") : ENGINE_NAMES[x]}</option>)}
              </select>
              <span className="hint">{t("imageEngineHint")}</span>
            </label>
          </div>
        </div>
        <div className="stack">
          <div className="stat-row">
            <div className="stat"><b>{c.images}</b><span>{t("statImages")}</span></div>
            <div className="stat"><b>{c.videos}</b><span>{t("statVideos")}</span></div>
            <div className="stat"><b>{c.characters}</b><span>{t("navCast")}</span></div>
            <div className="stat"><b>{c.timelines}</b><span>{t("navTimeline")}</span></div>
          </div>
          <div className="card">
            <h2>{t("overviewCast")} <div className="card-actions"><button className="btn sm ghost" onClick={() => app.go("cast")}>{t("open")}</button></div></h2>
            <div className="row wrap" style={{ gap: 14 }}>
              {chars.map((ch) => (
                <button key={ch.id} className="row" style={{ background: "none", border: 0, cursor: "pointer", gap: 10, padding: 0 }}
                  onClick={() => ch.canonical_asset_id && app.openAsset(ch.canonical_asset_id)}>
                  {ch.canonical_asset_id
                    ? <img src={thumbUrl({ id: ch.canonical_asset_id, thumb_path: "x", kind: "image" })} alt="" style={{ width: 44, height: 44, borderRadius: "50%", objectFit: "cover", border: `2px solid ${ch.palette[0] || "var(--accent)"}` }} />
                    : <span style={{ width: 44, height: 44, borderRadius: "50%", background: ch.palette[0] || "var(--surface-3)" }} />}
                  <span style={{ textAlign: "left" }}><strong>{ch.name}</strong><br /><span className="muted small">{ch.role}</span></span>
                </button>
              ))}
            </div>
          </div>
          {renders[0] && (
            <div className="card">
              <h2>{t("renderReady")}</h2>
              <div className="video-frame" style={{ maxWidth: 250 }}>
                <video src={fileUrl(renders[0].id)} controls muted loop preload="metadata" poster={thumbUrl(renders[0])} />
              </div>
            </div>
          )}
          <div className="card">
            <h2>{t("overviewRecent")} <div className="card-actions"><button className="btn sm ghost" onClick={() => app.go("library")}>{t("open")}</button></div></h2>
            <div className="thumb-grid">
              {(recent.data?.items || []).map((a) => (
                <AssetTile key={a.id} asset={a} square onClick={() => app.openAsset(a.id, recent.data!.items.map((x) => x.id))} />
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
