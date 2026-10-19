//! Colour management in the renderer: source colour space resolution (Interpret Footage override
//! or stream metadata), source → working conversion of decoded frames, and the working → monitor
//! transform. The maths lives in `lumiere_codecbase_color::transform`.
//!
//! A sequence whose pipeline is "plain" (Rec. 709 working space without wide gamut) and media
//! whose metadata says Rec. 709 / sRGB / linear take the original fast path unchanged.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use lumiere_codecbase_color::{ColorPipeline, ColorSpace, InputTransform, OutputTransform, Range};
use lumiere_codecbase_frame::VideoFrame;
use lumiere_codecbase_project::{ItemId, ItemKind, Project};

use crate::Image;

/// The Interpret Footage colour-space override of an item (following subclips to their media).
pub fn override_of(project: &Project, item: ItemId) -> Option<ColorSpace> {
    match &project.item(item)?.kind {
        ItemKind::Media(m) => m.interpret.color_space,
        ItemKind::Subclip { parent, .. } => override_of(project, *parent),
        _ => None,
    }
}

/// The colour space a frame of `item` is interpreted in.
pub fn source_space(project: &Project, item: ItemId, frame: &VideoFrame) -> ColorSpace {
    override_of(project, item).unwrap_or_else(|| ColorSpace::from_info(&frame.color))
}

/// Whether the frame needs more than the default (transfer-only) decode.
pub fn needs_management(pipe: &ColorPipeline, cs: ColorSpace, frame: &VideoFrame) -> bool {
    let default_decode = cs == ColorSpace::from_info(&frame.color) && matches!(cs, ColorSpace::Rec709 | ColorSpace::Srgb | ColorSpace::Linear709);
    !(pipe.is_plain() && default_decode)
}

type Key = (ColorSpace, Range, ColorPipeline);

/// A cached input transform.
pub fn input_transform(cs: ColorSpace, range: Range, pipe: &ColorPipeline) -> Arc<InputTransform> {
    static C: OnceLock<Mutex<HashMap<Key, Arc<InputTransform>>>> = OnceLock::new();
    let c = C.get_or_init(Default::default);
    let key = (cs, range, *pipe);
    if let Some(t) = c.lock().unwrap_or_else(|e| e.into_inner()).get(&key) {
        return t.clone();
    }
    let t = Arc::new(InputTransform::new(cs, range, pipe, None));
    c.lock().unwrap_or_else(|e| e.into_inner()).insert(key, t.clone());
    t
}

/// Decode a frame of `item` (box-decimated by `n`) into the working space of `pipe`.
pub fn decode(project: &Project, item: ItemId, frame: &VideoFrame, n: usize, pipe: &ColorPipeline) -> Image {
    let cs = source_space(project, item, frame);
    if !needs_management(pipe, cs, frame) {
        let (w, h, px) = frame.to_linear_f32_decimated(n);
        return Image { w, h, px };
    }
    // RGB stills/generators carry full-range code values; YUV carries its own range flag
    let range = match frame.data {
        lumiere_codecbase_frame::PixelData::Rgba8(_) | lumiere_codecbase_frame::PixelData::RgbaF32(_) => Range::Full,
        _ => frame.color.range,
    };
    let t = input_transform(cs, range, pipe);
    let float_src = matches!(frame.data, lumiere_codecbase_frame::PixelData::RgbaF32(_));
    // float frames are linear already: only the gamut/tone stages apply to them
    let (w, h, px) = frame.to_linear_f32_decimated_with(n, if float_src { None } else { Some(&t.table) });
    let mut img = Image { w, h, px };
    if t.has_pixel_stage() {
        img.map_rgb(|c, _, _| t.apply(c));
    }
    img
}

/// Working space → monitor (SDR BT.709) for a non-plain pipeline; no-op otherwise.
pub fn to_display(img: &mut Image, pipe: &ColorPipeline) {
    if pipe.is_plain() {
        return;
    }
    let o = display_transform(pipe);
    if o.is_identity() {
        return;
    }
    img.map_rgb(|c, _, _| o.apply(c));
}

pub fn display_transform(pipe: &ColorPipeline) -> Arc<OutputTransform> {
    static C: OnceLock<Mutex<HashMap<ColorPipeline, Arc<OutputTransform>>>> = OnceLock::new();
    let c = C.get_or_init(Default::default);
    c.lock().unwrap_or_else(|e| e.into_inner()).entry(*pipe).or_insert_with(|| Arc::new(OutputTransform::display(pipe))).clone()
}

#[cfg(test)]
mod tests {
    use super::*;
    use lumiere_codecbase_color::{ColorInfo, Primaries, Transfer, WorkingSpace};

    fn frame(rgb: [u8; 3], color: ColorInfo) -> VideoFrame {
        let mut f = VideoFrame::rgba8(2, 2, [rgb[0], rgb[1], rgb[2], 255].repeat(4));
        f.color = color;
        f
    }

    #[test]
    fn plain_media_keeps_the_fast_path() {
        let f = frame([200, 100, 50], ColorInfo::SRGB_FULL);
        assert!(!needs_management(&ColorPipeline::REC709, ColorSpace::Srgb, &f));
        let hdr = ColorPipeline { working: WorkingSpace::Rec2100Pq, ..ColorPipeline::REC709 };
        assert!(needs_management(&hdr, ColorSpace::Srgb, &f));
        let pq = ColorInfo { transfer: Transfer::Pq, primaries: Primaries::Bt2020, ..ColorInfo::SRGB_FULL };
        assert!(needs_management(&ColorPipeline::REC709, ColorSpace::from_info(&pq), &frame([1, 2, 3], pq)));
    }

    #[test]
    fn pq_frame_is_tone_mapped_into_rec709() {
        let p = Project::new("t");
        let pq = ColorInfo { transfer: Transfer::Pq, primaries: Primaries::Bt2020, ..ColorInfo::SRGB_FULL };
        // ~1000 cd/m² white (PQ 0.75) → SDR peak after BT.2390; 203 cd/m² (PQ 0.58) → ≈ 0.8
        let hi = decode(&p, ItemId(99), &frame([192, 192, 192], pq), 1, &ColorPipeline::REC709);
        assert!((hi.px[0] - 1.0).abs() < 0.03, "{:?}", &hi.px[..4]);
        let rw = decode(&p, ItemId(99), &frame([148, 148, 148], pq), 1, &ColorPipeline::REC709);
        assert!((0.7..0.9).contains(&rw.px[0]), "{:?}", &rw.px[..4]);
        // the same frame in a PQ sequence keeps its HDR values (working units, 1.0 = 203 cd/m²)
        let hdr = ColorPipeline { working: WorkingSpace::Rec2100Pq, ..ColorPipeline::REC709 };
        let w = decode(&p, ItemId(99), &frame([192, 192, 192], pq), 1, &hdr);
        assert!(w.px[0] > 4.0, "{:?}", &w.px[..4]);
        let mut d = w.clone();
        to_display(&mut d, &hdr);
        assert!((d.px[0] - 1.0).abs() < 0.03);
    }
}
