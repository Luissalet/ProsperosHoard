import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../App.jsx";
import { Spinner } from "../components/ui.jsx";
import { useEd } from "./EditorContext.js";

const words = {
  es: {
    textTitle: "Montaje como texto", textHint: "El montaje entero escrito en texto: una línea por clip, los tiempos en milisegundos. Cambia valores (start, src, speed, volume, fade, label, el texto de un título), borra la línea de un clip para quitarlo o mueve una línea bajo otra pista. Comprobar muestra qué cambiaría; Aplicar lo guarda como un solo paso que se puede deshacer.",
    reload: "Volver a cargar", check: "Comprobar", apply: "Aplicar cambios", revision: "Revisión", stale: "El montaje ha cambiado desde que cargaste el texto: vuelve a cargarlo antes de aplicar.",
    changed: "Clips cambiados", removed: "Clips quitados", dur: "Duración", okCheck: "Se puede aplicar. No se ha guardado nada.", applied: "Aplicado: revisión", nothing: "El texto es igual que el montaje: no hay nada que cambiar.", ripple: "Cerrar o abrir el hueco al cambiar longitudes y borrar",
    propTitle: "Propuestas de corte", propHint: "Muletillas y pausas que se podrían quitar, cada una con su confianza y su motivo. Revisa la lista y acepta las que quieras: se cortan todas de una vez, en un solo paso que se puede deshacer. Si cambia la transcripción, las propuestas caducan.",
    fillers: "Muletillas y repeticiones", pauses: "Pausas largas", find: "Buscar propuestas", finding: "Buscando", none: "Sin propuestas todavía.", needs: "Falta transcribir el audio; se está preparando. Vuelve a buscar cuando termine.",
    expired: "La transcripción cambió después de buscar estas propuestas, así que ya no valen. Busca de nuevo.", at: "En", what: "Qué", conf: "Confianza", why: "Motivo", status: "Estado",
    selected: "seleccionadas", accept: "Aceptar seleccionadas", acceptAll: "Aceptar todas desde", reject: "Descartar seleccionadas", minConf: "Confianza mínima (%)", pickConf: "Marcar desde esa confianza",
    accepted: "Cortadas", open: "abiertas", filler: "muletilla", pause: "pausa", st: { open: "abierta", accepted: "aceptada", rejected: "descartada" }, saved: "Ahorro", s: "s",
  },
  en: {
    textTitle: "Timeline as text", textHint: "The whole edit written as text: one line per clip, times in milliseconds. Change values (start, src, speed, volume, fade, label, a title's text), delete a clip's line to remove it, or move a line under another track. Check shows what would change; Apply saves it as one undoable step.",
    reload: "Reload", check: "Check", apply: "Apply changes", revision: "Revision", stale: "The edit changed since you loaded the text: reload it before applying.",
    changed: "Clips changed", removed: "Clips removed", dur: "Duration", okCheck: "It can be applied. Nothing was saved.", applied: "Applied: revision", nothing: "The text equals the edit: nothing to change.", ripple: "Close or open the gap when lengths change or clips are deleted",
    propTitle: "Cut proposals", propHint: "Filler words and pauses that could go, each with its confidence and reason. Review the list and accept the ones you want: they are all cut at once, as one undoable step. If the transcript changes, the proposals expire.",
    fillers: "Filler words and repeats", pauses: "Long pauses", find: "Find proposals", finding: "Looking", none: "No proposals yet.", needs: "The audio still needs transcribing; it is being prepared. Search again when it ends.",
    expired: "The transcript changed after these proposals were made, so they no longer apply. Search again.", at: "At", what: "What", conf: "Confidence", why: "Reason", status: "State",
    selected: "selected", accept: "Accept selected", acceptAll: "Accept all from", reject: "Dismiss selected", minConf: "Minimum confidence (%)", pickConf: "Select from that confidence",
    accepted: "Cut", open: "open", filler: "filler", pause: "pause", st: { open: "open", accepted: "accepted", rejected: "dismissed" }, saved: "Saved", s: "s",
  },
};

async function request(method, path, body) {
  const response = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!response.ok) throw new Error((data && (data.error || (typeof data.detail === "string" ? data.detail : ""))) || `HTTP ${response.status}`);
  return data;
}

export default function MontageText() {
  const { lang, notify, fail } = useApp();
  const t = words[String(lang).startsWith("es") ? "es" : "en"];
  const ed = useEd();
  const base = `/editing/api/projects/${encodeURIComponent(ed.projectId)}`;
  const current = ed.view?.rev;
  const [loaded, setLoaded] = useState(null);
  const [text, setText] = useState("");
  const [ripple, setRipple] = useState(false);
  const [report, setReport] = useState(null);
  const [busy, setBusy] = useState(false);
  const [kinds, setKinds] = useState({ fillers: true, pauses: true });
  const [doc, setDoc] = useState(null);
  const [picked, setPicked] = useState({});
  const [minConf, setMinConf] = useState(70);
  const [message, setMessage] = useState("");

  const run = async (action) => {
    setBusy(true); setMessage("");
    try { await action(); } catch (e) { setReport({ error: e.message }); fail?.(e); } finally { setBusy(false); }
  };
  const loadText = useCallback(() => run(async () => {
    const data = await request("GET", `${base}/text`);
    setLoaded(data); setText(data.text); setReport(null);
  }), [base]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadText(); }, [base]); // eslint-disable-line react-hooks/exhaustive-deps
  const stale = loaded && current !== undefined && loaded.revision !== current;
  // The project changed somewhere else (a cut accepted, an undo, a drag on the timeline): show it, unless the text has unsaved edits.
  const tried = useRef(null);
  useEffect(() => {
    if (!stale || busy || text !== loaded.text || tried.current === current) return;
    tried.current = current; loadText();
  }, [current, busy]); // eslint-disable-line react-hooks/exhaustive-deps
  const body = (extra = {}) => ({ text, expected_revision: loaded.revision, ripple, ...extra });
  const check = () => run(async () => setReport(await request("POST", `${base}/text/check`, body())));
  const apply = () => run(async () => {
    const done = await request("POST", `${base}/text/apply`, body());
    setReport(done);
    if (done.applied) { notify?.(`${t.applied} ${done.revision}`, "ok"); await ed.reload?.(); const next = await request("GET", `${base}/text`); setLoaded(next); setText(next.text); }
  });

  const pick = (view, floor = minConf) => {
    const next = {};
    for (const item of view.items) if (item.status === "open" && Math.round(item.confidence * 100) >= floor) next[item.id] = true;
    setPicked(next);
  };
  const show = (view) => { setDoc(view); pick(view); };
  const loadDoc = useCallback(() => run(async () => {
    try { show(await request("GET", `${base}/cut-proposals?limit=500`)); } catch { setDoc(null); }
  }), [base]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { loadDoc(); }, [base]); // eslint-disable-line react-hooks/exhaustive-deps
  const find = () => run(async () => {
    const chosen = Object.keys(kinds).filter((k) => kinds[k]);
    const made = await request("POST", `${base}/cut-proposals`, { kinds: chosen });
    if (made.done === false) { setMessage(t.needs); return; }
    show(await request("GET", `${base}/cut-proposals?proposal=${made.proposal}&limit=500`));
  });
  const chosenIds = Object.keys(picked).filter((id) => picked[id]);
  const accept = (ids, floor) => run(async () => {
    const done = await request("POST", `${base}/cut-proposals/accept`, { proposal: doc.proposal, ids: ids || undefined, min_confidence: floor, expected_revision: current });
    notify?.(`${t.accepted}: ${done.accepted.length} · ${t.saved} ${(done.removed_ms / 1000).toFixed(1)} ${t.s}`, "ok");
    await ed.reload?.(); await ed.reloadTranscript?.();
    show(await request("GET", `${base}/cut-proposals?proposal=${doc.proposal}&limit=500`));
  });
  const reject = () => run(async () => {
    await request("POST", `${base}/cut-proposals/reject`, { proposal: doc.proposal, ids: chosenIds });
    show(await request("GET", `${base}/cut-proposals?proposal=${doc.proposal}&limit=500`));
  });

  return (
    <div className="montage-text" style={{ overflow: "auto", padding: "10px 14px", display: "grid", gap: 18 }}>
      <section aria-labelledby="mt-text-title">
        <h3 id="mt-text-title" style={{ margin: "0 0 6px" }}>{t.textTitle}</h3>
        <p className="muted" style={{ maxWidth: "80ch" }}>{t.textHint}</p>
        {stale && <p role="alert" className="chip chip-err" style={{ height: "auto", padding: "6px 10px" }}>{t.stale}</p>}
        <textarea className="field" aria-label={t.textTitle} spellCheck={false} rows={12} value={text} onChange={(e) => setText(e.target.value)}
          style={{ width: "100%", fontFamily: "var(--mono, monospace)", fontSize: 12.5, lineHeight: 1.5, whiteSpace: "pre", overflow: "auto" }} />
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", margin: "8px 0" }}>
          <button type="button" className="btn" disabled={busy} onClick={loadText}>{t.reload}</button>
          <button type="button" className="btn" disabled={busy || !loaded || stale || text === loaded.text} onClick={check}>{t.check}</button>
          <button type="button" className="btn btn-primary" disabled={busy || !loaded || stale || text === loaded.text} onClick={apply}>{busy ? <Spinner /> : null}{t.apply}</button>
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}><input type="checkbox" checked={ripple} onChange={(e) => setRipple(e.target.checked)} />{t.ripple}</label>
          {loaded && <span className="muted num">{t.revision}: {loaded.revision}</span>}
        </div>
        {report && (report.error
          ? <p role="alert" className="chip chip-err" style={{ height: "auto", padding: "6px 10px", whiteSpace: "normal" }}>{report.error}</p>
          : <div role="status">
            <p>{report.no_change ? t.nothing : (report.applied ? `${t.applied} ${report.revision}` : t.okCheck)}</p>
            {!report.no_change && <p className="muted">{t.changed}: {Object.keys(report.changes?.clips_changed || {}).length} · {t.removed}: {(report.changes?.clips_removed || []).length} · {t.dur}: {report.changes?.duration_ms?.join(" → ")} ms</p>}
          </div>)}
      </section>
      <section aria-labelledby="mt-prop-title">
        <h3 id="mt-prop-title" style={{ margin: "0 0 6px" }}>{t.propTitle}</h3>
        <p className="muted" style={{ maxWidth: "80ch" }}>{t.propHint}</p>
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center", margin: "8px 0" }}>
          <label><input type="checkbox" checked={kinds.fillers} onChange={(e) => setKinds({ ...kinds, fillers: e.target.checked })} /> {t.fillers}</label>
          <label><input type="checkbox" checked={kinds.pauses} onChange={(e) => setKinds({ ...kinds, pauses: e.target.checked })} /> {t.pauses}</label>
          <button type="button" className="btn" disabled={busy || (!kinds.fillers && !kinds.pauses)} onClick={find}>{busy ? <Spinner /> : null}{t.find}</button>
        </div>
        {message && <p role="status" className="muted">{message}</p>}
        {!doc && !message && <p className="muted">{t.none}</p>}
        {doc?.status === "expired" && <p role="alert" className="chip chip-err" style={{ height: "auto", padding: "6px 10px", whiteSpace: "normal" }}>{t.expired}</p>}
        {doc && doc.status !== "expired" && <>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "end", margin: "8px 0" }}>
            <label className="field-label"><span>{t.minConf}</span>
              <input className="field" type="number" min="0" max="100" step="5" value={minConf} onChange={(e) => setMinConf(Number(e.target.value))} style={{ width: 90 }} /></label>
            <button type="button" className="btn" onClick={() => pick(doc)}>{t.pickConf}</button>
            <button type="button" className="btn btn-primary" disabled={busy || !chosenIds.length} onClick={() => accept(chosenIds)}>{t.accept} ({chosenIds.length})</button>
            <button type="button" className="btn" disabled={busy || !doc.counts.open} onClick={() => accept(null, minConf / 100)}>{t.acceptAll} {minConf}%</button>
            <button type="button" className="btn" disabled={busy || !chosenIds.length} onClick={reject}>{t.reject}</button>
            <span className="muted">{doc.counts.open} {t.open} · {doc.counts.accepted} {t.accepted.toLowerCase()}</span>
          </div>
          <div style={{ overflow: "auto", maxHeight: 320 }}>
            <table className="html-motion-sound-table" style={{ width: "100%" }}>
              <thead><tr><th /><th>{t.at}</th><th>{t.what}</th><th>{t.conf}</th><th>{t.why}</th><th>{t.status}</th></tr></thead>
              <tbody>{doc.items.map((item) => (
                <tr key={item.id} data-proposal={item.id}>
                  <td><input type="checkbox" aria-label={`${item.id} ${item.text}`} disabled={item.status !== "open"} checked={!!picked[item.id]} onChange={(e) => setPicked({ ...picked, [item.id]: e.target.checked })} /></td>
                  <td className="num">{item.at}</td>
                  <td>{t[item.kind]}: {item.text}</td>
                  <td className="num">{Math.round(item.confidence * 100)}%</td>
                  <td className="muted">{item.reason}</td>
                  <td>{t.st[item.status] || item.status}</td>
                </tr>))}</tbody>
            </table>
          </div>
        </>}
      </section>
    </div>
  );
}
