import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { useCopilotRefresh } from "../copilot-hooks.js";

const NESTED = ["transform", "crop", "style", "mask"];

export function mergeProps(a = {}, b = {}) {
  const out = { ...a };
  for (const [k, v] of Object.entries(b)) {
    out[k] = NESTED.includes(k) && v && typeof v === "object" && out[k] && typeof out[k] === "object" ? { ...out[k], ...v } : v;
  }
  return out;
}

function applyOverlay(view, overlay) {
  const ids = Object.keys(overlay);
  if (!view || !ids.length) return view?.doc;
  return {
    ...view.doc,
    tracks: view.doc.tracks.map((tr) => (tr.clips.some((c) => overlay[c.id])
      ? { ...tr, clips: tr.clips.map((c) => (overlay[c.id] ? mergeProps(c, overlay[c.id]) : c)) }
      : tr)),
  };
}

// The project as the server holds it, plus: serialised edits (with one retry on a revision conflict), undo / redo,
// smart commands that wait for the analyses they need, and local "overlays" so sliders feel immediate.
export function useProject(projectId, { fail, notify, jobs, t }) {
  const [view, setView] = useState(null);
  const [overlay, setOverlay] = useState({});
  const [analysis, setAnalysis] = useState(null);
  const [busy, setBusy] = useState(0);
  const [loadError, setLoadError] = useState("");
  const viewRef = useRef(null);
  const chain = useRef(Promise.resolve());
  const timers = useRef({});
  const pending = useRef({});
  const mounted = useRef(true);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const applyView = useCallback((v) => {
    if (!v || !mounted.current) return;
    viewRef.current = v;
    setView(v);
  }, []);

  const reload = useCallback(async () => {
    try {
      const v = await api.project(projectId);
      applyView(v);
      setLoadError("");
      return v;
    } catch (e) {
      if (!viewRef.current) setLoadError(e.message); else fail(e);
      return null;
    }
  }, [projectId, applyView, fail]);

  useEffect(() => { reload(); }, [reload]);
  useCopilotRefresh(reload);  // an agent turn may have edited the project: reload in place, no remount

  const edit = useCallback((ops, label, { quiet } = {}) => {
    const run = async () => {
      setBusy((n) => n + 1);
      try {
        for (let attempt = 0; attempt < 2; attempt++) {
          try {
            const res = await api.edit(projectId, { ops, label, base_rev: viewRef.current?.rev });
            applyView(res.view);
            const warn = (res.issues || []).find((i) => i.level === "error");
            if (warn && !quiet) notify(warn.message, "error");
            return res;
          } catch (e) {
            if (e.status === 409 && attempt === 0) {
              await reload();
              continue;
            }
            fail(e);
            await reload();
            return null;
          }
        }
        return null;
      } finally {
        setBusy((n) => n - 1);
      }
    };
    const p = chain.current.then(run, run);
    chain.current = p.catch(() => {});
    return p;
  }, [projectId, applyView, reload, fail, notify]);

  const undo = useCallback(() => {
    const run = async () => {
      if (viewRef.current && !viewRef.current.can_undo) return;
      try { applyView(await api.undo(projectId)); } catch (e) { if (e.code !== "no_history") fail(e); }
    };
    chain.current = chain.current.then(run, run);
    return chain.current;
  }, [projectId, applyView, fail]);

  const redo = useCallback(() => {
    const run = async () => {
      if (viewRef.current && !viewRef.current.can_redo) return;
      try { applyView(await api.redo(projectId)); } catch (e) { if (e.code !== "no_history") fail(e); }
    };
    chain.current = chain.current.then(run, run);
    return chain.current;
  }, [projectId, applyView, fail]);

  // Debounced `set` on one clip. The overlay shows the new values at once; the edit is sent ~250 ms after the last change.
  const commitClipProps = useCallback((clipId, props, label, delay = 250) => {
    setOverlay((o) => ({ ...o, [clipId]: mergeProps(o[clipId] || {}, props) }));
    const key = `${clipId}|${label}`;
    const slot = timers.current[key] || (timers.current[key] = { props: {}, timer: 0 });
    slot.props = mergeProps(slot.props, props);
    pending.current[clipId] = (pending.current[clipId] || 0) + 1;
    clearTimeout(slot.timer);
    slot.timer = setTimeout(async () => {
      const send = slot.props;
      delete timers.current[key];
      const n = pending.current[clipId];
      try {
        await edit([{ op: "set", clip: clipId, props: send, ripple: false }], label);
      } finally {
        pending.current[clipId] = Math.max(0, (pending.current[clipId] || 1) - n);
        if (!pending.current[clipId] && mounted.current) {
          setOverlay((o) => { const c = { ...o }; delete c[clipId]; return c; });
        }
      }
    }, delay);
  }, [edit]);

  // Anything that may answer {done:false, needs:[jobs]} because an analysis is missing: wait for those jobs, then ask again.
  const withAnalysis = useCallback(async (fn) => {
    setBusy((n) => n + 1);
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        const res = await fn();
        if (res.done !== false) {
          setAnalysis(null);
          return res;
        }
        const ids = (res.needs || []).map((n) => n.job).filter(Boolean);
        setAnalysis({ message: res.message, ids });
        notify(t("analysis_wait"), "info");
        jobs.poke();
        const finals = await Promise.all(ids.map((id) => jobs.watch(id)));
        const bad = finals.find((j) => j.state !== "done");
        if (bad) throw new Error(bad.error || bad.state);
      }
      throw new Error(t("analysis_gave_up"));
    } finally {
      setAnalysis(null);
      setBusy((n) => n - 1);
    }
  }, [notify, jobs, t]);

  const runCommand = useCallback(async (command, args = {}, { preview = false } = {}) => {
    const res = await withAnalysis(() => api.command(projectId, { command, args, preview }));
    if (!preview && res.view) applyView(res.view);
    return res;
  }, [projectId, withAnalysis, applyView]);

  const rename = useCallback(async (name) => {
    try {
      await api.projectRename(projectId, name);
      await reload();
    } catch (e) { fail(e); }
  }, [projectId, reload, fail]);

  const doc = useMemo(() => applyOverlay(view, overlay), [view, overlay]);

  return { view, doc, overlay, edit, undo, redo, reload, applyView, commitClipProps, runCommand, withAnalysis, rename, analysis, busy, loadError };
}

// Per-media details the timeline needs and the project view leaves out (filmstrip layout).
export function useMediaInfo(media) {
  const [infos, setInfos] = useState({});
  const fetched = useRef(new Set());
  const key = media ? Object.values(media).map((m) => `${m.id}|${m.urls?.sprite ? 1 : 0}|${m.urls?.waveform ? 1 : 0}|${m.proxy}`).join(",") : "";
  useEffect(() => {
    if (!media) return;
    for (const m of Object.values(media)) {
      if (m.kind === "sequence") continue;  // a nested project has no filmstrip
      const k = `${m.id}|${m.urls?.sprite ? 1 : 0}|${m.urls?.waveform ? 1 : 0}|${m.proxy}`;
      if (fetched.current.has(k)) continue;
      fetched.current.add(k);
      api.mediaGet(m.id).then((info) => setInfos((s) => ({ ...s, [m.id]: info }))).catch(() => {});
    }
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return infos;
}
