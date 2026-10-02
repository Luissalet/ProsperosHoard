import { useEffect, useState } from "react";
import { Loader2, Sparkles } from "lucide-react";
import { useT, type MessageKey } from "../i18n";
import type { Finishing } from "../api";

// One-click looks: a starting point the sliders below then adjust.
const PRESETS: { key: MessageKey; look: Finishing }[] = [
  { key: "lookPresetClean", look: {} },
  { key: "lookPresetClub", look: { color_grade: "teal_orange", beat_fx: { source: "kick", zoom: 0.5, flash: 0.35 } } },
  { key: "lookPresetLive", look: { color_grade: "bleach_bypass", grain: 0.25, beat_fx: { source: "beats", zoom: 0.3, shake: 0.3 } } },
  { key: "lookPresetHorror", look: { color_grade: "sodium_night", grain: 0.35, vignette: true, lyric_style: "horror",
    beat_fx: { source: "downbeats", shake: 0.6, flash: 0.2 } } },
  { key: "lookPresetCinema", look: { color_grade: "teal_orange", grain: 0.15, letterbox: true } },
];

const same = (a: Finishing, b: Finishing) => JSON.stringify(a) === JSON.stringify(b);

/** The cut's look: colour grade, texture, lyric style and the beat effects
 * (zoom / flash / shake on the kick, the beats or the bars). Edits stay
 * local until "Apply", which the caller turns into a re-render. */
export function LookPanel({ value, onApply, note, compact }: {
  value: Finishing | undefined; onApply: (f: Finishing) => Promise<void>; note?: string; compact?: boolean;
}) {
  const { t } = useT();
  const [look, setLook] = useState<Finishing>(value || {});
  const [busy, setBusy] = useState(false);
  useEffect(() => { setLook(value || {}); }, [JSON.stringify(value || {})]); // eslint-disable-line react-hooks/exhaustive-deps
  const fx = look.beat_fx || { source: "kick" as const };
  const setFx = (patch: Partial<NonNullable<Finishing["beat_fx"]>>) => {
    const next = { ...fx, ...patch };
    const on = (next.zoom || 0) > 0 || (next.flash || 0) > 0 || (next.shake || 0) > 0;
    const copy = { ...look };
    if (on) copy.beat_fx = next; else delete copy.beat_fx;
    setLook(copy);
  };
  const set = (k: keyof Finishing, v: unknown) => {
    const copy: Record<string, unknown> = { ...look };
    if (v === "" || v === false || v === 0 || v === null || v === undefined) delete copy[k];
    else copy[k] = v;
    setLook(copy as Finishing);
  };
  const dirty = !same(look, value || {});
  const apply = async () => {
    setBusy(true);
    try { await onApply(look); } finally { setBusy(false); }
  };
  const slider = (label: string, v: number | undefined, onChange: (n: number) => void) => (
    <label className="field">{label} <span className="mono small">{Math.round((v || 0) * 100)}%</span>
      <input type="range" min={0} max={1} step={0.05} value={v || 0} onChange={(e) => onChange(Number(e.target.value))} /></label>
  );

  return (
    <div className="stack look-panel" style={{ gap: 10 }}>
      <div className="row wrap" style={{ gap: 6 }}>
        {PRESETS.map((p) => (
          <button key={p.key} type="button" className={`btn sm ${same(look, p.look) ? "primary" : "ghost"}`} onClick={() => setLook(p.look)}>
            {t(p.key)}
          </button>
        ))}
      </div>
      <div className={compact ? "stack" : "grid-2"} style={{ gap: 10 }}>
        <div className="stack" style={{ gap: 8 }}>
          <span className="panel-title">{t("lookBeatTitle")}</span>
          <label className="field">{t("lookBeatSource")}
            <select value={fx.source || "kick"} onChange={(e) => setFx({ source: e.target.value as "kick" | "beats" | "downbeats" })}>
              <option value="kick">{t("lookSourceKick")}</option>
              <option value="beats">{t("lookSourceBeats")}</option>
              <option value="downbeats">{t("lookSourceBars")}</option>
            </select></label>
          {slider(t("lookZoom"), fx.zoom, (n) => setFx({ zoom: n }))}
          {slider(t("lookFlash"), fx.flash, (n) => setFx({ flash: n }))}
          {slider(t("lookShake"), fx.shake, (n) => setFx({ shake: n }))}
        </div>
        <div className="stack" style={{ gap: 8 }}>
          <span className="panel-title">{t("lookColourTitle")}</span>
          <label className="field">{t("lookGrade")}
            <select value={look.color_grade || ""} onChange={(e) => set("color_grade", e.target.value)}>
              <option value="">{t("lookGradeNone")}</option>
              <option value="teal_orange">{t("lookGradeTeal")}</option>
              <option value="sodium_night">{t("lookGradeSodium")}</option>
              <option value="bleach_bypass">{t("lookGradeBleach")}</option>
            </select></label>
          {slider(t("lookGrain"), look.grain, (n) => set("grain", n))}
          <div className="row wrap" style={{ gap: 12 }}>
            <label className="check"><input type="checkbox" checked={!!look.vignette} onChange={(e) => set("vignette", e.target.checked)} /> {t("lookVignette")}</label>
            <label className="check"><input type="checkbox" checked={!!look.letterbox} onChange={(e) => set("letterbox", e.target.checked)} /> {t("lookLetterbox")}</label>
            <label className="check"><input type="checkbox" checked={!!look.glitch_on_downbeats} onChange={(e) => set("glitch_on_downbeats", e.target.checked)} /> {t("lookGlitch")}</label>
          </div>
          <label className="field">{t("lookLyrics")}
            <select value={look.lyric_style || ""} onChange={(e) => set("lyric_style", e.target.value)}>
              <option value="">{t("lookLyricsDefault")}</option>
              <option value="bold">{t("lookLyricsBold")}</option>
              <option value="horror">{t("lookLyricsHorror")}</option>
            </select></label>
        </div>
      </div>
      <div className="row" style={{ gap: 8 }}>
        <span className="hint grow">{note || t("lookNote")}</span>
        <button className="btn sm primary" disabled={!dirty || busy} onClick={apply}>
          {busy ? <Loader2 size={13} className="spin" /> : <Sparkles size={13} />} {t("lookApply")}
        </button>
      </div>
    </div>
  );
}
