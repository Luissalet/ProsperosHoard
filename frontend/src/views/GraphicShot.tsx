import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Copy, Film, Image as ImageIcon, Loader2, Palette, RefreshCw, Star, Trash2, Type, Layers } from "lucide-react";
import { api, type GraphicLook, type GraphicOptions, type GraphicSpec, type ProductionShot, type ProductionState, type StyleCard, type StylePreset } from "../api";
import { useT } from "../i18n";
import { Modal, useApp, useAsync } from "../components/ui";

const clockTenths = (s: number) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
const parseClock = (text: string) => {
  const parts = text.trim().split(":").map(Number);
  return parts.some((n) => Number.isNaN(n)) ? 0 : parts.reduce((a, n) => a * 60 + n, 0);
};
const HEX = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
const GRAMMARS = ["title_card", "kinetic_lyrics", "lower_third", "outro_card"] as const;
const DEFAULT_MODE: Record<string, "clip" | "overlay"> = { kinetic_lyrics: "clip", title_card: "clip", lower_third: "overlay", outro_card: "clip" };
const PALETTE_KEYS = ["gfxPalBg", "gfxPalInk", "gfxPalAccent", "gfxPalAccent2", "gfxPalMuted"] as const;

type Props = {
  state: ProductionState; shot?: ProductionShot; after?: string | null; running: boolean;
  onPause: () => void; onClose: () => void; onSaved: () => void;
  initialSpan?: { start: number; end: number }; initialSection?: string; kindSwitch?: ReactNode;
};

function text(value: unknown): string { return typeof value === "string" ? value : ""; }

/** The "Graphic" side of the shot editor: a title, kinetic lyrics, a lower third or credits drawn by code,
 * with a live still of what it will look like and a button to render it. */
export function GraphicShotEditor({ state, shot, after, running, onPause, onClose, onSaved, initialSpan, initialSection, kindSwitch }: Props) {
  const { t } = useT();
  const app = useApp();
  const isNew = !shot || shot.kind !== "graphic";
  const g0: Partial<GraphicSpec> = shot?.kind === "graphic" ? shot.graphic || {} : {};
  const d0 = (g0.data || {}) as Record<string, unknown>;
  const options = useAsync<GraphicOptions>(() => api.graphicOptions(), []);
  const projectId = state.project_id || app.projectId || "";
  const [cardsKey, setCardsKey] = useState(0);
  const cards = useAsync(() => api.styleCards(projectId || undefined), [projectId, cardsKey]);

  const [grammar, setGrammar] = useState<GraphicSpec["grammar"]>(g0.grammar || "title_card");
  const [mode, setMode] = useState<"clip" | "overlay">(g0.mode || DEFAULT_MODE[g0.grammar || "title_card"]);
  const [title, setTitle] = useState(text(d0.title));
  const [subtitle, setSubtitle] = useState(text(d0.subtitle));
  const [kicker, setKicker] = useState(text(d0.kicker));
  const [name, setName] = useState(text(d0.name));
  const [caption, setCaption] = useState(text(d0.caption));
  const [credits, setCredits] = useState(() => ((d0.credits as { role?: string; name?: string }[] | undefined) || [])
    .map((c) => `${c.role ? `${c.role}: ` : ""}${c.name || ""}`).join("\n"));
  const ownLines = (d0.lines as { text: string }[] | undefined) || [];
  const [source, setSource] = useState<"song" | "text">(grammar === "kinetic_lyrics" && ownLines.length ? "text" : "song");
  const [lineText, setLineText] = useState(grammar === "kinetic_lyrics" ? ownLines.map((l) => (typeof l === "string" ? l : l.text)).join("\n") : "");
  const [layout, setLayout] = useState(text(d0.layout) || "line");
  const [snap, setSnap] = useState(!!d0.snap_to_beats);
  const [upcoming, setUpcoming] = useState(!!d0.upcoming);
  const [style, setStyle] = useState(g0.style || "");
  const [transition, setTransition] = useState(g0.look?.transition || "");
  const [easing, setEasing] = useState(g0.look?.motion?.easing || "");
  const [palette, setPalette] = useState<string[] | null>(g0.look?.palette || null);
  const [suppress, setSuppress] = useState<boolean | null>(g0.suppress_captions ?? null);
  const [section] = useState(shot?.section || initialSection || "");
  const ownSpan = shot && shot.start_s != null && shot.end_s != null ? { start: shot.start_s, end: shot.end_s } : null;
  const [span, setSpan] = useState<{ start: number; end: number } | null>(initialSpan || ownSpan);
  const [spanText, setSpanText] = useState(() => {
    const s0 = initialSpan || ownSpan;
    return s0 ? [clockTenths(s0.start), clockTenths(s0.end)] : ["", ""];
  });
  const applySpanText = (a: string, b: string) => {
    setSpanText([a, b]);
    const s0 = parseClock(a), s1 = parseClock(b);
    if (a.trim() && b.trim() && s1 - s0 >= 0.5) setSpan({ start: s0, end: s1 });
    else setSpan(null);
  };
  const [aspect, setAspect] = useState("9:16");
  const [at, setAt] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [cardsOpen, setCardsOpen] = useState(false);
  const locked = running || busy;
  const effectiveMode = grammar === "lower_third" ? "overlay" : mode;

  const graphic = useMemo<GraphicSpec>(() => {
    const data: Record<string, unknown> = {};
    const dur = span ? span.end - span.start : 4;
    if (grammar === "title_card") Object.assign(data, { title: title.trim(), subtitle: subtitle.trim(), kicker: kicker.trim() });
    if (grammar === "lower_third") Object.assign(data, { name: name.trim(), caption: caption.trim() });
    if (grammar === "outro_card") {
      const rows = credits.split("\n").map((r) => r.trim()).filter(Boolean).map((r) => {
        const i = r.indexOf(":");
        return i > 0 ? { role: r.slice(0, i).trim(), name: r.slice(i + 1).trim() } : { role: "", name: r };
      });
      Object.assign(data, { title: title.trim(), subtitle: subtitle.trim(), credits: rows });
    }
    if (grammar === "kinetic_lyrics") {
      const rows = lineText.split("\n").map((r) => r.trim()).filter(Boolean);
      data.lines = source === "text" && rows.length
        ? rows.map((tx, i) => ({ text: tx, start_s: +(dur * i / rows.length).toFixed(2), end_s: +(dur * (i + 1) / rows.length - 0.05).toFixed(2) }))
        : null;
      Object.assign(data, { layout, snap_to_beats: snap, upcoming });
    }
    const g: GraphicSpec = { grammar, mode: effectiveMode, data, style: style || "" };
    const look: GraphicLook = {};
    if (transition) look.transition = transition;
    if (easing) look.motion = { easing };
    if (palette && palette.every((c) => HEX.test(c))) look.palette = palette;
    g.look = look;
    if (suppress !== null) g.suppress_captions = suppress;
    return g;
  }, [grammar, effectiveMode, title, subtitle, kicker, name, caption, credits, source, lineText, layout, snap, upcoming, style, transition, easing, palette, suppress, span]);

  const complete = grammar === "kinetic_lyrics" ? (source === "song" || lineText.trim().length > 0)
    : grammar === "lower_third" ? name.trim().length > 0
    : grammar === "outro_card" ? title.trim().length > 0 || credits.trim().length > 0
    : title.trim().length > 0;

  // a live still: redrawn shortly after the last change, the older request dropped
  const [preview, setPreview] = useState<{ url?: string; error?: string; busy: boolean }>({ busy: false });
  const lastUrl = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!complete || !span) { setPreview({ busy: false }); return; }
    const ctl = new AbortController();
    setPreview((p) => ({ ...p, busy: true, error: undefined }));
    const timer = window.setTimeout(async () => {
      try {
        const blob = await api.graphicPreview({ production: state.slug, graphic, start_s: span.start, end_s: span.end, aspect, max_side: 720,
                                                ...(at != null ? { at_s: at } : {}) }, ctl.signal);
        if (ctl.signal.aborted) return;
        if (lastUrl.current) URL.revokeObjectURL(lastUrl.current);
        lastUrl.current = URL.createObjectURL(blob);
        setPreview({ url: lastUrl.current, busy: false });
      } catch (e) {
        if (!ctl.signal.aborted) setPreview((p) => ({ ...p, busy: false, error: (e as Error).message }));
      }
    }, 450);
    return () => { window.clearTimeout(timer); ctl.abort(); };
  }, [graphic, span, aspect, at, complete, state.slug]);
  useEffect(() => () => { if (lastUrl.current) URL.revokeObjectURL(lastUrl.current); }, []);

  const pickGrammar = (g: GraphicSpec["grammar"]) => { setGrammar(g); setMode(DEFAULT_MODE[g]); };
  const cardList: StylePreset[] = (cards.data?.items || []).filter((c) => (c.palette || []).length > 0);
  const chosen = cardList.find((c) => c.name === style);
  const lineHint = grammar === "kinetic_lyrics" && source === "song" && !(state.timing?.lines || []).length;

  const save = async (run: boolean) => {
    if (!complete) { app.toast(t("gfxNeedText"), "bad"); return; }
    if (!span) { app.toast(t("gfxNeedSpan"), "bad"); return; }
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        ...(shot ? { key: shot.key } : { after: after ?? "start" }),
        grammar: graphic.grammar, mode: graphic.mode, data: graphic.data, style: graphic.style, look: graphic.look,
        start_s: span.start, end_s: span.end, ...(section ? { section } : {}), run,
      };
      if (suppress !== null) body.suppress_captions = suppress;
      await api.setGraphicShot(state.slug, body);
      app.toast(run ? t("sbSavedRun") : t("sbSaved"), "ok");
      onSaved();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!shot || !window.confirm(t("sbDeleteConfirm", { n: shot.key }))) return;
    setBusy(true);
    try { await api.changeShots(state.slug, [{ key: shot.key, delete: true }], false); onSaved(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const render = async (what: "video" | "still" | "alpha") => {
    if (!shot) return;
    setBusy(true);
    try { await api.renderGraphic(state.slug, shot.key, what, aspect); app.toast(t("gfxRendering"), "ok"); app.refreshJobs(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };

  const opt = options.data;
  return (
    <>
      <Modal title={isNew ? t("sbNewShot") : t("sbShotN", { n: shot!.key })} onClose={onClose} wide footer={running ? (
        <>
          <span className="hint grow">{t("sbPauseFirst")}</span>
          {state.job_id && <button className="btn primary" onClick={onPause}>{t("sbPause")}</button>}
        </>
      ) : (
        <>
          {!isNew && <button className="btn ghost" disabled={busy} onClick={remove}><Trash2 size={14} /> {t("sbDelete")}</button>}
          <span className="grow" />
          <button className="btn" disabled={busy} onClick={() => save(false)}>{t("sbSave")}</button>
          <button className="btn primary" disabled={busy} onClick={() => save(true)}>{busy ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} {t("sbSaveRun")}</button>
        </>
      )}>
        {kindSwitch}
        <div className="gfx-grid">
          <fieldset disabled={locked} className="stack gfx-form" style={{ border: 0, padding: 0, margin: 0 }}>
            <div className="field">{t("gfxGrammar")}
              <div className="seg gfx-seg" role="group" aria-label={t("gfxGrammar")}>
                {GRAMMARS.map((g) => (
                  <button key={g} type="button" className={grammar === g ? "on" : ""} onClick={() => pickGrammar(g)}>{t(`gfxG_${g}` as never)}</button>
                ))}
              </div>
              <span className="small muted">{t(`gfxGHint_${grammar}` as never)}</span>
            </div>

            {(grammar === "title_card" || grammar === "outro_card") && (
              <>
                {grammar === "title_card" && <label className="field">{t("gfxKicker")}<input value={kicker} onChange={(e) => setKicker(e.target.value)} placeholder={t("gfxKickerPh")} /></label>}
                <label className="field">{grammar === "title_card" ? t("gfxTitle") : t("gfxOutroTitle")}<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
                <label className="field">{t("gfxSubtitle")}<input value={subtitle} onChange={(e) => setSubtitle(e.target.value)} /></label>
              </>
            )}
            {grammar === "outro_card" && (
              <label className="field">{t("gfxCredits")}
                <textarea rows={4} value={credits} onChange={(e) => setCredits(e.target.value)} placeholder={t("gfxCreditsPh")} />
                <span className="small muted">{t("gfxCreditsHint")}</span>
              </label>
            )}
            {grammar === "lower_third" && (
              <>
                <label className="field">{t("gfxName")}<input value={name} onChange={(e) => setName(e.target.value)} /></label>
                <label className="field">{t("gfxCaption")}<input value={caption} onChange={(e) => setCaption(e.target.value)} /></label>
              </>
            )}
            {grammar === "kinetic_lyrics" && (
              <>
                <div className="field">{t("gfxLyricsFrom")}
                  <div className="seg gfx-seg">
                    <button type="button" className={source === "song" ? "on" : ""} onClick={() => setSource("song")}>{t("gfxFromSong")}</button>
                    <button type="button" className={source === "text" ? "on" : ""} onClick={() => setSource("text")}>{t("gfxOwnText")}</button>
                  </div>
                </div>
                {source === "text" && (
                  <label className="field">{t("gfxLines")}
                    <textarea rows={4} value={lineText} onChange={(e) => setLineText(e.target.value)} />
                    <span className="small muted">{t("gfxLinesHint")}</span>
                  </label>
                )}
                {lineHint && <p className="small muted">{t("gfxNoTimedLyrics")}</p>}
                <div className="row wrap" style={{ gap: 14 }}>
                  <label className="field" style={{ minWidth: 150 }}>{t("gfxLayout")}
                    <select value={layout} onChange={(e) => setLayout(e.target.value)}>
                      <option value="line">{t("gfxLayoutLine")}</option>
                      <option value="word">{t("gfxLayoutWord")}</option>
                    </select>
                  </label>
                  <label className="check"><input type="checkbox" checked={snap} onChange={(e) => setSnap(e.target.checked)} /> {t("gfxSnap")}</label>
                  <label className="check"><input type="checkbox" checked={upcoming} onChange={(e) => setUpcoming(e.target.checked)} /> {t("gfxUpcoming")}</label>
                </div>
              </>
            )}

            <div className="field">{t("spanTitle")}
              <span className="small muted">{span ? t("gfxSpanHint") : t("gfxNeedSpan")}</span>
              <div className="row wrap" style={{ gap: 8 }}>
                <label className="row small" style={{ gap: 4 }}>{t("trackFrom")}
                  <input style={{ width: 80 }} value={spanText[0]} placeholder="0:12.4" onChange={(e) => applySpanText(e.target.value, spanText[1])} /></label>
                <label className="row small" style={{ gap: 4 }}>{t("trackTo")}
                  <input style={{ width: 80 }} value={spanText[1]} placeholder="0:19.8" onChange={(e) => applySpanText(spanText[0], e.target.value)} /></label>
                {span && <span className="small muted">{(span.end - span.start).toFixed(1)} s</span>}
              </div>
            </div>

            <div className="row wrap" style={{ gap: 14 }}>
              <label className="field" style={{ minWidth: 190 }}>{t("gfxMode")}
                <select value={effectiveMode} disabled={grammar === "lower_third"} onChange={(e) => setMode(e.target.value as "clip" | "overlay")}>
                  <option value="clip">{t("gfxModeClip")}</option>
                  <option value="overlay">{t("gfxModeOverlay")}</option>
                </select>
              </label>
              <label className="check" title={t("gfxSuppressHint")}>
                <input type="checkbox" checked={suppress ?? grammar === "kinetic_lyrics"} onChange={(e) => setSuppress(e.target.checked)} /> {t("gfxSuppress")}
              </label>
            </div>

            <div className="field">{t("gfxStyle")}
              <div className="row wrap" style={{ gap: 8 }}>
                <select value={style} onChange={(e) => setStyle(e.target.value)} style={{ minWidth: 200 }}>
                  <option value="">{t("gfxStyleNone")}</option>
                  {cardList.map((c) => <option key={c.id} value={c.name}>{c.name} {"★".repeat(c.quality || 0)}</option>)}
                  {style && !chosen && <option value={style}>{style}</option>}
                </select>
                <button type="button" className="btn sm" onClick={() => setCardsOpen(true)}><Palette size={13} /> {t("gfxCards")}</button>
              </div>
              {chosen && (
                <span className="gfx-card-line">
                  <Swatches colours={chosen.palette || []} />
                  <span className="small muted">{chosen.signature_transition ? t(`gfxT_${chosen.signature_transition}` as never) : ""}</span>
                  {chosen.technique && <span className="small muted">{chosen.technique}</span>}
                  {chosen.pitfalls && <span className="small muted gfx-pitfalls">{t("gfxPitfalls")}: {chosen.pitfalls}</span>}
                </span>
              )}
              {style && !chosen && cards.data && <span className="small muted">{t("gfxStyleMissing")}</span>}
            </div>

            <details className="gfx-adv">
              <summary>{t("gfxLookTitle")}</summary>
              <div className="row wrap" style={{ gap: 14, marginTop: 8 }}>
                <label className="field" style={{ minWidth: 170 }}>{t("gfxTransition")}
                  <select value={transition} onChange={(e) => setTransition(e.target.value)}>
                    <option value="">{t("gfxFromStyle")}</option>
                    {(opt?.transitions || []).map((x) => <option key={x} value={x}>{t(`gfxT_${x}` as never)}</option>)}
                  </select>
                </label>
                <label className="field" style={{ minWidth: 170 }}>{t("gfxEasing")}
                  <select value={easing} onChange={(e) => setEasing(e.target.value)}>
                    <option value="">{t("gfxFromStyle")}</option>
                    {(opt?.easings || []).map((x) => <option key={x} value={x}>{x.replace(/_/g, " ")}</option>)}
                  </select>
                </label>
              </div>
              <label className="check" style={{ marginTop: 8 }}>
                <input type="checkbox" checked={palette !== null}
                  onChange={(e) => setPalette(e.target.checked ? [...(chosen?.palette?.length ? chosen.palette : opt?.default_look.palette || ["#0b0b12", "#f4f1ea", "#ff3d81", "#3dd6ff", "#8a8aa0"])] : null)} /> {t("gfxOwnPalette")}
              </label>
              {palette && <PaletteEditor palette={palette} onChange={setPalette} />}
            </details>
          </fieldset>

          <div className="gfx-side">
            <div className="gfx-stage" data-aspect={aspect}>
              {preview.url && <img src={preview.url} alt={t("gfxPreview")} className={preview.busy ? "dim" : ""} />}
              {!preview.url && !preview.error && <span className="muted small">{!span ? t("gfxNeedSpan") : complete ? "" : t("gfxNeedText")}</span>}
              {preview.busy && <Loader2 size={20} className="spin gfx-spin" />}
              {preview.error && <p className="gfx-error small">{preview.error}</p>}
            </div>
            <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
              <div className="seg">
                {["9:16", "16:9", "1:1"].map((a) => <button key={a} type="button" className={aspect === a ? "on" : ""} onClick={() => setAspect(a)}>{a}</button>)}
              </div>
              {span && (
                <label className="row small grow" style={{ gap: 6, minWidth: 140 }} title={t("gfxMoment")}>
                  <span className="muted">{t("gfxMoment")}</span>
                  <input type="range" min={0} max={Math.max(1, span.end - span.start)} step={0.1} value={at ?? 0} style={{ flex: 1 }}
                    onChange={(e) => setAt(parseFloat(e.target.value))} />
                  {at != null && <button type="button" className="btn xs ghost" onClick={() => setAt(null)}>{t("gfxMomentAuto")}</button>}
                </label>
              )}
            </div>
            <div className="gfx-render">
              <span className="small muted">{t("gfxRenderTitle")}</span>
              <div className="row wrap" style={{ gap: 6 }}>
                <button className="btn sm" disabled={isNew || locked} onClick={() => render("video")}><Film size={13} /> {t("gfxRenderVideo")}</button>
                <button className="btn sm" disabled={isNew || locked} onClick={() => render("still")}><ImageIcon size={13} /> {t("gfxRenderStill")}</button>
                <button className="btn sm" disabled={isNew || locked} title={t("gfxRenderAlphaHint")} onClick={() => render("alpha")}><Layers size={13} /> {t("gfxRenderAlpha")}</button>
              </div>
              {isNew && <span className="small muted">{t("gfxSaveFirst")}</span>}
            </div>
          </div>
        </div>
      </Modal>
      {cardsOpen && <StyleCardsModal projectId={projectId} onClose={() => { setCardsOpen(false); setCardsKey((k) => k + 1); }} />}
    </>
  );
}

function Swatches({ colours }: { colours: string[] }) {
  return <span className="gfx-swatches" aria-hidden="true">{colours.map((c, i) => <i key={i} style={{ background: c }} />)}</span>;
}

function PaletteEditor({ palette, onChange }: { palette: string[]; onChange: (p: string[]) => void }) {
  const { t } = useT();
  return (
    <div className="gfx-palette">
      {PALETTE_KEYS.map((k, i) => (
        <label key={k} className="gfx-colour">
          <span className="small muted">{t(k)}</span>
          <span className="row" style={{ gap: 4 }}>
            <input type="color" value={HEX.test(palette[i] || "") && palette[i].length === 7 ? palette[i] : "#000000"} aria-label={t(k)}
              onChange={(e) => onChange(palette.map((c, j) => (j === i ? e.target.value : c)))} />
            <input value={palette[i] || ""} className={HEX.test(palette[i] || "") ? "" : "bad"} maxLength={7} style={{ width: 84 }}
              onChange={(e) => onChange(palette.map((c, j) => (j === i ? e.target.value : c)))} />
          </span>
        </label>
      ))}
    </div>
  );
}

type Draft = {
  name: string; technique: string; palette: string[]; transition: string; quality: number; pitfalls: string;
  easing: string; bezier: string; stagger: number; stepped: number; pop: number;
  fontDisplay: string; fontBody: string; textCase: string; align: string; tracking: number;
  bg: string; grain: number; glow: number; shadow: string; rgbSplit: number; scanlines: number; jitter: number; rule: boolean;
};

function draftOf(c: StylePreset | StyleCard): Draft {
  const typo = c.typography || {};
  const motion = c.motion || {};
  return {
    name: c.name, technique: c.technique || "", palette: [...(c.palette || [])], transition: c.signature_transition || "", quality: c.quality || 0,
    pitfalls: c.pitfalls || "", easing: motion.easing || "", bezier: motion.bezier ? motion.bezier.join(", ") : "", stagger: motion.stagger_s ?? 0.07,
    stepped: motion.stepped_fps ?? 0, pop: motion.pop ?? 1, fontDisplay: typo.fonts?.display || "bebas-neue", fontBody: typo.fonts?.body || "inter",
    textCase: typo.case || "upper", align: typo.align || "center", tracking: typo.tracking ?? 0, bg: typo.background?.kind || "gradient",
    grain: typo.background?.grain ?? 0, glow: typo.fx?.glow ?? 0, shadow: typo.fx?.shadow || "soft", rgbSplit: typo.fx?.rgb_split ?? 0,
    scanlines: typo.fx?.scanlines ?? 0, jitter: typo.fx?.jitter_deg ?? 0, rule: typo.fx?.rule ?? true,
  };
}

function inputOf(d: Draft, builtinTypo: StylePreset["typography"]) {
  const bez = d.bezier.split(",").map((x) => parseFloat(x)).filter((x) => !Number.isNaN(x));
  return {
    name: d.name, technique: d.technique, palette: d.palette, signature_transition: d.transition || null, quality: d.quality, pitfalls: d.pitfalls,
    motion: { ...(bez.length === 4 ? { bezier: bez } : d.easing ? { easing: d.easing } : {}), stagger_s: d.stagger, stepped_fps: d.stepped, pop: d.pop },
    typography: {
      ...(builtinTypo || {}),
      fonts: { display: d.fontDisplay, body: d.fontBody }, case: d.textCase, align: d.align, tracking: d.tracking,
      background: { kind: d.bg, grain: d.grain },
      fx: { glow: d.glow, shadow: d.shadow, rgb_split: d.rgbSplit, scanlines: d.scanlines, jitter_deg: d.jitter, rule: d.rule },
    },
  };
}

/** The style cards: how each look draws and moves its titles and lyrics. The built-in ones are read-only;
 * "Duplicate" makes one of your own to change. */
export function StyleCardsModal({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [reload, setReload] = useState(0);
  const list = useAsync(() => api.styleCards(projectId || undefined), [projectId, reload]);
  const options = useAsync<GraphicOptions>(() => api.graphicOptions(), []);
  const cards = (list.data?.items || []).filter((c) => (c.palette || []).length > 0);
  const [selected, setSelected] = useState<string | null>(null);
  const card = cards.find((c) => c.id === selected) || cards[0];
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setDraft(card ? draftOf(card) : null); }, [card?.id, list.data]);
  const readOnly = !card || !!card.is_builtin;
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => (d ? { ...d, [k]: v } : d));
  const opt = options.data;

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    try { await fn(); app.toast(ok, "ok"); setReload((n) => n + 1); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const duplicate = async () => {
    if (!card) return;
    const base = `${card.name} (${t("gfxCopy")})`;
    try {
      setBusy(true);
      const made = await api.createStyleCard({ name: base, from_card: card.id, ...(projectId ? { project: projectId } : {}) });
      setSelected(made.id);
      setReload((n) => n + 1);
      app.toast(t("gfxCardDuplicated"), "ok");
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={t("gfxCardsTitle")} onClose={onClose} wide footer={(
      <>
        {card && !card.is_builtin && <button className="btn ghost" disabled={busy}
          onClick={() => window.confirm(t("gfxCardDeleteConfirm", { n: card.name })) && run(async () => { await api.deleteStyleCard(card.id); setSelected(null); }, t("gfxCardDeleted"))}>
          <Trash2 size={14} /> {t("sbDelete")}</button>}
        <span className="grow" />
        {card && <button className="btn" disabled={busy} onClick={duplicate}><Copy size={14} /> {t("gfxCardDuplicate")}</button>}
        {card && !card.is_builtin && draft && <button className="btn primary" disabled={busy}
          onClick={() => run(() => api.updateStyleCard(card.id, inputOf(draft, card.typography)), t("saved"))}>{t("save")}</button>}
      </>
    )}>
      <p className="small muted">{t("gfxCardsHint")}</p>
      <div className="gfx-cards">
        <div className="gfx-card-list" role="listbox" aria-label={t("gfxCardsTitle")}>
          {cards.map((c) => (
            <button key={c.id} type="button" role="option" aria-selected={c.id === card?.id} className={`gfx-card-item${c.id === card?.id ? " on" : ""}`} onClick={() => setSelected(c.id)}>
              <span className="row" style={{ gap: 6 }}><strong className="grow">{c.name}</strong>
                <span className="gfx-stars" title={t("gfxQuality")}>{[1, 2, 3].map((n) => <Star key={n} size={11} fill={n <= (c.quality || 0) ? "currentColor" : "none"} />)}</span></span>
              <Swatches colours={c.palette || []} />
              <span className="small muted">{c.is_builtin ? t("gfxBuiltin") : t("gfxCustom")}{c.signature_transition ? ` · ${t(`gfxT_${c.signature_transition}` as never)}` : ""}</span>
            </button>
          ))}
          {list.loading && <Loader2 className="spin" size={16} />}
        </div>
        {card && draft && (
          <fieldset className="stack gfx-card-form" disabled={readOnly || busy} style={{ border: 0, padding: 0, margin: 0 }}>
            {readOnly && <p className="small muted"><Type size={12} /> {t("gfxBuiltinHint")}</p>}
            <label className="field">{t("name")}<input value={draft.name} onChange={(e) => set("name", e.target.value)} /></label>
            <label className="field">{t("gfxTechnique")}<textarea rows={3} value={draft.technique} onChange={(e) => set("technique", e.target.value)} /></label>
            <label className="field">{t("gfxPitfallsLabel")}<textarea rows={2} value={draft.pitfalls} onChange={(e) => set("pitfalls", e.target.value)} /></label>
            <div className="row wrap" style={{ gap: 14 }}>
              <label className="field" style={{ minWidth: 150 }}>{t("gfxQuality")}
                <select value={draft.quality} onChange={(e) => set("quality", parseInt(e.target.value, 10))}>
                  {[0, 1, 2, 3].map((n) => <option key={n} value={n}>{n === 0 ? t("gfxUnrated") : "★".repeat(n)}</option>)}
                </select>
              </label>
              <label className="field" style={{ minWidth: 170 }}>{t("gfxSignature")}
                <select value={draft.transition} onChange={(e) => set("transition", e.target.value)}>
                  <option value="">{t("gfxNone")}</option>
                  {(opt?.transitions || []).map((x) => <option key={x} value={x}>{t(`gfxT_${x}` as never)}</option>)}
                </select>
              </label>
            </div>
            <div className="field">{t("gfxPalette")}<PaletteEditor palette={draft.palette} onChange={(p) => set("palette", p)} /></div>
            <div className="field">{t("gfxMotion")}
              <div className="row wrap" style={{ gap: 12 }}>
                <label className="field" style={{ minWidth: 150 }}>{t("gfxEasing")}
                  <select value={draft.easing} onChange={(e) => set("easing", e.target.value)}>
                    <option value="">{t("gfxNone")}</option>
                    {(opt?.easings || []).map((x) => <option key={x} value={x}>{x.replace(/_/g, " ")}</option>)}
                  </select>
                </label>
                <label className="field" style={{ minWidth: 150 }}>{t("gfxBezier")}
                  <input value={draft.bezier} placeholder="0.7, 0, 0.2, 1" onChange={(e) => set("bezier", e.target.value)} /></label>
                <label className="field" style={{ width: 110 }}>{t("gfxStagger")}
                  <input type="number" step={0.01} min={0} max={0.5} value={draft.stagger} onChange={(e) => set("stagger", parseFloat(e.target.value) || 0)} /></label>
                <label className="field" style={{ width: 110 }} title={t("gfxSteppedHint")}>{t("gfxStepped")}
                  <input type="number" step={1} min={0} max={30} value={draft.stepped} onChange={(e) => set("stepped", parseInt(e.target.value, 10) || 0)} /></label>
                <label className="field" style={{ width: 100 }}>{t("gfxPop")}
                  <input type="number" step={0.1} min={0} max={2} value={draft.pop} onChange={(e) => set("pop", parseFloat(e.target.value) || 0)} /></label>
              </div>
            </div>
            <div className="field">{t("gfxTypography")}
              <div className="row wrap" style={{ gap: 12 }}>
                <label className="field" style={{ minWidth: 150 }}>{t("gfxFontDisplay")}
                  <select value={draft.fontDisplay} onChange={(e) => set("fontDisplay", e.target.value)}>{(opt?.fonts || []).map((f) => <option key={f}>{f}</option>)}</select></label>
                <label className="field" style={{ minWidth: 150 }}>{t("gfxFontBody")}
                  <select value={draft.fontBody} onChange={(e) => set("fontBody", e.target.value)}>{(opt?.fonts || []).map((f) => <option key={f}>{f}</option>)}</select></label>
                <label className="field" style={{ minWidth: 120 }}>{t("gfxCase")}
                  <select value={draft.textCase} onChange={(e) => set("textCase", e.target.value)}>
                    <option value="upper">{t("gfxCaseUpper")}</option><option value="none">{t("gfxCaseNone")}</option></select></label>
                <label className="field" style={{ minWidth: 120 }}>{t("gfxAlign")}
                  <select value={draft.align} onChange={(e) => set("align", e.target.value)}>
                    <option value="center">{t("gfxAlignCenter")}</option><option value="left">{t("gfxAlignLeft")}</option></select></label>
                <label className="field" style={{ width: 100 }}>{t("gfxTracking")}
                  <input type="number" step={0.01} min={-0.1} max={0.5} value={draft.tracking} onChange={(e) => set("tracking", parseFloat(e.target.value) || 0)} /></label>
              </div>
            </div>
            <div className="field">{t("gfxBackground")}
              <div className="row wrap" style={{ gap: 12 }}>
                <label className="field" style={{ minWidth: 150 }}>{t("gfxBgKind")}
                  <select value={draft.bg} onChange={(e) => set("bg", e.target.value)}>{(opt?.backgrounds || []).map((b) => <option key={b} value={b}>{t(`gfxBg_${b}` as never)}</option>)}</select></label>
                <label className="field" style={{ width: 100 }}>{t("gfxGrain")}
                  <input type="number" step={0.05} min={0} max={1} value={draft.grain} onChange={(e) => set("grain", parseFloat(e.target.value) || 0)} /></label>
                <label className="field" style={{ minWidth: 130 }}>{t("gfxShadow")}
                  <select value={draft.shadow} onChange={(e) => set("shadow", e.target.value)}>{(opt?.shadows || []).map((b) => <option key={b} value={b}>{t(`gfxSh_${b}` as never)}</option>)}</select></label>
                <label className="field" style={{ width: 100 }}>{t("gfxGlow")}
                  <input type="number" step={0.05} min={0} max={1} value={draft.glow} onChange={(e) => set("glow", parseFloat(e.target.value) || 0)} /></label>
                <label className="field" style={{ width: 110 }}>{t("gfxRgbSplit")}
                  <input type="number" step={0.05} min={0} max={1} value={draft.rgbSplit} onChange={(e) => set("rgbSplit", parseFloat(e.target.value) || 0)} /></label>
                <label className="field" style={{ width: 110 }}>{t("gfxScanlines")}
                  <input type="number" step={0.05} min={0} max={1} value={draft.scanlines} onChange={(e) => set("scanlines", parseFloat(e.target.value) || 0)} /></label>
                <label className="field" style={{ width: 110 }}>{t("gfxJitter")}
                  <input type="number" step={0.5} min={0} max={6} value={draft.jitter} onChange={(e) => set("jitter", parseFloat(e.target.value) || 0)} /></label>
                <label className="check"><input type="checkbox" checked={draft.rule} onChange={(e) => set("rule", e.target.checked)} /> {t("gfxRule")}</label>
              </div>
            </div>
          </fieldset>
        )}
      </div>
    </Modal>
  );
}
