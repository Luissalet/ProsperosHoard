import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity, AudioLines, Menu, Clapperboard, Film, FolderKanban, Images, LayoutDashboard, LayoutGrid, ListChecks, Mic2, Moon,
  Palette, Server, Settings as SettingsIcon, Sun, Users, Wand2,
} from "lucide-react";
import "@fontsource-variable/space-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "./styles.css";
import { api, type ApiError, type Job, type Paged, type Project } from "./api";
import { I18nContext, detectLang, makeT, type Lang, type MessageKey } from "./i18n";
import { AppContext, ErrorBoundary, type Route, useToasts } from "./components/ui";
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
import { VoiceView } from "./views/Voice";
import { ProductionsView } from "./views/Productions";

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
  { id: "productions", key: "navProductions", icon: Film },
  { id: "voice", key: "navVoice", icon: Mic2 },
  { id: "jobs", key: "navJobs", icon: ListChecks },
  { id: "backends", key: "navBackends", icon: Server },
  { id: "activity", key: "navActivity", icon: Activity },
  { id: "settings", key: "navSettings", icon: SettingsIcon },
];
const PROJECT_IDS = new Set(PROJECT_SECTIONS.map((s) => s.id));
const ACTIVE_STATES: string[] = ["queued", "waiting_gpu", "running"];

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
  const [navOpen, setNavOpen] = useState(false);
  const { toast, host } = useToasts();
  const t = useMemo(() => makeT(lang), [lang]);
  const i18n = useMemo(() => ({ t, lang }), [t, lang]);

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

  // Jobs: every active job (paged), the most recent ones, and any job a view
  // tracks (a render, an audiobook...) even once it is older than both.
  const tracked = useRef<Set<string>>(new Set());
  const trackedCache = useRef<Map<string, Job>>(new Map());
  const pollSeq = useRef(0);
  const jobsSig = useRef("");
  const refreshJobs = useCallback(() => {
    const seq = ++pollSeq.current;
    (async () => {
      const activeItems: Job[] = [];
      let offset: number | null = 0;
      for (let page = 0; page < 4 && offset !== null; page++) {
        const r: Paged<Job> = await api.jobs({ state: "active", limit: 50, offset });
        activeItems.push(...r.items);
        offset = r.next_offset;
      }
      const recent = await api.jobs({ limit: 50 });
      const byId = new Map<string, Job>();
      for (const j of [...recent.items, ...activeItems]) byId.set(j.id, j);
      const missing = [...tracked.current].filter((id) => {
        if (byId.has(id)) return false;
        const cached = trackedCache.current.get(id);
        return !cached || ACTIVE_STATES.includes(cached.state);
      });
      const fetched = await Promise.all(missing.map((id) => api.job(id).catch((e) => {
        if ((e as ApiError).status === 404) tracked.current.delete(id);
        return null;
      })));
      for (const j of fetched) if (j) trackedCache.current.set(j.id, j);
      for (const id of tracked.current) {
        const cached = trackedCache.current.get(id);
        if (byId.has(id)) trackedCache.current.set(id, byId.get(id)!);
        else if (cached) byId.set(id, cached);
      }
      if (seq !== pollSeq.current) return;  // a newer poll already answered
      const list = [...byId.values()].sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id));
      const sig = list.map((j) => `${j.id}:${j.state}:${j.progress}:${j.message}:${j.finished_at}`).join("|");
      if (sig !== jobsSig.current) { jobsSig.current = sig; setJobs(list); }
    })().catch(() => undefined);
  }, []);
  const trackJobs = useCallback((ids: (string | null | undefined)[]) => {
    for (const id of ids) if (id) tracked.current.add(id);
    // keep the set bounded: the oldest tracked ids go first
    while (tracked.current.size > 200) {
      const first = tracked.current.values().next().value as string;
      tracked.current.delete(first);
      trackedCache.current.delete(first);
    }
  }, []);
  const active = useMemo(() => jobs.filter((j) => ACTIVE_STATES.includes(j.state)), [jobs]);
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

  const openAsset = useCallback((id: string, list?: string[]) => setLightbox({ id, list: list || [id] }), []);
  const bump = useCallback(() => setDataVersion((v) => v + 1), []);
  // one object per real change, so consumers' effects are not re-run on every render
  const ctx = useMemo(() => ({
    route: { ...route, projectId }, go, projectId, setProject, toast, openAsset,
    jobs, refreshJobs, trackJobs, demo, dataVersion, bump,
  }), [route, projectId, go, setProject, toast, openAsset, jobs, refreshJobs, trackJobs, demo, dataVersion, bump]);

  // a new section starts at the top (and the narrow-screen menu closes)
  useEffect(() => {
    document.querySelector(".content")?.scrollTo({ top: 0 });
    setNavOpen(false);
  }, [route.section, route.projectId]);

  // a file dropped outside a drop zone must not make the browser navigate away
  useEffect(() => {
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types || []).includes("Files");
    const onOver = (e: DragEvent) => {
      if (!hasFiles(e) || e.defaultPrevented) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "none";
    };
    const onDrop = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
    window.addEventListener("dragover", onOver);
    window.addEventListener("drop", onDrop);
    return () => { window.removeEventListener("dragover", onOver); window.removeEventListener("drop", onDrop); };
  }, []);

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
  else if (section === "voice") view = <VoiceView />;
  else if (section === "productions") view = <ProductionsView key={route.arg || ""} />;
  else if (section === "jobs") view = <JobsView projects={projects} />;
  else if (section === "backends") view = <BackendsView />;
  else if (section === "activity") view = <ActivityView />;
  else if (section === "settings") view = <SettingsView />;
  else view = <ProjectsView projects={projects} reload={loadProjects} />;

  return (
    <I18nContext.Provider value={i18n}>
      <AppContext.Provider value={ctx}>
        <div className={`app${navOpen ? " nav-open" : ""}`}>
          <aside className="sidebar" id="app-sidebar">
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
                  onClick={() => { setNavOpen(false); go(s.id); }}>
                  <s.icon size={17} /> {t(s.key)}
                  {s.id === "library" && current && <span className="count">{current.counts.assets}</span>}
                  {s.id === "cast" && current && <span className="count">{current.counts.characters}</span>}
                </button>
              ))}
            </nav>
            <nav className="nav-group">
              <div className="nav-label">{t("global")}</div>
              {GLOBAL_SECTIONS.map((s) => (
                <button key={s.id} className={`nav-item${section === s.id ? " active" : ""}`} onClick={() => { setNavOpen(false); go(s.id); }}>
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
              <button className="btn sm icon ghost nav-toggle" onClick={() => setNavOpen(!navOpen)} aria-label={t("menu")} title={t("menu")}
                aria-expanded={navOpen} aria-controls="app-sidebar">
                <Menu size={17} />
              </button>
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
            <div className="content"><ErrorBoundary key={`${section}/${projectId || ""}/${route.arg || ""}`}>{view}</ErrorBoundary></div>
          </main>
          {navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} />}
        </div>
        {lightbox && <Lightbox assetId={lightbox.id} list={lightbox.list} onClose={() => setLightbox(null)}
          onNavigate={(id) => setLightbox({ ...lightbox, id })} />}
        {host}
      </AppContext.Provider>
    </I18nContext.Provider>
  );
}
