//! 2D inverse transforms and reconstruction (8.6.2, 8.7.2). The 1D DCT / ADST butterflies are
//! generated from the specification's step lists (`transform_gen.rs`); ADST4 (8.7.1.6) and the
//! Walsh-Hadamard transform (8.7.1.10) are written out here.

use crate::tables::{ADST_ADST, ADST_DCT, DCT_ADST, DCT_DCT};
use crate::transform_gen::{narrow, wide};

const SINPI_1_9: i64 = 5283;
const SINPI_2_9: i64 = 9929;
const SINPI_3_9: i64 = 13377;
const SINPI_4_9: i64 = 15212;

macro_rules! adst4 {
    ($name:ident, $t:ty) => {
        #[inline(always)]
        fn $name(x: [$t; 4]) -> [$t; 4] {
            let (s1_9, s2_9, s3_9, s4_9) = (SINPI_1_9 as $t, SINPI_2_9 as $t, SINPI_3_9 as $t, SINPI_4_9 as $t);
            let s0 = s1_9.wrapping_mul(x[0]);
            let s1 = s2_9.wrapping_mul(x[0]);
            let s2 = s3_9.wrapping_mul(x[1]);
            let s3 = s4_9.wrapping_mul(x[2]);
            let s4 = s1_9.wrapping_mul(x[2]);
            let s5 = s2_9.wrapping_mul(x[3]);
            let s6 = s4_9.wrapping_mul(x[3]);
            let v = x[0].wrapping_sub(x[2]).wrapping_add(x[3]);
            let s7 = s3_9.wrapping_mul(v);
            let x0 = s0.wrapping_add(s3).wrapping_add(s5);
            let x1 = s1.wrapping_sub(s4).wrapping_sub(s6);
            let x2 = s7;
            let x3 = s2;
            let r = |v: $t| v.wrapping_add(1 << 13) >> 14;
            [r(x0.wrapping_add(x3)), r(x1.wrapping_add(x3)), r(x2), r(x0.wrapping_add(x1).wrapping_sub(x3))]
        }
    };
}
adst4!(iadst4_narrow, i32);
adst4!(iadst4_wide, i64);

/// Inverse Walsh-Hadamard transform (8.7.1.10).
fn iwht4(x: [i32; 4], shift: u32) -> [i32; 4] {
    let mut a = x[0] >> shift;
    let mut c = x[1] >> shift;
    let mut d = x[2] >> shift;
    let mut b = x[3] >> shift;
    a = a.wrapping_add(c);
    d = d.wrapping_sub(b);
    let e = a.wrapping_sub(d) >> 1;
    b = e.wrapping_sub(b);
    c = e.wrapping_sub(c);
    a = a.wrapping_sub(b);
    d = d.wrapping_add(c);
    [a, b, c, d]
}

/// Apply a 1D transform of length `n` (4, 8, 16, 32) in place.
#[inline(always)]
fn tx1d_narrow(buf: &mut [i32], n: usize, adst: bool) {
    match (n, adst) {
        (4, false) => {
            let r = narrow::idct4(buf[..4].try_into().unwrap());
            buf[..4].copy_from_slice(&r);
        }
        (4, true) => {
            let r = iadst4_narrow(buf[..4].try_into().unwrap());
            buf[..4].copy_from_slice(&r);
        }
        (8, false) => {
            let r = narrow::idct8(buf[..8].try_into().unwrap());
            buf[..8].copy_from_slice(&r);
        }
        (8, true) => {
            let r = narrow::iadst8(buf[..8].try_into().unwrap());
            buf[..8].copy_from_slice(&r);
        }
        (16, false) => {
            let r = narrow::idct16(buf[..16].try_into().unwrap());
            buf[..16].copy_from_slice(&r);
        }
        (16, true) => {
            let r = narrow::iadst16(buf[..16].try_into().unwrap());
            buf[..16].copy_from_slice(&r);
        }
        _ => {
            let r = narrow::idct32(buf[..32].try_into().unwrap());
            buf[..32].copy_from_slice(&r);
        }
    }
}

#[inline(always)]
fn tx1d_wide(buf: &mut [i64], n: usize, adst: bool) {
    match (n, adst) {
        (4, false) => {
            let r = wide::idct4(buf[..4].try_into().unwrap());
            buf[..4].copy_from_slice(&r);
        }
        (4, true) => {
            let r = iadst4_wide(buf[..4].try_into().unwrap());
            buf[..4].copy_from_slice(&r);
        }
        (8, false) => {
            let r = wide::idct8(buf[..8].try_into().unwrap());
            buf[..8].copy_from_slice(&r);
        }
        (8, true) => {
            let r = wide::iadst8(buf[..8].try_into().unwrap());
            buf[..8].copy_from_slice(&r);
        }
        (16, false) => {
            let r = wide::idct16(buf[..16].try_into().unwrap());
            buf[..16].copy_from_slice(&r);
        }
        (16, true) => {
            let r = wide::iadst16(buf[..16].try_into().unwrap());
            buf[..16].copy_from_slice(&r);
        }
        _ => {
            let r = wide::idct32(buf[..32].try_into().unwrap());
            buf[..32].copy_from_slice(&r);
        }
    }
}

/// Clamp coefficients so that garbage streams cannot overflow the 32-bit arithmetic (conformant
/// streams keep far smaller values, 8 + BitDepth bits).
#[inline]
fn sat(v: i32, lim: i32) -> i32 {
    v.clamp(-lim, lim)
}

/// Inverse transform `coefs` (row-major n x n dequantized coefficients, `rows` = number of leading
/// rows that can hold non-zero values) and add the residual to `dst` (8.6.2 step 4).
#[allow(clippy::too_many_arguments)]
pub fn inverse_transform_add(coefs: &[i32], tx_size: u8, tx_type: u8, lossless: bool, eob: usize, rows: usize, bit_depth: u8, dst: &mut [u16], stride: usize) {
    let n = 4usize << tx_size;
    let max = (1i32 << bit_depth) - 1;
    if lossless {
        let mut t = [0i32; 16];
        for i in 0..4 {
            let r = iwht4(coefs[i * 4..i * 4 + 4].try_into().unwrap(), 2);
            t[i * 4..i * 4 + 4].copy_from_slice(&r);
        }
        for j in 0..4 {
            let r = iwht4([t[j], t[4 + j], t[8 + j], t[12 + j]], 0);
            for i in 0..4 {
                let p = &mut dst[i * stride + j];
                *p = (*p as i32).saturating_add(r[i]).clamp(0, max) as u16;
            }
        }
        return;
    }
    let shift = (tx_size as u32 + 4).min(6);
    let round = 1i32 << (shift - 1);
    if eob == 1 && tx_type == DCT_DCT {
        // Only the DC coefficient is present: every output of both passes is equal (8.7.2 with a
        // single non-zero input).
        let dc = coefs[0] as i64;
        let a = (dc * 11585 + (1 << 13)) >> 14;
        let b = (a * 11585 + (1 << 13)) >> 14;
        let v = ((b + round as i64) >> shift).clamp(-(1 << 20), 1 << 20) as i32;
        for i in 0..n {
            for p in dst[i * stride..i * stride + n].iter_mut() {
                *p = (*p as i32 + v).clamp(0, max) as u16;
            }
        }
        return;
    }
    let row_adst = matches!(tx_type, DCT_ADST | ADST_ADST);
    let col_adst = matches!(tx_type, ADST_DCT | ADST_ADST);
    let p = TxParams { row_adst, col_adst, rows, shift, round, max };
    match (tx_size, bit_depth == 8) {
        (0, true) => itx_narrow::<4>(coefs, &p, dst, stride),
        (1, true) => itx_narrow::<8>(coefs, &p, dst, stride),
        (2, true) => itx_narrow::<16>(coefs, &p, dst, stride),
        (_, true) => itx_narrow::<32>(coefs, &p, dst, stride),
        (0, false) => itx_wide::<4>(coefs, &p, dst, stride),
        (1, false) => itx_wide::<8>(coefs, &p, dst, stride),
        (2, false) => itx_wide::<16>(coefs, &p, dst, stride),
        (_, false) => itx_wide::<32>(coefs, &p, dst, stride),
    }
}

struct TxParams {
    row_adst: bool,
    col_adst: bool,
    rows: usize,
    shift: u32,
    round: i32,
    max: i32,
}

/// 2D inverse transform of size N with 32-bit intermediates (8-bit streams).
fn itx_narrow<const N: usize>(coefs: &[i32], p: &TxParams, dst: &mut [u16], stride: usize) {
    let lim = 1 << 24;
    let mut t = [[0i32; N]; N];
    for (i, row) in t.iter_mut().enumerate().take(p.rows.min(N)) {
        for (d, s) in row.iter_mut().zip(&coefs[i * N..i * N + N]) {
            *d = sat(*s, lim);
        }
        tx1d_narrow(row, N, p.row_adst);
        for v in row.iter_mut() {
            *v = sat(*v, lim);
        }
    }
    let mut col = [0i32; N];
    for j in 0..N {
        for i in 0..N {
            col[i] = t[i][j];
        }
        tx1d_narrow(&mut col, N, p.col_adst);
        for i in 0..N {
            let d = &mut dst[i * stride + j];
            *d = (*d as i32 + (col[i].wrapping_add(p.round) >> p.shift).clamp(-(1 << 20), 1 << 20)).clamp(0, p.max) as u16;
        }
    }
}

/// 2D inverse transform of size N with 64-bit intermediates (high bit depth).
fn itx_wide<const N: usize>(coefs: &[i32], p: &TxParams, dst: &mut [u16], stride: usize) {
    let lim = 1i64 << 40;
    let mut t = [[0i64; N]; N];
    for (i, row) in t.iter_mut().enumerate().take(p.rows.min(N)) {
        for (d, s) in row.iter_mut().zip(&coefs[i * N..i * N + N]) {
            *d = *s as i64;
        }
        tx1d_wide(row, N, p.row_adst);
        for v in row.iter_mut() {
            *v = (*v).clamp(-lim, lim);
        }
    }
    let mut col = [0i64; N];
    for j in 0..N {
        for i in 0..N {
            col[i] = t[i][j];
        }
        tx1d_wide(&mut col, N, p.col_adst);
        for i in 0..N {
            let d = &mut dst[i * stride + j];
            let r = ((col[i].clamp(-lim, lim) + p.round as i64) >> p.shift).clamp(-(1 << 30), 1 << 30) as i32;
            *d = (*d as i32 + r).clamp(0, p.max) as u16;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Reference inverse DCT (orthonormal, scaled like VP9: DC gain 1/sqrt(2) per pass times
    /// sqrt(2) ... ) — compare basis functions up to rounding.
    fn float_idct(input: &[f64]) -> Vec<f64> {
        let n = input.len();
        (0..n)
            .map(|x| {
                let mut s = input[0] / 2f64.sqrt();
                for (k, &v) in input.iter().enumerate().skip(1) {
                    s += v * ((2 * x + 1) as f64 * k as f64 * std::f64::consts::PI / (2 * n) as f64).cos();
                }
                s
            })
            .collect()
    }

    #[test]
    fn idct_matches_float_reference() {
        for n in [4usize, 8, 16, 32] {
            for k in 0..n {
                let mut buf = vec![0i32; n];
                buf[k] = 1000;
                tx1d_narrow(&mut buf, n, false);
                let mut inp = vec![0f64; n];
                inp[k] = 1000.0;
                let r = float_idct(&inp);
                for (a, b) in buf.iter().zip(&r) {
                    assert!((*a as f64 - b).abs() <= 3.0, "n {n} k {k}: {buf:?} vs {r:?}");
                }
                let mut w: Vec<i64> = vec![0; n];
                w[k] = 1000;
                tx1d_wide(&mut w, n, false);
                assert_eq!(w.iter().map(|&v| v as i32).collect::<Vec<_>>(), buf);
            }
        }
    }

    #[test]
    fn adst_is_sine_basis() {
        // VP9's ADST (for n = 8, 16) is a DST-IV variant: out[x] ~ sum in[k] sin(pi (2x+1)(2k+1) / 4n).
        for n in [8usize, 16] {
            for k in 0..n {
                let mut buf = vec![0i32; n];
                buf[k] = 1000;
                tx1d_narrow(&mut buf, n, true);
                let r: Vec<f64> = (0..n)
                    .map(|x| 1000.0 * ((2 * x + 1) as f64 * (2 * k + 1) as f64 * std::f64::consts::PI / (4 * n) as f64).sin() * 2f64.sqrt() / 2f64.sqrt())
                    .collect();
                for (a, b) in buf.iter().zip(&r) {
                    assert!((*a as f64 - b).abs() <= 4.0, "n {n} k {k}: {buf:?} vs {r:?}");
                }
            }
        }
        // ADST4 is the sine transform sin(pi (x+1)(2k+1) / 9) scaled.
        for k in 0..4 {
            let mut buf = vec![0i32; 4];
            buf[k] = 1000;
            tx1d_narrow(&mut buf, 4, true);
            let r: Vec<f64> =
                (0..4).map(|x| 1000.0 * (std::f64::consts::PI * (x + 1) as f64 * (2 * k + 1) as f64 / 9.0).sin() * 2.0 * 2f64.sqrt() / 3.0).collect();
            for (a, b) in buf.iter().zip(&r) {
                assert!((*a as f64 - b).abs() <= 3.0, "k {k}: {buf:?} vs {r:?}");
            }
        }
    }

    #[test]
    fn dc_only_shortcut_matches_full_transform() {
        for tx in 0..4u8 {
            let n = 4usize << tx;
            for dc in [-5000, -37, -1, 1, 2, 57, 1234, 9999] {
                let mut coefs = vec![0i32; n * n];
                coefs[0] = dc;
                let mut a = vec![128u16; n * n];
                let mut b = vec![128u16; n * n];
                inverse_transform_add(&coefs, tx, DCT_DCT, false, 1, 1, 8, &mut a, n);
                inverse_transform_add(&coefs, tx, DCT_DCT, false, 2, n, 8, &mut b, n);
                assert_eq!(a, b, "tx {tx} dc {dc}");
            }
        }
    }

    #[test]
    fn wht_round_trip_identity() {
        // A single DC coefficient of 4 * v spreads v / 4 ... check invertibility on a simple case:
        // input [4, 0, ..] (after the >> 2 pre-scaling a unit impulse).
        let mut coefs = [0i32; 16];
        coefs[0] = 4 * 4;
        let mut dst = [100u16; 16];
        inverse_transform_add(&coefs, 0, DCT_DCT, true, 1, 4, 8, &mut dst, 4);
        let sum: i32 = dst.iter().map(|&v| v as i32 - 100).sum();
        assert_eq!(sum, 16);
    }
}
