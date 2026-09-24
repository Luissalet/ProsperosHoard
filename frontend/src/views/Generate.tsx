import { useEffect, useMemo, useRef, useState } from "react";
import { Columns2, Dices, ImagePlus, Loader2, Lock, LockOpen, Upload, Wand2, X } from "lucide-react";
import { api, ENGINE_NAMES, fileUrl, IMAGE_ENGINES, thumbUrl, type Asset, type Character, type Composed, type ImageEngine, type Job, type WorkflowSpec } from "../api";
import { useT } from "../i18n";
import { AssetPicker, AssetTile, Empty, JobState, Progress, useApp, useAsync, useDebounced } from "../components/ui";

const ASPECTS: Record<string, [number, number]> = {
  "1:1": [1024, 1024], "4:5": [896, 1120], "2:3": [832, 1216], "9:16": [768, 1344], "3:2": [1216, 832], "16:9": [1344, 768],
};
const SAMPLERS = ["dpmpp_2m", "dpmpp_2m_sde", "euler", "euler_ancestral", "dpmpp_sde", "heun", "uni_pc", "ddim"];
const SCHEDULERS = ["karras", "normal", "exponential", "sgm_uniform", "simple"];
// with a reference image, a txt2img template becomes its engine's edit template
const TXT2IMG_TO_EDIT: Record<string, string> = {
  sdxl_txt2img: "sdxl_img2img", qwen21_txt2img: "qwen21_edit", flux_schnell_txt2img: "flux_kontext_edit",
};
// built-in image templates that need more than a prompt (a mask, a source recipe) stay in the lightbox
const NOT_FOR_GENERATE = new Set(["sdxl_inpaint", "sdxl_hires"]);
// the sampler panel only means something for the classic Stable Diffusion workflows
const CLASSIC_VRAM = new Set(["sdxl", "sd15"]);

function readSession(key: string): string {
  try { return sessionStorage.getItem(key) || ""; } catch { return ""; }
}
function writeSession(key: string, value: string) {
  try { sessionStorage.setItem(key, value); } catch { /* storage disabled */ }
}

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
  const project = useAsync(() => api.project(pid), [pid]);

  const [prompt, setPrompt] = useState(() => readSession(`prospero.prompt.${pid}`));
  const [negative, setNegative] = useState("");
  const [style, setStyle] = useState<string>("");
  // "" = let the server pick the template from the engine (the project's, or `engine` below)
  const [template, setTemplate] = useState("");
  const [engine, setEngine] = useState<"" | ImageEngine>("");
  const [consistent, setConsistent] = useState(false);
  const [checkpoint, setCheckpoint] = useState("");
  const [aspect, setAspect] = useState("1:1");
  const [steps, setSteps] = useState(30);
  const [cfg, setCfg] = useState(6.5);
  const [sampler, setSampler] = useState("dpmpp_2m");
  const [scheduler, setScheduler] = useState("karras");
  const [seed, setSeed] = useState<number>(() => Math.floor(Math.random() * 2 ** 31));
  const [seedLocked, setSeedLocked] = useState(false);
  const [count, setCount] = useState(2);
  const [reference, setReference] = useState<Asset | null>(null);
  const [useCharRef, setUseCharRef] = useState(false);
  const [strength, setStrength] = useState(0.6);
  const [picking, setPicking] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [composed, setComposed] = useState<Composed | null>(null);
  const [queuedIds, setQueuedIds] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState<{ query: string; start: number; index: number } | null>(null);
  const [compare, setCompare] = useState<string[] | null>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { writeSession(`prospero.prompt.${pid}`, prompt); }, [prompt, pid]);

  // live final-prompt preview (a slow answer for an older prompt never replaces a newer one)
  const dPrompt = useDebounced(prompt, 300);
  const dNegative = useDebounced(negative, 300);
  useEffect(() => {
    if (!dPrompt.trim()) { setComposed(null); return; }
    let alive = true;
    api.compose(pid, dPrompt, dNegative, style || null)
      .then((c) => { if (alive) setComposed(c); })
      .catch(() => { if (alive) setComposed(null); });
    return () => { alive = false; };
  }, [dPrompt, dNegative, style, pid]);

  // style preset defaults fill the parameter panel
  useEffect(() => {
    const preset = styles.data?.items.find((s) => s.id === style);
    if (!preset) return;
    const d = preset.defaults;
    if (d.steps) setSteps(Number(d.steps));
    if (d.cfg) setCfg(Number(d.cfg));
    if (d.sampler) setSampler(String(d.sampler));
    if (d.scheduler) setScheduler(String(d.scheduler));
  }, [style, styles.data]);

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

  const { trackJobs } = app;
  useEffect(() => { trackJobs(queuedIds); }, [queuedIds, trackJobs]);
  const myJobs: Job[] = queuedIds.map((id) => app.jobs.find((j) => j.id === id)).filter(Boolean) as Job[];
  const comfy = backend.data?.comfy;
  const checkpoints = comfy?.checkpoints || [];
  const builtins: WorkflowSpec[] = (workflows.data?.builtin || []).filter((w) => w.kind === "image" && !NOT_FOR_GENERATE.has(w.template));
  const customs: WorkflowSpec[] = workflows.data?.custom || [];
  const projectEngine = (project.data?.image_engine || "auto") as ImageEngine;
  const engineName = (e: ImageEngine) => (e === "auto" ? t("engineAuto") : ENGINE_NAMES[e]);
  const spec = [...builtins, ...customs].find((w) => w.template === template);
  // the sampler panel applies to an explicit SD workflow, or to "auto" with SDXL forced
  const classic = template ? CLASSIC_VRAM.has(spec?.vram_class || "") : (engine || projectEngine) === "sdxl";

  const queue = async () => {
    if (busy || !prompt.trim()) return;
    setBusy(true);
    const [w, h] = ASPECTS[aspect];
    try {
      const body: Record<string, unknown> = {
        prompt, negative: negative || null, style: style || null, width: w, height: h,
        seed, count, checkpoint: checkpoint || null, reference_asset_id: reference?.id || null,
        strength: reference || useCharRef ? strength : null, use_character_reference: useCharRef, consistent,
      };
      if (template) body.template = reference || consistent ? TXT2IMG_TO_EDIT[template] || template : template;
      if (engine) body.engine = engine;
      // tuned per engine on the server otherwise (Flux schnell wants 4 steps, not SDXL's 30)
      if (classic) Object.assign(body, { steps, cfg, sampler, scheduler });
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
    const id = e.dataTransfer.getData("text/prospero-asset");
    if (id) { api.asset(id).then(setReference).catch((err) => app.toast((err as Error).message, "bad")); return; }
    const file = e.dataTransfer.files?.[0];
    if (file) {
      try { setReference(await api.upload(pid, file)); app.bump(); } catch (err) { app.toast((err as Error).message, "bad"); }
    }
  };

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
                  } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); queue(); }
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
                <button className={`btn sm${compare ? " primary" : ""}`} onClick={() => setCompare(compare ? null : [])}><Columns2 size={14} /> {t("compare")}</button>
              </div>
            </h2>
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
                  <AssetTile key={a.id} asset={a} selected={compare?.includes(a.id)}
                    onClick={() => (compare ? toggleCompare(a.id) : app.openAsset(a.id, recentItems.map((x) => x.id)))} />
                ))}
              </div>
            )}
          </div>
        </div>

        <aside className="card stack" style={{ position: "sticky", top: 0 }}>
          <div className="params">
            <label className="field" style={{ gridColumn: "1 / -1" }}>{t("imageEngine")}
              <select value={engine} onChange={(e) => setEngine(e.target.value as "" | ImageEngine)} disabled={Boolean(template)}>
                <option value="">{t("engineDefault", { engine: engineName(projectEngine) })}</option>
                {IMAGE_ENGINES.map((x) => <option key={x} value={x}>{engineName(x)}</option>)}
              </select>
            </label>
            <label className="field" style={{ gridColumn: "1 / -1" }}>{t("template")}
              <select value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="">{t("templateAuto")}</option>
                {builtins.map((w) => <option key={w.template} value={w.template}>{w.name || w.template}</option>)}
                {customs.map((w) => <option key={w.template} value={w.template}>{w.name || w.template}</option>)}
              </select>
            </label>
            <label className="field" style={{ gridColumn: "1 / -1" }}>{t("checkpoint")}
              <select value={checkpoint} onChange={(e) => setCheckpoint(e.target.value)}>
                <option value="">{t("checkpointDefault")}</option>
                {checkpoints.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </label>
            <div className="field" style={{ gridColumn: "1 / -1" }}>{t("aspect")}
              <div className="segmented" style={{ flexWrap: "wrap" }}>
                {Object.keys(ASPECTS).map((a) => <button key={a} className={aspect === a ? "on" : ""} onClick={() => setAspect(a)}>{a}</button>)}
              </div>
              <span className="hint mono block">{ASPECTS[aspect][0]} x {ASPECTS[aspect][1]}</span>
            </div>
            {classic ? (
              <>
                <label className="field">{t("steps")}<input type="number" min={1} max={150} value={steps} onChange={(e) => setSteps(Number(e.target.value))} /></label>
                <label className="field">{t("cfg")}<input type="number" min={0} max={30} step={0.5} value={cfg} onChange={(e) => setCfg(Number(e.target.value))} /></label>
                <label className="field">{t("sampler")}
                  <select value={sampler} onChange={(e) => setSampler(e.target.value)}>{SAMPLERS.map((s) => <option key={s}>{s}</option>)}</select></label>
                <label className="field">{t("scheduler")}
                  <select value={scheduler} onChange={(e) => setScheduler(e.target.value)}>{SCHEDULERS.map((s) => <option key={s}>{s}</option>)}</select></label>
              </>
            ) : <span className="muted small" style={{ gridColumn: "1 / -1", fontSize: 12 }}>{t("samplerHint")}</span>}
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
          <div className="field">{t("referenceSlot")}
            <div className={`drop-slot${dragOver ? " over" : ""}`} onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)} onDrop={onDrop}>
              {reference ? <img src={thumbUrl(reference)} alt="" /> : <ImagePlus size={22} />}
              <span className="grow">{reference ? reference.name : t("referenceDrop")}</span>
              {reference ? <button className="btn sm icon ghost" onClick={() => setReference(null)} aria-label={t("removeReference")} title={t("removeReference")}><X size={14} /></button>
                : <button className="btn sm" onClick={() => setPicking(true)} aria-label={t("pickImage")} title={t("pickImage")}><Upload size={14} /></button>}
            </div>
          </div>
          <label className="check"><input type="checkbox" checked={consistent} onChange={(e) => setConsistent(e.target.checked)} /> {t("consistent")}</label>
          {!consistent && <label className="check"><input type="checkbox" checked={useCharRef} onChange={(e) => setUseCharRef(e.target.checked)} /> {t("useCharacterRef")}</label>}
          {(reference || useCharRef) && (
            <label className="field">{t("strength")} <span className="mono">{strength.toFixed(2)}</span>
              <input type="range" min={0.1} max={1} step={0.05} value={strength} onChange={(e) => setStrength(Number(e.target.value))} /></label>
          )}
          <button className="btn primary lg" onClick={queue} disabled={busy || !prompt.trim()}>
            {busy ? <Loader2 size={17} className="spin" /> : <Wand2 size={17} />} {t("queue")} <span className="kbd" style={{ color: "#fff", borderColor: "#fff6" }}>Ctrl+Enter</span>
          </button>
        </aside>
      </div>
      {picking && <AssetPicker projectId={pid} onClose={() => setPicking(false)} onPick={(a) => { setReference(a); setPicking(false); }} />}
    </>
  );
}
