import React, { useState } from "react";
import { api } from "../../api.js";
import { useApp } from "../../App.jsx";
import { Icon, NumInput, Spinner, Toggle } from "../../components/ui.jsx";
import { useEd } from "../EditorContext.js";
import { Hint } from "./Shared.jsx";

const FOLDER_KEY = "lumiere-music-folder";

function savedFolder() {
  try { return localStorage.getItem(FOLDER_KEY) || ""; } catch { return ""; } // storage may be blocked
}

function Row({ label, children }) {
  return <div className="row"><span className="l">{label}</span><div style={{ minWidth: 0 }}>{children}</div></div>;
}

function Card({ id, icon, title, help, children }) {
  return (
    <details className="panel" style={{ marginBottom: 10 }} data-tool={id}>
      <summary style={{ padding: "9px 12px", display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
        <Icon name={icon} size={16} style={{ color: "var(--accent)" }} />{title}
      </summary>
      <div style={{ padding: "0 12px 12px" }}>
        <Hint>{help}</Hint>
        {children}
      </div>
    </details>
  );
}

// Reasons and warnings come as {code, text, ...}: the code picks the translated sentence, the English text is the fallback.
function ReasonLine({ r, kind }) {
  const { t } = useApp();
  const key = `music_${kind}_${r.code}`;
  const text = t(key, { bpm: r.bpm, cpm: r.cuts_per_min, share: Math.round((r.share || 0) * 100), short: r.short_ms ? (r.short_ms / 1000).toFixed(1) : "", lufs: r.lufs });
  return (
    <div style={{ fontSize: 11.5, color: kind === "warn" ? "var(--warn, #f5b700)" : "var(--muted)" }}>
      {kind === "warn" ? "! " : "+ "}{text === key ? r.text : text}
    </div>
  );
}

export function MusicPicker() {
  const { t, fail, notify } = useApp();
  const ed = useEd();
  const [folder, setFolder] = useState(savedFolder);
  const [recursive, setRecursive] = useState(false);
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState("");
  const find = async () => {
    setBusy("find");
    try {
      const r = await api.musicPick(ed.projectId, { folder: folder.trim(), recursive, count: 5 });
      setRes(r);
      try { localStorage.setItem(FOLDER_KEY, folder.trim()); } catch { /* storage may be blocked */ }
    } catch (e) { fail(e); } finally { setBusy(""); }
  };
  const put = async (track) => {
    setBusy(track.path);
    try {
      const r = await ed.runCommand("music_add", { path: track.path, duck: true });
      const s = r.summary || {};
      notify(t("music_added", { name: s.name || track.name, at: s.starts_at || "0:00" }), "ok");
    } catch (e) { fail(e); } finally { setBusy(""); }
  };
  return (
    <Card id="music_pick" icon="music" title={t("tool_music")} help={t("tool_music_help")}>
      <Row label={t("music_folder")}>
        <input className="field" value={folder} placeholder="~/Music" onChange={(e) => setFolder(e.target.value)} onKeyDown={(e) => e.key === "Enter" && folder.trim() && find()} data-testid="music-folder" />
      </Row>
      <Toggle checked={recursive} onChange={setRecursive} label={t("music_recursive")} />
      <div style={{ height: 8 }} />
      <button type="button" className="btn btn-sm btn-primary" disabled={!folder.trim() || !!busy} onClick={find} data-testid="music-find">
        {busy === "find" ? <Spinner /> : <Icon name="search" size={14} />}{t("music_find")}
      </button>
      {res ? (
        <div style={{ marginTop: 10 }} data-testid="music-result">
          <div className="muted" style={{ fontSize: 11.5, marginBottom: 6 }}>
            {t("music_edit_profile", { d: res.edit.duration, cpm: res.edit.cuts_per_min ?? "—" })} · {t("music_analysed", { n: res.analysed })}
          </div>
          {res.tracks.map((tr) => (
            <div key={tr.path} className="step" style={{ marginBottom: 6 }} data-testid="music-track">
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <div style={{ flex: "1 1 120px", minWidth: 0 }}>
                  <div className="ellipsis" style={{ fontWeight: 600, fontSize: 12.5 }} title={tr.path}>{tr.rank}. {tr.name}</div>
                  <div className="muted num" style={{ fontSize: 11.5 }}>{tr.duration} · {tr.bpm} BPM · {Math.round(tr.score * 100)}%</div>
                </div>
                <button type="button" className="btn btn-sm" disabled={!!busy} onClick={() => put(tr)} data-testid="music-use">
                  {busy === tr.path ? <Spinner /> : <Icon name="plus" size={14} />}{t("music_use")}
                </button>
              </div>
              {tr.reasons.map((r, i) => <ReasonLine key={`r${i}`} r={r} kind="ok" />)}
              {tr.warnings.map((r, i) => <ReasonLine key={`w${i}`} r={r} kind="warn" />)}
            </div>
          ))}
          {res.skipped?.length ? <div className="muted" style={{ fontSize: 11.5 }}>{t("music_skipped", { n: res.skipped.length })}</div> : null}
        </div>
      ) : null}
    </Card>
  );
}

export function BrollTool() {
  const { t, fail, notify } = useApp();
  const ed = useEd();
  const [useModel, setUseModel] = useState(false);
  const [per, setPer] = useState(3);
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState("");
  const run = async (place) => {
    setBusy(place);
    try {
      const r = await ed.withAnalysis(() => api.broll(ed.projectId, { per_sentence: per, use_model: useModel, place }));
      setRes(r);
      if (place === "best") {
        await ed.reload();
        notify(t("broll_placed", { n: (r.placed || []).length }), "ok");
      }
    } catch (e) { fail(e); } finally { setBusy(""); }
  };
  const place = async (s, o) => {
    setBusy(`${s.start_ms}-${o.media}`);
    try {
      await ed.edit([{ op: "add_overlay", media: o.media, start: s.start_ms, length: Math.min(o.length, s.end_ms - s.start_ms), src_in: o.src_in }], t("broll_label"));
    } finally { setBusy(""); }
  };
  return (
    <Card id="broll" icon="film" title={t("tool_broll")} help={t("tool_broll_help")}>
      <Toggle checked={useModel} onChange={setUseModel} label={t("broll_model")} />
      <div style={{ height: 6 }} />
      <Row label={t("broll_per")}><NumInput value={per} min={1} max={8} step={1} decimals={0} onCommit={(v) => setPer(Math.round(v))} /></Row>
      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        <button type="button" className="btn btn-sm btn-primary" disabled={!!busy || ed.busy > 0} onClick={() => run("none")} data-testid="broll-find">
          {busy === "none" ? <Spinner /> : <Icon name="search" size={14} />}{t("broll_find")}
        </button>
        <button type="button" className="btn btn-sm" disabled={!!busy || ed.busy > 0} onClick={() => run("best")} data-testid="broll-place-best">
          {busy === "best" ? <Spinner /> : <Icon name="check" size={14} />}{t("broll_place_best")}
        </button>
      </div>
      {res ? (
        <div style={{ marginTop: 10 }} data-testid="broll-result">
          <div className="muted" style={{ fontSize: 11.5, marginBottom: 6 }}>
            {t("broll_summary", { a: res.with_options, b: res.suggestions.length, n: res.library_checked })}
            {res.model?.requested ? ` · ${res.model.used ? t("broll_model_used", { m: res.model.name || "" }) : t("broll_model_off")}` : ""}
          </div>
          {res.suggestions.map((s, i) => (
            <div key={i} className="step" style={{ marginBottom: 6 }}>
              <div style={{ fontSize: 12 }}><span className="mono num">{s.range || s.at}</span> {s.sentence ? `· ${s.sentence}` : ""}</div>
              {s.options.length === 0 ? <div className="muted" style={{ fontSize: 11.5 }}>{t("broll_none")}</div> : null}
              {s.options.map((o) => (
                <div key={o.media} style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 3 }} data-testid="broll-option">
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="ellipsis" style={{ fontSize: 12, fontWeight: 600 }}>{o.name} <span className="muted num" style={{ fontWeight: 400 }}>{Math.round(o.score * 100)}%</span></div>
                    <div className="muted ellipsis" style={{ fontSize: 11.5 }}>{t(`broll_via_${o.via}`)}: {(o.matched || []).join(", ")}</div>
                  </div>
                  <button type="button" className="btn btn-sm" disabled={!!busy || ed.busy > 0} onClick={() => place(s, o)}>{t("broll_place")}</button>
                </div>
              ))}
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
