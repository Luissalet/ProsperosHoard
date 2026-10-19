import React, { useState } from "react";
import { api } from "../../api.js";
import { useApp } from "../../App.jsx";
import { Icon, Seg, SliderRow, Spinner, Toggle } from "../../components/ui.jsx";
import { useEd } from "../EditorContext.js";
import { Hint, PanelHead } from "./Shared.jsx";
import TranslateSubtitles from "./TranslateSubtitles.jsx";

const STYLES = {
  clean: { color: "#fff", fontWeight: 500, textShadow: "0 1px 4px #000" },
  bold: { color: "#fff", fontWeight: 800, WebkitTextStroke: "1.2px #000" },
  karaoke: { color: "#FFD400", fontWeight: 800, WebkitTextStroke: "1px #000" },
  pop: { color: "#FFD400", fontWeight: 900, WebkitTextStroke: "1.2px #000", transform: "scale(1.08)" },
  boxed: { color: "#fff", fontWeight: 700, background: "#000000ad", padding: "1px 8px", borderRadius: 4 },
  minimal: { color: "#fff", fontWeight: 400, fontSize: 12, textShadow: "0 1px 3px #000" },
};

export default function CaptionsPanel() {
  const { t, fail, notify, jobs } = useApp();
  const ed = useEd();
  const cap = ed.doc.captions;
  const [busy, setBusy] = useState(false);
  const missing = ed.transcript?.missing || [];

  const enable = async (enabled, style) => {
    setBusy(true);
    try {
      await ed.runCommand("captions", { enabled, ...(style ? { style } : {}) });
      await ed.reloadTranscript();
    } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const setCap = (props, style) => ed.edit([{ op: "captions", ...(style ? { style } : {}), props: props || {} }], t("lbl_captions"));
  const transcribe = async () => {
    setBusy(true);
    try {
      const ids = [];
      for (const mid of missing) {
        const res = await api.mediaAnalyze(mid, ["transcript"]);
        for (const j of res.jobs) if (j.job) ids.push(j.job);
      }
      jobs.poke();
      notify(t("analysis_wait"));
      await Promise.all(ids.map((id) => jobs.watch(id)));
      await ed.reloadTranscript();
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  return (
    <div className="ed-panel">
      <PanelHead title={t("tab_captions")}>{busy ? <Spinner /> : null}</PanelHead>
      <div className="ed-panel-body">
        <Hint>{t("captions_help")}</Hint>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          <Toggle checked={cap.enabled} disabled={busy} onChange={(v) => enable(v)} label={cap.enabled ? t("captions_on") : t("captions_off")} />
        </div>
        {cap.enabled && missing.length ? (
          <div style={{ padding: 10, background: "color-mix(in srgb, var(--warn) 16%, var(--panel))", border: "1px solid color-mix(in srgb, var(--warn) 45%, var(--panel))", borderRadius: 8, marginBottom: 12, fontSize: 12.5 }}>
            {t("captions_missing", { n: missing.length })}
            <div style={{ marginTop: 8 }}><button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={transcribe}><Icon name="mic" size={14} />{t("transcribe")}</button></div>
          </div>
        ) : null}
        <span className="label">{t("style")}</span>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 14 }}>
          {Object.keys(STYLES).map((s) => (
            <button key={s} type="button" className="tile" aria-pressed={cap.style === s && cap.enabled} disabled={busy} onClick={() => (cap.enabled ? setCap({}, s) : enable(true, s))} style={{ textAlign: "center" }}>
              <div style={{ background: "var(--sunken)", borderRadius: 6, padding: "10px 4px", fontSize: 15, minHeight: 40 }}><span style={STYLES[s]}>{t("captions_sample")}</span></div>
              <div style={{ fontSize: 11.5, marginTop: 5 }}>{t(`capstyle_${s}`)}</div>
            </button>
          ))}
        </div>
        <span className="label">{t("position")}</span>
        <div style={{ marginBottom: 12 }}>
          <Seg value={cap.position} onChange={(v) => setCap({ position: v })} options={["top", "middle", "lower_third", "bottom"].map((p) => ({ value: p, label: t(`cappos_${p}`) }))} />
        </div>
        <Toggle checked={cap.uppercase} onChange={(v) => setCap({ uppercase: v })} label={t("uppercase")} />
        <div style={{ margin: "12px 0 4px" }} data-testid="cap-speakers">
          <span className="label">{t("cap_speakers")}</span>
          <select className="field" value={cap.speaker_labels || "off"} onChange={(e) => setCap({ speaker_labels: e.target.value })} data-testid="cap-speaker-labels">
            {["off", "color", "prefix", "both"].map((v) => <option key={v} value={v}>{t(`cap_speakers_${v}`)}</option>)}
          </select>
          <div className="muted" style={{ fontSize: 11, marginTop: 3 }}>{t("cap_speakers_help")}</div>
        </div>
        <div style={{ marginTop: 10 }}>
          <SliderRow label={t("max_words")} value={cap.max_words} min={1} max={16} step={1} decimals={0} defaultValue={4} onChange={(v) => setCap({ max_words: Math.round(v) })} />
          <SliderRow label={t("size")} value={cap.size} min={0} max={200} step={1} decimals={0} defaultValue={0} onChange={(v) => setCap({ size: Math.round(v) })} />
        </div>
        <div className="muted" style={{ fontSize: 11, margin: "-2px 0 10px" }}>{t("size_auto_help")}</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10 }}>
          {[["color", t("color")], ["highlight", t("highlight")], ["outline", t("outline")]].map(([k, label]) => (
            <div key={k}>
              <span className="label">{label}</span>
              <input type="color" value={cap[k]} onChange={(e) => setCap({ [k]: e.target.value.toUpperCase() })} style={{ width: "100%", height: 28, padding: 0, border: "1px solid var(--line-2)", borderRadius: 5, background: "var(--field)" }} />
            </div>
          ))}
        </div>
        <TranslateSubtitles busy={busy} />
      </div>
    </div>
  );
}
