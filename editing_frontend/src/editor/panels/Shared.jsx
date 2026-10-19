import React from "react";

export function PanelHead({ title, children }) {
  return (
    <div className="ed-panel-head">
      <h3 style={{ fontSize: 13.5 }}>{title}</h3>
      <div style={{ display: "flex", gap: 6 }}>{children}</div>
    </div>
  );
}

export function Hint({ children }) {
  return <div className="muted" style={{ fontSize: 12, marginBottom: 10, lineHeight: 1.45 }}>{children}</div>;
}
