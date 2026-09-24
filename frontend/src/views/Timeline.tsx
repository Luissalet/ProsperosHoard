import { useEffect, useMemo, useState } from "react";
import { Clapperboard, Film, Loader2, Minus, Plus, Scissors, Wand2 } from "lucide-react";
import { api, fileUrl, thumbUrl, type Analysis, type Asset, type Clip, type LyricClip, type Timeline } from "../api";
import { useT } from "../i18n";
import { Empty, JobState, Modal, Progress, fmtTime, useApp, useAsync, useTrackedJob } from "../components/ui";

const TRANSITIONS = ["cut", "crossfade", "dip_black", "flash_white"];
const PANS = ["none", "left", "right", "up", "down"];

export function TimelineView() {
  const { t } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const timelines = useAsync(() => api.timelines(pid), [pid, app.dataVersion]);
  const assets = useAsync(() => api.assets(pid, { limit: 60 }), [pid, app.dataVersion]);
  const renders = useAsync(() => api.assets(pid, { kind: "video", source: "rendered", limit: 30 }), [pid, app.dataVersion]);
  const [tlId, setTlId] = useState<string | null>(null);
  const [tl, setTl] = useState<Timeline | null>(null);
  const [song, setSong] = useState<Asset | null>(null);
  const [pxPerS, setPxPerS] = useState(46);
  const [fitted, setFitted] = useState<string | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [dropAt, setDropAt] = useState<number | null>(null);
  const [autoOpen, setAutoOpen] = useState(false);
  const [renderJob, setRenderJob] = useState<string | null>(null);

  useEffect(() => {
    const items = timelines.data?.items || [];
    if (!tlId && items[0]) setTlId(items[0].id);
  }, [timelines.data, tlId]);
  // an answer for a timeline or song that is no longer selected is dropped
  useEffect(() => {
    if (!tlId) return;
    let alive = true;
    api.timeline(tlId).then((x) => { if (alive) setTl(x); }).catch((e) => { if (alive) app.toast((e as Error).message, "bad"); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tlId, app.dataVersion]);
  useEffect(() => {
    const songId = tl?.audio_asset_id;
    if (!songId) { setSong(null); return; }
    let alive = true;
    api.asset(songId).then((a) => { if (alive) setSong(a); }).catch(() => { if (alive) setSong(null); });
    return () => { alive = false; };
  }, [tl?.audio_asset_id]);

  const [extra, setExtra] = useState<Record<string, Asset>>({});
  const byId = useMemo(() => ({ ...extra, ...Object.fromEntries((assets.data?.items || []).map((a) => [a.id, a])) }), [assets.data, extra]);
  const visual = (tl?.tracks.find((x) => x.type === "visual")?.clips || []) as Clip[];
  // clips may use assets beyond the first page of the library
  useEffect(() => {
    if (!assets.data) return;
    const missing = [...new Set(visual.map((c) => c.asset_id))].filter((id) => !byId[id]).slice(0, 80);
    if (!missing.length) return;
    Promise.all(missing.map((id) => api.asset(id).catch(() => null))).then((found) => {
      setExtra((x) => ({ ...x, ...Object.fromEntries(found.filter(Boolean).map((a) => [a!.id, a!])) }));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tl, assets.data]);
  const lyrics = (tl?.tracks.find((x) => x.type === "lyrics")?.clips || []) as LyricClip[];
  const duration = visual.reduce((s, c) => s + c.duration_s, 0);
  const analysis: Analysis | null | undefined = song?.analysis;
  const width = Math.max(600, duration * pxPerS);
  // start zoomed to fit the whole song in the track view
  useEffect(() => {
    if (!tl || fitted === tl.id || !duration) return;
    const avail = (document.querySelector(".content") as HTMLElement | null)?.clientWidth || 1100;
    setPxPerS(Math.max(12, Math.min(200, (avail - 90) / duration)));
    setFitted(tl.id);
  }, [tl, duration, fitted]);
  const latest = (renders.data?.items || []).find((r) => r.recipe?.timeline_id === tl?.id);
  const job = useTrackedJob(renderJob);

  const patch = async (body: Record<string, unknown>) => {
    if (!tl) return;
    try {
      setTl(await api.patchTimeline(tl.id, body));
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const render = async (quality: "preview" | "final") => {
    if (!tl) return;
    try {
      const r = await api.render(tl.id, quality);
      setRenderJob(r.job.id);
      app.refreshJobs();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const clip = sel !== null ? visual[sel] : null;
  const ticks: number[] = [];
  const step = pxPerS > 60 ? 1 : pxPerS > 25 ? 2 : 5;
  for (let s = 0; s <= duration; s += step) ticks.push(s);

  return (
    <>
      <div className="page-head">
        <div><h1>{t("timelineTitle")}</h1>{tl && <p>{tl.name} · {tl.aspect} · {tl.fps} fps · {fmtTime(duration)}</p>}</div>
        <div className="actions">
          {(timelines.data?.items || []).length > 1 && (
            <select value={tlId || ""} onChange={(e) => { setTlId(e.target.value); setSel(null); }}>
              {timelines.data!.items.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
          )}
          <button className="btn" onClick={() => setAutoOpen(true)}><Wand2 size={16} /> {t("autoCut")}</button>
          <button className="btn primary" onClick={() => render("preview")} disabled={!tl}><Film size={16} /> {t("renderPreview")}</button>
          <button className="btn" onClick={() => render("final")} disabled={!tl}>{t("renderFinal")}</button>
        </div>
      </div>

      {!tl ? (
        <Empty icon={<Clapperboard size={34} />} text={t("noTimelines")}>
          <button className="btn primary" onClick={() => setAutoOpen(true)}><Wand2 size={16} /> {t("autoCut")}</button>
        </Empty>
      ) : (
        <>
          <div className="tl-toolbar">
            <span className="muted small">{t("selectClip")}</span>
            <div className="grow" />
            <button className="btn sm icon" onClick={() => setPxPerS((p) => Math.max(12, p / 1.4))} aria-label={t("zoomOut")} title={t("zoomOut")}><Minus size={14} /></button>
            <span className="mono small muted">{Math.round(pxPerS)} px/s</span>
            <button className="btn sm icon" onClick={() => setPxPerS((p) => Math.min(200, p * 1.4))} aria-label={t("zoomIn")} title={t("zoomIn")}><Plus size={14} /></button>
          </div>
          <div className="tracks">
            <div className="tracks-inner" style={{ width }}>
              <div className="ruler">{ticks.map((s) => <span key={s} style={{ left: s * pxPerS }}>{fmtTime(s).replace(".0", "")}</span>)}</div>
              <div className="track-label">{t("pool")} · {visual.length}</div>
              <div className="track">
                {analysis?.sections.map((s, i) => (
                  <div key={i} className="section-band" style={{ left: s.start_s * pxPerS, width: (s.end_s - s.start_s) * pxPerS,
                    background: s.energy === "high" ? "rgba(255,77,141,.07)" : s.energy === "low" ? "rgba(122,167,255,.05)" : "transparent" }} />
                ))}
                <div className="beat-grid">
                  {analysis?.beat_times.map((b, i) => <i key={i} className={analysis.downbeats.includes(b) ? "down" : ""} style={{ left: b * pxPerS }} />)}
                </div>
                {visual.map((c, i) => {
                  const a = byId[c.asset_id];
                  const tr = c.transition_in?.type || "cut";
                  return (
                    <div key={i} draggable
                      className={`clip-block${sel === i ? " selected" : ""}${dropAt === i ? " drop-target" : ""}`}
                      style={{ left: c.start_s * pxPerS + 1, width: Math.max(4, c.duration_s * pxPerS - 2),
                        backgroundImage: a && thumbUrl(a) ? `url(${thumbUrl(a)})` : undefined }}
                      title={`${t("clip", { n: i + 1 })} · ${c.duration_s.toFixed(2)} s · ${tr}`}
                      onClick={() => setSel(i)}
                      onDragStart={(e) => {
                        // Firefox only starts a drag that carries data
                        e.dataTransfer.setData("text/prospero-clip", String(i));
                        e.dataTransfer.effectAllowed = "move";
                        setDragFrom(i);
                      }}
                      onDragEnd={() => { setDragFrom(null); setDropAt(null); }}
                      onDragOver={(e) => { e.preventDefault(); setDropAt(i); }}
                      onDragLeave={() => setDropAt(null)}
                      onDrop={(e) => {
                        e.preventDefault();
                        setDropAt(null);
                        const assetId = e.dataTransfer.getData("text/prospero-asset");
                        if (assetId) patch({ clip_updates: [{ index: i, asset_id: assetId, kind: byId[assetId]?.kind === "video" ? "video" : "image" }] });
                        else if (dragFrom !== null && dragFrom !== i) patch({ clip_updates: [{ index: dragFrom, move_to: i }] });
                        setDragFrom(null);
                      }}>
                      {tr === "flash_white" && <span className="flash" />}
                      {(tr === "crossfade" || tr === "dip_black") && <span className="xfade" />}
                      <span className="clip-label">{i + 1}</span>
                    </div>
                  );
                })}
              </div>
              <div className="track-label">{t("lyrics")} · {lyrics.length}</div>
              <div className="track lyrics">
                {lyrics.map((l, i) => (
                  <div key={i} className="lyric-block" style={{ left: l.start_s * pxPerS, width: Math.max(8, (l.end_s - l.start_s) * pxPerS - 2) }} title={l.text}>{l.text}</div>
                ))}
              </div>
              <div className="track-label">{t("song")} · {song?.name}</div>
              <div className="track audio">
                {song?.waveform && (
                  <svg width={width} height={46} style={{ display: "block" }} preserveAspectRatio="none" viewBox={`0 0 ${song.waveform.length} 46`}>
                    {song.waveform.map((v, i) => <rect key={i} x={i} y={23 - v * 20} width={0.8} height={Math.max(1, v * 40)} fill="var(--accent)" opacity={0.55} />)}
                  </svg>
                )}
              </div>
            </div>
          </div>

          <div className="tl-bottom">
            <div className="stack">
              {job && job.state !== "done" && (
                <div className="card stack">
                  <div className="row"><JobState state={job.state} /><span className="muted small">{job.message}</span>
                    <span className="mono small" style={{ marginLeft: "auto" }}>{Math.round(job.progress * 100)}%</span></div>
                  <Progress value={job.progress} />
                </div>
              )}
              <div className="card">
                <h2>{t("renderReady")} {latest && <span className="muted small" style={{ fontWeight: 400 }}>{latest.name}</span>}</h2>
                {latest ? (
                  <div className="render-row">
                    <div className="video-frame" style={{ width: tl.aspect === "9:16" ? 250 : tl.aspect === "1:1" ? 380 : 520, maxWidth: "100%" }}>
                      <video key={latest.id} src={fileUrl(latest.id)} controls preload="metadata" poster={thumbUrl(latest)} />
                    </div>
                    <dl className="kv" style={{ minWidth: 200 }}>
                      <dt>{t("duration")}</dt><dd>{latest.duration_s?.toFixed(1)} s</dd>
                      <dt>{t("size")}</dt><dd>{latest.width} x {latest.height}</dd>
                      <dt>{t("pool")}</dt><dd>{visual.length}</dd>
                      <dt>{t("lyrics")}</dt><dd>{lyrics.length}{lyrics.some((l) => l.karaoke) ? " · karaoke" : ""}</dd>
                      <dt>{t("when")}</dt><dd>{new Date(latest.created_at).toLocaleString()}</dd>
                      <dt /><dd><a className="btn sm" href={`${fileUrl(latest.id)}?download=true`}>{t("download")}</a></dd>
                    </dl>
                  </div>
                ) : <p className="muted">{t("renderPreview")}...</p>}
              </div>
            </div>
            <div className="card stack">
              {clip ? (
                <ClipEditor key={`${tl.id}-${sel}-${tl.updated_at}`} index={sel!} clip={clip} asset={byId[clip.asset_id]}
                  onApply={(fields) => patch({ clip_updates: [{ index: sel, ...fields }] })}
                  onDelete={() => { patch({ clip_updates: [{ index: sel, delete: true }] }); setSel(null); }} />
              ) : (
                <>
                  <h2><Scissors size={16} /> {tl.name}</h2>
                  <label className="check"><input type="checkbox" checked={lyrics.some((l) => l.karaoke)} disabled={!lyrics.length}
                    onChange={(e) => patch({ karaoke: e.target.checked })} /> {t("karaoke")}</label>
                  <p className="muted small">{t("selectClip")}</p>
                </>
              )}
            </div>
          </div>
        </>
      )}
      {autoOpen && <AutoCutDialog onClose={() => setAutoOpen(false)} onBuilt={(x) => { setAutoOpen(false); setTlId(x.id); setTl(x); app.bump(); }} />}
    </>
  );
}

function ClipEditor({ index, clip, asset, onApply, onDelete }: {
  index: number; clip: Clip; asset?: Asset; onApply: (f: Record<string, unknown>) => void; onDelete: () => void;
}) {
  const { t } = useT();
  const app = useApp();
  const [duration, setDuration] = useState(clip.duration_s);
  const [transition, setTransition] = useState(clip.transition_in?.type || "cut");
  const [tDur, setTDur] = useState(clip.transition_in?.duration_s || 0.3);
  const [zs, setZs] = useState(clip.ken_burns?.zoom_start ?? 1);
  const [ze, setZe] = useState(clip.ken_burns?.zoom_end ?? 1.1);
  const [pan, setPan] = useState(clip.ken_burns?.pan || "none");
  return (
    <>
      <h2>{t("clip", { n: index + 1 })} <span className="muted small mono" style={{ fontWeight: 400 }}>{fmtTime(clip.start_s)}</span></h2>
      {asset && thumbUrl(asset) && <img src={thumbUrl(asset)} alt="" style={{ width: "100%", maxHeight: 180, objectFit: "cover", borderRadius: 10, cursor: "zoom-in" }}
        onClick={() => app.openAsset(asset.id)} />}
      <label className="field">{t("duration")} <span className="mono">{duration.toFixed(2)} s</span>
        <input type="range" min={0.5} max={8} step={0.05} value={duration} onChange={(e) => setDuration(Number(e.target.value))} /></label>
      <div className="grid-2">
        <label className="field">{t("transition")}
          <select value={transition} onChange={(e) => setTransition(e.target.value)}>{TRANSITIONS.map((x) => <option key={x}>{x}</option>)}</select></label>
        <label className="field">{t("duration")}
          <input type="number" min={0.05} max={2} step={0.05} value={tDur} disabled={transition === "cut"} onChange={(e) => setTDur(Number(e.target.value))} /></label>
      </div>
      {clip.kind === "image" && (
        <div className="grid-3">
          <label className="field">{t("zoomStart")}<input type="number" min={1} max={2} step={0.02} value={zs} onChange={(e) => setZs(Number(e.target.value))} /></label>
          <label className="field">{t("zoomEnd")}<input type="number" min={1} max={2} step={0.02} value={ze} onChange={(e) => setZe(Number(e.target.value))} /></label>
          <label className="field">{t("pan")}<select value={pan} onChange={(e) => setPan(e.target.value)}>{PANS.map((x) => <option key={x}>{x}</option>)}</select></label>
        </div>
      )}
      <div className="row">
        <button className="btn primary" onClick={() => onApply({
          duration_s: Math.round(duration * 1000) / 1000,
          transition_in: { type: transition, duration_s: transition === "cut" ? 0 : tDur },
          ...(clip.kind === "image" ? { ken_burns: { zoom_start: zs, zoom_end: ze, pan } } : {}),
        })}>{t("save")}</button>
        <button className="btn danger" onClick={onDelete}>{t("delete")}</button>
      </div>
    </>
  );
}

function AutoCutDialog({ onClose, onBuilt }: { onClose: () => void; onBuilt: (tl: Timeline) => void }) {
  const { t } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const songs = useAsync(() => api.assets(pid, { kind: "audio", limit: 60 }), [pid]);
  const lyrics = useAsync(() => api.assets(pid, { kind: "lyrics", limit: 60 }), [pid]);
  const boards = useAsync(() => api.boards(pid), [pid]);
  const [song, setSong] = useState("");
  const [board, setBoard] = useState("");
  const [lyr, setLyr] = useState("");
  const [aspect, setAspect] = useState("9:16");
  const [karaoke, setKaraoke] = useState(true);
  const [flash, setFlash] = useState(true);
  const [kb, setKb] = useState(true);
  const [low, setLow] = useState(4);
  const [high, setHigh] = useState(2);
  const [busy, setBusy] = useState(false);
  const songList = (songs.data?.items || []).filter((s) => !(s.tags || []).includes("voice"));
  useEffect(() => { if (!song && songList[0]) setSong(songList[0].id); }, [songList, song]);

  const build = async () => {
    setBusy(true);
    try {
      const tl = await api.autoCut(pid, {
        song_asset_id: song, board_id: board || null, lyrics_asset_id: lyr || null, aspect,
        options: { karaoke, flash_on_strong_downbeats: flash, ken_burns_variety: kb, beats_low: low, beats_mid: low, beats_high: high },
      });
      onBuilt(tl);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={t("autoCut")} onClose={onClose}
      footer={<><button className="btn ghost" onClick={onClose}>{t("cancel")}</button>
        <button className="btn primary" onClick={build} disabled={!song || busy}>{busy ? <Loader2 size={15} className="spin" /> : <Wand2 size={15} />} {t("build")}</button></>}>
      <div className="stack">
        <label className="field">{t("song")}
          <select value={song} onChange={(e) => setSong(e.target.value)}>{songList.map((s) => <option key={s.id} value={s.id}>{s.name || s.id}</option>)}</select></label>
        <div className="grid-2">
          <label className="field">{t("pool")}
            <select value={board} onChange={(e) => setBoard(e.target.value)}>
              <option value="">{t("poolDefault")}</option>
              {(boards.data?.items || []).map((b) => <option key={b.id} value={b.id}>{t("board")}: {b.name} ({b.items.length})</option>)}
            </select></label>
          <label className="field">{t("lyrics")}
            <select value={lyr} onChange={(e) => setLyr(e.target.value)}>
              <option value="">{t("noLyrics")}</option>
              {(lyrics.data?.items || []).map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select></label>
        </div>
        <div className="field">{t("aspect")}
          <div className="segmented">{["9:16", "16:9", "1:1"].map((a) => <button key={a} className={aspect === a ? "on" : ""} onClick={() => setAspect(a)}>{a}</button>)}</div>
        </div>
        <div className="grid-2">
          <label className="field">{t("beatsLow")} <span className="mono">{low}</span>
            <input type="range" min={1} max={16} value={low} onChange={(e) => setLow(Number(e.target.value))} /></label>
          <label className="field">{t("beatsHigh")} <span className="mono">{high}</span>
            <input type="range" min={1} max={16} value={high} onChange={(e) => setHigh(Number(e.target.value))} /></label>
        </div>
        <div className="row wrap">
          <label className="check"><input type="checkbox" checked={flash} onChange={(e) => setFlash(e.target.checked)} /> {t("flashDownbeats")}</label>
          <label className="check"><input type="checkbox" checked={kb} onChange={(e) => setKb(e.target.checked)} /> {t("kenBurns")}</label>
          <label className="check"><input type="checkbox" checked={karaoke} onChange={(e) => setKaraoke(e.target.checked)} /> {t("karaoke")}</label>
        </div>
      </div>
    </Modal>
  );
}
