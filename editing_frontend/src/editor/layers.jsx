import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { filtersWithEqKeys } from "./eqKeys.js";
import { frameSrcMs, frameTime } from "./gl/scene.js";
import { clamp, clipDur, kfValue, speedAt, srcAt } from "./time.js";

// ---------- geometry: where the picture sits inside the preview box (mirrors the render's fit / crop / focus logic) ----------

export function focusAt(clip, srcMs) {
  const tr = clip.transform;
  const path = clip.reframe?.path;
  if (!path || !path.length) return [tr.focus_x, tr.focus_y];
  if (srcMs <= path[0][0]) return [path[0][1], path[0][2] ?? tr.focus_y];
  const last = path[path.length - 1];
  if (srcMs >= last[0]) return [last[1], last[2] ?? tr.focus_y];
  let lo = 0;
  let hi = path.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (path[mid][0] <= srcMs) lo = mid; else hi = mid;
  }
  const a = path[lo];
  const b = path[hi];
  const p = (srcMs - a[0]) / Math.max(1, b[0] - a[0]);
  return [a[1] + (b[1] - a[1]) * p, (a[2] ?? tr.focus_y) + ((b[2] ?? tr.focus_y) - (a[2] ?? tr.focus_y)) * p];
}

export function placement(clip, media, boxW, boxH, k) {
  const cr = clip.crop || {};
  const l = cr.left || 0;
  const r = cr.right || 0;
  const tp = cr.top || 0;
  const b = cr.bottom || 0;
  const mw = media?.width || boxW / k;
  const mh = media?.height || boxH / k;
  const cw = Math.max(2, mw * (1 - l - r));
  const ch = Math.max(2, mh * (1 - tp - b));
  const fit = clip.transform.fit;
  const geo = { l, tp, cover: null };
  if (fit === "fill") {
    Object.assign(geo, { x: 0, y: 0, w: boxW, h: boxH });
  } else if (fit === "none") {
    const w = cw * k;
    const h = ch * k;
    Object.assign(geo, { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h });
  } else if (fit === "contain" || fit === "blur") {
    geo.blur = fit === "blur";
    const ratio = cw / ch;
    let w = boxW;
    let h = w / ratio;
    if (h > boxH) { h = boxH; w = h * ratio; }
    Object.assign(geo, { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h });
  } else {
    const ratio = cw / ch;
    let sw = boxW;
    let sh = sw / ratio;
    if (sh < boxH) { sh = boxH; sw = sh * ratio; }
    Object.assign(geo, { x: 0, y: 0, w: boxW, h: boxH, cover: { sw, sh } });
  }
  const sw = geo.cover ? geo.cover.sw : geo.w;
  const sh = geo.cover ? geo.cover.sh : geo.h;
  geo.fullW = sw / Math.max(0.05, 1 - l - r);
  geo.fullH = sh / Math.max(0.05, 1 - tp - b);
  return geo;
}

const FILTER_CSS = {
  grayscale: () => "grayscale(1)",
  sepia: () => "sepia(1)",
  vintage: () => "sepia(0.45) contrast(1.1) saturate(0.8)",
  warm: (p) => `sepia(${0.25 * (p.amount ?? 0.5)}) saturate(${1 + 0.4 * (p.amount ?? 0.5)}) hue-rotate(-8deg)`,
  cool: (p) => `saturate(${1 + 0.1 * (p.amount ?? 0.5)}) hue-rotate(${12 * (p.amount ?? 0.5)}deg)`,
  contrast_pop: (p) => `contrast(${1 + 0.35 * (p.amount ?? 0.5)}) saturate(${1 + 0.4 * (p.amount ?? 0.5)})`,
  blur: (p, k) => `blur(${(p.radius ?? 6) * k * 0.5}px)`,
  eq: (p) => `brightness(${1 + (p.brightness ?? 0)}) contrast(${p.contrast ?? 1}) saturate(${p.saturation ?? 1})`,
};
export function cssFilter(filters, k) {
  const parts = [];
  for (const f of filters || []) {
    if (f.enabled === false) continue;
    const fn = FILTER_CSS[f.type];
    if (fn) parts.push(fn(f.params || {}, k));
  }
  return parts.join(" ") || "none";
}

// Transition look for the incoming clip at progress p (0..1) and for the outgoing one.
function transitionIn(type, p) {
  const pct = (1 - p) * 100;
  switch (type) {
    case "fade_black":
    case "fade_white": return { opacity: Math.max(0, 2 * p - 1) };
    case "slide_left": return { transform: `translateX(${pct}%)` };
    case "slide_right": return { transform: `translateX(${-pct}%)` };
    case "slide_up": return { transform: `translateY(${pct}%)` };
    case "slide_down": return { transform: `translateY(${-pct}%)` };
    case "smooth_left": return { transform: `translateX(${pct}%)`, opacity: p };
    case "smooth_right": return { transform: `translateX(${-pct}%)`, opacity: p };
    case "wipe_left": return { clip: `inset(0 0 0 ${pct}%)` };
    case "wipe_right": return { clip: `inset(0 ${pct}% 0 0)` };
    case "wipe_up": return { clip: `inset(${pct}% 0 0 0)` };
    case "wipe_down": return { clip: `inset(0 0 ${pct}% 0)` };
    case "circle_open": return { clip: `circle(${p * 75}% at 50% 50%)` };
    case "circle_close": return { clip: `circle(${p * 75}% at 50% 50%)` };
    case "radial": return { clip: `circle(${p * 75}% at 50% 50%)`, opacity: Math.min(1, p * 2) };
    case "zoom_in": return { transform: `scale(${0.6 + 0.4 * p})`, opacity: p };
    case "blur": return { opacity: p, filter: `blur(${(1 - p) * 8}px)` };
    case "pixelize": return { opacity: p };
    default: return { opacity: p };
  }
}
function transitionOut(type, p) {
  if (type === "fade_black" || type === "fade_white") return { opacity: Math.max(0, 1 - 2 * p) };
  return {};
}

export const dbToGain = (db) => clamp(Math.pow(10, db / 20), 0, 1);

// Shape mask as a CSS mask image (an SVG drawn at the picture's size), animated with the mask keyframes. The render
// uses a signed-distance edge; a Gaussian blur of a third of the feather width looks the same at preview size.
export function maskCss(clip, local, w, h) {
  const m = clip.mask;
  if (!m || m.enabled === false || !w || !h) return "none";
  const kf = clip.keyframes || {};
  const cx = kfValue(kf.mask_x, local, m.x) * w;
  const cy = kfValue(kf.mask_y, local, m.y) * h;
  const mw = Math.max(1, kfValue(kf.mask_w, local, m.w) * w);
  const mh = Math.max(1, kfValue(kf.mask_h, local, m.h) * h);
  const fe = Math.max(0, kfValue(kf.mask_feather, local, m.feather) * Math.min(w, h));
  const r = m.shape === "rounded" ? (m.radius ?? 0.2) * Math.min(mw, mh) : 0;
  const filt = fe > 0.5 ? ' filter="url(#f)"' : "";
  const fill = m.invert ? "black" : "white";
  const shape = m.shape === "ellipse"
    ? `<ellipse cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" rx="${(mw / 2).toFixed(1)}" ry="${(mh / 2).toFixed(1)}" fill="${fill}"${filt}/>`
    : `<rect x="${(cx - mw / 2).toFixed(1)}" y="${(cy - mh / 2).toFixed(1)}" width="${mw.toFixed(1)}" height="${mh.toFixed(1)}" rx="${r.toFixed(1)}" fill="${fill}"${filt}/>`;
  const defs = `<filter id="f" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="${(fe / 3).toFixed(2)}"/></filter>`;
  const body = m.invert
    ? `<defs>${defs}<mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="${w}" height="${h}"><rect width="${w}" height="${h}" fill="white"/>${shape}</mask></defs><rect width="${w}" height="${h}" fill="white" mask="url(#m)"/>`
    : `<defs>${defs}</defs>${shape}`;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${body}</svg>`;
  return `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
}

function setMask(el, css) {
  if (!el || el.dataset.mask === css) return;
  el.dataset.mask = css;
  el.style.maskImage = css;
  el.style.webkitMaskImage = css;
  el.style.maskSize = "100% 100%";
  el.style.webkitMaskSize = "100% 100%";
  el.style.maskRepeat = "no-repeat";
  el.style.webkitMaskRepeat = "no-repeat";
}

// A nested sequence that has not been rendered yet: a placeholder with its name ("Exact frame" and exports are real).
function SequencePoster({ name }) {
  return (
    <div data-sequence-poster="1" style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 6,
      background: "repeating-linear-gradient(135deg, color-mix(in srgb, var(--accent) 24%, #10131a) 0 14px, color-mix(in srgb, var(--accent) 12%, #10131a) 14px 28px)",
      color: "#fff", textAlign: "center", padding: 8, pointerEvents: "none" }}>
      <svg width={28} height={28} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <rect x="3" y="3" width="18" height="18" rx="2" /><rect x="7" y="7" width="10" height="10" rx="1" />
      </svg>
      <div style={{ fontWeight: 700, fontSize: 14, textShadow: "0 1px 3px #000" }}>{name}</div>
      <div style={{ fontSize: 11, opacity: 0.85 }}>Secuencia anidada · «Fotograma exacto» y la exportación la muestran tal cual</div>
    </div>
  );
}

// One media clip as a live element (video / image / audio) kept in sync with the playback clock.
// With `gl` the picture is drawn by the WebGL preview: the element stays here (clock sync, sound) but is only a hidden source
// whose frames the compositor reads (onEl hands it over).
const GL_SOURCE_STYLE = { position: "absolute", left: 0, top: 0, width: 16, height: 16, opacity: 0, pointerEvents: "none", maxWidth: "none" };

export const ClipLayer = React.memo(function ClipLayer({ clip, track, media, next, canvas, box, k, z, pb, projectMuted, gl, onEl }) {
  const root = useRef(null);
  const xf = useRef(null);
  const el = useRef(null);
  const clipId = clip.id;
  const setEl = useCallback((node) => {
    el.current = node;
    if (onEl) onEl(clipId, node);
  }, [onEl, clipId]);
  const bgc = useRef(null);
  const pic = useRef(null);
  const state = useRef({});
  const forced = useRef(false);
  const settle = useRef(0);
  const isAudio = track.kind === "audio";
  const isImage = media?.kind === "image";
  const geo = useMemo(() => placement(clip, media, box.w, box.h, k), [clip, media, box.w, box.h, k]);
  const src = media?.urls?.play;
  const dur = clipDur(clip);

  const live = { clip, track, media, next, geo, box, k, dur, isAudio, isImage, projectMuted };
  state.current = live;

  // fit=blur: a tiny canvas copy of the picture, cover-fitted and blurred by CSS, drawn behind the contained picture.
  const drawBg = () => {
    const cv = bgc.current;
    const m = el.current;
    if (!cv || !m) return;
    const sw = m.videoWidth || m.naturalWidth;
    const sh = m.videoHeight || m.naturalHeight;
    if (!sw || !sh || (m.readyState !== undefined && m.readyState < 2 && !m.naturalWidth)) return;
    const cw = cv.width;
    const ch = cv.height;
    const ratio = cw / ch;
    let w = sw;
    let h = sw / ratio;
    if (h > sh) { h = sh; w = sh * ratio; }
    try { cv.getContext("2d").drawImage(m, (sw - w) / 2, (sh - h) / 2, w, h, 0, 0, cw, ch); } catch { /* frame not decodable yet */ }
  };

  const apply = (t, playing, force) => {
    const s = state.current;
    const c = s.clip;
    const tr = s.track;
    const local = t - c.start;
    const inside = local >= 0 && local < s.dur;
    const media_el = el.current;
    const rate = pb.rate || 1;
    // ----- picture
    if (!s.isAudio && root.current) {
      const visible = inside && !tr.hidden;
      root.current.style.display = visible ? "block" : "none";
      if (visible) {
        const tf = c.transform;
        const kf = c.keyframes || {};
        const x = kfValue(kf.x, local, tf.x);
        const y = kfValue(kf.y, local, tf.y);
        const scale = kfValue(kf.scale, local, tf.scale);
        const rot = kfValue(kf.rotation, local, tf.rotation);
        let op = kfValue(kf.opacity, local, tf.opacity);
        if (c.fade_in && !c.transition_in) op *= clamp(local / c.fade_in, 0, 1);
        if (c.fade_out) op *= clamp((s.dur - local) / c.fade_out, 0, 1);
        xf.current.style.transform = `translate(${x * s.box.w}px, ${y * s.box.h}px) rotate(${rot}deg) scale(${scale})`;
        xf.current.style.opacity = String(clamp(op, 0, 1));
        // transitions
        let rootStyle = { opacity: 1, transform: "none", clip: "none", filter: "none" };
        const ti = c.transition_in;
        if (ti && local < ti.dur) {
          const r = transitionIn(ti.type, clamp(local / ti.dur, 0, 1));
          rootStyle = { ...rootStyle, ...r };
        } else if (s.next?.transition_in && t >= s.next.start && t < s.next.start + s.next.transition_in.dur) {
          rootStyle = { ...rootStyle, ...transitionOut(s.next.transition_in.type, clamp((t - s.next.start) / s.next.transition_in.dur, 0, 1)) };
        }
        root.current.style.opacity = String(rootStyle.opacity);
        root.current.style.transform = rootStyle.transform;
        root.current.style.clipPath = rootStyle.clip;
        root.current.style.filter = rootStyle.filter;
        // reframe / focus
        const g = s.geo;
        if (media_el && g.cover) {
          const [fx, fy] = focusAt(c, srcAt(c, t));
          const cx = clamp(fx * g.cover.sw - g.w / 2, 0, Math.max(0, g.cover.sw - g.w));
          const cy = clamp(fy * g.cover.sh - g.h / 2, 0, Math.max(0, g.cover.sh - g.h));
          media_el.style.left = `${-cx - g.l * g.fullW}px`;
          media_el.style.top = `${-cy - g.tp * g.fullH}px`;
        }
        if (g.blur) drawBg();
        // shape mask: on the picture box, or on the whole frame with fit=blur (the render masks the blurred fill too)
        setMask(g.blur ? xf.current : pic.current, maskCss(c, local, g.blur ? s.box.w : g.w, g.blur ? s.box.h : g.h));
        // eq brightness / saturation keys: same evaluation as WebGL scene.effectList (shared eqKeys.js)
        if (media_el) {
          media_el.style.filter = cssFilter(filtersWithEqKeys(c, local), s.k ?? 1);
        }
      }
    }
    // ----- sound and time
    if (!media_el || s.isImage) return;
    const near = local >= -2000 && local < s.dur + 200;
    if (!near) {
      if (!media_el.paused) media_el.pause();
      return;
    }
    // The accelerated preview shows the frame the render would for this moment (snapped to the timeline's frame grid, plus a
    // millisecond so a frame that starts exactly there is not missed by a rounding error); the CSS preview follows the raw time.
    const exactFrame = gl && !playing;
    const at = exactFrame ? frameTime(t, canvas.fps || 30).A : t;
    const target = (inside ? (exactFrame ? frameSrcMs(c, at, canvas.fps || 30) : srcAt(c, at)) : c.reverse ? c.src_out : c.src_in) / 1000;
    const kf = c.keyframes || {};
    let db = kfValue(kf.volume_db, local, c.volume_db) + tr.volume_db;
    let gain = dbToGain(db);
    if (c.audio_fade_in) gain *= clamp(local / c.audio_fade_in, 0, 1);
    if (c.audio_fade_out) gain *= clamp((s.dur - local) / c.audio_fade_out, 0, 1);
    const silent = !inside || c.mute || tr.muted || pb.muted || s.projectMuted;
    media_el.muted = silent || forced.current;
    media_el.volume = clamp(gain, 0, 1);
    const wantPlay = playing && inside && !c.reverse;
    const speed = clamp(speedAt(c, t) * rate, 0.0625, 16);
    if (media_el.playbackRate !== speed) media_el.playbackRate = speed;
    const diff = Math.abs(media_el.currentTime - target);
    if (wantPlay) {
      if (diff > 0.15) media_el.currentTime = target;
      if (media_el.paused) {
        const p = media_el.play();
        if (p && p.catch) {
          p.catch((err) => {
            if (err?.name === "NotAllowedError") {
              forced.current = true;
              media_el.muted = true;
              media_el.play().catch(() => {});
            }
          });
        }
      }
    } else {
      if (!media_el.paused) media_el.pause();
      if (inside) {
        // Scrubbing: do not queue a seek behind a running one, catch up once it lands (see the "seeked" listener).
        if (diff > 0.02 && !media_el.seeking) media_el.currentTime = target;
      } else if (diff > 0.1) {
        // Preloaded neighbours only settle once the playhead stops moving.
        if (force || playing) { if (!media_el.seeking) media_el.currentTime = target; } else {
          clearTimeout(settle.current);
          settle.current = setTimeout(() => { if (el.current) apply(pb.t, pb.playing, true); }, 260);
        }
      }
    }
  };

  useEffect(() => pb.subscribe(apply), [pb]); // eslint-disable-line react-hooks/exhaustive-deps
  useLayoutEffect(() => { apply(pb.t, pb.playing); });
  useEffect(() => {
    const m = el.current;
    if (!m) return undefined;
    const catchUp = () => { if (!pb.playing) apply(pb.t, false); };
    m.addEventListener("seeked", catchUp);
    return () => { m.removeEventListener("seeked", catchUp); clearTimeout(settle.current); };
  }, [pb, src]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const m = el.current;
    if (!m || !geo.blur) return undefined;
    const events = ["seeked", "loadeddata", "load"];
    events.forEach((e) => m.addEventListener(e, drawBg));
    return () => events.forEach((e) => m.removeEventListener(e, drawBg));
  }, [geo.blur, src]); // eslint-disable-line react-hooks/exhaustive-deps

  if (isAudio) {
    return <audio ref={el} src={src} preload="auto" style={{ display: "none" }} />;
  }
  if (gl) {
    return isImage
      ? <img ref={setEl} src={src} alt="" style={GL_SOURCE_STYLE} draggable={false} />
      : <video ref={setEl} src={src} preload="auto" playsInline style={GL_SOURCE_STYLE} />;
  }
  const mediaStyle = {
    position: "absolute",
    width: geo.fullW,
    height: geo.fullH,
    left: -geo.l * geo.fullW,
    top: -geo.tp * geo.fullH,
    maxWidth: "none",
    pointerEvents: "none",
    objectFit: "fill",
    filter: cssFilter(filtersWithEqKeys(clip, 0), k),
    transform: clip.filters?.some((f) => f.type === "hflip" && f.enabled !== false) ? "scaleX(-1)" : clip.filters?.some((f) => f.type === "vflip" && f.enabled !== false) ? "scaleY(-1)" : undefined,
  };
  return (
    <div ref={root} style={{ position: "absolute", inset: 0, zIndex: z, display: "none", willChange: "opacity, transform" }}>
      <div ref={xf} style={{ position: "absolute", inset: 0, transformOrigin: "50% 50%", willChange: "transform, opacity" }}>
        {geo.blur ? (
          <div style={{ position: "absolute", inset: 0, overflow: "hidden" }}>
            <canvas ref={bgc} width={Math.max(8, Math.round(box.w / 10))} height={Math.max(8, Math.round(box.h / 10))} data-blur-bg="1" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", transform: "scale(1.12)", filter: "blur(24px) brightness(0.9)" }} />
          </div>
        ) : null}
        <div ref={pic} style={{ position: "absolute", left: geo.x, top: geo.y, width: geo.w, height: geo.h, overflow: "hidden" }}>
          {isImage
            ? <img ref={el} src={src} alt="" style={mediaStyle} draggable={false} />
            : <video ref={el} src={src || undefined} preload="auto" playsInline style={mediaStyle} />}
          {media?.kind === "sequence" && !src ? <SequencePoster name={media.name || clip.label} /> : null}
        </div>
      </div>
    </div>
  );
});

// A text clip drawn as HTML over the preview.
export const TextLayer = React.memo(function TextLayer({ clip, track, box, k, z, pb }) {
  const root = useRef(null);
  const body = useRef(null);
  const st = clip.style || {};
  const dur = clipDur(clip);
  const state = useRef({});
  state.current = { clip, dur, track, box };

  const apply = (t) => {
    const s = state.current;
    const local = t - s.clip.start;
    const inside = local >= 0 && local < s.dur && !s.track.hidden;
    if (!root.current) return;
    root.current.style.display = inside ? "block" : "none";
    if (!inside) return;
    const anim = s.clip.style?.animation || "none";
    const kf = s.clip.keyframes || {};
    const tf = s.clip.transform;
    const x = kfValue(kf.x, local, tf.x) * s.box.w;
    const y = kfValue(kf.y, local, tf.y) * s.box.h;
    let op = kfValue(kf.opacity, local, tf.opacity);
    let tr = `translate(${x}px, ${y}px) rotate(${kfValue(kf.rotation, local, tf.rotation)}deg) scale(${kfValue(kf.scale, local, tf.scale)})`;
    const fin = clamp(local / 250, 0, 1);
    const fout = clamp((s.dur - local) / 250, 0, 1);
    if (anim === "fade") op *= Math.min(fin, fout);
    if (anim === "pop") { tr += ` scale(${0.7 + 0.3 * (1 - Math.pow(1 - fin, 3))})`; op *= Math.min(1, fin * 2); }
    if (anim === "slide_up") { tr += ` translateY(${(1 - fin) * 40}px)`; op *= fin; }
    root.current.style.transform = tr;
    root.current.style.opacity = String(clamp(op, 0, 1));
    if (anim === "typewriter" && body.current) {
      const n = Math.ceil(s.clip.text.length * clamp(local / Math.min(s.dur * 0.6, 2500), 0, 1));
      body.current.textContent = s.clip.text.slice(0, n);
    } else if (body.current && body.current.textContent !== s.clip.text) body.current.textContent = s.clip.text;
  };
  useEffect(() => pb.subscribe(apply), [pb]); // eslint-disable-line react-hooks/exhaustive-deps
  useLayoutEffect(() => { apply(pb.t); });

  const m = (st.margin ?? 80) * k;
  const pos = st.position === "top" ? { top: m } : st.position === "bottom" ? { bottom: m } : { top: "50%", marginTop: 0 };
  const wrapY = st.position === "middle" || !st.position ? "translateY(-50%)" : "none";
  const stroke = (st.outline_width ?? 0) * k;
  return (
    <div ref={root} style={{ position: "absolute", left: m, right: m, ...pos, zIndex: z, display: "none", textAlign: st.align || "center", pointerEvents: "none", willChange: "transform, opacity" }}>
      <div style={{ transform: wrapY }}>
        <span
          ref={body}
          style={{
            display: "inline-block",
            whiteSpace: "pre-wrap",
            fontFamily: `"${st.font || "Arial"}", Arial, sans-serif`,
            fontSize: (st.size || 72) * k,
            fontWeight: st.bold === false ? 400 : 700,
            fontStyle: st.italic ? "italic" : "normal",
            color: st.color || "#fff",
            WebkitTextStroke: stroke ? `${stroke}px ${st.outline || "#000"}` : undefined,
            paintOrder: "stroke fill",
            textShadow: st.shadow ? `${st.shadow * k}px ${st.shadow * k}px ${st.shadow * k * 1.5}px #0009` : undefined,
            background: st.box || undefined,
            padding: st.box ? `${0.15 * (st.size || 72) * k}px ${0.3 * (st.size || 72) * k}px` : undefined,
            borderRadius: st.box ? 8 * k : undefined,
            lineHeight: 1.15,
          }}
        >
          {clip.text}
        </span>
      </div>
    </div>
  );
});
