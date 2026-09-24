import { Fragment, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Dices, Download, Film, Heart, Maximize2, Minimize2, Repeat, Sparkles, Wand2, X } from "lucide-react";
import { api, fileUrl, type Asset, type Board } from "../api";
import { useT } from "../i18n";
import { Stars, useApp, useAsync, useDialogFocus } from "./ui";

export function Lightbox({ assetId, list, onClose, onNavigate }: {
  assetId: string; list: string[]; onClose: () => void; onNavigate: (id: string) => void;
}) {
  const { t } = useT();
  const app = useApp();
  const [asset, setAsset] = useState<Asset | null>(null);
  const [zoom, setZoom] = useState(false);
  const [tags, setTags] = useState("");
  const [notes, setNotes] = useState("");
  const [editPrompt, setEditPrompt] = useState("");
  const [strength, setStrength] = useState(0.55);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useDialogFocus(ref);
  const boards = useAsync(() => (asset ? api.boards(asset.project_id) : Promise.resolve({ items: [] as Board[] })), [asset?.project_id]);

  useEffect(() => {
    let alive = true;
    setZoom(false);
    api.asset(assetId).then((a) => {
      if (!alive) return;
      setAsset(a);
      setTags(a.tags.join(", "));
      setNotes(a.notes || "");
      setEditPrompt(String((a.recipe?.params as Record<string, unknown> | undefined)?.positive_prompt || ""));
    }).catch((e) => app.toast(e.message, "bad"));
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetId]);

  const idx = list.indexOf(assetId);
  const move = (d: number) => {
    if (list.length < 2) return;
    onNavigate(list[(idx + d + list.length) % list.length]);
  };

  const patch = async (p: Parameters<typeof api.updateAsset>[1]) => {
    if (!asset) return;
    try {
      const a = await api.updateAsset(asset.id, p);
      setAsset(a);
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      const typing = el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT");
      if (e.key === "Escape") onClose();
      if (typing) return;
      if (e.key === "ArrowRight" || e.key === "j" || e.key === "J") move(1);
      if (e.key === "ArrowLeft" || e.key === "k" || e.key === "K") move(-1);
      if ((e.key === "f" || e.key === "F") && asset) patch({ favourite: !asset.favourite });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const run = async (label: string, fn: () => Promise<{ job: { id: string } }>) => {
    setBusy(true);
    try {
      const r = await fn();
      app.toast(t("jobQueued", { id: `${label} ${r.job.id.slice(-6)}` }), "ok");
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  if (!asset) return <div ref={ref} className="overlay" onClick={onClose} role="dialog" aria-modal="true" aria-busy="true" aria-label={t("loading")} tabIndex={-1} />;
  const recipe = asset.recipe;
  const params = (recipe?.params || {}) as Record<string, unknown>;
  const fromComfy = recipe?.backend === "comfyui";
  const paramRows: [string, unknown][] = [
    ["template", recipe?.template], ["checkpoint", recipe?.checkpoint], ["seed", params.seed], ["steps", params.steps],
    ["cfg", params.cfg], ["sampler", params.sampler], ["scheduler", params.scheduler], ["denoise", params.denoise],
    ["size", params.width ? `${params.width}x${params.height}` : undefined], ["style", recipe?.style],
    ["hash", recipe?.template_hash], ["elapsed", recipe?.elapsed_s ? `${recipe.elapsed_s} s` : undefined],
  ];

  return (
    <div ref={ref} className="overlay" role="dialog" aria-modal="true" aria-label={asset.name || asset.id} tabIndex={-1}>
      <div className="lightbox-stage" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
        <div className="lightbox-tools">
          <button className="btn sm" onClick={onClose}><X size={15} /> {t("close")}</button>
          {asset.kind === "image" && (
            <button className="btn sm" onClick={() => setZoom(!zoom)}>{zoom ? <Minimize2 size={15} /> : <Maximize2 size={15} />} {zoom ? t("fit") : t("zoom")}</button>
          )}
          <a className="btn sm" href={`${fileUrl(asset.id)}?download=true`}><Download size={15} /> {t("download")}</a>
        </div>
        {list.length > 1 && <>
          <button className="btn icon lightbox-nav prev" onClick={() => move(-1)} aria-label={t("previous")} title={t("previous")}><ChevronLeft size={18} /></button>
          <button className="btn icon lightbox-nav next" onClick={() => move(1)} aria-label={t("next")} title={t("next")}><ChevronRight size={18} /></button>
        </>}
        {asset.kind === "image" && <img src={fileUrl(asset.id)} alt={asset.name || ""} className={zoom ? "zoomed" : ""} onClick={() => setZoom(!zoom)} />}
        {asset.kind === "video" && <video src={fileUrl(asset.id)} controls autoPlay loop />}
        {asset.kind === "audio" && <audio src={fileUrl(asset.id)} controls autoPlay style={{ width: "min(640px, 90%)" }} />}
        {(asset.kind === "lyrics" || asset.kind === "font") && <div className="card">{asset.name}</div>}
      </div>
      <aside className="lightbox-side">
        <div>
          <div className="row" style={{ marginBottom: 6 }}>
            <span className="pill">{asset.kind}</span>
            <span className="pill">{asset.source}</span>
            {list.length > 1 && <span className="muted small">{idx + 1} / {list.length}</span>}
          </div>
          <h2>{asset.name || asset.id}</h2>
          <div className="mono muted small">{asset.id}</div>
        </div>
        <div className="row">
          <Stars value={asset.rating} onChange={(v) => patch({ rating: v })} />
          <button className={`btn sm${asset.favourite ? " primary" : ""}`} onClick={() => patch({ favourite: !asset.favourite })}>
            <Heart size={14} fill={asset.favourite ? "currentColor" : "none"} /> {t("favourite")}
          </button>
        </div>
        <dl className="kv">
          {asset.width && <><dt>{t("size")}</dt><dd>{asset.width} x {asset.height}</dd></>}
          {asset.duration_s && <><dt>{t("duration")}</dt><dd>{asset.duration_s.toFixed(2)} s</dd></>}
          <dt>mime</dt><dd>{asset.mime}</dd>
          <dt>{t("when")}</dt><dd>{new Date(asset.created_at).toLocaleString()}</dd>
        </dl>

        <div>
          <div className="panel-title">{t("lineage")}</div>
          {recipe ? (
            <div className="recipe-box">
              {(params.positive_prompt || recipe.text) && <p className="prompt">{String(params.positive_prompt || recipe.text)}</p>}
              {recipe.operation && <div className="row small" style={{ marginBottom: 8 }}><span className="pill accent">{recipe.operation}</span>
                {recipe.rerun && <span className="pill gold">{recipe.rerun}</span>}</div>}
              <dl className="kv">
                {paramRows.filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => (
                  <Fragment key={k}><dt>{k}</dt><dd>{String(v)}</dd></Fragment>
                ))}
                {recipe.fields && Object.entries(recipe.fields).filter(([, v]) => typeof v === "string" && !String(v).startsWith("a_")).slice(0, 6).map(([k, v]) => (
                  <Fragment key={`f${k}`}><dt>{k}</dt><dd>{String(v)}</dd></Fragment>
                ))}
              </dl>
              {(recipe.input_asset_ids || []).length > 0 && (
                <div style={{ marginTop: 10 }}>
                  <span className="muted small">{t("inputs")}</span>
                  <div className="input-thumbs">
                    {recipe.input_asset_ids!.slice(0, 16).map((id) => (
                      <button key={id} onClick={() => onNavigate(id)} title={id}>
                        <img src={`/api/assets/${id}/thumb`} alt="" onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : <p className="muted small">{t("noRecipe")}</p>}
        </div>

        {asset.kind === "image" && (
          <div className="stack">
            <div className="action-grid">
              <button className="btn sm" disabled={!fromComfy || busy} onClick={() => run(t("reuse"), () => api.edit(asset.id, { operation: "reuse" }))}><Repeat size={14} /> {t("reuse")}</button>
              <button className="btn sm" disabled={!fromComfy || busy} onClick={() => run(t("varySeed"), () => api.edit(asset.id, { operation: "vary", count: 2 }))}><Dices size={14} /> {t("varySeed")}</button>
              <button className="btn sm" disabled={busy || recipe?.template !== "sdxl_txt2img"} onClick={() => run(t("hires"), () => api.edit(asset.id, { operation: "hires" }))}><Sparkles size={14} /> {t("hires")}</button>
              <button className="btn sm" disabled={busy} onClick={() => run(t("animate"), () => api.animate(asset.id, { frames: 14, fps: 7, motion: 127 }))}><Film size={14} /> {t("animate")}</button>
            </div>
            <label className="field">{t("editPrompt")}
              <textarea value={editPrompt} onChange={(e) => setEditPrompt(e.target.value)} rows={3} />
            </label>
            <div className="row">
              <label className="field grow">{t("strength")} <span className="mono">{strength.toFixed(2)}</span>
                <input type="range" min={0.1} max={1} step={0.05} value={strength} onChange={(e) => setStrength(Number(e.target.value))} />
              </label>
              <button className="btn sm primary" disabled={busy} onClick={() => run(t("img2img"), () => api.edit(asset.id, { operation: "img2img", prompt: editPrompt || null, strength }))}>
                <Wand2 size={14} /> {t("img2img")}
              </button>
            </div>
          </div>
        )}

        <label className="field">{t("tags")} <span className="hint">{t("tagsHint")}</span>
          <input value={tags} onChange={(e) => setTags(e.target.value)}
            onBlur={() => patch({ tags: tags.split(",").map((x) => x.trim()).filter(Boolean) })} />
        </label>
        <label className="field">{t("notes")}
          <textarea value={notes} onChange={(e) => setNotes(e.target.value)} onBlur={() => notes !== (asset.notes || "") && patch({ notes })} rows={2} />
        </label>
        {(boards.data?.items || []).length > 0 && (asset.kind === "image" || asset.kind === "video") && (
          <label className="field">{t("addToBoard")}
            <select value="" onChange={async (e) => {
              const b = boards.data!.items.find((x) => x.id === e.target.value);
              if (!b) return;
              if (b.items.some((i) => i.asset_id === asset.id)) return;
              await api.setBoardItems(b.id, [...b.items, { asset_id: asset.id, note: "" }]);
              app.toast(`${t("applied")}: ${b.name}`, "ok");
              boards.reload();
            }}>
              <option value="">-</option>
              {boards.data!.items.map((b) => <option key={b.id} value={b.id}>{b.name} ({b.items.length})</option>)}
            </select>
          </label>
        )}
      </aside>
    </div>
  );
}
