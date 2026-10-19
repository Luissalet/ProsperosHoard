//! Inter-frame bit-exactness against libdav1d (run as an external ffmpeg process): SVT-AV1
//! GOPs with hierarchical references, compound prediction, OBMC / warped motion, global motion,
//! motion-field projection and all loop filters.

mod common;
use common::*;

fn specs() -> Vec<Spec> {
    let p = |preset: &'static str, crf: &'static str| -> Vec<&'static str> { vec!["-preset", preset, "-crf", crf] };
    vec![
        Spec::new("inter_testsrc_p8", "testsrc2=s=320x240:r=25", 10, "yuv420p", &p("8", "35")),
        Spec::new("inter_testsrc_p3", "testsrc2=s=352x288:r=25", 24, "yuv420p", &p("3", "30")),
        Spec::new("inter_mandel_10bit_p5", "mandelbrot=s=416x240:r=25", 16, "yuv420p10le", &p("5", "40")),
        Spec::new("inter_noise_odd_p6", "testsrc2=s=203x117:r=25,noise=alls=15:allf=t", 12, "yuv420p", &p("6", "45")),
    ]
}

/// Super-resolution (denominator 12 on the inter frames) and film grain synthesis.
fn specs_tools() -> Vec<Spec> {
    let p = |params: &'static str| -> Vec<&'static str> { vec!["-preset", "6", "-crf", "35", "-svtav1-params", params] };
    vec![
        Spec::new("inter_superres", "testsrc2=s=416x240:r=25", 8, "yuv420p", &p("superres-mode=1:superres-denom=12:superres-kf-denom=13")),
        Spec::new("inter_film_grain", "testsrc2=s=352x288:r=25,noise=alls=20:allf=t", 8, "yuv420p", &p("film-grain=10")),
    ]
}

#[test]
fn inter_superres_and_grain() {
    let Some(ff) = ffmpeg() else { return };
    for s in specs_tools() {
        check_bit_exact(&ff, &s);
    }
}

#[test]
fn inter_gops() {
    let Some(ff) = ffmpeg() else { return };
    for s in specs() {
        check_bit_exact(&ff, &s);
    }
}

/// Several tiles (decoded in parallel into private buffers, then merged).
fn specs_tiles() -> Vec<Spec> {
    let p = |preset: &'static str, params: &'static str| -> Vec<&'static str> { vec!["-preset", preset, "-crf", "32", "-svtav1-params", params] };
    vec![
        Spec::new("inter_tiles_4x2", "testsrc2=s=1280x720:r=25,noise=alls=4:allf=t", 8, "yuv420p", &p("8", "tile-columns=2:tile-rows=1")),
        Spec::new("inter_tiles_odd_10bit", "mandelbrot=s=650x370:r=25", 8, "yuv420p10le", &p("6", "tile-columns=1:tile-rows=1")),
    ]
}

#[test]
fn inter_tiles() {
    let Some(ff) = ffmpeg() else { return };
    for s in specs_tiles() {
        check_bit_exact(&ff, &s);
    }
}

/// The 1080p performance fixtures (slow; part of the extended run).
#[test]
#[ignore]
fn inter_1080p() {
    let Some(ff) = ffmpeg() else { return };
    let p = |crf: &'static str, params: &'static str| -> Vec<&'static str> { vec!["-preset", "8", "-crf", crf, "-svtav1-params", params] };
    let src = "testsrc2=s=1920x1080:r=25,noise=alls=6:allf=t";
    for s in [
        Spec::new("perf_1080p_gop", src, 60, "yuv420p", &p("30", "keyint=60")),
        Spec::new("perf_1080p_gop_10bit", src, 60, "yuv420p10le", &p("30", "keyint=60")),
        Spec::new("perf_1080p_gop_hq", src, 60, "yuv420p", &p("18", "keyint=60")),
        Spec::new("perf_1080p_gop_tiles", src, 60, "yuv420p", &p("30", "keyint=60:tile-columns=2:tile-rows=1")),
    ] {
        check_bit_exact(&ff, &s);
    }
}
