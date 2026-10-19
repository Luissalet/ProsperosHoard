//! Multi-camera rendering: one angle of a multi-camera source sequence, and the Multi-Camera
//! view's grid of angles.
//!
//! The grid renders every shown angle at a reduced scale (sources are asked for small frames, so
//! YUV→RGB conversion and compositing run at the cell size) in parallel, and tiles them into one
//! image. A 2×2 grid of 1080p angles at ¼ scale costs about as much as one ½-resolution frame of
//! compositing plus the four decodes.

use lumiere_codecbase_project::{ItemId, Project, Sequence};
use lumiere_codecbase_time::Tick;
use rayon::prelude::*;

use crate::{Image, RenderOptions, SourceProvider, output_size};

/// Grid layout for `n` angles: (columns, rows). 1 → 1×1, 2–4 → 2×2, 5–9 → 3×3, 10–16 → 4×4.
pub fn grid_dims(n: usize) -> (usize, usize) {
    let c = (1..=4).find(|c| c * c >= n).unwrap_or(4);
    let r = n.div_ceil(c).max(1);
    (c, r.min(c))
}

/// Render angle `angle` of the multi-camera source sequence `seq` at its time `t` (transparent
/// for an audio-only angle). The image is in the sequence's working space unless `opts.depth == 0`
/// and `!opts.working_output` (then display-ready like [`crate::render_sequence`]).
pub fn render_angle(project: &Project, seq: &Sequence, angle: usize, t: Tick, opts: RenderOptions, sources: &dyn SourceProvider) -> Image {
    match seq.angle_video_track_index(angle) {
        Some(ti) => crate::render_seq_tracks(project, seq, t, opts, sources, Some(ti)),
        None => {
            let (w, h) = output_size(seq, opts.scale);
            Image::new(w, h)
        }
    }
}

/// The Multi-Camera view's angle grid of the sequence `item` used as a multi-camera source at its time `t`: the shown
/// angles (Edit Cameras), each at `cell_scale` of the sequence frame size, tiled left to right,
/// top to bottom over black. Returns the image and the angles in grid order.
pub fn render_grid(project: &Project, item: ItemId, t: Tick, cell_scale: f32, sources: &dyn SourceProvider) -> Option<(Image, Vec<usize>)> {
    let seq = project.sequence(item)?;
    let angles = seq.cameras().shown_angles();
    let (cols, rows) = grid_dims(angles.len().max(1));
    let (cw, ch) = output_size(seq, cell_scale);
    let opts = RenderOptions { scale: cell_scale, ..Default::default() };
    let cells: Vec<Image> = angles.par_iter().map(|&a| render_angle(project, seq, a, t, opts, sources)).collect();
    let mut out = Image::filled(cw * cols, ch * rows, [0.0, 0.0, 0.0, 1.0]);
    for (k, cell) in cells.iter().enumerate().take(cols * rows) {
        let (x0, y0) = ((k % cols) * cw, (k / cols) * ch);
        for y in 0..ch.min(cell.h) {
            for x in 0..cw.min(cell.w) {
                let s = (y * cell.w + x) * 4;
                let d = ((y0 + y) * out.w + x0 + x) * 4;
                // cells over black: premultiplied over an opaque background
                let a = cell.px[s + 3];
                for c in 0..3 {
                    out.px[d + c] = cell.px[s + c] + out.px[d + c] * (1.0 - a);
                }
            }
        }
    }
    Some((out, angles))
}
