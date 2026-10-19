import React, { useMemo, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Icon, Modal, Spinner } from "../components/ui.jsx";
import { fmtMs } from "./time.js";
import { contactSheetRequest } from "./contactSheet.js";

function timeLabel(frame) {
  if (typeof frame?.time === "string" && frame.time) return frame.time;
  const time = frame?.time_ms ?? frame?.t_ms;
  if (Number.isFinite(Number(time))) return fmtMs(Number(time));
  return frame?.timecode || "";
}

function frameLayers(frame) {
  return (Array.isArray(frame?.layers) ? frame.layers : []).map((layer) => [
    layer?.media_name,
    layer?.text,
    typeof layer?.clip === "string" ? layer.clip : null,
  ].filter(Boolean).join(" · ")).filter(Boolean);
}

export default function ContactSheetDialog({ projectId, onClose }) {
  const { t, fail } = useApp();
  const [mode, setMode] = useState("overview");
  const [count, setCount] = useState(12);
  const [width, setWidth] = useState(320);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const request = useMemo(() => contactSheetRequest(mode, count, width), [mode, count, width]);

  const generate = async (event) => {
    event.preventDefault();
    setBusy(true); setError(""); setResult(null);
    try { setResult(await api.projectContactSheet(projectId, request)); }
    catch (err) { setError(err.message || String(err)); fail?.(err); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={t("contact_sheet_title")} onClose={onClose} width={850}
      footer={<><button type="button" className="btn" onClick={onClose}>{t("close")}</button><button type="submit" form="contact-sheet-form" className="btn btn-primary" disabled={busy}>{busy ? <Spinner /> : <Icon name="grid" size={14} />}{t("contact_sheet_make")}</button></>}>
      <form id="contact-sheet-form" onSubmit={generate} style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, alignItems: "end", marginBottom: 16 }}>
        <label className="field-label" style={{ display: "grid", gap: 5 }}>
          <span>{t("contact_sheet_mode")}</span>
          <select className="field" value={mode} onChange={(e) => setMode(e.target.value)} disabled={busy}>
            <option value="overview">{t("contact_sheet_overview")}</option>
            <option value="boundaries">{t("contact_sheet_boundaries")}</option>
            <option value="adaptive">{t("contact_sheet_adaptive")}</option>
          </select>
        </label>
        <label className="field-label" style={{ display: "grid", gap: 5 }}>
          <span>{t("contact_sheet_count")}</span>
          <input className="field" type="number" min="2" max="16" step="1" value={count} onChange={(e) => setCount(e.target.value)} disabled={busy} />
        </label>
        <label className="field-label" style={{ display: "grid", gap: 5 }}>
          <span>{t("contact_sheet_tile_width")}</span>
          <input className="field" type="number" min="128" max="640" step="1" value={width} onChange={(e) => setWidth(e.target.value)} disabled={busy} />
        </label>
      </form>
      {mode === "boundaries" && <p className="muted" style={{ margin: "-4px 0 14px", fontSize: 12 }}>{t("contact_sheet_boundary_help")}</p>}
      {mode === "adaptive" && <p className="muted" style={{ margin: "-4px 0 14px", fontSize: 12 }}>{t("contact_sheet_adaptive_help")}</p>}
      {error && <div className="chip chip-err" role="alert" style={{ height: "auto", whiteSpace: "normal", padding: "8px 10px", marginBottom: 12 }}>{t("contact_sheet_error")}: {error}</div>}
      {result && <div className="contact-sheet-results" data-testid="contact-sheet-result" style={{ display: "grid", gridTemplateColumns: "minmax(0, 2fr) minmax(0, 1fr)", gap: 16, alignItems: "start" }}>
        <div>
          {(result.png_url || result.url) && <img data-testid="contact-sheet-image" src={result.png_url || result.url} alt={t("contact_sheet_preview")} style={{ display: "block", width: "100%", maxHeight: 480, objectFit: "contain", background: "var(--panel-2)", borderRadius: 8 }} />}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
            {result.png_url && <a className="btn" href={result.png_url} download><Icon name="download" size={14} />{t("contact_sheet_download_png")}</a>}
            {result.receipt_url && <a className="btn" href={result.receipt_url} download><Icon name="download" size={14} />{t("contact_sheet_manifest")}</a>}
            {result.html_url && <a className="btn" href={result.html_url} target="_blank" rel="noreferrer">{t("contact_sheet_open_html")}</a>}
          </div>
          {result.truncated && <p className="muted" style={{ fontSize: 12 }}>{t("contact_sheet_truncated")}</p>}
        </div>
        <div style={{ minWidth: 0 }}>
          <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>{t("contact_sheet_frames", { n: result.frames?.length || 0 })}</div>
          <ol data-testid="contact-sheet-timecodes" style={{ margin: 0, paddingLeft: 22, maxHeight: 430, overflowY: "auto", fontSize: 12 }}>
            {(result.frames || []).map((frame, index) => <li key={`${frame.frame ?? index}-${frame.time ?? frame.time_ms ?? index}`} style={{ padding: "4px 0" }}>
              <code>{timeLabel(frame) || t("contact_sheet_time_unknown")}</code>
              {frameLayers(frame).map((layer, layerIndex) => <div key={layerIndex} className="muted" style={{ overflowWrap: "anywhere" }}>{layer}</div>)}
            </li>)}
          </ol>
        </div>
      </div>}
    </Modal>
  );
}
