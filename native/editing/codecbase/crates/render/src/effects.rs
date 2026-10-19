//! CPU reference implementations of the video effects in `lumiere_codecbase_project::effect`.
//!
//! Effects operate in place on a layer [`Image`] (premultiplied linear f32). Spatial parameters are
//! authored in full-resolution clip pixels; `FxCtx::px_scale` converts them to the working image
//! (so ½/¼ playback resolution renders the same look). Colour-grading math runs on display-encoded
//! straight colour where artists expect it (contrast, levels, posterize), linear light elsewhere.

use lumiere_codecbase_color::{hsl_to_rgb, linear_to_srgb, luma709, rgb_to_hsl, srgb_to_linear};
use lumiere_codecbase_geom::{Affine, Vec2};
use lumiere_codecbase_project::{EffectInstance, ParamValue};
use lumiere_codecbase_time::Tick;
use rayon::prelude::*;

use crate::image::Image;

/// Context for evaluating an effect at a time.
pub struct FxCtx<'a> {
    /// Media time used to evaluate keyframes.
    pub t: Tick,
    /// Working-image pixels per full-resolution clip pixel.
    pub px_scale: f32,
    /// Seconds since the clip start (for animated generators such as Noise/Strobe).
    pub seconds: f64,
    /// Formatted sequence timecode (for the Timecode effect).
    pub timecode: &'a str,
    pub clip_name: &'a str,
    /// The project (LUT library for Lumetri); `None` in isolated effect tests.
    pub project: Option<&'a lumiere_codecbase_project::Project>,
    /// The clip's surroundings (other frames, other tracks, sequence geometry) for temporal,
    /// track-reading and reframing effects; `None` in isolated tests and adjustment layers.
    pub env: Option<&'a dyn crate::vfx::FxEnv>,
}

fn f(e: &EffectInstance, id: &str, cx: &FxCtx) -> f32 {
    e.f64_at(id, cx.t) as f32
}
fn b(e: &EffectInstance, id: &str) -> bool {
    e.param(id).and_then(|p| p.value.as_bool()).unwrap_or(false)
}
/// A section switch (missing in older projects = on).
fn on(e: &EffectInstance, id: &str) -> bool {
    e.param(id).and_then(|p| p.value.as_bool()).unwrap_or(true)
}
fn text<'e>(e: &'e EffectInstance, id: &str) -> &'e str {
    match e.param(id).map(|p| &p.value) {
        Some(ParamValue::Text(s)) => s,
        _ => "",
    }
}
fn choice(e: &EffectInstance, id: &str) -> u32 {
    match e.param(id).map(|p| &p.value) {
        Some(ParamValue::Choice(c)) => *c,
        _ => 0,
    }
}
fn color(e: &EffectInstance, id: &str, cx: &FxCtx) -> [f32; 4] {
    e.param(id).and_then(|p| p.value_at(cx.t).as_color()).unwrap_or([1.0; 4])
}
/// A point param in working-image pixels; NaN components default to the image centre.
fn point(e: &EffectInstance, id: &str, cx: &FxCtx, img: &Image) -> Vec2 {
    let v = e.param(id).map(|p| p.vec2_at(cx.t)).unwrap_or(Vec2::new(f64::NAN, f64::NAN));
    Vec2::new(
        if v.x.is_nan() { img.w as f64 / 2.0 } else { v.x * cx.px_scale as f64 },
        if v.y.is_nan() { img.h as f64 / 2.0 } else { v.y * cx.px_scale as f64 },
    )
}

#[inline]
pub(crate) fn enc(c: [f32; 3]) -> [f32; 3] {
    [linear_to_srgb(c[0].max(0.0)), linear_to_srgb(c[1].max(0.0)), linear_to_srgb(c[2].max(0.0))]
}
#[inline]
pub(crate) fn dec(c: [f32; 3]) -> [f32; 3] {
    [srgb_to_linear(c[0].clamp(0.0, 1.0)), srgb_to_linear(c[1].clamp(0.0, 1.0)), srgb_to_linear(c[2].clamp(0.0, 1.0))]
}

pub(crate) fn hash3(x: usize, y: usize, z: u64) -> f32 {
    let mut h = (x as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ (y as u64).wrapping_mul(0xC2B2_AE3D_27D4_EB4F) ^ z.wrapping_mul(0x1656_67B1_9E37_79F9);
    h ^= h >> 31;
    h = h.wrapping_mul(0xBF58_476D_1CE4_E5B9);
    h ^= h >> 29;
    (h >> 40) as f32 / (1u64 << 24) as f32
}

/// Apply one effect. Unknown/unimplemented ids are a no-op (they still round-trip in the project).
pub fn apply(img: &mut Image, e: &EffectInstance, cx: &FxCtx) {
    if !e.enabled || img.w == 0 || img.h == 0 {
        return;
    }
    if crate::vfx::apply(img, e, cx) {
        return;
    }
    match e.effect.as_str() {
        "brightness_contrast" => {
            let br = f(e, "brightness", cx) / 100.0 * 0.4;
            let co = 1.0 + f(e, "contrast", cx) / 100.0;
            img.map_rgb(|c, _, _| {
                let c = enc(c);
                dec(c.map(|v| (v - 0.5) * co + 0.5 + br))
            });
        }
        "proc_amp" => {
            let br = f(e, "brightness", cx) / 100.0 * 0.4;
            let co = f(e, "contrast", cx) / 100.0;
            let hue = f(e, "hue", cx) / 360.0;
            let sat = f(e, "saturation", cx) / 100.0;
            img.map_rgb(|c, _, _| {
                let c = enc(c);
                let mut hsl = rgb_to_hsl(c[0], c[1], c[2]);
                hsl[0] = (hsl[0] + hue).rem_euclid(1.0);
                hsl[1] = (hsl[1] * sat).clamp(0.0, 1.0);
                let c = hsl_to_rgb(hsl[0], hsl[1], hsl[2]);
                dec(c.map(|v| (v - 0.5) * co + 0.5 + br))
            });
        }
        "tint" => {
            let bl = color(e, "black", cx);
            let wh = color(e, "white", cx);
            let amt = f(e, "amount", cx) / 100.0;
            img.map_rgb(|c, _, _| {
                let l = linear_to_srgb(luma709(c[0], c[1], c[2]).max(0.0));
                let t = [bl[0] + (wh[0] - bl[0]) * l, bl[1] + (wh[1] - bl[1]) * l, bl[2] + (wh[2] - bl[2]) * l];
                let e = enc(c);
                dec([e[0] + (t[0] - e[0]) * amt, e[1] + (t[1] - e[1]) * amt, e[2] + (t[2] - e[2]) * amt])
            });
        }
        "black_white" => img.map_rgb(|c, _, _| {
            let l = luma709(c[0], c[1], c[2]);
            [l, l, l]
        }),
        "color_balance" => {
            let g = |k: &str| f(e, k, cx) / 100.0 * 0.25;
            let sh = [g("shadow_r"), g("shadow_g"), g("shadow_b")];
            let md = [g("mid_r"), g("mid_g"), g("mid_b")];
            let hi = [g("hi_r"), g("hi_g"), g("hi_b")];
            let preserve = b(e, "preserve");
            img.map_rgb(|c, _, _| {
                let c = enc(c);
                let l = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
                let ws = (1.0 - l).powi(2);
                let wh = l.powi(2);
                let wm = 1.0 - ws - wh;
                let mut o = [0.0; 3];
                for k in 0..3 {
                    o[k] = c[k] + sh[k] * ws + md[k] * wm.max(0.0) + hi[k] * wh;
                }
                if preserve {
                    let l2 = 0.2126 * o[0] + 0.7152 * o[1] + 0.0722 * o[2];
                    let d = l - l2;
                    o = o.map(|v| v + d);
                }
                dec(o)
            });
        }
        "leave_color" => {
            let amt = f(e, "amount", cx) / 100.0;
            let key = color(e, "color", cx);
            let tol = f(e, "tolerance", cx) / 100.0;
            let soft = f(e, "softness", cx) / 100.0 + 1e-4;
            let kh = rgb_to_hsl(key[0], key[1], key[2])[0];
            img.map_rgb(|c, _, _| {
                let ec = enc(c);
                let h = rgb_to_hsl(ec[0], ec[1], ec[2])[0];
                let d = (h - kh).abs().min(1.0 - (h - kh).abs()) * 2.0;
                let keep = 1.0 - ((d - tol) / soft).clamp(0.0, 1.0);
                let l = luma709(c[0], c[1], c[2]);
                let k = amt * (1.0 - keep);
                [c[0] + (l - c[0]) * k, c[1] + (l - c[1]) * k, c[2] + (l - c[2]) * k]
            });
        }
        "change_to_color" => {
            let from = color(e, "from", cx);
            let to = color(e, "to", cx);
            let tol = f(e, "hue_tol", cx) / 100.0;
            let soft = f(e, "softness", cx) / 100.0 * 0.3 + 1e-4;
            let fh = rgb_to_hsl(from[0], from[1], from[2])[0];
            let th = rgb_to_hsl(to[0], to[1], to[2])[0];
            img.map_rgb(|c, _, _| {
                let ec = enc(c);
                let mut hsl = rgb_to_hsl(ec[0], ec[1], ec[2]);
                let d = (hsl[0] - fh).abs().min(1.0 - (hsl[0] - fh).abs());
                let w = 1.0 - ((d - tol) / soft).clamp(0.0, 1.0);
                hsl[0] = (hsl[0] + (th - fh) * w).rem_euclid(1.0);
                dec(hsl_to_rgb(hsl[0], hsl[1], hsl[2]))
            });
        }
        "color_pass" => {
            let key = color(e, "color", cx);
            let sim = f(e, "similarity", cx) / 100.0;
            let rev = b(e, "reverse");
            img.map_rgb(|c, _, _| {
                let ec = enc(c);
                let d = ((ec[0] - key[0]).powi(2) + (ec[1] - key[1]).powi(2) + (ec[2] - key[2]).powi(2)).sqrt();
                let pass = (d <= sim * 1.2) != rev;
                if pass {
                    c
                } else {
                    let l = luma709(c[0], c[1], c[2]);
                    [l, l, l]
                }
            });
        }
        "gamma_correction" => {
            let g = f(e, "gamma", cx) / 10.0;
            img.map_rgb(|c, _, _| dec(enc(c).map(|v| v.max(0.0).powf(g))));
        }
        "levels" => {
            let ib = f(e, "in_black", cx) / 255.0;
            let iw = (f(e, "in_white", cx) / 255.0).max(ib + 1e-3);
            let ob = f(e, "out_black", cx) / 255.0;
            let ow = f(e, "out_white", cx) / 255.0;
            let g = 100.0 / f(e, "gamma", cx).max(1.0);
            img.map_rgb(|c, _, _| dec(enc(c).map(|v| ob + (((v - ib) / (iw - ib)).clamp(0.0, 1.0)).powf(g) * (ow - ob))));
        }
        "extract" => {
            let lo = f(e, "black", cx) / 255.0;
            let hi = f(e, "white", cx) / 255.0;
            let soft = f(e, "softness", cx) / 100.0 * 0.2 + 1e-4;
            let inv = b(e, "invert");
            img.map_rgb(|c, _, _| {
                let l = linear_to_srgb(luma709(c[0], c[1], c[2]).max(0.0));
                let inside = ((l - lo) / soft).clamp(0.0, 1.0).min(((hi - l) / soft).clamp(0.0, 1.0));
                let v = if inv { 1.0 - inside } else { inside };
                [v, v, v]
            });
        }
        "invert" => {
            let ch = choice(e, "channel");
            let blend = f(e, "blend", cx) / 100.0;
            if ch == 4 {
                img.px.par_chunks_mut(4).for_each(|p| {
                    let a = p[3];
                    let na = 1.0 - a;
                    let k = if a > 1e-6 { na / a } else { 0.0 };
                    for c in &mut p[..3] {
                        *c *= k;
                    }
                    p[3] = na * (1.0 - blend) + a * blend;
                });
            } else {
                img.map_rgb(|c, _, _| {
                    let ec = enc(c);
                    let mut o = ec;
                    for k in 0..3 {
                        if ch == 0 || ch as usize == k + 1 {
                            o[k] = 1.0 - ec[k];
                        }
                    }
                    dec([o[0] + (ec[0] - o[0]) * blend, o[1] + (ec[1] - o[1]) * blend, o[2] + (ec[2] - o[2]) * blend])
                });
            }
        }
        "posterize" => {
            let n = f(e, "levels", cx).max(2.0) - 1.0;
            img.map_rgb(|c, _, _| dec(enc(c).map(|v| (v * n).round() / n)));
        }
        "lumetri" => lumetri(img, e, cx),
        "gaussian_blur" => {
            let r = f(e, "blurriness", cx) * cx.px_scale * 0.5;
            let dims = choice(e, "dimensions");
            let repeat = b(e, "repeat_edge");
            gaussian(img, if dims == 2 { 0.0 } else { r }, if dims == 1 { 0.0 } else { r }, repeat);
        }
        "camera_blur" => {
            let r = f(e, "percent", cx) * cx.px_scale * 0.3;
            gaussian(img, r, r, true);
        }
        "directional_blur" => {
            let len = f(e, "length", cx) * cx.px_scale * 2.0;
            let dir = (f(e, "direction", cx) as f64).to_radians();
            directional_blur(img, len, dir);
        }
        "sharpen" => {
            let amt = f(e, "amount", cx) / 100.0;
            unsharp(img, 1.0 * cx.px_scale.max(0.35), amt, 0.0);
        }
        "unsharp_mask" => {
            let amt = f(e, "amount", cx) / 100.0;
            let r = f(e, "radius", cx) * cx.px_scale;
            let th = f(e, "threshold", cx) / 255.0;
            unsharp(img, r, amt, th);
        }
        "median" => {
            let r = (f(e, "radius", cx) * cx.px_scale).round().clamp(0.0, 4.0) as isize;
            if r > 0 {
                median(img, r);
            }
        }
        "noise" => {
            let amt = f(e, "amount", cx) / 100.0;
            let colored = b(e, "color");
            let clip = b(e, "clip");
            let frame = (cx.seconds * 30.0) as u64;
            let w = img.w;
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let p = &mut row[x * 4..x * 4 + 4];
                    let a = p[3];
                    if a <= 0.0 {
                        continue;
                    }
                    let n0 = hash3(x, y, frame) - 0.5;
                    let ns = if colored { [n0, hash3(x, y, frame + 7777) - 0.5, hash3(x, y, frame + 99_991) - 0.5] } else { [n0; 3] };
                    let c = enc([p[0] / a, p[1] / a, p[2] / a]);
                    let mut o = [c[0] + ns[0] * amt, c[1] + ns[1] * amt, c[2] + ns[2] * amt];
                    if clip {
                        o = o.map(|v| v.clamp(0.0, 1.0));
                    }
                    let o = dec(o);
                    p[0] = o[0] * a;
                    p[1] = o[1] * a;
                    p[2] = o[2] * a;
                }
            });
        }
        "mosaic" => {
            let bx = f(e, "horizontal", cx).max(1.0) as usize;
            let by = f(e, "vertical", cx).max(1.0) as usize;
            mosaic(img, bx, by);
        }
        "find_edges" => {
            let inv = b(e, "invert");
            let blend = f(e, "blend", cx) / 100.0;
            let src = img.clone();
            let w = img.w;
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let l = |dx: isize, dy: isize| {
                        let p = src.get_clamped(x as isize + dx, y as isize + dy);
                        luma709(p[0], p[1], p[2])
                    };
                    let gx = l(1, -1) + 2.0 * l(1, 0) + l(1, 1) - l(-1, -1) - 2.0 * l(-1, 0) - l(-1, 1);
                    let gy = l(-1, 1) + 2.0 * l(0, 1) + l(1, 1) - l(-1, -1) - 2.0 * l(0, -1) - l(1, -1);
                    let mut m = (gx * gx + gy * gy).sqrt().min(1.0);
                    if !inv {
                        m = 1.0 - m;
                    }
                    let o = src.get(x, y);
                    let a = o[3];
                    for k in 0..3 {
                        row[x * 4 + k] = m * a * (1.0 - blend) + o[k] * blend;
                    }
                }
            });
        }
        "emboss" => {
            let dir = (f(e, "direction", cx) as f64).to_radians();
            let relief = f(e, "relief", cx) * cx.px_scale.max(0.25);
            let contrast = f(e, "contrast", cx) / 100.0;
            let blend = f(e, "blend", cx) / 100.0;
            let (dx, dy) = ((dir.cos() as f32) * relief, (-dir.sin() as f32) * relief);
            let src = img.clone();
            let w = img.w;
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let a = src.sample_bilinear_clamped(x as f32 + 0.5 + dx, y as f32 + 0.5 + dy);
                    let bq = src.sample_bilinear_clamped(x as f32 + 0.5 - dx, y as f32 + 0.5 - dy);
                    let v = srgb_to_linear((0.5 + (luma709(a[0], a[1], a[2]) - luma709(bq[0], bq[1], bq[2])) * contrast * 2.0).clamp(0.0, 1.0));
                    let o = src.get(x, y);
                    for k in 0..3 {
                        row[x * 4 + k] = v * o[3] * (1.0 - blend) + o[k] * blend;
                    }
                }
            });
        }
        "replicate" => {
            let n = f(e, "count", cx).clamp(1.0, 16.0) as usize;
            let src = img.clone();
            let (w, h) = (img.w, img.h);
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let u = (x * n) % w;
                    let v = (y * n) % h;
                    let s = src.sample_bilinear_clamped((u as f32 + 0.5) + (n as f32 - 1.0) * 0.5 / n as f32, v as f32 + 0.5);
                    row[x * 4..x * 4 + 4].copy_from_slice(&s);
                }
            });
        }
        "strobe" => {
            let period = f(e, "period", cx).max(0.001) as f64;
            let dur = f(e, "duration", cx) as f64;
            if (cx.seconds % period) < dur {
                let c = color(e, "color", cx);
                let blend = f(e, "blend", cx) / 100.0;
                let lc = dec([c[0], c[1], c[2]]);
                img.map_rgb(|o, _, _| [lc[0] + (o[0] - lc[0]) * blend, lc[1] + (o[1] - lc[1]) * blend, lc[2] + (o[2] - lc[2]) * blend]);
            }
        }
        "crop" => {
            let l = f(e, "left", cx) / 100.0;
            let t = f(e, "top", cx) / 100.0;
            let r = f(e, "right", cx) / 100.0;
            let bt = f(e, "bottom", cx) / 100.0;
            let feather = f(e, "feather", cx) * cx.px_scale;
            if b(e, "zoom") && l + r < 0.99 && t + bt < 0.99 {
                let src = img.clone();
                let (w, h) = (img.w as f64, img.h as f64);
                let m =
                    Affine::scale(1.0 / (1.0 - (l + r) as f64), 1.0 / (1.0 - (t + bt) as f64)).then_apply(&Affine::translate(-(l as f64) * w, -(t as f64) * h));
                *img = src.transformed(img.w, img.h, &m);
            } else {
                crop(img, l, t, r, bt, feather);
            }
        }
        "horizontal_flip" => {
            let w = img.w;
            img.px.par_chunks_mut(w * 4).for_each(|row| {
                for x in 0..w / 2 {
                    for k in 0..4 {
                        row.swap(x * 4 + k, (w - 1 - x) * 4 + k);
                    }
                }
            });
        }
        "vertical_flip" => {
            let (w, h) = (img.w, img.h);
            for y in 0..h / 2 {
                let (a, bb) = img.px.split_at_mut((h - 1 - y) * w * 4);
                a[y * w * 4..(y + 1) * w * 4].swap_with_slice(&mut bb[..w * 4]);
            }
        }
        "edge_feather" => {
            let amt = f(e, "amount", cx) / 100.0 * (img.w.min(img.h) as f32) * 0.5;
            crop(img, 0.0, 0.0, 0.0, 0.0, amt);
        }
        "transform" => {
            let anchor = point(e, "anchor", cx, img);
            let pos = point(e, "position", cx, img);
            let sh = f(e, "scale_height", cx) as f64 / 100.0;
            let sw = if b(e, "uniform_scale") { sh } else { f(e, "scale_width", cx) as f64 / 100.0 };
            let rot = f(e, "rotation", cx) as f64;
            let skew = (f(e, "skew", cx) as f64).to_radians().tan();
            let skew_axis = f(e, "skew_axis", cx) as f64;
            let op = f(e, "opacity", cx) / 100.0;
            let sk = Affine::rotate_deg(skew_axis)
                .then_apply(&Affine { a: 1.0, b: 0.0, c: skew, d: 1.0, e: 0.0, f: 0.0 })
                .then_apply(&Affine::rotate_deg(-skew_axis));
            let m = Affine::translate(pos.x, pos.y)
                .then_apply(&Affine::rotate_deg(rot))
                .then_apply(&sk)
                .then_apply(&Affine::scale(sw, sh))
                .then_apply(&Affine::translate(-anchor.x, -anchor.y));
            let mut out = img.transformed(img.w, img.h, &m);
            out.scale_alpha(op);
            *img = out;
        }
        "mirror" => {
            let c = point(e, "center", cx, img);
            let ang = (f(e, "angle", cx) as f64).to_radians();
            let (nx, ny) = (ang.cos(), ang.sin());
            let src = img.clone();
            let w = img.w;
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let (px, py) = (x as f64 + 0.5, y as f64 + 0.5);
                    let d = (px - c.x) * nx + (py - c.y) * ny;
                    if d > 0.0 {
                        let (rx, ry) = (px - 2.0 * d * nx, py - 2.0 * d * ny);
                        row[x * 4..x * 4 + 4].copy_from_slice(&src.sample_bilinear(rx as f32, ry as f32));
                    }
                }
            });
        }
        "offset" => {
            let s = point(e, "shift", cx, img);
            let (dx, dy) = (s.x - img.w as f64 / 2.0, s.y - img.h as f64 / 2.0);
            let blend = f(e, "blend", cx) / 100.0;
            let src = img.clone();
            let (w, h) = (img.w as f64, img.h as f64);
            let wi = img.w;
            img.px.par_chunks_mut(wi * 4).enumerate().for_each(|(y, row)| {
                for x in 0..wi {
                    let u = (x as f64 + 0.5 - dx).rem_euclid(w);
                    let v = (y as f64 + 0.5 - dy).rem_euclid(h);
                    let p = src.sample_bilinear_clamped(u as f32, v as f32);
                    let o = src.get(x, y);
                    for k in 0..4 {
                        row[x * 4 + k] = p[k] + (o[k] - p[k]) * blend;
                    }
                }
            });
        }
        "twirl" => {
            let c = point(e, "center", cx, img);
            let ang = (f(e, "angle", cx) as f64).to_radians();
            let rad = f(e, "radius", cx) as f64 / 100.0 * (img.w.min(img.h) as f64);
            warp(img, |x, y| {
                let (dx, dy) = (x - c.x, y - c.y);
                let d = (dx * dx + dy * dy).sqrt();
                if d >= rad {
                    return (x, y);
                }
                let k = 1.0 - d / rad;
                let a = ang * k * k;
                let (s, co) = a.sin_cos();
                (c.x + dx * co - dy * s, c.y + dx * s + dy * co)
            });
        }
        "wave_warp" => {
            let hgt = f(e, "height", cx) as f64 * cx.px_scale as f64;
            let wid = (f(e, "width", cx) as f64 * cx.px_scale as f64).max(1.0);
            let dir = (f(e, "direction", cx) as f64).to_radians();
            let phase = cx.seconds * f(e, "speed", cx) as f64 * std::f64::consts::TAU;
            let (ux, uy) = (dir.sin(), -dir.cos());
            warp(img, |x, y| {
                let along = x * ux + y * uy;
                let off = hgt * (along / wid * std::f64::consts::TAU - phase).sin();
                (x - uy * off, y + ux * off)
            });
        }
        "lens_distortion" => {
            let k = f(e, "curvature", cx) as f64 / 100.0;
            let (cxp, cyp) = (
                img.w as f64 / 2.0 + f(e, "h_decentering", cx) as f64 / 100.0 * img.w as f64 / 2.0,
                img.h as f64 / 2.0 + f(e, "v_decentering", cx) as f64 / 100.0 * img.h as f64 / 2.0,
            );
            let norm = (img.w as f64 / 2.0).hypot(img.h as f64 / 2.0);
            warp(img, |x, y| {
                let (dx, dy) = ((x - cxp) / norm, (y - cyp) / norm);
                let r2 = dx * dx + dy * dy;
                let s = 1.0 - k * r2;
                (cxp + dx * s * norm, cyp + dy * s * norm)
            });
        }
        "basic_3d" => {
            let sw = (f(e, "swivel", cx) as f64).to_radians();
            let tl = (f(e, "tilt", cx) as f64).to_radians();
            let dist = f(e, "distance", cx) as f64;
            let (w, h) = (img.w as f64, img.h as f64);
            let focal = w.max(h) * 1.2;
            // inverse perspective mapping: ray through (x,y) intersected with rotated plane
            warp_opt(img, |x, y| {
                let (px, py) = (x - w / 2.0, y - h / 2.0);
                let (ss, cs) = sw.sin_cos();
                let (st, ct) = tl.sin_cos();
                // plane normal after rotation (Ry(swivel) * Rx(tilt)) of (0,0,1)
                let n = [ss * ct, -st, cs * ct];
                let u = [cs, 0.0, -ss];
                let v = [ss * st, ct, cs * st];
                let z0 = focal + dist * 10.0;
                let dir = [px, py, focal];
                let denom = n[0] * dir[0] + n[1] * dir[1] + n[2] * dir[2];
                if denom.abs() < 1e-9 {
                    return None;
                }
                let tt = (n[2] * z0) / denom;
                if tt <= 0.0 {
                    return None;
                }
                let hit = [dir[0] * tt, dir[1] * tt, dir[2] * tt - z0];
                let su = hit[0] * u[0] + hit[1] * u[1] + hit[2] * u[2];
                let sv = hit[0] * v[0] + hit[1] * v[1] + hit[2] * v[2];
                Some((su + w / 2.0, sv + h / 2.0))
            });
        }
        "drop_shadow" => {
            let c = color(e, "color", cx);
            let op = f(e, "opacity", cx) / 100.0;
            let dir = (f(e, "direction", cx) as f64).to_radians();
            let dist = f(e, "distance", cx) as f64 * cx.px_scale as f64;
            let soft = f(e, "softness", cx) * cx.px_scale * 0.5;
            let only = b(e, "only");
            let (dx, dy) = (dir.sin() * dist, -dir.cos() * dist);
            let lc = dec([c[0], c[1], c[2]]);
            let mut sh = img.transformed(img.w, img.h, &Affine::translate(dx, dy));
            sh.px.par_chunks_mut(4).for_each(|p| {
                let a = p[3] * op;
                p[0] = lc[0] * a;
                p[1] = lc[1] * a;
                p[2] = lc[2] * a;
                p[3] = a;
            });
            if soft > 0.3 {
                gaussian(&mut sh, soft, soft, false);
            }
            if !only {
                crate::blend::composite(&mut sh, img, 1.0, crate::blend::Blend::Normal);
            }
            *img = sh;
        }
        "bevel_alpha" => {
            let th = f(e, "thickness", cx) * cx.px_scale;
            let ang = (f(e, "angle", cx) as f64).to_radians();
            let inten = f(e, "intensity", cx) / 100.0;
            let src = img.clone();
            let (lx, ly) = (ang.cos() as f32, -ang.sin() as f32);
            let w = img.w;
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let a = |dx: f32, dy: f32| src.sample_bilinear_clamped(x as f32 + 0.5 + dx, y as f32 + 0.5 + dy)[3];
                    let gx = a(th, 0.0) - a(-th, 0.0);
                    let gy = a(0.0, th) - a(0.0, -th);
                    let shade = -(gx * lx + gy * ly) * inten;
                    let p = &mut row[x * 4..x * 4 + 4];
                    for k in 0..3 {
                        p[k] = (p[k] + shade * p[3]).clamp(0.0, p[3]);
                    }
                }
            });
        }
        "ultra_key" | "color_key" => key(img, e, cx),
        "luma_key" => {
            let th = f(e, "threshold", cx) / 100.0;
            let cut = f(e, "cutoff", cx) / 100.0;
            img.px.par_chunks_mut(4).for_each(|p| {
                if p[3] <= 0.0 {
                    return;
                }
                let l = linear_to_srgb(luma709(p[0] / p[3], p[1] / p[3], p[2] / p[3]).max(0.0));
                let a = if l <= cut {
                    0.0
                } else if l >= th.max(cut + 1e-3) {
                    1.0
                } else {
                    (l - cut) / (th - cut).max(1e-3)
                };
                for v in p.iter_mut() {
                    *v *= a;
                }
            });
        }
        "four_color_gradient" => {
            let cs = [color(e, "c1", cx), color(e, "c2", cx), color(e, "c3", cx), color(e, "c4", cx)];
            let op = f(e, "opacity", cx) / 100.0;
            let (w, h) = (img.w as f32, img.h as f32);
            let pts = [(0.25 * w, 0.25 * h), (0.75 * w, 0.25 * h), (0.25 * w, 0.75 * h), (0.75 * w, 0.75 * h)];
            let blend_exp = 1.0 + f(e, "blend", cx) / 100.0;
            fill_over(img, op, |x, y| {
                let mut acc = [0.0f32; 3];
                let mut wsum = 0.0;
                for (i, p) in pts.iter().enumerate() {
                    let d = ((x - p.0).powi(2) + (y - p.1).powi(2)).sqrt().max(1.0);
                    let wgt = 1.0 / d.powf(blend_exp);
                    for k in 0..3 {
                        acc[k] += cs[i][k] * wgt;
                    }
                    wsum += wgt;
                }
                dec(acc.map(|v| v / wsum))
            });
        }
        "ramp" => {
            let s = point(e, "start", cx, img);
            let en = point(e, "end", cx, img);
            let sc = color(e, "start_color", cx);
            let ec = color(e, "end_color", cx);
            let radial = choice(e, "shape") == 1;
            let blend = f(e, "blend", cx) / 100.0;
            let (vx, vy) = (en.x - s.x, en.y - s.y);
            let len2 = (vx * vx + vy * vy).max(1e-9);
            fill_over(img, 1.0 - blend, |x, y| {
                let t = if radial {
                    ((x as f64 - s.x).hypot(y as f64 - s.y) / len2.sqrt()) as f32
                } else {
                    (((x as f64 - s.x) * vx + (y as f64 - s.y) * vy) / len2) as f32
                }
                .clamp(0.0, 1.0);
                dec([sc[0] + (ec[0] - sc[0]) * t, sc[1] + (ec[1] - sc[1]) * t, sc[2] + (ec[2] - sc[2]) * t])
            });
        }
        "circle" => {
            let c = point(e, "center", cx, img);
            let r = f(e, "radius", cx) * cx.px_scale;
            let col = color(e, "color", cx);
            let op = f(e, "opacity", cx) / 100.0;
            let lc = dec([col[0], col[1], col[2]]);
            let w = img.w;
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let d = ((x as f64 + 0.5 - c.x).hypot(y as f64 + 0.5 - c.y)) as f32;
                    let a = (r - d + 0.5).clamp(0.0, 1.0) * op;
                    if a > 0.0 {
                        let p = &mut row[x * 4..x * 4 + 4];
                        for k in 0..3 {
                            p[k] = lc[k] * a + p[k] * (1.0 - a);
                        }
                        p[3] = a + p[3] * (1.0 - a);
                    }
                }
            });
        }
        "grid" => {
            let size = (f(e, "size", cx) * cx.px_scale).max(1.0);
            let border = f(e, "border", cx) * cx.px_scale;
            let col = color(e, "color", cx);
            let op = f(e, "opacity", cx) / 100.0;
            let lc = dec([col[0], col[1], col[2]]);
            let w = img.w;
            let (ox, oy) = (img.w as f32 / 2.0, img.h as f32 / 2.0);
            img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
                for x in 0..w {
                    let gx = ((x as f32 - ox).rem_euclid(size)).min(size - (x as f32 - ox).rem_euclid(size));
                    let gy = ((y as f32 - oy).rem_euclid(size)).min(size - (y as f32 - oy).rem_euclid(size));
                    let a = ((border * 0.5 - gx.min(gy) + 0.5).clamp(0.0, 1.0)) * op;
                    if a > 0.0 {
                        let p = &mut row[x * 4..x * 4 + 4];
                        for k in 0..3 {
                            p[k] = lc[k] * a + p[k] * (1.0 - a);
                        }
                        p[3] = a + p[3] * (1.0 - a);
                    }
                }
            });
        }
        "lens_flare" => {
            let c = point(e, "center", cx, img);
            let br = f(e, "brightness", cx) / 100.0;
            let blend = f(e, "blend", cx) / 100.0;
            let (w, h) = (img.w as f64, img.h as f64);
            let (mx, my) = (w / 2.0, h / 2.0);
            let scale = w.max(h);
            let wi = img.w;
            img.px.par_chunks_mut(wi * 4).enumerate().for_each(|(y, row)| {
                for x in 0..wi {
                    let (px, py) = (x as f64 + 0.5, y as f64 + 0.5);
                    let d = (px - c.x).hypot(py - c.y) / scale;
                    let mut add = [1.0f32, 0.9, 0.7].map(|k| k * (0.35 * (-d * 18.0).exp() + 0.08 * (-d * 3.0).exp()) as f32);
                    for (i, (t, rad, tint)) in [(0.5, 0.05, [0.4f32, 0.6, 1.0]), (1.3, 0.03, [1.0, 0.5, 0.3]), (1.7, 0.08, [0.4, 1.0, 0.5])].iter().enumerate()
                    {
                        let gx = c.x + (mx - c.x) * 2.0 * t / 2.0 * 2.0;
                        let gy = c.y + (my - c.y) * 2.0 * t / 2.0 * 2.0;
                        let dd = (px - gx).hypot(py - gy) / scale;
                        let ring = (1.0 - ((dd - rad) / 0.01).abs()).max(0.0) as f32 * 0.15 + if dd < *rad { 0.06 } else { 0.0 };
                        let _ = i;
                        for k in 0..3 {
                            add[k] += tint[k] * ring;
                        }
                    }
                    let p = &mut row[x * 4..x * 4 + 4];
                    for k in 0..3 {
                        p[k] += add[k] * br * (1.0 - blend) * p[3].max(0.0);
                    }
                }
            });
        }
        "timecode" | "clip_name" => {
            let (text, family) = if e.effect == "timecode" { (cx.timecode.to_string(), "JetBrains Mono") } else { (cx.clip_name.to_string(), "Inter") };
            let pos = point(e, "position", cx, img);
            let px = (f(e, "size", cx) / 100.0 * img.h as f32 * 0.5).max(6.0);
            let box_alpha = if e.effect == "timecode" { (f(e, "opacity", cx) / 100.0).clamp(0.0, 1.0) * 0.8 } else { 0.8 };
            crate::graphics::burn_text(img, &text, family, pos, px, box_alpha);
        }
        _ => {}
    }
}

/// Fill an opaque generated colour over the image at `op` opacity (Generate category).
pub(crate) fn fill_over(img: &mut Image, op: f32, f: impl Fn(f32, f32) -> [f32; 3] + Sync) {
    let w = img.w;
    img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
        for x in 0..w {
            let c = f(x as f32 + 0.5, y as f32 + 0.5);
            let p = &mut row[x * 4..x * 4 + 4];
            for k in 0..3 {
                p[k] = c[k] * op + p[k] * (1.0 - op);
            }
            p[3] = op + p[3] * (1.0 - op);
        }
    });
}

pub(crate) fn warp(img: &mut Image, f: impl Fn(f64, f64) -> (f64, f64) + Sync) {
    warp_opt(img, |x, y| Some(f(x, y)));
}

pub(crate) fn warp_opt(img: &mut Image, f: impl Fn(f64, f64) -> Option<(f64, f64)> + Sync) {
    let src = img.clone();
    let w = img.w;
    img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
        for x in 0..w {
            let p = match f(x as f64 + 0.5, y as f64 + 0.5) {
                Some((u, v)) => src.sample_bilinear(u as f32, v as f32),
                None => [0.0; 4],
            };
            row[x * 4..x * 4 + 4].copy_from_slice(&p);
        }
    });
}

pub(crate) fn crop(img: &mut Image, l: f32, t: f32, r: f32, b: f32, feather: f32) {
    let (w, h) = (img.w as f32, img.h as f32);
    let (x0, x1, y0, y1) = (l * w, w * (1.0 - r), t * h, h * (1.0 - b));
    let fe = feather.max(0.0);
    let wi = img.w;
    img.px.par_chunks_mut(wi * 4).enumerate().for_each(|(y, row)| {
        let py = y as f32 + 0.5;
        for x in 0..wi {
            let px = x as f32 + 0.5;
            let d = (px - x0).min(x1 - px).min(py - y0).min(y1 - py);
            let a = if fe > 0.0 { (d / fe).clamp(0.0, 1.0) } else { (d + 0.5).clamp(0.0, 1.0) };
            if a < 1.0 {
                for v in &mut row[x * 4..x * 4 + 4] {
                    *v *= a;
                }
            }
        }
    });
}

pub(crate) fn key(img: &mut Image, e: &EffectInstance, cx: &FxCtx) {
    let ultra = e.effect == "ultra_key";
    let kc = if ultra { color(e, "key_color", cx) } else { color(e, "color", cx) };
    let kycc = lumiere_codecbase_color::rgb_to_ycbcr(kc[0], kc[1], kc[2], lumiere_codecbase_color::Matrix::Bt709);
    let (tol, soft, spill, output) = if ultra {
        let tol = 0.05 + f(e, "tolerance", cx) / 100.0 * 0.25 + f(e, "transparency", cx) / 100.0 * 0.05;
        (tol, 0.05 + f(e, "soften", cx) / 100.0 * 0.2 + f(e, "pedestal", cx) / 100.0 * 0.05, f(e, "spill", cx) / 100.0, choice(e, "output"))
    } else {
        (f(e, "tolerance", cx) / 255.0 * 0.6 + 0.01, f(e, "feather", cx) / 50.0 * 0.2 + 0.01, 0.0, 0)
    };
    let dom = if kc[1] >= kc[0] && kc[1] >= kc[2] {
        1
    } else if kc[2] >= kc[0] {
        2
    } else {
        0
    };
    img.px.par_chunks_mut(4).for_each(|p| {
        if p[3] <= 0.0 {
            return;
        }
        let c = enc([p[0] / p[3], p[1] / p[3], p[2] / p[3]]);
        let ycc = lumiere_codecbase_color::rgb_to_ycbcr(c[0], c[1], c[2], lumiere_codecbase_color::Matrix::Bt709);
        let d = ((ycc[1] - kycc[1]).powi(2) + (ycc[2] - kycc[2]).powi(2)).sqrt() + (ycc[0] - kycc[0]).abs() * 0.15;
        let alpha = ((d - tol) / soft).clamp(0.0, 1.0);
        let mut o = c;
        if spill > 0.0 {
            let others = (o[(dom + 1) % 3] + o[(dom + 2) % 3]) / 2.0;
            if o[dom] > others {
                o[dom] -= (o[dom] - others) * spill;
            }
        }
        let a = p[3] * alpha;
        let lo = dec(o);
        match output {
            1 => {
                p.copy_from_slice(&[alpha * p[3], alpha * p[3], alpha * p[3], p[3]]);
            }
            _ => {
                p[0] = lo[0] * a;
                p[1] = lo[1] * a;
                p[2] = lo[2] * a;
                p[3] = a;
            }
        }
    });
}

fn lumetri(img: &mut Image, e: &EffectInstance, cx: &FxCtx) {
    let (basic_on, creative_on, vignette_on) = (on(e, "basic_on"), on(e, "creative_on"), on(e, "vignette_on"));
    let input_lut = if basic_on { crate::luts::resolve(cx.project, text(e, "input_lut")) } else { None };
    let bf = |id: &str| if basic_on { f(e, id, cx) } else { 0.0 };
    let temp = bf("temperature") / 100.0;
    let tint = bf("tint") / 100.0;
    let exposure = 2f32.powf(bf("exposure"));
    let contrast = bf("contrast") / 100.0;
    let hl = bf("highlights") / 100.0;
    let sh = bf("shadows") / 100.0;
    let wh = bf("whites") / 100.0;
    let bl = bf("blacks") / 100.0;
    let sat = if basic_on { f(e, "saturation", cx) / 100.0 } else { 1.0 } * if creative_on { f(e, "creative_sat", cx) / 100.0 } else { 1.0 };
    let vib = if creative_on { f(e, "vibrance", cx) / 100.0 } else { 0.0 };
    let faded = if creative_on { f(e, "faded_film", cx) / 100.0 } else { 0.0 };
    let st = color(e, "shadow_tint", cx);
    let ht = color(e, "highlight_tint", cx);
    let va = if vignette_on { f(e, "vignette_amount", cx) } else { 0.0 };
    let vmid = f(e, "vignette_midpoint", cx) / 100.0;
    let vround = f(e, "vignette_roundness", cx) / 100.0;
    let vfeather = f(e, "vignette_feather", cx) / 100.0;
    let sharpen = if creative_on { f(e, "sharpen", cx) / 100.0 } else { 0.0 };
    let gains = [1.0 + 0.35 * temp, 1.0 - 0.3 * tint, 1.0 - 0.35 * temp];
    let (w, h) = (img.w as f32, img.h as f32);
    let aspect = w / h;
    img.map_rgb(|c, x, y| {
        let c = match &input_lut {
            Some(l) => dec(l.apply(enc(c))),
            None => c,
        };
        // white balance + exposure in linear light
        let lin = [c[0] * gains[0] * exposure, c[1] * gains[1] * exposure, c[2] * gains[2] * exposure];
        let mut v = enc(lin);
        // whites / blacks: endpoints
        let b0 = -bl * 0.15;
        let w0 = 1.0 - wh * 0.15;
        v = v.map(|q| (q - b0) / (w0 - b0).max(1e-3));
        // highlights / shadows: luma-weighted lift/compress, hue preserving
        let l = 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
        let ws = (1.0 - l).clamp(0.0, 1.0).powi(3);
        let whl = l.clamp(0.0, 1.0).powi(3);
        let nl = (l + sh * 0.35 * ws + hl * 0.35 * whl).max(0.0);
        if l > 1e-5 {
            let k = nl / l;
            v = v.map(|q| q * k);
        }
        // contrast: smooth S-curve around mid grey
        if contrast.abs() > 1e-4 {
            let k = 1.0 + contrast;
            v = v.map(|q| {
                let q = q.clamp(0.0, 1.0);
                let s = q * q * (3.0 - 2.0 * q);
                if k >= 1.0 { q + (s - q) * (k - 1.0) } else { 0.5 + (q - 0.5) * k }
            });
        }
        // faded film: lift blacks and compress
        if faded > 0.0 {
            v = v.map(|q| q * (1.0 - 0.25 * faded) + 0.12 * faded);
        }
        // split tone
        if creative_on {
            let l2 = (0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]).clamp(0.0, 1.0);
            for k in 0..3 {
                v[k] += (st[k] - 0.5) * 0.3 * (1.0 - l2) + (ht[k] - 0.5) * 0.3 * l2;
            }
        }
        // saturation & vibrance
        let l3 = 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2];
        let cur_sat = v[0].max(v[1]).max(v[2]) - v[0].min(v[1]).min(v[2]);
        let s = sat * (1.0 + vib * (1.0 - cur_sat.clamp(0.0, 1.0)));
        v = v.map(|q| l3 + (q - l3) * s);
        // vignette
        if va.abs() > 1e-4 {
            let nx = (x as f32 / w - 0.5) * 2.0 * (1.0 + vround * 0.0) * if vround < 0.0 { aspect.powf(-vround) } else { 1.0 };
            let ny = (y as f32 / h - 0.5) * 2.0;
            let d = (nx * nx + ny * ny).sqrt() / std::f32::consts::SQRT_2;
            let edge = ((d - vmid * 0.9) / (vfeather.max(0.01) * 0.9)).clamp(0.0, 1.0);
            let e2 = edge * edge * (3.0 - 2.0 * edge);
            let k = 1.0 + va * 0.2 * e2;
            v = v.map(|q| if va < 0.0 { q * k.max(0.0) } else { q + (1.0 - q) * (k - 1.0) });
        }
        dec(v)
    });
    lumetri_advanced(img, e, cx);
    if sharpen.abs() > 1e-3 {
        unsharp(img, 1.2 * cx.px_scale.max(0.35), sharpen.max(-1.0), 0.0);
    }
}

// ---------- blur kernels ----------

/// Box radii for an n-pass box blur approximating a Gaussian of sigma (Kovesi / Wells).
fn boxes_for_gauss(sigma: f32, n: usize) -> Vec<usize> {
    let wideal = ((12.0 * sigma * sigma / n as f32) + 1.0).sqrt();
    let mut wl = wideal.floor() as i32;
    if wl % 2 == 0 {
        wl -= 1;
    }
    let wu = wl + 2;
    let mideal = (12.0 * sigma * sigma - (n as i32 * wl * wl) as f32 - 4.0 * n as f32 * wl as f32 - 3.0 * n as f32) / (-4.0 * wl as f32 - 4.0);
    let m = mideal.round() as i32;
    (0..n as i32).map(|i| (((if i < m { wl } else { wu }) - 1) / 2).max(0) as usize).collect()
}

fn box_rows(px: &mut [f32], w: usize, r: usize, repeat: bool) {
    if r == 0 {
        return;
    }
    px.par_chunks_mut(w * 4).for_each_init(
        || vec![0f32; w * 4],
        |tmp, row| {
            tmp.copy_from_slice(row);
            let inv = 1.0 / (2 * r + 1) as f32;
            let fetch = |i: isize| -> usize { if repeat { i.clamp(0, w as isize - 1) as usize } else { i as usize } };
            let mut acc = [0f32; 4];
            for i in -(r as isize)..=(r as isize) {
                if repeat || (i >= 0 && (i as usize) < w) {
                    let j = fetch(i);
                    for k in 0..4 {
                        acc[k] += tmp[j * 4 + k];
                    }
                }
            }
            for x in 0..w {
                for k in 0..4 {
                    row[x * 4 + k] = acc[k] * inv;
                }
                let out_i = x as isize - r as isize;
                let in_i = x as isize + r as isize + 1;
                if repeat || out_i >= 0 {
                    let j = fetch(out_i);
                    for k in 0..4 {
                        acc[k] -= tmp[j * 4 + k];
                    }
                }
                if repeat || (in_i as usize) < w {
                    let j = fetch(in_i);
                    for k in 0..4 {
                        acc[k] += tmp[j * 4 + k];
                    }
                }
            }
        },
    );
}

fn transpose(img: &Image) -> Image {
    let (w, h) = (img.w, img.h);
    let mut out = Image::new(h, w);
    const B: usize = 32;
    out.px.par_chunks_mut(h * 4 * B).enumerate().for_each(|(bi, chunk)| {
        let x0 = bi * B;
        let rows = chunk.len() / (h * 4);
        for y in 0..h {
            for dx in 0..rows {
                let x = x0 + dx;
                let s = (y * w + x) * 4;
                let d = (dx * h + y) * 4;
                chunk[d..d + 4].copy_from_slice(&img.px[s..s + 4]);
            }
        }
    });
    out
}

/// Gaussian blur via 3 box passes per axis (O(1) per pixel for any radius).
pub fn gaussian(img: &mut Image, sigma_x: f32, sigma_y: f32, repeat_edge: bool) {
    // beyond a few image sizes every radius gives the same (flat) result; cap to keep box sizes sane
    let sigma_x = if sigma_x.is_finite() { sigma_x.min(img.w.max(8) as f32 * 2.0) } else { 0.0 };
    let sigma_y = if sigma_y.is_finite() { sigma_y.min(img.h.max(8) as f32 * 2.0) } else { 0.0 };
    if sigma_x > 0.3 {
        for r in boxes_for_gauss(sigma_x, 3) {
            box_rows(&mut img.px, img.w, r, repeat_edge);
        }
    }
    if sigma_y > 0.3 {
        let mut t = transpose(img);
        for r in boxes_for_gauss(sigma_y, 3) {
            box_rows(&mut t.px, t.w, r, repeat_edge);
        }
        *img = transpose(&t);
    }
}

pub(crate) fn unsharp(img: &mut Image, radius: f32, amount: f32, threshold: f32) {
    let mut blurred = img.clone();
    gaussian(&mut blurred, radius, radius, true);
    img.px.par_chunks_mut(4).zip(blurred.px.par_chunks(4)).for_each(|(p, bq)| {
        for k in 0..3 {
            let d = p[k] - bq[k];
            if d.abs() >= threshold * p[3] {
                p[k] = (p[k] + d * amount).clamp(0.0, p[3].max(p[k] + d * amount).max(0.0));
                p[k] = p[k].max(0.0);
            }
        }
    });
}

pub(crate) fn directional_blur(img: &mut Image, len: f32, dir: f64) {
    if len < 0.5 {
        return;
    }
    let steps = (len.ceil() as usize).clamp(2, 64);
    let (dx, dy) = ((dir.sin() as f32) * len, (-dir.cos() as f32) * len);
    let src = img.clone();
    let w = img.w;
    img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
        for x in 0..w {
            let mut acc = [0f32; 4];
            for s in 0..steps {
                let t = s as f32 / (steps - 1) as f32 - 0.5;
                let p = src.sample_bilinear_clamped(x as f32 + 0.5 + dx * t, y as f32 + 0.5 + dy * t);
                for k in 0..4 {
                    acc[k] += p[k];
                }
            }
            for k in 0..4 {
                row[x * 4 + k] = acc[k] / steps as f32;
            }
        }
    });
}

pub(crate) fn median(img: &mut Image, r: isize) {
    let src = img.clone();
    let w = img.w;
    img.px.par_chunks_mut(w * 4).enumerate().for_each_init(Vec::new, |buf: &mut Vec<f32>, (y, row)| {
        for x in 0..w {
            for k in 0..4 {
                buf.clear();
                for dy in -r..=r {
                    for dx in -r..=r {
                        buf.push(src.get_clamped(x as isize + dx, y as isize + dy)[k]);
                    }
                }
                let mid = buf.len() / 2;
                buf.select_nth_unstable_by(mid, |a, b| a.total_cmp(b));
                row[x * 4 + k] = buf[mid];
            }
        }
    });
}

pub(crate) fn mosaic(img: &mut Image, bx: usize, by: usize) {
    let (w, h) = (img.w, img.h);
    let bw = (w as f32 / bx as f32).max(1.0);
    let bh = (h as f32 / by as f32).max(1.0);
    let src = img.clone();
    img.px.par_chunks_mut(w * 4).enumerate().for_each(|(y, row)| {
        let cy = ((y as f32 / bh).floor() + 0.5) * bh;
        for x in 0..w {
            let cxp = ((x as f32 / bw).floor() + 0.5) * bw;
            let p = src.sample_bilinear_clamped(cxp, cy);
            row[x * 4..x * 4 + 4].copy_from_slice(&p);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use lumiere_codecbase_project::find_effect;

    fn cx() -> FxCtx<'static> {
        FxCtx { t: Tick::ZERO, px_scale: 1.0, seconds: 0.0, timecode: "00:00:01:00", clip_name: "x", project: None, env: None }
    }

    #[test]
    fn every_effect_runs_and_stays_finite() {
        for def in lumiere_codecbase_project::effect_defs() {
            if def.kind != lumiere_codecbase_project::EffectKind::Video || def.intrinsic {
                continue;
            }
            let mut img = Image::filled(24, 16, [0.2, 0.4, 0.1, 1.0]);
            let mut e = def.instance();
            // push params away from identity so code paths run
            for (id, p) in e.params.iter_mut() {
                if let ParamValue::Float(v) = &mut p.value
                    && let Some(lumiere_codecbase_project::ParamKind::Float { soft_max, .. }) = def.param(id).map(|d| &d.kind)
                {
                    *v = (*v + soft_max * 0.3).min(*soft_max);
                }
            }
            apply(&mut img, &e, &cx());
            assert!(img.px.iter().all(|v| v.is_finite()), "{}", def.id);
        }
    }

    #[test]
    fn gaussian_preserves_mean_and_blurs() {
        let mut img = Image::new(64, 64);
        for y in 28..36 {
            for x in 28..36 {
                let i = (y * 64 + x) * 4;
                img.px[i..i + 4].copy_from_slice(&[1.0, 1.0, 1.0, 1.0]);
            }
        }
        let sum0: f32 = img.px.iter().sum();
        gaussian(&mut img, 4.0, 4.0, false);
        let sum1: f32 = img.px.iter().sum();
        assert!((sum0 - sum1).abs() / sum0 < 0.02, "{sum0} {sum1}");
        assert!(img.get(32, 32)[0] < 0.9 && img.get(24, 32)[0] > 0.01);
    }

    #[test]
    fn flip_and_crop() {
        let mut img = Image::new(4, 1);
        img.px[0..4].copy_from_slice(&[1.0, 0.0, 0.0, 1.0]);
        apply(&mut img, &find_effect("horizontal_flip").unwrap().instance(), &cx());
        assert_eq!(img.get(3, 0), [1.0, 0.0, 0.0, 1.0]);
        let mut e = find_effect("crop").unwrap().instance();
        e.params.get_mut("right").unwrap().value = ParamValue::Float(50.0);
        apply(&mut img, &e, &cx());
        assert_eq!(img.get(3, 0)[3], 0.0);
    }

    #[test]
    fn identity_lumetri_is_identity() {
        let mut img = Image::filled(8, 8, [0.18, 0.3, 0.05, 1.0]);
        let before = img.clone();
        apply(&mut img, &find_effect("lumetri").unwrap().instance(), &cx());
        for (a, b) in img.px.iter().zip(&before.px) {
            assert!((a - b).abs() < 2e-3, "{a} {b}");
        }
    }
}

/// Monotone cubic (Fritsch–Carlson) interpolation through sorted control points, as a LUT of `n`
/// entries over x ∈ [0, 1]. Monotone curves never overshoot, which keeps tone curves well-behaved.
pub fn curve_lut(points: &[[f32; 2]], n: usize) -> Vec<f32> {
    let mut pts: Vec<[f32; 2]> = points.to_vec();
    pts.sort_by(|a, b| a[0].total_cmp(&b[0]));
    pts.dedup_by(|a, b| (a[0] - b[0]).abs() < 1e-6);
    if pts.len() < 2 {
        let y = pts.first().map_or(0.0, |p| p[1]);
        return if pts.is_empty() { (0..n).map(|i| i as f32 / (n - 1) as f32).collect() } else { vec![y; n] };
    }
    let m = pts.len();
    let d: Vec<f32> = (0..m - 1).map(|i| (pts[i + 1][1] - pts[i][1]) / (pts[i + 1][0] - pts[i][0]).max(1e-6)).collect();
    let mut t = vec![0f32; m];
    t[0] = d[0];
    t[m - 1] = d[m - 2];
    for i in 1..m - 1 {
        t[i] = if d[i - 1] * d[i] <= 0.0 { 0.0 } else { (d[i - 1] + d[i]) / 2.0 };
    }
    for i in 0..m - 1 {
        if d[i].abs() < 1e-9 {
            t[i] = 0.0;
            t[i + 1] = 0.0;
            continue;
        }
        let a = t[i] / d[i];
        let b = t[i + 1] / d[i];
        let h = a * a + b * b;
        if h > 9.0 {
            let k = 3.0 / h.sqrt();
            t[i] = k * a * d[i];
            t[i + 1] = k * b * d[i];
        }
    }
    (0..n)
        .map(|j| {
            let x = j as f32 / (n - 1) as f32;
            if x <= pts[0][0] {
                return pts[0][1];
            }
            if x >= pts[m - 1][0] {
                return pts[m - 1][1];
            }
            let i = pts.partition_point(|p| p[0] <= x) - 1;
            let hh = pts[i + 1][0] - pts[i][0];
            let u = (x - pts[i][0]) / hh;
            let (h00, h10, h01, h11) = (2.0 * u * u * u - 3.0 * u * u + 1.0, u * u * u - 2.0 * u * u + u, -2.0 * u * u * u + 3.0 * u * u, u * u * u - u * u);
            h00 * pts[i][1] + h10 * hh * t[i] + h01 * pts[i + 1][1] + h11 * hh * t[i + 1]
        })
        .collect()
}

/// Periodic (hue) curve: points around the colour wheel, neutral 0.5 where there are none.
fn hue_lut(points: &[[f32; 2]], n: usize) -> Option<Vec<f32>> {
    if points.is_empty() {
        return None;
    }
    // wrap: repeat points one period left and right, then sample 0..1
    let mut ext = Vec::new();
    for off in [-1.0f32, 0.0, 1.0] {
        for p in points {
            ext.push([p[0] + off, p[1]]);
        }
    }
    let lut = curve_lut(&ext.iter().map(|p| [(p[0] + 1.0) / 3.0, p[1]]).collect::<Vec<_>>(), n * 3);
    Some(lut[n..2 * n].to_vec())
}

fn curve_param(e: &EffectInstance, id: &str) -> Option<Vec<[f32; 2]>> {
    e.param(id).and_then(|p| p.value.as_curve().map(|c| c.to_vec()))
}

fn is_identity_curve(c: &[[f32; 2]]) -> bool {
    c.iter().all(|p| (p[0] - p[1]).abs() < 1e-4)
}

/// Wheel offset (zero-mean RGB direction for a wheel position).
fn wheel_rgb(v: Vec2) -> [f32; 3] {
    let len = (v.x * v.x + v.y * v.y).sqrt().min(1.0) as f32;
    if len < 1e-5 {
        return [0.0; 3];
    }
    let a = (v.y).atan2(v.x) as f32;
    let tau = std::f32::consts::TAU;
    [a.cos() * len, (a - tau / 3.0).cos() * len, (a + tau / 3.0).cos() * len]
}

/// Looks: our own procedural grades (no third-party LUTs), applied on display-encoded colour.
pub fn apply_look(look: u32, c: [f32; 3]) -> [f32; 3] {
    let l = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    let mix = |a: [f32; 3], b: [f32; 3], k: f32| [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k];
    let s_curve = |v: f32, k: f32| {
        let x = v.clamp(0.0, 1.0);
        x + (x * x * (3.0 - 2.0 * x) - x) * k
    };
    match look {
        1 => {
            // teal shadows, orange highlights
            let shadow = [0.0, 0.08, 0.1];
            let high = [0.1, 0.04, -0.06];
            let c = [c[0] + shadow[0] * (1.0 - l) + high[0] * l, c[1] + shadow[1] * (1.0 - l) + high[1] * l, c[2] + shadow[2] * (1.0 - l) + high[2] * l];
            c.map(|v| s_curve(v, 0.35))
        }
        2 => {
            let c = [c[0] * 1.06 + 0.02, c[1] * 1.0 + 0.01, c[2] * 0.9];
            mix(c, [l, l, l], 0.15).map(|v| v * 0.94 + 0.04)
        }
        3 => [c[0] * 0.9, c[1] * 0.98, c[2] * 1.1 + 0.02].map(|v| s_curve(v, 0.2)),
        4 => mix(c, [l, l, l], 0.55).map(|v| s_curve(v, 0.6)),
        5 => mix(c, [l, l, l], 0.25).map(|v| 0.08 + v * 0.84),
        6 => [l, l, l].map(|v| s_curve(v, 0.3)),
        7 => [c[0] * 1.1 + 0.03, c[1] * 1.02 + 0.01, c[2] * 0.82].map(|v| s_curve(v, 0.15)),
        8 => [c[0] * 0.75, c[1] * 0.85, c[2] * 1.15].map(|v| v * 0.8),
        _ => c,
    }
}

fn lumetri_advanced(img: &mut Image, e: &EffectInstance, cx: &FxCtx) {
    const N: usize = 1024;
    let (creative_on, curves_on, wheels_on) = (on(e, "creative_on"), on(e, "curves_on"), on(e, "wheels_on"));
    let lut = |id: &str| curve_param(e, id).filter(|c| curves_on && !is_identity_curve(c)).map(|c| curve_lut(&c, N));
    let (luma_c, red_c, green_c, blue_c) = (lut("curve_luma"), lut("curve_red"), lut("curve_green"), lut("curve_blue"));
    let hue = |id: &str| curve_param(e, id).filter(|_| curves_on).and_then(|c| hue_lut(&c, N));
    let (hvs, hvh, hvl, lvs, svs) = (hue("hue_vs_sat"), hue("hue_vs_hue"), hue("hue_vs_luma"), hue("luma_vs_sat"), hue("sat_vs_sat"));
    // Creative ▸ Look: a LUT ("Browse…" / built-in) takes precedence over the procedural menu
    let look_lut = if creative_on { crate::luts::resolve(cx.project, text(e, "look_lut")) } else { None };
    let look = if creative_on && look_lut.is_none() { choice(e, "look") } else { 0 };
    let look_k = f(e, "look_intensity", cx) / 100.0;
    let v2 = |id: &str| e.param(id).map(|p| p.vec2_at(cx.t)).unwrap_or_default();
    let (ws, wm, wh) = (wheel_rgb(v2("wheel_shadows")), wheel_rgb(v2("wheel_midtones")), wheel_rgb(v2("wheel_highlights")));
    let (ls, lm, lh) = (f(e, "wheel_shadows_l", cx) / 100.0, f(e, "wheel_midtones_l", cx) / 100.0, f(e, "wheel_highlights_l", cx) / 100.0);
    let wheels = wheels_on && (ws.iter().chain(&wm).chain(&wh).any(|v| v.abs() > 1e-5) || ls.abs() + lm.abs() + lh.abs() > 1e-5);
    let hsl_on = b(e, "hsl_on");
    let any = luma_c.is_some()
        || red_c.is_some()
        || green_c.is_some()
        || blue_c.is_some()
        || hvs.is_some()
        || hvh.is_some()
        || hvl.is_some()
        || lvs.is_some()
        || svs.is_some()
        || look > 0
        || look_lut.is_some()
        || wheels
        || hsl_on;
    if !any {
        return;
    }
    let (hc, hr) = (f(e, "hsl_hue", cx) / 360.0, (f(e, "hsl_hue_range", cx) / 360.0).max(1e-3));
    let (smin, lmin, lmax) = (f(e, "hsl_sat_min", cx) / 100.0, f(e, "hsl_luma_min", cx) / 100.0, f(e, "hsl_luma_max", cx) / 100.0);
    let soft = (f(e, "hsl_soft", cx) / 100.0 * 0.3).max(0.01);
    let show_mask = choice(e, "hsl_show_mask");
    let (htemp, htint, hsat, hshift) =
        (f(e, "hsl_temp", cx) / 100.0, f(e, "hsl_tint", cx) / 100.0, f(e, "hsl_sat", cx) / 100.0, f(e, "hsl_hue_shift", cx) / 360.0);
    let sample = |l: &Vec<f32>, x: f32| {
        let p = x.clamp(0.0, 1.0) * (N - 1) as f32;
        let i = p as usize;
        let j = (i + 1).min(N - 1);
        l[i] + (l[j] - l[i]) * (p - i as f32)
    };
    img.map_rgb(|c, _, _| {
        let mut v = enc(c);
        if let Some(l) = &look_lut {
            let lk = l.apply(v);
            v = [v[0] + (lk[0] - v[0]) * look_k, v[1] + (lk[1] - v[1]) * look_k, v[2] + (lk[2] - v[2]) * look_k];
        } else if look > 0 {
            let lk = apply_look(look, v);
            v = [v[0] + (lk[0] - v[0]) * look_k, v[1] + (lk[1] - v[1]) * look_k, v[2] + (lk[2] - v[2]) * look_k];
        }
        if wheels {
            let l = (0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]).clamp(0.0, 1.0);
            let wsh = (1.0 - l).powi(2);
            let whi = l * l;
            let wmid = (1.0 - wsh - whi).max(0.0);
            for k in 0..3 {
                // lift (shadows), gamma-ish (midtones), gain (highlights)
                v[k] += (ws[k] * 0.3 + ls * 0.3) * wsh;
                v[k] += (wm[k] * 0.3 + lm * 0.3) * wmid;
                v[k] *= 1.0 + (wh[k] * 0.5 + lh * 0.5) * whi;
            }
        }
        if let Some(l) = &luma_c {
            v = v.map(|q| sample(l, q));
        }
        if let Some(l) = &red_c {
            v[0] = sample(l, v[0]);
        }
        if let Some(l) = &green_c {
            v[1] = sample(l, v[1]);
        }
        if let Some(l) = &blue_c {
            v[2] = sample(l, v[2]);
        }
        if hvs.is_some() || hvh.is_some() || hvl.is_some() || lvs.is_some() || svs.is_some() {
            let mut h = rgb_to_hsl(v[0].clamp(0.0, 1.0), v[1].clamp(0.0, 1.0), v[2].clamp(0.0, 1.0));
            let (h0, s0, l0) = (h[0], h[1], h[2]);
            if let Some(t) = &hvh {
                h[0] = (h[0] + (sample(t, h0) - 0.5)).rem_euclid(1.0);
            }
            let mut sm = 1.0;
            if let Some(t) = &hvs {
                sm *= sample(t, h0) * 2.0;
            }
            if let Some(t) = &lvs {
                sm *= sample(t, l0) * 2.0;
            }
            if let Some(t) = &svs {
                sm *= sample(t, s0) * 2.0;
            }
            h[1] = (h[1] * sm).clamp(0.0, 1.0);
            if let Some(t) = &hvl {
                h[2] = (h[2] + (sample(t, h0) - 0.5) * 0.5).clamp(0.0, 1.0);
            }
            v = hsl_to_rgb(h[0], h[1], h[2]);
        }
        if hsl_on {
            let h = rgb_to_hsl(v[0].clamp(0.0, 1.0), v[1].clamp(0.0, 1.0), v[2].clamp(0.0, 1.0));
            let dh = (h[0] - hc).abs().min(1.0 - (h[0] - hc).abs());
            let mh = 1.0 - ((dh - hr / 2.0) / soft).clamp(0.0, 1.0);
            let ms = ((h[1] - smin) / soft).clamp(0.0, 1.0);
            let ml = ((h[2] - lmin) / soft).clamp(0.0, 1.0).min(((lmax - h[2]) / soft).clamp(0.0, 1.0));
            let m = mh * ms * ml;
            match show_mask {
                1 => {
                    let g = h[2];
                    v = [g + (v[0] - g) * m, g + (v[1] - g) * m, g + (v[2] - g) * m];
                }
                2 => v = v.map(|q| q * m),
                3 => v = [m, m, m],
                _ => {
                    let mut hh = h;
                    hh[0] = (hh[0] + hshift).rem_euclid(1.0);
                    hh[1] = (hh[1] * hsat).clamp(0.0, 1.0);
                    let mut c2 = hsl_to_rgb(hh[0], hh[1], hh[2]);
                    c2[0] *= 1.0 + 0.25 * htemp;
                    c2[2] *= 1.0 - 0.25 * htemp;
                    c2[1] *= 1.0 - 0.2 * htint;
                    v = [v[0] + (c2[0] - v[0]) * m, v[1] + (c2[1] - v[1]) * m, v[2] + (c2[2] - v[2]) * m];
                }
            }
        }
        dec(v)
    });
}

#[cfg(test)]
mod curve_tests {
    use super::*;

    #[test]
    fn identity_and_monotone() {
        let l = curve_lut(&[[0.0, 0.0], [1.0, 1.0]], 64);
        for (i, v) in l.iter().enumerate() {
            assert!((v - i as f32 / 63.0).abs() < 1e-4);
        }
        let s = curve_lut(&[[0.0, 0.0], [0.25, 0.15], [0.75, 0.85], [1.0, 1.0]], 256);
        assert!(s.windows(2).all(|w| w[1] >= w[0] - 1e-6), "monotone");
        let h = hue_lut(&[[0.0, 0.8], [0.5, 0.2]], 64).unwrap();
        assert!((h[0] - h[63]).abs() < 0.05, "periodic");
    }
}
