//! Fractional sample interpolation (8.5.3.3.3) and weighted sample prediction (8.5.3.3.4).

use crate::picture::Frame;
use crate::spec_tables::{CHROMA_FILTER, LUMA_FILTER};

/// Max prediction block size + filter margin.
const WIN: usize = 64 + 7;

/// Luma prediction samples (14-bit intermediate precision) of a w x h block at (x, y) displaced by the
/// quarter-sample motion vector `mv`, into `out` (stride w).
pub fn mc_luma(f: &Frame, x: i32, y: i32, mv: [i16; 2], w: usize, h: usize, out: &mut [i16]) {
    let bd = f.bit_depth;
    let (fx, fy) = ((mv[0] & 3) as usize, (mv[1] & 3) as usize);
    let xi = x + (mv[0] as i32 >> 2);
    let yi = y + (mv[1] as i32 >> 2);
    let shift1 = bd.min(12) - 8;
    let shift3 = 14 - bd;
    let mut win = [0i16; WIN * WIN];
    if fx == 0 && fy == 0 {
        f.luma_window(xi, yi, w, h, &mut win, WIN);
        for r in 0..h {
            for c in 0..w {
                out[r * w + c] = win[r * WIN + c] << shift3;
            }
        }
        return;
    }
    f.luma_window(xi - 3, yi - 3, w + 7, h + 7, &mut win, WIN);
    let hf = &LUMA_FILTER[fx];
    let vf = &LUMA_FILTER[fy];
    if fy == 0 {
        for r in 0..h {
            let row = &win[(r + 3) * WIN..];
            for c in 0..w {
                let s: i32 = (0..8).map(|k| hf[k] as i32 * row[c + k] as i32).sum();
                out[r * w + c] = (s >> shift1) as i16;
            }
        }
    } else if fx == 0 {
        for r in 0..h {
            for c in 0..w {
                let s: i32 = (0..8).map(|k| vf[k] as i32 * win[(r + k) * WIN + c + 3] as i32).sum();
                out[r * w + c] = (s >> shift1) as i16;
            }
        }
    } else {
        let mut tmp = [0i16; WIN * WIN];
        for r in 0..h + 7 {
            let row = &win[r * WIN..];
            for c in 0..w {
                let s: i32 = (0..8).map(|k| hf[k] as i32 * row[c + k] as i32).sum();
                tmp[r * WIN + c] = (s >> shift1) as i16;
            }
        }
        for r in 0..h {
            for c in 0..w {
                let s: i32 = (0..8).map(|k| vf[k] as i32 * tmp[(r + k) * WIN + c] as i32).sum();
                out[r * w + c] = (s >> 6) as i16;
            }
        }
    }
}

/// Chroma prediction samples of a w x h chroma block at chroma position (x, y) with a 1/8-sample
/// vector (4:2:0).
pub fn mc_chroma(f: &Frame, c: usize, x: i32, y: i32, mv: [i16; 2], w: usize, h: usize, out: &mut [i16]) {
    let bd = f.bit_depth_c;
    let (fx, fy) = ((mv[0] & 7) as usize, (mv[1] & 7) as usize);
    let xi = x + (mv[0] as i32 >> 3);
    let yi = y + (mv[1] as i32 >> 3);
    let shift1 = bd.min(12) - 8;
    let shift3 = 14 - bd;
    let mut win = [0i16; WIN * WIN];
    if fx == 0 && fy == 0 {
        f.chroma_window(c, xi, yi, w, h, &mut win, WIN);
        for r in 0..h {
            for cc in 0..w {
                out[r * w + cc] = win[r * WIN + cc] << shift3;
            }
        }
        return;
    }
    f.chroma_window(c, xi - 1, yi - 1, w + 3, h + 3, &mut win, WIN);
    let hf = &CHROMA_FILTER[fx];
    let vf = &CHROMA_FILTER[fy];
    if fy == 0 {
        for r in 0..h {
            let row = &win[(r + 1) * WIN..];
            for cc in 0..w {
                let s: i32 = (0..4).map(|k| hf[k] as i32 * row[cc + k] as i32).sum();
                out[r * w + cc] = (s >> shift1) as i16;
            }
        }
    } else if fx == 0 {
        for r in 0..h {
            for cc in 0..w {
                let s: i32 = (0..4).map(|k| vf[k] as i32 * win[(r + k) * WIN + cc + 1] as i32).sum();
                out[r * w + cc] = (s >> shift1) as i16;
            }
        }
    } else {
        let mut tmp = [0i16; WIN * WIN];
        for r in 0..h + 3 {
            let row = &win[r * WIN..];
            for cc in 0..w {
                let s: i32 = (0..4).map(|k| hf[k] as i32 * row[cc + k] as i32).sum();
                tmp[r * WIN + cc] = (s >> shift1) as i16;
            }
        }
        for r in 0..h {
            for cc in 0..w {
                let s: i32 = (0..4).map(|k| vf[k] as i32 * tmp[(r + k) * WIN + cc] as i32).sum();
                out[r * w + cc] = (s >> 6) as i16;
            }
        }
    }
}

/// Default weighted prediction, one list (8-262).
pub fn put_uni(p: &[i16], w: usize, h: usize, bd: u32, dst: &mut [u16], ds: usize) {
    let shift = 14 - bd;
    let off = if shift > 0 { 1 << (shift - 1) } else { 0 };
    let max = (1i32 << bd) - 1;
    for r in 0..h {
        for c in 0..w {
            dst[r * ds + c] = ((p[r * w + c] as i32 + off) >> shift).clamp(0, max) as u16;
        }
    }
}

/// Default weighted prediction, bi-prediction (8-263).
pub fn put_bi(p0: &[i16], p1: &[i16], w: usize, h: usize, bd: u32, dst: &mut [u16], ds: usize) {
    let shift = 15 - bd;
    let off = 1 << (shift - 1);
    let max = (1i32 << bd) - 1;
    for r in 0..h {
        for c in 0..w {
            let i = r * w + c;
            dst[r * ds + c] = ((p0[i] as i32 + p1[i] as i32 + off) >> shift).clamp(0, max) as u16;
        }
    }
}

/// Explicit weighted prediction, one list (8-265). `o` is the offset already scaled to the bit depth.
pub fn put_weighted_uni(p: &[i16], w: usize, h: usize, bd: u32, log2wd: u32, wt: i32, o: i32, dst: &mut [u16], ds: usize) {
    let max = (1i32 << bd) - 1;
    for r in 0..h {
        for c in 0..w {
            let v = p[r * w + c] as i32 * wt;
            let v = if log2wd >= 1 { ((v + (1 << (log2wd - 1))) >> log2wd) + o } else { v + o };
            dst[r * ds + c] = v.clamp(0, max) as u16;
        }
    }
}

/// Explicit weighted bi-prediction (8-266).
pub fn put_weighted_bi(p0: &[i16], p1: &[i16], w: usize, h: usize, bd: u32, log2wd: u32, w0: i32, w1: i32, o0: i32, o1: i32, dst: &mut [u16], ds: usize) {
    let max = (1i32 << bd) - 1;
    for r in 0..h {
        for c in 0..w {
            let i = r * w + c;
            let v = (p0[i] as i32 * w0 + p1[i] as i32 * w1 + ((o0 + o1 + 1) << log2wd)) >> (log2wd + 1);
            dst[r * ds + c] = v.clamp(0, max) as u16;
        }
    }
}
