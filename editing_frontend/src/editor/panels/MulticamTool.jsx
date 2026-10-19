import React, { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api.js";
import { useApp } from "../../App.jsx";
import { Icon, NumInput, Seg, Spinner, Toggle } from "../../components/ui.jsx";
import { useMediaLibrary } from "../../components/Media.jsx";
import { useEd } from "../EditorContext.js";
import { fmtMs } from "../time.js";
import { Hint } from "./Shared.jsx";

const Labeled = ({ label, children }) => <div className="row"><span className="l">{label}</span><div style={{ minWidth: 0 }}>{children}</div></div>;

// Confidence of a sync as a small bar: green when the sound matches well, amber when it is doubtful.
function Confidence({ value }) {
  const { t } = useApp();
  if (value === null || value === undefined) return <span className="chip chip-info">{t("mc_manual")}</span>;
  const good = value >= 0.35;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }} title={t("mc_confidence")}>
      <span style={{ width: 54, height: 6, borderRadius: 3, background: "var(--panel-3)", overflow: "hidden", display: "inline-block" }}>
        <span style={{ display: "block", height: "100%", width: `${Math.round(Math.min(1, value) * 100)}%`, background: good ? "var(--ok)" : "var(--warn)" }} />
      </span>
      <span className={`chip ${good ? "chip-ok" : "chip-warn"}`}>{Math.round(value * 100)}%</span>
    </span>
  );
}

function SyncTable({ rows, frameMs }) {
  const { t } = useApp();
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }} data-testid="mc-sync-table">
      <thead>
        <tr className="muted" style={{ textAlign: "left" }}><th style={{ padding: "2px 4px" }}>{t("mc_angle")}</th><th style={{ padding: "2px 4px" }}>{t("mc_offset")}</th><th style={{ padding: "2px 4px" }}>{t("mc_confidence")}</th></tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.media} style={{ borderTop: "1px solid var(--line)" }}>
            <td style={{ padding: "4px" }} className="ellipsis">{r.label || r.name}</td>
            <td style={{ padding: "4px" }} className="num">{Math.round(r.start_ms)} ms{frameMs ? <span className="muted"> ({(r.start_ms / frameMs).toFixed(1)} {t("mc_frames")})</span> : null}</td>
            <td style={{ padding: "4px" }}><Confidence value={r.confidence} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// Step 1: pick the recordings of the same event, see how well their sound matches, build the group.
function CreateMulticam() {
  const { t, fail, notify } = useApp();
  const ed = useEd();
  const { media } = useMediaLibrary();
  const [picked, setPicked] = useState([]);
  const [name, setName] = useState("");
  const [sync, setSync] = useState(null);
  const [busy, setBusy] = useState(null);
  const candidates = (media || []).filter((m) => m.has_audio && (m.kind === "video" || m.kind === "audio") && !m.missing);
  const toggle = (id) => { setSync(null); setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id])); };
  const names = Object.fromEntries(candidates.map((m) => [m.id, m.name]));

  const measure = async () => {
    setBusy("sync");
    try {
      const res = await api.multicamSync({ media: picked });
      setSync({ ...res, angles: res.angles.map((a) => ({ ...a, label: names[a.media] })) });
    } catch (e) { fail(e); } finally { setBusy(null); }
  };
  const create = async () => {
    setBusy("create");
    try {
      const res = await ed.runCommand("multicam_create", { media: picked, name: name.trim() || t("mc_default_name") });
      notify(t("mc_created", { n: picked.length }), "ok");
      setSync(null);
      setPicked([]);
      return res;
    } catch (e) { fail(e); return null; } finally { setBusy(null); }
  };

  return (
    <div data-testid="mc-create">
      <Hint>{t("mc_create_help")}</Hint>
      <div style={{ maxHeight: 170, overflow: "auto", border: "1px solid var(--line)", borderRadius: 6, marginBottom: 8 }}>
        {media === null ? <div style={{ padding: 8 }}><Spinner /></div> : candidates.length === 0 ? <div className="muted" style={{ padding: 8, fontSize: 12 }}>{t("mc_no_media")}</div> : candidates.map((m) => (
          <label key={m.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "5px 8px", borderBottom: "1px solid var(--line)", cursor: "pointer", fontSize: 12.5 }}>
            <input type="checkbox" checked={picked.includes(m.id)} onChange={() => toggle(m.id)} data-testid={`mc-pick-${m.id}`} />
            <Icon name={m.kind === "audio" ? "music" : "video"} size={14} style={{ color: "var(--muted)" }} />
            <span className="ellipsis" style={{ flex: 1, minWidth: 0 }}>{m.name}</span>
            <span className="muted num" style={{ fontSize: 11 }}>{fmtMs(m.duration_ms).replace(/\.\d+$/, "")}</span>
          </label>
        ))}
      </div>
      <Labeled label={t("name")}><input className="field" value={name} placeholder={t("mc_default_name")} onChange={(e) => setName(e.target.value)} /></Labeled>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button type="button" className="btn btn-sm" disabled={picked.length < 2 || !!busy} onClick={measure} data-testid="mc-measure">{busy === "sync" ? <Spinner /> : <Icon name="waveform" size={14} />}{t("mc_measure")}</button>
        <button type="button" className="btn btn-sm btn-primary" disabled={picked.length < 2 || !!busy || ed.busy > 0} onClick={create} data-testid="mc-create-btn">{busy === "create" ? <Spinner /> : <Icon name="video" size={14} />}{t("mc_create")}</button>
      </div>
      {picked.length === 1 ? <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>{t("mc_pick_two")}</div> : null}
      {sync ? (
        <div style={{ marginTop: 8, padding: 8, background: "var(--field)", borderRadius: 6 }} data-testid="mc-sync-result">
          <b style={{ fontSize: 12 }}>{t("mc_sync_result")}</b>
          <SyncTable rows={sync.angles} frameMs={sync.frame_ms} />
          {sync.notes?.length ? <div style={{ marginTop: 6, fontSize: 11.5, color: "var(--warn)" }}>{sync.notes.join(" ")}</div> : <div className="muted" style={{ marginTop: 6, fontSize: 11.5 }}>{t("mc_sync_ok")}</div>}
        </div>
      ) : null}
    </div>
  );
}

// Step 2: the angle viewer. Thumbnails of every camera at the playhead (made by the server); a click cuts to that camera there.
function AngleViewer({ group, view }) {
  const { t } = useApp();
  const ed = useEd();
  const [at, setAt] = useState(Math.round(ed.pb.t / 100) * 100);
  const timer = useRef(0);
  useEffect(() => {
    const off = ed.pb.subscribe((time) => {
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setAt(Math.round(time / 100) * 100), 220);
    });
    return () => { off(); clearTimeout(timer.current); };
  }, [ed.pb]);

  const shots = view?.shots || [];
  const shown = shots.find((s) => s.start <= at && at < s.end);
  const videoAngles = group.angles.map((a, i) => ({ ...a, n: i + 1 })).filter((a) => !a.audio_only);
  const range = ed.marks.in !== null && ed.marks.out !== null && ed.marks.out > ed.marks.in ? [ed.marks.in, ed.marks.out] : null;

  const cut = (n) => ed.edit([{ op: "multicam_switch", group: group.id, at: Math.round(ed.pb.t), angle: n }], t("mc_switch_label"));
  const cutRange = (n) => ed.edit([{ op: "multicam_switch", group: group.id, at: Math.round(range[0]), end: Math.round(range[1]), angle: n }], t("mc_switch_label"));

  return (
    <div data-testid="mc-viewer">
      <div className="muted" style={{ fontSize: 11.5, marginBottom: 6 }}>{t("mc_viewer_help")} <span className="num">{fmtMs(at).replace(/\.\d+$/, "")}</span></div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        {videoAngles.map((a) => {
          const active = shown && shown.angle === a.n;
          return (
            <button key={a.media} type="button" className="tile" aria-pressed={!!active} onClick={() => cut(a.n)} data-testid={`mc-angle-${a.n}`} title={t("mc_cut_here")} style={{ padding: 5, textAlign: "center" }}>
              <div style={{ position: "relative", background: "#000", borderRadius: 5, overflow: "hidden", aspectRatio: "16 / 9" }}>
                <img src={api.multicamFrameUrl(ed.projectId, group.id, a.n, at, 240, ed.view.rev)} alt={a.label} style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} draggable={false} />
                {active ? <span className="chip chip-ok" style={{ position: "absolute", top: 4, left: 4 }}>{t("mc_on_air")}</span> : null}
              </div>
              <div style={{ fontSize: 11.5, marginTop: 4, fontWeight: active ? 700 : 500 }}>{a.n}. {a.label}</div>
            </button>
          );
        })}
      </div>
      {range ? (
        <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" }}>
          <span className="muted" style={{ fontSize: 11.5 }}>{t("mc_range_marked", { a: fmtMs(range[0]).replace(/\.\d+$/, ""), b: fmtMs(range[1]).replace(/\.\d+$/, "") })}</span>
          {videoAngles.map((a) => <button key={a.media} type="button" className="btn btn-sm" onClick={() => cutRange(a.n)} data-testid={`mc-range-${a.n}`}>{a.n}</button>)}
        </div>
      ) : <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>{t("mc_range_hint")}</div>}
      <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>{t("mc_shots", { n: shots.length })}</div>
      <div style={{ display: "flex", height: 10, borderRadius: 3, overflow: "hidden", marginTop: 4, background: "var(--panel-3)" }} data-testid="mc-shot-strip">
        {shots.map((s) => (
          <div key={s.clip} title={`${s.label} ${fmtMs(s.start).replace(/\.\d+$/, "")}`} onClick={() => ed.pb.seek(s.start)} style={{ flex: s.end - s.start, background: `hsl(${(s.angle * 67) % 360} 55% 50%)`, borderRight: "1px solid var(--panel)", cursor: "pointer" }} />
        ))}
      </div>
    </div>
  );
}

function AutoSwitch({ group }) {
  const { t, fail, notify } = useApp();
  const ed = useEd();
  const [mode, setMode] = useState("loudness");
  const [minShot, setMinShot] = useState(2);
  const [hyst, setHyst] = useState(4);
  const [wide, setWide] = useState("");
  const [busy, setBusy] = useState(null);
  const [res, setRes] = useState(null);
  const run = async (preview) => {
    setBusy(preview ? "preview" : "apply");
    try {
      const out = await ed.runCommand("multicam_auto", { group: group.id, mode, min_shot_ms: Math.round(minShot * 1000), hysteresis_db: hyst, ...(wide ? { wide } : {}) }, { preview });
      setRes(out);
      if (!preview) notify(t("mc_auto_done", { n: out.summary?.shots ?? 0 }), "ok");
    } catch (e) { fail(e); } finally { setBusy(null); }
  };
  const s = res?.summary;
  return (
    <div data-testid="mc-auto">
      <Hint>{t("mc_auto_help")}</Hint>
      <Labeled label={t("mode")}>
        <Seg value={mode} onChange={setMode} options={[{ value: "loudness", label: t("mc_mode_loudness") }, { value: "speakers", label: t("mc_mode_speakers") }]} />
      </Labeled>
      <div className="muted" style={{ fontSize: 11.5, margin: "-3px 0 7px" }}>{t(`mc_mode_help_${mode}`)}</div>
      <Labeled label={t("mc_min_shot")}><NumInput value={minShot} min={0.5} max={30} step={0.5} decimals={1} onCommit={setMinShot} /></Labeled>
      {mode === "loudness" ? <Labeled label={t("mc_hysteresis")}><NumInput value={hyst} min={0} max={30} step={1} decimals={0} onCommit={setHyst} /></Labeled> : null}
      <Labeled label={t("mc_wide")}>
        <select className="field" value={wide} onChange={(e) => setWide(e.target.value)}>
          <option value="">{t("mc_wide_none")}</option>
          {group.angles.filter((a) => !a.audio_only).map((a, i) => <option key={a.media} value={String(group.angles.indexOf(a) + 1)}>{a.label || i + 1}</option>)}
        </select>
      </Labeled>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button type="button" className="btn btn-sm" disabled={!!busy || ed.busy > 0} onClick={() => run(true)}>{busy === "preview" ? <Spinner /> : <Icon name="eye" size={14} />}{t("preview")}</button>
        <button type="button" className="btn btn-sm btn-primary" disabled={!!busy || ed.busy > 0} onClick={() => run(false)} data-testid="mc-auto-apply">{busy === "apply" ? <Spinner /> : <Icon name="wand" size={14} />}{t("mc_auto_apply")}</button>
      </div>
      {s ? (
        <div style={{ marginTop: 8, padding: 8, background: "var(--field)", borderRadius: 6, fontSize: 12 }} data-testid="mc-auto-result">
          <b>{res.preview ? t("tool_preview_result") : t("tool_applied")}</b>
          <div style={{ marginTop: 3 }}>{t("mc_auto_result", { n: s.shots ?? 0, d: s.duration ?? "" })}</div>
          {s.speaker_angles ? <div className="muted" style={{ marginTop: 3 }}>{Object.entries(s.speaker_angles).map(([k, v]) => `${k} → ${v}`).join(" · ")}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

function GroupCard({ group, view, onChanged }) {
  const { t, fail } = useApp();
  const ed = useEd();
  const [busy, setBusy] = useState(false);
  const setOffset = async (media, value) => {
    setBusy(true);
    try { await ed.edit([{ op: "multicam_set", group: group.id, offsets: { [media]: Math.round(value) } }], t("mc_offset_label")); onChanged(); } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const setMaster = (media) => ed.edit([{ op: "multicam_set", group: group.id, master: media }], t("mc_master_label"));
  const resync = async () => {
    setBusy(true);
    try { await ed.runCommand("multicam_resync", { group: group.id }); onChanged(); } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const release = () => ed.edit([{ op: "multicam_set", group: group.id, release: true }], t("mc_release_label"));
  const names = view?.angles || [];
  return (
    <div style={{ marginBottom: 12 }} data-testid="mc-group">
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <b className="ellipsis" style={{ flex: 1, fontSize: 13 }}>{group.name}</b>
        {busy ? <Spinner /> : null}
        <button type="button" className="btn btn-sm btn-ghost" onClick={resync} disabled={busy} title={t("mc_resync_help")}><Icon name="refresh" size={14} />{t("mc_resync")}</button>
      </div>
      <AngleViewer group={group} view={view} />
      <details style={{ marginTop: 10 }} data-testid="mc-sync-details">
        <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 12.5 }}>{t("mc_sync_title")}</summary>
        <div style={{ marginTop: 6 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11.5 }}>
            <thead><tr className="muted" style={{ textAlign: "left" }}><th style={{ padding: "2px 4px" }}>{t("mc_angle")}</th><th style={{ padding: "2px 4px" }}>{t("mc_offset")} (ms)</th><th style={{ padding: "2px 4px" }}>{t("mc_confidence")}</th><th style={{ padding: "2px 4px" }}>{t("mc_sound")}</th></tr></thead>
            <tbody>
              {group.angles.map((a, i) => (
                <tr key={a.media} style={{ borderTop: "1px solid var(--line)" }}>
                  <td style={{ padding: 4 }} className="ellipsis">{a.label}{a.audio_only ? <span className="muted"> ({t("mc_audio_only")})</span> : null}</td>
                  <td style={{ padding: 4, width: 84 }}><NumInput value={a.start} min={0} step={10} decimals={0} width={76} onCommit={(v) => setOffset(a.media, v)} /></td>
                  <td style={{ padding: 4 }}><Confidence value={names[i]?.confidence ?? a.confidence} /></td>
                  <td style={{ padding: 4 }}><input type="radio" name={`master-${group.id}`} checked={group.master === i} onChange={() => setMaster(a.media)} aria-label={t("mc_sound")} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{t("mc_offset_help")}</div>
        </div>
      </details>
      <details style={{ marginTop: 8 }} open>
        <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 12.5 }}>{t("mc_auto_title")}</summary>
        <div style={{ marginTop: 6 }}><AutoSwitch group={group} /></div>
      </details>
      <button type="button" className="btn btn-sm btn-ghost" style={{ marginTop: 8 }} onClick={release} title={t("mc_release_help")}><Icon name="detach" size={14} />{t("mc_release")}</button>
    </div>
  );
}

export default function MulticamTool() {
  const { t } = useApp();
  const ed = useEd();
  const groups = ed.doc.multicams || [];
  const [views, setViews] = useState({});
  const rev = ed.view.rev;
  const key = useMemo(() => groups.map((g) => g.id).join(","), [groups]);
  const load = React.useCallback(async () => {
    if (!groups.length) { setViews({}); return; }
    try {
      const res = await api.multicam(ed.projectId);
      setViews(Object.fromEntries(res.groups.map((g) => [g.id, g])));
    } catch { /* the viewer shows what it can without the shot list */ }
  }, [ed.projectId, key]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [load, rev]);

  return (
    <details className="panel" style={{ marginBottom: 10 }} data-tool="multicam" open={groups.length > 0 || undefined}>
      <summary style={{ padding: "9px 12px", display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
        <Icon name="video" size={16} style={{ color: "var(--accent)" }} />{t("tool_multicam")}
        {groups.length ? <span className="chip chip-info" style={{ marginLeft: "auto" }}>{groups.length}</span> : null}
      </summary>
      <div style={{ padding: "0 12px 12px" }}>
        {groups.map((g) => <GroupCard key={g.id} group={g} view={views[g.id]} onChanged={load} />)}
        {groups.length ? (
          <details>
            <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 12.5, marginBottom: 6 }}>{t("mc_new_group")}</summary>
            <CreateMulticam />
          </details>
        ) : <CreateMulticam />}
      </div>
    </details>
  );
}
