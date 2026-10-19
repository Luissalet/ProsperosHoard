// Time helpers. Everything on the timeline is integer milliseconds.
export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// "m:ss.mmm" or "h:mm:ss.mmm"
export function fmtMs(ms) {
  ms = Math.max(0, Math.round(ms || 0));
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  const s = Math.floor((ms % 60000) / 1000);
  const f = ms % 1000;
  const mmm = String(f).padStart(3, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${mmm}` : `${m}:${String(s).padStart(2, "0")}.${mmm}`;
}

// Player timecode, always "hh:mm:ss:ff" (frames) like every editor, so 7 seconds never reads as 7 minutes.
export function fmtFrames(ms, fps = 30) {
  ms = Math.max(0, Math.round(ms || 0));
  const total = Math.floor((ms * fps) / 1000 + 1e-6);
  const f = total % Math.round(fps);
  const secs = Math.floor(ms / 1000);
  const s = secs % 60;
  const m = Math.floor(secs / 60) % 60;
  const h = Math.floor(secs / 3600);
  const two = (n) => String(n).padStart(2, "0");
  return `${two(h)}:${two(m)}:${two(s)}:${two(f)}`;
}

// Short label for rulers: 12s / 1:05 / 1:05.5
export function fmtRuler(ms, step) {
  const s = ms / 1000;
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  if (step >= 1000) return m ? `${m}:${String(Math.round(rest)).padStart(2, "0")}` : `${Math.round(rest)}s`;
  const digits = step >= 100 ? 1 : 2;
  return m ? `${m}:${rest.toFixed(digits).padStart(3 + digits, "0")}` : `${rest.toFixed(digits)}s`;
}

// Parses "1:23.5", "83.5", "1:02:03", "1500ms", "2s" -> ms (null when it is not a time).
export function parseTc(text) {
  const s = String(text ?? "").trim().replace(",", ".");
  if (!s) return null;
  let m = /^(-?\d+(?:\.\d+)?)\s*ms$/i.exec(s);
  if (m) return Math.round(parseFloat(m[1]));
  m = /^(\d+(?:\.\d+)?)\s*s$/i.exec(s);
  if (m) return Math.round(parseFloat(m[1]) * 1000);
  const parts = s.split(":");
  if (parts.length > 3 || parts.some((p) => !/^\d+(\.\d+)?$/.test(p))) return null;
  let secs = 0;
  for (const p of parts) secs = secs * 60 + parseFloat(p);
  return Math.round(secs * 1000);
}

export const frameMs = (fps) => 1000 / (fps || 30);

// Pick a ruler step (ms) so labels are at least `minPx` apart.
const STEPS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 1800000, 3600000];
export function rulerStep(pxPerMs, minPx = 90) {
  for (const s of STEPS) if (s * pxPerMs >= minPx) return s;
  return STEPS[STEPS.length - 1];
}

export function fmtBytes(n) {
  if (typeof n !== "number" || Number.isNaN(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

export function fmtDate(ts, lang) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString(lang === "en" ? "en-GB" : "es-ES", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}

// ---------- speed curves: the same constant-speed steps as the server (timeline.py ramp_segments), so durations agree.
const RAMP_STEP_MS = 100;
const RAMP_MAX_STEPS = 24;

export function easeValue(kind, u) {
  if (kind === "hold") return 0;
  if (kind === "ease_in") return u * u;
  if (kind === "ease_out") return 1 - (1 - u) * (1 - u);
  if (kind === "ease_in_out") return 3 * u * u - 2 * u * u * u;
  return u;
}

export function speedValue(keys, src) {
  if (!keys || !keys.length) return 1;
  const ks = [...keys].sort((a, b) => a.t - b.t);
  if (src <= ks[0].t) return ks[0].v;
  for (let i = 0; i < ks.length - 1; i++) {
    const a = ks[i];
    const b = ks[i + 1];
    if (a.t <= src && src < b.t) return a.v + (b.v - a.v) * easeValue(a.ease, (src - a.t) / Math.max(1e-9, b.t - a.t));
  }
  return ks[ks.length - 1].v;
}

function inverseIntegral(keys, a, b) {
  if (b <= a) return 0;
  const n = 8;
  const h = (b - a) / n;
  let total = 1 / speedValue(keys, a) + 1 / speedValue(keys, b);
  for (let i = 1; i < n; i++) total += (i % 2 ? 4 : 2) / speedValue(keys, a + i * h);
  return (total * h) / 3;
}

const segCache = new Map();
export function rampSegments(srcIn, srcOut, keys) {
  const key = `${srcIn}|${srcOut}|${keys.map((k) => `${k.t},${k.v},${k.ease}`).join(";")}`;
  const hit = segCache.get(key);
  if (hit) return hit;
  const ks = [...keys].sort((a, b) => a.t - b.t);
  const cuts = new Set([srcIn, srcOut]);
  for (const k of ks) if (srcIn < k.t && k.t < srcOut) cuts.add(k.t);
  for (let i = 0; i < ks.length - 1; i++) {
    const a = ks[i];
    const b = ks[i + 1];
    if (b.t <= srcIn || a.t >= srcOut || Math.abs(a.v - b.v) < 1e-6 || a.ease === "hold") continue;
    const n = Math.max(1, Math.min(RAMP_MAX_STEPS, Math.ceil((b.t - a.t) / RAMP_STEP_MS)));
    for (let j = 1; j < n; j++) {
      const x = a.t + ((b.t - a.t) * j) / n;
      if (srcIn < x && x < srcOut) cuts.add(x);
    }
  }
  const edges = [...cuts].sort((x, y) => x - y);
  const out = [];
  for (let i = 0; i < edges.length - 1; i++) {
    const a = edges[i];
    const b = edges[i + 1];
    if (b - a < 1e-6) continue;
    out.push([a, b, Math.max(0.1, Math.min(16, (b - a) / inverseIntegral(ks, a, b)))]);
  }
  const res = out.length ? out : [[srcIn, srcOut, ks[0].v]];
  if (segCache.size > 500) segCache.clear();
  segCache.set(key, res);
  return res;
}

export const hasRamp = (c) => c.type !== "text" && !!c.speed_keys?.length;

function steps(c) {
  const segs = rampSegments(c.src_in, c.src_out, c.speed_keys);
  return c.reverse ? [...segs].reverse().map(([a, b, v]) => [b, a, v]) : segs;
}

// Clip geometry helpers over the project document.
export function clipDur(c) {
  if (c.type === "text") return c.length;
  if (!hasRamp(c)) return Math.round((c.src_out - c.src_in) / (c.speed || 1));
  return Math.round(rampSegments(c.src_in, c.src_out, c.speed_keys).reduce((s, [a, b, v]) => s + (b - a) / v, 0));
}
export const clipEnd = (c) => c.start + clipDur(c);

export function srcAt(c, t) {
  if (!hasRamp(c)) {
    const off = (t - c.start) * (c.speed || 1);
    return c.reverse ? c.src_out - off : c.src_in + off;
  }
  const sign = c.reverse ? -1 : 1;
  const st = steps(c);
  const local = t - c.start;
  if (local <= 0) return st[0][0] + sign * local * st[0][2];
  let acc = 0;
  for (const [s0, s1, v] of st) {
    const d = Math.abs(s1 - s0) / v;
    if (local <= acc + d) return s0 + sign * (local - acc) * v;
    acc += d;
  }
  const last = st[st.length - 1];
  return last[1] + sign * (local - acc) * last[2];
}

// Timeline time at which source time `src` is shown.
export function timelineAt(c, src) {
  if (!hasRamp(c)) return c.start + (c.reverse ? c.src_out - src : src - c.src_in) / (c.speed || 1);
  const sign = c.reverse ? -1 : 1;
  const st = steps(c);
  const q = sign * (src - st[0][0]);
  if (q <= 0) return c.start + q / st[0][2];
  let accQ = 0;
  let accT = 0;
  for (const [s0, s1, v] of st) {
    const len = Math.abs(s1 - s0);
    if (q <= accQ + len) return c.start + accT + (q - accQ) / v;
    accQ += len;
    accT += len / v;
  }
  return c.start + accT + (q - accQ) / st[st.length - 1][2];
}

// Playback speed at timeline time t (the curve when there is one).
export const speedAt = (c, t) => (hasRamp(c) ? speedValue(c.speed_keys, srcAt(c, t)) : c.speed || 1);

// Ramp presets as the server builds them (ops.ramp_preset), for drawing a preview before applying.
export const RAMP_PRESETS = ["speed_up", "slow_down", "ease_in_out", "hit"];

export function findClip(doc, id) {
  for (const tr of doc.tracks) for (const c of tr.clips) if (c.id === id) return { track: tr, clip: c };
  return null;
}

// Rows top -> bottom as the timeline draws them: text, video (highest layer first), audio.
export function trackRows(doc) {
  const text = doc.tracks.filter((t) => t.kind === "text").reverse();
  const video = doc.tracks.filter((t) => t.kind === "video").reverse();
  const audio = doc.tracks.filter((t) => t.kind === "audio");
  return [...text, ...video, ...audio];
}

export const isMainTrack = (doc, track) => {
  const mains = doc.tracks.filter((t) => t.kind === "video" && t.role === "main");
  return mains.length ? mains[0].id === track.id : doc.tracks.find((t) => t.kind === "video")?.id === track.id;
};

export function projectDuration(doc) {
  const main = doc.tracks.find((t) => t.kind === "video" && t.role === "main") || doc.tracks.find((t) => t.kind === "video");
  const end = (tracks) => Math.max(0, ...tracks.flatMap((t) => t.clips.map(clipEnd)));
  if (doc.length_mode === "main" && main && main.clips.length) return end([main]);
  return end(doc.tracks);
}

// Keyframe interpolation.
function ease(kind, p) {
  switch (kind) {
    case "hold": return 0;
    case "ease_in": return p * p;
    case "ease_out": return 1 - (1 - p) * (1 - p);
    case "ease_in_out": return p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
    default: return p;
  }
}
export function kfValue(keys, local, fallback) {
  if (!keys || !keys.length) return fallback;
  if (local <= keys[0].t) return keys[0].v;
  const last = keys[keys.length - 1];
  if (local >= last.t) return last.v;
  for (let i = 0; i < keys.length - 1; i++) {
    const a = keys[i];
    const b = keys[i + 1];
    if (local >= a.t && local < b.t) {
      const p = (local - a.t) / Math.max(1, b.t - a.t);
      return a.v + (b.v - a.v) * ease(a.ease, p);
    }
  }
  return last.v;
}
