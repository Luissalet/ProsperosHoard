// Number helpers that reproduce the render's arithmetic (render/compiler.py) so the WebGL preview lands on the same pixels.

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// Python's round(): halves go to the even neighbour (JS rounds them up).
export function pyRound(x) {
  const f = Math.floor(x);
  const d = x - f;
  if (d < 0.5) return f;
  if (d > 0.5) return f + 1;
  return f % 2 === 0 ? f : f + 1;
}

// int(round(x / 2)) * 2: the even size ffmpeg filters are given.
export const even = (x) => pyRound(x / 2) * 2;
export const evenCeil = (x) => Math.ceil(x / 2) * 2;

// ffmpeg's overlay / crop positions: truncated to an integer, then to a multiple of 2 (4:2:0 chroma).
export const snap2 = (d) => Math.trunc(d) & ~1;

// ---- keyframes: the same eased points, sampled by the same flat expression the render builds (keyframe_expr) ----

const cache = new WeakMap();

function easeFn(kind, u) {
  if (kind === "ease_in") return u * u;
  if (kind === "ease_out") return 1 - (1 - u) * (1 - u);
  if (kind === "ease_in_out") return 3 * u * u - 2 * u * u * u;
  return u;
}

function segmentDomain(a, b) {
  if (a.ease_span != null && a.ease_into != null && a.ease_v0 != null && a.ease_v1 != null) {
    return { span: a.ease_span, into: a.ease_into, v0: a.ease_v0, v1: a.ease_v1 };
  }
  return { span: Math.max(1, b.t - a.t), into: 0, v0: a.v, v1: b.v };
}

const EASE_STEPS = 6;

function canonicalGrid(span, v0, v1, ease) {
  const out = [];
  for (let j = 0; j <= EASE_STEPS; j++) {
    const u = j / EASE_STEPS;
    out.push([span * u, v0 + (v1 - v0) * easeFn(ease, u)]);
  }
  return out;
}

function polylineAt(pts, x) {
  if (!pts.length) return 0;
  if (x <= pts[0][0]) return pts[0][1];
  for (let i = 0; i + 1 < pts.length; i++) {
    const [xa, va] = pts[i];
    const [xb, vb] = pts[i + 1];
    if (x <= xb) {
      const dt = xb - xa;
      return dt <= 1e-12 ? vb : va + (vb - va) * ((x - xa) / dt);
    }
  }
  return pts[pts.length - 1][1];
}

function renderEndpointV(k, prev) {
  if (k.ease_span != null && k.ease_into != null && k.ease_v0 != null && k.ease_v1 != null) {
    return polylineAt(canonicalGrid(k.ease_span, k.ease_v0, k.ease_v1, k.ease || "linear"), k.ease_into);
  }
  if (
    prev &&
    prev.ease &&
    prev.ease !== "linear" &&
    prev.ease !== "hold" &&
    prev.ease_span != null &&
    prev.ease_into != null &&
    prev.ease_v0 != null &&
    prev.ease_v1 != null
  ) {
    const { span, into, v0, v1 } = segmentDomain(prev, k);
    return polylineAt(canonicalGrid(span, v0, v1, prev.ease), into + (k.t - prev.t));
  }
  return k.v;
}

function easePoints(keys) {
  const sorted = [...keys].sort((a, b) => a.t - b.t);
  const out = [];
  for (let i = 0; i < sorted.length; i++) {
    const k = sorted[i];
    const prev = i ? sorted[i - 1] : null;
    out.push([k.t / 1000, renderEndpointV(k, prev)]);
    if (i + 1 >= sorted.length) break;
    const n = sorted[i + 1];
    if (k.ease === "hold") {
      out.push([(n.t - 1) / 1000, renderEndpointV(k, prev)]);
    } else if (k.ease && k.ease !== "linear") {
      // Canonical 6-step grid on the full ease domain, clipped to the visible
      // segment; endpoints lie on that polyline so arbitrary cuts stay exact.
      const { span, into, v0, v1 } = segmentDomain(k, n);
      const origin = k.t - into;
      const grid = canonicalGrid(span, v0, v1, k.ease);
      for (let j = 1; j < EASE_STEPS; j++) {
        const fullT = origin + span * (j / EASE_STEPS);
        if (fullT <= k.t || fullT >= n.t) continue;
        out.push([fullT / 1000, grid[j][1]]);
      }
    }
  }
  return out.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
}

// Value of an animated property at clip-local time `localMs`: v0 + sum(slope_i * clip(t - t_i, 0, dt_i)).
export function kfRender(keys, localMs, fallback) {
  if (!keys || !keys.length) return fallback;
  let pts = cache.get(keys);
  if (!pts) {
    pts = easePoints(keys);
    cache.set(keys, pts);
  }
  const t = localMs / 1000;
  let v = pts[0][1];
  for (let i = 0; i + 1 < pts.length; i++) {
    const [ta, va] = pts[i];
    const [tb, vb] = pts[i + 1];
    const dt = tb - ta;
    if (dt <= 1e-6 || Math.abs(vb - va) < 1e-6) continue;
    v += ((vb - va) / dt) * clamp(t - ta, 0, dt);
  }
  return v;
}

export function hexToRgb(hex, fallback = [0, 0, 0]) {
  const m = /^#?([0-9a-f]{6})/i.exec(hex || "");
  if (!m) return fallback;
  const n = parseInt(m[1], 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}
