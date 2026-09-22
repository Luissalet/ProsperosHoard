import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity, AudioLines, Clapperboard, FolderKanban, Images, LayoutDashboard, LayoutGrid, ListChecks, Moon,
  Palette, Server, Settings as SettingsIcon, Sun, Users, Wand2,
} from "lucide-react";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "./styles.css";
import { api, type Job, type Project } from "./api";
import { I18nContext, detectLang, makeT, type Lang, type MessageKey } from "./i18n";
import { AppContext, type Route, useToasts } from "./components/ui";
import { Lightbox } from "./components/Lightbox";
import { ProjectsView } from "./views/Projects";
import { OverviewView } from "./views/Overview";
import { CastView } from "./views/Cast";
import { GenerateView } from "./views/Generate";
import { LibraryView } from "./views/Library";
import { DesignerView } from "./views/Designer";
import { AudioView } from "./views/Audio";
import { TimelineView } from "./views/Timeline";
import { BoardsView } from "./views/Boards";
import { JobsView } from "./views/Jobs";
import { BackendsView } from "./views/Backends";
import { ActivityView } from "./views/Activity";
import { SettingsView } from "./views/Settings";

const PROJECT_SECTIONS: { id: string; key: MessageKey; icon: typeof Users }[] = [
  { id: "overview", key: "navOverview", icon: LayoutDashboard },
  { id: "cast", key: "navCast", icon: Users },
  { id: "generate", key: "navGenerate", icon: Wand2 },
  { id: "library", key: "navLibrary", icon: Images },
  { id: "designer", key: "navDesigner", icon: Palette },
  { id: "audio", key: "navAudio", icon: AudioLines },
  { id: "timeline", key: "navTimeline", icon: Clapperboard },
  { id: "boards", key: "navBoards", icon: LayoutGrid },
];
const GLOBAL_SECTIONS: { id: string; key: MessageKey; icon: typeof Users }[] = [
  { id: "projects", key: "navProjects", icon: FolderKanban },
  { id: "jobs", key: "navJobs", icon: ListChecks },
  { id: "backends", key: "navBackends", icon: Server },
  { id: "activity", key: "navActivity", icon: Activity },
  { id: "settings", key: "navSettings", icon: SettingsIcon },
];
const PROJECT_IDS = new Set(PROJECT_SECTIONS.map((s) => s.id));

function parseHash(): Route {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "p" && parts[1]) return { projectId: parts[1], section: parts[2] || "overview", arg: parts[3] };
  return { projectId: null, section: parts[0] || "projects", arg: parts[1] };
}

function readStore(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writeStore(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* ignore */ }
}

export default function App() {
  const [lang, setLang] = useState<Lang>(detectLang());
  // dark by default (a creative tool); the toggle remembers the choice
  const [theme, setTheme] = useState<"dark" | "light">((readStore("prospero.theme") as "dark" | "light") || "dark");
  const [route, setRoute] = useState<Route>(parseHash());
  const [lastProject, setLastProject] = useState<string | null>(readStore("prospero.project"));
  const [projects, setProjects] = useState<Project[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [demo, setDemo] = useState(false);
  const [lightbox, setLightbox] = useState<{ id: string; list: string[] } | null>(null);
  const [dataVersion, setDataVersion] = useState(0);
  const { toast, host } = useToasts();
  const t = useMemo(() => makeT(lang), [lang]);

  useEffect(() => { document.documentElement.dataset.theme = theme; writeStore("prospero.theme", theme); }, [theme]);
  useEffect(() => { writeStore("prospero.lang", lang); document.documentElement.lang = lang; }, [lang]);
  useEffect(() => {
    const onHash = () => { setRoute(parseHash()); setLightbox(null); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const projectId = route.projectId || lastProject;
  useEffect(() => { if (route.projectId) { setLastProject(route.projectId); writeStore("prospero.project", route.projectId); } }, [route.projectId]);

  const loadProjects = useCallback(() => {
    api.projects().then((r) => setProjects(r.items)).catch(() => undefined);
  }, []);
  useEffect(loadProjects, [loadProjects, dataVersion]);
  // forget a remembered project that no longer exists
  useEffect(() => {
    if (lastProject && projects.length && !projects.some((p) => p.id === lastProject)) setLastProject(null);
  }, [projects, lastProject]);
  useEffect(() => { api.health().then((h) => setDemo(h.demo)).catch(() => undefined); }, []);

  const refreshJobs = useCallback(() => {
    api.jobs({ limit: 40 }).then((r) => setJobs(r.items)).catch(() => undefined);
  }, []);
  const active = jobs.filter((j) => ["queued", "waiting_gpu", "running"].includes(j.state));
  useEffect(() => {
    refreshJobs();
    const id = setInterval(refreshJobs, active.length ? 1200 : 5000);
    return () => clearInterval(id);
  }, [refreshJobs, active.length]);
  // when a job finishes, views that list assets reload
  const doneKey = jobs.filter((j) => j.state === "done").map((j) => j.id).slice(0, 5).join(",");
  useEffect(() => { if (doneKey) setDataVersion((v) => v + 1); }, [doneKey]);

  const go = useCallback((section: string, arg?: string) => {
    let hash: string;
    if (PROJECT_IDS.has(section)) {
      const pid = projectId || projects[0]?.id;
      if (!pid) { window.location.hash = "#/projects"; return; }
      hash = `#/p/${pid}/${section}${arg ? `/${arg}` : ""}`;
    } else hash = `#/${section}${arg ? `/${arg}` : ""}`;
    if (window.location.hash !== hash) window.location.hash = hash;
    else setRoute(parseHash());
  }, [projectId, projects]);

  const setProject = useCallback((id: string | null) => {
    if (!id) { window.location.hash = "#/projects"; return; }
    const section = PROJECT_IDS.has(route.section) ? route.section : "overview";
    window.location.hash = `#/p/${id}/${section}`;
  }, [route.section]);

  // global shortcuts: G generate, / search (Library handles focus), Esc handled by overlays
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable)) return;
      if (e.ctrlKey || e.metaKey || e.altKey || lightbox) return;
      if (e.key === "g" || e.key === "G") { e.preventDefault(); go("generate"); }
      if (e.key === "/") {
        e.preventDefault();
        if (route.section !== "library") go("library");
        setTimeout(() => document.getElementById("library-search")?.focus(), 60);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, route.section, lightbox]);

  const ctx = {
    route: { ...route, projectId }, go, projectId, setProject, toast,
    openAsset: (id: string, list?: string[]) => setLightbox({ id, list: list || [id] }),
    jobs, refreshJobs, demo, dataVersion, bump: () => setDataVersion((v) => v + 1),
  };

  // a new section starts at the top
  useEffect(() => { document.querySelector(".content")?.scrollTo({ top: 0 }); }, [route.section, route.projectId]);

  const current = projects.find((p) => p.id === projectId);
  const section = route.section;
  const allSections = [...PROJECT_SECTIONS, ...GLOBAL_SECTIONS];
  const title = allSections.find((s) => s.id === section)?.key || "navProjects";

  let view;
  const needsProject = PROJECT_IDS.has(section);
  if (needsProject && !projectId) view = <ProjectsView projects={projects} reload={loadProjects} />;
  else if (section === "overview") view = <OverviewView key={projectId} />;
  else if (section === "cast") view = <CastView key={projectId} />;
  else if (section === "generate") view = <GenerateView key={projectId} />;
  else if (section === "library") view = <LibraryView key={projectId} />;
  else if (section === "designer") view = <DesignerView key={projectId} />;
  else if (section === "audio") view = <AudioView key={projectId} />;
  else if (section === "timeline") view = <TimelineView key={projectId} />;
  else if (section === "boards") view = <BoardsView key={projectId} />;
  else if (section === "jobs") view = <JobsView projects={projects} />;
  else if (section === "backends") view = <BackendsView />;
  else if (section === "activity") view = <ActivityView />;
  else if (section === "settings") view = <SettingsView />;
  else view = <ProjectsView projects={projects} reload={loadProjects} />;

  return (
    <I18nContext.Provider value={{ t, lang }}>
      <AppContext.Provider value={ctx}>
        <div className="app">
          <aside className="sidebar">
            <div className="brand">
              <div className="brand-mark"><img src="/favicon-192.png" alt="" width={28} height={28} /></div>
              <div>
                <div className="brand-name">{t("appName")}</div>
                <div className="brand-sub">{t("tagline")}</div>
              </div>
            </div>
            <div className="project-switch">
              <select value={projectId || ""} onChange={(e) => setProject(e.target.value || null)} aria-label={t("navProjects")}>
                <option value="">{t("noProject")}</option>
                {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </div>
            <nav className="nav-group">
              <div className="nav-label">{t("studio")}</div>
              {PROJECT_SECTIONS.map((s) => (
                <button key={s.id} className={`nav-item${section === s.id && projectId ? " active" : ""}`} disabled={!projectId && !projects.length}
                  onClick={() => go(s.id)}>
                  <s.icon size={17} /> {t(s.key)}
                  {s.id === "library" && current && <span className="count">{current.counts.assets}</span>}
                  {s.id === "cast" && current && <span className="count">{current.counts.characters}</span>}
                </button>
              ))}
            </nav>
            <nav className="nav-group">
              <div className="nav-label">{t("global")}</div>
              {GLOBAL_SECTIONS.map((s) => (
                <button key={s.id} className={`nav-item${section === s.id ? " active" : ""}`} onClick={() => go(s.id)}>
                  <s.icon size={17} /> {t(s.key)}
                  {s.id === "jobs" && active.length > 0 && <span className="pill accent">{active.length}</span>}
                </button>
              ))}
            </nav>
            <div className="sidebar-foot">
              <div className="toggles">
                <div className="segmented" aria-label={t("language")}>
                  <button className={lang === "es" ? "on" : ""} onClick={() => setLang("es")}>ES</button>
                  <button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>EN</button>
                </div>
                <button className="btn sm icon ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                  title={theme === "dark" ? t("themeLight") : t("themeDark")}>
                  {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
                </button>
              </div>
              <div className="muted small">{t("shortcuts")}</div>
            </div>
          </aside>
          <main className="main">
            <header className="topbar">
              <div className="crumbs">
                {needsProject && current && <><span className="ellipsis">{current.name}</span><span>/</span></>}
                <strong>{t(title)}</strong>
              </div>
              <div className="spacer" />
              {demo && <span className="pill gold" title={t("demoHint")}>{t("demoBadge")}</span>}
              {active.length > 0 && (
                <button className="btn sm" onClick={() => go("jobs")}>
                  <span className="dot warn" /> {t("activeJobs", { n: active.length })}
                </button>
              )}
            </header>
            <div className="content">{view}</div>
          </main>
        </div>
        {lightbox && <Lightbox assetId={lightbox.id} list={lightbox.list} onClose={() => setLightbox(null)}
          onNavigate={(id) => setLightbox({ ...lightbox, id })} />}
        {host}
      </AppContext.Provider>
    </I18nContext.Provider>
  );
}
