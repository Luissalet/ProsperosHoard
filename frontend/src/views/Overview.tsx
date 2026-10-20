import { useState } from "react";
import { Clapperboard, Image as ImageIcon, Pencil, Plus, ArrowRight, Film } from "lucide-react";
import { api, fileUrl } from "../api";
import { useT } from "../i18n";
import { AssetPicker, AssetTile, useApp, useAsync } from "../components/ui";
import { ProjectCast } from "../components/ProjectCast";
import { ProductionCard } from "./Productions";
import { VideoModal } from "./VideoModal";

export function OverviewView() {
  const { t, lang } = useT();
  const es = lang === "es", app = useApp(), pid = app.projectId!;
  const project = useAsync(() => api.project(pid), [pid, app.dataVersion]);
  const recent = useAsync(() => api.assets(pid, { limit: 12 }), [pid, app.dataVersion]);
  const productions = useAsync(() => api.productions(), [pid, app.dataVersion]);
  const [newVideo, setNewVideo] = useState(false), [editing, setEditing] = useState(false), [pickCover, setPickCover] = useState(false);
  const [brief, setBrief] = useState(""), [saving, setSaving] = useState(false), [error, setError] = useState("");
  const p = project.data;
  if (project.error) return <div role="alert">{project.error}<button className="btn" onClick={project.reload}>{es ? "Reintentar" : "Retry"}</button></div>;
  if (!p) return <p className="muted">{t("loading")}</p>;
  const videos = (productions.data?.items || []).filter(video => video.project_id === pid);
  const cover = p.cover_asset_id || p.auto_cover_asset_id;
  const saveBrief = async () => {
    setSaving(true); setError("");
    try { await api.updateProject(pid, { brief }); setEditing(false); app.bump(); }
    catch (err) { setError((err as Error).message); }
    finally { setSaving(false); }
  };
  return <div className="project-dashboard">
    <header className="project-dashboard-head">
      <div className="project-dashboard-cover"><button className="project-cover-preview" onClick={() => cover ? app.openAsset(cover) : setPickCover(true)} aria-label={t("overviewCover")}>
        {cover ? <img src={fileUrl(cover)} alt="" /> : <ImageIcon size={28} />}</button><button className="btn sm icon ghost" onClick={() => setPickCover(true)} aria-label={t("overviewPickCover")}><Pencil size={13} /></button></div>
      <div className="project-dashboard-identity"><h1>{p.name}</h1><p>{p.brief}</p><div className="project-resource-links">
        <button onClick={() => app.go("library")}>{p.counts.images} {t("statImages")}</button><button onClick={() => app.go("library")}>{p.counts.videos} {es ? "clips y vídeos" : "clips and videos"}</button><button onClick={() => app.go("audio")}>{p.counts.audio} {es ? "audios" : "audio files"}</button><button onClick={() => app.go("timeline")}>{p.counts.timelines} {es ? "montajes" : "edits"}</button><button onClick={() => { setBrief(p.brief || ""); setEditing(!editing); }}><Pencil size={12} />{t("editBrief")}</button></div></div>
      <div className="project-dashboard-actions"><button className="btn primary" onClick={() => app.go("generate", "clip")}><Film size={17} />{es ? "Crear clip" : "Create clip"}</button><button className="btn" onClick={() => setNewVideo(true)}><Plus size={16} />{t("newVideo")}</button></div>
    </header>
    {editing && <section className="dashboard-brief-editor"><label className="field">{t("brief")}<textarea value={brief} rows={3} disabled={saving} onChange={event => setBrief(event.target.value)} /></label>{error && <p role="alert" className="err-text">{error}</p>}<div className="row"><button className="btn primary" disabled={saving} onClick={saveBrief}>{t("save")}</button><button className="btn ghost" disabled={saving} onClick={() => setEditing(false)}>{t("cancel")}</button></div></section>}
    <div className="project-dashboard-columns"><div className="project-dashboard-work">
      <section className="dashboard-section project-videos"><div className="dashboard-section-head"><h2><Clapperboard size={18} />{es ? "Vídeos del proyecto" : "Project videos"}</h2><span className="spacer" /><button className="btn sm ghost" onClick={() => app.go("video")}>{es ? "Todos los vídeos" : "All videos"}<ArrowRight size={14} /></button></div>
        {productions.error && <p role="alert">{productions.error}<button className="btn sm" onClick={productions.reload}>{es ? "Reintentar" : "Retry"}</button></p>}
        {productions.loading && !productions.data ? <div className="dashboard-loading" aria-label={t("loading")} /> : videos.length ? <div className="dashboard-video-list">{videos.slice(0, 6).map(video => <ProductionCard key={video.slug} p={video} onOpen={() => app.go("video", video.slug)} />)}</div> : <div className="dashboard-empty"><p>{es ? "Crea un videoclip para organizar sus planos, canción y montaje." : "Create a music video to organize its shots, song and edit."}</p><button className="btn" onClick={() => setNewVideo(true)}><Plus size={15} />{t("newVideo")}</button></div>}
      </section>
      <section className="dashboard-section"><div className="dashboard-section-head"><h2>{t("overviewRecent")}</h2><span className="spacer" /><button className="btn sm ghost" onClick={() => app.go("library")}>{es ? "Ver biblioteca" : "View library"}<ArrowRight size={14} /></button></div>
        {recent.error && <p role="alert">{recent.error}<button className="btn sm" onClick={recent.reload}>{es ? "Reintentar" : "Retry"}</button></p>}
        <div className="dashboard-recent">{(recent.data?.items || []).map(asset => <AssetTile key={asset.id} asset={asset} square onClick={() => app.openAsset(asset.id, recent.data!.items.map(item => item.id))} />)}</div>
        {!recent.loading && !recent.data?.items.length && <p className="hint">{es ? "Las imágenes, clips y audios que crees aparecerán aquí." : "Images, clips and audio you create will appear here."}</p>}
      </section>
    </div><aside className="project-dashboard-side"><ProjectCast />
      <section className="dashboard-section"><h2>{es ? "Crear en este proyecto" : "Create in this project"}</h2><div className="dashboard-shortcuts"><button className="btn" onClick={() => app.go("generate", "clip")}><Film size={16} />{es ? "Estudio de clips" : "Clip studio"}<ArrowRight size={15} /></button><button className="btn" onClick={() => app.go("spaces")}>{es ? "Lienzos y nodos" : "Canvases and nodes"}<ArrowRight size={15} /></button><button className="btn" onClick={() => app.go("audio")}>{es ? "Canción, letra y voz" : "Song, lyrics and voice"}<ArrowRight size={15} /></button><button className="btn" onClick={() => app.go("designer")}>{t("navDesigner")}<ArrowRight size={15} /></button><button className="btn" onClick={() => app.go("timeline")}>{t("navTimeline")}<ArrowRight size={15} /></button></div></section>
    </aside></div>
    {pickCover && <AssetPicker projectId={pid} kind="image" title={t("overviewPickCover")} onClose={() => setPickCover(false)} onPick={async asset => { try { await api.updateProject(pid, { cover_asset_id: asset.id }); setPickCover(false); app.bump(); } catch (err) { app.toast((err as Error).message, "bad"); } }} />}
    {newVideo && <VideoModal projectId={pid} onClose={() => setNewVideo(false)} onStarted={slug => { setNewVideo(false); app.go("video", slug); }} />}
  </div>;
}
