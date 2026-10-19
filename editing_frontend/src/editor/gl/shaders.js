// GLSL for the WebGL preview. Every formula here was fitted against what ffmpeg 6.1 produces for the render's filters
// (render/filters.py, render/compiler.py, xfade): see the comments for the rule each one reproduces.
// Conventions: images are y-down (v = 0 is the top row); colours are straight (non-premultiplied) RGBA.

export const VERT = `#version 300 es
in vec2 aPos;
in vec2 aUv;
uniform vec2 uRes;
uniform float uFlip;
out vec2 vUv;
void main() {
  vec2 c = aPos / uRes * 2.0 - 1.0;
  gl_Position = vec4(c.x, c.y * uFlip, 0.0, 1.0);
  vUv = aUv;
}`;

const HEAD = `#version 300 es
precision highp float;
precision highp int;
precision highp sampler3D;
in vec2 vUv;
out vec4 outColor;
`;

// Picture of one layer: crop / fit / focus window, zoom (scale keyframes), and for fit=blur the blurred fill behind it.
export const FIT_FRAG = `${HEAD}
uniform sampler2D uSrc;
uniform sampler2D uBg;
uniform vec2 uSrcSize;
uniform vec4 uWin;   // part of the source shown (u0, v0, u1, v1)
uniform vec4 uFg;    // where the picture sits in the layer (x0, y0, x1, y1), 0..1
uniform float uZoom; // zoompan: >= 1, about the centre
uniform float uHasBg;
void main() {
  vec2 p = (vUv - 0.5) / uZoom + 0.5;
  vec4 col = vec4(0.0, 0.0, 0.0, 1.0);
  if (uHasBg > 0.5) col = vec4(texture(uBg, p).rgb, 1.0);
  if (p.x >= uFg.x && p.x < uFg.z && p.y >= uFg.y && p.y < uFg.w) {
    vec2 q = (p - uFg.xy) / (uFg.zw - uFg.xy);
    vec2 h = 0.5 / uSrcSize;
    vec2 suv = clamp(mix(uWin.xy, uWin.zw, q), uWin.xy + h, uWin.zw - h);
    col = vec4(texture(uSrc, suv).rgb, 1.0);
  }
  outColor = col;
}`;

// Colour / look effects, one per pass. uMat: 0 = BT.601, 1 = BT.709 (the matrix the picture was decoded with).
export const FX = {
  COPY: 0, EQ: 1, GRAY: 2, SEPIA: 3, CURVES: 4, BALANCE: 5, VIGNETTE: 6, UNSHARP: 7, PIXELATE: 8, KEY: 9, FLIPH: 10, FLIPV: 11,
  LUT: 12, BLUR: 13, COMBINE: 14, MASK: 15,
};

export const FX_FRAG = `${HEAD}
uniform sampler2D uTex;
uniform sampler2D uTex2;
uniform sampler2D uCurve;
uniform sampler3D uLut;
uniform int uOp;
uniform vec2 uSize;
uniform float uMat;
uniform vec4 uP0;
uniform vec4 uP1;
uniform vec4 uP2;
uniform vec2 uDir;

vec3 kw() { return uMat > 0.5 ? vec3(0.2126, 0.7152, 0.0722) : vec3(0.299, 0.587, 0.114); }

// code values: Y 16..235, Cb / Cr 16..240 (what the 8-bit filters work on)
vec3 toYuv(vec3 c) {
  vec3 k = kw();
  float y = dot(k, c);
  return vec3(16.0 + 219.0 * y, 128.0 + 224.0 * (c.b - y) / (2.0 * (1.0 - k.z)), 128.0 + 224.0 * (c.r - y) / (2.0 * (1.0 - k.x)));
}
vec3 fromYuv(vec3 v) {
  vec3 k = kw();
  float y = (v.x - 16.0) / 219.0;
  float cb = (v.y - 128.0) / 224.0;
  float cr = (v.z - 128.0) / 224.0;
  float r = y + 2.0 * (1.0 - k.x) * cr;
  float b = y + 2.0 * (1.0 - k.z) * cb;
  float g = (y - k.x * r - k.z * b) / k.y;
  return clamp(vec3(r, g, b), 0.0, 1.0);
}
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }

void main() {
  vec4 c = texture(uTex, vUv);
  vec2 px = floor(vUv * uSize);
  if (uOp == 1) {
    // eq: contrast / brightness on luma code values around 128 (the filter's lookup table is c*(Y-128)+127+255*b),
    // gamma on luma, saturation on the chroma around 128; a neutral setting leaves a plane untouched.
    vec3 v = toYuv(c.rgb);
    float lin = (uP0.y == 1.0 && uP0.x == 0.0) ? v.x : uP0.y * (v.x - 128.0) + 127.0 + 255.0 * uP0.x;
    lin = clamp(lin, 0.0, 255.0);
    if (uP0.w != 1.0) lin = 255.0 * pow(lin / 255.0, 1.0 / uP0.w);
    vec2 cc = v.yz;
    if (uP0.z != 1.0) cc = clamp(uP0.z * (cc - 128.0) + 127.0, 0.0, 255.0);
    outColor = vec4(fromYuv(vec3(lin, cc)), c.a);
  } else if (uOp == 2) {
    outColor = vec4(vec3(dot(kw(), c.rgb)), c.a);                       // hue=s=0
  } else if (uOp == 3) {
    outColor = vec4(clamp(vec3(dot(vec3(.393, .769, .189), c.rgb), dot(vec3(.349, .686, .168), c.rgb), dot(vec3(.272, .534, .131), c.rgb)), 0.0, 1.0), c.a);
  } else if (uOp == 4) {
    // curves=preset=vintage (a 256-entry table per channel) then temporal noise (about 3 code values of grain)
    vec3 q = clamp(c.rgb, 0.0, 1.0) * 255.0 + 0.5;
    vec3 m = vec3(texture(uCurve, vec2(q.r / 256.0, 0.5)).r, texture(uCurve, vec2(q.g / 256.0, 0.5)).g, texture(uCurve, vec2(q.b / 256.0, 0.5)).b);
    vec3 v = toYuv(m);
    if (uP0.x > 0.0) {
      vec2 s = px + uP0.y * vec2(17.31, 5.77);
      vec3 n = vec3(hash(s) + hash(s + 11.1) - 1.0, hash(s + 23.7) + hash(s + 31.3) - 1.0, hash(s + 47.9) + hash(s + 59.1) - 1.0);
      v += n * uP0.x;
    }
    outColor = vec4(fromYuv(v), c.a);
  } else if (uOp == 5) {
    // colorbalance: shadows / midtones / highlights weights from the lightness (max + min of the channels)
    float l = max(max(c.r, c.g), c.b) + min(min(c.r, c.g), c.b);
    float a = 4.0;
    float b0 = 0.333;
    float sc = 0.7;
    float ws = clamp((b0 - l) * a + 0.5, 0.0, 1.0) * sc;
    float wm = clamp((l - b0) * a + 0.5, 0.0, 1.0) * clamp((1.0 - l - b0) * a + 0.5, 0.0, 1.0) * sc;
    float wh = clamp((l + b0 - 1.0) * a + 0.5, 0.0, 1.0) * sc;
    outColor = vec4(clamp(c.rgb + uP0.xyz * ws + uP1.xyz * wm + uP2.xyz * wh, 0.0, 1.0), c.a);
  } else if (uOp == 6) {
    // vignette: luma and chroma scaled by cos(angle * distance / half diagonal)^4
    vec2 d = px - uSize * 0.5;
    float f = pow(max(0.0, cos(uP0.x * length(d) / length(uSize * 0.5))), 4.0);
    vec3 v = toYuv(c.rgb);
    v.x *= f;
    v.yz = 128.0 + (v.yz - 128.0) * f;
    outColor = vec4(fromYuv(v), c.a);
  } else if (uOp == 7) {
    // unsharp=5:5:amount:5:5:0: luma only, 5x5 binomial blur
    vec2 st = 1.0 / uSize;
    vec3 acc = vec3(0.0);
    float wsum = 0.0;
    for (int j = -2; j <= 2; j++) {
      for (int i = -2; i <= 2; i++) {
        float w = float((i == 0 ? 6 : (abs(i) == 1 ? 4 : 1)) * (j == 0 ? 6 : (abs(j) == 1 ? 4 : 1)));
        acc += w * texture(uTex, vUv + vec2(float(i), float(j)) * st).rgb;
        wsum += w;
      }
    }
    float dy = dot(kw(), c.rgb - acc / wsum);
    outColor = vec4(clamp(c.rgb + vec3(uP0.x * dy), 0.0, 1.0), c.a);
  } else if (uOp == 8) {
    // scale=iw/s,scale=iw*s with flags=neighbor: every block shows the pixel at its centre. The filter scales each plane by the
    // same factor, and the chroma planes are half size, so the colour comes in blocks twice as wide as the brightness.
    vec2 blk = floor(px / uP0.z);
    vec2 sp = floor(((blk + 0.5) / uP0.xy) * uP1.xy);
    vec4 lt = texture(uTex, (sp + 0.5) / uP1.xy);
    float yy = toYuv(lt.rgb).x;
    vec2 cSrc = ceil(uP1.xy * 0.5);
    vec2 cDst = ceil(uP0.xy * 0.5);
    vec2 cOut = ceil(uP0.xy * uP0.z * 0.5);
    vec2 cb = floor((floor(px * 0.5) + 0.5) * cDst / cOut);
    vec2 cp = floor((cb + 0.5) * cSrc / cDst) * 2.0;
    vec3 c4 = (texture(uTex, (cp + 0.5) / uP1.xy).rgb + texture(uTex, (cp + vec2(1.5, 0.5)) / uP1.xy).rgb
             + texture(uTex, (cp + vec2(0.5, 1.5)) / uP1.xy).rgb + texture(uTex, (cp + 1.5) / uP1.xy).rgb) * 0.25;
    outColor = vec4(fromYuv(vec3(yy, toYuv(c4).yz)), lt.a);
  } else if (uOp == 9) {
    // colorkey: distance in RGB, alpha from similarity / blend
    vec3 dd = c.rgb - uP0.xyz;
    float diff = sqrt(dot(dd, dd) / 3.0);
    float a = uP1.x > 0.0001 ? clamp((diff - uP0.w) / uP1.x, 0.0, 1.0) : (diff > uP0.w ? 1.0 : 0.0);
    outColor = vec4(c.rgb, c.a * a);
  } else if (uOp == 10) {
    outColor = texture(uTex, vec2(1.0 - vUv.x, vUv.y));
  } else if (uOp == 11) {
    outColor = texture(uTex, vec2(vUv.x, 1.0 - vUv.y));
  } else if (uOp == 12) {
    vec3 q = clamp((c.rgb - uP1.xyz) / max(uP2.xyz - uP1.xyz, vec3(1e-6)), 0.0, 1.0);
    float n = uP0.x;
    outColor = vec4(texture(uLut, (q * (n - 1.0) + 0.5) / n).rgb, c.a);
  } else if (uOp == 13) {
    // one direction of a Gaussian; uP0.x = sigma in pixels of this texture, uDir = texel step along the axis
    float sg = max(uP0.x, 0.01);
    int rad = min(int(ceil(3.0 * sg)), 24);
    vec4 acc = c;
    float ws = 1.0;
    for (int i = 1; i <= 24; i++) {
      if (i > rad) break;
      float w = exp(-float(i * i) / (2.0 * sg * sg));
      acc += w * (texture(uTex, vUv + uDir * float(i)) + texture(uTex, vUv - uDir * float(i)));
      ws += 2.0 * w;
    }
    outColor = acc / ws;
  } else if (uOp == 14) {
    // gblur blurs the chroma planes (half resolution) with the same sigma: twice as wide on screen
    vec3 a = toYuv(c.rgb);
    vec3 b = toYuv(texture(uTex2, vUv).rgb);
    outColor = vec4(fromYuv(vec3(a.x, b.y, b.z)), c.a);
  } else if (uOp == 15) {
    // shape mask (compiler.mask_alpha_expr): coverage from a signed distance to the outline, in pixel units, with a linear soft
    // edge of uP1.x pixels centred on it. uP0 = centre and half sizes, uP1 = (feather, corner radius, invert, ellipse).
    vec2 d0 = px - uP0.xy;
    float dist;
    if (uP1.w > 0.5) {
      dist = (length(d0 / uP0.zw) - 1.0) * min(uP0.z, uP0.w);
    } else {
      vec2 e = abs(d0) - uP0.zw + uP1.y;
      dist = length(max(e, 0.0)) + min(max(e.x, e.y), 0.0) - uP1.y;
    }
    float cover = clamp(0.5 - dist / uP1.x, 0.0, 1.0);
    if (uP1.z > 0.5) cover = 1.0 - cover;
    outColor = vec4(c.rgb, c.a * cover);
  } else {
    outColor = c;
  }
}`;

// A finished layer onto the picture (or onto one side of a transition): position, rotation, opacity.
export const LAYER_FRAG = `${HEAD}
uniform sampler2D uTex;
uniform vec2 uSize;
uniform float uAlpha;
uniform float uAA;
void main() {
  vec4 c = texture(uTex, vUv);
  float aa = 1.0;
  if (uAA > 0.5) {
    vec2 d = min(vUv, 1.0 - vUv) * uSize;
    aa = clamp(min(d.x, d.y) + 0.5, 0.0, 1.0);
  }
  outColor = vec4(c.rgb, c.a * uAlpha * aa);
}`;

// ffmpeg's xfade for the 20 transition types. p is the filter's progress: 1 at the start (all outgoing), 0 at the end.
// Both sides are full-canvas straight RGBA; the filter mixes the four planes alike, so a transparent side counts as black.
export const XFADE_FRAG = `${HEAD}
uniform sampler2D uA;
uniform sampler2D uB;
uniform sampler2D uNoise;
uniform int uType;
uniform float uP;
uniform vec2 uSize;

float ss(float t) { t = clamp(t, 0.0, 1.0); return t * t * (3.0 - 2.0 * t); }
vec4 A(vec2 px) { return texture(uA, (px + 0.5) / uSize); }
vec4 B(vec2 px) { return texture(uB, (px + 0.5) / uSize); }
vec4 hbox(sampler2D t, vec2 px, float size) {
  float n = min(size, 48.0);
  float stride = size / n;
  vec4 acc = vec4(0.0);
  for (int i = 0; i < 48; i++) {
    if (float(i) >= n) break;
    acc += texture(t, (vec2(px.x + (float(i) + 0.5) * stride, px.y + 0.5)) / uSize);
  }
  return acc / n;
}

void main() {
  float W = uSize.x;
  float H = uSize.y;
  vec2 px = floor(vUv * uSize);
  float x = px.x;
  float y = px.y;
  float p = uP;
  float d = length(px - uSize * 0.5) / length(uSize * 0.5);
  vec4 r;
  if (uType == 0) {
    r = A(px) * p + B(px) * (1.0 - p);
  } else if (uType == 1) {
    // dissolve: a random pick per pixel (the filter's own noise, reproduced); the colour planes of the real thing are noisier still
    float zf = texture(uNoise, vUv).r * 2.0 + p * 2.0 - 1.5;
    r = zf >= 0.5 ? A(px) : B(px);
  } else if (uType == 2 || uType == 3) {
    // fadeblack / fadewhite: out of A, a flat colour, into B (weights fitted to the filter's curves)
    vec4 bg = uType == 2 ? vec4(0.0, 0.0, 0.0, 1.0) : vec4(1.0, 1.0, 1.0, 1.0);
    float wa = ss((p - 0.8203) / 0.1859);
    float wb = ss((0.8043 - p) / 0.8675);
    r = A(px) * wa + B(px) * wb + bg * (1.0 - wa - wb);
  } else if (uType == 4) {
    float s = W * (1.0 - p);
    r = (x + s < W) ? A(vec2(x + s, y)) : B(vec2(x + s - W, y));
  } else if (uType == 5) {
    float s = W * (1.0 - p);
    r = (x - s >= 0.0) ? A(vec2(x - s, y)) : B(vec2(x - s + W, y));
  } else if (uType == 6) {
    float s = H * (1.0 - p);
    r = (y + s < H) ? A(vec2(x, y + s)) : B(vec2(x, y + s - H));
  } else if (uType == 7) {
    float s = H * (1.0 - p);
    r = (y - s >= 0.0) ? A(vec2(x, y - s)) : B(vec2(x, y - s + H));
  } else if (uType == 8) {
    r = x > W * p ? B(px) : A(px);
  } else if (uType == 9) {
    r = x > W * (1.0 - p) ? A(px) : B(px);
  } else if (uType == 10) {
    r = y > H * p ? B(px) : A(px);
  } else if (uType == 11) {
    r = y > H * (1.0 - p) ? A(px) : B(px);
  } else if (uType == 12) {
    float wb = ss(2.5 - 3.0 * p - d);
    r = B(px) * wb + A(px) * (1.0 - wb);
  } else if (uType == 13) {
    float wb = ss(d + 1.5 - 3.0 * p);
    r = B(px) * wb + A(px) * (1.0 - wb);
  } else if (uType == 14) {
    // zoomin: the outgoing picture is magnified towards its centre while the incoming one fades in
    float zf = ss((p - 0.5) / 0.5);
    vec2 u = (px - uSize * 0.5) * zf + uSize * 0.5;
    float wb = ss((0.5 - p) / 0.5);
    r = A(clamp(floor(u + 0.5), vec2(0.0), uSize - 1.0)) * (1.0 - wb) + B(px) * wb;   // nearest pixel, like the filter
  } else if (uType == 15) {
    float dd = min(p, 1.0 - p);
    float dist = ceil(dd * 50.0) / 50.0;
    float sq = 2.0 * dist * min(W, H) / 20.0;
    vec2 sp = px;
    if (dist > 0.0) sp = vec2(min((floor(x / sq) + 0.5) * sq, W - 1.0), min((floor(y / sq) + 0.5) * sq, H - 1.0));
    sp = floor(sp);
    r = A(sp) * p + B(sp) * (1.0 - p);
  } else if (uType == 16) {
    float sm = atan(x - W * 0.5, y - H * 0.5) - (p - 0.5) * (3.14159265 * 2.5);
    float wb = ss(sm);
    r = B(px) * wb + A(px) * (1.0 - wb);
  } else if (uType == 17) {
    float wb = ss(1.0 + x / W - p * 2.0);
    r = B(px) * wb + A(px) * (1.0 - wb);
  } else if (uType == 18) {
    float wb = ss(1.0 + (W - x) / W - p * 2.0);
    r = B(px) * wb + A(px) * (1.0 - wb);
  } else {
    // hblur: a box blur that grows to the middle of the transition and shrinks again, while the pictures cross-fade
    float prog = p <= 0.5 ? p * 2.0 : (1.0 - p) * 2.0;
    float size = 1.0 + floor(W * prog / 2.0);
    r = hbox(uA, px, size) * p + hbox(uB, px, size) * (1.0 - p);
  }
  outColor = r;
}`;
