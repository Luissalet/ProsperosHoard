import React, { useEffect, useState } from "react";

const words = {
  es: {
    stylesTitle: "Crear desde un estilo", stylesHint: "Fichas de estilo de dirección: esencia, materiales, color, tipografía, movimiento, sonido y qué se puede cambiar. Elige una y se crea una animación con su paleta, un texto por plano y un golpe visual en cada hito.",
    search: "Buscar un estilo", searchGo: "Buscar", all: "Todos los estilos", source: "Colección", category: "Categoría", results: "estilos", none: "Ningún estilo coincide.",
    pick: "Elegir", picked: "Estilo elegido", license: "Licencia", safe: "Se puede cambiar sin perder el estilo", character: "Cambia el carácter del estilo", essence: "Es la esencia: cambiarlo es otro estilo", free: "Libre en cada película",
    sections: "Secciones", create: "Crear animación desde este estilo", creating: "Creando", name: "Nombre", duration: "Duración (s)", caption: "Primer texto", draftNote: "Se crea con un tratamiento de borrador sacado de la ficha; un asistente puede reescribirlo con la herramienta de tratamiento.",
    directionTitle: "Dirección", treatment: "Tratamiento", noTreatment: "Esta animación no tiene tratamiento todavía. Un asistente puede escribirlo antes de generar el HTML: tres estructuras, los planos uno a uno con su porqué y el mapa de hitos.",
    chosen: "Estructura elegida", draft: "borrador", shots: "Planos", why: "Porqué", structures: "Estructuras", card: "Ficha",
    cues: "Hitos de imagen y sonido", cuesHint: "Cada hito es un momento en que la imagen golpea y suena algo. Los eventos de sonido salen de esta lista.", noCues: "Sin hitos.",
    time: "Tiempo (s)", visual: "Qué hace la imagen", role: "Sonido", silent: "Sin sonido", add: "Añadir hito", remove: "Quitar", saveCues: "Guardar hitos", savedCues: "Hitos guardados en la revisión",
    cuecheck: "Comprobar sincronía", cuechecking: "Comprobando", retime: "Mover hitos al fotograma real", readcheck: "Comprobar lectura", readchecking: "Comprobando",
    status: { ok: "Correcto", sound_late: "El sonido llega tarde", sound_early: "El sonido llega pronto", no_hit: "La imagen no golpea", unclear: "Golpe poco claro", no_frames: "Sin fotogramas" },
    cue: "Hito", offset: "Desfase (ms)", suggest: "Fotograma real (s)", unmapped: "Golpes visuales sin hito", dirty: "Guarda la composición antes de cambiar la dirección.",
    tooShort: "Textos que se van antes de poder leerse", never: "Textos que nunca se ven enteros", text: "Texto", seen: "Visible", needs: "Necesita (s)", rule: "Regla",
    facts: "Hechos y fuentes (FACTS.md)", factsHint: "Para vídeos informativos: cada afirmación con su fuente. Comprobar compara los números que se ven con los de este texto.", saveFacts: "Guardar hechos", checkFacts: "Comprobar números", unsourced: "Números en pantalla sin fuente", factsOk: "Todos los números que se ven están en los hechos.", factsSaved: "Hechos guardados",
    reviewMode: "Tipo de revisión", sheet: "Hoja de momentos", strip: "Tira de fotogramas", crop: "Detalle ampliado", span: "Tramo (s)", from: "Desde", to: "Hasta", point: "Punto x, y", selector: "o selector CSS", cropHint: "Recorta alrededor del punto (o del elemento) en cada momento y lo amplía.",
    soundOwned: "Los eventos de sonido salen de los hitos de esta animación: se cambian en la sección Dirección.",
    lint: "Comprobar que no es un pase de diapositivas", lintReject: "Pase de diapositivas: reescribe las secciones con movimiento", lintWarn: "Solo hay fundidos, sin movimiento",
    lintPass: "Hay movimiento: no es un pase de diapositivas", lintOnly: "Solo cambian de opacidad", lintPrompt: "Instrucción para reescribirla",
  },
  en: {
    stylesTitle: "Start from a style", stylesHint: "Direction style cards: essence, materials, colour, type, motion, sound and what may change. Pick one and an animation is made with its palette, a caption per shot and a visual hit on every cue.",
    search: "Search a style", searchGo: "Search", all: "All styles", source: "Collection", category: "Category", results: "styles", none: "No style matches.",
    pick: "Pick", picked: "Chosen style", license: "Licence", safe: "Safe to change", character: "Changes the character", essence: "Essence: changing it makes a different style", free: "Free in every film",
    sections: "Sections", create: "Create an animation from this style", creating: "Creating", name: "Name", duration: "Duration (s)", caption: "First caption", draftNote: "It starts with a draft treatment taken from the card; an assistant can rewrite it with the treatment tool.",
    directionTitle: "Direction", treatment: "Treatment", noTreatment: "This animation has no treatment yet. An assistant can write it before generating the HTML: three structures, the shots one by one with why, and the cue map.",
    chosen: "Chosen structure", draft: "draft", shots: "Shots", why: "Why", structures: "Structures", card: "Card",
    cues: "Picture and sound cues", cuesHint: "A cue is a moment where the picture hits and something is heard. The sound events are derived from this list.", noCues: "No cues.",
    time: "Time (s)", visual: "What the picture does", role: "Sound", silent: "No sound", add: "Add cue", remove: "Remove", saveCues: "Save cues", savedCues: "Cues saved as revision",
    cuecheck: "Check sync", cuechecking: "Checking", retime: "Move cues to the real frame", readcheck: "Check reading", readchecking: "Checking",
    status: { ok: "OK", sound_late: "Sound lands late", sound_early: "Sound lands early", no_hit: "The picture does not hit", unclear: "Unclear hit", no_frames: "No frames" },
    cue: "Cue", offset: "Offset (ms)", suggest: "Real frame (s)", unmapped: "Visual hits with no cue", dirty: "Save the composition before changing its direction.",
    tooShort: "Texts that leave before they can be read", never: "Texts never fully visible", text: "Text", seen: "Visible", needs: "Needs (s)", rule: "Rule",
    facts: "Facts and sources (FACTS.md)", factsHint: "For informative videos: each claim with its source. Check compares the numbers on screen with this text.", saveFacts: "Save facts", checkFacts: "Check numbers", unsourced: "Numbers on screen with no source", factsOk: "Every number shown is in the facts.", factsSaved: "Facts saved",
    reviewMode: "Review type", sheet: "Moments sheet", strip: "Frame strip", crop: "Enlarged detail", span: "Span (s)", from: "From", to: "To", point: "Point x, y", selector: "or CSS selector", cropHint: "Crops around the point (or the element) at each moment and enlarges it.",
    soundOwned: "The sound events come from this animation's cues: change them in the Direction section.",
    lint: "Check it is not a slideshow", lintReject: "Slideshow: rewrite the sections with movement", lintWarn: "Only fades, no movement",
    lintPass: "There is movement: not a slideshow", lintOnly: "Only their opacity changes", lintPrompt: "Instruction to rewrite it",
  },
};
const tr = (lang) => words[String(lang).startsWith("es") ? "es" : "en"];

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.detail || `HTTP ${response.status}`);
  return data;
}
const BASE = "/editing/api/html-motion";

export function StylePicker({ lang, busy, projectId, operate, onCreated }) {
  const t = tr(lang);
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [found, setFound] = useState(null);
  const [card, setCard] = useState(null);
  const [name, setName] = useState("");
  const [duration, setDuration] = useState(12);
  const [caption, setCaption] = useState("");
  const search = () => operate(async () => {
    const params = new URLSearchParams({ limit: "40" });
    if (query.trim()) params.set("q", query.trim());
    if (source) params.set("source", source);
    setFound(await request(`${BASE}/styles?${params}`));
  });
  const pick = (id) => operate(async () => {
    const data = await request(`${BASE}/styles/${id}`);
    setCard(data); setName(data.name);
  });
  const create = () => operate(async () => {
    const made = await request(`${BASE}/from-style`, { method: "POST", body: JSON.stringify({
      card: `${card.source}/${card.slug}`, name: name.trim() || undefined, duration: Number(duration), draft_treatment: true,
      caption: caption.trim() || undefined, project_id: projectId || undefined }) });
    await onCreated?.(made.id);
  });
  const parts = card?.variants || {};
  return (
    <fieldset className="html-motion-styles">
      <legend>{t.stylesTitle}</legend>
      <p className="muted">{t.stylesHint}</p>
      <form className="html-motion-actions" onSubmit={(e) => { e.preventDefault(); search(); }}>
        <label className="field-label"><span>{t.search}</span><input className="field" type="search" value={query} placeholder="archive, pixel, neon, HUD..." onChange={(e) => setQuery(e.target.value)} /></label>
        <label className="field-label"><span>{t.source}</span>
          <select className="field" value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">{t.all}</option><option value="lemo-opuscar">Lemo-Opuscar</option><option value="mg-styles-15">MG Styles 15</option>
          </select></label>
        <button className="btn" disabled={busy}>{t.searchGo}</button>
      </form>
      {found && <p className="muted" role="status">{found.count} {t.results}</p>}
      {found && found.items.length === 0 && <p className="muted">{t.none}</p>}
      {found && found.items.length > 0 && <ul className="html-motion-style-list">
        {found.items.map((item) => (
          <li key={item.id} className={card && card.slug === item.slug && card.source === item.source ? "chosen" : ""}>
            <button type="button" className="btn" disabled={busy} onClick={() => pick(item.id)} aria-label={`${t.pick} ${item.name}`}>{t.pick}</button>
            <span><strong>{item.name}</strong> <span className="muted">· {item.category || item.source}</span><br /><span className="muted">{item.line}</span></span>
          </li>))}
      </ul>}
      {card && <div className="html-motion-style-card">
        <h4>{t.picked}: {card.name}</h4>
        <p className="muted">{card.line}</p>
        <p className="muted">{t.license}: {card.license} {card.attribution}</p>
        <div className="html-motion-parts">
          {["safe", "character", "essence", "free"].filter((key) => parts[key]?.sections?.length).map((key) => (
            <div key={key} className={`part part-${key}`}><strong>{t[key]}</strong><span>{parts[key].sections.map((s) => card.sections?.[s]?.title || s).join(" · ")}</span></div>))}
        </div>
        <div className="html-motion-settings">
          <label className="field-label"><span>{t.name}</span><input className="field" value={name} onChange={(e) => setName(e.target.value)} /></label>
          <label className="field-label"><span>{t.duration}</span><input className="field" type="number" min="2" step="any" value={duration} onChange={(e) => setDuration(e.target.value)} /></label>
          <label className="field-label"><span>{t.caption}</span><input className="field" maxLength={60} value={caption} onChange={(e) => setCaption(e.target.value)} /></label>
        </div>
        <p className="muted">{t.draftNote}</p>
        <button type="button" className="btn btn-primary" disabled={busy || Number(duration) < 2} onClick={create}>{t.create}</button>
      </div>}
    </fieldset>
  );
}

export default function HtmlMotionDirection({ saved, dirty, busy, lang, operate, onChanged, onCueCount }) {
  const t = tr(lang);
  const key = `${saved?.id}:${saved?.revision}`;
  const [roles, setRoles] = useState([]);
  const [treatment, setTreatment] = useState(null);
  const [cues, setCues] = useState([]);
  const [notice, setNotice] = useState("");
  const [cut, setCut] = useState(null);
  const [read, setRead] = useState(null);
  const [lint, setLint] = useState(null);
  const [facts, setFacts] = useState("");
  const [factsReport, setFactsReport] = useState(null);

  useEffect(() => { request(`${BASE}/sound/roles`).then((d) => setRoles(d.roles.filter((r) => r.sounded !== false))).catch(() => {}); }, []);
  useEffect(() => {
    let live = true;
    setCut(null); setRead(null); setLint(null); setNotice(""); setFactsReport(null);
    request(`${BASE}/${saved.id}/treatment`).then((d) => live && setTreatment(d.treatment)).catch(() => {});
    request(`${BASE}/${saved.id}/cues`).then((d) => { if (!live) return; setCues(d.cues.map((c) => ({ ...c, sound: c.sound || "" }))); onCueCount?.(d.cues.length); }).catch(() => {});
    request(`${BASE}/${saved.id}/facts`).then((d) => live && setFacts(d.facts || "")).catch(() => {});
    return () => { live = false; };
  }, [key]);

  const edit = (index, patch) => setCues((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  const saveCues = () => operate(async () => {
    const body = { composition: saved.id, action: "set", expected_revision: saved.revision,
      cues: cues.map((c) => ({ ...(c.id ? { id: c.id } : {}), t: Number(c.t), visual: c.visual, ...(c.sound ? { sound: c.sound } : {}) })) };
    const result = await request(`${BASE}/${saved.id}/cues`, { method: "POST", body: JSON.stringify(body) });
    setNotice(`${t.savedCues} ${result.revision}`);
    await onChanged?.();
  });
  const runCut = (retime) => operate(async () => {
    const body = { composition: saved.id, revision: saved.revision, ...(retime ? { retime: true, expected_revision: saved.revision } : {}) };
    const result = await request(`${BASE}/${saved.id}/cuecheck`, { method: "POST", body: JSON.stringify(body) });
    setCut(result);
    if (retime) await onChanged?.();
  });
  const runRead = () => operate(async () => {
    setRead(await request(`${BASE}/${saved.id}/readcheck`, { method: "POST", body: JSON.stringify({ composition: saved.id, revision: saved.revision }) }));
  });
  const runLint = () => operate(async () => {
    setLint(await request(`${BASE}/${saved.id}/lint`, { method: "POST", body: JSON.stringify({ composition: saved.id, revision: saved.revision }) }));
  });
  const saveFacts = () => operate(async () => {
    await request(`${BASE}/${saved.id}/facts`, { method: "POST", body: JSON.stringify({ composition: saved.id, action: "set", content: facts }) });
    setNotice(t.factsSaved); setFactsReport(null);
  });
  const checkFacts = () => operate(async () => {
    setFactsReport(await request(`${BASE}/${saved.id}/facts`, { method: "POST", body: JSON.stringify({ composition: saved.id, revision: saved.revision, action: "check" }) }));
  });
  const fixable = cut?.cues?.some((c) => c.suggest_t !== undefined && (c.status === "sound_late" || c.status === "sound_early"));
  const structure = treatment?.structures?.[treatment.chosen];

  return (
    <fieldset className="html-motion-direction" disabled={dirty}>
      <legend>{t.directionTitle}</legend>
      {dirty && <p className="muted">{t.dirty}</p>}
      <h4>{t.treatment}</h4>
      {!treatment ? <p className="muted">{t.noTreatment}</p> : <div className="html-motion-treatment">
        <p>{treatment.card && <span className="muted">{t.card}: {treatment.card} · </span>}<strong>{t.chosen}: {structure?.name}</strong>{treatment.draft && <span className="badge"> {t.draft}</span>}</p>
        <p className="muted">{structure?.idea} {treatment.reason}</p>
        <ol className="html-motion-shots">{treatment.shots.map((s, i) => <li key={i}><strong>{s.t} s</strong> {s.what}<br /><span className="muted">{t.why}: {s.why}</span></li>)}</ol>
      </div>}
      <h4>{t.cues}</h4>
      <p className="muted">{t.cuesHint}</p>
      {cues.length === 0 ? <p className="muted">{t.noCues}</p> : (
        <table className="html-motion-sound-table">
          <thead><tr><th>{t.time}</th><th>{t.visual}</th><th>{t.role}</th><th /></tr></thead>
          <tbody>{cues.map((c, i) => (
            <tr key={c.id || i}>
              <td><input className="field" aria-label={`${t.time} ${i + 1}`} type="number" min="0" max={saved.duration} step="any" value={c.t} onChange={(e) => edit(i, { t: e.target.value })} /></td>
              <td><input className="field" aria-label={`${t.visual} ${i + 1}`} value={c.visual} onChange={(e) => edit(i, { visual: e.target.value })} /></td>
              <td><select className="field" aria-label={`${t.role} ${i + 1}`} value={c.sound} onChange={(e) => edit(i, { sound: e.target.value })}>
                <option value="">{t.silent}</option>{roles.map((r) => <option key={r.role} value={r.role}>{r.role}</option>)}
                {c.sound && !roles.some((r) => r.role === c.sound) && <option value={c.sound}>{c.sound}</option>}
              </select></td>
              <td><button type="button" className="btn" onClick={() => setCues((rows) => rows.filter((_, j) => j !== i))}>{t.remove}</button></td>
            </tr>))}</tbody>
        </table>)}
      <div className="html-motion-actions">
        <button type="button" className="btn" disabled={busy} onClick={() => setCues((rows) => [...rows, { t: rows.length ? Math.min(Number(saved.duration), Number(rows[rows.length - 1].t) + 0.5) : 0.2, visual: "", sound: "tap" }])}>{t.add}</button>
        <button type="button" className="btn btn-primary" disabled={busy || cues.some((c) => c.visual.trim().length < 2)} onClick={saveCues}>{t.saveCues}</button>
        <button type="button" className="btn" disabled={busy || !cues.length} onClick={() => runCut(false)}>{t.cuecheck}</button>
        {fixable && <button type="button" className="btn" disabled={busy} onClick={() => runCut(true)}>{t.retime}</button>}
        <button type="button" className="btn" disabled={busy} onClick={runRead}>{t.readcheck}</button>
        <button type="button" className="btn" disabled={busy} onClick={runLint} data-testid="motion-lint">{t.lint}</button>
        {notice && <span role="status">{notice}</span>}
      </div>
      {lint && <div className="html-motion-report" role="status" data-testid="motion-lint-report">
        <p className={lint.verdict === "pass" ? "ok" : "warn"}><strong>{lint.verdict === "reject" ? t.lintReject : lint.verdict === "warn" ? t.lintWarn : t.lintPass}</strong>
          {" "}<span className="muted">{lint.opacity_only_count} / {lint.threshold}</span></p>
        {lint.opacity_only.length > 0 && <p className="muted">{t.lintOnly}: {lint.opacity_only.join(", ")}</p>}
        {lint.reprompt && <><p>{t.lintPrompt}</p><textarea className="field html-motion-source" rows={3} readOnly value={lint.reprompt} /></>}
      </div>}
      {cut && <div className="html-motion-report" role="status">
        <p className={cut.ok ? "ok" : "warn"}><strong>{cut.summary}</strong> <span className="muted">±{cut.tolerance_ms} ms</span></p>
        <table className="html-motion-sound-table"><thead><tr><th>{t.cue}</th><th>{t.time}</th><th>{t.visual}</th><th>{t.offset}</th><th>{t.suggest}</th><th /></tr></thead>
          <tbody>{cut.cues.map((c) => <tr key={c.id}><td>{c.id}</td><td>{c.t}</td><td>{c.visual}</td><td>{c.offset_ms ?? "-"}</td><td>{c.suggest_t ?? ""}</td><td className={c.status === "ok" ? "ok" : "warn"}>{t.status[c.status] || c.status}</td></tr>)}</tbody></table>
        {cut.unmapped_hits?.length > 0 && <p className="warn">{t.unmapped}: {cut.unmapped_hits.map((u) => `${u.t} s`).join(", ")}</p>}
        {cut.warning && <p className="muted">{cut.warning}</p>}
      </div>}
      {read && <div className="html-motion-report" role="status">
        <p className={read.ok ? "ok" : "warn"}><strong>{read.summary}</strong> <span className="muted">{t.rule}: {read.rule}</span></p>
        {read.note && <p className="muted">{read.note}</p>}
        {read.too_short.length > 0 && <><p>{t.tooShort}</p><table className="html-motion-sound-table"><thead><tr><th>{t.text}</th><th>{t.seen}</th><th>{t.needs}</th></tr></thead>
          <tbody>{read.too_short.map((r, i) => <tr key={i}><td>{r.text}</td><td>{r.from}-{r.to} s ({r.dwell_s})</td><td>{r.need_s}</td></tr>)}</tbody></table></>}
        {read.never_readable.length > 0 && <><p>{t.never}</p><ul>{read.never_readable.map((r, i) => <li key={i}>{r.text} <span className="muted">· {r.reason}</span></li>)}</ul></>}
      </div>}
      <h4>{t.facts}</h4>
      <p className="muted">{t.factsHint}</p>
      <textarea className="field html-motion-source" rows={4} spellCheck={false} aria-label={t.facts} value={facts} onChange={(e) => setFacts(e.target.value)} />
      <div className="html-motion-actions">
        <button type="button" className="btn" disabled={busy} onClick={saveFacts}>{t.saveFacts}</button>
        <button type="button" className="btn" disabled={busy || !facts.trim()} onClick={checkFacts}>{t.checkFacts}</button>
      </div>
      {factsReport && <div className="html-motion-report" role="status">
        <p className={factsReport.ok ? "ok" : "warn"}><strong>{factsReport.ok ? t.factsOk : factsReport.summary}</strong></p>
        {factsReport.unsourced.length > 0 && <p className="warn">{t.unsourced}: {factsReport.unsourced.map((u) => `${u.number} (${u.first_seen} s)`).join(", ")}</p>}
      </div>}
    </fieldset>
  );
}
