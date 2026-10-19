import React, { useState } from "react";
import { useApp } from "../../App.jsx";
import { Icon, NumInput } from "../../components/ui.jsx";
import { useEd } from "../EditorContext.js";
import { Hint, PanelHead } from "./Shared.jsx";

const GLYPH = {
  crossfade: "M4 12h16M12 4v16", dissolve: "M5 5h14v14H5z", fade_black: "M4 4h16v16H4z", fade_white: "M4 4h16v16H4z",
  slide_left: "M20 12H4M9 7l-5 5 5 5", slide_right: "M4 12h16M15 7l5 5-5 5", slide_up: "M12 20V4M7 9l5-5 5 5", slide_down: "M12 4v16M7 15l5 5 5-5",
  wipe_left: "M4 4h16v16H4zM12 4v16", wipe_right: "M4 4h16v16H4zM12 4v16", wipe_up: "M4 4h16v16H4zM4 12h16", wipe_down: "M4 4h16v16H4zM4 12h16",
  circle_open: "M12 12m-8 0a8 8 0 1016 0 8 8 0 10-16 0", circle_close: "M12 12m-5 0a5 5 0 1010 0 5 5 0 10-10 0M12 12m-9 0a9 9 0 1018 0 9 9 0 10-18 0",
  zoom_in: "M5 5h14v14H5zM9 9h6v6H9z", pixelize: "M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z", radial: "M12 3v9l6 6M12 12m-9 0a9 9 0 1018 0 9 9 0 10-18 0",
  smooth_left: "M20 12H4M9 7l-5 5 5 5", smooth_right: "M4 12h16M15 7l5 5-5 5", blur: "M12 4c3 4 6 6 6 10a6 6 0 01-12 0c0-4 3-6 6-10z",
};

export default function TransitionsPanel() {
  const { t, presets, notify } = useApp();
  const ed = useEd();
  const [dur, setDur] = useState(500);
  const [last, setLast] = useState("crossfade");
  const sel = ed.actions.selectedClips.filter(({ clip, track }) => track.kind === "video" && clip.type === "media");

  const apply = (type) => {
    setLast(type);
    if (!sel.length) { notify(t("select_clip_first"), "error"); return; }
    ed.edit(sel.map(({ clip }) => ({ op: "transition", clip: clip.id, type, dur })), t("lbl_transition"));
  };
  const all = () => ed.edit([{ op: "transition", all_cuts: true, type: last, dur }], t("lbl_transition_all"));

  return (
    <div className="ed-panel">
      <PanelHead title={t("tab_transitions")} />
      <div className="ed-panel-body">
        <Hint>{t("transitions_help")}</Hint>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <span className="muted" style={{ fontSize: 12 }}>{t("duration")} (ms)</span>
          <NumInput value={dur} min={40} max={5000} step={50} decimals={0} onCommit={(v) => setDur(Math.round(v))} width={80} />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8 }}>
          {(presets?.transitions || []).map((x) => (
            <button key={x} type="button" className="tile" aria-pressed={last === x} style={{ textAlign: "center", padding: "10px 4px" }} onClick={() => apply(x)} title={t(`tr_${x}`)}>
              <Icon d={GLYPH[x] || GLYPH.crossfade} size={22} style={{ color: "var(--accent)" }} />
              <div className="ellipsis" style={{ fontSize: 11.5, marginTop: 4 }}>{t(`tr_${x}`)}</div>
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
          <button type="button" className="btn btn-primary" onClick={all}><Icon name="transition" size={14} />{t("transition_all")}</button>
          <button type="button" className="btn" onClick={() => (sel.length ? ed.edit(sel.map(({ clip }) => ({ op: "transition", clip: clip.id, type: null })), t("lbl_transition")) : notify(t("select_clip_first"), "error"))}>{t("transition_remove")}</button>
        </div>
      </div>
    </div>
  );
}
