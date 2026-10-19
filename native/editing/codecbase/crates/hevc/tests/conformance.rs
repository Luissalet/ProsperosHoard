//! Bit-exactness against ffmpeg's decoder on libx265 / VideoToolbox fixtures (skipped when ffmpeg is
//! absent).

mod common;

macro_rules! fixture_tests {
    ($($name:ident),* $(,)?) => {
        $(
            #[test]
            fn $name() {
                match common::check_fixture(stringify!($name)) {
                    Ok(true) => {}
                    Ok(false) => eprintln!("skipped {}", stringify!($name)),
                    Err(e) => panic!("{e}"),
                }
            }
        )*
    };
}

fixture_tests!(
    intra_main,
    intra_main10,
    intra_nosao_nodbk,
    ultrafast,
    veryfast,
    medium,
    slow,
    medium_main10,
    slow_main10,
    p_only,
    bframes,
    no_sao,
    no_deblock,
    deblock_m3_3,
    no_wpp,
    rect_amp,
    tskip,
    weightp,
    weightp_main10,
    crf0,
    crf51,
    qp1_main10,
    lossless,
    cu_lossless,
    slices4,
    scaling_list,
    ctu16,
    ctu32_tu4,
    min_cu16,
    no_signhide,
    constrained_intra,
    no_tmvp,
    no_strong_intra,
    max_merge1,
    open_gop,
    keyint5_idr,
    odd_1918x1078,
    hd_1080p,
    uhd_4k_main10,
    vt_main,
    vt_main10,
);

/// Print which coding tools each fixture exercises (run with `-- --ignored --nocapture`).
#[test]
#[ignore]
fn coverage_report() {
    for f in common::FIXTURES {
        let Some((hevc, _)) = common::ensure(f) else { continue };
        let data = std::fs::read(hevc).unwrap();
        let mut dec = lumiere_codecbase_hevc::Decoder::new();
        for au in common::split_access_units(&data) {
            let _ = dec.decode(au, 0);
        }
        dec.flush();
        println!("{:<20} {:?}", f.name, dec.stats());
    }
}

/// The comparison must report a single flipped sample (guards against a vacuous harness).
#[test]
fn compare_detects_mismatch() {
    let f = common::fixture("intra_main10");
    let Some((hevc, yuv)) = common::ensure(f) else { return };
    let pics = common::decode_file_threads(&hevc, 1).map_err(|e| e.1).unwrap();
    let mut reference = std::fs::read(yuv).unwrap();
    common::compare(&pics, &reference, f.width as usize, f.height as usize, f.bit_depth).unwrap();
    let pos = reference.len() - 7;
    reference[pos] ^= 1;
    let err = common::compare(&pics, &reference, f.width as usize, f.height as usize, f.bit_depth).unwrap_err();
    assert!(err.contains("plane V"), "{err}");
}

/// Pre-generate every fixture and its reference decode (`cargo xtask fixtures`).
#[test]
#[ignore]
fn generate_fixtures() {
    let dir = common::fixtures_dir();
    for f in common::FIXTURES {
        let outs = [dir.join(format!("{}.hevc", f.name)), dir.join(format!("{}.yuv", f.name))];
        lumiere_codecbase_testkit::fixtures::generate_and_report(&format!("hevc/{}", f.name), &outs, || common::ensure(f));
    }
}
