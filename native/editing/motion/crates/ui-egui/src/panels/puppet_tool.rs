//! Puppet tools in the Composition viewer: the deformed mesh and pins of the selected layers'
//! Puppet effect, click to add a pin (`puppet.addPin`), click / Shift-click a pin to select it
//! (`puppet.selectPins`), drag a pin to move it (`puppet.movePin`), and drag an Advanced or Bend
//! pin's ring to rotate it or its square to scale it (`puppet.setPin`); each drag is one undo step.

use std::sync::Arc;

use lumiere_motion_engine::effects::puppet::{self, Mesh, Pin, PinKind};
use lumiere_motion_engine::geom::vec2 as gv2;
use lumiere_motion_engine::project::{ItemId, Layer, LayerId};
use lumiere_motion_engine::render::EvalCtx;
use lumiere_motion_engine::time::Tick;
use egui::{Color32, Pos2, Stroke};

use super::viewer::{ViewerMap, l2c};
use crate::LumiereMotionApp;

/// Deformed meshes of a layer's Puppet effect: (mesh uid, mesh, deformed vertices, pins).
pub type Overlay = Arc<Vec<(u64, Arc<Mesh>, Vec<[f64; 2]>, Vec<Pin>)>>;

/// Rotation ring radius (screen points) of Advanced and Bend pins.
pub const RING: f32 = 9.0;

/// A pin drawn this frame.
#[derive(Clone, Copy, Debug)]
pub struct PinHit {
    pub layer: LayerId,
    pub pin: u64,
    pub kind: PinKind,
    pub pos: Pos2,
    /// Rotation (°) and scale (1 = 100 %) now, for Advanced and Bend pins.
    pub rotation: f64,
    pub scale: f64,
    /// The scale handle on the ring (Advanced and Bend pins).
    pub handle: Option<Pos2>,
}

/// The part of a pin under the pointer.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PinPart {
    /// The pin itself (drag moves Position, Advanced, Starch and Overlap pins).
    Center,
    /// The rotation ring of an Advanced or Bend pin.
    Ring,
    /// The scale handle on that ring.
    Scale,
}

/// The pin part under `p`, topmost pin first.
pub fn hit(hits: &[PinHit], p: Pos2) -> Option<(PinHit, PinPart)> {
    hits.iter().rev().find_map(|h| {
        let d = h.pos.distance(p);
        match h.handle {
            Some(s) if s.distance(p) < 5.0 => Some((*h, PinPart::Scale)),
            Some(_) if d < 6.0 => Some((*h, PinPart::Center)),
            Some(_) if (d - RING).abs() < 4.0 => Some((*h, PinPart::Ring)),
            None if d < 8.0 => Some((*h, PinPart::Center)),
            _ => None,
        }
    })
}

#[derive(Clone)]
struct Cached {
    key: (u64, u64, u64, i64),
    overlay: Overlay,
}

/// The deformed mesh of `layer`'s Puppet effect at comp time `t` (cached per project revision).
pub fn overlay(app: &LumiereMotionApp, ctx: &egui::Context, cid: ItemId, layer: &Layer, t: Tick) -> Option<Overlay> {
    let fx = layer.effects()?.groups().find(|g| puppet::is_puppet(g))?;
    let key = (app.session.revision, cid.0, layer.id.0, t.0);
    let id = egui::Id::new(("puppet-overlay", layer.id.0));
    if let Some(c) = ctx.data(|d| d.get_temp::<Cached>(id))
        && c.key == key
    {
        return Some(c.overlay);
    }
    let (buf, params) = lumiere_motion_engine::commands::puppet::puppet_eval(&app.session, cid, layer.id, fx.uid, t)?;
    let overlay: Overlay = Arc::new(puppet::overlay(&buf, &params));
    ctx.data_mut(|d| d.insert_temp(id, Cached { key, overlay: overlay.clone() }));
    Some(overlay)
}

/// Where a pin sits now (layer space): its Position, or (Bend / Starch / Overlap pins) its rest
/// point carried along by the deformation.
pub fn pin_position(mesh: &Mesh, def: &[[f64; 2]], p: &Pin) -> [f64; 2] {
    if p.kind.moves() {
        return p.position;
    }
    let v = (0..mesh.verts.len()).min_by(|a, b| {
        let da = (mesh.verts[*a][0] - p.rest[0]).hypot(mesh.verts[*a][1] - p.rest[1]);
        let db = (mesh.verts[*b][0] - p.rest[0]).hypot(mesh.verts[*b][1] - p.rest[1]);
        da.total_cmp(&db)
    });
    match v {
        Some(v) => [def[v][0] + p.rest[0] - mesh.verts[v][0], def[v][1] + p.rest[1] - mesh.verts[v][1]],
        None => p.rest,
    }
}

/// Pin colours by kind: Position yellow, Starch red, Overlap blue, Advanced blue-green, Bend
/// orange-brown.
pub fn pin_color(kind: PinKind) -> Color32 {
    match kind {
        PinKind::Position => Color32::from_rgb(0xf0, 0xd0, 0x40),
        PinKind::Starch => Color32::from_rgb(0xe0, 0x50, 0x50),
        PinKind::Overlap => Color32::from_rgb(0x40, 0x90, 0xf0),
        PinKind::Advanced => Color32::from_rgb(0x30, 0xc8, 0xb4),
        PinKind::Bend => Color32::from_rgb(0xc8, 0x82, 0x3c),
    }
}

/// Draw the mesh (when `show_mesh`) and pins of `layer` (the pin under `hover` is drawn larger);
/// returns pin hit targets.
#[allow(clippy::too_many_arguments)]
pub fn draw(
    painter: &egui::Painter,
    map: &ViewerMap,
    ectx: &EvalCtx,
    layer: &Layer,
    ov: &Overlay,
    show_mesh: bool,
    selected: &[u64],
    hover: Option<Pos2>,
) -> Vec<PinHit> {
    let (m, _) = l2c(ectx, layer);
    let scr = |q: [f64; 2]| {
        let c = m.apply(gv2(q[0], q[1]));
        map.to_screen([c.x, c.y])
    };
    let mut hits = vec![];
    for (_, mesh, def, pins) in ov.iter() {
        if show_mesh {
            let st = Stroke::new(1.0, Color32::from_rgba_unmultiplied(0xf0, 0xd0, 0x40, 110));
            for t in &mesh.tris {
                let (a, b, c) = (scr(def[t[0]]), scr(def[t[1]]), scr(def[t[2]]));
                painter.line_segment([a, b], st);
                painter.line_segment([b, c], st);
                painter.line_segment([c, a], st);
            }
        }
        for pn in pins {
            let at = pin_position(mesh, def, pn);
            let pos = scr(at);
            let sel = selected.contains(&pn.uid);
            let col = pin_color(pn.kind);
            if matches!(pn.kind, PinKind::Starch | PinKind::Overlap) {
                // Extent ring (layer pixels → screen).
                let r = (scr([pn.rest[0] + pn.extent, pn.rest[1]]) - scr(pn.rest)).length();
                painter.circle_stroke(pos, r.max(2.0), Stroke::new(1.0, col.gamma_multiply(0.6)));
            }
            // Advanced and Bend pins: the rotation ring with a square scale handle at the pin's
            // rotation (the layer's own orientation on screen).
            let mut handle = None;
            if matches!(pn.kind, PinKind::Bend | PinKind::Advanced) {
                painter.circle_stroke(pos, RING, Stroke::new(1.5, col));
                let (s, c) = pn.rotation.to_radians().sin_cos();
                let dir = (scr([at[0] + c, at[1] + s]) - pos).normalized();
                if dir.x.is_finite() && dir.y.is_finite() {
                    let hp = pos + dir * RING;
                    painter.rect_filled(egui::Rect::from_center_size(hp, egui::vec2(5.0, 5.0)), 0.0, col);
                    painter.rect_stroke(
                        egui::Rect::from_center_size(hp, egui::vec2(5.0, 5.0)),
                        0.0,
                        Stroke::new(1.0, Color32::BLACK),
                        egui::StrokeKind::Outside,
                    );
                    handle = Some(hp);
                }
            }
            // Pins grow under the pointer.
            let r = if sel { 5.5 } else { 4.5 } + if hover.is_some_and(|h| h.distance(pos) < 8.0) { 1.5 } else { 0.0 };
            painter.circle_filled(pos, r, if sel { col } else { col.gamma_multiply(0.85) });
            painter.circle_stroke(pos, r, Stroke::new(1.0, Color32::BLACK));
            hits.push(PinHit { layer: layer.id, pin: pn.uid, kind: pn.kind, pos, rotation: pn.rotation, scale: pn.scale, handle });
        }
    }
    hits
}
