// The WebGL2 compositor: draws a scene (see scene.js) from decoded video / image elements onto one canvas.
// Every layer is rendered on its own offscreen texture the way the render builds its filter chain (fit -> effects),
// then placed (rotation, offset, opacity) over what is below; transitions render both sides full-canvas and mix them.
import { FIT_FRAG, FX, FX_FRAG, LAYER_FRAG, VERT, XFADE_FRAG } from "./shaders.js";
import { hexToRgb } from "./mathx.js";

const POOL_LIMIT = 8;

function compile(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(sh);
    gl.deleteShader(sh);
    throw new Error(`shader: ${log}`);
  }
  return sh;
}

function program(gl, frag) {
  const p = gl.createProgram();
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, VERT));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, frag));
  gl.bindAttribLocation(p, 0, "aPos");
  gl.bindAttribLocation(p, 1, "aUv");
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(`link: ${gl.getProgramInfoLog(p)}`);
  const uniforms = {};
  const n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS);
  for (let i = 0; i < n; i++) {
    const info = gl.getActiveUniform(p, i);
    uniforms[info.name] = gl.getUniformLocation(p, info.name);
  }
  return { p, u: uniforms };
}

// Natural cubic spline through the points (what `curves` draws), sampled at 256 inputs.
function splineTable(points) {
  const n = points.length;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const m = new Array(n).fill(0);
  if (n > 2) {
    const a = new Array(n).fill(0);
    const b = new Array(n).fill(1);
    const c = new Array(n).fill(0);
    const d = new Array(n).fill(0);
    for (let i = 1; i < n - 1; i++) {
      const h0 = xs[i] - xs[i - 1];
      const h1 = xs[i + 1] - xs[i];
      a[i] = h0; b[i] = 2 * (h0 + h1); c[i] = h1;
      d[i] = 6 * ((ys[i + 1] - ys[i]) / h1 - (ys[i] - ys[i - 1]) / h0);
    }
    for (let i = 1; i < n; i++) {
      const w = a[i] / b[i - 1];
      b[i] -= w * c[i - 1];
      d[i] -= w * d[i - 1];
    }
    m[n - 1] = d[n - 1] / b[n - 1];
    for (let i = n - 2; i >= 0; i--) m[i] = (d[i] - c[i] * m[i + 1]) / b[i];
    m[0] = 0; m[n - 1] = 0;
  }
  const out = new Uint8Array(256);
  for (let i = 0; i < 256; i++) {
    const x = i / 255;
    let k = 0;
    while (k < n - 2 && x > xs[k + 1]) k++;
    const h = xs[k + 1] - xs[k];
    const A = (xs[k + 1] - x) / h;
    const B = (x - xs[k]) / h;
    const y = A * ys[k] + B * ys[k + 1] + (((A ** 3 - A) * m[k] + (B ** 3 - B) * m[k + 1]) * h * h) / 6;
    out[i] = Math.max(0, Math.min(255, Math.round(y * 255)));
  }
  return out;
}

// curves=preset=vintage
const VINTAGE = [
  [[0, 0.11], [0.42, 0.51], [1, 0.95]],
  [[0, 0], [0.5, 0.48], [1, 1]],
  [[0, 0.22], [0.49, 0.44], [1, 0.8]],
];

export class GLCompositor {
  constructor(canvas) {
    const gl = canvas.getContext("webgl2", { alpha: false, antialias: false, depth: false, stencil: false, premultipliedAlpha: false, powerPreference: "high-performance" });
    if (!gl) throw new Error("WebGL2 is not available");
    this.canvas = canvas;
    this.gl = gl;
    this.maxTex = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    this.progs = {
      fit: program(gl, FIT_FRAG), fx: program(gl, FX_FRAG), layer: program(gl, LAYER_FRAG), xfade: program(gl, XFADE_FRAG),
    };
    this.buf = gl.createBuffer();
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 16, 0);
    gl.enableVertexAttribArray(1);
    gl.vertexAttribPointer(1, 2, gl.FLOAT, false, 16, 8);
    this.pool = new Map();
    this.sources = new Map(); // key -> {tex, w, h, stamp, el}
    this.noise = null;
    this.luts = new Map(); // path -> {tex, lut}
    this.curve = this.makeCurveTexture();
    this.blankTex = this.makeTexture(1, 1, new Uint8Array([0, 0, 0, 255]));
    this.blank3d = this.makeTexture3d(2, new Uint8Array(2 * 2 * 2 * 4));
    this.W = 0;
    this.H = 0;
    this.frameNo = 0;
    this.lost = false;
    canvas.addEventListener("webglcontextlost", (e) => { e.preventDefault(); this.lost = true; if (this.onLost) this.onLost(); });
    canvas.addEventListener("webglcontextrestored", () => { if (this.onRestored) this.onRestored(); });
  }

  // ---------------------------------------------------------------- resources

  makeTexture(w, h, data) {
    const gl = this.gl;
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, data || null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return tex;
  }

  makeTexture3d(n, data) {
    const gl = this.gl;
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_3D, tex);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA, n, n, n, 0, gl.RGBA, gl.UNSIGNED_BYTE, data);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE],
      [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_3D, k, v);
    return tex;
  }

  makeCurveTexture() {
    const tables = VINTAGE.map(splineTable);
    const data = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) {
      data[i * 4] = tables[0][i]; data[i * 4 + 1] = tables[1][i]; data[i * 4 + 2] = tables[2][i]; data[i * 4 + 3] = 255;
    }
    const tex = this.makeTexture(256, 1, data);
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    return tex;
  }

  // rand(x, y) of the dissolve transition, computed the way the filter does (single precision), one byte per pixel.
  noiseTexture(W, H) {
    if (this.noise && this.noise.W === W && this.noise.H === H) return this.noise.tex;
    const gl = this.gl;
    const f = Math.fround;
    const data = new Uint8Array(W * H * 4);
    for (let y = 0; y < H; y++) {
      const by = f(y * f(78.233));
      for (let x = 0; x < W; x++) {
        const r = f(f(Math.sin(f(f(x * f(12.9898)) + by))) * f(43758.545));
        const v = r - Math.floor(r);
        const o = (y * W + x) * 4;
        data[o] = Math.round(v * 255);
        data[o + 3] = 255;
      }
    }
    if (this.noise) gl.deleteTexture(this.noise.tex);
    const tex = this.makeTexture(W, H, data);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    this.noise = { W, H, tex };
    return tex;
  }

  acquire(w, h) {
    const gl = this.gl;
    w = Math.max(1, Math.min(this.maxTex, Math.round(w)));
    h = Math.max(1, Math.min(this.maxTex, Math.round(h)));
    const key = `${w}x${h}`;
    const list = this.pool.get(key);
    if (list && list.length) return list.pop();
    const tex = this.makeTexture(w, h, null);
    const fbo = gl.createFramebuffer();
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    return { tex, fbo, w, h, key };
  }

  release(t) {
    if (!t) return;
    let list = this.pool.get(t.key);
    if (!list) { list = []; this.pool.set(t.key, list); }
    if (list.length >= POOL_LIMIT) {
      this.gl.deleteTexture(t.tex);
      this.gl.deleteFramebuffer(t.fbo);
    } else list.push(t);
  }

  clearPool() {
    const gl = this.gl;
    for (const list of this.pool.values()) for (const t of list) { gl.deleteTexture(t.tex); gl.deleteFramebuffer(t.fbo); }
    this.pool.clear();
  }

  resize(W, H) {
    if (this.W === W && this.H === H) return;
    this.W = W; this.H = H;
    this.canvas.width = W;
    this.canvas.height = H;
    this.clearPool();
  }

  // ---------------------------------------------------------------- sources (video / image elements)

  // Uploads the element's current frame when it has a new one; returns the source entry or null when nothing is decoded yet.
  source(key, el) {
    const gl = this.gl;
    if (!el) return null;
    let s = this.sources.get(key);
    if (s && s.el !== el) { gl.deleteTexture(s.tex); s = null; this.sources.delete(key); }
    const isVideo = el.tagName === "VIDEO";
    const w = isVideo ? el.videoWidth : el.naturalWidth;
    const h = isVideo ? el.videoHeight : el.naturalHeight;
    const ready = isVideo ? el.readyState >= 2 && w > 0 && !el.seeking : el.complete && w > 0;
    const stamp = isVideo ? `${el.currentTime.toFixed(4)}|${el.__glFrame || ""}` : "img";
    if (ready && (!s || s.stamp !== stamp)) {
      if (!s) { s = { tex: this.makeTexture(1, 1, null), w: 0, h: 0, stamp: "", el }; this.sources.set(key, s); }
      gl.bindTexture(gl.TEXTURE_2D, s.tex);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      try {
        if (!isVideo && (w > this.maxTex || h > this.maxTex)) {
          const k = Math.min(this.maxTex / w, this.maxTex / h);
          const c = document.createElement("canvas");
          c.width = Math.floor(w * k); c.height = Math.floor(h * k);
          c.getContext("2d").drawImage(el, 0, 0, c.width, c.height);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, c);
        } else gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, el);
        s.w = w; s.h = h; s.stamp = stamp; s.at = this.frameNo;
      } catch {
        return s && s.w ? s : null; // frame not decodable yet
      }
    }
    return s && s.w ? s : null;
  }

  dropUnused(liveKeys) {
    for (const [k, s] of this.sources) {
      if (!liveKeys.has(k) && this.frameNo - (s.at || 0) > 90) { this.gl.deleteTexture(s.tex); this.sources.delete(k); }
    }
  }

  lutTexture(path, entry) {
    let t = this.luts.get(path);
    if (!t && entry.lut) {
      t = { tex: this.makeTexture3d(entry.lut.size, entry.lut.data), lut: entry.lut };
      this.luts.set(path, t);
    }
    return t || null;
  }

  // ---------------------------------------------------------------- drawing primitives

  bind(target) {
    const gl = this.gl;
    if (target && target.fbo) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, target.fbo);
      gl.viewport(0, 0, target.w, target.h);
    } else {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, this.W, this.H);
    }
  }

  // verts: 4 corners [x, y, u, v] in target pixels (y down) as a triangle strip.
  draw(prog, target, verts, setup) {
    const gl = this.gl;
    this.bind(target);
    gl.useProgram(prog.p);
    const w = target && target.fbo ? target.w : this.W;
    const h = target && target.fbo ? target.h : this.H;
    gl.uniform2f(prog.u.uRes, w, h);
    gl.uniform1f(prog.u.uFlip, target && target.fbo ? 1 : -1);
    if (setup) setup(prog.u);
    gl.bindVertexArray(this.vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(verts.flat()), gl.DYNAMIC_DRAW);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  full(prog, target, setup) {
    const w = target && target.fbo ? target.w : this.W;
    const h = target && target.fbo ? target.h : this.H;
    this.draw(prog, target, [[0, 0, 0, 0], [w, 0, 1, 0], [0, h, 0, 1], [w, h, 1, 1]], setup);
  }

  tex(unit, texture, target = this.gl.TEXTURE_2D) {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(target, texture);
  }

  // One effect pass: src -> a new texture of dw x dh. `bindExtra` may bind more textures / set uniforms.
  fxPass(op, src, dw, dh, mat, p0, p1, p2, extra) {
    const gl = this.gl;
    const dst = this.acquire(dw, dh);
    this.gl.disable(gl.BLEND);
    this.full(this.progs.fx, dst, (u) => {
      this.tex(0, src.tex);
      gl.uniform1i(u.uTex, 0);
      this.tex(1, this.blankTex);
      gl.uniform1i(u.uTex2, 1);
      this.tex(2, this.curve);
      gl.uniform1i(u.uCurve, 2);
      this.tex(3, this.blank3d, gl.TEXTURE_3D);
      gl.uniform1i(u.uLut, 3);
      gl.uniform1i(u.uOp, op);
      gl.uniform2f(u.uSize, dw, dh);
      gl.uniform1f(u.uMat, mat);
      gl.uniform4f(u.uP0, ...(p0 || [0, 0, 0, 0]));
      gl.uniform4f(u.uP1, ...(p1 || [0, 0, 0, 0]));
      gl.uniform4f(u.uP2, ...(p2 || [0, 0, 0, 0]));
      gl.uniform2f(u.uDir, 0, 0);
      if (extra) extra(u);
    });
    return dst;
  }

  // gblur: separable Gaussian. Wide blurs run on a reduced copy (box-filtered halvings) and are scaled back up.
  gaussian(src, sigma, mat) {
    let cur = src;
    let cw = src.w;
    let ch = src.h;
    let s = sigma;
    const owned = [];
    while (s > 4 && cw > 8 && ch > 8) {
      const nw = Math.max(1, Math.ceil(cw / 2));
      const nh = Math.max(1, Math.ceil(ch / 2));
      cur = this.fxPass(FX.COPY, cur, nw, nh, mat);
      owned.push(cur);
      cw = nw; ch = nh; s /= 2;
    }
    const h = this.fxPass(FX.BLUR, cur, cw, ch, mat, [s, 0, 0, 0], null, null, (u) => this.gl.uniform2f(u.uDir, 1 / cw, 0));
    const v = this.fxPass(FX.BLUR, h, cw, ch, mat, [s, 0, 0, 0], null, null, (u) => this.gl.uniform2f(u.uDir, 0, 1 / ch));
    this.release(h);
    let out = v;
    if (cw !== src.w || ch !== src.h) {
      out = this.fxPass(FX.COPY, v, src.w, src.h, mat);
      this.release(v);
    }
    for (const t of owned) this.release(t);
    return out;
  }

  // gblur on a yuv picture: luma with sigma, chroma planes (half size) with the same sigma, so twice as wide on screen.
  blurLayer(src, sigma, mat) {
    const a = this.gaussian(src, sigma, mat);
    const b = this.gaussian(src, sigma * 2, mat);
    const out = this.fxPass(FX.COMBINE, a, src.w, src.h, mat, null, null, null, (u) => { this.tex(1, b.tex); this.gl.uniform1i(u.uTex2, 1); });
    this.release(a);
    this.release(b);
    return out;
  }

  // ---------------------------------------------------------------- one layer

  // The layer's own picture: fit (+ blurred fill) then its effects, as a texture w x h. Returns null without a decoded source.
  layerTexture(layer, src, ctx) {
    const gl = this.gl;
    const mat = layer.matrix === 709 ? 1 : 0;
    const fit = layer.fit;
    let bg = null;
    if (fit.mode === "blur") {
      const raw = this.acquire(layer.fitW, layer.fitH);
      gl.disable(gl.BLEND);
      this.full(this.progs.fit, raw, (u) => this.setFit(u, src, { win: fit.bgWin, fg: [0, 0, 1, 1], zoom: 1 }, null));
      const blurred = this.blurLayer(raw, fit.sigma, mat);
      this.release(raw);
      bg = this.fxPass(FX.EQ, blurred, blurred.w, blurred.h, mat, [-0.06, 1, 0.9, 1]);
      this.release(blurred);
    }
    let cur = this.acquire(layer.fitW, layer.fitH);
    gl.disable(gl.BLEND);
    this.full(this.progs.fit, cur, (u) => this.setFit(u, src, fit, bg));
    this.release(bg);
    for (const fx of layer.effects) {
      const next = this.applyEffect(fx, cur, mat, ctx);
      if (next && next !== cur) { this.release(cur); cur = next; }
    }
    const m = layer.mask;
    if (m) {
      // multiplies the alpha of the whole picture (with fit=blur that includes the blurred fill), before rotation and opacity
      const masked = this.fxPass(FX.MASK, cur, cur.w, cur.h, mat, [m.cx, m.cy, m.rx, m.ry], [m.feather, m.radius, m.invert ? 1 : 0, m.ellipse ? 1 : 0]);
      this.release(cur);
      cur = masked;
    }
    return cur;
  }

  setFit(u, src, fit, bg) {
    const gl = this.gl;
    this.tex(0, src.tex);
    gl.uniform1i(u.uSrc, 0);
    this.tex(1, bg ? bg.tex : this.blankTex);
    gl.uniform1i(u.uBg, 1);
    gl.uniform2f(u.uSrcSize, src.w, src.h);
    gl.uniform4f(u.uWin, ...fit.win);
    gl.uniform4f(u.uFg, ...fit.fg);
    gl.uniform1f(u.uZoom, fit.zoom || 1);
    gl.uniform1f(u.uHasBg, bg ? 1 : 0);
  }

  applyEffect(fx, cur, mat, ctx) {
    const gl = this.gl;
    const p = fx.p;
    const { w, h } = cur;
    switch (fx.type) {
      case "eq": return this.fxPass(FX.EQ, cur, w, h, mat, [p.brightness, p.contrast, p.saturation, p.gamma]);
      case "grayscale": return this.fxPass(FX.GRAY, cur, w, h, mat);
      case "sepia": return this.fxPass(FX.SEPIA, cur, w, h, mat);
      case "vintage": return this.fxPass(FX.CURVES, cur, w, h, mat, [3.07, ctx.frame % 97, 0, 0]);
      case "warm": {
        const a = p.amount;
        return this.fxPass(FX.BALANCE, cur, w, h, mat, [0.12 * a, 0.03 * a, -0.12 * a, 0], [0.08 * a, 0, -0.08 * a, 0], [0, 0, 0, 0]);
      }
      case "cool": {
        const a = p.amount;
        return this.fxPass(FX.BALANCE, cur, w, h, mat, [-0.1 * a, 0, 0.12 * a, 0], [-0.06 * a, 0, 0.08 * a, 0], [0, 0, 0, 0]);
      }
      case "contrast_pop": {
        const a = p.amount;
        const e = this.fxPass(FX.EQ, cur, w, h, mat, [0, 1 + 0.25 * a, 1 + 0.35 * a, 1]);
        const out = this.fxPass(FX.UNSHARP, e, w, h, mat, [0.6 * a, 0, 0, 0]);
        this.release(e);
        return out;
      }
      case "vignette": return this.fxPass(FX.VIGNETTE, cur, w, h, mat, [0.2 + 0.6 * p.strength, 0, 0, 0]);
      case "sharpen": return this.fxPass(FX.UNSHARP, cur, w, h, mat, [p.amount, 0, 0, 0]);
      case "blur": return this.blurLayer(cur, p.radius, mat);
      case "pixelate": {
        const s = Math.max(1, Math.trunc(p.size));
        const nx = Math.max(1, Math.trunc(w / s));
        const ny = Math.max(1, Math.trunc(h / s));
        return this.fxPass(FX.PIXELATE, cur, nx * s, ny * s, mat, [nx, ny, s, 0], [w, h, 0, 0]);
      }
      case "chromakey": {
        const [r, g, b] = hexToRgb(p.color, [0, 1, 0]);
        return this.fxPass(FX.KEY, cur, w, h, mat, [r, g, b, p.similarity], [p.blend, 0, 0, 0]);
      }
      case "hflip": return this.fxPass(FX.FLIPH, cur, w, h, mat);
      case "vflip": return this.fxPass(FX.FLIPV, cur, w, h, mat);
      case "lut": {
        const entry = p.file && ctx.lut ? ctx.lut(p.file) : null;
        const t = entry && entry.state === "ready" ? this.lutTexture(p.file, entry) : null;
        if (!t) return null;
        return this.fxPass(FX.LUT, cur, w, h, mat, [t.lut.size, 0, 0, 0], [...t.lut.dmin, 0], [...t.lut.dmax, 0], (u) => {
          this.tex(3, t.tex, gl.TEXTURE_3D);
          gl.uniform1i(u.uLut, 3);
        });
      }
      default: return null; // denoise (hqdn3d) is not drawn by the preview
    }
  }

  // Place a finished layer texture on `target` (the canvas, or one side of a transition).
  place(layer, tex, target, blend) {
    const gl = this.gl;
    const cx = layer.x + layer.ow / 2;
    const cy = layer.y + layer.oh / 2;
    const w = layer.w;
    const h = layer.h;
    let hw = w / 2;
    let hh = h / 2;
    let u0 = 0;
    let v0 = 0;
    let u1 = 1;
    let v1 = 1;
    const rotated = layer.rotated && Math.abs(layer.angle) > 1e-6;
    if (rotated) {
      hw += 1; hh += 1;
      u0 = -1 / w; u1 = 1 + 1 / w; v0 = -1 / h; v1 = 1 + 1 / h;
    }
    const c = Math.cos(layer.angle);
    const s = Math.sin(layer.angle);
    const corner = (x, y, u, v) => [cx + x * c - y * s, cy + x * s + y * c, u, v];
    const verts = [corner(-hw, -hh, u0, v0), corner(hw, -hh, u1, v0), corner(-hw, hh, u0, v1), corner(hw, hh, u1, v1)];
    if (blend) {
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    } else gl.disable(gl.BLEND);
    this.draw(this.progs.layer, target, verts, (u) => {
      this.tex(0, tex.tex);
      gl.uniform1i(u.uTex, 0);
      gl.uniform2f(u.uSize, w, h);
      gl.uniform1f(u.uAlpha, layer.alpha);
      gl.uniform1f(u.uAA, rotated ? 1 : 0);
    });
  }

  drawLayer(layer, sources, target, blend, ctx) {
    const src = this.source(layer.key, sources.get(layer.key));
    if (!src) return false;
    const tex = this.layerTexture(layer, src, ctx);
    if (!tex) return false;
    this.place(layer, tex, target, blend);
    this.release(tex);
    return true;
  }

  // ---------------------------------------------------------------- a frame

  // sources: Map(layer key -> <video> | <img>). ctx: {lut(path) -> cube entry, frame}.
  render(scene, sources, ctx = {}) {
    const gl = this.gl;
    if (this.lost || gl.isContextLost()) return { drawn: 0, missing: [] };
    this.frameNo++;
    const { W, H } = scene;
    this.resize(W, H);
    const rc = { ...ctx, frame: this.frameNo };
    const live = new Set();
    const missing = [];
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, W, H);
    gl.disable(gl.BLEND);
    gl.clearColor(scene.bg[0], scene.bg[1], scene.bg[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    for (const item of scene.items) {
      if (item.kind === "layer") {
        live.add(item.layer.key);
        if (!this.drawLayer(item.layer, sources, null, true, rc)) missing.push(item.layer.key);
        continue;
      }
      const sides = [item.a, item.b].map((layer) => {
        const side = this.acquire(W, H);
        gl.bindFramebuffer(gl.FRAMEBUFFER, side.fbo);
        gl.viewport(0, 0, W, H);
        gl.disable(gl.BLEND);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        if (layer) {
          live.add(layer.key);
          if (!this.drawLayer(layer, sources, side, false, rc)) missing.push(layer.key);
        }
        return side;
      });
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      const noise = item.type === 1 ? this.noiseTexture(W, H) : this.blankTex;
      this.full(this.progs.xfade, null, (u) => {
        this.tex(0, sides[0].tex);
        gl.uniform1i(u.uA, 0);
        this.tex(1, sides[1].tex);
        gl.uniform1i(u.uB, 1);
        this.tex(2, noise);
        gl.uniform1i(u.uNoise, 2);
        gl.uniform1i(u.uType, item.type);
        gl.uniform1f(u.uP, item.progress);
        gl.uniform2f(u.uSize, W, H);
      });
      for (const s of sides) this.release(s);
    }
    gl.disable(gl.BLEND);
    this.dropUnused(live);
    return { drawn: scene.items.length, missing };
  }

  dispose() {
    const gl = this.gl;
    this.clearPool();
    for (const s of this.sources.values()) gl.deleteTexture(s.tex);
    this.sources.clear();
    for (const t of this.luts.values()) gl.deleteTexture(t.tex);
    this.luts.clear();
    const ext = gl.getExtension("WEBGL_lose_context");
    if (ext) ext.loseContext();
  }
}

export function glSupported() {
  try {
    const c = document.createElement("canvas");
    const gl = c.getContext("webgl2");
    const ok = !!gl;
    const ext = gl && gl.getExtension("WEBGL_lose_context");
    if (ext) ext.loseContext();
    return ok;
  } catch {
    return false;
  }
}
