//! Sequence evaluation and the CPU reference compositor.
//!
//! [`render_sequence`] turns (project, sequence, time, scale) into a premultiplied linear-light
//! image. Tracks composite bottom (V1) to top. Per item: fetch the source frame at the mapped media
//! time (already reduced for low-resolution playback), run standard effects, then the fixed effects
//! (Motion → Opacity/blend), then composite. Transitions combine outgoing/incoming layers.
//!
//! The same code renders monitors, thumbnails and exports, and is the oracle for the GPU path.

#![cfg_attr(not(test), deny(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::unimplemented, clippy::todo, clippy::unreachable))]

pub mod audio;
pub mod audio_fx;
pub mod blend;
pub mod color_match;
pub mod colorman;
pub mod effects;
pub mod gpufx;
pub mod graphic_clip;
pub mod graphics;
pub mod image;
pub mod lumetri_presets;
pub mod luts;
pub mod mask;
pub mod mixer;
pub mod multicam;
pub mod offline;
pub mod plan;
pub mod preview;
pub mod remix;
pub mod scene;
pub mod track;
pub mod transitions;
pub mod vfx;

use std::sync::Arc;

use lumiere_edit_geom::{Affine, Vec2};
use lumiere_edit_media::{FrameRequest, SharedSource};
use lumiere_edit_project::{ItemId, ItemKind, ParamValue, Project, Sequence, TrackItem};
use lumiere_edit_time::{Tick, TimeDisplay, format_time};

pub use blend::Blend;
pub use image::Image;

/// Resolves project items to media sources (the engine owns the media pool).
pub trait SourceProvider: Sync {
    fn source(&self, item: ItemId) -> Option<SharedSource>;
}

impl<F: Fn(ItemId) -> Option<SharedSource> + Sync> SourceProvider for F {
    fn source(&self, item: ItemId) -> Option<SharedSource> {
        self(item)
    }
}

#[derive(Clone, Copy, Debug)]
pub struct RenderOptions {
    /// Output scale relative to the sequence frame size (1.0 = full, 0.5 = ½ resolution…).
    pub scale: f32,
    /// Skip standard effects (fast scrubbing / "Toggle Effects" off).
    pub effects: bool,
    /// Nesting depth guard.
    pub depth: u32,
    /// Draw the sequence's visible caption tracks over the picture (Program monitor, burn-in on
    /// export). Never applies to nested sequences.
    pub captions: bool,
    /// Return the image in the sequence's working space (HDR exports, scopes) instead of
    /// converting it for an SDR monitor. Only matters for HDR / wide-gamut sequences.
    pub working_output: bool,
}

impl Default for RenderOptions {
    fn default() -> Self {
        Self { scale: 1.0, effects: true, depth: 0, captions: false, working_output: false }
    }
}

/// Output size for a sequence at a scale.
pub fn output_size(seq: &Sequence, scale: f32) -> (usize, usize) {
    (((seq.settings.width as f32 * scale).round() as usize).max(1), ((seq.settings.height as f32 * scale).round() as usize).max(1))
}

/// Render sequence `seq_id` at timeline time `t`.
pub fn render_sequence(project: &Project, seq_id: ItemId, t: Tick, opts: RenderOptions, sources: &dyn SourceProvider) -> Image {
    let Some(seq) = project.sequence(seq_id) else { return Image::new(1, 1) };
    render_seq(project, seq, t, opts, sources)
}

fn render_seq(project: &Project, seq: &Sequence, t: Tick, opts: RenderOptions, sources: &dyn SourceProvider) -> Image {
    render_seq_tracks(project, seq, t, opts, sources, None)
}

/// Render a sequence, or only its video track `only` (a multi-camera angle; drawn even when the
/// track's output is off).
pub(crate) fn render_seq_tracks(project: &Project, seq: &Sequence, t: Tick, opts: RenderOptions, sources: &dyn SourceProvider, only: Option<usize>) -> Image {
    let (w, h) = output_size(seq, opts.scale);
    let mut canvas = Image::new(w, h);
    if opts.depth > 8 {
        return canvas;
    }
    let tc = format_time(t, seq.settings.frame_rate, seq.settings.drop_frame, TimeDisplay::Timecode, seq.settings.sample_rate as i64);
    for (ti, track) in seq.video_tracks.iter().enumerate() {
        // A cancelled frame job (playback moved on) stops here; its result is discarded.
        if lumiere_edit_media::cancel::cancelled() {
            return canvas;
        }
        if only.is_some_and(|o| o != ti) || (!track.enabled && only.is_none()) {
            continue;
        }
        // transition covering t?
        if let Some(tr) = track.transitions.iter().find(|tr| tr.range().contains(t)) {
            let a = tr.from.and_then(|id| track.item(id)).filter(|i| i.enabled);
            let b = tr.to.and_then(|id| track.item(id)).filter(|i| i.enabled);
            let la = a
                .and_then(|i| item_layer(project, seq, i, t, opts, sources, &tc))
                .map(|(img, op, _)| with_opacity(img, op))
                .unwrap_or_else(|| Image::new(w, h));
            let lb = b
                .and_then(|i| item_layer(project, seq, i, t, opts, sources, &tc))
                .map(|(img, op, _)| with_opacity(img, op))
                .unwrap_or_else(|| Image::new(w, h));
            let p = tr.progress(t) as f32;
            // Reverse plays the transition backwards (e.g. an iris closing on the outgoing clip)
            // while still going from A to B.
            let mixed = if tr.reverse {
                transitions::apply_scaled(&tr.effect, &lb, &la, 1.0 - p, opts.scale)
            } else {
                transitions::apply_scaled(&tr.effect, &la, &lb, p, opts.scale)
            };
            blend::composite(&mut canvas, &mixed, 1.0, Blend::Normal);
            continue;
        }
        let Some(item) = track.item_at(t) else { continue };
        if !item.enabled {
            continue;
        }
        let pi = project.item(item.item);
        if let Some(pi) = pi
            && matches!(pi.kind, ItemKind::AdjustmentLayer { .. })
        {
            if !opts.effects {
                continue;
            }
            let mt = item.effect_time_at(t);
            let mut adjusted = canvas.clone();
            let cx = effects::FxCtx {
                t: mt,
                px_scale: opts.scale,
                seconds: (t - item.start).seconds(),
                timecode: &tc,
                clip_name: &item.name,
                project: Some(project),
                env: None,
                working: seq.settings.color.working,
            };
            for e in item.effects.iter().filter(|e| e.def().is_some_and(|d| !d.intrinsic)) {
                mask::apply_effect(&mut adjusted, e, &cx);
            }
            let (op, bl) = opacity_blend(item, mt);
            // Adjustment layer opacity (and opacity masks, in sequence pixels) mix adjusted over original.
            let mut out = canvas.clone();
            let mut adj = adjusted;
            adj.scale_alpha(op);
            mask::apply_opacity_masks(&mut adj, item, mt, opts.scale);
            // Motion moves / scales the adjustment layer's frame: it only applies inside it.
            if let Some(region) = adjustment_region(seq, item, project, mt, opts.scale, w, h) {
                let cov: Vec<f32> = region.px.as_chunks::<4>().0.iter().map(|p| p[3]).collect();
                mask::scale_by(&mut adj, &cov);
            }
            blend::composite(&mut out, &adj, 1.0, bl);
            canvas = out;
            continue;
        }
        if let Some((layer, op, bl)) = item_layer(project, seq, item, t, opts, sources, &tc) {
            blend::composite(&mut canvas, &layer, op, bl);
        }
    }
    if opts.depth == 0 && !opts.working_output {
        colorman::to_display(&mut canvas, &seq.settings.color);
    }
    if opts.captions && opts.depth == 0 {
        for o in caption_overlays(seq, t, w, h) {
            o.composite_onto(&mut canvas.px, w, h);
        }
    }
    canvas
}

/// Coverage of an adjustment layer's (Motion-transformed) frame in output pixels, or `None` when
/// it covers the whole output.
fn adjustment_region(seq: &Sequence, item: &TrackItem, project: &Project, mt: Tick, scale: f32, w: usize, h: usize) -> Option<Image> {
    let size = source_size(project, item.item).unwrap_or((seq.settings.width, seq.settings.height));
    let motion = motion_matrix(seq, item, size, mt);
    let s = scale as f64;
    let (fw, fh) = (((size.0 as f64 * s).round() as usize).max(1), ((size.1 as f64 * s).round() as usize).max(1));
    let m = Affine::scale(s, s).then_apply(&motion).then_apply(&Affine::scale(1.0 / s, 1.0 / s));
    let full = fw == w
        && fh == h
        && (m.a - 1.0).abs() < 1e-9
        && (m.d - 1.0).abs() < 1e-9
        && m.b.abs() < 1e-12
        && m.c.abs() < 1e-12
        && m.e.abs() < 1e-9
        && m.f.abs() < 1e-9;
    if full {
        return None;
    }
    Some(Image::filled(fw, fh, [1.0; 4]).transformed(w, h, &m))
}

/// Rendered captions of the visible caption tracks at `t` for a `w`×`h` output.
pub fn caption_overlays(seq: &Sequence, t: Tick, w: usize, h: usize) -> Vec<lumiere_edit_captions::burn::Overlay> {
    if seq.caption_tracks.is_empty() {
        return Vec::new();
    }
    lumiere_edit_captions::burn::sequence_overlays(seq, t, w, h)
}

fn with_opacity(mut img: Image, op: f32) -> Image {
    img.scale_alpha(op);
    img
}

pub(crate) fn opacity_blend(item: &TrackItem, mt: Tick) -> (f32, Blend) {
    match item.effect("opacity") {
        Some(e) if e.enabled => {
            let op = (e.f64_at("opacity", mt) / 100.0).clamp(0.0, 1.0) as f32;
            let bl = match e.param("blend").map(|p| &p.value) {
                Some(ParamValue::Choice(c)) => Blend::from_index(*c),
                _ => Blend::Normal,
            };
            (op, bl)
        }
        _ => (1.0, Blend::Normal),
    }
}

/// Size of an item's source at full resolution.
pub fn source_size(project: &Project, item: ItemId) -> Option<(u32, u32)> {
    match &project.item(item)?.kind {
        ItemKind::Media(m) => m.info.video.as_ref().map(|v| (v.width, v.height)),
        ItemKind::Sequence(s) => Some((s.settings.width, s.settings.height)),
        ItemKind::AdjustmentLayer { width, height, .. } | ItemKind::Graphic { width, height, .. } => Some((*width, *height)),
        ItemKind::Subclip { parent, .. } => source_size(project, *parent),
    }
}

/// The Motion transform of an item at media time `mt`, mapping full-res source pixels to
/// full-res sequence pixels.
pub fn motion_matrix(seq: &Sequence, item: &TrackItem, src: (u32, u32), mt: Tick) -> Affine {
    let (sw, sh) = (seq.settings.width as f64, seq.settings.height as f64);
    let mut pos = Vec2::new(sw / 2.0, sh / 2.0);
    let mut anchor = Vec2::new(src.0 as f64 / 2.0, src.1 as f64 / 2.0);
    let mut scale = Vec2::new(1.0, 1.0);
    let mut rot = 0.0;
    if let Some(m) = item.effect("motion").filter(|m| m.enabled) {
        let p = m.vec2_at("position", mt);
        if !p.x.is_nan() && m.param("position").is_some() {
            pos = p;
        }
        let a = m.vec2_at("anchor", mt);
        if !a.x.is_nan() && m.param("anchor").is_some() {
            anchor = a;
        }
        let s = m.f64_at("scale", mt) / 100.0;
        let uniform = m.param("uniform_scale").and_then(|p| p.value.as_bool()).unwrap_or(true);
        let swid = if uniform { s } else { m.f64_at("scale_width", mt) / 100.0 };
        scale = Vec2::new(swid, s);
        rot = m.f64_at("rotation", mt);
    }
    if item.scale_to_frame {
        let fit = (sw / src.0 as f64).min(sh / src.1 as f64);
        scale = scale * fit;
    }
    Affine::motion(pos, scale, rot, anchor)
}

/// Render one track item's layer at timeline time `t` into a canvas-sized image.
/// Returns (layer, opacity, blend).
pub(crate) fn item_layer(
    project: &Project,
    seq: &Sequence,
    item: &TrackItem,
    t: Tick,
    opts: RenderOptions,
    sources: &dyn SourceProvider,
    tc: &str,
) -> Option<(Image, f32, Blend)> {
    let (w, h) = output_size(seq, opts.scale);
    // `mt`: where effects and Motion/Opacity are evaluated (differs from the shown frame's media
    // time inside a frame hold without Hold Filters).
    let mt = item.effect_time_at(t);
    let src_size = source_size(project, item.item)?;
    let motion = motion_matrix(seq, item, src_size, mt);
    // How many output pixels one source pixel covers → request a reduced frame when possible.
    let lin = ((motion.a * motion.a + motion.b * motion.b).sqrt()).max((motion.c * motion.c + motion.d * motion.d).sqrt());
    let want = (lin * opts.scale as f64).clamp(1.0 / 64.0, 1.0) as f32;
    let pi = project.item(item.item)?;
    if matches!(pi.kind, ItemKind::Graphic { .. }) && !(item.has_opacity_masks() || opts.effects && item.has_standard_effects()) {
        // vectors straight to the output: no resampling, crisp at any Motion scale
        let m = Affine::scale(opts.scale as f64, opts.scale as f64).then_apply(&motion);
        let mut canvas = Image::new(w, h);
        graphic_clip::render_graphic(item, mt, src_size, &m, &mut canvas);
        let (op, bl) = opacity_blend(item, mt);
        return Some((canvas, op, bl));
    }
    let mut layer = base_layer(project, seq, item, t, opts, sources, want)?;
    let px_scale = layer.w as f32 / src_size.0.max(1) as f32;
    // layer px → source px → sequence px → output px
    let m = Affine::scale(opts.scale as f64, opts.scale as f64).then_apply(&motion).then_apply(&Affine::scale(1.0 / px_scale as f64, 1.0 / px_scale as f64));
    if opts.effects {
        let env = vfx::ItemEnv { project, seq, item, t, opts, sources, want, layer_size: (layer.w, layer.h), layer_to_output: m, tc };
        let cx = effects::FxCtx {
            t: mt,
            px_scale,
            seconds: (t - item.start).seconds(),
            timecode: tc,
            clip_name: &item.name,
            project: Some(project),
            env: Some(&env),
            working: seq.settings.color.working,
        };
        for e in item.effects.iter().filter(|e| e.def().is_some_and(|d| !d.intrinsic) && !lumiere_edit_project::graphic::is_layer(e)) {
            if lumiere_edit_media::cancel::cancelled() {
                return None;
            }
            mask::apply_effect(&mut layer, e, &cx);
        }
    }
    mask::apply_opacity_masks(&mut layer, item, mt, px_scale);
    let placed = if layer.w == w
        && layer.h == h
        && (m.a - 1.0).abs() < 1e-9
        && (m.d - 1.0).abs() < 1e-9
        && m.b == 0.0
        && m.c == 0.0
        && m.e.abs() < 1e-9
        && m.f.abs() < 1e-9
    {
        layer
    } else {
        layer.transformed(w, h, &m)
    };
    let (op, bl) = opacity_blend(item, mt);
    Some((placed, op, bl))
}

/// A track item's picture at timeline `t` before any effect (decoded, colour managed, frame
/// blended; nested sequences rendered; graphics rasterised), at `want` of its source size.
pub(crate) fn base_layer(
    project: &Project,
    seq: &Sequence,
    item: &TrackItem,
    t: Tick,
    opts: RenderOptions,
    sources: &dyn SourceProvider,
    want: f32,
) -> Option<Image> {
    // `ft`: the media time of the frame shown
    let ft = item.source_time_at(t);
    let mt = item.effect_time_at(t);
    let src_size = source_size(project, item.item)?;
    let pi = project.item(item.item)?;
    Some(match &pi.kind {
        ItemKind::Media(_) | ItemKind::Subclip { .. } => {
            let src = sources.source(item.item)?;
            let frame = src.video_frame(FrameRequest { time: ft, scale: want }).ok()?;
            let n = decimation(frame.width as f32, src_size.0 as f32 * want);
            let img = colorman::decode(project, item.item, &frame, n, &seq.settings.color);
            match interpolation_blend(item, t, src.info().frame_rate()) {
                Some((next_time, wgt)) => match src.video_frame(FrameRequest { time: next_time, scale: want }) {
                    Ok(f2) if f2.width == frame.width && f2.height == frame.height => {
                        let b = colorman::decode(project, item.item, &f2, n, &seq.settings.color);
                        img.lerp(&b, wgt)
                    }
                    _ => img,
                },
                None => img,
            }
        }
        ItemKind::Sequence(nested) => {
            let sub = RenderOptions { scale: want, effects: opts.effects, depth: opts.depth + 1, captions: false, working_output: true };
            match item.multicam_angle(nested) {
                // a multi-camera clip shows its angle's track only (nothing for an audio-only angle)
                Some(angle) => match nested.angle_video_track_index(angle) {
                    Some(ti) => render_seq_tracks(project, nested, ft, sub, sources, Some(ti)),
                    None => {
                        let (nw, nh) = output_size(nested, want);
                        Image::new(nw, nh)
                    }
                },
                None => render_seq(project, nested, ft, sub, sources),
            }
        }
        ItemKind::AdjustmentLayer { .. } => return None,
        ItemKind::Graphic { .. } => {
            // standard effects work on the graphic at source resolution, then Motion places it
            let (gw, gh) = (((src_size.0 as f32 * want).ceil() as usize).max(1), ((src_size.1 as f32 * want).ceil() as usize).max(1));
            let mut img = Image::new(gw, gh);
            graphic_clip::render_graphic(item, mt, src_size, &Affine::scale(want as f64, want as f64), &mut img);
            img
        }
    })
}

/// The layer of one clip at timeline `t` with its effects applied, on a canvas the size of the
/// sequence output (transparent elsewhere), before opacity and blending. Used by Apply Match.
pub fn render_clip(
    project: &Project,
    seq_id: ItemId,
    clip: lumiere_edit_project::ClipId,
    t: Tick,
    opts: RenderOptions,
    sources: &dyn SourceProvider,
) -> Option<Image> {
    let seq = project.sequence(seq_id)?;
    let (_, item) = seq.find_item(clip)?;
    let tc = format_time(t, seq.settings.frame_rate, seq.settings.drop_frame, TimeDisplay::Timecode, seq.settings.sample_rate as i64);
    item_layer(project, seq, item, t, opts, sources, &tc).map(|(img, _, _)| img)
}

/// Frame Blending / Optical Flow on a speed-changed clip: the later source frame to mix in and
/// its weight (0..1) at timeline `t`, or None when the exact media time falls on a source frame
/// (or the clip plays at 100 %, is frame-held, or uses Frame Sampling).
///
/// TODO(optical flow): motion-compensated interpolation; Optical Flow renders as Frame Blending.
pub fn interpolation_blend(item: &TrackItem, t: Tick, src_rate: lumiere_edit_time::FrameRate) -> Option<(Tick, f32)> {
    if item.time_interpolation == lumiere_edit_project::TimeInterpolation::FrameSampling || item.frame_hold.is_some() {
        return None;
    }
    if (item.speed.abs() - 1.0).abs() < 1e-9 {
        return None;
    }
    let mt = item.source_time_at(t);
    if src_rate.frame_duration().0 <= 0 {
        return None;
    }
    // position in source frames from the media origin
    let f = src_rate.frame_at(mt);
    let f0 = src_rate.tick_of(f);
    let frac = (mt - f0).0 as f64 / (src_rate.tick_of(f + 1) - f0).0.max(1) as f64;
    if frac <= 1e-6 {
        return None;
    }
    // the mix depends only on the media position, so reversed clips blend the same pair
    Some((src_rate.tick_of(f + 1), frac.clamp(0.0, 1.0) as f32))
}

/// Largest power-of-two box decimation that keeps at least `target_w` pixels of width.
pub(crate) fn decimation(have_w: f32, target_w: f32) -> usize {
    let mut n = 1usize;
    while n < 16 && have_w / (n as f32 * 2.0) >= target_w.max(1.0) {
        n *= 2;
    }
    n
}

/// Render a single project item (e.g. for the Source monitor) at media time `t`.
pub fn render_item(project: &Project, item: ItemId, t: Tick, scale: f32, sources: &dyn SourceProvider) -> Option<Image> {
    let pi = project.item(item)?;
    match &pi.kind {
        ItemKind::Sequence(s) => Some(render_seq(project, s, t, RenderOptions { scale, ..Default::default() }, sources)),
        _ => {
            let src = sources.source(item)?;
            let f = src.video_frame(FrameRequest { time: t, scale }).ok()?;
            let full_w = src.info().video.as_ref().map_or(f.width, |v| v.width) as f32;
            let n = decimation(f.width as f32, full_w * scale);
            // the Source monitor shows media as SDR Rec. 709 (log/HDR tone mapped per its colour space)
            Some(colorman::decode(project, item, &f, n, &lumiere_edit_color::ColorPipeline::REC709))
        }
    }
}

/// A shared, clonable source map for tests and simple hosts.
#[derive(Default, Clone)]
pub struct SourceMap(pub std::collections::HashMap<ItemId, SharedSource>);

impl SourceProvider for SourceMap {
    fn source(&self, item: ItemId) -> Option<SharedSource> {
        self.0.get(&item).cloned()
    }
}

pub fn arc_source(s: impl lumiere_edit_media::MediaSource + 'static) -> SharedSource {
    Arc::new(s)
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod lumetri_hdr_tests;

#[cfg(test)]
#[path = "adjustment_tests.rs"]
mod adjustment_tests;

#[cfg(test)]
#[path = "mixer_tests.rs"]
mod mixer_tests;

#[cfg(test)]
#[path = "preview_tests.rs"]
mod preview_tests;
