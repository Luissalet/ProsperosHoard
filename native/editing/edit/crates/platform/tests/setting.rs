//! Settings ▸ Playback ▸ Hardware decoding through the decoder registry and the media stack:
//! Off gives the software decoder (and no hardware frames), Auto the hardware one where the
//! system has it. Its own test binary: the switch is process-wide.

mod common;

use common::*;
use lumiere_edit_media::FrameRequest;

#[test]
fn off_uses_the_software_decoder() {
    let ff = lumiere_edit_testkit::require_ffmpeg!();
    let Some(path) = named(&ff, "hevc_main.mp4") else { return };
    let s = read_stream(&path);
    let available = lumiere_edit_platform::register();
    assert_eq!(lumiere_edit_platform::register(), available, "registering twice is harmless");

    lumiere_edit_codecs::hw::set_hardware_decoding(false);
    let d = lumiere_edit_codecs::make_video_decoder(&s.entry).unwrap();
    assert_eq!(d.name(), "Lumiere Edit HEVC");
    let bytes: std::sync::Arc<[u8]> = std::fs::read(&path).unwrap().into();
    let (g0, h0) = (lumiere_edit_codecs::gop_stats(), lumiere_edit_codecs::hw::hw_stats());
    let src = lumiere_edit_codecs::open_bytes("hevc_main.mp4", bytes.clone()).unwrap();
    let rate = src.info().frame_rate();
    for i in 0..30 {
        src.video_frame(FrameRequest::full(rate.tick_of(i))).unwrap();
    }
    assert!(lumiere_edit_codecs::gop_stats().frames > g0.frames, "frames counted");
    assert_eq!(lumiere_edit_codecs::hw::hw_stats().frames, h0.frames, "Off: no hardware frames");

    lumiere_edit_codecs::hw::set_hardware_decoding(true);
    let d = lumiere_edit_codecs::make_video_decoder(&s.entry).unwrap();
    let hw = lumiere_edit_platform::hardware_decoder_for(&s.entry);
    if !hw {
        eprintln!("SKIPPED (Auto half): no hardware decoder ({available:?})");
        assert_eq!(d.name(), "Lumiere Edit HEVC");
        return;
    }
    assert_eq!(d.name(), "VideoToolbox HEVC");
    let h1 = lumiere_edit_codecs::hw::hw_stats();
    let src = lumiere_edit_codecs::open_bytes("hevc_main.mp4", bytes).unwrap();
    for i in 0..30 {
        src.video_frame(FrameRequest::full(rate.tick_of(i))).unwrap();
    }
    let h2 = lumiere_edit_codecs::hw::hw_stats();
    assert!(h2.frames > h1.frames && h2.sessions > h1.sessions, "Auto: hardware frames {h1:?} → {h2:?}");
}
