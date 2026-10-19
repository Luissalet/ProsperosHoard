import React, { useEffect, useRef, useState } from "react";

// Inline stroke icons (24x24 viewBox). No icon library.
const P = {
  back: "M15 18l-6-6 6-6",
  undo: "M9 14L4 9l5-5M4 9h10a6 6 0 010 12h-3",
  redo: "M15 14l5-5-5-5M20 9H10a6 6 0 000 12h3",
  play: "M7 4.5v15l12-7.5z",
  pause: "M8 5v14M16 5v14",
  stepback: "M6 5v14M18 5l-9 7 9 7z",
  stepfwd: "M18 5v14M6 5l9 7-9 7z",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  x: "M6 6l12 12M18 6L6 18",
  check: "M5 12.5l4.5 4.5L19 7.5",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  scissors: "M6 9a3 3 0 100-6 3 3 0 000 6zM6 21a3 3 0 100-6 3 3 0 000 6zM8.1 7.9L20 19M8.1 16.1L20 5",
  copy: "M9 9h11v11H9zM5 15V4h11",
  eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z",
  eyeoff: "M3 3l18 18M10.6 5.1A9.6 9.6 0 0112 5c6.4 0 10 7 10 7a17 17 0 01-3.2 4M6.5 6.6C3.6 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 004.3-1",
  lock: "M6 11h12v9H6zM8 11V8a4 4 0 118 0v3",
  unlock: "M6 11h12v9H6zM8 11V8a4 4 0 017.5-2",
  volume: "M4 9v6h4l5 4V5L8 9zM16.5 8.5a5 5 0 010 7M19 6a8.5 8.5 0 010 12",
  mute: "M4 9v6h4l5 4V5L8 9zM17 9l5 6M22 9l-5 6",
  film: "M4 4h16v16H4zM4 9h4M4 15h4M16 9h4M16 15h4M8 4v16M16 4v16",
  music: "M9 18V6l11-2v12M9 18a3 3 0 11-6 0 3 3 0 016 0zM20 16a3 3 0 11-6 0 3 3 0 016 0z",
  type: "M5 6V4h14v2M12 4v16M9 20h6",
  image: "M4 4h16v16H4zM4 16l5-5 4 4 3-3 4 4M9 9.5a1 1 0 100-.01",
  folder: "M3 6h6l2 2h10v11H3z",
  file: "M6 3h8l4 4v14H6zM14 3v4h4",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  download: "M12 4v12M7 11l5 5 5-5M4 20h16",
  search: "M11 18a7 7 0 100-14 7 7 0 000 14zM20 20l-4.5-4.5",
  settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 13a7.7 7.7 0 000-2l2-1.5-2-3.5-2.3 1a7.5 7.5 0 00-1.7-1L15 3.5h-4L10.6 6a7.5 7.5 0 00-1.7 1l-2.3-1-2 3.5 2 1.5a7.7 7.7 0 000 2l-2 1.5 2 3.5 2.3-1a7.5 7.5 0 001.7 1l.4 2.5h4l.4-2.5a7.5 7.5 0 001.7-1l2.3 1 2-3.5z",
  sparkles: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  wand: "M4 20L15 9M14 4l1 2.5L17.5 7.5 15 8.5 14 11l-1-2.5L10.5 7.5 13 6.5zM19 13l.6 1.4L21 15l-1.4.6L19 17l-.6-1.4L17 15l1.4-.6z",
  subtitles: "M3 5h18v14H3zM7 11h4M13 11h4M7 15h2M11 15h6",
  transition: "M3 5h8v14H3zM13 5h8v14h-8zM9 12h6M12.5 9.5L15 12l-2.5 2.5",
  effects: "M12 3a9 9 0 100 18 9 9 0 000-18zM12 3v18M12 8a4 4 0 010 8",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  magnet: "M6 3v8a6 6 0 0012 0V3M6 8h4M14 8h4",
  zoomin: "M11 18a7 7 0 100-14 7 7 0 000 14zM20 20l-4.5-4.5M11 8v6M8 11h6",
  zoomout: "M11 18a7 7 0 100-14 7 7 0 000 14zM20 20l-4.5-4.5M8 11h6",
  fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  marker: "M6 3h12v10l-6 8-6-8z",
  link: "M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1",
  mic: "M12 15a3 3 0 003-3V6a3 3 0 00-6 0v6a3 3 0 003 3zM6 11a6 6 0 0012 0M12 17v4",
  help: "M12 21a9 9 0 100-18 9 9 0 000 18zM9.5 9.5a2.5 2.5 0 114 2c-.9.6-1.5 1-1.5 2M12 17h.01",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  freeze: "M12 2v20M4.9 7l14.2 10M4.9 17L19.1 7M12 2l-2 2M12 2l2 2M12 22l-2-2M12 22l2-2",
  stabilize: "M5 8V5h3M16 5h3v3M19 16v3h-3M8 19H5v-3M9 12a3 3 0 106 0 3 3 0 00-6 0",
  split: "M12 3v18M7 7l-3 5 3 5M17 7l3 5-3 5",
  chevron: "M9 6l6 6-6 6",
  chevdown: "M6 9l6 6 6-6",
  clip: "M4 7h16v10H4zM8 7v10",
  duck: "M4 14c2-6 5-6 8 0s6 6 8 0",
  waveform: "M3 12h2M7 8v8M11 5v14M15 8v8M19 10v4M21 12h0",
  keyframe: "M12 3l9 9-9 9-9-9z",
  closegap: "M4 12h6M20 12h-6M7 9l3 3-3 3M17 9l-3 3 3 3",
  detach: "M4 8h10M4 16h10M17 6l3 3-3 3M14 18l3-3 3 3",
  crop: "M6 2v16h16M2 6h16v16",
  star: "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z",
  video: "M3 6h12v12H3zM15 10l6-3v10l-6-3z",
  inout: "M4 4v16M4 12h12M12 8l4 4-4 4M20 4v16",
  cut: "M3 12h5M16 12h5M8 7l8 10",
  refresh: "M20 8a8 8 0 00-14.5-2M4 4v4h4M4 16a8 8 0 0014.5 2M20 20v-4h-4",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  nest: "M3 4h18v16H3zM7 8h10v8H7z",
  mask: "M4 4h16v16H4zM12 16.5a4.5 4.5 0 100-9 4.5 4.5 0 000 9z",
  ramp: "M3 18c5 0 6-12 18-12M3 21h18M3 3v18",
};

export function Icon({ name, size = 16, d, className, style }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill={name === "play" || name === "keyframe" ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      style={{ flex: "none", ...style }}
    >
      <path d={d || P[name] || ""} />
    </svg>
  );
}

export const Spinner = () => <span className="spinner" role="status" aria-label="…" />;

export function Bar({ value, className = "" }) {
  const pct = Math.max(0, Math.min(1, value || 0)) * 100;
  return (
    <div className={`bar ${className}`} role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <i style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Modal({ title, onClose, children, footer, width = 520 }) {
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose?.();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} style={{ width: `min(${width}px, 100%)` }}>
        <div className="modal-head">
          <h3 style={{ fontSize: 14 }}>{title}</h3>
          <button type="button" className="btn btn-ghost btn-icon btn-sm" onClick={onClose} aria-label="×">
            <Icon name="x" />
          </button>
        </div>
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </div>
    </div>
  );
}

export function ConfirmButton({ label, confirmLabel, cancelLabel, onConfirm, className = "btn btn-sm btn-danger", disabled, icon }) {
  const [pending, setPending] = useState(false);
  if (pending) {
    return (
      <span style={{ display: "inline-flex", gap: 4 }}>
        <button type="button" className={className} onClick={() => { setPending(false); onConfirm(); }}>{confirmLabel}</button>
        <button type="button" className="btn btn-sm" onClick={() => setPending(false)}>{cancelLabel}</button>
      </span>
    );
  }
  return (
    <button type="button" className={className} disabled={disabled} onClick={() => setPending(true)}>
      {icon ? <Icon name={icon} size={14} /> : null}
      {label}
    </button>
  );
}

export function Field({ label, children, help }) {
  return (
    <label style={{ display: "block", marginBottom: 10 }}>
      <span className="label">{label}</span>
      {children}
      {help ? <span className="muted" style={{ fontSize: 11.5, display: "block", marginTop: 2 }}>{help}</span> : null}
    </label>
  );
}

export function Seg({ value, onChange, options, label }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={String(o.value)} type="button" aria-pressed={value === o.value} onClick={() => onChange(o.value)} title={o.title}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

// Number input that keeps what is being typed and commits on blur / Enter.
export function NumInput({ value, onCommit, step = 1, min, max, className = "field num-in", decimals, disabled, title, width }) {
  const fmt = (v) => (v === null || v === undefined || Number.isNaN(v) ? "" : String(decimals !== undefined ? Number(v).toFixed(decimals) : Math.round(v * 1000) / 1000));
  const [text, setText] = useState(fmt(value));
  const editing = useRef(false);
  useEffect(() => {
    if (!editing.current) setText(fmt(value));
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps
  const commit = () => {
    editing.current = false;
    let v = parseFloat(String(text).replace(",", "."));
    if (Number.isNaN(v)) {
      setText(fmt(value));
      return;
    }
    if (min !== undefined) v = Math.max(min, v);
    if (max !== undefined) v = Math.min(max, v);
    setText(fmt(v));
    if (v !== value) onCommit(v);
  };
  return (
    <input
      className={className}
      style={width ? { width, flex: "none" } : undefined}
      type="text"
      inputMode="decimal"
      value={text}
      disabled={disabled}
      title={title}
      onFocus={(e) => { editing.current = true; e.target.select(); }}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.target.blur();
        if (e.key === "Escape") { editing.current = false; setText(fmt(value)); e.target.blur(); }
        if (e.key === "ArrowUp" || e.key === "ArrowDown") {
          e.preventDefault();
          let v = parseFloat(String(text).replace(",", ".")) || 0;
          v += (e.key === "ArrowUp" ? 1 : -1) * step * (e.shiftKey ? 10 : 1);
          if (min !== undefined) v = Math.max(min, v);
          if (max !== undefined) v = Math.min(max, v);
          setText(fmt(v));
          onCommit(v);
        }
      }}
    />
  );
}

// Slider + number box. onChange fires continuously while dragging (callers debounce the server edit).
export function SliderRow({ label, value, min, max, step = 0.01, onChange, decimals = 2, unit, disabled, defaultValue }) {
  const v = Number.isFinite(value) ? value : 0;
  return (
    <div className="row">
      <span className="l" onDoubleClick={() => defaultValue !== undefined && onChange(defaultValue)} title={defaultValue !== undefined ? "↺ doble clic" : undefined}>{label}</span>
      <div className="row-slider">
        <input type="range" min={min} max={max} step={step} value={Math.min(max, Math.max(min, v))} disabled={disabled} onChange={(e) => onChange(parseFloat(e.target.value))} />
        <NumInput value={v} decimals={decimals} step={step} min={min} max={max} onCommit={onChange} disabled={disabled} />
      </div>
      {unit ? null : null}
    </div>
  );
}

export function Toggle({ checked, onChange, label, disabled }) {
  return (
    <label style={{ display: "inline-flex", alignItems: "center", gap: 6, cursor: disabled ? "not-allowed" : "pointer", fontSize: 12.5 }}>
      <input type="checkbox" checked={!!checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      {label}
    </label>
  );
}

export function Empty({ children }) {
  return <div className="muted" style={{ padding: 16, textAlign: "center", fontSize: 12.5 }}>{children}</div>;
}

export function Collapsible({ title, children, defaultOpen = false }) {
  return (
    <details open={defaultOpen} style={{ marginTop: 6 }}>
      <summary className="muted" style={{ fontSize: 11.5 }}>{title}</summary>
      {children}
    </details>
  );
}

export function Logo({ size = 22 }) {
  return <img src="/icon-192.png" width={size} height={size} alt="" style={{ borderRadius: Math.round(size / 5), display: "block" }} />;
}
