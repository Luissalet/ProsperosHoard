import React from "react";
import { useApp } from "../../App.jsx";
import { useEd } from "../EditorContext.js";
import { TEXT_PRESETS } from "../fxspec.js";
import { Hint, PanelHead } from "./Shared.jsx";

const SAMPLES = {
  title: { text: "title_text", css: { fontSize: 26, fontWeight: 800, WebkitTextStroke: "1px #000", color: "#fff" } },
  subtitle: { text: "subtitle_text", css: { fontSize: 17, fontWeight: 500, color: "#fff" } },
  lower: { text: "lower_text", css: { fontSize: 15, fontWeight: 700, color: "#fff", background: "#000000b3", padding: "3px 10px", borderRadius: 4 } },
  cta: { text: "cta_text", css: { fontSize: 17, fontWeight: 800, color: "#0a0a0a", background: "#FFD400", padding: "3px 12px", borderRadius: 6 } },
};

export default function TextPanel() {
  const { t } = useApp();
  const ed = useEd();
  const add = (key) => {
    const p = TEXT_PRESETS[key];
    ed.edit([{ op: "add_text", text: t(SAMPLES[key].text), start: Math.round(ed.pb.t), length: 3000, style: p }], t("lbl_add_text")).then((res) => {
      const id = res?.results?.[0]?.clip;
      if (id) ed.setSelection({ ids: [id], track: null });
    });
  };
  return (
    <div className="ed-panel">
      <PanelHead title={t("tab_text")} />
      <div className="ed-panel-body">
        <Hint>{t("text_panel_help")}</Hint>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
          {Object.keys(SAMPLES).map((key) => (
            <button key={key} type="button" className="tile" style={{ minHeight: 84, display: "flex", flexDirection: "column", justifyContent: "space-between" }} onClick={() => add(key)}>
              <div style={{ background: "var(--sunken)", borderRadius: 6, flex: 1, display: "flex", alignItems: "center", justifyContent: "center", minHeight: 52, padding: 6, textAlign: "center" }}>
                <span style={SAMPLES[key].css}>{t(SAMPLES[key].text)}</span>
              </div>
              <span style={{ fontSize: 12, marginTop: 6 }}>{t(`text_preset_${key}`)}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
