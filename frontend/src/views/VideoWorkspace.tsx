import { useEffect, useState } from "react";
import { api } from "../api";
import { useApp } from "../components/ui";
import { useT } from "../i18n";

// Share an in-flight open across React's development remount. A visit creates
// at most one workspace, and an existing project's work is always resumed.
const opening = new Map<string, Promise<string>>();
const visits = new Map<string, Promise<string>>();
let creatingProject: ReturnType<typeof api.createProject> | null = null;
export function VideoWorkspaceView() {
  const app = useApp();
  const { lang } = useT();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let alive = true;
    const open = async () => {
      const projects = await api.projects();
      let project = projects.items.find(p => p.id === app.projectId) || projects.items[0];
      if (!project) {
        if (!creatingProject) creatingProject = api.createProject(lang === "es" ? "Mis vídeos" : "My videos").finally(() => { creatingProject = null; });
        project = await creatingProject;
      }
      const referenceId = app.route.arg;
      const key = `${project.id}.${referenceId || "resume"}`;
      let pending = opening.get(key);
      if (!pending) {
        pending = (async () => {
          const source = referenceId ? await api.asset(referenceId) : null;
          if (source && source.kind !== "image") throw new Error(lang === "es" ? "Elige una imagen como referencia." : "Choose an image as reference.");
          const spaces = await api.spaces(project.id);
          let remembered: string | null = null;
          try { remembered = localStorage.getItem(`prospero.workspace.${project.id}`); } catch { /* storage optional */ }
          const existing = !referenceId && (spaces.items.find(s => s.id === remembered) || spaces.items[0]);
          if (existing) return `#/p/${project.id}/spaces/${existing.id}`;
          const space = await api.createSpace(project.id, lang === "es" ? "Mi vídeo" : "My video", "quick_video");
          if (source) {
            await api.saveSpace(space.id, { ...space.graph, nodes: space.graph.nodes.map(node => node.id === "reference" ? { ...node, data: { ...node.data, asset_ids: [source.id] } } : node) }, space.version);
          }
          return `#/p/${project.id}/spaces/${space.id}`;
        })();
        opening.set(key, pending);
        pending.finally(() => { if (opening.get(key) === pending) opening.delete(key); }).catch(() => undefined);
      }
      return pending;
    };
    // Deduplicate before the first GET too: a delayed empty project listing
    // must not create another project after the first POST has completed.
    const visitKey = `${app.projectId || "default"}.${app.route.arg || "resume"}`;
    let visit = visits.get(visitKey);
    if (!visit) {
      visit = open();
      visits.set(visitKey, visit);
      visit.finally(() => { if (visits.get(visitKey) === visit) visits.delete(visitKey); }).catch(() => undefined);
    }
    visit.then(hash => { if (alive) window.location.replace(hash); })
      .catch(e => { if (alive) setError((e as Error).message); });
    return () => { alive = false; };
  }, [app.projectId, app.route.arg, lang, attempt]);
  return <section className="workspace-opening" role={error ? "alert" : "status"}>
    <h1>{lang === "es" ? "Crear vídeo" : "Create video"}</h1>
    {error ? <><p>{error}</p><button className="btn primary" onClick={() => { setError(""); setAttempt(v => v + 1); }}>{lang === "es" ? "Reintentar" : "Retry"}</button></>
      : <p>{lang === "es" ? "Abriendo tu lienzo…" : "Opening your canvas…"}</p>}
  </section>;
}
