import React from "react";
import { useApp } from "../App.jsx";
import { Modal } from "./ui.jsx";

const ROWS = [
  ["Space", "sc_play"], ["K", "sc_pause"], ["L", "sc_forward"], ["J", "sc_back5"],
  ["← →", "sc_frame"], ["Shift + ← →", "sc_second"], ["↑ ↓", "sc_cut_jump"], ["Home / End", "sc_ends"],
  ["S / C", "sc_split"], ["{del} / ⌫", "sc_delete"], ["Shift + {del}", "sc_delete_gap"], ["Ctrl + D", "sc_duplicate"],
  ["Ctrl + Z", "sc_undo"], ["Ctrl + Shift + Z / Ctrl + Y", "sc_redo"], ["I / O", "sc_marks"], ["M", "sc_marker"],
  ["+ / −", "sc_zoom"], ["Ctrl + {wheel}", "sc_zoom_wheel"], ["\\", "sc_fit"], ["Ctrl + A", "sc_select_all"],
  ["Esc", "sc_deselect"], ["Alt + {drag}", "sc_noripple"], ["Ctrl + C / X / V", "sc_clipboard"], ["Alt + {dragbody}", "sc_slip"], ["Ctrl + {dragcut}", "sc_roll"], ["Ctrl + E", "sc_export"], ["?", "sc_help"],
];

export default function ShortcutsDialog({ onClose }) {
  const { t } = useApp();
  return (
    <Modal title={t("shortcuts")} onClose={onClose} width={560}>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 24px" }}>
        {ROWS.map(([k, label]) => (
          <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 12.5 }}>{t(label)}</span>
            <span style={{ display: "flex", gap: 3, flexWrap: "wrap", justifyContent: "flex-end" }}>{k.split(/( \+ | \/ )/).map((p, i) => (p.trim() === "+" || p.trim() === "/" ? <span key={i} className="dim">{p.trim()}</span> : p.split(" ").map((q, j) => <kbd key={`${i}-${j}`} className="kbd">{/^\{\w+\}$/.test(q) ? t(`key_${q.slice(1, -1)}`) : q}</kbd>)))}</span>
          </div>
        ))}
      </div>
    </Modal>
  );
}
