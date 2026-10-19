import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Icon, Spinner } from "../components/ui.jsx";
import { useEd } from "./EditorContext.js";
import { ClipLayer, TextLayer } from "./layers.jsx";
import Captions from "./Captions.jsx";
import GLStage from "./GLStage.jsx";
import { drawable } from "./gl/scene.js";
import { SourceRegistry } from "./gl/sources.js";
import { usePlaybackState, useTime } from "./playback.js";
import { setGlPreview, useGlPreview, webglAvailable } from "./previewPrefs.js";
import { clipDur, clipEnd, fmtFrames } from "./time.js";

const PRELOAD = 2000;
const KEEP = 250;
const SCRUB_GAP = 220;

// Which clips need a live element right now: the ones under the playhead plus the next ones (preloaded).
function useActiveClips(doc, pb) {
  const [active, setActive] = useState([]);
  const sig = useRef("");
  const docRef = useRef(doc);
  docRef.current = doc;
  useEffect(() => {
    let last = 0;
    let timer = 0;
    const build = (t) => {
      const list = [];
      docRef.current.tracks.forEach((tr, ti) => {
        const sorted = tr.clips;
        for (let i = 0; i < sorted.length; i++) {
          const c = sorted[i];
          const s = c.start;
          const e = clipEnd(c);
          if (t >= s - PRELOAD * Math.max(1, pb.rate) && t < e + KEEP) list.push({ track: tr, ti, clip: c, next: sorted[i + 1] || null });
        }
      });
      return list;
    };
    const commit = (t, force) => {
      timer = 0;
      const list = build(t);
      const signature = list.map((x) => x.clip.id).join(",");
      if (force || signature !== sig.current) {
        sig.current = signature;
        last = performance.now();
        setActive(list);
      }
    };
    // While scrubbing (paused) a fast drag crosses many clips: mounting a media element for each would queue decoder work
    // that nobody sees, so changes are applied at most every SCRUB_GAP ms (leading + trailing). Playback applies them at once.
    const onTick = (t, playing) => {
      if (playing) { clearTimeout(timer); timer = 0; commit(t, false); return; }
      const wait = SCRUB_GAP - (performance.now() - last);
      if (wait <= 0) { clearTimeout(timer); commit(t, false); return; }
      if (!timer) timer = setTimeout(() => commit(pb.t, false), wait);
    };
    commit(pb.t, true);
    const unsub = pb.subscribe(onTick);
    return () => { unsub(); clearTimeout(timer); };
  }, [doc, pb]);
  return active;
}

function useBoxSize(ref, aspect) {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => {
      const w = el.clientWidth - 20;
      const h = el.clientHeight - 20;
      if (w <= 0 || h <= 0) return;
      let bw = w;
      let bh = bw / aspect;
      if (bh > h) { bh = h; bw = bh * aspect; }
      setSize((s) => (Math.abs(s.w - bw) < 0.5 && Math.abs(s.h - bh) < 0.5 ? s : { w: Math.floor(bw), h: Math.floor(bh) }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, aspect]);
  return size;
}

function ExactFrame({ projectId, rev, pb, width }) {
  const [t, setT] = useState(Math.round(pb.t));
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let timer = 0;
    const unsub = pb.subscribe((time) => {
      clearTimeout(timer);
      timer = setTimeout(() => setT(Math.round(time)), 220);
    });
    return () => { clearTimeout(timer); unsub(); };
  }, [pb]);
  const src = api.frameUrl(projectId, t, width, rev);
  useEffect(() => { setLoaded(false); setFailed(false); }, [src]);
  return (
    <>
      <img src={src} alt="" onLoad={() => setLoaded(true)} onError={() => setFailed(true)} style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "contain", zIndex: 9500, opacity: loaded ? 1 : 0, background: "#000" }} />
      {!loaded ? <div style={{ position: "absolute", right: 8, top: 8, zIndex: 9600 }}>{failed ? <span className="chip chip-err">!</span> : <Spinner />}</div> : null}
    </>
  );
}

export default function Player() {
  const { t } = useApp();
  const ed = useEd();
  const { doc, view, pb } = ed;
  const media = view.media;
  const stage = useRef(null);
  const aspect = doc.canvas.width / doc.canvas.height;
  const box = useBoxSize(stage, aspect);
  const k = box.w ? box.w / doc.canvas.width : 1;
  const active = useActiveClips(doc, pb);
  const ps = usePlaybackState(pb);
  const time = useTime(pb, 50);
  const [exact, setExact] = useState(false);
  const total = ed.duration;
  // Accelerated preview (WebGL): on when the person wants it and the browser can; any failure drops back to the CSS preview.
  const glWanted = useGlPreview();
  const [glFailed, setGlFailed] = useState(false);
  const glOk = webglAvailable();
  const useGL = glWanted && glOk && !glFailed;
  const registry = useMemo(() => new SourceRegistry(), []);
  const onGlFail = useCallback((error) => {
    console.warn("WebGL preview unavailable, using the CSS preview:", error);
    setGlFailed(true);
  }, []);

  // rank within the stack: track layer first, then start time
  const zOf = useMemo(() => {
    const map = new Map();
    active.forEach((a) => map.set(a.clip.id, a.ti * 1000 + Math.min(900, Math.round(a.clip.start / 1000) % 900)));
    return map;
  }, [active]);
  const ordered = useMemo(() => [...active].sort((a, b) => (a.clip.id < b.clip.id ? -1 : 1)), [active]);

  const boxStyle = { width: box.w || 0, height: box.h || 0, background: doc.canvas.background || "#000" };
  const showExact = exact && !ps.playing;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const frameWidth = Math.max(320, Math.min(1920, Math.round((box.w * dpr) / 10) * 10));

  return (
    <div className="ed-center">
      <div className="stage" ref={stage} onClick={(e) => { if (e.target === stage.current) pb.toggle(); }}>
        {box.w ? (
          <div className="stage-box" style={boxStyle} onClick={() => pb.toggle()} role="img" aria-label="Preview">
            {useGL ? <GLStage doc={doc} media={media} box={box} pb={pb} registry={registry} onFail={onGlFail} /> : null}
            {ordered.map(({ clip, track, next }) => (clip.type === "text"
              ? <TextLayer key={clip.id} clip={clip} track={track} box={box} k={k} z={useGL ? 8000 + (zOf.get(clip.id) || 0) : zOf.get(clip.id)} pb={pb} />
              : (
                // a nested sequence whose intermediate is not rendered yet keeps the DOM poster; everything else is a GL source
                <ClipLayer key={clip.id} clip={clip} track={track} next={next} media={media[clip.media]} canvas={doc.canvas} box={box} k={k} z={zOf.get(clip.id)} pb={pb} projectMuted={false}
                  gl={useGL && drawable(media[clip.media])} onEl={useGL && drawable(media[clip.media]) ? registry.set : undefined} />
              )))}
            <Captions doc={doc} words={ed.transcript?.words} box={box} k={k} pb={pb} />
            {showExact ? <ExactFrame projectId={ed.projectId} rev={view.rev} pb={pb} width={frameWidth} /> : null}
            {total === 0 ? (
              <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--dim)", textAlign: "center", padding: 20, zIndex: 9700 }}>
                {t("player_empty")}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className="player-bar">
        <button type="button" className="btn btn-ghost btn-icon btn-sm" title={`${t("prev_frame")} (←)`} onClick={() => pb.step(-1)}><Icon name="stepback" /></button>
        <button type="button" className="btn btn-primary btn-icon btn-sm" title={`${ps.playing ? t("pause") : t("play")} (Space)`} aria-label={ps.playing ? t("pause") : t("play")} onClick={() => pb.toggle()}>
          <Icon name={ps.playing ? "pause" : "play"} />
        </button>
        <button type="button" className="btn btn-ghost btn-icon btn-sm" title={`${t("next_frame")} (→)`} onClick={() => pb.step(1)}><Icon name="stepfwd" /></button>
        <span className="tc num" data-testid="timecode" style={{ marginLeft: 8 }}>{fmtFrames(time, doc.canvas.fps)} <span className="dim">/ {fmtFrames(total, doc.canvas.fps)}</span></span>
        {ps.rate !== 1 ? <span className="chip chip-info">×{ps.rate}</span> : null}
        <div style={{ flex: 1 }} />
        <button
          type="button"
          className={`btn btn-sm ${useGL ? "btn-on" : "btn-ghost"}`}
          aria-pressed={useGL}
          disabled={!glOk}
          data-testid="gl-toggle"
          onClick={() => { setGlFailed(false); setGlPreview(!glWanted); }}
          title={glOk ? t("gl_preview_help") : t("gl_preview_unavailable")}
        >
          <Icon name="effects" size={14} />WebGL
        </button>
        <button type="button" className={`btn btn-sm ${exact ? "btn-on" : "btn-ghost"}`} aria-pressed={exact} onClick={() => setExact((v) => !v)} title={t("exact_frame_help")}>
          <Icon name="image" size={14} />{t("exact_frame")}
        </button>
        <button type="button" className="btn btn-ghost btn-icon btn-sm" title={t("mute")} aria-label={t("mute")} aria-pressed={ps.muted} onClick={() => pb.setMuted(!ps.muted)}>
          <Icon name={ps.muted ? "mute" : "volume"} />
        </button>
      </div>
    </div>
  );
}
