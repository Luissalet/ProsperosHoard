import { useEffect, useState } from "react";
import {
  Check, Download, Loader2, Plus, RefreshCcw, Sparkles, Star, Trash2, Upload, X,
} from "lucide-react";
import { api, thumbUrl, type Adapter, type Character, type Job, type Take, type TrainerEntry } from "../api";
import { useT, type MessageKey } from "../i18n";
import { Modal, Progress, useApp, useAsync, useDebounced } from "../components/ui";

type Tab = "overview" | "sheet" | "dataset" | "training" | "takes";

/** Poll a job until it settles, calling `cb` on every tick (including the final one). Fire-and-forget. */
function pollJob(id: string, cb: (job: Job) => void) {
  const tick = () => {
    api.job(id).then((j) => {
      cb(j);
      if (!["done", "failed", "cancelled"].includes(j.state)) setTimeout(tick, 1800);
    }).catch(() => undefined);
  };
  tick();
}

export function CharacterKitModal({ character, onClose }: { character: Character; onClose: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [tab, setTab] = useState<Tab>("overview");
  const kitQ = useAsync(() => api.charKit(character.id), [character.id]);
  const reloadKit = () => kitQ.reload();
  const notify = () => { app.bump(); reloadKit(); };

  const tabs: { id: Tab; label: string }[] = [
    { id: "overview", label: t("kitTabOverview") },
    { id: "sheet", label: t("kitTabSheet") },
    { id: "dataset", label: t("kitTabDataset") },
    { id: "training", label: t("kitTabTraining") },
    { id: "takes", label: t("kitTabTakes") },
  ];

  return (
    <Modal title={t("kitTitle", { name: character.name })} onClose={onClose} wide>
      <div className="stack">
        <div className="segmented">
          {tabs.map((x) => <button key={x.id} className={tab === x.id ? "on" : ""} onClick={() => setTab(x.id)}>{x.label}</button>)}
        </div>
        {!kitQ.data ? <p className="muted">{t("loading")}</p> : (
          <>
            {tab === "overview" && <OverviewTab character={character} kit={kitQ.data} onChanged={notify} />}
            {tab === "sheet" && <SheetTab character={character} kit={kitQ.data} onChanged={notify} />}
            {tab === "dataset" && <DatasetTab character={character} onChanged={notify} />}
            {tab === "training" && <TrainingTab character={character} onChanged={notify} />}
            {tab === "takes" && <TakesTab character={character} threshold={kitQ.data.identity_threshold} onChanged={notify} />}
          </>
        )}
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------- overview

function OverviewTab({ character, kit, onChanged }: {
  character: Character; kit: Awaited<ReturnType<typeof api.charKit>>; onChanged: () => void;
}) {
  const { t } = useT();
  const app = useApp();
  const [trigger, setTrigger] = useState(kit.trigger);
  const [threshold, setThreshold] = useState(kit.identity_threshold);
  const dThreshold = useDebounced(threshold, 500);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [exp, setExp] = useState({ dataset: true, adapters: true });
  const [available, setAvailable] = useState<string[] | null>(null);
  const [attachLora, setAttachLora] = useState("");
  const [attachArch, setAttachArch] = useState("qwen_image");

  useEffect(() => { setTrigger(kit.trigger); setThreshold(kit.identity_threshold); }, [kit.trigger, kit.identity_threshold]);
  useEffect(() => {
    if (dThreshold === kit.identity_threshold) return;
    api.charSettings(character.id, { identity_threshold: dThreshold }).then(onChanged).catch((e) => app.toast((e as Error).message, "bad"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dThreshold]);

  const saveTrigger = async () => {
    if (trigger === kit.trigger) return;
    try { await api.charSettings(character.id, { trigger }); onChanged(); } catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const toggleAdapters = async () => {
    try { await api.charSettings(character.id, { use_adapters: !kit.use_adapters }); onChanged(); } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const updateAdapter = async (a: Adapter, patch: { strength?: number; enabled?: boolean }) => {
    try { await api.adapterUpdate(character.id, a.id, patch); onChanged(); } catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const removeAdapter = async (a: Adapter) => {
    try { await api.adapterRemove(character.id, a.id); onChanged(); } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const loadAvailable = async () => {
    try { const r = await api.adaptersAvailable(character.id); setAvailable(r.loras); if (r.loras[0]) setAttachLora(r.loras[0]); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const attach = async () => {
    if (!attachLora) return;
    try { await api.adapterAttach(character.id, attachLora, attachArch); onChanged(); app.toast(t("kitAttach"), "ok"); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const saveToLibrary = async () => {
    setSaving(true);
    try {
      const r = await api.librarySave(character.project_id, character.id, note || undefined);
      app.toast(t("kitPackSaved", { v: r.version }), "ok");
      setNote("");
      onChanged();
    } catch (e) { app.toast((e as Error).message, "bad"); } finally { setSaving(false); }
  };

  const doExport = async () => {
    setSaving(true);
    try {
      const r = await api.charPackExport(character.id, exp.dataset, exp.adapters);
      const a = document.createElement("a");
      a.href = api.charPackDownloadUrl(r.file);
      a.download = r.file;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e) { app.toast((e as Error).message, "bad"); } finally { setSaving(false); }
  };

  const refs = character.reference_asset_ids.filter((id) => id !== character.canonical_asset_id);

  return (
    <div className="stack">
      <div className="field">
        {t("kitReferencesStrip")}
        {!character.canonical_asset_id ? <p className="small muted">{t("kitNoCanonical")}</p> : (
          <div className="row wrap">
            <button className="tile" style={{ width: 84 }} onClick={() => app.openAsset(character.canonical_asset_id!)}>
              <img src={thumbUrl({ id: character.canonical_asset_id, thumb_path: "x", kind: "image" })} alt="" />
              <div className="tile-badges"><span className="pill badge-dark">{t("canonical")}</span></div>
            </button>
            {refs.map((id) => (
              <button key={id} className="tile" style={{ width: 84 }} onClick={() => app.openAsset(id, [character.canonical_asset_id!, ...refs])}>
                <img src={thumbUrl({ id, thumb_path: "x", kind: "image" })} alt="" />
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="grid-2">
        <label className="field">{t("kitTrigger")} <span className="hint">{t("kitTriggerHint")}</span>
          <input className="mono" value={trigger} onChange={(e) => setTrigger(e.target.value)} onBlur={saveTrigger} /></label>
        <label className="field">{t("kitIdentityThreshold")} <span className="mono">{threshold.toFixed(1)}</span>
          <input type="range" min={0} max={10} step={0.5} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} /></label>
      </div>
      <label className="row small" style={{ gap: 6 }}>
        <input type="checkbox" checked={kit.use_adapters} onChange={toggleAdapters} /> {t("kitUseAdapters")}
      </label>

      <div className="card" style={{ padding: 14 }}>
        <h2>{t("kitAdaptersTitle")}</h2>
        {kit.adapters.length === 0 ? <p className="small muted">{t("kitNoAdapters")}</p> : (
          <div className="stack" style={{ gap: 10 }}>
            {kit.adapters.map((a) => (
              <div key={a.id} className="row wrap" style={{ background: "var(--surface-2)", borderRadius: 9, padding: "8px 10px", gap: 10 }}>
                <span className="pill">{a.arch}</span>
                <span className="mono small ellipsis" style={{ maxWidth: 220 }} title={a.lora_name}>{a.lora_name}</span>
                <span className="small muted">
                  {a.source === "trained" ? t("kitSourceTrained") : a.source === "pack" ? t("kitSourcePack") : t("kitSourceImported")}
                  {a.trained && ` · ${t("kitTrainedInfo", { steps: a.trained.steps, rank: a.trained.rank, images: a.trained.images })}`}
                  {!a.installed && ` · ${t("kitNotInstalled")}`}
                </span>
                <span className="row small grow" style={{ minWidth: 140, gap: 6, justifyContent: "flex-end" }}>
                  <input type="range" min={0} max={2} step={0.05} defaultValue={a.strength}
                    onChange={(e) => updateAdapter(a, { strength: Number(e.target.value) })} style={{ width: 100 }} />
                  <span className="mono">{a.strength.toFixed(2)}</span>
                </span>
                <label className="row small" style={{ gap: 4 }}>
                  <input type="checkbox" checked={a.enabled} onChange={() => updateAdapter(a, { enabled: !a.enabled })} /> {t("kitEnabled")}
                </label>
                <button className="btn sm ghost icon" onClick={() => removeAdapter(a)}><Trash2 size={14} /></button>
              </div>
            ))}
          </div>
        )}
        <div className="row wrap" style={{ marginTop: 10, gap: 8 }}>
          {available === null ? <button className="btn sm" onClick={loadAvailable}><Plus size={14} /> {t("kitAttachAdapter")}</button> : (
            <>
              <select value={attachLora} onChange={(e) => setAttachLora(e.target.value)} style={{ minWidth: 220 }}>
                {available.length === 0 && <option value="">{t("kitLoraNamePick")}</option>}
                {available.map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
              <select value={attachArch} onChange={(e) => setAttachArch(e.target.value)}>
                {["qwen_image", "flux1", "sdxl", "sd15", "wan22_5b", "z_image"].map((a) => <option key={a} value={a}>{a}</option>)}
              </select>
              <button className="btn sm primary" disabled={!attachLora} onClick={attach}>{t("kitAttach")}</button>
            </>
          )}
        </div>
      </div>

      <div className="grid-2">
        <div className="card" style={{ padding: 14 }}>
          <h2>{t("kitLibraryStatus")}</h2>
          {kit.library ? <p className="small">{t("kitLibraryVersion", { v: kit.library.version })}</p> : <p className="small muted">{t("kitNotInLibrary")}</p>}
          <label className="field">{t("kitLibraryNote")}<input value={note} onChange={(e) => setNote(e.target.value)} /></label>
          <button className="btn sm primary" disabled={saving} onClick={saveToLibrary} style={{ marginTop: 8 }}>
            {saving ? <Loader2 size={14} className="spin" /> : <Upload size={14} />} {t("kitSaveToLibrary")}
          </button>
        </div>
        <div className="card" style={{ padding: 14 }}>
          <h2>{t("kitExport")}</h2>
          <div className="stack" style={{ gap: 4 }}>
            <label className="row small" style={{ gap: 6 }}>
              <input type="checkbox" checked={exp.dataset} onChange={(e) => setExp({ ...exp, dataset: e.target.checked })} /> {t("kitIncludeDataset")}
            </label>
            <label className="row small" style={{ gap: 6 }}>
              <input type="checkbox" checked={exp.adapters} onChange={(e) => setExp({ ...exp, adapters: e.target.checked })} /> {t("kitIncludeAdapters")}
            </label>
          </div>
          <button className="btn sm primary" disabled={saving} onClick={doExport} style={{ marginTop: 8 }}>
            {saving ? <Loader2 size={14} className="spin" /> : <Download size={14} />} {t("kitExportNow")}
          </button>
        </div>
      </div>

      <div className="field">
        {t("kitHistory")}
        {kit.history.length === 0 ? <p className="small muted">{t("kitNoHistory")}</p> : (
          <ul className="small" style={{ margin: 0, paddingLeft: 18, maxHeight: 160, overflow: "auto" }}>
            {[...kit.history].reverse().map((h, i) => (
              <li key={i}><span className="muted mono">{(h.at || "").slice(0, 16).replace("T", " ")}</span> <strong>{h.event}</strong>{h.detail ? ` · ${h.detail}` : ""}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// -------------------------------------------------------------------- sheet

function SheetTab({ character, kit, onChanged }: {
  character: Character; kit: Awaited<ReturnType<typeof api.charKit>>; onChanged: () => void;
}) {
  const { t } = useT();
  const app = useApp();
  const [views, setViews] = useState<string[]>(kit.default_views);
  const [engine, setEngine] = useState("auto");
  const [job, setJob] = useState<Job | null>(null);
  const dataset = useAsync(() => api.datasetGet(character.id), [character.id, kit.sheet.contact_sheet_id]);
  const sheetItems = (dataset.data?.items || []).filter((d) => d.source === "sheet");

  const toggleView = (v: string) => setViews((vs) => vs.includes(v) ? vs.filter((x) => x !== v) : [...vs, v]);

  const render = async () => {
    if (!character.canonical_asset_id) { app.toast(t("kitNoCanonical"), "bad"); return; }
    try {
      const r = await api.charSheet(character.id, { views, engine: engine === "auto" ? undefined : engine });
      setJob(r.job);
      if (r.job.state !== "done") {
        pollJob(r.job.id, (j) => { setJob(j); if (j.state === "done") { onChanged(); dataset.reload(); } });
      } else { onChanged(); dataset.reload(); }
    } catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const busy = job && !["done", "failed", "cancelled"].includes(job.state);

  return (
    <div className="stack">
      <div className="field">
        {t("kitSheetViewsPick")}
        <div className="row wrap">
          {kit.views.map((v) => (
            <label key={v} className="chip" style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "4px 9px", width: "auto", borderRadius: 999, background: views.includes(v) ? "var(--accent-soft)" : "var(--surface-2)" }}>
              <input type="checkbox" checked={views.includes(v)} onChange={() => toggleView(v)} /> {viewLabel(t, v)}
            </label>
          ))}
        </div>
      </div>
      <div className="row wrap" style={{ gap: 10 }}>
        <label className="field" style={{ flex: "none" }}>{t("kitEngine")}
          <select value={engine} onChange={(e) => setEngine(e.target.value)}>
            <option value="auto">{t("kitEngineAuto")}</option>
            <option value="qwen21">Qwen-Image 2.1</option>
            <option value="flux">Flux Kontext</option>
          </select></label>
        <button className="btn primary" disabled={Boolean(busy) || views.length === 0} onClick={render} style={{ alignSelf: "flex-end" }}>
          {busy ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />} {t("kitRenderSheet")}
        </button>
      </div>
      {job && (
        <div className="stack" style={{ gap: 4 }}>
          <Progress value={job.progress} waiting={job.state === "waiting_gpu"} />
          <span className="small muted">{job.message || job.state}</span>
        </div>
      )}
      {kit.sheet.contact_sheet_id && (
        <div className="field">
          {t("kitContactSheet")}
          <button className="tile" style={{ width: 260 }} onClick={() => app.openAsset(kit.sheet.contact_sheet_id!)}>
            <img src={thumbUrl({ id: kit.sheet.contact_sheet_id, thumb_path: "x", kind: "image" })} alt="" />
          </button>
        </div>
      )}
      <div className="field">
        {t("kitTabSheet")}
        {sheetItems.length === 0 ? <p className="small muted">{t("kitSheetEmpty")}</p> : (
          <div className="thumb-grid">
            {sheetItems.map((d) => (
              <button key={d.asset_id} className="tile" onClick={() => app.openAsset(d.asset_id, sheetItems.map((x) => x.asset_id))}>
                <img src={thumbUrl({ id: d.asset_id, thumb_path: d.thumb || "x", kind: "image" })} alt="" loading="lazy" />
                <div className="tile-badges"><span className="pill badge-dark">{viewLabel(t, d.view)}</span></div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ----------------------------------------------------------------- dataset

const DATASET_SOURCES = ["canonical", "references", "sheet", "takes"] as const;

function DatasetTab({ character, onChanged }: { character: Character; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const ds = useAsync(() => api.datasetGet(character.id), [character.id]);
  const [sources, setSources] = useState<string[]>(["canonical", "references", "sheet"]);
  const [minIdentity, setMinIdentity] = useState(6);
  const [building, setBuilding] = useState(false);
  const [captioning, setCaptioning] = useState<Job | null>(null);
  const [onlyMissing, setOnlyMissing] = useState(true);

  const toggleSource = (s: string) => setSources((v) => v.includes(s) ? v.filter((x) => x !== s) : [...v, s]);

  const build = async () => {
    setBuilding(true);
    try { await api.datasetBuild(character.id, sources, sources.includes("takes") ? minIdentity : undefined); ds.reload(); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); } finally { setBuilding(false); }
  };

  const setInclude = async (assetId: string, include: boolean) => {
    try { await api.datasetUpdate(character.id, [{ asset_id: assetId, include }]); ds.reload(); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const setCaption = async (assetId: string, caption: string) => {
    try { await api.datasetUpdate(character.id, [{ asset_id: assetId, caption }]); ds.reload(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const removeItem = async (assetId: string) => {
    try { await api.datasetUpdate(character.id, [{ asset_id: assetId, remove: true }]); ds.reload(); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const autoCaption = async () => {
    try {
      const r = await api.datasetCaption(character.id, onlyMissing);
      setCaptioning(r.job);
      if (r.job.state !== "done") pollJob(r.job.id, (j) => { setCaptioning(j); if (j.state === "done") ds.reload(); });
      else ds.reload();
    } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const report = ds.data?.report;
  const captioningBusy = captioning && !["done", "failed", "cancelled"].includes(captioning.state);

  return (
    <div className="stack">
      <div className="card" style={{ padding: 14 }}>
        <h2>{t("kitDatasetBuildFrom")}</h2>
        <div className="row wrap" style={{ gap: 12 }}>
          {DATASET_SOURCES.map((s) => (
            <label key={s} className="row small" style={{ gap: 5 }}>
              <input type="checkbox" checked={sources.includes(s)} onChange={() => toggleSource(s)} />
              {t(s === "canonical" ? "kitSrcCanonical" : s === "references" ? "kitSrcReferences" : s === "sheet" ? "kitSrcSheet" : "kitSrcTakes")}
            </label>
          ))}
          {sources.includes("takes") && (
            <label className="row small" style={{ gap: 5 }}>{t("kitMinIdentity")}
              <input type="number" min={0} max={10} step={0.5} value={minIdentity} className="mono"
                onChange={(e) => setMinIdentity(Number(e.target.value))} style={{ width: 60 }} /></label>
          )}
          <button className="btn sm primary" disabled={building || sources.length === 0} onClick={build}>
            {building ? <Loader2 size={14} className="spin" /> : <RefreshCcw size={14} />} {t("kitBuildDataset")}
          </button>
        </div>
      </div>

      {report && (
        <div className="card" style={{ padding: 14 }}>
          <h2>{t("kitReadiness")} <span className={`pill ${report.ready ? "ok" : "warn"}`}>{report.ready ? t("kitReady") : t("kitNotReady")}</span></h2>
          {report.warnings.length > 0 && (
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {report.warnings.map((w, i) => <li key={i} className={report.ready ? "muted" : "err-text"}>{w}</li>)}
            </ul>
          )}
        </div>
      )}

      <div className="row wrap" style={{ gap: 10 }}>
        <label className="row small" style={{ gap: 5 }}>
          <input type="checkbox" checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} /> {t("kitAutoCaptionOnlyMissing")}
        </label>
        <button className="btn sm" disabled={Boolean(captioningBusy)} onClick={autoCaption}>
          {captioningBusy ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />} {t("kitAutoCaption")}
        </button>
        {captioning && <span className="small muted">{captioning.message || captioning.state}</span>}
      </div>
      <p className="small muted" style={{ margin: 0 }}>{t("kitCaptionNote")}</p>

      {(ds.data?.items || []).length === 0 ? <p className="small muted">{t("kitDatasetEmpty")}</p> : (
        <div className="grid-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
          {(ds.data?.items || []).map((d) => (
            <div key={d.asset_id} className="row" style={{ background: "var(--surface-2)", borderRadius: 9, padding: 8, alignItems: "flex-start", gap: 8 }}>
              <button className="tile" style={{ width: 64, flex: "none" }} onClick={() => app.openAsset(d.asset_id)}>
                <img src={thumbUrl({ id: d.asset_id, thumb_path: d.thumb || "x", kind: "image" })} alt="" loading="lazy" />
              </button>
              <div className="stack grow" style={{ gap: 4 }}>
                <div className="row small" style={{ gap: 6 }}>
                  <label className="row small" style={{ gap: 4 }}>
                    <input type="checkbox" checked={d.include} onChange={(e) => setInclude(d.asset_id, e.target.checked)} /> {t("kitInclude")}
                  </label>
                  <span className="muted grow ellipsis">{d.source}{d.identity != null && ` · ${d.identity.toFixed(1)}`}</span>
                  <button className="btn sm ghost icon" onClick={() => removeItem(d.asset_id)}><X size={13} /></button>
                </div>
                <textarea rows={2} defaultValue={d.caption} className="small" onBlur={(e) => e.target.value !== d.caption && setCaption(d.asset_id, e.target.value)} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- training

const ARCHS = ["qwen_image", "flux1", "sdxl", "sd15", "wan22_5b", "z_image"];

function TrainingTab({ character, onChanged }: { character: Character; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const info = useAsync(() => api.trainers(), [character.id]);
  const status = useAsync(() => api.trainStatus(character.id), [character.id]);
  const [arch, setArch] = useState("qwen_image");
  const [overrides, setOverrides] = useState<{ steps?: number; rank?: number; lr?: number; resolution?: number }>({});
  const [plan, setPlan] = useState<Awaited<ReturnType<typeof api.trainPlan>> | null>(null);
  const [busyPlan, setBusyPlan] = useState(false);
  const [configuring, setConfiguring] = useState(false);
  const [expandedLog, setExpandedLog] = useState<Record<string, string[]>>({});

  const refreshPlan = async (a = arch, ov = overrides) => {
    setBusyPlan(true);
    try { setPlan(await api.trainPlan(character.id, a, ov)); } catch (e) { app.toast((e as Error).message, "bad"); setPlan(null); }
    finally { setBusyPlan(false); }
  };
  useEffect(() => { refreshPlan(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [character.id, arch]);
  useEffect(() => {
    if (!plan) return;
    setOverrides((o) => ({ steps: o.steps ?? plan.plan.steps, rank: o.rank ?? plan.plan.rank, lr: o.lr ?? plan.plan.lr, resolution: o.resolution ?? plan.plan.resolution }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan?.arch]);

  const active = status.data?.runs.some((j) => ["queued", "running", "waiting_gpu"].includes(j.state));
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => {
      status.reload();
      // keep open logs of running trainings live
      for (const j of status.data?.runs || []) {
        if (j.run_id && expandedLog[j.id] && !["done", "failed", "cancelled"].includes(j.state)) {
          api.trainingLog(j.run_id).then((r) => setExpandedLog((s) => (s[j.id] ? { ...s, [j.id]: r.lines } : s))).catch(() => undefined);
        }
      }
    }, 3000);
    return () => clearInterval(id);
  }, [active, status.reload, status.data, expandedLog]);

  const start = async () => {
    try {
      await api.trainStart(character.id, arch, overrides);
      app.toast(t("kitStartTraining"), "ok");
      status.reload();
      onChanged();
    } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const toggleLog = async (job: Job & { run_id?: string | null }) => {
    const runId = job.run_id || (job.outputs as { run_id?: string } | null)?.run_id;
    if (!runId) return;
    if (expandedLog[job.id]) { setExpandedLog((s) => { const n = { ...s }; delete n[job.id]; return n; }); return; }
    try { const r = await api.trainingLog(runId); setExpandedLog((s) => ({ ...s, [job.id]: r.lines })); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };

  return (
    <div className="stack">
      <div className="card" style={{ padding: 14 }}>
        <h2>{t("kitTrainerStatusTitle")}
          <div className="card-actions"><button className="btn sm ghost" onClick={() => setConfiguring((v) => !v)}>{t("kitConfigureTrainer")}</button></div>
        </h2>
        {(info.data?.trainers.length ?? 0) === 0 ? <p className="small muted">{t("kitNoTrainerConfigured")}</p> : (
          <div className="stack" style={{ gap: 6 }}>
            {info.data!.trainers.map((tr) => (
              <div key={tr.name} className="row small">
                <span className={`pill ${tr.ok ? "ok" : "bad"}`}>{tr.name}</span>
                <span className="muted">{tr.kind} · {tr.archs.join(", ")}</span>
                {!tr.ok && <span className="err-text">{tr.reason}</span>}
              </div>
            ))}
          </div>
        )}
        {configuring && info.data && (
          <TrainerConfigForm info={info.data} onSaved={() => { setConfiguring(false); info.reload(); }} />
        )}
      </div>

      <div className="grid-2">
        <label className="field">{t("kitArchSelect")}
          <select value={arch} onChange={(e) => { setArch(e.target.value); setOverrides({}); }}>
            {ARCHS.map((a) => <option key={a} value={a}>{info.data?.archs[a]?.label || a}</option>)}
          </select></label>
        <div className="grid-3">
          <label className="field">{t("kitStepsField")}<input type="number" className="mono" value={overrides.steps ?? ""} onChange={(e) => setOverrides({ ...overrides, steps: Number(e.target.value) })} onBlur={() => refreshPlan(arch, overrides)} /></label>
          <label className="field">{t("kitRankField")}<input type="number" className="mono" value={overrides.rank ?? ""} onChange={(e) => setOverrides({ ...overrides, rank: Number(e.target.value) })} onBlur={() => refreshPlan(arch, overrides)} /></label>
          <label className="field">{t("kitResolutionField")}<input type="number" className="mono" value={overrides.resolution ?? ""} onChange={(e) => setOverrides({ ...overrides, resolution: Number(e.target.value) })} onBlur={() => refreshPlan(arch, overrides)} /></label>
        </div>
      </div>

      {busyPlan ? <p className="small muted">{t("loading")}</p> : plan && (
        <div className="card" style={{ padding: 14 }}>
          <h2>{t("kitPlanTitle")}</h2>
          <div className="row wrap small" style={{ gap: 14 }}>
            <span>{t("kitStepsField")}: <strong className="mono">{plan.plan.steps}</strong></span>
            <span>{t("kitRankField")}: <strong className="mono">{plan.plan.rank}</strong></span>
            <span>{t("kitEstVram")}: <strong className="mono">{(plan.plan.est_vram_mb / 1024).toFixed(1)} GB</strong></span>
            <span>{t("kitEstMinutes")}: <strong className="mono">{plan.plan.est_minutes} min</strong></span>
          </div>
          {plan.plan.warnings.map((w, i) => <p key={i} className="small muted" style={{ margin: "4px 0 0" }}>{w}</p>)}
          {plan.trainer_problem && <p className="small err-text">{plan.trainer_problem}</p>}
          {!plan.dataset.ready && plan.dataset.warnings.map((w, i) => <p key={`d${i}`} className="small err-text">{w}</p>)}
          <ConfirmStart disabled={!plan.ready} onStart={start} label={t("kitStartTraining")} confirm={t("kitStartConfirm")} />
        </div>
      )}

      <div className="field">
        {t("kitRunsTitle")}
        {(status.data?.runs.length ?? 0) === 0 ? <p className="small muted">{t("kitNoRuns")}</p> : (
          <div className="stack" style={{ gap: 8 }}>
            {status.data!.runs.map((j) => {
              const runId = j.run_id || (j.outputs as { run_id?: string } | null)?.run_id;
              return (
                <div key={j.id} className="card" style={{ padding: 10 }}>
                  <div className="row small">
                    <span className={`pill ${j.state === "done" ? "ok" : j.state === "failed" ? "bad" : "accent"}`}>{j.state}</span>
                    <span className="muted grow ellipsis">{j.message || ""}</span>
                    {runId && <button className="btn sm ghost" onClick={() => toggleLog(j)}>{expandedLog[j.id] ? t("kitHideLog") : t("kitShowLog")}</button>}
                  </div>
                  {!["done", "failed", "cancelled"].includes(j.state) && <Progress value={j.progress} waiting={j.state === "waiting_gpu"} />}
                  {expandedLog[j.id] && (
                    <pre className="mono small" style={{ maxHeight: 200, overflow: "auto", background: "var(--surface-2)", padding: 8, borderRadius: 8, marginTop: 6 }}>
                      {expandedLog[j.id].join("\n")}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function ConfirmStart({ disabled, onStart, label, confirm }: { disabled: boolean; onStart: () => void; label: string; confirm: string }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { if (!armed) return; const id = setTimeout(() => setArmed(false), 4000); return () => clearTimeout(id); }, [armed]);
  return (
    <div className="stack" style={{ gap: 6, marginTop: 8 }}>
      {armed && <p className="small muted" style={{ margin: 0 }}>{confirm}</p>}
      <button className={`btn primary${armed ? " armed" : ""}`} disabled={disabled} style={{ alignSelf: "flex-start" }}
        onClick={() => { if (armed) { setArmed(false); onStart(); } else setArmed(true); }}>
        <Sparkles size={14} /> {label}
      </button>
    </div>
  );
}

function TrainerConfigForm({ info, onSaved }: { info: Awaited<ReturnType<typeof api.trainers>>; onSaved: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [loraDir, setLoraDir] = useState(info.lora_dir || "");
  const [gpu, setGpu] = useState(String(info.gpu ?? "auto"));
  const [trainers, setTrainers] = useState<TrainerEntry[]>(info.trainers.map((tr) => ({ kind: tr.kind, name: tr.name, dir: tr.dir })));
  const [baseModels, setBaseModels] = useState<Record<string, string>>(info.base_models);
  const [saving, setSaving] = useState(false);

  const addRow = () => setTrainers([...trainers, { kind: "ai_toolkit", name: "", dir: "" }]);
  const setRow = (i: number, patch: Partial<TrainerEntry>) => setTrainers(trainers.map((r, j) => j === i ? { ...r, ...patch } : r));
  const removeRow = (i: number) => setTrainers(trainers.filter((_, j) => j !== i));

  const save = async () => {
    setSaving(true);
    try {
      await api.trainingSettings({ lora_dir: loraDir, gpu: gpu === "auto" ? "auto" : Number(gpu), trainers, base_models: baseModels });
      app.toast(t("save"), "ok");
      onSaved();
    } catch (e) { app.toast((e as Error).message, "bad"); } finally { setSaving(false); }
  };

  return (
    <div className="stack" style={{ marginTop: 12, gap: 10 }}>
      <div className="grid-2">
        <label className="field">{t("kitLoraDir")}<input value={loraDir} onChange={(e) => setLoraDir(e.target.value)} /></label>
        <label className="field">{t("kitGpu")}<input value={gpu} onChange={(e) => setGpu(e.target.value)} placeholder={t("kitGpuAuto")} /></label>
      </div>
      <div className="field">
        {t("kitBaseModels")}
        <div className="stack" style={{ gap: 6 }}>
          {ARCHS.map((a) => (
            <div key={a} className="row small"><span style={{ width: 90 }}>{a}</span>
              <input className="grow" value={baseModels[a] || ""} onChange={(e) => setBaseModels({ ...baseModels, [a]: e.target.value })}
                placeholder={info.archs[a]?.base_hint} /></div>
          ))}
        </div>
      </div>
      <div className="field">
        {t("kitTrainerName")}
        <div className="stack" style={{ gap: 6 }}>
          {trainers.map((tr, i) => (
            <div key={i} className="row small" style={{ gap: 6 }}>
              <select value={tr.kind} onChange={(e) => setRow(i, { kind: e.target.value as TrainerEntry["kind"] })}>
                {(["ai_toolkit", "musubi", "custom", "fake"] as const).map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
              <input placeholder={t("kitTrainerName")} value={tr.name} onChange={(e) => setRow(i, { name: e.target.value })} style={{ width: 110 }} />
              <input placeholder={t("kitTrainerDir")} className="grow" value={tr.dir} onChange={(e) => setRow(i, { dir: e.target.value })} />
              <input placeholder={t("kitTrainerPython")} value={tr.python || ""} onChange={(e) => setRow(i, { python: e.target.value })} style={{ width: 140 }} />
              <button className="btn sm ghost icon" onClick={() => removeRow(i)}><X size={13} /></button>
            </div>
          ))}
          <button className="btn sm ghost" onClick={addRow}><Plus size={14} /> {t("kitAddTrainerRow")}</button>
        </div>
      </div>
      <button className="btn sm primary" disabled={saving} onClick={save} style={{ alignSelf: "flex-start" }}>
        {saving ? <Loader2 size={14} className="spin" /> : <Check size={14} />} {t("kitSaveTrainingSettings")}
      </button>
    </div>
  );
}

// -------------------------------------------------------------------- takes

function TakesTab({ character, threshold, onChanged }: { character: Character; threshold: number; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [sort, setSort] = useState<"recent" | "identity">("recent");
  const takes = useAsync(() => api.takesList(character.id, sort), [character.id, sort]);
  const [scoring, setScoring] = useState<Job | null>(null);

  const score = async () => {
    try {
      const r = await api.takesScore(character.id);
      setScoring(r.job);
      if (r.job.state !== "done") pollJob(r.job.id, (j) => { setScoring(j); if (j.state === "done") { takes.reload(); onChanged(); } });
      else { takes.reload(); onChanged(); }
    } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const act = async (take: Take, action: Parameters<typeof api.takeAction>[2]) => {
    try { await api.takeAction(character.id, take.asset_id, action); takes.reload(); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const scoringBusy = scoring && !["done", "failed", "cancelled"].includes(scoring.state);

  return (
    <div className="stack">
      <div className="row wrap" style={{ gap: 10 }}>
        <div className="segmented">
          <button className={sort === "recent" ? "on" : ""} onClick={() => setSort("recent")}>{t("kitSortRecent")}</button>
          <button className={sort === "identity" ? "on" : ""} onClick={() => setSort("identity")}>{t("kitSortIdentity")}</button>
        </div>
        <button className="btn sm" disabled={Boolean(scoringBusy)} onClick={score}>
          {scoringBusy ? <Loader2 size={14} className="spin" /> : <Star size={14} />} {t("kitScoreIdentity")}
        </button>
        {scoring && <span className="small muted">{scoring.message || scoring.state}</span>}
      </div>
      {(takes.data?.items.length ?? 0) === 0 ? <p className="small muted">{t("kitNoTakes")}</p> : (
        <div className="thumb-grid">
          {takes.data!.items.map((tk) => {
            const idOk = tk.identity != null && tk.identity >= threshold;
            return (
              <div key={tk.asset_id} className="tile" style={{ cursor: "default" }}>
                <img src={`/api/assets/${tk.asset_id}/thumb`} alt="" loading="lazy" onClick={() => app.openAsset(tk.asset_id)} style={{ cursor: "zoom-in" }} />
                <div className="tile-badges">
                  <span className="pill badge-dark">{t("kitTakeOf", { n: tk.take, m: tk.takes_in_shot })}</span>
                  {tk.identity != null && (
                    <span className={`pill ${idOk ? "ok" : "warn"}`} title={tk.identity_method === "rough" ? t("kitRoughHint") : undefined}>
                      {tk.identity.toFixed(1)}{tk.identity_method === "rough" ? "~" : ""}</span>
                  )}
                </div>
                {/* status stays visible; actions appear on hover like the Library tiles */}
                <div className="row wrap" style={{ gap: 4, position: "absolute", top: 36, left: 8, right: 8 }}>
                  {tk.is_canonical && <span className="pill badge-dark">{t("kitBadgeCanonical")}</span>}
                  {tk.is_reference && <span className="pill badge-dark">{t("kitBadgeReference")}</span>}
                  {tk.in_dataset && <span className="pill badge-dark">{t("kitBadgeDataset")}</span>}
                  {tk.adapters.length > 0 && <span className="pill badge-dark">LoRA</span>}
                  {tk.rejected && <span className="pill bad">{t("kitBadgeRejected")}</span>}
                </div>
                <div className="tile-meta stack" style={{ gap: 4, alignItems: "flex-start" }}>
                  <div className="row wrap" style={{ gap: 4 }}>
                    <button className="btn sm ghost" onClick={() => act(tk, "canonical")}>{t("kitMakeCanonical")}</button>
                    <button className="btn sm ghost" onClick={() => act(tk, tk.is_reference ? "unreference" : "reference")}>{t("kitAddReference")}</button>
                    {!tk.in_dataset && <button className="btn sm ghost" onClick={() => act(tk, "dataset")}>{t("kitAddDataset")}</button>}
                    <button className="btn sm ghost" onClick={() => act(tk, tk.rejected ? "unreject" : "reject")}>{tk.rejected ? t("kitUnreject") : t("kitReject")}</button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}


const VIEW_KEYS: Record<string, MessageKey> = {
  front: "kitViewFront", three_quarter: "kitViewThreeQuarter", profile: "kitViewProfile", back: "kitViewBack",
  closeup: "kitViewCloseup", happy: "kitViewHappy", angry: "kitViewAngry", scared: "kitViewScared",
  surprised: "kitViewSurprised", action: "kitViewAction", sitting: "kitViewSitting", night: "kitViewNight",
};

function viewLabel(t: (k: MessageKey) => string, view: string | null | undefined): string {
  if (!view) return "";
  const key = VIEW_KEYS[view];
  return key ? t(key) : view;
}
