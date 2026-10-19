// What the render draws at one instant, as plain data the WebGL compositor can draw.
// A port of render/compiler.py (pieces_in, clip_chain, _fit_chain, _focus_exprs, _position, the xfade branch of chunk_graph):
// the same rounding to even sizes, the same crop / fit rules, the same keyframe maths, the same piece boundaries.
import { activeEqKeyProps, applyEqKeys } from "../eqKeys.js";
import { AUDIO_FX, FX_DEFAULTS } from "../fxspec.js";
import { hasRamp, rampSegments, srcAt } from "../time.js";
import { clamp, even, evenCeil, hexToRgb, kfRender, pyRound, snap2 } from "./mathx.js";

export const XFADE_ID = {
  crossfade: 0, dissolve: 1, fade_black: 2, fade_white: 3, slide_left: 4, slide_right: 5, slide_up: 6, slide_down: 7,
  wipe_left: 8, wipe_right: 9, wipe_up: 10, wipe_down: 11, circle_open: 12, circle_close: 13, zoom_in: 14, pixelize: 15,
  radial: 16, smooth_left: 17, smooth_right: 18, blur: 19,
};

// Length of a clip on the timeline, as the render rounds it (Clip.duration): with a speed curve it is the sum over its constant-speed steps.
export const clipDurMs = (c) => {
  if (c.type === "text") return c.length;
  if (hasRamp(c)) return pyRound(rampSegments(c.src_in, c.src_out, c.speed_keys).reduce((s, [x, y, v]) => s + (y - x) / v, 0));
  return pyRound((c.src_out - c.src_in) / (c.speed || 1));
};
// Source time shown at timeline time t: the same function the CSS preview seeks with (time.js), speed curves and reverse included.
export const srcAtMs = (c, t) => srcAt(c, t);

// Where to seek a <video> so it shows the source frame the render puts on the output frame at time A. The render retimes the
// source frames (setpts) and then the fps filter gives output slot A the last frame that lands before A + half a frame, so the
// frame is the one at or before the source time of A + 0.5 / fps (this only differs from the source time of A when the clip
// plays faster or slower than 1x; for a reversed clip it comes out the same way). A millisecond below it keeps a frame that
// lands exactly on the boundary out, as the filter does.
export const frameSrcMs = (clip, A, fps) => srcAtMs(clip, A + 500 / fps) - 1;

// The output the render uses for a frame of `width` pixels (render_frame): even sizes, factor = output / canvas.
export function outputSize(canvas, width) {
  let W = canvas.width;
  let H = canvas.height;
  if (width && width < W) {
    H = Math.max(2, even((H * width) / W));
    W = Math.max(2, even(width));
  }
  return { W, H, fps: canvas.fps || 30, factor: W / canvas.width };
}

// The render snaps a requested time to a frame: f0 = round(t * fps / 1000), shown at f0 * 1000 / fps.
export function frameTime(tMs, fps) {
  const f0 = pyRound((Math.round(tMs) * fps) / 1000);
  return { f0, A: (f0 * 1000) / fps };
}

// Which matrix the browser decodes the picture with: the one the file is tagged with (the server probes it), and BT.601 when
// it is untagged or unknown (what browsers assume then). Only the effects that work on luma / chroma care about it.
export const matrixOf = (media) => (/^(bt709|bt2020)/i.test(media?.color_space || "") ? 709 : 601);

// ---------------------------------------------------------------- focus (cover) and fit

function focusAtPath(clip, srcMs) {
  const tr = clip.transform;
  const path = clip.reframe?.path;
  if (!path || !path.length) return [tr.focus_x, tr.focus_y];
  const fy = (p) => (p.length > 2 ? p[2] : tr.focus_y);
  if (srcMs <= path[0][0]) return [path[0][1], fy(path[0])];
  const last = path[path.length - 1];
  if (srcMs >= last[0]) return [last[1], fy(last)];
  let lo = 0;
  let hi = path.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (path[mid][0] <= srcMs) lo = mid; else hi = mid;
  }
  const a = path[lo];
  const b = path[hi];
  const p = (srcMs - a[0]) / Math.max(1e-9, b[0] - a[0]);
  return [a[1] + (b[1] - a[1]) * p, fy(a) + (fy(b) - fy(a)) * p];
}

// _fit_chain: size of the fitted picture and, for cover, the scaled size it is cropped from.
function fitChain(clip, media, out, boxScale, fit) {
  const { W, H } = out;
  const cr = clip.crop || {};
  const l = cr.left || 0;
  const r = cr.right || 0;
  const t = cr.top || 0;
  const b = cr.bottom || 0;
  const iw = media.width ? Math.max(2, media.width * (1 - l - r)) : W;
  const ih = media.height ? Math.max(2, media.height * (1 - t - b)) : H;
  const bw = Math.max(2, even(W * boxScale));
  const bh = Math.max(2, even(H * boxScale));
  const crop = { l, t, r, b };
  if (fit === "fill") return { w: bw, h: bh, cover: null, crop, bw, bh };
  if (fit === "none") {
    return { w: Math.max(2, even(iw * boxScale * out.factor)), h: Math.max(2, even(ih * boxScale * out.factor)), cover: null, crop, bw, bh };
  }
  const ratio = iw / ih;
  if (fit === "contain") {
    if (bw / bh > ratio) return { w: Math.max(2, even(bh * ratio)), h: bh, cover: null, crop, bw, bh };
    return { w: bw, h: Math.max(2, even(bw / ratio)), cover: null, crop, bw, bh };
  }
  let sw;
  let sh;
  if (bw / bh > ratio) {
    sw = bw;
    sh = Math.max(bh, evenCeil(bw / ratio));
  } else {
    sh = bh;
    sw = Math.max(bw, evenCeil(bh * ratio));
  }
  return { w: bw, h: bh, cover: { sw, sh }, crop, bw, bh };
}

// The part of the source (uv, y down) a fit shows: the crop rectangle, narrowed to the focus window for cover.
function sourceWindow(fc, focus) {
  const { l, t, r, b } = fc.crop;
  let u0 = 0;
  let v0 = 0;
  let u1 = 1;
  let v1 = 1;
  if (fc.cover) {
    const { sw, sh } = fc.cover;
    const maxX = Math.max(0, sw - fc.bw);
    const maxY = Math.max(0, sh - fc.bh);
    const cx = maxX ? snap2(clamp(focus[0] * sw - fc.bw / 2, 0, maxX)) : 0;
    const cy = maxY ? snap2(clamp(focus[1] * sh - fc.bh / 2, 0, maxY)) : 0;
    u0 = cx / sw; u1 = (cx + fc.bw) / sw;
    v0 = cy / sh; v1 = (cy + fc.bh) / sh;
  }
  const kw = 1 - l - r;
  const kh = 1 - t - b;
  return [l + kw * u0, t + kh * v0, l + kw * u1, t + kh * v1];
}

// Brightness / saturation keys (compiler.eq_key_exprs) drive the first enabled eq at clip-local time `local`; with keys and no eq
// effect at all the clip gets a default one in front; when every eq on the clip is off the keys do nothing (eqKeys.js).
// Blur radius and pixelate block are given in canvas pixels, so a smaller output scales them (filters.py does the same).
function effectList(clip, factor, local) {
  const out = [];
  const eqs = (clip.filters || []).filter((f) => f.type === "eq");
  const keyed = activeEqKeyProps(clip);
  let keyedDone = false;
  if (keyed.length && !eqs.length) {
    out.push({ type: "eq", p: applyEqKeys(clip, local, FX_DEFAULTS.eq).params });
    keyedDone = true;
  }
  for (const f of clip.filters || []) {
    if (f.enabled === false || AUDIO_FX.has(f.type)) continue;
    const base = FX_DEFAULTS[f.type];
    if (!base) continue;
    let p = { ...base, ...(f.params || {}) };
    if (f.type === "blur") p.radius *= factor;
    if (f.type === "pixelate") p.size = Math.max(1, pyRound(p.size * factor));
    if (f.type === "eq" && keyed.length && !keyedDone) {
      p = applyEqKeys(clip, local, p).params;
      keyedDone = true;
    }
    out.push({ type: f.type, p });
  }
  return out;
}

// Shape mask (compiler.mask_alpha_expr) in the pixels of the clip's own picture, w x h: centre, half sizes, soft edge width
// and corner radius, with the animated values (mask_x / mask_y / mask_w / mask_h / mask_feather) read at clip-local time.
function maskFor(clip, local, w, h) {
  const m = clip.mask;
  if (!m || m.enabled === false) return null;
  const kf = clip.keyframes || {};
  const val = (name, dflt) => kfRender(kf[`mask_${name}`], local, m[name] ?? dflt);
  const rx = Math.max(0.5, (val("w", 0.8) * w) / 2);
  const ry = Math.max(0.5, (val("h", 0.8) * h) / 2);
  const shape = m.shape || "ellipse";
  return {
    cx: val("x", 0.5) * w,
    cy: val("y", 0.5) * h,
    rx,
    ry,
    feather: Math.max(1, val("feather", 0) * Math.min(w, h)),
    radius: shape === "rounded" ? (m.radius ?? 0.2) * 2 * Math.min(rx, ry) : 0,
    invert: !!m.invert,
    ellipse: shape === "ellipse",
  };
}

// ---------------------------------------------------------------- one clip at one instant

// clip_chain + _position for the clip shown at frame time A. `fades` is false inside transitions.
export function layerFor(clip, media, out, A, { fades = true, pieceEndsAtClipEnd = true } = {}) {
  const { W, H } = out;
  const tf = clip.transform;
  const kf = clip.keyframes || {};
  const local = A - clip.start;
  const scaleKeys = kf.scale && kf.scale.length;
  const boxScale = scaleKeys ? 1 : tf.scale;
  const srcMs = frameSrcMs(clip, A, out.fps);
  const focusMs = srcAtMs(clip, A);

  // ----- fit
  let w;
  let h;
  const layer = { key: clip.id, clipId: clip.id, mediaId: clip.media, mediaKind: media.kind, matrix: matrixOf(media), srcMs };
  if (tf.fit === "blur") {
    const bgFc = fitChain(clip, media, out, boxScale, "cover");
    const fgFc = fitChain(clip, media, out, boxScale, "contain");
    w = bgFc.w;
    h = bgFc.h;
    const fgx = snap2((w - fgFc.w) / 2);
    const fgy = snap2((h - fgFc.h) / 2);
    layer.fit = {
      mode: "blur",
      win: sourceWindow(fgFc, [0.5, 0.5]),
      bgWin: sourceWindow(bgFc, [tf.focus_x, tf.focus_y]),
      fg: [fgx / w, fgy / h, (fgx + fgFc.w) / w, (fgy + fgFc.h) / h],
      sigma: Math.max(8, Math.trunc(Math.min(w, h) / 30)),
    };
  } else {
    const fc = fitChain(clip, media, out, boxScale, tf.fit);
    w = fc.w;
    h = fc.h;
    layer.fit = { mode: tf.fit, win: sourceWindow(fc, focusAtPath(clip, focusMs)), fg: [0, 0, 1, 1] };
  }
  layer.fit.zoom = scaleKeys ? clamp(Math.max(1, kfRender(kf.scale, local, 1)), 1, 10) : 1;
  layer.fitW = w;
  layer.fitH = h;

  // ----- effects (pixelate trims the picture to whole blocks)
  layer.effects = effectList(clip, out.factor, local);
  for (const fx of layer.effects) {
    if (fx.type === "pixelate") {
      const s = Math.max(1, Math.trunc(fx.p.size));
      fx.inW = w; fx.inH = h;
      w = Math.max(1, Math.trunc(w / s)) * s;
      h = Math.max(1, Math.trunc(h / s)) * s;
    }
  }
  layer.w = w;
  layer.h = h;
  layer.mask = maskFor(clip, local, w, h);

  // ----- rotation (the layer grows to hold it)
  const rotKeys = kf.rotation && kf.rotation.length;
  let ow = w;
  let oh = h;
  let angle = 0;
  if (rotKeys) {
    angle = (kfRender(kf.rotation, local, 0) * Math.PI) / 180;
    ow = oh = evenCeil(Math.hypot(w, h));
  } else if (Math.abs(tf.rotation) > 1e-3) {
    angle = (tf.rotation * Math.PI) / 180;
    ow = evenCeil(Math.abs(w * Math.cos(angle)) + Math.abs(h * Math.sin(angle)));
    oh = evenCeil(Math.abs(w * Math.sin(angle)) + Math.abs(h * Math.cos(angle)));
  }
  layer.angle = angle;
  layer.rotated = !!rotKeys || Math.abs(tf.rotation) > 1e-3;
  layer.ow = ow;
  layer.oh = oh;

  // ----- position and opacity
  const xv = kf.x && kf.x.length ? kfRender(kf.x, local, 0) : tf.x;
  const yv = kf.y && kf.y.length ? kfRender(kf.y, local, 0) : tf.y;
  layer.x = snap2((W - ow) / 2 + xv * W);
  layer.y = snap2((H - oh) / 2 + yv * H);
  let alpha = 1;
  if (kf.opacity && kf.opacity.length) alpha = clamp(kfRender(kf.opacity, local, 1), 0, 1);
  else if (tf.opacity < 0.999) alpha = tf.opacity;
  if (fades) {
    const dur = clipDurMs(clip);
    if (clip.fade_in && !clip.transition_in) alpha *= clamp(local / clip.fade_in, 0, 1);
    if (clip.fade_out && pieceEndsAtClipEnd) alpha *= clamp((dur - local) / Math.min(clip.fade_out, dur), 0, 1);
  }
  layer.alpha = alpha;
  return layer;
}

// ---------------------------------------------------------------- pieces and transitions

// pieces_in for the single frame [A, A + frame): what each visible video track shows, bottom track first.
function piecesAt(doc, A, frameMs) {
  const B = A + frameMs;
  const order = new Map(doc.tracks.map((t, i) => [t.id, i]));
  const out = [];
  for (const track of doc.tracks) {
    if (track.kind !== "video" || track.hidden) continue;
    const clips = track.clips.filter((c) => c.type === "media" || c.type === "sequence").sort((a, b) => a.start - b.start);
    clips.forEach((c, i) => {
      const nxt = clips[i + 1] || null;
      const cEnd = c.start + clipDurMs(c);
      const start = c.start + (c.transition_in && i > 0 ? c.transition_in.dur : 0);
      let end = cEnd;
      let truncated = false;
      if (nxt && nxt.transition_in && nxt.start < cEnd) {
        end = nxt.start;
        truncated = true;
        const ta = nxt.start;
        const tb = nxt.start + nxt.transition_in.dur;
        if (ta < B && tb > A) out.push({ track, kind: "xfade", a: c, b: nxt, ta, tb, t0: Math.max(A, ta), type: nxt.transition_in.type });
      }
      const a = Math.max(A, start);
      const b = Math.min(B, end);
      if (b - a > 0.5) out.push({ track, kind: "piece", clip: c, t0: a, pieceEndsAtClipEnd: !truncated });
    });
  }
  return out
    .map((p, i) => ({ p, i }))
    .sort((x, y) => order.get(x.p.track.id) - order.get(y.p.track.id) || x.p.t0 - y.p.t0 || x.i - y.i)
    .map((x) => x.p);
}

// A nested sequence is a video once its intermediate is rendered; before that the DOM draws its poster (layers.jsx), not the GL.
export const drawable = (m) => !!m && (m.kind !== "sequence" || !!m.urls?.play);

// Everything to draw at time `tMs` (ms on the timeline), at the output size `out`.
export function buildScene(doc, media, tMs, out) {
  const { A } = frameTime(tMs, out.fps);
  const frameMs = 1000 / out.fps;
  const items = [];
  for (const p of piecesAt(doc, A, frameMs)) {
    if (p.t0 > A + 1e-6) continue; // its first frame comes after this one
    if (p.kind === "piece") {
      const m = media[p.clip.media];
      if (!drawable(m)) continue;
      items.push({ kind: "layer", layer: layerFor(p.clip, m, out, A, { pieceEndsAtClipEnd: p.pieceEndsAtClipEnd }) });
    } else {
      const ma = media[p.a.media];
      const mb = media[p.b.media];
      const tdur = (p.tb - p.ta) / 1000;
      const dur = Math.max(0.04, tdur - 0.001);
      const progress = clamp(1 - (A - p.ta) / 1000 / dur, 0, 1);
      const inRange = (c) => A >= c.start && A < c.start + clipDurMs(c);
      items.push({
        kind: "xfade",
        type: XFADE_ID[p.type] ?? 0,
        name: p.type,
        progress,
        a: drawable(ma) && inRange(p.a) ? layerFor(p.a, ma, out, A, { fades: false }) : null,
        b: drawable(mb) && inRange(p.b) ? layerFor(p.b, mb, out, A, { fades: false }) : null,
      });
    }
  }
  return { A, W: out.W, H: out.H, bg: hexToRgb(doc.canvas.background), items };
}
