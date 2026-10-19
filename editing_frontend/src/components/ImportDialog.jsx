import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Icon, Modal, Spinner } from "./ui.jsx";
import { fmtBytes } from "../editor/time.js";

const sep = (p) => (p.includes("\\") && !p.includes("/") ? "\\" : "/");

export default function ImportDialog({ onClose, onImported }) {
  const { t, notify, fail, jobs } = useApp();
  const [listing, setListing] = useState(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(() => new Set());
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);

  const browse = useCallback(async (path) => {
    setLoading(true);
    try {
      setListing(await api.fs(path));
      setSelected(new Set());
    } catch (e) {
      fail(e);
    } finally {
      setLoading(false);
    }
  }, [fail]);

  useEffect(() => { browse(""); }, [browse]);

  const done = (list, errors = []) => {
    if (errors.length) notify(errors.map((x) => `${x.file}: ${x.error}`).join(" · "), "error");
    if (list.length) {
      notify(t("imported_n", { n: list.length }), "ok");
      jobs.poke();
      onImported?.(list);
      onClose();
    }
  };

  const importSelected = async () => {
    setBusy(true);
    const ok = [];
    const errors = [];
    for (const path of selected) {
      try {
        ok.push(await api.mediaImport({ path }));
      } catch (e) {
        errors.push({ file: path.split(/[\\/]/).pop(), error: e.message });
      }
    }
    setBusy(false);
    done(ok, errors);
  };

  const importFolder = async () => {
    if (!listing?.path) return;
    setBusy(true);
    try {
      const res = await api.mediaImport({ folder: listing.path });
      done(res.imported, res.errors);
      if (!res.imported.length && !res.errors.length) notify(t("import_none"));
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const importPasted = async () => {
    const path = pasted.trim().replace(/^"|"$/g, "");
    if (!path) return;
    setBusy(true);
    try {
      let folder = null;
      try { folder = await api.fs(path); } catch { /* not a folder: import it as a file */ }
      if (folder && Array.isArray(folder.entries)) {
        setListing(folder);
        setSelected(new Set());
        setPasted("");
        return;
      }
      done([await api.mediaImport({ path })]);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const toggle = (path) => setSelected((s) => {
    const n = new Set(s);
    if (n.has(path)) n.delete(path); else n.add(path);
    return n;
  });

  const entries = listing?.entries || [];
  const files = entries.filter((e) => !e.dir);

  return (
    <Modal
      title={t("import_title")}
      onClose={onClose}
      width={720}
      footer={(
        <>
          <button type="button" className="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="button" className="btn" disabled={busy || !listing?.path} onClick={importFolder}><Icon name="folder" size={14} />{t("import_folder")}</button>
          <button type="button" className="btn btn-primary" disabled={busy || !selected.size} onClick={importSelected}>{busy ? <Spinner /> : <Icon name="plus" size={14} />}{t("import_selected", { n: selected.size })}</button>
        </>
      )}
    >
      <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
        <input className="field" value={pasted} placeholder={t("import_paste")} onChange={(e) => setPasted(e.target.value)} onKeyDown={(e) => e.key === "Enter" && importPasted()} aria-label={t("import_paste")} />
        <button type="button" className="btn" disabled={busy || !pasted.trim()} onClick={importPasted}>{t("import_btn")}</button>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8, minHeight: 28 }}>
        <button type="button" className="btn btn-sm" disabled={!listing?.path} onClick={() => browse(listing.parent || "")} title={t("import_up")}><Icon name="back" size={14} />{t("import_up")}</button>
        <span className="mono ellipsis" style={{ flex: 1 }} title={listing?.path}>{listing?.path || t("import_places")}</span>
        {loading ? <Spinner /> : null}
      </div>
      <div style={{ border: "1px solid var(--line)", borderRadius: 6, height: 320, overflow: "auto", background: "var(--field)" }}>
        {entries.length === 0 && !loading ? <div className="muted" style={{ padding: 16, textAlign: "center" }}>{t("import_empty")}</div> : null}
        {entries.map((e) => (
          <div
            key={e.path}
            style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 10px", borderBottom: "1px solid #ffffff08", cursor: "pointer", background: selected.has(e.path) ? "var(--accent-soft)" : "transparent" }}
            onClick={() => (e.dir ? browse(e.path) : toggle(e.path))}
            onDoubleClick={() => !e.dir && api.mediaImport({ path: e.path }).then((m) => done([m])).catch(fail)}
          >
            {e.dir ? <Icon name="folder" size={15} style={{ color: "var(--warn)" }} /> : <input type="checkbox" checked={selected.has(e.path)} onChange={() => toggle(e.path)} onClick={(ev) => ev.stopPropagation()} aria-label={e.name} />}
            <span className="ellipsis" style={{ flex: 1 }}>{e.name}</span>
            {!e.dir ? <span className="muted num" style={{ fontSize: 11.5 }}>{fmtBytes(e.bytes)}</span> : null}
          </div>
        ))}
      </div>
      {files.length ? (
        <div style={{ marginTop: 6 }}>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setSelected(new Set(files.map((f) => f.path)))}>{t("select_all")}</button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => setSelected(new Set())}>{t("select_none")}</button>
        </div>
      ) : null}
    </Modal>
  );
}

export { sep };
