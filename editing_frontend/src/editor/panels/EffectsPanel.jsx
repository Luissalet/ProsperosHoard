import React from "react";
import { useApp } from "../../App.jsx";
import { Icon } from "../../components/ui.jsx";
import { useEd } from "../EditorContext.js";
import { AUDIO_FX } from "../fxspec.js";
import { Hint, PanelHead } from "./Shared.jsx";

export default function EffectsPanel() {
  const { t, presets, notify } = useApp();
  const ed = useEd();
  const sel = ed.actions.selectedClips.filter(({ clip }) => clip.type === "media");
  const apply = (type) => {
    if (!sel.length) { notify(t("select_clip_first"), "error"); return; }
    ed.edit([{ op: "filter_add", clips: sel.map(({ clip }) => clip.id), type, params: { ...(presets.effects[type] || {}) } }], t("lbl_effect"));
  };
  const keys = Object.keys(presets?.effects || {});
  const group = (list, title) => (
    <>
      <h4 className="muted" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", margin: "12px 0 8px" }}>{title}</h4>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 8 }}>
        {list.map((k) => (
          <button key={k} type="button" className="tile" onClick={() => apply(k)} style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Icon name={AUDIO_FX.has(k) ? "waveform" : "effects"} size={16} style={{ color: "var(--accent)" }} />
            <span style={{ minWidth: 0 }}>
              <span className="ellipsis" style={{ display: "block", fontSize: 12.5, fontWeight: 600 }}>{t(`fx_${k}`)}</span>
              <span className="muted ellipsis" style={{ display: "block", fontSize: 10.5 }}>{Object.entries(presets.effects[k] || {}).map(([p, v]) => `${p}=${v}`).join(" ") || t("fx_no_params")}</span>
            </span>
          </button>
        ))}
      </div>
    </>
  );
  return (
    <div className="ed-panel">
      <PanelHead title={t("tab_effects")} />
      <div className="ed-panel-body">
        <Hint>{t("effects_help")}</Hint>
        {group(keys.filter((k) => !AUDIO_FX.has(k)), t("fx_group_video"))}
        {group(keys.filter((k) => AUDIO_FX.has(k)), t("fx_group_audio"))}
      </div>
    </div>
  );
}
