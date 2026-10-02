import { Fragment, useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Dices, Download, Eraser, Film, Heart, Maximize2, Minimize2, Repeat, Sparkles, Trash2, Wand2, X, ZoomIn, Ratio, AudioLines, Scissors, RefreshCw } from "lucide-react";
import { api, fileUrl, type Asset, type Board } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, Stars, useApp, useAsync } from "./ui";

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
      if (e.key === "Delete" && asset) { if (delArmed) { setDelArmed(false); remove(); } else setDelArmed(true); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Delete (key: Delete, twice): to the trash, then the next one in the list
  const [delArmed, setDelArmed] = useState(false);
  useEffect(() => { setDelArmed(false); }, [assetId]);
  const remove = async (force = false) => {
    if (!asset) return;
    try {
      const r = await api.deleteAssets([asset.id], force);
      if (r.failed.length) {
        const f = r.failed[0];
        if (f.error === "asset_in_use" && !force && window.confirm(`${f.message}\n\n${t("deleteForce")}?`)) return remove(true);
        if (f.error !== "asset_in_use") app.toast(f.message, "bad");
        return;
      }
      app.toast(t("deleted", { n: 1 }), "ok");
      app.bump();
      const rest = list.filter((x) => x !== asset.id);
      if (!rest.length) onClose();
      else onNavigate(rest[Math.min(Math.max(0, idx), rest.length - 1)]);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const [reframeHow, setReframeHow] = useState("fill");
  const [retakeFrom, setRetakeFrom] = useState(1);
  const [retakeTo, setRetakeTo] = useState(2);
  const [retakePrompt, setRetakePrompt] = useState("");
  const [retakeQuality, setRetakeQuality] = useState<"draft" | "final">("draft");
  const [stemList, setStemList] = useState<Record<string, string>>({});
  useEffect(() => {
    setStemList({});
    if (asset && (asset.kind === "audio" || asset.kind === "video")) api.assetStems(asset.id).then((r) => setStemList(r.stems)).catch(() => undefined);
  }, [asset?.id]); // eslint-disable-line react-hooks/exhaustive-deps
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

  if (!asset) return <div className="overlay" onClick={onClose} />;
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
    <div className="overlay" role="dialog" aria-label={asset.name || asset.id}>
      <div className="lightbox-stage" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
        <div className="lightbox-tools">
          <button className="btn sm" onClick={onClose}><X size={15} /> {t("close")}</button>
          {asset.kind === "image" && (
            <button className="btn sm" onClick={() => setZoom(!zoom)}>{zoom ? <Minimize2 size={15} /> : <Maximize2 size={15} />} {zoom ? t("fit") : t("zoom")}</button>
          )}
          <a className="btn sm" href={`${fileUrl(asset.id)}?download=true`}><Download size={15} /> {t("download")}</a>
          <ConfirmButton className="btn sm danger" onConfirm={() => remove()}><Trash2 size={15} /> {t("deleteAsset")}</ConfirmButton>
          {delArmed && <span className="pill bad">{t("confirm")} (Supr / Del)</span>}
        </div>
        {list.length > 1 && <>
          <button className="btn icon lightbox-nav prev" onClick={() => move(-1)} aria-label="previous"><ChevronLeft size={18} /></button>
          <button className="btn icon lightbox-nav next" onClick={() => move(1)} aria-label="next"><ChevronRight size={18} /></button>
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
        {asset.kind === "image" && <CastReference asset={asset} />}
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
              <button className="btn sm" disabled={busy} onClick={() => run(t("animate"), () => api.animate(asset.id, { engine: "auto", prompt: editPrompt ? `${editPrompt}, gentle natural motion` : null }))}><Film size={14} /> {t("animate")}</button>
              <button className="btn sm" disabled={busy} onClick={() => run(t("upscaleX2"), () => api.edit(asset.id, { operation: "upscale", scale: 2 }))}><ZoomIn size={14} /> {t("upscaleX2")}</button>
              <button className="btn sm" disabled={busy} onClick={() => run(t("upscaleX4"), () => api.edit(asset.id, { operation: "upscale", scale: 4 }))}><ZoomIn size={14} /> {t("upscaleX4")}</button>
              <button className="btn sm" disabled={busy} onClick={() => run(t("removeBackground"), () => api.edit(asset.id, { operation: "remove_background" }))}><Eraser size={14} /> {t("removeBackground")}</button>
            </div>
            <label className="field">{t("editPrompt")}
              <textarea value={editPrompt} onChange={(e) => setEditPrompt(e.target.value)} rows={3} />
            </label>
            <div className="row">
              <label className="field grow">{t("strength")} <span className="mono">{strength.toFixed(2)}</span>
                <input type="range" min={0.1} max={1} step={0.05} value={strength} onChange={(e) => setStrength(Number(e.target.value))} />
              </label>
              <button className="btn sm" disabled={busy} onClick={() => run(t("img2img"), () => api.edit(asset.id, { operation: "img2img", prompt: editPrompt || null, strength }))}>
                <Wand2 size={14} /> {t("img2img")}
              </button>
            </div>
            <button className="btn sm primary" disabled={busy || !editPrompt.trim()} title={t("editInstructionHint")}
              onClick={() => run(t("editInstruction"), () => api.generate(asset.project_id, {
                prompt: editPrompt, engine: "auto", reference_asset_id: asset.id, reference_asset_ids: [asset.id], count: 1 }))}>
              <Sparkles size={14} /> {t("editInstruction")}
            </button>
          </div>
        )}

        {(asset.kind === "audio" || asset.kind === "video") && asset.recipe?.operation !== "stems" && (
          <div className="stack" style={{ gap: 6 }}>
            <div className="row wrap" style={{ gap: 6 }}>
              <span className="small muted grow"><AudioLines size={13} /> {t("stemsTitle")}</span>
              <button className="btn sm" disabled={busy} title={t("stemsHint")} onClick={async () => {
                setBusy(true);
                try {
                  const r = await api.makeStems(asset.id, ["vocals", "drums", "bass", "other"].every((k) => stemList[k]));
                  if (r.job) { app.toast(t("stemsQueued"), "ok"); app.refreshJobs(); }
                  setStemList(r.stems);
                } catch (e) { app.toast((e as Error).message, "bad"); }
                finally { setBusy(false); }
              }}><Scissors size={13} /> {["vocals", "drums", "bass", "other"].every((k) => stemList[k]) ? t("stemsAgain") : t("stemsMake")}</button>
            </div>
            {Object.keys(stemList).length > 0 && (
              <div className="stack" style={{ gap: 4 }}>
                {["vocals", "drums", "bass", "other", "instrumental"].filter((k) => stemList[k]).map((k) => (
                  <div key={k} className="row" style={{ gap: 6 }}>
                    <span className="small mono" style={{ width: 60 }}>{t(`stem_${k}` as never)}</span>
                    <audio controls preload="none" src={`/api/assets/${stemList[k]}/file`} style={{ flex: 1, height: 30 }} />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        {asset.kind === "video" && (
          <div className="stack" style={{ gap: 6 }}>
            <span className="small muted" title={t("retakeHint")}><Scissors size={13} /> {t("retakeTitle")}</span>
            <div className="row wrap" style={{ gap: 6 }}>
              <label className="row small" style={{ gap: 4 }}>{t("trackFrom")}<input type="number" min={0} step={0.1} value={retakeFrom}
                style={{ width: 70 }} onChange={(e) => setRetakeFrom(Number(e.target.value))} />s</label>
              <label className="row small" style={{ gap: 4 }}>{t("retakeTo")}<input type="number" min={0} step={0.1} value={retakeTo}
                style={{ width: 70 }} onChange={(e) => setRetakeTo(Number(e.target.value))} />s</label>
              <select value={retakeQuality} onChange={(e) => setRetakeQuality(e.target.value as "draft" | "final")} style={{ width: "auto" }}>
                <option value="draft">{t("sbDraft")}</option><option value="final">{t("sbFinal")}</option>
              </select>
            </div>
            <input value={retakePrompt} placeholder={t("retakePh")} onChange={(e) => setRetakePrompt(e.target.value)} />
            <button className="btn sm" disabled={busy || retakeTo <= retakeFrom} onClick={() => run(t("retakeTitle"),
              () => api.retake(asset.id, { start_s: retakeFrom, end_s: retakeTo, prompt: retakePrompt || undefined, quality: retakeQuality }))}>
              <RefreshCw size={13} /> {t("retakeGo")}</button>
          </div>
        )}
        {(asset.kind === "image" || asset.kind === "video") && (
          <div className="row wrap" style={{ gap: 6 }} title={t("assetReframeHint")}>
            <span className="small muted"><Ratio size={13} /> {t("assetReframe")}</span>
            <select value={reframeHow} onChange={(e) => setReframeHow(e.target.value)} style={{ width: "auto" }}>
              <option value="fill">{t("lookFramingFill")}</option>
              <option value="blur">{t("lookFramingBlur")}</option>
              <option value="fit">{t("lookFramingFit")}</option>
            </select>
            {["9:16", "16:9", "1:1", "4:5"].map((a) => (
              <button key={a} className="btn sm" disabled={busy} onClick={() => run(t("assetReframeQueued", { aspect: a }),
                () => api.reframeAsset(asset.id, a, reframeHow))}>{a}</button>
            ))}
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

/** Make this picture a character's reference image (its canonical look), in one click from the viewer. */
function CastReference({ asset }: { asset: Asset }) {
  const { t } = useT();
  const app = useApp();
  const chars = useAsync(() => api.characters(asset.project_id), [asset.project_id, app.dataVersion]);
  const [pick, setPick] = useState("");
  const items = chars.data?.items || [];
  const using = items.filter((c) => c.canonical_asset_id === asset.id);
  if (!items.length) return null;
  return (
    <div className="row wrap" style={{ gap: 6 }}>
      {using.length > 0 ? <span className="pill ok">{t("refOf", { names: using.map((c) => c.name).join(", ") })}</span> : (
        <>
          <select value={pick} onChange={(e) => setPick(e.target.value)} style={{ maxWidth: 200 }}>
            <option value="">{t("refPick")}</option>
            {items.map((c) => <option key={c.id} value={c.id}>{c.name}{c.canonical_asset_id ? "" : ` · ${t("refNone")}`}</option>)}
          </select>
          <button className="btn sm" disabled={!pick} onClick={async () => {
            try {
              await api.updateCharacter(pick, { canonical_asset_id: asset.id });
              app.toast(t("refSet", { name: items.find((c) => c.id === pick)?.name || "" }), "ok");
              app.bump();
            } catch (e) { app.toast((e as Error).message, "bad"); }
          }}>{t("refUse")}</button>
        </>
      )}
    </div>
  );
}
