import { useEffect, useState } from "react";
import { ImagePlus, Loader2, Printer, Stamp, X } from "lucide-react";
import { api, thumbUrl, type DesignTemplate } from "../api";
import { useT } from "../i18n";
import { AssetPicker, useApp, useAsync, useDebounced } from "../components/ui";

const LABELS: Record<string, Record<string, string>> = {
  en: {
    photocard_front: "Photocard · front", photocard_back: "Photocard · back", album_cover: "Album cover",
    teaser_poster: "Teaser poster", lyric_card: "Lyric card", tracklist_back: "Tracklist back", thumbnail: "Video thumbnail",
  },
  es: {
    photocard_front: "Photocard · anverso", photocard_back: "Photocard · reverso", album_cover: "Portada de álbum",
    teaser_poster: "Cartel teaser", lyric_card: "Tarjeta de letra", tracklist_back: "Contraportada", thumbnail: "Miniatura de vídeo",
  },
};

export function DesignerView() {
  const { t, lang } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const templates = useAsync(() => api.templates(), []);
  const project = useAsync(() => api.project(pid), [pid]);
  const firstImage = useAsync(() => api.assets(pid, { kind: "image", source: "generated", min_rating: 5, limit: 1 }), [pid]);
  const cast = useAsync(() => api.characters(pid), [pid]);
  const [name, setName] = useState("photocard_front");
  const [fieldsByTemplate, setFieldsByTemplate] = useState<Record<string, Record<string, string>>>({});
  const [variant, setVariant] = useState<string | null>(null);
  const [print, setPrint] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [picking, setPicking] = useState<string | null>(null);
  const [rendering, setRendering] = useState(false);

  const tpl: DesignTemplate | undefined = templates.data?.items.find((x) => x.template === name);
  const fields = fieldsByTemplate[name] || {};
  const setField = (k: string, v: string) => setFieldsByTemplate((all) => ({ ...all, [name]: { ...(all[name] || {}), [k]: v } }));

  // sensible starting values from the project
  useEffect(() => {
    if (!project.data || !firstImage.data || !cast.data || !tpl || fieldsByTemplate[name]) return;
    const p = project.data;
    const seed: Record<string, string> = { accent: "#ff4d8d" };
    const img = firstImage.data.items[0]?.id;
    for (const f of tpl.fields) if (f.type === "image" && f.required && img) seed[f.name] = img;
    if (tpl?.fields.some((f) => f.name === "title")) seed.title = p.name;
    if (tpl?.fields.some((f) => f.name === "group_name")) seed.group_name = p.name;
    const member = cast.data.items[0];
    if (tpl?.fields.some((f) => f.name === "member_name")) seed.member_name = member?.name || "Member";
    if (tpl?.fields.some((f) => f.name === "role") && member?.role) seed.role = member.role;
    if (member?.canonical_asset_id) for (const f of tpl.fields) if (f.type === "image" && f.required) seed[f.name] = member.canonical_asset_id;
    if (member?.palette[0]) seed.accent = member.palette[0];
    if (tpl?.fields.some((f) => f.name === "quote")) seed.quote = "Such stuff as dreams are made on";
    if (tpl?.fields.some((f) => f.name === "tracks")) seed.tracks = "1. Intro\n2. Afterglow\n3. Static Heart";
    setFieldsByTemplate((all) => ({ ...all, [name]: seed }));
  }, [project.data, firstImage.data, cast.data, name, tpl, fieldsByTemplate]);

  useEffect(() => { setVariant(tpl?.variants[0] || null); }, [tpl]);

  // debounce template + fields together so a template switch never previews
  // the previous template's fields
  const dState = useDebounced(JSON.stringify({ name, variant, fields }), 350);
  useEffect(() => {
    const state = JSON.parse(dState) as { name: string; variant: string | null; fields: Record<string, string> };
    if (!tpl || state.name !== name || !fieldsByTemplate[name]) return;  // wait for the starting values
    let alive = true;
    let url: string | null = null;
    setPreviewing(true);
    const clean = Object.fromEntries(Object.entries(state.fields).filter(([, v]) => v));
    api.preview(state.name, clean, state.variant).then((blob) => {
      if (!alive) return;
      url = URL.createObjectURL(blob);
      setPreview(url);
      setPreviewError(null);
    }).catch((e) => alive && setPreviewError(e.message)).finally(() => alive && setPreviewing(false));
    return () => { alive = false; if (url) setTimeout(() => URL.revokeObjectURL(url!), 2000); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dState, tpl]);

  const render = async () => {
    setRendering(true);
    try {
      const clean = Object.fromEntries(Object.entries(fields).filter(([, v]) => v));
      const a = await api.design(pid, name, clean, variant, print);
      app.toast(t("rendered", { name: a.name || a.id }), "ok");
      app.bump();
      app.openAsset(a.id);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setRendering(false);
    }
  };

  return (
    <>
      <div className="page-head"><div><h1>{t("designerTitle")}</h1></div></div>
      <div className="designer">
        <div className="template-list">
          <div className="panel-title">{t("templates")}</div>
          {(templates.data?.items || []).map((x) => (
            <button key={x.template} className={`template-item${x.template === name ? " on" : ""}`} onClick={() => setName(x.template)}>
              <div>{LABELS[lang]?.[x.template] || x.template}</div>
              <div className="dims">{x.width} x {x.height}</div>
            </button>
          ))}
        </div>
        <div className="card stack">
          <div className="panel-title">{t("fields")}</div>
          {tpl?.variants.length ? (
            <label className="field">{t("variant")}
              <div className="segmented">
                {tpl.variants.map((v) => <button key={v} className={variant === v ? "on" : ""} onClick={() => setVariant(v)}>{v.replace("_", " ")}</button>)}
              </div>
            </label>
          ) : null}
          {tpl?.fields.map((f) => (
            <div key={f.name} className="field">
              <span><strong style={{ fontWeight: 550, color: "var(--text)" }}>{f.name.replace("_", " ")}{f.required ? " *" : ""}</strong> <span className="hint">· {f.description}</span></span>
              {f.type === "image" ? (
                <div className="drop-slot" onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => { const id = e.dataTransfer.getData("text/prospero-asset"); if (id) setField(f.name, id); }}>
                  {fields[f.name] ? <img src={thumbUrl({ id: fields[f.name], thumb_path: "x", kind: "image" })} alt="" /> : <ImagePlus size={20} />}
                  <button className="btn sm" onClick={() => setPicking(f.name)}>{t("pickImage")}</button>
                  {fields[f.name] && <button className="btn sm icon ghost" onClick={() => setField(f.name, "")}><X size={14} /></button>}
                </div>
              ) : f.type === "colour" ? (
                <div className="row">
                  <input type="color" value={(fields[f.name] || "#ff4d8d").slice(0, 7)} onChange={(e) => setField(f.name, e.target.value)}
                    style={{ width: 44, height: 34, padding: 2, background: "var(--surface)", border: "1px solid var(--border-strong)", borderRadius: 8 }} />
                  <input className="mono grow" value={fields[f.name] || ""} onChange={(e) => setField(f.name, e.target.value)} placeholder="#ff4d8d" />
                </div>
              ) : f.name === "tracks" || f.name === "message" || f.name === "quote" ? (
                <textarea value={fields[f.name] || ""} rows={f.name === "tracks" ? 5 : 3} onChange={(e) => setField(f.name, e.target.value)} />
              ) : (
                <input value={fields[f.name] || ""} onChange={(e) => setField(f.name, e.target.value)} />
              )}
            </div>
          ))}
          <label className="check"><input type="checkbox" checked={print} onChange={(e) => setPrint(e.target.checked)} /> <Printer size={14} /> {t("printBleed")}</label>
          <button className="btn primary lg" onClick={render} disabled={rendering}>
            {rendering ? <Loader2 size={17} className="spin" /> : <Stamp size={17} />} {t("renderFull")}
          </button>
        </div>
        <div className="stack">
          <div className="panel-title">{t("livePreview")}</div>
          <div className="preview-stage">
            {previewing && <Loader2 size={18} className="spin busy" />}
            {previewError ? <p className="err-text">{previewError}</p> : preview && <img src={preview} alt="" />}
          </div>
        </div>
      </div>
      {picking && <AssetPicker projectId={pid} onClose={() => setPicking(null)} onPick={(a) => { setField(picking, a.id); setPicking(null); }} />}
    </>
  );
}
