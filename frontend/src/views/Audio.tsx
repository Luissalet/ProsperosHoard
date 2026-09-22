import { useCallback, useEffect, useRef, useState } from "react";
import { AudioLines, Download, Loader2, Mic, Pause, Play, RefreshCw, Save, Sparkles, Square, Timer } from "lucide-react";
import { api, fileUrl, type Analysis, type Asset } from "../api";
import { useT } from "../i18n";
import { Empty, fmtTime, useApp, useAsync } from "../components/ui";

const ENERGY_COLOURS: Record<string, string> = { low: "rgba(122,167,255,0.10)", mid: "rgba(245,194,107,0.10)", high: "rgba(255,77,141,0.16)" };

function Waveform({ peaks, analysis, duration, time, onSeek }: {
  peaks: number[]; analysis: Analysis | null | undefined; duration: number; time: number; onSeek: (t: number) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext("2d")!;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    const styles = getComputedStyle(document.documentElement);
    const accent = styles.getPropertyValue("--accent").trim() || "#ff4d8d";
    const gold = styles.getPropertyValue("--gold").trim() || "#f5c26b";
    const muted = styles.getPropertyValue("--muted").trim() || "#888";
    if (analysis && duration) {
      for (const s of analysis.sections) {
        ctx.fillStyle = ENERGY_COLOURS[s.energy] || "transparent";
        ctx.fillRect((s.start_s / duration) * w, 0, ((s.end_s - s.start_s) / duration) * w, h);
        ctx.fillStyle = muted;
        ctx.font = "11px 'Space Grotesk Variable', sans-serif";
        ctx.fillText(`${s.label} · ${s.energy}`, (s.start_s / duration) * w + 6, 14);
      }
    }
    const mid = h / 2 + 6;
    const n = peaks.length;
    const played = duration ? time / duration : 0;
    for (let x = 0; x < w; x += 2) {
      const v = n ? peaks[Math.min(n - 1, Math.floor((x / w) * n))] : 0;
      const bar = Math.max(1, v * (h / 2 - 16));
      ctx.fillStyle = x / w <= played ? accent : `${accent}88`;
      ctx.fillRect(x, mid - bar, 1.4, bar * 2);
    }
    if (analysis && duration) {
      const downs = new Set(analysis.downbeats.map((d) => d.toFixed(3)));
      for (const b of analysis.beat_times) {
        const x = (b / duration) * w;
        const isDown = downs.has(b.toFixed(3));
        ctx.fillStyle = isDown ? gold : `${muted}99`;
        ctx.fillRect(x, h - (isDown ? 14 : 7), isDown ? 1.5 : 1, isDown ? 14 : 7);
      }
    }
    if (duration) {
      ctx.fillStyle = "#fff";
      ctx.fillRect((time / duration) * w, 0, 1.5, h);
    }
  }, [peaks, analysis, duration, time]);
  return (
    <div className="wave-wrap" onClick={(e) => {
      const r = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
      onSeek(((e.clientX - r.left) / r.width) * duration);
    }}>
      <canvas ref={ref} />
    </div>
  );
}

export function AudioView() {
  const { t } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const songs = useAsync(() => api.assets(pid, { kind: "audio", limit: 60 }), [pid, app.dataVersion]);
  const lyricsList = useAsync(() => api.assets(pid, { kind: "lyrics", limit: 60 }), [pid, app.dataVersion]);
  const chars = useAsync(() => api.characters(pid), [pid]);
  const backend = useAsync(() => api.backend(), []);
  const [songId, setSongId] = useState<string | null>(null);
  const [song, setSong] = useState<Asset | null>(null);
  const [analysing, setAnalysing] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const audio = useRef<HTMLAudioElement>(null);

  const [lyricsId, setLyricsId] = useState<string>("");
  const [lines, setLines] = useState<{ time_s: number | null; text: string }[]>([]);
  const [rawText, setRawText] = useState("");
  const [timing, setTiming] = useState<number | null>(null);

  const [voiceText, setVoiceText] = useState("");
  const [voiceChar, setVoiceChar] = useState("");
  const [speaking, setSpeaking] = useState(false);

  const allAudio = songs.data?.items || [];
  const songList = allAudio.filter((a) => !(a.tags || []).includes("voice"));
  const voiceList = allAudio.filter((a) => (a.tags || []).includes("voice"));

  useEffect(() => { if (!songId && songList[0]) setSongId(songList[0].id); }, [songList, songId]);
  useEffect(() => {
    if (!songId) return;
    api.asset(songId).then(setSong);
    setPlaying(false);
    setTime(0);
  }, [songId]);

  useEffect(() => {
    if (!lyricsId) { setLines([]); setRawText(""); return; }
    api.lyrics(lyricsId).then((l) => {
      setRawText(l.text);
      const timed = l.all_lines || l.lines;
      setLines(timed.length ? timed : l.text.split("\n").filter((x) => x.trim()).map((text) => ({ time_s: null, text })));
    });
  }, [lyricsId]);
  useEffect(() => { if (!lyricsId && lyricsList.data?.items[0]) setLyricsId(lyricsList.data.items[0].id); }, [lyricsList.data, lyricsId]);

  const analyse = async (force: boolean) => {
    if (!song) return;
    setAnalysing(true);
    try {
      await api.analyze(song.id, force);
      setSong(await api.asset(song.id));
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setAnalysing(false);
    }
  };

  const toggle = () => {
    const el = audio.current;
    if (!el) return;
    if (el.paused) el.play(); else el.pause();
  };

  const stamp = useCallback(() => {
    const el = audio.current;
    if (timing === null || !el) return;
    setLines((ls) => ls.map((l, i) => (i === timing ? { ...l, time_s: Math.round(el.currentTime * 100) / 100 } : l)));
    if (timing + 1 >= lines.length) { setTiming(null); el.pause(); } else setTiming(timing + 1);
  }, [timing, lines.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (timing === null) return;
      if (e.code === "Space") { e.preventDefault(); stamp(); }
      if (e.key === "Escape") setTiming(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [timing, stamp]);

  const startTiming = () => {
    const src = rawText.split("\n").map((x) => x.replace(/^\[[\d:.]+\]/, "").trim()).filter(Boolean);
    setLines(src.map((text) => ({ time_s: null, text })));
    setTiming(0);
    const el = audio.current;
    if (el) { el.currentTime = 0; el.play(); }
  };

  const lrc = lines.filter((l) => l.time_s !== null).map((l) => {
    const m = Math.floor(l.time_s! / 60);
    const s = (l.time_s! - m * 60).toFixed(2).padStart(5, "0");
    return `[${String(m).padStart(2, "0")}:${s}]${l.text}`;
  }).join("\n");

  const [autoTiming, setAutoTiming] = useState(false);
  const autoTime = async () => {
    if (!song) return;
    setAutoTiming(true);
    try {
      const text = rawText.split("\n").map((x) => x.replace(/^(\[[\d:.]+\])+/, "").trim()).filter(Boolean).join("\n");
      const r = await api.timeLyrics(pid, song.id, text, `${song.name || "Song"} - timed lyrics`);
      setLyricsId(r.id);
      app.toast(t("autoTimed", { n: r.lines, s: r.sections.length }), "ok");
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setAutoTiming(false);
    }
  };

  const saveLyrics = async () => {
    try {
      const text = lrc || rawText;
      if (lyricsId) await api.saveLyrics(lyricsId, text);
      else {
        const a = await api.createLyrics(pid, text, `${song?.name || "Song"} lyrics`);
        setLyricsId(a.id);
      }
      app.toast(t("saved"), "ok");
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const speak = async () => {
    setSpeaking(true);
    try {
      const a = await api.voice(pid, voiceText, voiceChar || undefined);
      new Audio(fileUrl(a.id)).play().catch(() => undefined);
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setSpeaking(false);
    }
  };

  const analysis = song?.analysis;
  const duration = analysis?.duration_s || song?.duration_s || 0;
  const currentLine = lines.reduce((acc, l, i) => (l.time_s !== null && l.time_s <= time ? i : acc), -1);

  return (
    <>
      <div className="page-head">
        <div><h1>{t("audioTitle")}</h1></div>
        <div className="actions">
          {backend.data && !backend.data.music.some((m) => m.available) && (
            <span className="pill" title={backend.data.music.map((m) => m.reason).join("\n")}>{t("musicNotInstalled")}</span>
          )}
        </div>
      </div>
      {songList.length === 0 ? <Empty icon={<AudioLines size={34} />} text={t("noSongs")}>
        <button className="btn primary" onClick={() => app.go("library")}>{t("upload")}</button></Empty> : (
        <div className="stack">
          <div className="card stack">
            <div className="row wrap">
              <select value={songId || ""} onChange={(e) => setSongId(e.target.value)} style={{ minWidth: 280 }}>
                {songList.map((s) => <option key={s.id} value={s.id}>{s.name || s.id}</option>)}
              </select>
              <button className="btn primary icon" onClick={toggle} title={playing ? t("pause") : t("play")}>{playing ? <Pause size={16} /> : <Play size={16} />}</button>
              <span className="mono muted">{fmtTime(time)} / {fmtTime(duration)}</span>
              <div className="grow" />
              {analysis && <><span className="bpm-readout">{analysis.tempo_bpm ?? "-"}</span><span className="muted">{t("bpm")} · {analysis.beat_times.length} {t("beats")}</span></>}
              <button className="btn sm" onClick={() => analyse(!!analysis)} disabled={analysing}>
                {analysing ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} {analysis ? t("reanalyse") : t("analyse")}
              </button>
            </div>
            {song && <Waveform peaks={song.waveform || []} analysis={analysis} duration={duration} time={time}
              onSeek={(s) => { if (audio.current) audio.current.currentTime = s; }} />}
            {song && <audio ref={audio} src={fileUrl(song.id)} preload="auto" onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
              onTimeUpdate={(e) => setTime((e.target as HTMLAudioElement).currentTime)} />}
            {analysis && (
              <div className="section-legend">
                {analysis.sections.map((s, i) => (
                  <span key={i} className={`pill ${s.energy === "high" ? "accent" : s.energy === "low" ? "info" : "gold"}`}>
                    {s.label} · {fmtTime(s.start_s)}-{fmtTime(s.end_s)} · {t("energy")} {s.energy}
                  </span>
                ))}
                {analysis.notes && <span className="muted small">{analysis.notes}</span>}
              </div>
            )}
          </div>

          <div className="grid-2" style={{ alignItems: "start" }}>
            <div className="card stack">
              <h2><Timer size={16} /> {t("lyricsTitle")}</h2>
              <p className="muted small" style={{ margin: 0 }}>{t("lyricsLead")}</p>
              <div className="row">
                <select value={lyricsId} onChange={(e) => setLyricsId(e.target.value)} className="grow">
                  <option value="">{t("newLyrics")}</option>
                  {(lyricsList.data?.items || []).map((l) => <option key={l.id} value={l.id}>{l.name || l.id}</option>)}
                </select>
              </div>
              {timing === null ? (
                <textarea value={lines.length && lines.some((l) => l.time_s !== null) ? lrc || rawText : rawText} rows={7} className="mono"
                  onChange={(e) => { setRawText(e.target.value); setLines(e.target.value.split("\n").filter((x) => x.trim()).map((text) => ({ time_s: null, text }))); }} />
              ) : (
                <div className="stack">
                  <span className="pill accent">{t("lineN", { n: timing + 1, total: lines.length })}</span>
                  <button className="btn primary lg" onClick={stamp}>{t("stamp")} <span className="kbd" style={{ color: "#fff" }}>Space</span></button>
                </div>
              )}
              <div className="lyric-lines">
                {lines.map((l, i) => (
                  <div key={i} className={`lyric-line${i === (timing ?? currentLine) ? " current" : ""}`}>
                    <span className="t">{l.time_s !== null ? fmtTime(l.time_s) : "--"}</span><span>{l.text}</span>
                  </div>
                ))}
              </div>
              <div className="row wrap">
                {timing === null
                  ? <button className="btn" onClick={startTiming} disabled={!rawText.trim() || !song}><Play size={14} /> {t("start")}</button>
                  : <button className="btn" onClick={() => { setTiming(null); audio.current?.pause(); }}><Square size={14} /> {t("stop")}</button>}
                {timing === null && <button className="btn" onClick={autoTime} disabled={!rawText.trim() || !song || autoTiming}
                  title={t("autoTimeHint")}>{autoTiming ? <Loader2 size={14} className="spin" /> : <Sparkles size={14} />} {t("autoTime")}</button>}
                <button className="btn primary" onClick={saveLyrics} disabled={!rawText.trim() && !lrc}><Save size={14} /> {t("saveLrc")}</button>
                {lrc && <a className="btn ghost" download="lyrics.lrc" href={`data:text/plain;charset=utf-8,${encodeURIComponent(lrc)}`}><Download size={14} /> {t("exportLrc")}</a>}
              </div>
            </div>

            <div className="card stack">
              <h2><Mic size={16} /> {t("voiceLines")}</h2>
              <textarea value={voiceText} onChange={(e) => setVoiceText(e.target.value)} rows={3} placeholder={t("voiceTestLine")} />
              <div className="row">
                <select value={voiceChar} onChange={(e) => setVoiceChar(e.target.value)} className="grow" aria-label={t("speakAs")}>
                  <option value="">{t("speakAs")}: {t("none")}</option>
                  {(chars.data?.items || []).map((c) => <option key={c.id} value={c.id}>{c.name} · {c.voice?.voice_id}</option>)}
                </select>
                <button className="btn primary" onClick={speak} disabled={speaking || !voiceText.trim()}>
                  {speaking ? <Loader2 size={14} className="spin" /> : <Mic size={14} />} {t("speak")}
                </button>
              </div>
              <p className="muted small" style={{ margin: 0 }}>{t("noVoiceCloning")}</p>
              <div className="stack" style={{ gap: 6 }}>
                {voiceList.map((v) => (
                  <div key={v.id} className="row" style={{ background: "var(--surface-2)", borderRadius: 8, padding: "6px 10px" }}>
                    <span className="grow ellipsis small">{v.name}</span>
                    <audio src={fileUrl(v.id)} controls preload="none" style={{ height: 30, width: 220 }} />
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
