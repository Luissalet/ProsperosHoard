import { useEffect, useMemo, useRef, useState } from "react";
import { Columns2, Dices, ImagePlus, Loader2, Lock, LockOpen, Upload, Wand2, X } from "lucide-react";
import { api, fileUrl, thumbUrl, type Asset, type Character, type Composed, type Job, type WorkflowSpec } from "../api";
import { useT } from "../i18n";
import { AssetPicker, AssetTile, Empty, JobState, Progress, useApp, useAsync, useDebounced } from "../components/ui";
import { EngineBar } from "./EngineBar";
import { useDeleteAssets } from "../components/useDeleteAssets";
import { Trash2 } from "lucide-react";

const ASPECTS: Record<string, [number, number]> = {
  "1:1": [1024, 1024], "4:5": [896, 1120], "2:3": [832, 1216], "9:16": [768, 1344], "3:2": [1216, 832], "16:9": [1344, 768],
};
// What each engine's own template uses when a field is left empty (shown as
// placeholders; nothing is sent unless you type a value).
const ENGINE_DEFAULTS: Record<string, { steps: number; cfg: number; sampler: string; scheduler: string }> = {
  qwen21: { steps: 25, cfg: 1, sampler: "euler", scheduler: "simple" },
  flux: { steps: 4, cfg: 1, sampler: "euler", scheduler: "simple" },
  sdxl: { steps: 30, cfg: 6.5, sampler: "dpmpp_2m", scheduler: "karras" },
  sd15: { steps: 30, cfg: 6.5, sampler: "dpmpp_2m", scheduler: "karras" },
};
const ENGINE_LABELS: Record<string, string> = { qwen21: "Qwen-Image 2.1", flux: "FLUX.1", sdxl: "SDXL", sd15: "SD 1.5" };
const MAX_REFS: Record<string, number> = { qwen21: 10, flux: 1, sdxl: 1, sd15: 0, custom: 1 };
const SAMPLERS = ["euler", "dpmpp_2m", "dpmpp_2m_sde", "euler_ancestral", "dpmpp_sde", "heun", "uni_pc", "ddim"];
const SCHEDULERS = ["simple", "karras", "normal", "exponential", "sgm_uniform"];

function highlight(text: string, names: string[], fragments: string[]) {
  // mark the inlined character fragments inside the final prompt
  const parts: (string | { m: string })[] = [text];
  for (const f of [...fragments, ...names].filter(Boolean)) {
    for (let i = 0; i < parts.length; i++) {
      const p = parts[i];
      if (typeof p !== "string") continue;
      const at = p.indexOf(f);
      if (at >= 0) {
        parts.splice(i, 1, p.slice(0, at), { m: f }, p.slice(at + f.length));
        i++;
      }
    }
  }
  return parts.map((p, i) => (typeof p === "string" ? <span key={i}>{p}</span> : <mark key={i}>{p.m}</mark>));
}

export function GenerateView() {
  const { t } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const chars = useAsync(() => api.characters(pid), [pid]);
  const styles = useAsync(() => api.styles(pid), [pid]);
  const backend = useAsync(() => api.backend(), []);
  const workflows = useAsync(() => api.workflows(), []);
  const recent = useAsync(() => api.assets(pid, { source: "generated", kind: "image", limit: 24 }), [pid, app.dataVersion]);
  const engines = useAsync(() => api.imageEngines(pid), [pid]);

  const [prompt, setPrompt] = useState(() => sessionStorage.getItem(`prospero.prompt.${pid}`) || "");
  const [negative, setNegative] = useState("");
  const [style, setStyle] = useState<string>("");
  // "auto" | "qwen21" | "flux" | "sdxl" | "tmpl:<template>" (SD 1.5, imported workflows)
  const [engineSel, setEngineSel] = useState<string | null>(null);
  const [checkpoint, setCheckpoint] = useState("");
  const [model, setModel] = useState("");
  // "ref" = an edit keeps the canvas of <image1> (nothing sent, the template
  // sizes it from the first reference); any ratio forces that exact size
  const [aspect, setAspect] = useState("1:1");
  // empty = the engine template's own value
  const [steps, setSteps] = useState("");
  const [cfg, setCfg] = useState("");
  const [sampler, setSampler] = useState("");
  const [scheduler, setScheduler] = useState("");
  const [seed, setSeed] = useState<number>(() => Math.floor(Math.random() * 2 ** 31));
  const [seedLocked, setSeedLocked] = useState(false);
  const [count, setCount] = useState(2);
  const [refs, setRefs] = useState<Asset[]>([]);
  const [useCharRef, setUseCharRef] = useState(false);
  const [strength, setStrength] = useState(0.6);
  const [picking, setPicking] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [composed, setComposed] = useState<Composed | null>(null);
  const [queuedIds, setQueuedIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState<{ query: string; start: number; index: number } | null>(null);
  const [compare, setCompare] = useState<string[] | null>(null);
  const [picked, setPicked] = useState<string[] | null>(null);
  const del = useDeleteAssets();
  const textRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { sessionStorage.setItem(`prospero.prompt.${pid}`, prompt); }, [prompt, pid]);

  // live final-prompt preview
  const dPrompt = useDebounced(prompt, 300);
  const dNegative = useDebounced(negative, 300);
  useEffect(() => {
    if (!dPrompt.trim()) { setComposed(null); return; }
    api.compose(pid, dPrompt, dNegative, style || null).then(setComposed).catch(() => setComposed(null));
  }, [dPrompt, dNegative, style, pid]);

  useEffect(() => { setEngineSel(null); }, [pid]);
  useEffect(() => {
    if (engineSel === null && engines.data) setEngineSel(engines.data.project_default || "auto");
  }, [engines.data, engineSel]);
  const sel = engineSel || "auto";
  const customTemplate = sel.startsWith("tmpl:") ? sel.slice(5) : null;
  // the engine this render really gets ("custom" = an imported workflow)
  const resolved = customTemplate ? (customTemplate === "sd15_txt2img" ? "sd15" : "custom")
    : sel === "auto" ? (engines.data?.auto_resolves_to || "qwen21") : sel;
  const sdLike = resolved === "sdxl" || resolved === "sd15" || resolved === "custom";
  const maxRefs = MAX_REFS[resolved] ?? 1;
  const defaults = ENGINE_DEFAULTS[resolved] || ENGINE_DEFAULTS.sdxl;
  useEffect(() => { if (refs.length > maxRefs) setRefs(refs.slice(0, maxRefs)); }, [maxRefs]);
  const refSized = !sdLike && refs.length > 0;
  // a multi-reference edit only uses the references its instruction names
  const unnamedRefs = refs.map((_, i) => `<image${i + 1}>`).filter((tag) => !prompt.includes(tag));
  const hadRefs = useRef(false);
  useEffect(() => {
    // first reference in: follow its size; last one out: back to a ratio
    if (refSized && !hadRefs.current) setAspect("ref");
    if (!refSized && aspect === "ref") setAspect("1:1");
    hadRefs.current = refSized;
  }, [refSized]);

  // a style preset is tuned for SDXL: its sampler settings only fill the panel there
  useEffect(() => {
    const preset = styles.data?.items.find((s) => s.id === style);
    if (!preset || !sdLike) return;
    const d = preset.defaults;
    if (d.steps) setSteps(String(d.steps));
    if (d.cfg) setCfg(String(d.cfg));
    if (d.sampler) setSampler(String(d.sampler));
    if (d.scheduler) setScheduler(String(d.scheduler));
  }, [style, styles.data, sdLike]);

  const characters = chars.data?.items || [];
  const suggestions = useMemo(() => {
    if (!menu) return [] as Character[];
    const q = menu.query.toLowerCase();
    return characters.filter((c) => c.name.toLowerCase().startsWith(q) || c.name.toLowerCase().replace(/\s+/g, "").startsWith(q)).slice(0, 6);
  }, [menu, characters]);

  const onPromptChange = (value: string, caret: number) => {
    setPrompt(value);
    const before = value.slice(0, caret);
    const m = before.match(/(^|[^\w])@([\w\- ]{0,24})$/u);
    if (m && !m[2].includes("  ")) setMenu({ query: m[2], start: caret - m[2].length - 1, index: 0 });
    else setMenu(null);
  };

  const insertMention = (c: Character) => {
    if (!menu) return;
    const caret = textRef.current?.selectionStart ?? prompt.length;
    const next = `${prompt.slice(0, menu.start)}@${c.name} ${prompt.slice(caret)}`;
    setPrompt(next);
    setMenu(null);
    setTimeout(() => {
      const pos = menu.start + c.name.length + 2;
      textRef.current?.focus();
      textRef.current?.setSelectionRange(pos, pos);
    }, 0);
  };

  const myJobs: Job[] = queuedIds.map((id) => app.jobs.find((j) => j.id === id)).filter(Boolean) as Job[];
  const comfy = backend.data?.comfy;
  const checkpoints = comfy?.checkpoints || [];
  const customs: WorkflowSpec[] = workflows.data?.custom || [];

  const queue = async () => {
    setBusy(true);
    const body: Record<string, unknown> = {
      prompt, negative: negative || null, style: style || null, seed, count,
    };
    if (aspect !== "ref" || !refSized) {
      const [w, h] = ASPECTS[aspect] || ASPECTS["1:1"];
      body.width = w;
      body.height = h;
    }
    if (customTemplate) body.template = customTemplate;
    else body.engine = sel;
    if (model && !customTemplate) body.model = model;
    if (refs.length) {
      body.reference_asset_id = refs[0].id;
      body.reference_asset_ids = refs.map((r) => r.id);
    }
    if (steps.trim()) body.steps = Number(steps);
    if (cfg.trim()) body.cfg = Number(cfg);
    if (sampler) body.sampler = sampler;
    if (scheduler) body.scheduler = scheduler;
    if (sdLike) {
      if (checkpoint) body.checkpoint = checkpoint;
      if (refs.length || useCharRef) body.strength = strength;
      body.use_character_reference = useCharRef;
    } else {
      // Qwen-Image / Kontext: the character's canonical image goes in as the
      // edit's first reference and the prompt becomes the instruction
      body.consistent = useCharRef;
    }
    try {
      const r = await api.generate(pid, body);
      setQueuedIds((ids) => [r.job.id, ...ids].slice(0, 12));
      app.refreshJobs();
      if (!seedLocked) setSeed(Math.floor(Math.random() * 2 ** 31));
      if (r.unknown_mentions.length) app.toast(t("unknownMentions", { names: r.unknown_mentions.join(", ") }), "bad");
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (maxRefs === 0) return;
    const id = e.dataTransfer.getData("text/prospero-asset");
    if (id) { addRef(await api.asset(id)); return; }
    const file = e.dataTransfer.files?.[0];
    if (file) {
      try { addRef(await api.upload(pid, file)); app.bump(); } catch (err) { app.toast((err as Error).message, "bad"); }
    }
  };
  const addRef = (a: Asset) => setRefs((cur) => (cur.some((x) => x.id === a.id) ? cur
    : maxRefs <= 1 ? [a] : [...cur, a].slice(0, maxRefs)));
  const engineLine = [
    ENGINE_LABELS[resolved] || customTemplate || resolved,
    refs.length ? (sdLike ? t("genModeImg2img") : t("genModeEdit", { n: refs.length })) : useCharRef && !sdLike ? t("genModeConsistent") : t("genModeTxt2img"),
  ].join(" · ");

  const recentItems = recent.data?.items || [];
  const toggleCompare = (id: string) => {
    if (!compare) return;
    setCompare(compare.includes(id) ? compare.filter((x) => x !== id) : [...compare, id].slice(-4));
  };

  const fragments = characters.filter((c) => composed?.matched_characters.includes(c.name)).map((c) => c.prompt || "");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("generateTitle")}</h1>
          {comfy && !comfy.reachable && <p className="err-text">{t("comfyDown", { reason: comfy.reason || "" })}</p>}
        </div>
      </div>
      <div style={{ marginBottom: 14 }}>
        <EngineBar sel={sel} setSel={setEngineSel} model={model} setModel={setModel} resolved={resolved}
          customs={customs} engines={engines.data} />
      </div>
      <div className="gen-layout">
        <div className="stack">
          <div className="card stack">
            <div className="prompt-wrap">
              <textarea ref={textRef} value={prompt} placeholder={t("promptPlaceholder")} aria-label={t("prompt")}
                onChange={(e) => onPromptChange(e.target.value, e.target.selectionStart)}
                onKeyDown={(e) => {
                  if (menu && suggestions.length) {
                    if (e.key === "ArrowDown") { e.preventDefault(); setMenu({ ...menu, index: (menu.index + 1) % suggestions.length }); }
                    if (e.key === "ArrowUp") { e.preventDefault(); setMenu({ ...menu, index: (menu.index - 1 + suggestions.length) % suggestions.length }); }
                    if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); insertMention(suggestions[menu.index]); }
                    if (e.key === "Escape") setMenu(null);
                  } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) queue();
                }} />
              {menu && suggestions.length > 0 && (
                <div className="mention-menu">
                  {suggestions.map((c, i) => (
                    <button key={c.id} className={i === menu.index ? "on" : ""} onMouseDown={(e) => { e.preventDefault(); insertMention(c); }}>
                      {c.canonical_asset_id ? <img src={thumbUrl({ id: c.canonical_asset_id, thumb_path: "x", kind: "image" })} alt="" />
                        : <span className="chip" style={{ background: c.palette[0], borderRadius: "50%" }} />}
                      <span><strong>{c.name}</strong> <span className="muted small">{c.role}</span></span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="row wrap">
              <span className="muted small">{t("mention")}:</span>
              {characters.map((c) => (
                <button key={c.id} className="btn sm ghost" onClick={() => setPrompt((p) => `${p}${p && !p.endsWith(" ") ? " " : ""}@${c.name} `)}>@{c.name}</button>
              ))}
            </div>
            <div className="grid-2">
              <label className="field">{t("style")}
                <select value={style} onChange={(e) => setStyle(e.target.value)}>
                  <option value="">{t("noStyle")}</option>
                  {(styles.data?.items || []).map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </label>
              <label className="field">{t("negative")}
                <input value={negative} onChange={(e) => setNegative(e.target.value)} placeholder="blurry, extra fingers" />
              </label>
            </div>
            {composed && (
              <div className="stack" style={{ gap: 6 }}>
                <div className="row small">
                  <span className="muted">{t("finalPrompt")}</span>
                  {composed.matched_characters.length > 0 && <span className="pill accent">{t("matched", { names: composed.matched_characters.join(", ") })}</span>}
                  {composed.unknown_mentions.length > 0 && <span className="pill bad">{t("unknownMentions", { names: composed.unknown_mentions.join(", ") })}</span>}
                </div>
                <div className="final-prompt">{highlight(composed.positive_prompt, [], fragments)}</div>
                {composed.negative_prompt && <div className="final-prompt" style={{ opacity: 0.75 }}>- {composed.negative_prompt}</div>}
              </div>
            )}
          </div>

          <div className="card">
            <h2>{t("results")}
              <div className="card-actions">
                {picked && picked.length > 0 && (
                  <button className="btn sm danger" onClick={async () => { await del.remove(picked); setPicked([]); }}>
                    <Trash2 size={14} /> {t("deleteSelected", { n: picked.length })}</button>
                )}
                <button className={`btn sm${picked ? " primary" : ""}`} onClick={() => { setPicked(picked ? null : []); setCompare(null); }}>
                  {picked ? t("selectDone") : t("selectMode")}</button>
                <button className={`btn sm${compare ? " primary" : ""}`} onClick={() => { setCompare(compare ? null : []); setPicked(null); }}><Columns2 size={14} /> {t("compare")}</button>
              </div>
            </h2>
            {del.bar}
            {compare && compare.length < 2 && <p className="muted small">{t("compareHint")}</p>}
            {compare && compare.length >= 2 && (
              <div className="compare" style={{ gridTemplateColumns: `repeat(${compare.length}, 1fr)`, marginBottom: 14 }}>
                {compare.map((id) => <img key={id} src={fileUrl(id)} alt="" onClick={() => app.openAsset(id, compare)} />)}
              </div>
            )}
            {myJobs.length === 0 && recentItems.length === 0 ? <Empty icon={<Wand2 size={30} />} text={t("noResults")} /> : (
              <div className="results-grid">
                {myJobs.filter((j) => j.state !== "done").map((j) => (
                  <div key={j.id} className="job-tile">
                    {j.state === "running" && <Loader2 size={22} className="spin" />}
                    <JobState state={j.state} />
                    <div style={{ width: "80%" }}><Progress value={j.progress} waiting={j.state === "waiting_gpu"} /></div>
                    <span>{j.message}</span>
                  </div>
                ))}
                {recentItems.map((a) => (
                  <AssetTile key={a.id} asset={a} selected={compare?.includes(a.id) || picked?.includes(a.id)} selecting={!!picked}
                    onClick={() => (picked ? setPicked(picked.includes(a.id) ? picked.filter((x) => x !== a.id) : [...picked, a.id])
                      : compare ? toggleCompare(a.id) : app.openAsset(a.id, recentItems.map((x) => x.id)))} />
                ))}
              </div>
            )}
          </div>
        </div>

        <aside className="card stack" style={{ position: "sticky", top: 0 }}>
          <div className="params">
            <div className="field" style={{ gridColumn: "1 / -1" }}><span className="hint">{engineLine}</span></div>
            <div className="field" style={{ gridColumn: "1 / -1" }}>{t("aspect")}
              <div className="segmented" style={{ flexWrap: "wrap" }}>
                {refSized && <button className={aspect === "ref" ? "on" : ""} onClick={() => setAspect("ref")} title={t("aspectRefHint")}>{t("aspectRef")}</button>}
                {Object.keys(ASPECTS).map((a) => <button key={a} className={aspect === a ? "on" : ""} onClick={() => setAspect(a)}>{a}</button>)}
              </div>
              <span className="hint mono block">{aspect === "ref" && refSized
                ? (refs[0].width && refs[0].height ? t("aspectRefSize", { w: refs[0].width, h: refs[0].height }) : t("aspectRefHint"))
                : `${(ASPECTS[aspect] || ASPECTS["1:1"])[0]} x ${(ASPECTS[aspect] || ASPECTS["1:1"])[1]}`}</span>
            </div>
            <div className="field" style={{ gridColumn: "1 / -1" }}>{t("seed")}
              <div className="seed-row">
                <input type="number" value={seed} onChange={(e) => setSeed(Number(e.target.value))} className="mono" />
                <button className={`btn icon${seedLocked ? " primary" : ""}`} onClick={() => setSeedLocked(!seedLocked)} title={t("seedLock")}>
                  {seedLocked ? <Lock size={15} /> : <LockOpen size={15} />}</button>
                <button className="btn icon" onClick={() => setSeed(Math.floor(Math.random() * 2 ** 31))} title={t("seedDice")}><Dices size={15} /></button>
              </div>
            </div>
            <label className="field" style={{ gridColumn: "1 / -1" }}>{t("count")} <span className="mono">{count}</span>
              <input type="range" min={1} max={8} value={count} onChange={(e) => setCount(Number(e.target.value))} /></label>
          </div>
          {maxRefs > 0 && (
            <div className="field">{maxRefs > 1 ? t("referencesSlot", { n: maxRefs }) : sdLike ? t("referenceSlot") : t("referenceEditSlot")}
              <div className={`drop-slot${dragOver ? " over" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)} onDrop={onDrop}>
                {refs.length === 0 && <ImagePlus size={22} />}
                <span className="grow row wrap" style={{ gap: 6 }}>
                  {refs.length === 0 && t("referenceDrop")}
                  {refs.map((r, i) => (
                    <span key={r.id} className="ref-chip" title={r.name || undefined}>
                      <img src={thumbUrl(r)} alt="" style={{ width: 40, height: 40, objectFit: "cover", borderRadius: 6 }} />
                      {maxRefs > 1 && <span className="mono small">{`<image${i + 1}>`}</span>}
                      <button className="btn sm icon ghost" onClick={() => setRefs(refs.filter((x) => x.id !== r.id))}><X size={12} /></button>
                    </span>
                  ))}
                </span>
                {refs.length < maxRefs && <button className="btn sm" onClick={() => setPicking(true)}><Upload size={14} /></button>}
              </div>
              {!sdLike && refs.length > 0 && <span className="hint">{maxRefs > 1 ? t("referenceEditHintMulti") : t("referenceEditHint")}</span>}
              {!sdLike && refs.length > 1 && unnamedRefs.length > 0 && (
                <span className="hint warn-text">{t("referenceUnnamed", { tags: unnamedRefs.join(", ") })}</span>
              )}
            </div>
          )}
          <label className="check"><input type="checkbox" checked={useCharRef} onChange={(e) => setUseCharRef(e.target.checked)} /> {sdLike ? t("useCharacterRef") : t("keepCharacter")}</label>
          {sdLike && (refs.length > 0 || useCharRef) && (
            <label className="field">{t("strength")} <span className="mono">{strength.toFixed(2)}</span>
              <input type="range" min={0.1} max={1} step={0.05} value={strength} onChange={(e) => setStrength(Number(e.target.value))} /></label>
          )}
          <details>
            <summary className="panel-title" style={{ cursor: "pointer" }}>{t("advanced")}</summary>
            <div className="params" style={{ marginTop: 8 }}>
              {sdLike && (
                <label className="field" style={{ gridColumn: "1 / -1" }}>{t("checkpoint")}
                  <select value={checkpoint} onChange={(e) => setCheckpoint(e.target.value)}>
                    <option value="">{t("checkpointDefault")}</option>
                    {checkpoints.map((c) => <option key={c} value={c}>{c}</option>)}
                  </select>
                </label>
              )}
              <label className="field">{t("steps")}<input type="number" min={1} max={150} value={steps} placeholder={String(defaults.steps)} onChange={(e) => setSteps(e.target.value)} /></label>
              <label className="field">{t("cfg")}<input type="number" min={0} max={30} step={0.5} value={cfg} placeholder={String(defaults.cfg)} onChange={(e) => setCfg(e.target.value)} /></label>
              <label className="field">{t("sampler")}
                <select value={sampler} onChange={(e) => setSampler(e.target.value)}>
                  <option value="">{defaults.sampler} ({t("engineDefault")})</option>
                  {SAMPLERS.map((x) => <option key={x}>{x}</option>)}</select></label>
              <label className="field">{t("scheduler")}
                <select value={scheduler} onChange={(e) => setScheduler(e.target.value)}>
                  <option value="">{defaults.scheduler} ({t("engineDefault")})</option>
                  {SCHEDULERS.map((x) => <option key={x}>{x}</option>)}</select></label>
            </div>
            <span className="hint">{t("advancedHint")}</span>
          </details>
          <button className="btn primary lg" onClick={queue} disabled={busy || !prompt.trim()}>
            {busy ? <Loader2 size={17} className="spin" /> : <Wand2 size={17} />} {t("queue")} <span className="kbd" style={{ color: "#fff", borderColor: "#fff6" }}>Ctrl+Enter</span>
          </button>
        </aside>
      </div>
      {picking && <AssetPicker projectId={pid} onClose={() => setPicking(false)} onPick={(a) => { addRef(a); setPicking(false); }} />}
    </>
  );
}
