import { useEffect, useRef, useState } from "react";
import { Download, ImagePlus, Plus, Redo2, Save, Trash2, Undo2 } from "lucide-react";
import { api, fileUrl, type Asset, type SpatialDocument, type SpatialItem } from "../api";
import { AssetPicker, useApp, useAsync } from "../components/ui";
import { useT } from "../i18n";
import "./spatial.css";

const blank = (): SpatialDocument => ({ format: "prospero.composition/1", width: 1024, height: 1024, caption: "", items: [] });

export function SpatialView() {
  const app = useApp();
  const { lang } = useT();
  const es = lang === "es";
  const pid = app.projectId!;
  const sources = useAsync(() => api.spatialList(pid), [pid, app.dataVersion]);
  const key = `prospero-spatial:${pid}`;
  const [doc, setDoc] = useState<SpatialDocument>(blank);
  const [name, setName] = useState(es ? "Composición" : "Composition");
  const [source, setSource] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [output, setOutput] = useState("");
  const [picking, setPicking] = useState(false);
  const [loaded, setLoaded] = useState("");
  const [older, setOlder] = useState<Asset[]>([]), [nextOffset, setNextOffset] = useState<number | null>(null);
  const past = useRef<SpatialDocument[]>([]), future = useRef<SpatialDocument[]>([]);
  const stage = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: string; x: number; y: number; box: SpatialItem["box"]; scale: number; before: SpatialDocument } | null>(null);
  const item = doc.items.find(x => x.id === selected);
  const text = (a: string, b: string) => es ? a : b;

  useEffect(() => {
    try {
      const draft = JSON.parse(sessionStorage.getItem(key) || "null");
      setDoc(draft?.document || blank()); setName(draft?.name || text("Composición", "Composition")); setSource(draft?.source || null);
    } catch { setDoc(blank()); setSource(null); }
    past.current = []; future.current = []; setSelected(""); setLoaded(key);
  }, [key]);
  useEffect(() => {
    if (loaded === key) sessionStorage.setItem(key, JSON.stringify({ document: doc, name, source }));
  }, [key, doc, name, source, loaded]);
  useEffect(() => { setOlder([]); setNextOffset(sources.data?.next_offset ?? null); }, [sources.data]);
  useEffect(() => {
    window.dispatchEvent(new CustomEvent("prospero:spatial-context", { detail: { source_id: source, selected_id: selected, document: doc } }));
  }, [source, selected, doc]);
  useEffect(() => () => { window.dispatchEvent(new CustomEvent("prospero:spatial-context", { detail: null })); }, []);

  const change = (next: SpatialDocument) => { past.current.push(doc); future.current = []; setDoc(next); setOutput(""); };
  const edit = (fields: Partial<SpatialItem>) => item && change({ ...doc, items: doc.items.map(x => x.id === item.id ? { ...x, ...fields } : x) });
  const move = (box: SpatialItem["box"], dx: number, dy: number): SpatialItem["box"] => [
    Math.max(0, Math.min(doc.width - box[2], Math.round(box[0] + dx))),
    Math.max(0, Math.min(doc.height - box[3], Math.round(box[1] + dy))), box[2], box[3],
  ];
  const run = async (work: () => Promise<void>) => { setBusy(true); setError(""); try { await work(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const save = async () => { const result = await api.spatialSave(pid, name, doc, source); setSource(result.id); sources.reload(); return result.id; };
  const download = (data: string, filename: string) => {
    const url = URL.createObjectURL(new Blob([data], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const add = (kind: SpatialItem["kind"]) => {
    const id = crypto.randomUUID();
    change({ ...doc, items: [...doc.items, { id, kind, caption: kind === "object" ? text("Describe el objeto", "Describe the object") : "", text: kind === "text" ? text("Tu texto", "Your text") : "", box: [Math.min(48, doc.width/8), Math.min(48, doc.height/8), Math.min(320, doc.width/2), Math.min(180, doc.height/3)] }] });
    setSelected(id);
  };

  return <section className="spatial-editor" aria-label={text("Composición de imagen", "Image composition")}>
    <div className="page-head"><h1>{text("Composición de imagen", "Image composition")}</h1></div>
    <p className="hint">{text("Coloca objetos y texto antes de generar. Las cajas indican la disposición solicitada; revisa después la imagen del motor.", "Place objects and text before generation. Boxes describe the requested arrangement; inspect the engine's resulting image.")}</p>
    <fieldset disabled={busy} className="spatial-controls">
      <div className="spatial-toolbar">
        <button className="btn" onClick={() => { change(blank()); setSource(null); setName(text("Composición", "Composition")); setSelected(""); }}>{text("Nueva", "New")}</button>
        <label className="field grow">{text("Abrir fuente guardada", "Open saved source")}
          <select value={source || ""} onChange={e => e.target.value && run(async () => { const result = await api.spatialGet(e.target.value); change(result.document); setName(result.name); setSource(result.id); setSelected(""); })}>
            <option value="">{text("Borrador", "Draft")}</option>{[...(sources.data?.items || []), ...older].map(s => <option key={s.id} value={s.id}>{s.name} · {s.id.slice(-6)}</option>)}
          </select>
        </label>
        {nextOffset !== null && <button className="btn" onClick={() => run(async () => { const page = await api.spatialList(pid, nextOffset); setOlder(all => [...all, ...page.items.filter(x => !all.some(a => a.id === x.id))]); setNextOffset(page.next_offset); })}>{text("Más fuentes", "More sources")}</button>}
        <button className="btn primary" onClick={() => run(async () => { await save(); app.toast(text("Fuente guardada", "Source saved"), "ok"); })}><Save size={16} />{text("Guardar copia", "Save copy")}</button>
      </div>
      {(error || sources.error) && <p role="alert" className="err-text">{error || sources.error}</p>}
      <div className="spatial-toolbar">
        <button className="btn" onClick={() => add("object")}><Plus size={16} />{text("Objeto", "Object")}</button>
        <button className="btn" onClick={() => add("text")}><Plus size={16} />{text("Texto", "Text")}</button>
        <button className="btn icon" aria-label={text("Deshacer", "Undo")} disabled={!past.current.length} onClick={() => { const previous = past.current.pop(); if (previous) { future.current.push(doc); setDoc(previous); } }}><Undo2 size={16} /></button>
        <button className="btn icon" aria-label={text("Rehacer", "Redo")} disabled={!future.current.length} onClick={() => { const next = future.current.pop(); if (next) { past.current.push(doc); setDoc(next); } }}><Redo2 size={16} /></button>
        <span className="hint">{doc.width} × {doc.height} · {doc.items.length} {text("elementos", "elements")}</span>
      </div>
      <div className="spatial-workbench">
        <div className="spatial-artboard" ref={stage} style={{ aspectRatio: `${doc.width}/${doc.height}` }} aria-label={text("Lienzo de composición", "Composition canvas")}
          onPointerMove={e => { const d = drag.current; if (!d) return; const box = move(d.box, (e.clientX-d.x)/d.scale, (e.clientY-d.y)/d.scale); setDoc(current => ({ ...current, items: current.items.map(x => x.id === d.id ? { ...x, box } : x) })); }}
          onPointerUp={() => { const d = drag.current; if (d) { past.current.push(d.before); future.current = []; drag.current = null; setOutput(""); } }}
          onPointerCancel={() => { const d = drag.current; if (d) setDoc(d.before); drag.current = null; }}>
          {!doc.items.length && <p className="spatial-empty">{text("Añade un objeto o texto para empezar.", "Add an object or text to begin.")}</p>}
          {doc.items.map(x => <button key={x.id} className={`spatial-box${selected === x.id ? " selected" : ""}`} aria-pressed={selected === x.id}
            style={{ left: `${100*x.box[0]/doc.width}%`, top: `${100*x.box[1]/doc.height}%`, width: `${100*x.box[2]/doc.width}%`, height: `${100*x.box[3]/doc.height}%`, background: x.fill || "#e4edf3", color: x.colour || "#172c3e" }}
            onClick={() => setSelected(x.id)}
            onPointerDown={e => { if (busy) return; setSelected(x.id); e.currentTarget.setPointerCapture(e.pointerId); drag.current = { id: x.id, x: e.clientX, y: e.clientY, box: [...x.box], scale: (stage.current?.clientWidth || doc.width)/doc.width, before: doc }; }}
            onKeyDown={e => { const delta: Record<string, [number, number]> = { ArrowLeft: [-1,0], ArrowRight: [1,0], ArrowUp: [0,-1], ArrowDown: [0,1] }; const d = delta[e.key]; if (d) { e.preventDefault(); setSelected(x.id); change({ ...doc, items: doc.items.map(i => i.id === x.id ? { ...i, box: move(i.box, d[0]*(e.shiftKey ? 10 : 1), d[1]*(e.shiftKey ? 10 : 1)) } : i) }); } }}>
            {x.reference_asset_id && <img src={fileUrl(x.reference_asset_id)} alt="" draggable={false} />}
            <span>{x.kind === "text" ? x.text : x.caption || text("Objeto", "Object")}</span>
          </button>)}
        </div>
        <div className="spatial-inspector stack">
          <label className="field">{text("Nombre", "Name")}<input value={name} onChange={e => setName(e.target.value)} /></label>
          <label className="field">{text("Descripción general", "Overall description")}<textarea rows={3} value={doc.caption} onChange={e => change({ ...doc, caption: e.target.value })} /></label>
          <div className="spatial-numbers">{(["width", "height"] as const).map(k => <label className="field" key={k}>{k === "width" ? text("Ancho", "Width") : text("Alto", "Height")}<input type="number" min={1} value={doc[k]} onChange={e => change({ ...doc, [k]: Math.max(1, Number(e.target.value)) })} /></label>)}</div>
          <label className="field">{text("Seleccionar elemento", "Select element")}<select value={selected} onChange={e => setSelected(e.target.value)}><option value="">{text("Sin selección", "No selection")}</option>{doc.items.map(x => <option key={x.id} value={x.id}>{x.text || x.caption || x.id}</option>)}</select></label>
          {item && <>
            <label className="field">{text("Descripción del elemento", "Element description")}<textarea rows={2} value={item.caption} onChange={e => edit({ caption: e.target.value })} /></label>
            {item.kind === "text" && <label className="field">{text("Texto exacto", "Exact text")}<input value={item.text || ""} onChange={e => edit({ text: e.target.value })} /></label>}
            <div className="spatial-numbers">{["X", "Y", text("Ancho", "Width"), text("Alto", "Height")].map((label, i) => <label key={i} className="field">{label}<input type="number" value={item.box[i]} onChange={e => { const box = [...item.box] as SpatialItem["box"]; box[i] = Number(e.target.value); edit({ box }); }} /></label>)}</div>
            <div className="spatial-numbers"><label className="field">{text("Fondo de vista previa", "Preview background")}<input type="color" value={item.fill || "#e4edf3"} onChange={e => edit({ fill: e.target.value })} /></label><label className="field">{text("Color del texto", "Text colour")}<input type="color" value={item.colour || "#172c3e"} onChange={e => edit({ colour: e.target.value })} /></label></div>
            <button className="btn" onClick={() => setPicking(true)}><ImagePlus size={16} />{text("Imagen de referencia", "Reference image")}</button>
            {item.reference_asset_id && <button className="btn" onClick={() => edit({ reference_asset_id: "" })}>{text("Quitar referencia", "Remove reference")}</button>}
            <button className="btn" onClick={() => { change({ ...doc, items: doc.items.filter(x => x.id !== selected) }); setSelected(""); }}><Trash2 size={16} />{text("Eliminar elemento", "Delete element")}</button>
          </>}
        </div>
      </div>
      <div className="spatial-toolbar">
        <button className="btn" onClick={() => run(async () => { const result = await api.spatialCompile(doc, "caption"); setOutput(result.prompt); })}>{text("Ver instrucciones", "View instructions")}</button>
        <button className="btn" onClick={() => run(async () => { const result = await api.spatialCompile(doc, "moss"); setOutput(result.prompt); })}>{text("Formato MOSS", "MOSS format")}</button>
        <button className="btn" onClick={() => download(JSON.stringify(doc, null, 2), "composition.json")}><Download size={16} />JSON</button>
        <label className="btn spatial-import">{text("Importar JSON", "Import JSON")}<input type="file" accept=".json,application/json" onChange={e => { const file = e.target.files?.[0]; if (file) run(async () => { const next = JSON.parse(await file.text()); await api.spatialCompile(next, "caption"); change(next); setSource(null); setSelected(""); }); e.target.value = ""; }} /></label>
        <button className="btn" onClick={() => run(async () => { const id = await save(); const result = await api.spatialPreview(id); app.bump(); app.openAsset(result.asset.id); })}>{text("Guardar vista previa PNG", "Save PNG preview")}</button>
        <button className="btn primary" disabled={!doc.items.length} onClick={() => run(async () => { const id = await save(); const result = await api.generate(pid, { prompt: "", composition_asset_id: id, engine: "qwen21" }); app.refreshJobs(); app.toast(text("Generación encolada", "Generation queued"), "ok"); setOutput(result.final_prompt); })}>{text("Generar con Qwen", "Generate with Qwen")}</button>
      </div>
      {busy && <p role="status">{text("Procesando…", "Working…")}</p>}
      {source && <p className="hint spatial-source">{text("Fuente guardada", "Saved source")}: {source}</p>}
      {output && <label className="field">{text("Instrucciones compiladas", "Compiled instructions")}<textarea readOnly rows={8} value={output} /></label>}
    </fieldset>
    {picking && item && <AssetPicker projectId={pid} kind="image" onClose={() => setPicking(false)} onPick={a => { edit({ reference_asset_id: a.id }); setPicking(false); }} />}
  </section>;
}
