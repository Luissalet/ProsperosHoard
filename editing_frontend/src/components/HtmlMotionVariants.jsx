import React, { useEffect, useState } from "react";

const words = {
  en: { title: "Variants from data", hint: "Write {{name}} where a value goes (and, for the script, a motion-props JSON block with the defaults). Each row below becomes its own composition; the template is not changed.",
    none: "This composition has no parameters yet: write {{title}} (or any name) in its HTML and save it.", params: "Parameters", rows: "Rows (CSV with a header, or JSON)",
    nameField: "Column that names each variant", render: "Also export each one to video", make: "Create variants", made: "variants created", queued: "queued for export",
    dirty: "Save the composition before making variants.", open: "Open" },
  es: { title: "Variantes con datos", hint: "Escribe {{nombre}} donde va un valor (y, para el script, un bloque JSON motion-props con los valores por defecto). Cada fila de abajo se convierte en su propia composición; la plantilla no cambia.",
    none: "Esta composición aún no tiene parámetros: escribe {{titulo}} (o cualquier nombre) en su HTML y guárdala.", params: "Parámetros", rows: "Filas (CSV con cabecera, o JSON)",
    nameField: "Columna que da nombre a cada variante", render: "Exportar también cada una a vídeo", make: "Crear variantes", made: "variantes creadas", queued: "en cola para exportar",
    dirty: "Guarda la composición antes de crear variantes.", open: "Abrir" },
};

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.detail || `HTTP ${response.status}`);
  return data;
}

export default function HtmlMotionVariants({ saved, dirty, busy, lang, operate, onOpen }) {
  const t = words[String(lang).startsWith("es") ? "es" : "en"];
  const [info, setInfo] = useState(null);
  const [rows, setRows] = useState("");
  const [nameField, setNameField] = useState("");
  const [render, setRender] = useState(false);
  const [made, setMade] = useState(null);
  const key = `${saved?.id}:${saved?.revision}`;

  useEffect(() => {
    setInfo(null); setMade(null);
    if (!saved?.id) return;
    request(`/editing/api/html-motion/${saved.id}/props?revision=${saved.revision}`).then((data) => {
      setInfo(data);
      const names = data.props.map((p) => p.name);
      if (names.length) {
        setRows((current) => current || `${names.join(",")}\n${data.props.map((p) => (p.default ?? "")).join(",")}`);
        setNameField((current) => current || names[0]);
      }
    }).catch(() => setInfo({ props: [] }));
  }, [key]);

  const create = () => operate(async () => {
    setMade(await request(`/editing/api/html-motion/${saved.id}/variants`, { method: "POST", body: JSON.stringify({ rows, revision: saved.revision, name_field: nameField, render }) }));
  });

  if (!saved) return null;
  return (
    <section className="html-motion-variants" aria-labelledby="html-motion-variants-title" data-testid="html-motion-variants">
      <h4 id="html-motion-variants-title">{t.title}</h4>
      <p className="muted">{t.hint}</p>
      {info && !info.props.length && <p className="muted" data-testid="html-motion-variants-none">{t.none}</p>}
      {info?.props?.length > 0 && <>
        <p><strong>{t.params}:</strong> {info.props.map((p) => <code key={p.name} title={p.type}>{p.name}{p.default !== null && p.default !== undefined ? ` = ${String(p.default)}` : ""} </code>)}</p>
        <label className="field-label"><span>{t.rows}</span><textarea className="field" rows={5} spellCheck={false} value={rows} onChange={(e) => setRows(e.target.value)} data-testid="html-motion-variants-rows" /></label>
        <div className="html-motion-actions">
          <label className="field-label"><span>{t.nameField}</span><select className="field" value={nameField} onChange={(e) => setNameField(e.target.value)}>{info.props.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}</select></label>
          <label><input type="checkbox" checked={render} onChange={(e) => setRender(e.target.checked)} /> {t.render}</label>
          <button type="button" className="btn btn-primary" disabled={busy || dirty || !rows.trim()} onClick={create} data-testid="html-motion-variants-make">{t.make}</button>
        </div>
        {dirty && <p className="muted">{t.dirty}</p>}
      </>}
      {made && <div role="status" data-testid="html-motion-variants-result">
        <p>{made.count} {t.made}{made.rendering ? ` · ${t.queued}` : ""}</p>
        <ul>{made.variants.map((v) => <li key={v.composition}>{v.name}{onOpen && <> <button type="button" className="btn" onClick={() => onOpen(v.composition)}>{t.open}</button></>}</li>)}</ul>
      </div>}
    </section>
  );
}
