//! Block inter prediction process (8.5.2.4): separable 8-tap sub-sample interpolation with edge
//! clamping, for unscaled (step 16) and scaled references.

use crate::tables::SUBPEL_FILTERS;

/// Filter taps of `filter` (0..3) at sub-sample position `frac` (0..15).
#[inline(always)]
fn taps(filter: u8, frac: usize) -> &'static [i16] {
    let o = (filter as usize * 16 + frac) * 8;
    &SUBPEL_FILTERS[o..o + 8]
}

/// A reference plane: samples addressed `data[y * stride + x]`, valid for x <= last_x, y <= last_y.
pub struct RefPlane<'a> {
    pub data: &'a [u16],
    pub stride: usize,
    pub last_x: i32,
    pub last_y: i32,
}

/// Predict a `w` x `h` block whose top-left sample is at (`x`, `y`) in 1/16 sample units of the
/// reference, stepping `step_x` / `step_y` (16 = unscaled). Output samples go to `out` (stride
/// `w`). `tmp` must hold at least `(h * step_y / 16 + 8) * w` entries, `win` at least
/// `(w + 7) * (h + 7)` (edge-clamped source window).
#[allow(clippy::too_many_arguments)]
pub fn predict(
    r: &RefPlane,
    x: i32,
    y: i32,
    step_x: i32,
    step_y: i32,
    w: usize,
    h: usize,
    filter: u8,
    bit_depth: u8,
    out: &mut [u16],
    tmp: &mut [u16],
    win: &mut [u16],
) {
    if step_x == 16 && step_y == 16 {
        predict_unscaled(r, x, y, w, h, filter, bit_depth, out, tmp, win);
    } else {
        predict_scaled(r, x, y, step_x, step_y, w, h, filter, bit_depth, out, tmp);
    }
}

/// Copy the (w + 7) x (h + 7) source window starting at (x0 - 3, y0 - 3) with edge clamping.
fn fetch_window(r: &RefPlane, x0: i32, y0: i32, w: usize, h: usize, win: &mut [u16]) -> usize {
    let ws = w + 7;
    for row in 0..h + 7 {
        let yy = (y0 - 3 + row as i32).clamp(0, r.last_y) as usize;
        let src = &r.data[yy * r.stride..];
        let dst = &mut win[row * ws..row * ws + ws];
        let xs = x0 - 3;
        if xs >= 0 && xs + ws as i32 - 1 <= r.last_x {
            dst.copy_from_slice(&src[xs as usize..xs as usize + ws]);
        } else {
            for (c, d) in dst.iter_mut().enumerate() {
                *d = src[(xs + c as i32).clamp(0, r.last_x) as usize];
            }
        }
    }
    ws
}

#[allow(clippy::too_many_arguments)]
fn predict_unscaled(r: &RefPlane, x: i32, y: i32, w: usize, h: usize, filter: u8, bit_depth: u8, out: &mut [u16], tmp: &mut [u16], win: &mut [u16]) {
    let (x0, y0) = (x >> 4, y >> 4);
    let (fx, fy) = ((x & 15) as usize, (y & 15) as usize);
    let max = (1i32 << bit_depth) - 1;
    let inside = x0 - 3 >= 0 && y0 - 3 >= 0 && x0 + w as i32 + 4 <= r.last_x && y0 + h as i32 + 4 <= r.last_y;
    // Source view with origin at (x0 - 3, y0 - 3).
    let (src, ss, so): (&[u16], usize, usize) = if inside {
        (r.data, r.stride, (y0 - 3) as usize * r.stride + (x0 - 3) as usize)
    } else {
        let ws = fetch_window(r, x0, y0, w, h, win);
        (&win[..], ws, 0)
    };
    match (fx, fy) {
        (0, 0) => {
            for i in 0..h {
                let s = so + (i + 3) * ss + 3;
                out[i * w..i * w + w].copy_from_slice(&src[s..s + w]);
            }
        }
        (_, 0) => {
            let f = taps(filter, fx);
            for i in 0..h {
                filter_h(&src[so + (i + 3) * ss..], f, max, &mut out[i * w..i * w + w]);
            }
        }
        (0, _) => {
            let f = taps(filter, fy);
            for i in 0..h {
                filter_v(&src[so + i * ss + 3..], ss, f, max, &mut out[i * w..i * w + w]);
            }
        }
        _ => {
            let fh = taps(filter, fx);
            let fv = taps(filter, fy);
            for i in 0..h + 7 {
                filter_h(&src[so + i * ss..], fh, max, &mut tmp[i * w..i * w + w]);
            }
            for i in 0..h {
                filter_v(&tmp[i * w..], w, fv, max, &mut out[i * w..i * w + w]);
            }
        }
    }
}

/// Horizontal 8-tap filter of one row: `o[j]` from `s[j..j + 8]`. The taps are the outer loop
/// so that the row vectorizes (same sums, same order of the exact integer additions).
#[inline(always)]
fn filter_h(s: &[u16], f: &[i16], max: i32, o: &mut [u16]) {
    match o.len() {
        4 => filter_h_n::<4>(s, f, max, o),
        8 => filter_h_n::<8>(s, f, max, o),
        16 => filter_h_n::<16>(s, f, max, o),
        32 => filter_h_n::<32>(s, f, max, o),
        64 => filter_h_n::<64>(s, f, max, o),
        _ => {
            for (j, d) in o.iter_mut().enumerate() {
                let acc: i32 = (0..8).map(|t| f[t] as i32 * s[j + t] as i32).sum();
                *d = ((acc + 64) >> 7).clamp(0, max) as u16;
            }
        }
    }
}

#[inline(always)]
fn filter_h_n<const W: usize>(s: &[u16], f: &[i16], max: i32, o: &mut [u16]) {
    let o: &mut [u16; W] = (&mut o[..W]).try_into().expect("row");
    let mut acc = [64i32; W];
    for t in 0..8 {
        let c = f[t] as i32;
        let p: &[u16; W] = s[t..t + W].try_into().expect("row");
        for j in 0..W {
            acc[j] += c * p[j] as i32;
        }
    }
    for j in 0..W {
        o[j] = (acc[j] >> 7).clamp(0, max) as u16;
    }
}

/// Vertical 8-tap filter of one row: `o[j]` from `s[j + t * ss]`, t = 0..8.
#[inline(always)]
fn filter_v(s: &[u16], ss: usize, f: &[i16], max: i32, o: &mut [u16]) {
    match o.len() {
        4 => filter_v_n::<4>(s, ss, f, max, o),
        8 => filter_v_n::<8>(s, ss, f, max, o),
        16 => filter_v_n::<16>(s, ss, f, max, o),
        32 => filter_v_n::<32>(s, ss, f, max, o),
        64 => filter_v_n::<64>(s, ss, f, max, o),
        _ => {
            for (j, d) in o.iter_mut().enumerate() {
                let acc: i32 = (0..8).map(|t| f[t] as i32 * s[j + t * ss] as i32).sum();
                *d = ((acc + 64) >> 7).clamp(0, max) as u16;
            }
        }
    }
}

#[inline(always)]
fn filter_v_n<const W: usize>(s: &[u16], ss: usize, f: &[i16], max: i32, o: &mut [u16]) {
    let o: &mut [u16; W] = (&mut o[..W]).try_into().expect("row");
    let mut acc = [64i32; W];
    for t in 0..8 {
        let c = f[t] as i32;
        let p: &[u16; W] = s[t * ss..t * ss + W].try_into().expect("row");
        for j in 0..W {
            acc[j] += c * p[j] as i32;
        }
    }
    for j in 0..W {
        o[j] = (acc[j] >> 7).clamp(0, max) as u16;
    }
}

/// General process of 8.5.2.4 (any step).
#[allow(clippy::too_many_arguments)]
fn predict_scaled(r: &RefPlane, x: i32, y: i32, step_x: i32, step_y: i32, w: usize, h: usize, filter: u8, bit_depth: u8, out: &mut [u16], tmp: &mut [u16]) {
    let max = (1i32 << bit_depth) - 1;
    let ih = ((((h as i32 - 1) * step_y + 15) >> 4) + 8) as usize;
    for row in 0..ih {
        let yy = ((y >> 4) + row as i32 - 3).clamp(0, r.last_y) as usize;
        let src = &r.data[yy * r.stride..];
        for c in 0..w {
            let p = x + step_x * c as i32;
            let f = taps(filter, (p & 15) as usize);
            let mut acc = 0i32;
            for t in 0..8 {
                acc += f[t] as i32 * src[((p >> 4) + t as i32 - 3).clamp(0, r.last_x) as usize] as i32;
            }
            tmp[row * w + c] = ((acc + 64) >> 7).clamp(0, max) as u16;
        }
    }
    for rr in 0..h {
        let p = (y & 15) + step_y * rr as i32;
        let f = taps(filter, (p & 15) as usize);
        let base = (p >> 4) as usize;
        for c in 0..w {
            let mut acc = 0i32;
            for t in 0..8 {
                acc += f[t] as i32 * tmp[(base + t) * w + c] as i32;
            }
            out[rr * w + c] = ((acc + 64) >> 7).clamp(0, max) as u16;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scaled_path_with_unit_step_matches_unscaled() {
        let (w, h) = (40usize, 30usize);
        let data: Vec<u16> = (0..w * h).map(|i| ((i * 37 + (i / w) * 11) % 256) as u16).collect();
        let r = RefPlane { data: &data, stride: w, last_x: w as i32 - 1, last_y: h as i32 - 1 };
        let mut tmp = vec![0u16; 80 * 80];
        for filter in 0..4u8 {
            for &(x, y) in &[(0, 0), (5, 3), (16 * 7 + 5, 16 * 9 + 11), (-40, -3), (16 * 36 + 1, 16 * 25 + 15), (16 * 10, 16 * 10 + 8)] {
                for &(bw, bh) in &[(4usize, 4usize), (8, 8), (16, 8)] {
                    let mut a = vec![0u16; bw * bh];
                    let mut b = vec![0u16; bw * bh];
                    predict_unscaled(&r, x, y, bw, bh, filter, 8, &mut a, &mut tmp, &mut [0u16; 71 * 71]);
                    predict_scaled(&r, x, y, 16, 16, bw, bh, filter, 8, &mut b, &mut tmp);
                    assert_eq!(a, b, "filter {filter} pos ({x},{y}) {bw}x{bh}");
                }
            }
        }
    }
}
