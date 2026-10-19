import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Icon, Spinner } from "./ui.jsx";
import { fmtMs } from "../editor/time.js";
import { useCopilotRefresh } from "../copilot-hooks.js";

// The media library list, refreshed when background jobs finish and while proxies are still being made.
export function useMediaLibrary() {
  const { fail, jobs } = useApp();
  const [media, setMedia] = useState(null);
  const refresh = useCallback(async () => {
    try {
      setMedia((await api.media()).media);
    } catch (e) {
      fail(e);
    }
  }, [fail]);
  useEffect(() => { refresh(); }, [refresh, jobs.doneTick]);
  useCopilotRefresh(refresh);
  const pending = media?.some((m) => m.proxy === "pending");
  useEffect(() => {
    if (!pending) return undefined;
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [pending, refresh]);
  return { media, refresh };
}

export function uploadAll(files, { notify, fail, jobs, t, onDone }) {
  const list = [...files];
  if (!list.length) return Promise.resolve();
  return (async () => {
    let ok = 0;
    for (const f of list) {
      try {
        await api.mediaUpload(f);
        ok += 1;
      } catch (e) {
        fail(new Error(`${f.name}: ${e.message}`));
      }
    }
    if (ok) {
      notify(t("imported_n", { n: ok }), "ok");
      jobs.poke();
      onDone?.();
    }
  })();
}

export function MediaThumb({ m, onAdd, onClick, draggable = true, selected, children }) {
  const { t } = useApp();
  const preparing = m.proxy === "pending";
  const kindIcon = m.kind === "audio" ? "music" : m.kind === "image" ? "image" : "film";
  return (
    <div
      className="mthumb"
      draggable={draggable}
      aria-label={m.name}
      aria-pressed={selected}
      style={selected ? { borderColor: "var(--accent)" } : undefined}
      onClick={onClick}
      onDoubleClick={() => onAdd?.(m)}
      onDragStart={(e) => {
        e.dataTransfer.setData("application/x-lumiere-media", m.id);
        e.dataTransfer.effectAllowed = "copy";
      }}
    >
      {m.urls?.poster ? <img src={m.urls.poster} alt="" loading="lazy" /> : (
        <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--dim)", background: m.kind === "audio" ? "color-mix(in srgb, var(--clip-audio) 28%, var(--sunken))" : "var(--sunken)" }}>
          <Icon name={kindIcon} size={26} />
        </div>
      )}
      {m.kind !== "image" ? <span className="mdur num">{fmtMs(m.duration_ms).replace(/\.\d+$/, "")}</span> : null}
      {preparing ? <span className="chip chip-info" style={{ position: "absolute", left: 4, top: 4, height: 18 }}><Spinner />{t("media_preparing")}</span> : null}
      {m.proxy === "failed" ? <span className="chip chip-err" style={{ position: "absolute", left: 4, top: 4, height: 18 }}>{t("media_failed")}</span> : null}
      {m.missing ? <span className="chip chip-err" style={{ position: "absolute", left: 4, top: 4, height: 18 }}>{t("media_missing")}</span> : null}
      {onAdd ? (
        <button type="button" className="btn btn-primary btn-sm btn-icon madd" title={t("add_to_timeline")} aria-label={t("add_to_timeline")} onClick={(e) => { e.stopPropagation(); onAdd(m); }}>
          <Icon name="plus" size={14} />
        </button>
      ) : null}
      {children}
    </div>
  );
}

// Wraps content so that files dropped from the desktop are uploaded.
export function DropUpload({ children, onUploaded, style }) {
  const app = useApp();
  const [over, setOver] = useState(false);
  const depth = useRef(0);
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  return (
    <div
      className="dropzone"
      style={{ position: "relative", ...style }}
      onDragEnter={(e) => { if (hasFiles(e)) { depth.current += 1; setOver(true); } }}
      onDragLeave={(e) => { if (hasFiles(e)) { depth.current = Math.max(0, depth.current - 1); if (!depth.current) setOver(false); } }}
      onDragOver={(e) => { if (hasFiles(e)) e.preventDefault(); }}
      onDrop={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current = 0;
        setOver(false);
        uploadAll(e.dataTransfer.files, { ...app, onDone: onUploaded });
      }}
    >
      {children}
      {over ? (
        <div style={{ position: "absolute", inset: 0, background: "var(--accent-soft)", border: "2px dashed var(--accent)", borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center", pointerEvents: "none", zIndex: 5, color: "var(--accent)", fontWeight: 600 }}>
          <Icon name="upload" size={20} />&nbsp;{app.t("drop_files")}
        </div>
      ) : null}
    </div>
  );
}

export function useUploadPicker(onUploaded) {
  const app = useApp();
  const input = useMemo(() => {
    const el = document.createElement("input");
    el.type = "file";
    el.multiple = true;
    el.accept = "video/*,audio/*,image/*";
    return el;
  }, []);
  useEffect(() => {
    input.onchange = () => {
      uploadAll(input.files, { ...app, onDone: onUploaded });
      input.value = "";
    };
  });
  return () => input.click();
}
