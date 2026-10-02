import { useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import { AudioLines, Clapperboard, GripVertical, Library, Loader2, Music, Pause, Pin, Play, Plus, RefreshCw, Upload, Wand2, X } from "lucide-react";
import { api, fileUrl, type Asset, type ProductionShot, type ProductionState, type TimedLine } from "../api";
import { useT } from "../i18n";
import { AssetPicker, Modal, useApp } from "../components/ui";
import { LyricsModal, SECTIONS, ShotEditor, liveRun } from "./Storyboard";

const clock = (s: number) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
const parseTime = (text: string): number | null => {
  const parts = text.trim().split(":").map(Number);
  if (!parts.length || parts.some((n) => Number.isNaN(n))) return null;
  return parts.reduce((a, n) => a * 60 + n, 0);
};
/** "Pre-Chorus 2" -> "prechorus", the section values a shot takes */
export function sectionValue(label: string | null | undefined): string {
  const l = (label || "").toLowerCase().replace(/[\s\d]+$/g, "").replace(/[\s_-]/g, "");
  if (SECTIONS.includes(l)) return l;
  if (l.startsWith("pre")) return "prechorus";
  if (l.startsWith("hook") || l.startsWith("refrain")) return "chorus";
  if (l.startsWith("instrumental") || l.startsWith("solo")) return "breakdown";
  return SECTIONS.find((s) => l.startsWith(s)) || "";
}

type Sel = { start: number; end: number };
type Span = { start: number; end: number };
type Op = { key: string; mode: "move" | "start" | "end"; t0: number; orig: Span; lo: number; hi: number; moved: boolean };

const r1 = (n: number) => Math.round(n * 10) / 10;

/** The song as a vertical editor timeline: the timed lyrics on one line, the
 * sections beside them and a shot track where the shots go one after another
 * - dragged in from the shot list, appended, moved, trimmed at either edge -
 * each block saying which lyrics it plays over. Shots left out of the track
 * are placed by the cut by their section. */
export function SongTrackCard({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const timing = state.timing;
  const lines: TimedLine[] = timing?.lines || [];
  const songId = timing?.song_asset_id || (state.done?.song?.song_asset_id as string | undefined) || null;
  const duration = Number(timing?.duration_s || state.done?.song?.duration_s || 0) || (lines.length ? (lines[lines.length - 1].end_s || lines[lines.length - 1].time_s + 3) : 0);
  const shots = state.spec.shots || [];
  const frames = (state.done?.frames?.items || {}) as Record<string, { best?: string }>;
  const running = liveRun(state, app.jobs);
  const audio = useRef<HTMLAudioElement>(null);
  const rail = useRef<HTMLDivElement>(null);
  const [now, setNow] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [stopAt, setStopAt] = useState<number | null>(null);
  const [sel, setSel] = useState<Sel | null>(null);
  const [anchor, setAnchor] = useState<number | null>(null);
  const [changing, setChanging] = useState(false);
  const [lyricsOpen, setLyricsOpen] = useState(false);
  const [editing, setEditing] = useState<{ shot?: ProductionShot; after?: string | null; span?: Sel; section?: string } | null>(null);
  const [assignKey, setAssignKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<Record<string, Span | null>>({});
  const [op, setOp] = useState<Op | null>(null);
  const [hover, setHover] = useState<number | null>(null);
  const pps = duration ? Math.max(16, Math.min(44, 1600 / duration)) : 20;
  const height = Math.max(240, duration * pps + 24);
  const y = (s: number) => 12 + s * pps;
  const timeAt = (clientY: number) => {
    const box = rail.current!.getBoundingClientRect();
    return Math.max(0, Math.min(duration, ((clientY - box.top - 12) / pps)));
  };
  useEffect(() => { setDraft({}); }, [state]);

  useEffect(() => {
    const el = audio.current;
    if (!el) return;
    const tick = () => {
      setNow(el.currentTime);
      if (stopAt != null && el.currentTime >= stopAt) { el.pause(); setStopAt(null); }
    };
    const on = () => setPlaying(true);
    const off = () => setPlaying(false);
    el.addEventListener("timeupdate", tick);
    el.addEventListener("play", on);
    el.addEventListener("pause", off);
    return () => { el.removeEventListener("timeupdate", tick); el.removeEventListener("play", on); el.removeEventListener("pause", off); };
  }, [stopAt, songId]);

  // the shot track: every shot placed on its stretch (with the edit in progress)
  const spanOf = (key: string): Span | null => {
    if (key in draft) return draft[key];
    const s = shots.find((x) => x.key === key);
    return s && s.start_s != null && s.end_s != null ? { start: s.start_s, end: s.end_s } : null;
  };
  const blocks = shots.map((s) => ({ key: s.key, span: spanOf(s.key) }))
    .filter((b): b is { key: string; span: Span } => Boolean(b.span)).sort((a, b) => a.span.start - b.span.start);
  const unplaced = shots.filter((s) => !spanOf(s.key));
  const autoAt = (key: string) => timing?.shots?.[key] || [];
  const linesIn = (a: number, b: number) => lines.filter((l) => l.time_s < b - 0.05 && (l.end_s ?? l.time_s + 1) > a + 0.05);
  const snapPoints = useMemo(() => {
    const pts = new Set<number>([0, duration]);
    lines.forEach((l) => { pts.add(l.time_s); if (l.end_s != null) pts.add(l.end_s); });
    (timing?.sections || []).forEach((s) => { if (s.start_s != null) pts.add(s.start_s); });
    return [...pts].sort((a, b) => a - b);
  }, [lines, duration, timing]);
  const snap = (v: number, free = false) => {
    if (!free && snapPoints.length) {
      const near = snapPoints.reduce((a, b) => (Math.abs(b - v) < Math.abs(a - v) ? b : a));
      if (Math.abs(near - v) * pps <= 10) return near;
    }
    return r1(v);
  };
  /** The free room around `at` on the track, without `except`. */
  const room = (at: number, except?: string) => {
    const others = blocks.filter((b) => b.key !== except);
    const lo = Math.max(0, ...others.filter((b) => b.span.end <= at + 1e-6).map((b) => b.span.end));
    const hi = Math.min(duration, ...others.filter((b) => b.span.start >= at - 1e-6).map((b) => b.span.start));
    return { lo, hi, inside: others.find((b) => b.span.start - 1e-6 <= at && at < b.span.end - 1e-6) };
  };
  /** A default stretch starting at `start`: to the end of its lyric line, at
   * least about two seconds, never over the next shot. */
  const stretchFrom = (start: number, except?: string): Span | null => {
    let s = start;
    const r0 = room(s, except);
    if (r0.inside) s = r0.inside.span.end;
    const { hi } = room(s, except);
    let end = s + 4;
    const line = lines.find((l) => l.time_s - 0.05 <= s && s < (l.end_s ?? l.time_s + 3));
    if (line) {
      end = line.end_s ?? line.time_s + 3;
      let i = lines.indexOf(line);
      while (end - s < 2 && i + 1 < lines.length) { i += 1; end = lines[i].end_s ?? lines[i].time_s + 3; }
    }
    end = Math.min(end, hi, duration);
    return end - s >= 0.5 ? { start: r1(s), end: r1(end) } : null;
  };

  const save = async (key: string, span: Span | null) => {
    setBusy(true);
    try {
      await api.changeShots(state.slug, [{ key, span: span ? { start_s: span.start, end_s: span.end } : null }], false);
      onChanged();
    } catch (e) {
      app.toast((e as Error).message, "bad");
      setDraft((d) => { const n = { ...d }; delete n[key]; return n; });
    } finally { setBusy(false); }
  };
  const place = (key: string, at: number) => {
    const span = stretchFrom(snap(at), key);
    if (!span) { app.toast(t("trackNoRoom"), "bad"); return; }
    setDraft((d) => ({ ...d, [key]: span }));
    save(key, span);
  };
  const appendNext = (key?: string) => {
    const k = key || unplaced[0]?.key;
    const at = blocks.length ? blocks[blocks.length - 1].span.end : 0;
    if (!k) {
      const span = stretchFrom(at);
      if (span) setEditing({ span, after: blocks.length ? blocks[blocks.length - 1].key : "start", section: sectionValue(sectionsOf(span.start, span.end)[0]?.label) });
      return;
    }
    place(k, at);
  };
  const sectionsOf = (from: number, to: number) => (timing?.sections || []).filter((s) => s.start_s != null && s.start_s < to && (s.end_s ?? duration) > from);

  // pointer editing of a block: move it, or trim its start / end
  const startOp = (e: RPointerEvent, key: string, mode: Op["mode"]) => {
    if (running || e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    const span = spanOf(key)!;
    const others = blocks.filter((b) => b.key !== key);
    const lo = Math.max(0, ...others.filter((b) => b.span.end <= span.start + 1e-6).map((b) => b.span.end));
    const hi = Math.min(duration, ...others.filter((b) => b.span.start >= span.end - 1e-6).map((b) => b.span.start));
    rail.current?.setPointerCapture(e.pointerId);
    setOp({ key, mode, t0: timeAt(e.clientY), orig: span, lo, hi, moved: false });
  };
  const moveOp = (e: RPointerEvent) => {
    if (!op) return;
    const d = timeAt(e.clientY) - op.t0;
    if (!op.moved && Math.abs(d) * pps < 3) return;
    const free = e.altKey;
    let next: Span;
    if (op.mode === "move") {
      const len = op.orig.end - op.orig.start;
      let s = snap(op.orig.start + d, free);
      const endSnap = snap(op.orig.end + d, free);
      if (Math.abs(endSnap - (op.orig.end + d)) < Math.abs(s - (op.orig.start + d))) s = endSnap - len;
      s = Math.max(op.lo, Math.min(op.hi - len, s));
      next = { start: r1(s), end: r1(s + len) };
    } else if (op.mode === "start") {
      next = { start: Math.max(op.lo, Math.min(op.orig.end - 0.5, snap(op.orig.start + d, free))), end: op.orig.end };
    } else {
      next = { start: op.orig.start, end: Math.min(op.hi, Math.max(op.orig.start + 0.5, snap(op.orig.end + d, free))) };
    }
    setOp({ ...op, moved: true });
    setDraft((dr) => ({ ...dr, [op.key]: next }));
  };
  const endOp = () => {
    if (!op) return;
    const span = draft[op.key];
    if (op.moved && span && (span.start !== op.orig.start || span.end !== op.orig.end)) save(op.key, span);
    else if (!op.moved) {
      const shot = shots.find((s) => s.key === op.key);
      if (shot) setEditing({ shot });
    }
    setOp(null);
  };

  const seek = (s: number, until?: number) => {
    const el = audio.current;
    if (!el) return;
    el.currentTime = s;
    setStopAt(until ?? null);
    el.play().catch(() => undefined);
  };
  const clickLine = (i: number, shift: boolean) => {
    const line = lines[i];
    const end = line.end_s ?? Math.min(duration, line.time_s + 3);
    if (shift && anchor != null) {
      const a = lines[anchor];
      const hiLine = a.time_s > line.time_s ? a : line;
      setSel({ start: Math.min(a.time_s, line.time_s), end: hiLine.end_s ?? Math.min(duration, hiLine.time_s + 3) });
    } else {
      setAnchor(i);
      setSel({ start: line.time_s, end });
    }
  };
  const covered = sel ? linesIn(sel.start, sel.end) : [];
  const overlap = sel ? blocks.filter((b) => b.span.start < sel.end - 1e-3 && sel.start < b.span.end - 1e-3) : [];
  const timeLyrics = async () => {
    setBusy(true);
    try { await api.timeProductionLyrics(state.slug); onChanged(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };

  const songInfo = (state.spec.song || {}) as { tags?: string; lyrics?: string };
  const hasLyrics = Boolean((songInfo.lyrics || "").trim());
  const ready = lines.length > 0 && duration > 0;
  const placedTotal = blocks.reduce((a, b) => a + (b.span.end - b.span.start), 0);

  return (
    <div className="card">
      <h2 className="row" style={{ gap: 8 }}>
        <span className="grow"><Music size={16} /> {t("trackTitle")}</span>
        <button className="btn sm" onClick={() => setLyricsOpen(true)}>{t("sbLyrics")}</button>
        <button className="btn sm" disabled={running} title={running ? t("sbPauseFirst") : undefined} onClick={() => setChanging(true)}>
          <RefreshCw size={13} /> {t("trackChangeSong")}
        </button>
      </h2>
      <p className="small muted" style={{ marginTop: -6 }}>{t("trackLead")}</p>
      {songId ? (
        <div className="row" style={{ gap: 10, marginBottom: 10 }}>
          <button className="btn sm icon" onClick={() => (playing ? audio.current?.pause() : audio.current?.play())}>{playing ? <Pause size={14} /> : <Play size={14} />}</button>
          <audio ref={audio} src={fileUrl(songId)} preload="metadata" controls style={{ flex: 1, height: 32 }} />
          <span className="mono small muted">{clock(now)} / {clock(duration)}</span>
        </div>
      ) : (
        <p className="small">{((state.partial?.song?.song_asset_ids as string[] | undefined) || []).length ? t("trackPickTakeFirst")
          : t("trackNoSong")}{songInfo.tags ? ` · ${songInfo.tags}` : ""}</p>
      )}
      {songId && !timing?.timed && hasLyrics && (
        <div className="row" style={{ gap: 8, marginBottom: 10 }}>
          <span className="small muted grow">{t("trackUntimed")}</span>
          <button className="btn sm primary" disabled={busy || running} onClick={timeLyrics}>{busy ? <Loader2 size={13} className="spin" /> : <Wand2 size={13} />} {t("trackTimeNow")}</button>
        </div>
      )}
      {songId && !hasLyrics && <p className="small muted">{t("trackNoLyrics")}</p>}

      {sel && (
        <div className="track-sel">
          <div className="row wrap" style={{ gap: 8 }}>
            <strong className="mono">{clock(sel.start)} – {clock(sel.end)}</strong>
            <span className="small muted">{(sel.end - sel.start).toFixed(1)} s · {t("trackLines", { n: covered.length })}</span>
            <span className="grow" />
            <label className="row small" style={{ gap: 4 }}>{t("trackFrom")}
              <input style={{ width: 70 }} defaultValue={clock(sel.start)} key={`s${sel.start}`}
                onBlur={(e) => { const v = parseTime(e.target.value); if (v != null && v < sel.end - 0.4) setSel({ ...sel, start: Math.max(0, v) }); }} /></label>
            <label className="row small" style={{ gap: 4 }}>{t("trackTo")}
              <input style={{ width: 70 }} defaultValue={clock(sel.end)} key={`e${sel.end}`}
                onBlur={(e) => { const v = parseTime(e.target.value); if (v != null && v > sel.start + 0.4) setSel({ ...sel, end: Math.min(duration || v, v) }); }} /></label>
            <button className="btn sm icon ghost" onClick={() => setSel(null)} title={t("trackClear")}><X size={13} /></button>
          </div>
          {covered.length > 0 && <div className="small track-sel-lines">{covered.map((l) => l.text).join(" / ")}</div>}
          {overlap.length > 0 && <div className="small err-text">{t("trackOverlap", { list: overlap.map((b) => b.key).join(", ") })}</div>}
          <div className="row wrap" style={{ gap: 6, marginTop: 6 }}>
            {songId && <button className="btn sm" onClick={() => seek(sel.start, sel.end)}><Play size={13} /> {t("trackListen")}</button>}
            <button className="btn sm primary" disabled={running || overlap.length > 0}
              onClick={() => setEditing({ span: sel, after: blocks.filter((b) => b.span.start < sel.start).pop()?.key || "start",
                                         section: sectionValue(sectionsOf(sel.start, sel.end)[0]?.label) })}>
              <Plus size={13} /> {t("trackNewShot")}</button>
            <select value={assignKey} onChange={(e) => setAssignKey(e.target.value)} style={{ maxWidth: 260 }}>
              <option value="">{t("trackAssignPick")}</option>
              {shots.map((s) => <option key={s.key} value={s.key}>{s.key} · {s.prompt.slice(0, 50)}</option>)}
            </select>
            <button className="btn sm" disabled={!assignKey || busy || running || overlap.some((b) => b.key !== assignKey)}
              onClick={() => { setDraft((d) => ({ ...d, [assignKey]: sel })); save(assignKey, sel); setSel(null); }}>
              <Pin size={13} /> {t("trackAssign")}</button>
          </div>
        </div>
      )}

      {ready ? (
        <div className="track-wrap">
          <div className="track-scroll">
            <div className="track" ref={rail} style={{ height }}
              onPointerMove={moveOp} onPointerUp={endOp} onPointerCancel={() => setOp(null)}>
              <div className="track-heads">
                <span>{t("trackColLyrics")}</span>
                <span>{t("trackColShots")} · {blocks.length} · {placedTotal.toFixed(1)} s</span>
              </div>
              {Array.from({ length: Math.floor(duration / 5) + 1 }, (_, i) => i * 5).map((s) => (
                <div key={s} className="track-tick" style={{ top: y(s) }}><span>{clock(s).replace(/\.0$/, "")}</span></div>
              ))}
              {(timing?.sections || []).filter((s) => s.start_s != null).map((s, i) => (
                <div key={i} className={`track-section sec-${sectionValue(s.label) || "other"}`}
                  style={{ top: y(s.start_s!), height: Math.max(14, ((s.end_s ?? duration) - s.start_s!) * pps) }}>
                  <span>{s.label}</span>
                </div>
              ))}
              {sel && <div className="track-selbox" style={{ top: y(sel.start), height: Math.max(4, (sel.end - sel.start) * pps) }} />}
              {lines.map((l, i) => {
                const end = l.end_s ?? l.time_s + 2;
                const on = sel ? l.time_s < sel.end - 0.05 && end > sel.start + 0.05 : false;
                const live = playing && now >= l.time_s && now < end;
                return (
                  <button key={i} className={`track-line${on ? " on" : ""}${live ? " live" : ""}`}
                    style={{ top: y(l.time_s), minHeight: Math.max(18, (end - l.time_s) * pps - 2) }}
                    title={`${clock(l.time_s)} – ${clock(end)}`}
                    onClick={(e) => clickLine(i, e.shiftKey)} onDoubleClick={() => seek(l.time_s, end)}>
                    <span className="mono track-line-t">{clock(l.time_s)}</span> <span>{l.text}</span>
                  </button>
                );
              })}
              {/* the shot track */}
              <div className={`track-lane${hover != null ? " over" : ""}`}
                onDragOver={(e) => { if (running) return; e.preventDefault(); setHover(snap(timeAt(e.clientY))); }}
                onDragLeave={() => setHover(null)}
                onDrop={(e) => {
                  e.preventDefault();
                  const key = e.dataTransfer.getData("text/x-shot");
                  const at = timeAt(e.clientY);
                  setHover(null);
                  if (key) place(key, at);
                }}
                onDoubleClick={(e) => {
                  if (running || (e.target as HTMLElement).closest(".track-block")) return;
                  const span = stretchFrom(snap(timeAt(e.clientY)));
                  if (span) setEditing({ span, after: blocks.filter((b) => b.span.start < span.start).pop()?.key || "start", section: sectionValue(sectionsOf(span.start, span.end)[0]?.label) });
                }}>
                {blocks.length === 0 && <div className="track-lane-empty small muted">{t("trackLaneEmpty")}</div>}
              </div>
              {hover != null && <div className="track-ghost" style={{ top: y(hover) }}><span className="mono">{clock(hover)}</span></div>}
              {blocks.map((b) => {
                const shot = shots.find((s) => s.key === b.key)!;
                const best = frames[b.key]?.best;
                const words = linesIn(b.span.start, b.span.end);
                const h = (b.span.end - b.span.start) * pps;
                return (
                  <div key={b.key} className={`track-block${op?.key === b.key ? " active" : ""}`}
                    style={{ top: y(b.span.start), height: Math.max(14, h - 1) }}
                    onPointerDown={(e) => startOp(e, b.key, "move")}
                    title={`${b.key} · ${clock(b.span.start)} – ${clock(b.span.end)}\n${shot.prompt}`}>
                    <div className="track-handle top" onPointerDown={(e) => startOp(e, b.key, "start")} />
                    <div className="track-block-body">
                      {best ? <img src={`/api/assets/${best}/thumb`} alt="" draggable={false} /> : <div className="track-block-icon"><Clapperboard size={14} /></div>}
                      <div className="track-block-text">
                        <div className="row" style={{ gap: 4 }}>
                          <span className="pill badge-dark">{b.key}</span>
                          <span className="mono small">{clock(b.span.start)}–{clock(b.span.end)}</span>
                          <span className="mono small muted">{(b.span.end - b.span.start).toFixed(1)}s</span>
                        </div>
                        {h > 34 && <div className="track-block-words">{words.length ? words.map((w) => `“${w.text}”`).join(" ") : <span className="muted">{t("trackInstrumental")}</span>}</div>}
                        {h > 70 && <div className="small muted ellipsis">{shot.prompt}</div>}
                      </div>
                      {!running && <button className="track-block-x" title={t("trackRemove")}
                        onPointerDown={(e) => e.stopPropagation()}
                        onClick={(e) => { e.stopPropagation(); setDraft((d) => ({ ...d, [b.key]: null })); save(b.key, null); }}><X size={11} /></button>}
                    </div>
                    <div className="track-handle bottom" onPointerDown={(e) => startOp(e, b.key, "end")} />
                  </div>
                );
              })}
              {songId && <div className="track-head" style={{ top: y(now) }} />}
            </div>
          </div>
          {/* the shot list: drag a shot onto the track, or add the next one */}
          <div className="track-bin">
            <div className="row" style={{ gap: 6, marginBottom: 6 }}>
              <strong className="small grow">{t("trackBin")}</strong>
              {busy && <Loader2 size={13} className="spin" />}
            </div>
            <button className="btn sm primary" style={{ width: "100%", marginBottom: 6 }} disabled={running} onClick={() => appendNext()}>
              <Plus size={13} /> {unplaced.length ? t("trackAppend", { n: unplaced[0].key }) : t("trackAppendNew")}
            </button>
            <p className="hint" style={{ margin: "0 0 8px" }}>{t("trackBinHint")}</p>
            <div className="track-bin-list">
              {shots.map((s) => {
                const sp = spanOf(s.key);
                const auto = autoAt(s.key);
                return (
                  <div key={s.key} className={`track-bin-item${sp ? " placed" : ""}`} draggable={!running}
                    onDragStart={(e) => { e.dataTransfer.setData("text/x-shot", s.key); e.dataTransfer.effectAllowed = "move"; }}
                    onDragEnd={() => setHover(null)}
                    onDoubleClick={() => setEditing({ shot: s })} title={s.prompt}>
                    <GripVertical size={12} className="muted" />
                    {frames[s.key]?.best ? <img src={`/api/assets/${frames[s.key].best}/thumb`} alt="" draggable={false} /> : <div className="track-block-icon"><Clapperboard size={12} /></div>}
                    <div style={{ minWidth: 0, flex: 1 }}>
                      <div className="row" style={{ gap: 4 }}>
                        <span className="pill badge-dark">{s.key}</span>
                        {sp ? <span className="mono small">{clock(sp.start)}–{clock(sp.end)}</span>
                          : <span className="small muted">{s.section ? t(`sec_${s.section}` as never) || s.section : t("trackAuto")}{auto.length ? ` · ${clock(auto[0].start_s)}` : ""}</span>}
                      </div>
                      <div className="small ellipsis">{s.prompt}</div>
                    </div>
                    {!sp && !running && <button className="btn sm icon ghost" title={t("trackAppendThis")} onClick={() => appendNext(s.key)}><Plus size={12} /></button>}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      ) : (
        hasLyrics && (
          <div className="sb-lyrics" style={{ maxHeight: 260 }}>
            {(timing?.sections || []).map((s, i) => (
              <div key={i}><strong className="small">{s.label}</strong>
                <div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{s.lines.join("\n")}</div></div>
            ))}
          </div>
        )
      )}

      {editing && (
        <ShotEditor state={state} shot={editing.shot} after={editing.shot ? undefined : editing.after} running={running}
          initialSpan={editing.shot ? undefined : editing.span} initialSection={editing.section}
          onPause={() => undefined} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setSel(null); onChanged(); }} />
      )}
      {changing && <SongChangeModal state={state} onClose={() => setChanging(false)} onChanged={() => { setChanging(false); onChanged(); }} />}
      {lyricsOpen && <LyricsModal state={state} running={running} onClose={() => setLyricsOpen(false)}
        onSaved={() => { setLyricsOpen(false); onChanged(); }} />}
    </div>
  );
}

/** Change the song: one from the library (any project), an upload, another
 * of the takes already made, or compose a new one. */
function SongChangeModal({ state, onClose, onChanged }: { state: ProductionState; onClose: () => void; onChanged: () => void }) {
  const { t } = useT();
  const app = useApp();
  const song = (state.spec.song || {}) as { tags?: string; bpm?: number; duration?: number; key?: string; language?: string; lyrics?: string; count?: number };
  const done = state.done?.song as { song_asset_ids?: string[]; song_asset_id?: string } | undefined;
  const takes: string[] = done?.song_asset_ids?.length ? done.song_asset_ids : ((state.partial?.song?.song_asset_ids || []) as string[]);
  const reused = Boolean((state.spec.song as { asset_id?: string } | undefined)?.asset_id);
  const [tab, setTab] = useState<"library" | "upload" | "takes" | "compose">("library");
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [run, setRun] = useState(false);
  const [tags, setTags] = useState(song.tags || "");
  const [bpm, setBpm] = useState(song.bpm || 120);
  const [dur, setDur] = useState(song.duration || 120);
  const [key, setKey] = useState(song.key || "");
  const [lang, setLang] = useState(song.language || "");
  const [count, setCount] = useState(Math.max(1, song.count || 1));
  const [lyrics, setLyrics] = useState(song.lyrics || "");
  const projectId = state.project_id || app.projectId || "";

  const apply = async (body: Parameters<typeof api.setProductionSong>[1]) => {
    setBusy(true);
    try {
      const out = await api.setProductionSong(state.slug, { ...body, run });
      app.toast(out.composes_on_run ? t("songWillCompose") : t("songChanged"), "ok");
      if (out.lyrics_source === "previous") app.toast(t("songKeptLyrics"), "info");
      if (out.timing_note) app.toast(out.timing_note, "info");
      app.refreshJobs();
      onChanged();
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const upload = async (file: File) => {
    if (!projectId) return;
    setBusy(true);
    try {
      const a = await api.upload(projectId, file);
      app.bump();
      if (a.kind !== "audio") { app.toast(t("songNotAudio"), "bad"); return; }
      await apply({ asset_id: a.id });
    } catch (e) { app.toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };

  return (
    <Modal title={t("trackChangeSong")} onClose={onClose} wide footer={(
      <>
        <label className="check grow"><input type="checkbox" checked={run} onChange={(e) => setRun(e.target.checked)} /> {t("songRunAfter")}</label>
        {busy && <Loader2 size={16} className="spin" />}
        {tab === "compose" && <button className="btn primary" disabled={busy || !tags.trim()} onClick={() => apply({
          compose: { tags: tags.trim(), bpm, duration: dur, key: key || undefined, language: lang || undefined, count, lyrics: lyrics.trim() || undefined },
        })}><Wand2 size={14} /> {t("songCompose")}</button>}
      </>
    )}>
      <div className="seg" style={{ marginBottom: 12 }}>
        <button className={tab === "library" ? "on" : ""} onClick={() => setTab("library")}><Library size={12} /> {t("songTabLibrary")}</button>
        <button className={tab === "upload" ? "on" : ""} onClick={() => setTab("upload")}><Upload size={12} /> {t("songTabUpload")}</button>
        <button className={tab === "takes" ? "on" : ""} onClick={() => setTab("takes")}><AudioLines size={12} /> {t("songTabTakes")}{takes.length ? ` · ${takes.length}` : ""}</button>
        <button className={tab === "compose" ? "on" : ""} onClick={() => setTab("compose")}><Wand2 size={12} /> {t("songTabCompose")}</button>
      </div>
      {done?.song_asset_id && (
        <div className="row" style={{ gap: 8, marginBottom: 12 }}>
          <span className="small muted">{t("songCurrent")}{reused ? ` · ${t("songFromLibrary")}` : ""}</span>
          <audio src={fileUrl(done.song_asset_id)} controls preload="none" style={{ flex: 1, height: 32 }} />
        </div>
      )}
      {tab === "library" && (
        <div className="stack">
          <p className="small muted">{t("songLibraryHint")}</p>
          <div><button className="btn primary" disabled={busy} onClick={() => setPicking(true)}><Library size={14} /> {t("songOpenLibrary")}</button></div>
        </div>
      )}
      {tab === "upload" && (
        <div className="stack">
          <p className="small muted">{t("songUploadHint")}</p>
          <label className="btn primary" style={{ alignSelf: "flex-start" }}><Upload size={14} /> {t("songUploadPick")}
            <input type="file" accept="audio/*,.mp3,.wav,.flac,.ogg,.m4a" hidden disabled={busy}
              onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ""; }} /></label>
        </div>
      )}
      {tab === "takes" && (
        takes.length ? (
          <div className="stack">
            {takes.map((id, i) => (
              <div key={id} className="row" style={{ gap: 10 }}>
                <strong className="mono small">{t("takeN", { n: i + 1 })}</strong>
                <audio src={fileUrl(id)} controls preload="none" style={{ flex: 1, height: 32 }} />
                {done?.song_asset_id === id ? <span className="pill ok">{t("takeInUse")}</span>
                  : <button className="btn sm primary" disabled={busy} onClick={() => apply({ take: i + 1 })}>{t("useThisTake")}</button>}
              </div>
            ))}
          </div>
        ) : <p className="small muted">{t("songNoTakes")}</p>
      )}
      {tab === "compose" && (
        <div className="stack">
          <p className="small muted">{t("songComposeHint")}</p>
          <label className="field">{t("songStyle")}<input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="funky disco pop, slap bass, four on the floor" /></label>
          <div className="row wrap" style={{ gap: 10 }}>
            <label className="field" style={{ width: 90 }}>BPM<input type="number" min={40} max={240} value={bpm} onChange={(e) => setBpm(Number(e.target.value))} /></label>
            <label className="field" style={{ width: 110 }}>{t("songDuration")}<input type="number" min={10} max={600} value={dur} onChange={(e) => setDur(Number(e.target.value))} /></label>
            <label className="field" style={{ width: 120 }}>{t("songKey")}<input value={key} onChange={(e) => setKey(e.target.value)} placeholder="A minor" /></label>
            <label className="field" style={{ width: 90 }}>{t("songLanguage")}<input value={lang} onChange={(e) => setLang(e.target.value)} placeholder="es" /></label>
            <label className="field" style={{ width: 90 }}>{t("songTakesN")}
              <select value={count} onChange={(e) => setCount(Number(e.target.value))}>{[1, 2, 3, 4].map((n) => <option key={n}>{n}</option>)}</select></label>
          </div>
          <label className="field">{t("sbLyrics")}<textarea rows={10} value={lyrics} onChange={(e) => setLyrics(e.target.value)} placeholder={"[Verse]\n...\n[Chorus]\n..."} /></label>
        </div>
      )}
      {picking && projectId && (
        <AssetPicker projectId={projectId} kind="audio" allProjects title={t("songOpenLibrary")}
          onPick={(a: Asset) => { setPicking(false); apply({ asset_id: a.id }); }} onClose={() => setPicking(false)} />
      )}
    </Modal>
  );
}
