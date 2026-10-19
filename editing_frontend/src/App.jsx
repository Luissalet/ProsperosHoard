import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api.js";
import { initialLang, makeT, saveLang } from "./i18n.js";
import { Icon, Logo } from "./components/ui.jsx";
import { useCopilotRefresh } from "./copilot-hooks.js";
import Home from "./pages/Home.jsx";
import Settings from "./pages/Settings.jsx";
import Editor from "./pages/Editor.jsx";

const AppContext = createContext(null);
export const useApp = () => useContext(AppContext);

export const go = (path) => { window.location.hash = `#/${path}`; };

function useHashRoute() {
  const read = () => {
    const [path] = window.location.hash.replace(/^#\/?/, "").split("?");
    const parts = path.split("/");
    return { page: parts[0] || "home", id: parts[1] || null };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onChange = () => setRoute(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

// Jobs: poll the active ones (fast while any runs, slow otherwise) and tell the UI when one finishes.
function useJobs(notify, t) {
  const [active, setActive] = useState([]);
  const [doneTick, setDoneTick] = useState(0);
  const prev = useRef(new Map());
  const listeners = useRef(new Set());
  const emitted = useRef(new Set());
  const tRef = useRef(t);
  tRef.current = t;

  const emitDone = useCallback((job) => {
    if (!job || emitted.current.has(job.id)) return;
    emitted.current.add(job.id);
    if (job.state === "failed") notify(`${job.label || job.kind}: ${job.error || "error"}`, "error");
    else if (job.state === "done" && ["render", "preview_render", "copy_cut", "stabilize", "plan_apply"].includes(job.kind)) notify(`${job.label || job.kind} ✓`, "ok");
    for (const fn of listeners.current) {
      try { fn(job); } catch { /* a listener must not break the others */ }
    }
    setDoneTick((n) => n + 1);
  }, [notify]);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    const loop = async () => {
      let next = 6000;
      try {
        const { jobs } = await api.jobs({ state: "active" });
        if (stopped) return;
        setActive(jobs);
        const ids = new Set(jobs.map((j) => j.id));
        for (const id of prev.current.keys()) {
          if (!ids.has(id)) api.job(id).then(emitDone).catch(() => {});
        }
        prev.current = new Map(jobs.map((j) => [j.id, j]));
        if (jobs.length) next = 1500;
      } catch {
        next = 6000;
      }
      if (!stopped) timer = setTimeout(loop, next);
    };
    loop();
    return () => { stopped = true; clearTimeout(timer); };
  }, [emitDone]);

  const poke = useCallback(async () => {
    try {
      const { jobs } = await api.jobs({ state: "active" });
      setActive(jobs);
      prev.current = new Map([...prev.current, ...jobs.map((j) => [j.id, j])]);
    } catch { /* next poll will do */ }
  }, []);

  useCopilotRefresh(poke);  // the agent may have started a render or an analysis

  // Follow one job to its end (own polling, so jobs shorter than the global interval are not missed).
  const watch = useCallback((jobId, onProgress) => new Promise((resolve) => {
    let stop = false;
    const step = async () => {
      if (stop) return;
      try {
        const job = await api.job(jobId);
        onProgress?.(job);
        if (["done", "failed", "canceled"].includes(job.state)) {
          emitDone(job);
          resolve(job);
          return;
        }
      } catch {
        // transient; try again
      }
      setTimeout(step, 700);
    };
    step();
    return () => { stop = true; };
  }), [emitDone]);

  const onDone = useCallback((fn) => {
    listeners.current.add(fn);
    return () => listeners.current.delete(fn);
  }, []);

  return useMemo(() => ({ active, doneTick, watch, onDone, poke }), [active, doneTick, watch, onDone, poke]);
}

export function Toasts({ items, dismiss }) {
  return (
    <div className="toast-stack" aria-live="polite">
      {items.map((x) => (
        <div key={x.id} className={`toast ${x.kind === "error" ? "toast-error" : x.kind === "ok" ? "toast-ok" : ""}`} role="status" onClick={() => dismiss(x.id)}>
          {x.message}
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const route = useHashRoute();
  const [lang, setLang] = useState(initialLang);
  const t = useMemo(() => makeT(lang), [lang]);
  const [toasts, setToasts] = useState([]);
  const [presets, setPresets] = useState(null);
  const seq = useRef(0);

  useEffect(() => { document.documentElement.lang = lang; }, [lang]);

  const dismiss = useCallback((id) => setToasts((l) => l.filter((x) => x.id !== id)), []);
  const notify = useCallback((message, kind = "info") => {
    const id = ++seq.current;
    setToasts((l) => [...l.slice(-3), { id, message: String(message), kind }]);
    setTimeout(() => dismiss(id), kind === "error" ? 8000 : 4500);
  }, [dismiss]);
  const fail = useCallback((e) => notify(e?.code === "network" ? t("network_error") : e?.message || String(e), "error"), [notify, t]);

  const jobs = useJobs(notify, t);

  useEffect(() => {
    api.presets().then(setPresets).catch(fail);
  }, [fail]);

  const switchLang = () => {
    const next = lang === "es" ? "en" : "es";
    saveLang(next);
    setLang(next);
  };

  const value = useMemo(() => ({ t, lang, notify, fail, presets, jobs, switchLang }), [t, lang, notify, fail, presets, jobs]); // eslint-disable-line react-hooks/exhaustive-deps

  let page;
  if (route.page === "p" && route.id) page = <Editor key={route.id} projectId={route.id} />;
  else if (route.page === "settings") page = <Shell route={route}><Settings /></Shell>;
  else page = <Shell route={route}><Home /></Shell>;

  return (
    <AppContext.Provider value={value}>
      {page}
      <Toasts items={toasts} dismiss={dismiss} />
    </AppContext.Provider>
  );
}

function Shell({ route, children }) {
  const { t, lang, switchLang, jobs } = useApp();
  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <header className="topnav">
        <a className="brand hoard-brand" href="#/"><img src="/studio-assets/app-icon.png" alt="" width="28" height="28" /> Prospero · Montaje</a>
        <nav style={{ display: "flex", gap: 4 }} aria-label="Main">
          <a className="navlink" href="#/" aria-current={route.page === "home" ? "page" : undefined}>{t("nav_projects")}</a>
          <a className="navlink" href="#/settings" aria-current={route.page === "settings" ? "page" : undefined}>{t("nav_settings")}</a>
        </nav>
        <div style={{ flex: 1 }} />
        {jobs.active.length ? <span className="chip chip-info"><span className="spinner" style={{ width: 10, height: 10 }} /> {t("jobs_n", { n: jobs.active.length })}</span> : null}
        <button type="button" className="btn btn-sm btn-ghost" onClick={switchLang} title="Idioma / Language"><Icon name="help" size={14} />{lang === "es" ? "ES" : "EN"}</button>
      </header>
      <div style={{ flex: 1, minHeight: 0 }}>{children}</div>
    </div>
  );
}
